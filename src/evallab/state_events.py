"""Append-only projection of state-journal producer events.

Sequence establishes temporal precedence within a trial. It is observation
order only: these facts do not claim that any agent action caused a state event.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from evallab.results import TrialRecord
from evallab.schemas import StateEventMetadata, StateJournalEvent

PRODUCER = "evallab-state-journal"
FACT_SCHEMA_VERSION = "state-event-fact-v1"
TEMPORAL_SEMANTICS = "sequence_precedence_non_causal"
KNOWN_OPERATIONS = frozenset(
    {
        "modify",
        "attrib",
        "close_write",
        "moved_from",
        "moved_to",
        "create",
        "delete",
        "delete_self",
        "move_self",
        "unmount",
        "queue_overflow",
        "ignored",
    }
)


class StateEventValidationError(ValueError):
    """The event stream cannot be projected without inventing evidence."""


@dataclass(frozen=True)
class StateEventFact:
    experiment_id: str | None
    job_id: str
    trial_id: str
    sequence: int
    precedence: int
    predecessor_sequence: int | None
    event_at: str | None
    operations: tuple[str, ...]
    path: str | None
    is_directory: bool | None
    cookie: int | None
    before_state_digest: str | None
    after_state_digest: str | None
    before_content_sha256: str | None
    after_content_sha256: str | None
    before_size_bytes: int | None
    after_size_bytes: int | None
    producer: str
    producer_schema_version: int | None
    fact_schema_version: str
    source_digest: str
    source_record_digest: str | None
    temporal_semantics: str
    evidence_status: str
    invalid_reason: str | None
    invalid_error_digest: str | None


def _sha256(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _metadata_digest(value: StateEventMetadata | None) -> str | None:
    if value is None:
        return None
    payload = json.dumps(
        value.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(payload)


def _validate_timestamp(value: str, *, line_number: int) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StateEventValidationError(
            f"state-events.jsonl line {line_number}: timestamp is not ISO-8601"
        ) from exc
    if parsed.tzinfo is None:
        raise StateEventValidationError(
            f"state-events.jsonl line {line_number}: timestamp must include an offset"
        )


def _producer_status(journal_dir: Path) -> tuple[int, str]:
    status_path = journal_dir / "status.json"
    try:
        payload: Any = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateEventValidationError(
            f"state-journal producer status is unreadable: {type(exc).__name__}"
        ) from exc
    if not isinstance(payload, dict):
        raise StateEventValidationError("state-journal producer status must be an object")
    version = payload.get("schema_version")
    if type(version) is not int or version != 1:
        raise StateEventValidationError(
            f"unsupported state-journal producer schema_version: {version!r}"
        )
    status = payload.get("status")
    if not isinstance(status, str) or not status:
        raise StateEventValidationError("state-journal producer status is missing")
    return version, status


def _initial_states(journal_dir: Path) -> dict[str, StateEventMetadata | None]:
    diff_path = journal_dir / "state-diff.json"
    if not diff_path.is_file():
        return {}
    try:
        payload: Any = json.loads(diff_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or not isinstance(payload.get("changes"), list):
        return {}
    states: dict[str, StateEventMetadata | None] = {}
    for change in payload["changes"]:
        if not isinstance(change, dict) or not isinstance(change.get("path"), str):
            continue
        before = change.get("before")
        if before is None:
            states[change["path"]] = None
            continue
        try:
            states[change["path"]] = StateEventMetadata.model_validate(before)
        except ValidationError:
            continue
    return states


def invalid_state_event_fact(
    trial: TrialRecord,
    *,
    job_id: str,
    experiment_id: str | None,
    error: StateEventValidationError,
) -> StateEventFact:
    """Create one deterministic invalid sentinel without hiding sibling facts."""

    source_path = trial.path / "state-journal/state-events.jsonl"
    try:
        source_digest = _sha256(source_path.read_bytes())
    except OSError:
        source_digest = _sha256(b"")
    reason = str(error)
    try:
        producer_version, _ = _producer_status(trial.path / "state-journal")
    except StateEventValidationError:
        producer_version = None
    return StateEventFact(
        experiment_id=experiment_id,
        job_id=str(job_id),
        trial_id=str(trial.id),
        sequence=0,
        precedence=0,
        predecessor_sequence=None,
        event_at=None,
        operations=(),
        path=None,
        is_directory=None,
        cookie=None,
        before_state_digest=None,
        after_state_digest=None,
        before_content_sha256=None,
        after_content_sha256=None,
        before_size_bytes=None,
        after_size_bytes=None,
        producer=PRODUCER,
        producer_schema_version=producer_version,
        fact_schema_version=FACT_SCHEMA_VERSION,
        source_digest=source_digest,
        source_record_digest=None,
        temporal_semantics=TEMPORAL_SEMANTICS,
        evidence_status="invalid",
        invalid_reason=reason,
        invalid_error_digest=_sha256(reason.encode("utf-8")),
    )


def load_state_event_facts(
    trial: TrialRecord,
    *,
    job_id: str,
    experiment_id: str | None,
) -> tuple[StateEventFact, ...]:
    """Project every producer record, failing closed on ambiguous evidence."""

    journal_dir = trial.path / "state-journal"
    source_path = journal_dir / "state-events.jsonl"
    status_path = journal_dir / "status.json"
    if not status_path.is_file() and not source_path.is_file():
        return ()
    producer_schema_version, producer_status = _producer_status(journal_dir)
    if producer_status in {"unavailable", "disabled"}:
        return ()
    if not source_path.is_file():
        raise StateEventValidationError(
            f"state-events.jsonl missing while producer status is {producer_status!r}"
        )
    try:
        source_bytes = source_path.read_bytes()
        source_text = source_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise StateEventValidationError(
            f"state-events.jsonl is unreadable: {type(exc).__name__}"
        ) from exc

    source_digest = _sha256(source_bytes)
    parsed_events: list[tuple[StateJournalEvent, str]] = []
    seen_sequences: dict[int, str] = {}
    previous_sequence = 0
    for line_number, line in enumerate(source_text.splitlines(), start=1):
        if not line.strip():
            raise StateEventValidationError(
                f"state-events.jsonl line {line_number}: blank records are invalid"
            )
        record_digest = _sha256(line.encode("utf-8"))
        try:
            payload = json.loads(line)
            event = StateJournalEvent.model_validate(payload)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise StateEventValidationError(
                f"state-events.jsonl line {line_number}: malformed event"
            ) from exc
        _validate_timestamp(event.timestamp, line_number=line_number)
        if (
            any(
                operation not in KNOWN_OPERATIONS or len(operation) > 64
                for operation in event.operations
            )
            or len(set(event.operations)) != len(event.operations)
        ):
            raise StateEventValidationError(
                f"state-events.jsonl line {line_number}: operations are invalid"
            )
        if event.state is not None and event.state.path != event.path:
            raise StateEventValidationError(
                f"state-events.jsonl line {line_number}: state path conflicts with event path"
            )
        if event.state is not None and (
            event.is_directory != (event.state.type == "directory")
        ):
            raise StateEventValidationError(
                f"state-events.jsonl line {line_number}: state type conflicts with event kind"
            )
        if event.sequence in seen_sequences:
            kind = (
                "duplicate" if seen_sequences[event.sequence] == record_digest else "conflicting"
            )
            raise StateEventValidationError(
                f"state-events.jsonl line {line_number}: {kind} sequence {event.sequence}"
            )
        if event.sequence != previous_sequence + 1:
            raise StateEventValidationError(
                f"state-events.jsonl line {line_number}: sequence {event.sequence} "
                f"does not append after {previous_sequence}"
            )
        seen_sequences[event.sequence] = record_digest
        previous_sequence = event.sequence
        parsed_events.append((event, record_digest))

    facts: list[StateEventFact] = []
    state_by_path = _initial_states(journal_dir)
    absence_operations = {"delete", "delete_self", "moved_from"}
    for event, record_digest in parsed_events:
        before = state_by_path.get(event.path)
        after = event.state
        if event.state is not None:
            state_by_path[event.path] = event.state
        elif absence_operations.intersection(event.operations):
            state_by_path[event.path] = None
        facts.append(
            StateEventFact(
                experiment_id=experiment_id,
                job_id=str(job_id),
                trial_id=str(trial.id),
                sequence=event.sequence,
                precedence=event.sequence,
                predecessor_sequence=event.sequence - 1 if event.sequence > 1 else None,
                event_at=event.timestamp,
                operations=tuple(event.operations),
                path=event.path,
                is_directory=event.is_directory,
                cookie=event.cookie,
                before_state_digest=_metadata_digest(before),
                after_state_digest=_metadata_digest(after),
                before_content_sha256=before.sha256 if before else None,
                after_content_sha256=after.sha256 if after else None,
                before_size_bytes=before.size_bytes if before else None,
                after_size_bytes=after.size_bytes if after else None,
                producer=PRODUCER,
                producer_schema_version=producer_schema_version,
                fact_schema_version=FACT_SCHEMA_VERSION,
                source_digest=source_digest,
                source_record_digest=record_digest,
                temporal_semantics=TEMPORAL_SEMANTICS,
                evidence_status="valid",
                invalid_reason=None,
                invalid_error_digest=None,
            )
        )
    return tuple(facts)
