"""``evallab qa-data``: budgeted, admission-first answers over synthetic DEV, TEST and locked runs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from evallab.cli import parser as cli_parser
from evallab.production import qa_data, qa_data_calls
from evallab.production.cloud_trace_db import CloudTraceDatabase, CloudTraceRetriever
from evallab.production.corpus_queue import CorpusRegistry
from evallab.production.qa_data import UNAVAILABLE, Budget, QaContext, QaDataError, estimate_tokens

TIER1 = "tier1_container_verified"


class SyntheticRegistry(CorpusRegistry):
    """Admits the synthetic corpus; ``locked-task`` is TEST-assigned under another key."""

    def __init__(self) -> None:
        authority = b"key_id,item_id,split\nsynthetic-authority,source,dev\n"
        self.split_assignments_digest = "sha256:" + hashlib.sha256(authority).hexdigest()
        self._assignment_keys = frozenset({"synthetic-authority"})
        self._assignments = {("synthetic-authority", "source"): "dev"}
        self._assignment_names = {}
        self._assigned_test_ids = set()
        self._assigned_test_names = {"locked-task"}
        self._sealed_names = set()
        self._sealed_digests = set()
        self._sealed_ids = set()


def facet(run_id: str, **changes: Any) -> dict[str, Any]:
    return {
        "run_id": run_id, "source": "private-corpus-name", "split": "DEV",
        "facet_version": "run-facets/v1", "facet_model": "synthetic/model",
        "input_tokens": 100, "output_tokens": 10, "goal": "Fix the parser",
        "approach": "Edited the test file", "outcome": "pass", "tests": "modified",
        "grader_or_harness_touched": False, "answer_material_accessed": False,
        "success_claim_without_evidence": False, "eval_awareness": "none", "shortcut": "none",
        "suspicion": 1, "evidence_steps": [1], **changes,
    }


def add_task(db: CloudTraceDatabase, task_id: str, split: str = "dev") -> None:
    db.upsert_task({
        "task_id": task_id, "dataset_id": "private-corpus-name", "split": split,
        "instruction_text": "repair the parser so every output check passes",
    })


def add_run(
    db: CloudTraceDatabase, task_id: str, name: str, *, steps: int = 6, tier: str = TIER1,
    faceted: bool = True, **facet_changes: Any,
) -> str:
    run_id = f"{task_id}/{name}"
    db.upsert_trace({
        "trace_id": run_id, "run_id": "batch", "task_id": task_id, "model_id": "m", "reward": 1.0,
        "trust_tier": tier, "is_exploit": True,
    }, steps=[
        {"step_index": index, "role": "assistant", "content_text": f"step {index} inspect the parser"}
        for index in range(steps)
    ])
    if faceted:
        assert db.upsert_run_facets(
            [facet(run_id, **facet_changes)], registry=SyntheticRegistry(),
        )["stored"] == 1
    return run_id


@pytest.fixture
def store(tmp_path: Path) -> Iterator[CloudTraceDatabase]:
    with CloudTraceDatabase(tmp_path / "qa.sqlite", sqlite_fts=False) as database:
        add_task(database, "alpha")
        add_task(database, "beta")
        add_task(database, "locked-task")
        add_task(database, "held-out", "test")
        add_run(database, "alpha", "a1", suspicion=3, tests="modified")
        add_run(database, "alpha", "a2", suspicion=1, tests="read", outcome="fail")
        add_run(database, "beta", "b1", suspicion=2, tests="modified")
        yield database


def context(store: CloudTraceDatabase, tokens: int = 4000) -> QaContext:
    return QaContext(
        database=store, retriever=CloudTraceRetriever(store, registry=SyntheticRegistry()),
        repo_root=Path.cwd(), state_dir=Path.cwd(), budget=Budget(tokens),
    )


def test_budget_takes_whole_items_and_counts_what_it_omits() -> None:
    budget = Budget(10)
    assert budget.take("x" * 20)  # 5 tokens
    assert not budget.take("y" * 40)  # 10 tokens, only 5 left
    assert budget.take("z" * 20)
    assert (budget.used, budget.omitted, budget.remaining) == (10, 1, 0)
    text, cut = Budget(2).take_text("abcdefghijkl")
    assert (text, cut) == ("abcdefgh", True)


def test_run_search_returns_blind_triage_records_most_suspicious_first(store: CloudTraceDatabase) -> None:
    data = qa_data_calls.runs(
        context(store), facets={"tests": "modified"}, cluster_id=None, like_run_id=None,
        query="", task_id=None, limit=10,
    )
    assert [run["run_id"] for run in data["runs"]] == ["alpha/a1", "beta/b1"]
    record = data["runs"][0]
    assert {"source", "split", "facet_model"}.isdisjoint(record)
    assert (record["suspicion"], record["tests"], record["task_id"]) == (3, "modified", "alpha")
    only_alpha = qa_data_calls.runs(
        context(store), facets={}, cluster_id=None, like_run_id=None, query="", task_id="alpha", limit=10,
    )
    assert [run["run_id"] for run in only_alpha["runs"]] == ["alpha/a1", "alpha/a2"]


def test_run_search_budget_omits_whole_runs_and_says_so(store: CloudTraceDatabase) -> None:
    one_run = estimate_tokens(qa_data_calls.runs(
        context(store), facets={"tests": "modified"}, cluster_id=None, like_run_id=None,
        query="", task_id=None, limit=1,
    )["runs"][0])
    data = qa_data_calls.runs(
        context(store, one_run + 5), facets={"tests": "modified"}, cluster_id=None, like_run_id=None,
        query="", task_id=None, limit=10,
    )
    assert len(data["runs"]) == 1 and data["matched"] == 2 and data["omitted"] == 1 and data["truncated"]


def test_cluster_and_similarity_search_attach_the_runs_deepest_cluster(store: CloudTraceDatabase) -> None:
    registry = SyntheticRegistry()
    store.replace_run_clusters([
        {"cluster_id": "top", "name": "edits tests", "level": 1, "parent": None,
         "run_ids": ["alpha/a1", "beta/b1"], "centroid_text": "edited tests", "size": 2},
        {"cluster_id": "mid", "name": "edits tests, passes", "level": 2, "parent": "top",
         "run_ids": ["alpha/a1", "beta/b1"], "centroid_text": "edited tests", "size": 2},
        {"cluster_id": "leaf", "name": "edits tests, alpha only", "level": 3, "parent": "mid",
         "run_ids": ["alpha/a1"], "centroid_text": "edited tests", "size": 1},
    ], registry=registry)
    members = qa_data_calls.runs(
        context(store), facets={}, cluster_id="top", like_run_id=None, query="", task_id=None, limit=10,
    )
    assert {run["run_id"] for run in members["runs"]} == {"alpha/a1", "beta/b1"}
    assert next(r for r in members["runs"] if r["run_id"] == "alpha/a1")["cluster"] == {"id": "leaf", "level": 3}
    similar = qa_data_calls.runs(
        context(store), facets={}, cluster_id=None, like_run_id="alpha/a1", query="", task_id=None, limit=10,
    )
    # Every other run is listed (not one per task), the reference itself is not, and the shared deepest
    # cluster adds one point of similarity.
    assert [run["run_id"] for run in similar["runs"]] == ["beta/b1", "alpha/a2"]
    assert similar["runs"][0]["facet_similarity"] == similar["runs"][1]["facet_similarity"] + 1 + 3 - 3 or (
        similar["runs"][0]["facet_similarity"] > similar["runs"][1]["facet_similarity"]
    )


@pytest.mark.parametrize("identity", ("held-out", "locked-task", "missing-task"))
def test_unavailable_tasks_and_runs_are_indistinguishable(store: CloudTraceDatabase, identity: str) -> None:
    if identity != "missing-task":
        add_run(store, identity, "r1", faceted=False)
    ctx = context(store)
    refusals = []
    for call in (
        lambda: qa_data_calls.history(ctx, identity, flags=[], limit=3),
        lambda: qa_data_calls.window(ctx, f"{identity}/r1", first=None, last=None, around=None, radius=3),
        lambda: qa_data_calls.runs(
            ctx, facets={}, cluster_id=None, like_run_id=None, query="", task_id=identity, limit=3,
        ),
        lambda: qa_data_calls.runs(
            ctx, facets={}, cluster_id=None, like_run_id=f"{identity}/r1", query="", task_id=None, limit=3,
        ),
        lambda: qa_data_calls.runs(
            ctx, facets={}, cluster_id="no-such-cluster", like_run_id=None, query="", task_id=None, limit=3,
        ),
    ):
        with pytest.raises(QaDataError) as raised:
            call()
        refusals.append(str(raised.value))
    assert set(refusals) == {UNAVAILABLE}


def test_window_returns_a_radius_around_a_step_and_pages_under_a_small_budget(store: CloudTraceDatabase) -> None:
    data = qa_data_calls.window(
        context(store), "alpha/a1", first=None, last=None, around=3, radius=1,
    )
    assert [step["step_index"] for step in data["steps"]] == [2, 3, 4]
    assert data["run_id"] == "alpha/a1" and not data["truncated"]
    one_step = estimate_tokens(data["steps"][0])
    small = qa_data_calls.window(
        context(store, one_step * 2 + 1), "alpha/a1", first=0, last=5, around=None, radius=3,
    )
    assert [step["step_index"] for step in small["steps"]] == [0, 1]
    assert small["truncated"] and small["omitted"] == 4 and small["next_first"] == 2


def test_history_lists_own_issues_similar_issues_and_matching_cards(store: CloudTraceDatabase) -> None:
    for task_id, issue_id, defect, status in (
        ("alpha", "alpha/open", "zero_invariant_tolerance", "confirmed"),
        ("alpha", "alpha/wrong", "weak_check", "refuted"),
        ("beta", "beta/issue", "zero_invariant_tolerance", "confirmed"),
    ):
        store.upsert_task_issue({
            "issue_id": issue_id, "task_id": task_id, "defect_class": defect, "direction": "wrong_pass",
            "status": status, "trust_tier": TIER1, "reproducer_script": "parser accepts wrong output",
        })
    data = qa_data_calls.history(context(store), "alpha", flags=[], limit=5)
    assert [issue["issue_id"] for issue in data["issues"]] == ["alpha/open"]
    assert (data["issues_total"], data["refuted_issues"]) == (1, 1)
    assert data["issues"][0]["expand"] == "issue:alpha/open"
    assert [card["card_id"] for card in data["playbook_cards"]] == ["QA05"]  # issue:<defect_class> trigger
    assert [issue["task_id"] for issue in data["similar_issues"]] == ["beta"]
    assert {card["card_id"] for card in data["card_index"]} >= {"QA01", "QA06"}


def test_cli_envelope_reports_tokens_latency_and_a_uniform_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "qa.sqlite"
    with CloudTraceDatabase(database, sqlite_fts=False) as db:
        add_task(db, "held-out", "test")
    argv = ["qa-data", "history", "held-out", "--cloud-database", str(database), "--budget", "800"]
    args = cli_parser().parse_args(argv)
    assert args.func(args, Path.cwd(), harbor=None) == 2  # the real registry never admits a TEST-split task
    reply = json.loads(capsys.readouterr().out)
    assert reply["ok"] is False and reply["error"] == UNAVAILABLE
    assert {"call", "budget", "tokens_est", "latency_ms", "truncated"} <= reply["meta"].keys()
    assert reply["meta"]["call"] == "history" and reply["meta"]["latency_ms"] >= 0


def test_cli_rejects_a_search_with_no_criteria_and_malformed_facets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "qa.sqlite"
    CloudTraceDatabase(database, sqlite_fts=False).close()
    for extra, message in (([], "give at least one"), (["--facet", "tests"], "key=value")):
        args = cli_parser().parse_args(
            ["qa-data", "runs", "--cloud-database", str(database), *extra],
        )
        assert args.func(args, Path.cwd(), harbor=None) == 2
        assert message in json.loads(capsys.readouterr().out)["error"]


def test_window_steps_carry_no_evaluator_annotations_or_pointers(store: CloudTraceDatabase) -> None:
    store.upsert_trace({
        "trace_id": "alpha/annotated", "run_id": "batch", "task_id": "alpha", "model_id": "m", "reward": 1.0,
        "trust_tier": TIER1, "is_exploit": True, "flagged_passage_steps": [0],
    }, steps=[{
        "step_index": 0, "role": "assistant", "content_text": "edit the test",
        "observation_cas_uri": "cas://sha256/" + "a" * 64,
        "flags_json": {"label": "cheat", "evaluator_explanation": "graded as a hack", "passage_positive": True},
    }])
    (step,) = qa_data_calls.window(context(store), "alpha/annotated", first=0, last=3, around=None, radius=1)["steps"]
    assert step == {
        "step_index": 0, "role": "assistant", "tool_name": None, "command_or_action": None,
        "content_text": "edit the test", "observation_preview": None,
    }


def test_cluster_names_and_selection_ignore_members_that_became_unavailable(store: CloudTraceDatabase) -> None:
    def clusters(hidden: bool) -> list[dict[str, Any]]:
        runs_a = ["alpha/a1", "alpha/a2", "beta/b1"] + (["gamma/g1"] if hidden else [])
        return [
            {"cluster_id": "a", "name": "alpha group", "level": 1, "parent": None, "run_ids": runs_a,
             "centroid_text": "x", "size": len(runs_a)},
            {"cluster_id": "z", "name": "beta group", "level": 1, "parent": None,
             "run_ids": ["alpha/a1", "alpha/a2"], "centroid_text": "x", "size": 2},
        ]

    add_task(store, "gamma")
    add_run(store, "gamma", "g1")
    store.replace_run_clusters(clusters(False), registry=SyntheticRegistry())
    outcomes = []
    for hidden in (False, True):
        if hidden:  # gamma leaves DEV after its run was clustered under cluster "a"
            add_task(store, "gamma", "test")
        with store._transaction(write=True):
            store._execute("DELETE FROM run_clusters")
            for row in clusters(hidden):
                store._upsert("run_clusters", {**row, "label_rates": None, "provenance_json": {}}, ("cluster_id",))
        data = qa_data_calls.runs(
            context(store), facets={"tests": "modified"}, cluster_id=None, like_run_id=None, query="", task_id=None, limit=5,
        )
        outcomes.append({run["run_id"]: run["cluster"] for run in data["runs"] if "cluster" in run})
    visible, with_hidden_member = outcomes
    # What is returned (cluster id, level, and no model-written name) is identical whether or not
    # a now-unavailable run sits in the cluster.
    assert visible == with_hidden_member
    assert visible["alpha/a1"] == {"id": "z", "level": 1}
    assert visible["beta/b1"] == {"id": "a", "level": 1}


def test_a_query_naming_a_protected_identity_behaves_like_any_other_empty_query(store: CloudTraceDatabase) -> None:
    results = [
        qa_data_calls.runs(
            context(store), facets={}, cluster_id=None, like_run_id=None, query=query, task_id=None, limit=5,
        )
        for query in ("held-out", "locked-task", "no-such-task-anywhere")
    ]
    assert all(result["runs"] == [] and result["matched"] == 0 for result in results)


def test_runs_lists_every_run_of_a_task_not_one_per_task(store: CloudTraceDatabase) -> None:
    data = qa_data_calls.runs(
        context(store), facets={}, cluster_id=None, like_run_id=None, query="parser", task_id=None, limit=10,
    )
    assert sorted(run["run_id"] for run in data["runs"]) == ["alpha/a1", "alpha/a2", "beta/b1"]
    assert qa_data_calls.runs(
        context(store), facets={}, cluster_id=None, like_run_id=None, query="trace text only", task_id=None, limit=10,
    )["matched"] == 0  # the query reads facet goal/approach/shortcut, not trace text


def test_admission_and_row_read_share_one_snapshot(store: CloudTraceDatabase, tmp_path: Path) -> None:
    """A writer reclassifying a task mid-call cannot make an earlier admission authorize the later read."""
    ctx = context(store)
    path = tmp_path / "qa.sqlite"
    store._execute("PRAGMA journal_mode=WAL")  # a reader's snapshot does not block the writer
    original = ctx.retriever._admitted_ids

    def admit_then_reclassify() -> Any:
        result = original()
        with CloudTraceDatabase(path, sqlite_fts=False) as writer:
            writer.upsert_task({
                "task_id": "alpha", "dataset_id": "private-corpus-name", "split": "test",
                "instruction_text": "now held out",
            })
        return result

    ctx.retriever._admitted_ids = admit_then_reclassify  # type: ignore[method-assign]
    row = ctx.require_task("alpha")
    # The snapshot began before the writer, so the row is the one that was admitted, not the TEST rewrite.
    assert row["split"] == "dev" and row["instruction_text"] != "now held out"


def test_read_only_open_never_writes_or_migrates(tmp_path: Path) -> None:
    path = tmp_path / "ro.sqlite"
    with CloudTraceDatabase(path, sqlite_fts=False) as writable:
        add_task(writable, "alpha")
    before = path.read_bytes()
    with CloudTraceDatabase(path, read_only=True) as reader:
        assert reader.get_task("alpha") is not None
        with pytest.raises(Exception, match="readonly|read-only"):
            reader.upsert_task({"task_id": "x", "dataset_id": "d", "split": "dev", "instruction_text": "t"})
    assert path.read_bytes() == before


def test_a_reply_larger_than_the_budget_becomes_a_bounded_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "qa.sqlite"
    CloudTraceDatabase(database, sqlite_fts=False).close()
    monkeypatch.setattr(qa_data, "_dispatch", lambda *_: {"blob": "x" * 8000})
    args = cli_parser().parse_args(["qa-data", "runs", "--task", "t", "--cloud-database", str(database), "--budget", "300"])
    assert args.func(args, Path.cwd(), harbor=None) == 2
    reply = json.loads(capsys.readouterr().out)
    assert reply["ok"] is False and "raise --budget" in reply["error"]
    assert reply["meta"]["tokens_est"] <= 300
    args = cli_parser().parse_args(["qa-data", "runs", "--task", "t", "--cloud-database", str(database), "--budget", "50"])
    assert args.func(args, Path.cwd(), harbor=None) == 2
    assert "at least" in json.loads(capsys.readouterr().out)["error"]
