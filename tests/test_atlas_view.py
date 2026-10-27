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
        "cluster_id": cluster_id,
        "name": f"cluster {cluster_id}",
        "level": level,
        "parent": parent,
        "run_ids": runs,
        "centroid_text": "reads tests then edits",
        "size": len(runs),
        "label_rates": [],
    } | fields


def clusters_document(*items: dict[str, Any], scored_at: str | None = None) -> dict[str, Any]:
    return {
        "schema": "run-clusters/v1",
        "facet_version": "run-facets/v1",
        "generated_at": "2026-10-27T20:00:00Z",
        "scored_at": scored_at,
        "clusters": list(items),
    }


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
    (
        "rates before scoring",
        lambda doc: doc["clusters"][0].update(
            label_rates=[{"label": "cheat", "hits_n": 1, "of_n": 2}]
        ),
    ),
    ("level 4", lambda doc: doc["clusters"][1].update(level=4)),
]


@pytest.mark.parametrize(
    ("name", "mutate"), CLUSTER_REJECTIONS, ids=[name for name, _ in CLUSTER_REJECTIONS]
)
def test_cluster_tree_must_be_a_consistent_hierarchy(
    name: str, mutate: Callable[[dict[str, Any]], None]
) -> None:
    def document() -> dict[str, Any]:
        return clusters_document(
            cluster("root", 1, None, ["a", "b", "c"]), cluster("child", 2, "root", ["a", "b"])
        )

    RunClusters.model_validate(document())
    broken = document()
    mutate(broken)
    with pytest.raises(ValidationError):
        RunClusters.model_validate(broken)


def test_label_rates_cannot_exceed_their_cluster_or_their_hits() -> None:
    def scored(rates: list[dict[str, Any]]) -> dict[str, Any]:
        return clusters_document(
            cluster("root", 1, None, ["a", "b"], label_rates=rates),
            scored_at="2026-10-27T21:00:00Z",
        )

    RunClusters.model_validate(scored([{"label": "cheat", "hits_n": 1, "of_n": 2}]))
    for rates in (
        [{"label": "cheat", "hits_n": 3, "of_n": 2}],
        [{"label": "cheat", "hits_n": 1, "of_n": 3}],
        [{"label": "cheat", "hits_n": 1, "of_n": 2}, {"label": "CHEAT", "hits_n": 0, "of_n": 2}],
    ):
        with pytest.raises(ValidationError):
            RunClusters.model_validate(scored(rates))


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
    (tmp_path / CLUSTERS_FILE).write_text(
        json.dumps(clusters_document(cluster("root", 1, None, ["a"], size=2))), encoding="utf-8"
    )
    store = load_atlas_store(tmp_path)
    assert list(store.facets) == ["run-a"]
    assert store.clusters is None and "size" in (store.cluster_error or "")


def _store(tmp_path: Path) -> Any:
    facets = [
        facet("run-a", suspicion=3, answer_material_accessed=True, source="s1", evidence_steps=[0]),
        facet("run-b", suspicion=1, source="s2", tests="modified", evidence_steps=[1]),
        facet("run-c", suspicion=2, source="s1", evidence_steps=[2]),
        facet("run-far", evidence_steps=[40]),
        facet("run-ghost"),
    ]
    (tmp_path / FACETS_FILE).write_text(
        "".join(json.dumps(row) + "\n" for row in facets), encoding="utf-8"
    )
    document = clusters_document(
        cluster(
            "root",
            1,
            None,
            ["run-a", "run-b", "run-c", "run-ghost"],
            label_rates=[{"label": "cheat", "hits_n": 2, "of_n": 3}],
        ),
        cluster("leaf", 2, "root", ["run-a", "run-c"]),
        scored_at="2026-10-27T21:00:00Z",
    )
    (tmp_path / CLUSTERS_FILE).write_text(json.dumps(document), encoding="utf-8")
    return load_atlas_store(tmp_path)


TRACES = {
    f"run-{name}": {
        "task_id": f"task-{name}",
        "dataset_id": "ds",
        "trust_tier": "tier2_consensus_labeled",
        "step_count": 5,
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
    assert listing["options"]["answer_material_accessed"] == 1
    tree = {item["cluster_id"]: item for item in listing["clusters"]}
    # `run-ghost` is in the recorded cluster but not admitted, so the cluster says so.
    assert (tree["root"]["size"], tree["root"]["admitted_n"], tree["root"]["matching_n"]) == (
        4,
        3,
        3,
    )
    assert tree["root"]["label_rates"] == [{"label": "cheat", "hits_n": 2, "of_n": 3}]
    assert listing["runs"][0]["cluster_ids"] == ["root", "leaf"]


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
    assert ids(cluster_id="leaf") == ["run-a", "run-c"]
    assert ids(q="TASK-B") == ["run-b"]
    assert ids(cluster_id="leaf", min_suspicion=3) == ["run-a"]
    narrowed = atlas_listing(store, TRACES, AtlasFilters(min_suspicion=3))
    assert {item["cluster_id"]: item["matching_n"] for item in narrowed["clusters"]} == {
        "root": 1,
        "leaf": 1,
    }
    page = atlas_listing(store, TRACES, AtlasFilters(), limit=1, offset=1)
    assert (page["total"], [run["run_id"] for run in page["runs"]]) == (3, ["run-c"])
    with pytest.raises(ValueError, match="unknown cluster"):
        atlas_listing(store, TRACES, AtlasFilters(cluster_id="missing"))
    with pytest.raises(ValueError, match="limit"):
        atlas_listing(store, TRACES, AtlasFilters(), limit=0)


def test_cluster_paths_run_root_to_leaf(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert [[item["cluster_id"] for item in chain] for chain in cluster_paths(store, "run-a")] == [
        ["root", "leaf"]
    ]
    assert [[item["cluster_id"] for item in chain] for chain in cluster_paths(store, "run-b")] == [
        ["root"]
    ]
    assert cluster_paths(store, "run-far") == []
