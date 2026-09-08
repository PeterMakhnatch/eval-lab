"""Read-only run and analysis explorer (M005).

Assembles linked, provenance-labeled views over the evidence the lab already
holds — raw Harbor job directories, analysis sidecars, and (optionally) the
status snapshot — so an operator can select a task, job, trial, trajectory,
or analysis and understand what ran, what happened, why it was classified,
and the exact safe command for the next action.

Guarantees, enforced by tests:

- **Zero writes.** Every loader opens files read-only; building an index
  leaves the evidence byte-identical.
- **Every field labels its provenance**: ``observed`` (read from evidence),
  ``derived`` (computed here), ``draft`` (unreviewed model output),
  ``unavailable`` (missing/malformed, with a reason).
- **Infrastructure exceptions are never conflated with reward failures.**
- **Path jail.** Nothing outside the configured roots is ever read or
  linked; ``..`` escapes resolve to a refusal, not a file.
- **No secrets, no hidden verifier content.** Key-shaped names are redacted
  from any rendered mapping; task ``tests/`` and ``solution/`` contents are
  never listed or read.
- **Withheld evidence is never rendered as present.** Promotion replaces the
  prompt text of ``system``/``user`` steps with
  ``<<evallab-redacted: N bytes, sha256:...>>``
  (``scripts/promote_codex_bundle.py``, rule R1). Every step and every
  citation therefore carries a three-state content label — ``observed``
  (readable), ``withheld`` (removed on purpose, byte count and digest kept so
  the claim stays auditable), ``unavailable`` (genuinely absent) — so a
  reader can never mistake a citation into a redacted prompt for a citation
  into real agent behaviour.
- **Runs are found or named, never dropped.** ``ExperimentSpec.jobs_dir`` is
  free-form (``schemas.py:27``) while jobs are addressed as
  ``<jobs_root>/<job>/<trial>``, so a nested run is reported with its exact
  location instead of vanishing behind an empty job (F-04).
- **Next Action emits commands, never executes them.**
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from evallab.labels import ReviewQueueItem, select_review_queue
from evallab.registry import TaskRegistry
from evallab.schemas import ANALYSIS_SIDECAR_FILENAME, TrialAnalysisSidecar
from evallab.traj import TrajectoryOutline, outline_trajectory

Provenance = Literal["observed", "derived", "draft", "withheld", "unavailable"]

_SECRET_MARKERS = (
    "API_KEY",
    "API_TOKEN",
    "_SECRET",
    "ACCESS_KEY",
    "PASSWORD",
    "TOKEN",
    "OAUTH",
    "SESSION",
)
_SECRET_TEXT_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\b(?:sk|rk)-[A-Za-z0-9._-]+"),
    re.compile(
        r"(?i)\b(?:api[_ -]?key|access[_ -]?token|oauth[_ -]?token|"
        r"session[_ -]?token|password|secret)(\s*[:=]\s*)[^\s,;]+"
    ),
)
_HIDDEN_TASK_DIRS = frozenset({"tests", "solution"})
_VERIFY_HINTS = ("pytest", "test", "verify", "check", "lint", "validate")
CONTROL_AGENTS = frozenset({"oracle", "nop"})

# Emitted by scripts/promote_codex_bundle.py (_marker) for every string it
# removes, in trajectory prompt text (R1) and oversize verifier strings (R3a).
# The byte count and digest are the whole audit trail for the removed text, so
# anything that reports withheld content must carry them through verbatim.
_REDACTION_MARKER = re.compile(
    r"<<evallab-redacted: (?P<bytes>\d+) bytes, (?P<digest>sha256:[0-9a-f]{64})>>"
)
# A cited file is read only to look for markers; anything larger than this is
# reported as unclassified rather than pulled into memory by a page render.
_MARKER_SCAN_LIMIT_BYTES = 4 * 1024 * 1024
# Jobs are addressed exactly as ``<jobs_root>/<job_name>/<trial>``. When a
# directory at that level is not a job, this is how far below it the explorer
# looks *to name* a run it will not render. Diagnostic only — nothing found by
# this probe ever enters the index.
_NESTED_PROBE_DEPTH = 4


@dataclass(frozen=True)
class Labeled:
    """A value plus the provenance label the UI must render beside it."""

    value: Any
    provenance: Provenance
    reason: str | None = None


@dataclass(frozen=True)
class Withheld:
    """One ``<<evallab-redacted: N bytes, sha256:...>>`` marker, parsed."""

    byte_count: int
    digest: str


def observed(value: Any) -> Labeled:
    return Labeled(value, "observed")


def derived(value: Any, reason: str | None = None) -> Labeled:
    return Labeled(value, "derived", reason)


def unavailable(reason: str) -> Labeled:
    return Labeled(None, "unavailable", reason)


def withheld(markers: Sequence[Withheld], *, readable_chars: int, what: str) -> Labeled:
    """Label content that promotion removed on purpose, keeping its audit trail."""
    total = sum(m.byte_count for m in markers)
    detail = "; ".join(f"{m.byte_count} bytes {m.digest}" for m in markers)
    scope = "partly withheld" if readable_chars else "withheld"
    return Labeled(
        {
            "readable_chars": readable_chars,
            "withheld_bytes": total,
            "markers": tuple({"bytes": m.byte_count, "digest": m.digest} for m in markers),
        },
        "withheld",
        f"{what} {scope} by redaction before promotion ({detail}); "
        "the digest identifies the original text",
    )


def _markers(text: str) -> tuple[tuple[Withheld, ...], int]:
    """Return the redaction markers in *text* and its readable character count."""
    found: list[Withheld] = []
    marked_chars = 0
    for match in _REDACTION_MARKER.finditer(text):
        found.append(Withheld(int(match.group("bytes")), match.group("digest")))
        marked_chars += len(match.group(0))
    return tuple(found), len(text) - marked_chars


def content_label(text: Any, *, what: str) -> Labeled:
    """Three-state availability of a piece of evidence text.

    ``observed`` when it is readable, ``withheld`` when promotion removed it
    (byte count and digest preserved), ``unavailable`` when it is genuinely
    absent or empty. Rendering the first and second states the same way is the
    defect this function exists to prevent.
    """
    if text is None:
        return unavailable(f"{what} is absent from the evidence")
    if isinstance(text, list):  # ATIF content parts (atif.py:279)
        text = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in text
        )
    if not isinstance(text, str):
        return unavailable(f"{what} is not text ({type(text).__name__})")
    found, readable = _markers(text)
    if found:
        return withheld(found, readable_chars=readable, what=what)
    if not text:
        return unavailable(f"{what} is recorded but empty")
    return Labeled({"readable_chars": len(text)}, "observed", f"{what} is readable in full")


def _file_content_label(path: Path, *, what: str) -> Labeled:
    """Availability of a cited file: readable, partly/fully withheld, or absent."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        return unavailable(f"{what} could not be examined ({exc.__class__.__name__})")
    if size > _MARKER_SCAN_LIMIT_BYTES:
        return derived(
            {"size_bytes": size},
            f"{what} is too large to scan for redaction markers "
            f"({size} bytes > {_MARKER_SCAN_LIMIT_BYTES})",
        )
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return unavailable(f"{what} could not be read ({exc.__class__.__name__})")
    found, readable = _markers(text)
    if found:
        return withheld(found, readable_chars=readable, what=what)
    if not size:
        return unavailable(f"{what} exists but is empty")
    return Labeled({"size_bytes": size}, "observed", f"{what} is readable in full")


def content_summary(labeled: Labeled) -> str:
    """One line naming a content state, shared by every surface that renders it.

    The wording lives here, not in the page, because ``dashboard/`` runs under a
    Streamlit that is deliberately not a project dependency and so cannot be
    imported by the test suite. Keeping the sentence here is what makes
    "a withheld step never reads like a verbatim one" a tested guarantee rather
    than an unverifiable claim about a page.
    """
    value = labeled.value or {}
    if labeled.provenance == "withheld":
        digests = ", ".join(f"{m['digest'][:19]}…" for m in value.get("markers", ()))
        readable = int(value.get("readable_chars") or 0)
        head = f"withheld {value.get('withheld_bytes', 0)} bytes ({digests})"
        return f"{head} · {readable} chars readable" if readable else head
    if labeled.provenance == "observed":
        if "readable_chars" in value:
            return f"readable · {value['readable_chars']} chars"
        if "size_bytes" in value:
            return f"readable · {value['size_bytes']} bytes"
        return "readable"
    return f"{labeled.provenance}: {labeled.reason or 'no reason recorded'}"


def redact_text(text: str) -> str:
    """Redact common bearer/key-shaped values before a UI can render them."""
    redacted = text
    for pattern in _SECRET_TEXT_PATTERNS:
        redacted = pattern.sub("[redacted]", redacted)
    return redacted


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    return value


def redact_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    """Redact credential-shaped keys and nested string values recursively."""
    clean: dict[str, Any] = {}
    for key, value in mapping.items():
        if any(marker in str(key).upper() for marker in _SECRET_MARKERS):
            clean[key] = "[redacted]"
        else:
            clean[key] = _redact_value(value)
    return clean


def jail(root: Path, candidate: str) -> Path | None:
    """Resolve *candidate* strictly inside *root*; None on any escape."""
    if candidate.startswith(("/", "~")):
        return None
    resolved = (root / candidate).resolve()
    root_resolved = root.resolve()
    if resolved == root_resolved or root_resolved in resolved.parents:
        # hidden verifier inputs are never exposed even inside the jail
        relative = resolved.relative_to(root_resolved)
        if any(part in _HIDDEN_TASK_DIRS for part in relative.parts[:-1]) or (
            relative.parts and relative.parts[0] in _HIDDEN_TASK_DIRS
        ):
            return None
        return resolved
    return None


def _load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        return None, f"missing: {path.name}"
    except (OSError, ValueError) as exc:
        return None, f"malformed {path.name}: {exc.__class__.__name__}"
    if not isinstance(payload, dict):
        return None, f"malformed {path.name}: not an object"
    return payload, None


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCallRow:
    step_id: int
    tool_call_id: str
    function: str
    exit_code: int | None  # observed from linked observation when present
    observation: Labeled  # three-state content of the linked observation


@dataclass(frozen=True)
class StepRow:
    """One trajectory step, with the availability of its message text.

    ``message`` is the whole point: a promoted ``system``/``user`` step keeps
    its envelope (``step_id``, ``source``, counts) but its text was replaced by
    a digest marker. Without this label the envelope of a withheld step is
    indistinguishable from the envelope of a verbatim one.
    """

    step_id: int | None
    source: str | None
    n_tool_calls: int
    n_observations: int
    message: Labeled


@dataclass(frozen=True)
class TrajectoryView:
    step_count: Labeled
    steps: tuple[StepRow, ...]
    tool_calls: tuple[ToolCallRow, ...]
    repeated_signatures: Labeled  # derived: [(function, count)] repeats > 1
    verify_before_done: Labeled  # derived tri-state True/False
    redaction: Labeled  # withheld(total) when any step text was removed


@dataclass(frozen=True)
class ArtifactLink:
    name: str
    relative_path: str  # jailed, trial-relative; safe to open read-only
    size_bytes: int
    content: Labeled  # what promotion did to this file, per PROMOTION.json


@dataclass(frozen=True)
class CitationResolution:
    citation_path: str
    step_id: int | None
    tool_call_id: str | None
    supports: str
    resolution: Labeled  # derived "resolved" | unavailable(reason)
    content: Labeled  # what the citation actually points at: observed /
    #                   withheld (bytes + digest) / unavailable


@dataclass(frozen=True)
class AnalysisView:
    analysis_id: str
    trial_key: str | None
    link: Labeled  # observed(trial_key), or unavailable with the exact reason
    #                the source trial could not be found — never a bare
    #                "unlinked" with no explanation (F-04)
    status: Labeled  # observed validation_status
    validity: Labeled  # draft — model output until reviewed
    category: Labeled  # draft
    summary: Labeled  # draft
    confidence: Labeled  # draft
    citations: tuple[CitationResolution, ...]
    alternatives: Labeled  # draft
    provenance: Labeled  # observed analysis_provenance (agent/model/digests)


@dataclass(frozen=True)
class StoredAnalysisView:
    """One durable analyst conclusion plus its optional process artifact."""

    analysis_id: str
    trial_id: str | None
    trial_key: str | None
    link: Labeled
    category: Labeled
    summary: Labeled
    confidence: Labeled
    provenance: Labeled
    citations: tuple[CitationResolution, ...]
    transcript: Labeled  # artifact path/step count, never synthesized reasoning


@dataclass(frozen=True)
class TrialView:
    trial_key: str  # job_name/trial_name
    job_name: str
    trial_name: str
    trial_dir: str
    jobs_root: str
    status_root: str
    task_name: Labeled
    agent: Labeled
    model: Labeled
    reward: Labeled
    outcome_class: Labeled  # derived: pass|reward-failure|infra-exception|no-verdict
    exception: Labeled  # observed; infra — NEVER merged with reward
    timing: Labeled
    cost: Labeled
    config: Labeled  # observed, redacted
    trajectory: TrajectoryView | Labeled
    artifacts: tuple[ArtifactLink, ...]
    omitted_files: Labeled  # withheld: files promotion removed from the bundle
    trajectory_outline: TrajectoryOutline | None = None
    trajectory_fallback: Labeled | None = None
    verifier_output: Labeled | None = None
    reward_dimensions: Labeled | None = None
    exit_code: Labeled | None = None


@dataclass(frozen=True)
class JobView:
    job_name: str
    job_dir: str
    jobs_root: str  # which configured root this job came from: live runs or
    #                 promoted evidence are not the same kind of claim
    task_names: Labeled
    trial_keys: tuple[str, ...]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskView:
    task_name: str
    registration: Labeled  # derived from library/registry presence when given
    control_state: Labeled  # derived: which control agents have evidence here
    trial_keys: tuple[str, ...]


@dataclass(frozen=True)
class NextAction:
    label: str
    command: str  # copyable; NEVER executed by the explorer


@dataclass(frozen=True)
class ExplorerIndex:
    tasks: tuple[TaskView, ...]
    jobs: tuple[JobView, ...]
    trials: dict[str, TrialView]
    analyses: tuple[AnalysisView, ...]
    notes: tuple[str, ...]  # degradation reasons; cold start stays navigable
    review_queue: tuple[ReviewQueueItem, ...] = ()
    analyst_analyses: tuple[StoredAnalysisView, ...] = ()


# ---------------------------------------------------------------------------
# Trajectory assembly
# ---------------------------------------------------------------------------


def _observation_results(step: dict[str, Any]) -> list[dict[str, Any]]:
    """Observation results of a step, in the shape ATIF actually validates.

    ``atif.py:299`` requires ``steps[].observation.results``; the nine promoted
    Codex trajectories all use it. A bare ``observations`` list is the older
    shape still present in ``tests/fixtures/explorer``, so both are read here
    rather than leaving 58 real observation results invisible to every surface.
    """
    observation = step.get("observation")
    raw = observation.get("results") if isinstance(observation, dict) else step.get("observations")
    return [item for item in (raw or []) if isinstance(item, dict)]


def _trajectory_view(trial_dir: Path) -> TrajectoryView | Labeled:
    path = trial_dir / "agent" / "trajectory.json"
    payload, error = _load_json(path)
    if payload is None:
        return unavailable(error or "trajectory missing")
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list):
        return unavailable("trajectory has no steps array")

    steps: list[StepRow] = []
    calls: list[ToolCallRow] = []
    signature_counts: dict[tuple[str, str], int] = {}
    exit_by_call: dict[str, int] = {}
    content_by_call: dict[str, Labeled] = {}
    for step in raw_steps:
        if not isinstance(step, dict):
            continue
        for obs in _observation_results(step):
            call_ref = obs.get("source_call_id")
            if not isinstance(call_ref, str):
                continue
            exit_code = obs.get("command_exit_code")
            if isinstance(exit_code, int):
                exit_by_call[call_ref] = exit_code
            if "content" in obs:
                content_by_call[call_ref] = content_label(
                    obs.get("content"), what=f"observation of {call_ref}"
                )
    for step in raw_steps:
        if not isinstance(step, dict):
            continue
        step_id = step.get("step_id")
        step_calls = [c for c in (step.get("tool_calls") or []) if isinstance(c, dict)]
        results = _observation_results(step)
        steps.append(
            StepRow(
                step_id=step_id if isinstance(step_id, int) else None,
                source=step.get("source"),
                n_tool_calls=len(step_calls),
                n_observations=len(results),
                message=content_label(
                    step.get("message") if "message" in step else None,
                    what=f"step {step_id} message",
                ),
            )
        )
        for call in step_calls:
            function = str(
                (call.get("function") or {}).get("name")
                if isinstance(call.get("function"), dict)
                else call.get("function_name") or call.get("name") or "?"
            )
            arguments = json.dumps(
                (call.get("function") or {}).get("arguments")
                if isinstance(call.get("function"), dict)
                else call.get("arguments"),
                sort_keys=True,
                default=str,
            )
            signature_counts[(function, arguments)] = (
                signature_counts.get((function, arguments), 0) + 1
            )
            call_id = str(call.get("tool_call_id") or "?")
            calls.append(
                ToolCallRow(
                    step_id=int(step_id) if isinstance(step_id, int) else -1,
                    tool_call_id=call_id,
                    function=function,
                    exit_code=exit_by_call.get(call_id),
                    observation=content_by_call.get(
                        call_id,
                        unavailable(f"no observation recorded for {call_id}"),
                    ),
                )
            )

    repeats = sorted(
        ((fn, count) for (fn, _args), count in signature_counts.items() if count > 1),
        key=lambda item: -item[1],
    )
    tail_functions = [c.function.lower() for c in calls[-5:]]
    verify = any(any(h in fn for h in _VERIFY_HINTS) for fn in tail_functions)
    return TrajectoryView(
        step_count=observed(len(steps)),
        steps=tuple(steps),
        tool_calls=tuple(calls),
        repeated_signatures=derived(tuple(repeats), "identical (function, arguments)"),
        verify_before_done=derived(
            verify if calls else None,
            "any verification-shaped tool call within the final five calls",
        ),
        redaction=_redaction_summary(steps, _as_mapping(payload.get("evallab_redaction"))),
    )


def _redaction_summary(steps: Sequence[StepRow], in_band: dict[str, Any]) -> Labeled:
    """One headline so a trial page states up front how much text was withheld.

    ``in_band`` is the ``evallab_redaction`` block promotion writes into the
    document; it is reported beside the count measured from the markers so the
    two can be compared rather than trusted.
    """
    hidden = [s for s in steps if s.message.provenance == "withheld"]
    if not hidden:
        return derived(
            {"steps_withheld": 0, "withheld_bytes": 0},
            "no evallab-redacted marker in any step message",
        )
    total = sum(int(s.message.value["withheld_bytes"]) for s in hidden)
    markers = tuple(
        {"step_id": s.step_id, "source": s.source, **dict(marker)}
        for s in hidden
        for marker in s.message.value["markers"]
    )
    return Labeled(
        {
            "steps_withheld": len(hidden),
            "steps_total": len(steps),
            "withheld_bytes": total,
            "sources": tuple(sorted({str(s.source) for s in hidden})),
            "markers": markers,
            "declared": in_band or None,
        },
        "withheld",
        f"{len(hidden)} of {len(steps)} step messages were removed before "
        f"promotion ({total} bytes); each marker keeps a sha256 of the original",
    )


# ---------------------------------------------------------------------------
# Trial / job assembly
# ---------------------------------------------------------------------------


def _promotion_manifest(trial_dir: Path) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Read the job's promotion manifest: what each file kept, lost, or lost entirely.

    ``scripts/promote_codex_bundle.py`` writes ``PROMOTION.json`` beside the
    trials with one entry per source file — ``verbatim``, ``redacted`` (rule R1
    or R3), or ``omitted`` (rule R2, removed from the bundle altogether) — with
    the original byte count and sha256. That is exact and free, so the artifact
    list reports from it rather than guessing from file names or sizes.
    """
    payload, _ = _load_json(trial_dir.parent / "PROMOTION.json")
    if payload is None:
        return {}, []
    prefix = f"{trial_dir.name}/"
    kept: dict[str, dict[str, Any]] = {}
    omitted: list[dict[str, Any]] = []
    for entry in payload.get("files") or []:
        if not isinstance(entry, dict):
            continue
        promoted, source = entry.get("promoted_path"), entry.get("source_path") or ""
        if entry.get("action") == "omitted":
            if source.startswith(prefix):
                omitted.append(entry)
        elif isinstance(promoted, str) and promoted.startswith(prefix):
            kept[promoted[len(prefix) :]] = entry
    return kept, omitted


def _artifact_content(entry: dict[str, Any] | None, relative: str) -> Labeled:
    if entry is None:
        return derived(None, f"{relative} has no promotion record; nothing here was redacted")
    if entry.get("action") != "redacted":
        return Labeled(
            {"size_bytes": entry.get("promoted_bytes")},
            "observed",
            f"{relative} was promoted verbatim",
        )
    original, digest = entry.get("source_bytes"), entry.get("source_sha256")
    kept_bytes = int(entry.get("promoted_bytes") or 0)
    return Labeled(
        {
            "readable_chars": kept_bytes,
            "withheld_bytes": max(int(original or 0) - kept_bytes, 0),
            "markers": ({"bytes": original, "digest": digest},),
            "rule": entry.get("rule"),
        },
        "withheld",
        f"{relative} was redacted by rule {entry.get('rule')}: "
        f"{kept_bytes} of {original} original bytes remain; {digest} is the digest "
        "of the unredacted parent",
    )


def _artifact_links(trial_dir: Path) -> tuple[ArtifactLink, ...]:
    manifest, _ = _promotion_manifest(trial_dir)
    links: list[ArtifactLink] = []
    for sub in ("artifacts", "verifier", "agent"):
        base = trial_dir / sub
        if not base.is_dir():
            continue
        for item in sorted(base.rglob("*")):
            if not item.is_file():
                continue
            relative = item.relative_to(trial_dir).as_posix()
            if jail(trial_dir, relative) is None:
                continue  # path jail: hidden or escaping entries never linked
            links.append(
                ArtifactLink(
                    name=item.name,
                    relative_path=relative,
                    size_bytes=item.stat().st_size,
                    content=_artifact_content(manifest.get(relative), relative),
                )
            )
    return tuple(links)


def _omitted_files(trial_dir: Path) -> Labeled:
    """Files promotion removed entirely, which no artifact list can show."""
    _, omitted = _promotion_manifest(trial_dir)
    if not omitted:
        return derived((), "no file was omitted from this trial by promotion")
    prefix = f"{trial_dir.name}/"
    total = sum(int(entry.get("source_bytes") or 0) for entry in omitted)
    return Labeled(
        {
            "withheld_bytes": total,
            "markers": tuple(
                {
                    "path": str(entry.get("source_path"))[len(prefix) :],
                    "bytes": entry.get("source_bytes"),
                    "digest": entry.get("source_sha256"),
                    "rule": entry.get("rule"),
                }
                for entry in omitted
            ),
        },
        "withheld",
        f"{len(omitted)} file(s) totalling {total} bytes were removed from this "
        "bundle entirely, so they appear in no artifact list; each digest "
        "identifies the original",
    )


def _status_root_for_jobs_root(jobs_root: Path) -> Path:
    """Return the root accepted by ``evallab status --from`` for a jobs root."""
    resolved = jobs_root.resolve()
    if (
        resolved.name == "runs"
        and resolved.parent.name == "evidence"
        and resolved.parent.parent.name == "research"
    ):
        return resolved.parents[2]
    return resolved.parent


def _as_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _outline_for_trial(trial_dir: Path, jobs_root: Path) -> TrajectoryOutline | None:
    """Use the M030 typed outline while keeping index discovery non-throwing."""
    try:
        resolved_jobs_root = jobs_root.resolve()
        return outline_trajectory(
            trial_dir.resolve(),
            repo_root=resolved_jobs_root.parent,
            explicit_runs_root=resolved_jobs_root,
        )
    except Exception:
        return None


def _trajectory_fallback(outline: TrajectoryOutline | None, agent_name: Any) -> Labeled | None:
    """Make AGY's print-mode limitation explicit instead of implying a trace."""
    if outline is None or outline.status == "featured":
        return None
    reason = outline.unavailable_reason or "trajectory unavailable"
    if str(agent_name).lower() == "antigravity-cli":
        return unavailable(
            f"{reason}; AGY fallback: final response only; process stream was not captured"
        )
    return unavailable(reason)


def _first_exit_code(*payloads: dict[str, Any]) -> int | str | None:
    for payload in payloads:
        for key in ("exit_code", "exit_status", "return_code", "returncode"):
            value = payload.get(key)
            if isinstance(value, int | str):
                return value
    return None


def _stored_analysis_views(
    analysis_dir: Path, trials: dict[str, TrialView]
) -> tuple[tuple[StoredAnalysisView, ...], tuple[str, ...]]:
    """Load ``research/analysis`` conclusions and transcript artifacts read-only."""
    views: list[StoredAnalysisView] = []
    notes: list[str] = []
    if not analysis_dir.is_dir():
        return (), (f"analyst storage unavailable: {analysis_dir}",)

    by_trial_id: dict[str, TrialView] = {}
    duplicate_ids: set[str] = set()
    for trial in trials.values():
        payload, _ = _load_json(Path(trial.trial_dir) / "result.json")
        trial_id = payload.get("id") if payload else None
        if not trial_id:
            continue
        key = str(trial_id)
        if key in by_trial_id:
            duplicate_ids.add(key)
        else:
            by_trial_id[key] = trial

    for path in sorted(analysis_dir.glob("*.json")):
        if path.name.endswith(".trajectory.json") or path.name.endswith(".provenance.json"):
            continue
        payload, error = _load_json(path)
        if payload is None or "analysis_id" not in payload:
            if error:
                notes.append(f"analyst {path.name}: {error}")
            continue
        analysis_id = str(payload["analysis_id"])
        source_trial_id = str(payload.get("trial_id") or "")
        trial = None if source_trial_id in duplicate_ids else by_trial_id.get(source_trial_id)
        if source_trial_id in duplicate_ids:
            link = unavailable(f"source trial id {source_trial_id} is duplicated in the index")
        elif trial is None:
            link = unavailable(
                f"source trial {source_trial_id or 'unknown'} was not found among indexed trials"
            )
        else:
            link = observed(trial.trial_key)

        evidence_rows = payload.get("evidence")
        citations: list[CitationResolution] = []
        if isinstance(evidence_rows, list):
            for raw in evidence_rows:
                if not isinstance(raw, dict):
                    continue
                citations.append(
                    _resolve_citation(
                        {
                            "path": raw.get("path"),
                            "step_id": raw.get("step", raw.get("step_id")),
                            "supports": raw.get("supports") or "stored analyst evidence",
                        },
                        trial,
                    )
                )

        transcript_path = analysis_dir / f"{analysis_id}.trajectory.json"
        transcript_payload, transcript_error = _load_json(transcript_path)
        if transcript_payload is None:
            transcript = unavailable(
                "analyst transcript artifact unavailable; only the stored conclusion/final "
                f"response is recorded ({transcript_error or 'file missing'})"
            )
        else:
            steps = transcript_payload.get("steps")
            count = len(steps) if isinstance(steps, list) else 0
            transcript = observed({"path": transcript_path.name, "steps": count})

        provenance = {
            key: payload[key]
            for key in ("model", "rubric_digest", "created_at", "inputs")
            if key in payload
        }
        views.append(
            StoredAnalysisView(
                analysis_id=analysis_id,
                trial_id=source_trial_id or None,
                trial_key=trial.trial_key if trial else None,
                link=link,
                category=Labeled(redact_text(str(payload.get("category") or "")), "draft"),
                summary=Labeled(
                    redact_text(str(payload.get("summary") or "")),
                    "draft",
                    "stored analyst conclusion; not ground truth",
                ),
                confidence=Labeled(
                    redact_mapping(_as_mapping(payload.get("confidence"))),
                    "draft",
                ),
                provenance=observed(redact_mapping(provenance)),
                citations=tuple(citations),
                transcript=transcript,
            )
        )
    return tuple(views), tuple(notes)


def _trial_view(job_name: str, trial_dir: Path, jobs_root: Path) -> TrialView:
    result, result_error = _load_json(trial_dir / "result.json")
    result = result or {}
    trial_name = trial_dir.name
    trial_key = f"{job_name}/{trial_name}"

    agent_info = _as_mapping(result.get("agent_info"))
    exception = result.get("exception_info")
    verifier_result = _as_mapping(result.get("verifier_result"))
    rewards = _as_mapping(verifier_result.get("rewards"))
    reward_value = rewards.get("reward")

    if result_error:
        outcome = unavailable(result_error)
        reward = unavailable(result_error)
    elif exception:
        outcome = derived("infra-exception", "exception_info present; not a score")
        reward = (
            observed(reward_value)
            if reward_value is not None
            else unavailable("no reward recorded (exception before verdict)")
        )
    elif reward_value is None:
        outcome = derived("no-verdict", "no exception and no reward recorded")
        reward = unavailable("no reward recorded")
    else:
        try:
            numeric_reward = float(reward_value)
        except (TypeError, ValueError):
            outcome = unavailable("reward is not numeric")
            reward = unavailable("reward is not numeric")
        else:
            outcome = derived("pass" if numeric_reward >= 1.0 else "reward-failure")
            reward = observed(reward_value)

    started, finished = result.get("started_at"), result.get("finished_at")
    timing = (
        observed({"started_at": started, "finished_at": finished})
        if started or finished
        else unavailable("no timestamps in result.json")
    )
    agent_result = _as_mapping(result.get("agent_result"))
    cost = (
        observed(agent_result.get("cost_usd"))
        if agent_result.get("cost_usd") is not None
        else unavailable("no cost recorded (controls and subscription runs bill nothing)")
    )
    config, config_error = _load_json(trial_dir / "config.json")
    outline = _outline_for_trial(trial_dir, jobs_root)
    verifier_output = (
        observed(redact_mapping(verifier_result))
        if verifier_result
        else unavailable(result_error or "verifier_result missing")
    )
    reward_dimensions = (
        observed(redact_mapping(rewards))
        if rewards
        else unavailable(result_error or "verifier rewards missing")
    )
    recorded_exit = _first_exit_code(result, agent_result, verifier_result)
    exit_code = (
        observed(recorded_exit)
        if recorded_exit is not None
        else unavailable("no process exit code recorded")
    )
    return TrialView(
        trial_key=trial_key,
        job_name=job_name,
        trial_name=trial_name,
        trial_dir=str(trial_dir),
        jobs_root=str(jobs_root.resolve()),
        status_root=str(_status_root_for_jobs_root(jobs_root)),
        task_name=(
            observed(result.get("task_name"))
            if result.get("task_name")
            else unavailable(result_error or "task name absent")
        ),
        agent=(
            observed(agent_info.get("name"))
            if agent_info.get("name")
            else unavailable("agent name absent")
        ),
        model=(
            observed(_as_mapping(agent_info.get("model_info")).get("name"))
            if _as_mapping(agent_info.get("model_info")).get("name")
            else unavailable("no model recorded (controls run without one)")
        ),
        reward=reward,
        outcome_class=outcome,
        exception=(
            observed(redact_mapping(exception))
            if isinstance(exception, dict)
            else observed(redact_text(str(exception)))
            if exception
            else derived(None, "no exception recorded")
        ),
        timing=timing,
        cost=cost,
        config=(
            observed(redact_mapping(config))
            if config is not None
            else unavailable(config_error or "config missing")
        ),
        trajectory=_trajectory_view(trial_dir),
        artifacts=_artifact_links(trial_dir),
        omitted_files=_omitted_files(trial_dir),
        trajectory_outline=outline,
        trajectory_fallback=_trajectory_fallback(outline, agent_info.get("name")),
        verifier_output=verifier_output,
        reward_dimensions=reward_dimensions,
        exit_code=exit_code,
    )


def _is_job_result(payload: dict[str, Any] | None) -> bool:
    """A job-level ``result.json`` is Harbor's roll-up, not a trial verdict.

    Same positive test ``evallab.results.discover_job_dirs`` applies: the
    roll-up carries ``n_total_trials`` and ``stats``. Without this a nested
    layout mistakes the job directory for a trial of its parent (F-04).
    """
    return bool(payload) and "n_total_trials" in payload and "stats" in payload


def _is_trial_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    result = path / "result.json"
    if not result.is_file():
        return False
    return not _is_job_result(_load_json(result)[0])


def _is_job_dir(path: Path) -> bool:
    """A job directory is Harbor output; a dot-prefixed one is bookkeeping.

    The executor keeps its own state under ``<jobs_root>/.executor``, and the
    queue keeps lock files alongside it. Rendering those as evaluation output
    invited an operator to open a job that never ran (M009 F-08).
    """
    return path.is_dir() and not path.name.startswith(".")


def _subdirs(path: Path) -> list[Path]:
    try:
        return sorted(p for p in path.iterdir() if _is_job_dir(p))
    except OSError:
        return []


def _nested_runs(start: Path, depth: int = _NESTED_PROBE_DEPTH) -> list[tuple[Path, int]]:
    """Diagnostic probe: job-shaped directories *below* the addressed level.

    This never feeds the index. Its only job is to let a note say exactly which
    run a free-form ``jobs_dir`` put out of reach, instead of leaving the reader
    to guess why a directory rendered empty.
    """
    found: list[tuple[Path, int]] = []
    frontier = [(child, 1) for child in _subdirs(start)]
    while frontier:
        candidate, level = frontier.pop(0)
        trials = [c for c in _subdirs(candidate) if _is_trial_dir(c)]
        if trials:
            found.append((candidate, len(trials)))
            continue  # a job's trials are never probed further
        if level < depth:
            frontier.extend((child, level + 1) for child in _subdirs(candidate))
    return found


def _discover_jobs(root: Path) -> tuple[list[Path], list[str]]:
    """Job directories under *root*, plus a loud note for every directory that
    is not one.

    Job addressing stays ``<jobs_root>/<job_name>/<trial>`` — the shape the
    executor writes (``runner.py:601``) and the only shape Harbor's own viewer
    scans (``harbor/viewer/scanner.py:50,86``). This explorer deliberately does
    **not** search deeper: a private, deeper convention here would disagree with
    every other reader of the same directories.

    ``ExperimentSpec.jobs_dir`` is free-form, though (``schemas.py:27``), so a
    run can be written where none of those readers look. That used to render the
    intermediate directory as a job with no trials while the real run vanished
    and its analysis showed as ``unlinked`` with unresolvable citations (F-04).
    Now the directory is named, together with the nested run it hides and what
    to do about it, and no phantom job is rendered.
    """
    jobs: list[Path] = []
    notes: list[str] = []
    for entry in sorted(p for p in root.iterdir() if _is_job_dir(p)):
        if any(_is_trial_dir(child) for child in _subdirs(entry)):
            jobs.append(entry)
            continue
        hidden = _nested_runs(entry)
        if hidden:
            located = "; ".join(
                f"{job.relative_to(root).as_posix()} ({count} "
                f"{'trial' if count == 1 else 'trials'})"
                for job, count in hidden
            )
            reachable = sorted({job.parent.relative_to(root).as_posix() for job, _ in hidden})
            notes.append(
                f"jobs root {root}: {entry.name}/ is not a job directory, but a run "
                f"exists below it — {located}. Nothing under it is rendered here, "
                "because jobs are addressed as <jobs-root>/<job>/<trial>, the shape "
                "the executor writes and Harbor's viewer scans. To see it, add a "
                f"jobs root at {', '.join(reachable)} (relative to this root), or "
                "record the run one level under a jobs root."
            )
        else:
            notes.append(
                f"jobs root {root}: {entry.name}/ holds no trial result; it is "
                "reported here rather than rendered as a job with no trials"
            )
    return jobs, notes


# ---------------------------------------------------------------------------
# Analyses
# ---------------------------------------------------------------------------


def _cited_content(trial: TrialView, jailed: Path | None, step_id: Any, call_id: Any) -> Labeled:
    """What a *resolved* citation actually lets a reader see.

    Resolution answers "does this exist"; this answers "is it readable". A
    citation into a promoted ``system`` step resolves exactly like a citation
    into an agent message, so without this the two are indistinguishable.
    """
    trajectory = trial.trajectory
    if step_id is not None and isinstance(trajectory, TrajectoryView):
        step = next((s for s in trajectory.steps if s.step_id == step_id), None)
        if step is None:  # pragma: no cover - resolution already refused it
            return unavailable(f"cited step {step_id} is not in the trajectory")
        if call_id is not None:
            call = next(
                (
                    c
                    for c in trajectory.tool_calls
                    if c.step_id == step_id and c.tool_call_id == call_id
                ),
                None,
            )
            if call is not None:
                # A tool call is kept verbatim by promotion; its observation
                # carries its own state, so report the weaker of the two.
                if call.observation.provenance == "withheld":
                    return call.observation
                return Labeled(
                    {"function": call.function, "observation": call.observation.provenance},
                    "observed",
                    f"tool call {call_id} is recorded verbatim; its observation is "
                    f"{call.observation.provenance}",
                )
        return step.message
    if jailed is not None:
        return _file_content_label(jailed, what=f"cited file {jailed.name}")
    return unavailable("citation names neither a readable file nor a trajectory step")


def _resolve_citation(citation: dict[str, Any], trial: TrialView | None) -> CitationResolution:
    path = str(citation.get("path") or "")
    step_id = citation.get("step_id")
    call_id = citation.get("tool_call_id")
    supports = str(citation.get("supports") or "")

    jailed: Path | None = None
    if trial is None:
        resolution = unavailable("cited trial not found in this index")
    else:
        trial_dir = Path(trial.trial_dir)
        jailed = jail(trial_dir, path) if path else None
        if path and jailed is None:
            resolution = unavailable(f"citation path refused (escape or hidden): {path!r}")
        elif path and jailed is not None and not jailed.is_file():
            resolution = unavailable(f"cited file does not exist: {path!r}")
        else:
            trajectory = trial.trajectory
            if step_id is not None and isinstance(trajectory, TrajectoryView):
                known_steps = {s.step_id for s in trajectory.steps}
                if step_id not in known_steps:
                    resolution = unavailable(f"cited step {step_id} not in trajectory")
                elif call_id is not None and not any(
                    c.step_id == step_id and c.tool_call_id == call_id
                    for c in trajectory.tool_calls
                ):
                    resolution = unavailable(
                        f"cited tool call {call_id!r} not found in step {step_id}"
                    )
                else:
                    resolution = derived("resolved", "file, step, and call verified")
            elif step_id is not None:
                resolution = unavailable("cited a step but the trajectory is unavailable")
            else:
                resolution = derived("resolved", "file verified")
    if trial is None or resolution.provenance == "unavailable":
        content = unavailable(
            f"nothing to read: the citation does not resolve ({resolution.reason})"
        )
    else:
        content = _cited_content(trial, jailed, step_id, call_id)
    return CitationResolution(
        citation_path=path,
        step_id=step_id,
        tool_call_id=call_id,
        supports=supports,
        resolution=resolution,
        content=content,
    )


CitationState = Literal["unresolved", "withheld", "absent", "readable"]


def citation_state(citation: CitationResolution) -> CitationState:
    """The four states a rendered citation can be in, in precedence order.

    ``unresolved`` — the cited file, step, or call is not there at all.
    ``withheld``   — it resolves, but the text was removed before promotion.
    ``absent``     — it resolves, but there is no content to read.
    ``readable``   — it resolves and a human can read it.

    A surface that collapses ``withheld`` into ``readable`` is asserting that a
    redacted prompt is evidence of agent behaviour. That is the defect.
    """
    if citation.resolution.value != "resolved":
        return "unresolved"
    if citation.content.provenance == "withheld":
        return "withheld"
    if citation.content.provenance == "unavailable":
        return "absent"
    return "readable"


def _analyses_relative(path: Path, analyses_dir: Path) -> str:
    """Name a sidecar by its analysis directory; every file is ``analysis.json``."""
    try:
        return path.relative_to(analyses_dir).as_posix()
    except ValueError:
        return path.name


def _analysis_views(
    analyses_dir: Path, trials: dict[str, TrialView]
) -> tuple[tuple[AnalysisView, ...], tuple[str, ...]]:
    views: list[AnalysisView] = []
    notes: list[str] = []
    if not analyses_dir.is_dir():
        return (), (f"analyses: none at {analyses_dir.name}/ (cold start ok)",)
    trials_by_id: dict[str, TrialView] = {}
    duplicate_trial_ids: set[str] = set()
    for trial in trials.values():
        result = _load_json(Path(trial.trial_dir) / "result.json")[0] or {}
        trial_id = result.get("id")
        if not trial_id:
            continue
        key = str(trial_id)
        if key in trials_by_id:
            duplicate_trial_ids.add(key)
        else:
            trials_by_id[key] = trial
    # Positive discovery: a sidecar is the file named ``analysis.json``. Any
    # other JSON under the destination root — reviews written by
    # ``evallab analyze review``, and whatever artifact type comes next — is
    # not a sidecar and must not be parsed as one (M009 F-03).
    for path in sorted(analyses_dir.rglob(ANALYSIS_SIDECAR_FILENAME)):
        try:
            sidecar = TrialAnalysisSidecar.model_validate_json(path.read_text())
        except (OSError, ValidationError) as exc:
            notes.append(
                f"analysis {_analyses_relative(path, analyses_dir)}: "
                f"unreadable ({exc.__class__.__name__})"
            )
            continue
        source_trial_id = str(sidecar.source_trial_id)
        duplicated = source_trial_id in duplicate_trial_ids
        trial = None if duplicated else trials_by_id.get(source_trial_id)
        if duplicated:
            link = unavailable(
                f"source trial id {source_trial_id} is recorded by more than one "
                "indexed trial; linking it would name the wrong run"
            )
            notes.append(
                f"analysis {sidecar.analysis_id}: source trial id "
                f"{source_trial_id} is duplicated; analysis left unlinked"
            )
        elif trial is None:
            # F-04: the catalog joins this analysis to its trial, so a miss here
            # means discovery did not reach the run — say so instead of showing
            # a bare "unlinked" the reader cannot act on.
            link = unavailable(
                f"source trial {source_trial_id} was not found among the "
                f"{len(trials_by_id)} trials discovered under the configured jobs "
                "roots; its citations cannot be verified from this index"
            )
            notes.append(
                f"analysis {sidecar.analysis_id}: source trial {source_trial_id} "
                f"is not under any configured jobs root ({len(trials_by_id)} trials "
                "indexed); the analysis is shown unlinked, not dropped"
            )
        else:
            link = observed(trial.trial_key)
        citations = tuple(_resolve_citation(c.model_dump(), trial) for c in sidecar.output.evidence)
        views.append(
            AnalysisView(
                analysis_id=str(sidecar.analysis_id),
                trial_key=trial.trial_key if trial else None,
                link=link,
                status=observed(sidecar.validation_status),
                validity=Labeled(sidecar.output.validity, "draft"),
                category=Labeled(sidecar.output.primary_category, "draft"),
                summary=Labeled(sidecar.output.summary, "draft"),
                confidence=Labeled(sidecar.output.confidence, "draft"),
                citations=citations,
                alternatives=Labeled(tuple(sidecar.output.alternative_explanations), "draft"),
                provenance=observed(
                    redact_mapping(sidecar.analysis_provenance.model_dump(mode="json"))
                ),
            )
        )
    return tuple(views), tuple(notes)


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


def build_index(
    jobs_roots: list[Path],
    analyses_dir: Path | None = None,
    registry_dir: Path | None = None,
    *,
    repo_root: Path | None = None,
    analyst_dir: Path | None = None,
    review_queue_limit: int = 3,
) -> ExplorerIndex:
    """Assemble the full linked index. Read-only; degrades, never raises."""
    notes: list[str] = []
    trials: dict[str, TrialView] = {}
    jobs: list[JobView] = []
    seen_trial_keys: set[str] = set()
    registry = None
    if registry_dir is not None:
        try:
            registry = TaskRegistry.from_dir(registry_dir)
        except ValueError as exc:
            notes.append(f"registry unavailable: {exc}")

    job_dir_by_name: dict[str, Path] = {}
    for root in jobs_roots:
        if not root.is_dir():
            notes.append(f"jobs root unavailable: {root}")
            continue
        job_dirs, discovery_notes = _discover_jobs(root)
        notes.extend(discovery_notes)
        for job_dir in job_dirs:
            job_notes: list[str] = []
            trial_keys: list[str] = []
            task_names: set[str] = set()
            previous = job_dir_by_name.setdefault(job_dir.name, job_dir)
            if previous != job_dir:
                # Two jobs_dir values can end in the same job name. Say which
                # directories collide instead of skipping trials unexplained.
                job_notes.append(
                    f"job name {job_dir.name!r} is also used by {previous}; trial "
                    "keys below that already exist are skipped, not merged"
                )
            for trial_dir in sorted(p for p in job_dir.iterdir() if _is_trial_dir(p)):
                view = _trial_view(job_dir.name, trial_dir, root)
                if view.trial_key in seen_trial_keys:
                    job_notes.append(
                        f"duplicate trial key skipped: {view.trial_key} "
                        f"(second copy at {trial_dir})"
                    )
                    continue
                seen_trial_keys.add(view.trial_key)
                trials[view.trial_key] = view
                trial_keys.append(view.trial_key)
                if view.task_name.provenance == "observed":
                    task_names.add(str(view.task_name.value))
            jobs.append(
                JobView(
                    job_name=job_dir.name,
                    job_dir=str(job_dir),
                    jobs_root=str(root.resolve()),
                    task_names=(
                        observed(sorted(task_names))
                        if task_names
                        else unavailable("no readable trial results")
                    ),
                    trial_keys=tuple(trial_keys),
                    notes=tuple(job_notes),
                )
            )

    by_task: dict[str, list[str]] = {}
    controls_by_task: dict[str, set[str]] = {}
    for key, view in trials.items():
        if view.task_name.provenance != "observed":
            continue
        task = str(view.task_name.value)
        by_task.setdefault(task, []).append(key)
        if view.agent.provenance == "observed" and view.agent.value in CONTROL_AGENTS:
            controls_by_task.setdefault(task, set()).add(str(view.agent.value))
    tasks = tuple(
        TaskView(
            task_name=task,
            registration=(
                observed(registry.records[task].state)
                if registry is not None and task in registry.records
                else observed("not registered")
                if registry is not None
                else unavailable("registry not configured for this explorer root")
            ),
            control_state=derived(sorted(controls_by_task.get(task, set()))),
            trial_keys=tuple(sorted(keys)),
        )
        for task, keys in sorted(by_task.items())
    )

    analyses, analysis_notes = _analysis_views(analyses_dir, trials) if analyses_dir else ((), ())
    notes.extend(analysis_notes)
    review_queue: tuple[ReviewQueueItem, ...] = ()
    if review_queue_limit > 0:
        try:
            review_queue = tuple(
                select_review_queue(
                    limit=review_queue_limit,
                    runs_roots=jobs_roots,
                    repo_root=(repo_root or Path.cwd()),
                )
            )[:review_queue_limit]
        except Exception as exc:
            notes.append(f"trajectory review queue unavailable: {type(exc).__name__}")
    analyst_analyses: tuple[StoredAnalysisView, ...] = ()
    if analyst_dir is not None:
        analyst_analyses, analyst_notes = _stored_analysis_views(analyst_dir, trials)
        notes.extend(analyst_notes)
    if not trials:
        notes.append("cold start: no readable trials; views render empty, not broken")
    return ExplorerIndex(
        tasks=tasks,
        jobs=tuple(jobs),
        trials=trials,
        analyses=analyses,
        notes=tuple(notes),
        review_queue=review_queue,
        analyst_analyses=analyst_analyses,
    )


# ---------------------------------------------------------------------------
# Next Action: copyable commands only. Nothing here executes anything.
# ---------------------------------------------------------------------------


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if len(slug) < 3:
        slug = f"task-{slug or 'unknown'}"
    return slug[:60].rstrip("-")


def next_actions_for_task(task_name: str, task_path: str | None = None) -> tuple[NextAction, ...]:
    slug = _safe_slug(task_name.rsplit("/", 1)[-1])
    path = task_path or f"path/to/{slug}"
    quoted_path = shlex.quote(path)
    return (
        NextAction(
            "Run the oracle control (free, local)",
            f"uv run evallab run --task {quoted_path} --agent oracle --name {slug}-oracle",
        ),
        NextAction(
            "Run the nop control (free, local)",
            f"uv run evallab run --task {quoted_path} --agent nop --name {slug}-nop",
        ),
    )


def next_actions_for_trial(trial: TrialView) -> tuple[NextAction, ...]:
    actions = [
        NextAction(
            "Open Harbor's viewer for this trial's jobs root",
            f"harbor view {shlex.quote(trial.jobs_root)} --jobs",
        ),
        NextAction(
            "Show the no-call stage-5 analysis plan",
            f"uv run evallab analyze plan {shlex.quote(trial.trial_dir)}",
        ),
    ]
    if trial.outcome_class.value == "infra-exception":
        actions.append(
            NextAction(
                "Re-run this job's controls before drawing any conclusion",
                f"uv run evallab status --from {shlex.quote(trial.status_root)}",
            )
        )
    return tuple(actions)


def next_actions_for_queue() -> tuple[NextAction, ...]:
    return (
        NextAction(
            "Submit an experiment spec (policy-gated)", "uv run evallab submit path/to/spec.json"
        ),
        NextAction(
            "Approve one waiting experiment (Peter's ceilings still apply)",
            "uv run evallab approve SPEC_ID --actor peter",
        ),
    )

# ---------------------------------------------------------------------------
# Native experiment evidence view (Standing Harbor Integration consumer).
# ---------------------------------------------------------------------------
# Read-only: queue specs (DirectoryQueue with create=False), lightweight
# lab-metadata.json / result.json headers for filtering, results.load_job plus
# evidence.facts.extract_trial_fact for explicitly selected jobs only, and one
# optional supplied DataEngineer coverage report parsed as plain JSON.
# Nothing here writes, projects, recomputes coverage, executes commands, or
# touches the network. Metadata without a spec_id is a native Harbor job that
# is simply unbound (``harbor_unbound``) — never an external Docker audit;
# external comparison context is the lead's separate layer.

_EXPERIMENT_VIEW_KIND = "experiment_evidence_view"
_HARBOR_UNBOUND = "harbor_unbound"
_COVERAGE_SECTIONS = (
    "native_jobs_present",
    "catalogued",
    "projected",
    "excepted",
    "failed",
)
_COVERAGE_JOB_NAME_LIMIT = 25


def _view_json_dict(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Read *path* as a JSON object; (None, reason) on any failure."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"absent: {path}"
    except OSError as exc:
        return None, f"unreadable {path}: {type(exc).__name__}"
    except ValueError as exc:
        return None, f"malformed JSON at {path}: {exc}"
    if not isinstance(payload, dict):
        return None, f"malformed JSON at {path}: top-level value is not an object"
    return payload, None


def _reasons_for(queue: Any, spec_id: str | None) -> list[dict[str, Any]]:
    """Reason receipts matching one spec id: ``reasons/<spec_id>-<ULID>.json``.

    Parsed as text context only. A receipt may name a repair command; it is
    rendered, never executed (nothing in this module executes anything).
    """
    if not spec_id:
        return []
    reasons_dir = queue.reasons_dir
    if not reasons_dir.is_dir():
        return []
    receipts: list[dict[str, Any]] = []
    for path in sorted(reasons_dir.glob(f"{spec_id}-*.json")):
        payload, error = _view_json_dict(path)
        if payload is None:
            receipts.append({"path": str(path), "status": "unreadable", "reason": error})
            continue
        receipts.append(
            {
                "path": str(path),
                "status": "observed",
                "code": payload.get("code"),
                "message": payload.get("message"),
            }
        )
    return receipts


def _queue_rows(queue: Any) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[str]]:
    """Every queued spec across QUEUE_STATES, plus specs keyed by spec_id."""
    from evallab.queue import QUEUE_STATES

    rows: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    issues: list[str] = []
    for state in QUEUE_STATES:
        state_dir = queue.state_dir(state)
        if not state_dir.is_dir():
            continue
        for path in sorted(state_dir.glob("*.json")):
            try:
                spec = queue.load(path)
            except ValueError as exc:
                issues.append(f"queued spec unreadable: {exc}")
                continue
            spec_id = str(spec.spec_id) if spec.spec_id else _HARBOR_UNBOUND
            row = {
                "state": state,
                "spec_id": spec_id,
                "name": spec.name,
                "agent": spec.agent,
                "path": str(path),
                "jobs_dir": spec.jobs_dir,
                "reasons": _reasons_for(queue, spec.spec_id),
            }
            rows.append(row)
            if spec.spec_id:
                by_id.setdefault(str(spec.spec_id), row)
    rows.sort(key=lambda item: (item["state"], item["name"]))
    return rows, by_id, issues


def _jobs_roots(root: Path, specs: list[dict[str, Any]]) -> tuple[list[Path], list[str]]:
    """Filesystem roots worth scanning for Harbor job directories."""
    candidates = [root / "runs", root / "research" / "evidence" / "runs"]
    for spec in specs:
        jobs_dir = spec.get("jobs_dir")
        if not jobs_dir or not isinstance(jobs_dir, str):
            continue
        if jobs_dir.startswith("/") or ".." in jobs_dir.split("/"):
            continue
        candidates.append(root / jobs_dir)
    roots = [candidate for candidate in dict.fromkeys(candidates) if candidate.is_dir()]
    notices: list[str] = []
    if (root / "result.json").is_file() and root not in roots:
        roots.append(root)
    if not roots:
        notices.append(f"no jobs roots present under {root}")
    return roots, notices


def _light_job_row(job_dir: Path) -> dict[str, Any]:
    """Inventory row from headers only — never loads trajectories."""
    metadata, _ = _view_json_dict(job_dir / "lab-metadata.json")
    result, _ = _view_json_dict(job_dir / "result.json")
    experiment = (metadata or {}).get("experiment")
    experiment = experiment if isinstance(experiment, dict) else {}
    spec_id = experiment.get("spec_id")
    bound = spec_id if isinstance(spec_id, str) and spec_id else _HARBOR_UNBOUND
    native = isinstance(result, dict) and "n_total_trials" in result and "stats" in result
    finished = isinstance(result, dict) and bool(result.get("finished_at"))
    expected = result.get("n_total_trials") if isinstance(result, dict) else None
    return {
        "job_id": result.get("id") if isinstance(result, dict) else None,
        "name": job_dir.name,
        "path": str(job_dir),
        "origin": "harbor_native" if native else "external_diagnostics",
        "experiment_id": bound,
        "execution_status": "finished" if finished else "unknown",
        "trial_count": expected if isinstance(expected, int) and not isinstance(expected, bool) else None,
        "trials": [],
    }


def _refine_status_from_metadata(row: dict[str, Any], job_dir: Path) -> None:
    metadata, _ = _view_json_dict(job_dir / "lab-metadata.json")
    exit_code = (metadata or {}).get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        row["execution_status"] = "succeeded" if exit_code == 0 else "failed"


def _trial_row(job: Any, trial: Any) -> dict[str, Any]:
    """One selected trial: fact-producer values plus explicit capture states."""
    from evallab.evidence.facts import extract_trial_fact

    try:
        fact = extract_trial_fact(job, trial)
    except Exception as exc:
        return {
            "trial_id": None,
            "trial_name": trial.path.name,
            "trial_state": "unavailable",
            "reason": f"trial fact extraction failed: {type(exc).__name__}: {exc}",
            "task": None,
            "agent": {"name": None, "version": None, "model": None},
            "reward": {"value": None, "state": "unavailable", "reason": "fact unavailable"},
            "exception": {"class": None, "phase": None},
            "links": {
                "source": str(trial.path),
                "result": str(trial.path / "result.json"),
                "trajectory": str(trial.path / "agent" / "trajectory.json"),
            },
            "trajectory_present": (trial.path / "agent" / "trajectory.json").is_file(),
            "usage": {
                "input_tokens": None,
                "output_tokens": None,
                "cost_usd": None,
                "state": "unavailable",
            },
            "capture": {"state": "unavailable", "reason": "fact unavailable"},
        }
    agent_name = fact.agent_name
    reward_value = fact.primary_reward
    usage_values = (fact.input_tokens, fact.output_tokens, fact.cost_usd)
    trajectory_present = (trial.path / "agent" / "trajectory.json").is_file()
    if agent_name in CONTROL_AGENTS:
        capture = {
            "state": "not_applicable",
            "reason": (
                f"{agent_name} is a free control: absent trajectory/usage is "
                "expected, not a capture failure"
            ),
        }
        usage_state = "not_applicable"
    elif fact.trajectory_count == 0:
        capture = {
            "state": "missing",
            "reason": "model trial recorded no trajectories",
        }
        if all(value is not None for value in usage_values):
            usage_state = "complete"
        elif all(value is None for value in usage_values):
            usage_state = "missing"
        else:
            usage_state = "partial"
    elif fact.invalid_trajectory_count > 0 or any(value is None for value in usage_values):
        if fact.invalid_trajectory_count > 0 and any(value is None for value in usage_values):
            reason = (
                f"{fact.invalid_trajectory_count} invalid trajectories; usage incomplete"
            )
        elif fact.invalid_trajectory_count > 0:
            reason = f"{fact.invalid_trajectory_count} invalid trajectories"
        else:
            reason = "usage incomplete"
        capture = {"state": "partial", "reason": reason}
        usage_state = "partial"
    else:
        capture = {
            "state": "observed",
            "reason": "trajectory and usage observed; presence is not a validity claim",
        }
        usage_state = "complete"
    reward: dict[str, Any] = {
        "value": reward_value,
        "state": "observed" if reward_value is not None else "unavailable",
    }
    if reward_value is None:
        reward["reason"] = "no verifier reward recorded; never defaulted to 0"
    return {
        "trial_id": fact.trial_id,
        "trial_name": fact.trial_name,
        "trial_state": "observed",
        "task": fact.task_name,
        "agent": {"name": fact.agent_name, "version": fact.agent_version, "model": fact.model_name},
        "reward": reward,
        "exception": {"class": fact.exception_class, "phase": fact.exception_phase},
        "links": {
            "source": str(trial.path),
            "result": str(trial.path / "result.json"),
            "trajectory": str(trial.path / "agent" / "trajectory.json"),
        },
        "trajectory_present": trajectory_present,
        "trajectory_count": fact.trajectory_count,
        "invalid_trajectory_count": fact.invalid_trajectory_count,
        "usage": {
            "input_tokens": fact.input_tokens,
            "output_tokens": fact.output_tokens,
            "cost_usd": fact.cost_usd,
            "state": usage_state,
        },
        "capture": capture,
    }


def _selected_job_row(job_dir: Path) -> tuple[dict[str, Any], list[str]]:
    """Full row for an explicitly selected job; stays visible on failure."""
    from evallab.results import load_job

    row = _light_job_row(job_dir)
    issues: list[str] = []
    try:
        job = load_job(job_dir)
    except Exception as exc:
        row["execution_status"] = "unavailable"
        row["load_issue"] = f"selected job payload unavailable ({type(exc).__name__}): {exc}"
        issues.append(f"selected job {job_dir.name} unavailable: {row['load_issue']}")
        return row, issues
    row["job_id"] = job.id
    row["trial_count"] = len(job.trials)
    _refine_status_from_metadata(row, job_dir)
    if row["execution_status"] == "unknown":
        row["execution_status"] = "finished"
    row["trials"] = [_trial_row(job, trial) for trial in job.trials]
    return row, issues


def _capture_report_view(coverage_report_path: Path | None) -> tuple[dict[str, Any], list[str]]:
    """Optional supplied DataEngineer CoverageReport.as_dict JSON, parsed only.

    The report carries no attested root/experiment scope, so it is preserved
    as unbound-scope context: sample names are never joined into per-job
    truth and the report never certifies a selected job.
    """
    import hashlib

    notices: list[str] = []
    if coverage_report_path is None:
        return {
            "status": "unavailable",
            "reason": "no coverage report supplied",
            "path": None,
            "sha256": None,
            "scope": "unbound",
            "data": None,
            "summary": None,
        }, notices
    path = Path(coverage_report_path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return {
            "status": "unreadable",
            "reason": f"coverage report unreadable ({type(exc).__name__})",
            "path": str(path),
            "sha256": None,
            "scope": "unbound",
            "data": None,
            "summary": None,
        }, notices
    digest = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        return {
            "status": "unreadable",
            "reason": f"coverage report malformed JSON: {exc}",
            "path": str(path),
            "sha256": digest,
            "scope": "unbound",
            "data": None,
            "summary": None,
        }, notices
    if not isinstance(payload, dict):
        return {
            "status": "unreadable",
            "reason": "coverage report top-level value is not an object",
            "path": str(path),
            "sha256": digest,
            "scope": "unbound",
            "data": None,
            "summary": None,
        }, notices
    summary_sections: dict[str, Any] = {}
    truncated_any = False
    for section in _COVERAGE_SECTIONS:
        value = payload.get(section)
        if not isinstance(value, dict):
            continue
        jobs = value.get("jobs")
        truncated = bool(value.get("truncated"))
        truncated_any = truncated_any or truncated
        summary_sections[section] = {
            "count": value.get("count"),
            "jobs": jobs if isinstance(jobs, list) else None,
            "truncated": truncated,
        }
    availability = payload.get("trajectory_availability_by_agent")
    summary = {
        "sections": summary_sections,
        "trajectory_availability_by_agent": availability if isinstance(availability, dict) else None,
        "reasons": payload.get("reasons"),
        "repair_path": payload.get("repair_path"),
    }
    notices.append(
        "coverage report is unbound-scope supplied context: it names no attested "
        "root or experiment, so its job-name samples are never joined into "
        "per-job truth and it never certifies a selected job"
    )
    if truncated_any:
        notices.append(
            "coverage report sections are truncated: counts are lower bounds, "
            "never evidence that a missing job was covered"
        )
    return {
        "status": "supplied_unbound_scope",
        "path": str(path),
        "sha256": digest,
        "scope": "unbound",
        "data": payload,
        "summary": summary,
    }, notices


def inspect_experiment(
    root: Path,
    *,
    experiment_id: str | None = None,
    job_dir: Path | None = None,
    coverage_report_path: Path | None = None,
) -> dict[str, Any]:
    """Read-only native experiment evidence view over one lab root.

    Exactly one of *experiment_id* / *job_dir* may be given, or neither:
    neither lists lightweight inventory (headers only, no trajectory loads);
    either loads only the explicitly selected job payload(s).
    """
    from evallab.queue import DirectoryQueue
    from evallab.results import discover_job_dirs

    if experiment_id is not None and job_dir is not None:
        raise ValueError("pass exactly one of experiment_id or job_dir, not both")
    root = Path(root).resolve()
    notices: list[str] = []
    issues: list[str] = []

    queue = DirectoryQueue(root / "queue", create=False)
    queue_rows: list[dict[str, Any]] = []
    specs_by_id: dict[str, dict[str, Any]] = {}
    if (root / "queue").is_dir():
        queue_rows, specs_by_id, queue_issues = _queue_rows(queue)
        issues.extend(queue_issues)
    else:
        notices.append(f"queue absent at {root / 'queue'}: experiment inventory from jobs only")

    if experiment_id is not None:
        selection: dict[str, Any] = {"mode": "by_spec", "experiment_id": experiment_id, "job_dir": None}
    elif job_dir is not None:
        resolved = Path(job_dir).resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"job_dir {job_dir} is outside the lab root {root}")
        selection = {"mode": "by_job", "experiment_id": None, "job_dir": str(resolved)}
    else:
        selection = {"mode": "inventory", "experiment_id": None, "job_dir": None}

    roots, root_notices = _jobs_roots(root, queue_rows)
    notices.extend(root_notices)
    discovered = discover_job_dirs(roots) if roots else []

    light_rows = [_light_job_row(path) for path in sorted(discovered)]
    if selection["mode"] == "by_spec":
        matched = [row for row in light_rows if row["experiment_id"] == experiment_id]
        if experiment_id not in specs_by_id:
            notices.append(f"spec {experiment_id} not in queue: matching jobs by lab-metadata only")
        if not matched:
            notices.append(f"no bound jobs for spec {experiment_id}")
        jobs = []
        for row in matched:
            full, job_issues = _selected_job_row(Path(row["path"]))
            issues.extend(job_issues)
            jobs.append(full)
        if jobs:
            notices.append(
                "file presence is evidence availability, never a validity claim: "
                "a present trajectory file is reported observed, not certified"
            )
    elif selection["mode"] == "by_job":
        full, job_issues = _selected_job_row(Path(selection["job_dir"]))
        issues.extend(job_issues)
        jobs = [full]
        notices.append(
            "file presence is evidence availability, never a validity claim: "
            "a present trajectory file is reported observed, not certified"
        )
        light_by_path = {row["path"]: row for row in light_rows}
        if full["path"] not in light_by_path:
            notices.append(
                f"selected job {Path(full['path']).name} is outside the scanned jobs "
                "roots: shown as supplied-path evidence"
            )
    else:
        jobs = light_rows

    bound_names: dict[str, list[str]] = {}
    for row in light_rows:
        bound_names.setdefault(row["experiment_id"], []).append(row["name"])
    experiments: list[dict[str, Any]] = []
    seen: set[str] = set()
    for queue_row in queue_rows:
        spec_id = queue_row["spec_id"]
        if spec_id == _HARBOR_UNBOUND or spec_id in seen:
            continue
        seen.add(spec_id)
        names = sorted(bound_names.get(spec_id, ()))
        experiments.append(
            {
                "spec_id": spec_id,
                "name": queue_row["name"],
                "agent": queue_row["agent"],
                "queue_state": queue_row["state"],
                "job_names": names,
                "job_count": len(names),
                "source": "queue",
            }
        )
    for spec_id in sorted(bound_names):
        if spec_id in seen or spec_id == _HARBOR_UNBOUND:
            continue
        names = sorted(bound_names[spec_id])
        experiments.append(
            {
                "spec_id": spec_id,
                "name": names[0],
                "agent": None,
                "queue_state": "absent",
                "job_names": names,
                "job_count": len(names),
                "source": "jobs_only",
            }
        )
        notices.append(f"spec {spec_id} binds jobs but is absent from the queue")
    unbound = sorted(bound_names.get(_HARBOR_UNBOUND, ()))
    if unbound:
        experiments.append(
            {
                "spec_id": _HARBOR_UNBOUND,
                "name": _HARBOR_UNBOUND,
                "agent": None,
                "queue_state": "absent",
                "job_names": unbound,
                "job_count": len(unbound),
                "source": "jobs_only",
                "note": (
                    "native Harbor jobs without a lab-metadata spec binding; "
                    "unbound is not an external Docker audit"
                ),
            }
        )
    experiments.sort(key=lambda item: item["spec_id"])

    capture_report, report_notices = _capture_report_view(
        Path(coverage_report_path) if coverage_report_path is not None else None
    )
    notices.extend(report_notices)

    return {
        "kind": _EXPERIMENT_VIEW_KIND,
        "selection": selection,
        "experiments": experiments,
        "queue": queue_rows,
        "jobs": jobs,
        "capture_report": capture_report,
        "notices": notices,
        "issues": issues,
    }


def render_experiment_text(report: dict[str, Any]) -> str:
    """Concise copyable text render of an experiment evidence view.

    Credential-shaped values are redacted with the explorer's existing
    helpers. Paths and commands are copyable text only; nothing executes.
    """
    lines: list[str] = []
    selection = report.get("selection") or {}
    lines.append(f"experiment evidence view ({report.get('kind')})")
    lines.append(
        f"selection: {selection.get('mode')} "
        f"{selection.get('experiment_id') or selection.get('job_dir') or 'inventory'}"
    )
    lines.append("")
    lines.append("experiments:")
    for experiment in report.get("experiments") or ():
        lines.append(
            f"  - {experiment.get('spec_id')} name={experiment.get('name')} "
            f"state={experiment.get('queue_state')} jobs={experiment.get('job_count')}"
        )
        for name in experiment.get("job_names") or ():
            lines.append(f"      job: {name}")
    if not (report.get("experiments") or ()):
        lines.append("  (none)")
    lines.append("")
    lines.append("queue:")
    for row in report.get("queue") or ():
        clean = redact_mapping(
            {"name": row.get("name"), "agent": row.get("agent"), "jobs_dir": row.get("jobs_dir")}
        )
        lines.append(
            f"  - [{row.get('state')}] {row.get('spec_id')} "
            f"{clean.get('name')} ({clean.get('agent')}) {row.get('path')}"
        )
        for receipt in row.get("reasons") or ():
            message = redact_text(str(receipt.get("message") or receipt.get("reason") or ""))
            lines.append(
                f"      reason {receipt.get('code') or receipt.get('status')}: "
                f"{message} [{receipt.get('path')}]"
            )
    if not (report.get("queue") or ()):
        lines.append("  (no queued specs)")
    lines.append("")
    lines.append("jobs:")
    for job in report.get("jobs") or ():
        lines.append(
            f"  - {job.get('name')} origin={job.get('origin')} "
            f"experiment={job.get('experiment_id')} status={job.get('execution_status')} "
            f"{job.get('path')}"
        )
        if job.get("load_issue"):
            lines.append(f"      unavailable: {redact_text(str(job['load_issue']))}")
        for trial in job.get("trials") or ():
            reward = trial.get("reward") or {}
            usage = trial.get("usage") or {}
            capture = trial.get("capture") or {}
            exception = trial.get("exception") or {}
            agent = trial.get("agent") or {}
            lines.append(
                f"      trial {trial.get('trial_name')} task={trial.get('task')} "
                f"agent={agent.get('name')} reward={reward.get('value')} "
                f"({reward.get('state')}) capture={capture.get('state')}"
            )
            if exception.get("class"):
                lines.append(
                    f"        exception: {exception.get('class')} "
                    f"(phase {exception.get('phase')}; execution signal, not a capture state)"
                )
            links = trial.get("links") or {}
            lines.append(
                f"        source: {links.get('source')} result: {links.get('result')} "
                f"trajectory: {links.get('trajectory')}"
            )
            lines.append(
                f"        usage: in={usage.get('input_tokens')} "
                f"out={usage.get('output_tokens')} usd={usage.get('cost_usd')} "
                f"({usage.get('state')}); capture note: {capture.get('reason')}"
            )
        lines.append(f"      next (copy, do not auto-run): harbor view {job.get('path')}")
    if not (report.get("jobs") or ()):
        lines.append("  (none)")
    lines.append("")
    capture_report = report.get("capture_report") or {}
    lines.append(
        f"capture report: {capture_report.get('status')} "
        f"scope={capture_report.get('scope')} {capture_report.get('path') or ''}".rstrip()
    )
    summary = capture_report.get("summary") or {}
    for section, value in (summary.get("sections") or {}).items():
        names = value.get("jobs") or []
        shown = ", ".join(str(name) for name in names[:_COVERAGE_JOB_NAME_LIMIT])
        extra = f" +{len(names) - _COVERAGE_JOB_NAME_LIMIT} more" if len(names) > _COVERAGE_JOB_NAME_LIMIT else ""
        lines.append(
            f"  - {section}: count={value.get('count')} "
            f"truncated={value.get('truncated')} jobs=[{shown}{extra}]"
        )
    if summary.get("trajectory_availability_by_agent") is not None:
        clean_availability = redact_mapping(summary["trajectory_availability_by_agent"])
        lines.append(f"  availability by agent: {clean_availability}")
    if summary.get("repair_path"):
        lines.append(f"  repair (copy, do not auto-run): {summary['repair_path']}")
    lines.append("")
    if report.get("notices"):
        lines.append("notices:")
        for notice in report["notices"]:
            lines.append(f"  - {redact_text(notice)}")
        lines.append("")
    if report.get("issues"):
        lines.append("issues:")
        for issue in report["issues"]:
            lines.append(f"  - {redact_text(issue)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
