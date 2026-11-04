"""Behavioral coverage for frozen score readers, task joins, and denominators."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scoreboard as sb  # noqa: E402


def write_rows(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def run(key, *, set_name="toy", split="dev", label=0, task="task", natural=None, **extra):
    return {"run_key": key, "set": set_name, "split": split, "label": label,
            "cluster": task, "n_chars": 10, "n_steps": 2, "natural": natural, **extra}


def signal(scores, reasons=None):
    return sb.Signal("toy", scores, reasons or {}, ("frozen",), {})


def test_reader_rejects_duplicate_run_keys_without_losing_opaque_identity(tmp_path):
    key = 'malt:["source", "id:with:colons", "task"]'
    path = write_rows(tmp_path / "runs.jsonl", [run(key), run(key)])
    with pytest.raises(ValueError, match="duplicate run_key"):
        sb.read_rows(path)


def test_reader_keeps_absent_abstained_and_numeric_diagnostics_distinct(tmp_path):
    runs = {key: run(key, label=int(key != "negative"))
            for key in ("negative", "numeric", "abstained", "absent")}
    path = write_rows(tmp_path / "frozen.jsonl", [
        {"run_key": "negative", "score": 0, "version": "v1"},
        {"run_key": "numeric", "score": 8, "abstain": "partial_windows_failed", "version": "v2"},
        {"run_key": "abstained", "score": None, "abstain": "runner_error", "version": "v2"},
    ])
    stored = sb.read_signal(path, runs)
    cov = sb.coverage(runs, stored)
    joined, join = sb.metrics.prepare_rows(runs, stored)
    assert {row.run_key for row in joined} == {"negative", "numeric"}
    assert (cov["selected"], cov["scored"], cov["absent"], cov["abstained"]) == (4, 2, 1, 1)
    assert cov["abstention_reasons"] == {"runner_error": 1}
    assert cov["numeric_diagnostics"] == {"partial_windows_failed": 1}
    assert stored.versions == ("v1", "v2")
    assert stored.provenance["sha256"] == sb.digest(path)


@pytest.mark.parametrize("rows,match", [
    ([{"run_key": "alien", "score": 1}], "score without source run"),
    ([{"run_key": "known", "score": 1}, {"run_key": "known", "score": 2}], "duplicate run_key"),
    ([{"run_key": "known", "score": None}], "null score without abstention"),
    ([{"run_key": "known", "score": True}], "invalid score"),
    ([{"run_key": "known", "score": float("nan")}], "invalid score"),
])
def test_signal_reader_rejects_ambiguous_or_invalid_scores(tmp_path, rows, match):
    path = write_rows(tmp_path / "signal.jsonl", rows)
    with pytest.raises(ValueError, match=match):
        sb.read_signal(path, {"known": run("known")})


def test_goodfire_manifest_preserves_variants_and_never_changes_source_clusters(tmp_path):
    runs = {f"goodfire:{key}": run(f"goodfire:{key}", set_name="goodfire", task=f"run-{key}")
            for key in ("a", "b", "c")}
    path = write_rows(tmp_path / "manifest.jsonl", [
        {"id": "a", "task_id": "bench:original:tools:task1@task-bundle-sha256-abcd"},
        {"id": "b", "task_id": "bench:original:tools:task1@task-bundle-sha256-1234"},
        {"id": "c", "task_id": "bench:oneoff:tools:task1@task-bundle-sha256-5678"},
    ])
    normalized, summary = sb.normalize_tasks(runs, path)
    assert normalized["goodfire:a"]["task"] == normalized["goodfire:b"]["task"]
    assert normalized["goodfire:a"]["task"] != normalized["goodfire:c"]["task"]
    assert "task" not in runs["goodfire:a"]
    assert runs["goodfire:a"]["cluster"] == "run-a"
    assert summary["sets"]["goodfire"] == {"rows": 3, "tasks": 2, "retained_clusters": 3}
    assert summary["goodfire_50_task_mapping_verified"] is False


def test_actual_task_override_is_namespaced_and_unknown_keys_fail():
    runs = {"a": run("a", set_name="one"), "b": run("b", set_name="two")}
    normalized, _ = sb.normalize_tasks(runs, overrides={"a": "shared", "b": "shared"})
    joined, _ = sb.metrics.prepare_rows(normalized, signal({"a": 1, "b": 2}))
    assert joined[0].task != joined[1].task
    with pytest.raises(ValueError, match="unknown run keys"):
        sb.normalize_tasks(runs, overrides={"absent": "task"})
    with pytest.raises(ValueError, match="no actual task identifier"):
        sb.normalize_tasks({"a": run("a", task="")})


def test_population_policies_exclude_extensions_decisive_and_nonpool_rows():
    original = {
        "dev": run("dev", natural=True),
        "pool": run("pool", split="heldout", natural=True),
        "unknown": run("unknown", split="heldout"),
        "decisive": run("decisive", split="heldout", decisive_rule=True, natural=True),
        "separate": run("separate", split="heldout", pool=False, natural=True),
    }
    extensions = {"malt": {"m": run("m", split="heldout", set_name="malt", natural=True)},
                  "hackbench": {"h": run("h", split="heldout", set_name="hackbench", label=1)}}
    pops = sb.populations({"original": original, **extensions})
    assert set(pops["original/pool/heldout"][1]) == {"pool", "unknown"}
    assert set(pops["original/pool/heldout_natural"][1]) == {"pool"}
    assert set(pops["original/set/toy"][1]) == set(original)
    assert set(pops["original/natural/toy"][2]) == set(original)
    assert not any(name.startswith("hackbench/pool/") for name in pops)


def test_sparse_signal_cannot_refit_full_population_deciles():
    runs = {str(i): run(str(i), label=i % 2, n_chars=i + 1) for i in range(100)}
    normalized, _ = sb.normalize_tasks(runs)
    edges = sb.full_edges(normalized)
    matched, _ = sb.metrics.prepare_rows(normalized, signal({"0": 0, "99": 1}))
    result = sb.evaluate_job(matched, None, edges, {}, 0, 1106, False)
    assert result["strata_edges"] == list(range(10, 100, 10))
    assert result["scores"]["signal"]["length_stratified"]["auroc"] is None
    assert result["scores"]["signal"]["length_stratified"]["eligible_rows"] == 0


def test_hackbench_recall_preserves_all_selected_positive_denominator():
    runs = {str(i): run(str(i), label=1) for i in range(4)}
    frozen = signal({"0": 9, "1": None}, {"1": "runner_error"})
    assert sb.operational_recall(runs, frozen, 8)["operational_recall"] == 0.25
    assert sb.operational_recall(runs, frozen, math.inf)["operational_recall"] == 0
    assert sb.operational_recall(runs, signal({"0": None}, {"0": "unsupported"}), 8)["operational_recall"] == 0
    assert sb.operational_recall(runs, None, 8)["operational_recall"] is None
    assert sb.operational_recall(runs, frozen, None)["operational_recall"] is None
    normalized, _ = sb.normalize_tasks(runs)
    rows, _ = sb.metrics.prepare_rows(normalized, frozen)
    result = sb.evaluate_job(rows, None, (), {"signal": 8}, 0, 1106, True)
    assert result["scores"]["signal"]["dev_fixed_operating_point"]["recall"] == 1
    assert "fpr" not in result["scores"]["signal"]["dev_fixed_operating_point"]
    assert "raw_auroc" not in result["scores"]["signal"]
    assert "paired_gain" not in result


def test_historical_or_retains_missing_member_policy_without_heldout_calibration():
    runs = {"n": run("n"), "p": run("p", label=1),
            "h": run("h", split="heldout", label=1)}
    definition = {"signal": "or_frozen", "method": "or", "members": ["one", "two"]}
    inputs = {"one": signal({"n": 0, "p": 1}), "two": signal({"n": 0, "p": None}, {"p": "missing"})}
    combined = sb.combine_signals(definition, runs, inputs)
    assert combined.scores == {"n": 0, "p": 1, "h": 0}
    changed = {**runs, "h": {**runs["h"], "label": 0}}
    assert sb.combine_signals(definition, changed, inputs).scores == combined.scores


def test_length_model_fixed_threshold_uses_same_logit_units_as_evaluation():
    runs = {"n": run("n", n_chars=0), "p": run("p", label=1, n_chars=1)}
    normalized, _ = sb.normalize_tasks(runs)
    frozen = signal({"n": 0, "p": 1})
    # Both probabilities saturate at 1, but their linear predictors remain ordered.
    model = sb.metrics.LengthModel(1000, 1, 0, 1, 2)
    calibrated, receipt = sb.thresholds(frozen, normalized, model)
    assert calibrated["length_model"] > 1000
    assert receipt["length_model_threshold_units"] == "logit"
    rows, _ = sb.metrics.prepare_rows(normalized, frozen)
    result = sb.evaluate_job(rows, model, (), calibrated, 0, 1106, False)
    fixed = result["scores"]["length_model"]["dev_fixed_operating_point"]
    assert (fixed["fpr"], fixed["recall"]) == (0, 1)
