"""Typed DSPy judge contract: scoring, bundle construction and record identity.

No language model and no ``dspy`` import are needed: the metric and bundle
builder only look at the typed ``judgments`` attribute of a prediction object.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab.calibrate import (
    DSPY_OMITTED_RATIONALE,
    RUBRICS,
    _record_id,
    dspy_metric,
    dspy_prediction_bundle,
    evaluate_predictions,
    load_corpus,
    load_dspy_examples,
    make_stub_bundle,
)
from evallab.schemas import JudgeCalibrationRecord, JudgeCriterionVerdict

REPO_ROOT = Path(__file__).resolve().parents[1]
FAMILY = "checkout-pool-exhaustion"


def _cells(family: str, verdict: str) -> dict[str, dict[str, JudgeCriterionVerdict]]:
    return {
        dimension: {name: JudgeCriterionVerdict(verdict=verdict, rationale="r") for name in block}
        for dimension, block in RUBRICS[family]["criteria"].items()
    }


def test_dspy_metric_scores_exact_cells_and_treats_missing_or_invalid_as_disagreement() -> None:
    example = load_dspy_examples(REPO_ROOT, FAMILY)[9]  # 10-correct-timeline-dense
    expected = json.loads(example.expected_json)
    total = sum(len(block) for block in expected.values())

    perfect = {
        dimension: {name: JudgeCriterionVerdict(verdict=v, rationale="r") for name, v in block.items()}
        for dimension, block in expected.items()
    }
    assert dspy_metric(example, SimpleNamespace(judgments=perfect)) == 1.0

    # Drop one criterion and hand another an unparseable cell: both count against the judge.
    damaged = {d: dict(b) for d, b in perfect.items()}
    del damaged["causal_reasoning"]["identifies_the_mechanism"]
    damaged["evidence_fidelity"]["invents_evidence"] = {"verdict": "maybe", "rationale": "r"}
    assert dspy_metric(example, SimpleNamespace(judgments=damaged)) == pytest.approx((total - 2) / total)

    assert dspy_metric(example, SimpleNamespace(judgments=None)) == 0.0
    assert dspy_metric(example, SimpleNamespace()) == 0.0


def test_dspy_prediction_bundle_fills_omitted_criteria_and_stays_scoreable() -> None:
    documents = load_corpus(REPO_ROOT, FAMILY)
    full = _cells(FAMILY, "yes")
    partial = {"causal_reasoning": dict(full["causal_reasoning"])}  # two dimensions omitted
    predictions = [(documents[0].document_id, SimpleNamespace(judgments=partial))] + [
        (document.document_id, SimpleNamespace(judgments=full)) for document in documents[1:]
    ]

    bundle, omitted = dspy_prediction_bundle(
        REPO_ROOT, FAMILY, predictions, judge_backend="dspy-test", judge_model="fake-model"
    )

    criteria = RUBRICS[FAMILY]["criteria"]
    assert omitted == len(criteria["action_quality"]) + len(criteria["evidence_fidelity"])
    first = bundle.predictions[0].criteria
    assert all(cell.rationale == DSPY_OMITTED_RATIONALE for cell in first["action_quality"].values())
    assert all(cell.verdict == "no" for cell in first["evidence_fidelity"].values())
    assert [p.document_id for p in bundle.predictions] == [d.document_id for d in documents]

    record = evaluate_predictions(REPO_ROOT, bundle, prediction_artifact="test://bundle")
    assert isinstance(record, JudgeCalibrationRecord)
    assert record.document_count == len(documents)
    assert record.judge_backend == "dspy-test"


def test_record_id_distinguishes_judge_programs_on_the_same_model_and_day() -> None:
    stub = make_stub_bundle(REPO_ROOT, FAMILY)
    unoptimized = stub.model_copy(update={"judge_backend": "dspy-cot-unoptimized", "judge_model": "glm-5.3-flash"})
    compiled = stub.model_copy(update={"judge_backend": "dspy-gepa-checkout", "judge_model": "glm-5.3-flash"})
    day = date(2026, 9, 16)

    first, second = _record_id(unoptimized, day), _record_id(compiled, day)

    assert first != second
    assert first.startswith(f"{FAMILY}-20260916-dspy-cot-unoptimized-glm-5-3-flash-")
    pattern = JudgeCalibrationRecord.model_fields["record_id"].metadata[0].pattern
    import re

    assert re.fullmatch(pattern, first) and re.fullmatch(pattern, second)
