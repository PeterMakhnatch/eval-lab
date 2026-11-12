"""Shared consumers retain source-review identity, cost, and uncertainty."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab.qa.models import load_findings_dir


@pytest.fixture
def finding() -> dict[str, Any]:
    return {
        "schema": "qa.finding/v1",
        "subject": {"kind": "task", "task_id": "package", "task_digest": "sha256:" + "ab" * 32},
        "stage": "source-review", "verdict": "fail", "category": "weak",
        "severity": "high", "fault": "eval",
        "summary": "The check accepts 7 although the instruction requires 42.",
        "evidence_refs": ["instruction.md:1", "tests/check.py:1-2"],
        "refutation": {"status": "unverified"},
        "reviewer": {"agent": "source-review", "model": "vendor/model-x", "cost_usd": 0.10},
        "measurement": "not_run", "root_cause": ["weak_check"],
    }


def test_several_defects_including_one_class_are_recorded_with_incremental_cost(
    tmp_path: Path, finding: dict[str, Any],
) -> None:
    second = {**finding, "summary": "The check never compares against 42.",
              "reviewer": {**finding["reviewer"], "cost_usd": 0.0}}
    oracle = {**finding, "summary": "The check rejects other valid integers.",
              "category": "broken", "root_cause": ["overstrict_oracle"],
              "reviewer": {**finding["reviewer"], "cost_usd": 0.15}}
    (tmp_path / "findings.jsonl").write_text(
        "\n".join(json.dumps(row) for row in [finding, second, oracle]) + "\n"
    )
    stored = load_findings_dir(tmp_path)
    assert sorted((f.root_cause[0], f.category) for f in stored) == [
        ("overstrict_oracle", "broken"), ("weak_check", "weak"), ("weak_check", "weak"),
    ]
    for item in stored:
        assert item.subject.task_id == "package"
        assert item.subject.task_digest == finding["subject"]["task_digest"]
        assert item.refutation.status == "unverified" and item.refutation.by is None
        assert item.measurement == "not_run"
        assert item.reviewer.model == "vendor/model-x"
    assert [f.reviewer.cost_usd for f in stored] == pytest.approx([0.10, 0.0, 0.15])


def test_a_repeated_source_review_record_is_one_unverified_claim(
    tmp_path: Path, finding: dict[str, Any],
) -> None:
    (tmp_path / "one.json").write_text(json.dumps(finding))
    (tmp_path / "duplicate.json").write_text(json.dumps(finding))
    only, = load_findings_dir(tmp_path)
    assert only.refutation.status == "unverified" and only.measurement == "not_run"
