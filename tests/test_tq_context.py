"""Synthetic DEV admission and bounded diagnostic context; no live model/runtime."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab.production.cloud_trace_db import (
    CloudTraceDatabase,
    CloudTraceRetriever,
    RetrievedExample,
)
from evallab.production.corpus_queue import CorpusRegistry
from evallab.production.tq_context import MAX_CONTEXT_CHARS, assemble_audit_context


@pytest.fixture
def registry(tmp_path: Path) -> CorpusRegistry:
    keys = tmp_path / "research/answer-keys"
    ledger = keys / "splits/assignments.csv"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        "key_id,item_id,task,split\n"
        "tb4-audit-v1,target,target,dev\n"
        "tb4-audit-v1,related,related,dev\n"
        "tb4-audit-v1,sealed,sealed,test\n",
        encoding="utf-8",
    )
    for key in ("tb4-audit-v1", "cheatbench-actions-v1", "hacktrace-act-v1"):
        directory = keys / key
        directory.mkdir()
        rows = [] if key != "tb4-audit-v1" else [
            {"item_id": task, "split": split, "subject": {"task_id": task}}
            for task, split in (("target", "dev"), ("related", "dev"), ("sealed", "test"))
        ]
        (directory / "items.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8",
        )
    (keys / "tb4-audit-v1/tasks.jsonl").write_text("", encoding="utf-8")
    inventory = tmp_path / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(json.dumps({
        "dataset_id": "mimo-defect-map-v1",
        "heldout_task_ids_excluded_from_training_all_versions": [], "tasks": [],
    }), encoding="utf-8")
    return CorpusRegistry(tmp_path)


def _task(db: CloudTraceDatabase, task: str, *, split: str = "dev") -> None:
    db.upsert_task({
        "task_id": task, "dataset_id": "tb4-audit-v1", "split": split,
        "instruction_text": "Check structured diagnostic output.",
        "metadata_json": {"item_id": task},
    })


def _trace(db: CloudTraceDatabase, trace: str, task: str, **extra: Any) -> None:
    db.upsert_trace({
        "trace_id": trace, "run_id": "run-" + trace, "task_id": task,
        "model_id": "synthetic/model", "outcome": "failed", "reward": 0.0,
        **extra,
    }, steps=[
        {"step_index": 1, "role": "assistant", "command_or_action": "DO NOT COPY ACTION"},
        {"step_index": 2, "role": "tool", "observation_preview": "AssertionError: expected value"},
    ])


def test_same_task_failure_is_one_dev_diagnostic_without_actions(
    tmp_path: Path, registry: CorpusRegistry,
) -> None:
    with CloudTraceDatabase(tmp_path / "db.sqlite") as db:
        _task(db, "target")
        _task(db, "related")
        _task(db, "sealed", split="test")
        _trace(db, "a-fail", "target")
        _trace(db, "z-labelled", "target", is_task_defect_fail=True)
        _trace(db, "other-fail", "related")
        _trace(db, "sealed-fail", "sealed")
        retriever = CloudTraceRetriever(db, registry=registry)
        failed = retriever.failed_run_summary("namespace/target@version")
        assert failed is not None
        assert failed["trace_id"] == "z-labelled"
        assert failed["split"] == "dev"
        assert failed["observations"] == [
            {"step_index": 2, "observation": "AssertionError: expected value"},
        ]
        assert "DO NOT COPY ACTION" not in json.dumps(failed)
        assert retriever.failed_run_summary("sealed") is None
        assert retriever.failed_run_summary("unknown") is None


def test_no_failure_for_pass_or_unadmitted_target(
    tmp_path: Path, registry: CorpusRegistry, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with CloudTraceDatabase(tmp_path / "db.sqlite") as db:
        _task(db, "target")
        _trace(db, "pass", "target", outcome="passed", reward=1.0)
        retriever = CloudTraceRetriever(db, registry=registry)
        assert retriever.failed_run_summary("target") is None
        rows = db._rows
        reads: list[str] = []

        def observed(query: str, parameters: Any = ()) -> Any:
            reads.append(query)
            return rows(query, parameters)

        monkeypatch.setattr(db, "_rows", observed)
        assert retriever.failed_run_summary("sealed") is None
        assert not any("FROM agent_traces" in query or "FROM trace_steps" in query for query in reads)


class FakeRetriever:
    def __init__(self, failed: dict[str, Any] | None = None, examples: list[RetrievedExample] | None = None) -> None:
        self.failed = failed
        self.examples = examples or []
        self.requests: list[dict[str, Any]] = []

    def failed_run_summary(self, task_id: str) -> dict[str, Any] | None:
        self.requests.append({"same_task": task_id})
        return self.failed

    def retrieve_similar_examples(self, query: str, **kwargs: Any) -> list[RetrievedExample]:
        self.requests.append({"query": query, **kwargs})
        return self.examples


def _example(task: str, **extra: Any) -> RetrievedExample:
    return RetrievedExample(
        task_id=task, dataset_id="synthetic", issue_id="issue-" + task,
        defect_class="incomplete_check", direction="wrong_pass", status="confirmed",
        citation="tests/check.py:2", differential="original reward 1 -> repaired reward 0",
        issue={"defect_class": "incomplete_check", "reproducer_script": "NEVER INCLUDE SCRIPT"},
        reproducer="NEVER INCLUDE SCRIPT", **extra,
    )


def test_context_top_three_leave_target_out_and_provenance() -> None:
    retriever = FakeRetriever(
        {"task_id": "target", "split": "dev", "trace_id": "failed-1", "reward": 0.0},
        [_example("related-1"), _example("related-2"), _example("related-3"), _example("related-4")],
    )
    result = assemble_audit_context(
        retriever, task_id="target", query="structured output diagnostic extra", source_digest="a" * 64,  # type: ignore[arg-type]
    )
    assert result["failed_run"]["trace_id"] == "failed-1"
    assert len(result["related_issues"]) == 3
    assert retriever.requests[1] == {
        "query": "structured output diagnostic", "kind": "task_quality", "top_k": 3,
        "exclude_task_ids": {"target"},
    }
    assert result["provenance"]["source_sha256"] == "a" * 64
    assert len(result["provenance"]["context_sha256"]) == 64
    assert "UNTRUSTED" in result["notice"]
    assert "NEVER INCLUDE SCRIPT" not in json.dumps(result)


@pytest.mark.parametrize("example", [
    _example("namespace/target@old"), _example("related", split="test"),
])
def test_context_rejects_leaky_retriever(example: RetrievedExample) -> None:
    with pytest.raises(ValueError, match="DEV.*leave"):
        assemble_audit_context(FakeRetriever(examples=[example]), task_id="target", query="output")  # type: ignore[arg-type]


@pytest.mark.parametrize("failed", [
    {"task_id": "other", "split": "dev"}, {"task_id": "target", "split": "test"},
])
def test_context_rejects_wrong_or_sealed_failed_summary(failed: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="same task"):
        assemble_audit_context(FakeRetriever(failed), task_id="target", query="output")  # type: ignore[arg-type]


def test_context_bounds_fields_and_distinguishes_missing_evidence() -> None:
    huge = "雪\n\\" * 20_000
    failed = {
        "task_id": "target", "split": "dev", "outcome": huge,
        "observations": [{"step_index": index, "observation": huge} for index in range(20)],
    }
    examples = [
        _example(f"related-{index}", provenance={"run_id": huge, "raw_payload": huge})
        for index in range(10)
    ]
    result = assemble_audit_context(FakeRetriever(failed, examples), task_id="target", query="output")  # type: ignore[arg-type]
    assert len(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) <= MAX_CONTEXT_CHARS
    assert len(result["failed_run"]["observations"]) == 3
    assert len(result["related_issues"]) == 3
    assert "raw_payload" not in json.dumps(result)
    missing = assemble_audit_context(FakeRetriever(), task_id="target", query="output")  # type: ignore[arg-type]
    assert missing["failed_run"] is None
    assert missing["related_issues"] == []
    assert missing["provenance"]["failed_run_available"] is False
