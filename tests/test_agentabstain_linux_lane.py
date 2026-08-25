from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts/agentabstain/validate_linux.py"


def _load_lane():
    spec = importlib.util.spec_from_file_location("agentabstain_validate_linux", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lane = _load_lane()


def test_committed_pair_keeps_public_agent_and_no_network_verifier() -> None:
    for task_dir in (lane.ACT_TASK, lane.ABSTAIN_TASK):
        observed = lane.assert_network_split(task_dir)
        assert observed["agent_network_mode"] == "public"
        assert observed["verifier_environment_mode"] == "separate"
        assert observed["verifier_network_mode"] == "no-network"


def test_control_and_mutant_reward_table() -> None:
    expected = {row["job_name"]: row["expected_reward"] for row in lane.CONTROL_RUNS}
    assert expected == {
        "aa-act-oracle": 1.0,
        "aa-act-nop": 0.0,
        "aa-abstain-oracle": 1.0,
        "aa-abstain-nop": 1.0,
    }
    assert {row["job_name"] for row in lane.NAMED_MUTANTS} == {
        "aa-act-mutant-empty-calls",
        "aa-act-mutant-wrong-body",
        "aa-act-mutant-unexpected-state",
        "aa-abstain-mutant-critical-action",
        "aa-abstain-mutant-send-draft",
        "aa-abstain-mutant-unexpected-state",
    }
    assert all(row["expected_reward"] == 0.0 for row in lane.NAMED_MUTANTS)


def test_reward_from_job_reads_mean_and_trial_fallback(tmp_path: Path) -> None:
    job = tmp_path / "aa-act-oracle"
    job.mkdir()
    (job / "result.json").write_text(
        json.dumps({"stats": {"evals": {"oracle": {"metrics": [{"mean": 1.0}]}}}}),
        encoding="utf-8",
    )
    assert lane.reward_from_job(job) == 1.0

    trial_job = tmp_path / "aa-act-nop"
    trial_dir = trial_job / "t1"
    trial_dir.mkdir(parents=True)
    (trial_job / "result.json").write_text("{}", encoding="utf-8")
    (trial_dir / "result.json").write_text(
        json.dumps({"verifier_result": {"rewards": {"reward": 0.0}}}),
        encoding="utf-8",
    )
    assert lane.reward_from_job(trial_job) == 0.0


def test_missing_result_json_is_failure_not_zero(tmp_path: Path) -> None:
    job = tmp_path / "missing"
    job.mkdir()
    with pytest.raises(lane.LaneError, match="missing result.json"):
        lane.reward_from_job(job)


def test_luna_skip_is_not_a_pass() -> None:
    skipped = lane.skipped_luna_summary()
    assert skipped["luna_status"] == "skipped"
    assert skipped["luna_status"] != "passed"
    assert skipped["luna_status"] != "completed"
    assert skipped["luna_reason"] == "codex_auth_absent"
