"""Typed DSPy judge program for the calibration rubrics.

The output is a Pydantic-typed nested mapping rather than a free JSON string, so
missing criteria or non yes/no verdicts fail at parse time instead of silently
scoring zero downstream. ``flatten_verdicts`` gives the raw verdict mapping the
Lab's ``dspy_metric`` / prediction bundle expect.
"""

from __future__ import annotations

from typing import Literal

import dspy
from pydantic import BaseModel, Field


class CriterionVerdict(BaseModel):
    verdict: Literal["yes", "no"] = Field(description="raw pre-inversion answer to the criterion question")
    rationale: str = Field(description="one sentence citing the document passage or supplied fact that decides it")


class JudgeSignature(dspy.Signature):
    """Judge an incident postmortem against every criterion in the rubric.

    For each dimension and criterion in rubric_json.criteria, answer the criterion's
    question about the document with a raw yes/no. Negated criteria ask whether a
    flaw is PRESENT; answer yes when the flaw is present and do not invert.
    Decide evidence questions only against rubric_json.reference_facts and the
    document text; do not assume facts that are not supplied.
    """

    family: str = dspy.InputField(desc="calibration family identifier")
    rubric_json: str = dspy.InputField(
        desc="JSON with reference_facts, criteria (dimension -> criterion -> question) and verdict_convention"
    )
    document: str = dspy.InputField(desc="the postmortem under judgment")
    judgments: dict[str, dict[str, CriterionVerdict]] = dspy.OutputField(
        desc="dimension -> criterion -> {verdict, rationale}; include every criterion from rubric_json exactly once"
    )


class CalibrationJudge(dspy.Module):
    def __init__(self) -> None:
        super().__init__()
        self.judge = dspy.ChainOfThought(JudgeSignature)

    def forward(self, family: str, rubric_json: str, document: str) -> dspy.Prediction:
        return self.judge(family=family, rubric_json=rubric_json, document=document)


def flatten_verdicts(prediction: dspy.Prediction) -> dict[str, dict[str, str]]:
    """dimension -> criterion -> 'yes'/'no' from a typed prediction (empty on failure)."""
    judgments = getattr(prediction, "judgments", None)
    if not isinstance(judgments, dict):
        return {}
    flat: dict[str, dict[str, str]] = {}
    for dimension, block in judgments.items():
        if not isinstance(block, dict):
            continue
        for name, cell in block.items():
            verdict = getattr(cell, "verdict", None)
            if verdict is None and isinstance(cell, dict):
                verdict = cell.get("verdict")
            if verdict in ("yes", "no"):
                flat.setdefault(dimension, {})[name] = verdict
    return flat


def rationales(prediction: dspy.Prediction) -> dict[str, dict[str, str]]:
    judgments = getattr(prediction, "judgments", None)
    out: dict[str, dict[str, str]] = {}
    if not isinstance(judgments, dict):
        return out
    for dimension, block in judgments.items():
        if not isinstance(block, dict):
            continue
        for name, cell in block.items():
            text = getattr(cell, "rationale", None)
            if text is None and isinstance(cell, dict):
                text = cell.get("rationale")
            out.setdefault(dimension, {})[name] = (text or "").strip() or "no rationale returned"
    return out
