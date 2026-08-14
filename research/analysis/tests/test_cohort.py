from __future__ import annotations

import json
from pathlib import Path

import pytest

from harbor_lab.cohort import compare, wilson_interval, write_comparison
from harbor_lab.schemas import CohortComparisonSpec

from .test_atif import _make_job

ROOT = Path(__file__).resolve().parents[3]


def _control_spec(*, mode: str = "causal") -> CohortComparisonSpec:
    return CohortComparisonSpec.model_validate(
        {
            "schema_version": 1,
            "comparison_id": "event-summary-oracle-vs-nop",
            "experiment_id": "event-summary-local-controls",
            "declared_variable": "agent_name",
            "mode": mode,
            "reward_name": "reward",
            "pass_threshold": 1.0,
            "pass_k": [1],
            "pairing_key": "task_digest",
            "cohorts": [
                {
                    "label": "oracle",
                    "paths": ["evidence/runs/event-summary-oracle-evidence"],
                },
                {
                    "label": "nop",
                    "paths": ["evidence/runs/event-summary-nop-evidence"],
                },
            ],
        }
    )


def _synthetic_job(
    root: Path,
    *,
    suffix: int,
    agent: str,
    reward: float,
    environment: str = "docker",
    task_digest: str = "sha256:task",
    exception: str | None = None,
) -> Path:
    job = _make_job(root, with_trajectory=False)
    job_result_path = job / "result.json"
    job_result = json.loads(job_result_path.read_text())
    job_result["id"] = f"00000000-0000-0000-0000-{suffix:012d}"
    job_result_path.write_text(json.dumps(job_result))
    trial = next(path for path in job.iterdir() if path.is_dir())
    trial_result_path = trial / "result.json"
    trial_result = json.loads(trial_result_path.read_text())
    trial_result["id"] = f"10000000-0000-0000-0000-{suffix:012d}"
    trial_result["agent_info"]["name"] = agent
    trial_result["verifier_result"]["rewards"]["reward"] = reward
    trial_result["exception_info"] = (
        {"exception_type": exception, "exception_message": "fixture"}
        if exception
        else None
    )
    trial_result_path.write_text(json.dumps(trial_result))
    trial_lock_path = trial / "lock.json"
    trial_lock = json.loads(trial_lock_path.read_text())
    trial_lock["task"]["digest"] = task_digest
    trial_lock["agent"]["name"] = agent
    trial_lock["environment"]["type"] = environment
    trial_lock_path.write_text(json.dumps(trial_lock))
    return job


def _synthetic_spec(
    *,
    left_paths: list[str],
    right_paths: list[str],
    mode: str = "causal",
) -> CohortComparisonSpec:
    return CohortComparisonSpec.model_validate(
        {
            "schema_version": 1,
            "comparison_id": "synthetic-comparison",
            "experiment_id": "synthetic-experiment",
            "declared_variable": "agent_name",
            "mode": mode,
            "pass_k": [1],
            "cohorts": [
                {"label": "left", "paths": left_paths},
                {"label": "right", "paths": right_paths},
            ],
        }
    )


def test_wilson_interval_known_bounds() -> None:
    assert wilson_interval(0, 0) is None
    lower, upper = wilson_interval(1, 1) or (0.0, 0.0)
    assert lower == pytest.approx(0.20654931437723745)
    assert upper == pytest.approx(1.0)


def test_existing_oracle_vs_nop_is_single_variable_paired_comparison() -> None:
    report = compare(_control_spec(), repo_root=ROOT)

    assert report["validity_warnings"] == []
    oracle, nop = report["cohorts"]
    assert oracle["capability_denominator"] == 1
    assert oracle["pass_at_k"][0]["passes"] == 1
    assert oracle["pass_at_k"][0]["denominator"] == 1
    assert nop["pass_at_k"][0]["passes"] == 0
    assert nop["pass_at_k"][0]["denominator"] == 1
    assert report["paired"][0]["n_pairs"] == 1
    assert report["paired"][0]["mean_reward_delta"] == -1.0


def test_comparison_output_is_deterministic_and_machine_readable(tmp_path: Path) -> None:
    spec_path = tmp_path / "comparison.json"
    spec_path.write_text(_control_spec().model_dump_json(indent=2))

    json_path, markdown_path, first = write_comparison(
        spec_path,
        repo_root=ROOT,
        output_root=tmp_path / "reports",
    )
    first_json = json_path.read_bytes()
    first_markdown = markdown_path.read_bytes()
    _, _, second = write_comparison(
        spec_path,
        repo_root=ROOT,
        output_root=tmp_path / "reports",
    )

    assert first == second
    assert json_path.read_bytes() == first_json
    assert markdown_path.read_bytes() == first_markdown
    assert json.loads(first_json)["comparison_id"] == "event-summary-oracle-vs-nop"
    assert b"capability denominator" in first_markdown


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("environment", "daytona", "environment_digest"),
        ("task_digest", "sha256:different", "task_digest"),
    ],
)
def test_causal_comparison_refuses_invariant_or_second_variable_difference(
    tmp_path: Path,
    field: str,
    value: str,
    match: str,
) -> None:
    _synthetic_job(tmp_path / "left", suffix=1, agent="oracle", reward=1.0)
    kwargs = {field: value}
    _synthetic_job(tmp_path / "right", suffix=2, agent="nop", reward=0.0, **kwargs)
    spec = _synthetic_spec(
        left_paths=["left/sample-job"],
        right_paths=["right/sample-job"],
    )

    with pytest.raises(ValueError, match=match):
        compare(spec, repo_root=tmp_path)


def test_exploratory_comparison_carries_validity_warnings() -> None:
    spec = _control_spec(mode="exploratory").model_copy(
        update={"declared_variable": "environment_digest"}
    )

    report = compare(spec, repo_root=ROOT)

    assert report["mode"] == "exploratory"
    assert any("declared variable" in warning for warning in report["validity_warnings"])
    assert any("agent_name" in warning for warning in report["validity_warnings"])


def test_exceptions_are_reported_beside_but_excluded_from_denominator(
    tmp_path: Path,
) -> None:
    _synthetic_job(tmp_path / "left-pass", suffix=1, agent="oracle", reward=1.0)
    _synthetic_job(
        tmp_path / "left-error",
        suffix=2,
        agent="oracle",
        reward=0.0,
        exception="AgentTimeoutError",
    )
    _synthetic_job(tmp_path / "right", suffix=3, agent="nop", reward=0.0)
    spec = _synthetic_spec(
        left_paths=["left-pass/sample-job", "left-error/sample-job"],
        right_paths=["right/sample-job"],
    )

    report = compare(spec, repo_root=tmp_path)

    left = report["cohorts"][0]
    assert left["n_total"] == 2
    assert left["capability_denominator"] == 1
    assert left["exception_count"] == 1
    assert left["exceptions"] == {"AgentTimeoutError": 1}
    assert left["pass_at_k"][0]["denominator"] == 1


def test_pass_at_one_uses_trials_and_pass_at_two_uses_task_groups(tmp_path: Path) -> None:
    _synthetic_job(tmp_path / "left-1", suffix=1, agent="oracle", reward=1.0)
    _synthetic_job(tmp_path / "left-2", suffix=2, agent="oracle", reward=0.0)
    _synthetic_job(tmp_path / "right-1", suffix=3, agent="nop", reward=0.0)
    _synthetic_job(tmp_path / "right-2", suffix=4, agent="nop", reward=0.0)
    spec = _synthetic_spec(
        left_paths=["left-1/sample-job", "left-2/sample-job"],
        right_paths=["right-1/sample-job", "right-2/sample-job"],
    ).model_copy(update={"pass_k": [1, 2]})

    report = compare(spec, repo_root=tmp_path)

    left = report["cohorts"][0]
    assert left["pass_at_k"][0]["selection"] == "all-exception-free-scored-trials"
    assert left["pass_at_k"][0]["passes"] == 1
    assert left["pass_at_k"][0]["denominator"] == 2
    assert left["pass_at_k"][1]["selection"] == "first-k-by-trial-id-per-pairing-key"
    assert left["pass_at_k"][1]["passes"] == 1
    assert left["pass_at_k"][1]["denominator"] == 1
