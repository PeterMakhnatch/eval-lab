"""Tests for `research/answer-keys/breaker-bench-v1/` (`qa.answer_key/v1`)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEY_DIR = ROOT / "research" / "answer-keys" / "breaker-bench-v1"


def test_breaker_bench_v1_build_check_and_invariants() -> None:
    res = subprocess.run(
        [sys.executable, str(KEY_DIR / "build.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr

    manifest = json.loads((KEY_DIR / "manifest.json").read_text(encoding="utf-8"))
    items = [
        json.loads(line)
        for line in (KEY_DIR / "items.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    pairs = [
        json.loads(line)
        for line in (KEY_DIR / "pairs.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    tw_upstream = json.loads(
        (KEY_DIR / "upstream" / "terminal_wrench_tasks.json").read_text(encoding="utf-8")
    )

    assert len(items) == 626 == manifest["files"]["items_jsonl"]["count"]
    assert len(pairs) == 214 == manifest["files"]["pairs_jsonl"]["count"]

    by_id = {row["item_id"]: row for row in items}
    assert len(by_id) == len(items)

    # Required qa.answer_key/v1 fields on every item; unmeasured is never pass.
    for row in items:
        assert row["schema"] == "qa.answer_key/v1"
        assert row["key_id"] == "breaker-bench-v1"
        assert row["subject"]["kind"] == "task"
        assert row["subject"]["task_id"]
        assert row["subject"]["task_digest"].startswith("sha256:")
        assert row["expected_verdict"] in ("fail", "pass", "unknown")
        assert row["split"] in ("dev", "test")
        if row.get("role") == "unmeasured":
            assert row["expected_verdict"] == "unknown"

    # Pairs never straddle splits and link a fail child (0.2.0) to a pass parent (0.2.2).
    for pair in pairs:
        child = by_id[pair["child"]]
        parent = by_id[pair["parent"]]
        assert child["sandbox_version"] == "0.2.0"
        assert parent["sandbox_version"] == "0.2.2"
        assert child["expected_verdict"] == "fail"
        assert parent["expected_verdict"] == "pass"
        assert child["split"] == parent["split"] == pair["split"]

    # Zero overlap with auditor-sft-v1's 139 training tasks.
    train_tids = {
        f"terminal-wrench/{r['task_id']}"
        for r in tw_upstream["tasks"]
        if r["auditor_sft_split"] == "train_139"
    }
    assert len(train_tids) == 139
    eval_tids = {
        row["subject"]["task_id"]
        for row in items
        if row["source_set"] == "terminal_wrench_holdout"
    }
    assert len(eval_tids) == 192
    assert not (train_tids & eval_tids)

    # All 5 frozen staged holdout tasks are in the test split.
    staged = [row for row in items if row["subsplit"] == "tw_holdout_5_staged"]
    assert len(staged) == 5
    assert all(row["split"] == "test" and row["expected_verdict"] == "fail" for row in staged)
