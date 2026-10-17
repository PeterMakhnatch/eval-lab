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
