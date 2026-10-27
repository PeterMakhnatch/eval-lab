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
from evallab.production.tq_context import (
    MAX_CONTEXT_CHARS,
    assemble_audit_context,
    is_proposal_eligible,
    resolve_mechanism_description,
)


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


@pytest.mark.parametrize("verified, floor", [(True, 0.5), (False, 0.8)])
@pytest.mark.parametrize("offset, expected", [(-0.000001, False), (0.0, True), (0.000001, True)])
def test_proposal_eligibility_boundaries(verified: bool, floor: float, offset: float, expected: bool) -> None:
    assert is_proposal_eligible(floor + offset, verified) is expected


@pytest.mark.parametrize("confidence, verified, verified_floor, unverified_floor, expected", [
    (0.6, True, 0.7, 0.8, False),
    (0.7, True, 0.7, 0.8, True),
    (0.6, False, 0.9, 0.6, True),
    (0.59, False, 0.1, 0.6, False),
    (0.5, True, 0.5, 0.9, True),
    (0.8, False, 0.9, 0.8, True),
    (0, True, 0.0, 1.0, True),
    (0, False, 1.0, 0.0, True),
    (1, True, 1.0, 0.8, True),
    (1, False, 0.5, 1.0, True),
])
def test_proposal_eligibility_threshold_overrides(
    confidence: float, verified: bool, verified_floor: float, unverified_floor: float, expected: bool,
) -> None:
    assert is_proposal_eligible(
        confidence, verified,
        min_verified_confidence=verified_floor,
        min_unverified_confidence=unverified_floor,
    ) is expected


@pytest.mark.parametrize("confidence", [
    -0.001, 1.001, float("nan"), float("inf"), float("-inf"),
    10 ** 400, -(10 ** 400), True, False, "0.9", None,
])
@pytest.mark.parametrize("verified", [True, False])
def test_proposal_eligibility_rejects_invalid_confidence(confidence: Any, verified: bool) -> None:
    assert is_proposal_eligible(confidence, verified) is False


@pytest.mark.parametrize("verified", [0, 1, None, "true", "false", [], {}])
def test_proposal_eligibility_requires_actual_citation_boolean(verified: Any) -> None:
    assert is_proposal_eligible(1.0, verified) is False


@pytest.mark.parametrize("floor", [-0.001, 1.001, float("nan"), float("inf"), True, "0.5", None])
@pytest.mark.parametrize("verified", [True, False])
def test_proposal_eligibility_rejects_invalid_thresholds(floor: Any, verified: bool) -> None:
    assert is_proposal_eligible(1.0, verified, min_verified_confidence=floor) is False
    assert is_proposal_eligible(1.0, verified, min_unverified_confidence=floor) is False


_MIMO_TASK = "format-code-task-999001"
_MIMO_PACKAGE = "sha256:" + "1" * 64
_MIMO_GENERIC_SUMMARY = (
    "Archived instruction-adjudicated wrong-patch witness; taxonomy mapping is "
    "inference, not fresh differential confirmation."
)
_MIMO_REASONS = {
    "block": "The report checker omits the requested title comparison, leaving a stale title undetected.",
    "line-0123456789": "The report checker counts rows without checking unique identifiers, leaving duplicates undetected.",
}
_MIMO_PROGRAMS = {"block": "a" * 64, "line-0123456789": "b" * 64}


def _mimo_finding(witness_id: str, *, projected: bool = False) -> dict[str, Any]:
    identity = f"mimo-weak-tests-v2.1:{_MIMO_TASK}:{witness_id}"
    if projected:
        return {
            "key_id": identity, "package_digest": _MIMO_PACKAGE,
            "summary": _MIMO_GENERIC_SUMMARY, "direction": "false_pass",
        }
    return {
        "finding_id": identity, "source_finding_id": identity,
        "task_id": _MIMO_TASK, "package_digest": _MIMO_PACKAGE,
        "primary_class": {"id": "TS01", "key": "weak_check"},
        "safe_summary": _MIMO_GENERIC_SUMMARY,
        "program": {"source_program_id": identity, "sha256": "sha256:" + _MIMO_PROGRAMS[witness_id]},
    }


def _mimo_atlas_row() -> dict[str, Any]:
    return {
        "task_id": _MIMO_TASK, "package_digest": _MIMO_PACKAGE,
        "witnesses": [
            {
                "id": witness_id, "sha256": _MIMO_PROGRAMS[witness_id],
                "reason": reason, "review_label": "required_gap",
                "raw_patch": "NEVER EXPORT RAW PATCH",
            }
            for witness_id, reason in _MIMO_REASONS.items()
        ],
    }


@pytest.mark.parametrize("witness_id", ["block", "line-0123456789"])
@pytest.mark.parametrize("projected", [False, True])
def test_mechanism_resolves_exact_mimo_witness_without_mutating_inputs(
    witness_id: str, projected: bool,
) -> None:
    item, row = _mimo_finding(witness_id, projected=projected), _mimo_atlas_row()
    before = json.dumps([item, row], sort_keys=True)
    assert resolve_mechanism_description(item, retained_atlas_row=row) == _MIMO_REASONS[witness_id]
    assert json.dumps([item, row], sort_keys=True) == before
    assert resolve_mechanism_description(item) is None


@pytest.mark.parametrize("program", [
    {}, {"sha256": "a" * 64}, {"sha256": "sha256:" + "a" * 64},
])
def test_mimo_program_digest_is_optional_but_bound_when_supplied(program: dict[str, Any]) -> None:
    item = _mimo_finding("block")
    item["program"] = program
    assert resolve_mechanism_description(item, retained_atlas_row=_mimo_atlas_row()) == _MIMO_REASONS["block"]


@pytest.mark.parametrize("override", [
    {"task_id": "format-code-task-999002"},
    {"package_digest": "sha256:" + "2" * 64},
    {"finding_id": f"mimo-weak-tests-v2.1:{_MIMO_TASK}:line-0123456789"},
    {"source_finding_id": f"mimo-weak-tests-v2.1:{_MIMO_TASK}:line-0123456789"},
    {"key_id": f"mimo-weak-tests-v2.1:{_MIMO_TASK}:line-0123456789"},
    {"id": f"mimo-weak-tests-v2.1:{_MIMO_TASK}:line-0123456789"},
    {"finding_id": "mimo-weak-tests-v2.1:format-code-task-999002:block"},
    {"finding_id": f"mimo-weak-tests-v2.1:{_MIMO_TASK}:block:extra"},
    {"finding_id": f"other-key:{_MIMO_TASK}:block"},
    {"finding_id": None},
    {"witness_id": "line-0123456789"},
    {"program": {"source_program_id": f"mimo-weak-tests-v2.1:{_MIMO_TASK}:line-0123456789"}},
    {"program": {"sha256": "sha256:" + "b" * 64}},
    {"program": {"sha256": "not-a-sha"}},
    {"program": {"sha256": None}},
    {"program": []},
    {"program_sha256": "sha256:" + "b" * 64},
    {"subject": {"task_id": "format-code-task-999002"}},
    {"subject": {"task_digest": "sha256:" + "2" * 64}},
])
def test_mimo_supplied_identity_mismatches_have_no_description_fallback(override: dict[str, Any]) -> None:
    item = _mimo_finding("block")
    item["mechanism"] = "A specific description cannot rescue an incorrectly bound retained row."
    item.update(override)
    assert resolve_mechanism_description(item, retained_atlas_row=_mimo_atlas_row()) is None


@pytest.mark.parametrize("override", [
    {"task_id": "format-code-task-999002"},
    {"package_digest": "sha256:" + "2" * 64},
    {"package_digest": None},
    {"witnesses": []},
    {"witnesses": [{"id": "unrelated", "sha256": "a" * 64, "reason": "Other witness reason."}]},
    {"witnesses": [{"id": "block", "sha256": "b" * 64, "reason": "Same label, wrong program."}]},
    {"witnesses": [{"id": "block", "sha256": None, "reason": "Missing program digest."}]},
    {"witnesses": [
        {"id": "block", "sha256": "a" * 64, "reason": "First reason."},
        {"id": "block", "sha256": "a" * 64, "reason": "Conflicting reason."},
    ]},
    {"witnesses": {"block": {"reason": "Not an atlas witness list."}}},
])
def test_mimo_retained_row_mismatches_fail_closed(override: dict[str, Any]) -> None:
    item, row = _mimo_finding("block"), _mimo_atlas_row()
    item["summary"] = "A specific summary cannot rescue a mismatched retained row."
    row.update(override)
    assert resolve_mechanism_description(item, retained_atlas_row=row) is None


@pytest.mark.parametrize("item", [
    {"task_id": _MIMO_TASK, "package_digest": _MIMO_PACKAGE, "witness_id": "block"},
    {"program": {
        "source_program_id": f"mimo-weak-tests-v2.1:{_MIMO_TASK}:block", "sha256": "a" * 64,
    }, "package_digest": _MIMO_PACKAGE},
    {"key_id": f"mimo-weak-tests-v2.1:{_MIMO_TASK}:missing", "package_digest": _MIMO_PACKAGE},
    {"key_id": f"mimo-weak-tests-v2.1:{_MIMO_TASK}:block"},
])
def test_mimo_requires_finding_identity_package_and_matching_witness(item: dict[str, Any]) -> None:
    assert resolve_mechanism_description(item, retained_atlas_row=_mimo_atlas_row()) is None


@pytest.mark.parametrize("root_key", ["root_cause", "root_causes"])
def test_mechanism_preserves_tb4_root_cause_summaries(root_key: str) -> None:
    first = "The required output filename is not checked, so a misplaced report is accepted."
    second = "The checker accepts empty sections without checking the requested content."
    item = {
        root_key: [
            {"id": "synthetic-1", "category": "weak", "source_class": "incomplete_check", "summary": first},
            {"id": "synthetic-2", "category": "weak", "source_class": "incomplete_check", "summary": second},
            {"summary": first}, {"category": "weak", "summary": "weak"}, {"summary": "Unavailable"},
        ],
        "summary": "weak", "defect_class": "incomplete_check",
    }
    assert resolve_mechanism_description(item) == first + "\n" + second


@pytest.mark.parametrize("item, expected", [
    (
        {"mechanism": "The parser drops the timezone before comparing timestamps.", "summary": _MIMO_GENERIC_SUMMARY},
        "The parser drops the timezone before comparing timestamps.",
    ),
    (
        {
            "card_id": "QA-synthetic", "mechanism_summary": "The checker counts entries.\nIt never checks their required fields.",
            "signature_pattern": "PATTERN NOT A MECHANISM", "verification_recipe": "RECIPE NOT A MECHANISM",
        },
        "The checker counts entries.\nIt never checks their required fields.",
    ),
    (
        {"finding": {"mechanism": "The comparator rounds away the required decimal precision."}, "summary": "weak_check"},
        "The comparator rounds away the required decimal precision.",
    ),
    (
        {"summary": "The report checker uses yesterday's date instead of the requested date."},
        "The report checker uses yesterday's date instead of the requested date.",
    ),
    (
        {"safe_summary": "The original module uses a timing assertion that rejects the reference intermittently."},
        "The original module uses a timing assertion that rejects the reference intermittently.",
    ),
])
def test_mechanism_preserves_specific_supplied_prose(item: dict[str, Any], expected: str) -> None:
    assert resolve_mechanism_description(item) == expected


@pytest.mark.parametrize("item", [
    {},
    {"defect_class": "incomplete_check", "taxonomy_class": "weak_check", "primary_class": {"id": "TS01"}},
    {"summary": "weak_check"},
    {"summary": "weak check", "primary_class": {"id": "TS01", "key": "weak_check"}},
    {"mechanism": "TS01", "summary": "TS01 weak check", "primary_class": {"id": "TS01", "key": "weak_check"}},
    {"mechanism": "Unavailable", "mechanism_summary": "N/A", "summary": "No mechanism available."},
    {"mechanism": {"raw_patch": "DO NOT STRINGIFY"}, "summary": []},
    {"summary": _MIMO_GENERIC_SUMMARY},
    {"safe_summary": _MIMO_GENERIC_SUMMARY},
    {"summary": "  ARCHIVED instruction-adjudicated wrong-patch witness; taxonomy mapping is inference.  "},
    {"root_cause": [{"category": "weak", "source_class": "incomplete_check"}]},
    {"root_causes": ["incomplete_check"], "expected_root_causes": ["weak_check"]},
    {"raw": {"mechanism": "NEVER USE ARBITRARY RAW"}, "reproducer": "NEVER USE REPRODUCER"},
])
def test_missing_mechanism_does_not_become_a_class_or_placeholder(item: dict[str, Any]) -> None:
    assert resolve_mechanism_description(item) is None


def test_context_prefers_bounded_specific_mechanism_and_preserves_provenance() -> None:
    mechanism = "The checker compares row counts but omits the required identifier comparison. " + "detail " * 150
    example = RetrievedExample(
        task_id="related", dataset_id="synthetic", issue_id="source-finding",
        status="source_supported_historical", defect_class="weak_check",
        issue={
            "mechanism": mechanism, "summary": _MIMO_GENERIC_SUMMARY,
            "source_sha256": "c" * 64, "source_locator": "synthetic-source#/finding",
            "repair_missing_reason": "No bound repair diff retained",
            "raw": "NEVER INCLUDE RAW", "reproducer_script": "NEVER INCLUDE REPRODUCER",
            "finding": {"raw_patch": "NEVER INCLUDE PATCH"},
        },
        provenance={
            "issue_trust_tier": "source_supported_historical", "issue_cas_uri": "cas://synthetic",
            "receipt_status": "source-only", "raw_payload": "NEVER INCLUDE PAYLOAD",
        },
    )
    result = assemble_audit_context(FakeRetriever(examples=[example]), task_id="target", query="report")  # type: ignore[arg-type]
    related = result["related_issues"][0]
    assert related["issue"]["summary"].startswith("The checker compares row counts")
    assert len(related["issue"]["summary"]) == 600
    assert related["issue"]["summary"].endswith("…")
    assert related["status"] == "source_supported_historical"
    assert related["trust_tier"] == "source_supported_historical"
    assert related["differential"] is None
    assert related["repair_diff"] is None
    assert related["repair_missing_reason"] == "No bound repair diff retained"
    assert related["label_source_sha256"] == "c" * 64
    assert related["label_source_locator"] == "synthetic-source#/finding"
    assert related["provenance"]["issue_cas_uri"] == "cas://synthetic"
    assert related["provenance"]["receipt_status"] == "source-only"
    assert "not by itself a verified control differential" in result["notice"]
    assert _MIMO_GENERIC_SUMMARY not in json.dumps(result)
    assert "NEVER INCLUDE" not in json.dumps(result)
    assert len(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) <= MAX_CONTEXT_CHARS


def test_context_leaves_generic_mechanism_unavailable() -> None:
    example = RetrievedExample(
        task_id="related", dataset_id="synthetic", issue_id="source-finding",
        defect_class="weak_check", issue={"summary": _MIMO_GENERIC_SUMMARY, "defect_class": "weak_check"},
    )
    result = assemble_audit_context(FakeRetriever(examples=[example]), task_id="target", query="report")  # type: ignore[arg-type]
    assert result["related_issues"][0]["issue"]["summary"] is None
