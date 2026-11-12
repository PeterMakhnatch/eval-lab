"""Independent model-call capture for Harbor trials.

Harness trajectories are agent-controlled: installed agents write them inside
the container (``/logs/agent``), a root agent can delete them, and timeouts or
cancels can skip conversion, so a missing trajectory is ambiguous on its own.
The vendor proxies (``containers/*_secret_proxy.py``) record usage only, never
bodies. This module owns the independent record: a launcher-agnostic recording
reverse proxy (host process) plus attribution of captured calls to Harbor
trials and a per-trial completeness verdict against the harness ATIF.

Layout of a capture directory::

    <capture_dir>/calls.jsonl      one JSON record per proxied call (append + flush)
    <capture_dir>/capture.json     manifest written at startup
    <capture_dir>/provenance.json  ProvenanceMetadata-compatible sidecar at close

``link`` writes derived Parquet (``model_calls``, ``trial_capture``) plus a
``capture_link.json`` receipt under the resolved derived root, next to the
existing ``job_id=<uuid>/`` projections.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROXY_VERSION = "1.0.0"
LINK_RECEIPT_SCHEMA = "evallab.model_capture_link/v1"

DEFAULT_PORT = 0
DEFAULT_BIND = "127.0.0.1"

#: ATIF trajectory candidates, mirroring
#: ``evallab.evidence.capture_authority`` so both readers agree on filenames.
TRAJECTORY_CANDIDATES = ("agent/trajectory.json", "trajectory.json", "agent/trajectory.jsonl")

#: Control agents never emit trajectories and never call models; both records
#: empty is their normal state, not missing evidence.
CONTROL_AGENTS = frozenset({"nop", "oracle"})

_WS_RE = re.compile(r"\s+")
_ROUTE_RE = re.compile(r"^/t/([^/]{1,128})/(.*)$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")

#: Request headers kept on the record. Auth-shaped headers are never selected,
#: so client credentials cannot reach ``calls.jsonl`` by header.

#: Headers never forwarded upstream alongside an injected provider key.
_AUTH_HEADERS = frozenset(
    {"authorization", "x-api-key", "api-key", "cookie", "proxy-authorization"}
)

_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 ``Z`` string."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp; ``None`` when absent or malformed."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def normalize_text(value: Any) -> str:
    """Collapse whitespace for stable cross-record comparison."""
    if not isinstance(value, str):
        return ""
    return _WS_RE.sub(" ", value).strip()


def sha256_file(path: Path) -> str:
    """Hex SHA-256 of a file's bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def classify_kind(path: str) -> str:
    """Classify an upstream path by suffix (route prefix already stripped)."""
    clean = path.split("?", 1)[0].rstrip("/") or "/"
    if clean.endswith("/chat/completions"):
        return "chat"
    if clean.endswith("/api/chat"):
        return "ollama_chat"
    if clean.endswith("/completions"):
        return "legacy_completions"
    if clean.endswith("/responses"):
        return "responses"
    if clean.endswith("/messages"):
        return "anthropic_messages"
    return "unknown"


# --------------------------------------------------------------------------- #
# Request message extraction (shared by attribution and verdicts)
# --------------------------------------------------------------------------- #


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                for key in ("text", "input_text", "output_text"):
                    if isinstance(part.get(key), str):
                        parts.append(str(part[key]))
        return "".join(parts)
    return ""


def request_message_pairs(kind: str, body: Any) -> list[tuple[str, str]]:
    """Ordered ``(role, text)`` pairs of one request body, best-effort."""
    if not isinstance(body, dict):
        return []
    pairs: list[tuple[str, str]] = []
    if kind in ("chat", "ollama_chat", "unknown"):
        messages = body.get("messages")
        if isinstance(messages, list):
            for item in messages:
                if not isinstance(item, dict):
                    continue
                role = item.get("role")
                text = _content_text(item.get("content"))
                if isinstance(role, str) and text:
                    pairs.append((role, text))
    elif kind == "legacy_completions":
        prompt = body.get("prompt")
        if isinstance(prompt, str) and prompt:
            pairs.append(("user", prompt))
        elif isinstance(prompt, list):
            for item in prompt:
                if isinstance(item, str) and item:
                    pairs.append(("user", item))
    elif kind == "responses":
        for item in _responses_input_texts(body.get("input")):
            pairs.append(item)
    elif kind == "anthropic_messages":
        system = body.get("system")
        if isinstance(system, str) and system:
            pairs.append(("system", system))
        elif isinstance(system, list):
            text = _content_text(system)
            if text:
                pairs.append(("system", text))
        messages = body.get("messages")
        if isinstance(messages, list):
            for item in messages:
                if not isinstance(item, dict):
                    continue
                role = item.get("role")
                text = _content_text(item.get("content"))
                if isinstance(role, str) and text:
                    pairs.append((role, text))
    return pairs


def _responses_input_texts(value: Any) -> list[tuple[str, str]]:
    if isinstance(value, str) and value:
        return [("user", value)]
    if isinstance(value, list):
        out: list[tuple[str, str]] = []
        for item in value:
            if isinstance(item, str) and item:
                out.append(("user", item))
            elif isinstance(item, dict):
                role = item.get("role")
                if isinstance(role, str):
                    text = _content_text(item.get("content"))
                    if text:
                        out.append((role, text))
        return out
    return []


def first_user_text(kind: str, body: Any) -> str:
    """Normalized first user-role text of a request, or ``""``."""
    for role, text in request_message_pairs(kind, body):
        if role == "user" and normalize_text(text):
            return normalize_text(text)
    return ""


# --------------------------------------------------------------------------- #
# Response extraction: one shared SSE + usage canonicalization
# --------------------------------------------------------------------------- #
# Mirrors the behavior of the vendor proxies (``containers/*_secret_proxy.py``,
# ``_canonicalize_sse_and_usage``): split on blank lines, read ``data:`` lines,
# skip ``[DONE]``, parse each event as JSON. Unlike the metered proxies this
# module never rejects an upstream body shape: unparsable events are skipped
# for extraction but the raw bytes are always recorded and forwarded.


# --------------------------------------------------------------------------- #
# Recorder, manifest, provenance
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Recording reverse proxy
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Link: attribute captured calls to trials, judge completeness
# --------------------------------------------------------------------------- #


@dataclass
class TrialEvidence:
    """Everything ``link`` knows about one Harbor trial."""

    name: str
    trial_id: str | None
    directory: Path
    agent: str | None
    window_start: datetime | None
    window_end: datetime | None
    attempt_id: str | None = None
    atif_session: str | None = None
    atif_user_texts: list[str] = field(default_factory=list)
    atif_agent_texts: list[str] = field(default_factory=list)
    atif_agent_steps: int = 0
    atif_present: bool = False
    instruction: str | None = None


def iter_trial_dirs(job_dir: str | Path) -> list[Path]:
    """Trial directories directly under a Harbor job directory, sorted by name."""
    job = Path(job_dir)
    found: list[Path] = []
    if not job.is_dir():
        return found
    for child in sorted(job.iterdir()):
        if not child.is_dir():
            continue
        result_path = child / "result.json"
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("trial_name"):
            found.append(child)
    return found


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _atif_documents(trial_dir: Path) -> list[dict[str, Any]]:
    """Load the ATIF document plus Harbor continuation segments, best-effort."""
    documents: list[dict[str, Any]] = []
    for candidate in TRAJECTORY_CANDIDATES:
        path = trial_dir / candidate
        if not path.is_file():
            continue
        if path.suffix == ".jsonl":
            steps: list[Any] = []
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        steps.append(json.loads(line))
            except (OSError, ValueError):
                continue
            documents.append({"steps": steps})
        else:
            payload = _load_json(path)
            if payload is not None:
                documents.append(payload)
        break
    if documents:
        root = trial_dir / TRAJECTORY_CANDIDATES[0]
        base = root.parent
        for extra in sorted(base.glob("trajectory.cont-*.json")):
            payload = _load_json(extra)
            if payload is not None:
                documents.append(payload)
    return documents


def _resolve_instruction(result: dict[str, Any], job_dir: Path) -> str | None:
    """Normalized task instruction for a trial, via the job's task path."""
    config = result.get("config")
    if not isinstance(config, dict):
        return None
    task = config.get("task")
    rel: str | None = None
    if isinstance(task, str) and task:
        rel = task
    elif isinstance(task, dict) and isinstance(task.get("path"), str):
        rel = str(task["path"])
    if not rel:
        return None
    candidate = Path(rel)
    if not candidate.is_absolute():
        from evallab.storage.paths import shared_checkout_root

        candidate = shared_checkout_root(job_dir.resolve()) / rel / "instruction.md"
    else:
        candidate = candidate / "instruction.md"
    try:
        text = candidate.read_text(encoding="utf-8")
    except OSError:
        return None
    return normalize_text(text) or None


def _job_attempt_id(job_dir: Path) -> str | None:
    """Job attempt id the runner's secret proxy stamps as the capture route token."""
    metadata = _load_json(job_dir / "lab-metadata.json")
    usage = metadata.get("provider_usage") if isinstance(metadata, dict) else None
    if isinstance(usage, dict):
        attempt = usage.get("attempt_id")
        if isinstance(attempt, str) and attempt.strip():
            return attempt.strip()
    return None


def collect_trial_evidence(trial_dir: str | Path, job_dir: str | Path) -> TrialEvidence:
    """Gather ATIF texts, the agent-execution window, and the instruction."""
    directory = Path(trial_dir)
    result = _load_json(directory / "result.json") or {}
    config = result.get("config") if isinstance(result.get("config"), dict) else {}
    agent_cfg = config.get("agent") if isinstance(config, dict) else None
    agent = None
    if isinstance(agent_cfg, dict) and isinstance(agent_cfg.get("name"), str):
        agent = str(agent_cfg["name"])
    if agent is None:
        agent_info = result.get("agent_info")
        if isinstance(agent_info, dict) and isinstance(agent_info.get("name"), str):
            agent = str(agent_info["name"])
    window = result.get("agent_execution")
    window_start = window_end = None
    if isinstance(window, dict):
        window_start = parse_ts(window.get("started_at"))
        window_end = parse_ts(window.get("finished_at"))
    attempt_id = _job_attempt_id(Path(job_dir))
    evidence = TrialEvidence(
        name=str(result.get("trial_name") or directory.name),
        trial_id=str(result["id"]) if isinstance(result.get("id"), str) else None,
        directory=directory,
        agent=agent,
        window_start=window_start,
        window_end=window_end,
        attempt_id=attempt_id,
    )
    documents = _atif_documents(directory)
    if documents:
        first = documents[0]
        session = first.get("session_id")
        evidence.atif_session = str(session) if isinstance(session, str) and session else None
        for document in documents:
            steps = document.get("steps")
            if not isinstance(steps, list):
                continue
            for step in steps:
                if not isinstance(step, dict):
                    continue
                source = step.get("source")
                message = step.get("message")
                if message is None:
                    text = ""
                elif isinstance(message, str):
                    text = normalize_text(message)
                else:
                    text = normalize_text(json.dumps(message, sort_keys=True))
                if source == "agent":
                    evidence.atif_agent_steps += 1
                    if text:
                        evidence.atif_agent_texts.append(text)
                elif source == "user" and text:
                    evidence.atif_user_texts.append(text)
        # Present means a trajectory document with a non-empty step list was
        # readable; agent-step counts below decide truncated vs complete.
        evidence.atif_present = any(
            isinstance(document.get("steps"), list) and len(document["steps"]) > 0
            for document in documents
        )
    evidence.instruction = _resolve_instruction(result, Path(job_dir))
    return evidence


def _normalized_pairs(kind: str, body: Any) -> list[tuple[str, str]]:
    return [(role, normalize_text(text)) for role, text in request_message_pairs(kind, body)]


def _is_prefix(shorter: list[tuple[str, str]], longer: list[tuple[str, str]]) -> bool:
    if not shorter or len(shorter) > len(longer):
        return False
    return longer[: len(shorter)] == shorter


def _conversations(calls: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group calls into conversations by message-prefix chaining (time order)."""
    ordered = sorted(calls, key=lambda c: (str(c.get("started_at") or ""), int(c.get("seq") or 0)))
    groups: list[list[dict[str, Any]]] = []
    tails: list[list[tuple[str, str]]] = []
    for call in ordered:
        pairs = _normalized_pairs(str(call.get("kind") or kind_of(call)), call.get("request_body"))
        placed = False
        for index, tail in enumerate(tails):
            if _is_prefix(tail, pairs) or _is_prefix(pairs, tail):
                groups[index].append(call)
                if len(pairs) > len(tail):
                    tails[index] = pairs
                placed = True
                break
        if not placed:
            groups.append([call])
            tails.append(pairs)
    return groups


def kind_of(call: dict[str, Any]) -> str:
    """Classify a stored call record by its (stripped) path."""
    path = call.get("path")
    return classify_kind(path if isinstance(path, str) else "")


def _anchor_match(root_first_user: str, trial: TrialEvidence) -> bool:
    if not root_first_user:
        return False
    if root_first_user in trial.atif_user_texts:
        return True
    if trial.instruction and root_first_user == trial.instruction:
        return True
    # full instruction.md inside a larger first user message), so containment
    # either way anchors — guarded so short greetings cannot match vacuously.
    instruction = trial.instruction or ""
    if len(instruction) < _MIN_ANCHOR_CHARS or len(root_first_user) < _MIN_ANCHOR_CHARS:
        return False
    return instruction in root_first_user or root_first_user in instruction


def _window_contains(trial: TrialEvidence, start: datetime | None, end: datetime | None) -> bool:
    if trial.window_start is None or trial.window_end is None:
        return False
    if start is None or end is None:
        return False
    return trial.window_start <= start and end <= trial.window_end


@dataclass
class Attribution:
    """Call-to-trial assignment produced by ``link_capture``."""

    assigned: dict[int, str] = field(default_factory=dict)
    method: dict[int, str] = field(default_factory=dict)
    unassigned: list[int] = field(default_factory=list)
    ambiguous_trials: set[str] = field(default_factory=set)
    ambiguous_conversations: int = 0


def attribute_calls(calls: list[dict[str, Any]], trials: list[TrialEvidence]) -> Attribution:
    """Assign calls to trials: route token, then session, then conversation chaining.

    A present route token foreign to this job stays unassigned (never chained).
    """
    by_name: dict[str, list[TrialEvidence]] = {}
    for trial in trials:
        by_name.setdefault(trial.name, []).append(trial)
    by_session: dict[str, list[TrialEvidence]] = {}
    for trial in trials:
        if trial.atif_session:
            by_session.setdefault(trial.atif_session, []).append(trial)
    attribution = Attribution()
    pending: list[dict[str, Any]] = []
    for call in calls:
        seq = int(call.get("seq") or 0)
        token = call.get("route_token")
        matched: list[TrialEvidence] | None = None
        how = ""
        if isinstance(token, str) and token:
            candidates = [t for t in trials if t.name == token or t.trial_id == token]
            if len(candidates) == 1:
                matched, how = candidates, "route_token"
            elif len(candidates) > 1:
                for candidate in candidates:
                    attribution.ambiguous_trials.add(candidate.name)
                attribution.unassigned.append(seq)
                continue
            else:
                # Runner-stamped job attempt id (secret proxy ``/t/<token>/``
                # prefix): exact for one-trial jobs. A multi-trial job shares
                # one attempt id, so those calls fall through to conversation
                # chaining instead of going ambiguous here. A token foreign
                # to this job (another job's calls in a shared capture)
                # stays unassigned here: it must never be claimed by
                # session/conversation chaining below.
                job_candidates = [t for t in trials if t.attempt_id == token]
                if len(job_candidates) == 1:
                    matched, how = job_candidates, "route_token"
                elif not job_candidates:
                    attribution.unassigned.append(seq)
                    continue
        if matched is None and isinstance(call.get("session_id"), str) and call["session_id"]:
            candidates = by_session.get(str(call["session_id"]), [])
            if len(candidates) == 1:
                matched, how = candidates, "session"
            elif len(candidates) > 1:
                for candidate in candidates:
                    attribution.ambiguous_trials.add(candidate.name)
                attribution.unassigned.append(seq)
                continue
        if matched is not None:
            attribution.assigned[seq] = matched[0].name
            attribution.method[seq] = how
        else:
            pending.append(call)
    for conversation in _conversations(pending):
        first = conversation[0]
        root_user = first_user_text(kind_of(first), first.get("request_body"))
        starts = [parse_ts(c.get("started_at")) for c in conversation]
        ends = [parse_ts(c.get("ended_at")) for c in conversation]
        conv_start = min((s for s in starts if s is not None), default=None)
        conv_end = max((e for e in ends if e is not None), default=None)
        candidates = [
            trial
            for trial in trials
            if _anchor_match(root_user, trial) and _window_contains(trial, conv_start, conv_end)
        ]
        if len(candidates) == 1:
            for call in conversation:
                seq = int(call.get("seq") or 0)
                attribution.assigned[seq] = candidates[0].name
                attribution.method[seq] = "conversation"
        else:
            for call in conversation:
                attribution.unassigned.append(int(call.get("seq") or 0))
            if len(candidates) > 1:
                attribution.ambiguous_conversations += 1
                for candidate in candidates:
                    attribution.ambiguous_trials.add(candidate.name)
    return attribution


# --------------------------------------------------------------------------- #
# Completeness verdicts
# --------------------------------------------------------------------------- #

#: Per-trial completeness of the harness trajectory against the independent
#: record. ``capture_missing`` also covers the both-absent corner (no ATIF and
#: no captured calls): with zero evidence on either side an idle control agent
#: is indistinguishable from a full bypass, so the operator must check the
#: ``agent_execution`` window in ``result.json``; the receipt says so.


def _captured_texts(calls: list[dict[str, Any]]) -> list[str]:
    texts: list[str] = []
    for call in calls:
        for text in call.get("assistant_texts") or []:
            normalized = normalize_text(text)
            if normalized:
                texts.append(normalized)
    return texts


def _contains(needle: str, haystack: str) -> bool:
    return needle in haystack or haystack in needle


_MIN_ANCHOR_CHARS = 64


_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_MIN_ALNUM_MATCH = 32


def _alnum(text: str) -> str:
    """Lowercased alphanumeric skeleton: immune to code fences, JSON syntax,
    key renaming (``"analysis": "x"`` vs ``Analysis: x``), and whitespace."""
    return _ALNUM_RE.sub("", text.lower())


def _turn_present(text: str, atif_texts: list[str]) -> bool:
    """Whether a captured turn survives anywhere in the ATIF agent messages.

    Plain containment covers verbatim records; the alphanumeric fallback covers
    harnesses that store a parsed rendering (e.g. Terminus keeps ``Analysis: …``
    while the model emitted fenced ``{"analysis": "…"}``). The fallback needs a
    32-char skeleton on both sides so short greetings cannot match vacuously.
    """
    if any(_contains(text, agent_text) for agent_text in atif_texts):
        return True
    skeleton = _alnum(text)
    if len(skeleton) < _MIN_ALNUM_MATCH:
        return False
    for agent_text in atif_texts:
        agent_skeleton = _alnum(agent_text)
        if len(agent_skeleton) < _MIN_ALNUM_MATCH:
            continue
        if skeleton in agent_skeleton or agent_skeleton in skeleton:
            return True
    return False


def judge_trial(
    trial: TrialEvidence, calls: list[dict[str, Any]], *, ambiguous: bool
) -> dict[str, Any]:
    """Judge one trial; ``calls`` are the captured calls assigned to it."""
    texts = _captured_texts(calls)
    verdict: str
    first_divergence: dict[str, Any] | None = None
    detail = ""
    if ambiguous:
        verdict = "ambiguous"
        detail = "attribution matched more than one trial; calls left unassigned"
    elif not trial.atif_present and texts:
        verdict = "trajectory_missing"
        detail = "calls captured but the harness trajectory is absent or empty"
    elif trial.atif_present and not texts and not calls:
        verdict = "capture_missing"
        detail = "harness trajectory present but no calls captured (proxy not in path?)"
    elif not trial.atif_present and not calls:
        verdict = "capture_missing"
        detail = (
            "no harness trajectory and no captured calls; idle control, pre-call "
            "crash, or full bypass — check the agent_execution window"
        )
    elif len(texts) > trial.atif_agent_steps:
        verdict = "trajectory_truncated"
        detail = (
            f"fewer ATIF agent steps ({trial.atif_agent_steps}) than captured "
            f"assistant turns ({len(texts)})"
        )
        first_divergence = {"kind": "count_mismatch"}
    else:
        missing_index: int | None = None
        for index, text in enumerate(texts):
            if not _turn_present(text, trial.atif_agent_texts):
                missing_index = index
                break
        if missing_index is not None:
            verdict = "trajectory_truncated"
            excerpt = texts[missing_index][:280]
            detail = f"captured assistant turn {missing_index} absent from the ATIF"
            first_divergence = {
                "kind": "missing_turn",
                "turn_index": missing_index,
                "excerpt": excerpt,
            }
        else:
            verdict = "complete"
            detail = "captured turns agree with the harness trajectory"
    return {
        "trial_name": trial.name,
        "trial_id": trial.trial_id,
        "agent": trial.agent,
        "verdict": verdict,
        "detail": detail,
        "captured_calls": len(calls),
        "captured_assistant_turns": len(texts),
        "atif_agent_steps": trial.atif_agent_steps,
        "atif_present": trial.atif_present,
        "first_divergence": first_divergence,
    }


# --------------------------------------------------------------------------- #
# link: Parquet output and receipt
# --------------------------------------------------------------------------- #


def _model_calls_schema() -> Any:
    import pyarrow as pa

    return pa.schema(
        [
            ("seq", pa.int64()),
            ("trial_name", pa.string()),
            ("trial_id", pa.string()),
            ("attribution", pa.string()),
            ("started_at", pa.string()),
            ("ended_at", pa.string()),
            ("method", pa.string()),
            ("path", pa.string()),
            ("route_token", pa.string()),
            ("session_id", pa.string()),
            ("model", pa.string()),
            ("response_status", pa.int64()),
            ("response_sse", pa.bool_()),
            ("assistant_turns", pa.int64()),
            ("tool_calls", pa.int64()),
            ("prompt_tokens", pa.int64()),
            ("completion_tokens", pa.int64()),
            ("upstream_latency_s", pa.float64()),
            ("error_kind", pa.string()),
        ]
    )


def _trial_capture_schema() -> Any:
    import pyarrow as pa

    return pa.schema(
        [
            ("trial_name", pa.string()),
            ("trial_id", pa.string()),
            ("agent", pa.string()),
            ("verdict", pa.string()),
            ("captured_calls", pa.int64()),
            ("captured_assistant_turns", pa.int64()),
            ("atif_agent_steps", pa.int64()),
            ("atif_present", pa.bool_()),
            ("first_divergence", pa.string()),
            ("capture_digest", pa.string()),
            ("linked_at", pa.string()),
        ]
    )


def _usage_int(usage: Any, key: str) -> int | None:
    if isinstance(usage, dict) and isinstance(usage.get(key), int):
        return int(usage[key])
    return None


def link_capture(
    capture_dir: str | Path,
    job_dir: str | Path,
    *,
    derived_root: str | Path | None = None,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Attribute a capture to a job's trials; write Parquet + receipt; return summary."""
    from evallab.evidence.parquet_io import write_table_atomic
    from evallab.storage.fs import durable_mkdir
    from evallab.storage.paths import derived_root_from_environment

    capture_path = Path(capture_dir)
    job_path = Path(job_dir)
    calls: list[dict[str, Any]] = []
    calls_path = capture_path / "calls.jsonl"
    if calls_path.is_file():
        with calls_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    calls.append(payload)
    trials = [collect_trial_evidence(trial, job_path) for trial in iter_trial_dirs(job_path)]
    attribution = attribute_calls(calls, trials)
    by_seq = {int(c.get("seq") or 0): c for c in calls}
    trial_calls: dict[str, list[dict[str, Any]]] = {trial.name: [] for trial in trials}
    for seq, name in attribution.assigned.items():
        if seq in by_seq and name in trial_calls:
            trial_calls[name].append(by_seq[seq])
    judgments = [
        judge_trial(
            trial, trial_calls[trial.name], ambiguous=trial.name in attribution.ambiguous_trials
        )
        for trial in trials
    ]
    capture_digest = (
        f"sha256:{sha256_file(calls_path)}" if calls_path.is_file() else "sha256:" + "0" * 64
    )
    linked_at = utc_now_iso()
    base = Path(derived_root) if derived_root is not None else None
    if base is None:
        anchor = Path(repo_root) if repo_root is not None else job_path
        base = derived_root_from_environment(anchor, notify=lambda _message: None)
    job_result = _load_json(job_path / "result.json") or {}
    job_id = job_result.get("id") if isinstance(job_result.get("id"), str) else None
    leaf = f"job_id={job_id}" if job_id else f"job_name={job_path.name}"
    out_dir = base / leaf
    durable_mkdir(out_dir)
    model_rows: list[dict[str, Any]] = []
    for call in sorted(calls, key=lambda c: int(c.get("seq") or 0)):
        seq = int(call.get("seq") or 0)
        trial_name = attribution.assigned.get(seq)
        trial_id = next((t.trial_id for t in trials if t.name == trial_name), None)
        error = call.get("error")
        model_rows.append(
            {
                "seq": seq,
                "trial_name": trial_name,
                "trial_id": trial_id,
                "attribution": attribution.method.get(seq, "unassigned"),
                "started_at": call.get("started_at"),
                "ended_at": call.get("ended_at"),
                "method": call.get("method"),
                "path": call.get("path"),
                "route_token": call.get("route_token"),
                "session_id": call.get("session_id"),
                "model": call.get("model"),
                "response_status": call.get("response_status"),
                "response_sse": bool(call.get("response_sse")),
                "assistant_turns": len(
                    [t for t in (call.get("assistant_texts") or []) if normalize_text(t)]
                ),
                "tool_calls": len(call.get("tool_calls") or []),
                "prompt_tokens": _usage_int(call.get("usage"), "prompt_tokens"),
                "completion_tokens": _usage_int(call.get("usage"), "completion_tokens"),
                "upstream_latency_s": call.get("upstream_latency_s"),
                "error_kind": error.get("kind") if isinstance(error, dict) else None,
            }
        )
    trial_rows: list[dict[str, Any]] = []
    for judgment in judgments:
        trial_rows.append(
            {
                "trial_name": judgment["trial_name"],
                "trial_id": judgment["trial_id"],
                "agent": judgment["agent"],
                "verdict": judgment["verdict"],
                "captured_calls": judgment["captured_calls"],
                "captured_assistant_turns": judgment["captured_assistant_turns"],
                "atif_agent_steps": judgment["atif_agent_steps"],
                "atif_present": judgment["atif_present"],
                "first_divergence": json.dumps(judgment["first_divergence"], sort_keys=True)
                if judgment["first_divergence"] is not None
                else None,
                "capture_digest": capture_digest,
                "linked_at": linked_at,
            }
        )
    write_table_atomic(out_dir / "model_calls.parquet", model_rows, _model_calls_schema())
    write_table_atomic(out_dir / "trial_capture.parquet", trial_rows, _trial_capture_schema())
    receipt = {
        "schema": LINK_RECEIPT_SCHEMA,
        "job": job_path.name,
        "job_id": job_id,
        "capture_dir": str(capture_path),
        "capture_digest": capture_digest,
        "linked_at": linked_at,
        "proxy_version": PROXY_VERSION,
        "parquet_dir": str(out_dir),
        "calls_total": len(calls),
        "calls_assigned": len(attribution.assigned),
        "calls_unassigned": sorted(attribution.unassigned),
        "ambiguous_trials": sorted(attribution.ambiguous_trials),
        "ambiguous_conversations": attribution.ambiguous_conversations,
        "trials": judgments,
    }
    (out_dir / "capture_link.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return receipt


# --------------------------------------------------------------------------- #
# Report lookup
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Operator smoke: one live call through secret proxy -> capture -> upstream
# --------------------------------------------------------------------------- #


SMOKE_KEY_ENV = "MIMO_SELFHOSTED_API_KEY"


