import importlib.util
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1] / "research/experiments/pj-self-cb2a4d-4-finding-cap"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"pj_cap_{name}", BASE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


judge = _load("judge_arms")


class _Transport:
    """Stand-in for the production answer validator: rejects more than three findings after other checks."""

    def __init__(self):
        self.seen = []

    def _answer(self, data, expected_array):
        import json

        rows = json.loads(data)[expected_array]
        self.seen.append(len(rows))
        if len(rows) > 3:
            raise ValueError("finding budget exceeded")
        return {expected_array: rows}

    @staticmethod
    def _json(text):
        import json

        return json.loads(text)


def _meta(arm, case, keys_raw, eligible, parity="even"):
    return {
        f"{arm}:{case}": {
            "arm": arm,
            "case": case,
            "parity": parity,
            "tokens": 1000,
            "raw": keys_raw,
            "eligible_ids": eligible,
        }
    }


def test_six_finding_answer_is_accepted_only_up_to_the_arm_cap():
    pytest.importorskip("evallab")
    driver_spec = importlib.util.spec_from_file_location(
        "pj_cap_driver_stub", BASE / "cap_driver.py"
    )
    # The driver imports the OQ-8 driver at import time; skip where that retained helper is not importable.
    try:
        driver = importlib.util.module_from_spec(driver_spec)
        driver_spec.loader.exec_module(driver)
    except (FileNotFoundError, ImportError):
        pytest.skip("OQ-8 driver unavailable")
    import json

    helpers = {"transport": _Transport()}
    five = json.dumps({"findings": [{"i": n} for n in range(5)]}).encode()
    assert len(driver.budgeted_answer(helpers, five, 6)["findings"]) == 5
    with pytest.raises(ValueError):
        driver.budgeted_answer(helpers, five, 3)
    seven = json.dumps({"findings": [{"i": n} for n in range(7)]}).encode()
    with pytest.raises(ValueError):
        driver.budgeted_answer(helpers, seven, 6)


def test_metrics_exclude_a_task_whose_audit_was_invalid_from_every_arm(tmp_path, monkeypatch):
    meta = {}
    for arm in judge.ARMS:
        meta |= _meta(arm, "D1", 2, ["C1"])
        meta |= _meta(arm, "D2", None if arm == "S6" else 2, ["C1"])
    judged = {
        f"{arm}-{case}": {"C1": {"matched_key_ids": [f"K-{case}"]}}
        for arm in judge.ARMS
        for case in ("D1", "D2")
    }
    common = {
        c for c in ("D1", "D2") if all(meta[f"{a}:{c}"]["raw"] is not None for a in judge.ARMS)
    }
    assert common == {"D1"}
    row = judge.arm_metrics(meta, judged, "T6", common)
    assert (row["distinct_keys"], row["eligible_findings"], row["tasks_with_hit"]) == (1, 1, 1)


def test_arm_metrics_counts_a_key_once_and_marks_cells_at_the_cap():
    meta = {}
    meta |= _meta("T6", "D1", 6, ["C1", "C2"])
    meta |= _meta("T6", "D2", 2, ["C1"], parity="odd")
    judged = {
        "T6-D1": {"C1": {"matched_key_ids": ["K1"]}, "C2": {"matched_key_ids": ["K1"]}},
        "T6-D2": {"C1": {"matched_key_ids": []}},
    }
    row = judge.arm_metrics(meta, judged, "T6", {"D1", "D2"})
    assert (row["distinct_keys"], row["matched_findings"], row["eligible_findings"]) == (1, 2, 3)
    assert row["cells_at_cap"] == 1 and row["by_parity"] == {"even": 1, "odd": 0}
    assert row["key_backed_precision"] == round(2 / 3, 3)
