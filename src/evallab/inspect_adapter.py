"""Inspect AI EvalLog ingestion into Eval Lab's canonical trajectory spine.

Raw Inspect logs remain authoritative and may be archived to CAS. This module
projects source-native run, attempt, score, and event facts alongside the same
trajectory/step/tool/observation tables used by Harbor ATIF projections.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
from pydantic import Field

from evallab.evidence.atif import (
    PARQUET_SCHEMAS,
    ObservationFact,
    StepFact,
    ToolCallFact,
    TrajectoryFact,
    TrialTrajectoryProjection,
)
from evallab.evidence.parquet_io import write_table_atomic
from evallab.evidence_store import archive_evidence
from evallab.schemas import ContractModel


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def _digest_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _digest_json(value: Any) -> str:
    return _digest_bytes(_canonical_bytes(value))


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _plain(value.model_dump(mode="json"))
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain(item) for item in value]
    return value


def _mapping(value: Any) -> dict[str, Any]:
    plain = _plain(value)
    return plain if isinstance(plain, dict) else {}


def _sequence(value: Any) -> list[Any]:
    plain = _plain(value)
    return plain if isinstance(plain, list) else []


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(_plain(value), sort_keys=True, ensure_ascii=False)


class InspectRunFactV1(ContractModel):
    schema_version: Literal["inspect-run-fact/v1"] = "inspect-run-fact/v1"
    job_id: str
    source_path: str
    source_digest: str
    inspect_log_version: int | None = None
    status: str
    task_name: str | None = None
    model_name: str | None = None
    run_id: str | None = None
    created_at: str | None = None
    completed_at: str | None = None
    sample_count: int = Field(ge=0)


class InspectAttemptFactV1(ContractModel):
    schema_version: Literal["inspect-attempt-fact/v1"] = "inspect-attempt-fact/v1"
    job_id: str
    trial_id: str
    sample_id: str
    sample_uuid: str | None = None
    epoch: int = Field(ge=1)
    status: str
    started_at: str | None = None
    completed_at: str | None = None
    total_time: float | None = None
    working_time: float | None = None
    error_type: str | None = None
    error_message_digest: str | None = None
    retry_error_count: int = Field(ge=0)
    limit_type: str | None = None


class InspectScoreFactV1(ContractModel):
    schema_version: Literal["inspect-score-fact/v1"] = "inspect-score-fact/v1"
    job_id: str
    trial_id: str
    score_name: str
    outcome_namespace: Literal["inspect"] = "inspect"
    authority: Literal["inspect_scorer"] = "inspect_scorer"
    value_json: str
    value_type: str
    answer: str | None = None
    explanation_digest: str | None = None
    metadata_digest: str | None = None


class InspectEventFactV1(ContractModel):
    schema_version: Literal["inspect-event-fact/v1"] = "inspect-event-fact/v1"
    job_id: str
    trial_id: str
    event_id: str
    event_index: int = Field(ge=0)
    event_type: str
    timestamp: str | None = None
    payload_size_bytes: int = Field(ge=0)
    payload_digest: str


@dataclass(frozen=True)
class InspectProjection:
    run: InspectRunFactV1
    attempts: tuple[InspectAttemptFactV1, ...]
    scores: tuple[InspectScoreFactV1, ...]
    events: tuple[InspectEventFactV1, ...]
    trajectories: TrialTrajectoryProjection


@dataclass(frozen=True)
class InspectIngestResult:
    projection: InspectProjection
    table_paths: dict[str, Path]
    raw_cas_uri: str | None


INSPECT_SCHEMAS: dict[str, pa.Schema] = {
    "inspect_runs": pa.schema(
        [
            pa.field("schema_version", pa.string(), nullable=False),
            pa.field("job_id", pa.string(), nullable=False),
            pa.field("source_path", pa.string(), nullable=False),
            pa.field("source_digest", pa.string(), nullable=False),
            pa.field("inspect_log_version", pa.int64()),
            pa.field("status", pa.string(), nullable=False),
            pa.field("task_name", pa.string()),
            pa.field("model_name", pa.string()),
            pa.field("run_id", pa.string()),
            pa.field("created_at", pa.string()),
            pa.field("completed_at", pa.string()),
            pa.field("sample_count", pa.int64(), nullable=False),
        ]
    ),
    "inspect_attempts": pa.schema(
        [
            pa.field("schema_version", pa.string(), nullable=False),
            pa.field("job_id", pa.string(), nullable=False),
            pa.field("trial_id", pa.string(), nullable=False),
            pa.field("sample_id", pa.string(), nullable=False),
            pa.field("sample_uuid", pa.string()),
            pa.field("epoch", pa.int64(), nullable=False),
            pa.field("status", pa.string(), nullable=False),
            pa.field("started_at", pa.string()),
            pa.field("completed_at", pa.string()),
            pa.field("total_time", pa.float64()),
            pa.field("working_time", pa.float64()),
            pa.field("error_type", pa.string()),
            pa.field("error_message_digest", pa.string()),
            pa.field("retry_error_count", pa.int64(), nullable=False),
            pa.field("limit_type", pa.string()),
        ]
    ),
    "inspect_scores": pa.schema(
        [
            pa.field("schema_version", pa.string(), nullable=False),
            pa.field("job_id", pa.string(), nullable=False),
            pa.field("trial_id", pa.string(), nullable=False),
            pa.field("score_name", pa.string(), nullable=False),
            pa.field("outcome_namespace", pa.string(), nullable=False),
            pa.field("authority", pa.string(), nullable=False),
            pa.field("value_json", pa.string(), nullable=False),
            pa.field("value_type", pa.string(), nullable=False),
            pa.field("answer", pa.string()),
            pa.field("explanation_digest", pa.string()),
            pa.field("metadata_digest", pa.string()),
        ]
    ),
    "inspect_events": pa.schema(
        [
            pa.field("schema_version", pa.string(), nullable=False),
            pa.field("job_id", pa.string(), nullable=False),
            pa.field("trial_id", pa.string(), nullable=False),
            pa.field("event_id", pa.string(), nullable=False),
            pa.field("event_index", pa.int64(), nullable=False),
            pa.field("event_type", pa.string(), nullable=False),
            pa.field("timestamp", pa.string()),
            pa.field("payload_size_bytes", pa.int64(), nullable=False),
            pa.field("payload_digest", pa.string(), nullable=False),
        ]
    ),
}


def load_inspect_eval_log(path: Path) -> dict[str, Any]:
    """Read .eval/.json through Inspect's API when installed; JSON remains fixture-friendly."""
    path = path.resolve()
    if path.suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Inspect JSON log root must be an object")
        return payload
    try:
        inspect_log = importlib.import_module("inspect_ai.log")
        read_eval_log = inspect_log.read_eval_log
    except (ImportError, AttributeError) as exc:
        raise RuntimeError(
            "Reading binary .eval logs requires the optional 'inspect' dependency group"
        ) from exc
    return _mapping(read_eval_log(path, resolve_attachments="core"))


def _usage_totals(sample: Mapping[str, Any]) -> tuple[int, int, int, float | None]:
    prompt = 0
    completion = 0
    cached = 0
    cost = 0.0
    has_cost = False
    for usage in _mapping(sample.get("model_usage")).values():
        row = _mapping(usage)
        prompt += _optional_int(row.get("input_tokens")) or 0
        completion += _optional_int(row.get("output_tokens")) or 0
        cached += (
            _optional_int(row.get("cache_read_tokens"))
            or _optional_int(row.get("cached_tokens"))
            or 0
        )
        row_cost = _optional_float(row.get("cost"))
        if row_cost is not None:
            cost += row_cost
            has_cost = True
    return prompt, completion, cached, cost if has_cost else None


def _score_facts(job_id: str, trial_id: str, sample: Mapping[str, Any]) -> list[InspectScoreFactV1]:
    facts = []
    for name, raw_score in sorted(_mapping(sample.get("scores")).items()):
        score = _mapping(raw_score)
        value = score.get("value", raw_score)
        value_json = json.dumps(_plain(value), sort_keys=True, separators=(",", ":"))
        explanation = score.get("explanation")
        metadata = score.get("metadata")
        facts.append(
            InspectScoreFactV1(
                job_id=job_id,
                trial_id=trial_id,
                score_name=name,
                value_json=value_json,
                value_type=type(value).__name__,
                answer=str(score["answer"]) if score.get("answer") is not None else None,
                explanation_digest=_digest_json(explanation) if explanation is not None else None,
                metadata_digest=_digest_json(metadata) if metadata is not None else None,
            )
        )
    return facts


def project_inspect_eval_log(
    payload: Mapping[str, Any] | Any,
    *,
    source_path: str,
    source_bytes: bytes | None = None,
) -> InspectProjection:
    """Project one Inspect EvalLog into source facts and the canonical trajectory tables."""
    log = _mapping(payload)
    if not log:
        raise ValueError("Inspect EvalLog payload is empty or unsupported")
    canonical_source = source_bytes if source_bytes is not None else _canonical_bytes(log)
    source_digest = _digest_bytes(canonical_source)
    eval_spec = _mapping(log.get("eval"))
    task_name = eval_spec.get("task") or eval_spec.get("task_name")
    model = eval_spec.get("model")
    model_name = _text(model) or None
    run_id = eval_spec.get("run_id") or eval_spec.get("eval_set_id")
    job_id = _stable_id("inspect", source_digest, str(task_name or ""), str(run_id or ""))
    samples = [_mapping(sample) for sample in _sequence(log.get("samples"))]
    run = InspectRunFactV1(
        job_id=job_id,
        source_path=source_path,
        source_digest=source_digest,
        inspect_log_version=_optional_int(log.get("version")),
        status=str(log.get("status", "unknown")),
        task_name=str(task_name) if task_name is not None else None,
        model_name=model_name,
        run_id=str(run_id) if run_id is not None else None,
        created_at=str(eval_spec["created_at"]) if eval_spec.get("created_at") else None,
        completed_at=str(eval_spec["completed_at"]) if eval_spec.get("completed_at") else None,
        sample_count=len(samples),
    )

    attempts: list[InspectAttemptFactV1] = []
    scores: list[InspectScoreFactV1] = []
    inspect_events: list[InspectEventFactV1] = []
    trajectories: list[TrajectoryFact] = []
    steps: list[StepFact] = []
    tool_calls: list[ToolCallFact] = []
    observations: list[ObservationFact] = []

    for sample_index, sample in enumerate(samples):
        sample_id = str(sample.get("id", sample_index))
        epoch = _optional_int(sample.get("epoch")) or 1
        sample_uuid = str(sample["uuid"]) if sample.get("uuid") else None
        trial_id = sample_uuid or _stable_id(job_id, sample_id, str(epoch))
        document_id = _stable_id(trial_id, source_digest, "inspect")
        error = _mapping(sample.get("error"))
        retry_errors = _sequence(sample.get("error_retries"))
        limit = _mapping(sample.get("limit"))
        error_message = error.get("message") or error.get("traceback")
        attempts.append(
            InspectAttemptFactV1(
                job_id=job_id,
                trial_id=trial_id,
                sample_id=sample_id,
                sample_uuid=sample_uuid,
                epoch=epoch,
                status="error" if error else "success",
                started_at=str(sample["started_at"]) if sample.get("started_at") else None,
                completed_at=(str(sample["completed_at"]) if sample.get("completed_at") else None),
                total_time=_optional_float(sample.get("total_time")),
                working_time=_optional_float(sample.get("working_time")),
                error_type=str(error.get("type") or error.get("error_type")) if error else None,
                error_message_digest=(
                    _digest_json(error_message) if error_message is not None else None
                ),
                retry_error_count=len(retry_errors),
                limit_type=str(limit.get("type") or sample.get("token_limit_type"))
                if limit or sample.get("token_limit_type")
                else None,
            )
        )
        scores.extend(_score_facts(job_id, trial_id, sample))

        event_rows = [_mapping(event) for event in _sequence(sample.get("events"))]
        for event_index, event in enumerate(event_rows):
            event_bytes = _canonical_bytes(event)
            event_type = str(
                event.get("event") or event.get("event_type") or event.get("type") or "unknown"
            )
            inspect_events.append(
                InspectEventFactV1(
                    job_id=job_id,
                    trial_id=trial_id,
                    event_id=_stable_id(trial_id, str(event_index), _digest_bytes(event_bytes)),
                    event_index=event_index,
                    event_type=event_type,
                    timestamp=str(event["timestamp"]) if event.get("timestamp") else None,
                    payload_size_bytes=len(event_bytes),
                    payload_digest=_digest_bytes(event_bytes),
                )
            )

        messages = [_mapping(message) for message in _sequence(sample.get("messages"))]
        output = _mapping(sample.get("output"))
        if not messages and output:
            completion = output.get("completion")
            if completion is not None:
                messages.append({"role": "assistant", "content": completion})
        prompt_tokens, completion_tokens, cached_tokens, cost_usd = _usage_totals(sample)
        llm_calls = sum(
            1
            for event in event_rows
            if str(event.get("event") or event.get("event_type") or event.get("type"))
            in {"model", "model_event", "ModelEvent"}
        )
        if llm_calls == 0:
            llm_calls = sum(message.get("role") == "assistant" for message in messages)

        for step_index, message in enumerate(messages):
            role = str(message.get("role") or message.get("source") or "unknown")
            message_tool_calls = [_mapping(call) for call in _sequence(message.get("tool_calls"))]
            is_observation = role in {"tool", "observation"}
            steps.append(
                StepFact(
                    job_id=job_id,
                    trial_id=trial_id,
                    document_id=document_id,
                    source_path=source_path,
                    source_sha256=source_digest,
                    step_id=step_index,
                    source=role,
                    timestamp=str(message["timestamp"]) if message.get("timestamp") else None,
                    model_name=str(message["model"]) if message.get("model") else model_name,
                    is_copied_context=False,
                    llm_call_count=1 if role == "assistant" else 0,
                    prompt_tokens=None,
                    completion_tokens=None,
                    cached_tokens=None,
                    cost_usd=None,
                    tool_call_count=len(message_tool_calls),
                    observation_count=1 if is_observation else 0,
                )
            )
            for call_index, call in enumerate(message_tool_calls):
                function = _mapping(call.get("function"))
                name = call.get("name") or function.get("name") or call.get("function_name")
                arguments = call.get("arguments", function.get("arguments", {}))
                call_id = str(call.get("id") or call.get("tool_call_id") or call_index)
                tool_calls.append(
                    ToolCallFact(
                        job_id=job_id,
                        trial_id=trial_id,
                        document_id=document_id,
                        source_path=source_path,
                        source_sha256=source_digest,
                        step_id=step_index,
                        tool_call_id=call_id,
                        function_name=str(name or "unknown"),
                        arguments_sha256=_digest_json(arguments),
                    )
                )
            if is_observation:
                content = _text(message.get("content"))
                content_bytes = content.encode()
                source_call_id = message.get("tool_call_id") or message.get("source_call_id")
                observations.append(
                    ObservationFact(
                        job_id=job_id,
                        trial_id=trial_id,
                        document_id=document_id,
                        source_path=source_path,
                        source_sha256=source_digest,
                        step_id=step_index,
                        observation_index=0,
                        source_call_id=str(source_call_id) if source_call_id else None,
                        content_size_bytes=len(content_bytes),
                        content_sha256=_digest_bytes(content_bytes),
                        subagent_ref_count=0,
                        subagent_refs_sha256=None,
                        command_exit_code=None,
                    )
                )

        trajectories.append(
            TrajectoryFact(
                job_id=job_id,
                trial_id=trial_id,
                document_id=document_id,
                source_path=source_path,
                source_sha256=source_digest,
                embedded_path=None,
                schema_version=f"inspect-eval/v{log.get('version', 'unknown')}",
                session_id=sample_uuid,
                trajectory_id=sample_uuid or trial_id,
                validation_status="valid",
                validator="inspect_ai.log.read_eval_log",
                validation_error=None,
                agent_name=_text(eval_spec.get("solver")) or "inspect",
                agent_version=str(log.get("version")) if log.get("version") is not None else None,
                model_name=model_name,
                continued_trajectory_ref=None,
                step_count=len(messages),
                llm_call_count=llm_calls,
                prompt_tokens=prompt_tokens or None,
                completion_tokens=completion_tokens or None,
                cached_tokens=cached_tokens or None,
                cost_usd=cost_usd,
            )
        )

    return InspectProjection(
        run=run,
        attempts=tuple(attempts),
        scores=tuple(scores),
        events=tuple(inspect_events),
        trajectories=TrialTrajectoryProjection(
            trajectories=tuple(trajectories),
            steps=tuple(steps),
            tool_calls=tuple(tool_calls),
            observations=tuple(observations),
        ),
    )


def write_inspect_projection(
    projection: InspectProjection,
    output_root: Path,
) -> dict[str, Path]:
    """Write Inspect-native and canonical facts into one partitioned Parquet root."""
    root = output_root.resolve() / "source=inspect" / f"job_id={projection.run.job_id}"
    root.mkdir(parents=True, exist_ok=True)
    table_rows: dict[str, list[dict[str, Any]]] = {
        "inspect_runs": [projection.run.model_dump(mode="json")],
        "inspect_attempts": [row.model_dump(mode="json") for row in projection.attempts],
        "inspect_scores": [row.model_dump(mode="json") for row in projection.scores],
        "inspect_events": [row.model_dump(mode="json") for row in projection.events],
        "trajectories": [asdict(row) for row in projection.trajectories.trajectories],
        "steps": [asdict(row) for row in projection.trajectories.steps],
        "tool_calls": [asdict(row) for row in projection.trajectories.tool_calls],
        "observations": [asdict(row) for row in projection.trajectories.observations],
    }
    paths = {}
    for name, rows in table_rows.items():
        schema = INSPECT_SCHEMAS.get(name) or PARQUET_SCHEMAS[name]
        path = root / f"{name}.parquet"
        write_table_atomic(path, rows, schema)
        paths[name] = path
    return paths


def ingest_inspect_eval_log(
    path: Path,
    *,
    output_root: Path,
    store_root: Path | None = None,
) -> InspectIngestResult:
    """Read, optionally archive, normalize, and project one Inspect evaluation log."""
    source_bytes = path.read_bytes()
    payload = load_inspect_eval_log(path)
    projection = project_inspect_eval_log(
        payload,
        source_path=path.name,
        source_bytes=source_bytes,
    )
    table_paths = write_inspect_projection(projection, output_root)
    cas_uri = None
    if store_root is not None:
        with tempfile.TemporaryDirectory(prefix="evallab-inspect-") as temporary:
            staging = Path(temporary)
            shutil.copy2(path, staging / path.name)
            (staging / "source-manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": "inspect-source-manifest/v1",
                        "job_id": projection.run.job_id,
                        "source_digest": projection.run.source_digest,
                        "source_file": path.name,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            archive = archive_evidence(
                staging,
                store_root,
                record_id=projection.run.job_id,
                kind="inspect_eval_log",
            )
            cas_uri = archive.uri
    return InspectIngestResult(
        projection=projection,
        table_paths=table_paths,
        raw_cas_uri=cas_uri,
    )
