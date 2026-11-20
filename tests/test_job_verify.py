"""Reward-integrity verification over synthetic Harbor job directories."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab.job_verify import exit_code, verify_job, verify_jobs


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _job(
    root: Path,
    *,
    rewards: dict[str, Any] | None = None,
    file_rewards: dict[str, Any] | None = None,
    n_total: int = 1,
    stats_rewards: dict[str, Any] | None = None,
    trial_name: str = "task-a__abc1234",
    seat_results: list[dict[str, Any]] | None = None,
    participants: list[dict[str, Any]] | None = None,
) -> Path:
    job = root / "job"
    trial = job / trial_name
    rewards = {"reward": 1.0} if rewards is None else rewards
    result: dict[str, Any] = {
        "id": "trial-1",
        "task_name": "task-a",
        "trial_name": trial_name,
        "task_checksum": "checksum-namespace-value",
        "verifier_result": {"rewards": rewards},
        "exception_info": None,
    }
    if seat_results is not None:
        result["seat_results"] = seat_results
    if participants is not None:
        result["config"] = {"participants": participants}
    _write(trial / "result.json", result)
    if file_rewards is not None:
        _write(trial / "verifier" / "reward.json", file_rewards)
    scored = stats_rewards if stats_rewards is not None else rewards
    reward_stats = {
        str(name): {str(value): [trial_name]}
        for name, value in scored.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    _write(
        job / "result.json",
        {
            "id": "job-1",
            "finished_at": "2026-11-18T00:00:00",
            "n_total_trials": n_total,
            "stats": {
                "evals": {
                    "oracle__adhoc": {
                        "n_trials": 1,
                        "n_errors": 0,
                        "metrics": [scored],
                        "pass_at_k": {},
                        "reward_stats": reward_stats,
                        "exception_stats": {},
                    }
                }
            },
        },
    )
    _write(job / "config.json", {})
    _write(
        job / "lock.json",
        {
            "schema_version": 3,
            "harbor": {"version": "0.24.0"},
            "trials": [{"task": {"name": "task-a", "digest": "sha256:lock-namespace-value"}}],
        },
    )
    return job


def _codes(report) -> list[str]:
    return [finding.code for finding in report.findings]


def test_clean_job_passes(tmp_path: Path) -> None:
    report = verify_job(_job(tmp_path, file_rewards={"reward": 1.0}))
    assert report.verdict == "pass"
    assert report.trials_indexed == 1
    assert "task_digest_namespace" in _codes(report)
    assert exit_code(verify_jobs([report.path])) == 0


@pytest.mark.parametrize(
    ("reward", "stored_reward"),
    [(1, 1), (1.0, 1), (1, 1.0), (0, -0.0), (10**20, 1e20), (2**53 + 1, 2**53 + 1)],
)
def test_equivalent_job_stats_reward_keys_pass(
    tmp_path: Path, reward: int | float, stored_reward: int | float
) -> None:
    report = verify_job(
        _job(tmp_path, rewards={"reward": reward}, stats_rewards={"reward": stored_reward})
    )
    assert report.verdict == "pass"


def test_bool_reward_blocks(tmp_path: Path) -> None:
    report = verify_job(_job(tmp_path, rewards={"reward": True}, stats_rewards={}))
    assert report.verdict == "block"
    assert "reward_bool" in _codes(report)


def test_minus_one_sentinel_blocks(tmp_path: Path) -> None:
    report = verify_job(_job(tmp_path, rewards={"reward": -1}, stats_rewards={}))
    assert report.verdict == "block"
    assert "reward_unscored_sentinel" in _codes(report)


def test_string_reward_blocks(tmp_path: Path) -> None:
    report = verify_job(_job(tmp_path, rewards={"reward": "1"}, stats_rewards={}))
    assert report.verdict == "block"
    assert "reward_non_numeric" in _codes(report)


def test_stale_verifier_file_blocks(tmp_path: Path) -> None:
    report = verify_job(
        _job(tmp_path, rewards={"reward": 0.0}, file_rewards={"reward": 1.0})
    )
    assert report.verdict == "block"
    assert "reward_mismatch_result_vs_file" in _codes(report)


@pytest.mark.parametrize(("reward", "stored_reward"), [(1, 2), (2**53, 2**53 + 1)])
def test_desynced_job_stats_block(tmp_path: Path, reward: int, stored_reward: int) -> None:
    report = verify_job(
        _job(tmp_path, rewards={"reward": reward}, stats_rewards={"reward": stored_reward})
    )
    assert report.verdict == "block"
    assert "job_stats_reward_desync" in _codes(report)


def test_trial_count_mismatch_blocks(tmp_path: Path) -> None:
    report = verify_job(_job(tmp_path, n_total=2))
    assert report.verdict == "block"
    assert "trial_count_mismatch" in _codes(report)


def test_missing_trial_result_blocks_but_job_still_readable(tmp_path: Path) -> None:
    job = _job(tmp_path)
    orphan = job / "task-a__orphan1"
    _write(orphan / "lock.json", {})
    report = verify_job(job)
    assert report.verdict == "block"
    assert "trial_result_missing" in _codes(report)
    assert report.trials_indexed == 1


def test_multi_dim_rewards_explain_empty_pass_at_k(tmp_path: Path) -> None:
    rewards = {"correctness": 1.0, "reward": 1.0}
    report = verify_job(_job(tmp_path, rewards=rewards))
    assert report.verdict == "pass"
    infos = [f for f in report.findings if f.code == "pass_at_k_empty"]
    assert infos and infos[0].severity == "info"


def test_exceptioned_trial_is_excluded_from_eval_count(tmp_path: Path) -> None:
    job = _job(tmp_path, n_total=2)
    errored = job / "task-a__err0001"
    _write(
        errored / "result.json",
        {
            "id": "trial-2",
            "task_name": "task-a",
            "trial_name": "task-a__err0001",
            "verifier_result": None,
            "exception_info": {"exception_type": "NonZeroAgentExitCodeError"},
        },
    )
    report = verify_job(job)
    assert report.verdict == "pass"
    assert "reward_missing_exceptioned" in _codes(report)
    assert "job_stats_trial_count_desync" not in _codes(report)


_SEATS = [{"seat": "p1"}, {"seat": "p2"}]


def test_clean_seat_trial_passes(tmp_path: Path) -> None:
    report = verify_job(
        _job(
            tmp_path,
            seat_results=[
                {"seat": "p1", "under_test": True, "logs_downloaded": True},
                {"seat": "p2", "under_test": False, "logs_downloaded": True},
            ],
            participants=_SEATS,
        )
    )
    assert report.verdict == "pass"
    assert not [code for code in _codes(report) if code.startswith("seat_")]


def test_seat_results_missing_blocks(tmp_path: Path) -> None:
    report = verify_job(_job(tmp_path, participants=_SEATS))
    assert report.verdict == "block"
    assert "seat_results_missing" in _codes(report)


def test_seat_participant_mismatch_blocks(tmp_path: Path) -> None:
    report = verify_job(
        _job(
            tmp_path,
            seat_results=[{"seat": "p1", "under_test": True}],
            participants=_SEATS,
        )
    )
    assert report.verdict == "block"
    assert "seat_participant_mismatch" in _codes(report)


def test_seat_exception_and_missing_logs_warn(tmp_path: Path) -> None:
    report = verify_job(
        _job(
            tmp_path,
            seat_results=[
                {"seat": "p1", "under_test": True, "logs_downloaded": True},
                {
                    "seat": "p2",
                    "under_test": False,
                    "logs_downloaded": False,
                    "exception_info": {"exception_type": "AgentTimeoutError"},
                },
            ],
            participants=_SEATS,
        )
    )
    assert report.verdict == "pass"
    assert "seat_exception_beside_reward" in _codes(report)
    assert "seat_logs_missing" in _codes(report)


def test_per_seat_reward_keys_without_reward_key_pass(tmp_path: Path) -> None:
    rewards = {"p1": 1.0, "p2": 0.0}
    report = verify_job(
        _job(
            tmp_path,
            rewards=rewards,
            file_rewards=rewards,
            seat_results=[
                {"seat": "p1", "under_test": True},
                {"seat": "p2", "under_test": True},
            ],
            participants=_SEATS,
        )
    )
    assert report.verdict == "pass"
    assert "reward_missing" not in _codes(report)


def test_not_a_job_is_error(tmp_path: Path) -> None:
    report = verify_job(tmp_path)
    assert report.verdict == "error"
    assert exit_code(verify_jobs([tmp_path])) == 2


def test_exit_codes(tmp_path: Path) -> None:
    clean = _job(tmp_path / "a")
    blocked = _job(tmp_path / "b", rewards={"reward": True}, stats_rewards={})
    assert exit_code(verify_jobs([clean])) == 0
    assert exit_code(verify_jobs([clean, blocked])) == 1
    assert exit_code(verify_jobs([])) == 2
