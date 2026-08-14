"""Frozen judge-criterion names for the two judged-output families.

Names are taken from the read-only harbor-practice tasks
(`datasets/judged-output/{family}/tests/{causal_reasoning,action_quality}/judge.toml`
and `tests/evidence_fidelity/contradictions.toml`). This module does not invent
criteria. `negate=True` items still record the judge's pre-inversion yes/no.
"""

from __future__ import annotations

from typing import Final

# Judge dimensions brief 09 will score. Deterministic gate criteria (section
# presence, incident-id substring, copy detection) are not part of the sealed
# per-criterion keys.
DIMENSIONS: Final[tuple[str, ...]] = (
    "causal_reasoning",
    "action_quality",
    "evidence_fidelity",
)

CHECKOUT_CRITERIA: Final[dict[str, tuple[str, ...]]] = {
    "causal_reasoning": (
        "identifies_the_mechanism",
        "grounded_in_evidence",
        "rules_out_the_decoy",
        "separates_contributing_factors",
        "uncertainty_is_genuine",
    ),
    "action_quality": (
        "fixes_the_capacity_coupling",
        "closes_the_detection_gap",
        "actions_are_actionable",
        "actions_trace_to_findings",
        "proposes_unsupported_work",
    ),
    "evidence_fidelity": (
        "blames_payments_vendor",
        "asserts_unsupported_cause",
        "misstates_a_fact",
        "invents_evidence",
    ),
}

RETRY_CRITERIA: Final[dict[str, tuple[str, ...]]] = {
    "causal_reasoning": (
        "identifies_the_mechanism",
        "separates_trigger_from_cause",
        "grounded_in_evidence",
        "rules_out_the_decoys",
        "separates_contributing_factors",
        "uncertainty_is_genuine",
    ),
    "action_quality": (
        "bounds_the_amplification",
        "closes_the_detection_gap",
        "actions_are_actionable",
        "actions_trace_to_findings",
        "proposes_unsupported_work",
    ),
    "evidence_fidelity": (
        "blames_the_deploy",
        "treats_db_cpu_as_cause",
        "asserts_unsupported_cause",
        "misstates_a_fact",
        "invents_evidence",
    ),
}

# Pre-inversion polarity: True means the judge question is "is the flaw present?"
NEGATED: Final[dict[str, frozenset[str]]] = {
    "checkout-pool-exhaustion": frozenset(
        {
            "proposes_unsupported_work",
            "blames_payments_vendor",
            "asserts_unsupported_cause",
            "misstates_a_fact",
            "invents_evidence",
        }
    ),
    "retry-storm-backlog": frozenset(
        {
            "proposes_unsupported_work",
            "blames_the_deploy",
            "treats_db_cpu_as_cause",
            "asserts_unsupported_cause",
            "misstates_a_fact",
            "invents_evidence",
        }
    ),
}

FAMILY_CRITERIA: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "checkout-pool-exhaustion": CHECKOUT_CRITERIA,
    "retry-storm-backlog": RETRY_CRITERIA,
}

VERDICTS: Final[frozenset[str]] = frozenset({"yes", "no"})


def criteria_for(family: str) -> dict[str, tuple[str, ...]]:
    try:
        return FAMILY_CRITERIA[family]
    except KeyError as exc:
        raise KeyError(f"unknown family {family!r}") from exc


def all_criterion_names(family: str) -> list[tuple[str, str]]:
    """Return (dimension, criterion_name) pairs in rubric order."""
    pairs: list[tuple[str, str]] = []
    for dimension, names in criteria_for(family).items():
        for name in names:
            pairs.append((dimension, name))
    return pairs
