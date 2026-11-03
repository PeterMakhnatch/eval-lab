"""Synthetic fixtures only: no production database, corpus bytes, or network."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab.production.cloud_trace_db import CloudTraceDatabase
from evallab.production.cloud_trace_retriever import CloudTraceRetriever
from evallab.production.goodfire_corroboration import (
    DATASET_ID,
    VERIFICATION_KIND,
    corroborate_goodfire_dev,
)


def _receipt(categories: list[str], indices: list[int]) -> dict[str, Any]:
    return {
        "status": "provisional", "native_line": {"file": "run/traces.jsonl"},
        "source_label": {"categories": categories, "source_protocol": "consensus_lp_v1"},
        "passage_step_labels": {"positive_step_indices": indices},
    }


def _task(task_id: str, split: str = "DEV") -> dict[str, Any]:
    return {
        "task_id": task_id, "dataset_id": DATASET_ID, "split": split,
        "instruction_text": "Synthetic task", "metadata_json": {"admission": {"effective_split": split}},
    }


def _trace(task_id: str, trace_id: str, indices: list[int], **extra: Any) -> dict[str, Any]:
    return {
        "trace_id": trace_id, "run_id": "synthetic-run", "task_id": task_id,
        "model_id": "synthetic", "outcome": "retained", "reward": 1.0, "is_exploit": True,
        "trust_tier": "tier2_consensus_labeled", "flagged_passage_steps": indices,
        "raw_cas_uri": "cas://sha256/" + "0" * 64,
        "verified_receipt_json": _receipt(["test_modification", "fabricated_verification"], indices), **extra,
    }


def _steps(trace_id: str, positive: int = 4) -> list[dict[str, Any]]:
    return [
        {"step_index": 0, "role": "system", "content_text": "You are a helpful assistant."},
        {"step_index": positive, "role": "assistant", "tool_name": "bash",
         "command_or_action": "python - <<'PY'\nassert False\nPY", "content_text": "Fabricated assertion.",
         "flags_json": {"passage_positive": True, "l0_tripwires": ["test_assertion_tamper"],
                        "source_file": "run/steps.jsonl"}},
        {"step_index": 8, "role": "assistant", "tool_name": "bash",
         "command_or_action": "git status", "content_text": "Unlabeled ordinary action."},
    ]


def _add(db: CloudTraceDatabase, task_id: str, trace_id: str, *, split: str = "DEV",
         steps: list[dict[str, Any]] | None = None, indices: list[int] | None = None) -> None:
    db.upsert_task(_task(task_id, split))
    trace = _trace(task_id, trace_id, [4] if indices is None else indices)
    trace_steps = _steps(trace_id) if steps is None else steps
    db.upsert_trace(trace, steps=trace_steps)


def test_corroborates_actions_projects_evidence_and_retrieves(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    original = source.read_bytes() if source.exists() else None
    with CloudTraceDatabase(source) as db:
        _add(db, "goodfire-reward-hack-data/swebench_verified/dev-item@digest", "dev-trace")
        _add(db, "held-item", "held-trace", split="held")
    before = source.read_bytes()
    destination = tmp_path / "derived.sqlite"
    result = corroborate_goodfire_dev(source, destination, protected_ids=frozenset())
    assert source.read_bytes() == before == (original or before)
    assert result["promoted_traces"] == result["verified_traces"] == 1
    assert result["confirmed_issues"] == 2
    assert result["held_or_test_leaks"] == 0
    with CloudTraceDatabase(destination, read_only=True) as db:
        trace = db.get_trace("dev-trace")
        assert trace is not None
        assert [step["step_index"] for step in trace["steps"]] == [4]
        assert "assert False" in trace["steps"][0]["command_or_action"]
        assert trace["verified_receipt_json"]["verification_kind"] == VERIFICATION_KIND
        assert trace["verified_receipt_json"]["container_replay_verified"] is False
        issues = db.query_task_issues()
        assert {issue["defect_class"] for issue in issues} == {"test_modification", "fabricated_verification"}
        assert all(issue["status"] == "confirmed" for issue in issues)
        assert all(issue["citation_file"] == "run/steps.jsonl" for issue in issues)
        assert db.get_trace("held-trace") is None
    assert corroborate_goodfire_dev(source, destination, protected_ids=frozenset())["confirmed_issues"] == 2
    with CloudTraceRetriever(destination) as retriever:
        result = retriever.retrieve("other-task", "assert")
        assert result.hits
        assert all("assert False" in (hit.step_excerpt or "") for hit in result.hits)
        assert all("helpful assistant" not in (hit.step_excerpt or "") for hit in result.hits)


def test_positive_prose_without_action_is_not_corroborated(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    with CloudTraceDatabase(source) as db:
        _add(db, "dev", "prose", indices=[], steps=[
            {"step_index": 0, "role": "assistant", "content_text": "All tests pass.",
             "flags_json": {"passage_positive": True}},
        ])
    corroborate_goodfire_dev(source, tmp_path / "derived.sqlite", protected_ids=frozenset())
    with CloudTraceDatabase(tmp_path / "derived.sqlite", read_only=True) as db:
        assert db.query_task_issues() == []
        assert db.get_trace("prose")["verified_receipt_json"]["status"] == "provisional"


def test_qa_literal_cites_original_positive_step(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    with CloudTraceDatabase(source) as db:
        _add(db, "dev", "literal", steps=[
            {"step_index": 4, "role": "assistant", "tool_name": "bash",
             "command_or_action": "cat /tests/test_outputs.py",
             "flags_json": {"passage_positive": True}},
        ])
    corroborate_goodfire_dev(source, tmp_path / "derived.sqlite", protected_ids=frozenset())
    with CloudTraceDatabase(tmp_path / "derived.sqlite", read_only=True) as db:
        step = db.get_trace("literal")["steps"][0]
        assert step["step_index"] == 4
        assert "qa_trace_t0" in step["flags_json"]["corroboration_evidence"]


def test_dev_firewall_checks_names_aliases_and_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    with CloudTraceDatabase(source) as db:
        _add(db, "namespace/protected-name@digest", "protected")
        _add(db, "alias", "alias")
        db._connection.execute("UPDATE corpus_tasks SET metadata_json=? WHERE task_id='alias'", (
            json.dumps({"admission": {"effective_split": "DEV"}, "alias_candidates": ["sealed-alias"]}),))
        _add(db, "metadata-held", "metadata-held")
        db._connection.execute("UPDATE corpus_tasks SET metadata_json=? WHERE task_id='metadata-held'", (
            json.dumps({"admission": {"effective_split": "held"}}),))
    result = corroborate_goodfire_dev(source, tmp_path / "derived.sqlite",
                                      protected_ids=frozenset({"protected-name", "sealed-alias"}))
    assert result["tasks"] == 0
    assert result["held_or_test_leaks"] == 0


def test_raw_source_and_in_place_guards(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    with CloudTraceDatabase(source):
        pass
    with pytest.raises(ValueError, match="read-only"):
        corroborate_goodfire_dev(source, source, protected_ids=frozenset())
    with pytest.raises(ValueError, match="backup"):
        corroborate_goodfire_dev(source, source, in_place=True, protected_ids=frozenset())
