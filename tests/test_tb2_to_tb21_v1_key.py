import json
from pathlib import Path

KEY = Path(__file__).parents[1] / "research/answer-keys/tb2-to-tb21-v1"


def _items():
    return [json.loads(line) for line in (KEY / "items.jsonl").read_text().splitlines() if line.strip()]


def test_counts_and_pairs():
    items = _items()
    assert len(items) == 104
    by_task: dict[str, list[dict]] = {}
    for r in items:
        by_task.setdefault(r["paired_counterpart_id"].split(":")[-1], []).append(r)
    assert len(by_task) == 52
    modified = [t for t, rs in by_task.items() if rs[0]["subsplit"] == "pr53_modified"]
    controls = [t for t, rs in by_task.items() if rs[0]["subsplit"] == "format_only_control"]
    assert len(modified) == 26 and len(controls) == 26
    for rs in by_task.values():
        assert {r["subject"]["task_id"].split("/")[0] for r in rs} == {"tb2", "tb21"}
        assert {r["subject"]["task_digest"] for r in rs}  # digest-bound


def test_verdict_shape():
    for r in _items():
        version = r["subject"]["task_id"].split("/")[0]
        if r["subsplit"] == "pr53_modified" and version == "tb2":
            assert r["expected_verdict"] == "fail"
        else:
            assert r["expected_verdict"] == "pass"
        assert r["split"] == "test"


def test_pairs_load_for_scorer():
    from evallab.qa.answer_key import load_answer_key

    key = load_answer_key(KEY)
    assert len(key.pairs) == 52
    for pair in key.pairs:
        assert pair.child.startswith("tb2-to-tb21-v1:tb2:")
        assert pair.parent.startswith("tb2-to-tb21-v1:tb21:")


def test_unexposed_subset_keeps_canonical_pairs():
    import sys

    from evallab.qa.answer_key import load_answer_key

    repo = KEY.parents[2]
    sys.path.insert(0, str(repo / "research/experiments/tb2-to-tb21-v1"))
    import score_tb2_to_tb21 as scorer

    items = _items()
    root, excluded = scorer._subset_key(items)
    assert excluded == 24
    key = load_answer_key(root)
    assert len(key.items) == 80
    assert len(key.pairs) == 40
    exposed = {row["item_id"] for row in items if row["breaker_bench_dev_exposure"]}
    for pair in key.pairs:
        assert pair.child not in exposed
        assert pair.parent not in exposed


def test_tb21_transfer_is_a_false_discovery():
    import sys

    repo = KEY.parents[2]
    sys.path.insert(0, str(repo / "research/experiments/tb2-to-tb21-v1"))
    import run_tb2_to_tb21 as driver
    import score_tb2_to_tb21 as scorer

    cell = driver.TaskCell(
        "tb2-to-tb21-v1:pair:fix-git",
        "pair",
        "terminal-bench-2",
        "fix-git",
        {"subsplit": "pr53_modified"},
    )
    items = {
        cell.item_id(version): {"subsplit": "pr53_modified", "item_id": cell.item_id(version)}
        for version in ("tb2", "tb21")
    }
    state = type("State", (), {})()
    state.replays = {
        (cell.cell_id, "floor", 1, "tb2"): {"reward": 1.0},
        (cell.cell_id, "floor", 1, "tb21"): {"reward": 1.0},
    }
    found = scorer._false_discoveries(cell, "floor", state, items)
    assert [item["item_id"] for item, _replay in found] == [cell.item_id("tb21")]

    state.replays[(cell.cell_id, "floor", 1, "tb21")] = {"reward": 0.0}
    assert scorer._false_discoveries(cell, "floor", state, items) == []
