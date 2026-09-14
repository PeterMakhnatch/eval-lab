from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evallab.cohort import (
    NOT_COMPARABLE,
    assemble_members,
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


def _content_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _preamble_job(
    root: Path,
    *,
    suffix: int,
    reward: float = 1.0,
    instruction_paths: list[str] | None = None,
    lock_instructions: list[dict[str, object]] | None = None,
    provenance_path: str | None = None,
    provenance_sha256: str | None = None,
) -> Path:
    """A completed synthetic trial plus retained preamble evidence.

    ``instruction_paths`` writes the declared ``extra_instruction_paths``
    list into the frozen trial lock, ``lock_instructions`` Harbor's digest
    bearing ``extra_instructions`` entries, and the provenance pair the
    queue's ``lab-metadata.json`` run-time record.
    """
    job = _synthetic_job(root, suffix=suffix, agent="oracle", reward=reward)
    trial = next(path for path in job.iterdir() if path.is_dir())
    if instruction_paths is not None or lock_instructions is not None:
        trial_lock_path = trial / "lock.json"
        trial_lock = json.loads(trial_lock_path.read_text())
        if instruction_paths is not None:
            trial_lock["extra_instruction_paths"] = instruction_paths
        if lock_instructions is not None:
            trial_lock["extra_instructions"] = lock_instructions
        trial_lock_path.write_text(json.dumps(trial_lock))
    experiment = {
        key: value
        for key, value in (
            ("preamble_path", provenance_path),
            ("preamble_sha256", provenance_sha256),
        )
        if value is not None
    }
    if experiment:
        (job / "lab-metadata.json").write_text(json.dumps({"experiment": experiment}))
    return job


def _preamble_identities(
    root: Path,
    probe_paths: list[str],
    *,
    anchor_path: str = "anchor/sample-job",
) -> dict[str, str | None]:
    """Assemble probe-cohort members and return their retained identities."""
    spec = CohortComparisonSpec.model_validate(
        {
            "comparison_id": "retained-preamble-identity",
            "experiment_id": "synthetic-experiment",
            "declared_variable": "preamble_hash",
            "pass_k": [1],
            "cohorts": [
                {"label": "probe", "paths": probe_paths},
                {"label": "anchor", "paths": [anchor_path]},
            ],
        }
    )
    return {
        member.trial_id: member.preamble_hash
        for member in assemble_members(root, spec)
        if member.cohort == "probe"
    }


def test_retained_candidate_identity_survives_source_mutation_and_removal(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate.txt"
    candidate.write_text("Retained candidate at execution time.\n")
    retained = _content_digest(candidate.read_bytes())
    for label, suffix in (("left", 1), ("right", 2)):
        _preamble_job(
            tmp_path / label,
            suffix=suffix,
            instruction_paths=["candidate.txt"],
            provenance_path="candidate.txt",
            provenance_sha256=retained,
        )
    _synthetic_job(tmp_path / "anchor", suffix=9, agent="oracle", reward=1.0)
    probe = ["left/sample-job", "right/sample-job"]

    before = _preamble_identities(tmp_path, probe)
    candidate.write_text("Edited after the completed trial.\n")
    after_edit = _preamble_identities(tmp_path, probe)
    candidate.unlink()
    after_remove = _preamble_identities(tmp_path, probe)

    assert len(set(before.values())) == 1
    assert next(iter(before.values())) is not None
    assert before == after_edit == after_remove


def test_distinct_retained_candidates_and_relocated_content_identity(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.txt"
    first.write_text("first retained candidate\n")
    second = tmp_path / "second.txt"
    second.write_text("second retained candidate\n")
    _preamble_job(
        tmp_path / "left",
        suffix=1,
        instruction_paths=["first.txt"],
        provenance_path="first.txt",
        provenance_sha256=_content_digest(first.read_bytes()),
    )
    _preamble_job(
        tmp_path / "right",
        suffix=2,
        instruction_paths=["second.txt"],
        provenance_path="second.txt",
        provenance_sha256=_content_digest(second.read_bytes()),
    )
    _preamble_job(
        tmp_path / "moved",
        suffix=3,
        instruction_paths=["elsewhere/renamed.txt"],
        provenance_path="elsewhere/renamed.txt",
        provenance_sha256=_content_digest(first.read_bytes()),
    )
    _synthetic_job(tmp_path / "anchor", suffix=9, agent="oracle", reward=1.0)

    left = next(iter(_preamble_identities(tmp_path, ["left/sample-job"]).values()))
    right = next(iter(_preamble_identities(tmp_path, ["right/sample-job"]).values()))
    moved = next(iter(_preamble_identities(tmp_path, ["moved/sample-job"]).values()))

    assert left != right
    assert moved == left


def test_declared_instruction_path_without_retained_provenance_is_unknown(
    tmp_path: Path,
) -> None:
    _preamble_job(
        tmp_path / "left",
        suffix=1,
        instruction_paths=["candidate.txt"],
        provenance_path="candidate.txt",
    )
    _preamble_job(tmp_path / "right", suffix=2, instruction_paths=["candidate.txt"])
    spec = _synthetic_spec(
        left_paths=["left/sample-job"],
        right_paths=["right/sample-job"],
    ).model_copy(update={"declared_variable": "preamble_hash"})

    report = compare(spec, repo_root=tmp_path)

    assert all(
        member["preamble_hash"] is None
        for cohort in report["cohorts"]
        for member in cohort["members"]
    )
    assert any(
        "controlled preamble identity is unknown" in warning
        for warning in report["validity_warnings"]
    )
    assert (
        "controlled preamble provenance is missing content sha256"
        in report["validity_warnings"]
    )
    assert report["paired"][0]["statement"].startswith(NOT_COMPARABLE)


def test_multiple_ordered_instructions_require_complete_retained_digests(
    tmp_path: Path,
) -> None:
    one = tmp_path / "one.txt"
    one.write_text("first ordered instruction\n")
    two = tmp_path / "two.txt"
    two.write_text("second ordered instruction\n")
    first_digest = _content_digest(one.read_bytes())
    second_digest = _content_digest(two.read_bytes())
    _preamble_job(
        tmp_path / "complete",
        suffix=1,
        lock_instructions=[
            {"path": "/abs/one.txt", "digest": first_digest},
            {"path": "/abs/two.txt", "digest": second_digest},
        ],
    )
    _preamble_job(
        tmp_path / "partial",
        suffix=2,
        instruction_paths=["/abs/one.txt", "/abs/two.txt"],
        lock_instructions=[{"path": "/abs/one.txt", "digest": first_digest}],
        provenance_path="/abs/one.txt",
        provenance_sha256=first_digest,
    )
    _preamble_job(
        tmp_path / "reversed",
        suffix=3,
        lock_instructions=[
            {"path": "/abs/two.txt", "digest": second_digest},
            {"path": "/abs/one.txt", "digest": first_digest},
        ],
    )
    _synthetic_job(tmp_path / "anchor", suffix=9, agent="oracle", reward=1.0)

    identity = _preamble_identities(tmp_path, ["complete/sample-job"])
    one.write_text("mutated after the completed trial\n")
    two.unlink()

    assert next(iter(identity.values())) is not None
    assert _preamble_identities(tmp_path, ["complete/sample-job"]) == identity
    # One retained digest cannot certify a two-file effective preamble.
    assert (
        next(iter(_preamble_identities(tmp_path, ["partial/sample-job"]).values()))
        is None
    )
    # Ordered files are part of the identity: reversal is a different preamble.
    assert (
        next(iter(_preamble_identities(tmp_path, ["reversed/sample-job"]).values()))
        != next(iter(identity.values()))
    )


def test_contradictory_retained_identity_is_unknown(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.txt"
    candidate.write_text("candidate at execution time\n")
    _preamble_job(
        tmp_path / "conflict",
        suffix=1,
        instruction_paths=["candidate.txt"],
        lock_instructions=[{"path": "candidate.txt", "digest": "sha256:" + "0" * 64}],
        provenance_path="candidate.txt",
        provenance_sha256=_content_digest(candidate.read_bytes()),
    )
    _synthetic_job(tmp_path / "anchor", suffix=9, agent="oracle", reward=1.0)

    identity = _preamble_identities(tmp_path, ["conflict/sample-job"])

    assert next(iter(identity.values())) is None


def test_no_preamble_baseline_stays_distinct_from_unknown_identity(
    tmp_path: Path,
) -> None:
    _synthetic_job(tmp_path / "plain", suffix=1, agent="oracle", reward=1.0)
    _synthetic_job(tmp_path / "anchor", suffix=9, agent="oracle", reward=1.0)
    _preamble_job(tmp_path / "missing", suffix=2, instruction_paths=["gone.txt"])

    plain = _preamble_identities(tmp_path, ["plain/sample-job"])
    missing = _preamble_identities(tmp_path, ["missing/sample-job"])

    assert next(iter(plain.values())) is not None
    assert next(iter(missing.values())) is None


@pytest.mark.parametrize(
    ("declared", "retained"),
    [
        ("intended/candidate.txt", "other/candidate.txt"),
        ("/repo/link/../candidate.txt", "/repo/candidate.txt"),
    ],
)
def test_same_basename_does_not_certify_another_candidate(
    tmp_path: Path, declared: str, retained: str,
) -> None:
    _preamble_job(
        tmp_path / "foreign",
        suffix=1,
        instruction_paths=[declared],
        provenance_path=retained,
        provenance_sha256=_content_digest(b"other candidate"),
    )
    _synthetic_job(tmp_path / "anchor", suffix=9, agent="oracle", reward=1.0)

    identity = _preamble_identities(tmp_path, ["foreign/sample-job"])

    assert next(iter(identity.values())) is None


def test_repeated_instruction_content_is_not_deduplicated(tmp_path: Path) -> None:
    instruction = {"path": "candidate.txt", "digest": _content_digest(b"retained")}
    _preamble_job(tmp_path / "once", suffix=1, lock_instructions=[instruction])
    _preamble_job(
        tmp_path / "twice", suffix=2, lock_instructions=[instruction, instruction]
    )
    _synthetic_job(tmp_path / "anchor", suffix=9, agent="oracle", reward=1.0)

    once = next(iter(_preamble_identities(tmp_path, ["once/sample-job"]).values()))
    twice = next(iter(_preamble_identities(tmp_path, ["twice/sample-job"]).values()))

    assert once is not None and twice is not None
    assert once != twice


def test_provenance_path_alone_is_not_a_no_preamble_baseline(tmp_path: Path) -> None:
    _preamble_job(tmp_path / "missing", suffix=1, provenance_path="candidate.txt")
    _synthetic_job(tmp_path / "anchor", suffix=9, agent="oracle", reward=1.0)

    identity = _preamble_identities(tmp_path, ["missing/sample-job"])

    assert next(iter(identity.values())) is None


def test_conflicting_retained_instruction_order_is_unknown(tmp_path: Path) -> None:
    _preamble_job(
        tmp_path / "conflict",
        suffix=1,
        lock_instructions=[
            {"path": "first.txt", "digest": _content_digest(b"first")},
            {"path": "second.txt", "digest": _content_digest(b"second")},
        ],
        instruction_paths=["second.txt", "first.txt"],
    )
    _synthetic_job(tmp_path / "anchor", suffix=9, agent="oracle", reward=1.0)

    identity = _preamble_identities(tmp_path, ["conflict/sample-job"])

    assert next(iter(identity.values())) is None


def test_parent_components_preserve_distinct_retained_instruction_files(tmp_path: Path) -> None:
    first = _content_digest(b"symlink parent content")
    second = _content_digest(b"root content")
    for label, suffix, first_path in (
        ("symlink", 1, "link/../p.txt"),
        ("canonical", 2, "sub/p.txt"),
    ):
        _preamble_job(
            tmp_path / label,
            suffix=suffix,
            lock_instructions=[
                {"path": first_path, "digest": first},
                {"path": "p.txt", "digest": second},
            ],
        )
    _synthetic_job(tmp_path / "anchor", suffix=9, agent="oracle", reward=1.0)

    identities = _preamble_identities(
        tmp_path, ["symlink/sample-job", "canonical/sample-job"],
    )
    assert None not in identities.values()
    assert len(set(identities.values())) == 1


def test_unmodeled_nested_instruction_declaration_is_not_no_preamble(tmp_path: Path) -> None:
    job = _preamble_job(tmp_path / "nested", suffix=1)
    trial = next(path for path in job.iterdir() if path.is_dir())
    lock_path = trial / "lock.json"
    lock = json.loads(lock_path.read_text())
    lock["agent"] = {
        "name": "oracle",
        "kwargs": {
            "extra_instructions": [
                {"path": "candidate.txt", "digest": _content_digest(b"retained")},
            ],
        },
    }
    lock_path.write_text(json.dumps(lock))
    _synthetic_job(tmp_path / "anchor", suffix=9, agent="oracle", reward=1.0)

    identity = _preamble_identities(tmp_path, ["nested/sample-job"])
    assert next(iter(identity.values())) is None
