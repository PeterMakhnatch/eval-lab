"""The judge program lives in ``evallab.calibrate``; this module only adapts it."""

from __future__ import annotations

import dspy

from evallab.calibrate import build_dspy_program, dspy_verdicts


def CalibrationJudge() -> dspy.Module:  # noqa: N802 - factory kept under the program's name
    return build_dspy_program(dspy)


def flatten_verdicts(prediction: dspy.Prediction) -> dict[str, dict[str, str]]:
    """dimension -> criterion -> 'yes'/'no' (empty when the prediction failed)."""
    return {
        dimension: {name: cell.verdict for name, cell in block.items()}
        for dimension, block in dspy_verdicts(prediction).items()
    }


def rationales(prediction: dspy.Prediction) -> dict[str, dict[str, str]]:
    return {
        dimension: {name: cell.rationale for name, cell in block.items()}
        for dimension, block in dspy_verdicts(prediction).items()
    }
