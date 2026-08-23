from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import ValidationError

from evallab.eventlog import read_event_log_lines
from evallab.results import JobRecord, TrialRecord, sha256_file

JsonObject = dict[str, Any]
ValidationStatus = Literal["valid", "invalid", "unsupported"]
SUPPORTED_SCHEMA_VERSIONS = {f"ATIF-v1.{minor}" for minor in range(8)}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _digest_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _digest_json(value: Any) -> str:
    return _digest_bytes(_canonical_bytes(value))


def _content_bytes(value: Any) -> bytes:
    if isinstance(value, str):
        return value.encode()
    return _canonical_bytes(value)


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_float(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


@dataclass(frozen=True)
class TrajectoryFact:
    job_id: str
    trial_id: str
    document_id: str
    source_path: str
    source_sha256: str
    embedded_path: str | None
    schema_version: str | None
    session_id: str | None
    trajectory_id: str | None
    validation_status: ValidationStatus
    validator: str
    validation_error: str | None
    agent_name: str | None
    agent_version: str | None
    model_name: str | None
    continued_trajectory_ref: str | None
    step_count: int
    llm_call_count: int
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    cost_usd: float | None


@dataclass(frozen=True)
class StepFact:
    job_id: str
    trial_id: str
    document_id: str
    source_path: str
    source_sha256: str
    step_id: int
    source: str
    timestamp: str | None
    model_name: str | None
    is_copied_context: bool
    llm_call_count: int
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    cost_usd: float | None
    tool_call_count: int
    observation_count: int


@dataclass(frozen=True)
class ToolCallFact:
    job_id: str
    trial_id: str
    document_id: str
    source_path: str
    source_sha256: str
    step_id: int
    tool_call_id: str
    function_name: str
    arguments_sha256: str


@dataclass(frozen=True)
class ObservationFact:
    job_id: str
    trial_id: str
    document_id: str
    source_path: str
    source_sha256: str
    step_id: int
    observation_index: int
    source_call_id: str | None
    content_size_bytes: int
    content_sha256: str
    subagent_ref_count: int
    subagent_refs_sha256: str | None
    command_exit_code: int | None


@dataclass(frozen=True)
class TrialTrajectoryProjection:
    trajectories: tuple[TrajectoryFact, ...]
    steps: tuple[StepFact, ...]
    tool_calls: tuple[ToolCallFact, ...]
    observations: tuple[ObservationFact, ...]


@dataclass(frozen=True)
class ExportedTable:
    table: str
    path: Path
    rows: int
    sha256: str


@dataclass(frozen=True)
class ExportResult:
    root: Path
    tables: tuple[ExportedTable, ...]

    @property
    def row_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for table in self.tables:
            counts[table.table] = counts.get(table.table, 0) + table.rows
        return counts


PROJECTION_FAILURE_REASON = "projection_failed"
JOB_PROJECTION_FILE = "jobs.parquet"
PROJECTED_TABLES = frozenset(
    {
        "trajectories.parquet",
        "steps.parquet",
        "tool_calls.parquet",
        "observations.parquet",
        "trial_facts.parquet",
        "reward_facts.parquet",
        "artifact_facts.parquet",
        "tool_usage.parquet",
        "state_changes.parquet",
        "trajectory_events.parquet",
        "agent_actions.parquet",
        "llm_calls.parquet",
        "trajectory_phases.parquet",
        "action_effects.parquet",
    }
)


@dataclass(frozen=True)
class ProjectionFailure:
    job_id: str
    job_name: str
    error_type: str
    message: str

    @property
    def reason_code(self) -> str:
        return f"{PROJECTION_FAILURE_REASON}:{self.job_id}:{self.error_type}"


@dataclass(frozen=True)
class IngestProjectionResult:
    cataloged_jobs: int
    tables: tuple[ExportedTable, ...]
    failures: tuple[ProjectionFailure, ...]

    @property
    def row_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for table in self.tables:
            counts[table.table] = counts.get(table.table, 0) + table.rows
        return counts


@dataclass(frozen=True)
class ProjectionInvariant:
    catalog_job_ids: frozenset[str]
    projected_job_ids: frozenset[str]
    excepted_job_ids: frozenset[str]
    missing_job_ids: frozenset[str]
    extra_job_ids: frozenset[str]
    exceptions_by_reason: dict[str, frozenset[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.missing_job_ids and not self.extra_job_ids

    @property
    def detail(self) -> str:
        base = (
            f"catalog={len(self.catalog_job_ids)} projected={len(self.projected_job_ids)} "
            f"exceptions={len(self.excepted_job_ids)}"
        )
        if self.exceptions_by_reason:
            breakdown = (
                " ("
                + ", ".join(
                    f"{reason}={len(job_ids)}"
                    for reason, job_ids in sorted(self.exceptions_by_reason.items())
                )
                + ")"
            )
            base += breakdown
        return f"{base} missing={len(self.missing_job_ids)} extra={len(self.extra_job_ids)}"


CatalogRowsLoader = Callable[[str], list[tuple[str, str, str | None]]]


def _load_json(path: Path) -> tuple[Any | None, str | None]:
    try:
        return json.loads(path.read_text()), None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _validate_with_harbor(payload: JsonObject) -> tuple[str, str | None] | None:
    """Use Harbor's installed model when it is importable in this interpreter."""
    try:
        from harbor.models.trajectories import Trajectory  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        Trajectory.model_validate(payload)
    except ValidationError as exc:
        sanitized = [
            {
                "type": item.get("type"),
                "loc": list(item.get("loc") or ()),
                "msg": item.get("msg"),
            }
            for item in exc.errors(include_input=False, include_url=False)
        ]
        return "harbor", json.dumps(sanitized, separators=(",", ":"), sort_keys=True)
    except Exception as exc:  # Harbor owns non-Pydantic validator failures.
        return "harbor", f"{type(exc).__name__}: validation failed"
    return "harbor", None


def _validate_fallback(payload: JsonObject) -> str | None:
    schema_version = payload.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        return f"unsupported schema_version {schema_version!r}"
    agent = payload.get("agent")
    if not isinstance(agent, dict) or not isinstance(agent.get("name"), str):
        return "agent.name must be a string"
    if not isinstance(agent.get("version"), str):
        return "agent.version must be a string"
    steps = payload.get("steps")
    if not isinstance(steps, list) or not steps:
        return "steps must be a non-empty array"
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            return f"steps[{index - 1}] must be an object"
        if step.get("step_id") != index:
            return f"steps[{index - 1}].step_id must equal {index}"
        if step.get("source") not in {"system", "user", "agent"}:
            return f"steps[{index - 1}].source is invalid"
        if "message" not in step or not isinstance(step["message"], str | list):
            return f"steps[{index - 1}].message must be text or content parts"
        calls = step.get("tool_calls") or []
        if not isinstance(calls, list):
            return f"steps[{index - 1}].tool_calls must be an array"
        call_ids: set[str] = set()
        for call_index, call in enumerate(calls):
            if not isinstance(call, dict):
                return f"steps[{index - 1}].tool_calls[{call_index}] must be an object"
            call_id = call.get("tool_call_id")
            if not isinstance(call_id, str) or call_id in call_ids:
                return f"steps[{index - 1}] has invalid or duplicate tool_call_id"
            if not isinstance(call.get("function_name"), str):
                return f"steps[{index - 1}] has a tool call without function_name"
            if not isinstance(call.get("arguments"), dict):
                return f"steps[{index - 1}] has non-object tool arguments"
            call_ids.add(call_id)
        observation = step.get("observation")
        if observation is not None:
            results = observation.get("results") if isinstance(observation, dict) else None
            if not isinstance(results, list):
                return f"steps[{index - 1}].observation.results must be an array"
            for result in results:
                if not isinstance(result, dict):
                    return f"steps[{index - 1}] has a non-object observation result"
                source_call_id = result.get("source_call_id")
                if source_call_id is not None and source_call_id not in call_ids:
                    return f"step {index} observation references unknown tool call"
    embedded = payload.get("subagent_trajectories") or []
    if not isinstance(embedded, list):
        return "subagent_trajectories must be an array"
    embedded_ids: set[str] = set()
    for index, child in enumerate(embedded):
        if not isinstance(child, dict):
            return f"subagent_trajectories[{index}] must be an object"
        trajectory_id = child.get("trajectory_id")
        if not isinstance(trajectory_id, str) or trajectory_id in embedded_ids:
            return "embedded subagents require unique trajectory_id values"
        embedded_ids.add(trajectory_id)
        child_error = _validate_fallback(child)
        if child_error:
            return f"subagent_trajectories[{index}]: {child_error}"
    return None


def _referenced_paths(payload: JsonObject) -> list[str]:
    references: list[str] = []
    continued = payload.get("continued_trajectory_ref")
    if isinstance(continued, str):
        references.append(continued)
    for step in payload.get("steps") or []:
        if not isinstance(step, dict):
            continue
        observation = step.get("observation") or {}
        for result in observation.get("results") or []:
            if not isinstance(result, dict):
                continue
            for reference in result.get("subagent_trajectory_ref") or []:
                if isinstance(reference, dict) and isinstance(
                    reference.get("trajectory_path"), str
                ):
                    references.append(reference["trajectory_path"])
    return references


def _embedded_reference_error(payload: JsonObject) -> str | None:
    available = {
        child.get("trajectory_id")
        for child in payload.get("subagent_trajectories") or []
        if isinstance(child, dict)
    }
    for step in payload.get("steps") or []:
        if not isinstance(step, dict):
            continue
        observation = step.get("observation") or {}
        for result in observation.get("results") or []:
            if not isinstance(result, dict):
                continue
            for reference in result.get("subagent_trajectory_ref") or []:
                if not isinstance(reference, dict):
                    continue
                trajectory_id = reference.get("trajectory_id")
                trajectory_path = reference.get("trajectory_path")
                if trajectory_id is None and trajectory_path is None:
                    return "subagent reference has no trajectory_id or trajectory_path"
                if (
                    trajectory_id is not None
                    and trajectory_path is None
                    and trajectory_id not in available
                ):
                    return f"embedded subagent reference {trajectory_id!r} is unresolved"
    return None


def _resolve_reference(source_file: Path, trial_dir: Path, reference: str) -> Path | None:
    candidate = Path(reference)
    if candidate.is_absolute():
        candidate = trial_dir / candidate.as_posix().lstrip("/")
    else:
        candidate = source_file.parent / candidate
    resolved = candidate.resolve()
    trial_root = trial_dir.resolve()
    if resolved != trial_root and trial_root not in resolved.parents:
        return None
    return resolved


def _initial_candidates(trial: TrialRecord) -> list[Path]:
    agent_dir = trial.path / "agent"
    if not agent_dir.is_dir():
        return []
    candidates: list[Path] = []
    for path in sorted(agent_dir.rglob("*.json")):
        payload, error = _load_json(path)
        if path.name.startswith("trajectory") or (
            error is None
            and isinstance(payload, dict)
            and str(payload.get("schema_version", "")).startswith("ATIF-")
        ):
            candidates.append(path)
    canonical = agent_dir / "trajectory.json"
    return sorted(candidates, key=lambda path: (path != canonical, path.as_posix()))


def _flatten_payloads(
    payload: JsonObject,
    *,
    embedded_path: str | None = None,
) -> list[tuple[JsonObject, str | None]]:
    flattened = [(payload, embedded_path)]
    for index, child in enumerate(payload.get("subagent_trajectories") or []):
        if not isinstance(child, dict):
            continue
        identifier = child.get("trajectory_id") or str(index)
        child_path = f"{embedded_path + '/' if embedded_path else ''}subagent:{identifier}"
        flattened.extend(_flatten_payloads(child, embedded_path=child_path))
    return flattened


def _document_validation(
    payload: JsonObject,
    source_file: Path,
    trial_dir: Path,
) -> tuple[ValidationStatus, str, str | None]:
    schema_version = payload.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        return "unsupported", "internal-atif-v1", f"unsupported schema_version {schema_version!r}"
    harbor_validation = _validate_with_harbor(payload)
    if harbor_validation is None:
        validator = "internal-atif-v1"
        error = _validate_fallback(payload)
    else:
        validator, error = harbor_validation
    if error is None:
        error = _embedded_reference_error(payload)
    if error is None:
        for reference in _referenced_paths(payload):
            resolved = _resolve_reference(source_file, trial_dir, reference)
            if resolved is None:
                error = f"trajectory reference escapes trial directory: {reference!r}"
                break
            if not resolved.is_file():
                error = f"trajectory reference is missing: {reference!r}"
                break
    return ("invalid" if error else "valid"), validator, error


def _command_exit_code(result: JsonObject) -> int | None:
    extra = result.get("extra")
    if not isinstance(extra, dict):
        return None
    for key in ("exit_code", "returncode", "return_code"):
        value = _optional_int(extra.get(key))
        if value is not None:
            return value
    return None


def _project_payload(
    job: JobRecord,
    trial: TrialRecord,
    source_file: Path,
    source_sha256: str,
    payload: JsonObject,
    embedded_path: str | None,
    parse_error: str | None = None,
) -> TrialTrajectoryProjection:
    source_path = source_file.relative_to(trial.path).as_posix()
    document_id = _stable_id(trial.id, source_path, embedded_path or "root")
    if parse_error is not None:
        status: ValidationStatus = "invalid"
        validator = "json"
        validation_error = parse_error
    else:
        status, validator, validation_error = _document_validation(payload, source_file, trial.path)

    raw_steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
    step_facts: list[StepFact] = []
    tool_facts: list[ToolCallFact] = []
    observation_facts: list[ObservationFact] = []
    for raw_step in raw_steps:
        if not isinstance(raw_step, dict) or _optional_int(raw_step.get("step_id")) is None:
            continue
        step_id = int(raw_step["step_id"])
        metrics = raw_step.get("metrics") if isinstance(raw_step.get("metrics"), dict) else {}
        calls = raw_step.get("tool_calls") if isinstance(raw_step.get("tool_calls"), list) else []
        observation = (
            raw_step.get("observation") if isinstance(raw_step.get("observation"), dict) else {}
        )
        results = observation.get("results") if isinstance(observation.get("results"), list) else []
        step_facts.append(
            StepFact(
                job_id=job.id,
                trial_id=trial.id,
                document_id=document_id,
                source_path=source_path,
                source_sha256=source_sha256,
                step_id=step_id,
                source=str(raw_step.get("source", "")),
                timestamp=(
                    str(raw_step["timestamp"]) if raw_step.get("timestamp") is not None else None
                ),
                model_name=(
                    str(raw_step["model_name"]) if raw_step.get("model_name") is not None else None
                ),
                is_copied_context=bool(raw_step.get("is_copied_context", False)),
                llm_call_count=_optional_int(raw_step.get("llm_call_count")) or 0,
                prompt_tokens=_optional_int(metrics.get("prompt_tokens")),
                completion_tokens=_optional_int(metrics.get("completion_tokens")),
                cached_tokens=_optional_int(metrics.get("cached_tokens")),
                cost_usd=_optional_float(metrics.get("cost_usd")),
                tool_call_count=len(calls),
                observation_count=len(results),
            )
        )
        for call in calls:
            if not isinstance(call, dict):
                continue
            call_id = call.get("tool_call_id")
            function_name = call.get("function_name")
            arguments = call.get("arguments")
            if not isinstance(call_id, str) or not isinstance(function_name, str):
                continue
            tool_facts.append(
                ToolCallFact(
                    job_id=job.id,
                    trial_id=trial.id,
                    document_id=document_id,
                    source_path=source_path,
                    source_sha256=source_sha256,
                    step_id=step_id,
                    tool_call_id=call_id,
                    function_name=function_name,
                    arguments_sha256=_digest_json(arguments),
                )
            )
        for observation_index, result in enumerate(results):
            if not isinstance(result, dict):
                continue
            content = _content_bytes(result.get("content"))
            references = result.get("subagent_trajectory_ref") or []
            observation_facts.append(
                ObservationFact(
                    job_id=job.id,
                    trial_id=trial.id,
                    document_id=document_id,
                    source_path=source_path,
                    source_sha256=source_sha256,
                    step_id=step_id,
                    observation_index=observation_index,
                    source_call_id=(
                        str(result["source_call_id"])
                        if result.get("source_call_id") is not None
                        else None
                    ),
                    content_size_bytes=len(content),
                    content_sha256=_digest_bytes(content),
                    subagent_ref_count=len(references) if isinstance(references, list) else 0,
                    subagent_refs_sha256=(
                        _digest_json(references)
                        if isinstance(references, list) and references
                        else None
                    ),
                    command_exit_code=_command_exit_code(result),
                )
            )

    agent = payload.get("agent") if isinstance(payload.get("agent"), dict) else {}
    final_metrics = (
        payload.get("final_metrics") if isinstance(payload.get("final_metrics"), dict) else {}
    )
    trajectory = TrajectoryFact(
        job_id=job.id,
        trial_id=trial.id,
        document_id=document_id,
        source_path=source_path,
        source_sha256=source_sha256,
        embedded_path=embedded_path,
        schema_version=(
            str(payload["schema_version"]) if payload.get("schema_version") is not None else None
        ),
        session_id=str(payload["session_id"]) if payload.get("session_id") is not None else None,
        trajectory_id=(
            str(payload["trajectory_id"]) if payload.get("trajectory_id") is not None else None
        ),
        validation_status=status,
        validator=validator,
        validation_error=validation_error,
        agent_name=str(agent["name"]) if agent.get("name") is not None else None,
        agent_version=str(agent["version"]) if agent.get("version") is not None else None,
        model_name=str(agent["model_name"]) if agent.get("model_name") is not None else None,
        continued_trajectory_ref=(
            str(payload["continued_trajectory_ref"])
            if payload.get("continued_trajectory_ref") is not None
            else None
        ),
        step_count=len(step_facts),
        llm_call_count=sum(step.llm_call_count for step in step_facts),
        prompt_tokens=_optional_int(final_metrics.get("total_prompt_tokens")),
        completion_tokens=_optional_int(final_metrics.get("total_completion_tokens")),
        cached_tokens=_optional_int(final_metrics.get("total_cached_tokens")),
        cost_usd=_optional_float(final_metrics.get("total_cost_usd")),
    )
    return TrialTrajectoryProjection(
        trajectories=(trajectory,),
        steps=tuple(step_facts),
        tool_calls=tuple(tool_facts),
        observations=tuple(observation_facts),
    )


def project_trial(job: JobRecord, trial: TrialRecord) -> TrialTrajectoryProjection:
    queue = deque(_initial_candidates(trial))
    visited: set[Path] = set()
    projections: list[TrialTrajectoryProjection] = []
    while queue:
        source_file = queue.popleft().resolve()
        if source_file in visited:
            continue
        visited.add(source_file)
        payload, parse_error = _load_json(source_file)
        source_sha256 = f"sha256:{sha256_file(source_file)}" if source_file.is_file() else ""
        if parse_error is not None or not isinstance(payload, dict):
            projections.append(
                _project_payload(
                    job,
                    trial,
                    source_file,
                    source_sha256,
                    {},
                    None,
                    parse_error or "trajectory root must be an object",
                )
            )
            continue
        for document_payload, embedded_path in _flatten_payloads(payload):
            projections.append(
                _project_payload(
                    job,
                    trial,
                    source_file,
                    source_sha256,
                    document_payload,
                    embedded_path,
                )
            )
        for reference in _referenced_paths(payload):
            resolved = _resolve_reference(source_file, trial.path, reference)
            if resolved is not None and resolved.is_file():
                queue.append(resolved)

    return TrialTrajectoryProjection(
        trajectories=tuple(fact for projection in projections for fact in projection.trajectories),
        steps=tuple(fact for projection in projections for fact in projection.steps),
        tool_calls=tuple(fact for projection in projections for fact in projection.tool_calls),
        observations=tuple(fact for projection in projections for fact in projection.observations),
    )


PARQUET_SCHEMAS = {
    "jobs": pa.schema(
        [
            pa.field("job_id", pa.string(), nullable=False),
            pa.field("job_name", pa.string(), nullable=False),
            pa.field("trial_count", pa.int64(), nullable=False),
        ]
    ),
    "trajectories": pa.schema(
        [
            pa.field("job_id", pa.string(), nullable=False),
            pa.field("trial_id", pa.string(), nullable=False),
            pa.field("document_id", pa.string(), nullable=False),
            pa.field("source_path", pa.string(), nullable=False),
            pa.field("source_sha256", pa.string(), nullable=False),
            pa.field("embedded_path", pa.string()),
            pa.field("schema_version", pa.string()),
            pa.field("session_id", pa.string()),
            pa.field("trajectory_id", pa.string()),
            pa.field("validation_status", pa.string(), nullable=False),
            pa.field("validator", pa.string(), nullable=False),
            pa.field("validation_error", pa.string()),
            pa.field("agent_name", pa.string()),
            pa.field("agent_version", pa.string()),
            pa.field("model_name", pa.string()),
            pa.field("continued_trajectory_ref", pa.string()),
            pa.field("step_count", pa.int64(), nullable=False),
            pa.field("llm_call_count", pa.int64(), nullable=False),
            pa.field("prompt_tokens", pa.int64()),
            pa.field("completion_tokens", pa.int64()),
            pa.field("cached_tokens", pa.int64()),
            pa.field("cost_usd", pa.float64()),
        ]
    ),
    "steps": pa.schema(
        [
            pa.field("job_id", pa.string(), nullable=False),
            pa.field("trial_id", pa.string(), nullable=False),
            pa.field("document_id", pa.string(), nullable=False),
            pa.field("source_path", pa.string(), nullable=False),
            pa.field("source_sha256", pa.string(), nullable=False),
            pa.field("step_id", pa.int64(), nullable=False),
            pa.field("source", pa.string(), nullable=False),
            pa.field("timestamp", pa.string()),
            pa.field("model_name", pa.string()),
            pa.field("is_copied_context", pa.bool_(), nullable=False),
            pa.field("llm_call_count", pa.int64(), nullable=False),
            pa.field("prompt_tokens", pa.int64()),
            pa.field("completion_tokens", pa.int64()),
            pa.field("cached_tokens", pa.int64()),
            pa.field("cost_usd", pa.float64()),
            pa.field("tool_call_count", pa.int64(), nullable=False),
            pa.field("observation_count", pa.int64(), nullable=False),
        ]
    ),
    "tool_calls": pa.schema(
        [
            pa.field("job_id", pa.string(), nullable=False),
            pa.field("trial_id", pa.string(), nullable=False),
            pa.field("document_id", pa.string(), nullable=False),
            pa.field("source_path", pa.string(), nullable=False),
            pa.field("source_sha256", pa.string(), nullable=False),
            pa.field("step_id", pa.int64(), nullable=False),
            pa.field("tool_call_id", pa.string(), nullable=False),
            pa.field("function_name", pa.string(), nullable=False),
            pa.field("arguments_sha256", pa.string(), nullable=False),
        ]
    ),
    "observations": pa.schema(
        [
            pa.field("job_id", pa.string(), nullable=False),
            pa.field("trial_id", pa.string(), nullable=False),
            pa.field("document_id", pa.string(), nullable=False),
            pa.field("source_path", pa.string(), nullable=False),
            pa.field("source_sha256", pa.string(), nullable=False),
            pa.field("step_id", pa.int64(), nullable=False),
            pa.field("observation_index", pa.int64(), nullable=False),
            pa.field("source_call_id", pa.string()),
            pa.field("content_size_bytes", pa.int64(), nullable=False),
            pa.field("content_sha256", pa.string(), nullable=False),
            pa.field("subagent_ref_count", pa.int64(), nullable=False),
            pa.field("subagent_refs_sha256", pa.string()),
            pa.field("command_exit_code", pa.int64()),
        ]
    ),
}


def _write_parquet(path: Path, table_name: str, rows: list[dict[str, Any]]) -> ExportedTable:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=PARQUET_SCHEMAS[table_name])
    temporary = path.with_suffix(".parquet.tmp")
    pq.write_table(
        table,
        temporary,
        compression="zstd",
        use_dictionary=False,
        write_statistics=True,
    )
    temporary.replace(path)
    return ExportedTable(
        table=table_name,
        path=path,
        rows=len(rows),
        sha256=f"sha256:{sha256_file(path)}",
    )


def export_trajectories(jobs: list[JobRecord], output_root: Path) -> ExportResult:
    output_root = output_root.resolve()
    exported: list[ExportedTable] = []
    for job in sorted(jobs, key=lambda item: item.id):
        for trial in sorted(job.trials, key=lambda item: item.id):
            projection = project_trial(job, trial)
            partition = output_root / f"job_id={job.id}" / f"trial_id={trial.id}"
            rows_by_table = {
                "trajectories": [asdict(item) for item in projection.trajectories],
                "steps": [asdict(item) for item in projection.steps],
                "tool_calls": [asdict(item) for item in projection.tool_calls],
                "observations": [asdict(item) for item in projection.observations],
            }
            for table_name, rows in rows_by_table.items():
                exported.append(
                    _write_parquet(partition / f"{table_name}.parquet", table_name, rows)
                )
    return ExportResult(root=output_root, tables=tuple(exported))


def ingest_and_project(
    database_url: str,
    jobs: list[JobRecord],
    *,
    root: Path,
    output_root: Path,
) -> IngestProjectionResult:
    """Land completed jobs in the catalog, then rebuild their derived Parquet.

    Catalog transactions finish before any Parquet write begins. A filesystem or
    Arrow failure is therefore returned to the caller for event attribution and
    never rolls back the searchable job/trial catalog.
    """
    from evallab import database
    from evallab.facts import ingest_catalog

    ordered_jobs = sorted(jobs, key=lambda item: item.id)
    derived_root = output_root.resolve()
    database.initialize(database_url)
    cataloged_jobs = database.ingest(database_url, ordered_jobs, root=root)
    # Index document-level and deterministic facts before touching Parquet. The
    # paths describe the deterministic target even when a later write is recorded
    # as a projection exception.
    ingest_catalog(
        database_url,
        ordered_jobs,
        root=root,
        derived_root=derived_root,
    )

    tables, failures = project_jobs(ordered_jobs, derived_root)
    return IngestProjectionResult(
        cataloged_jobs=cataloged_jobs,
        tables=tables,
        failures=failures,
    )


def project_jobs(
    jobs: list[JobRecord], output_root: Path
) -> tuple[tuple[ExportedTable, ...], tuple[ProjectionFailure, ...]]:
    """Project raw jobs without requiring the PostgreSQL catalog.

    This is the deterministic, Docker-free half of the composed ingestion path.
    The full path remains :func:`ingest_and_project`, which catalogs first and
    then delegates here. Keeping one projection implementation lets CI exercise
    real Parquet writes while a local smoke also proves PostgreSQL agreement.
    """
    from evallab.facts import rebuild_from_raw

    ordered_jobs = sorted(jobs, key=lambda item: item.id)
    derived_root = output_root.resolve()
    tables: list[ExportedTable] = []
    failures: list[ProjectionFailure] = []
    for job in ordered_jobs:
        try:
            job_table = _write_parquet(
                derived_root / f"job_id={job.id}" / JOB_PROJECTION_FILE,
                "jobs",
                [{"job_id": job.id, "job_name": job.name, "trial_count": len(job.trials)}],
            )
            tables.append(job_table)
            rebuilt = rebuild_from_raw([job], derived_root)
        except Exception as exc:  # Projection failure is data, not an agent result.
            failures.append(
                ProjectionFailure(
                    job_id=job.id,
                    job_name=job.name,
                    error_type=type(exc).__name__,
                    message=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        tables.extend(rebuilt.tables)
    return tuple(tables), tuple(failures)


def _load_catalog_projection_rows(database_url: str) -> list[tuple[str, str, str | None]]:
    import psycopg

    with psycopg.connect(database_url, connect_timeout=2) as connection:
        return [
            (str(job_id), str(job_name), str(trial_id) if trial_id is not None else None)
            for job_id, job_name, trial_id in connection.execute(
                """
                SELECT j.id, j.job_name, t.id
                FROM jobs j
                LEFT JOIN trials t ON t.job_id = j.id
                ORDER BY j.id, t.id
                """
            ).fetchall()
        ]


def _recorded_projection_exceptions_map(events_path: Path) -> dict[str, str]:
    exceptions: dict[str, str] = {}
    for _segment, _line_number, line in read_event_log_lines(events_path):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        reason = payload.get("reason_code") if isinstance(payload, dict) else None
        if not isinstance(reason, str) or not reason.startswith(f"{PROJECTION_FAILURE_REASON}:"):
            continue
        parts = reason.split(":", 2)
        if len(parts) >= 3 and parts[1]:
            exceptions[parts[1]] = parts[2]
        elif len(parts) == 2 and parts[1]:
            exceptions[parts[1]] = "unspecified"
    return exceptions


def _recorded_projection_exceptions(events_path: Path) -> frozenset[str]:
    return frozenset(_recorded_projection_exceptions_map(events_path).keys())


def check_projection_invariant(
    database_url: str,
    output_root: Path,
    events_path: Path,
    *,
    catalog_rows_loader: CatalogRowsLoader = _load_catalog_projection_rows,
) -> ProjectionInvariant:
    """Check catalog jobs against complete Parquet trial partitions.

    A catalog job without a complete partition is acceptable only while its job
    ID has a recorded projection-failure reason in the append-only queue events.
    """
    catalog_rows = catalog_rows_loader(database_url)
    catalog_trials: dict[str, set[str]] = defaultdict(set)
    for job_id, _job_name, trial_id in catalog_rows:
        if trial_id is not None:
            catalog_trials[job_id].add(trial_id)
        else:
            catalog_trials.setdefault(job_id, set())
    catalog_job_ids = frozenset(catalog_trials)

    derived_root = output_root.resolve()
    present_job_ids = {
        path.name.removeprefix("job_id=") for path in derived_root.glob("job_id=*") if path.is_dir()
    }
    projected_job_ids: set[str] = set()
    for job_id, trial_ids in catalog_trials.items():
        job_root = derived_root / f"job_id={job_id}"
        if not (job_root / JOB_PROJECTION_FILE).is_file():
            continue
        if not trial_ids:
            projected_job_ids.add(job_id)
            continue
        if all(
            {
                child.name
                for child in (job_root / f"trial_id={trial_id}").glob("*.parquet")
                if child.is_file()
            }
            >= PROJECTED_TABLES
            for trial_id in trial_ids
        ):
            projected_job_ids.add(job_id)

    recorded_map = _recorded_projection_exceptions_map(events_path)
    recorded = frozenset(recorded_map.keys())
    missing = catalog_job_ids - projected_job_ids
    excepted = missing & recorded
    exceptions_by_reason: dict[str, set[str]] = defaultdict(set)
    for job_id in excepted:
        reason = recorded_map.get(job_id, "unknown")
        exceptions_by_reason[reason].add(job_id)
    frozen_exceptions = {k: frozenset(v) for k, v in sorted(exceptions_by_reason.items())}
    return ProjectionInvariant(
        catalog_job_ids=catalog_job_ids,
        projected_job_ids=frozenset(projected_job_ids),
        excepted_job_ids=frozenset(excepted),
        missing_job_ids=frozenset(missing - excepted),
        extra_job_ids=frozenset(present_job_ids - catalog_job_ids),
        exceptions_by_reason=frozen_exceptions,
    )
