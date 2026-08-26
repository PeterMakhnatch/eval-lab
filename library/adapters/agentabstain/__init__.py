"""Lean AgentAbstain canary adapter and admission gate integration."""

from evallab.agentabstain_gate import (
    HardenedExecutionEvent,
    PairAdmissionResult,
    SingleDeltaAdmissionGate,
    evaluate_control_matrix,
    verify_abstain_execution,
    verify_act_execution,
)

from .adapter import TaskVariant, load_variants, primary_verdict, source_digest
from .controls import evaluate, evaluate_hardened_controls

__all__ = [
    "HardenedExecutionEvent",
    "PairAdmissionResult",
    "SingleDeltaAdmissionGate",
    "TaskVariant",
    "evaluate",
    "evaluate_control_matrix",
    "evaluate_hardened_controls",
    "load_variants",
    "primary_verdict",
    "source_digest",
    "verify_abstain_execution",
    "verify_act_execution",
]
