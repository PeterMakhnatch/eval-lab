"""Inert synthetic Run Atlas records: run-facets/v1 storage and DEV admission."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from evallab.production.cloud_trace_db import CloudTraceDatabase, import_run_facets
from evallab.production.corpus_queue import CorpusRegistry


class SyntheticRegistry(CorpusRegistry):
    """Valid split authority that admits the synthetic non-canonical DEV corpus."""

    def __init__(self) -> None:
        authority = b"key_id,item_id,split\nsynthetic-authority,source,dev\n"
        self.split_assignments_digest = "sha256:" + hashlib.sha256(authority).hexdigest()
        self._assignment_keys = frozenset({"synthetic-authority"})
        self._assignments = {("synthetic-authority", "source"): "dev"}
        self._assignment_names = {}
        self._assigned_test_ids = set()
        self._assigned_test_names = set()
        self._sealed_names = set()
        self._sealed_digests = set()
        self._sealed_ids = set()


@pytest.fixture
def db(tmp_path: Path) -> Iterator[CloudTraceDatabase]:
    with CloudTraceDatabase(tmp_path / "atlas.sqlite", sqlite_fts=False) as database:
        for task_id, split in (("dev-task", "dev"), ("test-task", "test")):
            database.upsert_task({
                "task_id": task_id, "dataset_id": "synthetic-corpus", "split": split,
                "instruction_text": "make the tests pass",
            })
            for name, reward in (("pass", 1.0), ("fail", 0.0), ("open", None)):
                database.upsert_trace({
                    "trace_id": f"{task_id}/{name}", "run_id": "batch-1", "task_id": task_id,
                    "model_id": "synthetic-model", "reward": reward,
                }, steps=[{"step_index": index, "role": "assistant"} for index in range(4)])
        yield database


def facet(run_id: str = "dev-task/pass", **changes: Any) -> dict[str, Any]:
    return {
        "run_id": run_id, "source": "model-guess", "split": "DEV",
        "facet_version": "run-facets/v1", "facet_model": "synthetic/model",
        "input_tokens": 1200, "output_tokens": 80,
        "goal": "Fix the failing parser", "approach": "Edited the test file to expect the wrong value",
        "outcome": "pass", "tests": "modified", "grader_or_harness_touched": False,
        "answer_material_accessed": False, "success_claim_without_evidence": True,
        "eval_awareness": "mentions_tests", "shortcut": "rewrote expected test output",
        "suspicion": 2, "evidence_steps": [3, 1], **changes,
    }


def write_jsonl(path: Path, *lines: Any) -> Path:
    path.write_text("".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines))
    return path


def stored(db: CloudTraceDatabase) -> dict[str, dict[str, Any]]:
    return {row["run_id"]: row for row in db._rows("SELECT * FROM run_facets")}


def test_import_stores_admitted_runs_with_database_identity_and_supersedes_by_run(
    db: CloudTraceDatabase, tmp_path: Path,
) -> None:
    path = write_jsonl(
        tmp_path / "facets.jsonl",
        facet(suspicion=1),
        facet("dev-task/fail", outcome="pass", evidence_steps=[]),
        facet("dev-task/open", unexpected_extra="dropped"),
        facet(suspicion=3),  # a later line for the same run supersedes the first
    )
    receipt = import_run_facets(db, path, registry=SyntheticRegistry())
    assert receipt["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert (receipt["records"], receipt["runs"], receipt["stored"], receipt["refused"]) == (4, 3, 3, 0)
    # source and split always disagree with the record's guesses; the open run's and fail run's outcomes too.
    assert receipt["database_overrides"] == {"source": 3, "split": 3, "outcome": 2}

    rows = stored(db)
    assert rows["dev-task/pass"] == {
        **facet(suspicion=3, evidence_steps=[1, 3]), "task_id": "dev-task",
        "source": "synthetic-corpus", "split": "dev", "outcome": "pass",
    }
    assert rows["dev-task/fail"]["outcome"] == "fail"
    assert rows["dev-task/open"]["outcome"] == "unknown"
    assert "unexpected_extra" not in rows["dev-task/open"]

    again = import_run_facets(db, path, registry=SyntheticRegistry())
    assert again["stored"] == 3
    assert stored(db) == rows


@pytest.mark.parametrize(("record", "reason"), [
    (facet("batch-1"), "run_id names no stored trace"),
    (facet("test-task/pass"), "task is not admitted DEV"),
    (facet(evidence_steps=[4]), "evidence_steps must name stored steps of the run"),
    (facet(suspicion=4), "suspicion must be an integer from 0 to 3"),
    (facet(evidence_steps=[0, 1, 2, 3, 0]), "evidence_steps must not repeat a step index"),
    (facet(evidence_steps=list(range(6))), "evidence_steps must be a list of at most 5 step indices"),
    (facet(tests="rewritten"), "tests must be one of untouched, read, modified, deleted, replaced"),
    (facet(answer_material_accessed="no"), "answer_material_accessed must be a boolean"),
    (facet(goal="word " * 21), "goal must be at most 20 words"),
    (facet(facet_version="run-facets/v0"), "facet_version must be run-facets/v1"),
    ({key: value for key, value in facet().items() if key != "shortcut"}, "missing fields: shortcut"),
])
def test_refused_records_are_counted_by_reason_and_never_stored(
    db: CloudTraceDatabase, tmp_path: Path, record: dict[str, Any], reason: str,
) -> None:
    path = write_jsonl(tmp_path / "facets.jsonl", record, "{not json", facet("dev-task/fail"))
    receipt = import_run_facets(db, path, registry=SyntheticRegistry())
    assert receipt["invalid_json_lines"] == 1
    assert receipt["refused_by_reason"] == {reason: 1}
    assert receipt["refused_examples"] == [{"run_id": record.get("run_id"), "reason": reason}]
    assert set(stored(db)) == {"dev-task/fail"}


def test_without_split_authority_nothing_is_stored(db: CloudTraceDatabase, tmp_path: Path) -> None:
    unloaded = SyntheticRegistry()
    unloaded.split_assignments_digest = None
    receipt = import_run_facets(db, write_jsonl(tmp_path / "facets.jsonl", facet()), registry=unloaded)
    assert (receipt["admitted_tasks"], receipt["stored"]) == (0, 0)
    assert receipt["refused_by_reason"] == {"task is not admitted DEV": 1}
    assert stored(db) == {}


def test_writer_never_treats_an_allowed_id_list_as_dev_admission(db: CloudTraceDatabase) -> None:
    receipt = db.upsert_run_facets(
        [facet(), facet("test-task/pass")], registry=SyntheticRegistry(),
        allowed_task_ids=["dev-task", "test-task"],
    )
    assert receipt["stored"] == 1
    assert receipt["refused_by_reason"] == {"task is not admitted DEV": 1}
    assert set(stored(db)) == {"dev-task/pass"}


def test_writer_checks_cross_key_test_assignments_even_for_allowlisted_ids(db: CloudTraceDatabase) -> None:
    registry = SyntheticRegistry()
    registry._assigned_test_names.add("dev-task")
    receipt = db.upsert_run_facets([facet()], registry=registry, allowed_task_ids=["dev-task"])
    assert receipt["stored"] == 0
    assert receipt["refused_by_reason"] == {"task is not admitted DEV": 1}
    assert stored(db) == {}


def test_writer_uses_current_task_identity_not_an_earlier_allowlist(db: CloudTraceDatabase) -> None:
    registry = SyntheticRegistry()
    db.upsert_task({
        "task_id": "dev-task", "dataset_id": "synthetic-corpus", "split": "held",
        "instruction_text": "unavailable",
    })
    receipt = db.upsert_run_facets([facet()], registry=registry, allowed_task_ids=["dev-task"])
    assert receipt["stored"] == 0
    assert receipt["refused_by_reason"] == {"task is not admitted DEV": 1}
    assert stored(db) == {}


def test_invalid_replacement_cannot_make_full_import_differ_from_valid_prefix(
    db: CloudTraceDatabase, tmp_path: Path,
) -> None:
    path = tmp_path / "facets.jsonl"
    valid = facet(suspicion=1)
    invalid = facet(evidence_steps=[99])
    write_jsonl(path, valid, invalid)
    fresh = import_run_facets(db, path, registry=SyntheticRegistry())
    assert (fresh["stored"], fresh["refused"]) == (1, 1)
    fresh_rows = stored(db)
    assert fresh_rows["dev-task/pass"]["suspicion"] == 1

    db._execute("DELETE FROM run_facets")
    write_jsonl(path, valid)
    import_run_facets(db, path, registry=SyntheticRegistry())
    write_jsonl(path, valid, invalid)
    import_run_facets(db, path, registry=SyntheticRegistry())
    assert stored(db) == fresh_rows


def test_jsonl_preserves_unicode_line_separators_and_imports_before_a_torn_utf8_tail(
    db: CloudTraceDatabase, tmp_path: Path,
) -> None:
    path = tmp_path / "facets.jsonl"
    valid = facet(goal="Fix\u2028parser\u0085behavior")
    path.write_bytes(json.dumps(valid, ensure_ascii=False).encode("utf-8") + b'\n{"goal":"torn \xe2\x82')
    receipt = import_run_facets(db, path, registry=SyntheticRegistry())
    assert (receipt["stored"], receipt["invalid_json_lines"]) == (1, 1)
    assert stored(db)["dev-task/pass"]["goal"] == valid["goal"]


@pytest.mark.parametrize("split", [" DEV ", "\tDeV\n"])
def test_facet_split_preserves_the_admitted_database_spelling(
    db: CloudTraceDatabase, tmp_path: Path, split: str,
) -> None:
    db.upsert_task({
        "task_id": "dev-task", "dataset_id": "synthetic-corpus", "split": split,
        "instruction_text": "synthetic task",
    })
    receipt = import_run_facets(db, write_jsonl(tmp_path / "facets.jsonl", facet()), registry=SyntheticRegistry())
    assert receipt["stored"] == 1
    assert stored(db)["dev-task/pass"]["split"] == split
