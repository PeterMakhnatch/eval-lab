"""Inert synthetic Run Atlas records: facets, clusters, facet search and DEV admission."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from evallab.production.cloud_trace_db import (
    CloudTraceDatabase,
    CloudTraceRetriever,
    import_run_clusters,
    import_run_facets,
)
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


ALL = ("dev-task", "test-task")


@pytest.fixture
def faceted(db: CloudTraceDatabase) -> CloudTraceDatabase:
    """Stored facets whose TEST task was admitted when its record was written."""
    db.upsert_task({
        "task_id": "test-task", "dataset_id": "synthetic-corpus", "split": "dev",
        "instruction_text": "synthetic task",
    })
    receipt = db.upsert_run_facets([
        facet("dev-task/pass", tests="modified", suspicion=3, answer_material_accessed=True),
        facet("dev-task/fail", tests="deleted", suspicion=1),
        facet("dev-task/open", tests="untouched", suspicion=0, evidence_steps=[]),
        facet("test-task/pass", tests="modified", suspicion=3),
    ], registry=SyntheticRegistry(), allowed_task_ids=ALL)
    assert receipt["stored"] == 4
    db.upsert_task({
        "task_id": "test-task", "dataset_id": "synthetic-corpus", "split": "test",
        "instruction_text": "synthetic task",
    })
    return db


def run_ids(rows: list[dict[str, Any]]) -> list[str]:
    return [row["run_id"] for row in rows]


def test_search_runs_filters_every_facet_and_ranks_by_suspicion(faceted: CloudTraceDatabase) -> None:
    search = partial(faceted.search_runs, registry=SyntheticRegistry())
    assert run_ids(search(allowed_task_ids=["dev-task"])) == ["dev-task/pass", "dev-task/fail", "dev-task/open"]
    assert run_ids(search({"tests": ["modified", "deleted"]}, allowed_task_ids=["dev-task"])) == [
        "dev-task/pass", "dev-task/fail",
    ]
    # Outcome is the database's (reward), not the record's "pass".
    assert run_ids(search({"tests": ("modified", "deleted"), "outcome": "fail"}, allowed_task_ids=ALL)) == [
        "dev-task/fail",
    ]
    assert run_ids(search({"answer_material_accessed": True}, allowed_task_ids=ALL)) == ["dev-task/pass"]
    assert run_ids(search({"suspicion": {2, 3}}, allowed_task_ids=ALL, limit=1)) == ["dev-task/pass"]
    assert run_ids(search({"suspicion": [3]}, allowed_task_ids=ALL)) == ["dev-task/pass"]
    assert search(allowed_task_ids=[]) == []
    (row,) = search({"outcome": "pass", "split": "dev"}, allowed_task_ids=["dev-task"])
    assert row["answer_material_accessed"] is True and row["evidence_steps"] == [1, 3]


@pytest.mark.parametrize("facets", [
    {"test": "modified"}, {"tests": "rewritten"}, {"outcome": []}, {"suspicion": True},
    {"answer_material_accessed": 1}, {"source": ""}, ["tests"], {"suspicion": [1, 2.0]},
])
def test_search_runs_rejects_unknown_fields_and_values(faceted: CloudTraceDatabase, facets: Any) -> None:
    with pytest.raises(ValueError, match="facets"):
        faceted.search_runs(facets, registry=SyntheticRegistry(), allowed_task_ids=ALL)


def test_retriever_search_runs_reads_admitted_dev_runs_only(faceted: CloudTraceDatabase) -> None:
    retriever = CloudTraceRetriever(faceted, registry=SyntheticRegistry())
    assert run_ids(retriever.search_runs({"suspicion": 3})) == ["dev-task/pass"]
    unloaded = SyntheticRegistry()
    unloaded.split_assignments_digest = None
    assert CloudTraceRetriever(faceted, registry=unloaded).search_runs() == []


def cluster(cluster_id: str, level: int, parent: str | None, runs: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "cluster_id": cluster_id, "name": f"{cluster_id} name", "level": level, "parent": parent,
        "run_ids": runs, "centroid_text": "edited tests to pass", "size": len(runs), **extra,
    }


HIERARCHY = [
    cluster("c3", 3, "c2", ["dev-task/pass"]),
    cluster("c2", 2, "c1", ["dev-task/pass", "dev-task/fail"]),
    cluster("c1", 1, None, ["dev-task/pass", "dev-task/fail", "dev-task/open"], label_rates={"cheat": 0.33}),
]


def clusters(db: CloudTraceDatabase) -> dict[str, dict[str, Any]]:
    return {row["cluster_id"]: row for row in db._rows("SELECT * FROM run_clusters")}


def test_cluster_hierarchy_replaces_atomically(faceted: CloudTraceDatabase) -> None:
    assert faceted.replace_run_clusters(HIERARCHY, registry=SyntheticRegistry()) == {"clusters": 3, "runs": 3}
    stored_clusters = clusters(faceted)
    assert stored_clusters["c1"] == {**HIERARCHY[2], "provenance_json": {}}
    assert stored_clusters["c3"] == {**HIERARCHY[0], "label_rates": None, "provenance_json": {}}

    build = {"cluster_version": "run-clusters/v1", "seed": 7, "embedder": "hash-512"}
    faceted.replace_run_clusters(
        [cluster("solo", 1, None, ["dev-task/open"], **build)], registry=SyntheticRegistry(),
    )
    assert {key: row["provenance_json"] for key, row in clusters(faceted).items()} == {"solo": build}


@pytest.mark.parametrize(("hierarchy", "message"), [
    ([cluster("c1", 1, None, ["test-task/pass"])], "lack stored facets of an admitted DEV task"),
    ([cluster("c1", 1, None, ["dev-task/missing"])], "lack stored facets"),
    ([cluster("c1", 1, "c0", ["dev-task/pass"])], "no parent at level 1"),
    ([cluster("c2", 2, None, ["dev-task/pass"])], "no parent at level 1"),
    ([*HIERARCHY[1:], cluster("c3", 3, "c1", ["dev-task/pass"])], "one a level up"),
    ([cluster("c1", 1, None, ["dev-task/pass"]), cluster("c2", 2, "c1", ["dev-task/fail"])], "outside its parent"),
    ([{**cluster("c1", 1, None, ["dev-task/pass"]), "size": 2}], "size must equal"),
    ([cluster("c1", 1, None, ["dev-task/pass", "dev-task/pass"])], "distinct run IDs"),
    ([cluster("c1", 4, None, ["dev-task/pass"])], "level must be"),
    ([cluster("c1", 1, None, ["dev-task/pass"])] * 2, "duplicate cluster_id"),
    ([cluster("c1", 1, None, ["dev-task/pass"], label_rates=[0.5])], "label_rates"),
    ([cluster("c1", 1, None, ["dev-task/pass"], cluster_version="run-clusters/v2")], "cluster_version"),
    ([{**cluster("c1", 1, None, ["dev-task/pass"]), "level": 1.0}], "level must be"),
    ([{**cluster("c1", 1, None, ["dev-task/pass"]), "parent": []}], "parent must be"),
    ([{**cluster("c1", 1, None, ["dev-task/pass"]), "run_ids": [{}]}], "distinct run IDs"),
    ([cluster("c1", 1, None, ["dev-task/pass"], size=1.0)], "size must be"),
])
def test_invalid_hierarchy_keeps_the_stored_one(
    faceted: CloudTraceDatabase, hierarchy: list[dict[str, Any]], message: str,
) -> None:
    faceted.replace_run_clusters(HIERARCHY, registry=SyntheticRegistry())
    with pytest.raises(ValueError, match=message):
        faceted.replace_run_clusters(hierarchy, registry=SyntheticRegistry(), allowed_task_ids=ALL)
    assert set(clusters(faceted)) == {"c1", "c2", "c3"}


def test_reparented_trace_is_not_searchable_or_clusterable_under_a_stale_facet(faceted: CloudTraceDatabase) -> None:
    registry = SyntheticRegistry()
    faceted.replace_run_clusters([cluster("kept", 1, None, ["dev-task/open"])], registry=registry)
    faceted.upsert_trace({
        "trace_id": "dev-task/pass", "run_id": "batch-1", "task_id": "test-task",
        "model_id": "synthetic-model",
    })
    assert faceted.search_runs({"suspicion": 3}, registry=registry) == []
    with pytest.raises(ValueError, match="admitted DEV"):
        faceted.replace_run_clusters(
            [cluster("reparented", 1, None, ["dev-task/pass"])], registry=registry,
            allowed_task_ids=ALL,
        )
    assert set(clusters(faceted)) == {"kept"}


def test_empty_admission_never_accesses_facet_body_columns(faceted: CloudTraceDatabase) -> None:
    def authorize(action: int, table: str, column: str, *unused: Any) -> int:
        if action == sqlite3.SQLITE_READ and table == "run_facets":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    faceted._connection.set_authorizer(authorize)
    try:
        assert faceted.search_runs(registry=SyntheticRegistry(), allowed_task_ids=[]) == []
        unloaded = SyntheticRegistry()
        unloaded.split_assignments_digest = None
        assert CloudTraceRetriever(faceted, registry=unloaded).search_runs() == []
    finally:
        faceted._connection.set_authorizer(None)


def test_retriever_does_not_pass_stale_admission_to_search(
    faceted: CloudTraceDatabase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_search = faceted.search_runs

    def change_split_then_search(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        faceted.upsert_task({
            "task_id": "dev-task", "dataset_id": "synthetic-corpus", "split": "TEST",
            "instruction_text": "no longer admitted",
        })
        return original_search(*args, **kwargs)

    monkeypatch.setattr(faceted, "search_runs", change_split_then_search)
    assert CloudTraceRetriever(faceted, registry=SyntheticRegistry()).search_runs() == []


TIER1, TIER2, TIER3 = "tier1_container_verified", "tier2_consensus_labeled", "tier3_provisional"


@pytest.fixture(params=(True, False), ids=("sqlite-fts", "sqlite-like"))
def retrieval_db(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[CloudTraceDatabase]:
    with CloudTraceDatabase(tmp_path / "facet-retrieval.sqlite", sqlite_fts=request.param) as database:
        yield database


def retrieval_task(db: CloudTraceDatabase, task_id: str) -> None:
    db.upsert_task({
        "task_id": task_id, "dataset_id": "synthetic-corpus", "split": "dev",
        "instruction_text": "repair the parser",
    })


def retrieval_trace(
    db: CloudTraceDatabase, task_id: str, name: str = "run", *, tier: str = TIER1,
    reward: float | None = 1.0, count: int = 1, faceted: bool = True,
    facet_changes: dict[str, Any] | None = None, **changes: Any,
) -> str:
    run_id = f"{task_id}/{name}"
    db.upsert_trace({
        "trace_id": run_id, "run_id": "synthetic-batch", "task_id": task_id,
        "model_id": "synthetic-model", "trust_tier": tier, "reward": reward,
        "is_exploit": True, **changes,
    }, steps=[
        {"step_index": index, "role": "assistant", "content_text": "inspect the parser"}
        for index in range(count)
    ])
    if faceted:
        receipt = db.upsert_run_facets([
            facet(run_id, evidence_steps=[0] if count else [], **(facet_changes or {})),
        ], registry=SyntheticRegistry())
        assert receipt["stored"] == 1
    return run_id


def retrieval_issue(db: CloudTraceDatabase, task_id: str) -> None:
    db.upsert_task_issue({
        "issue_id": f"{task_id}/issue", "task_id": task_id, "defect_class": "missing_output_check",
        "direction": "wrong_pass", "status": "confirmed", "trust_tier": TIER1,
        "reproducer_script": "parser accepts the wrong output",
    })


@pytest.mark.parametrize("query", ("", "parser"))
def test_examples_filter_facets_on_the_returned_trace_not_just_its_task(
    retrieval_db: CloudTraceDatabase, query: str,
) -> None:
    db = retrieval_db
    expected = {}
    for tests in ("modified", "deleted", "replaced", "untouched"):
        retrieval_task(db, tests)
        run_id = retrieval_trace(db, tests, facet_changes={"tests": tests})
        if tests != "untouched":
            expected[tests] = run_id
    retrieval_trace(db, "modified", "louder-but-unmatched", count=8, facet_changes={"tests": "untouched"})
    retrieval_task(db, "failed")
    retrieval_trace(db, "failed", reward=0.0)
    retrieval_task(db, "unfaceted")
    retrieval_trace(db, "unfaceted", faceted=False)
    retrieval_task(db, "issue-only")
    retrieval_issue(db, "issue-only")

    examples = CloudTraceRetriever(db, registry=SyntheticRegistry()).retrieve_similar_examples(
        query, top_k=20, facets={"outcome": "pass", "tests": ["modified", "deleted", "replaced"]},
    )
    assert {example.task_id: example.trace_id for example in examples} == expected
    assert all(example.provenance["run_facets_matched"] is True for example in examples)
    assert all(example.provenance["facet_similarity"] == 0 for example in examples)


@pytest.mark.parametrize("query", ("", "!!!", "parser"))
def test_empty_facet_mapping_requires_faceted_trace_backing_even_for_issue_led_queries(
    retrieval_db: CloudTraceDatabase, query: str,
) -> None:
    db = retrieval_db
    for task_id in ("faceted", "unfaceted", "issue-only"):
        retrieval_task(db, task_id)
        retrieval_issue(db, task_id)
    retrieval_trace(db, "faceted")
    retrieval_trace(db, "unfaceted", faceted=False)
    retriever = CloudTraceRetriever(db, registry=SyntheticRegistry())

    examples = retriever.retrieve_similar_examples(query, "task_quality", 10, facets={})
    assert [(example.task_id, example.trace_id) for example in examples] == [("faceted", "faceted/run")]
    assert examples[0].provenance["facet_similarity"] == 0
    assert examples[0].score == (3.0 if query == "parser" else 0.0)
    if query == "parser":
        assert {example.task_id for example in retriever.retrieve_similar_examples(query, "task_quality", 10)} == {
            "faceted", "unfaceted", "issue-only",
        }
    else:
        assert retriever.retrieve_similar_examples(query, "task_quality", 10) == []


@pytest.mark.parametrize("query", ("", "!!!", "parser"))
def test_examples_filter_clusters_and_intersect_facet_filters(
    retrieval_db: CloudTraceDatabase, query: str,
) -> None:
    db = retrieval_db
    for task_id in ("member", "outside"):
        retrieval_task(db, task_id)
    member = retrieval_trace(db, "member", "included")
    retrieval_trace(db, "member", "excluded", count=8, facet_changes={"tests": "deleted"})
    retrieval_trace(db, "outside")
    db.replace_run_clusters([cluster("selected", 1, None, [member])], registry=SyntheticRegistry())
    retriever = CloudTraceRetriever(db, registry=SyntheticRegistry())

    (example,) = retriever.retrieve_similar_examples(query, cluster_id="selected")
    assert (example.task_id, example.trace_id) == ("member", member)
    assert example.provenance["run_facets_matched"] is True
    if query != "parser":
        assert example.score == 0.0 and example.matched_terms == ()
    assert retriever.retrieve_similar_examples(
        query, cluster_id="selected", facets={"tests": "deleted"},
    ) == []


def test_like_run_ranks_similarity_inside_a_tier_but_never_above_stronger_trust(
    retrieval_db: CloudTraceDatabase,
) -> None:
    db = retrieval_db
    for task_id in ("reference", "verified", "a-lexical", "z-similar", "provisional"):
        retrieval_task(db, task_id)
    reference = retrieval_trace(db, "reference")
    retrieval_trace(db, "verified", reward=0.0, facet_changes={
        "tests": "untouched", "eval_awareness": "none", "shortcut": "no shortcut",
        "grader_or_harness_touched": True, "answer_material_accessed": True,
        "success_claim_without_evidence": False, "suspicion": 0,
    })
    retrieval_trace(db, "a-lexical", tier=TIER2, count=8, facet_changes={"tests": "read", "suspicion": 0})
    retrieval_trace(db, "z-similar", tier=TIER2)
    retrieval_trace(db, "provisional", tier=TIER3)
    retriever = CloudTraceRetriever(db, registry=SyntheticRegistry())

    examples = retriever.retrieve_similar_examples("parser", top_k=10, like_run_id=reference)
    assert [example.task_id for example in examples] == ["verified", "z-similar", "a-lexical"]
    assert [example.provenance["facet_similarity"] for example in examples] == [0, 8, 6]
    assert examples[1].score < examples[2].score
    assert retriever.retrieve_similar_examples("parser", top_k=1, like_run_id=reference) == examples[:1]
    assert [example.task_id for example in retriever.retrieve_similar_examples(
        "parser", top_k=10, like_run_id=reference, exclude_task_ids={"z-similar"},
    )] == ["verified", "a-lexical"]
    assert [example.task_id for example in retriever.retrieve_similar_examples(
        "", top_k=10, like_run_id=reference, include_provisional=True,
    )] == ["verified", "z-similar", "a-lexical", "provisional"]


def test_like_run_chooses_most_similar_trace_within_the_tasks_strongest_trace_tier(
    retrieval_db: CloudTraceDatabase,
) -> None:
    db = retrieval_db
    for task_id in ("reference", "same-tier", "mixed-tier"):
        retrieval_task(db, task_id)
    reference = retrieval_trace(db, "reference")
    retrieval_trace(db, "same-tier", "a-lexical", count=8, facet_changes={"tests": "read"})
    similar = retrieval_trace(db, "same-tier", "z-similar")
    trusted = retrieval_trace(db, "mixed-tier", "trusted", facet_changes={"tests": "read", "suspicion": 0})
    retrieval_trace(db, "mixed-tier", "similar", tier=TIER2)
    retriever = CloudTraceRetriever(db, registry=SyntheticRegistry())

    examples = retriever.retrieve_similar_examples("", top_k=10, like_run_id=reference)
    assert [(example.trace_id, example.provenance["facet_similarity"]) for example in examples] == [
        (similar, 8), (trusted, 6),
    ]
    assert retriever.retrieve_similar_examples("", top_k=1, like_run_id=reference) == examples[:1]


def test_like_run_uses_only_its_deepest_cluster_for_the_similarity_bonus(
    retrieval_db: CloudTraceDatabase,
) -> None:
    db = retrieval_db
    runs = {}
    for task_id in ("reference", "a-broad", "m-middle", "z-deep"):
        retrieval_task(db, task_id)
        runs[task_id] = retrieval_trace(db, task_id)
    db.replace_run_clusters([
        cluster("root", 1, None, list(runs.values())),
        cluster("middle", 2, "root", [runs[task_id] for task_id in ("reference", "m-middle", "z-deep")]),
        cluster("deep", 3, "middle", [runs["reference"], runs["z-deep"]]),
    ], registry=SyntheticRegistry())

    examples = CloudTraceRetriever(db, registry=SyntheticRegistry()).retrieve_similar_examples(
        "", top_k=10, like_run_id=runs["reference"],
    )
    assert [example.task_id for example in examples] == ["z-deep", "a-broad", "m-middle"]
    assert [example.provenance["facet_similarity"] for example in examples] == [9, 8, 8]
    assert all(example.score == 0.0 for example in examples)


def test_like_reference_need_not_match_the_hard_facet_filter(retrieval_db: CloudTraceDatabase) -> None:
    retrieval_task(retrieval_db, "reference")
    reference = retrieval_trace(retrieval_db, "reference")
    retrieval_task(retrieval_db, "read-tests")
    run_id = retrieval_trace(retrieval_db, "read-tests", facet_changes={"tests": "read"})
    (example,) = CloudTraceRetriever(retrieval_db, registry=SyntheticRegistry()).retrieve_similar_examples(
        "", facets={"tests": "read"}, like_run_id=reference,
    )
    assert example.trace_id == run_id and example.provenance["facet_similarity"] == 7


@pytest.mark.parametrize("facets", [
    {"test": "modified"}, {"tests": "rewritten"}, {"outcome": []}, {"suspicion": True},
    {"answer_material_accessed": 1}, {"source": ""}, ["tests"], {"suspicion": [1, 2.0]},
])
def test_example_facet_validation_matches_search_runs(retrieval_db: CloudTraceDatabase, facets: Any) -> None:
    with pytest.raises(ValueError, match="facets"):
        CloudTraceRetriever(retrieval_db, registry=SyntheticRegistry()).retrieve_similar_examples("", facets=facets)


def test_unknown_cluster_and_unavailable_like_runs_raise_without_disclosing_admission(
    retrieval_db: CloudTraceDatabase,
) -> None:
    db = retrieval_db
    for task_id in ("safe", "held", "locked", "unfaceted"):
        retrieval_task(db, task_id)
    runs = [retrieval_trace(db, task_id) for task_id in ("safe", "held", "locked")]
    retrieval_trace(db, "unfaceted", faceted=False)
    db.replace_run_clusters([cluster("all", 1, None, runs)], registry=SyntheticRegistry())
    db.upsert_task({
        "task_id": "held", "dataset_id": "synthetic-corpus", "split": "TEST",
        "instruction_text": "repair the parser",
    })
    registry = SyntheticRegistry()
    registry._assigned_test_names.add("locked")
    retriever = CloudTraceRetriever(db, registry=registry)

    assert [example.trace_id for example in retriever.retrieve_similar_examples(
        "parser", top_k=10, facets={}, cluster_id="all",
    )] == ["safe/run"]
    errors = []
    for run_id in ("missing/run", "held/run", "locked/run", "unfaceted/run"):
        with pytest.raises(ValueError, match="like_run_id") as error:
            retriever.retrieve_similar_examples("", like_run_id=run_id)
        errors.append(str(error.value))
    assert len(set(errors)) == 1
    with pytest.raises(ValueError, match="cluster_id"):
        retriever.retrieve_similar_examples("", cluster_id="unknown")


def test_empty_admission_reads_no_atlas_bodies(retrieval_db: CloudTraceDatabase) -> None:
    db = retrieval_db
    retrieval_task(db, "safe")
    run_id = retrieval_trace(db, "safe")
    db.replace_run_clusters([cluster("known", 1, None, [run_id])], registry=SyntheticRegistry())
    registry = SyntheticRegistry()
    registry.split_assignments_digest = None
    retriever = CloudTraceRetriever(db, registry=registry)

    def authorize(action: int, table: str, column: str, *unused: Any) -> int:
        if action == sqlite3.SQLITE_READ and (
            table in {"run_facets", "run_clusters"}
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    db._connection.set_authorizer(authorize)
    try:
        assert retriever.retrieve_similar_examples("", facets={}) == []
        for cluster_id in ("known", "unknown"):
            with pytest.raises(ValueError, match="^unknown cluster_id$"):
                retriever.retrieve_similar_examples("", cluster_id=cluster_id)
        with pytest.raises(ValueError, match="like_run_id"):
            retriever.retrieve_similar_examples("", like_run_id=run_id)
    finally:
        db._connection.set_authorizer(None)


def test_screening_cannot_make_top_one_stop_before_a_more_similar_returnable_task(
    retrieval_db: CloudTraceDatabase,
) -> None:
    db = retrieval_db
    for task_id in ("reference", "a-first", "z-masked"):
        retrieval_task(db, task_id)
    reference = retrieval_trace(db, "reference")
    retrieval_trace(db, "a-first", facet_changes={"tests": "read"})
    retrieval_issue(db, "z-masked")
    retrieval_trace(db, "z-masked", "screened", model_id="sealed-target", reward=0.0, facet_changes={
        "tests": "untouched", "eval_awareness": "none", "shortcut": "no shortcut",
        "grader_or_harness_touched": True, "answer_material_accessed": True,
        "success_claim_without_evidence": False, "suspicion": 0,
    })
    winner = retrieval_trace(db, "z-masked", "returnable", tier=TIER2)
    retriever = CloudTraceRetriever(db, registry=SyntheticRegistry(), sealed_task_ids=["sealed-target"])

    (example,) = retriever.retrieve_similar_examples("parser", top_k=1, like_run_id=reference)
    assert example.trace_id == winner and example.trust_tier == TIER1
    assert example.provenance["trace_trust_tier"] == TIER2
    assert example.provenance["facet_similarity"] == 8
    assert "sealed-target" not in json.dumps(example.to_dict())


def test_screening_a_best_similarity_trace_preserves_best_first_ranking(retrieval_db: CloudTraceDatabase) -> None:
    db = retrieval_db
    for task_id in ("reference", "a-screened", "z-winner"):
        retrieval_task(db, task_id)
    reference = retrieval_trace(db, "reference")
    retrieval_trace(db, "a-screened", "screened", model_id="sealed-target")
    retrieval_trace(db, "a-screened", "returnable", facet_changes={"tests": "read", "suspicion": 0})
    winner = retrieval_trace(db, "z-winner", facet_changes={"tests": "read"})
    retriever = CloudTraceRetriever(db, registry=SyntheticRegistry(), sealed_task_ids=["sealed-target"])

    examples = retriever.retrieve_similar_examples("parser", top_k=10, like_run_id=reference)
    assert [example.task_id for example in examples] == ["z-winner", "a-screened"]
    assert examples[0].trace_id == winner
    assert retriever.retrieve_similar_examples("parser", top_k=1, like_run_id=reference) == examples[:1]


@pytest.mark.parametrize("marker", ("sealed-target", 'synthetic-"quoted"'))
def test_sealed_facet_text_is_neither_returned_nor_usable_as_a_like_reference(
    retrieval_db: CloudTraceDatabase, marker: str,
) -> None:
    db = retrieval_db
    for task_id in ("safe", "contaminated"):
        retrieval_task(db, task_id)
    retrieval_trace(db, "safe")
    contaminated = retrieval_trace(db, "contaminated", facet_changes={"goal": f"inspect {marker}", "shortcut": marker})
    retriever = CloudTraceRetriever(db, registry=SyntheticRegistry(), sealed_task_ids=[marker])
    assert [example.task_id for example in retriever.retrieve_similar_examples("parser", facets={})] == ["safe"]
    with pytest.raises(ValueError, match="like_run_id"):
        retriever.retrieve_similar_examples("", like_run_id=contaminated)


def test_empty_query_misalignment_can_use_flagged_nonexploit_trace(retrieval_db: CloudTraceDatabase) -> None:
    retrieval_task(retrieval_db, "flagged")
    run_id = retrieval_trace(retrieval_db, "flagged", is_exploit=False, flagged_passage_steps=[0])
    (example,) = CloudTraceRetriever(retrieval_db, registry=SyntheticRegistry()).retrieve_similar_examples(
        "", "misalignment", facets={},
    )
    assert example.trace_id == run_id and example.trace_steps[0]["flagged"] is True
    assert example.score == 0.0


@pytest.mark.parametrize("seal", (None, "similarity", "run_facets_matched"))
def test_default_retrieval_does_not_consult_or_change_with_atlas_records(
    retrieval_db: CloudTraceDatabase, seal: str | None,
) -> None:
    db = retrieval_db
    retrieval_task(db, "default-task")
    run_id = retrieval_trace(db, "default-task", faceted=False)
    retriever = CloudTraceRetriever(db, registry=SyntheticRegistry(), sealed_task_ids=() if seal is None else (seal,))
    before = [example.to_dict() for example in retriever.retrieve_similar_examples("parser")]
    assert len(before) == 1
    db.upsert_run_facets([facet(run_id, evidence_steps=[0])], registry=SyntheticRegistry())
    db.replace_run_clusters([cluster("stored", 1, None, [run_id])], registry=SyntheticRegistry())

    def authorize(action: int, table: str, column: str, *unused: Any) -> int:
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table in {
            "run_facets", "run_clusters",
        } else sqlite3.SQLITE_OK

    db._connection.set_authorizer(authorize)
    try:
        after = retriever.retrieve_similar_examples("parser", facets=None, cluster_id=None, like_run_id=None)
        assert [example.to_dict() for example in after] == before
        assert "facet_similarity" not in after[0].provenance
        assert "run_facets_matched" not in after[0].provenance
        assert retriever.retrieve_similar_examples("") == []
    finally:
        db._connection.set_authorizer(None)


def test_admission_facets_clusters_and_evidence_share_one_snapshot(
    retrieval_db: CloudTraceDatabase, tmp_path: Path,
) -> None:
    db = retrieval_db
    db._execute("PRAGMA journal_mode = WAL")
    for task_id in ("reference", "candidate"):
        retrieval_task(db, task_id)
    reference = retrieval_trace(db, "reference")
    candidate = retrieval_trace(db, "candidate")
    db.replace_run_clusters([cluster("before", 1, None, [candidate])], registry=SyntheticRegistry())
    changed = False
    with closing(sqlite3.connect(tmp_path / "facet-retrieval.sqlite")) as writer:
        def authorize(action: int, table: str, column: str, *unused: Any) -> int:
            nonlocal changed
            if not changed and action == sqlite3.SQLITE_READ and table == "run_facets":
                changed = True
                with writer:
                    writer.execute("UPDATE corpus_tasks SET split = 'TEST' WHERE task_id = 'candidate'")
                    writer.execute("UPDATE run_facets SET tests = 'untouched' WHERE run_id = ?", (candidate,))
                    writer.execute("DELETE FROM run_clusters")
                    writer.execute(
                        "UPDATE trace_steps SET content_text = 'changed after admission' WHERE trace_id = ?",
                        (candidate,),
                    )
            return sqlite3.SQLITE_OK

        db._connection.set_authorizer(authorize)
        try:
            retriever = CloudTraceRetriever(db, registry=SyntheticRegistry())
            (example,) = retriever.retrieve_similar_examples(
                "parser", facets={"tests": "modified"}, cluster_id="before", like_run_id=reference,
            )
            assert changed and example.trace_id == candidate
            assert example.trace_steps[0]["content_text"] == "inspect the parser"
        finally:
            db._connection.set_authorizer(None)
    assert retriever.retrieve_similar_examples(
        "parser", facets={"tests": "modified"}, like_run_id=reference,
    ) == []


@pytest.mark.parametrize("unavailable", ("test", "held", "locked", "sealed", "reparented", "sealed-facet"))
def test_cluster_ranking_and_filtering_ignore_unavailable_members(
    retrieval_db: CloudTraceDatabase, unavailable: str,
) -> None:
    db = retrieval_db
    runs = {}
    for task_id in ("reference", "alpha", "zeta", "hidden"):
        retrieval_task(db, task_id)
        runs[task_id] = retrieval_trace(db, task_id)
    db.replace_run_clusters([
        cluster("a", 1, None, [runs["reference"], runs["alpha"], runs["hidden"]]),
        cluster("z", 1, None, [runs["reference"], runs["zeta"]]),
        cluster("hidden-only", 1, None, [runs["hidden"]]),
    ], registry=SyntheticRegistry())
    registry = SyntheticRegistry()
    seals = []
    if unavailable in {"test", "held"}:
        db.upsert_task({
            "task_id": "hidden", "dataset_id": "synthetic-corpus", "split": unavailable,
            "instruction_text": "repair the parser",
        })
    elif unavailable == "locked":
        registry._assigned_test_names.add("hidden")
    elif unavailable == "sealed":
        seals = ["hidden"]
    elif unavailable == "reparented":
        retrieval_task(db, "new-owner")
        db.upsert_trace({
            "trace_id": runs["hidden"], "task_id": "new-owner", "run_id": "synthetic-batch",
            "model_id": "synthetic-model",
        })
    else:
        db.upsert_run_facets([
            facet(runs["hidden"], evidence_steps=[0], goal="inspect sealed-target"),
        ], registry=registry)
        seals = ["sealed-target"]
    retriever = CloudTraceRetriever(db, registry=registry, sealed_task_ids=seals)

    before = retriever.retrieve_similar_examples("", top_k=10, like_run_id=runs["reference"])
    assert [(example.task_id, example.provenance["facet_similarity"]) for example in before] == [
        ("alpha", 9), ("zeta", 8),
    ]
    assert [example.task_id for example in retriever.retrieve_similar_examples(
        "", cluster_id="a", like_run_id=runs["reference"],
    )] == ["alpha"]
    for cluster_id in ("hidden-only", "missing"):
        with pytest.raises(ValueError, match="^unknown cluster_id$"):
            retriever.retrieve_similar_examples("", cluster_id=cluster_id)

    db.replace_run_clusters([
        cluster("a", 1, None, [runs["reference"], runs["alpha"]]),
        cluster("z", 1, None, [runs["reference"], runs["zeta"]]),
    ], registry=registry)
    assert retriever.retrieve_similar_examples("", top_k=10, like_run_id=runs["reference"]) == before


@pytest.mark.parametrize("task_wide_evidence", ("excluded-trace", "issue"))
def test_facet_matching_trace_trust_controls_partial_match_widening(
    retrieval_db: CloudTraceDatabase, task_wide_evidence: str,
) -> None:
    db = retrieval_db
    for task_id in ("a-unreturnable", "z-partial"):
        retrieval_task(db, task_id)
    retrieval_trace(db, "a-unreturnable", "matching-provisional", tier=TIER3)
    if task_wide_evidence == "excluded-trace":
        full_match = retrieval_trace(db, "a-unreturnable", "excluded-trusted", facet_changes={"tests": "untouched"})
        db.replace_trace_steps(full_match, [{"step_index": 0, "role": "assistant", "content_text": "alpha beta"}])
    else:
        db.upsert_task_issue({
            "issue_id": "a-unreturnable/issue", "task_id": "a-unreturnable",
            "defect_class": "missing_output_check", "direction": "wrong_pass", "status": "confirmed",
            "trust_tier": TIER1, "reproducer_script": "alpha beta",
        })
    partial = retrieval_trace(db, "z-partial")
    db.replace_trace_steps(partial, [{"step_index": 0, "role": "assistant", "content_text": "alpha"}])
    retriever = CloudTraceRetriever(db, registry=SyntheticRegistry())

    for top_k in (1, 2):
        examples = retriever.retrieve_similar_examples(
            "alpha beta", top_k=top_k, facets={"tests": "modified"},
        )
        assert [example.task_id for example in examples] == ["z-partial"]
        assert examples[0].trace_id == partial
    assert [example.task_id for example in retriever.retrieve_similar_examples(
        "alpha beta", top_k=1,
    )] == ["a-unreturnable"]


def build(build_id: str, runs: list[str], **extra: Any) -> list[dict[str, Any]]:
    """One producer build: a level-1 cluster holding every run, one child per level below."""
    meta = {"cluster_version": "run-clusters/v1", "build_id": build_id, "facet_count": len(runs), **extra}
    return [
        {**cluster("l1", 1, None, runs), **meta},
        {**cluster("l2", 2, "l1", runs), **meta},
        {**cluster("l3", 3, "l2", runs[:1]), **meta},
        {**cluster("l3b", 3, "l2", runs[1:]), **meta},
    ]


def test_cluster_import_stores_only_the_final_build_and_withholds_scoring_labels(
    faceted: CloudTraceDatabase, tmp_path: Path,
) -> None:
    runs = ["dev-task/pass", "dev-task/fail"]
    stale = build("old", runs[:1]) + build("old-2", runs)
    final = build("final", runs, label_rates={"cheat": 0.5}, rule_recall=0.9, rule_fpr=0.1, samples=["edited tests"])
    path = write_jsonl(tmp_path / "clusters.jsonl", *stale, *final, "{torn")
    receipt = import_run_clusters(faceted, path, registry=SyntheticRegistry())
    assert (receipt["build_id"], receipt["build_clusters"], receipt["invalid_json_lines"]) == ("final", 4, 1)
    assert (receipt["clusters"], receipt["runs"], receipt["clusters_with_label_fields_withheld"]) == (4, 2, 4)
    stored_clusters = clusters(faceted)
    assert set(stored_clusters) == {"l1", "l2", "l3", "l3b"}
    assert stored_clusters["l1"]["label_rates"] is None
    assert stored_clusters["l1"]["provenance_json"]["samples"] == ["edited tests"]
    assert {"rule_recall", "rule_fpr", "label_rates"}.isdisjoint(stored_clusters["l1"]["provenance_json"])


def test_cluster_import_refuses_an_incomplete_build_and_keeps_the_stored_hierarchy(
    faceted: CloudTraceDatabase, tmp_path: Path,
) -> None:
    faceted.replace_run_clusters(HIERARCHY, registry=SyntheticRegistry())
    unfinished = build("half", ["dev-task/pass", "dev-task/fail"])[:2]  # level 3 not yet written
    path = write_jsonl(tmp_path / "clusters.jsonl", *unfinished)
    with pytest.raises(ValueError, match="incomplete cluster build"):
        import_run_clusters(faceted, path, registry=SyntheticRegistry())
    assert set(clusters(faceted)) == {"c1", "c2", "c3"}


def test_cluster_import_refuses_unfaceted_runs_unless_asked_to_drop_and_count_them(
    faceted: CloudTraceDatabase, tmp_path: Path,
) -> None:
    runs = ["dev-task/pass", "elsewhere/no-trace"]
    path = write_jsonl(tmp_path / "clusters.jsonl", *build("mixed", runs))
    with pytest.raises(ValueError, match="lack stored facets"):
        import_run_clusters(faceted, path, registry=SyntheticRegistry())
    assert clusters(faceted) == {}
    receipt = import_run_clusters(faceted, path, registry=SyntheticRegistry(), drop_unfaceted=True)
    assert (receipt["runs"], receipt["dropped_runs"], receipt["dropped_clusters"]) == (1, 1, 1)
    stored_clusters = clusters(faceted)
    # The level-3 cluster that held only the unstored run vanishes; the others shrink.
    assert set(stored_clusters) == {"l1", "l2", "l3"}
    assert stored_clusters["l1"]["run_ids"] == ["dev-task/pass"] and stored_clusters["l1"]["size"] == 1
