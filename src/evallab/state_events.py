"""Append-only projection of state-journal producer events.

Sequence establishes temporal precedence within a trial. It is observation
order only: these facts do not claim that any agent action caused a state event.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from evallab.results import TrialRecord
from evallab.schemas import StateEventMetadata, StateJournalEvent

PRODUCER = "evallab-state-journal"
FACT_SCHEMA_VERSION = "state-event-fact-v1"
TEMPORAL_SEMANTICS = "sequence_precedence_non_causal"
ALLOWED_CHANGE_TYPES = frozenset({"added", "modified", "deleted"})
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
    before_evidence_status: str
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

def _parse_iso_timestamp(value: str, *, context: str) -> datetime:
    """Parse ISO-8601 timestamp string and require an explicit timezone offset."""
    if not isinstance(value, str) or not value.strip():
        raise StateEventValidationError(f"{context}: timestamp must be a non-empty string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StateEventValidationError(f"{context}: timestamp is not ISO-8601: {value!r}") from exc
    if parsed.tzinfo is None:
        raise StateEventValidationError(f"{context}: timestamp must include an offset: {value!r}")
    return parsed


def _validate_timestamp(value: str, *, line_number: int) -> None:
    _parse_iso_timestamp(value, context=f"state-events.jsonl line {line_number}")

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

INT64_MIN = -9_223_372_036_854_775_808
INT64_MAX = 9_223_372_036_854_775_807


def _validate_nonnegative_int64(val: Any, *, name: str, context: str) -> int:
    """Validate that val is a non-boolean integer in [0, 2**63 - 1]."""
    if type(val) is not int or val < 0 or val > INT64_MAX:
        raise StateEventValidationError(
            f"{context}: {name} must be a non-negative int64 integer, got {val!r}"
        )
    return val


def _validate_signed_int64(val: Any, *, name: str, context: str) -> int:
    """Validate that val is a non-boolean integer in [-2**63, 2**63 - 1]."""
    if type(val) is not int or val < INT64_MIN or val > INT64_MAX:
        raise StateEventValidationError(
            f"{context}: {name} must be a signed int64 integer, got {val!r}"
        )
    return val


def validate_safe_relative_posix_path(path: Any, *, context: str = "path") -> str:
    """Ensure path is exact producer-canonical relative POSIX path or '.' without redundant tokens."""
    if not isinstance(path, str) or not path.strip():
        raise StateEventValidationError(f"{context}: path is invalid (must be a non-empty string)")
    if "\\" in path:
        raise StateEventValidationError(f"{context}: path is invalid (backslashes forbidden)")
    if path.startswith("/"):
        raise StateEventValidationError(f"{context}: path is invalid (absolute paths forbidden)")
    if path == ".":
        return "."
    if (
        path.startswith("./")
        or "//" in path
        or "/./" in path
        or path.endswith("/")
        or path == ".."
        or path.startswith("../")
        or "/../" in path
    ):
        raise StateEventValidationError(f"{context}: path is invalid (non-canonical or traversal)")
    normalized = posixpath.normpath(path)
    if normalized != path or normalized in ("", ".", "/"):
        raise StateEventValidationError(f"{context}: path is invalid (un-normalized POSIX path)")
    if normalized.startswith("../"):
        raise StateEventValidationError(f"{context}: path is invalid (path traversal forbidden)")
    return normalized


def validate_state_event_metadata(
    value: Any,
    *,
    expected_path: str,
    side: str,
    context: str = "state-diff.json change",
) -> StateEventMetadata:
    """Validate StateEventMetadata with strict=True, strict enclosing-path equality, signed-int64 bounds, and producer field exclusivity."""
    if not isinstance(value, dict):
        raise StateEventValidationError(f"{context}: {side} metadata is invalid (must be an object)")
    try:
        meta = StateEventMetadata.model_validate(value, strict=True)
    except ValidationError as exc:
        raise StateEventValidationError(f"{context}: {side} metadata is invalid") from exc

    try:
        norm_meta_path = validate_safe_relative_posix_path(meta.path, context=f"{context}: {side} path")
    except StateEventValidationError as exc:
        raise StateEventValidationError(f"{context}: {side} metadata is invalid") from exc

    if norm_meta_path != expected_path:
        raise StateEventValidationError(
            f"{context}: {side} path conflicts with change path"
        )

    # Signed-int64 bounds & non-boolean checks
    _validate_nonnegative_int64(value.get("size_bytes"), name="size_bytes", context=f"{context}: {side} metadata")
    _validate_signed_int64(value.get("mtime_ns"), name="mtime_ns", context=f"{context}: {side} metadata")

    # Producer field exclusivity rules
    if meta.type == "file":
        if meta.target is not None:
            raise StateEventValidationError(
                f"{context}: {side} metadata is invalid (regular file cannot have symlink target)"
            )
        if meta.hash_status == "complete":
            if not meta.sha256:
                raise StateEventValidationError(
                    f"{context}: {side} metadata is invalid (hash_status='complete' requires sha256)"
                )
        elif meta.hash_status in ("size_limit", "unreadable") and meta.sha256 is not None:
            raise StateEventValidationError(
                f"{context}: {side} metadata is invalid (hash_status={meta.hash_status!r} must not have sha256)"
            )
        elif meta.hash_status is None and meta.sha256 is not None:
            raise StateEventValidationError(
                f"{context}: {side} metadata is invalid (file without hash_status must not have sha256)"
            )
    elif meta.type in ("directory", "symlink", "other"):
        if meta.sha256 is not None:
            raise StateEventValidationError(
                f"{context}: {side} metadata is invalid (non-file {meta.type!r} must not have sha256)"
            )
        if meta.hash_status is not None:
            raise StateEventValidationError(
                f"{context}: {side} metadata is invalid (non-file {meta.type!r} must not have hash_status)"
            )
        if meta.type in ("directory", "other") and meta.target is not None:
            raise StateEventValidationError(
                f"{context}: {side} metadata is invalid ({meta.type} cannot have symlink target)"
            )

    return meta


@dataclass(frozen=True)
class StateDiffChange:
    """Canonical validated change record from state-diff.json."""

    path: str
    change_type: str
    before: StateEventMetadata | None
    after: StateEventMetadata | None
    event_count: int
    first_event_at: str | None = None
    last_event_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "change_type": self.change_type,
            "before": self.before.model_dump(mode="json", exclude_none=True) if self.before else None,
            "after": self.after.model_dump(mode="json", exclude_none=True) if self.after else None,
            "event_count": self.event_count,
            "first_event_at": self.first_event_at,
            "last_event_at": self.last_event_at,
        }
@dataclass(frozen=True)
class StateDiffDocument:
    """Canonical validated state-diff.json document matching producer.py schema v1."""

    schema_version: int
    status: Literal["available", "partial"] | str
    root: str
    before_captured_at: str
    after_captured_at: str
    change_count: int
    event_count: int
    dropped_event_count: int
    changes: tuple[StateDiffChange, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "root": self.root,
            "before_captured_at": self.before_captured_at,
            "after_captured_at": self.after_captured_at,
            "change_count": self.change_count,
            "event_count": self.event_count,
            "dropped_event_count": self.dropped_event_count,
            "changes": [c.to_dict() for c in self.changes],
        }

def validate_state_diff_payload(payload: Any) -> StateDiffDocument:
    """Validate a state-diff document against canonical producer schema v1 rules."""
    if not isinstance(payload, dict):
        raise StateEventValidationError(
            "state-diff.json must be a schema_version 1 object with changes"
        )

    schema_ver = payload.get("schema_version")
    if type(schema_ver) is not int or schema_ver != 1:
        raise StateEventValidationError(
            "state-diff.json must be a schema_version 1 object with changes"
        )
    schema_version = schema_ver

    raw_status = payload.get("status")
    if not isinstance(raw_status, str) or raw_status not in ("available", "partial"):
        raise StateEventValidationError(
            f"state-diff.json status must be 'available' or 'partial', got {raw_status!r}"
        )
    status = raw_status

    raw_root = payload.get("root")
    if not isinstance(raw_root, str) or not raw_root.strip():
        raise StateEventValidationError("state-diff.json root must be a non-empty string")
    root = raw_root.strip()

    before_cap = payload.get("before_captured_at")
    if not isinstance(before_cap, str) or not before_cap.strip():
        raise StateEventValidationError("state-diff.json before_captured_at is required")
    b_dt = _parse_iso_timestamp(before_cap, context="state-diff.json before_captured_at")

    after_cap = payload.get("after_captured_at")
    if not isinstance(after_cap, str) or not after_cap.strip():
        raise StateEventValidationError("state-diff.json after_captured_at is required")
    a_dt = _parse_iso_timestamp(after_cap, context="state-diff.json after_captured_at")

    if a_dt < b_dt:
        raise StateEventValidationError(
            f"state-diff.json after_captured_at ({after_cap}) precedes before_captured_at ({before_cap})"
        )

    doc_change_count = _validate_nonnegative_int64(
        payload.get("change_count"), name="change_count", context="state-diff.json"
    )
    doc_event_count = _validate_nonnegative_int64(
        payload.get("event_count"), name="event_count", context="state-diff.json"
    )

    doc_dropped = 0
    if "dropped_event_count" in payload:
        doc_dropped = _validate_nonnegative_int64(
            payload["dropped_event_count"], name="dropped_event_count", context="state-diff.json"
        )

    raw_changes = payload.get("changes")
    if not isinstance(raw_changes, list):
        raise StateEventValidationError(
            "state-diff.json must be a schema_version 1 object with changes"
        )

    validated_changes: list[StateDiffChange] = []
    seen_paths: set[str] = set()

    for index, change in enumerate(raw_changes):
        ctx = f"state-diff.json change {index}"
        if not isinstance(change, dict):
            raise StateEventValidationError(f"{ctx}: change must be an object")

        path_val = change.get("path")
        path = validate_safe_relative_posix_path(path_val, context=ctx)
        if path in seen_paths:
            raise StateEventValidationError(f"{ctx}: duplicate or conflicting path {path_val!r}")
        seen_paths.add(path)

        change_type = change.get("change_type")
        if not isinstance(change_type, str) or change_type not in ALLOWED_CHANGE_TYPES:
            raise StateEventValidationError(
                f"{ctx}: change_type {change_type!r} is not an allowed change type"
            )

        for side in ("before", "after"):
            if side not in change:
                raise StateEventValidationError(f"{ctx}: {side} evidence is missing")

        b_val = change["before"]
        a_val = change["after"]
        b_meta = (
            validate_state_event_metadata(b_val, expected_path=path, side="before", context=ctx)
            if b_val is not None
            else None
        )
        a_meta = (
            validate_state_event_metadata(a_val, expected_path=path, side="after", context=ctx)
            if a_val is not None
            else None
        )

        if change_type == "added":
            if b_meta is not None or a_meta is None:
                raise StateEventValidationError(
                    f"{ctx}: change_type 'added' requires before=None and after!=None"
                )
        elif change_type == "deleted":
            if b_meta is None or a_meta is not None:
                raise StateEventValidationError(
                    f"{ctx}: change_type 'deleted' requires before!=None and after=None"
                )
        elif change_type == "modified" and (b_meta is None or a_meta is None):
            raise StateEventValidationError(
                f"{ctx}: change_type 'modified' requires both before and after metadata"
            )

        if "event_count" not in change:
            raise StateEventValidationError(f"{ctx}: event_count is required")
        c_event_count = _validate_nonnegative_int64(change["event_count"], name="event_count", context=ctx)

        first_at = change.get("first_event_at")
        last_at = change.get("last_event_at")

        if c_event_count == 0:
            if first_at is not None or last_at is not None:
                raise StateEventValidationError(
                    f"{ctx}: event_count=0 requires first_event_at and last_event_at to be None"
                )
        else:
            if not isinstance(first_at, str) or not isinstance(last_at, str):
                raise StateEventValidationError(
                    f"{ctx}: event_count > 0 requires non-null first_event_at and last_event_at"
                )
            f_dt = _parse_iso_timestamp(first_at, context=f"{ctx} first_event_at")
            l_dt = _parse_iso_timestamp(last_at, context=f"{ctx} last_event_at")
            if l_dt < f_dt:
                raise StateEventValidationError(
                    f"{ctx}: last_event_at ({last_at}) precedes first_event_at ({first_at})"
                )

        validated_changes.append(
            StateDiffChange(
                path=path,
                change_type=change_type,
                before=b_meta,
                after=a_meta,
                event_count=c_event_count,
                first_event_at=first_at,
                last_event_at=last_at,
            )
        )

    if doc_change_count != len(validated_changes):
        raise StateEventValidationError(
            f"state-diff.json change_count ({doc_change_count}) does not match changes length ({len(validated_changes)})"
        )

    return StateDiffDocument(
        schema_version=schema_version,
        status=status,
        root=root,
        before_captured_at=before_cap,
        after_captured_at=after_cap,
        change_count=doc_change_count,
        event_count=doc_event_count,
        dropped_event_count=doc_dropped,
        changes=tuple(validated_changes),
    )

def load_state_diff(diff_path: Path) -> StateDiffDocument:
    """Load and validate state-diff.json, failing closed with StateEventValidationError."""
    if not diff_path.exists():
        raise StateEventValidationError("state-diff.json missing for available event stream")
    try:
        payload: Any = json.loads(diff_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateEventValidationError(
            f"state-diff.json is unreadable or malformed: {type(exc).__name__}"
        ) from exc
    return validate_state_diff_payload(payload)


def _initial_states(journal_dir: Path) -> dict[str, StateEventMetadata | None]:
    diff_path = journal_dir / "state-diff.json"
    if not diff_path.exists():
        raise StateEventValidationError("state-diff.json missing for available event stream")
    doc = load_state_diff(diff_path)
    return {change.path: change.before for change in doc.changes}

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
        before_evidence_status="invalid",
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
        has_before_evidence = event.path in state_by_path
        before = state_by_path.get(event.path)
        before_evidence_status = (
            "known_state"
            if has_before_evidence and before is not None
            else "known_absent"
            if has_before_evidence
            else "unknown_not_in_diff"
        )
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
                before_evidence_status=before_evidence_status,
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
