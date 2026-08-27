from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.cohort import (
    NOT_COMPARABLE,
    compare,
    pass_at_k_probability,
    pass_at_k_unbiased,
    pass_power_k_unbiased,
    wilson_interval,
    write_comparison,
)
from evallab.schemas import CohortComparisonSpec

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
                    "paths": ["research/evidence/runs/event-summary-oracle-evidence"],
                },
                {
                    "label": "nop",
                    "paths": ["research/evidence/runs/event-summary-nop-evidence"],
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
    started_at: str | None = None,
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
        {"exception_type": exception, "exception_message": "fixture"} if exception else None
    )
    if started_at is None:
        trial_result["started_at"] = f"2026-08-14T00:00:{suffix:02d}.000000Z"
    elif started_at == "":
        trial_result.pop("started_at", None)
    else:
        trial_result["started_at"] = started_at
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
    assert oracle["pass_any_first_k"][0]["passes"] == 1
    assert oracle["pass_any_first_k"][0]["denominator"] == 1
    assert nop["pass_any_first_k"][0]["passes"] == 0
    assert nop["pass_any_first_k"][0]["denominator"] == 1
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
        ("task_digest", "sha256:different", "eligible task"),
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

    report = compare(spec, repo_root=tmp_path)

    assert report["paired"][0]["statement"].startswith(NOT_COMPARABLE)
    assert match in report["paired"][0]["statement"]


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
    assert left["pass_any_first_k"][0]["denominator"] == 1


def test_pass_at_one_and_pass_at_two_both_use_task_groups(tmp_path: Path) -> None:
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
    assert left["pass_any_first_k"][0]["selection"] == "first-k-by-started-at-per-task"
    assert left["pass_any_first_k"][0]["passes"] == 1
    assert left["pass_any_first_k"][0]["denominator"] == 1
    assert left["pass_any_first_k"][1]["selection"] == "first-k-by-started-at-per-task"
    assert left["pass_any_first_k"][1]["passes"] == 1
    assert left["pass_any_first_k"][1]["denominator"] == 1


def _assert_no_legacy_realized_keys(value: object) -> None:
    forbidden = {
        "pass_at_k",
        "pass_power_k",
        "mean_pass_at_k_delta",
        "mean_pass_power_k_delta",
        "pass_power_k_bootstrap_95",
        "pass_power_k_wins",
        "pass_power_k_ties",
        "pass_power_k_losses",
        "pass_at_k_delta",
        "pass_power_k_delta",
        "first-k-by-trial-id-per-task",
    }
    allowed_unbiased = {"pass_at_k_unbiased", "pass_power_k_unbiased"}
    if isinstance(value, dict):
        for key, item in value.items():
            assert key not in forbidden or key in allowed_unbiased
            if isinstance(key, str):
                assert "first-k-by-trial-id" not in key
            _assert_no_legacy_realized_keys(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_legacy_realized_keys(item)


@pytest.mark.parametrize(
    ("n", "c", "k", "expected"),
    [
        (5, 3, 1, 0.6),
        (5, 3, 2, 0.9),
        (5, 3, 3, 1.0),
        (2, 1, 3, None),
        (5, 0, 1, 0.0),
        (5, 5, 2, 1.0),
    ],
)
def test_pass_at_k_unbiased_domain(n: int, c: int, k: int, expected: float | None) -> None:
    value = pass_at_k_unbiased(n, c, k)
    if expected is None:
        assert value is None
    else:
        assert value == pytest.approx(expected)


def test_pass_at_k_unbiased_preserves_rare_success_at_large_n() -> None:
    assert pass_at_k_unbiased(10**20, 1, 1) == 1e-20


@pytest.mark.parametrize(
    ("n", "c", "k", "expected"),
    [
        (5, 3, 1, 0.6),
        (5, 3, 2, 0.3),
        (5, 3, 3, 0.1),
        (5, 2, 3, 0.0),
        (2, 1, 3, None),
    ],
)
def test_pass_power_k_unbiased_domain(n: int, c: int, k: int, expected: float | None) -> None:
    value = pass_power_k_unbiased(n, c, k)
    if expected is None:
        assert value is None
    else:
        assert value == pytest.approx(expected)


@pytest.mark.parametrize("fn", [pass_at_k_unbiased, pass_power_k_unbiased])
@pytest.mark.parametrize(
    "args",
    [
        (True, 1, 1),
        (1, True, 1),
        (1, 1, True),
        (1.0, 1, 1),
        (-1, 0, 1),
        (1, -1, 1),
        (1, 2, 1),
        (1, 0, 0),
    ],
)
def test_unbiased_helpers_reject_invalid_inputs(fn, args) -> None:
    with pytest.raises(ValueError):
        fn(*args)


def test_unbiased_averages_per_task_and_does_not_pool(tmp_path: Path) -> None:
    # Task A: n=3,c=1 -> Chen 2/3; Task B: n=3,c=3 -> 1.0; mean 5/6.
    # Pooled n=6,c=4,k=2 -> 14/15 must not appear.
    rewards_a = [1.0, 0.0, 0.0]
    rewards_b = [1.0, 1.0, 1.0]
    for index, reward in enumerate(rewards_a, start=1):
        _synthetic_job(
            tmp_path / f"left-a-{index}",
            suffix=index,
            agent="oracle",
            reward=reward,
            task_digest="sha256:task-a",
        )
    for index, reward in enumerate(rewards_b, start=1):
        _synthetic_job(
            tmp_path / f"left-b-{index}",
            suffix=10 + index,
            agent="oracle",
            reward=reward,
            task_digest="sha256:task-b",
        )
    _synthetic_job(
        tmp_path / "right-a", suffix=20, agent="nop", reward=0.0, task_digest="sha256:task-a"
    )
    _synthetic_job(
        tmp_path / "right-b", suffix=21, agent="nop", reward=0.0, task_digest="sha256:task-b"
    )
    spec = _synthetic_spec(
        left_paths=[f"left-a-{i}/sample-job" for i in range(1, 4)]
        + [f"left-b-{i}/sample-job" for i in range(1, 4)],
        right_paths=["right-a/sample-job", "right-b/sample-job"],
    ).model_copy(update={"pass_k": [2]})

    report = compare(spec, repo_root=tmp_path)
    metric = report["cohorts"][0]["pass_at_k_unbiased"][0]
    assert metric["selection"] == "all-eligible-attempts-per-task-unbiased"
    assert metric["n_tasks"] == 2
    assert metric["denominator"] == 2
    assert metric["rate"] == pytest.approx(5 / 6)
    assert metric["task_estimates"]["sha256:task-a"] == pytest.approx(2 / 3)
    assert metric["task_estimates"]["sha256:task-b"] == pytest.approx(1.0)
    assert metric["rate"] != pytest.approx(14 / 15)
    power = report["cohorts"][0]["pass_power_k_unbiased"][0]
    # A: C(1,2)/C(3,2)=0; B: C(3,2)/C(3,2)=1; mean 0.5. Pooled C(4,2)/C(6,2)=15/15? C(4,2)=6, C(6,2)=15 -> 0.4
    assert power["rate"] == pytest.approx(0.5)


def test_first_k_follows_started_at_not_trial_id(tmp_path: Path) -> None:
    _synthetic_job(
        tmp_path / "left-late-pass",
        suffix=1,
        agent="oracle",
        reward=1.0,
        started_at="2026-08-14T00:00:10Z",
    )
    _synthetic_job(
        tmp_path / "left-early-fail",
        suffix=2,
        agent="oracle",
        reward=0.0,
        started_at="2026-08-14T00:00:01Z",
    )
    _synthetic_job(tmp_path / "right-1", suffix=3, agent="nop", reward=0.0)
    _synthetic_job(tmp_path / "right-2", suffix=4, agent="nop", reward=0.0)
    spec = _synthetic_spec(
        left_paths=["left-late-pass/sample-job", "left-early-fail/sample-job"],
        right_paths=["right-1/sample-job", "right-2/sample-job"],
    ).model_copy(update={"pass_k": [1]})

    report = compare(spec, repo_root=tmp_path)
    left = report["cohorts"][0]["pass_any_first_k"][0]
    assert left["selection"] == "first-k-by-started-at-per-task"
    assert left["rate"] == 0.0
    assert left["passes"] == 0
    selected = next(iter(left["selected_trials"].values()))
    assert selected == ["10000000-0000-0000-0000-000000000002"]
    _assert_no_legacy_realized_keys(report)


@pytest.mark.parametrize(
    "started_at",
    ["", "not-a-timestamp", "2026-08-14T00:00:00"],
)
def test_invalid_started_at_excludes_realized_first_k(tmp_path: Path, started_at: str) -> None:
    _synthetic_job(tmp_path / "left-ok", suffix=1, agent="oracle", reward=1.0)
    _synthetic_job(
        tmp_path / "left-bad",
        suffix=2,
        agent="oracle",
        reward=0.0,
        started_at=started_at,
    )
    _synthetic_job(tmp_path / "right", suffix=3, agent="nop", reward=0.0)
    spec = _synthetic_spec(
        left_paths=["left-ok/sample-job", "left-bad/sample-job"],
        right_paths=["right/sample-job"],
    ).model_copy(update={"pass_k": [1]})

    report = compare(spec, repo_root=tmp_path)
    metric = report["cohorts"][0]["pass_any_first_k"][0]
    assert metric["n_tasks"] == 0
    assert metric["unavailable_order_groups"]["sha256:task"] == "missing or invalid started_at"
    unbiased = report["cohorts"][0]["pass_at_k_unbiased"][0]
    assert unbiased["n_tasks"] == 1
    assert unbiased["rate"] == pytest.approx(0.5)
    assert "first-k order is unavailable" in report["paired"][0]["statement"]


def test_boundary_tie_excludes_but_wholly_selected_tie_is_eligible(tmp_path: Path) -> None:
    tie = "2026-08-14T00:00:05Z"
    _synthetic_job(tmp_path / "left-1", suffix=1, agent="oracle", reward=1.0, started_at=tie)
    _synthetic_job(tmp_path / "left-2", suffix=2, agent="oracle", reward=0.0, started_at=tie)
    _synthetic_job(tmp_path / "right-1", suffix=3, agent="nop", reward=0.0, started_at=tie)
    _synthetic_job(tmp_path / "right-2", suffix=4, agent="nop", reward=0.0, started_at=tie)
    spec = _synthetic_spec(
        left_paths=["left-1/sample-job", "left-2/sample-job"],
        right_paths=["right-1/sample-job", "right-2/sample-job"],
    ).model_copy(update={"pass_k": [1, 2]})

    report = compare(spec, repo_root=tmp_path)
    left = report["cohorts"][0]
    k1 = left["pass_any_first_k"][0]
    k2 = left["pass_any_first_k"][1]
    assert k1["n_tasks"] == 0
    assert k1["unavailable_order_groups"]["sha256:task"] == (
        "started_at tie straddles first-k boundary"
    )
    assert k2["n_tasks"] == 1
    assert k2["unavailable_order_groups"] == {}
    assert k2["rate"] == 1.0
    assert k2["selected_trials"]["sha256:task"] == [
        "10000000-0000-0000-0000-000000000001",
        "10000000-0000-0000-0000-000000000002",
    ]


def test_wholly_before_tied_block_may_use_trial_id(tmp_path: Path) -> None:
    _synthetic_job(
        tmp_path / "left-b",
        suffix=2,
        agent="oracle",
        reward=0.0,
        started_at="2026-08-14T00:00:01Z",
    )
    _synthetic_job(
        tmp_path / "left-a",
        suffix=1,
        agent="oracle",
        reward=1.0,
        started_at="2026-08-14T00:00:01Z",
    )
    _synthetic_job(
        tmp_path / "left-later",
        suffix=3,
        agent="oracle",
        reward=0.0,
        started_at="2026-08-14T00:00:09Z",
    )
    _synthetic_job(tmp_path / "right-1", suffix=4, agent="nop", reward=0.0)
    _synthetic_job(tmp_path / "right-2", suffix=5, agent="nop", reward=0.0)
    _synthetic_job(tmp_path / "right-3", suffix=6, agent="nop", reward=0.0)
    spec = _synthetic_spec(
        left_paths=["left-b/sample-job", "left-a/sample-job", "left-later/sample-job"],
        right_paths=["right-1/sample-job", "right-2/sample-job", "right-3/sample-job"],
    ).model_copy(update={"pass_k": [2]})

    report = compare(spec, repo_root=tmp_path)
    metric = report["cohorts"][0]["pass_any_first_k"][0]
    assert metric["n_tasks"] == 1
    assert metric["unavailable_order_groups"] == {}
    assert metric["selected_trials"]["sha256:task"] == [
        "10000000-0000-0000-0000-000000000001",
        "10000000-0000-0000-0000-000000000002",
    ]


def test_planning_transform_is_not_chen_or_realized() -> None:
    assert pass_at_k_unbiased(5, 3, 2) == pytest.approx(0.9)
    assert pass_at_k_probability(3 / 5, 2) == pytest.approx(0.84)
    assert pass_at_k_unbiased(5, 3, 2) != pass_at_k_probability(3 / 5, 2)
