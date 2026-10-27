"""Cross-key-safe fixture construction and bounded evidence, entirely synthetic."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from evallab.production import tq_fixture
from evallab.production.cloud_trace_db import CloudTraceDatabase, CloudTraceRetriever
from evallab.production.corpus_queue import CorpusRegistry
from evallab.production.tq_context import MAX_CONTEXT_CHARS, assemble_audit_context
from evallab.production.tq_fixture import FixtureIssue, build_task_quality_fixture

_ALLOWED = ("alpha", "beta", "gamma", "delta")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def inputs(tmp_path: Path) -> tuple[Path, CorpusRegistry]:
    keys = tmp_path / "registry/research/answer-keys"
    ledger = keys / "splits/assignments.csv"
    ledger.parent.mkdir(parents=True)
    assignments = [(task, "dev") for task in (*_ALLOWED, "cross-held")] + [("native-test", "test")]
    ledger.write_text(
        "key_id,item_id,task,split\n"
        + "".join(f"tb4-audit-v1,{task},{task},{split}\n" for task, split in assignments)
        + "another-key,other-version,cross-held,test\n",
        encoding="utf-8",
    )
    for key in ("tb4-audit-v1", "cheatbench-actions-v1", "hacktrace-act-v1"):
        directory = keys / key
        directory.mkdir()
        rows = [
            {"item_id": task, "split": split, "subject": {"task_id": task}}
            for task, split in assignments
        ] if key == "tb4-audit-v1" else []
        (directory / "items.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8",
        )
    (keys / "tb4-audit-v1/tasks.jsonl").write_text("", encoding="utf-8")
    inventory = tmp_path / "registry/research/experiments/mimo-defect-map-v1/inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(json.dumps({
        "dataset_id": "mimo-defect-map-v1",
        "heldout_task_ids_excluded_from_training_all_versions": [], "tasks": [],
    }), encoding="utf-8")
    source = tmp_path / "source.sqlite"
    with CloudTraceDatabase(source) as database:
        for task, split in [*assignments, ("outsider", "dev")]:
            database.upsert_task({
                "task_id": task, "dataset_id": "tb4-audit-v1",
                "split": split, "instruction_text": "Inspect test output quality.",
                "metadata_json": {"item_id": task, "unrelated": "DO_NOT_PUBLISH"},
            })
            database.upsert_task_issue({
                "issue_id": task + "-old", "task_id": task,
                "defect_class": "weak_check", "direction": "wrong_pass", "status": "confirmed",
                "reproducer_script": "DO_NOT_PUBLISH", "citation_file": "tests/old.py",
                "trust_tier": "tier1_container_verified",
            })
            database.upsert_trace({
                "trace_id": task + "-fail", "run_id": task + "-run", "task_id": task,
                "model_id": "synthetic/model", "outcome": "failed", "reward": 0.0,
                "is_task_defect_fail": True,
                "verified_receipt_json": {"unrelated": "DO_NOT_PUBLISH"},
            }, steps=[
                {"step_index": 0, "role": "assistant", "command_or_action": "DO_NOT_PUBLISH"},
                {"step_index": 1, "role": "tool", "observation_preview": "AssertionError: test mismatch"},
            ])
    return source, CorpusRegistry(tmp_path / "registry")


def _seed(task: str, *, missing: bool = False, provisional: bool = False) -> FixtureIssue:
    text = "--- original/tests/test.py\n+++ repaired/tests/test.py\n" + "-old check\n+stronger check\n" * 180
    return FixtureIssue(
        issue={
            "issue_id": task + "-curated", "task_id": task, "defect_class": "weak_check",
            "direction": "wrong_pass", "status": "provisional" if provisional else "confirmed",
            "trust_tier": "tier3_provisional" if provisional else "tier2_consensus_labeled",
            "citation_file": "tests/test.py", "citation_line": 1,
        },
        evidence={
            "summary": "The test omits an output requirement.",
            "source_sha256": _sha(task.encode()), "source_locator": "labels.json:1",
            "repair_diff": None if missing else {
                "text": text, "sha256": _sha(text.encode()),
                "original_files_sha256": "1" * 64, "repaired_files_sha256": "2" * 64,
                "kind": "retained_native_repair",
            },
            "repair_missing_reason": "no retained grader repair" if missing else None,
            "unrelated_instruction": "DO_NOT_PUBLISH",
        },
    )


def test_fixture_is_source_immutable_diagnostic_only_and_provenance_bound(
    inputs: tuple[Path, CorpusRegistry], tmp_path: Path,
) -> None:
    source, registry = inputs
    before = source.read_bytes()
    destination = tmp_path / "fixture.sqlite"
    manifest = build_task_quality_fixture(
        source, destination, registry=registry, task_ids=_ALLOWED,
        expected_source_sha256=_sha(before), load_issues=lambda task: [_seed(task, missing=task == "gamma")],
    )
    assert source.read_bytes() == before
    assert manifest["database_sha256"] == _sha(destination.read_bytes())
    assert b"DO_NOT_PUBLISH" not in destination.read_bytes()
    assert manifest["admitted_task_ids"] == sorted(_ALLOWED)
    assert manifest["failed_run_summaries"] == len(_ALLOWED)
    with CloudTraceDatabase(destination) as database:
        assert {row["task_id"] for row in database._rows("SELECT * FROM corpus_tasks")} == set(_ALLOWED)
        assert all(row["reproducer_script"] is None for row in database._rows("SELECT * FROM task_issues"))
        traces = database._rows("SELECT * FROM agent_traces")
        steps = database._rows("SELECT * FROM trace_steps")
        assert all(row["step_count"] == 1 for row in traces)
        assert all(row["command_or_action"] is None and row["content_text"] is None for row in steps)
        assert "DO_NOT_PUBLISH" not in json.dumps(traces + steps)
        for receipt in manifest["rows"]:
            predicates = " AND ".join(f"{key} = ?" for key in receipt["identity"])
            row = database._rows(
                f"SELECT * FROM {receipt['table']} WHERE {predicates}", tuple(receipt["identity"].values()),
            )[0]
            assert receipt["row_sha256"] == _sha(json.dumps(
                row, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
            ).encode())
        context = assemble_audit_context(
            CloudTraceRetriever(database, registry=registry), task_id="alpha", query="test",
            source_digest=manifest["database_sha256"],
        )
    assert context["failed_run"]["trace_id"] == "alpha-fail"
    examples = context["related_issues"]
    assert {row["task_id"] for row in examples} == set(_ALLOWED) - {"alpha"}
    assert len(examples) == 3
    assert all(row["split"] == "dev" for row in examples)
    assert "DO_NOT_PUBLISH" not in json.dumps(context)
    assert len(json.dumps(context, sort_keys=True, ensure_ascii=False)) <= MAX_CONTEXT_CHARS
    for example in examples:
        repair = example["repair_diff"]
        if example["task_id"] == "gamma":
            assert repair is None
            assert example["repair_missing_reason"] == "no retained grader repair"
        else:
            assert "\n" in repair["text"]
            assert len(repair["text"]) == 2000
            assert repair["truncated"] is True
            assert repair["shown_sha256"] == _sha(repair["text"].encode())
            assert repair["sha256"] == _seed(example["task_id"]).evidence["repair_diff"]["sha256"]


@pytest.mark.parametrize("blocked", ["cross-held", "native-test", "outsider", "absent"])
def test_cross_key_test_or_unadmitted_identity_is_rejected_before_loading_content(
    inputs: tuple[Path, CorpusRegistry], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked: str,
) -> None:
    source, registry = inputs
    before = source.read_bytes()
    called: list[str] = []

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("actor content projection must not start")

    monkeypatch.setattr(tq_fixture, "project_actor_database", forbidden)
    destination = tmp_path / "blocked.sqlite"
    with pytest.raises(ValueError, match="unadmitted, sealed or TEST"):
        build_task_quality_fixture(
            source, destination, registry=registry, task_ids=["alpha", blocked],
            expected_source_sha256=_sha(before), load_issues=lambda task: called.append(task) or [],
        )
    assert called == []
    assert not destination.exists()
    assert source.read_bytes() == before


def test_fixture_does_not_promote_source_labels_or_fill_missing_repairs(
    inputs: tuple[Path, CorpusRegistry], tmp_path: Path,
) -> None:
    source, registry = inputs
    destination = tmp_path / "provisional.sqlite"
    build_task_quality_fixture(
        source, destination, registry=registry, task_ids=["alpha"],
        expected_source_sha256=_sha(source.read_bytes()),
        load_issues=lambda task: [_seed(task, missing=True, provisional=True)],
    )
    with CloudTraceDatabase(destination) as database:
        row = database._rows("SELECT * FROM task_issues")[0]
        assert (row["status"], row["trust_tier"]) == ("provisional", "tier3_provisional")
        assert (row["original_reward"], row["repaired_reward"]) == (None, None)
        assert CloudTraceRetriever(database, registry=registry).retrieve_similar_examples(
            "test", kind="task_quality", include_provisional=True,
        ) == []
        evidence = database._rows("SELECT * FROM corpus_tasks")[0]["metadata_json"]["task_quality_evidence"]
        assert evidence["alpha-curated"]["repair_diff"] is None


def test_tampered_diff_is_not_published(
    inputs: tuple[Path, CorpusRegistry], tmp_path: Path,
) -> None:
    source, registry = inputs
    seed = _seed("alpha")
    seed.evidence["repair_diff"]["text"] += "unattested change"
    destination = tmp_path / "tampered.sqlite"
    with pytest.raises(ValueError, match="invalid source/diff attestation"):
        build_task_quality_fixture(
            source, destination, registry=registry, task_ids=["alpha"],
            expected_source_sha256=_sha(source.read_bytes()), load_issues=lambda task: [seed],
        )
    assert not destination.exists()
    assert not list(tmp_path.glob(".tampered.sqlite.tq-*"))


def test_fixture_publication_refuses_concurrent_output(
    inputs: tuple[Path, CorpusRegistry], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, registry = inputs
    destination = tmp_path / "racing.sqlite"
    link = os.link

    def racing_link(staged: Path, target: Path, **kwargs: Any) -> None:
        if target == destination:
            target.write_bytes(b"other writer owns this file")
        link(staged, target, **kwargs)

    monkeypatch.setattr(tq_fixture.os, "link", racing_link)
    with pytest.raises(FileExistsError):
        build_task_quality_fixture(
            source, destination, registry=registry, task_ids=["alpha"],
            expected_source_sha256=_sha(source.read_bytes()), load_issues=lambda task: [_seed(task)],
        )
    assert destination.read_bytes() == b"other writer owns this file"
    assert not list(tmp_path.glob(".racing.sqlite.tq-*"))


def test_no_assignment_authority_fails_before_opening_source(
    inputs: tuple[Path, CorpusRegistry], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, registry = inputs
    monkeypatch.setattr(registry, "has_assignment_authority", lambda: False)
    with pytest.raises(ValueError, match="canonical split assignment authority"):
        build_task_quality_fixture(
            tmp_path / "not-opened.sqlite", tmp_path / "no-output.sqlite",
            registry=registry, task_ids=["alpha"], expected_source_sha256="0" * 64,
            load_issues=lambda task: pytest.fail("issue content must not be loaded"),
        )


def test_retriever_drops_a_diff_changed_after_fixture_publication(
    inputs: tuple[Path, CorpusRegistry], tmp_path: Path,
) -> None:
    source, registry = inputs
    destination = tmp_path / "changed.sqlite"
    build_task_quality_fixture(
        source, destination, registry=registry, task_ids=["alpha"],
        expected_source_sha256=_sha(source.read_bytes()), load_issues=lambda task: [_seed(task)],
    )
    with CloudTraceDatabase(destination) as database:
        task = database._rows("SELECT * FROM corpus_tasks")[0]
        supplement = task["metadata_json"]["task_quality_evidence"]["alpha-curated"]
        supplement["repair_diff"]["text"] += "changed after publication"
        database.upsert_task(task)
        examples = CloudTraceRetriever(database, registry=registry).retrieve_similar_examples(
            "test", kind="task_quality",
        )
    assert len(examples) == 1
    assert examples[0].issue is not None
    assert "repair_diff" not in examples[0].issue
    assert "summary" not in examples[0].issue


@pytest.mark.parametrize("field", ["summary", "diff"])
def test_escaped_sealed_identity_is_not_returned_in_supplemental_strings(
    inputs: tuple[Path, CorpusRegistry], tmp_path: Path, field: str,
) -> None:
    source, registry = inputs
    seal = 'synthetic-"quoted"'
    seed = _seed("beta")
    evidence = dict(seed.evidence)
    if field == "summary":
        evidence["summary"] += " " + seal
    else:
        repair = dict(evidence["repair_diff"])
        repair["text"] = repair["text"][:1000] + seal + repair["text"][1000:]
        repair["sha256"] = _sha(repair["text"].encode())
        evidence["repair_diff"] = repair
    destination = tmp_path / "sealed-supplement.sqlite"
    build_task_quality_fixture(
        source, destination, registry=registry, task_ids=["beta"],
        expected_source_sha256=_sha(source.read_bytes()),
        load_issues=lambda task: [FixtureIssue(seed.issue, evidence)],
    )
    with CloudTraceDatabase(destination) as database:
        context = assemble_audit_context(
            CloudTraceRetriever(database, registry=registry, sealed_task_ids=[seal]),
            task_id="alpha", query="test",
        )
    assert len(context["related_issues"]) == 1
    assert context["related_issues"][0]["issue"]["summary"] is None
    assert context["related_issues"][0]["repair_diff"] is None


def test_related_issue_keeps_its_own_tier_when_a_stronger_trace_is_present(
    inputs: tuple[Path, CorpusRegistry], tmp_path: Path,
) -> None:
    source, registry = inputs
    destination = tmp_path / "mixed-trust.sqlite"
    build_task_quality_fixture(
        source, destination, registry=registry, task_ids=["beta"],
        expected_source_sha256=_sha(source.read_bytes()), load_issues=lambda task: [_seed(task)],
    )
    with CloudTraceDatabase(destination) as database:
        with database._transaction(write=True):
            database._execute("UPDATE agent_traces SET trust_tier = 'tier1_container_verified'")
        retriever = CloudTraceRetriever(database, registry=registry)
        examples = retriever.retrieve_similar_examples("test", kind="task_quality")
        assert examples[0].trust_tier == "tier1_container_verified"
        context = assemble_audit_context(retriever, task_id="alpha", query="test")
    assert context["related_issues"][0]["trust_tier"] == "tier2_consensus_labeled"
