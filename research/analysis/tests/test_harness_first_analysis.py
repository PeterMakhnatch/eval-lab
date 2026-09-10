from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import UUID

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
renderer = _load("har13_renderer", ANALYSIS / "harness_first/renderer.py")
fixture_source = _load("har13_fixture_source", ANALYSIS / "fixtures.py")
analyze, main = consumer.analyze, consumer.main
render_markdown_report = renderer.render_markdown_report
render_svg_plot = renderer.render_svg_plot
generate_cpu_fixtures, write_json, digest = (
    fixture_source.generate_cpu_fixtures,
    fixture_source.write_json,
    fixture_source.digest,
)

HAR12_PAYLOAD_TEMPLATE = {
    "source_format": "authors-rlm-root-messages",
    "schema_version": None,
    "root_calls": 2,
    "worker_calls": 1,
    "root_input_tokens": 300,
    "root_output_tokens": 150,
    "agent_result_totals_include_workers": False,
    "exhausted_iterations": False,
    "secret_source": "env:DEEPSEEK_API_KEY",
    "backend": "evallab.rlm_runtime:ManagedReplBackend",
    "backend_git_ref": "00cf4af7496894ac87c64f16117c52666108afc4",
}


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
    with pytest.raises(ValueError):
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


# =============================================================================
# HAR-17 Consolidated Regressions
# =============================================================================


# 1. Identity Source Conflicts & Revision Boundaries
@pytest.mark.parametrize(
    ("field", "result_patch", "lock_patch"),
    [
        (
            "model_name",
            lambda d: d["agent_info"]["model_info"].update(name="fixture/open-root"),
            lambda d: d["agent"].update(model_name="fixture/conflicting-lock-model"),
        ),
        (
            "model_revision",
            lambda d: d["agent_info"]["model_info"].update(revision="rev-1"),
            lambda d: d["agent"].update(model_revision="rev-2"),
        ),
        (
            "task_digest",
            lambda d: d.update(task_checksum=digest("task-variant-a")),
            lambda d: d["task"].update(digest=digest("task-variant-b")),
        ),
    ],
)
def test_contradictory_identity_sources_disqualify_pair(corpus, field, result_patch, lock_patch):
    """Explicit contradictions between result and lock records emit identity_conflict and disqualify."""
    task = "task-pos"
    change_trial(corpus, task, result_patch, arm="candidate", file="result.json")
    change_trial(corpus, task, lock_patch, arm="candidate", file="lock.json")

    row = pairs(report(corpus))[task]
    issues = row["candidate"]["issues"]
    assert any(
        i.startswith(f"identity_conflict:{field}") or "identity_conflict" in i for i in issues
    )
    assert row["qualification"]["is_qualified"] is False
    assert row["delta"] is None


def test_source_native_lock_model_conflict_disqualifies_pair(corpus):
    """Lock model contradiction must not be masked by result metadata and root-messages agreement."""
    task = "task-native-only"
    change_trial(
        corpus,
        task,
        lambda d: d["agent"].update(model_name="fixture/conflicting-lock-model"),
        arm="candidate",
        file="lock.json",
    )
    change_trial(
        corpus,
        task,
        lambda d: d["agent_result"].update(
            metadata={
                "root_model": "fixture/open-root",
                "atif": False,
                "root_input_tokens": 500,
                "root_output_tokens": 50,
            }
        ),
        arm="candidate",
        file="result.json",
    )
    write_json(
        corpus / "candidate-job" / f"{task}__1" / "agent/rlm/root-messages.json",
        {
            "source_format": "authors-rlm-root-messages",
            "schema_version": None,
            "root_calls": 2,
            "worker_calls": None,
        },
    )

    row = pairs(report(corpus))[task]
    issues = row["candidate"]["issues"]
    assert any("identity_conflict" in i or "root_identity_conflict" in i for i in issues)
    assert row["qualification"]["is_qualified"] is False
    assert row["delta"] is None


def test_unknown_revision_preserved_as_valid_descriptive_qualification(corpus):
    """Missing or unstated model revisions qualify under descriptive inference without conflict."""
    task = "task-pos"
    res = report(corpus)
    row = pairs(res)[task]
    assert row["baseline"]["model_revision"] is None
    assert row["candidate"]["model_revision"] is None
    assert row["qualification"]["model_qualification"] == "unknown_revision"
    assert row["qualification"]["is_qualified"] is True
    assert row["delta"] is not None
    assert res["metadata"]["inference_level"] == "descriptive_only"


# 2. UUID Spelling Normalization, Self-Pairs, and Collisions
def test_cross_arm_uuid_spelling_alias_detected_as_physical_self_pair(corpus):
    """Hex vs hyphenated UUID spelling aliases across arms are recognized as physical self-pairs."""
    baseline_id = json.loads((corpus / "baseline-job/task-pos__1/result.json").read_text())["id"]
    change_trial(
        corpus,
        "task-pos",
        lambda d: d.update(id=UUID(baseline_id).hex),
        arm="candidate",
        file="result.json",
    )

    row = pairs(report(corpus))["task-pos"]
    assert row["qualification"]["is_qualified"] is False
    assert row["delta"] is None
    assert (
        row["qualification"].get("same_physical_trial_across_arms") is True
        or "same_physical_trial_across_arms" in row["qualification"]["disqualification_reasons"]
    )


def test_same_arm_uuid_spelling_collision_is_refused(corpus):
    """Distinct trial directories in the same arm claiming the same UUID under alternate spellings fail."""
    pos_id = json.loads((corpus / "baseline-job/task-pos__1/result.json").read_text())["id"]
    change_trial(
        corpus,
        "task-neg",
        lambda d: d.update(id=UUID(pos_id).hex),
        arm="baseline",
        file="result.json",
    )

    spec = json.loads((corpus / "spec.json").read_text())
    spec["cohorts"][0]["trial_names"] = ["task-pos__1", "task-neg__1"]
    spec["cohorts"][1]["trial_names"] = ["task-pos__1", "task-neg__1"]
    with pytest.raises(ValueError):
        analyze(spec, repo_root=corpus, evidence_kind="fixture")


# 3. Malformed Sibling Isolation for Direct and Partial Jobs
def test_malformed_unselected_sibling_does_not_erase_explicit_trial_selection(corpus):
    """Explicit trial selection does not fail when an unselected sibling in the directory is malformed."""
    (corpus / "candidate-job/task-neg__1/result.json").write_text("{not-json\n")
    spec = json.loads((corpus / "spec.json").read_text())
    spec["cohorts"] = [
        {"label": "baseline", "paths": ["baseline-job/task-pos__1"]},
        {"label": "candidate", "paths": ["candidate-job/task-pos__1"]},
    ]

    res = analyze(spec, repo_root=corpus, evidence_kind="fixture")
    row = pairs(res)["task-pos"]
    assert row["candidate"] is not None
    assert row["candidate"]["raw_reward"] == 1
    assert row["delta"]["classification"] == "positive"
    assert res["summary"]["counts_and_denominators"]["n_total_candidate_trials"] == 1


def test_partial_job_quarantines_malformed_sibling_preserving_valid_trials(corpus):
    """A partial job quarantines a malformed trial record and preserves valid siblings with diagnostics."""
    job_path = corpus / "candidate-job/result.json"
    job_data = json.loads(job_path.read_text())
    job_data.update(finished_at=None)
    write_json(job_path, job_data)

    (corpus / "candidate-job/task-neg__1/result.json").write_text("{not-json\n")

    spec = json.loads((corpus / "spec.json").read_text())
    spec["cohorts"][0]["trial_names"] = ["task-pos__1", "task-neg__1"]
    spec["cohorts"][1]["trial_names"] = ["task-pos__1", "task-neg__1"]

    res = analyze(spec, repo_root=corpus, evidence_kind="fixture")
    rows = pairs(res)
    assert rows["task-pos"]["candidate"] is not None
    assert rows["task-pos"]["delta"]["classification"] == "positive"
    assert rows["task-neg"]["candidate"] is None
    assert rows["task-neg"]["delta"] is None
    assert res["summary"]["counts_and_denominators"]["n_partial_baseline_only"] == 1
    assert any("task-neg__1" in warning for warning in res["summary"]["warnings"])


# 4. Overlapping Missing-Arm and Repeat Denominator
def test_missing_arm_denominator_includes_ambiguous_repeat_group(corpus):
    """Missing-arm counts and ambiguous repeat counts overlap without masking each other."""
    spec = json.loads((corpus / "spec.json").read_text())
    spec["cohorts"][0]["paths"] = ["baseline-job"]
    spec["cohorts"][0]["trial_names"] = ["task-ambig__1", "task-ambig__2"]
    spec["cohorts"][1]["paths"] = ["candidate-job/task-pos__1"]

    res = analyze(spec, repo_root=corpus, evidence_kind="fixture")
    rows = pairs(res)
    assert rows["task-ambig"]["candidate"] is None
    assert rows["task-ambig"]["delta"] is None
    assert rows["task-pos"]["baseline"] is None

    counts = res["summary"]["counts_and_denominators"]
    assert counts["n_ambiguous_pairs"] == 1
    assert counts["n_partial_baseline_only"] == 1
    assert counts["n_partial_candidate_only"] == 1
    assert counts["n_qualified_pairs"] == 0


# 5. Malformed Structured Exception Stale Reward Suppression
def test_structured_error_dict_without_exception_type_suppresses_stale_reward(corpus):
    """Structured error dictionary lacking exception_type suppresses stale verifier reward."""
    change_trial(
        corpus,
        "task-pos",
        lambda d: d.update(
            exception_info={
                "error": "ContainerKilledError",
                "message": "Out of memory in sandbox",
                "exit_code": 137,
            },
            verifier_result={"rewards": {"reward": 1.0}},
        ),
    )

    row = pairs(report(corpus))["task-pos"]["candidate"]
    assert row["raw_reward"] == 1.0
    assert row["effective_reward"] is None
    assert row["reward_suppressed"] is True
    assert pairs(report(corpus))["task-pos"]["delta"] is None


def test_generic_and_infrastructure_exceptions_never_convert_to_zero_under_budget_policy(corpus):
    """Infrastructure failure must remain suppressed and never convert to zero model credit."""
    change_trial(
        corpus,
        "task-pos",
        lambda d: d.update(
            exception_info={
                "exception_type": "DockerDaemonError",
                "exception_message": "daemon connection lost",
            },
            verifier_result={"rewards": {"reward": 1.0}},
        ),
    )

    res = report(corpus, budget_exhaustion_is_failure=True)
    row = pairs(res)["task-pos"]
    assert row["candidate"]["effective_reward"] is None
    assert row["candidate"]["reward_suppressed"] is True
    assert row["delta"] is None
    assert res["summary"]["candidate_cohort_summary"]["n_suppressed_infrastructure_attempts"] >= 1


# 6. Tiny Genuine Cost Delta Survival
def test_tiny_genuine_sub_micro_cost_delta_survives_without_rounding(corpus):
    """A genuine sub-1e-6 cost difference on native trial paths is preserved in native_cost_delta_usd."""
    # Baseline native cost is 0.005. Candidate native cost is 0.0050005 (delta is +0.0000005 = 5e-7 < 1e-6).
    change_trial(
        corpus,
        "task-native-only",
        lambda d: d["agent_result"].update(cost_usd=0.0050005),
        arm="candidate",
    )

    pair = pairs(report(corpus))["task-native-only"]
    delta = pair["delta"]
    assert delta is not None
    assert delta["native_cost_delta_usd"] is not None
    assert delta["native_cost_delta_usd"] > 0.0
    assert delta["native_cost_delta_usd"] == pytest.approx(5e-7, rel=1e-5)


# 7. HAR-12 Payload-Only Fields, Sourced Revisions, and Model Conflicts
def test_har12_payload_only_tokens_and_provenance_retained(corpus):
    """When metadata omits root tokens, valid root-messages payload populates root usage and provenance."""
    task = "task-native-only"
    change_trial(
        corpus,
        task,
        lambda d: d["agent_result"].update(
            n_input_tokens=None,
            metadata={
                "root_model": "fixture/open-root",
                "worker_model": "fixture/fixed-worker",
                "root_input_tokens": None,
                "root_output_tokens": None,
                "atif": False,
            },
        ),
    )
    payload = dict(HAR12_PAYLOAD_TEMPLATE)
    payload.update(root_input_tokens=350, root_output_tokens=120, exhausted_iterations=True)
    write_json(corpus / "candidate-job" / f"{task}__1" / "agent/rlm/root-messages.json", payload)

    cand = pairs(report(corpus))[task]["candidate"]
    assert cand["source_native_accounting"]["source_format"] == "authors-rlm-root-messages"
    assert cand["root_usage"]["input_tokens"] == 350
    assert cand["root_usage"]["output_tokens"] == 120
    assert cand["source_native_accounting"]["backend"] == "evallab.rlm_runtime:ManagedReplBackend"
    assert cand["source_native_accounting"]["exhausted_iterations"] is True
    assert cand["total_usage"]["coverage_reason"] == "har12_worker_usage_unavailable"


def test_har12_payload_root_model_conflict_disqualifies_pair(corpus):
    """Root model contradiction between payload and lock/metadata flags identity conflict and suppresses reward."""
    task = "task-native-only"
    payload = dict(HAR12_PAYLOAD_TEMPLATE)
    payload["root_model"] = "fixture/conflicting-rogue-root"
    write_json(corpus / "candidate-job" / f"{task}__1" / "agent/rlm/root-messages.json", payload)

    pair = pairs(report(corpus))[task]
    cand = pair["candidate"]
    assert any("identity_conflict" in i or "root_identity" in i for i in cand["issues"])
    assert cand["effective_reward"] is None
    assert pair["qualification"]["is_qualified"] is False
    assert pair["delta"] is None


def test_har12_model_revision_extracted_from_producer_when_lock_missing(corpus):
    """When lock.json omits model revision, root_model_revision in producer metadata is extracted."""
    task = "task-native-only"
    rev = "sha256:abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234"
    change_trial(
        corpus,
        task,
        lambda d: d["agent_result"].update(
            metadata={"root_model": "fixture/open-root", "root_model_revision": rev, "atif": False}
        ),
    )
    change_trial(
        corpus,
        task,
        lambda d: d.get("agent", {}).update(model_revision=None),
        file="lock.json",
    )
    payload = dict(HAR12_PAYLOAD_TEMPLATE)
    payload["root_model_revision"] = rev
    write_json(corpus / "candidate-job" / f"{task}__1" / "agent/rlm/root-messages.json", payload)

    cand = pairs(report(corpus))[task]["candidate"]
    assert cand["model_revision"] == rev


# 8. Worker Reference Graph Completeness & Repeated-Ref Counterexample
def test_partial_worker_reference_graph_does_not_claim_complete_usage(corpus):
    """Unresolved reference shapes do not become complete capture when a worker is present."""

    def setup_workers(data):
        data["steps"][1]["observation"] = {
            "results": [
                {
                    "content": "Spawned subagents",
                    "subagent_trajectory_ref": ["worker-present", "worker-missing"],
                }
            ]
        }
        data["subagent_trajectories"] = [
            {
                "schema_version": "ATIF-v1.7",
                "session_id": "worker-present",
                "trajectory_id": "worker-present",
                "agent": {
                    "name": "specialist-worker",
                    "version": "v1.0",
                    "model_name": "fixture/worker",
                },
                "steps": [
                    {"step_id": 1, "source": "user", "message": "subtask"},
                    {
                        "step_id": 2,
                        "source": "agent",
                        "message": "done",
                        "llm_call_count": 1,
                        "metrics": {
                            "prompt_tokens": 80,
                            "completion_tokens": 30,
                            "cost_usd": 0.001,
                        },
                    },
                ],
                "final_metrics": {"total_steps": 2},
            }
        ]

    change_trial(corpus, "task-pos", setup_workers, file="agent/trajectory.json")
    cand = pairs(report(corpus))["task-pos"]["candidate"]
    assert cand["root_usage"]["input_tokens"] == 200
    assert cand["worker_usage"]["coverage_reason"] == "retained_worker_step_metrics"
    assert cand["total_usage"]["coverage_reason"] == "worker_references_without_usage"
    assert cand["total_usage"]["input_tokens"] is None
    assert cand["total_usage"]["cost_usd"] is None


def test_repeated_references_to_single_present_worker_does_not_double_count(corpus):
    """Multiple observation references to one present worker aggregate metrics once without double-counting."""

    def setup_repeat_ref(data):
        data["steps"] = [
            {"step_id": 1, "source": "user", "message": "prompt"},
            {
                "step_id": 2,
                "source": "agent",
                "message": "call worker 1",
                "llm_call_count": 1,
                "metrics": {"prompt_tokens": 100, "completion_tokens": 20, "cost_usd": 0.002},
                "observation": {
                    "results": [
                        {
                            "content": "first",
                            "subagent_trajectory_ref": [{"trajectory_id": "worker-single"}],
                        }
                    ]
                },
            },
            {
                "step_id": 3,
                "source": "agent",
                "message": "call worker again",
                "llm_call_count": 1,
                "metrics": {"prompt_tokens": 100, "completion_tokens": 30, "cost_usd": 0.003},
                "observation": {
                    "results": [
                        {
                            "content": "second",
                            "subagent_trajectory_ref": [{"trajectory_id": "worker-single"}],
                        }
                    ]
                },
            },
        ]
        data["subagent_trajectories"] = [
            {
                "schema_version": "ATIF-v1.7",
                "session_id": "worker-single",
                "trajectory_id": "worker-single",
                "agent": {"name": "worker", "version": "v1.0", "model_name": "fixture/worker"},
                "steps": [
                    {"step_id": 1, "source": "user", "message": "work"},
                    {
                        "step_id": 2,
                        "source": "agent",
                        "message": "work done",
                        "llm_call_count": 1,
                        "metrics": {
                            "prompt_tokens": 50,
                            "completion_tokens": 15,
                            "cost_usd": 0.001,
                        },
                    },
                ],
                "final_metrics": {"total_steps": 2},
            }
        ]

    change_trial(corpus, "task-pos", setup_repeat_ref, file="agent/trajectory.json")
    cand = pairs(report(corpus))["task-pos"]["candidate"]
    assert cand["root_usage"]["input_tokens"] == 200
    assert cand["root_usage"]["output_tokens"] == 50
    assert cand["worker_usage"]["input_tokens"] == 50
    assert cand["worker_usage"]["output_tokens"] == 15
    assert cand["total_usage"]["input_tokens"] == 250
    assert cand["total_usage"]["output_tokens"] == 65
    assert cand["total_usage"]["cost_usd"] == pytest.approx(0.006)
    assert cand["total_usage"]["coverage_reason"] == "retained_total_step_metrics"


# 9. Nested Source Manifest, Pre/Post Mutation Detection, and Boundary Escapes
def test_nested_worker_trajectories_tracked_in_source_manifest(corpus):
    """Nested worker trajectory files within trial directories are captured in source_inputs_sha256 manifest."""
    task = "task-subagent"
    worker_dir = corpus / "candidate-job" / f"{task}__1" / "agent" / "workers"
    worker_dir.mkdir(parents=True, exist_ok=True)
    worker_file = worker_dir / "worker_nested.json"
    worker_payload = {
        "schema_version": "ATIF-v1.7",
        "session_id": "nested-worker-1",
        "trajectory_id": "nested-worker-1",
        "agent": {"name": "worker", "version": "v1", "model_name": "fixture/open-root"},
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "message": "nested",
                "llm_call_count": 1,
                "metrics": {"prompt_tokens": 200, "completion_tokens": 40, "cost_usd": 0.005},
            }
        ],
    }
    write_json(worker_file, worker_payload)

    main_traj_path = corpus / "candidate-job" / f"{task}__1" / "agent" / "trajectory.json"
    main_traj = json.loads(main_traj_path.read_text())
    main_traj["subagent_trajectories"] = [worker_payload]
    write_json(main_traj_path, main_traj)

    manifest = report(corpus)["metadata"]["source_inputs_sha256"]
    rel_path = f"candidate-job/{task}__1/agent/workers/worker_nested.json"
    assert rel_path in manifest


def test_input_mutation_during_collection_raises_error(corpus, monkeypatch):
    """Any file mutation during the analysis collection window is detected and refused."""
    analysis_mod = consumer.analysis
    original_collect = analysis_mod.collect_cohort_trials

    def mutating_collect(*args, **kwargs):
        res = original_collect(*args, **kwargs)
        target = corpus / "candidate-job" / "task-pos__1" / "result.json"
        data = json.loads(target.read_text())
        data["task_checksum"] = "mutated_checksum_during_window"
        write_json(target, data)
        return res

    monkeypatch.setattr(analysis_mod, "collect_cohort_trials", mutating_collect)
    with pytest.raises(ValueError):
        analyze(corpus / "spec.json", repo_root=corpus, evidence_kind="fixture")


def test_input_addition_during_collection_raises_error(corpus, monkeypatch):
    """Adding a file during the analysis collection window is detected as a manifest mutation."""
    analysis_mod = consumer.analysis
    original_collect = analysis_mod.collect_cohort_trials

    def adding_collect(*args, **kwargs):
        res = original_collect(*args, **kwargs)
        write_json(corpus / "candidate-job" / "task-pos__1" / "agent/added.json", {"added": True})
        return res

    monkeypatch.setattr(analysis_mod, "collect_cohort_trials", adding_collect)
    with pytest.raises(ValueError):
        analyze(corpus / "spec.json", repo_root=corpus, evidence_kind="fixture")


def test_symlink_boundary_escapes_rejected(corpus, tmp_path):
    """Symlinks escaping selected job boundary or repository root are refused."""
    # Symlink escaping selected job boundary
    foreign_symlink = corpus / "candidate-job" / "task-foreign__1"
    foreign_symlink.symlink_to(corpus / "baseline-job" / "task-neg__1")

    spec = json.loads((corpus / "spec.json").read_text())
    spec["cohorts"][1]["paths"] = ["candidate-job"]
    with pytest.raises(ValueError):
        analyze(spec, repo_root=corpus, evidence_kind="fixture")

    # External symlink escaping repo root
    foreign_symlink.unlink()
    external_dir = tmp_path / "outside_repo"
    external_dir.mkdir()
    write_json(external_dir / "result.json", {"id": "outside-1", "task_name": "outside"})
    escaped_symlink = corpus / "candidate-job" / "task-escaped__1"
    escaped_symlink.symlink_to(external_dir)

    with pytest.raises(ValueError):
        analyze(spec, repo_root=corpus, evidence_kind="fixture")


def test_explicit_evidence_kind_contradictions_raise_error(corpus):
    """Explicitly contradictory evidence kinds (historical vs model-run or fixture) raise ValueError."""
    meta_path = corpus / "baseline-job" / "lab-metadata.json"
    meta = json.loads(meta_path.read_text())
    meta["evidence_kind"] = "historical"
    write_json(meta_path, meta)

    with pytest.raises(ValueError):
        analyze(corpus / "spec.json", repo_root=corpus, evidence_kind="model-run")


# 10. Publication Immutability, Renderer Escaping, and Source Evidence Preservation
def test_public_cli_refuses_overlapping_output_and_file_destinations(corpus, tmp_path):
    """Public CLI main() refuses publication paths that overlap source evidence or target regular files."""
    # Output is ancestor / overlap of repository root
    args_overlap = [
        str(corpus / "spec.json"),
        "--repo-root",
        str(corpus),
        "--evidence-kind",
        "fixture",
        "--output",
        str(corpus),
    ]
    with pytest.raises(SystemExit):
        main(args_overlap)

    # Output targets an existing regular file
    file_dest = tmp_path / "regular_file.json"
    file_dest.write_text("existing content")
    args_file = [
        str(corpus / "spec.json"),
        "--repo-root",
        str(corpus),
        "--evidence-kind",
        "fixture",
        "--output",
        str(file_dest),
    ]
    with pytest.raises(SystemExit):
        main(args_file)
    assert file_dest.read_text() == "existing content"


def test_renderer_escaping_and_determinism(corpus):
    """Renderers are strictly deterministic and properly escape table pipes and XML script tags."""
    rep = report(corpus)
    # Determinism
    assert render_markdown_report(rep) == render_markdown_report(rep)
    assert render_svg_plot(rep) == render_svg_plot(rep)

    # Escaping: inject pipe and script in task key
    injection_key = "task|with|pipes&<script>"
    pair = copy.deepcopy(rep["per_task_pairs"][0])
    pair["pairing_key_value"] = injection_key
    rep_injected = copy.deepcopy(rep)
    rep_injected["per_task_pairs"].append(pair)

    md = render_markdown_report(rep_injected)
    assert "task\\|with\\|pipes" in md

    svg = render_svg_plot(rep_injected)
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg
    ET.fromstring(svg)  # Valid XML structure


def test_source_evidence_is_strictly_read_only(corpus):
    """Analysis does not alter any source evidence in repo_root."""
    before_snapshot = {
        p.relative_to(corpus).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(corpus.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }
    analyze(corpus / "spec.json", repo_root=corpus, evidence_kind="fixture")
    after_snapshot = {
        p.relative_to(corpus).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(corpus.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }
    assert before_snapshot == after_snapshot


def test_input_deletion_during_collection_is_refused(corpus, monkeypatch):
    original_collect = consumer.analysis.collect_cohort_trials

    def deleting_collect(*args, **kwargs):
        result = original_collect(*args, **kwargs)
        (corpus / "baseline-job/lab-metadata.json").unlink(missing_ok=True)
        return result

    monkeypatch.setattr(consumer.analysis, "collect_cohort_trials", deleting_collect)
    with pytest.raises(ValueError):
        report(corpus)


def test_known_control_is_not_model_evidence_without_origin_metadata(corpus):
    for job in ("baseline-job", "candidate-job"):
        path = corpus / job / "lab-metadata.json"
        metadata = json.loads(path.read_text())
        metadata.pop("evidence_kind", None)
        write_json(path, metadata)
    change_trial(corpus, "task-pos", lambda data: data["agent_info"].update(name="oracle"))
    with pytest.raises(ValueError):
        analyze(corpus / "spec.json", repo_root=corpus, evidence_kind="model-run")


def test_external_escape_behind_in_root_alias_is_refused_before_collection(
    corpus, tmp_path, monkeypatch
):
    shared = corpus / "shared-agent"
    shared.mkdir()
    external = tmp_path / "outside.json"
    write_json(external, {"outside": True})
    (shared / "escape.json").symlink_to(external)
    (corpus / "candidate-job/task-pos__1/agent/alias").symlink_to(shared, target_is_directory=True)

    def collection_must_not_start(*args, **kwargs):
        raise AssertionError("Input confinement was not checked before collection")

    monkeypatch.setattr(consumer.analysis, "collect_cohort_trials", collection_must_not_start)
    with pytest.raises(ValueError):
        report(corpus)


def test_discovery_directory_manifest_includes_descendant_trial_inputs(corpus):
    spec = json.loads((corpus / "spec.json").read_text())
    for cohort in spec["cohorts"]:
        cohort["paths"] = ["."]
        cohort["trial_names"] = ["task-pos__1"]
    result = analyze(spec, repo_root=corpus, evidence_kind="fixture")
    relative = "candidate-job/task-pos__1/result.json"
    expected = hashlib.sha256((corpus / relative).read_bytes()).hexdigest()
    assert result["metadata"]["source_inputs_sha256"][relative] == expected


def test_retained_step_costs_preserve_sub_micro_precision(corpus):
    def set_small_step_costs(data):
        first = data["steps"][1]
        first["metrics"]["cost_usd"] = 5e-7
        second = copy.deepcopy(first)
        second["step_id"] = 3
        second["metrics"]["cost_usd"] = 7e-7
        data["steps"].append(second)

    change_trial(corpus, "task-pos", set_small_step_costs, file="agent/trajectory.json")
    candidate = pairs(report(corpus))["task-pos"]["candidate"]
    assert candidate["root_usage"]["cost_usd"] == 5e-7 + 7e-7


def test_source_native_conflicting_amounts_remain_unknown(corpus):
    task = "task-native-only"
    payload = {**HAR12_PAYLOAD_TEMPLATE, "atif": False, "root_model": "fixture/open-root"}
    write_json(corpus / f"candidate-job/{task}__1/agent/rlm/root-messages.json", payload)
    change_trial(
        corpus,
        task,
        lambda data: data["agent_result"].update(
            metadata={
                "atif": False,
                "root_model": "fixture/open-root",
                "root_input_tokens": 301,
                "root_calls": 3,
            }
        ),
    )
    candidate = pairs(report(corpus))[task]["candidate"]
    assert candidate["root_usage"]["input_tokens"] is None
    assert candidate["root_usage"]["output_tokens"] == 150
    assert candidate["source_native_accounting"]["root_calls"] is None


def test_source_native_unknown_and_floating_revisions_stay_unknown(corpus):
    task = "task-native-only"
    payload = {**HAR12_PAYLOAD_TEMPLATE, "atif": False, "root_model_revision": "main"}
    write_json(corpus / f"candidate-job/{task}__1/agent/rlm/root-messages.json", payload)
    change_trial(
        corpus,
        task,
        lambda data: data["agent_result"].update(
            metadata={
                "atif": False,
                "root_model_revision": "unknown",
            }
        ),
    )
    pair = pairs(report(corpus))[task]
    assert pair["candidate"]["model_revision"] is None
    assert pair["qualification"]["is_qualified"]
    assert pair["qualification"]["model_qualification"] == "unknown_revision"


def test_source_native_conflicting_scope_flags_do_not_establish_root_usage(corpus):
    task = "task-native-only"
    payload = {**HAR12_PAYLOAD_TEMPLATE, "atif": False}
    write_json(corpus / f"candidate-job/{task}__1/agent/rlm/root-messages.json", payload)
    change_trial(
        corpus,
        task,
        lambda data: data["agent_result"].update(metadata={"atif": True}),
    )
    candidate = pairs(report(corpus))[task]["candidate"]
    assert candidate["root_usage"]["input_tokens"] is None
    assert candidate["total_usage"]["input_tokens"] is None
