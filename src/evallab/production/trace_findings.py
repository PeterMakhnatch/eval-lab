"""Shared ``trace-findings/v1`` validation for retained trace readers."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from evallab.production.cloud_trace_db import TRACE_FINDING_KINDS, TRACE_FINDING_VERDICTS, _integer

MAX_EVIDENCE_STEPS = 20
MAX_SUMMARY_WORDS = 40
MAX_SUMMARY_CHARS = 600
_INT32 = 2**31 - 1  # the PostgreSQL INTEGER column limit; SQLite would take more
_REQUIRED = ("run_id", "detector", "detector_version", "verdict", "kind", "produced_at")
_DETECTOR = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+:/@\-]*\Z")
_READER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+\-]*\Z")


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
        "reader_model": "" if reader in (None, "") else _token(reader, "reader_model", _READER, 96),
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
