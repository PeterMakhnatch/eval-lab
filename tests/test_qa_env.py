"""Retained source-review findings load without an environment reviewer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab.qa.models import load_findings_dir


@pytest.fixture
def source_finding() -> dict[str, Any]:
    return {
        "schema": "qa.finding/v1",
        "subject": {"kind": "task", "task_id": "package", "task_digest": "sha256:" + "ab" * 32},
        "stage": "source-review", "verdict": "fail", "category": "weak",
        "severity": "high", "fault": "eval",
        "summary": "The check accepts an integer without checking its value.",
        "evidence_refs": ["instruction.md:1", "tests/check.py:1-2"],
        "refutation": {"status": "unverified"},
        "reviewer": {"agent": "qa-env-test"},
        "measurement": "not_run", "root_cause": ["weak_check"],
    }


def test_recorded_source_finding_remains_suspected_in_shared_consumer(
    tmp_path: Path, source_finding: dict[str, Any],
) -> None:
    (tmp_path / "finding.json").write_text(json.dumps(source_finding))
    retained, = load_findings_dir(tmp_path)
    assert retained.subject.task_digest == source_finding["subject"]["task_digest"]
    assert retained.root_cause == ["weak_check"]
    assert retained.refutation.status == "unverified"
    assert retained.measurement == "not_run"
    assert retained.evidence_refs == ["instruction.md:1", "tests/check.py:1-2"]


def test_current_sealed_view_preserves_scorer_identity_and_denies_root_metadata(
    tmp_path: Path, source_finding: dict[str, Any],
) -> None:
    source_finding["subject"]["task_id"] = "terminal-bench/package"
    (tmp_path / "finding.json").write_text(json.dumps(source_finding))
    (tmp_path / "bundle.json").write_text(json.dumps({"schema": "qa.trace_bundle/v3"}))
    retained, = load_findings_dir(tmp_path)
    assert retained.subject.task_id == "terminal-bench/package"
    assert retained.subject.task_digest == source_finding["subject"]["task_digest"]
