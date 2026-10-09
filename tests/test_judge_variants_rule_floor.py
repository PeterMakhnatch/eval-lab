"""Measured rule-credit removal and faithful sidecar floor calculation."""

from __future__ import annotations

import pytest

from evallab.general_nop_gate import build_meta, rule_floor
from evallab.task_variants import VariantInvalid


@pytest.fixture()
def meta():
    return {"items": [
        {"id": "preserve", "method": "rule", "fn": "check_preserve", "weight": 2},
        {"id": "mutate", "method": "rule", "fn": "check_mutate", "weight": 1},
        {"id": "answer", "method": "llm", "weight": 1},
        {"id": "vision", "method": "llm", "judge": "vision", "weight": 99},
        {"id": "agent", "method": "agent", "weight": 99},
    ], "check_code": "unchanged executable checks"}


def test_pristine_reward_credit_removed_without_disabling_checks(meta):
    scores = {"preserve": 1.0, "mutate": 0.0}
    assert rule_floor(meta, scores) == 0.5
    updated = build_meta(meta, {"preserve": 1.0})
    assert rule_floor(updated, scores) == 0.0
    assert updated["items"][0]["fn"] == "check_preserve"
    assert updated["check_code"] == meta["check_code"]
    assert meta["items"][0]["weight"] == 2
    assert rule_floor(updated, {"preserve": 1, "mutate": 1}) == 0.5


@pytest.mark.parametrize("scores", [{}, {"missing": 1}, {"answer": 1},
                                    {"preserve": 0}, {"vision": 1}, {"agent": 1}])
def test_unmeasured_or_nonrule_decisions_refused(meta, scores):
    with pytest.raises(VariantInvalid):
        build_meta(meta, scores)


def test_existing_gate_cannot_be_zero_weighted(meta):
    meta["items"][0]["gate"] = True
    with pytest.raises(VariantInvalid):
        build_meta(meta, {"preserve": 1.0})
    assert rule_floor(meta, {"preserve": 0, "mutate": 1}) == 0


def test_duplicate_ids_refused(meta):
    meta["items"].append(meta["items"][0].copy())
    with pytest.raises(VariantInvalid, match="duplicate"):
        build_meta(meta, {"preserve": 1})
