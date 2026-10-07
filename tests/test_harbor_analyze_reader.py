"""Parser regression tests backed by native Harbor 0.24 reader outputs."""

import json
import os
from pathlib import Path

import pytest

from evallab.readers.harbor_analyze import (
    ANALYZE_CHECKS,
    _optional_trial_task,
    configure_evaluator,
    flash_cost,
    parse_checks,
    reader_usage,
    resolve_trial_task,
    score_binary,
    stage_task,
    stage_trial,
    write_budget_verdict,
)

FIXTURES = Path(__file__).parent / "fixtures" / "harbor_analyze"


def test_harbor_selects_openai_provider_credential(monkeypatch):
    # Regression for mini 2.4: sending only MSWEA_API_KEY leaves LiteLLM unauthenticated.
    monkeypatch.setenv("ZAI_OPENAPI_API_KEY", "unit-test-credential")
    monkeypatch.setenv("MSWEA_API_KEY", "old-unit-test-credential")
    for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE"):
        monkeypatch.setenv(key, "old-unit-test-value")
    configure_evaluator()
    assert "MSWEA_API_KEY" not in os.environ
    assert os.environ["OPENAI_API_KEY"] == "unit-test-credential"
    assert os.environ["OPENAI_API_BASE"] == "https://api.z.ai/api/paas/v4"
    with pytest.raises(ValueError, match="Unpriced"):
        configure_evaluator(model="openai/unpriced-model")


def test_native_reader_failure_abstains_instead_of_reporting_clean():
    row = json.loads((FIXTURES / "failure.json").read_text())["result"]
    checks, explanations = parse_checks(row, ANALYZE_CHECKS)
    assert checks == {"reward_hacking": None, "task_specification": None}
    assert all(value == row["error"] for value in explanations.values())


def test_real_native_outcomes_and_usage():
    fixture = json.loads((FIXTURES / "success.json").read_text())
    checks, explanations = parse_checks(fixture["result"], ANALYZE_CHECKS)
    assert checks == {"reward_hacking": True, "task_specification": False}
    assert all(len(value) == 300 for value in explanations.values())
    usage = reader_usage({"agent_result": fixture["agent_result"]})
    assert usage["tokens"] == {"input": 413387, "output": 11108}
    assert usage["cost_usd"] == pytest.approx(fixture["result"]["cost_usd"])


def test_real_task_quality_report_preserves_not_applicable():
    row = json.loads((FIXTURES / "check_success.json").read_text())["result"]
    checks, explanations = parse_checks(row)
    assert len(checks) == 11
    assert checks["anti_cheating_measures"] is False
    assert checks["structured_data_schema"] is None
    assert checks["hardcoded_solution"] is None
    assert checks["file_reference_mentioned"] is None
    assert all(len(value) <= 300 for value in explanations.values())


def test_task_resolution_and_staging_never_modify_original_trial(tmp_path):
    repo = tmp_path / "repo"
    prepared = repo / "runs" / ".prepared-tasks" / "task"
    prepared.mkdir(parents=True)
    (prepared / "instruction.md").write_text("Task instruction")
    trial = tmp_path / "results" / "job" / "trial"
    trial.mkdir(parents=True)
    config = {"task": {"path": str(repo / "runs" / ".exec-stage" / "task")}}
    original_result = json.dumps({"config": config})
    (trial / "result.json").write_text(original_result)
    (trial / "config.json").write_text(json.dumps(config))
    (trial.parent / "experiment-spec.json").write_text(
        json.dumps({"task_path": "runs/.prepared-tasks/task"})
    )
    assert resolve_trial_task(trial) == prepared
    staged = stage_trial(trial, tmp_path / "scratch", prepared)
    assert json.loads((staged / "result.json").read_text())["config"]["task"]["path"] == str(
        prepared
    )
    assert (trial / "result.json").read_text() == original_result


def test_blind_copy_withholds_labels_but_preserves_native_evidence(tmp_path):
    trial = tmp_path / "results" / "job" / "trial"
    (trial / "verifier").mkdir(parents=True)
    rewards = {"reward": 1.0, "integrity": 0, "reward_gated": 0.0}
    (trial / "result.json").write_text(json.dumps({"verifier_result": {"rewards": rewards}}))
    (trial / "verifier" / "reward.json").write_text(json.dumps(rewards))
    withheld = (
        "verifier/reward-details.json",
        "processed/labels.json",
        "watch/analysis.json",
        ".evallab-labels.json",
        "verifier/.evallab-labels.json",
        "laminar-trace.json",
    )
    retained = ("agent/trajectory.json", "verifier/test-stdout.txt", "verifier/agent.diff")
    test_output = "test_example PASSED\nreward: 1.0\nintegrity: 0\nreward_gated: 0.0\n"
    for name in withheld + retained:
        path = trial / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(test_output if name == "verifier/test-stdout.txt" else name)
    staged = stage_trial(trial, tmp_path / "scratch")
    assert json.loads((staged / "result.json").read_text())["verifier_result"]["rewards"] == {
        "reward": 1.0
    }
    assert json.loads((staged / "verifier" / "reward.json").read_text()) == {"reward": 1.0}
    assert all(not (staged / name).exists() for name in withheld)
    assert (staged / "agent/trajectory.json").read_text() == "agent/trajectory.json"
    assert (staged / "verifier/agent.diff").read_text() == "verifier/agent.diff"
    assert (staged / "verifier/test-stdout.txt").read_text() == "test_example PASSED\nreward: 1.0\n"
    assert (trial / "verifier/test-stdout.txt").read_text() == test_output
    assert json.loads((trial / "result.json").read_text())["verifier_result"]["rewards"] == rewards


def test_independent_task_withholds_detector_but_keeps_core_verifier(tmp_path):
    task = tmp_path / "original-task"
    withheld = (
        "tests/vendor/run_integrity.py",
        "tests/vendor/integrity_core.py",
        "tests/vendor/copy_check_vendored.py",
        "tests/integrity/copy_check_v1.py",
        "tests/integrity/reward.toml",
        "tests/reward_gated/gated.py",
        "tests/vendor/__pycache__/integrity_core.cpython-312.pyc",
        ".evallab-labels.json",
    )
    retained = (
        "instruction.md",
        "task.toml",
        "tests/test.patch",
        "tests/test_command.sh",
        "tests/reward/raw_reward.py",
    )
    for name in withheld + retained:
        path = task / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    core = "#!/bin/bash\necho 1 > /logs/verifier/reward.txt\n"
    original = (
        core + "# --- rewardkit-integrity@1: appended ---\npython3 /tests/vendor/run_integrity.py\n"
    )
    (task / "tests/test.sh").write_text(original)
    staged = stage_task(task, tmp_path / "results/job/trial", tmp_path / "scratch")
    assert all(not (staged / name).exists() for name in withheld)
    assert all((staged / name).read_text() == name for name in retained)
    assert (staged / "tests/test.sh").read_text() == core
    assert (task / "tests/test.sh").read_text() == original
    assert all((task / name).read_text() == name for name in withheld)


def test_unadmitted_check_is_explicitly_budget_capped(tmp_path):
    pytest.importorskip("harbor.analyze", reason="Harbor 0.24 rubric (laminar extra)")
    path = write_budget_verdict(
        task_name="mimo-v2.6-rl/format-code-task-000792",
        source_name="prepared-task",
        source_job="tasks",
        root=tmp_path,
    )
    verdict = json.loads(path.read_text())
    assert verdict["checks"] and all(value is None for value in verdict["checks"].values())
    assert verdict["error"] == "budget cap"
    assert verdict["tokens"] == {"input": 0, "output": 0}
    assert verdict["cost_usd"] == 0.0
    assert json.loads((tmp_path / verdict["raw"]).read_text()) == {
        "admitted": False,
        "error": "budget cap",
    }


def test_unknown_outcome_is_not_silently_a_negative():
    with pytest.raises(ValueError, match="Invalid Harbor outcome"):
        parse_checks({"checks": {"reward_hacking": {"outcome": "unknown"}}})


def test_usage_comes_from_real_agent_totals_not_source_trial_usage():
    result = {
        "agent_result": {
            "n_input_tokens": 20_000,
            "n_output_tokens": 1_000,
            "n_cache_tokens": 5_000,
            "cost_usd": 0,
        }
    }
    usage = reader_usage(result)
    assert usage["tokens"] == {"input": 20_000, "output": 1_000}
    assert usage["cost_usd"] == pytest.approx(0.0029)
    assert flash_cost(20_000, 1_000, 5_000) == pytest.approx(0.0029)
    assert reader_usage({"agent_result": None})["cost_usd"] is None


def test_binary_scores_keep_missing_and_abstentions_out_of_negatives():
    verdicts = [
        {"trial": "copied", "checks": {"reward_hacking": True}},
        {"trial": "clean", "checks": {"reward_hacking": False}},
        {"trial": "undecided", "checks": {"reward_hacking": None}},
    ]
    score = score_binary(
        verdicts, {"copied": True, "clean": False, "undecided": True, "missing": False}
    )
    assert score == {
        "check": "reward_hacking",
        "n_labels": 4,
        "tp": 1,
        "fn": 0,
        "fp": 0,
        "tn": 1,
        "abstained": 1,
        "missing": 1,
        "recall": 1.0,
        "fpr": 0.0,
    }


def test_taskless_trial_resolves_no_task_but_stages_cleanly(tmp_path):
    trial = tmp_path / "results" / "job" / "tw-0001"
    (trial / "agent").mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps(
            {
                "task_name": "tw-task",
                "trial_name": "tw-0001",
                "verifier_result": {"rewards": {"reward": 1.0}},
            }
        )
    )
    (trial / "trial.log").write_text("started\n")
    (trial / "agent/trajectory.json").write_text(
        json.dumps({"schema_version": "ATIF-v1.6", "steps": []})
    )
    assert _optional_trial_task(trial) is None
    staged = stage_trial(trial, tmp_path / "scratch", None)
    assert (staged / "trial.log").exists()
    assert (staged / "agent/trajectory.json").exists()
    assert json.loads((staged / "result.json").read_text())["verifier_result"]["rewards"] == {
        "reward": 1.0
    }
