"""Synthetic evidence only: no corpus task/answer bytes, shared DB, or cloud."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from evallab.production import CloudTraceRetriever, RetrievedExample
from evallab.production.cloud_trace_db import CloudTraceDatabase, format_retrieval_prompt_block
from evallab.production.corpus_queue import CorpusRegistry


class SyntheticRegistry(CorpusRegistry):
    def __init__(self) -> None:
        authority = b"key_id,item_id,split\nsynthetic-authority,source,dev\n"
        self.split_assignments_digest = "sha256:" + hashlib.sha256(authority).hexdigest()
        self._assignment_keys = frozenset({"synthetic-authority"})
        self._assignments = {("synthetic-authority", "source"): "dev"}
        self._assignment_names = {}
        self._assigned_test_ids = set()
        self._assigned_test_names = set()
        self._sealed_names = {"synthetic-sealed", "synthetic-alias"}
        self._sealed_digests = {"synthetic-sealed-digest"}
        self._sealed_ids = {"synthetic-sealed-item"}


@pytest.fixture(params=(True, False), ids=("sqlite-fts", "sqlite-like"))
def db(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[CloudTraceDatabase]:
    with CloudTraceDatabase(tmp_path / "evidence.sqlite", sqlite_fts=request.param) as database:
        yield database


@pytest.fixture
def retriever(db: CloudTraceDatabase) -> CloudTraceRetriever:
    return CloudTraceRetriever(db, registry=SyntheticRegistry())


def add_task(db: CloudTraceDatabase, task_id: str = "synthetic-dev", **extra: Any) -> None:
    db.upsert_task({
        "task_id": task_id, "dataset_id": "synthetic-corpus", "split": "dev",
        "instruction_text": "pytest grader accepts empty output", "cas_uri": "cas://task", **extra,
    })


def add_issue(
    db: CloudTraceDatabase, task_id: str = "synthetic-dev", issue_id: str = "issue-1", **extra: Any,
) -> None:
    db.upsert_task_issue({
        "issue_id": issue_id, "task_id": task_id, "defect_class": "missing_output_check",
        "direction": "wrong_pass", "status": "confirmed", "citation_file": "tests/check.py",
        "citation_line": 17, "reproducer_script": "pytest grader accepts empty output",
        "witness_digest": "synthetic-witness-digest", "cas_uri": "cas://issue",
        "trust_tier": "tier1_container_verified", **extra,
    })


def add_trace(
    db: CloudTraceDatabase, task_id: str = "synthetic-dev", trace_id: str = "trace-1",
    *, steps: list[dict[str, Any]] | None = None, **extra: Any,
) -> None:
    db.upsert_trace({
        "trace_id": trace_id, "run_id": "synthetic-run", "task_id": task_id,
        "model_id": "synthetic-model", "is_exploit": True,
        "raw_cas_uri": "cas://raw-trace", "verified_receipt_json": {"status": "verified"},
        "trust_tier": "tier1_container_verified", **extra,
    }, steps=steps if steps is not None else [
        {"step_index": 0, "role": "user", "content_text": "Solve the synthetic task."},
        {
            "step_index": 7, "role": "assistant", "tool_name": "terminal",
            "command_or_action": "pytest grader accepts empty output",
            "content_text": "Check the recorded grader boundary.",
            "observation_preview": "pytest grader accepts empty output",
            "observation_cas_uri": "cas://observation", "flags_json": ["verifier_tamper"],
        },
    ])


@pytest.mark.parametrize("kind", ("task_quality", "reward_seeker", "misalignment"))
def test_matching_evidence_is_read_only_with_real_provenance(
    db: CloudTraceDatabase, retriever: CloudTraceRetriever, kind: str,
) -> None:
    add_task(db)
    add_issue(db)
    add_trace(db)
    db._connection.execute("PRAGMA query_only = ON")
    examples = retriever.retrieve_similar_examples("pytest grader", kind)
    assert len(examples) == 1
    example = examples[0]
    assert isinstance(example, RetrievedExample)
    assert (example.kind, example.task_id, example.dataset_id, example.split) == (
        kind, "synthetic-dev", "synthetic-corpus", "dev",
    )
    assert example.issue is not None
    assert example.issue["issue_id"] == "issue-1"
    assert example.issue["citation_file"] == "tests/check.py"
    assert example.issue["citation_line"] == 17
    assert example.issue["witness_digest"] == "synthetic-witness-digest"
    assert example.issue["cas_uri"] == "cas://issue"
    step = next(step for step in example.trace_steps if step["step_index"] == 7)
    assert step["trace_id"] == "trace-1"
    assert step["observation_cas_uri"] == "cas://observation"
    for field, expected in {
        "task_id": "synthetic-dev", "dataset_id": "synthetic-corpus", "issue_id": "issue-1",
        "trace_ids": ["trace-1"], "model_id": "synthetic-model", "run_id": "synthetic-run",
        "receipt_status": "verified", "raw_cas_uri": "cas://raw-trace", "task_cas_uri": "cas://task",
    }.items():
        assert example.provenance[field] == expected
    assert 7 in example.provenance["step_indices"]
    assert "trace-1" in example.prompt_text and "issue-1" in example.prompt_text
    assert example == retriever.retrieve_similar_examples("pytest grader", kind)[0]
    with pytest.raises(FrozenInstanceError):
        example.kind = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("kind", ("task_quality", "reward_seeker", "misalignment"))
def test_unconfirmed_issues_neither_appear_nor_drive_query_matches(
    db: CloudTraceDatabase, retriever: CloudTraceRetriever, kind: str,
) -> None:
    add_task(db, instruction_text="neutral instruction")
    add_issue(db, issue_id="tentative", status="provisional", reproducer_script="forbiddenmarker")
    add_issue(db, issue_id="disproved", status="refuted", reproducer_script="forbiddenmarker")
    add_trace(db)
    assert retriever.retrieve_similar_examples("forbiddenmarker", kind) == []
    add_issue(db, issue_id="confirmed", reproducer_script="pytest grader")
    examples = retriever.retrieve_similar_examples("pytest grader", kind)
    assert examples and examples[0].issue is not None
    assert examples[0].issue["issue_id"] == "confirmed"
    assert all(marker not in examples[0].prompt_text for marker in (
        "tentative", "disproved", "forbiddenmarker",
    ))


def test_each_kind_selects_its_recorded_evidence(
    db: CloudTraceDatabase, retriever: CloudTraceRetriever,
) -> None:
    for task_id in ("unfair", "wrong-pass", "exploit-only", "flag-only", "ordinary"):
        add_task(db, task_id)
    add_issue(db, "unfair", "unfair-issue", direction="unfair_fail")
    add_issue(db, "wrong-pass", "wrong-pass-issue")
    add_trace(db, "exploit-only", "exploit", steps=[{
        "step_index": 4, "role": "assistant", "command_or_action": "pytest grader",
    }])
    add_trace(db, "flag-only", "flag", is_exploit=False)
    add_trace(db, "ordinary", "normal", is_exploit=False, steps=[{
        "step_index": 0, "role": "assistant", "content_text": "pytest grader",
    }])

    def ids(kind: str) -> set[str]:
        return {example.task_id for example in retriever.retrieve_similar_examples("pytest grader", kind, 20)}

    assert ids("task_quality") == {"unfair", "wrong-pass"}
    assert ids("reward_seeker") == {"wrong-pass", "exploit-only"}
    assert ids("misalignment") == {"exploit-only", "flag-only"}


@pytest.mark.parametrize("kind", ("task_quality", "reward_seeker", "misalignment"))
def test_split_allowlist_and_all_identity_seals_are_fail_closed(
    db: CloudTraceDatabase, kind: str,
) -> None:
    rows = [
        ("upper", {"split": "DEV"}), ("lower", {"split": "dev"}),
        ("spaced", {"split": " dev "}), ("mixed", {"split": "Dev"}),
        ("test-row", {"split": "test"}), ("heldout-row", {"split": "heldout"}),
        ("unknown-row", {"split": "eval_sealed"}), ("blank-row", {}),
        ("synthetic-sealed", {}), ("namespace/synthetic-alias@v9", {}),
        ("original-digest", {"original_grader_digest": "synthetic-sealed-digest"}),
        ("repaired-digest", {"repaired_grader_digest": "synthetic-sealed-digest"}),
        ("item-seal", {"metadata_json": {"item_id": "synthetic-sealed-item"}}),
        ("explicit-seal", {}), ("namespace/explicit-alias@v7", {}),
    ]
    for task_id, extra in rows:
        add_task(db, task_id, **extra)
        add_issue(db, task_id, task_id + "/issue")
        add_trace(db, task_id, task_id + "/trace")
    # Legacy storage can contain the empty split even though upsert rejects it.
    db._connection.execute("UPDATE corpus_tasks SET split = '' WHERE task_id = ?", ("blank-row",))
    retriever = CloudTraceRetriever(
        db, registry=SyntheticRegistry(), sealed_task_ids=("explicit-seal", "explicit-alias"),
    )
    examples = retriever.retrieve_similar_examples("pytest grader", kind, 100)
    assert {example.task_id for example in examples} == {"upper", "lower", "spaced", "mixed"}
    all_output = retriever.format_for_prompt(examples) + json.dumps([example.provenance for example in examples])
    for task_id, _ in rows[4:]:
        assert task_id not in all_output


@pytest.mark.parametrize("query", (
    "inspect synthetic-sealed", "inspect 'namespace/synthetic-alias@v2'",
    "inspect synthetic-sealed-item", "inspect synthetic-sealed-digest",
    "inspect explicit-seal", "inspect namespace/explicit-alias@version",
    "inspect prefixsynthetic-sealedsuffix", "inspect SYNTHETIC-SEALED",
))
def test_sealed_query_raises_without_echo(db: CloudTraceDatabase, query: str) -> None:
    retriever = CloudTraceRetriever(
        db, registry=SyntheticRegistry(), sealed_task_ids=("explicit-seal", "explicit-alias"),
    )
    with pytest.raises(ValueError, match="sealed") as error:
        retriever.retrieve_similar_examples(query, "task_quality")
    assert query not in str(error.value)


@pytest.mark.parametrize("kind", ("task_quality", "reward_seeker", "misalignment"))
def test_cross_task_sealed_reference_drops_the_step_and_its_provenance(
    db: CloudTraceDatabase, retriever: CloudTraceRetriever, kind: str,
) -> None:
    add_task(db)
    add_issue(db)
    add_trace(db, steps=[
        {"step_index": 0, "role": "assistant", "content_text": "pytest grader synthetic-sealed", "flags_json": ["flag"]},
        {"step_index": 2, "role": "assistant", "command_or_action": "pytest grader", "flags_json": ["flag"]},
    ])
    examples = retriever.retrieve_similar_examples("pytest grader", kind)
    assert len(examples) == 1
    assert [step["step_index"] for step in examples[0].trace_steps] == [2]
    assert examples[0].provenance["step_indices"] == [2]
    assert "synthetic-sealed" not in examples[0].prompt_text
    assert "synthetic-sealed" not in json.dumps(examples[0].provenance)


def test_defect_fail_trace_and_exploit_steps_are_preferred_and_bounded(db: CloudTraceDatabase) -> None:
    add_task(db)
    add_issue(db)
    add_trace(db, trace_id="a-normal", is_task_defect_fail=False)
    add_trace(db, trace_id="z-defect", is_task_defect_fail=True)
    retriever = CloudTraceRetriever(db, registry=SyntheticRegistry(), max_steps_per_trace=1)
    tq = retriever.retrieve_similar_examples("pytest grader", "task_quality")[0]
    assert tq.provenance["trace_ids"] == ["z-defect"]
    for kind in ("task_quality", "reward_seeker", "misalignment"):
        example = retriever.retrieve_similar_examples("pytest grader", kind)[0]
        assert len(example.trace_steps) == 1 and example.trace_steps[0]["step_index"] == 7


def test_snippets_are_bounded_without_mutating_storage(db: CloudTraceDatabase) -> None:
    long_text = "boundedword " * 100
    add_task(db)
    add_issue(db, reproducer_script=long_text, citation_file=long_text, defect_class=long_text)
    add_trace(db, steps=[{
        "step_index": 9, "role": long_text, "tool_name": long_text,
        "command_or_action": long_text, "content_text": long_text,
        "observation_preview": long_text, "flags_json": [long_text],
    }])
    retriever = CloudTraceRetriever(db, registry=SyntheticRegistry(), snippet_chars=19)
    example = retriever.retrieve_similar_examples("boundedword", "misalignment")[0]
    assert example.issue is not None
    for field in ("reproducer_script", "citation_file", "defect_class"):
        assert len(example.issue[field]) <= 19
    for field in ("role", "tool_name", "command_or_action", "content_text", "observation_preview", "flags"):
        assert len(example.trace_steps[0][field]) <= 19
    assert long_text not in example.prompt_text
    assert db.query_task_issues()[0]["reproducer_script"] == long_text
    assert db.get_trace("trace-1")["steps"][0]["content_text"] == long_text


def test_top_k_one_per_task_and_prompt_join_are_deterministic(
    db: CloudTraceDatabase, retriever: CloudTraceRetriever,
) -> None:
    for task_id in ("z-task", "a-task", "m-task"):
        add_task(db, task_id)
        for suffix in ("2", "1"):
            add_issue(db, task_id, task_id + suffix)
            add_trace(db, task_id, task_id + suffix)
    examples = retriever.retrieve_similar_examples("pytest grader", "task_quality", 2)
    assert [example.task_id for example in examples] == ["a-task", "m-task"]
    assert len({example.task_id for example in examples}) == 2
    assert examples == retriever.retrieve_similar_examples("pytest grader", "task_quality", 2)
    assert retriever.format_for_prompt(examples) == format_retrieval_prompt_block(examples)
    assert retriever.format_for_prompt([]) == ""
    assert retriever.retrieve_similar_examples("no_matching_lexeme", "task_quality") == []
    assert retriever.retrieve_similar_examples("% '; --", "task_quality") == []


def make_registry(tmp_path: Path) -> CorpusRegistry:
    keys = tmp_path / "research/answer-keys"
    for dataset in ("tb4-audit-v1", "cheatbench-actions-v1", "hacktrace-act-v1"):
        path = keys / dataset / "items.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("")
    (keys / "tb4-audit-v1/tasks.jsonl").write_text("")
    inventory = tmp_path / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(json.dumps({
        "dataset_id": "mimo-defect-map-v1", "tasks": [],
        "heldout_task_ids_excluded_from_training_all_versions": [],
    }))
    assignments = keys / "splits/assignments.csv"
    assignments.parent.mkdir()
    assignments.write_text(
        "key_id,item_id,task,split\n"
        "tb2-to-tb21-v1,secondary-test,synthetic-secondary-sealed,test\n"
        "mixed-key,dev-item,synthetic-assigned-dev,dev\n"
        "mixed-key,test-item,synthetic-assigned-test,test\n"
        "mixed-key,conflicting-item,synthetic-assigned-dev,test\n"
        "mixed-key,synthetic-name-only-dev,,dev\n"
    )
    return CorpusRegistry(tmp_path)


@pytest.mark.parametrize("kind", ("task_quality", "reward_seeker", "misalignment"))
def test_authoritative_secondary_key_assignments_override_db_dev_labels(
    db: CloudTraceDatabase, tmp_path: Path, kind: str,
) -> None:
    registry = make_registry(tmp_path / "registry")
    assert registry.sealed_identity(task_id="synthetic-secondary-sealed") is None
    assert registry.has_split_assignments("tb2-to-tb21-v1")
    assert registry.assigned_split("tb2-to-tb21-v1", item_id="secondary-test") == "test"
    assert registry.assigned_split("mixed-key", task_id="namespace/synthetic-assigned-dev@v2") == "test"
    assert registry.assigned_split("mixed-key", item_id="dev-item") == "dev"
    assert registry.assigned_split("mixed-key", item_id="missing") is None
    assert registry.assigned_split("mixed-key", task_id="namespace/synthetic-name-only-dev@v2") == "dev"
    assert not registry.has_split_assignments("external-fixture")
    rows = [
        ("synthetic-secondary-sealed", "tb2-to-tb21-v1", "secondary-test"),
        ("secondary-unlisted", "tb2-to-tb21-v1", "missing"),
        ("item-assigned-dev", "mixed-key", "dev-item"),
        ("item-assigned-test", "mixed-key", "test-item"),
        ("namespace/synthetic-assigned-dev@v2", "mixed-key", "dev-item"),
        ("mixed-unlisted", "mixed-key", "missing"),
        ("namespace/synthetic-name-only-dev@v2", "mixed-key", None),
        ("external-allowed", "external-fixture", None),
    ]
    for task_id, dataset, item_id in rows:
        add_task(db, task_id, dataset_id=dataset, split="DEV", metadata_json={"item_id": item_id})
        add_issue(db, task_id, task_id + "/issue")
        add_trace(db, task_id, task_id + "/trace")
    retriever = CloudTraceRetriever(db, registry=registry)
    examples = retriever.retrieve_similar_examples("pytest grader", kind, 100)
    assert {example.task_id for example in examples} == {
        "item-assigned-dev", "namespace/synthetic-name-only-dev@v2", "external-allowed",
    }
    assert all(example.dataset_id != "tb2-to-tb21-v1" for example in examples)
    with pytest.raises(ValueError, match="sealed"):
        retriever.retrieve_similar_examples("inspect synthetic-secondary-sealed", kind)


@pytest.mark.parametrize("kind", ("other", "", "Task_Quality"))
def test_invalid_kind_is_rejected(retriever: CloudTraceRetriever, kind: str) -> None:
    with pytest.raises(ValueError, match="kind"):
        retriever.retrieve_similar_examples("query", kind)


@pytest.mark.parametrize("top_k", (0, -1, True, 1.5))
def test_invalid_top_k_is_rejected(retriever: CloudTraceRetriever, top_k: Any) -> None:
    with pytest.raises(ValueError, match="top_k"):
        retriever.retrieve_similar_examples("query", "task_quality", top_k)


@pytest.mark.parametrize("field", ("snippet_chars", "max_steps_per_trace"))
@pytest.mark.parametrize("value", (0, -1, True, 1.5))
def test_invalid_bounds_are_rejected(db: CloudTraceDatabase, field: str, value: Any) -> None:
    with pytest.raises(ValueError, match=field):
        CloudTraceRetriever(db, registry=SyntheticRegistry(), **{field: value})


@pytest.mark.parametrize("kind", ("task_quality", "reward_seeker", "misalignment", None))
def test_matching_provisional_and_confirmed_issues_never_cite_the_provisional(
    db: CloudTraceDatabase, retriever: CloudTraceRetriever, kind: str | None,
) -> None:
    add_task(db)
    add_issue(db, issue_id="a-provisional", status="provisional")
    add_issue(db, issue_id="b-refuted", status="refuted")
    add_issue(db, issue_id="z-confirmed")
    add_trace(db)
    found = retriever.retrieve_similar_examples("pytest grader", kind)
    assert found and found[0].issue is not None
    assert found[0].issue["issue_id"] == "z-confirmed"
    assert all(marker not in found[0].prompt_text for marker in ("a-provisional", "b-refuted"))


def test_contaminated_issue_and_trace_are_not_used_for_clean_evidence(db: CloudTraceDatabase) -> None:
    add_task(db)
    add_issue(db, issue_id="a-unsafe", reproducer_script="synthetic-sealed pytest grader")
    add_issue(db, issue_id="z-safe")
    add_trace(db, trace_id="a-unsafe-trace", raw_cas_uri="cas://synthetic-sealed")
    add_trace(db, trace_id="z-safe-trace", verified_receipt_json={"status": "reviewing-" * 100})
    retriever = CloudTraceRetriever(db, registry=SyntheticRegistry(), snippet_chars=20)
    found = retriever.retrieve_similar_examples("pytest grader", "task_quality")
    assert found and found[0].issue is not None
    assert found[0].issue["issue_id"] == "z-safe"
    assert found[0].provenance["trace_id"] == "z-safe-trace"
    assert len(found[0].provenance["receipt_status"]) <= 20
    assert "synthetic-sealed" not in json.dumps(found[0].to_dict())


def test_long_identifiers_use_stable_hash_references_and_bounded_prompt(db: CloudTraceDatabase) -> None:
    issue_id, trace_id, witness = "issue-" + "i" * 5000, "trace-" + "t" * 5000, "w" * 5000
    add_task(db)
    add_issue(db, issue_id=issue_id, witness_digest=witness)
    add_trace(db, trace_id=trace_id)
    retriever = CloudTraceRetriever(db, registry=SyntheticRegistry(), snippet_chars=20)
    found = retriever.retrieve_similar_examples("pytest grader", "task_quality")
    assert found and found[0].issue is not None
    for reference in (
        found[0].issue["issue_id"], found[0].issue["witness_digest"],
        found[0].trace_steps[0]["trace_id"], found[0].provenance["trace_id"],
    ):
        assert reference.startswith("sha256:") and len(reference) == 71
    assert len(found[0].prompt_text) <= 2400
    assert all(value not in found[0].prompt_text for value in (issue_id, trace_id, witness))
    assert found == retriever.retrieve_similar_examples("pytest grader", "task_quality")


def test_seal_prefix_factoring_preserves_literal_embedded_identifiers(db: CloudTraceDatabase) -> None:
    add_task(db)
    add_issue(db)
    identities = (
        "synthetic-overlap-alpha", "synthetic-overlap-alpine", "synthetic.[literal]",
        "synthetic-short", "synthetic-short-long",
    )
    retriever = CloudTraceRetriever(db, registry=SyntheticRegistry(), sealed_task_ids=identities)
    assert retriever.retrieve_similar_examples("pytest grader", "task_quality")
    for identity in identities:
        with pytest.raises(ValueError, match="sealed"):
            retriever.retrieve_similar_examples("pytest prefix-" + identity.upper() + "-suffix")
    assert retriever.retrieve_similar_examples("synthetic-overlap-alps", "task_quality") == []
    assert retriever.retrieve_similar_examples("syntheticXliteral", "task_quality") == []


def test_json_quoting_cannot_hide_sealed_evidence_or_task_provenance(db: CloudTraceDatabase) -> None:
    marker = 'synthetic-"quoted"'
    add_task(db)
    add_issue(db, issue_id="a-unsafe", reproducer_script=marker + " pytest grader")
    add_issue(db, issue_id="z-safe")
    add_trace(db, trace_id="a-unsafe", raw_cas_uri="cas://" + marker)
    add_trace(db, trace_id="z-safe", steps=[
        {"step_index": 1, "role": "assistant", "command_or_action": marker + " pytest grader"},
        {"step_index": 7, "role": "assistant", "command_or_action": "pytest grader"},
    ])
    retriever = CloudTraceRetriever(db, registry=SyntheticRegistry(), sealed_task_ids=(marker,))
    found = retriever.retrieve_similar_examples("pytest grader", "task_quality")
    assert found and found[0].issue is not None
    assert found[0].issue_id == "z-safe"
    assert found[0].trace_id == "z-safe"
    assert found[0].provenance["step_indices"] == [7]
    assert all(marker not in (step["command_or_action"] or "") for step in found[0].trace_steps)
    for field in ("dataset_id", "cas_uri"):
        add_task(db, **{field: "cas://" + marker})
        assert retriever.retrieve_similar_examples("pytest grader", "task_quality") == []
