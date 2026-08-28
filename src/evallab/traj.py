"""M030 LOOP-TRAJ: Trajectory analysis, mechanical feature extraction, and human review queue.

Contract for downstream UI (GYM-UI) and AGY capture:
- A trajectory outline is a deterministic typed record with trial/job identity,
  ordered phases, step/tool/error counts, loop-suspicion features, and source citations.
- Persisted mechanical feature rows are queryable through Parquet and DuckDB views.
- Missing trajectory data is an explicit accounted/unavailable state,
  never a fabricated empty success.
- Path traversal outside configured roots is prevented via strict path jail enforcement.
- Review selection and behavior labels are implemented in ``evallab.labels``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
import pyarrow.parquet as pq

from evallab.results import sha256_file
from evallab.storage.paths import derived_root_from_environment, shared_checkout_root
from evallab.trajectory_error_taxonomy import (
    classify_intervention_provenance,
    classify_step_error,
)

PhaseType = Literal["setup", "prompt", "work", "verifier", "unknown"]
AvailabilityStatus = Literal["featured", "accounted_unavailable"]

CONTROL_AGENTS = frozenset({"oracle", "nop"})
EDIT_TOOL_NAMES = frozenset(
    {
        "ast_edit",
        "apply_patch",
        "create_file",
        "edit",
        "edit_file",
        "file_change",
        "patch",
        "sed",
        "write",
        "write_file",
    }
)
EDIT_COMMAND_PATTERNS = re.compile(
    r"\b("
    r"apply_patch|git\s+(?:apply|checkout\s+--)|"
    r"sed\s+-i|echo\s+.*>|cat\s+.*>|tee\s+|touch\s+|truncate\s+|"
    r"python\s+.*(?:write|open\(|write_text)|"
    r"node\s+.*(?:writeFileSync|writeFile)|fs\.writeFileSync"
    r")\b"
)

_REDACTION_PATTERN = re.compile(
    r"<<evallab-redacted: (?P<bytes>\d+) bytes, (?P<digest>sha256:[0-9a-f]{64})>>"
)


class TrajectoryError(Exception):
    """Base error for trajectory operations."""


class PathJailError(TrajectoryError):
    """Raised when a path escapes the allowed root jail."""


class TrajectoryNotFoundError(TrajectoryError):
    """Raised when an expected trajectory cannot be located."""


class TrajectoryParseError(TrajectoryError):
    """Raised when a trajectory file contains invalid or unparseable JSON."""


@dataclass(frozen=True)
class SourceCitation:
    """Provenance citation to an underlying evidence file and step."""

    path: str
    sha256: str
    step_id: int | None = None
    kind: str = "trajectory"


@dataclass(frozen=True)
class LoopSuspicion:
    """Deterministic loop-suspicion heuristic features."""

    score: float
    detected: bool
    reasons: tuple[str, ...]
    repeated_command_count: int
    repeated_error_count: int
    cyclic_patterns_count: int


@dataclass(frozen=True)
class StepOutline:
    """Condensed outline of one trajectory step with CAS reasoning citations."""

    step_id: int
    source: str
    timestamp: str | None
    model_name: str | None
    tool_name: str | None
    tool_command: str | None
    exit_code: int | None
    is_error: bool
    error_message: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    cost_usd: float | None
    thought_snippet: str | None
    is_redacted: bool = False
    redaction_digest: str | None = None
    reasoning_content: str | None = None
    reasoning_content_ref: str | None = None
    reasoning_tokens: int | None = None
    prompt_token_ids_ref: str | None = None
    completion_token_ids_ref: str | None = None
    logprobs_ref: str | None = None
    sample_index: int | None = None
    sampling_params: dict[str, Any] | None = None
    is_expected_probe: bool = False
    error_category: str = "none"


@dataclass(frozen=True)
class PhaseOutline:
    """Ordered semantic execution phase within a trajectory."""

    phase_id: int
    name: str
    phase_type: PhaseType
    step_start: int
    step_end: int
    step_count: int
    tool_calls: int
    errors: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    cost_usd: float
    summary: str


@dataclass(frozen=True)
class TrajectoryOutline:
    """Deterministic typed outline of a full trial trajectory."""

    trial_id: str
    job_id: str
    trial_name: str
    job_name: str
    task_name: str
    agent_name: str
    agent_version: str | None
    model_name: str
    status: AvailabilityStatus
    unavailable_reason: str | None
    source_path: str
    source_sha256: str
    duration_seconds: float | None
    primary_reward: float | None
    exception_class: str | None
    total_steps: int
    agent_steps: int
    system_steps: int
    user_steps: int
    total_tool_calls: int
    total_errors: int
    recovery_count: int
    step_to_first_tool: int | None
    step_to_first_edit: int | None
    time_to_first_tool_seconds: float | None
    time_to_first_edit_seconds: float | None
    total_prompt_tokens: int
    total_completion_tokens: int
    total_cached_tokens: int
    total_cost_usd: float
    loop_suspicion: LoopSuspicion
    phases: tuple[PhaseOutline, ...]
    steps: tuple[StepOutline, ...]
    citations: tuple[SourceCitation, ...]
    tool_mix: dict[str, int] = field(default_factory=dict)
    is_expected_negative: bool = False
    expected_probe_count: int = 0
    step_to_first_error: int | None = None
    time_to_first_error_seconds: float | None = None
    recovery_latency_steps: int | None = None
    recovery_latency_seconds: float | None = None
    unrecovered_at_terminal: bool = False
    intervention_category: str = "autonomous"
    intervention_provenance_notes: str = ""
    autonomous_step_count: int = 0
    assisted_step_count: int = 0
    intervention_count: int = 0
    state_diff_observed: bool = False
    state_journal_status: str = "not_observed"
    state_journal_reason: str | None = None
    state_events_count: int = 0
    state_mutations_count: int = 0
    state_files_created_count: int = 0
    state_files_modified_count: int = 0
    state_files_deleted_count: int = 0
    state_diff_path_count: int = 0
    state_diff_bytes_delta: int = 0
    unobserved_state_mutations_count: int = 0
    path_reference_count: int = 0
    valid_path_reference_count: int = 0
    invalid_path_reference_count: int = 0
    citation_reference_count: int = 0
    valid_citation_reference_count: int = 0
    invalid_citation_reference_count: int = 0
    edit_call_count: int = 0
    state_coverage_extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TrajectoryFeatures:
    """Persisted mechanical features extracted from a trajectory."""

    trial_id: str
    job_id: str
    trial_name: str
    job_name: str
    task_name: str
    agent_name: str
    agent_version: str | None
    model_name: str
    status: str
    unavailable_reason: str | None
    source_path: str
    source_sha256: str
    step_count: int
    agent_step_count: int
    system_step_count: int
    user_step_count: int
    tool_call_count: int
    unique_tools_count: int
    tool_mix_json: str
    error_count: int
    recovery_count: int
    loop_suspicion_score: float
    loop_suspicion_detected: bool
    loop_reasons_json: str
    repeated_command_count: int
    step_to_first_tool: int | None
    step_to_first_edit: int | None
    time_to_first_tool_seconds: float | None
    time_to_first_edit_seconds: float | None
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    cost_usd: float
    primary_reward: float | None
    exception_class: str | None
    duration_seconds: float | None
    created_at: str
    context_burn_velocity_screening: float | None = None
    max_exit_code_cascade_screening: int = 0
    is_expected_negative: bool = False
    expected_probe_count: int = 0
    step_to_first_error: int | None = None
    time_to_first_error_seconds: float | None = None
    recovery_latency_steps: int | None = None
    recovery_latency_seconds: float | None = None
    unrecovered_at_terminal: bool = False
    intervention_category: str = "autonomous"
    autonomous_step_count: int = 0
    assisted_step_count: int = 0
    intervention_count: int = 0
    state_diff_observed: bool = False
    state_journal_status: str = "not_observed"
    state_journal_reason: str | None = None
    state_events_count: int = 0
    state_mutations_count: int = 0
    state_files_created_count: int = 0
    state_files_modified_count: int = 0
    state_files_deleted_count: int = 0
    state_diff_path_count: int = 0
    state_diff_bytes_delta: int = 0
    unobserved_state_mutations_count: int = 0
    path_reference_count: int = 0
    valid_path_reference_count: int = 0
    invalid_path_reference_count: int = 0
    citation_reference_count: int = 0
    valid_citation_reference_count: int = 0
    invalid_citation_reference_count: int = 0
    edit_call_count: int = 0
    edit_efficiency_screening: float | None = None
    path_reference_validity_rate_screening: float | None = None
    citation_reference_validity_rate_screening: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TrajectoryProjectResult:
    """Result of projecting mechanical features to Parquet."""

    total_scanned: int
    featured_count: int
    unavailable_count: int
    output_path: Path
    table_rows: int
    sha256: str


TRAJ_FEATURES_PARQUET_SCHEMA = pa.schema(
    [
        pa.field("trial_id", pa.string(), nullable=False),
        pa.field("job_id", pa.string(), nullable=False),
        pa.field("trial_name", pa.string(), nullable=False),
        pa.field("job_name", pa.string(), nullable=False),
        pa.field("task_name", pa.string(), nullable=False),
        pa.field("agent_name", pa.string(), nullable=False),
        pa.field("agent_version", pa.string(), nullable=True),
        pa.field("model_name", pa.string(), nullable=False),
        pa.field("status", pa.string(), nullable=False),
        pa.field("unavailable_reason", pa.string(), nullable=True),
        pa.field("source_path", pa.string(), nullable=False),
        pa.field("source_sha256", pa.string(), nullable=False),
        pa.field("step_count", pa.int64(), nullable=False),
        pa.field("agent_step_count", pa.int64(), nullable=False),
        pa.field("system_step_count", pa.int64(), nullable=False),
        pa.field("user_step_count", pa.int64(), nullable=False),
        pa.field("tool_call_count", pa.int64(), nullable=False),
        pa.field("unique_tools_count", pa.int64(), nullable=False),
        pa.field("tool_mix_json", pa.string(), nullable=False),
        pa.field("error_count", pa.int64(), nullable=False),
        pa.field("recovery_count", pa.int64(), nullable=False),
        pa.field("loop_suspicion_score", pa.float64(), nullable=False),
        pa.field("loop_suspicion_detected", pa.bool_(), nullable=False),
        pa.field("loop_reasons_json", pa.string(), nullable=False),
        pa.field("repeated_command_count", pa.int64(), nullable=False),
        pa.field("step_to_first_tool", pa.int64(), nullable=True),
        pa.field("step_to_first_edit", pa.int64(), nullable=True),
        pa.field("time_to_first_tool_seconds", pa.float64(), nullable=True),
        pa.field("time_to_first_edit_seconds", pa.float64(), nullable=True),
        pa.field("prompt_tokens", pa.int64(), nullable=False),
        pa.field("completion_tokens", pa.int64(), nullable=False),
        pa.field("cached_tokens", pa.int64(), nullable=False),
        pa.field("cost_usd", pa.float64(), nullable=False),
        pa.field("primary_reward", pa.float64(), nullable=True),
        pa.field("exception_class", pa.string(), nullable=True),
        pa.field("duration_seconds", pa.float64(), nullable=True),
        pa.field("created_at", pa.string(), nullable=False),
        pa.field("context_burn_velocity_screening", pa.float64(), nullable=True),
        pa.field("max_exit_code_cascade_screening", pa.int64(), nullable=False),
        pa.field("is_expected_negative", pa.bool_(), nullable=False),
        pa.field("expected_probe_count", pa.int64(), nullable=False),
        pa.field("step_to_first_error", pa.int64(), nullable=True),
        pa.field("time_to_first_error_seconds", pa.float64(), nullable=True),
        pa.field("recovery_latency_steps", pa.int64(), nullable=True),
        pa.field("recovery_latency_seconds", pa.float64(), nullable=True),
        pa.field("unrecovered_at_terminal", pa.bool_(), nullable=False),
        pa.field("intervention_category", pa.string(), nullable=False),
        pa.field("autonomous_step_count", pa.int64(), nullable=False),
        pa.field("assisted_step_count", pa.int64(), nullable=False),
        pa.field("intervention_count", pa.int64(), nullable=False),
        pa.field("state_diff_observed", pa.bool_(), nullable=False),
        pa.field("state_journal_status", pa.string(), nullable=False),
        pa.field("state_journal_reason", pa.string(), nullable=True),
        pa.field("state_events_count", pa.int64(), nullable=False),
        pa.field("state_mutations_count", pa.int64(), nullable=False),
        pa.field("state_files_created_count", pa.int64(), nullable=False),
        pa.field("state_files_modified_count", pa.int64(), nullable=False),
        pa.field("state_files_deleted_count", pa.int64(), nullable=False),
        pa.field("state_diff_path_count", pa.int64(), nullable=False),
        pa.field("state_diff_bytes_delta", pa.int64(), nullable=False),
        pa.field("unobserved_state_mutations_count", pa.int64(), nullable=False),
        pa.field("path_reference_count", pa.int64(), nullable=False),
        pa.field("valid_path_reference_count", pa.int64(), nullable=False),
        pa.field("invalid_path_reference_count", pa.int64(), nullable=False),
        pa.field("citation_reference_count", pa.int64(), nullable=False),
        pa.field("valid_citation_reference_count", pa.int64(), nullable=False),
        pa.field("invalid_citation_reference_count", pa.int64(), nullable=False),
        pa.field("edit_call_count", pa.int64(), nullable=False),
        pa.field("edit_efficiency_screening", pa.float64(), nullable=True),
        pa.field("path_reference_validity_rate_screening", pa.float64(), nullable=True),
        pa.field("citation_reference_validity_rate_screening", pa.float64(), nullable=True),
    ]
)


def _check_path_jail(path: Path, roots: Sequence[Path]) -> Path:
    """Ensure path resolves cleanly inside at least one allowed root."""
    resolved = path.resolve()
    for root in roots:
        root_resolved = root.resolve()
        if resolved == root_resolved or root_resolved in resolved.parents:
            return resolved
    raise PathJailError(
        f"Path {path} ({resolved}) escapes all allowed jail roots: {[str(r) for r in roots]}"
    )


def _resolve_candidate_roots(repo_root: Path, explicit_runs_root: Path | None = None) -> list[Path]:
    if explicit_runs_root is not None:
        return [explicit_runs_root.resolve()]
    env = os.environ.get("EVALLAB_RUNS_ROOT")
    if env:
        return [Path(env).resolve()]
    primary = shared_checkout_root(repo_root)
    candidates = [
        repo_root / "runs",
        repo_root / "research/evidence/runs",
        repo_root / "evidence/runs",
        primary / "runs",
        primary / "research/evidence/runs",
        primary / "evidence/runs",
    ]
    seen: set[Path] = set()
    roots: list[Path] = []
    for c in candidates:
        rc = c.resolve()
        if rc not in seen and rc.exists():
            seen.add(rc)
            roots.append(c)
    if not roots:
        roots = [repo_root / "runs", primary / "runs"]
    return roots


def _safe_str(val: Any, default: str = "") -> str:
    return str(val) if val is not None else default


def _parse_iso_seconds(t1_str: str | None, t2_str: str | None) -> float | None:
    if not t1_str or not t2_str:
        return None
    try:
        t1 = datetime.fromisoformat(t1_str.replace("Z", "+00:00"))
        t2 = datetime.fromisoformat(t2_str.replace("Z", "+00:00"))
        delta = (t2 - t1).total_seconds()
        return max(0.0, float(delta))
    except Exception:
        return None


def _extract_command_string(call_args: Any) -> str | None:
    if isinstance(call_args, str):
        return call_args.strip()
    if isinstance(call_args, dict):
        for key in ("cmd", "command", "input", "script", "code"):
            if key in call_args and isinstance(call_args[key], str):
                return call_args[key].strip()
        # Fallback to compact JSON string
        try:
            return json.dumps(call_args, sort_keys=True)
        except Exception:
            return None
    return None


def _is_edit_action(tool_name: str | None, command_snippet: str | None) -> bool:
    if tool_name and tool_name.lower() in EDIT_TOOL_NAMES:
        return True
    return bool(command_snippet and EDIT_COMMAND_PATTERNS.search(command_snippet))


def _analyze_loop_suspicion(steps: Sequence[StepOutline]) -> LoopSuspicion:
    """Analyze step sequences for repeated commands, failing cycles, and loops."""
    repeated_commands = 0
    repeated_errors = 0
    cyclic_patterns = 0
    reasons: list[str] = []

    # 1. Consecutive identical tool commands
    consecutive_cmd_count = 1
    last_cmd: str | None = None
    for step in steps:
        cmd = step.tool_command
        if cmd and len(cmd) > 5:
            if cmd == last_cmd:
                consecutive_cmd_count += 1
                if consecutive_cmd_count == 3:
                    repeated_commands += 1
                    reasons.append(f"repeated_consecutive_command: {cmd[:40]!r} (3+ times)")
            else:
                consecutive_cmd_count = 1
                last_cmd = cmd
        else:
            consecutive_cmd_count = 1
            last_cmd = None

    # 2. Repeated failing commands with identical error/exit code
    failed_cmds: Counter[str] = Counter()
    for step in steps:
        if step.is_error and step.tool_command:
            norm = f"{step.tool_name}:{step.tool_command[:60]}:{step.exit_code}"
            failed_cmds[norm] += 1
    for failed_cmd, count in failed_cmds.items():
        if count >= 3:
            repeated_errors += 1
            reasons.append(f"repeated_failing_command: {failed_cmd} ({count} failures)")

    # 3. Alternating tool cycles (e.g. A -> B -> A -> B -> A -> B)
    tool_sequence = [s.tool_name for s in steps if s.tool_name]
    if len(tool_sequence) >= 6:
        for period in (2, 3):
            matches = 0
            for i in range(len(tool_sequence) - period * 2 + 1):
                chunk1 = tool_sequence[i : i + period]
                chunk2 = tool_sequence[i + period : i + period * 2]
                if chunk1 == chunk2 and len(set(chunk1)) > 1:
                    matches += 1
            if matches >= 2:
                cyclic_patterns += 1
                reasons.append(f"cyclic_tool_pattern: period={period} repeated {matches} times")
                break

    # Calculate bounded score [0.0, 1.0]
    score = 0.0
    if repeated_commands > 0:
        score += 0.35 + min(0.35, repeated_commands * 0.15)
    if repeated_errors > 0:
        score += 0.30 + min(0.30, repeated_errors * 0.15)
    if cyclic_patterns > 0:
        score += 0.40

    score = min(1.0, round(score, 4))
    detected = score >= 0.50

    return LoopSuspicion(
        score=score,
        detected=detected,
        reasons=tuple(reasons),
        repeated_command_count=repeated_commands,
        repeated_error_count=repeated_errors,
        cyclic_patterns_count=cyclic_patterns,
    )


def _compute_cbv_slope(steps: Sequence[StepOutline]) -> float | None:
    """Compute regression slope of prompt_tokens over step_ordinal.

    Returns None if fewer than 2 steps have prompt_tokens or if step indices have 0 variance.
    """
    points: list[tuple[int, int]] = []
    for step in steps:
        if step.prompt_tokens is not None:
            points.append((step.step_id, step.prompt_tokens))

    if len(points) < 2:
        return None

    n = len(points)
    sum_x = sum(x for x, _ in points)
    sum_y = sum(y for _, y in points)
    sum_xy = sum(x * y for x, y in points)
    sum_x2 = sum(x * x for x, _ in points)

    denom = (n * sum_x2) - (sum_x * sum_x)
    if denom == 0:
        return None

    slope = ((n * sum_xy) - (sum_x * sum_y)) / denom
    return round(slope, 4)


def _compute_exit_code_cascade(steps: Sequence[StepOutline]) -> int:
    """Compute the maximum streak of consecutive steps with non-zero exit codes."""
    max_streak = 0
    current_streak = 0

    for step in steps:
        is_failing = (step.exit_code is not None and step.exit_code != 0) or step.is_error
        if is_failing:
            current_streak += 1
            if current_streak > max_streak:
                max_streak = current_streak
        else:
            current_streak = 0

    return max_streak


_PATH_TOKEN_PATTERN = re.compile(
    r"(?:[a-zA-Z0-9_\-\.]+/)+[a-zA-Z0-9_\-\.]+|/[a-zA-Z0-9_\-\.]+(?:/[a-zA-Z0-9_\-\.]+)*|\b[a-zA-Z0-9_\-]+\.[a-zA-Z0-9]{1,8}\b"
)


def _extract_state_journal_metrics(
    trial_dir: Path | None,
    steps: Sequence[StepOutline],
    citations: Sequence[SourceCitation] = (),
) -> dict[str, Any]:
    """Extract mechanical state diff and state journal metrics from trial artifacts."""
    not_observed = {
        "state_diff_observed": False,
        "state_journal_status": "not_observed",
        "state_journal_reason": "state-diff.json not present in trial directory",
        "state_events_count": 0,
        "state_mutations_count": 0,
        "state_files_created_count": 0,
        "state_files_modified_count": 0,
        "state_files_deleted_count": 0,
        "state_diff_path_count": 0,
        "state_diff_bytes_delta": 0,
        "unobserved_state_mutations_count": 0,
        "state_coverage_extra": {},
    }
    if trial_dir is None or not trial_dir.is_dir():
        return not_observed

    candidate_diff_paths = [
        trial_dir / "state-diff.json",
        trial_dir / "agent" / "state-diff.json",
        trial_dir / "journal" / "state-diff.json",
        trial_dir / "state_journal" / "state-diff.json",
    ]
    diff_path: Path | None = None
    for p in candidate_diff_paths:
        if p.is_file():
            diff_path = p
            break

    if diff_path is None:
        events_path = trial_dir / "state-events.jsonl"
        if not events_path.is_file():
            events_path = trial_dir / "agent" / "state-events.jsonl"
        if events_path.is_file():
            return {
                **not_observed,
                "state_journal_status": "incomplete_stream",
                "state_journal_reason": "state-events.jsonl present but state-diff.json missing",
            }
        return not_observed

    try:
        raw_diff = json.loads(diff_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            **not_observed,
            "state_diff_observed": True,
            "state_journal_status": "malformed",
            "state_journal_reason": f"unparseable_state_diff: {type(exc).__name__}: {exc}",
        }

    if not isinstance(raw_diff, dict):
        return {
            **not_observed,
            "state_diff_observed": True,
            "state_journal_status": "malformed",
            "state_journal_reason": "state_diff_not_dict",
        }

    status = str(raw_diff.get("status") or "available")
    reason = raw_diff.get("reason")
    event_count = int(raw_diff.get("event_count") or 0)
    changes = raw_diff.get("changes")
    if not isinstance(changes, list):
        changes = []

    created = 0
    modified = 0
    deleted = 0
    bytes_delta = 0
    mutated_paths: set[str] = set()

    for ch in changes:
        if not isinstance(ch, dict):
            continue
        ctype = str(ch.get("change_type") or "").lower()
        path_str = ch.get("path")
        if path_str:
            mutated_paths.add(str(path_str))
        if ctype in ("added", "created"):
            created += 1
        elif ctype == "modified":
            modified += 1
        elif ctype == "deleted":
            deleted += 1

        before = ch.get("before") if isinstance(ch.get("before"), dict) else None
        after = ch.get("after") if isinstance(ch.get("after"), dict) else None
        size_before = int(before.get("size_bytes") or 0) if before else 0
        size_after = int(after.get("size_bytes") or 0) if after else 0
        bytes_delta += size_after - size_before

    step_referenced_paths: set[str] = set()
    for s in steps:
        if s.tool_command:
            for token in _PATH_TOKEN_PATTERN.findall(s.tool_command):
                step_referenced_paths.add(Path(token).name)
                step_referenced_paths.add(token)
    for c in citations:
        step_referenced_paths.add(Path(c.path).name)
        step_referenced_paths.add(c.path)
    unobserved_mutations = 0
    for p in mutated_paths:
        p_name = Path(p).name
        if p not in step_referenced_paths and p_name not in step_referenced_paths:
            unobserved_mutations += 1

    return {
        "state_diff_observed": True,
        "state_journal_status": status,
        "state_journal_reason": str(reason) if reason is not None else None,
        "state_events_count": event_count,
        "state_mutations_count": len(changes),
        "state_files_created_count": created,
        "state_files_modified_count": modified,
        "state_files_deleted_count": deleted,
        "state_diff_path_count": len(mutated_paths),
        "state_diff_bytes_delta": bytes_delta,
        "unobserved_state_mutations_count": unobserved_mutations,
        "state_coverage_extra": {
            "root": raw_diff.get("root"),
            "schema_version": raw_diff.get("schema_version"),
        },
    }


def _extract_reference_and_citation_metrics(
    trial_dir: Path | None,
    steps: Sequence[StepOutline],
    citations: Sequence[SourceCitation],
) -> dict[str, Any]:
    """Extract mechanical path reference and citation validity counts."""
    path_refs: list[str] = []
    for s in steps:
        if s.tool_command:
            for token in _PATH_TOKEN_PATTERN.findall(s.tool_command):
                path_refs.append(token)

    valid_paths = 0
    invalid_paths = 0
    for p in path_refs:
        if "\x00" in p or ".." in p.split("/"):
            invalid_paths += 1
        else:
            valid_paths += 1

    total_paths = len(path_refs)

    valid_cites = 0
    invalid_cites = 0
    for c in citations:
        sha = c.sha256.strip()
        sha_hex = sha[7:] if sha.startswith("sha256:") else sha
        if len(sha_hex) == 64 and all(ch in "0123456789abcdefABCDEF" for ch in sha_hex) and c.path:
            valid_cites += 1
        else:
            invalid_cites += 1

    total_cites = len(citations)
    return {
        "path_reference_count": total_paths,
        "valid_path_reference_count": valid_paths,
        "invalid_path_reference_count": invalid_paths,
        "citation_reference_count": total_cites,
        "valid_citation_reference_count": valid_cites,
        "invalid_citation_reference_count": invalid_cites,
    }


def _build_phases(steps: Sequence[StepOutline]) -> tuple[PhaseOutline, ...]:
    """Group ordered steps into semantic phases."""
    if not steps:
        return ()

    phases: list[PhaseOutline] = []
    current_phase_type: PhaseType | None = None
    current_start = 1
    phase_steps: list[StepOutline] = []

    def flush_phase() -> None:
        nonlocal current_phase_type, current_start, phase_steps
        if not phase_steps or current_phase_type is None:
            return
        p_id = len(phases) + 1
        name_map = {
            "setup": "System Setup",
            "prompt": "User Prompt",
            "work": "Agent Execution",
            "verifier": "Verifier Evaluation",
            "unknown": "Other",
        }
        name = name_map.get(current_phase_type, "Phase")
        t_calls = sum(1 for s in phase_steps if s.tool_name is not None)
        errs = sum(1 for s in phase_steps if s.is_error)
        p_tok = sum(s.prompt_tokens or 0 for s in phase_steps)
        c_tok = sum(s.completion_tokens or 0 for s in phase_steps)
        ca_tok = sum(s.cached_tokens or 0 for s in phase_steps)
        c_usd = sum(s.cost_usd or 0.0 for s in phase_steps)

        summary = f"{len(phase_steps)} step(s), {t_calls} tool(s), {errs} error(s)"
        phases.append(
            PhaseOutline(
                phase_id=p_id,
                name=f"Phase {p_id}: {name}",
                phase_type=current_phase_type,
                step_start=current_start,
                step_end=phase_steps[-1].step_id,
                step_count=len(phase_steps),
                tool_calls=t_calls,
                errors=errs,
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
                cached_tokens=ca_tok,
                cost_usd=round(c_usd, 6),
                summary=summary,
            )
        )
        phase_steps = []

    for step in steps:
        src = step.source.lower()
        step_type: PhaseType
        if src == "system":
            step_type = "setup"
        elif src == "user":
            step_type = "prompt"
        elif src == "agent":
            step_type = "work"
        elif src == "verifier":
            step_type = "verifier"
        else:
            step_type = "unknown"

        if current_phase_type is None:
            current_phase_type = step_type
            current_start = step.step_id
            phase_steps.append(step)
        elif current_phase_type == step_type:
            phase_steps.append(step)
        else:
            flush_phase()
            current_phase_type = step_type
            current_start = step.step_id
            phase_steps.append(step)

    flush_phase()
    return tuple(phases)


def _unavailable_outline(
    *,
    trial_id: str,
    job_id: str,
    trial_name: str,
    job_name: str,
    task_name: str,
    agent_name: str,
    agent_version: Any,
    model_name: str,
    reason: str,
    source_path: str,
    source_sha256: str,
    duration_seconds: float | None,
    primary_reward: float | None,
    exception_class: str | None,
    citations: Sequence[SourceCitation],
) -> TrajectoryOutline:
    return TrajectoryOutline(
        trial_id=trial_id,
        job_id=job_id,
        trial_name=trial_name,
        job_name=job_name,
        task_name=task_name,
        agent_name=agent_name,
        agent_version=str(agent_version) if agent_version else None,
        model_name=model_name,
        status="accounted_unavailable",
        unavailable_reason=reason,
        source_path=source_path,
        source_sha256=source_sha256,
        duration_seconds=duration_seconds,
        primary_reward=primary_reward,
        exception_class=exception_class,
        total_steps=0,
        agent_steps=0,
        system_steps=0,
        user_steps=0,
        total_tool_calls=0,
        total_errors=0,
        recovery_count=0,
        step_to_first_tool=None,
        step_to_first_edit=None,
        time_to_first_tool_seconds=None,
        time_to_first_edit_seconds=None,
        total_prompt_tokens=0,
        total_completion_tokens=0,
        total_cached_tokens=0,
        total_cost_usd=0.0,
        loop_suspicion=LoopSuspicion(0.0, False, (), 0, 0, 0),
        phases=(),
        steps=(),
        citations=tuple(citations),
        tool_mix={},
        is_expected_negative=False,
        expected_probe_count=0,
        step_to_first_error=None,
        time_to_first_error_seconds=None,
        recovery_latency_steps=None,
        recovery_latency_seconds=None,
        unrecovered_at_terminal=False,
        intervention_category="autonomous",
        intervention_provenance_notes="",
        autonomous_step_count=0,
        assisted_step_count=0,
        intervention_count=0,
        state_diff_observed=False,
        state_journal_status="not_observed",
        state_journal_reason=reason,
        state_events_count=0,
        state_mutations_count=0,
        state_files_created_count=0,
        state_files_modified_count=0,
        state_files_deleted_count=0,
        state_diff_path_count=0,
        state_diff_bytes_delta=0,
        unobserved_state_mutations_count=0,
        path_reference_count=0,
        valid_path_reference_count=0,
        invalid_path_reference_count=0,
        citation_reference_count=len(citations),
        valid_citation_reference_count=len(citations),
        invalid_citation_reference_count=0,
        edit_call_count=0,
        state_coverage_extra={},
    )


def resolve_trial_target(
    target: str | Path,
    repo_root: Path | None = None,
    explicit_runs_root: Path | None = None,
) -> tuple[Path, Path | None, Path | None]:
    """Resolve a target identifier or path to (trial_dir, trajectory_path, result_path).

    Enforces path jail against configured roots.
    """
    root = (repo_root or Path.cwd()).resolve()
    candidate_roots = _resolve_candidate_roots(root, explicit_runs_root)

    target_str = str(target).strip()
    is_path_like = (
        "/" in target_str
        or "\\" in target_str
        or target_str.startswith(".")
        or Path(target).is_absolute()
    )

    if is_path_like:
        target_path = Path(target)
        resolved = target_path if target_path.is_absolute() else (root / target_path).resolve()
        _check_path_jail(resolved, [root, *candidate_roots])
        if resolved.is_file():
            if resolved.name == "trajectory.json":
                trial_dir = resolved.parent
                if trial_dir.name == "agent":
                    trial_dir = trial_dir.parent
                res_path = trial_dir / "result.json"
                return trial_dir, resolved, res_path if res_path.is_file() else None
            if resolved.name == "result.json":
                trial_dir = resolved.parent
                traj_cand = trial_dir / "agent" / "trajectory.json"
                if not traj_cand.is_file():
                    traj_cand = trial_dir / "trajectory.json"
                return trial_dir, traj_cand if traj_cand.is_file() else None, resolved
            trial_dir = resolved.parent
            traj_cand = trial_dir / "agent" / "trajectory.json"
            res_cand = trial_dir / "result.json"
            return (
                trial_dir,
                traj_cand if traj_cand.is_file() else None,
                res_cand if res_cand.is_file() else None,
            )
        elif resolved.is_dir():
            traj_cand = resolved / "agent" / "trajectory.json"
            if not traj_cand.is_file():
                traj_cand = resolved / "trajectory.json"
            res_cand = resolved / "result.json"
            return (
                resolved,
                traj_cand if traj_cand.is_file() else None,
                res_cand if res_cand.is_file() else None,
            )
        raise TrajectoryNotFoundError(f"Path target {target!r} ({resolved}) does not exist")

    # 2. Identifier search across runs roots
    for c_root in candidate_roots:
        if not c_root.exists():
            continue
        cand_dir = c_root / target_str
        if cand_dir.is_dir():
            _check_path_jail(cand_dir, [root, *candidate_roots])
            traj_cand = cand_dir / "agent" / "trajectory.json"
            if not traj_cand.is_file():
                traj_cand = cand_dir / "trajectory.json"
            res_cand = cand_dir / "result.json"
            return (
                cand_dir,
                traj_cand if traj_cand.is_file() else None,
                res_cand if res_cand.is_file() else None,
            )
        for job_dir in c_root.iterdir():
            if not job_dir.is_dir():
                continue
            for cand_trial in job_dir.iterdir():
                if not cand_trial.is_dir():
                    continue
                matched = False
                if cand_trial.name == target_str:
                    matched = True
                else:
                    r_file = cand_trial / "result.json"
                    if r_file.is_file():
                        try:
                            r_data = json.loads(r_file.read_text(encoding="utf-8"))
                            if (
                                r_data.get("id") == target_str
                                or r_data.get("trial_name") == target_str
                            ):
                                matched = True
                        except Exception:
                            pass
                if matched:
                    _check_path_jail(cand_trial, [root, *candidate_roots])
                    traj_cand = cand_trial / "agent" / "trajectory.json"
                    if not traj_cand.is_file():
                        traj_cand = cand_trial / "trajectory.json"
                    res_cand = cand_trial / "result.json"
                    return (
                        cand_trial,
                        traj_cand if traj_cand.is_file() else None,
                        res_cand if res_cand.is_file() else None,
                    )

    root_list = [str(r) for r in candidate_roots]
    raise TrajectoryNotFoundError(
        f"Trial target {target!r} not found in candidate runs roots: {root_list}"
    )


def outline_trajectory(
    target: str | Path,
    repo_root: Path | None = None,
    explicit_runs_root: Path | None = None,
    store_root: Path | None = None,
) -> TrajectoryOutline:
    """Build a deterministic typed trajectory outline from trial evidence."""
    root = (repo_root or Path.cwd()).resolve()
    try:
        trial_dir, traj_path, result_path = resolve_trial_target(
            target, repo_root=root, explicit_runs_root=explicit_runs_root
        )
    except PathJailError as exc:
        # Explicit accounted unavailable state for path violations
        return TrajectoryOutline(
            trial_id=str(target),
            job_id="unknown",
            trial_name=str(target),
            job_name="unknown",
            task_name="unknown",
            agent_name="unknown",
            agent_version=None,
            model_name="unknown",
            status="accounted_unavailable",
            unavailable_reason=f"path_escapes_jail: {exc}",
            source_path=str(target),
            source_sha256="",
            duration_seconds=None,
            primary_reward=None,
            exception_class=None,
            total_steps=0,
            agent_steps=0,
            system_steps=0,
            user_steps=0,
            total_tool_calls=0,
            total_errors=0,
            recovery_count=0,
            step_to_first_tool=None,
            step_to_first_edit=None,
            time_to_first_tool_seconds=None,
            time_to_first_edit_seconds=None,
            total_prompt_tokens=0,
            total_completion_tokens=0,
            total_cached_tokens=0,
            total_cost_usd=0.0,
            loop_suspicion=LoopSuspicion(0.0, False, (), 0, 0, 0),
            phases=(),
            steps=(),
            citations=(),
            tool_mix={},
        )

    result_data: dict[str, Any] = {}
    if result_path and result_path.is_file():
        try:
            loaded_result = json.loads(result_path.read_text(encoding="utf-8"))
            if isinstance(loaded_result, dict):
                result_data = loaded_result
        except Exception:
            result_data = {}

    cfg_value = result_data.get("config")
    cfg = cfg_value if isinstance(cfg_value, dict) else {}
    trial_id = _safe_str(result_data.get("id") or trial_dir.name)
    trial_name = _safe_str(result_data.get("trial_name") or trial_dir.name)
    task_name = _safe_str(result_data.get("task_name") or "unknown")
    job_id = _safe_str(cfg.get("job_id") or trial_dir.parent.name)
    job_name = _safe_str(trial_dir.parent.name)

    agent_info_value = result_data.get("agent_info")
    agent_info = agent_info_value if isinstance(agent_info_value, dict) else {}
    agent_cfg_value = cfg.get("agent")
    agent_cfg = agent_cfg_value if isinstance(agent_cfg_value, dict) else {}
    agent_name = _safe_str(agent_info.get("name") or agent_cfg.get("name") or "unknown")
    agent_version = agent_info.get("version") or agent_cfg.get("version")
    model_name = _safe_str(agent_info.get("model_name") or agent_cfg.get("model") or "unknown")
    verifier_value = result_data.get("verifier_result")
    verifier_result = verifier_value if isinstance(verifier_value, dict) else {}
    rewards_value = verifier_result.get("rewards")
    rewards = rewards_value if isinstance(rewards_value, dict) else {}
    primary_reward = None
    if "reward" in rewards and isinstance(rewards["reward"], int | float):
        primary_reward = float(rewards["reward"])

    exception_info = result_data.get("exception_info")
    exception_class = None
    if isinstance(exception_info, dict):
        exception_class = exception_info.get("exception_type") or exception_info.get("type")

    started_at = result_data.get("started_at")
    finished_at = result_data.get("finished_at")
    duration_seconds = _parse_iso_seconds(started_at, finished_at)

    citations: list[SourceCitation] = []
    if result_path and result_path.is_file():
        citations.append(
            SourceCitation(
                path=str(result_path),
                sha256=sha256_file(result_path),
                kind="result",
            )
        )

    # Missing trajectory handling -> Accounted Unavailable
    if traj_path is None or not traj_path.is_file():
        return TrajectoryOutline(
            trial_id=trial_id,
            job_id=job_id,
            trial_name=trial_name,
            job_name=job_name,
            task_name=task_name,
            agent_name=agent_name,
            agent_version=str(agent_version) if agent_version else None,
            model_name=model_name,
            status="accounted_unavailable",
            unavailable_reason="missing_trajectory_file",
            source_path=str(trial_dir / "agent/trajectory.json"),
            source_sha256="",
            duration_seconds=duration_seconds,
            primary_reward=primary_reward,
            exception_class=exception_class,
            total_steps=0,
            agent_steps=0,
            system_steps=0,
            user_steps=0,
            total_tool_calls=0,
            total_errors=0,
            recovery_count=0,
            step_to_first_tool=None,
            step_to_first_edit=None,
            time_to_first_tool_seconds=None,
            time_to_first_edit_seconds=None,
            total_prompt_tokens=0,
            total_completion_tokens=0,
            total_cached_tokens=0,
            total_cost_usd=0.0,
            loop_suspicion=LoopSuspicion(0.0, False, (), 0, 0, 0),
            phases=(),
            steps=(),
            citations=tuple(citations),
            tool_mix={},
        )

    try:
        traj_text = traj_path.read_text(encoding="utf-8")
        traj_data = json.loads(traj_text)
    except Exception as exc:
        return TrajectoryOutline(
            trial_id=trial_id,
            job_id=job_id,
            trial_name=trial_name,
            job_name=job_name,
            task_name=task_name,
            agent_name=agent_name,
            agent_version=str(agent_version) if agent_version else None,
            model_name=model_name,
            status="accounted_unavailable",
            unavailable_reason=f"unparseable_trajectory_json: {type(exc).__name__}: {exc}",
            source_path=str(traj_path),
            source_sha256=sha256_file(traj_path),
            duration_seconds=duration_seconds,
            primary_reward=primary_reward,
            exception_class=exception_class,
            total_steps=0,
            agent_steps=0,
            system_steps=0,
            user_steps=0,
            total_tool_calls=0,
            total_errors=0,
            recovery_count=0,
            step_to_first_tool=None,
            step_to_first_edit=None,
            time_to_first_tool_seconds=None,
            time_to_first_edit_seconds=None,
            total_prompt_tokens=0,
            total_completion_tokens=0,
            total_cached_tokens=0,
            total_cost_usd=0.0,
            loop_suspicion=LoopSuspicion(0.0, False, (), 0, 0, 0),
            phases=(),
            steps=(),
            citations=tuple(citations),
            tool_mix={},
        )

    traj_sha256 = sha256_file(traj_path)
    citations.append(
        SourceCitation(
            path=str(traj_path),
            sha256=traj_sha256,
            kind="trajectory",
        )
    )
    if not isinstance(traj_data, dict):
        return _unavailable_outline(
            trial_id=trial_id,
            job_id=job_id,
            trial_name=trial_name,
            job_name=job_name,
            task_name=task_name,
            agent_name=agent_name,
            agent_version=agent_version,
            model_name=model_name,
            reason="invalid_trajectory_shape: top-level JSON must be an object",
            source_path=str(traj_path),
            source_sha256=traj_sha256,
            duration_seconds=duration_seconds,
            primary_reward=primary_reward,
            exception_class=exception_class,
            citations=citations,
        )

    agent_section_value = traj_data.get("agent")
    agent_section = agent_section_value if isinstance(agent_section_value, dict) else {}
    if not agent_name or agent_name == "unknown":
        agent_name = _safe_str(agent_section.get("name") or "unknown")
    if not model_name or model_name == "unknown":
        model_name = _safe_str(agent_section.get("model_name") or "unknown")
    if agent_version is None:
        agent_version = agent_section.get("version")

    raw_steps_value = traj_data.get("steps")
    if not isinstance(raw_steps_value, list) or not raw_steps_value:
        return _unavailable_outline(
            trial_id=trial_id,
            job_id=job_id,
            trial_name=trial_name,
            job_name=job_name,
            task_name=task_name,
            agent_name=agent_name,
            agent_version=agent_version,
            model_name=model_name,
            reason="missing_trajectory_steps",
            source_path=str(traj_path),
            source_sha256=traj_sha256,
            duration_seconds=duration_seconds,
            primary_reward=primary_reward,
            exception_class=exception_class,
            citations=citations,
        )
    if any(not isinstance(step, dict) for step in raw_steps_value):
        return _unavailable_outline(
            trial_id=trial_id,
            job_id=job_id,
            trial_name=trial_name,
            job_name=job_name,
            task_name=task_name,
            agent_name=agent_name,
            agent_version=agent_version,
            model_name=model_name,
            reason="invalid_trajectory_shape: steps must contain objects",
            source_path=str(traj_path),
            source_sha256=traj_sha256,
            duration_seconds=duration_seconds,
            primary_reward=primary_reward,
            exception_class=exception_class,
            citations=citations,
        )
    raw_steps = raw_steps_value
    steps_out: list[StepOutline] = []
    tool_mix_counter: Counter[str] = Counter()

    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_cached_tokens = 0
    total_cost_usd = 0.0

    step_to_first_tool: int | None = None
    step_to_first_edit: int | None = None
    step_to_first_error: int | None = None
    first_step_timestamp: str | None = None
    first_tool_timestamp: str | None = None
    first_edit_timestamp: str | None = None
    first_error_timestamp: str | None = None
    first_recovery_timestamp: str | None = None
    first_recovery_step: int | None = None

    last_was_error = False
    recovery_count = 0
    total_errors = 0
    expected_probe_count = 0
    for idx, raw_step in enumerate(raw_steps, start=1):
        step_id = int(raw_step.get("step_id") or idx)
        source = _safe_str(raw_step.get("source") or "agent")
        timestamp = raw_step.get("timestamp")
        if first_step_timestamp is None and timestamp:
            first_step_timestamp = str(timestamp)

        m_name = raw_step.get("model_name") or model_name
        msg = str(raw_step.get("message") or "")

        is_redacted = False
        redaction_digest = None
        redact_match = _REDACTION_PATTERN.search(msg)
        if redact_match:
            is_redacted = True
            redaction_digest = redact_match.group("digest")

        tool_calls_value = raw_step.get("tool_calls")
        tool_calls = tool_calls_value if isinstance(tool_calls_value, list) else []
        primary_tool_name = None
        primary_tool_cmd = None
        if tool_calls and isinstance(tool_calls[0], dict):
            first_call = tool_calls[0]
            primary_tool_name = _safe_str(first_call.get("function_name"))
            primary_tool_cmd = _extract_command_string(first_call.get("arguments"))
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                tname = _safe_str(tc.get("function_name"))
                if tname:
                    tool_mix_counter[tname] += 1

        if primary_tool_name and step_to_first_tool is None:
            step_to_first_tool = step_id
            first_tool_timestamp = timestamp

        if _is_edit_action(primary_tool_name, primary_tool_cmd) and step_to_first_edit is None:
            step_to_first_edit = step_id
            first_edit_timestamp = timestamp

        # Determine observation errors / exit codes across observation / observation_results
        exit_code: int | None = None
        is_error = False
        error_msg: str | None = None
        obs = raw_step.get("observation")
        obs_results_value = raw_step.get("observation_results")
        results: list[dict[str, Any]] = []
        if isinstance(obs, dict):
            res_list = obs.get("results")
            if isinstance(res_list, list):
                results.extend(r for r in res_list if isinstance(r, dict))
        if isinstance(obs_results_value, list):
            results.extend(r for r in obs_results_value if isinstance(r, dict))
        for res in results:
            extra_value = res.get("extra")
            extra = extra_value if isinstance(extra_value, dict) else {}
            if "exit_code" in extra and isinstance(extra["exit_code"], int):
                exit_code = extra["exit_code"]
            content = str(res.get("content") or "")
            result_type = str(res.get("type") or "").lower()
            result_status = str(res.get("status") or "").lower()
            if exit_code is not None and exit_code != 0:
                is_error = True
                error_msg = error_msg or f"command exited with code {exit_code}"
            elif result_type in {"error", "tool_error"} or result_status in {
                "error",
                "failed",
            }:
                is_error = True
                error_msg = error_msg or content[:120].strip() or "tool result reported an error"
        # Deterministic error taxonomy and expected negative probe classification
        primary_content = results[0].get("content") if results else error_msg
        error_classification = classify_step_error(
            tool_name=primary_tool_name,
            tool_command=primary_tool_cmd,
            exit_code=exit_code,
            output_content=str(primary_content or error_msg or ""),
            result_type=result_type if results else None,
            result_status=result_status if results else None,
        )
        is_probe = error_classification.is_expected_probe
        if is_probe:
            expected_probe_count += 1
        is_error = error_classification.is_error

        if is_error:
            total_errors += 1
            if step_to_first_error is None:
                step_to_first_error = step_id
                first_error_timestamp = timestamp
            last_was_error = True
        else:
            if last_was_error and (primary_tool_name or source == "agent"):
                recovery_count += 1
                if first_recovery_step is None:
                    first_recovery_step = step_id
                    first_recovery_timestamp = timestamp
                last_was_error = False
        metrics_value = raw_step.get("metrics")
        metrics = metrics_value if isinstance(metrics_value, dict) else {}
        p_tok = metrics.get("prompt_tokens")
        c_tok = metrics.get("completion_tokens")
        ca_tok = metrics.get("cached_tokens")
        c_usd = metrics.get("cost_usd")

        if isinstance(p_tok, int):
            total_prompt_tokens += p_tok
        if isinstance(c_tok, int):
            total_completion_tokens += c_tok
        if isinstance(ca_tok, int):
            total_cached_tokens += ca_tok
        if isinstance(c_usd, int | float):
            total_cost_usd += float(c_usd)

        # Reasoning content & CAS storage
        raw_reasoning = raw_step.get("reasoning_content") or raw_step.get("thought")
        reasoning_content = str(raw_reasoning) if raw_reasoning is not None else None
        reasoning_content_ref = None
        if reasoning_content:
            if store_root is not None:
                from evallab.evidence_store import store_blob

                reasoning_content_ref = store_blob(store_root, reasoning_content)
            else:
                d_hex = hashlib.sha256(reasoning_content.encode("utf-8")).hexdigest()
                reasoning_content_ref = f"cas://sha256/{d_hex}"

        # Reasoning tokens
        from evallab.trajectory_ir import _extract_reasoning_tokens, _extract_sampling_params

        reasoning_tokens = _extract_reasoning_tokens(raw_step, metrics)

        # Sampling params & sample index
        sampling_params_obj = _extract_sampling_params(raw_step, agent_cfg)
        sampling_params_dict = asdict(sampling_params_obj) if sampling_params_obj else None
        sample_index = (
            raw_step.get("sample_index") if isinstance(raw_step.get("sample_index"), int) else None
        )

        # CAS refs for token arrays if present
        p_ids_ref = None
        c_ids_ref = None
        logprobs_ref = None
        if store_root is not None:
            from evallab.evidence_store import store_blob

            if isinstance(metrics.get("prompt_token_ids"), list):
                p_ids_ref = store_blob(
                    store_root, json.dumps(metrics["prompt_token_ids"]).encode("utf-8")
                )
            if isinstance(metrics.get("completion_token_ids"), list):
                c_ids_ref = store_blob(
                    store_root, json.dumps(metrics["completion_token_ids"]).encode("utf-8")
                )
            if isinstance(metrics.get("logprobs"), list):
                logprobs_ref = store_blob(
                    store_root, json.dumps(metrics["logprobs"]).encode("utf-8")
                )

        thought_snippet = None
        if reasoning_content:
            thought_snippet = reasoning_content[:120].replace("\n", " ").strip()
        elif msg and not is_redacted:
            thought_snippet = msg[:120].replace("\n", " ").strip()

        steps_out.append(
            StepOutline(
                step_id=step_id,
                source=source,
                timestamp=timestamp,
                model_name=m_name,
                tool_name=primary_tool_name,
                tool_command=primary_tool_cmd,
                exit_code=exit_code,
                is_error=is_error,
                error_message=error_msg,
                prompt_tokens=p_tok if isinstance(p_tok, int) else None,
                completion_tokens=c_tok if isinstance(c_tok, int) else None,
                cached_tokens=ca_tok if isinstance(ca_tok, int) else None,
                cost_usd=float(c_usd) if isinstance(c_usd, int | float) else None,
                thought_snippet=thought_snippet,
                is_redacted=is_redacted,
                redaction_digest=redaction_digest,
                reasoning_content=reasoning_content,
                reasoning_content_ref=reasoning_content_ref,
                reasoning_tokens=reasoning_tokens,
                prompt_token_ids_ref=p_ids_ref,
                completion_token_ids_ref=c_ids_ref,
                logprobs_ref=logprobs_ref,
                sample_index=sample_index,
                sampling_params=sampling_params_dict,
                is_expected_probe=is_probe,
                error_category=error_classification.category.value,
            )
        )

    # Time & error latency calculations
    time_to_first_tool_sec = _parse_iso_seconds(first_step_timestamp, first_tool_timestamp)
    time_to_first_edit_sec = _parse_iso_seconds(first_step_timestamp, first_edit_timestamp)
    time_to_first_error_sec = _parse_iso_seconds(first_step_timestamp, first_error_timestamp)
    recovery_latency_steps: int | None = None
    recovery_latency_sec: float | None = None
    if first_recovery_step is not None and step_to_first_error is not None:
        recovery_latency_steps = first_recovery_step - step_to_first_error
    if first_recovery_timestamp and first_error_timestamp:
        recovery_latency_sec = _parse_iso_seconds(first_error_timestamp, first_recovery_timestamp)

    unrecovered_at_terminal = bool(last_was_error)

    # Expected negative trial detection (e.g. AgentAbstain control or probe-heavy trials)
    is_expected_neg = "abstain" in task_name.lower() or (
        expected_probe_count > 0 and total_errors == expected_probe_count
    )

    user_step_indices = [
        sidx for sidx, s in enumerate(steps_out, 1) if s.source.lower() in {"user", "human"}
    ]
    user_steps = len(user_step_indices)
    agent_steps = sum(1 for s in steps_out if s.source.lower() in {"agent", "assistant"})
    system_steps = sum(1 for s in steps_out if s.source.lower() == "system")

    intervention_cat, intervention_notes = classify_intervention_provenance(
        user_steps=user_steps,
        agent_steps=agent_steps,
        system_steps=system_steps,
        user_step_indices=user_step_indices,
        first_error_step=step_to_first_error,
    )
    autonomous_steps = agent_steps
    assisted_steps = user_steps
    intervention_cnt = sum(
        1
        for sidx in user_step_indices
        if step_to_first_error is None or sidx >= step_to_first_error
    )
    loop_suspicion = _analyze_loop_suspicion(steps_out)
    phases = _build_phases(steps_out)
    state_metrics = _extract_state_journal_metrics(trial_dir, steps_out, citations)
    ref_metrics = _extract_reference_and_citation_metrics(trial_dir, steps_out, citations)
    edit_call_count = sum(1 for s in steps_out if _is_edit_action(s.tool_name, s.tool_command))

    return TrajectoryOutline(
        trial_id=trial_id,
        job_id=job_id,
        trial_name=trial_name,
        job_name=job_name,
        task_name=task_name,
        agent_name=agent_name,
        agent_version=str(agent_version) if agent_version else None,
        model_name=model_name,
        status="featured",
        unavailable_reason=None,
        source_path=str(traj_path),
        source_sha256=traj_sha256,
        duration_seconds=duration_seconds,
        primary_reward=primary_reward,
        exception_class=exception_class,
        total_steps=len(steps_out),
        agent_steps=agent_steps,
        system_steps=system_steps,
        user_steps=user_steps,
        total_tool_calls=sum(tool_mix_counter.values()),
        total_errors=total_errors,
        recovery_count=recovery_count,
        step_to_first_tool=step_to_first_tool,
        step_to_first_edit=step_to_first_edit,
        time_to_first_tool_seconds=time_to_first_tool_sec,
        time_to_first_edit_seconds=time_to_first_edit_sec,
        total_prompt_tokens=total_prompt_tokens,
        total_completion_tokens=total_completion_tokens,
        total_cached_tokens=total_cached_tokens,
        total_cost_usd=round(total_cost_usd, 6),
        loop_suspicion=loop_suspicion,
        phases=phases,
        steps=tuple(steps_out),
        citations=tuple(citations),
        tool_mix=dict(sorted(tool_mix_counter.items())),
        is_expected_negative=is_expected_neg,
        expected_probe_count=expected_probe_count,
        step_to_first_error=step_to_first_error,
        time_to_first_error_seconds=time_to_first_error_sec,
        recovery_latency_steps=recovery_latency_steps,
        recovery_latency_seconds=recovery_latency_sec,
        unrecovered_at_terminal=unrecovered_at_terminal,
        intervention_category=intervention_cat.value,
        intervention_provenance_notes=intervention_notes,
        autonomous_step_count=autonomous_steps,
        assisted_step_count=assisted_steps,
        intervention_count=intervention_cnt,
        state_diff_observed=state_metrics["state_diff_observed"],
        state_journal_status=state_metrics["state_journal_status"],
        state_journal_reason=state_metrics["state_journal_reason"],
        state_events_count=state_metrics["state_events_count"],
        state_mutations_count=state_metrics["state_mutations_count"],
        state_files_created_count=state_metrics["state_files_created_count"],
        state_files_modified_count=state_metrics["state_files_modified_count"],
        state_files_deleted_count=state_metrics["state_files_deleted_count"],
        state_diff_path_count=state_metrics["state_diff_path_count"],
        state_diff_bytes_delta=state_metrics["state_diff_bytes_delta"],
        unobserved_state_mutations_count=state_metrics["unobserved_state_mutations_count"],
        path_reference_count=ref_metrics["path_reference_count"],
        valid_path_reference_count=ref_metrics["valid_path_reference_count"],
        invalid_path_reference_count=ref_metrics["invalid_path_reference_count"],
        citation_reference_count=ref_metrics["citation_reference_count"],
        valid_citation_reference_count=ref_metrics["valid_citation_reference_count"],
        invalid_citation_reference_count=ref_metrics["invalid_citation_reference_count"],
        edit_call_count=edit_call_count,
        state_coverage_extra=state_metrics["state_coverage_extra"],
    )


def extract_features(outline: TrajectoryOutline) -> TrajectoryFeatures:
    """Extract flat mechanical feature record from a typed outline."""
    return TrajectoryFeatures(
        trial_id=outline.trial_id,
        job_id=outline.job_id,
        trial_name=outline.trial_name,
        job_name=outline.job_name,
        task_name=outline.task_name,
        agent_name=outline.agent_name,
        agent_version=outline.agent_version,
        model_name=outline.model_name,
        status=outline.status,
        unavailable_reason=outline.unavailable_reason,
        source_path=outline.source_path,
        source_sha256=outline.source_sha256,
        step_count=outline.total_steps,
        agent_step_count=outline.agent_steps,
        system_step_count=outline.system_steps,
        user_step_count=outline.user_steps,
        tool_call_count=outline.total_tool_calls,
        unique_tools_count=len(outline.tool_mix),
        tool_mix_json=json.dumps(outline.tool_mix, sort_keys=True),
        error_count=outline.total_errors,
        recovery_count=outline.recovery_count,
        loop_suspicion_score=outline.loop_suspicion.score,
        loop_suspicion_detected=outline.loop_suspicion.detected,
        loop_reasons_json=json.dumps(list(outline.loop_suspicion.reasons)),
        repeated_command_count=outline.loop_suspicion.repeated_command_count,
        step_to_first_tool=outline.step_to_first_tool,
        step_to_first_edit=outline.step_to_first_edit,
        time_to_first_tool_seconds=outline.time_to_first_tool_seconds,
        time_to_first_edit_seconds=outline.time_to_first_edit_seconds,
        prompt_tokens=outline.total_prompt_tokens,
        completion_tokens=outline.total_completion_tokens,
        cached_tokens=outline.total_cached_tokens,
        cost_usd=outline.total_cost_usd,
        primary_reward=outline.primary_reward,
        exception_class=outline.exception_class,
        duration_seconds=outline.duration_seconds,
        created_at=(outline.steps[0].timestamp or "") if outline.steps else "",
        context_burn_velocity_screening=_compute_cbv_slope(outline.steps),
        max_exit_code_cascade_screening=_compute_exit_code_cascade(outline.steps),
        is_expected_negative=outline.is_expected_negative,
        expected_probe_count=outline.expected_probe_count,
        step_to_first_error=outline.step_to_first_error,
        time_to_first_error_seconds=outline.time_to_first_error_seconds,
        recovery_latency_steps=outline.recovery_latency_steps,
        recovery_latency_seconds=outline.recovery_latency_seconds,
        unrecovered_at_terminal=outline.unrecovered_at_terminal,
        intervention_category=outline.intervention_category,
        autonomous_step_count=outline.autonomous_step_count,
        assisted_step_count=outline.assisted_step_count,
        intervention_count=outline.intervention_count,
        state_diff_observed=outline.state_diff_observed,
        state_journal_status=outline.state_journal_status,
        state_journal_reason=outline.state_journal_reason,
        state_events_count=outline.state_events_count,
        state_mutations_count=outline.state_mutations_count,
        state_files_created_count=outline.state_files_created_count,
        state_files_modified_count=outline.state_files_modified_count,
        state_files_deleted_count=outline.state_files_deleted_count,
        state_diff_path_count=outline.state_diff_path_count,
        state_diff_bytes_delta=outline.state_diff_bytes_delta,
        unobserved_state_mutations_count=outline.unobserved_state_mutations_count,
        path_reference_count=outline.path_reference_count,
        valid_path_reference_count=outline.valid_path_reference_count,
        invalid_path_reference_count=outline.invalid_path_reference_count,
        citation_reference_count=outline.citation_reference_count,
        valid_citation_reference_count=outline.valid_citation_reference_count,
        invalid_citation_reference_count=outline.invalid_citation_reference_count,
        edit_call_count=outline.edit_call_count,
        edit_efficiency_screening=(
            round(float(outline.state_mutations_count) / float(outline.edit_call_count), 4)
            if outline.edit_call_count > 0
            else None
        ),
        path_reference_validity_rate_screening=(
            round(
                float(outline.valid_path_reference_count) / float(outline.path_reference_count), 4
            )
            if outline.path_reference_count > 0
            else None
        ),
        citation_reference_validity_rate_screening=(
            round(
                float(outline.valid_citation_reference_count)
                / float(outline.citation_reference_count),
                4,
            )
            if outline.citation_reference_count > 0
            else None
        ),
    )


def render_outline(
    outline: TrajectoryOutline,
    *,
    verbose: bool = False,
    max_steps: int = 15,
) -> str:
    """Render a human-readable, deterministic step and phase outline."""
    lines: list[str] = []
    status_label = outline.status.upper()
    lines.append(f"TRAJECTORY OUTLINE: {outline.trial_name} [{status_label}]")
    lines.append("=" * 80)
    lines.append(f"Trial ID:       {outline.trial_id}")
    lines.append(f"Job ID:         {outline.job_id} ({outline.job_name})")
    lines.append(f"Task:           {outline.task_name}")
    agent_info = f"{outline.agent_name} ({outline.model_name})"
    if outline.agent_version:
        agent_info += f" v{outline.agent_version}"
    lines.append(f"Agent:          {agent_info}")

    reward_str = f"{outline.primary_reward:.2f}" if outline.primary_reward is not None else "None"
    dur_str = f"{outline.duration_seconds:.1f}s" if outline.duration_seconds is not None else "None"
    exc_str = outline.exception_class or "None"
    lines.append(f"Outcome:        Reward={reward_str} | Duration={dur_str} | Exception={exc_str}")
    lines.append(f"Source:         {outline.source_path} ({outline.source_sha256[:16]}...)")

    if outline.status == "accounted_unavailable":
        lines.append("")
        lines.append(f"UNAVAILABLE REASON: {outline.unavailable_reason}")
        return "\n".join(lines)

    lines.append("")
    lines.append("METRICS SUMMARY:")
    lines.append(
        f"  Steps:        {outline.total_steps} (agent: {outline.agent_steps}, "
        f"system: {outline.system_steps}, user: {outline.user_steps})"
    )
    mix_desc = ", ".join(f"{k}:{v}" for k, v in sorted(outline.tool_mix.items())) or "none"
    mix_str = f"mix: [{mix_desc}]"
    lines.append(
        f"  Tools:        {outline.total_tool_calls} (unique: {len(outline.tool_mix)}, {mix_str})"
    )
    lines.append(f"  Errors:       {outline.total_errors} (recoveries: {outline.recovery_count})")
    loop_str = f"{outline.loop_suspicion.score:.2f} (detected={outline.loop_suspicion.detected})"
    if outline.loop_suspicion.reasons:
        loop_str += f" -> {', '.join(outline.loop_suspicion.reasons)}"
    lines.append(f"  Loop Suspicion: {loop_str}")
    lines.append(
        f"  Tokens:       prompt={outline.total_prompt_tokens:,}, "
        f"completion={outline.total_completion_tokens:,}, "
        f"cached={outline.total_cached_tokens:,} (${outline.total_cost_usd:.4f})"
    )

    t_tool = f"step {outline.step_to_first_tool}" if outline.step_to_first_tool else "none"
    if outline.time_to_first_tool_seconds is not None:
        t_tool += f" ({outline.time_to_first_tool_seconds:.1f}s)"
    t_edit = f"step {outline.step_to_first_edit}" if outline.step_to_first_edit else "none"
    if outline.time_to_first_edit_seconds is not None:
        t_edit += f" ({outline.time_to_first_edit_seconds:.1f}s)"
    lines.append(f"  First Action: tool={t_tool}, edit={t_edit}")

    lines.append("")
    lines.append("ORDERED PHASES:")
    for phase in outline.phases:
        tok_sum = phase.prompt_tokens + phase.completion_tokens
        lines.append(
            f"  [{phase.name}] steps {phase.step_start}–{phase.step_end} "
            f"({phase.summary}, {tok_sum:,} tokens, ${phase.cost_usd:.4f})"
        )

    lines.append("")
    lines.append("STEP HIGHLIGHTS:")
    rendered_steps = outline.steps if verbose else outline.steps[:max_steps]
    for step in rendered_steps:
        tag = f"[{step.source}]"
        detail = ""
        if step.is_redacted:
            detail = f"(redacted prompt text, {(step.redaction_digest or '')[:16]}...)"
        elif step.tool_name:
            cmd_snippet = (step.tool_command or "")[:60].replace("\n", " ")
            status_txt = "error" if step.is_error else "ok"
            if step.exit_code is not None:
                status_txt += f" exit={step.exit_code}"
            detail = f"{step.tool_name}: {cmd_snippet} -> {status_txt}"
        elif step.thought_snippet:
            detail = f'"{step.thought_snippet}"'
        else:
            detail = "message"
        lines.append(f"  Step {step.step_id:3d} {tag:<8s} {detail}")

    if not verbose and len(outline.steps) > max_steps:
        lines.append(
            f"  ... ({len(outline.steps) - max_steps} more steps, use --verbose to view all)"
        )

    return "\n".join(lines)


def project_trajectory_features(
    runs_roots: Sequence[Path] | None = None,
    output_root: Path | None = None,
    repo_root: Path | None = None,
) -> TrajectoryProjectResult:
    """Scan candidate runs, extract mechanical features, and atomically write Parquet."""
    root = (repo_root or Path.cwd()).resolve()
    candidate_roots = list(runs_roots) if runs_roots else _resolve_candidate_roots(root)
    droot = output_root or (derived_root_from_environment(root) / "traj_features")

    discovered_trial_dirs: set[Path] = set()
    for c_root in candidate_roots:
        if not c_root.exists():
            continue
        for job_dir in c_root.iterdir():
            if not job_dir.is_dir() or job_dir.name.startswith("."):
                continue
            for trial_dir in job_dir.iterdir():
                if trial_dir.is_dir() and not trial_dir.name.startswith("."):
                    discovered_trial_dirs.add(trial_dir)

    features_list: list[TrajectoryFeatures] = []
    featured_count = 0
    unavailable_count = 0

    for t_dir in sorted(discovered_trial_dirs, key=lambda p: str(p)):
        try:
            outline = outline_trajectory(
                t_dir, repo_root=root, explicit_runs_root=t_dir.parent.parent
            )
            feat = extract_features(outline)
            features_list.append(feat)
            if feat.status == "featured":
                featured_count += 1
            else:
                unavailable_count += 1
        except Exception as exc:
            # Explicit missing/failed accounting
            feat = TrajectoryFeatures(
                trial_id=t_dir.name,
                job_id=t_dir.parent.name,
                trial_name=t_dir.name,
                job_name=t_dir.parent.name,
                task_name="unknown",
                agent_name="unknown",
                agent_version=None,
                model_name="unknown",
                status="accounted_unavailable",
                unavailable_reason=f"extraction_exception: {type(exc).__name__}: {exc}",
                source_path=str(t_dir),
                source_sha256="",
                step_count=0,
                agent_step_count=0,
                system_step_count=0,
                user_step_count=0,
                tool_call_count=0,
                unique_tools_count=0,
                tool_mix_json="{}",
                error_count=0,
                recovery_count=0,
                loop_suspicion_score=0.0,
                loop_suspicion_detected=False,
                loop_reasons_json="[]",
                repeated_command_count=0,
                step_to_first_tool=None,
                step_to_first_edit=None,
                time_to_first_tool_seconds=None,
                time_to_first_edit_seconds=None,
                prompt_tokens=0,
                completion_tokens=0,
                cached_tokens=0,
                cost_usd=0.0,
                primary_reward=None,
                exception_class=None,
                duration_seconds=None,
                created_at="",
                is_expected_negative=False,
                expected_probe_count=0,
                step_to_first_error=None,
                time_to_first_error_seconds=None,
                recovery_latency_steps=None,
                recovery_latency_seconds=None,
                unrecovered_at_terminal=False,
                intervention_category="autonomous",
                autonomous_step_count=0,
                assisted_step_count=0,
                intervention_count=0,
                state_diff_observed=False,
                state_journal_status="not_observed",
                state_journal_reason=None,
                state_events_count=0,
                state_mutations_count=0,
                state_files_created_count=0,
                state_files_modified_count=0,
                state_files_deleted_count=0,
                state_diff_path_count=0,
                state_diff_bytes_delta=0,
                unobserved_state_mutations_count=0,
                path_reference_count=0,
                valid_path_reference_count=0,
                invalid_path_reference_count=0,
                citation_reference_count=0,
                valid_citation_reference_count=0,
                invalid_citation_reference_count=0,
                edit_call_count=0,
                edit_efficiency_screening=None,
                path_reference_validity_rate_screening=None,
                citation_reference_validity_rate_screening=None,
            )
            features_list.append(feat)
            unavailable_count += 1

    droot.mkdir(parents=True, exist_ok=True)
    out_file = droot / "traj_features.parquet"

    rows = [f.to_dict() for f in features_list]
    table = pa.Table.from_pylist(rows, schema=TRAJ_FEATURES_PARQUET_SCHEMA)

    tmp_file = out_file.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp_file, compression="zstd")
    tmp_file.replace(out_file)

    out_sha = sha256_file(out_file)

    return TrajectoryProjectResult(
        total_scanned=len(discovered_trial_dirs),
        featured_count=featured_count,
        unavailable_count=unavailable_count,
        output_path=out_file,
        table_rows=len(rows),
        sha256=out_sha,
    )
