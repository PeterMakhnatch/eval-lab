"""QA inventory, cluster projections and facet recounts over synthetic split authority."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from evallab.production.cloud_trace_db import (
    CloudTraceDatabase,
    CloudTraceRetriever,
    import_run_facets,
)
from evallab.production.cloud_trace_export import CHEATBENCH_DATASET
from evallab.production.corpus_queue import CorpusItem, CorpusRegistry
from evallab.production.qa_data import UNAVAILABLE, Budget, QaContext, QaDataError, estimate_tokens
from evallab.production.qa_data_inventory import clusters, inventory, recount, traces


class SyntheticRegistry(CorpusRegistry):
    def __init__(self, root: Path) -> None:
        self.split_assignments_digest = "sha256:" + hashlib.sha256(b"synthetic authority").hexdigest()
        self._assignment_keys = frozenset({"authority", CHEATBENCH_DATASET})
        self._assignments = {
            ("authority", "source"): "dev",
            (CHEATBENCH_DATASET, "external-dev"): "dev",
            (CHEATBENCH_DATASET, "external-locked"): "dev",
        }
        self._assignment_names = {}
        self._assigned_test_ids = {"external-locked"}
        self._assigned_test_names = {"locked-task"}
        self._sealed_names = set()
        self._sealed_digests = set()
        self._sealed_ids = set()
        self.external_items = tuple(CorpusItem(
            dataset_id=CHEATBENCH_DATASET, item_id=item_id, split="dev", task_id=item_id,
            task_digest=None, source_path=root / "unopened.json", metadata={}, source_record_sha256="a" * 64,
        ) for item_id in ("external-dev", "external-locked"))

    def items(self, *, dataset_id: str | None = None, split: str | None = None) -> tuple[CorpusItem, ...]:
        return tuple(item for item in self.external_items
                     if dataset_id in (None, item.dataset_id) and split in (None, item.split))


def facet(run_id: str, **changes: Any) -> dict[str, Any]:
    return {
        "run_id": run_id, "source": "dataset-a", "split": "DEV", "facet_version": "run-facets/v1",
        "facet_model": "synthetic/model", "input_tokens": 1, "output_tokens": 1,
        "goal": "Repair output", "approach": "Changed implementation", "outcome": "pass", "tests": "read",
        "grader_or_harness_touched": False, "answer_material_accessed": False,
        "success_claim_without_evidence": False, "eval_awareness": "none", "shortcut": "none",
        "suspicion": 1, "evidence_steps": [], **changes,
    }


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[QaContext]:
    registry = SyntheticRegistry(tmp_path)
    with CloudTraceDatabase(tmp_path / "qa.sqlite", sqlite_fts=False) as db:
        for task_id, dataset, split in (
            ("namespace/alpha", "dataset-a", "dev"), ("beta", "dataset-b", "dev"),
            ("empty", "dataset-a", "dev"), ("test-task", "hidden-dataset", "test"),
            ("locked-task", "hidden-dataset", "dev"), ("sealed-task", "hidden-dataset", "dev"),
        ):
            db.upsert_task({"task_id": task_id, "dataset_id": dataset, "split": split, "instruction_text": "Repair output"})
        for trace_id, task_id, model, reward in (
            ("run-a", "namespace/alpha", "model-a", 1), ("run-b", "namespace/alpha", "model-b", 0),
            ("run-c", "beta", "model-a", 0.5), ("test-run", "test-task", "hidden-model", 1),
            ("locked-run", "locked-task", "hidden-model", 1), ("sealed-run", "sealed-task", "hidden-model", 1),
        ):
            db.upsert_trace({
                "trace_id": trace_id, "run_id": "batch", "task_id": task_id, "model_id": model, "reward": reward,
            }, steps=[])
        assert db.upsert_run_facets([facet("run-a"), facet("run-c", answer_material_accessed=True)], registry=registry)["stored"] == 2
        yield QaContext(
            database=db, retriever=CloudTraceRetriever(db, registry=registry, sealed_task_ids=["sealed-task"]),
            repo_root=tmp_path, state_dir=tmp_path / "state", budget=Budget(10_000),
        )


def listing(ctx: QaContext, **changes: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "task_id": None, "dataset": None, "model": None, "outcome": None, "limit": 100, "ids_only": False, **changes,
    }
    return traces(ctx, **arguments)


def write_cluster_file(ctx: QaContext, *nodes: dict[str, Any]) -> None:
    path = ctx.state_dir / "run-atlas/clusters-v1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({
        "cluster_version": "run-clusters/v1", "build_id": "current-build", "facet_count": 9,
        "name": "admitted approach", "level": 1, "parent": None, **node,
    }) + "\n" for node in nodes), encoding="utf-8")


def write_db_clusters(ctx: QaContext, *nodes: dict[str, Any]) -> None:
    # Model a previously clustered run becoming TEST without rebuilding the stored projection.
    with ctx.database._transaction(write=True):
        for node in nodes:
            ctx.database._upsert("run_clusters", {
                "name": "private hidden-run name", "level": 1, "parent": None,
                "centroid_text": "PRIVATE CENTROID", "size": len(node["run_ids"]),
                "label_rates": {"private_label": 1}, "provenance_json": {"build_id": "db-build"}, **node,
            }, ("cluster_id",))


def test_inventory_counts_only_admitted_tasks_and_all_their_runs(ctx: QaContext) -> None:
    payload = inventory(ctx, ids_out=None)
    assert (payload["admitted_tasks"], payload["admitted_runs"]) == (3, 3)
    assert payload["by_dataset"] == {"dataset-a": {"tasks": 2, "runs": 2}, "dataset-b": {"tasks": 1, "runs": 1}}
    assert payload["run_outcomes"] == {"pass": 1, "fail": 1, "unknown": 1}
    assert payload["models"] == {"model-a": 2, "model-b": 1}
    assert payload["models_total"] == 2
    assert "hidden" not in json.dumps(payload)


def test_inventory_budget_keeps_totals_and_counts_omitted_dataset_and_model_entries(ctx: QaContext) -> None:
    full_ctx = replace(ctx, budget=Budget(10_000))
    full = inventory(full_ctx, ids_out=None)
    entries = [{key: value} for field in ("by_dataset", "models") for key, value in full[field].items()]
    room = full_ctx.budget.used - sum(map(estimate_tokens, entries)) + estimate_tokens(entries[0])
    bounded = replace(ctx, budget=Budget(room))
    payload = inventory(bounded, ids_out=None)
    kept = len(payload["by_dataset"]) + len(payload["models"])
    assert (payload["admitted_tasks"], payload["admitted_runs"]) == (3, 3)
    assert 0 < kept < len(entries)
    assert payload["omitted"] == bounded.budget.omitted == len(entries) - kept
    assert payload["truncated"] and estimate_tokens(payload) <= bounded.budget.limit


def test_inventory_reports_the_top_twelve_model_cut_with_distinct_total(ctx: QaContext) -> None:
    for index in range(15):
        ctx.database.upsert_trace({
            "trace_id": f"extra-model-{index:02d}", "run_id": "batch", "task_id": "beta",
            "model_id": f"model-{index:02d}",
        }, steps=[])
    payload = inventory(ctx, ids_out=None)
    assert payload["models_total"] == 17
    assert list(payload["models"]) == ["model-a", *(f"model-{index:02d}" for index in range(11))]
    assert payload["omitted"] == ctx.budget.omitted == 5 and payload["truncated"]


def test_inventory_id_file_is_complete_exclusive_and_requires_existing_parent(ctx: QaContext, tmp_path: Path) -> None:
    path = tmp_path / "ids.txt"
    payload = inventory(ctx, ids_out=path)
    assert payload["ids_file"] == {"path": str(path), "lines": 3}
    assert path.read_text().splitlines() == ["run-a", "run-b", "run-c"]
    assert "run-a" not in json.dumps(payload)
    with pytest.raises(QaDataError, match="must not exist"):
        inventory(ctx, ids_out=path)
    assert path.read_text().splitlines() == ["run-a", "run-b", "run-c"]
    with pytest.raises(QaDataError, match="parent must exist"):
        inventory(ctx, ids_out=tmp_path / "missing/ids.txt")


def test_traces_list_unfaceted_runs_and_filter_by_canonical_task(ctx: QaContext) -> None:
    full = listing(ctx, task_id="namespace/alpha")
    bare = listing(replace(ctx, budget=Budget(10_000)), task_id="alpha")
    assert bare == full
    assert [row["trace_id"] for row in full["traces"]] == ["run-a", "run-b"]
    assert full["task_id"] == "namespace/alpha"
    assert full["traces"][0] == {
        "trace_id": "run-a", "task_id": "namespace/alpha", "dataset_id": "dataset-a", "model_id": "model-a",
        "outcome": "pass", "step_count": 0, "faceted": True,
    }
    assert full["traces"][1]["faceted"] is False
    assert listing(ctx, dataset="dataset-a", model="model-b", outcome="fail", ids_only=True)["traces"] == ["run-b"]
    assert listing(ctx, outcome="unknown", ids_only=True)["traces"] == ["run-c"]


@pytest.mark.parametrize("task_id", ["test-task", "locked-task", "sealed-task", "missing-task"])
def test_non_admitted_task_filters_refuse_uniformly(ctx: QaContext, task_id: str) -> None:
    with pytest.raises(QaDataError) as error:
        listing(ctx, task_id=task_id)
    assert str(error.value) == UNAVAILABLE


def test_empty_listing_says_zero_matches_without_disclosing_hidden_identities(ctx: QaContext) -> None:
    payload = listing(ctx, dataset="hidden-dataset")
    assert payload == {"traces": [], "matched": 0, "omitted": 0, "truncated": False}
    assert listing(ctx, task_id="empty")["matched"] == 0


def test_trace_limit_and_budget_omit_whole_records_and_count_every_omission(ctx: QaContext) -> None:
    full_ctx = replace(ctx, budget=Budget(10_000))
    full = listing(full_ctx)
    limited = listing(replace(ctx, budget=Budget(10_000)), limit=1)
    assert (len(limited["traces"]), limited["matched"], limited["omitted"], limited["truncated"]) == (1, 3, 2, True)
    framing = full_ctx.budget.used - sum(map(estimate_tokens, full["traces"]))
    small = replace(ctx, budget=Budget(framing + estimate_tokens(full["traces"][0])))
    payload = listing(small)
    assert payload["traces"] == full["traces"][:1]
    assert payload["omitted"] == small.budget.omitted == 2
    assert payload["truncated"] and estimate_tokens(payload) <= small.budget.limit


def test_db_clusters_project_members_withhold_unsafe_names_and_bind_build(ctx: QaContext) -> None:
    write_db_clusters(ctx,
        {"cluster_id": "safe", "run_ids": ["run-a", "external-dev"], "name": "safe name"},
        {"cluster_id": "mixed", "run_ids": ["run-a", "test-run"], "name": "HIDDEN NAME"},
        {"cluster_id": "only-hidden", "run_ids": ["locked-run"]},
    )
    payload = clusters(ctx, members=None, source="db")
    by_id = {node["cluster_id"]: node for node in payload["clusters"]}
    assert set(by_id) == {"mixed", "safe"}
    assert payload["build_id"] == "db-build"
    assert all("name" not in node and "stored_size" not in node for node in by_id.values())
    assert by_id["mixed"]["admitted_size"] == 1
    assert "PRIVATE" not in json.dumps(payload) and "HIDDEN" not in json.dumps(payload)
    assert all("build_id" not in node for node in by_id.values())
    member_view = clusters(ctx, members="mixed", source="db")
    assert member_view["admitted_members"] == ["run-a"]
    assert member_view["build_id"] == "db-build"
    for cluster_id in ("only-hidden", "missing"):
        with pytest.raises(QaDataError) as error:
            clusters(ctx, members=cluster_id, source="db")
        assert str(error.value) == UNAVAILABLE


def test_file_clusters_use_latest_build_and_admit_registry_items_with_cross_key_gate(ctx: QaContext) -> None:
    write_cluster_file(ctx,
        {"build_id": "old-build", "cluster_id": "group", "run_ids": ["run-c"]},
        {"cluster_id": "group", "run_ids": ["run-a", "external-dev", "external-locked", "test-run"], "name": "HIDDEN NAME"},
        {"cluster_id": "safe", "level": 2, "parent": "group", "run_ids": ["run-b", "external-dev"]},
    )
    payload = clusters(ctx, members=None, source="file")
    assert (payload["build_id"], payload["facet_count"], payload["levels"]) == ("current-build", 9, {1: 1, 2: 1})
    group = payload["clusters"][0]
    assert (group["admitted_members"], group["cloud_runs"], group["registry_only_dev"]) == (2, 1, 1)
    assert all("name" not in node and "refused" not in node for node in payload["clusters"])
    members = clusters(ctx, members="group", source="file")
    assert members["build_id"] == "current-build"
    assert members["cloud_run_ids"] == ["run-a"]
    assert members["registry_only_dev_ids"] == ["external-dev"]
    assert "name" not in members and "refused" not in members
    assert (members["admitted_members"], members["cloud_runs"], members["registry_only_dev"]) == (2, 1, 1)
    serialized = json.dumps([payload, members])
    assert all(secret not in serialized for secret in ("external-locked", "test-run", "HIDDEN NAME"))
    with pytest.raises(QaDataError) as error:
        clusters(ctx, members="missing", source="file")
    assert str(error.value) == UNAVAILABLE


def test_registry_dev_item_cannot_readmit_a_denied_cloud_trace_with_the_same_id(ctx: QaContext) -> None:
    ctx.database.upsert_trace({
        "trace_id": "external-dev", "run_id": "batch", "task_id": "test-task", "model_id": "hidden-model",
    }, steps=[])
    write_cluster_file(ctx,
        {"cluster_id": "mixed", "run_ids": ["run-a", "external-dev"]},
        {"cluster_id": "only-denied", "run_ids": ["external-dev"]},
    )
    listing_ = clusters(ctx, members=None, source="file")
    assert [node["cluster_id"] for node in listing_["clusters"]] == ["mixed"]
    assert listing_["clusters"][0]["admitted_members"] == listing_["clusters"][0]["cloud_runs"] == 1
    assert listing_["clusters"][0]["registry_only_dev"] == 0
    members = clusters(ctx, members="mixed", source="file")
    assert members["cloud_run_ids"] == ["run-a"] and members["registry_only_dev_ids"] == []
    assert (members["admitted_members"], members["registry_only_dev"]) == (1, 0)
    assert "external-dev" not in json.dumps([listing_, members])
    with pytest.raises(QaDataError) as error:
        clusters(ctx, members="only-denied", source="file")
    assert str(error.value) == UNAVAILABLE


@pytest.mark.parametrize("source", ["db", "file"])
@pytest.mark.parametrize("members", [None, "visible"])
def test_cluster_answers_never_depend_on_denied_members_or_model_written_names(
    ctx: QaContext, source: str, members: str | None,
) -> None:
    write = write_db_clusters if source == "db" else write_cluster_file
    write(ctx, {"cluster_id": "visible", "run_ids": ["run-a"], "name": "initial name"})
    before = clusters(replace(ctx, budget=Budget(10_000)), members=members, source=source)
    write(ctx,
        {"cluster_id": "visible", "run_ids": ["run-a", "test-run", "locked-run", "external-locked"], "name": "HIDDEN NAME"},
        {"cluster_id": "only-denied", "run_ids": ["test-run"]},
    )
    after = clusters(replace(ctx, budget=Budget(10_000)), members=members, source=source)
    assert before == after
    serialized = json.dumps(after)
    assert all(key not in serialized for key in ("stored_size", "refused", "name", "test-run", "HIDDEN NAME"))
    with pytest.raises(QaDataError) as error:
        clusters(ctx, members="only-denied", source=source)
    assert str(error.value) == UNAVAILABLE


@pytest.mark.parametrize("postgres", [False, True])
def test_membership_builder_binds_backend_native_collections_without_interpolating_ids(postgres: bool) -> None:
    database = object.__new__(CloudTraceDatabase)
    database._postgres = postgres
    parameters: list[Any] = []
    identities = ["namespace/alpha", "quote'and-?"]
    predicate = database._task_id_scope("a.task_id", identities, parameters)
    assert all(identity not in predicate for identity in identities)
    if postgres:
        assert predicate == "a.task_id = ANY(?)"
        assert parameters == [identities]
    else:
        assert predicate == "a.task_id IN (SELECT value FROM json_each(?))"
        assert len(parameters) == 1 and json.loads(parameters[0]) == identities


@pytest.mark.parametrize("source", ["db", "file"])
def test_cluster_listings_rank_admitted_sizes_with_id_ties_and_filter_levels(ctx: QaContext, source: str) -> None:
    nodes = [
        {"cluster_id": "a-small", "run_ids": ["run-a"]},
        {"cluster_id": "z-large", "run_ids": ["run-a", "run-b", "run-c"]},
        {"cluster_id": "c-medium", "run_ids": ["run-a", "run-b"]},
        {"cluster_id": "b-medium", "run_ids": ["run-a", "run-c"]},
        {"cluster_id": "nested", "level": 2, "parent": "z-large", "run_ids": ["run-a", "run-b", "run-c"]},
    ]
    if source == "db":
        write_db_clusters(ctx, *nodes)
    else:
        write_cluster_file(ctx, *nodes)
    full = clusters(ctx, members=None, source=source)
    assert [node["cluster_id"] for node in full["clusters"]] == [
        "z-large", "b-medium", "c-medium", "a-small", "nested",
    ]
    assert full["build_id"] == ("db-build" if source == "db" else "current-build")
    assert all("build_id" not in node for node in full["clusters"])
    filtered = clusters(replace(ctx, budget=Budget(10_000)), members=None, source=source, level=2)
    assert filtered["clusters"] == full["clusters"][-1:]
    assert filtered["level"] == 2 and filtered["omitted"] == 0 and not filtered["truncated"]
    assert filtered["build_id"] == full["build_id"]
    empty = clusters(replace(ctx, budget=Budget(10_000)), members=None, source=source, level=3)
    assert empty["clusters"] == [] and empty["build_id"] == full["build_id"]
    if source == "file":
        assert filtered["levels"] == {2: 1}


def test_cluster_level_rejects_values_outside_the_three_level_tree(ctx: QaContext) -> None:
    for level in (0, 4):
        with pytest.raises(QaDataError, match="--level"):
            clusters(ctx, members=None, source="db", level=level)


def test_cluster_budget_charges_whole_ids_and_reports_omissions(ctx: QaContext) -> None:
    write_cluster_file(ctx, {"cluster_id": "group", "run_ids": ["run-a", "run-b", "run-c", "external-dev"]})
    full_ctx = replace(ctx, budget=Budget(10_000))
    full = clusters(full_ctx, members="group", source="file")
    all_ids = full["cloud_run_ids"] + full["registry_only_dev_ids"]
    framing = full_ctx.budget.used - sum(estimate_tokens([value]) for value in all_ids)
    bounded = replace(ctx, budget=Budget(framing + estimate_tokens(["run-a"])))
    payload = clusters(bounded, members="group", source="file")
    assert payload["cloud_run_ids"] == ["run-a"] and payload["registry_only_dev_ids"] == []
    assert payload["omitted"] == bounded.budget.omitted == 3 and payload["truncated"]
    assert (payload["admitted_members"], payload["cloud_runs"], payload["registry_only_dev"]) == (4, 3, 1)
    no_ids = clusters(replace(ctx, budget=Budget(framing)), members="group", source="file")
    assert no_ids["cloud_run_ids"] == no_ids["registry_only_dev_ids"] == []
    assert (no_ids["admitted_members"], no_ids["cloud_runs"], no_ids["registry_only_dev"]) == (4, 3, 1)
    assert no_ids["omitted"] == 4 and no_ids["truncated"]
    assert estimate_tokens(payload) <= bounded.budget.limit


@pytest.mark.parametrize("call", ["traces", "db-members", "file-members"])
def test_many_short_ids_charge_json_quoting_and_stay_inside_the_reply_budget(ctx: QaContext, call: str) -> None:
    ids = [f'id-"{index:03d}' for index in range(200)]
    for run_id in ids:
        ctx.database.upsert_trace({
            "trace_id": run_id, "run_id": "batch", "task_id": "namespace/alpha", "model_id": "short-id-model",
        }, steps=[])
    bounded = replace(ctx, budget=Budget(280))  # CLI --budget 400, less its 120-token envelope.
    if call == "traces":
        payload = listing(bounded, limit=200, ids_only=True)
        taken = payload["traces"]
        total = payload["matched"]
    else:
        write = write_db_clusters if call == "db-members" else write_cluster_file
        write(ctx, {"cluster_id": "many", "run_ids": ids})
        payload = clusters(bounded, members="many", source="db" if call == "db-members" else "file")
        taken = payload["admitted_members"] if call == "db-members" else payload["cloud_run_ids"]
        total = len(ids)
    assert 0 < len(taken) < 200
    assert all(run_id in ids or run_id in ("run-a", "run-b", "run-c") for run_id in taken)
    assert payload["omitted"] == total - len(taken) == bounded.budget.omitted
    assert payload["truncated"] and estimate_tokens(payload) <= bounded.budget.limit


def test_recount_hashes_locked_snapshot_deduplicates_last_and_histograms_admitted_runs(ctx: QaContext) -> None:
    path = ctx.state_dir / "run-atlas/facets-v1.jsonl"
    path.parent.mkdir(parents=True)
    records = [facet("run-a", answer_material_accessed=True), facet("run-a", suspicion=3),
               facet("run-b", tests="modified", answer_material_accessed=True),
               facet("test-run", suspicion=2), facet("locked-run", suspicion=2)]
    raw = ("\n".join(json.dumps(row) for row in records) + "\n\n").encode()
    path.write_bytes(raw)
    payload = recount(ctx)
    assert payload["jsonl_sha256"] == hashlib.sha256(raw).hexdigest()
    assert (payload["jsonl_lines"], payload["jsonl_unique_runs"], payload["jsonl_only_runs"], payload["db_facets"]) == (5, 4, 3, 2)
    assert payload["jsonl"] == {
        "admitted_cloud_runs": 2, "answer_material_accessed": 1, "suspicion": {1: 1, 3: 1}, "tests": {"modified": 1, "read": 1},
    }
    assert payload["db"] == {
        "admitted_cloud_runs": 2, "answer_material_accessed": 1, "suspicion": {1: 2}, "tests": {"read": 2},
    }
    assert "test-run" not in json.dumps(payload) and "locked-run" not in json.dumps(payload)


def test_recount_universe_is_the_runs_universe_and_a_withheld_run_leaves_no_count_behind(ctx: QaContext) -> None:
    named = facet("run-c", approach="Compared with sealed-task", suspicion=3, answer_material_accessed=True)
    assert ctx.database.upsert_run_facets([named], registry=ctx.retriever.registry)["stored"] == 1
    path = ctx.state_dir / "run-atlas/facets-v1.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in (facet("run-a"), facet("run-b"), named)))
    shown = ctx.database.search_runs({}, registry=ctx.retriever.registry, sealed_task_ids=ctx.retriever._sealed_ids)
    assert "run-c" not in {row["run_id"] for row in shown}  # `runs` withholds it
    payload = recount(ctx)
    assert payload["db"]["admitted_cloud_runs"] == len(shown) == payload["db_facets"]
    assert payload["jsonl"]["admitted_cloud_runs"] == payload["jsonl_unique_runs"] == 2  # run-a and run-b
    assert payload["jsonl_only_runs"] == 1  # run-b alone; the withheld run-c is not counted as missing
    assert payload["db"]["suspicion"].get(3) is None and payload["db"]["answer_material_accessed"] == 0


def test_recount_the_newest_ledger_record_decides_visibility_before_any_import(ctx: QaContext) -> None:
    path = ctx.state_dir / "run-atlas/facets-v1.jsonl"
    path.parent.mkdir(parents=True)
    records = [facet("run-b", suspicion=1), facet("run-b", suspicion=3, approach="Compared with sealed-task")]
    path.write_text("".join(json.dumps(row) + "\n" for row in records))
    payload = recount(ctx)
    assert payload["jsonl"]["admitted_cloud_runs"] == 0 and payload["jsonl_unique_runs"] == 0


def test_recount_screens_what_an_import_would_store_not_ledger_only_text(ctx: QaContext) -> None:
    # `source` is replaced by the task's dataset on import and unknown fields are never stored.
    records = [facet("run-b", source="sealed-task", note="sealed-task", suspicion=2)]
    path = ctx.state_dir / "run-atlas/facets-v1.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in records))
    before = recount(ctx)
    assert before["jsonl"]["admitted_cloud_runs"] == 1
    summary = import_run_facets(ctx.database, path, registry=ctx.retriever.registry)
    assert summary["stored"] == 1 and summary["refused"] == 0
    after = recount(ctx)
    assert after["db"]["suspicion"][2] == 1 and after["jsonl"]["admitted_cloud_runs"] == 1
