"""Many cheap agents versus one strong model: the production swarm harness.

The same four production questions can be asked four ways:

``SINGLE_STRONG``
    One frontier pass (``anthropic/claude-opus-5-5`` by default) with the full
    harness and cloud-retrieved evidence. The baseline arm.
``CHEAP_SWARM``
    Parallel fan-out over the cheap subscription models, aggregated by ``union``,
    ``majority_vote`` or ``citation_verified_union``.
``HYBRID_ADJUDICATOR``
    The cheap swarm proposes; cloud retrieval supplies matching historical issues
    and traces; one compact frontier call verifies citations, rejects false
    alarms, and writes the final selection.
``ADAPTIVE_CASCADE``
    The cheap swarm runs first and is accepted for free when every answering
    model agrees on a citation-verified finding at ``confidence >= threshold``;
    only disagreement or uncertainty escalates to the frontier model.

Work kinds are Task Quality Audit, Repair Proposal, Reward Seeking, and Trace
Adjudication; the mode chooses who does the work, never what the work is.

Invariants enforced here in code, not in prompts:

* Every call records ``requested_model``, ``served_model``, ``prompt_sha256``,
  ``response_sha256``, ``tokens_in``, ``tokens_out`` and ``latency_ms``.
* A response whose served model is not exactly the requested model - including a
  response that names no served model at all - is quarantined and never
  attributed to ``requested_model``. Its candidates never reach aggregation,
  adjudication, or a selection.
* ``CorpusRegistry.admit`` runs before the first model call, so a sealed TEST item
  (``tb4-audit-v1`` TEST, MiMo permanent heldouts) raises ``SplitLockError`` with
  zero tokens spent.
* Retrieved evidence is filtered through ``CorpusRegistry.sealed_identity``, so a
  historical record naming a sealed task never enters a prompt.
* Citations are checked against the workspace by this module. A model's claim that
  a file, line range, or quote exists is never taken on trust, and an
  adjudicator's corrected citation is re-checked exactly like a proposer's.

Nothing here executes a task, scores a reward, or admits a finding to the asset
store. It produces verified candidates plus an attested receipt that the live
workers and exporters consume.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from evallab.production.cloud_trace_db import CloudTraceDatabase
from evallab.production.corpus_queue import CorpusItem, CorpusRegistry

#: The frontier models a strong arm may be pinned to. A bare selector is ambiguous,
#: so every route is fully qualified and the served model is verified per call.
STRONG_MODELS = ("anthropic/claude-opus-5-5", "openai-codex/gpt-6.1-sol:max")
DEFAULT_STRONG_MODEL = "anthropic/claude-opus-5-5"

#: Cheap subscription routes the swarm fans out over, best first.
CHEAP_MODELS = (
    "kimi-code/k3.1-pro-highspeed",
    "meta/muse-wonder-1.0-contributor",
    "anthropic/claude-sonnet-5-5",
)

#: Two findings describe the same locus within this many lines, or when their
#: content tokens overlap at least this much (the ``swarm_bench`` convention).
LOCUS_WINDOW_LINES = 25
MECHANISM_OVERLAP = 0.35

#: Bounds that keep a fan-out and an adjudicator prompt small and cheap.
MAX_CANDIDATES_PER_MODEL = 5
MAX_ADJUDICATED_CANDIDATES = 12
MAX_RETRIEVAL_RECORDS = 8
MAX_RETRIEVAL_CHARS = 6_000
MAX_INLINE_PAYLOAD_CHARS = 2_000
MAX_ADJUDICATOR_CANDIDATE_CHARS = 6_000

EVENT_ROUND_COMPLETED = "swarm.round_completed"
EVENT_ADJUDICATED = "swarm.adjudicated"

_TOKEN_PATTERN = re.compile(r"[a-z][a-z0-9]*")
_JSON_FENCE = re.compile(r"```(?:json)?\s*([\[{].*?[\]}])\s*```", re.S)


class SwarmMode(StrEnum):
    """Who does the work: one frontier pass, a cheap swarm, or a cascade."""

    SINGLE_STRONG = "single_strong"
    CHEAP_SWARM = "cheap_swarm"
    HYBRID_ADJUDICATOR = "hybrid_adjudicator"
    ADAPTIVE_CASCADE = "adaptive_cascade"


class Aggregation(StrEnum):
    """How a cheap swarm's proposals become one answer."""

    UNION = "union"
    MAJORITY_VOTE = "majority_vote"
    CITATION_VERIFIED_UNION = "citation_verified_union"


class WorkKind(StrEnum):
    """The four production questions this harness can ask."""

    TASK_QUALITY_AUDIT = "task_quality_audit"
    REPAIR_PROPOSAL = "repair_proposal"
    REWARD_SEEKING = "reward_seeking"
    TRACE_ADJUDICATION = "trace_adjudication"


class Role(StrEnum):
    """What a call is for, so receipts never mix proposer and frontier cost."""

    PROPOSER = "proposer"
    ADJUDICATOR = "adjudicator"
    STRONG = "strong"


class SwarmError(RuntimeError):
    """Base class for a swarm run that could not produce an honest answer."""


class CandidateParseError(SwarmError):
    """A model response did not carry a usable JSON candidate document."""


class TransportError(SwarmError):
    """A model call could not be completed at all."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a nonempty string")
    return value


_STOPWORDS = frozenset(
    (
        "the", "and", "for", "that", "with", "this", "from", "into", "than", "then",
        "when", "where", "which", "while", "what", "are", "was", "were", "has", "have",
        "had", "not", "but", "its", "you", "your", "our", "their", "there", "these",
        "those", "can", "could", "should", "would", "will", "may", "might", "must",
        "does", "did", "done", "also", "only", "very", "more", "most", "much", "such",
        "some", "any", "all", "each", "other", "because", "before", "after", "about",
        "above", "below", "over", "under", "again", "further", "once", "here", "both",
        "few", "own", "same", "too",
    )
)


def content_tokens(value: str) -> frozenset[str]:
    """Comparable content words: lowercase, 3+ characters, stopword-free."""
    return frozenset(
        token
        for token in _TOKEN_PATTERN.findall(value.lower())
        if len(token) > 2 and token not in _STOPWORDS
    )


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Overlap of two token sets; empty input is no evidence of agreement."""
    return len(left & right) / len(left | right) if left and right else 0.0


def model_identity_matches(requested: str, served: str | None) -> bool:
    """True only when ``served`` names exactly the model that was requested.

    A provider that reports a bare model id (no ``provider/`` prefix) still matches
    when that id is the requested model's own name. Anything else - a different
    model, a sibling version, or a missing served id - is a silent fallback this
    harness refuses to attribute.
    """
    if not isinstance(served, str) or not served.strip():
        return False
    asked, answered = requested.strip().casefold(), served.strip().casefold()
    return asked == answered or ("/" not in answered and asked.rsplit("/", 1)[-1] == answered)


# --------------------------------------------------------------------------- #
# Citations
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Citation:
    """A model's claim that evidence sits at a path, line window, and quote."""

    path: str
    line: int
    end_line: int | None = None
    quote: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path.strip():
            raise ValueError("citation path must be a nonempty string")
        if isinstance(self.line, bool) or not isinstance(self.line, int) or self.line < 1:
            raise ValueError("citation line must be an integer >= 1")
        if self.end_line is not None and (
            isinstance(self.end_line, bool)
            or not isinstance(self.end_line, int)
            or self.end_line < self.line
        ):
            raise ValueError("citation end_line must be an integer >= line")
        if self.quote is not None and not isinstance(self.quote, str):
            raise ValueError("citation quote must be a string or None")

    @property
    def window(self) -> tuple[int, int]:
        """Inclusive ``(first, last)`` line of the claim."""
        return self.line, self.line if self.end_line is None else self.end_line

    def as_payload(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "end_line": self.end_line,
            "quote": self.quote,
        }

    @classmethod
    def from_payload(cls, value: Any) -> Citation:
        if not isinstance(value, Mapping):
            raise ValueError("citation must be an object")
        line = value.get("line")
        end_line = value.get("end_line")
        return cls(
            path=_text(value.get("path"), "citation path"),
            line=line,
            end_line=None if end_line is None else end_line,
            quote=value.get("quote"),
        )


@dataclass(frozen=True)
class CitationCheck:
    """What the workspace actually says about one citation."""

    citation: Citation
    file_exists: bool
    line_range_exists: bool
    quote_found: bool | None
    detail: str | None = None

    @property
    def verified(self) -> bool:
        """The cited file, line range, and (when quoted) text all exist."""
        return self.file_exists and self.line_range_exists and self.quote_found is not False

    def as_payload(self) -> dict[str, Any]:
        return {
            **self.citation.as_payload(),
            "file_exists": self.file_exists,
            "line_range_exists": self.line_range_exists,
            "quote_found": self.quote_found,
            "verified": self.verified,
            "detail": self.detail,
        }


def verify_citation(root: Path, citation: Citation) -> CitationCheck:
    """Check a citation against the task package without trusting the model.

    Absolute paths, ``..`` segments, and symlinks that leave ``root`` never verify,
    so a prompt-injected path cannot read the lab's answer keys. A supplied quote
    is compared after whitespace folding against the cited lines only.
    """
    relative = PurePosixPath(citation.path.replace("\\", "/"))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        return CitationCheck(citation, False, False, None, "path is absolute or escapes the package")
    base = root.resolve()
    try:
        target = (base / Path(*relative.parts)).resolve()
    except OSError as exc:
        return CitationCheck(citation, False, False, None, f"path cannot be resolved: {exc}")
    if not target.is_relative_to(base):
        return CitationCheck(citation, False, False, None, "path resolves outside the package")
    if not target.is_file():
        return CitationCheck(citation, False, False, None, "file does not exist")
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return CitationCheck(citation, False, False, None, f"file cannot be read: {exc}")
    start, end = citation.window
    if start < 1 or end > len(lines):
        return CitationCheck(
            citation, True, False, None, f"line range {start}-{end} is outside 1-{len(lines)}"
        )
    quote_found: bool | None = None
    if citation.quote is not None and citation.quote.strip():
        window = " ".join(" ".join(lines[start - 1 : end]).split())
        quote_found = " ".join(citation.quote.split()) in window
    return CitationCheck(citation, True, True, quote_found, None)


# --------------------------------------------------------------------------- #
# Candidates and agreement
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Candidate:
    """One model's proposed finding, repair, or exploit with its checked citations."""

    candidate_id: str
    model: str
    rank: int
    work_kind: WorkKind
    subject_id: str
    direction: str
    mechanism: str
    confidence: float
    citations: tuple[Citation, ...]
    checks: tuple[CitationCheck, ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def citation_verified(self) -> bool:
        """Every citation verified against the workspace; no citation is no evidence."""
        return bool(self.checks) and all(check.verified for check in self.checks)

    @property
    def mechanism_tokens(self) -> frozenset[str]:
        return content_tokens(self.mechanism)

    @property
    def locus(self) -> tuple[str, int, int] | None:
        """The primary citation as ``(path, first, last)``, or ``None``."""
        if not self.citations:
            return None
        first = self.citations[0]
        start, end = first.window
        return first.path, start, end

    def as_payload(self, *, inline_payload: bool = True) -> dict[str, Any]:
        payload_sha = _sha256_text(_canonical(dict(self.payload)))
        rendered_payload: Any = dict(self.payload)
        if not inline_payload or len(_canonical(rendered_payload)) > MAX_INLINE_PAYLOAD_CHARS:
            rendered_payload = {"omitted": True, "sha256": payload_sha}
        return {
            "candidate_id": self.candidate_id,
            "model": self.model,
            "rank": self.rank,
            "work_kind": self.work_kind.value,
            "subject_id": self.subject_id,
            "direction": self.direction,
            "mechanism": self.mechanism,
            "confidence": self.confidence,
            "citations": [citation.as_payload() for citation in self.citations],
            "checks": [check.as_payload() for check in self.checks],
            "citation_verified": self.citation_verified,
            "payload": rendered_payload,
            "payload_sha256": payload_sha,
        }


def _representative_key(candidate: Candidate) -> tuple[int, float, int, str]:
    return (
        1 if candidate.citation_verified else 0,
        candidate.confidence,
        -candidate.rank,
        candidate.candidate_id,
    )


def _same_locus(left: Candidate, right: Candidate) -> bool:
    """Two candidates describe one locus when direction, place, or wording agree."""
    if left.direction != right.direction:
        return False
    if left.locus is not None and right.locus is not None:
        left_path, left_start, left_end = left.locus
        right_path, right_start, right_end = right.locus
        if (
            left_path == right_path
            and left_start <= right_end + LOCUS_WINDOW_LINES
            and right_start <= left_end + LOCUS_WINDOW_LINES
        ):
            return True
    return jaccard(left.mechanism_tokens, right.mechanism_tokens) >= MECHANISM_OVERLAP


@dataclass(frozen=True)
class Cluster:
    """Candidates from different models that describe the same finding."""

    members: tuple[Candidate, ...]

    @property
    def models(self) -> frozenset[str]:
        return frozenset(member.model for member in self.members)

    @property
    def support(self) -> int:
        return len(self.models)

    @property
    def representative(self) -> Candidate:
        return max(self.members, key=_representative_key)

    @property
    def citation_verified(self) -> bool:
        return self.representative.citation_verified

    def as_payload(self) -> dict[str, Any]:
        representative = self.representative
        return {
            "support": self.support,
            "models": sorted(self.models),
            "citation_verified": self.citation_verified,
            "candidate": representative.as_payload(inline_payload=False),
            "candidate_ids": [member.candidate_id for member in self.members],
        }


def cluster_candidates(candidates: Sequence[Candidate]) -> tuple[Cluster, ...]:
    """Greedy single-pass clustering, strongest candidates claiming a locus first."""
    clusters: list[list[Candidate]] = []
    for candidate in sorted(candidates, key=_representative_key, reverse=True):
        for members in clusters:
            if any(_same_locus(candidate, member) for member in members):
                members.append(candidate)
                break
        else:
            clusters.append([candidate])
    ordered = [Cluster(tuple(members)) for members in clusters]
    return tuple(sorted(ordered, key=lambda cluster: (-cluster.support, cluster.representative.candidate_id)))


def aggregate(
    clusters: Sequence[Cluster], *, aggregation: Aggregation, responding_models: Sequence[str]
) -> tuple[Cluster, ...]:
    """Select clusters under one policy; the policy never invents evidence.

    ``union`` keeps every cluster, ``majority_vote`` keeps clusters a strict
    majority of the answering models support, and ``citation_verified_union``
    keeps every cluster whose representative citations verify.
    """
    if aggregation is Aggregation.UNION:
        return tuple(clusters)
    if aggregation is Aggregation.MAJORITY_VOTE:
        responding = len(set(responding_models))
        return tuple(cluster for cluster in clusters if cluster.support * 2 > responding)
    return tuple(cluster for cluster in clusters if cluster.citation_verified)


# --------------------------------------------------------------------------- #
# Model calls
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModelRequest:
    """One text-only model call: the prompt, the route, and why it was asked."""

    model: str
    prompt: str
    role: Role
    work_kind: WorkKind
    subject_id: str


@dataclass(frozen=True)
class ModelResponse:
    """What a transport saw: the text, the model that answered, and its cost."""

    text: str
    served_model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_ms: int = 0
    error: str | None = None


class ModelTransport(Protocol):
    """The one method the coordinator needs from a model route."""

    def complete(self, request: ModelRequest) -> ModelResponse: ...


@dataclass(frozen=True)
class ModelCall:
    """The attestation record every call must leave behind."""

    call_id: str
    role: Role
    requested_model: str
    served_model: str | None
    prompt_sha256: str
    response_sha256: str
    tokens_in: int | None
    tokens_out: int | None
    latency_ms: int
    quarantined_fallback: bool
    work_kind: WorkKind
    subject_id: str
    error: str | None = None

    @property
    def usable(self) -> bool:
        """A response is usable only when it completed and names its own model."""
        return self.error is None and not self.quarantined_fallback

    @property
    def attributed_model(self) -> str | None:
        """The model this response may be credited to; ``None`` when quarantined."""
        return None if self.quarantined_fallback else self.requested_model

    def as_payload(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "role": self.role.value,
            "requested_model": self.requested_model,
            "served_model": self.served_model,
            "attributed_model": self.attributed_model,
            "prompt_sha256": self.prompt_sha256,
            "response_sha256": self.response_sha256,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "latency_ms": self.latency_ms,
            "quarantined_fallback": self.quarantined_fallback,
            "work_kind": self.work_kind.value,
            "subject_id": self.subject_id,
            "error": self.error,
        }


@dataclass(frozen=True)
class CallOutcome:
    """A call plus whatever it parsed into; text is kept for the adjudicator."""

    call: ModelCall
    candidates: tuple[Candidate, ...]
    text: str


def parse_omp_stream(stream: str) -> tuple[str | None, str, int | None, int | None]:
    """Read one ``omp -p --mode json`` stream: served model, text, and token counts.

    ``omp`` prints one JSON object per line. The assistant ``message_end`` (or the
    last ``agent_end`` message) carries the provider, the model that actually
    answered, and its usage. A stream that names no model returns ``None``, which
    the coordinator quarantines rather than attributing.
    """
    assistants: list[Mapping[str, Any]] = []
    for raw in stream.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, Mapping):
            continue
        kind = event.get("type")
        if kind == "message_end" and isinstance(event.get("message"), Mapping):
            message = event["message"]
            if message.get("role") == "assistant":
                assistants.append(message)
        elif kind == "agent_end" and not assistants:
            messages = event.get("messages")
            if isinstance(messages, list):
                assistants.extend(
                    message
                    for message in messages
                    if isinstance(message, Mapping) and message.get("role") == "assistant"
                )
    if not assistants:
        return None, "", None, None
    message = assistants[-1]
    provider, model = message.get("provider"), message.get("model")
    served = (
        f"{provider}/{model}"
        if isinstance(provider, str) and provider and isinstance(model, str) and model
        else (model if isinstance(model, str) and model else None)
    )
    return served, _message_text(message), *_usage(message)


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, Mapping) and part.get("type") == "text" and isinstance(part.get("text"), str)
        )
    return ""


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _usage(message: Mapping[str, Any]) -> tuple[int | None, int | None]:
    """Codex-shaped usage: reported input includes cached and cache-write tokens."""
    raw = message.get("usage")
    if not isinstance(raw, Mapping):
        return None, None
    fresh, output = _count(raw.get("input")), _count(raw.get("output"))
    cached, cache_write = _count(raw.get("cacheRead")), _count(raw.get("cacheWrite"))
    if fresh is None or output is None or cached is None or cache_write is None:
        return None, output
    return fresh + cached + cache_write, output


class OmpCliTransport:
    """Headless ``omp -p`` calls whose served model comes from the event stream.

    No retry, no reroute, no fallback: a failure returns a response with no served
    model and an error, and the coordinator quarantines it. ``runner`` is injected
    in tests so the argument contract is checked without spending a token.
    """

    def __init__(
        self,
        *,
        executable: str = "omp",
        timeout_seconds: float = 300.0,
        cwd: Path | None = None,
        thinking: str = "medium",
        extra_args: Sequence[str] = (),
        runner: Callable[[Sequence[str], Path | None, float], tuple[int, str, str]] | None = None,
    ) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.cwd = cwd
        self.thinking = thinking
        self.extra_args = tuple(extra_args)
        self.runner = runner if runner is not None else _run_subprocess

    def argv(self, request: ModelRequest) -> list[str]:
        """The exact command line, with the prompt last so it can be swapped for a marker."""
        return [
            self.executable,
            "-p",
            "--mode",
            "json",
            "--no-session",
            "--no-title",
            "--no-lsp",
            "--no-tools",
            "--no-rules",
            "--no-skills",
            "--no-extensions",
            "--thinking",
            self.thinking,
            "--model",
            request.model,
            *self.extra_args,
            request.prompt,
        ]

    def complete(self, request: ModelRequest) -> ModelResponse:
        argv = self.argv(request)
        started = time.monotonic()
        try:
            code, stdout, stderr = self.runner(argv, self.cwd, self.timeout_seconds)
        except (OSError, subprocess.SubprocessError) as exc:
            return ModelResponse(
                text="",
                served_model=None,
                latency_ms=_elapsed_ms(started),
                error=f"{type(exc).__name__}: {exc}",
            )
        latency_ms = _elapsed_ms(started)
        if code != 0:
            return ModelResponse(
                text="",
                served_model=None,
                latency_ms=latency_ms,
                error=f"omp exited {code}: {stderr.strip()[-300:]}",
            )
        served, text, tokens_in, tokens_out = parse_omp_stream(stdout)
        if not text.strip():
            return ModelResponse(
                text="",
                served_model=served,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency_ms,
                error="omp returned no assistant text",
            )
        return ModelResponse(
            text=text,
            served_model=served,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
        )


def _elapsed_ms(started: float) -> int:
    return int(round((time.monotonic() - started) * 1000))


def _run_subprocess(argv: Sequence[str], cwd: Path | None, timeout: float) -> tuple[int, str, str]:
    completed = subprocess.run(
        list(argv),
        cwd=None if cwd is None else str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return completed.returncode, completed.stdout or "", completed.stderr or ""


# --------------------------------------------------------------------------- #
# Candidate parsing
# --------------------------------------------------------------------------- #


def _json_document(text: str) -> Any:
    """Extract the JSON document from a model reply, tolerating fences and preamble."""
    cleaned = text.strip()
    if cleaned.startswith("Working..."):
        cleaned = cleaned.removeprefix("Working...").strip()
    blobs = [match.group(1) for match in _JSON_FENCE.finditer(cleaned)]
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = cleaned.find(opener), cleaned.rfind(closer)
        if start != -1 and end > start:
            blobs.append(cleaned[start : end + 1])
    for blob in blobs:
        try:
            return json.loads(blob)
        except ValueError:
            continue
    raise CandidateParseError("response did not contain a JSON object or array")


def _confidence(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        return None
    return number


def build_candidate(
    raw: Any,
    *,
    index: int,
    model: str,
    rank: int,
    work_kind: WorkKind,
    subject_id: str,
    verify: Callable[[Citation], CitationCheck],
) -> Candidate | None:
    """One parsed finding, or ``None`` when it cannot stand as evidence."""
    if not isinstance(raw, Mapping):
        return None
    direction, mechanism = raw.get("direction"), raw.get("mechanism")
    if not isinstance(direction, str) or not direction.strip():
        return None
    if not isinstance(mechanism, str) or not mechanism.strip():
        return None
    confidence = _confidence(raw.get("confidence"))
    if confidence is None:
        return None
    citations_raw = raw.get("citations")
    if not isinstance(citations_raw, list) or not citations_raw:
        return None
    citations: list[Citation] = []
    for value in citations_raw:
        try:
            citations.append(Citation.from_payload(value))
        except (TypeError, ValueError):
            return None
    citation_tuple = tuple(citations)
    payload = raw.get("payload")
    return Candidate(
        candidate_id=f"{model}#{index}",
        model=model,
        rank=rank,
        work_kind=work_kind,
        subject_id=subject_id,
        direction=direction.strip(),
        mechanism=mechanism.strip(),
        confidence=confidence,
        citations=citation_tuple,
        checks=tuple(verify(citation) for citation in citation_tuple),
        payload=dict(payload) if isinstance(payload, Mapping) else {},
    )


def parse_candidates(
    text: str,
    *,
    model: str,
    rank: int,
    work_kind: WorkKind,
    subject_id: str,
    verify: Callable[[Citation], CitationCheck],
    limit: int = MAX_CANDIDATES_PER_MODEL,
) -> tuple[Candidate, ...]:
    """Parse a proposer reply into candidates; malformed entries are not evidence.

    An explicit empty ``findings`` list is a legitimate abstention and returns no
    candidates. A non-empty list that yields nothing usable raises
    ``CandidateParseError`` so a broken response is never read as silence.
    """
    document = _json_document(text)
    raw_findings: Any = document.get("findings") if isinstance(document, Mapping) else document
    if isinstance(raw_findings, Mapping):
        raw_findings = [raw_findings]
    if not isinstance(raw_findings, list):
        raise CandidateParseError("candidate document has no findings array")
    candidates: list[Candidate] = []
    for index, raw in enumerate(raw_findings[:limit], 1):
        candidate = build_candidate(
            raw,
            index=index,
            model=model,
            rank=rank,
            work_kind=work_kind,
            subject_id=subject_id,
            verify=verify,
        )
        if candidate is not None:
            candidates.append(candidate)
    if raw_findings and not candidates:
        raise CandidateParseError("no finding in the response carried a usable citation")
    return tuple(candidates)


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RetrievalRecord:
    """One historical issue or trace, compact enough to put in a prompt."""

    kind: str
    task_id: str
    record_id: str
    summary: str
    fields: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.kind, "retrieval kind")
        _text(self.task_id, "retrieval task_id")
        _text(self.record_id, "retrieval record_id")
        _text(self.summary, "retrieval summary")

    def as_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "task_id": self.task_id,
            "record_id": self.record_id,
            "summary": self.summary,
            "fields": dict(self.fields),
        }


class TraceRetriever(Protocol):
    """Historical evidence lookup; ``CloudTraceDatabaseRetriever`` is the default."""

    def retrieve(
        self, *, work_kind: WorkKind, subject_id: str, query: str, limit: int = MAX_RETRIEVAL_RECORDS
    ) -> Sequence[RetrievalRecord]: ...


class CloudTraceDatabaseRetriever:
    """Read the production cloud trace database for matching past issues and traces.

    Reads are projection-only: the task's own issues join exactly, and traces are
    matched by text. Sealed identities are filtered by the coordinator, not here,
    so the drop stays visible in the round event.
    """

    def __init__(self, database: CloudTraceDatabase) -> None:
        self.database = database

    def retrieve(
        self, *, work_kind: WorkKind, subject_id: str, query: str, limit: int = MAX_RETRIEVAL_RECORDS
    ) -> Sequence[RetrievalRecord]:
        records: list[RetrievalRecord] = []
        for issue in self.database.query_task_issues(subject_id, confirmed_only=False):
            records.append(_issue_record(issue))
        terms = query.strip() or subject_id
        for trace in self.database.search_traces(terms):
            if trace.get("task_id") != subject_id and not trace.get("issues") and not trace.get("is_exploit"):
                continue
            records.append(_trace_record(trace))
            if len(records) >= limit * 2:
                break
        return tuple(records[:limit])


def _issue_record(issue: Mapping[str, Any]) -> RetrievalRecord:
    location = f"{issue.get('citation_file')}:{issue.get('citation_line')}"
    return RetrievalRecord(
        kind="issue",
        task_id=str(issue["task_id"]),
        record_id=str(issue["issue_id"]),
        summary=(
            f"issue {issue['issue_id']} on {issue['task_id']}: {issue.get('defect_class')} "
            f"({issue.get('direction')}, {issue.get('status')}) at {location}"
        ),
        fields={
            "defect_class": issue.get("defect_class"),
            "direction": issue.get("direction"),
            "status": issue.get("status"),
            "citation_file": issue.get("citation_file"),
            "citation_line": issue.get("citation_line"),
            "original_reward": issue.get("original_reward"),
            "repaired_reward": issue.get("repaired_reward"),
        },
    )


def _trace_record(trace: Mapping[str, Any]) -> RetrievalRecord:
    return RetrievalRecord(
        kind="trace",
        task_id=str(trace["task_id"]),
        record_id=str(trace["trace_id"]),
        summary=(
            f"trace {trace['trace_id']} on {trace['task_id']} by {trace.get('model_id')}: "
            f"outcome {trace.get('outcome')}, reward {trace.get('reward')}, "
            f"steps {trace.get('step_count')}"
        ),
        fields={
            "model_id": trace.get("model_id"),
            "outcome": trace.get("outcome"),
            "reward": trace.get("reward"),
            "step_count": trace.get("step_count"),
            "is_exploit": trace.get("is_exploit"),
        },
    )


class EventSink(Protocol):
    """The durable journal surface ``ContinuousWorkQueue`` provides.

    ``frozen_eval_token`` is forwarded unchanged: journaling a sealed TEST round
    needs the same explicit authorization the run itself needed, and the coordinator
    never invents one.
    """

    def append_event(
        self,
        event: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        frozen_eval_token: str | None = None,
    ) -> dict[str, Any]: ...


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #

WORK_BRIEF = {
    WorkKind.TASK_QUALITY_AUDIT: (
        "Audit this task package for grader defects: a check that passes a non-solution "
        "(direction wrong_pass) or fails a correct solution (direction unfair_fail)."
    ),
    WorkKind.REPAIR_PROPOSAL: (
        "Propose the smallest repair that closes one defect without weakening legitimate "
        "grading. Name the files and lines you would change; put the patch under payload."
    ),
    WorkKind.REWARD_SEEKING: (
        "Propose an exploit: a solution that reaches reward without doing the task's work. "
        "Name the check it bypasses and put the candidate script under payload."
    ),
    WorkKind.TRACE_ADJUDICATION: (
        "Label what actually happened in this trace: the first failure, whose fault it is, "
        "and whether the recorded reward is real."
    ),
}

_CANDIDATE_CONTRACT = """Reply with one JSON object and nothing else:
{"findings": [{"direction": "<token>", "mechanism": "<one sentence>", "confidence": <0..1>,
  "citations": [{"path": "<relative path in the task package>", "line": <int >= 1>,
                 "end_line": <int >= line>, "quote": "<exact text from that range>"}],
  "payload": {<optional structured artifact>}}]}
Rules:
- Cite only relative paths inside the task package; absolute paths and ".." are rejected.
- Every finding needs at least one citation with a real line range.
- When you quote, the quote must appear inside the cited lines; a wrong quote is a failed citation.
- A finding without a verifiable citation is discarded before anyone reads it.
- Return {"findings": []} when you find nothing. No prose outside the JSON object."""

_ADJUDICATOR_CONTRACT = """Reply with one JSON object and nothing else:
{"verdicts": [{"candidate_id": "<id from the list>", "decision": "accept" | "reject",
               "reason": "<short specific reason>"}],
 "final": [{"direction": "...", "mechanism": "...", "confidence": <0..1>,
            "citations": [{"path": "...", "line": <int>, "end_line": <int>, "quote": "..."}]}]}
Rules:
- Accept only a candidate whose citations all read "verified": true.
- Reject every false alarm and say which check it fails.
- "final" is your own selection; copy citations exactly from the candidates you accept.
- Every final citation is re-checked against the package before it is kept.
- No prose outside the JSON object."""


def _evidence_block(records: Sequence[RetrievalRecord]) -> str:
    if not records:
        return ""
    rendered = "\n".join(f"- {record.summary}" for record in records)[:MAX_RETRIEVAL_CHARS]
    return (
        "Historical evidence from the production cloud trace database (past issues and "
        f"traces; use it to avoid known dead ends):\n{rendered}\n"
    )


def build_proposer_prompt(
    *, work_kind: WorkKind, subject_id: str, prompt: str, evidence: Sequence[RetrievalRecord] = ()
) -> str:
    """The cheap-swarm prompt: work brief, retrieval, subject, citation contract."""
    return (
        f"{WORK_BRIEF[work_kind]}\n\n"
        f"{_evidence_block(evidence)}"
        f"Subject: {subject_id}\n\n"
        f"{prompt.strip()}\n\n"
        f"{_CANDIDATE_CONTRACT}"
    )


def build_strong_prompt(
    *, work_kind: WorkKind, subject_id: str, prompt: str, evidence: Sequence[RetrievalRecord] = ()
) -> str:
    """The single-strong prompt: the same harness with the frontier model's remit."""
    return (
        f"{WORK_BRIEF[work_kind]}\n\n"
        f"You are the only reviewer for this subject; there is no second pass to catch you.\n"
        f"{_evidence_block(evidence)}"
        f"Subject: {subject_id}\n\n"
        f"{prompt.strip()}\n\n"
        f"{_CANDIDATE_CONTRACT}"
    )


def build_adjudicator_prompt(
    *,
    work_kind: WorkKind,
    subject_id: str,
    clusters: Sequence[Cluster],
    unverified: Sequence[Candidate],
    evidence: Sequence[RetrievalRecord] = (),
) -> str:
    """The compact adjudicator prompt: checked candidates, never raw file bodies.

    Every block is truncated at the source and the joined list at
    ``MAX_ADJUDICATOR_CANDIDATE_CHARS``, so one runaway mechanism string cannot
    push the frontier call past its budget or evict the whole candidate list.
    """
    blocks: list[str] = []
    for cluster in clusters[:MAX_ADJUDICATED_CANDIDATES]:
        candidate = cluster.representative
        block = [
            f"- candidate_id: {candidate.candidate_id}",
            f"  models: {', '.join(sorted(cluster.models))} (support {cluster.support})",
            f"  direction: {candidate.direction[:80]}",
            f"  mechanism: {candidate.mechanism[:300]}",
            f"  confidence: {candidate.confidence}",
            f"  citation_verified: {str(cluster.citation_verified).lower()}",
        ]
        for check in candidate.checks:
            start, end = check.citation.window
            block.append(
                f"  citation: {check.citation.path}:{start}-{end} "
                f"verified={str(check.verified).lower()} "
                f"quote_found={check.quote_found} detail={check.detail or 'none'}"
            )
        blocks.append("\n".join(block))
    for candidate in unverified[:MAX_ADJUDICATED_CANDIDATES]:
        blocks.append(
            f"- candidate_id: {candidate.candidate_id}\n"
            f"  models: {candidate.model} (unverified proposal, not a cluster)\n"
            f"  direction: {candidate.direction[:80]}\n"
            f"  mechanism: {candidate.mechanism[:300]}\n"
            f"  confidence: {candidate.confidence}\n"
            f"  citation_verified: false\n"
            f"  detail: {candidate.checks[0].detail if candidate.checks else 'no citation'}"
        )
    rendered = "\n".join(blocks)[:MAX_ADJUDICATOR_CANDIDATE_CHARS]
    return (
        f"You are the adjudicator for {work_kind.value} on {subject_id}.\n"
        f"{WORK_BRIEF[work_kind]}\n\n"
        f"{_evidence_block(evidence)}"
        f"Candidates proposed by the cheap swarm (citations already checked against the package):\n"
        f"{rendered or '- none'}\n\n"
        f"{_ADJUDICATOR_CONTRACT}"
    )


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SwarmRound:
    """Everything one strategy did for one subject, attested call by call."""

    round_id: str
    mode: SwarmMode
    work_kind: WorkKind
    subject_id: str
    dataset_id: str
    item_id: str
    split: str
    calls: tuple[ModelCall, ...]
    candidates: tuple[Candidate, ...]
    rejected: tuple[Candidate, ...]
    clusters: tuple[Cluster, ...]
    cascade_confidence: float | None = None

    @property
    def frontier_calls(self) -> int:
        """Calls to the strong model: the cascade's whole point is keeping this at 0."""
        return sum(1 for call in self.calls if call.role is not Role.PROPOSER)

    @property
    def cheap_calls(self) -> int:
        return sum(1 for call in self.calls if call.role is Role.PROPOSER)

    @property
    def quarantined(self) -> tuple[ModelCall, ...]:
        return tuple(call for call in self.calls if call.quarantined_fallback)

    @property
    def attributed_models(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    call.requested_model
                    for call in self.calls
                    if call.usable
                }
            )
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "item_id": self.item_id,
            "split": self.split,
            "round_id": self.round_id,
            "mode": self.mode.value,
            "work_kind": self.work_kind.value,
            "subject_id": self.subject_id,
            "calls": [call.as_payload() for call in self.calls],
            "candidates": [candidate.as_payload() for candidate in self.candidates],
            "rejected": [candidate.as_payload(inline_payload=False) for candidate in self.rejected],
            "clusters": [cluster.as_payload() for cluster in self.clusters],
            "cascade_confidence": self.cascade_confidence,
            "frontier_calls": self.frontier_calls,
            "cheap_calls": self.cheap_calls,
            "quarantined_calls": [call.call_id for call in self.quarantined],
            "attributed_models": list(self.attributed_models),
        }


@dataclass(frozen=True)
class SwarmResult:
    """The round, its final answer, and the evidence that the answer is honest."""

    round: SwarmRound
    aggregation: Aggregation | None
    selected: tuple[Candidate, ...]
    adjudication: Mapping[str, Any] | None
    escalated: bool
    retrieval: tuple[RetrievalRecord, ...]
    retrieval_dropped_sealed: int
    events: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        """True when the final selection is non-empty and every citation verified."""
        return bool(self.selected) and all(
            candidate.citation_verified for candidate in self.selected
        )

    @property
    def tokens_in(self) -> int | None:
        """Total input tokens, or ``None`` when any call's usage stayed unknown."""
        return _sum_known(call.tokens_in for call in self.round.calls)

    @property
    def tokens_out(self) -> int | None:
        return _sum_known(call.tokens_out for call in self.round.calls)

    def attestation(self) -> dict[str, Any]:
        """A receipt-ready record of the run; safe to persist verbatim."""
        return {
            "schema": "swarm.coordinator/v1",
            "round": self.round.as_payload(),
            "aggregation": None if self.aggregation is None else self.aggregation.value,
            "selected": [candidate.as_payload() for candidate in self.selected],
            "adjudication": None if self.adjudication is None else dict(self.adjudication),
            "escalated": self.escalated,
            "accepted": self.accepted,
            "retrieval": {
                "records": [record.as_payload() for record in self.retrieval],
                "dropped_sealed": self.retrieval_dropped_sealed,
            },
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "events": list(self.events),
        }


# --------------------------------------------------------------------------- #
# The coordinator
# --------------------------------------------------------------------------- #


class SwarmCoordinator:
    """Run one of the four strategies for one subject and attest the result.

    The coordinator is deliberately stateless between runs: every run re-admits its
    corpus item, re-reads retrieval, and rebuilds its prompts from the same inputs,
    so a receipt can be reproduced from the round id alone.
    """

    def __init__(
        self,
        *,
        transport: ModelTransport,
        registry: CorpusRegistry,
        queue: EventSink | None = None,
        retriever: TraceRetriever | None = None,
        strong_model: str = DEFAULT_STRONG_MODEL,
        cheap_models: Sequence[str] = CHEAP_MODELS,
        confidence_threshold: float = 0.8,
        max_fanout: int = len(CHEAP_MODELS),
        run_id: str = "swarm",
        workspace: Path | None = None,
    ) -> None:
        if strong_model not in STRONG_MODELS:
            raise ValueError(f"strong_model must be one of {', '.join(STRONG_MODELS)}")
        if not cheap_models:
            raise ValueError("cheap_models must be non-empty")
        if len(set(cheap_models)) != len(cheap_models):
            raise ValueError("cheap_models must not repeat a route")
        if strong_model in cheap_models:
            raise ValueError("cheap_models must not include the strong model")
        if not 0.0 <= confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold must be within 0..1")
        if max_fanout < 1:
            raise ValueError("max_fanout must be positive")
        self.transport = transport
        self.registry = registry
        self.queue = queue
        self.retriever = retriever
        self.strong_model = strong_model
        self.cheap_models = tuple(cheap_models)
        self.confidence_threshold = confidence_threshold
        self.max_fanout = max_fanout
        self.run_id = run_id
        self.workspace = workspace

    # -- public surface ----------------------------------------------------- #

    def run(
        self,
        *,
        mode: SwarmMode,
        work_kind: WorkKind,
        subject_id: str,
        prompt: str,
        dataset_id: str,
        item_id: str,
        workspace: Path | None = None,
        aggregation: Aggregation = Aggregation.CITATION_VERIFIED_UNION,
        frozen_eval_token: str | None = None,
        run_id: str | None = None,
        query: str | None = None,
    ) -> SwarmResult:
        """Run one strategy end to end; raises ``SplitLockError`` before any spend.

        ``dataset_id``/``item_id`` name the corpus item under work and are admitted
        first, so sealed work costs nothing. ``workspace`` is the task package the
        citations are checked against. ``prompt`` is the work-specific brief the
        caller would give a single reviewer; this module wraps it in the mode's
        harness. ``query`` overrides the retrieval query (defaults to the subject).
        ``run_id`` scopes the round and event identities, so re-running the same
        round with the same id is idempotent in the journal.
        """
        item = self._admit(dataset_id, item_id, frozen_eval_token)
        run = self.run_id if run_id is None else _text(run_id, "run_id")
        root = self._workspace(workspace)
        verify = self._verifier(root)
        evidence, dropped = self._evidence(work_kind, subject_id, query)
        round_id = str(
            uuid5(
                NAMESPACE_URL,
                f"evallab.swarm.round:{run}:{mode.value}:{work_kind.value}:{subject_id}",
            )
        )
        proposer_prompt = (
            ""
            if mode is SwarmMode.SINGLE_STRONG
            else build_proposer_prompt(
                work_kind=work_kind, subject_id=subject_id, prompt=prompt, evidence=evidence
            )
        )
        if mode is SwarmMode.SINGLE_STRONG:
            calls, candidates, clusters, selected, adjudication, escalated, confidence = (
                self._single_strong(round_id, work_kind, subject_id, prompt, evidence, verify)
            )
            policy: Aggregation | None = None
        elif mode is SwarmMode.CHEAP_SWARM:
            calls, candidates, clusters, selected, adjudication, escalated, confidence = (
                self._cheap_swarm(round_id, work_kind, subject_id, proposer_prompt, verify, aggregation)
            )
            policy = aggregation
        elif mode is SwarmMode.HYBRID_ADJUDICATOR:
            calls, candidates, clusters, selected, adjudication, escalated, confidence = (
                self._hybrid(
                    round_id, work_kind, subject_id, proposer_prompt, evidence, verify, escalate=True
                )
            )
            policy = None
        else:
            calls, candidates, clusters, selected, adjudication, escalated, confidence = (
                self._hybrid(
                    round_id,
                    work_kind,
                    subject_id,
                    proposer_prompt,
                    evidence,
                    verify,
                    escalate=None,
                )
            )
            policy = None
        rejected = tuple(candidate for candidate in candidates if not candidate.citation_verified)
        swarm_round = SwarmRound(
            round_id=round_id,
            mode=mode,
            work_kind=work_kind,
            subject_id=subject_id,
            dataset_id=dataset_id,
            item_id=item_id,
            split=item.split,
            calls=tuple(calls),
            candidates=candidates,
            rejected=rejected,
            clusters=clusters,
            cascade_confidence=confidence,
        )
        result = SwarmResult(
            round=swarm_round,
            aggregation=policy,
            selected=selected,
            adjudication=adjudication,
            escalated=escalated,
            retrieval=evidence,
            retrieval_dropped_sealed=dropped,
            events=self._emit(
                run_id=run,
                round_id=round_id,
                swarm_round=swarm_round,
                selected=selected,
                adjudication=adjudication,
                escalated=escalated,
                aggregation=policy,
                retrieval=evidence,
                dropped=dropped,
                frozen_eval_token=frozen_eval_token,
            ),
        )
        return result

    # -- admission and evidence -------------------------------------------- #

    def _admit(
        self, dataset_id: str, item_id: str, frozen_eval_token: str | None
    ) -> CorpusItem:
        """Resolve the corpus item, refusing sealed work before a single token is spent.

        ``CorpusRegistry.admit`` is the only enforcement point: it raises
        ``SplitLockError`` for a sealed item unless the caller supplied the frozen
        evaluation token itself. The coordinator never supplies one on its own.
        """
        return self.registry.admit(dataset_id, item_id, frozen_eval_token=frozen_eval_token)

    def _workspace(self, workspace: Path | None) -> Path:
        root = workspace if workspace is not None else self.workspace
        if root is None:
            raise ValueError("a workspace directory is required to verify citations")
        return root

    def _verifier(self, root: Path) -> Callable[[Citation], CitationCheck]:
        return lambda citation: verify_citation(root, citation)

    def _evidence(
        self, work_kind: WorkKind, subject_id: str, query: str | None
    ) -> tuple[tuple[RetrievalRecord, ...], int]:
        """Retrieved history with every sealed identity removed."""
        if self.retriever is None:
            return (), 0
        records = self.retriever.retrieve(
            work_kind=work_kind,
            subject_id=subject_id,
            query=subject_id if query is None else query,
        )
        kept: list[RetrievalRecord] = []
        dropped = 0
        for record in records:
            digest = record.fields.get("task_digest")
            seal = self.registry.sealed_identity(
                item_id=record.record_id,
                task_id=record.task_id,
                task_digest=digest if isinstance(digest, str) else None,
            )
            if seal is not None:
                dropped += 1
                continue
            kept.append(record)
        return tuple(kept[:MAX_RETRIEVAL_RECORDS]), dropped

    # -- strategies --------------------------------------------------------- #

    def _single_strong(
        self,
        round_id: str,
        work_kind: WorkKind,
        subject_id: str,
        prompt: str,
        evidence: Sequence[RetrievalRecord],
        verify: Callable[[Citation], CitationCheck],
    ) -> tuple[
        list[ModelCall],
        tuple[Candidate, ...],
        tuple[Cluster, ...],
        tuple[Candidate, ...],
        Mapping[str, Any] | None,
        bool,
        None,
    ]:
        outcome = self._call(
            round_id=round_id,
            model=self.strong_model,
            rank=0,
            role=Role.STRONG,
            work_kind=work_kind,
            subject_id=subject_id,
            prompt=build_strong_prompt(
                work_kind=work_kind, subject_id=subject_id, prompt=prompt, evidence=evidence
            ),
            verify=verify,
        )
        clusters = cluster_candidates(outcome.candidates)
        selected = tuple(cluster.representative for cluster in clusters if cluster.citation_verified)
        return [outcome.call], outcome.candidates, clusters, selected, None, False, None

    def _cheap_swarm(
        self,
        round_id: str,
        work_kind: WorkKind,
        subject_id: str,
        prompt: str,
        verify: Callable[[Citation], CitationCheck],
        aggregation: Aggregation,
    ) -> tuple[
        list[ModelCall],
        tuple[Candidate, ...],
        tuple[Cluster, ...],
        tuple[Candidate, ...],
        Mapping[str, Any] | None,
        bool,
        None,
    ]:
        outcomes = self._fanout(round_id, work_kind, subject_id, prompt, verify)
        candidates = tuple(
            candidate for outcome in outcomes for candidate in outcome.candidates
        )
        clusters = cluster_candidates(candidates)
        responding = [outcome.call.requested_model for outcome in outcomes if outcome.call.usable]
        selected = tuple(
            cluster.representative
            for cluster in aggregate(
                clusters, aggregation=aggregation, responding_models=responding
            )
        )
        return (
            [outcome.call for outcome in outcomes],
            candidates,
            clusters,
            selected,
            None,
            False,
            None,
        )

    def _hybrid(
        self,
        round_id: str,
        work_kind: WorkKind,
        subject_id: str,
        prompt: str,
        evidence: Sequence[RetrievalRecord],
        verify: Callable[[Citation], CitationCheck],
        *,
        escalate: bool | None,
    ) -> tuple[
        list[ModelCall],
        tuple[Candidate, ...],
        tuple[Cluster, ...],
        tuple[Candidate, ...],
        Mapping[str, Any] | None,
        bool,
        float | None,
    ]:
        """Cheap proposals first; the frontier model only when ``escalate`` says so.

        ``escalate=True`` always adjudicates (hybrid). ``escalate=None`` is the
        adaptive cascade: accept a unanimous, citation-verified, confident swarm for
        free and escalate anything else.
        """
        outcomes = self._fanout(round_id, work_kind, subject_id, prompt, verify)
        calls = [outcome.call for outcome in outcomes]
        candidates = tuple(candidate for outcome in outcomes for candidate in outcome.candidates)
        clusters = cluster_candidates(candidates)
        responding = [outcome.call.requested_model for outcome in outcomes if outcome.call.usable]
        confidence = (
            None if escalate is True else self._cascade_confidence(clusters, responding)
        )
        if escalate is None and confidence is not None and confidence >= self.confidence_threshold:
            selected = (clusters[0].representative,)
            return calls, candidates, clusters, selected, None, False, confidence
        unverified = tuple(candidate for candidate in candidates if not candidate.citation_verified)
        adjudicator, selected, adjudication = self._adjudicate(
            round_id=round_id,
            work_kind=work_kind,
            subject_id=subject_id,
            clusters=clusters,
            unverified=unverified,
            evidence=evidence,
            verify=verify,
        )
        calls.append(adjudicator.call)
        return calls, candidates, clusters, selected, adjudication, True, confidence

    def _cascade_confidence(
        self, clusters: Sequence[Cluster], responding_models: Sequence[str]
    ) -> float | None:
        """The confidence a unanimous, citation-verified swarm earned, else ``None``.

        ``None`` means the swarm did not earn a free answer and the cascade must
        escalate. Confidence is the weakest agreeing model's own confidence: the
        cascade may only accept for free what every cheap model was sure about. A
        model that was quarantined never counts toward unanimity, so a silent
        provider fallback cannot make a two-model swarm look like three.
        """
        if not clusters or len(set(responding_models)) != len(self.cheap_models):
            return None
        top = clusters[0]
        if top.support != len(self.cheap_models):
            return None
        if not all(member.citation_verified for member in top.members):
            return None
        weakest = min(member.confidence for member in top.members)
        return weakest if weakest > 0.0 else None

    # -- calls and adjudication --------------------------------------------- #

    def _fanout(
        self,
        round_id: str,
        work_kind: WorkKind,
        subject_id: str,
        prompt: str,
        verify: Callable[[Citation], CitationCheck],
    ) -> list[CallOutcome]:
        """Ask every cheap model in parallel; order is the configured route order."""
        requests = [
            (model, rank)
            for rank, model in enumerate(self.cheap_models, 1)
        ]
        with ThreadPoolExecutor(
            max_workers=min(self.max_fanout, len(requests)), thread_name_prefix="swarm"
        ) as pool:
            futures = [
                pool.submit(
                    self._call,
                    round_id=round_id,
                    model=model,
                    rank=rank,
                    role=Role.PROPOSER,
                    work_kind=work_kind,
                    subject_id=subject_id,
                    prompt=prompt,
                    verify=verify,
                )
                for model, rank in requests
            ]
            return [future.result() for future in futures]

    def _call(
        self,
        *,
        round_id: str,
        model: str,
        rank: int,
        role: Role,
        work_kind: WorkKind,
        subject_id: str,
        prompt: str,
        verify: Callable[[Citation], CitationCheck],
    ) -> CallOutcome:
        """One attested call; a fallback or a failure never yields candidates."""
        request = ModelRequest(
            model=model, prompt=prompt, role=role, work_kind=work_kind, subject_id=subject_id
        )
        prompt_sha = _sha256_text(prompt)
        try:
            response = self.transport.complete(request)
        except Exception as exc:  # a transport failure is data, not a crash
            response = ModelResponse(text="", error=f"{type(exc).__name__}: {exc}")
        response_sha = _sha256_text(response.text)
        quarantined = not model_identity_matches(model, response.served_model)
        call = ModelCall(
            call_id=str(
                uuid5(
                    NAMESPACE_URL,
                    f"evallab.swarm.call:{round_id}:{model}:{role.value}:{prompt_sha}:{response_sha}",
                )
            ),
            role=role,
            requested_model=model,
            served_model=response.served_model,
            prompt_sha256=prompt_sha,
            response_sha256=response_sha,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            latency_ms=response.latency_ms,
            quarantined_fallback=quarantined,
            work_kind=work_kind,
            subject_id=subject_id,
            error=response.error,
        )
        if not call.usable:
            return CallOutcome(call, (), response.text)
        if role is Role.ADJUDICATOR:
            # The adjudicator answers in its own document shape; ``_adjudicate``
            # parses it and re-checks every citation it proposes.
            return CallOutcome(call, (), response.text)
        try:
            candidates = parse_candidates(
                response.text,
                model=model,
                rank=rank,
                work_kind=work_kind,
                subject_id=subject_id,
                verify=verify,
            )
        except CandidateParseError as exc:
            return CallOutcome(replace(call, error=f"parse_error: {exc}"), (), response.text)
        return CallOutcome(call, candidates, response.text)

    def _adjudicate(
        self,
        *,
        round_id: str,
        work_kind: WorkKind,
        subject_id: str,
        clusters: Sequence[Cluster],
        unverified: Sequence[Candidate],
        evidence: Sequence[RetrievalRecord],
        verify: Callable[[Citation], CitationCheck],
    ) -> tuple[CallOutcome, tuple[Candidate, ...], Mapping[str, Any]]:
        """One compact frontier call; its final citations are re-checked here."""
        outcome = self._call(
            round_id=round_id,
            model=self.strong_model,
            rank=0,
            role=Role.ADJUDICATOR,
            work_kind=work_kind,
            subject_id=subject_id,
            prompt=build_adjudicator_prompt(
                work_kind=work_kind,
                subject_id=subject_id,
                clusters=clusters,
                unverified=unverified,
                evidence=evidence,
            ),
            verify=verify,
        )
        if not outcome.call.usable:
            return (
                outcome,
                (),
                {
                    "status": "unavailable",
                    "reason": outcome.call.error or "quarantined_fallback",
                    "call_id": outcome.call.call_id,
                    "requested_model": outcome.call.requested_model,
                    "served_model": outcome.call.served_model,
                    "selected": 0,
                },
            )
        try:
            document = _json_document(outcome.text)
        except CandidateParseError as exc:
            return (
                outcome,
                (),
                {
                    "status": "unparsed",
                    "reason": str(exc),
                    "call_id": outcome.call.call_id,
                    "selected": 0,
                },
            )
        verdicts = _verdicts(document)
        finals = document.get("final") if isinstance(document, Mapping) else None
        finals = finals if isinstance(finals, list) else []
        accepted: list[Candidate] = []
        dropped: list[dict[str, Any]] = []
        for index, raw in enumerate(finals[:MAX_ADJUDICATED_CANDIDATES], 1):
            candidate = build_candidate(
                raw,
                index=index,
                model=self.strong_model,
                rank=0,
                work_kind=work_kind,
                subject_id=subject_id,
                verify=verify,
            )
            if candidate is None:
                dropped.append({"reason": "malformed_final"})
                continue
            if not candidate.citation_verified:
                dropped.append(
                    {"candidate_id": candidate.candidate_id, "reason": "citation_unverified"}
                )
                continue
            accepted.append(candidate)
        if not accepted and verdicts:
            accepted = [
                member
                for member in (cluster.representative for cluster in clusters)
                if member.citation_verified
                and member.candidate_id in _accepted_ids(verdicts)
            ]
        accepted_ids = {candidate.candidate_id for candidate in accepted}
        return (
            outcome,
            tuple(accepted),
            {
                "status": "completed",
                "call_id": outcome.call.call_id,
                "requested_model": outcome.call.requested_model,
                "served_model": outcome.call.served_model,
                "response_sha256": outcome.call.response_sha256,
                "verdicts": verdicts,
                "accepted_candidate_ids": sorted(accepted_ids),
                "rejected_candidate_ids": sorted(
                    _rejected_ids(verdicts) - {candidate.candidate_id for candidate in accepted}
                ),
                "dropped": dropped,
                "selected": len(accepted),
            },
        )

    # -- events ------------------------------------------------------------- #

    def _emit(
        self,
        *,
        run_id: str,
        round_id: str,
        swarm_round: SwarmRound,
        selected: Sequence[Candidate],
        adjudication: Mapping[str, Any] | None,
        escalated: bool,
        aggregation: Aggregation | None,
        retrieval: Sequence[RetrievalRecord],
        dropped: int,
        frozen_eval_token: str | None,
    ) -> tuple[str, ...]:
        """Journal the round, then the adjudication when one happened."""
        sink = self.queue
        if sink is None:
            return ()
        payload = {
            **swarm_round.as_payload(),
            "aggregation": None if aggregation is None else aggregation.value,
            "selected": [candidate.as_payload() for candidate in selected],
            "escalated": escalated,
            "retrieval": {
                "records": [record.as_payload() for record in retrieval],
                "dropped_sealed": dropped,
            },
        }
        events = [
            self._append(
                sink,
                run_id=run_id,
                round_id=round_id,
                event_type=EVENT_ROUND_COMPLETED,
                status="applied",
                payload=payload,
                frozen_eval_token=frozen_eval_token,
            )
        ]
        if adjudication is not None:
            events.append(
                self._append(
                    sink,
                    run_id=run_id,
                    round_id=round_id,
                    event_type=EVENT_ADJUDICATED,
                    status="confirmed" if selected else "refuted",
                    payload={
                        "dataset_id": swarm_round.dataset_id,
                        "item_id": swarm_round.item_id,
                        "split": swarm_round.split,
                        "round_id": round_id,
                        "mode": swarm_round.mode.value,
                        "work_kind": swarm_round.work_kind.value,
                        "subject_id": swarm_round.subject_id,
                        "escalated": escalated,
                        "adjudication": dict(adjudication),
                        "selected": [candidate.as_payload() for candidate in selected],
                    },
                    frozen_eval_token=frozen_eval_token,
                )
            )
        return tuple(event for event in events if event)

    def _append(
        self,
        sink: EventSink,
        *,
        run_id: str,
        round_id: str,
        event_type: str,
        status: str,
        payload: Mapping[str, Any],
        frozen_eval_token: str | None,
    ) -> str:
        event = {
            "event_id": str(
                uuid5(NAMESPACE_URL, f"evallab.swarm.event:{round_id}:{event_type}")
            ),
            "run_id": run_id,
            "system": "swarm",
            "event_type": event_type,
            "seq": 0,
            "ts": _now(),
            "prefix_step": None,
            "status": status,
            "payload": dict(payload),
        }
        sink.append_event(
            event,
            idempotency_key=f"swarm:{round_id}:{event_type}",
            frozen_eval_token=frozen_eval_token,
        )
        return str(event["event_id"])


def _sum_known(values: Any) -> int | None:
    """Sum reported counts, staying ``None`` when any count was never reported."""
    total = 0
    for value in values:
        if value is None:
            return None
        total += value
    return total


def _verdicts(document: Any) -> list[dict[str, Any]]:
    if not isinstance(document, Mapping):
        return []
    raw = document.get("verdicts")
    if not isinstance(raw, list):
        return []
    verdicts: list[dict[str, Any]] = []
    for value in raw:
        if not isinstance(value, Mapping):
            continue
        candidate_id = value.get("candidate_id")
        decision = value.get("decision")
        if not isinstance(candidate_id, str) or decision not in ("accept", "reject"):
            continue
        reason = value.get("reason")
        verdicts.append(
            {
                "candidate_id": candidate_id,
                "decision": decision,
                "reason": reason if isinstance(reason, str) else None,
            }
        )
    return verdicts


def _accepted_ids(verdicts: Sequence[Mapping[str, Any]]) -> set[str]:
    return {
        str(verdict["candidate_id"])
        for verdict in verdicts
        if verdict.get("decision") == "accept"
    }


def _rejected_ids(verdicts: Sequence[Mapping[str, Any]]) -> set[str]:
    return {
        str(verdict["candidate_id"])
        for verdict in verdicts
        if verdict.get("decision") == "reject"
    }
