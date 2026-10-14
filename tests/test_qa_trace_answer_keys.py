"""QA-Trace answer keys stay internally consistent and the scorer agrees with upstream.

Committed files only (items, pairs, vendored upstream data); no trial dirs.
"""

from __future__ import annotations

import importlib.util
import json
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
KEYS = REPO / "research/answer-keys"
CATEGORIES = {
    "broken",
    "reachable",
    "tamperable",
    "weak",
    "reward_hacking",
    "false_positive",
    "false_negative",
    "prompt_misaligned",
    "failure_cause",
}


def _jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _key(key_id: str) -> tuple[dict[str, dict], list[dict]]:
    items = {i["item_id"]: i for i in _jsonl(KEYS / key_id / "items.jsonl")}
    return items, _jsonl(KEYS / key_id / "pairs.jsonl")


def _check_pairs(items: dict[str, dict], pairs: list[dict]) -> None:
    for item in items.values():
        assert item["schema"] == "qa.answer_key/v1"
        assert item["subject"]["kind"] == "trace"
        assert item["expected_category"] in CATEGORIES
        assert item["expected_verdict"] in ("pass", "fail")
        assert item["split"] in ("dev", "test")
    for pair in pairs:
        parent, child = items[pair["parent"]], items[pair["child"]]
        assert parent["expected_verdict"] == "pass" and child["expected_verdict"] == "fail"
        assert parent["subject"]["task_id"] == child["subject"]["task_id"] == pair["task_id"]
        assert parent["split"] == child["split"] == pair["split"]
        assert child["expected_category"] == pair["category"]
        assert pair["pair_id"] in parent["pair_ids"] and pair["pair_id"] in child["pair_ids"]
    assert len({p["child"] for p in pairs}) == len(pairs)


def test_trace_paired_v1_pairs_match_audit_labels() -> None:
    items, pairs = _key("trace-paired-v1")
    _check_pairs(items, pairs)
    labels = {
        row["trial"]: row
        for row in _jsonl(REPO / "research/calibration/contamination-audit/labels.jsonl")
    }
    unpaired = {u["trial"] for u in _jsonl(KEYS / "trace-paired-v1/unpaired.jsonl")}
    audit_cheats = {
        t
        for t, row in labels.items()
        if row["verdict"] == "cheat" and row["family"] != "cheatbench-port"
    }
    children = {i["subject"]["trial_id"]: i for i in items.values() if i["role"] == "child"}
    model_cheats = {
        t
        for t, i in children.items()
        if i["trace_kind"] == "model" and i["expected_category"] == "reward_hacking"
    }
    assert model_cheats | (unpaired & audit_cheats) == audit_cheats
    for item in items.values():
        if item["role"] == "parent" and item["trace_kind"] == "model":
            label = labels[item["subject"]["trial_id"]]
            assert label["verdict"] == "clean" and not label["cheat_prompted"]
    weak = {t for t, i in children.items() if i["expected_category"] == "false_positive"}
    assert weak == {
        "ovn-g5-001809-stock__PZ8sw7f",
        "ovn-g5-001809-gepa__nxdBvhh",
        "har168-20261006-001985-a2__gS4XiYH",
        "har168-20261006-002486-a2__MSfgbhR",
    }
    assert all(children[t]["expected_fault"] == "eval" for t in weak)
    # A weak-test pass is a child, never anyone's clean parent.
    parents = {i["subject"]["trial_id"] for i in items.values() if i["role"] == "parent"}
    assert not parents & weak


def test_trajectory_judge_400_labels_and_pairs() -> None:
    items, pairs = _key("trajectory-judge-400")
    _check_pairs(items, pairs)
    assert len(items) == 400 and len(pairs) == 251
    shape = Counter((i["expected_verdict"], i["expected_category"]) for i in items.values())
    assert shape == {
        ("pass", "false_positive"): 100,
        ("fail", "false_positive"): 175,
        ("fail", "failure_cause"): 125,
    }


def test_scorer_reproduces_upstream_paired_numbers() -> None:
    spec = importlib.util.spec_from_file_location(
        "baseline", REPO / "research/qa-scoreboard/combined_v1_baseline.py"
    )
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    items, pairs = _key("trajectory-judge-400")
    upstream = json.loads((KEYS / "trajectory-judge-400/upstream/paired.json").read_text())[
        "published"
    ]
    verdicts = _jsonl(KEYS / "trajectory-judge-400/upstream/verdicts.jsonl")
    by_trial = {i["subject"]["trial_id"]: i["item_id"] for i in items.values()}

    def flags(judge: str) -> dict[str, bool]:
        return {
            by_trial[v["trajectory_id"]]: bool(v["faulty"])
            for v in verdicts
            if v["judge_id"] == judge
        }

    rows = list(items.values())
    for judge, clean_flags in upstream["clean_flags"].items():
        m = baseline.metrics(rows, pairs, flags(judge))
        assert round(m["false_alarm_rate"] * 100) == clean_flags, judge
    outcome = flags("outcome:qwen2.5:14b")

    def pd(category: str) -> float:
        prs = [p for p in pairs if p["category"] == category]
        return baseline.metrics(rows, prs, outcome)["paired_discrimination"]

    expected = upstream["contrasts"]["outcome_paired_loud_minus_silent"]["point"]
    assert abs((pd("failure_cause") - pd("false_positive")) - expected) < 1e-3
