import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "pj_consensus",
    Path(__file__).resolve().parents[1] / "research/experiments/pj-self-cb2a4d-3-consensus/consensus_filter.py",
)
consensus = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(consensus)


def _entry():
    return {
        "pooled": [
            {"id": "C1", "audit": "S"},
            {"id": "C2", "audit": "TA"},
            {"id": "C3", "audit": "TB"},
            {"id": "C4", "audit": "TA"},
        ]
    }


def test_only_groups_with_both_sonnet_and_terra_are_kept():
    groups = [{"ids": ["C1", "C2"]}, {"ids": ["C3", "C4"]}]
    assert consensus.kept_ids(_entry(), groups) == {"C1", "C2"}


def test_terra_a_and_terra_b_agreement_does_not_count_as_two_detectors():
    groups = [{"ids": ["C2", "C3", "C4"]}, {"ids": ["C1"]}]
    assert consensus.kept_ids(_entry(), groups) == set()


def test_grouping_that_omits_a_finding_is_rejected_not_treated_as_a_singleton():
    with pytest.raises(ValueError):
        consensus.kept_ids(_entry(), [{"ids": ["C1", "C2"]}, {"ids": ["C3"]}])


def test_evaluate_splits_precision_between_kept_and_not_kept_findings():
    mapping = {"D1": _entry()}
    groupings = {"D1": [{"ids": ["C1", "C2"]}, {"ids": ["C3"]}, {"ids": ["C4"]}]}
    judged = {
        "D1": {
            "C1": {"matched_key_ids": ["K1"]},
            "C2": {"matched_key_ids": ["K1"]},
            "C3": {"matched_key_ids": ["K2"]},
            "C4": {"matched_key_ids": []},
        }
    }
    result = consensus.evaluate(mapping, groupings, judged)
    assert (result["kept"]["findings"], result["kept"]["matched"], result["kept"]["distinct_keys"]) == (2, 2, 1)
    assert (result["not_kept"]["findings"], result["not_kept"]["matched"]) == (2, 1)
    assert (result["tasks_flagged"], result["distinct_keys_all_findings"]) == (1, 2)
