"""Run Atlas facets and clusters are validated strictly before anything is shown."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from evallab.production.atlas_view import (
    CLUSTERS_FILE,
    FACETS_FILE,
    AtlasFilters,
    RunCluster,
    RunClusters,
    RunFacets,
    atlas_listing,
    cluster_paths,
    load_atlas_store,
)


def facet(run_id: str = "run-a", **fields: Any) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "source": "synthetic-source",
        "split": "DEV",
        "facet_version": "run-facets/v1",
        "facet_model": "provider/facet-model",
        "input_tokens": 1200,
        "output_tokens": 300,
        "goal": "Fix the parser so the sample inputs pass.",
        "approach": "Read the code, edited the parser, ran the tests.",
        "outcome": "pass",
        "tests": "read",
        "grader_or_harness_touched": False,
        "answer_material_accessed": False,
        "success_claim_without_evidence": False,
        "eval_awareness": "none",
        "shortcut": "none",
        "suspicion": 0,
        "evidence_steps": [1, 3],
    } | fields


def cluster(
    cluster_id: str, level: int, parent: str | None, runs: list[str], **fields: Any
) -> dict[str, Any]:
    return {
        "cluster_version": "run-clusters/v1",
        "cluster_id": cluster_id,
        "name": f"cluster {cluster_id}",
        "level": level,
        "parent": parent,
        "run_ids": runs,
        "centroid_text": "reads tests then edits",
        "size": len(runs),
        "label_rates": None,
        "build_id": "synthetic-build",
        "facet_count": len(runs),
        "created_at": "2026-10-27T20:00:00Z",
    } | fields


def cluster_build(*items: dict[str, Any]) -> dict[str, Any]:
    count = sum(item["size"] for item in items if item["parent"] is None)
    return {"clusters": [dict(item, facet_count=count) for item in items]}


def write_clusters(directory: Path, document: dict[str, Any]) -> None:
    (directory / CLUSTERS_FILE).write_text(
        "".join(json.dumps(row) + "\n" for row in document["clusters"]), encoding="utf-8"
    )


FACET_REJECTIONS: list[dict[str, Any]] = [
    {"suspicion": 4},
    {"evidence_steps": [0, 1, 2, 3, 4, 5]},
    {"evidence_steps": [2, 2]},
    {"evidence_steps": [-1]},
    {"split": "TEST"},
    {"outcome": "won"},
    {"tests": "ignored"},
    {"eval_awareness": "plans_to_cheat"},
    {"goal": "   "},
    {"facet_model": "no-provider"},
    {"input_tokens": -1},
    {"grader_or_harness_touched": "yes"},
    {"label": "cheat"},
]


@pytest.mark.parametrize("change", FACET_REJECTIONS, ids=lambda change: next(iter(change)))
def test_facets_refuse_values_outside_the_contract(change: dict[str, Any]) -> None:
    RunFacets.model_validate(facet())
    with pytest.raises(ValidationError):
        RunFacets.model_validate(facet(**change))


CLUSTER_REJECTIONS: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
    ("size", lambda doc: doc["clusters"][0].update(size=5)),
    ("duplicate runs", lambda doc: doc["clusters"][0].update(run_ids=["a", "a", "b"], size=3)),
    ("root with parent", lambda doc: doc["clusters"][0].update(parent="x")),
    ("child without parent", lambda doc: doc["clusters"][1].update(parent=None)),
    ("skipped level", lambda doc: doc["clusters"][1].update(level=3)),
    ("child outside parent", lambda doc: doc["clusters"][1].update(run_ids=["z"], size=1)),
    ("duplicate ids", lambda doc: doc["clusters"][1].update(cluster_id="root")),
    ("mixed builds", lambda doc: doc["clusters"][1].update(build_id="different-build")),
    ("incomplete build", lambda doc: doc["clusters"].pop()),
    ("level 4", lambda doc: doc["clusters"][1].update(level=4)),
]


@pytest.mark.parametrize(
    ("name", "mutate"), CLUSTER_REJECTIONS, ids=[name for name, _ in CLUSTER_REJECTIONS]
)
def test_cluster_tree_must_be_a_consistent_hierarchy(
    name: str, mutate: Callable[[dict[str, Any]], None]
) -> None:
    def document() -> dict[str, Any]:
        return cluster_build(
            cluster("root", 1, None, ["a", "b", "c"]),
            cluster("child", 2, "root", ["a", "b", "c"]),
            cluster("leaf", 3, "child", ["a", "b", "c"]),
        )

    RunClusters.model_validate(document())
    broken = document()
    mutate(broken)
    with pytest.raises(ValidationError):
        RunClusters.model_validate(broken)


def test_label_rates_cannot_exceed_members_or_disagree_with_counts() -> None:
    def scored(rates: dict[str, Any]) -> dict[str, Any]:
        return cluster("root", 1, None, ["a", "b"], label_rates=rates)

    RunCluster.model_validate(scored({"labelled": 2, "positives": 1, "rate": 0.5}))
    for rates in (
        {"labelled": 2, "positives": 3, "rate": 1.0},
        {"labelled": 3, "positives": 1, "rate": 1 / 3},
        {"labelled": 2, "positives": 1, "rate": 0.9},
        {"labelled": 2, "positives": 1, "rate": None},
        {"labelled": 0, "positives": 0, "rate": 0.0},
    ):
        with pytest.raises(ValidationError):
            RunCluster.model_validate(scored(rates))


def test_store_keeps_the_latest_record_per_run_and_counts_what_it_refuses(tmp_path: Path) -> None:
    lines = [
        json.dumps(facet("run-a", suspicion=1)),
        json.dumps(facet("run-a", suspicion=3)),
        json.dumps(facet("run-b", suspicion=9)),
        '{"run_id": "run-c", "run_id": "run-d"}',
        "not json",
        '{"input_tokens": NaN}',
        "",
        json.dumps(facet("run-e")),
    ]
    (tmp_path / FACETS_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    store = load_atlas_store(tmp_path)
    assert sorted(store.facets) == ["run-a", "run-e"]
    assert store.facets["run-a"]["suspicion"] == 3
    assert (store.superseded_n, store.rejected_n) == (1, 4)
    assert [note["line"] for note in store.rejected] == [3, 4, 5, 6]
    assert store.clusters is None and set(store.sha256) == {FACETS_FILE}


def test_invalid_cluster_file_is_reported_without_hiding_the_facets(tmp_path: Path) -> None:
    (tmp_path / FACETS_FILE).write_text(json.dumps(facet("run-a")) + "\n", encoding="utf-8")
    write_clusters(tmp_path, cluster_build(cluster("root", 1, None, ["a"], size=2)))
    store = load_atlas_store(tmp_path)
    assert list(store.facets) == ["run-a"]
    assert store.clusters is None and "size" in (store.cluster_error or "")


def _store(tmp_path: Path) -> Any:
    facets = [
        facet("run-a", suspicion=3, answer_material_accessed=True, source="s1", evidence_steps=[0]),
        facet("run-b", suspicion=1, source="s2", tests="modified", evidence_steps=[1], facet_model="other/reader"),
        facet("run-c", suspicion=2, source="s1", evidence_steps=[2]),
        facet("run-far", evidence_steps=[40]),
        facet("run-ghost"),
    ]
    (tmp_path / FACETS_FILE).write_text(
        "".join(json.dumps(row) + "\n" for row in facets), encoding="utf-8"
    )
    document = cluster_build(
        cluster(
            "root",
            1,
            None,
            ["run-a", "run-b", "run-c", "run-ghost"],
            label_rates={"labelled": 3, "positives": 2, "rate": 2 / 3},
        ),
        cluster("middle", 2, "root", ["run-a", "run-b", "run-c", "run-ghost"]),
        cluster("leaf", 3, "middle", ["run-a", "run-c"]),
        cluster("other", 3, "middle", ["run-b", "run-ghost"]),
    )
    write_clusters(tmp_path, document)
    return load_atlas_store(tmp_path)


TRACES = {
    f"run-{name}": {
        "task_id": f"task-{name}",
        "dataset_id": "s2" if name == "b" else "s1",
        "trust_tier": "tier2_consensus_labeled",
        "step_count": 5,
        "step_indices": frozenset(range(5)),
        "outcome": "pass",
    }
    for name in ("a", "b", "c", "far")
}


def test_listing_shows_only_admitted_runs_with_real_evidence_steps(tmp_path: Path) -> None:
    listing = atlas_listing(_store(tmp_path), TRACES, AtlasFilters())
    assert [run["run_id"] for run in listing["runs"]] == [
        "run-a",
        "run-c",
        "run-b",
    ]  # suspicion, then id
    store = listing["store"]
    assert (
        store["facets_n"],
        store["visible_n"],
        store["unadmitted_n"],
        store["evidence_outside_run_n"],
    ) == (5, 3, 1, 1)
    assert listing["options"]["source"] == {"s1": 2, "s2": 1}
    assert listing["options"]["facet_model"] == {"other/reader": 1, "provider/facet-model": 2}
    assert listing["options"]["answer_material_accessed"] == 1
    tree = {item["cluster_id"]: item for item in listing["clusters"]}
    # `run-ghost` is in the recorded cluster but not admitted, so the cluster says so.
    assert (tree["root"]["size"], tree["root"]["admitted_n"], tree["root"]["matching_n"]) == (
        4,
        3,
        3,
    )
    assert tree["root"]["label_rates"] == [
        {"label": "positive source label", "hits_n": 2, "of_n": 3}
    ]
    assert listing["scored_at"] is None
    assert listing["runs"][0]["cluster_ids"] == ["root", "middle", "leaf"]


def test_filters_narrow_runs_and_cluster_counts_together(tmp_path: Path) -> None:
    store = _store(tmp_path)

    def ids(**fields: Any) -> list[str]:
        return [
            run["run_id"] for run in atlas_listing(store, TRACES, AtlasFilters(**fields))["runs"]
        ]

    assert ids(min_suspicion=2) == ["run-a", "run-c"]
    assert ids(source="s2") == ["run-b"]
    assert ids(answer_material_accessed=True) == ["run-a"]
    assert ids(answer_material_accessed=False) == ["run-c", "run-b"]
    assert ids(tests="modified") == ["run-b"]
    assert ids(facet_model="other/reader") == ["run-b"]
    assert ids(facet_model="provider/facet-model", min_suspicion=2) == ["run-a", "run-c"]
    assert ids(facet_model="nobody/read-this") == []
    assert ids(cluster_id="leaf") == ["run-a", "run-c"]
    assert ids(q="TASK-B") == ["run-b"]
    assert ids(cluster_id="leaf", min_suspicion=3) == ["run-a"]
    narrowed = atlas_listing(store, TRACES, AtlasFilters(min_suspicion=3))
    assert {item["cluster_id"]: item["matching_n"] for item in narrowed["clusters"]} == {
        "root": 1,
        "middle": 1,
        "leaf": 1,
        "other": 0,
    }
    page = atlas_listing(store, TRACES, AtlasFilters(), limit=1, offset=1)
    assert (page["total"], [run["run_id"] for run in page["runs"]]) == (3, ["run-c"])
    with pytest.raises(ValueError, match="unknown cluster"):
        atlas_listing(store, TRACES, AtlasFilters(cluster_id="missing"))
    with pytest.raises(ValueError, match="limit"):
        atlas_listing(store, TRACES, AtlasFilters(), limit=0)


def test_detector_alarms_filter_order_and_never_turn_missing_results_into_a_pass(tmp_path: Path) -> None:
    store = _store(tmp_path)
    alarms = {"run-b": ["scout", "metr", "run_health.loop"], "run-c": ["scout"], "run-ghost": ["scout"]}

    def listing(**fields: Any) -> dict[str, Any]:
        return atlas_listing(
            store, TRACES, AtlasFilters(**fields), alarms=alarms, read={"run-b": 3, "run-c": 1}, alarms_available=True
        )

    default = listing()
    assert [run["run_id"] for run in default["runs"]] == ["run-a", "run-c", "run-b"]  # suspicion order is unchanged
    assert {run["run_id"]: run["alarms"] for run in default["runs"]} == {
        "run-a": [], "run-c": ["scout"], "run-b": ["metr", "run_health.loop", "scout"],
    }
    assert default["options"]["alarmed_by"] == {"metr": 1, "run_health.loop": 1, "scout": 2}  # admitted runs only
    assert [run["run_id"] for run in listing(alarmed_by="scout")["runs"]] == ["run-c", "run-b"]
    assert [run["run_id"] for run in listing(alarmed_by="metr", min_suspicion=2)["runs"]] == []
    assert [run["run_id"] for run in listing(sort="alarms")["runs"]] == ["run-b", "run-c", "run-a"]
    narrowed = listing(alarmed_by="metr")
    assert narrowed["total"] == 1 and {c["cluster_id"]: c["matching_n"] for c in narrowed["clusters"]}["root"] == 1
    silent = atlas_listing(store, TRACES, AtlasFilters())
    assert silent["alarms_available"] is False and silent["options"]["alarmed_by"] == {}
    assert all(run["alarms"] == [] for run in silent["runs"])
    assert {run["run_id"]: run["detector_results_n"] for run in default["runs"]} == {"run-a": 0, "run-c": 1, "run-b": 3}


def test_cluster_paths_run_root_to_leaf(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert [[item["cluster_id"] for item in chain] for chain in cluster_paths(store, "run-a")] == [
        ["root", "middle", "leaf"]
    ]
    assert [[item["cluster_id"] for item in chain] for chain in cluster_paths(store, "run-b")] == [
        ["root", "middle", "other"]
    ]
    assert cluster_paths(store, "run-far") == []


def test_sparse_evidence_uses_actual_step_indices_and_database_metadata(tmp_path: Path) -> None:
    rows = [
        facet("real", source="forged-source", outcome="pass", evidence_steps=[9]),
        facet("gap", evidence_steps=[1]),
    ]
    (tmp_path / FACETS_FILE).write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    identities = {
        run_id: {
            "task_id": "admitted-task",
            "dataset_id": "database-source",
            "trust_tier": "tier2_consensus_labeled",
            "step_count": 3,
            "step_indices": frozenset({0, 4, 9}),
            "outcome": "fail",
        }
        for run_id in ("real", "gap")
    }
    listing = atlas_listing(load_atlas_store(tmp_path), identities, AtlasFilters())
    assert [(row["run_id"], row["source"], row["outcome"]) for row in listing["runs"]] == [
        ("real", "database-source", "fail")
    ]
    assert listing["store"]["evidence_outside_run_n"] == 1
    assert listing["options"]["source"] == {"database-source": 1}


def test_store_refuses_symlinked_directory_and_fifo(tmp_path: Path) -> None:
    import os

    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "run-atlas"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        load_atlas_store(link)
    os.mkfifo(outside / FACETS_FILE)
    with pytest.raises(ValueError, match="regular file"):
        load_atlas_store(outside)


def test_cluster_stream_selects_final_build_and_rejects_partial_tail(tmp_path: Path) -> None:
    old = cluster_build(
        cluster("old", 1, None, ["a"]),
        cluster("old-middle", 2, "old", ["a"]),
        cluster("old-leaf", 3, "old-middle", ["a"]),
    )
    latest = cluster_build(
        cluster("new", 1, None, ["b"]),
        cluster("new-middle", 2, "new", ["b"]),
        cluster("new-leaf", 3, "new-middle", ["b"]),
    )
    for index, row in enumerate(latest["clusters"]):
        row.update(
            build_id="earlier-alphabetically",
            created_at=f"2026-10-26T20:00:00.{index:06d}Z",
        )
    write_clusters(tmp_path, {"clusters": old["clusters"] + latest["clusters"]})
    store = load_atlas_store(tmp_path)
    assert store.cluster_error is None
    assert store.clusters is not None
    assert [node["cluster_id"] for node in store.clusters["clusters"]] == [
        "new",
        "new-middle",
        "new-leaf",
    ]
    incomplete = dict(latest["clusters"][0], build_id="partial")
    with (tmp_path / CLUSTERS_FILE).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(incomplete) + "\n")
    refused = load_atlas_store(tmp_path)
    assert refused.clusters is None
    assert "incomplete cluster build" in (refused.cluster_error or "")
