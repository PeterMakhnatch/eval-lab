"""``trace-findings/v1``: one record per (run, detector, detector version, reader), stored in ``trace_findings``.

Every trace detector writes the same record, so one harness can merge them per
run. The database owns admission: a record is stored only for a run of a
registry-admitted DEV task, and its cited steps must be stored steps of that
run. ``actor_inputs_available`` is recomputed from the stored steps with the
exports' own rule and never taken from a record; a run with no actor transcript
cannot be recorded ``clear`` (no model actor to read is unknown, not clean).

Records are keyed by ``(run_id, detector, detector_version, reader_model)``; a
missing reader is stored as the empty string. Among records sharing a key the
newest ``produced_at`` that passes every check is stored, so a refused newer
record never hides an older valid one. Re-importing a file stores the same
rows, and an older ``produced_at`` never replaces a newer stored one.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Collection, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.production.cloud_trace_db import (
    TRACE_FINDING_KINDS,
    TRACE_FINDING_VERDICTS,
    CloudTraceDatabase,
    CloudTraceRetriever,
    _integer,
)
from evallab.production.corpus_queue import CorpusRegistry

MAX_EVIDENCE_STEPS = 20
MAX_SUMMARY_WORDS = 40
MAX_SUMMARY_CHARS = 600
_INT32 = 2**31 - 1  # the PostgreSQL INTEGER column limit; SQLite would take more
_REQUIRED = ("run_id", "detector", "detector_version", "verdict", "kind", "produced_at")
_DETECTOR = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+:/@\-]*\Z")
_READER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+\-]*\Z")
_KEY = ("run_id", "detector", "detector_version", "reader_model")
COLUMNS = (
    "run_id", "task_id", "detector", "detector_version", "reader_model", "verdict", "score", "kind",
    "evidence_steps", "summary", "input_tokens", "output_tokens", "cost_usd", "latency_ms",
    "produced_at", "actor_inputs_available",
)


def _number(value: Any, field: str, *, nullable: bool = False, minimum: float | None = None) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{field} must be a finite number")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    return float(value)


def _small_integer(value: Any, field: str) -> int:
    if _integer(value, field) > _INT32:
        raise ValueError(f"{field} must be at most {_INT32}")
    return value


def _clean(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(f"{field} must be a nonempty string of at most {limit} characters without NUL")
    return value


def _token(value: Any, field: str, pattern: re.Pattern[str], limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit or not pattern.match(value):
        raise ValueError(f"{field} must be a short identifier")
    return value


def _timestamp(value: Any) -> str:
    """A timezone-aware ISO instant as canonical, fixed-width UTC text (it sorts as time)."""
    if not isinstance(value, str):
        raise ValueError("produced_at must be a UTC ISO timestamp")
    try:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            raise ValueError("produced_at must carry a timezone")
        return moment.astimezone(UTC).isoformat(timespec="microseconds").removesuffix("+00:00") + "Z"
    except OverflowError:
        raise ValueError("produced_at must be a UTC ISO timestamp") from None
    except ValueError as error:
        if "timezone" in str(error):
            raise
        raise ValueError("produced_at must be a UTC ISO timestamp") from None


def _finding(record: Any) -> dict[str, Any]:
    """Validate one ``trace-findings/v1`` record; keys outside the schema are dropped.

    A record's own ``actor_inputs_available`` is kept only so the writer can
    count disagreements with the value the database computes.
    """
    if not isinstance(record, Mapping):
        raise ValueError("record must be a JSON object")
    missing = [field for field in _REQUIRED if field not in record]
    if missing:
        raise ValueError(f"missing fields: {', '.join(missing)}")
    reader = record.get("reader_model")
    verdict, kind = record["verdict"], record["kind"]
    if verdict not in TRACE_FINDING_VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(TRACE_FINDING_VERDICTS)}")
    if kind not in TRACE_FINDING_KINDS:
        raise ValueError(f"kind must be one of {', '.join(TRACE_FINDING_KINDS)}")
    summary = record.get("summary", "")
    if (
        not isinstance(summary, str) or "\x00" in summary or len(summary) > MAX_SUMMARY_CHARS
        or len(summary.split()) > MAX_SUMMARY_WORDS
    ):
        raise ValueError(
            f"summary must be text of at most {MAX_SUMMARY_WORDS} words and {MAX_SUMMARY_CHARS} characters",
        )
    steps = record.get("evidence_steps", [])
    if not isinstance(steps, list) or len(steps) > MAX_EVIDENCE_STEPS:
        raise ValueError(f"evidence_steps must be a list of at most {MAX_EVIDENCE_STEPS} step indices")
    indices = [_small_integer(index, "evidence_steps entry") for index in steps]
    if len(set(indices)) != len(indices):
        raise ValueError("evidence_steps must not repeat a step index")
    row: dict[str, Any] = {
        "run_id": _clean(record["run_id"], "run_id", 512),
        "detector": _token(record["detector"], "detector", _DETECTOR, 64),
        "detector_version": _token(record["detector_version"], "detector_version", _VERSION, 64),
        "reader_model": "" if reader is None else _token(reader, "reader_model", _READER, 96),
        "verdict": verdict,
        "score": _number(record.get("score"), "score", nullable=True),
        "kind": kind,
        "evidence_steps": sorted(indices),
        "summary": summary.strip(),
        "input_tokens": _small_integer(record.get("input_tokens", 0), "input_tokens"),
        "output_tokens": _small_integer(record.get("output_tokens", 0), "output_tokens"),
        "cost_usd": _number(record.get("cost_usd", 0.0), "cost_usd", minimum=0.0),
        "latency_ms": _small_integer(record.get("latency_ms", 0), "latency_ms"),
        "produced_at": _timestamp(record["produced_at"]),
    }
    if "actor_inputs_available" in record:
        row["actor_inputs_available"] = record["actor_inputs_available"]
    return row


def has_actor_inputs(database: CloudTraceDatabase, run_id: str) -> bool:
    """The exports' ``actor_inputs_available`` rule applied to the run's stored steps.

    Same projection as ``cloud_trace_export``: ATIF, then ``blind_atif`` (which drops
    CheatBench annotation steps), then "some agent step has text or a tool call".
    Steps are independent in all three, so they are checked one at a time and the
    scan stops at the first step that counts.
    """
    from evallab.cheatbench_fidelity import blind_atif
    from evallab.production.cloud_trace_export import _actor_inputs_available, _atif

    cursor = database._execute(
        "SELECT step_index, role, content_text, tool_name, command_or_action FROM trace_steps "
        "WHERE trace_id = ? ORDER BY step_index", (run_id,),
    )
    for step_index, role, content, tool_name, command in cursor:
        step = {
            "step_index": step_index, "role": role, "content_text": content, "tool_name": tool_name,
            "command_or_action": command, "observation_preview": None,
        }
        if _actor_inputs_available(blind_atif(_atif([step], "probe"), item_id="probe")["steps"]):
            return True
    return False


def has_table(database: CloudTraceDatabase) -> bool:
    """Whether this store has a ``trace_findings`` table (older read-only stores do not)."""
    if database._postgres:
        return database._exists("trace_findings")
    return database._execute("SELECT 1 FROM sqlite_master WHERE name = 'trace_findings'").fetchone() is not None


def upsert_trace_findings(
    database: CloudTraceDatabase, records: Iterable[Any], *, registry: CorpusRegistry | None = None,
    allowed_task_ids: Collection[str] | None = None,
) -> dict[str, Any]:
    """Store valid records of admitted DEV runs; refuse the rest, counted by reason.

    Admission is recomputed inside the write snapshot; ``allowed_task_ids`` can
    only narrow it. Refusals never partly store a record. Per key the newest
    ``produced_at`` that passes (a tie goes to the later record) is the candidate,
    and a stored record is replaced only by one that is at least as new.
    """
    reasons: dict[str, int] = {}
    examples: list[dict[str, Any]] = []

    def refuse(run_id: Any, reason: str) -> None:
        reasons[reason] = reasons.get(reason, 0) + 1
        if len(examples) < 20:
            examples.append({"run_id": run_id if isinstance(run_id, str) else None, "reason": reason})

    count = 0
    candidates: dict[tuple[str, ...], list[tuple[str, int, dict[str, Any]]]] = {}
    for record in records:
        count += 1
        try:
            row = _finding(record)
        except ValueError as error:
            refuse(record.get("run_id") if isinstance(record, Mapping) else None, str(error))
            continue
        candidates.setdefault(tuple(row[field] for field in _KEY), []).append((row["produced_at"], count, row))
    stored = unchanged = overrides = superseded = 0
    with database._transaction(write=True):
        if database._postgres:
            database._execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        admitted, _ = CloudTraceRetriever(database, registry=registry)._admitted_ids()
        allowed = frozenset(admitted)
        if allowed_task_ids is not None:
            allowed = allowed.intersection(allowed_task_ids)
        everything = [row for options in candidates.values() for _, _, row in options]
        run_ids = {row["run_id"] for row in everything}
        parameters: list[Any] = []
        scope = database._task_id_scope("trace_id", run_ids, parameters)
        tasks = {
            run["trace_id"]: run["task_id"]
            for run in database._rows(f"SELECT trace_id, task_id FROM agent_traces WHERE {scope}", parameters)
        }
        present = database._stored_steps(
            (row["run_id"], index) for row in everything if tasks.get(row["run_id"]) in allowed
            for index in row["evidence_steps"]
        )
        actor: dict[str, bool] = {}
        chosen: list[dict[str, Any]] = []
        for options in candidates.values():
            selected: dict[str, Any] | None = None
            for _, _, row in sorted(options, key=lambda option: option[:2], reverse=True):
                run_id = row["run_id"]
                task_id = tasks.get(run_id)
                if task_id is None:
                    refuse(run_id, "run_id names no stored trace")
                    continue
                if task_id not in allowed:
                    refuse(run_id, "run is not an admitted DEV run")
                    continue
                if any((run_id, index) not in present for index in row["evidence_steps"]):
                    refuse(run_id, "evidence_steps must name stored steps of the run")
                    continue
                if run_id not in actor:
                    actor[run_id] = has_actor_inputs(database, run_id)
                if not actor[run_id] and row["verdict"] == "clear":
                    refuse(run_id, "a run with no actor transcript cannot be recorded clear")
                    continue
                if selected is not None:
                    superseded += 1
                    continue
                claimed = row.pop("actor_inputs_available", actor[run_id])
                overrides += claimed != actor[run_id]
                selected = row | {"task_id": task_id, "actor_inputs_available": actor[run_id]}
            if selected is not None:
                chosen.append(selected)
        parameters = []
        scope = database._task_id_scope("run_id", {row["run_id"] for row in chosen}, parameters)
        newest = {
            tuple(found[field] for field in _KEY): found["produced_at"] for found in database._rows(
                f"SELECT {', '.join(_KEY)}, produced_at FROM trace_findings WHERE {scope}", parameters,
            )
        } if chosen else {}
        writes = []
        for selected in chosen:
            existing = newest.get(tuple(selected[field] for field in _KEY))
            if existing is not None and existing > selected["produced_at"]:
                unchanged += 1
                continue
            writes.append({column: selected[column] for column in COLUMNS})
        database._upsert_many("trace_findings", writes, _KEY)
        stored = len(writes)
    return {
        "admitted_tasks": len(allowed), "records": count, "keys": len(candidates), "stored": stored,
        "kept_newer_stored": unchanged, "superseded_in_input": superseded,
        "refused": sum(reasons.values()), "refused_by_reason": dict(sorted(reasons.items())),
        "refused_examples": examples, "actor_inputs_overrides": overrides,
    }


def import_trace_findings(
    database: CloudTraceDatabase, path: str | Path, *, registry: CorpusRegistry | None = None,
) -> dict[str, Any]:
    """Import the interim JSONL, one ``trace-findings/v1`` object per line."""
    data = Path(path).read_bytes()
    records: list[Any] = []
    invalid = 0
    for line in data.split(b"\n"):
        if line.strip():
            try:
                records.append(json.loads(line))
            except (json.JSONDecodeError, UnicodeDecodeError):
                invalid += 1
    return {
        "schema": "evallab.trace_findings_import/v1", "path": str(path),
        "sha256": hashlib.sha256(data).hexdigest(), "invalid_json_lines": invalid,
        **upsert_trace_findings(database, records, registry=registry),
    }
