"""Synthetic-count tests for report_metrics; no benchmark row, id or model output is touched."""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path
from typing import Any

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "research/experiments/oq10-task-quality-cross/report_metrics.py"
)
_SPEC = importlib.util.spec_from_file_location("oq10_report_metrics", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
metrics = importlib.util.module_from_spec(_SPEC)
sys.modules["oq10_report_metrics"] = metrics
_SPEC.loader.exec_module(metrics)

AB = "parsewave__automationbench-verified"
MB = "swe_bench_multilingual"
GYM = "swe_gym"
Z = 1.959963984540054


def _cell(corpus: str = AB, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "corpus": corpus,
        "planned_positive": 30,
        "planned_negative": 30,
        "confusion": {"TP": 12, "FN": 18, "FP": 6, "TN": 24},
        "input_tokens": 1_000,
        "output_tokens": 200,
        "reasoning_tokens": None,
        "actual_paid_usd": None,
    }
    kwargs.update(overrides)
    return metrics.summarize_cell(**kwargs)


def _on_wilson_boundary(p: float, successes: int, trials: int) -> bool:
    observed = successes / trials
    return math.isclose(trials * (observed - p) ** 2, Z * Z * p * (1 - p), abs_tol=1e-4)


# ----------------------------------------------------------------- corpus semantics


def test_automationbench_unchanged_negatives_are_uncertified_alarms_not_fpr() -> None:
    cell = _cell(AB)

    assert cell["negatives_certified"] is False
    assert cell["confusion"] == {"tp": 12, "fn": 18, "unverified_alarms": 6, "unflagged_unverified": 24}
    assert cell["unverified_alarm_rate"] == {"num": 6, "den": 30, "value": 0.2}
    assert cell["conditional_precision"] == {"num": 12, "den": 18, "value": round(12 / 18, 6)}
    assert "precision" not in cell
    assert not any("false_positive" in key for key in cell)


@pytest.mark.parametrize("corpus", [MB, GYM])
def test_pai_aligned_negatives_give_true_sample_fpr_and_precision(corpus: str) -> None:
    cell = _cell(corpus)

    assert cell["negatives_certified"] is True
    assert cell["confusion"] == {"tp": 12, "fn": 18, "fp_false_alarms": 6, "tn": 24}
    assert cell["false_positive_rate_on_human_aligned"] == {"num": 6, "den": 30, "value": 0.2}
    assert cell["precision"] == {"num": 12, "den": 18, "value": round(12 / 18, 6)}
    assert "conditional_precision" not in cell
    assert "unverified_alarm_rate" not in cell


@pytest.mark.parametrize("corpus", ["paichecker", "automationbench-verified", "AutomationBench", ""])
def test_unknown_or_pooled_corpus_is_rejected(corpus: str) -> None:
    with pytest.raises(metrics.MetricsError):
        _cell(corpus)


# ----------------------------------------------------------------- recall and Wilson


def test_recall_wilson_interval_solves_the_score_equation() -> None:
    recall = _cell()["recall"]

    assert (recall["num"], recall["den"], recall["value"]) == (12, 30, 0.4)
    low, high = recall["wilson95"]
    assert low < 0.4 < high
    assert _on_wilson_boundary(low, 12, 30) and _on_wilson_boundary(high, 12, 30)


def test_zero_recall_keeps_a_defined_interval_but_undefined_precision() -> None:
    cell = _cell(confusion={"TP": 0, "FN": 10, "FP": 0, "TN": 10}, planned_positive=10, planned_negative=10)

    assert cell["recall"]["value"] == 0.0
    assert cell["recall"]["wilson95"][0] == 0.0
    assert cell["recall"]["wilson95"][1] == pytest.approx(Z * Z / (10 + Z * Z), abs=1e-6)
    assert cell["conditional_precision"] == {"num": 0, "den": 0, "value": None}


def test_no_completed_positives_makes_recall_and_interval_null_not_zero() -> None:
    cell = _cell(GYM, confusion={"TP": 0, "FN": 0, "FP": 3, "TN": 27})

    assert cell["recall"]["den"] == 0
    assert cell["recall"]["value"] is None and cell["recall"]["wilson95"] is None
    assert cell["coverage"]["positives"] == {"planned": 30, "completed": 0, "missing": 30}
    assert cell["precision"] == {"num": 0, "den": 3, "value": 0.0}


# ----------------------------------------------------------------- partial coverage


def test_missing_tasks_are_coverage_not_false_negatives_or_true_negatives() -> None:
    cell = _cell(MB, confusion={"TP": 5, "FN": 5, "FP": 0, "TN": 20})

    assert cell["coverage"]["positives"] == {"planned": 30, "completed": 10, "missing": 20}
    assert cell["coverage"]["negatives"] == {"planned": 30, "completed": 20, "missing": 10}
    assert cell["coverage"]["tasks"] == {"planned": 60, "completed": 30, "missing": 30}
    assert cell["coverage"]["complete"] is False
    assert cell["recall"]["den"] == 10 and cell["recall"]["value"] == 0.5
    assert cell["false_positive_rate_on_human_aligned"]["den"] == 20
    assert cell["confusion"]["fn"] == 5 and cell["confusion"]["tn"] == 20


# ----------------------------------------------------------------- tokens and cost


def _values(per_task: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {kind: ratio["value"] for kind, ratio in per_task.items()}


def test_unknown_token_components_and_subscription_cash_stay_null() -> None:
    cell = _cell(input_tokens=None, reasoning_tokens=None, actual_paid_usd=None)
    tokens = cell["tokens"]

    assert (tokens["input"], tokens["output"], tokens["input_plus_output"], tokens["reasoning"]) == (None, 200, None, None)
    assert tokens["per_completed_task"]["output"] == {"num": 200, "den": 60, "value": round(200 / 60, 6)}
    assert _values(tokens["per_completed_task"]) == {
        "input": None, "output": round(200 / 60, 6), "input_plus_output": None, "reasoning": None,
    }
    assert cell["cost"]["actual_paid_usd"] is None and cell["cost"]["actual_cash"] == "N/A"
    assert cell["cost"]["usd_per_completed_task"] is None
    assert cell["cost"]["usd_per_flagged_known_positive_task"] is None


def test_partial_cell_divides_all_attempt_totals_by_completed_and_flagged_known_positive_tasks() -> None:
    cell = _cell(MB, confusion={"TP": 4, "FN": 6, "FP": 2, "TN": 28}, actual_paid_usd=3, reasoning_tokens=80)
    tokens = cell["tokens"]

    assert cell["coverage"]["tasks"]["missing"] == 20
    assert tokens["input_plus_output"] == 1_200 and tokens["reasoning"] == 80
    assert _values(tokens["per_completed_task"]) == {"input": 25.0, "output": 5.0, "input_plus_output": 30.0, "reasoning": 2.0}
    assert _values(tokens["per_flagged_known_positive_task"]) == {
        "input": 250.0, "output": 50.0, "input_plus_output": 300.0, "reasoning": 20.0,
    }
    assert cell["cost"]["actual_cash"] == "recorded"
    assert cell["cost"]["usd_per_completed_task"] == 0.075
    assert cell["cost"]["usd_per_flagged_known_positive_task"] == 0.75


def test_per_flagged_known_positive_cost_and_tokens_are_null_with_zero_tp() -> None:
    cell = _cell(confusion={"TP": 0, "FN": 30, "FP": 5, "TN": 25}, actual_paid_usd=2.0)
    per_flagged = cell["tokens"]["per_flagged_known_positive_task"]

    assert cell["cost"]["usd_per_completed_task"] == round(2.0 / 60, 6)
    assert cell["cost"]["usd_per_flagged_known_positive_task"] is None
    assert per_flagged["input"] == {"num": 1_000, "den": 0, "value": None}
    assert set(_values(per_flagged).values()) == {None}


# ----------------------------------------------------------------- invalid input


@pytest.mark.parametrize(
    "overrides",
    [
        {"confusion": {"TP": 20, "FN": 11, "FP": 0, "TN": 0}},
        {"confusion": {"TP": 0, "FN": 0, "FP": 16, "TN": 15}},
        {"confusion": {"TP": -1, "FN": 1, "FP": 0, "TN": 0}},
        {"confusion": {"TP": 1, "FN": 1, "FP": 0}},
        {"confusion": {"TP": 1, "FN": 1, "FP": 0, "TN": 0, "MISSING": 3}},
        {"confusion": {"TP": True, "FN": 1, "FP": 0, "TN": 0}},
        {"confusion": {"TP": 1.0, "FN": 1, "FP": 0, "TN": 0}},
        {"planned_positive": -1},
        {"input_tokens": -5},
        {"reasoning_tokens": -1},
        {"output_tokens": 2.5},
        {"actual_paid_usd": -0.01},
        {"actual_paid_usd": float("nan")},
        {"actual_paid_usd": True},
    ],
)
def test_impossible_counts_tokens_or_cash_are_rejected_not_repaired(overrides: dict[str, Any]) -> None:
    with pytest.raises(metrics.MetricsError):
        _cell(**overrides)


# ----------------------------------------------------------------- cluster result


def _view(corpus: str = AB, **changes: Any) -> dict[str, Any]:
    """A Library score_view-shaped dict matching the default 12/18/6/24 cell."""
    rate = "unverified_alarm_rate" if corpus == AB else "false_positive_rate_on_human_aligned"
    view: dict[str, Any] = {
        "recall": {"value": 0.4, "num": 12, "den": 30, "ci95": [0.2, 0.61], "undefined_resamples": 0},
        rate: {"value": 0.2, "num": 6, "den": 30, "ci95": [0.05, 0.4], "undefined_resamples": 3},
        "clusters": {"kind": "domain", "count": 7},
    }
    view.update(changes)
    return view


def test_existing_cluster_view_is_retained_beside_wilson_not_recomputed() -> None:
    view = _view(AB)
    cell = _cell(AB, cluster_result=view)

    assert cell["cluster_bootstrap"]["result"] == view
    assert cell["recall"]["wilson95"] != view["recall"]["ci95"]
    assert "ci95" not in cell["recall"]
    assert _cell(AB)["cluster_bootstrap"] is None


@pytest.mark.parametrize(
    ("corpus", "cluster_result"),
    [
        (AB, _view(AB, recall={"value": 11 / 30, "num": 11, "den": 30})),
        (GYM, _view(GYM, false_positive_rate_on_human_aligned={"value": 6 / 29, "num": 6, "den": 29})),
        (AB, _view(AB, false_positive_rate_on_human_aligned={"value": 0.2, "num": 6, "den": 30})),
        (MB, _view(MB, unverified_alarm_rate={"value": 0.2, "num": 6, "den": 30})),
        (AB, _view(AB, ids_or_text_in_this_output=True)),
        (AB, _view(AB, conditional_balanced_accuracy={"value": float("nan")})),
        (AB, {key: value for key, value in _view(AB).items() if key != "recall"}),
        (AB, {"schema": "task-qa-gate-score/v1", "views": {"all_800": _view(AB)}}),
    ],
)
def test_cluster_result_that_is_not_this_cells_one_view_is_rejected(corpus: str, cluster_result: dict[str, Any]) -> None:
    with pytest.raises(metrics.MetricsError):
        _cell(corpus, cluster_result=cluster_result)


def test_summary_is_strict_json() -> None:
    cell = _cell(GYM, confusion={"TP": 0, "FN": 0, "FP": 0, "TN": 0}, actual_paid_usd=1.5)

    assert json.loads(json.dumps(cell, allow_nan=False)) == cell
