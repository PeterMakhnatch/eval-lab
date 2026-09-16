"""Exact per-criterion agreement, with textual feedback for reflective optimizers."""

from __future__ import annotations

import json

import dspy

from .program import flatten_verdicts


def cell_outcomes(
    example: dspy.Example, prediction: dspy.Prediction
) -> list[tuple[str, str, str, str | None]]:
    """(dimension, criterion, expected, observed-or-None) for every gold cell."""
    expected = json.loads(example.expected_json)
    observed = flatten_verdicts(prediction)
    return [
        (dimension, name, verdict, observed.get(dimension, {}).get(name))
        for dimension, block in expected.items()
        for name, verdict in block.items()
    ]


def agreement(example: dspy.Example, prediction: dspy.Prediction, trace=None) -> float:
    cells = cell_outcomes(example, prediction)
    if not cells:
        return 0.0
    return sum(exp == obs for _, _, exp, obs in cells) / len(cells)


def agreement_with_feedback(
    gold: dspy.Example,
    pred: dspy.Prediction,
    trace=None,
    pred_name: str | None = None,
    pred_trace=None,
) -> dspy.Prediction:
    """GEPA metric: score plus the gold rationale for every disagreeing cell."""
    cells = cell_outcomes(gold, pred)
    score = sum(exp == obs for _, _, exp, obs in cells) / len(cells) if cells else 0.0
    rationales = json.loads(getattr(gold, "rationales_json", "{}") or "{}")
    lines: list[str] = []
    for dimension, name, expected, observed in cells:
        if expected == observed:
            continue
        why = rationales.get(dimension, {}).get(name, "")
        got = observed if observed is not None else "MISSING"
        lines.append(f"- {dimension}.{name}: expected {expected}, got {got}. Reviewer note: {why}")
    if not lines:
        feedback = (
            f"All {len(cells)} criteria agree with the reviewer for document {gold.document_id}."
        )
    else:
        feedback = (
            f"{len(lines)} of {len(cells)} criteria disagree with the reviewer for document "
            f"{gold.document_id} (variant hint in id). Disagreements:\n" + "\n".join(lines)
        )
    return dspy.Prediction(score=score, feedback=feedback)
