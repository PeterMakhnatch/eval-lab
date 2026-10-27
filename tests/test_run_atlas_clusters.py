"""Offline Run Atlas clustering, naming, persistence and scoring contracts."""

from __future__ import annotations

import argparse
import builtins
import copy
import importlib
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evallab.production import run_atlas
from evallab.production import run_atlas_clusters as atlas


def _facets(count: int = 80) -> list[dict[str, Any]]:
    topics = (
        "repair parser syntax token grammar", "implement database transaction query index",
        "alter hidden tests grader assertions", "retrieve reference answer solution archive",
    )
    return [{
        "run_id": f"run-{index:04d}", "source": "private-corpus", "split": "DEV",
        "approach": topics[index % len(topics)] + f" operation{index % 11}",
        "shortcut": "none" if index % 2 == 0 else "read answer material",
        "suspicion": index % 4, "outcome": "pass", "label": "do not transmit",
    } for index in range(count)]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evallab atlas")
    atlas.add_clusters_parser(parser.add_subparsers(required=True))
    return parser


def _cluster(run_ids: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "cluster_id": "L1-01", "level": 1, "parent": None, "run_ids": run_ids,
        "size": len(run_ids), "centroid_text": "repair parser | none",
        "samples": ["repair parser | none"], "label_rates": None,
        "rule_recall": None, "rule_fpr": None,
        **extra,
    }


def _empty_registry(root: Path) -> Path:
    inventory = root / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(json.dumps({
        "dataset_id": "mimo-defect-map-v1",
        "heldout_task_ids_excluded_from_training_all_versions": [], "tasks": [],
    }))
    keys = root / "research/answer-keys"
    for dataset in ("tb4-audit-v1", "cheatbench-actions-v1", "hacktrace-act-v1"):
        directory = keys / dataset
        directory.mkdir(parents=True)
        (directory / "items.jsonl").touch()
    (keys / "tb4-audit-v1/tasks.jsonl").touch()
    return keys


def test_hashing_embedding_import_never_requires_lancedb(monkeypatch: pytest.MonkeyPatch) -> None:
    original = builtins.__import__

    def without_lance(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "lancedb" or name.startswith("lancedb."):
            raise AssertionError("HashingEmbedder must not import optional LanceDB")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_lance)
    from evallab import lance

    importlib.reload(lance)
    vectors = lance.HashingEmbedder().embed(["repair parser | none"])
    assert len(vectors) == 1 and len(vectors[0]) == 256
    assert vectors == lance.HashingEmbedder().embed(["repair parser | none"])


def test_hierarchy_determinism_membership_parents_and_centroids() -> None:
    facets = _facets()
    clusters = atlas.cluster_facets(facets, seed=42, min_size=2)
    assert clusters == atlas.cluster_facets(reversed(facets), seed=42, min_size=2)
    assert 1 < sum(cluster["level"] == 1 for cluster in clusters) <= 12
    by_id = {cluster["cluster_id"]: cluster for cluster in clusters}
    texts = {facet["run_id"]: atlas.facet_text(facet) for facet in facets}
    for level in (1, 2, 3):
        assert Counter(run_id for cluster in clusters if cluster["level"] == level
                       for run_id in cluster["run_ids"]) == Counter(texts.keys())
    for cluster in clusters:
        assert cluster["size"] == len(cluster["run_ids"])
        assert cluster["centroid_text"] in [texts[run_id] for run_id in cluster["run_ids"]]
        assert cluster["cluster_version"] == "run-clusters/v1"
        assert cluster["label_rates"] is None
        assert cluster["rule_recall"] is None and cluster["rule_fpr"] is None
        if cluster["parent"] is None:
            assert cluster["level"] == 1
        else:
            parent = by_id[cluster["parent"]]
            assert parent["level"] + 1 == cluster["level"]
            assert set(cluster["run_ids"]) <= set(parent["run_ids"])
        children = [child for child in clusters if child["parent"] == cluster["cluster_id"]]
        assert bool(children) == (cluster["level"] < 3)
        if children:
            assert Counter(run_id for child in children for run_id in child["run_ids"]) == Counter(
                cluster["run_ids"],
            )


def test_tiny_and_indistinguishable_groups_continue_without_splitting() -> None:
    for facets in (_facets(1), [dict(facet, approach="same", shortcut="none")
                               for facet in _facets(30)]):
        clusters = atlas.cluster_facets(facets, min_size=5)
        assert [cluster["level"] for cluster in clusters] == [1, 2, 3]
        assert [cluster["size"] for cluster in clusters] == [len(facets)] * 3
        assert clusters[1]["parent"] == clusters[0]["cluster_id"]
        assert clusters[2]["parent"] == clusters[1]["cluster_id"]
        assert all(len(cluster["samples"]) >= min(3, len(facets)) for cluster in clusters)
    assert atlas.cluster_facets([]) == []


def test_embeddings_use_only_exact_approach_shortcut_text() -> None:
    inputs = []

    class Embedder:
        def embed(self, texts: list[str]) -> list[list[float]]:
            inputs.extend(texts)
            return [[1.0, 0.0] for _ in texts]

    facets = _facets(3)
    atlas.cluster_facets(facets, embedder=Embedder())
    assert inputs == [facet["approach"] + " | " + facet["shortcut"] for facet in facets]
    modified = [dict(facet, suspicion=999, outcome="fail", label="opposite") for facet in facets]
    assert atlas.cluster_facets(facets) == atlas.cluster_facets(modified)


def test_member_samples_start_with_three_centroid_nearest_texts() -> None:
    facets = _facets(3)
    embedder = SimpleNamespace(embed=lambda _: [[1.0, 0.0], [0.8, 0.6], [0.0, 1.0]])
    clusters = atlas.cluster_facets(facets, embedder=embedder)
    expected = [atlas.facet_text(facets[index]) for index in (1, 0, 2)]
    assert all(cluster["samples"] == expected for cluster in clusters)


@pytest.mark.parametrize("facets,kwargs,match", [
    ([_facets(1)[0]] * 2, {}, "duplicate"),
    ([dict(_facets(1)[0], split="TEST")], {}, "DEV"),
    (_facets(1), {"min_size": 0}, "positive"),
])
def test_invalid_inputs_fail_closed(facets: list[dict[str, Any]], kwargs: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        atlas.cluster_facets(facets, **kwargs)


@pytest.mark.parametrize("vectors", [[], [[1.0], [0.0]], [[float("nan")]], [[]]])
def test_invalid_embedding_output_is_rejected(vectors: list[list[float]]) -> None:
    with pytest.raises(ValueError, match="vectors"):
        atlas.cluster_facets(_facets(1), embedder=SimpleNamespace(embed=lambda _: vectors))


def test_default_naming_is_deterministic_distinctive_and_short() -> None:
    clusters = [
        _cluster(["a"], samples=["shared parser parser grammar | none"]),
        _cluster(["b"], cluster_id="L1-02", samples=["shared database database query | none"]),
    ]
    result = atlas.name_clusters(clusters)
    assert result == atlas.name_clusters(copy.deepcopy(clusters))
    assert result[0]["name"].split()[0] == "parser"
    assert result[1]["name"].split()[0] == "database"
    assert all(1 <= len(cluster["name"].split()) <= 6 for cluster in result)
    assert all(cluster["name_source"] == "deterministic" for cluster in result)


@pytest.mark.parametrize("reply", ["", "   ", "one two three four five six seven", "two\nlines", None])
def test_invalid_namer_reply_falls_back(reply: Any) -> None:
    cluster = _cluster(["a"])
    atlas.name_clusters([cluster], namer=lambda _: reply)
    assert cluster["name_source"] == "deterministic_fallback"
    assert cluster["name"] == "parser repair"


def test_namer_failure_and_valid_reply() -> None:
    def failing(_: list[str]) -> str:
        raise RuntimeError("offline")

    failed = atlas.name_clusters([_cluster(["a"])], namer=failing)[0]
    assert failed["name_source"] == "deterministic_fallback"
    valid = atlas.name_clusters([_cluster(["a"])], namer=lambda _: "  Parser repairs  ")[0]
    assert valid["name"] == "Parser repairs" and valid["name_source"] == "namer"


def test_gateway_naming_prompt_is_blind_and_subscription_only() -> None:
    captured = []

    def complete(route: str, messages: list[dict[str, str]], *, max_tokens: int) -> Any:
        captured.append((route, messages, max_tokens))
        return SimpleNamespace(text="Reference lookup")

    facet = dict(_facets(1)[0], run_id="secret-run-id", source="Goodfire",
                 approach="Goodfire secret-run-id reads reference", shortcut="none")
    clusters = atlas.cluster_facets([facet])
    atlas.name_clusters(clusters, namer=atlas.model_namer(
        "openai-codex/test", SimpleNamespace(complete=complete),
    ))
    assert len(captured) == 3
    for route, messages, max_tokens in captured:
        payload = json.loads(messages[1]["content"])
        assert isinstance(payload, list) and all(isinstance(text, str) for text in payload)
        serialized = json.dumps(messages).lower()
        for forbidden in ("goodfire", "secret-run-id", "private-corpus", "do not transmit",
                          "suspicion", '"outcome"', '"label"'):
            assert forbidden not in serialized
        assert route == "openai-codex/test" and max_tokens == 128
    with pytest.raises(ValueError, match="subscription"):
        atlas.model_namer("openai/paid", SimpleNamespace(complete=complete))


def test_label_rate_math_separates_positive_recall_from_flagged_negatives() -> None:
    cluster = _cluster(["a", "b", "c", "d", "missing"])
    labels = {"a": True, "b": False, "c": None, "d": True}
    atlas.cluster_label_rates([cluster], labels,
                              rule_coverage={"a": True, "b": True, "c": True, "d": False})
    assert cluster["label_rates"] == {"labelled": 3, "positives": 2, "rate": 2 / 3}
    assert cluster["rule_recall"] == 1 / 2 and cluster["rule_labelled"] == 2
    assert cluster["rule_fpr"] == 1
    atlas.cluster_label_rates([cluster], labels, rule_coverage={"b": True, "d": False})
    assert cluster["rule_recall"] is None and cluster["rule_labelled"] == 1
    assert cluster["rule_fpr"] == 1
    assert atlas.uncovered_clusters([cluster], min_size=1, min_labelled=1) == []
    atlas.cluster_label_rates([cluster], labels, rule_coverage={"a": False, "d": False})
    assert cluster["rule_recall"] == 0 and cluster["rule_labelled"] == 2
    assert cluster["rule_fpr"] is None
    atlas.cluster_label_rates([cluster], {"b": False}, rule_coverage={"b": True})
    assert cluster["rule_recall"] is None and cluster["rule_labelled"] == 0
    assert cluster["rule_fpr"] == 1
    atlas.cluster_label_rates([cluster], {}, rule_coverage={})
    assert cluster["label_rates"] == {"labelled": 0, "positives": 0, "rate": None}
    assert cluster["rule_recall"] is None and cluster["rule_fpr"] is None


def test_rule_fpr_uses_only_negatives_with_known_coverage() -> None:
    cluster = _cluster(["p", "n1", "n2", "unknown-negative"])
    atlas.cluster_label_rates(
        [cluster], {"p": True, "n1": False, "n2": False, "unknown-negative": False},
        rule_coverage={"p": False, "n1": True, "n2": False},
    )
    assert cluster["rule_recall"] == 0 and cluster["rule_labelled"] == 1
    assert cluster["rule_fpr"] == 1 / 2


def test_uncovered_selection_thresholds_and_positive_sort() -> None:
    clusters = [
        _cluster([f"p{i}" for i in range(10)], cluster_id="L1-02"),
        _cluster([f"p{i}" for i in range(5)], cluster_id="L1-01"),
        _cluster(["p1"], cluster_id="tiny"),
        _cluster([f"n{i}" for i in range(6)], cluster_id="clean"),
        _cluster([f"h{i}" for i in range(6)], cluster_id="covered"),
        _cluster([f"u{i}" for i in range(6)], cluster_id="unknown"),
        _cluster(["p1"] + [f"missing{i}" for i in range(9)], cluster_id="sparse"),
    ]
    labels = {**dict.fromkeys([f"p{i}" for i in range(10)], True),
              **dict.fromkeys([f"n{i}" for i in range(6)], False),
              **dict.fromkeys([f"h{i}" for i in range(6)], True),
              **dict.fromkeys([f"u{i}" for i in range(6)], True)}
    coverage = {run_id: run_id.startswith("h") for run_id in labels if not run_id.startswith("u")}
    coverage["p0"] = True
    atlas.cluster_label_rates(clusters, labels, rule_coverage=coverage)
    assert [cluster["cluster_id"] for cluster in atlas.uncovered_clusters(clusters)] == [
        "L1-02", "L1-01",
    ]
    assert [cluster["cluster_id"] for cluster in atlas.uncovered_clusters(clusters, min_size=10)] == [
        "L1-02",
    ]
    assert [cluster["cluster_id"] for cluster in atlas.uncovered_clusters(
        clusters, min_labelled=6,
    )] == ["L1-02"]
    assert [cluster["cluster_id"] for cluster in atlas.uncovered_clusters(
        clusters, min_labelled=1,
    )] == ["L1-02", "L1-01", "sparse"]
    assert [cluster["cluster_id"] for cluster in atlas.uncovered_clusters(
        clusters, min_size=1, min_labelled=1,
    )] == ["L1-02", "L1-01", "sparse", "tiny"]
    assert atlas.uncovered_clusters(clusters, max_rule_recall=0) == []


def test_frozen_rule_coverage_prefers_stage1_and_omits_excluded(tmp_path: Path) -> None:
    (tmp_path / "e2escore-goodfire-pf_r.json").write_text(json.dumps({"traces": [
        {"trace_id": "a", "stage1_any": False, "alarmed": True},
        {"trace_id": "b", "alarmed": True},
        {"trace_id": "c", "stage1_any": True, "excluded": True},
        {"trace_id": "d"},
    ]}))
    assert atlas.load_rule_coverage(tmp_path) == {"a": False, "b": True}


def test_current_rules_replay_real_atif_without_judge() -> None:
    def run(run_id: str, command: str) -> run_atlas.AtlasRun:
        return run_atlas.AtlasRun(run_id, "private", "DEV", "unknown", {"steps": [{
            "step_id": 7, "source": "assistant", "message": "Inspect files.",
            "tool_calls": [{"function_name": "bash", "arguments": {"command": command}}],
        }]})

    clean, flagged = run("clean", "pwd"), run("flagged", "cat /tests/test_outputs.py")
    assert atlas.rule_hits([clean, flagged]) == {"clean": False, "flagged": True}
    assert flagged.doc["steps"][0]["source"] == "assistant"
    with pytest.raises(ValueError, match="DEV"):
        atlas.rule_hits([run_atlas.AtlasRun("test", "private", "TEST", "unknown", {})])


def test_parser_registration_has_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_: Any, **__: Any) -> Any:
        raise AssertionError("registration must not open files, databases or a gateway")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(run_atlas, "connect_readonly", forbidden)
    monkeypatch.setattr(run_atlas, "GatewayTransport", forbidden)
    parser = _parser()
    assert parser.parse_args(["clusters", "build"]).embedder == "hashing"
    assert not parser.parse_args(["clusters", "show"]).with_labels


@pytest.mark.parametrize("cap", [None, "0", "-1", "nan", "inf"])
def test_remote_embedding_refused_before_reading_facets(
    cap: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run_atlas, "load_facets", lambda _: pytest.fail("input was read"))
    arguments = ["clusters", "build", "--embedder", "remote"]
    if cap is not None:
        arguments += ["--spend-cap-usd", cap]
    args = _parser().parse_args(arguments)
    with pytest.raises(ValueError, match="spend-cap-usd"):
        args.func(args, tmp_path)


def test_remote_embedding_only_wires_existing_backend_without_a_call() -> None:
    from evallab.lance import OpenAICompatibleEmbedder

    args = _parser().parse_args([
        "clusters", "build", "--embedder", "remote", "--spend-cap-usd", "1",
        "--embedding-endpoint", "https://remote.invalid", "--embedding-model", "embedding",
        "--embedding-model-revision", "sha256:revision", "--embedding-tokenizer", "tokens",
        "--embedding-tokenizer-revision", "sha256:tokens", "--embedding-dim", "256",
    ])
    embedder = atlas._embedder(args)
    assert isinstance(embedder, OpenAICompatibleEmbedder)
    assert embedder.endpoint == "https://remote.invalid" and embedder.dim == 256


def test_build_appends_snapshots_and_show_uses_latest_with_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    facets_path, clusters_path = tmp_path / "facets.jsonl", tmp_path / "clusters.jsonl"
    facets = _facets(12)
    run_atlas.append_jsonl(facets_path, facets)
    admitted = []
    monkeypatch.setattr(atlas, "_admit_facets", lambda facets, root, db: admitted.append(len(facets)))
    args = _parser().parse_args([
        "clusters", "build", "--facets", str(facets_path), "--clusters", str(clusters_path),
        "--min-size", "2",
    ])
    assert args.func(args, tmp_path) == 0
    first = atlas.load_clusters(clusters_path)
    assert first and all(row["facet_count"] == 12 for row in first)
    assert all(row["label_rates"] is None for row in first)
    run_atlas.append_jsonl(facets_path, _facets(13)[-1:])
    assert args.func(args, tmp_path) == 0
    latest = atlas.load_clusters(clusters_path)
    assert admitted == [12, 13]
    assert first[0]["build_id"] != latest[0]["build_id"]
    assert all(row["facet_count"] == 13 for row in latest)
    assert len(clusters_path.read_text().splitlines()) == len(first) + len(latest)
    monkeypatch.setattr(atlas, "_load_labels", lambda db, root, ids: dict.fromkeys(ids, True))
    monkeypatch.setattr(atlas, "load_rule_coverage", lambda _: dict.fromkeys(
        [facet["run_id"] for facet in _facets(13)], False,
    ))
    show = _parser().parse_args(["clusters", "show", "--clusters", str(clusters_path), "--with-labels"])
    assert show.func(show, tmp_path) == 0
    output = capsys.readouterr().out
    assert f"Build {latest[0]['build_id']} — 13 facets" in output
    assert "  L1-01.1" in output and "    L1-01.1.1" in output
    assert "rule_recall=0.0%" in output and "rule_fpr=unknown" in output
    assert "Uncovered clusters" in output
    assert atlas.load_clusters(clusters_path)[0]["label_rates"] is None


def test_show_without_labels_never_reads_labels_or_rule_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "clusters.jsonl"
    cluster = _cluster(["a"], cluster_version=atlas.CLUSTER_VERSION,
                       build_id="example", facet_count=1, name="Parser repair")
    run_atlas.append_jsonl(path, [cluster])
    monkeypatch.setattr(atlas, "_load_labels", lambda *args: pytest.fail("labels opened"))
    monkeypatch.setattr(atlas, "load_rule_coverage", lambda *args: pytest.fail("coverage opened"))
    args = _parser().parse_args(["clusters", "show", "--clusters", str(path)])
    assert args.func(args, tmp_path) == 0
    assert "L1-01 n=1 Parser repair" in capsys.readouterr().out


def test_show_uncovered_counts_and_three_nearest_texts_despite_flagged_negatives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    samples = [
        "nearest parser repair | none", "next token repair | none",
        "third grammar repair | none", "farther context | none",
    ]
    clusters = [
        _cluster(["p1", "p2", "p3", "n1", "n2"], name="Parser repairs", samples=samples),
        _cluster(["s1"] + [f"u{i}" for i in range(9)], cluster_id="L1-02",
                 name="Sparse evidence"),
    ]
    for cluster in clusters:
        cluster.update(cluster_version=atlas.CLUSTER_VERSION, build_id="one", facet_count=15)
    path = tmp_path / "clusters.jsonl"
    run_atlas.append_jsonl(path, clusters)
    labels = {"p1": True, "p2": True, "p3": True, "n1": False, "n2": False, "s1": True}
    coverage = {run_id: run_id.startswith("n") for run_id in labels}
    monkeypatch.setattr(atlas, "_load_labels", lambda *args: labels)
    monkeypatch.setattr(atlas, "load_rule_coverage", lambda _: coverage)
    args = _parser().parse_args(["clusters", "show", "--clusters", str(path), "--with-labels"])
    assert args.func(args, tmp_path) == 0
    output = capsys.readouterr().out
    assert "rule_recall=0.0%" in output and "rule_fpr=100.0%" in output
    uncovered = output.split("Uncovered clusters", 1)[1]
    assert "labelled>=5" in uncovered and "L1-01 labelled=5 positives=3 Parser repairs" in uncovered
    assert "Sparse evidence" not in uncovered
    assert uncovered.count(" | none") == 3
    assert all(f"    {text}\n" in uncovered for text in samples[:3])
    assert samples[3] not in uncovered


def test_optional_rule_replay_only_requests_unknown_labelled_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "clusters.jsonl"
    run_atlas.append_jsonl(path, [_cluster(["known", "missing", "unknown"],
        cluster_version=atlas.CLUSTER_VERSION, build_id="one", facet_count=3, name="Parser repair")])
    monkeypatch.setattr(atlas, "_load_labels", lambda *args: {
        "known": True, "missing": False, "unknown": None,
    })
    monkeypatch.setattr(atlas, "load_rule_coverage", lambda _: {"known": False})
    requested = []
    monkeypatch.setattr(atlas, "_replay_missing", lambda args, root, ids: (
        requested.append(ids) or {"missing": True}
    ))
    args = _parser().parse_args([
        "clusters", "show", "--clusters", str(path), "--with-labels", "--replay-rules",
    ])
    assert args.func(args, tmp_path) == 0
    assert requested == [{"missing"}]


def test_build_admission_honors_current_cross_key_test_seal(tmp_path: Path) -> None:
    keys = _empty_registry(tmp_path)
    items = [
        {"item_id": f"cheatbench-actions-v1:{name}", "split": "dev",
         "subject": {"task_id": "synthetic/" + name}}
        for name in ("allowed", "sealed")
    ]
    run_atlas.append_jsonl(keys / "cheatbench-actions-v1/items.jsonl", items)
    assignments = keys / "splits/assignments.csv"
    assignments.parent.mkdir()
    assignments.write_text(
        "key_id,item_id,split\n"
        "cheatbench-actions-v1,cheatbench-actions-v1:allowed,dev\n"
        "cheatbench-actions-v1,cheatbench-actions-v1:sealed,dev\n"
        "other-key,cheatbench-actions-v1:sealed,test\n"
    )
    facets = [dict(_facets(1)[0], run_id=item["item_id"]) for item in items]
    atlas._admit_facets(facets[:1], tmp_path, tmp_path / "no-db")
    with pytest.raises(ValueError, match="cross-key DEV"):
        atlas._admit_facets(facets, tmp_path, tmp_path / "no-db")
