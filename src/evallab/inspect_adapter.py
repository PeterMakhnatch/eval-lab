"""Inspect AI EvalLog ingestion and normalization into Eval Lab's canonical trajectory spine.

Invariants:
1. Raw Source Fidelity: Raw Inspect EvalLogs are read via the official API (or structured JSON),
   preserving source-native epochs, multi-score entries, sample UUIDs, limits, retry errors, and events.
2. No Flattening / No Silent Coercion: Inspect epochs are distinct attempts, never flattened into
   Harbor retries; arbitrary scorer outputs remain in the 'inspect' outcome namespace under 'inspect_scorer'
   authority and are never coerced into primary benchmark reward.
3. Content-Addressed Lineage: Source digests and deterministic rebuild digests link canonical facts,
   source-native tables, and CAS-archived evidence manifests.
4. Fail-Closed Validation: Rejects malformed log roots, missing/invalid schema versions, identity collisions,
   and inconsistent projection rows.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import pyarrow as pa
from pydantic import Field

from evallab.evidence.atif import (
    ObservationFact,
    StepFact,
    ToolCallFact,
    TrajectoryFact,
    TrialTrajectoryProjection,
)
from evallab.schemas import ContractModel

ATTACHMENT_PROTOCOLS = ("attachment://", "tc://")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _plain(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _digest_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _digest_json(value: Any) -> str:
    return _digest_bytes(_canonical_bytes(value))


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _plain(value: Any) -> Any:
    """Recursively convert Pydantic models, dataclasses, enums, and objects to plain JSON primitives."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if hasattr(value, "model_dump") and callable(value.model_dump):
        try:
            return _plain(value.model_dump(mode="json"))
        except Exception:
            return _plain(value.model_dump())
    if hasattr(value, "dict") and callable(value.dict):
        try:
            return _plain(value.dict())
        except Exception:
            pass
    if hasattr(value, "to_dict") and callable(value.to_dict):
        try:
            return _plain(value.to_dict())
        except Exception:
            pass
    if hasattr(value, "__dict__") and not isinstance(value, type):
        try:
            return _plain({k: v for k, v in value.__dict__.items() if not k.startswith("_")})
        except Exception:
            pass
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(item) for item in value]
    return str(value)


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
    plain = _plain(value)
    if isinstance(plain, str):
        return plain
    return json.dumps(plain, sort_keys=True, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Source-Native Inspect Fact Models
# --------------------------------------------------------------------------- #


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
    rebuild_digest: str | None = None
    eval_spec_digest: str | None = None
    plan_name: str | None = None


class InspectAttemptFactV1(ContractModel):
    schema_version: Literal["inspect-attempt-fact/v1"] = "inspect-attempt-fact/v1"
    job_id: str
    trial_id: str
    sample_id: str
    sample_uuid: str | None = None
    epoch: int = Field(default=1, ge=1)
    status: str
    started_at: str | None = None
    completed_at: str | None = None
    total_time: float | None = None
    working_time: float | None = None
    error_type: str | None = None
    error_message_digest: str | None = None
    retry_error_count: int = Field(default=0, ge=0)
    limit_type: str | None = None
    input_digest: str | None = None
    target_digest: str | None = None
    message_count: int = Field(default=0, ge=0)
    event_count: int = Field(default=0, ge=0)


class InspectScoreFactV1(ContractModel):
    schema_version: Literal["inspect-score-fact/v1"] = "inspect-score-fact/v1"
    job_id: str
    trial_id: str
    score_name: str
    value_json: str
    value_type: str
    answer: str | None = None
    explanation_digest: str | None = None
    metadata_digest: str | None = None
    scorer: str | None = None
    outcome_namespace: Literal["inspect"] = "inspect"
    authority: Literal["inspect_scorer"] = "inspect_scorer"


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
    span_id: str | None = None


class InspectAttachmentFactV1(ContractModel):
    schema_version: Literal["inspect-attachment-fact/v1"] = "inspect-attachment-fact/v1"
    job_id: str
    trial_id: str | None = None
    attachment_id: str
    content_type: str
    content_size_bytes: int = Field(ge=0)
    content_digest: str
    resolved_count: int = Field(default=0, ge=0)


@dataclass(frozen=True)
class InspectProjection:
    run: InspectRunFactV1
    attempts: tuple[InspectAttemptFactV1, ...]
    scores: tuple[InspectScoreFactV1, ...]
    events: tuple[InspectEventFactV1, ...]
    attachments: tuple[InspectAttachmentFactV1, ...]
    trajectories: TrialTrajectoryProjection
    rebuild_digest: str


@dataclass(frozen=True)
class InspectIngestResult:
    projection: InspectProjection
    table_paths: dict[str, Path]
    raw_cas_uri: str | None
    source_manifest: Any = None


INSPECT_SCHEMAS: dict[str, pa.Schema] = {
    "inspect_runs": pa.schema(
        [
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
            pa.field("rebuild_digest", pa.string()),
            pa.field("eval_spec_digest", pa.string()),
            pa.field("plan_name", pa.string()),
        ]
    ),
    "inspect_attempts": pa.schema(
        [
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
            pa.field("input_digest", pa.string()),
            pa.field("target_digest", pa.string()),
            pa.field("message_count", pa.int64(), nullable=False),
            pa.field("event_count", pa.int64(), nullable=False),
        ]
    ),
    "inspect_scores": pa.schema(
        [
            pa.field("job_id", pa.string(), nullable=False),
            pa.field("trial_id", pa.string(), nullable=False),
            pa.field("score_name", pa.string(), nullable=False),
            pa.field("value_json", pa.string(), nullable=False),
            pa.field("value_type", pa.string(), nullable=False),
            pa.field("answer", pa.string()),
            pa.field("explanation_digest", pa.string()),
            pa.field("metadata_digest", pa.string()),
            pa.field("scorer", pa.string()),
            pa.field("outcome_namespace", pa.string(), nullable=False),
            pa.field("authority", pa.string(), nullable=False),
        ]
    ),
    "inspect_events": pa.schema(
        [
            pa.field("job_id", pa.string(), nullable=False),
            pa.field("trial_id", pa.string(), nullable=False),
            pa.field("event_id", pa.string(), nullable=False),
            pa.field("event_index", pa.int64(), nullable=False),
            pa.field("event_type", pa.string(), nullable=False),
            pa.field("timestamp", pa.string()),
            pa.field("payload_size_bytes", pa.int64(), nullable=False),
            pa.field("payload_digest", pa.string(), nullable=False),
            pa.field("span_id", pa.string()),
        ]
    ),
    "inspect_attachments": pa.schema(
        [
            pa.field("job_id", pa.string(), nullable=False),
            pa.field("trial_id", pa.string()),
            pa.field("attachment_id", pa.string(), nullable=False),
            pa.field("content_type", pa.string(), nullable=False),
            pa.field("content_size_bytes", pa.int64(), nullable=False),
            pa.field("content_digest", pa.string(), nullable=False),
            pa.field("resolved_count", pa.int64(), nullable=False),
        ]
    ),
}


# --------------------------------------------------------------------------- #
# Loading & Schema Validation
# --------------------------------------------------------------------------- #


def validate_inspect_eval_log(log: Mapping[str, Any]) -> None:
    """Fail-closed validation of raw Inspect EvalLog structure."""
    if not isinstance(log, (dict, Mapping)) or not log:
        raise ValueError("Inspect EvalLog payload is empty or not a dictionary")
    version = log.get("version")
    if version is None:
        raise ValueError("Inspect EvalLog version is missing")
    if not isinstance(version, (int, str)) or isinstance(version, bool):
        raise ValueError(f"Inspect EvalLog version has invalid type: {type(version).__name__}")
    if isinstance(version, int) and version <= 0:
        raise ValueError(f"Inspect EvalLog version must be positive, got {version}")

    status = log.get("status")
    if status is None:
        raise ValueError("Inspect EvalLog status is missing")
    if not isinstance(status, str) or not status.strip():
        raise ValueError("Inspect EvalLog status must be a non-empty string")

    eval_spec = log.get("eval")
    if eval_spec is not None and not isinstance(eval_spec, (dict, Mapping)):
        raise ValueError("Inspect EvalLog 'eval' field must be an object")

    samples = log.get("samples")
    if samples is not None and not isinstance(samples, (list, tuple)):
        raise ValueError("Inspect EvalLog 'samples' field must be a list")


def load_inspect_eval_log(path: Path) -> dict[str, Any]:
    """Read .eval/.json through Inspect's API when installed; JSON remains fixture-friendly."""
    path = path.resolve()
    if path.suffix == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"Failed to parse Inspect JSON log at {path}: {exc}") from exc
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
    raw_log = read_eval_log(path, resolve_attachments="core")
    return _mapping(raw_log)


# --------------------------------------------------------------------------- #
# Attachments and Content Block Normalization
# --------------------------------------------------------------------------- #


def _resolve_attachment_string(
    text: str,
    sample_attachments: Mapping[str, Any],
    log_attachments: Mapping[str, Any],
    tracker: dict[str, int],
) -> str:
    for protocol in ATTACHMENT_PROTOCOLS:
        if text.startswith(protocol):
            key = text[len(protocol) :]
            if key in sample_attachments:
                tracker[key] = tracker.get(key, 0) + 1
                resolved = sample_attachments[key]
                return resolved if isinstance(resolved, str) else _text(resolved)
            if key in log_attachments:
                tracker[key] = tracker.get(key, 0) + 1
                resolved = log_attachments[key]
                return resolved if isinstance(resolved, str) else _text(resolved)
    return text


def _resolve_content_blocks(
    content: Any,
    sample_attachments: Mapping[str, Any],
    log_attachments: Mapping[str, Any],
    tracker: dict[str, int],
) -> str:
    if isinstance(content, str):
        return _resolve_attachment_string(content, sample_attachments, log_attachments, tracker)
    if isinstance(content, (list, tuple)):
        resolved_parts = []
        for part in content:
            if isinstance(part, str):
                resolved_parts.append(
                    _resolve_attachment_string(part, sample_attachments, log_attachments, tracker)
                )
            elif isinstance(part, dict):
                part_type = str(part.get("type", ""))
                if part_type == "text" and "text" in part:
                    resolved_parts.append(
                        _resolve_attachment_string(
                            str(part["text"]),
                            sample_attachments,
                            log_attachments,
                            tracker,
                        )
                    )
                elif part_type == "reasoning" and "reasoning" in part:
                    resolved_parts.append(f"[Reasoning: {part['reasoning']}]")
                elif part_type == "refusal" and "refusal" in part:
                    resolved_parts.append(f"[Refusal: {part['refusal']}]")
                elif part_type == "image" and "image" in part:
                    img_ref = _resolve_attachment_string(
                        str(part["image"]),
                        sample_attachments,
                        log_attachments,
                        tracker,
                    )
                    resolved_parts.append(f"[Image: {img_ref}]")
                else:
                    resolved_parts.append(_text(part))
            else:
                resolved_parts.append(_text(part))
        return "\n".join(resolved_parts)
    if isinstance(content, dict):
        part_type = str(content.get("type", ""))
        if part_type == "text" and "text" in content:
            return _resolve_attachment_string(
                str(content["text"]),
                sample_attachments,
                log_attachments,
                tracker,
            )
        return _text(content)
    return _text(content)


def _usage_totals(sample: Mapping[str, Any]) -> tuple[int, int, int, float | None]:
    prompt = 0
    completion = 0
    cached = 0
    cost = 0.0
    has_cost = False
    for usage in _mapping(sample.get("model_usage")).values():
        row = _mapping(usage)
        prompt += (
            _optional_int(row.get("input_tokens")) or _optional_int(row.get("prompt_tokens")) or 0
        )
        completion += (
            _optional_int(row.get("output_tokens"))
            or _optional_int(row.get("completion_tokens"))
            or 0
        )
        cached += (
            _optional_int(row.get("cache_read_tokens"))
            or _optional_int(row.get("cached_tokens"))
            or _optional_int(row.get("cache_write_tokens"))
            or 0
        )
        row_cost = _optional_float(row.get("cost")) or _optional_float(row.get("cost_usd"))
        if row_cost is not None:
            cost += row_cost
            has_cost = True
    return prompt, completion, cached, cost if has_cost else None


def _score_facts(
    job_id: str,
    trial_id: str,
    sample: Mapping[str, Any],
) -> list[InspectScoreFactV1]:
    facts = []
    raw_scores = _mapping(sample.get("scores"))
    for name, raw_score in sorted(raw_scores.items()):
        score = _mapping(raw_score) if isinstance(_plain(raw_score), dict) else {}
        value = score.get("value", raw_score) if score else raw_score
        value_plain = _plain(value)
        value_json = json.dumps(
            value_plain, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        value_type = type(value_plain).__name__
        answer = str(score["answer"]) if score.get("answer") is not None else None
        explanation = score.get("explanation")
        metadata = score.get("metadata")
        scorer = str(score["scorer"]) if score.get("scorer") is not None else None
        facts.append(
            InspectScoreFactV1(
                job_id=job_id,
                trial_id=trial_id,
                score_name=name,
                value_json=value_json,
                value_type=value_type,
                answer=answer,
                explanation_digest=_digest_json(explanation) if explanation is not None else None,
                metadata_digest=_digest_json(metadata) if metadata is not None else None,
                scorer=scorer,
                outcome_namespace="inspect",
                authority="inspect_scorer",
            )
        )
    return facts


# --------------------------------------------------------------------------- #
# Rebuild Digest and Row Reconciliation
# --------------------------------------------------------------------------- #


def compute_rebuild_digest(
    run: InspectRunFactV1,
    attempts: tuple[InspectAttemptFactV1, ...],
    scores: tuple[InspectScoreFactV1, ...],
    events: tuple[InspectEventFactV1, ...],
    attachments: tuple[InspectAttachmentFactV1, ...],
    trajectories: TrialTrajectoryProjection,
) -> str:
    """Compute a cryptographic digest of the complete projected dataset for deterministic rebuild verification."""
    payload = {
        "run": {k: v for k, v in run.model_dump(mode="json").items() if k != "rebuild_digest"},
        "attempts": sorted(
            [row.model_dump(mode="json") for row in attempts],
            key=lambda item: (item["trial_id"], item["epoch"]),
        ),
        "scores": sorted(
            [row.model_dump(mode="json") for row in scores],
            key=lambda item: (item["trial_id"], item["score_name"]),
        ),
        "events": sorted(
            [row.model_dump(mode="json") for row in events],
            key=lambda item: (item["trial_id"], item["event_index"]),
        ),
        "attachments": sorted(
            [row.model_dump(mode="json") for row in attachments],
            key=lambda item: (item["attachment_id"], item.get("trial_id") or ""),
        ),
        "trajectories": sorted(
            [asdict(row) for row in trajectories.trajectories],
            key=lambda item: item["trial_id"],
        ),
        "steps": sorted(
            [asdict(row) for row in trajectories.steps],
            key=lambda item: (item["trial_id"], item["step_id"]),
        ),
        "tool_calls": sorted(
            [asdict(row) for row in trajectories.tool_calls],
            key=lambda item: (item["trial_id"], item["step_id"], item["tool_call_id"]),
        ),
        "observations": sorted(
            [asdict(row) for row in trajectories.observations],
            key=lambda item: (item["trial_id"], item["step_id"], item["observation_index"]),
        ),
    }
    return _digest_json(payload)


def reconcile_inspect_projection(
    run: InspectRunFactV1,
    attempts: tuple[InspectAttemptFactV1, ...],
    scores: tuple[InspectScoreFactV1, ...],
    events: tuple[InspectEventFactV1, ...],
    attachments: tuple[InspectAttachmentFactV1, ...],
    trajectories: TrialTrajectoryProjection,
) -> None:
    """Verify projection invariants, cross-table linkages, and row reconciliation."""
    if len(attempts) != len(trajectories.trajectories):
        raise ValueError(
            f"Row mismatch: {len(attempts)} attempts but {len(trajectories.trajectories)} trajectories"
        )
    attempt_trial_ids = {a.trial_id for a in attempts}
    if len(attempt_trial_ids) != len(attempts):
        raise ValueError("Collision: Duplicate trial_id detected in attempts")

    for traj in trajectories.trajectories:
        if traj.job_id != run.job_id:
            raise ValueError(
                f"Trajectory job_id {traj.job_id} does not match run job_id {run.job_id}"
            )
        if traj.trial_id not in attempt_trial_ids:
            raise ValueError(f"Orphan trajectory trial_id: {traj.trial_id}")

    for step in trajectories.steps:
        if step.job_id != run.job_id:
            raise ValueError(f"Step job_id {step.job_id} does not match run job_id {run.job_id}")
        if step.trial_id not in attempt_trial_ids:
            raise ValueError(f"Orphan step trial_id: {step.trial_id}")

    for score in scores:
        if score.job_id != run.job_id:
            raise ValueError(f"Score job_id {score.job_id} does not match run job_id {run.job_id}")
        if score.trial_id not in attempt_trial_ids:
            raise ValueError(f"Orphan score trial_id: {score.trial_id}")

    for event in events:
        if event.job_id != run.job_id:
            raise ValueError(f"Event job_id {event.job_id} does not match run job_id {run.job_id}")
        if event.trial_id not in attempt_trial_ids:
            raise ValueError(f"Orphan event trial_id: {event.trial_id}")


# --------------------------------------------------------------------------- #
# Projection Engine
# --------------------------------------------------------------------------- #


def project_inspect_eval_log(
    payload: Mapping[str, Any] | Any,
    *,
    source_path: str,
    source_bytes: bytes | None = None,
) -> InspectProjection:
    """Project one Inspect EvalLog into source facts and the canonical trajectory tables."""
    log = _mapping(payload)
    validate_inspect_eval_log(log)

    canonical_source = source_bytes if source_bytes is not None else _canonical_bytes(log)
    source_digest = _digest_bytes(canonical_source)
    eval_spec = _mapping(log.get("eval"))
    task_name = eval_spec.get("task") or eval_spec.get("task_name")
    model = eval_spec.get("model")
    model_name = _text(model) or None
    run_id = eval_spec.get("run_id") or eval_spec.get("eval_set_id")
    job_id = _stable_id("inspect", source_digest, str(task_name or ""), str(run_id or ""))

    log_attachments = _mapping(log.get("attachments"))
    samples = [_mapping(sample) for sample in _sequence(log.get("samples"))]

    eval_spec_digest = _digest_json(eval_spec) if eval_spec else None
    plan = _mapping(log.get("plan"))
    plan_name = str(plan["name"]) if plan.get("name") else None

    attempts: list[InspectAttemptFactV1] = []
    scores: list[InspectScoreFactV1] = []
    inspect_events: list[InspectEventFactV1] = []
    attachment_facts: list[InspectAttachmentFactV1] = []
    trajectories: list[TrajectoryFact] = []
    steps: list[StepFact] = []
    tool_calls: list[ToolCallFact] = []
    observations: list[ObservationFact] = []

    seen_trial_ids: set[str] = set()
    seen_sample_epoch_identities: set[tuple[str, int]] = set()
    attachment_tracker: dict[str, int] = {}

    # Register log-level attachments
    for att_name, att_val in sorted(log_attachments.items()):
        att_bytes = _canonical_bytes(att_val) if not isinstance(att_val, str) else att_val.encode()
        attachment_facts.append(
            InspectAttachmentFactV1(
                job_id=job_id,
                trial_id=None,
                attachment_id=att_name,
                content_type="text" if isinstance(att_val, str) else "json",
                content_size_bytes=len(att_bytes),
                content_digest=_digest_bytes(att_bytes),
                resolved_count=0,
            )
        )

    for sample_index, sample in enumerate(samples):
        sample_id = str(sample.get("id", sample_index))
        raw_epoch = sample.get("epoch")
        if raw_epoch is not None:
            epoch_opt = _optional_int(raw_epoch)
            if epoch_opt is None or epoch_opt < 1:
                raise ValueError(f"Sample {sample_id} has invalid non-positive epoch: {raw_epoch}")
            epoch = epoch_opt
        else:
            epoch = 1

        identity_pair = (sample_id, epoch)
        if identity_pair in seen_sample_epoch_identities:
            raise ValueError(
                f"Duplicate sample identity collision for (id={sample_id}, epoch={epoch})"
            )
        seen_sample_epoch_identities.add(identity_pair)

        sample_uuid = str(sample["uuid"]) if sample.get("uuid") else None
        trial_id = sample_uuid or _stable_id(job_id, sample_id, str(epoch))

        if trial_id in seen_trial_ids:
            raise ValueError(f"Duplicate trial_id collision detected: {trial_id}")
        seen_trial_ids.add(trial_id)

        sample_attachments = _mapping(sample.get("attachments"))
        for att_name, att_val in sorted(sample_attachments.items()):
            att_bytes = (
                _canonical_bytes(att_val) if not isinstance(att_val, str) else att_val.encode()
            )
            attachment_facts.append(
                InspectAttachmentFactV1(
                    job_id=job_id,
                    trial_id=trial_id,
                    attachment_id=att_name,
                    content_type="text" if isinstance(att_val, str) else "json",
                    content_size_bytes=len(att_bytes),
                    content_digest=_digest_bytes(att_bytes),
                    resolved_count=0,
                )
            )

        document_id = _stable_id(trial_id, source_digest, "inspect")
        error = _mapping(sample.get("error"))
        retry_errors = _sequence(sample.get("error_retries"))
        limit = _mapping(sample.get("limit"))
        error_message = error.get("message") or error.get("traceback")

        sample_input = sample.get("input")
        input_digest = _digest_json(sample_input) if sample_input is not None else None
        sample_target = sample.get("target")
        target_digest = _digest_json(sample_target) if sample_target is not None else None

        limit_type_val = limit.get("type") or sample.get("token_limit_type")
        if not limit_type_val and sample.get("time_limit"):
            limit_type_val = "time"
        if not limit_type_val and sample.get("message_limit"):
            limit_type_val = "message"

        event_rows = [_mapping(event) for event in _sequence(sample.get("events"))]
        messages = [_mapping(message) for message in _sequence(sample.get("messages"))]
        output = _mapping(sample.get("output"))
        if not messages and output:
            completion = output.get("completion")
            if completion is not None:
                messages.append({"role": "assistant", "content": completion})

        sample_status = str(sample.get("status")) if sample.get("status") else None
        if not sample_status:
            sample_status = "error" if error else "success"

        attempts.append(
            InspectAttemptFactV1(
                job_id=job_id,
                trial_id=trial_id,
                sample_id=sample_id,
                sample_uuid=sample_uuid,
                epoch=epoch,
                status=sample_status,
                started_at=str(sample["started_at"]) if sample.get("started_at") else None,
                completed_at=str(sample["completed_at"]) if sample.get("completed_at") else None,
                total_time=_optional_float(sample.get("total_time")),
                working_time=_optional_float(sample.get("working_time")),
                error_type=str(error.get("type") or error.get("error_type") or error.get("name"))
                if error
                else None,
                error_message_digest=_digest_json(error_message)
                if error_message is not None
                else None,
                retry_error_count=len(retry_errors),
                limit_type=str(limit_type_val) if limit_type_val else None,
                input_digest=input_digest,
                target_digest=target_digest,
                message_count=len(messages),
                event_count=len(event_rows),
            )
        )
        scores.extend(_score_facts(job_id, trial_id, sample))

        seen_event_ids: set[str] = set()
        for event_index, event in enumerate(event_rows):
            event_bytes = _canonical_bytes(event)
            event_type = str(
                event.get("event") or event.get("event_type") or event.get("type") or "unknown"
            )
            event_id = _stable_id(trial_id, str(event_index), _digest_bytes(event_bytes))
            if event_id in seen_event_ids:
                raise ValueError(f"Duplicate event_id collision: {event_id} in trial {trial_id}")
            seen_event_ids.add(event_id)

            inspect_events.append(
                InspectEventFactV1(
                    job_id=job_id,
                    trial_id=trial_id,
                    event_id=event_id,
                    event_index=event_index,
                    event_type=event_type,
                    timestamp=str(event["timestamp"]) if event.get("timestamp") else None,
                    payload_size_bytes=len(event_bytes),
                    payload_digest=_digest_bytes(event_bytes),
                    span_id=str(event["span_id"]) if event.get("span_id") else None,
                )
            )

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
            raw_calls = message.get("tool_calls")
            message_tool_calls = [_mapping(call) for call in _sequence(raw_calls)]
            is_observation = role in {"tool", "observation"}

            # Resolve attachments in all message contents
            resolved_content = _resolve_content_blocks(
                message.get("content"),
                sample_attachments,
                log_attachments,
                attachment_tracker,
            )

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
                function_entry = call.get("function")
                if isinstance(function_entry, str):
                    name = function_entry
                    arguments = call.get("arguments", {})
                elif isinstance(function_entry, dict):
                    name = function_entry.get("name") or call.get("name")
                    arguments = function_entry.get("arguments") or call.get("arguments", {})
                else:
                    name = call.get("name") or call.get("function_name")
                    arguments = call.get("arguments", {})

                call_id = str(call.get("id") or call.get("tool_call_id") or f"call_{call_index}")
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
                        arguments_sha256=_digest_json(_plain(arguments)),
                    )
                )

            if is_observation:
                content_bytes = resolved_content.encode()
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
                        command_exit_code=_optional_int(message.get("exit_code"))
                        or _optional_int(_mapping(message.get("metadata")).get("exit_code")),
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

    # Update resolved counts in attachment facts
    updated_attachment_facts = [
        att.model_copy(update={"resolved_count": attachment_tracker.get(att.attachment_id, 0)})
        for att in attachment_facts
    ]

    trajectory_projection = TrialTrajectoryProjection(
        trajectories=tuple(trajectories),
        steps=tuple(steps),
        tool_calls=tuple(tool_calls),
        observations=tuple(observations),
    )

    run_fact_prelim = InspectRunFactV1(
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
        eval_spec_digest=eval_spec_digest,
        plan_name=plan_name,
    )

    reconcile_inspect_projection(
        run_fact_prelim,
        tuple(attempts),
        tuple(scores),
        tuple(inspect_events),
        tuple(updated_attachment_facts),
        trajectory_projection,
    )

    rebuild_digest = compute_rebuild_digest(
        run_fact_prelim,
        tuple(attempts),
        tuple(scores),
        tuple(inspect_events),
        tuple(updated_attachment_facts),
        trajectory_projection,
    )

    run_fact = run_fact_prelim.model_copy(update={"rebuild_digest": rebuild_digest})

    return InspectProjection(
        run=run_fact,
        attempts=tuple(attempts),
        scores=tuple(scores),
        events=tuple(inspect_events),
        attachments=tuple(updated_attachment_facts),
        trajectories=trajectory_projection,
        rebuild_digest=rebuild_digest,
    )


# --------------------------------------------------------------------------- #
# Re-exported Storage Operations (Backward Compatibility)
# --------------------------------------------------------------------------- #


def write_inspect_projection(
    projection: InspectProjection,
    output_root: Path,
    *,
    write_manifest: bool = True,
    source_file: str | None = None,
    source_bytes_size: int | None = None,
) -> dict[str, Path]:
    """Write Inspect-native and canonical facts into one partitioned Parquet root."""
    from evallab.storage.inspect_storage import (
        write_inspect_projection as _write_inspect_projection,
    )

    return _write_inspect_projection(
        projection,
        output_root,
        write_manifest=write_manifest,
        source_file=source_file,
        source_bytes_size=source_bytes_size,
    )


def ingest_inspect_eval_log(
    path: Path,
    *,
    output_root: Path,
    store_root: Path | None = None,
) -> InspectIngestResult:
    """Read, optionally archive, normalize, and project one Inspect evaluation log."""
    from evallab.storage.inspect_storage import (
        ingest_inspect_eval_log as _ingest_inspect_eval_log,
    )

    return _ingest_inspect_eval_log(
        path,
        output_root=output_root,
        store_root=store_root,
    )
