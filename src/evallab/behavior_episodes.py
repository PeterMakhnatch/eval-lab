"""Canonical ATIF behavior episodes, conservative detectors, and storage."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from evallab.evidence.event_mart import EventMartProjection
from evallab.paths import derived_root_from_environment

EpisodeStatus = Literal["candidate", "reviewed", "confirmed", "rejected"]
AnnotatorKind = Literal["code", "model", "human"]
EvidenceRelevance = Literal["relevant", "irrelevant", "unknown"]
ActionIntent = Literal["mutation", "verification", "wait", "poll", "other", "unknown"]
StateEvidenceStatus = Literal["present", "none", "unknown"]
Outcome = Literal["success", "error", "unknown"]
Confidence = Literal["low", "medium", "high"]
_SHA256 = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{64}$")
_ACTION_INTENTS: dict[str, ActionIntent] = {
    "mutation": "mutation",
    "verification": "verification",
    "wait": "wait",
    "poll": "poll",
    "other": "other",
    "unknown": "unknown",
}


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


class BehaviorAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    step_id: int
    action_id: str
    span_id: str | None
    function_name: str
    action_family: str
    arguments_sha256: str
    observation_sha256: str | None
    outcome: Outcome
    exit_code: int | None
    intent: ActionIntent = "unknown"
    task_relevance: EvidenceRelevance = "unknown"
    evidence_ids: tuple[str, ...] = ()
    state_evidence_ids: tuple[str, ...] = ()
    state_evidence_status: StateEvidenceStatus = "unknown"

    @field_validator("action_id", "function_name", "action_family", "arguments_sha256")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        if not str(value).strip():
            raise ValueError("action identity fields must be non-empty")
        return str(value)

    @field_validator("arguments_sha256", "observation_sha256")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _SHA256.fullmatch(str(value)):
            raise ValueError("digest must be sha256:<64 hex> or 64 hex")
        return (
            str(value).lower()
            if str(value).startswith("sha256:")
            else f"sha256:{str(value).lower()}"
        )

    @field_validator("evidence_ids", "state_evidence_ids")
    @classmethod
    def _ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not str(item).strip() for item in value):
            raise ValueError("evidence IDs must be non-empty")
        return tuple(str(item) for item in value)

    @model_validator(mode="after")
    def _state_status(self) -> BehaviorAction:
        if self.state_evidence_status == "present" and not self.state_evidence_ids:
            raise ValueError("present state evidence requires IDs")
        if self.state_evidence_status == "none" and self.state_evidence_ids:
            raise ValueError("none state evidence cannot have IDs")
        return self


class BehaviorDetectionContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    trial_id: str
    document_id: str
    trajectory_id: str | None = None
    session_id: str | None = None
    source_sha256: str
    observed_at: datetime
    catalog_version: str = "behavior-catalog/v1"

    @field_validator("trial_id", "document_id", "source_sha256")
    @classmethod
    def _required_text(cls, value: str) -> str:
        if not str(value).strip():
            raise ValueError("context identity fields must be non-empty")
        return str(value)

    @field_validator("source_sha256")
    @classmethod
    def _source_digest(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("source_sha256 must be a sha256 digest")
        return value.lower() if value.startswith("sha256:") else f"sha256:{value.lower()}"


class BehaviorEpisode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: int = 1
    episode_id: str
    trial_id: str
    document_id: str
    trajectory_id: str | None = None
    session_id: str | None = None
    start_step: int
    end_step: int
    label: str
    status: EpisodeStatus = "candidate"
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence: Confidence | None = None
    evidence_step_ids: tuple[int, ...]
    evidence_span_ids: tuple[str, ...] = ()
    annotator_kind: AnnotatorKind
    annotator_id: str
    detector_version: str | None = None
    rubric_version: str | None = None
    catalog_version: str = "behavior-catalog/v1"
    source_sha256: str
    input_digest: str
    rationale: str
    provenance: Mapping[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    reviewed_at: datetime | None = None

    @field_validator("episode_id", "trial_id", "document_id", "label", "annotator_id", "rationale")
    @classmethod
    def _text(cls, value: str) -> str:
        if not str(value).strip():
            raise ValueError("episode text fields must be non-empty")
        return str(value)

    @field_validator("episode_id", "source_sha256", "input_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("digest must be a sha256 digest")
        return value.lower() if value.startswith("sha256:") else f"sha256:{value.lower()}"

    @field_validator("evidence_step_ids")
    @classmethod
    def _steps(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value or any(int(item) < 0 for item in value):
            raise ValueError("evidence_step_ids must be non-empty non-negative integers")
        return tuple(int(item) for item in value)

    @field_validator("evidence_span_ids")
    @classmethod
    def _spans(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not str(item).strip() for item in value):
            raise ValueError("evidence span IDs must be non-empty")
        return tuple(str(item) for item in value)

    @field_validator("provenance", mode="after")
    @classmethod
    def _provenance(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return _freeze_json(value)

    @field_serializer("provenance")
    def _serialize_provenance(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return _thaw_json(value)

    @model_validator(mode="after")
    def _bounds_and_provenance(self) -> BehaviorEpisode:
        if self.schema_version != 1:
            raise ValueError("unsupported behavior episode schema version")
        if self.end_step < self.start_step:
            raise ValueError("end_step must be >= start_step")
        if any(step < self.start_step or step > self.end_step for step in self.evidence_step_ids):
            raise ValueError("evidence steps must lie within episode bounds")
        if self.annotator_kind == "code" and not self.detector_version:
            raise ValueError("code provenance requires detector_version")
        if self.annotator_kind == "model" and not (self.detector_version and self.rubric_version):
            raise ValueError("model provenance requires detector_version and rubric_version")
        if self.status in {"reviewed", "confirmed", "rejected"} and self.reviewed_at is None:
            raise ValueError("reviewed statuses require reviewed_at")
        if self.reviewed_at is not None and self.reviewed_at < self.created_at:
            raise ValueError("reviewed_at cannot precede created_at")
        return self


class DetectionUnknown(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    behavior: str
    start_step: int | None = None
    end_step: int | None = None
    reason: str
    evidence_step_ids: tuple[int, ...] = ()


class BehaviorDetectionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    episodes: tuple[BehaviorEpisode, ...] = ()
    unknowns: tuple[DetectionUnknown, ...] = ()


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def deterministic_episode_id(
    trial_id: str,
    document_id: str,
    start_step: int,
    end_step: int,
    label: str,
    evidence_step_ids: Sequence[int] = (),
    evidence_span_ids: Sequence[str] = (),
    detector_version: str | None = None,
    rubric_version: str | None = None,
    catalog_version: str = "behavior-catalog/v1",
    annotator_kind: AnnotatorKind = "code",
    annotator_id: str = "detector",
    trajectory_id: str | None = None,
    session_id: str | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> str:
    """Hash immutable identity; mutable review status/timestamps are excluded."""
    identity = {
        "schema_version": 1,
        "trial_id": trial_id,
        "document_id": document_id,
        "trajectory_id": trajectory_id,
        "session_id": session_id,
        "start_step": int(start_step),
        "end_step": int(end_step),
        "label": label,
        "evidence_step_ids": [int(item) for item in evidence_step_ids],
        "evidence_span_ids": [str(item) for item in evidence_span_ids],
        "detector_version": detector_version,
        "rubric_version": rubric_version,
        "catalog_version": catalog_version,
        "annotator_kind": annotator_kind,
        "annotator_id": annotator_id,
        "provenance": _thaw_json(provenance or {}),
    }
    return _canonical_digest(identity)


def _intent(name: str, family: str) -> ActionIntent:
    tokens = set(re.findall(r"[a-z0-9]+", f"{name} {family}".lower()))
    if tokens & {
        "create",
        "delete",
        "edit",
        "insert",
        "move",
        "patch",
        "remove",
        "replace",
        "set",
        "update",
        "write",
    }:
        return "mutation"
    if tokens & {"wait", "sleep", "await"}:
        return "wait"
    if tokens & {"poll", "polling"}:
        return "poll"
    if tokens & {"verify", "verification", "assert", "pytest"}:
        return "verification"
    return "unknown"


def _normalized_intent(value: Any, name: str, family: str) -> ActionIntent:
    if isinstance(value, str) and value in _ACTION_INTENTS:
        return _ACTION_INTENTS[value]
    return _intent(name, family)


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    return tuple(str(item) for item in value if str(item))


def normalize_behavior_actions(event_mart: EventMartProjection) -> tuple[BehaviorAction, ...]:
    """Normalize actions; temporal action_effects are never causal evidence."""
    normalized: list[BehaviorAction] = []
    for row in event_mart.agent_actions:
        outcome = str(row.get("outcome") or "unknown").lower()
        if outcome not in {"success", "error", "unknown"}:
            outcome = "unknown"
        relevance = str(row.get("task_relevance") or "unknown").lower()
        if relevance not in {"relevant", "irrelevant", "unknown"}:
            relevance = "unknown"
        evidence_ids = _string_tuple(row.get("evidence_ids"))
        state_ids = _string_tuple(row.get("state_evidence_ids"))
        status = str(row.get("state_evidence_status") or ("present" if state_ids else "unknown"))
        if status not in {"present", "none", "unknown"}:
            status = "unknown"
        normalized.append(
            BehaviorAction(
                step_id=int(row["step_id"]),
                action_id=str(row["action_id"]),
                span_id=str(row["span_id"]) if row.get("span_id") else None,
                function_name=str(row.get("function_name") or "unknown"),
                action_family=str(row.get("action_family") or "other"),
                arguments_sha256=str(row.get("arguments_sha256") or _canonical_digest("")),
                observation_sha256=row.get("observation_sha256"),
                outcome=outcome,
                exit_code=row.get("exit_code"),
                intent=_normalized_intent(
                    row.get("intent"),
                    str(row.get("function_name") or ""),
                    str(row.get("action_family") or ""),
                ),
                task_relevance=relevance,
                evidence_ids=evidence_ids,
                state_evidence_ids=state_ids,
                state_evidence_status=status,
            )
        )
    return tuple(sorted(normalized, key=lambda item: (item.step_id, item.action_id)))


def _input_digest(context: BehaviorDetectionContext, actions: Sequence[BehaviorAction]) -> str:
    return _canonical_digest(
        {
            "context": context.model_dump(mode="json"),
            "actions": [item.model_dump(mode="json") for item in actions],
        }
    )


def _episode(
    context: BehaviorDetectionContext,
    actions: Sequence[BehaviorAction],
    *,
    start: int,
    end: int,
    label: str,
    detector_version: str,
    evidence: Sequence[BehaviorAction],
    rationale: str,
    score: float | None = None,
    confidence: Literal["low", "medium", "high"] | None = "medium",
) -> BehaviorEpisode:
    now = context.observed_at
    steps = tuple(item.step_id for item in evidence)
    spans = tuple(item.span_id for item in evidence if item.span_id)
    provenance = {"detector": detector_version}
    episode_id = deterministic_episode_id(
        context.trial_id,
        context.document_id,
        start,
        end,
        label,
        steps,
        spans,
        detector_version,
        None,
        context.catalog_version,
        "code",
        detector_version,
        context.trajectory_id,
        context.session_id,
        provenance,
    )
    return BehaviorEpisode(
        episode_id=episode_id,
        trial_id=context.trial_id,
        document_id=context.document_id,
        trajectory_id=context.trajectory_id,
        session_id=context.session_id,
        start_step=start,
        end_step=end,
        label=label,
        evidence_step_ids=steps,
        evidence_span_ids=spans,
        annotator_kind="code",
        annotator_id=detector_version,
        detector_version=detector_version,
        catalog_version=context.catalog_version,
        source_sha256=context.source_sha256,
        input_digest=_input_digest(context, actions),
        rationale=rationale,
        provenance=provenance,
        score=score,
        confidence=confidence,
        created_at=now,
        updated_at=now,
    )


def _episode_id(item: BehaviorEpisode) -> str:
    return item.episode_id


def detect_behavior_episodes(
    context: BehaviorDetectionContext,
    actions: Sequence[BehaviorAction],
    *,
    detector_version: str = "behavior-detectors/v1",
) -> BehaviorDetectionResult:
    ordered = tuple(sorted(actions, key=lambda item: (item.step_id, item.action_id)))
    episodes: list[BehaviorEpisode] = []
    unknowns: list[DetectionUnknown] = []
    errors: list[BehaviorAction] = []
    for action in ordered:
        if action.observation_sha256 is None or action.task_relevance == "unknown":
            unknowns.append(
                DetectionUnknown(
                    behavior="recovered_progress",
                    start_step=action.step_id,
                    end_step=action.step_id,
                    reason=(
                        "Missing observation or explicit task relevance prevents "
                        "conservative adjudication."
                    ),
                    evidence_step_ids=(action.step_id,),
                )
            )
        observed_error = (
            action.outcome == "error" or (action.exit_code is not None and action.exit_code != 0)
        ) and action.observation_sha256 is not None
        if observed_error:
            errors.append(action)
            episodes.append(
                _episode(
                    context,
                    ordered,
                    start=action.step_id,
                    end=action.step_id,
                    label="tool_error",
                    detector_version=detector_version,
                    evidence=(action,),
                    rationale="Observed error outcome or nonzero tool exit with an observation.",
                    score=1.0,
                    confidence="high",
                )
            )
        elif action.outcome == "error" or (action.exit_code is not None and action.exit_code != 0):
            unknowns.append(
                DetectionUnknown(
                    behavior="tool_error",
                    start_step=action.step_id,
                    end_step=action.step_id,
                    reason="Error outcome lacks an observed observation digest.",
                    evidence_step_ids=(action.step_id,),
                )
            )
    for error in errors:
        later = [item for item in ordered if item.step_id > error.step_id]
        eligible_retries = [
            item
            for item in later
            if item.intent not in {"wait", "poll", "verification"}
            and item.function_name == error.function_name
        ]
        retry = eligible_retries[0] if eligible_retries else None
        if retry is not None and retry.arguments_sha256 == error.arguments_sha256:
            episodes.append(
                _episode(
                    context,
                    ordered,
                    start=error.step_id,
                    end=retry.step_id,
                    label="unchanged_retry",
                    detector_version=detector_version,
                    evidence=(error, retry),
                    rationale="Same normalized function and arguments followed an observed error.",
                    score=1.0,
                    confidence="high",
                )
            )
        recovered = None
        missing_evidence = False
        for candidate in later:
            if candidate.outcome != "success" or candidate.observation_sha256 is None:
                continue
            if (
                candidate.function_name == error.function_name
                and candidate.arguments_sha256 == error.arguments_sha256
            ):
                continue
            if candidate.task_relevance != "relevant":
                if candidate.task_relevance == "unknown":
                    missing_evidence = True
                continue
            new_evidence = set(candidate.evidence_ids) - set(error.evidence_ids)
            new_state = candidate.state_evidence_status == "present" and (
                set(candidate.state_evidence_ids) - set(error.state_evidence_ids)
            )
            if not (new_evidence or new_state):
                missing_evidence = True
                continue
            recovered = candidate
            break
        if recovered is not None:
            episodes.append(
                _episode(
                    context,
                    ordered,
                    start=error.step_id,
                    end=recovered.step_id,
                    label="recovered_progress",
                    detector_version=detector_version,
                    evidence=(error, recovered),
                    rationale="Changed strategy succeeded with explicit relevant new evidence.",
                    score=1.0,
                    confidence="high",
                )
            )
        else:
            end = later[-1].step_id if later else error.step_id
            episodes.append(
                _episode(
                    context,
                    ordered,
                    start=error.step_id,
                    end=end,
                    label="unresolved_error",
                    detector_version=detector_version,
                    evidence=(error,),
                    rationale="Observed error had no adjudicated changed-strategy recovery.",
                    confidence="low",
                )
            )
            if missing_evidence or any(item.task_relevance == "unknown" for item in later):
                unknowns.append(
                    DetectionUnknown(
                        behavior="recovered_progress",
                        start_step=error.step_id,
                        end_step=end,
                        reason=(
                            "Missing task relevance or new evidence prevents recovery adjudication."
                        ),
                        evidence_step_ids=(error.step_id,),
                    )
                )
    mutations = [item for item in ordered if item.intent == "mutation"]
    if mutations:
        final = mutations[-1]
        later = [
            item
            for item in ordered
            if item.step_id > final.step_id and item.intent == "verification"
        ]
        successful = next(
            (
                item
                for item in later
                if item.outcome == "success" and item.observation_sha256 and item.evidence_ids
            ),
            None,
        )
        if successful is None:
            if (
                any(item.outcome == "unknown" or item.observation_sha256 is None for item in later)
                or final.outcome == "unknown"
                or final.observation_sha256 is None
            ):
                unknowns.append(
                    DetectionUnknown(
                        behavior="verification_gap",
                        start_step=final.step_id,
                        end_step=later[-1].step_id if later else final.step_id,
                        reason="Mutation or verification was not observably complete.",
                        evidence_step_ids=(final.step_id,),
                    )
                )
            else:
                episodes.append(
                    _episode(
                        context,
                        ordered,
                        start=final.step_id,
                        end=final.step_id,
                        label="verification_gap",
                        detector_version=detector_version,
                        evidence=(final,),
                        rationale=(
                            "Final explicit mutation has no later successful explicit verification."
                        ),
                        score=1.0,
                        confidence="medium",
                    )
                )
    return BehaviorDetectionResult(
        episodes=tuple(sorted(episodes, key=_episode_id)), unknowns=tuple(unknowns)
    )


def detect_effect_loop_candidates(
    context: BehaviorDetectionContext,
    actions: Sequence[BehaviorAction],
    *,
    detector_version: str = "effect-loop-candidate/v1",
) -> BehaviorDetectionResult:
    ordered = tuple(sorted(actions, key=lambda item: (item.step_id, item.action_id)))
    episodes: list[BehaviorEpisode] = []
    unknowns: list[DetectionUnknown] = []
    eligible = [item for item in ordered if item.intent not in {"wait", "poll", "verification"}]
    repeated = False
    insufficient = False
    for index, first in enumerate(eligible):
        for second in eligible[index + 1 :]:
            if (
                first.function_name != second.function_name
                or first.arguments_sha256 != second.arguments_sha256
            ):
                continue
            repeated = True
            if (
                first.observation_sha256 is None
                or second.observation_sha256 is None
                or first.observation_sha256 != second.observation_sha256
            ):
                insufficient = True
                continue
            interval = [item for item in ordered if first.step_id <= item.step_id <= second.step_id]
            if any(
                item.state_evidence_status == "present" or item.state_evidence_ids
                for item in interval
            ):
                continue
            if any(item.state_evidence_status == "unknown" for item in interval):
                insufficient = True
                continue
            episodes.append(
                _episode(
                    context,
                    ordered,
                    start=first.step_id,
                    end=second.step_id,
                    label="effect_loop_candidate",
                    detector_version=detector_version,
                    evidence=(first, second),
                    rationale=(
                        "Repeated same action had equivalent observations and "
                        "explicitly no state evidence."
                    ),
                    score=1.0,
                    confidence="medium",
                )
            )
            break
    if not episodes and repeated and insufficient:
        unknowns.append(
            DetectionUnknown(
                behavior="effect_loop_candidate",
                reason=(
                    "Repeated action lacks sufficient observations or complete no-state coverage."
                ),
            )
        )
    return BehaviorDetectionResult(
        episodes=tuple(sorted(episodes, key=_episode_id)), unknowns=tuple(unknowns)
    )


_BEHAVIOR_EPISODE_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.int64(), nullable=False),
        pa.field("episode_id", pa.string(), nullable=False),
        pa.field("trial_id", pa.string(), nullable=False),
        pa.field("document_id", pa.string(), nullable=False),
        pa.field("trajectory_id", pa.string()),
        pa.field("session_id", pa.string()),
        pa.field("start_step", pa.int64(), nullable=False),
        pa.field("end_step", pa.int64(), nullable=False),
        pa.field("label", pa.string(), nullable=False),
        pa.field("status", pa.string(), nullable=False),
        pa.field("score", pa.float64()),
        pa.field("confidence", pa.string()),
        pa.field("evidence_step_ids", pa.list_(pa.int64()), nullable=False),
        pa.field("evidence_span_ids", pa.list_(pa.string()), nullable=False),
        pa.field("annotator_kind", pa.string(), nullable=False),
        pa.field("annotator_id", pa.string(), nullable=False),
        pa.field("detector_version", pa.string()),
        pa.field("rubric_version", pa.string()),
        pa.field("catalog_version", pa.string(), nullable=False),
        pa.field("source_sha256", pa.string(), nullable=False),
        pa.field("input_digest", pa.string(), nullable=False),
        pa.field("rationale", pa.string(), nullable=False),
        pa.field("provenance_json", pa.string(), nullable=False),
        pa.field("created_at", pa.string(), nullable=False),
        pa.field("updated_at", pa.string(), nullable=False),
        pa.field("reviewed_at", pa.string()),
    ]
)


def _row(episode: BehaviorEpisode) -> dict[str, Any]:
    data = episode.model_dump(mode="python", exclude={"provenance"})
    data.update(
        evidence_step_ids=list(episode.evidence_step_ids),
        evidence_span_ids=list(episode.evidence_span_ids),
        provenance_json=json.dumps(
            _thaw_json(episode.provenance), sort_keys=True, separators=(",", ":")
        ),
        created_at=episode.created_at.isoformat(),
        updated_at=episode.updated_at.isoformat(),
        reviewed_at=episode.reviewed_at.isoformat() if episode.reviewed_at else None,
    )
    return data


def _from_row(row: Mapping[str, Any]) -> BehaviorEpisode:
    data = dict(row)
    data["provenance"] = json.loads(str(data.pop("provenance_json") or "{}"))
    for key in ("created_at", "updated_at", "reviewed_at"):
        if data.get(key):
            data[key] = datetime.fromisoformat(str(data[key]))
    return BehaviorEpisode.model_validate(data)


def _episodes_path(repo_root: Path | None, derived_root: Path | None) -> Path:
    root = (repo_root or Path.cwd()).resolve()
    derived = (
        derived_root.resolve() if derived_root is not None else derived_root_from_environment(root)
    )
    return derived / "behavior_episodes" / "behavior_episodes.parquet"


@contextmanager
def _episodes_lock(path: Path, *, exclusive: bool):
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / ".behavior_episodes.lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_unlocked(path: Path) -> list[BehaviorEpisode]:
    if not path.is_file():
        return []
    return sorted(
        (_from_row(row) for row in pq.read_table(path).to_pylist()),
        key=lambda item: item.episode_id,
    )


def load_behavior_episodes(
    repo_root: Path | None = None, derived_root: Path | None = None
) -> list[BehaviorEpisode]:
    path = _episodes_path(repo_root, derived_root)
    with _episodes_lock(path, exclusive=False):
        return _load_unlocked(path)


def _write_unlocked(path: Path, episodes: Sequence[BehaviorEpisode]) -> None:
    table = pa.Table.from_pylist(
        [_row(item) for item in sorted(episodes, key=lambda item: item.episode_id)],
        schema=_BEHAVIOR_EPISODE_SCHEMA,
    )
    with tempfile.NamedTemporaryFile(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
    ) as staged:
        temporary = Path(staged.name)
    try:
        pq.write_table(
            table, temporary, compression="zstd", use_dictionary=False, write_statistics=True
        )
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


_MUTABLE_REVIEW_FIELDS = {"status", "score", "confidence", "rationale", "updated_at", "reviewed_at"}


def persist_behavior_episodes(
    episodes: Iterable[BehaviorEpisode],
    *,
    repo_root: Path | None = None,
    derived_root: Path | None = None,
) -> tuple[BehaviorEpisode, ...]:
    incoming: dict[str, BehaviorEpisode] = {}
    for episode in episodes:
        prior = incoming.get(episode.episode_id)
        if prior is not None:
            immutable = set(type(prior).model_fields) - _MUTABLE_REVIEW_FIELDS
            if any(getattr(prior, field) != getattr(episode, field) for field in immutable):
                raise ValueError(f"immutable episode conflict: {episode.episode_id}")
        incoming[episode.episode_id] = episode
    path = _episodes_path(repo_root, derived_root)
    with _episodes_lock(path, exclusive=True):
        loaded = _load_unlocked(path)
        existing = {item.episode_id: item for item in loaded}
        for episode_id, episode in incoming.items():
            previous = existing.get(episode_id)
            if previous is not None:
                immutable = set(type(previous).model_fields) - _MUTABLE_REVIEW_FIELDS
                if any(getattr(previous, field) != getattr(episode, field) for field in immutable):
                    raise ValueError(f"immutable episode conflict: {episode_id}")
            existing[episode_id] = episode
        updated = tuple(sorted(existing.values(), key=lambda item: item.episode_id))
        if loaded != list(updated):
            _write_unlocked(path, updated)
        return tuple(incoming[item] for item in sorted(incoming))
