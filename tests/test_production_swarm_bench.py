"""Blind orchestration, evaluator accounting, retained DEV regressions, and CLI behavior."""

from __future__ import annotations

import inspect
import json
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from evallab.production import swarm_bench as bench

_ALIASES = ("muse", "kimi", "sonnet", "codex")
_BUDGETED_SELECTORS = (
    bench.select_round_robin_top1,
    bench.select_locus_consensus_v1,
    bench.select_specialist_diversity_v2,
)


def _prediction(
    alias: str,
    index: int,
    *,
    task: str = "task-a",
    path: str = "tests/test_alpha.py",
    line: int = 10,
    end_line: int | None = None,
    direction: str = "wrong_pass",
    text: str = "orbit charcoal amber",
    valid: bool = True,
    quote_valid: bool | None = True,
) -> dict[str, Any]:
    check: dict[str, Any] = {"file_exists": valid, "line_range_exists": valid}
    if quote_valid is not None:
        check["quote_found_in_cited_range"] = quote_valid
    return {
        "prediction_id": f"{alias}:{task}:{index}",
        "task": task,
        "alias": alias,
        "index": index,
        "finding": {
            "class": text.replace(" ", "_"),
            "confidence": "high",
            "direction": direction,
            "mechanism": text,
            "location": {
                "path": path,
                "line": line,
                "end_line": line + 1 if end_line is None else end_line,
                "quote": text,
            },
        },
        "location_check": check,
    }


@pytest.fixture
def synthetic_bundle() -> bench.SwarmBenchBundle:
    """Twelve tasks, 46 DEV keys; complementary workers each miss one sound locus."""
    predictions: list[bench.Record] = []
    keys: list[bench.Record] = []
    cases: list[dict[str, Any]] = []
    matches: list[dict[str, Any]] = []
    for task_index in range(12):
        task = f"task-{task_index:02}"
        item_ids = {locus: f"key:{task}:{locus}" for locus in "abc"}
        keys.extend(
            {"item_id": item_id, "task": task, "split": "dev"} for item_id in item_ids.values()
        )
        cases.append({"task": task, "anchor_id": item_ids["a"]})
        layouts = {
            "muse": ("a", "c", "noise"),
            "kimi": ("a", "noise", "noise"),
            "sonnet": ("b", "c", "noise"),
            "codex": ("b", "noise", "noise"),
        }
        for alias, layout in layouts.items():
            for index, locus in enumerate(layout, 1):
                if locus == "a":
                    prediction = _prediction(alias, index, task=task)
                elif locus == "b":
                    prediction = _prediction(
                        alias,
                        index,
                        task=task,
                        path="task.toml",
                        line=80,
                        direction="unfair_fail",
                        text="trident glacier cobalt",
                    )
                elif locus == "c":
                    prediction = _prediction(
                        alias,
                        index,
                        task=task,
                        path="tests/test_gamma.py",
                        line=160,
                        text="quartz velvet cedar",
                    )
                else:
                    prediction = _prediction(
                        alias,
                        index,
                        task=task,
                        path=f"absent/{alias}/{index}.py",
                        line=1000 * index,
                        text=f"unsupported noise {alias}",
                        valid=False,
                    )
                predictions.append(prediction)
                matches.append(
                    {
                        "prediction_id": prediction["prediction_id"],
                        "matched_key_id": item_ids.get(locus),
                    }
                )
    keys.extend(
        {"item_id": f"key:unfound:{index}", "task": "task-00", "split": "dev"}
        for index in range(10)
    )
    return bench.SwarmBenchBundle(
        manifest={"cases": cases, "models": list(_ALIASES), "source_commit": "synthetic"},
        dev_key=keys,
        predictions=predictions,
        adjudication={"predictions": matches},
        attestation={"models": {}},
    )


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_bundle(root: Path, bundle: bench.SwarmBenchBundle) -> None:
    for name, value in (
        ("manifest.json", bundle.manifest),
        ("finding-pack.json", bundle.predictions),
        ("dev-key-subset.json", bundle.dev_key),
        ("adjudication.json", bundle.adjudication),
        ("attestation.json", bundle.attestation),
    ):
        _write_json(root / name, value)


@pytest.mark.parametrize("split", ["test", "train", "DEV", "", None])
def test_split_leak_guard_rejects_non_dev(
    synthetic_bundle: bench.SwarmBenchBundle, tmp_path: Path, split: str | None
) -> None:
    _write_bundle(tmp_path, synthetic_bundle)
    keys = [dict(item) for item in synthetic_bundle.dev_key]
    keys[-1]["split"] = split
    _write_json(tmp_path / "dev-key-subset.json", keys)
    with pytest.raises(ValueError, match="Split-leak guard"):
        bench.load_bundle(tmp_path)
    with pytest.raises(ValueError, match="Split-leak guard"):
        replace(synthetic_bundle, dev_key=keys)


def test_missing_split_is_also_a_leak(synthetic_bundle: bench.SwarmBenchBundle) -> None:
    with pytest.raises(ValueError, match="Split-leak guard"):
        replace(synthetic_bundle, dev_key=[{"item_id": "missing", "task": "task-00"}])


def test_load_bundle_and_reference_archive_fallback(
    synthetic_bundle: bench.SwarmBenchBundle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "retained"
    _write_bundle(source, synthetic_bundle)
    archive = tmp_path / "archive"
    manifest = dict(synthetic_bundle.manifest)
    manifest["cases"] = [
        {**case, "prompt_path": str(source / "prompts" / f"{case['task']}.txt")}
        for case in synthetic_bundle.manifest["cases"]
    ]
    _write_json(
        archive / "cross-model-swarm-bench-evidence-index.json",
        {
            "manifest": manifest,
            "unfair_fail_inventory": {
                "dev_key_subset": {"path": str(source / "dev-key-subset.json")}
            },
        },
    )
    _write_json(
        archive / "cross-model-swarm-bench-adjudication.json", synthetic_bundle.adjudication
    )
    _write_json(archive / "cross-model-swarm-bench-attestation.json", synthetic_bundle.attestation)
    monkeypatch.setattr(bench, "DEFAULT_BUNDLE_DIR", tmp_path / "unavailable")
    monkeypatch.setattr(bench, "FALLBACK_BUNDLE_DIR", archive)
    loaded = bench.load_bundle()
    assert loaded.bundle_dir == archive
    assert loaded.predictions == synthetic_bundle.predictions
    assert loaded.dev_key == synthetic_bundle.dev_key
    assert loaded.adjudication == synthetic_bundle.adjudication
    assert bench.load_predictions(archive) == synthetic_bundle.predictions


def test_archive_does_not_fabricate_missing_prediction_bytes(tmp_path: Path) -> None:
    _write_json(tmp_path / "cross-model-swarm-bench-evidence-index.json", {"manifest": {}})
    with pytest.raises(FileNotFoundError, match="finding-pack.json"):
        bench.load_predictions(tmp_path)


def test_prediction_only_loader_never_opens_sealed_files(
    synthetic_bundle: bench.SwarmBenchBundle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_bundle(tmp_path, synthetic_bundle)
    original = bench._read_json
    opened: list[str] = []

    def read(path: Path) -> Any:
        opened.append(path.name)
        assert path.name not in {"adjudication.json", "dev-key-subset.json"}
        return original(path)

    monkeypatch.setattr(bench, "_read_json", read)
    assert bench.load_predictions(tmp_path) == synthetic_bundle.predictions
    assert opened == ["finding-pack.json"]


def test_one_to_one_key_credit_penalizes_redundant_predictions(
    synthetic_bundle: bench.SwarmBenchBundle,
) -> None:
    one = bench.score_strategy(["muse:task-00:1"], synthetic_bundle)
    two = bench.score_strategy(["muse:task-00:1", "kimi:task-00:1"], synthetic_bundle)
    assert two["selected_count"] == 2
    assert two["secondary_hits_n"] == one["secondary_hits_n"] == 1
    assert two["primary_hits_n"] == one["primary_hits_n"] == 1
    assert two["secondary_recall"] == one["secondary_recall"] == pytest.approx(1 / 46)
    assert two["unique_key_precision"] == 0.5 < one["unique_key_precision"]
    assert two["secondary_f1"] == pytest.approx(2 / 48)
    assert two["primary_f1"] == pytest.approx(2 / 14)
    stage = two["stage_attribution"]
    assert stage["credited_selected_predictions"] == 2
    assert stage["redundant_selected_predictions"] == 1
    assert stage["false_alarms_emitted_n"] == 1


@pytest.mark.parametrize("selected", [["missing"], ["muse:task-00:1", "muse:task-00:1"]])
def test_scorer_rejects_unknown_or_repeated_ids(
    synthetic_bundle: bench.SwarmBenchBundle, selected: list[str]
) -> None:
    with pytest.raises(ValueError, match="[Uu]nknown|duplicate"):
        bench.score_strategy(selected, synthetic_bundle)


def test_cross_task_or_non_dev_adjudication_cannot_receive_credit(
    synthetic_bundle: bench.SwarmBenchBundle,
) -> None:
    rows = [dict(row) for row in synthetic_bundle.adjudication["predictions"]]
    rows[0]["matched_key_id"] = "key:task-01:a"
    with pytest.raises(ValueError, match="another task"):
        bench.score_strategy([], replace(synthetic_bundle, adjudication={"predictions": rows}))
    rows[0]["matched_key_id"] = "key:test:secret"
    with pytest.raises(ValueError, match="non-DEV"):
        bench.score_strategy([], replace(synthetic_bundle, adjudication={"predictions": rows}))


def test_scorer_requires_explicit_complete_adjudication(
    synthetic_bundle: bench.SwarmBenchBundle,
) -> None:
    rows = list(synthetic_bundle.adjudication["predictions"])
    with pytest.raises(ValueError, match="explicit adjudication"):
        bench.score_strategy([], replace(synthetic_bundle, adjudication={"predictions": rows[:-1]}))


def _assert_stage_invariants(score: bench.Record, *, total: int = 46) -> None:
    stage = score["stage_attribution"]
    assert stage["merger_kept_n"] + stage["merger_dropped_n"] == stage["worker_pool_ceiling_n"]
    assert stage["worker_pool_ceiling_n"] + stage["worker_miss_n"] == total
    assert stage["merger_kept_n"] == score["secondary_hits_n"]
    assert stage["false_alarms_emitted_n"] == score["selected_count"] - score["secondary_hits_n"]
    assert stage["false_alarms_emitted_n"] == (
        stage["uncredited_selected_predictions"] + stage["redundant_selected_predictions"]
    )
    assert (
        stage["duplicate_hits_filtered_n"] + stage["credited_selected_predictions"]
        == stage["pool_credited_predictions"]
    )
    assert (
        stage["false_alarms_filtered_n"] + stage["uncredited_selected_predictions"]
        == stage["pool_uncredited_predictions"]
    )


@pytest.mark.parametrize("selector", _BUDGETED_SELECTORS)
def test_stage_attribution_invariants(
    synthetic_bundle: bench.SwarmBenchBundle,
    selector: Callable[[Sequence[bench.Record], int], list[str]],
) -> None:
    score = bench.score_strategy(selector(synthetic_bundle.predictions, 3), synthetic_bundle)
    _assert_stage_invariants(score)
    assert score["stage_attribution"]["worker_pool_ceiling_n"] == 36
    assert score["stage_attribution"]["worker_miss_n"] == 10


@pytest.mark.parametrize(
    "selector", [bench.select_locus_consensus_v1, bench.select_specialist_diversity_v2]
)
def test_blind_strategies_strictly_beat_every_worker_on_synthetic_pack(
    synthetic_bundle: bench.SwarmBenchBundle,
    selector: Callable[[Sequence[bench.Record], int], list[str]],
) -> None:
    selected = selector(synthetic_bundle.predictions, 3)
    score = bench.score_strategy(selected, synthetic_bundle)
    assert score["selected_count"] == score["secondary_hits_n"] == 36
    assert score["within_task_budget"] is True
    for alias in _ALIASES:
        single = bench.score_strategy(
            bench.select_single_model(synthetic_bundle.predictions, alias), synthetic_bundle
        )
        assert score["secondary_f1"] > single["secondary_f1"]
        assert score["secondary_recall"] > single["secondary_recall"]


@pytest.mark.parametrize("selector", _BUDGETED_SELECTORS)
def test_budget_and_input_permutation_invariance(
    synthetic_bundle: bench.SwarmBenchBundle,
    selector: Callable[[Sequence[bench.Record], int], list[str]],
) -> None:
    for budget in (0, 1, 2, 3, 5):
        selected = selector(synthetic_bundle.predictions, budget)
        assert selected == selector(list(reversed(synthetic_bundle.predictions)), budget)
        counts = Counter(prediction_id.split(":")[1] for prediction_id in selected)
        assert all(count <= budget for count in counts.values())
        assert len(selected) == len(set(selected))
    with pytest.raises(ValueError, match="nonnegative"):
        selector(synthetic_bundle.predictions, -1)


def test_selectors_have_no_evaluator_argument() -> None:
    for selector in (
        bench.select_single_model,
        bench.select_naive_union,
        *_BUDGETED_SELECTORS,
        bench.select_from_external_file,
    ):
        parameters = inspect.signature(selector).parameters
        assert next(iter(parameters)) == "predictions"
        assert not {"bundle", "adjudication", "dev_key", "manifest"} & parameters.keys()


def test_round_robin_uses_only_top_one_and_nearby_direction_aware_deduplication() -> None:
    predictions = [
        _prediction("muse", 1),
        _prediction("kimi", 1, line=25),
        _prediction("sonnet", 1, line=10, direction="unfair_fail"),
        _prediction("codex", 1, line=100),
        _prediction("muse", 2, path="elsewhere.py"),
    ]
    assert bench.select_round_robin_top1(predictions) == [
        "muse:task-a:1",
        "sonnet:task-a:1",
        "codex:task-a:1",
    ]
    assert bench.select_round_robin_top1(predictions, 1) == ["muse:task-a:1"]


def test_locus_clustering_is_transitive_but_direction_and_task_scoped() -> None:
    predictions = [
        _prediction("muse", 1, line=10, text="first amber"),
        _prediction("kimi", 1, line=35, text="second cedar"),
        _prediction("sonnet", 1, line=60, text="third cobalt"),
        _prediction("codex", 1, line=10, direction="unfair_fail", text="fourth glacier"),
        _prediction("muse", 1, task="task-b", text="first amber"),
    ]
    selected = bench.select_locus_consensus_v1(predictions)
    assert len(selected) == 3
    assert set(selected) == {"muse:task-a:1", "codex:task-a:1", "muse:task-b:1"}


def test_content_jaccard_links_different_files_but_empty_quotes_do_not() -> None:
    predictions = [
        _prediction("muse", 1, path="one.py", text="orbit charcoal amber"),
        _prediction("kimi", 1, path="two.py", text="orbit charcoal amber extra"),
        _prediction("sonnet", 1, path="three.py", text=""),
        _prediction("codex", 1, path="four.py", text=""),
    ]
    assert len(bench.select_locus_consensus_v1(predictions, 5)) == 3


def test_location_validation_and_specialist_quote_requirement() -> None:
    invalid = _prediction("codex", 1, valid=False)
    invalid_range = _prediction("sonnet", 1, path="backwards.py", line=10, end_line=2)
    bad_quote = _prediction(
        "muse", 1, path="unquoted.py", text="plum cherry birch", quote_valid=False
    )
    missing_quote_check = _prediction(
        "kimi", 1, path="legacy.py", text="cider lemon elm", quote_valid=None
    )
    predictions = [invalid, invalid_range, bad_quote, missing_quote_check]
    assert set(bench.select_locus_consensus_v1(predictions)) == {"muse:task-a:1", "kimi:task-a:1"}
    assert bench.select_specialist_diversity_v2(predictions) == ["kimi:task-a:1"]


def test_consensus_anchor_breaks_model_ties_before_worker_index() -> None:
    predictions = [
        _prediction("muse", 3),
        _prediction("kimi", 1, path="other.py", text="velvet quartz"),
    ]
    assert bench.select_specialist_diversity_v2(predictions, 1) == ["muse:task-a:3"]


def test_specialist_routes_opposite_direction_and_harness_representative() -> None:
    predictions = [
        _prediction("muse", 1),
        _prediction("kimi", 1),
        _prediction("sonnet", 1),
        _prediction("sonnet", 2, direction="unfair_fail", path="task.toml", text="glacier cobalt"),
        _prediction("kimi", 2, direction="unfair_fail", path="task.toml", text="glacier cobalt"),
        _prediction("codex", 1, path="tests/conftest.py", text="velvet quartz"),
        _prediction("sonnet", 3, path="tests/conftest.py", text="velvet quartz"),
    ]
    selected = bench.select_specialist_diversity_v2(predictions)
    assert selected[:2] == ["muse:task-a:1", "sonnet:task-a:2"]
    # With no opposite direction, the boundary slot uses the Codex specialist.
    only_wrong_pass = [row for row in predictions if row["finding"]["direction"] == "wrong_pass"]
    assert bench.select_specialist_diversity_v2(only_wrong_pass)[:2] == [
        "muse:task-a:1",
        "codex:task-a:1",
    ]


def test_single_and_union_baselines_preserve_all_worker_rows(
    synthetic_bundle: bench.SwarmBenchBundle,
) -> None:
    assert len(bench.select_single_model(synthetic_bundle.predictions, "muse")) == 36
    assert len(bench.select_naive_union(synthetic_bundle.predictions)) == 144
    assert bench.select_single_model(synthetic_bundle.predictions, "absent") == []
    assert bench.select_single_model(synthetic_bundle.predictions, "muse", 0) == []
    with pytest.raises(ValueError, match="nonnegative"):
        bench.select_single_model(synthetic_bundle.predictions, "muse", -1)


@pytest.mark.parametrize("wrapped", [False, True])
def test_external_selection_formats(
    synthetic_bundle: bench.SwarmBenchBundle, tmp_path: Path, wrapped: bool
) -> None:
    selected = ["muse:task-00:1", "sonnet:task-01:1"]
    document = (
        {"selected_prediction_ids": selected}
        if wrapped
        else {"task-00": selected[:1], "task-01": selected[1:]}
    )
    path = tmp_path / "selection.json"
    _write_json(path, document)
    assert bench.select_from_external_file(synthetic_bundle.predictions, path) == selected


@pytest.mark.parametrize(
    ("document", "error"),
    [
        ({"task-00": ["muse:task-01:1"]}, "off-task"),
        ({"task-absent": []}, "Unknown selection task"),
        ({"selected_prediction_ids": ["absent"]}, "Unknown"),
        ({"selected_prediction_ids": ["muse:task-00:1"] * 2}, "duplicate"),
        ({"selected_prediction_ids": ["muse:task-00:1", "muse:task-00:2"]}, "exceeds"),
        ({"selected_prediction_ids": "muse:task-00:1"}, "list of strings"),
        ({"task-00": [123]}, "list of strings"),
    ],
)
def test_external_selection_rejects_invalid_ids_and_budget(
    synthetic_bundle: bench.SwarmBenchBundle, tmp_path: Path, document: Any, error: str
) -> None:
    path = tmp_path / "selection.json"
    _write_json(path, document)
    with pytest.raises(ValueError, match=error):
        bench.select_from_external_file(synthetic_bundle.predictions, path, budget_per_task=1)


def test_empty_selection_has_finite_zero_scores(synthetic_bundle: bench.SwarmBenchBundle) -> None:
    score = bench.score_strategy([], synthetic_bundle)
    for metric in (
        "primary_recall",
        "secondary_recall",
        "unique_key_precision",
        "primary_f1",
        "secondary_f1",
    ):
        assert score[metric] == 0.0
    _assert_stage_invariants(score)


def test_report_freezes_all_selection_ids_before_opening_evaluator(
    synthetic_bundle: bench.SwarmBenchBundle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_bundle(tmp_path, synthetic_bundle)
    events: list[str] = []
    original_selector = bench.select_specialist_diversity_v2
    original_loader = bench.load_bundle

    def select(predictions: Sequence[bench.Record], budget_per_task: int = 3) -> list[str]:
        events.append("blind-selection")
        return original_selector(predictions, budget_per_task)

    def load(bundle_dir: str | Path | None = None) -> bench.SwarmBenchBundle:
        events.append("sealed-evaluator")
        assert events == ["blind-selection", "sealed-evaluator"]
        return original_loader(bundle_dir)

    monkeypatch.setattr(bench, "select_specialist_diversity_v2", select)
    monkeypatch.setattr(bench, "load_bundle", load)
    monkeypatch.setattr(bench, "FALLBACK_BUNDLE_DIR", tmp_path / "no-tracks")
    report = bench.run_benchmark(tmp_path)
    assert events == ["blind-selection", "sealed-evaluator"]
    assert report["model_calls"] == report["paid_api_spend_usd"] == 0
    assert len(report["strategies"]) == 8


def test_modified_worker_pack_between_selection_and_scoring_is_rejected(
    synthetic_bundle: bench.SwarmBenchBundle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_bundle(tmp_path, synthetic_bundle)
    monkeypatch.setattr(bench, "load_bundle", lambda _: replace(synthetic_bundle, predictions=[]))
    with pytest.raises(ValueError, match="changed after selections"):
        bench.run_benchmark(tmp_path)


def test_cross_track_summary_retains_raw_faults_pooling_and_control_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repair = tmp_path / "cross-model-repair-challenger-swarm.json"
    _write_json(
        repair,
        {
            "round1_results": [{"qualified": True}, {"qualified": False}],
            "round2_results": [
                {"proposer": "muse", "task": "a", "qualified": False, "edit_applied_ok": False},
                {"proposer": "muse", "task": "a", "qualified": True, "edit_applied_ok": True},
                {"proposer": "codex", "task": "a", "qualified": True, "edit_applied_ok": False},
            ],
        },
    )
    seeker = tmp_path / "muse-seeker-swarm-round2.json"
    _write_json(
        seeker,
        {
            "summary": {
                "tasks_attempted": 2,
                "attempt_1_confirmed": 1,
                "outcomes": [
                    {"exploit_confirmed": True, "attempts_run": 1},
                    {"exploit_confirmed": True, "attempts_run": 2},
                ],
            },
            "paid_api_spend_usd": 0,
        },
    )
    _write_json(
        tmp_path / "muse-seeker-swarm.json",
        {
            "summary": {
                "outcomes": [
                    {"task_id": "terminal-bench/cad-model", "exploit_confirmed": False},
                    {
                        "task_id": "terminal-bench/photonic-waveguide-routing",
                        "exploit_confirmed": False,
                    },
                    {"task_id": "a-positive-task", "exploit_confirmed": True},
                ]
            }
        },
    )
    monkeypatch.setattr(bench, "FALLBACK_BUNDLE_DIR", tmp_path / "absent")
    report = bench.summarize_swarm_tracks({}, bundle_dir=tmp_path)
    repair_track, seeker_track = report["tracks"][1:]
    assert repair_track["round_1"]["qualified_n"] == 1
    assert repair_track["round_2"] == {"qualified_n": 2, "total_n": 2, "rate": 1.0}
    assert repair_track["round_2_raw"] == {
        "total_n": 3,
        "qualified_n": 2,
        "edit_applied_ok_true_n": 1,
        "edit_applied_ok_false_n": 2,
    }
    assert repair_track["challenger_lift_pp"] == 50.0
    faults = repair_track["challenger_faults"]
    assert faults["unparsed_or_unapplied_response_n"] == 2
    assert faults["fault_isolation_rate"] == 0.5
    assert faults["http_429_n"] is None
    assert seeker_track["attempt_1"]["confirmed_n"] == 1
    assert seeker_track["attempt_2"]["confirmed_n"] == 2
    assert seeker_track["handoff_lift_pp"] == 50.0
    assert seeker_track["unfair_fail_controls_in_file"] == 0
    assert seeker_track["unfair_fail_controls"]["total_n"] == 2
    assert seeker_track["unfair_fail_controls"]["false_positive_rate"] == 0.0
    assert seeker_track["unfair_fail_controls"]["source_path"].endswith("muse-seeker-swarm.json")


def test_round2_only_control_rate_is_unknown_and_third_attempt_is_not_attempt_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_json(
        tmp_path / "muse-seeker-swarm-round2.json",
        {
            "summary": {
                "tasks_attempted": 1,
                "attempt_1_confirmed": 0,
                "outcomes": [{"exploit_confirmed": True, "attempts_run": 3}],
            }
        },
    )
    monkeypatch.setattr(bench, "FALLBACK_BUNDLE_DIR", tmp_path / "absent")
    track = bench.summarize_swarm_tracks({}, bundle_dir=tmp_path)["tracks"][2]
    assert track["attempt_2"]["confirmed_n"] == 0
    assert track["unfair_fail_controls_in_file"] == 0
    assert track["unfair_fail_controls"]["false_positive_rate"] is None


def test_missing_track_artifacts_are_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bench, "FALLBACK_BUNDLE_DIR", tmp_path / "absent")
    report = bench.summarize_swarm_tracks({}, bundle_dir=tmp_path)
    assert [track["status"] for track in report["tracks"]] == [
        "available",
        "missing_artifact",
        "missing_artifact",
    ]


def test_cli_prints_table_and_json_and_writes_same_report(
    synthetic_bundle: bench.SwarmBenchBundle,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_bundle(tmp_path, synthetic_bundle)
    monkeypatch.setattr(bench, "FALLBACK_BUNDLE_DIR", tmp_path / "absent")
    external = tmp_path / "external.json"
    _write_json(external, {"task-00": ["muse:task-00:1"]})
    output = tmp_path / "output" / "report.json"
    assert (
        bench.main(
            [
                "--bundle-dir",
                str(tmp_path),
                "--external-selection",
                str(external),
                "--output-json",
                str(output),
            ]
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert "| Strategy | Selected | Primary | Secondary |" in printed
    assert "naive_union (unbudgeted)" in printed
    parsed = json.loads(printed[printed.index("{\n") :])
    assert parsed == json.loads(output.read_text(encoding="utf-8"))
    assert parsed["strategies"]["llm_adjudicator"]["selected_count"] == 1


@pytest.fixture
def retained_dev_report() -> dict[str, Any]:
    if not (bench.DEFAULT_BUNDLE_DIR / "finding-pack.json").is_file():
        raise pytest.skip.Exception(
            "Retained 2026-10-24 DEV bundle is not installed on this machine"
        )
    return bench.run_benchmark(bench.DEFAULT_BUNDLE_DIR)


def test_retained_dev_pack_exact_strategy_counts_and_stage_invariants(
    retained_dev_report: dict[str, Any],
) -> None:
    expected = {
        "single_muse": (36, 8, 14),
        "single_kimi": (34, 8, 12),
        "single_sonnet": (35, 6, 13),
        "single_codex": (30, 4, 9),
        "naive_union": (135, 9, 20),
        "round_robin_top1": (27, 7, 13),
        "locus_consensus_v1": (36, 9, 16),
        "specialist_diversity_v2": (36, 7, 15),
    }
    for name, (selected, primary, secondary) in expected.items():
        score = retained_dev_report["strategies"][name]
        assert (score["selected_count"], score["primary_hits_n"], score["secondary_hits_n"]) == (
            selected,
            primary,
            secondary,
        )
        assert score["unique_key_precision"] == pytest.approx(secondary / selected)
        assert score["secondary_recall"] == pytest.approx(secondary / 46)
        assert score["primary_recall"] == pytest.approx(primary / 12)
        assert score["secondary_f1"] == pytest.approx(2 * secondary / (selected + 46))
        assert score["primary_f1"] == pytest.approx(2 * primary / (selected + 12))
        assert score["best_single_alias"] == "muse"
        stage = score["stage_attribution"]
        _assert_stage_invariants(score)
        assert (stage["worker_pool_ceiling_n"], stage["worker_miss_n"]) == (20, 26)
        assert (stage["pool_credited_predictions"], stage["pool_uncredited_predictions"]) == (
            48,
            87,
        )
    assert retained_dev_report["strategies"]["naive_union"]["within_task_budget"] is False
    muse_gain = retained_dev_report["strategies"]["single_muse"]["equal_budget_gain_vs_best_single"]
    assert set(muse_gain.values()) == {0}
    v1_gain = retained_dev_report["strategies"]["locus_consensus_v1"][
        "equal_budget_gain_vs_best_single"
    ]
    assert v1_gain["delta_primary_hits"] == 1
    assert v1_gain["delta_secondary_hits"] == 2
    assert v1_gain["delta_precision_pp"] == pytest.approx(100 * (16 / 36 - 14 / 36))
    assert v1_gain["delta_secondary_f1_pp"] == pytest.approx(100 * (32 / 82 - 28 / 82))


@pytest.mark.parametrize("strategy", ["locus_consensus_v1", "specialist_diversity_v2"])
def test_retained_dev_strategies_strictly_beat_every_single_model(
    retained_dev_report: dict[str, Any], strategy: str
) -> None:
    scores = retained_dev_report["strategies"]
    for alias in _ALIASES:
        assert scores[strategy]["secondary_f1"] > scores[f"single_{alias}"]["secondary_f1"]
        assert scores[strategy]["secondary_recall"] > scores[f"single_{alias}"]["secondary_recall"]
    assert scores[strategy]["selected_count"] <= 36
    assert scores[strategy]["within_task_budget"] is True


def test_retained_cross_track_measured_lifts(retained_dev_report: dict[str, Any]) -> None:
    repair, seeker = retained_dev_report["tracks"][1:]
    if repair["status"] == "available":
        assert repair["round_1"]["qualified_n"] == 27
        assert repair["round_1"]["total_n"] == 30
        assert repair["round_2"]["qualified_n"] == repair["round_2"]["total_n"] == 30
        assert repair["challenger_lift_pp"] == pytest.approx(10.0)
        assert repair["round_2_raw"] == {
            "total_n": 40,
            "qualified_n": 39,
            "edit_applied_ok_true_n": 20,
            "edit_applied_ok_false_n": 20,
        }
        assert repair["challenger_faults"]["fault_isolation_rate"] == 0.95
    if seeker["status"] == "available":
        assert seeker["attempt_1"]["confirmed_n"] == 4
        assert seeker["attempt_2"]["confirmed_n"] == seeker["attempt_2"]["total_n"] == 10
        assert seeker["handoff_lift_pp"] == 60.0
        if seeker["unfair_fail_controls"]["status"] == "measured":
            assert seeker["unfair_fail_controls"]["total_n"] == 2
            assert seeker["unfair_fail_controls"]["false_positive_rate"] == 0.0
