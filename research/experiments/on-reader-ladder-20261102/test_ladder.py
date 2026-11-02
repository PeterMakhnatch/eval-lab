"""Tests for D2-READER-LADDER experiment artifacts and statistical helpers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, "/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/train")
import stats  # noqa: E402
from analyze_ladder import (  # noqa: E402
    REF_RUNG,
    RUNGS_ORDER,
    fast_auroc,
    tune_threshold,
)

EXP_DIR = Path(__file__).resolve().parent


def test_ladder_results_structure():
    results_path = EXP_DIR / "ladder_results.json"
    assert results_path.is_file(), "ladder_results.json missing"
    data = json.loads(results_path.read_text(encoding="utf-8"))

    assert data["cohort_size"] == 300
    assert data["tune_pos"] == 71
    assert data["tune_neg"] == 79
    assert data["report_pos"] == 71
    assert data["report_neg"] == 79

    rungs = data["rungs"]
    assert len(rungs) == len(RUNGS_ORDER) == 8

    rung_ids = [r["rung_id"] for r in rungs]
    assert REF_RUNG in rung_ids

    for r in rungs:
        assert r["total_cases"] == 300
        assert r["all_attested"] is True
        assert r["total_fallbacks"] == 0
        assert 0.0 <= r["auroc"]["val"] <= 1.0
        assert 0.0 <= r["rec_fa1"]["rate"] <= 1.0
        assert 0.0 <= r["rec_fa2"]["rate"] <= 1.0
        assert 0.0 <= r["paired_vs_ref_rec_fa2"]["p"] <= 1.0


def test_tune_threshold():
    negs = [10.0, 20.0, 30.0, 40.0, 50.0]
    # alpha = 20% -> allowed = 1. threshold is the 2nd highest -> 40.0
    t = tune_threshold(negs, 20.0)
    assert t == 40.0
    alerts = [s for s in negs if s > t]
    assert len(alerts) == 1


def test_fast_auroc():
    pos = [80.0, 90.0, 100.0]
    neg = [10.0, 20.0, 30.0]
    assert fast_auroc(pos, neg) == 1.0

    pos_rev = [10.0, 20.0, 30.0]
    neg_rev = [80.0, 90.0, 100.0]
    assert fast_auroc(pos_rev, neg_rev) == 0.0


def test_mcnemar_exact():
    # Complete agreement
    a = [True, False, True, False]
    b = [True, False, True, False]
    res = stats.paired(a, b)
    assert res["b"] == 0
    assert res["c"] == 0
    assert res["p"] == 1.0

    # Discordant
    a_disc = [True] * 6 + [False] * 10
    b_disc = [False] * 6 + [False] * 10
    res_disc = stats.paired(a_disc, b_disc)
    assert res_disc["b"] == 6
    assert res_disc["c"] == 0
    assert round(res_disc["p"], 4) == 0.0312
