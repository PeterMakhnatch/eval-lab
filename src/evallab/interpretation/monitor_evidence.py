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
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from evallab.explorer import redact_text
from evallab.step_layers import stitch_steps

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
_HEAD_REL = "agent/trajectory.json"
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
        if size > self.trial_remaining or size > self.total_remaining[0]:
            return False
        self.trial_remaining -= size
        self.total_remaining[0] -= size
        return True


def _stat_sig(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _read_frozen(path: Path, rel: str, *, budget: _Budget, whole: bool) -> _Capture | None:
    """Capture immutable raw bytes, or ``None`` when the file must be skipped.

    ``whole=True`` (JSON sources) refuses to truncate: an oversized file is
    skipped so a partial document never parses as complete. ``whole=False``
    (text artifacts) reads a bounded prefix and the caller marks truncation.
    """
    before = _stat_sig(path)
    if before is None:
        return None
    size = before[1]
    if whole:
        if size > budget.trial_remaining or size > budget.total_remaining[0]:
            return None
        want = size
    else:
        want = min(size, _TEXT_ARTIFACT_CAP, budget.trial_remaining, budget.total_remaining[0])
    try:
        with path.open("rb") as handle:
            raw = handle.read(want + 1 if not whole else want)
    except OSError:
        return None
    if not whole and len(raw) > want:
        raw = raw[:want]
    after = _stat_sig(path)
    short = whole and len(raw) != size
    changed = after is None or after != before or short
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
            if resolved.is_dir():
                found.append(resolved)
    unique = sorted(set(found))
    if not unique:
        return None, f"trial not found under explicit roots: {job}/{trial}"
    if len(unique) > 1:
        return None, f"ambiguous trial name across roots, not resolved: {job}/{trial}"
    return unique[0], None


def _coerce_alert(payload: Any, *, job: str, trial: str, task: str) -> MonitorAlert | None:
    if not isinstance(payload, dict):
        return None
    allowed = {"rule", "severity", "scope", "job", "trial", "task",
               "step_ref", "quote", "detail", "target", "trials"}
    cleaned = {key: payload[key] for key in allowed if key in payload}
    cleaned.setdefault("job", job)
    cleaned.setdefault("trial", trial)
    cleaned.setdefault("task", task)
    if cleaned.get("severity") not in ("high", "medium", "low"):
        return None
    try:
        return MonitorAlert(**cleaned)
    except ValidationError:
        return None


def _reward_from_result(result: Any) -> float | None:
    """Reward from frozen ``result.json`` bytes (mirrors probe03's rule).

    ``verifier_result.rewards.reward`` when present and scored; missing or
    ``-1`` means unscored. ``reward.txt`` is deliberately not consulted: it
    is outside the permitted source set.
    """
    if not isinstance(result, dict):
        return None
    verifier = result.get("verifier_result")
    rewards = verifier.get("rewards") if isinstance(verifier, dict) else None
    raw = rewards.get("reward", "MISSING") if isinstance(rewards, dict) else "MISSING"
    if raw == "MISSING" or raw == -1:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


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

    ordered = sorted(
        [row for row in rows if isinstance(row, dict)],
        key=lambda row: (str(row.get("job", "")), str(row.get("trial", ""))),
    )
    limitations: list[str] = []
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
        alert = _coerce_alert(payload, job="fleet", trial="fleet", task="fleet")
        if alert is None:
            limitations.append("fleet alert dropped: invalid shape or severity")
            continue
        fleet_alerts.append(alert)

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
    task_text = str(task) if isinstance(task, str) and task else None

    def shell(reason: str) -> TrialSnapshot:
        return TrialSnapshot(
            trial_key=trial_key,
            job=str(job) if isinstance(job, str) else str(job),
            trial=str(trial) if isinstance(trial, str) else str(trial),
            task=task_text,
            source_path="",
            state="unavailable",
            reward=None,
            complete=False,
            artifacts=(),
            records=(),
            alerts=(),
            limitations=tuple([reason]),
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
    def contained(path: Path) -> bool:
        try:
            resolved = path.resolve()
        except OSError:
            return False
        return (
            resolved == trial_dir or trial_dir in resolved.parents
            or resolved == job_dir or job_dir in resolved.parents
        )
    def capture_file(path: Path, rel: str, *, whole: bool, optional: bool) -> _Capture | None:
        nonlocal complete
        if path.is_symlink() and not contained(path):
            trial_limits.append(f"source escapes trial/job dir, not read: {rel}")
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

    # 1. Trajectory head + continuations, exact approved names only.
    agent_dir = trial_dir / "agent"
    part_names: list[str] = []
    try:
        entries = sorted(agent_dir.iterdir(), key=lambda p: p.name) if agent_dir.is_dir() else []
    except OSError:
        entries = []
    for entry in entries:
        if entry.name == "trajectory.json":
            part_names.append(entry.name)
        elif _CONT_RE.fullmatch(entry.name):
            part_names.append(entry.name)
    part_names.sort(key=lambda n: (n != "trajectory.json", int(_CONT_RE.fullmatch(n).group(1)) if n != "trajectory.json" else 0))
    if len(part_names) > 513:
        trial_limits.append(f"continuation count capped at 512 parts, rest ignored: {len(part_names)} found")
        part_names = part_names[:513]
        complete = False

    docs: list[Any] = []
    part_docs: dict[str, Any] = {}
    origin: dict[int, tuple[str, str, str]] = {}
    if not part_names:
        trial_limits.append("missing source: agent/trajectory.json")
        complete = False
    for name in part_names:
        rel = f"agent/{name}"
        capture = capture_file(agent_dir / name, rel, whole=True, optional=True)
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
        stem = "head" if name == "trajectory.json" else f"cont-{_CONT_RE.fullmatch(name).group(1)}"
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
    captured_rels = set(part_docs)
    for rel, payload in part_docs.items():
        ref = payload.get("continued_trajectory_ref")
        if isinstance(ref, str) and ref:
            target = _normalize_ref(ref)
            if target is None or target not in captured_rels:
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
    else:
        # capture_file already recorded missing/over-budget.
        pass
    if result_capture is None or not result_ok:
        complete = False

    reward = _reward_from_result(result) if result_ok else None
    finished = bool(result_ok and isinstance(result, dict) and result.get("finished_at"))
    state = "finished" if finished else "running"
    if not finished:
        trial_limits.append("live prefix: no finished result, not a completed verdict")
        complete = False

    # 3. Optional spec: instruction.md physically inside trial or job dir.
    spec_capture: _Capture | None = None
    spec_rel: str | None = None
    for base, spec_rel_name in ((trial_dir, "instruction.md"), (trial_dir.parent, "../instruction.md")):
        candidate = base / "instruction.md"
        if not candidate.is_file():
            continue
        try:
            resolved = candidate.resolve()
            scope = trial_dir.resolve()
            job_scope = trial_dir.parent.resolve()
        except OSError:
            continue
        contained = (
            resolved == scope or scope in resolved.parents
            or resolved == job_scope or job_scope in resolved.parents
        )
        if not contained:
            continue
        spec_rel = spec_rel_name
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

    # 4. Bounded verifier artifacts.
    text_captures: dict[str, _Capture] = {}
    for rel in _VERIFIER_RELS:
        capture = capture_file(trial_dir / rel, rel, whole=False, optional=True)
        if capture is not None:
            # Detect prefix-truncation: on-disk size exceeded what budgets allowed.
            try:
                on_disk = (trial_dir / rel).stat().st_size
            except OSError:
                on_disk = capture.size_bytes
            if on_disk > capture.size_bytes:
                trial_limits.append(f"source truncated to {capture.size_bytes} bytes: {rel}")
                complete = False
            text_captures[rel] = capture

    # --- Records ---
    records: list[EvidenceRecord] = []
    step_records = [step for step in stitched if isinstance(step, Mapping)]
    n_steps = len(step_records)
    truncated_count = 0
    redacted_count = 0
    for ordinal, step in enumerate(step_records, start=1):
        info = origin.get(id(step))
        document = info[0] if info else _HEAD_REL
        step_ref = info[2] if info else None
        part_sha = captures[document].sha256 if document in captures else hashlib.sha256(b"").hexdigest()
        raw_text = _render_step_text(step)
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
    if truncated_count:
        trial_limits.append(f"{truncated_count} step records truncated to {max_record_chars} chars")
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

    alerts: list[MonitorAlert] = []
    open_alerts = row.get("open_alerts", [])
    if isinstance(open_alerts, list):
        for payload in open_alerts:
            alert = _coerce_alert(payload, job=str(job), trial=str(trial),
                                  task=task_text or str(trial))
            if alert is None:
                trial_limits.append("watch alert dropped: invalid shape or severity")
                continue
            alerts.append(alert)

    return TrialSnapshot(
        trial_key=trial_key,
        job=str(job),
        trial=str(trial),
        task=task_text,
        source_path=str(trial_dir),
        state=state,  # type: ignore[arg-type]
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
        """Cap serialized output at max_chars by shortening text/excerpt fields."""
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded) <= self._max_chars:
            return payload
        omitted: dict[str, int] = {}
        if isinstance(payload.get("omitted"), dict):
            omitted.update(payload["omitted"])
        candidates: list[tuple[int, dict[str, Any], str]] = []

        def collect(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in ("text", "excerpt") and isinstance(value, str):
                        candidates.append((len(value), node, key))
                    else:
                        collect(value)
            elif isinstance(node, list):
                for item in node:
                    collect(item)

        collect(payload)
        candidates.sort(key=lambda item: item[0], reverse=True)
        for _, node, key in candidates:
            value = node[key]
            assert isinstance(value, str)
            keep = max(64, len(value) // 2)
            if len(value) <= keep:
                continue
            node[key] = value[:keep].rstrip() + "…[output-capped]"
            omitted["chars"] = omitted.get("chars", 0) + (len(value) - len(node[key]))
            encoded = json.dumps(payload, ensure_ascii=False)
            if len(encoded) <= self._max_chars:
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
        if end > len(steps):
            omitted["beyond_end"] = end - len(steps)
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
            spans = self._visible.get(citation.record_id, [])
            if not spans:
                problems.append(f"{label} {citation.record_id}: record never returned by tools")
                continue
            if not any(citation.quote in span for span in spans):
                problems.append(f"{label} {citation.record_id}: quote is not an exact visible substring")
        if problems:
            raise ValueError("; ".join(problems))


__all__ = ["WATCH_SCHEMA", "snapshot_watch", "EvidenceTools"]
