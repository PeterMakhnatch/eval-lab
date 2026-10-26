"""Unit tests for the deterministic training-data lints (pE-SELF-9)."""

from __future__ import annotations

import json
from pathlib import Path

from evallab.data_lint import (
    body_of,
    lint_dataset,
    lint_registry_split_leak,
    lint_rows,
    lint_split_leak,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS = REPO_ROOT / "research/datasets/seeker-sft-v1/seeker_tool_sft.jsonl"
ANSWER_KEYS = REPO_ROOT / "research/answer-keys/tb4-audit-v1/tasks.jsonl"
GYM = REPO_ROOT / "research/experiments/seeker-gym-v0/gym.json"


def _row(task: str, body: str) -> dict[str, object]:
    return {"task_id": task, "files": {"submission/solve.sh": body}}


def test_body_of_reads_every_row_shape() -> None:
    assert body_of(_row("a", "x")) == "x"
    assert body_of({"task": "a", "submission": {"submission/solve.sh": "y"}}) == "y"
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": "submission/solve.sh", "content": "z"}),
                    }
                }
            ],
        }
    ]
    assert body_of({"task_id": "a", "messages": messages}) == "z"
    assert body_of({"task_id": "a"}) is None


def test_dedup_reports_distinct_pairs_not_rows() -> None:
    findings = lint_rows([_row("a", "x"), _row("a", "x"), _row("b", "y")])
    dedup = [f for f in findings if f["check"] == "dedup" and f["severity"] == "high"]
    assert len(dedup) == 1
    assert dedup[0]["rows"] == 3
    assert dedup[0]["distinct_task_body_pairs"] == 2
    assert dedup[0]["extra_copies"] == 1


def test_dedup_is_quiet_when_every_row_is_distinct() -> None:
    findings = lint_rows([_row("a", "x"), _row("b", "y")])
    assert not [f for f in findings if f["check"] == "dedup" and f["severity"] == "high"]


def test_split_leak_flags_only_held_out_tasks(tmp_path: Path) -> None:
    keys = tmp_path / "tasks.jsonl"
    keys.write_text(
        json.dumps({"task": "terminal-bench/leaky", "split": "test"})
        + "\n"
        + json.dumps({"task": "terminal-bench/ok", "split": "dev"})
        + "\n",
        encoding="utf-8",
    )
    findings = lint_split_leak([_row("leaky", "x"), _row("ok", "y")], keys)
    assert len(findings) == 1
    assert findings[0]["leaked_tasks"] == ["leaky"]


def test_registry_split_leak_flags_a_dev_family_holding_held_out_tasks(tmp_path: Path) -> None:
    keys = tmp_path / "tasks.jsonl"
    keys.write_text(
        json.dumps({"task": "terminal-bench/leaky", "split": "test"})
        + "\n"
        + json.dumps({"task": "terminal-bench/ok", "split": "dev"})
        + "\n",
        encoding="utf-8",
    )
    registry = tmp_path / "gym.json"
    registry.write_text(
        json.dumps(
            {
                "pairs": [
                    {"family": "tb4-dev", "task": "leaky"},
                    {"family": "tb4-dev", "task": "ok"},
                ]
            }
        ),
        encoding="utf-8",
    )
    findings = lint_registry_split_leak(registry, keys)
    assert len(findings) == 1
    assert findings[0]["leaked_tasks"] == ["leaky"]
    assert findings[0]["dev_tasks"] == ["ok"]


def test_committed_corpus_reports_its_known_dedup_issue_and_no_split_leak() -> None:
    report = lint_dataset(CORPUS, answer_keys=ANSWER_KEYS)
    assert report["rows"] == 90
    dedup = [f for f in report["findings"] if f["check"] == "dedup" and f["severity"] == "high"]
    assert len(dedup) == 1
    assert dedup[0]["distinct_task_body_pairs"] == 39
    # The corpus itself is clean of held-out tasks; that is a separate, passing check.
    assert not [f for f in report["findings"] if f["check"] == "split_leak"]


def test_gym_registry_reports_the_41_mislabelled_held_out_pairs() -> None:
    report = lint_dataset(CORPUS, answer_keys=ANSWER_KEYS, registry=GYM)
    leak = [f for f in report["findings"] if f["check"] == "split_leak"]
    assert len(leak) == 1
    assert leak[0]["subject"] == "registry:tb4-dev"
    assert len(leak[0]["leaked_tasks"]) == 41
    assert len(leak[0]["dev_tasks"]) == 25
