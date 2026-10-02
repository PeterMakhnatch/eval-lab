"""HAR-151 read-only evidence snapshot and scoped investigation tools.

``snapshot_watch`` resolves explicit-root trial sources from an
``evallab.live_watch/v1`` status payload into an immutable
:class:`MonitorCorpus`: raw bytes are captured once, hashed, and parsed from
the frozen copy, so a file that grows or changes mid-read can never silently
read as clean. :class:`EvidenceTools` exposes bounded literal inspection over
exactly the primary plus related trials of one case.

Read-only: this module never writes to source roots, never follows symlinks
out of a root, never executes shell commands, and never touches the network.
All source text is untrusted data; it is redacted for secrets, never
interpreted as instruction, and never triggers filesystem reads.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from evallab.explorer import redact_text
from evallab.step_layers import TRUNCATION_MARKER_RE, stitch_steps

from .monitor_contracts import (
    EvidenceRecord,
    InvestigationCase,
    MonitorAlert,
    MonitorCorpus,
    MonitorFinding,
    SourceArtifact,
    TrialSnapshot,
)

WATCH_SCHEMA = "evallab.live_watch/v1"

#: Exact-safe job/trial directory names. Colons are excluded on purpose:
#: fleet pseudo-trials such as ``fleet:infra_spike`` never resolve to disk.
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")

#: Continuation part names inside ``agent/``.
_CONT_RE = re.compile(r"trajectory\.cont-(\d+)\.json")

#: Text artifacts are read at most this far even when trial/total budgets
#: allow more; JSON sources must fit wholly or are skipped, never truncated.
_TEXT_ARTIFACT_CAP = 2_000_000

#: Approved artifact names relative to a trial directory. Nothing else is
#: ever read: no config/lock (secret-bearing), no tests/solution trees, no
#: task directories named only in config.
_RESULT_REL = "result.json"
_VERIFIER_RELS = ("verifier/test-stdout.txt", "verifier/agent.diff")

_CALL_FIELDS = ("command", "keystrokes", "cmd", "script", "input", "code")

# Bounds for bounded tool output assembly.
_SNIPPET_CHARS = 400
_SNIPPET_COUNT = 8
_SEARCH_WINDOW = 200
_SEARCH_HITS = 20
_READ_STEP_LIMIT = 25


@dataclass
class _Capture:
    """One file's frozen bytes plus its identity."""

    rel: str
    sha256: str
    size_bytes: int
    raw: bytes
    changed: bool


@dataclass
class _Budget:
    trial_remaining: int
    total_remaining: list[int]

    def take(self, size: int) -> bool:
        """Debit exactly the bytes read; report whether they fit the caps."""
        fit = size <= self.trial_remaining and size <= self.total_remaining[0]
        self.trial_remaining -= size
        self.total_remaining[0] -= size
        return fit


def _refusal_for_link(base: Path, rel: str, *, top: Path) -> str | None:
    """Refuse when ``base/rel`` escapes scope or crosses a symlink component.

    Every component is checked with lstat (never followed), so a symlinked
    ``agent/`` directory, a symlinked source file, or a ``..`` climb out of
    scope is refused before any open. Best effort against a hostile local
    writer: the open itself uses ``O_NOFOLLOW`` and the open descriptor is
    revalidated; a mid-read race is flagged, not isolated.
    """
    node = base
    for part in Path(rel).parts:
        if part in ("", "."):
            continue
        node = node.parent if part == ".." else node / part
        if node != top and top not in node.parents:
            return f"path escapes permitted scope, not read: {rel}"
        try:
            if node.is_symlink():
                return f"symlinked path component, not read: {rel}"
        except OSError:
            return f"unreadable path component: {rel}"
    return None


def _read_frozen(path: Path, rel: str, *, budget: _Budget, whole: bool) -> _Capture | None:
    """Capture raw bytes through one file descriptor, or skip.

    Oversized files are refused whole and never hashed as a partial prefix:
    anything over the remaining trial/total budget (and, for text artifacts,
    ``_TEXT_ARTIFACT_CAP``) is skipped with an explicit limitation. Bounded
    truncation happens later at record rendering, after the full raw hash.

    The open uses ``O_NOFOLLOW`` with fstat/read/fstat on the same descriptor
    plus a descriptor-vs-path identity recheck, so a replaced or grown file
    is flagged ``changed``, never silently treated as clean. This does not
    claim isolation against a hostile local filesystem race.
    """
    try:
        claimed = path.stat(follow_symlinks=False).st_size
    except OSError:
        return None
    cap = budget.trial_remaining if whole else min(budget.trial_remaining, _TEXT_ARTIFACT_CAP)
    if claimed > cap or claimed > budget.total_remaining[0]:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size > cap
            or before.st_size > budget.total_remaining[0]
        ):
            return None
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(1 << 20, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    try:
        current = path.stat()
    except OSError:
        current = None
    replaced = current is None or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
    changed = (
        replaced
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or len(raw) != before.st_size
    )
    budget.take(len(raw))
    return _Capture(rel=rel, sha256=hashlib.sha256(raw).hexdigest(),
                     size_bytes=len(raw), raw=raw, changed=changed)


def _safe_name(value: Any) -> bool:
    return isinstance(value, str) and _NAME_RE.fullmatch(value) is not None


def _resolve_trial_dir(job: Any, trial: Any, roots: Sequence[Path]) -> tuple[Path | None, str | None]:
    """Resolve one ``job/trial`` pair under explicit roots only.

    Returns ``(directory, None)`` on success, else ``(None, limitation)``.
    Ambiguous names (same pair under two roots), unsafe names, and any
    symlink/traversal escape are refusals, never guesses.
    """
    if not _safe_name(job) or not _safe_name(trial):
        return None, f"unsafe job/trial name, not resolved: {job!r}/{trial!r}"
    found: list[Path] = []
    for root in roots:
        try:
            resolved_root = root.resolve()
        except OSError:
            continue
        if not resolved_root.is_dir():
            continue
        if resolved_root.name == trial and resolved_root.parent.name == job:
            options = [resolved_root]
        elif resolved_root.name == job:
            options = [resolved_root / trial]
        else:
            options = [resolved_root / job / trial]
        for candidate in options:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved != resolved_root and resolved_root not in resolved.parents:
                continue
            try:
                node = resolved_root
                linked = False
                for part in candidate.relative_to(resolved_root).parts:
                    node = node / part
                    if node.is_symlink():
                        linked = True
                        break
            except OSError:
                continue
            if linked:
                return None, f"symlinked path component under roots: {job}/{trial}"
            if resolved.is_dir():
                found.append(resolved)
    unique = sorted(set(found))
    if not unique:
        return None, f"trial not found under explicit roots: {job}/{trial}"
    if len(unique) > 1:
        return None, f"ambiguous trial name across roots, not resolved: {job}/{trial}"
    return unique[0], None


_ALERT_REDACTED_FIELDS = ("quote", "detail", "target", "trial", "task")


def _coerce_alert(payload: Any, *, job: str, trial: str, task: str) -> tuple[MonitorAlert | None, bool]:
    """Coerce one watch alert dict, redacting free-text fields for secrets."""
    if not isinstance(payload, dict):
        return None, False
    allowed = {"rule", "severity", "scope", "job", "trial", "task",
               "step_ref", "quote", "detail", "target", "trials"}
    cleaned = {key: payload[key] for key in allowed if key in payload}
    cleaned.setdefault("job", job)
    cleaned.setdefault("trial", trial)
    cleaned.setdefault("task", task)
    if cleaned.get("severity") not in ("high", "medium", "low"):
        return None, False
    redacted = False
    for field in _ALERT_REDACTED_FIELDS:
        value = cleaned.get(field)
        if isinstance(value, str):
            scrubbed = redact_text(value)
            if scrubbed != value:
                cleaned[field] = scrubbed
                redacted = True
    try:
        return MonitorAlert(**cleaned), redacted
    except ValidationError:
        return None, False


def _reward_from_result(result: Any) -> tuple[float | None, str | None]:
    """Reward from frozen ``result.json`` bytes (mirrors probe03's rule).

    ``verifier_result.rewards.reward`` when present and scored; missing or
    ``-1`` means unscored. Booleans, non-finite floats, and non-numeric
    values are rejected as unavailable (never zero, never ``float(True)``),
    because a NaN would crash whole-corpus validation. ``reward.txt`` is
    deliberately not consulted: it is outside the permitted source set.
    """
    if not isinstance(result, dict):
        return None, None
    verifier = result.get("verifier_result")
    rewards = verifier.get("rewards") if isinstance(verifier, dict) else None
    raw = rewards.get("reward", "MISSING") if isinstance(rewards, dict) else "MISSING"
    if raw == "MISSING" or raw == -1:
        return None, None
    if isinstance(raw, bool):
        return None, "result reward is a boolean, treated as unscored"
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, f"result reward is non-numeric ({type(raw).__name__}), treated as unscored"
    if not math.isfinite(value):
        return None, "result reward is non-finite, treated as unscored"
    return value, None


def _finished_state(result_ok: bool, result: Any) -> tuple[Literal["running", "finished"], str | None]:
    """Derive running/finished from a plausible recorded timestamp string."""
    if not result_ok or not isinstance(result, dict):
        return "running", None
    raw = result.get("finished_at")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "running", None
    if not isinstance(raw, str) or len(raw.strip()) > 100:
        return "running", "malformed finished_at, not a completed verdict"
    try:
        datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return "running", "malformed finished_at, not a completed verdict"
    return "finished", None


def _row_alerts(row: dict[str, Any], *, job: Any, trial: Any, task: str | None) -> tuple[tuple[MonitorAlert, ...], bool, int]:
    """Coerce a status row's open alerts, preserving suspicion on shells."""
    alerts: list[MonitorAlert] = []
    redacted = False
    dropped = 0
    open_alerts = row.get("open_alerts", [])
    if isinstance(open_alerts, list):
        for payload in open_alerts:
            alert, scrubbed = _coerce_alert(
                payload, job=str(job), trial=str(trial), task=task or str(trial))
            if alert is None:
                dropped += 1
                continue
            alerts.append(alert)
            redacted = redacted or scrubbed
    return tuple(alerts), redacted, dropped

def _render_call(call: Any) -> str | None:
    if not isinstance(call, dict):
        return None
    name = call.get("function_name")
    args = call.get("arguments")
    if isinstance(args, dict):
        for fname in _CALL_FIELDS:
            value = args.get(fname)
            if isinstance(value, str) and value.strip():
                keys = value.strip()
                break
        else:
            keys = ""
    elif isinstance(args, str):
        keys = args.strip()
    else:
        for fname in _CALL_FIELDS:
            value = call.get(fname)
            if isinstance(value, str) and value.strip():
                keys = value.strip()
                break
        else:
            keys = ""
    if not keys:
        return None
    if isinstance(name, str) and name.strip():
        return f"$ [{name.strip()}] {keys}"
    return f"$ {keys}"


def _render_step_text(step: Mapping[str, Any]) -> str:
    """Render one trajectory step as plain inspection text (data, not verdict)."""
    parts: list[str] = []
    message = step.get("message")
    if isinstance(message, str) and message.strip():
        parts.append(message.strip())
    reasoning = step.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        parts.append("reasoning: " + reasoning.strip())
    calls = step.get("tool_calls")
    if isinstance(calls, list):
        for call in calls:
            rendered = _render_call(call)
            if rendered:
                parts.append(rendered)
    observation = step.get("observation")
    if isinstance(observation, dict):
        results = observation.get("results")
        if isinstance(results, list):
            for result in results:
                if isinstance(result, str) and result.strip():
                    parts.append(result.strip())
                elif isinstance(result, dict):
                    content = result.get("content")
                    if isinstance(content, str) and content.strip():
                        parts.append(content.strip())
    elif isinstance(observation, str) and observation.strip():
        parts.append(observation.strip())
    return "\n".join(parts)

#: Producer-truncated terminal output whose full text lives in a sandbox
#: spill file outside snapshot scope. Exact marker written by
#: ``evallab.loopfix.cap_output`` (also matched by
#: ``evallab.upstream_fetch._OUTPUT_SPILL_RE`` when hydrating retained
#: output); the spill file is never read here, so a match means evidence is
#: missing, not merely mentioned. A bare spill path without this marker --
#: e.g. an agent ``cat``/``grep`` command naming it -- is benign and matches
#: nothing here.
_SPILL_MARKER_RE = re.compile(
    r"\[\.\.\. output limited to (?P<limit>\d+) characters; \d+ characters omitted\. "
    r"Full output: /logs/agent/evallab-output/step-\d{4,}\.txt — grep or read it there \.\.\.\]"
)

#: Sandbox spill failure appended by harbor_terminus when the full output
#: could not be saved at all: there is no fuller text anywhere in scope.
_NOT_SAVED_RE = re.compile(r"\[full output was not saved:[^\]]*\]")

#: Step keys whose nonempty out-of-line ``*_ref`` without its inline
#: counterpart means content this snapshot never read: refs are never
#: hydrated and no new paths are followed.
_REF_INLINE_PAIRS = (
    ("message_ref", "message"),
    ("reasoning_content_ref", "reasoning_content"),
    ("content_ref", "content"),
)


def _has_inline(value: Any) -> bool:
    """Whether an inline content slot actually carries readable content."""
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return False


def _nonempty_ref(value: Any) -> bool:
    """Whether an out-of-line content ref names something beyond this file."""
    return isinstance(value, str) and bool(value.strip())


def _step_content_gaps(step: Mapping[str, Any]) -> tuple[bool, bool]:
    """Report ``(unexpanded_ref, producer_truncated)`` for one stitched step.

    Only inline message/reasoning/observation strings and their ``*_ref``
    counterparts are inspected. Tool-call command text is never scanned, so
    an agent command that merely names a spill path stays a benign mention;
    only the producers' own truncation markers count as missing output.
    """
    unexpanded = any(
        _nonempty_ref(step.get(ref_key)) and not _has_inline(step.get(inline_key))
        for ref_key, inline_key in _REF_INLINE_PAIRS
    )
    inline_texts: list[str] = []
    for key in ("message", "reasoning_content", "content"):
        value = step.get(key)
        if isinstance(value, str) and value.strip():
            inline_texts.append(value)
    result_lists: list[Any] = []
    observation = step.get("observation")
    if isinstance(observation, dict):
        results = observation.get("results")
        if isinstance(results, list):
            result_lists.append(results)
    flat = step.get("observation_results")
    if isinstance(flat, list):
        result_lists.append(flat)
    for results in result_lists:
        for item in results:
            if not isinstance(item, dict):
                continue
            if _nonempty_ref(item.get("content_ref")) and not _has_inline(item.get("content")):
                unexpanded = True
            content = item.get("content")
            if isinstance(content, str) and content.strip():
                inline_texts.append(content)
    truncated = any(
        _SPILL_MARKER_RE.search(text) is not None
        or TRUNCATION_MARKER_RE.search(text) is not None
        or _NOT_SAVED_RE.search(text) is not None
        for text in inline_texts
    )
    return unexpanded, truncated


def _bound(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + "…", True


def _normalize_ref(ref: str) -> str | None:
    """Normalize a ``continued_trajectory_ref`` to an ``agent/``-relative name.

    Same ``.``/``..`` collapsing as the evidence chain walk; absolute refs
    are treated as trial-rooted. Returns ``None`` on escape.
    """
    work = ref.lstrip("/") if ref.startswith("/") else ref
    parts: list[str] = []
    for part in work.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    if not parts:
        return None
    if parts[0] == "agent":
        parts = parts[1:]
    if len(parts) != 1:
        return None
    return "agent/" + parts[0]


def snapshot_watch(
    status: dict[str, Any],
    roots: Sequence[Path],
    *,
    max_trials: int = 200,
    max_trial_bytes: int = 16_000_000,
    max_record_chars: int = 32_000,
    max_total_bytes: int = 64_000_000,
) -> MonitorCorpus:
    """Snapshot explicit-root trial sources named by a live-watch status payload."""
    if not isinstance(status, dict) or status.get("schema") != WATCH_SCHEMA:
        raise ValueError(f"status must carry schema {WATCH_SCHEMA!r}")
    rows = status.get("trials")
    if not isinstance(rows, list):
        raise ValueError("status['trials'] must be a list")
    fleet_raw = status.get("fleet_alerts", [])
    if not isinstance(fleet_raw, list):
        raise ValueError("status['fleet_alerts'] must be a list")

    nondicts = sum(1 for row in rows if not isinstance(row, dict))
    ordered = sorted(
        [row for row in rows if isinstance(row, dict)],
        key=lambda row: (str(row.get("job", "")), str(row.get("trial", ""))),
    )
    limitations: list[str] = []
    if nondicts:
        limitations.append(f"{nondicts} non-dict status rows ignored, never snapshotted")
    if len(ordered) > max_trials:
        limitations.append(
            f"trial cap: snapshotted first {max_trials} of {len(ordered)} status rows in (job, trial) order"
        )
    total_remaining = [max_total_bytes]
    snapshots: list[TrialSnapshot] = []
    seen_keys: set[str] = set()

    for index, row in enumerate(ordered):
        job = row.get("job")
        trial = row.get("trial")
        task = row.get("task")
        trial_key = f"{job}/{trial}"
        if trial_key in seen_keys:
            limitations.append(f"duplicate status row ignored: {trial_key}")
            continue
        seen_keys.add(trial_key)
        beyond_cap = index >= max_trials
        snapshots.append(_snapshot_row(
            row, job=job, trial=trial, task=task, trial_key=trial_key,
            roots=roots, beyond_cap=beyond_cap,
            max_trial_bytes=max_trial_bytes, max_record_chars=max_record_chars,
            total_remaining=total_remaining,
        ))

    fleet_alerts: list[MonitorAlert] = []
    for payload in fleet_raw:
        alert, redacted = _coerce_alert(payload, job="fleet", trial="fleet", task="fleet")
        if alert is None:
            limitations.append("fleet alert dropped: invalid shape or severity")
            continue
        fleet_alerts.append(alert)
        if redacted:
            limitations.append("fleet alert secret-redacted")

    corpus_limitations = list(limitations)
    budget_hit = total_remaining[0] <= 0 or any(
        "byte budget" in lim for snap in snapshots for lim in snap.limitations
    )
    if budget_hit:
        corpus_limitations.append(
            f"total byte budget exhausted: {max_total_bytes} bytes across trials in (job, trial) order"
        )
    return MonitorCorpus(trials=tuple(snapshots), fleet_alerts=tuple(fleet_alerts),
                         limitations=tuple(corpus_limitations))


def _snapshot_row(
    row: dict[str, Any],
    *,
    job: Any,
    trial: Any,
    task: Any,
    trial_key: str,
    roots: Sequence[Path],
    beyond_cap: bool,
    max_trial_bytes: int,
    max_record_chars: int,
    total_remaining: list[int],
) -> TrialSnapshot:
    trial_limits: list[str] = []
    row_task = str(task) if isinstance(task, str) and task else None
    task_text = row_task

    def shell(reason: str) -> TrialSnapshot:
        alerts, redacted, dropped = _row_alerts(row, job=job, trial=trial, task=row_task)
        limits = [reason]
        if redacted:
            limits.append("watch alert secret-redacted")
        if dropped:
            limits.append("watch alert dropped: invalid shape or severity")
        return TrialSnapshot(
            trial_key=trial_key,
            job=str(job),
            trial=str(trial),
            task=row_task,
            source_path="",
            state="unavailable",
            reward=None,
            complete=False,
            artifacts=(),
            records=(),
            alerts=alerts,
            limitations=tuple(limits),
        )

    if beyond_cap:
        return shell("beyond max_trials cap: not read")
    trial_dir, problem = _resolve_trial_dir(job, trial, roots)
    if trial_dir is None:
        return shell(problem or "unresolvable trial")
    if total_remaining[0] <= 0:
        return shell("total byte budget exhausted before this trial")

    budget = _Budget(trial_remaining=max_trial_bytes, total_remaining=total_remaining)
    artifacts: list[SourceArtifact] = []
    captures: dict[str, _Capture] = {}
    complete = True

    job_dir = trial_dir.parent

    def capture_file(path: Path, rel: str, *, whole: bool, optional: bool) -> _Capture | None:
        nonlocal complete
        refusal = _refusal_for_link(trial_dir, rel, top=job_dir)
        if refusal is not None:
            trial_limits.append(refusal)
            complete = False
            return None
        if not path.is_file():
            if not optional:
                trial_limits.append(f"missing source: {rel}")
                complete = False
            else:
                trial_limits.append(f"source unavailable: {rel}")
            return None
        capture = _read_frozen(path, rel, budget=budget, whole=whole)
        if capture is None:
            trial_limits.append(f"source over byte budget, skipped: {rel}")
            complete = False
            return None
        artifacts.append(SourceArtifact(path=rel, sha256=capture.sha256, size_bytes=capture.size_bytes))
        captures[rel] = capture
        if capture.changed:
            trial_limits.append(f"source changed during read, not treated as clean: {rel}")
            complete = False
        return capture

    # 1. Trajectory head (agent/ first, trial-root fallback like live_watch)
    #    plus agent/ continuations; exact approved names only.
    part_refs: list[tuple[str, str]] = []
    for candidate in ("agent/trajectory.json", "trajectory.json"):
        candidate_path = trial_dir / candidate
        refusal = _refusal_for_link(trial_dir, candidate, top=job_dir)
        if refusal is not None:
            if candidate_path.is_symlink():
                trial_limits.append(refusal)
                complete = False
            continue
        if candidate_path.is_file():
            part_refs.append((candidate, "head"))
            break
    if not part_refs:
        trial_limits.append("missing source: agent/trajectory.json")
        complete = False
    agent_path = trial_dir / "agent"
    if _refusal_for_link(trial_dir, "agent", top=job_dir) is None:
        try:
            entries = sorted(agent_path.iterdir(), key=lambda p: p.name) if agent_path.is_dir() else []
        except OSError:
            entries = []
        conts = sorted(
            (entry.name for entry in entries if _CONT_RE.fullmatch(entry.name)),
            key=lambda name: int(name.removeprefix("trajectory.cont-").removesuffix(".json")),
        )
        if len(conts) > 512:
            trial_limits.append(
                f"continuation count capped at 512 parts, rest ignored: {len(conts)} found")
            conts = conts[:512]
            complete = False
        part_refs.extend(
            (f"agent/{name}", name.removeprefix("trajectory.").removesuffix(".json"))
            for name in conts
        )
    elif agent_path.is_symlink():
        trial_limits.append("symlinked path component, not read: agent/ continuations")
        complete = False
    docs: list[Any] = []
    part_docs: dict[str, Any] = {}
    origin: dict[int, tuple[str, str, str]] = {}
    for rel, stem in part_refs:
        capture = capture_file(trial_dir / rel, rel, whole=True, optional=True)

        if capture is None:
            continue
        try:
            payload = json.loads(capture.raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            trial_limits.append(f"malformed source, not treated as clean: {rel} ({type(exc).__name__})")
            complete = False
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("steps"), list):
            trial_limits.append(f"malformed source, no steps list: {rel}")
            complete = False
            continue
        for pos, raw_step in enumerate(payload["steps"]):
            if isinstance(raw_step, Mapping) and not raw_step.get("is_copied_context"):
                sid = raw_step.get("step_id")
                ref = f"{stem}#{sid}" if isinstance(sid, int) else f"{stem}#idx{pos}"
                origin.setdefault(id(raw_step), (rel, stem, ref))
        docs.append(payload)
        part_docs[rel] = payload

    malformed_steps = sum(
        1 for payload in part_docs.values() for step in payload.get("steps", [])
        if not isinstance(step, Mapping)
    )
    if malformed_steps:
        trial_limits.append(f"{malformed_steps} malformed steps excluded, not counted as execution")
        complete = False

    stitched, stats = stitch_steps(docs)
    if stats.copied_context_steps:
        trial_limits.append(
            f"{stats.copied_context_steps} copied-context steps excluded from execution view"
        )

    # Continuation chain: every declared ref must be a captured part.
    # stitch_steps appends first-occurrence step objects unchanged, so
    # origin[id(step)] below maps each stitched step to its source part
    # (verified against the step_layers.stitch_steps source, not assumed).
    captured_names = {Path(item).name for item in part_docs}
    for rel, payload in part_docs.items():
        ref = payload.get("continued_trajectory_ref")
        if isinstance(ref, str) and ref:
            target = _normalize_ref(ref)
            if target is None or Path(target).name not in captured_names:
                trial_limits.append(f"continuation chain incomplete at {rel}: ref {ref!r} not captured")
                complete = False

    # 2. result.json (required for a finished verdict).
    result_capture = capture_file(trial_dir / _RESULT_REL, _RESULT_REL, whole=True, optional=False)
    result: Any = None
    result_ok = False
    if result_capture is not None:
        try:
            result = json.loads(result_capture.raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            trial_limits.append(f"malformed source, not treated as clean: {_RESULT_REL} ({type(exc).__name__})")
            complete = False
        else:
            if isinstance(result, dict):
                result_ok = True
            else:
                trial_limits.append(f"malformed source, top-level JSON is not an object: {_RESULT_REL}")
                complete = False
    if result_capture is None or not result_ok:
        complete = False

    reward: float | None = None
    if result_ok:
        reward, reward_limitation = _reward_from_result(result)
        if reward_limitation is not None:
            trial_limits.append(reward_limitation)
            complete = False
        task_name = result.get("task_name") if isinstance(result, dict) else None
        if isinstance(task_name, str) and task_name.strip():
            task_text = task_name.strip()
    state, state_limitation = _finished_state(result_ok, result)
    if state_limitation is not None:
        trial_limits.append(state_limitation)
    if state != "finished":
        trial_limits.append("live prefix: no finished result, not a completed verdict")
        complete = False

    # 3. Optional spec: instruction.md physically inside trial or job dir.
    spec_capture: _Capture | None = None
    spec_rel: str | None = None
    for candidate_rel in ("instruction.md", "../instruction.md"):
        candidate = trial_dir / candidate_rel
        refusal = _refusal_for_link(trial_dir, candidate_rel, top=job_dir)
        if refusal is not None:
            if candidate.is_symlink():
                trial_limits.append(refusal)
                complete = False
            continue
        if not candidate.is_file():
            continue
        spec_rel = candidate_rel
        spec_capture = _read_frozen(candidate, spec_rel, budget=budget, whole=False)
        if spec_capture is None:
            trial_limits.append("spec over byte budget, skipped: instruction.md")
        else:
            artifacts.append(SourceArtifact(
                path=spec_rel, sha256=spec_capture.sha256,
                size_bytes=spec_capture.size_bytes))
            if spec_capture.changed:
                trial_limits.append("source changed during read, not treated as clean: instruction.md")
                complete = False
        break
    if spec_capture is None and spec_rel is None:
        trial_limits.append("spec unavailable: no instruction.md inside trial/job dir")

    # 4. Bounded verifier artifacts (refused whole when over budget, never partial).
    text_captures: dict[str, _Capture] = {}
    for rel in _VERIFIER_RELS:
        capture = capture_file(trial_dir / rel, rel, whole=False, optional=True)
        if capture is not None:
            text_captures[rel] = capture

    # --- Records ---
    records: list[EvidenceRecord] = []
    step_records = [step for step in stitched if isinstance(step, Mapping)]
    n_steps = len(step_records)
    truncated_count = 0
    redacted_count = 0
    ref_gap_steps = 0
    capped_source_steps = 0
    orphan_steps = 0
    ordinal = 0
    for step in step_records:
        info = origin.get(id(step))
        if info is None:
            # No invented digest or source: a stitched step without a captured
            # origin part is excluded and the snapshot marked incomplete.
            orphan_steps += 1
            continue
        ordinal += 1
        document, step_ref = info[0], info[2]
        part_sha = captures[document].sha256
        raw_text = _render_step_text(step)
        gap_ref, gap_capped = _step_content_gaps(step)
        if gap_ref:
            ref_gap_steps += 1
        if gap_capped:
            capped_source_steps += 1
        redacted_text = redact_text(raw_text)
        was_redacted = redacted_text != raw_text
        if was_redacted:
            redacted_count += 1
        bounded, was_truncated = _bound(redacted_text, max_record_chars)
        if was_truncated:
            truncated_count += 1
        role = str(step.get("source") or "unknown").lower() or "unknown"
        records.append(EvidenceRecord(
            record_id=f"{trial_key}:step:{ordinal:06d}",
            trial_key=trial_key,
            document=document,
            source_sha256=part_sha,
            step_ref=step_ref,
            ordinal=ordinal,
            role=role,
            text=bounded,
            truncated=was_truncated,
            redacted=was_redacted,
        ))
    n_steps = ordinal
    if orphan_steps:
        trial_limits.append(f"{orphan_steps} stitched steps without origin part excluded")
        complete = False
    if truncated_count:
        trial_limits.append(f"{truncated_count} step records truncated to {max_record_chars} chars")
        complete = False
    if ref_gap_steps:
        trial_limits.append(
            f"{ref_gap_steps} steps reference out-of-line content not read in this snapshot, "
            "not treated as clean"
        )
        complete = False
    if capped_source_steps:
        trial_limits.append(
            f"{capped_source_steps} steps contain producer-truncated output outside snapshot scope, "
            "not treated as clean"
        )
        complete = False
    if redacted_count:
        trial_limits.append(f"{redacted_count} step records secret-redacted, digest is pre-redaction")

    meta_text = (
        f"trial {trial_key} task={task_text or '?'} state={state} "
        f"reward={reward if reward is not None else 'unscored'} "
        f"steps={n_steps} parts={len(part_docs)}"
    )
    records.append(EvidenceRecord(
        record_id=f"{trial_key}:metadata", trial_key=trial_key, document="metadata",
        source_sha256=hashlib.sha256(meta_text.encode("utf-8")).hexdigest(),
        step_ref=None, ordinal=None, role="metadata", text=meta_text,
        truncated=False, redacted=False,
    ))

    def artifact_record(capture: _Capture, record_id: str, document: str, role: str) -> EvidenceRecord:
        try:
            decoded = capture.raw.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 -- errors=replace never raises; belt and braces
            decoded = ""
        redacted_text = redact_text(decoded)
        was_redacted = redacted_text != decoded
        bounded, was_truncated = _bound(redacted_text, max_record_chars)
        return EvidenceRecord(
            record_id=record_id, trial_key=trial_key, document=document,
            source_sha256=capture.sha256, step_ref=None, ordinal=None, role=role,
            text=bounded, truncated=was_truncated, redacted=was_redacted,
        )

    if result_capture is not None:
        record = artifact_record(result_capture, f"{trial_key}:result", "result.json", "result")
        records.append(record)
        if record.truncated:
            trial_limits.append(f"result record truncated to {max_record_chars} chars")
            complete = False
        if record.redacted:
            trial_limits.append("result record secret-redacted, digest is pre-redaction")
    if spec_capture is not None and spec_rel is not None:
        record = artifact_record(spec_capture, f"{trial_key}:spec", "spec", "spec")
        records.append(record)
        if record.truncated:
            trial_limits.append("spec record truncated")
            complete = False
    for rel, capture in text_captures.items():
        record = artifact_record(capture, f"{trial_key}:{rel}", rel, "verifier")
        records.append(record)
        if record.truncated:
            trial_limits.append(f"verifier record truncated: {rel}")
            complete = False

    alerts, alerts_redacted, alerts_dropped = _row_alerts(row, job=job, trial=trial, task=task_text)
    if alerts_redacted:
        trial_limits.append("watch alert secret-redacted")
    if alerts_dropped:
        trial_limits.append("watch alert dropped: invalid shape or severity")

    return TrialSnapshot(
        trial_key=trial_key,
        job=str(job),
        trial=str(trial),
        task=task_text,
        source_path=str(trial_dir),
        state=state,
        reward=reward,
        complete=complete,
        artifacts=tuple(artifacts),
        records=tuple(records),
        alerts=tuple(alerts),
        limitations=tuple(trial_limits),
    )


class EvidenceTools:
    """Bounded literal inspection over one case's primary plus related trials.

    Every returned record carries its ``record_id``; only records actually
    returned count as viewed, and citation validation requires the quoted
    text to appear verbatim in a span actually shown for that record (an
    overview/search excerpt only supports quotes from its visible window),
    so unseen or fabricated quotes fail.
    """

    def __init__(self, corpus: MonitorCorpus, case: InvestigationCase, *, max_chars: int = 12000):
        if case.snapshot_id != corpus.digest:
            raise ValueError("case snapshot digest does not match corpus")
        known = {trial.trial_key for trial in corpus.trials}
        missing = [key for key in (case.primary_trial, *case.related_trials) if key not in known]
        if missing:
            raise ValueError(f"case references trials absent from corpus: {missing}")
        self._corpus = corpus
        self._case = case
        self._max_chars = max_chars
        self._allowed = (case.primary_trial, *case.related_trials)
        self._by_key = {trial.trial_key: trial for trial in corpus.trials}
        self._records: dict[str, EvidenceRecord] = {
            record.record_id: record for trial in corpus.trials for record in trial.records
        }
        self._viewed: list[str] = []
        self._viewed_set: set[str] = set()
        self._visible: dict[str, list[str]] = {}

    @property
    def viewed_records(self) -> tuple[str, ...]:
        return tuple(self._viewed)

    def _mark_viewed(self, shown: Sequence[tuple[str, str]]) -> None:
        """Record exactly the text spans shown for each record id.

        Citation validation checks quotes against these visible spans, never
        against unseen stored text: a truncated snippet only supports quotes
        from its visible window.
        """
        for record_id, span in shown:
            if record_id not in self._viewed_set:
                self._viewed_set.add(record_id)
                self._viewed.append(record_id)
            self._visible.setdefault(record_id, []).append(span)

    def _fit(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Enforce a hard serialized-char bound, pruning with explicit counts."""
        omitted: dict[str, int] = {}
        if isinstance(payload.get("omitted"), dict):
            omitted.update(payload["omitted"])

        def collect() -> list[tuple[dict[str, Any], str]]:
            found: list[tuple[dict[str, Any], str]] = []

            def visit(node: Any) -> None:
                if isinstance(node, dict):
                    for key, value in node.items():
                        if key in ("text", "excerpt") and isinstance(value, str):
                            found.append((node, key))
                        else:
                            visit(value)
                elif isinstance(node, list):
                    for item in node:
                        visit(item)

            visit(payload)
            found.sort(key=lambda item: len(item[0][item[1]]), reverse=True)
            return found

        while len(json.dumps(payload, ensure_ascii=False)) > self._max_chars:
            trimmed = False
            for node, key in collect():
                value = node[key]
                assert isinstance(value, str)
                keep = max(64, len(value) // 2)
                if len(value) <= 64:
                    continue
                shortened = value[:keep].rstrip() + "…[output-capped]"
                if len(shortened) >= len(value):
                    continue
                node[key] = shortened
                omitted["chars"] = omitted.get("chars", 0) + (len(value) - len(shortened))
                trimmed = True
                break
            if trimmed:
                continue
            pruned = False
            for list_key, omit_key in (("records", "records"), ("hits", "hits"),
                                      ("snippets", "snippets"), ("trials", "trials"),
                                      ("related", "related")):
                entries = payload.get(list_key)
                if isinstance(entries, list) and entries:
                    entries.pop()
                    omitted[omit_key] = omitted.get(omit_key, 0) + 1
                    pruned = True
                    break
            if not pruned:
                break
        payload["omitted"] = omitted
        return payload

    def _trial_meta(self, trial: TrialSnapshot) -> dict[str, Any]:
        ids: dict[str, str] = {}
        for record in trial.records:
            if record.document in ("metadata", "result.json", "spec") and record.document not in ids:
                ids[record.document] = record.record_id
        return {
            "trial_key": trial.trial_key,
            "job": trial.job,
            "trial": trial.trial,
            "task": trial.task,
            "state": trial.state,
            "complete": trial.complete,
            "reward": trial.reward,
            "n_records": len(trial.records),
            "record_ids": ids,
            "limitations": list(trial.limitations),
        }

    def overview(self) -> dict[str, Any]:
        trials = [self._trial_meta(self._by_key[key]) for key in self._allowed]
        primary = self._by_key[self._allowed[0]]
        steps = [r for r in primary.records if r.ordinal is not None]
        snippets: list[dict[str, Any]] = []
        omitted_snippets = max(0, len(steps) - _SNIPPET_COUNT)
        for record in steps[:_SNIPPET_COUNT]:
            excerpt, _ = _bound(record.text, _SNIPPET_CHARS)
            snippets.append({
                "record_id": record.record_id,
                "trial_key": record.trial_key,
                "document": record.document,
                "step_ref": record.step_ref,
                "ordinal": record.ordinal,
                "excerpt": excerpt,
            })
        fitted = self._fit({
            "case_id": self._case.case_id,
            "snapshot_id": self._case.snapshot_id,
            "primary_trial": self._case.primary_trial,
            "related_trials": list(self._case.related_trials),
            "trials": trials,
            "snippets": snippets,
            "omitted": {"snippets": omitted_snippets} if omitted_snippets else {},
            "limitations": list(self._corpus.limitations),
        })
        self._mark_viewed([
            (item["record_id"], item["excerpt"])
            for item in fitted.get("snippets", [])
            if isinstance(item, dict) and isinstance(item.get("excerpt"), str)
        ])
        return fitted

    def read_steps(self, trial_key: str, start: int, end: int) -> dict[str, Any]:
        if trial_key not in self._by_key or trial_key not in set(self._allowed):
            return {"error": f"trial out of case scope: {trial_key}"}
        if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
            return {"error": f"invalid 1-based range: {start}..{end}"}
        steps = sorted(
            (r for r in self._by_key[trial_key].records if r.ordinal is not None),
            key=lambda r: r.ordinal or 0,
        )
        window = steps[start - 1:end]
        if len(window) > _READ_STEP_LIMIT:
            omitted_extra = len(window) - _READ_STEP_LIMIT
            window = window[:_READ_STEP_LIMIT]
        else:
            omitted_extra = 0
        rendered = [{
            "record_id": record.record_id,
            "ordinal": record.ordinal,
            "document": record.document,
            "step_ref": record.step_ref,
            "role": record.role,
            "text": record.text,
            "truncated": record.truncated,
            "redacted": record.redacted,
        } for record in window]
        omitted: dict[str, int] = {}
        if omitted_extra:
            omitted["records"] = omitted_extra
        fitted = self._fit({
            "trial_key": trial_key,
            "start": start,
            "end": end,
            "total_steps": len(steps),
            "records": rendered,
            "omitted": omitted,
        })
        self._mark_viewed([
            (item["record_id"], item["text"])
            for item in fitted.get("records", [])
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ])
        return fitted

    def search(self, query: str, trial_key: str | None = None) -> dict[str, Any]:
        if not isinstance(query, str) or not (1 <= len(query) <= 300):
            return {"error": "query must be 1..300 characters of literal text"}
        if trial_key is not None and (trial_key not in self._by_key or trial_key not in set(self._allowed)):
            return {"error": f"trial out of case scope: {trial_key}"}
        keys = (trial_key,) if trial_key is not None else self._allowed
        needle = query.casefold()
        hits: list[dict[str, Any]] = []
        searched = 0
        omitted_hits = 0
        for key in keys:
            for record in self._by_key[key].records:
                searched += 1
                pos = record.text.casefold().find(needle)
                if pos < 0:
                    continue
                if len(hits) >= _SEARCH_HITS:
                    omitted_hits += 1
                    continue
                lo = max(0, pos - _SEARCH_WINDOW)
                hi = min(len(record.text), pos + len(query) + _SEARCH_WINDOW)
                hits.append({
                    "record_id": record.record_id,
                    "trial_key": record.trial_key,
                    "document": record.document,
                    "step_ref": record.step_ref,
                    "ordinal": record.ordinal,
                    "excerpt": record.text[lo:hi],
                })
        omitted: dict[str, int] = {}
        if omitted_hits:
            omitted["hits"] = omitted_hits
        fitted = self._fit({
            "query": query,
            "trial_key": trial_key,
            "hits": hits,
            "searched_records": searched,
            "omitted": omitted,
        })
        self._mark_viewed([
            (item["record_id"], item["excerpt"])
            for item in fitted.get("hits", [])
            if isinstance(item, dict) and isinstance(item.get("excerpt"), str)
        ])
        return fitted

    def related(self) -> dict[str, Any]:
        return self._fit({
            "primary_trial": self._case.primary_trial,
            "related": [self._trial_meta(self._by_key[key]) for key in self._case.related_trials],
            "alerts": [alert.model_dump(mode="json") for alert in self._by_key[self._case.primary_trial].alerts],
        })

    def validate_finding(self, finding: MonitorFinding) -> None:
        problems: list[str] = []
        for label, citation in (
            [("evidence", item) for item in finding.evidence]
            + [("counterevidence", item) for item in finding.counterevidence]
        ):
            record = self._records.get(citation.record_id)
            if record is None or record.trial_key not in set(self._allowed):
                problems.append(f"{label} {citation.record_id}: unknown or out-of-case record")
                continue
            if citation.quote not in record.text:
                problems.append(f"{label} {citation.record_id}: quote is not from the stored record text")
                continue
            spans = self._visible.get(citation.record_id, [])
            if not spans:
                problems.append(f"{label} {citation.record_id}: record never returned by tools")
                continue
            if not any(citation.quote in span for span in spans):
                problems.append(f"{label} {citation.record_id}: quote is not an exact visible substring")
        if problems:
            raise ValueError("; ".join(problems))


__all__ = ["WATCH_SCHEMA", "snapshot_watch", "EvidenceTools"]
