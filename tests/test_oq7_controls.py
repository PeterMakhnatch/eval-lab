"""Behavioral checks for run-matched OQ-7 statistics."""

import importlib.util
import sys
from pathlib import Path

import numpy as np

DIR = Path(__file__).resolve().parents[1] / "research/experiments/oq7-atlas-guided-audit"
sys.path.insert(0, str(DIR))
spec = importlib.util.spec_from_file_location("oq7_controls", DIR / "controls.py")
assert spec is not None and spec.loader is not None
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


def test_strata_never_split_equal_runs_and_keep_tail():
    rows = [{"dataset_id": "d", "task_id": str(i), "runs": i // 7} for i in range(41)]
    groups = c.bins(rows)
    assert sum(len(ids) for _, ids in groups) == 41
    assert all(len(ids) >= 10 for _, ids in groups)
    assert all(
        max(rows[i]["runs"] for i in a) < min(rows[i]["runs"] for i in b)
        for (_, a), (_, b) in zip(groups[:-1], groups[1:], strict=True)
    )


def test_metrics_and_random_expectation():
    metrics, null = c.metric_rows([1, 0, 1, 0])
    np.testing.assert_allclose(metrics[0], [5 / 6, 3 / 4, 1 / 2, 1 / 2])
    np.testing.assert_allclose(null[0], [49 / 72, 1 / 2, 1 / 2, 1 / 2])


def test_permutation_detects_perfect_order_and_bootstrap_effect():
    rows = [{"keyed": i < 10} for i in range(20)]
    ids = np.arange(20)
    result = c.group_analysis(rows, [("d", ids)], np.arange(20), np.arange(20), 2000, 500)
    assert result["AUROC"]["value"] == 1
    assert result["AUROC"]["p_one_sided"] < 0.01
    assert result["AUROC"]["bootstrap_effect_ci95"][0] > 0
    assert result["yield@20"]["effect"] == 0
