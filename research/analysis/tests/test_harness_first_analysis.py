from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ANALYSIS = Path(__file__).resolve().parents[1] / "harness-first"


def _load(name, path, package=False):
    specification = importlib.util.spec_from_file_location(
        name, path, submodule_search_locations=[str(path.parent)] if package else None
    )
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


consumer = _load("har13_analysis_test_package", ANALYSIS / "harness_first/__init__.py", True)
fixture_source = _load("har13_fixture_source", ANALYSIS / "fixtures.py")
analyze, main = consumer.analyze, consumer.main
generate_cpu_fixtures, write_json = fixture_source.generate_cpu_fixtures, fixture_source.write_json


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "cpu-fixtures"
    generate_cpu_fixtures(root)
    return root


def report(root: Path, **overrides):
    spec = json.loads((root / "spec.json").read_text())
    spec.update(overrides)
    return analyze(spec, repo_root=root, evidence_kind="fixture")


def pairs(value):
    return {row["pairing_key_value"]: row for row in value["per_task_pairs"]}


def change_trial(root, task, change, *, arm="candidate", file="result.json"):
    path = root / f"{arm}-job" / f"{task}__1" / file
    data = json.loads(path.read_text())
    change(data)
    write_json(path, data)


def test_ties_regressions_and_fractional_rewards_survive(corpus):
    rows = pairs(report(corpus))
    for task, delta, category in (
        ("task-pos", 1, "positive"),
        ("task-neu", 0, "neutral"),
        ("task-neg", -1, "negative"),
        ("task-frac", 0.5, "positive"),
    ):
        assert rows[task]["delta"]["effective_reward_delta"] == delta
        assert rows[task]["delta"]["classification"] == category
    assert rows["task-pos"]["qualification"]["model_qualification"] == "unknown_revision"
    assert report(corpus)["metadata"]["inference_level"] == "descriptive_only"


@pytest.mark.parametrize(
    "task,reason",
    [
        ("task-mismatch-model", "model_mismatch"),
        ("task-mismatch-revision", "model_revision_mismatch"),
        ("task-mismatch-task", "task_digest_mismatch"),
        ("task-mismatch-verif", "verifier_digest_mismatch"),
        ("task-empty-fallback", "task_identity_missing_unequated"),
    ],
)
def test_identity_mismatch_or_missing_is_not_a_pair(corpus, task, reason):
    row = pairs(report(corpus))[task]
    assert not row["qualification"]["is_qualified"]
    assert reason in row["qualification"]["disqualification_reasons"]
    assert row["delta"] is None


@pytest.mark.parametrize(
    "task", ["task-stale-infra", "task-stale-verifier", "task-stale-agent", "task-partial"]
)
def test_exception_or_partial_cannot_leak_stale_reward(corpus, task):
    row = pairs(report(corpus))[task]
    assert row["candidate"]["raw_reward"] == 1
    assert row["candidate"]["effective_reward"] is None
    assert row["candidate"]["native_aggregate_usage"]["input_tokens"] == 100
    assert row["delta"] is None


def test_explicit_agent_budget_failure_is_distinct_from_infrastructure(corpus):
    rows = pairs(report(corpus, budget_exhaustion_is_failure=True))
    assert rows["task-stale-agent"]["candidate"]["effective_reward"] == 0
    assert rows["task-stale-infra"]["candidate"]["effective_reward"] is None
    assert rows["task-stale-verifier"]["candidate"]["effective_reward"] is None


def test_absent_arms_and_ambiguous_attempts_are_visible(corpus):
    rows = pairs(report(corpus))
    assert rows["task-missing-cand"]["pairing_status"] == "missing_candidate"
    assert rows["task-missing-base"]["pairing_status"] == "missing_baseline"
    ambiguous = rows["task-ambig"]
    assert ambiguous["pairing_status"] == "ambiguous_duplicate_attempts"
    assert ambiguous["delta"] is None
    assert {t["raw_reward"] for t in ambiguous["baseline_duplicate_attempts"]} == {0, 1}


def test_root_and_worker_step_usage_never_double_counts_summary(corpus):
    row = pairs(report(corpus))["task-subagent"]["candidate"]
    # Root final_metrics deliberately says999 and native aggregate100. Neither
    # can be added to worker usage to invent a total.
    assert row["root_usage"]["input_tokens"] == 200
    assert row["worker_usage"]["input_tokens"] == 300
    assert row["total_usage"]["input_tokens"] == 500
    assert row["total_usage"]["coverage_reason"] == "retained_total_step_metrics"
    partial = pairs(report(corpus))["task-partial-worker"]["candidate"]
    assert partial["worker_usage"]["input_tokens"] is None
    assert partial["total_usage"]["input_tokens"] is None


def test_native_aggregate_and_unobserved_workers_remain_qualified_missingness(corpus):
    rows = pairs(report(corpus))
    native = rows["task-native-only"]["candidate"]
    assert native["native_aggregate_usage"]["input_tokens"] == 600
    assert native["root_usage"]["input_tokens"] is None
    assert native["worker_usage"]["input_tokens"] is None
    assert native["total_usage"]["input_tokens"] is None
    assert rows["task-pos"]["baseline"]["worker_usage"]["input_tokens"] is None


def test_requested_reward_dimension_does_not_fall_back_to_primary(corpus):
    result = report(corpus, reward_name="not-present")
    assert all(row["delta"] is None for row in result["per_task_pairs"])
    assert result["summary"]["outcomes"]["mean_effective_reward_delta"] is None


def test_configured_threshold_controls_success_not_exact_one(corpus):
    row = pairs(report(corpus, pass_threshold=0.5))["task-frac"]
    assert row["baseline"]["passed"] is False
    assert row["candidate"]["passed"] is True


def test_constraints_and_self_pairs_are_not_qualified(corpus):
    spec = json.loads((corpus / "spec.json").read_text())
    spec["cohorts"][1]["paths"] = spec["cohorts"][0]["paths"]
    rows = analyze(spec, repo_root=corpus, evidence_kind="fixture")["per_task_pairs"]
    assert all(row["delta"] is None for row in rows)
    constrained = report(corpus, constraints={"task_digest": "sha256:" + "f" * 64})
    assert constrained["summary"]["counts_and_denominators"]["n_qualified_pairs"] == 0


def test_single_trial_selector_does_not_filter_whole_job_siblings(corpus):
    spec = json.loads((corpus / "spec.json").read_text())
    spec["cohorts"][0]["paths"] = ["baseline-job/task-pos__1", "baseline-job"]
    value = analyze(spec, repo_root=corpus, evidence_kind="fixture")
    assert pairs(value)["task-neg"]["baseline"] is not None
    assert (
        value["summary"]["counts_and_denominators"]["n_total_baseline_trials"]
        == report(corpus)["summary"]["counts_and_denominators"]["n_total_baseline_trials"]
    )


@pytest.mark.parametrize("value", [True, -1, float("nan")])
def test_invalid_usage_is_missing_not_zero_or_a_global_crash(corpus, value):
    change_trial(
        corpus, "task-native-only", lambda data: data["agent_result"].update(n_input_tokens=value)
    )
    row = pairs(report(corpus))["task-native-only"]["candidate"]
    assert row["native_aggregate_usage"]["input_tokens"] is None
    assert row["issues"]


def test_invalid_reward_boolean_and_unfinished_metadata_do_not_qualify(corpus):
    change_trial(
        corpus, "task-pos", lambda data: data["verifier_result"]["rewards"].update(reward=True)
    )
    change_trial(corpus, "task-neu", lambda data: data.update(finished_at="not-a-time"))
    rows = pairs(report(corpus))
    assert rows["task-pos"]["delta"] is None
    assert rows["task-neu"]["delta"] is None


def test_source_origin_cannot_be_relabelled_as_model_results(corpus):
    with pytest.raises(ValueError, match="fixture records"):
        analyze(corpus / "spec.json", repo_root=corpus, evidence_kind="model-run")


def test_missing_cohort_retains_available_task_rows(corpus):
    spec = json.loads((corpus / "spec.json").read_text())
    spec["cohorts"][1]["paths"] = ["not-dispatched"]
    value = analyze(spec, repo_root=corpus, evidence_kind="fixture")
    assert value["summary"]["warnings"]
    assert value["per_task_pairs"]
    assert all(row["candidate"] is None and row["delta"] is None for row in value["per_task_pairs"])


def test_cli_is_reproducible_and_cannot_overwrite_source_evidence(corpus, tmp_path):
    destination = tmp_path / "report"
    args = [
        str(corpus / "spec.json"),
        "--repo-root",
        str(corpus),
        "--evidence-kind",
        "fixture",
        "--output",
        str(destination),
    ]
    assert main(args) == 0
    before = {p.name: p.read_bytes() for p in destination.iterdir()}
    assert main(args) == 0
    assert before == {p.name: p.read_bytes() for p in destination.iterdir()}
    source = corpus / "baseline-job/result.json"
    source_before = source.read_bytes()
    with pytest.raises(SystemExit):
        main([*args[:-1], str(corpus / "baseline-job")])
    assert source.read_bytes() == source_before
    (destination / "report.json").write_text("unrelated output")
    with pytest.raises(SystemExit):
        main(args)
    assert (destination / "report.json").read_text() == "unrelated output"


def test_partial_native_job_keeps_its_available_trials(corpus):
    path = corpus / "candidate-job/result.json"
    payload = json.loads(path.read_text())
    payload["finished_at"] = None
    write_json(path, payload)
    value = report(corpus)
    assert pairs(value)["task-pos"]["candidate"]["raw_reward"] == 1
    assert pairs(value)["task-partial"]["candidate"]["effective_reward"] is None
    assert any("Partial native job" in warning for warning in value["summary"]["warnings"])


def test_empty_environment_identity_cannot_match(corpus):
    for arm in ("baseline", "candidate"):
        change_trial(
            corpus, "task-pos", lambda data: data.update(environment={}), arm=arm, file="lock.json"
        )
    row = pairs(report(corpus))["task-pos"]
    assert (
        "environment_identity_missing_unequated" in row["qualification"]["disqualification_reasons"]
    )
    assert row["delta"] is None


def test_missing_harness_version_is_not_qualified(corpus):
    change_trial(corpus, "task-pos", lambda data: data["agent_info"].update(version=None))
    change_trial(
        corpus, "task-pos", lambda data: data["agent"].update(version=None), file="lock.json"
    )
    row = pairs(report(corpus))["task-pos"]
    assert "harness_version_missing" in row["qualification"]["disqualification_reasons"]
    assert row["delta"] is None


def test_positive_fraction_is_not_rounded_into_neutral(corpus):
    change_trial(
        corpus,
        "task-frac",
        lambda data: data["verifier_result"]["rewards"].update(reward=0.250000001),
    )
    row = pairs(report(corpus))["task-frac"]
    assert row["delta"]["effective_reward_delta"] > 0
    assert row["delta"]["classification"] == "positive"


def test_compute_sums_include_failed_attempts_and_name_partial_coverage(corpus):
    value = report(corpus)
    candidate = value["summary"]["candidate_cohort_summary"]
    metric = candidate["native_aggregate_usage"]["input_tokens"]
    observations = []
    for p in value["per_task_pairs"]:
        if p["candidate"]:
            observations.append(p["candidate"]["native_aggregate_usage"]["input_tokens"])
    assert metric["total"] is None
    assert metric["observed_subtotal"] == sum(x for x in observations if x is not None)
    assert metric["covered_count"] == sum(x is not None for x in observations)
    assert candidate["n_suppressed_infrastructure_attempts"] == 2
    assert candidate["n_reward_suppressed_attempts"] == 4
    elapsed = candidate["compute_and_timing"]
    assert elapsed["total_wall_time_seconds"] is None
    assert elapsed["observed_wall_time_seconds"] == 18 * 20
    assert elapsed["wall_time_covered_count"] == 18


@pytest.mark.parametrize("tokens", [None, 700])
def test_published_har12_root_only_accounting_keeps_worker_unknown(corpus, tokens):
    task = "task-native-only"
    change_trial(
        corpus,
        task,
        lambda data: data["agent_result"].update(
            n_input_tokens=tokens,
            metadata={
                "root_model": "fixture/open-root",
                "worker_model": "fixture/fixed-worker",
                "root_input_tokens": tokens,
                "root_output_tokens": None,
                "worker_usage": None,
                "atif": False,
            },
        ),
    )
    write_json(
        corpus / "candidate-job" / f"{task}__1" / "agent/rlm/root-messages.json",
        {
            "source_format": "authors-rlm-root-messages",
            "schema_version": None,
            "root_calls": 2,
            "worker_calls": 3,
            "messages": [{"content": "not exported"}],
        },
    )
    value = report(corpus)
    row = pairs(value)[task]["candidate"]
    assert row["root_usage"]["input_tokens"] == tokens
    assert row["worker_usage"]["input_tokens"] is None
    assert row["total_usage"]["input_tokens"] is None
    assert row["source_native_accounting"]["worker_calls"] == 3
    assert row["source_native_accounting"]["physical_request_attempts"] is None
    assert "messages" not in row["source_native_accounting"]
    assert any(
        path.endswith("root-messages.json") for path in value["metadata"]["source_inputs_sha256"]
    )


def test_source_native_root_conflict_cannot_create_a_qualified_pair(corpus):
    task = "task-native-only"
    change_trial(
        corpus,
        task,
        lambda data: data["agent_result"].update(
            metadata={"root_model": "fixture/wrong-root", "atif": False}
        ),
    )
    write_json(
        corpus / "candidate-job" / f"{task}__1" / "agent/rlm/root-messages.json",
        {
            "source_format": "authors-rlm-root-messages",
            "schema_version": None,
            "root_calls": 1,
            "worker_calls": None,
        },
    )
    row = pairs(report(corpus))[task]
    assert row["delta"] is None
    assert row["candidate"]["raw_reward"] == 1
    assert row["candidate"]["effective_reward"] is None
