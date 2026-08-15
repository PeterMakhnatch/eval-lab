from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from evallab import cli
from evallab.cohort import (
    NOT_COMPARABLE,
    bootstrap_mean_interval,
    compare,
    minimum_detectable_effect,
    power_requirements,
    render_markdown,
)
from evallab.facts import rebuild_from_raw
from evallab.report import draft_eval_card, family_report, render_family_report
from evallab.results import load_job
from evallab.schemas import CohortComparisonSpec


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_job(
    root: Path,
    *,
    name: str,
    agent: str,
    model: str | None,
    task_rewards: dict[str, list[float]],
    with_atif: bool = False,
) -> Path:
    job = root / name
    trials: list[Path] = []
    trial_index = 0
    id_offset = sum(map(ord, name)) * 1_000
    for task_name, rewards in task_rewards.items():
        for attempt, reward in enumerate(rewards, start=1):
            trial_index += 1
            trial = job / f"{task_name}__{attempt:02d}"
            trials.append(trial)
            trial_id = f"10000000-0000-0000-0000-{id_offset + trial_index:012d}"
            _write_json(trial / "config.json", {"agent": {"name": agent}})
            _write_json(
                trial / "lock.json",
                {
                    "schema_version": 2,
                    "task": {"name": task_name, "digest": f"sha256:{task_name}"},
                    "agent": {
                        "name": agent,
                        "model_name": model,
                        "skills": [],
                        "mcp_servers": [],
                        "kwargs": {},
                    },
                    "skills": [],
                    "environment": {"type": "docker"},
                    "verifier": {"environment_mode": "separate"},
                },
            )
            _write_json(
                trial / "result.json",
                {
                    "id": trial_id,
                    "trial_name": trial.name,
                    "task_name": task_name,
                    "task_checksum": task_name,
                    "config": {"extra_instruction_paths": []},
                    "agent_info": {
                        "name": agent,
                        "version": "1.2.3",
                        "model_info": {"name": model} if model else None,
                    },
                    "agent_result": {
                        "n_input_tokens": 10,
                        "n_cache_tokens": 0,
                        "n_output_tokens": 5,
                        "cost_usd": 0.02,
                    },
                    "verifier_result": {"rewards": {"reward": reward}},
                    "exception_info": None,
                },
            )
            if with_atif:
                steps: list[dict[str, Any]] = [
                    {"step_id": 1, "source": "user", "message": "do the task"},
                    {
                        "step_id": 2,
                        "source": "agent",
                        "message": "",
                        "tool_calls": [
                            {
                                "tool_call_id": "failure",
                                "function_name": "exec",
                                "arguments": {"command": "python app.py"},
                            }
                        ],
                        "observation": {
                            "results": [
                                {
                                    "source_call_id": "failure",
                                    "content": "failed",
                                    "extra": {"exit_code": 1},
                                }
                            ]
                        },
                    },
                    {
                        "step_id": 3,
                        "source": "agent",
                        "message": "",
                        "tool_calls": [
                            {
                                "tool_call_id": "retry",
                                "function_name": "exec",
                                "arguments": {"command": "python app.py"},
                            }
                        ],
                        "observation": {
                            "results": [
                                {
                                    "source_call_id": "retry",
                                    "content": "ok",
                                    "extra": {"exit_code": 0},
                                }
                            ]
                        },
                    },
                ]
                if attempt == 1:
                    steps.append(
                        {
                            "step_id": 4,
                            "source": "agent",
                            "message": "",
                            "tool_calls": [
                                {
                                    "tool_call_id": "verify",
                                    "function_name": "exec",
                                    "arguments": {"command": "pytest -q"},
                                }
                            ],
                            "observation": {
                                "results": [
                                    {
                                        "source_call_id": "verify",
                                        "content": "passed",
                                        "extra": {"exit_code": 0},
                                    }
                                ]
                            },
                        }
                    )
                steps.append(
                    {"step_id": len(steps) + 1, "source": "agent", "message": "done"}
                )
                _write_json(
                    trial / "agent/trajectory.json",
                    {
                        "schema_version": "ATIF-v1.7",
                        "session_id": trial_id,
                        "agent": {
                            "name": agent,
                            "version": "1.2.3",
                            "model_name": model or "not-applicable",
                        },
                        "steps": steps,
                        "final_metrics": {
                            "total_steps": len(steps),
                            "total_cost_usd": 0.02,
                        },
                    },
                )
    _write_json(job / "config.json", {"job_name": name})
    _write_json(job / "lock.json", {"harbor": {"version": "0.21.0"}})
    _write_json(
        job / "result.json",
        {
            "id": f"00000000-0000-0000-0000-{sum(map(ord, name)):012d}",
            "n_total_trials": len(trials),
            "stats": {"n_completed_trials": len(trials), "n_errored_trials": 0},
        },
    )
    return job


def _spec(left: str, right: str, *, k: int = 1) -> CohortComparisonSpec:
    return CohortComparisonSpec.model_validate(
        {
            "comparison_id": "known-truth",
            "experiment_id": "known-truth",
            "declared_variable": "agent_name",
            "pass_k": [k],
            "cohorts": [
                {"label": "baseline", "paths": [left]},
                {"label": "candidate", "paths": [right]},
            ],
        }
    )


def test_null_task_bootstrap_false_finding_rate_is_near_five_percent() -> None:
    generator = random.Random(20260814)
    findings = 0
    simulations = 240
    for simulation in range(simulations):
        deltas = [
            float(generator.random() < 0.5) - float(generator.random() < 0.5)
            for _ in range(120)
        ]
        interval = bootstrap_mean_interval(
            deltas,
            resamples=700,
            seed=simulation,
        )
        assert interval is not None
        findings += not (interval[0] <= 0 <= interval[1])
    assert 0.02 <= findings / simulations <= 0.09


def test_large_known_difference_is_detected() -> None:
    interval = bootstrap_mean_interval([1.0] * 80, seed=7)
    assert interval == (1.0, 1.0)
    assert interval[0] > 0


def test_task_cluster_interval_is_wider_than_naive_attempt_interval() -> None:
    task_units = [0.0] * 10 + [1.0] * 10
    naive_attempts = [value for value in task_units for _ in range(8)]
    clustered = bootstrap_mean_interval(task_units, seed=11)
    naive = bootstrap_mean_interval(naive_attempts, seed=11)
    assert clustered is not None and naive is not None
    assert clustered[1] - clustered[0] > naive[1] - naive[0]


def test_comparison_ranks_only_with_paired_tasks_interval_and_elicitation(
    tmp_path: Path,
) -> None:
    tasks = {f"task-{index:02d}": [0.0] for index in range(30)}
    _write_job(tmp_path, name="baseline-job", agent="agent-a", model="model-a", task_rewards=tasks)
    _write_job(
        tmp_path,
        name="candidate-job",
        agent="agent-b",
        model="model-b",
        task_rewards={key: [1.0] for key in tasks},
    )

    report = compare(_spec("baseline-job", "candidate-job"), repo_root=tmp_path)
    paired = report["paired"][0]
    assert paired["rankable"] is True
    assert paired["n_tasks"] == 30
    assert paired["k"] == 1
    assert paired["bootstrap_95"] == [1.0, 1.0]
    assert paired["elicitation"]["candidate"]["model_pin"] == "model-b"
    markdown = render_markdown(report)
    assert "Ranking: candidate > baseline" in markdown
    assert "n_tasks=30, k=1" in markdown


def test_comparison_prints_literal_refusal_when_model_pin_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tasks = {"task-a": [0.0], "task-b": [0.0]}
    _write_job(tmp_path, name="left", agent="agent-a", model=None, task_rewards=tasks)
    _write_job(tmp_path, name="right", agent="agent-b", model="model-b", task_rewards=tasks)

    report = compare(_spec("left", "right"), repo_root=tmp_path)
    paired = report["paired"][0]
    assert paired["rankable"] is False
    assert paired["statement"].startswith(NOT_COMPARABLE)
    assert "missing model pin" in paired["statement"]
    assert NOT_COMPARABLE in render_markdown(report)
    spec_path = tmp_path / "comparison.json"
    spec_path.write_text(_spec("left", "right").model_dump_json(), encoding="utf-8")
    assert cli.run_cli(["compare", "comparison.json"], workspace=tmp_path) == 0
    assert NOT_COMPARABLE in capsys.readouterr().out


def test_power_reports_mde_and_required_n_k_tradeoffs(capsys: pytest.CaptureFixture[str]) -> None:
    effect = minimum_detectable_effect(n_tasks=100, k=1, baseline=0.5)
    assert effect is not None and 0 < effect < 0.3
    rows = power_requirements(baseline=0.3, attempt_effect=0.2, max_k=3)
    assert [row["k"] for row in rows] == [1, 2, 3]
    assert all(int(row["required_n_tasks"] or 0) >= 2 for row in rows)

    assert (
        cli.run_cli(
            ["power", "--n-tasks", "100", "--k", "1", "--baseline", "0.5"],
            workspace=Path("/nonexistent-fixture-root"),
        )
        == 0
    )
    assert "minimum detectable per-attempt difference" in capsys.readouterr().out


def test_family_report_joins_parquet_to_raw_atif_and_explains_results(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job_path = _write_job(
        tmp_path / "runs",
        name="family-job",
        agent="agent-a",
        model="model-a",
        task_rewards={"task-family": [1.0, 0.0]},
        with_atif=True,
    )
    job = load_job(job_path)
    parquet = tmp_path / "derived/parquet"
    rebuild_from_raw([job], parquet)

    report = family_report("task-family", parquet_root=parquet, raw_roots=[tmp_path / "runs"])
    assert report["n_trials"] == 2
    assert report["first_failure_step_distribution"] == [{"step": 2, "trials": 2}]
    assert report["loop_detection"]["trials"] == 2
    assert report["verification_before_done"] == {
        "heuristic": "recognizable test, lint, typecheck, check, or verify tool call in raw ATIF",
        "yes": 1,
        "no": 1,
        "unknown": 0,
    }
    rendered = render_family_report(report)
    assert "2 trials across 1 jobs" in rendered
    assert "Step 2: 2 trial(s)" in rendered
    assert "verification ran in 1 trial(s)" in rendered
    assert (
        cli.run_cli(
            [
                "report",
                "family",
                "task-family",
                "--parquet-dir",
                "derived/parquet",
                "--raw-root",
                "runs",
            ],
            workspace=tmp_path,
        )
        == 0
    )
    assert "# Trajectory family report: task-family" in capsys.readouterr().out


def test_completed_spec_drafts_eval_card_with_digests_intervals_and_threats(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_job(
        tmp_path / "runs",
        name="completed-eval",
        agent="agent-a",
        model="model-a",
        task_rewards={"task-a": [1.0], "task-b": [0.0]},
    )
    spec_path = tmp_path / "queue/done/agent-spec.json"
    _write_json(
        spec_path,
        {
            "schema_version": 1,
            "name": "completed-eval",
            "hypothesis": "The pinned agent can solve this task family.",
            "task": "task-family",
            "agent": "agent-a",
            "model": "model-a",
            "attempts": 1,
            "submitted_by": "truth-test",
        },
    )
    template_source = Path(__file__).parents[1] / "research/cards/TEMPLATE.md"
    template = tmp_path / "research/cards/TEMPLATE.md"
    template.parent.mkdir(parents=True)
    template.write_text(template_source.read_text(encoding="utf-8"), encoding="utf-8")

    output = tmp_path / "research/cards/completed-eval.md"
    card_path, card = draft_eval_card(
        spec_path,
        repo_root=tmp_path,
        output_path=output,
    )
    rendered = card_path.read_text(encoding="utf-8")
    assert card["numbers"]["n_tasks"] == 2
    assert card["numbers"]["bootstrap_95"] is not None
    assert "Config digest: `sha256:" in rendered
    assert "model-a" in rendered
    assert "Not determined automatically" in rendered
    assert "generalization is weak" in rendered
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        draft_eval_card(spec_path, repo_root=tmp_path, output_path=output)
    assert (
        cli.run_cli(
            [
                "report",
                "card",
                "queue/done/agent-spec.json",
                "--output",
                "research/cards/cli-card.md",
            ],
            workspace=tmp_path,
        )
        == 0
    )
    assert "config digest: sha256:" in capsys.readouterr().out
