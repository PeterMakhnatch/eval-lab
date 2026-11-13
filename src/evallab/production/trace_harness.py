"""Shared trace-findings contracts for retained Gold and online readers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.production.run_atlas import (
    DEFAULT_DATABASE,
    STATE_ROOT,
    AtlasRun,
    BlindRun,
    blind_run,
)
from evallab.production.trace_findings import (
    MAX_EVIDENCE_STEPS,
    MAX_SUMMARY_CHARS,
    MAX_SUMMARY_WORDS,
    _finding,
)

HARNESS_ROOT = STATE_ROOT / "trace-harness"
FINDING_FIELDS = (
    "run_id", "detector", "detector_version", "reader_model", "verdict", "score", "kind",
    "evidence_steps", "summary", "input_tokens", "output_tokens", "cost_usd", "latency_ms",
    "produced_at",
)
SUMMARY_WORDS = MAX_SUMMARY_WORDS
SUMMARY_CHARS = MAX_SUMMARY_CHARS


# --------------------------------------------------------------------------- #
# the shared input
# --------------------------------------------------------------------------- #


def actor_inputs_available(steps: Iterable[Mapping[str, Any]]) -> bool:
    """Some agent-authored step has message text or a tool call (the export rule)."""
    from evallab.production.cloud_trace_export import _actor_inputs_available

    return _actor_inputs_available(list(steps))


@dataclass(frozen=True)
class HarnessRun:
    """One run as every detector receives it.

    ``steps`` are the ATIF-shaped steps with original ``step_id`` values; every
    ``evidence_steps`` entry a detector emits is an original step id. ``blind``
    is the allowlisted, renumbered view model readers must use.
    """

    run_id: str
    source: str
    split: str
    outcome: str
    steps: tuple[Mapping[str, Any], ...]
    actor_inputs_available: bool
    # Health facts only: never included in atlas_run/blind() or model prompts.
    meta: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    _blind: list[BlindRun] = field(default_factory=list, repr=False, compare=False)

    @classmethod
    def from_atlas(cls, run: AtlasRun) -> HarnessRun:
        steps = tuple(run.doc.get("steps") or ())
        return cls(run.run_id, run.source, run.split, run.outcome, steps, actor_inputs_available(steps))

    @property
    def atlas_run(self) -> AtlasRun:
        return AtlasRun(self.run_id, self.source, self.split, self.outcome,
                        {"schema_version": "ATIF-v1.6", "steps": list(self.steps)})

    @property
    def step_ids(self) -> list[Any]:
        return [step.get("step_id") for step in self.steps]

    def blind(self) -> BlindRun:
        """Blinded view, computed once per run and shared by every detector."""
        if not self._blind:
            self._blind.append(blind_run(self.atlas_run))
        return self._blind[0]


# --------------------------------------------------------------------------- #
# trace-findings/v1
# --------------------------------------------------------------------------- #


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def trim_words(text: str, limit: int = SUMMARY_WORDS) -> str:
    words = " ".join(str(text or "").split()).split(" ")
    return " ".join(words[:limit])[:SUMMARY_CHARS] if words != [""] else ""


def finding(
    run_id: str, detector: str, *, detector_version: str, reader_model: str | None,
    verdict: str, kind: str, score: float | None = None, evidence_steps: Iterable[int] = (),
    summary: str = "", input_tokens: int = 0, output_tokens: int = 0, cost_usd: float = 0.0,
    latency_ms: int = 0, produced_at: str | None = None,
) -> dict[str, Any]:
    """Build one validated ``trace-findings/v1`` record.

    Trim the summary and cap cited steps (lowest ids first) to the shared
    ``trace_findings._finding`` contract.
    """
    record = {
        "run_id": run_id, "detector": detector, "detector_version": detector_version,
        "reader_model": reader_model, "verdict": verdict,
        "score": None if score is None else float(score), "kind": kind,
        "evidence_steps": sorted({int(step) for step in evidence_steps})[:MAX_EVIDENCE_STEPS],
        "summary": trim_words(summary), "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens), "cost_usd": float(cost_usd),
        "latency_ms": int(latency_ms), "produced_at": produced_at or now_utc(),
    }
    errors = validate_finding(record)
    if errors:
        raise ValueError(f"invalid trace finding for {run_id}/{detector}: {errors}")
    return record


def validate_finding(record: Mapping[str, Any]) -> list[str]:
    """Reasons a record breaks ``trace-findings/v1``; empty when valid.

    ``trace_findings._finding`` supplies the shared value rules.
    The harness also requires an exact field set and a ``provider/id`` reader.
    """
    errors = [f"missing field {name}" for name in FINDING_FIELDS if name not in record]
    errors += [f"unexpected field {name}" for name in sorted(set(record) - set(FINDING_FIELDS))]
    if errors:
        return errors
    if record["reader_model"] is not None and (
        not isinstance(record["reader_model"], str) or "/" not in record["reader_model"]
    ):
        errors.append("reader_model must be provider/id or null")
    if any(isinstance(step, bool) for step in record["evidence_steps"] or ()):
        errors.append("evidence_steps entry must be an integer")
    try:
        _finding(record)
    except (TypeError, ValueError) as error:
        errors.append(str(error))
    return errors


# --------------------------------------------------------------------------- #
# detectors
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HarnessOptions:
    """Shared settings handed to every detector factory."""

    database: Path = DEFAULT_DATABASE
    repo_root: Path = Path(".")
    allow_model: bool = False
    reader: str | None = None


