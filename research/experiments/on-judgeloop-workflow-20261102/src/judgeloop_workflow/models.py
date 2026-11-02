"""Data models for the judge loop and monitor durable workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class TraceStep:
    step_id: int
    action: str
    tool: str | None = None
    output: str | None = None


@dataclass
class RunSpec:
    run_id: str
    dataset_id: str
    item_id: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    review_sla_seconds: float = 172800.0  # Default 48 hours
    model_route: str = "google-antigravity/gemini-3.8-flash"
    ledger_path: str | None = None


@dataclass
class L0Evaluation:
    triggered: bool
    trigger_kind: str  # "l0_tripwire", "target_surface", "cascade_stage1", "none"
    matched_rule: str | None = None
    step_index: int | None = None


@dataclass
class AutoReviewRequest:
    run_id: str
    trigger_kind: str
    prefix_step: int
    model_route: str
    ledger_path: str | None = None


@dataclass
class AutoReviewResult:
    run_id: str
    verdict: str  # "confirmed_cheat", "suspicious", "clean"
    confidence: float
    explanation: str
    tokens_used: int = 1500
    cost_usd: float = 0.0015
    model_attestation: str = "google-antigravity/gemini-3.8-flash"


@dataclass
class JudgeQueueEntry:
    run_id: str
    priority: int  # 0=normal, 1=medium, 2=high
    trigger_kind: str
    enqueued_at: str = ""
    hidden_model_verdict: str = "hidden"
    status: str = "queued"


@dataclass
class EscalationNotice:
    run_id: str
    escalated_at: str
    elapsed_seconds: float
    sla_threshold_seconds: float
    level: str = "manager_alert"
    reason: str = "Human review SLA exceeded"


@dataclass
class HumanVerdict:
    run_id: str
    reviewer_id: str
    verdict: str  # "cheated", "clean", "indeterminate"
    cheat_mechanism: str | None = None
    notes: str = ""
    submitted_at: str = ""


@dataclass
class CalibrationOutcome:
    run_id: str
    human_verdict: str
    autoreview_verdict: str
    agreement: bool
    calibrated_at: str = ""


@dataclass
class RunSummary:
    run_id: str
    final_verdict: str
    l0_triggered: bool
    trigger_kind: str
    autoreview_verdict: str | None
    human_verdict: str | None
    sla_breached: bool
    escalated: bool
    agreement_with_model: bool | None
    completed_at: str = ""


@dataclass
class IntakeBatch:
    batch_id: str
    runs: list[RunSpec]
    max_concurrent_runs: int = 3
    continue_as_new_threshold: int = 3
    generation: int = 1
    completed_count: int = 0
    completed_run_ids: list[str] = field(default_factory=list)
    ledger_path: str | None = None
