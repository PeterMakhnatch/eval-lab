"""Tests for LADDER difficulty screening and follow-up generation (src/evallab/screen.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from evallab.cli import run_cli
from evallab.ladder import (
    DifficultyVariantContract,
    ScreenSpec,
    analyze_screen_results,
    generate_stage1_screen,
    generate_stage2_screen,
)
from evallab.power import (
    pass_at_k_probability,
    plan_power_spec,
    power_requirements,
)
from evallab.registry import TaskNotRegisteredError
from evallab.schemas import ExperimentSpec


def test_screen_spec_validation_and_defaults() -> None:
    """ScreenSpec validates required fields and sets standard model level defaults."""
    spec = ScreenSpec(
        screen_id="screen-test-defaults",
        tasks=["event-summary"],
    )
    assert spec.screen_id == "screen-test-defaults"
    assert spec.purpose == "comparison"
    assert len(spec.model_levels) == 2
    assert spec.model_levels[0].name == "low"
    assert spec.model_levels[0].model == "gemini-3.7-flash-low"
    assert spec.model_levels[1].name == "medium"
    assert spec.model_levels[1].model == "gemini-3.7-flash-medium"
    assert spec.initial_k == 1
    assert spec.followup_k == 3


def test_screen_spec_custom_model_levels() -> None:
    """ScreenSpec supports arbitrary ordered capability levels and profile strings."""
    spec = ScreenSpec(
        screen_id="screen-test-custom",
        tasks=["event-summary"],
        model_levels=[
            {"name": "tier-1", "agent": "oracle"},
            {"name": "tier-2", "agent": "nop"},
        ],
    )
    assert len(spec.model_levels) == 2
    assert spec.model_levels[0].name == "tier-1"
    assert spec.model_levels[0].agent == "oracle"
    assert spec.model_levels[1].name == "tier-2"
    assert spec.model_levels[1].agent == "nop"


def test_screen_spec_rejects_empty_tasks() -> None:
    """ScreenSpec requires at least one task."""
    with pytest.raises(ValidationError):
        ScreenSpec(
            screen_id="screen-empty",
            tasks=[],
        )


def test_stage1_generation_registered_tasks(tmp_path: Path) -> None:
    """Stage 1 generates k=1 specs across registered tasks preserving human approval."""
    repo_root = Path.cwd()
    screen_spec = ScreenSpec(
        screen_id="screen-stage1-registered-tasks",
        tasks=["event-summary"],
    )
    out_dir = tmp_path / "queue" / "proposed"
    result = generate_stage1_screen(
        screen_spec,
        repo_root=repo_root,
        output_dir=out_dir,
    )

    # 1 registered task * 2 model levels = 2 specs.
    assert result.total_specs == 2
    assert result.total_trials == 2
    assert len(result.specs) == 2
    assert len(result.written_paths) == 2

    # Check spec properties
    for spec in result.specs:
        assert spec.grid_id == "screen-stage1-registered-tasks"
        assert spec.attempts == 1
        assert spec.purpose == "comparison"
        assert spec.grid_point is not None
        assert spec.grid_point["screen_id"] == "screen-stage1-registered-tasks"
        assert spec.grid_point["stage"] == 1
        assert spec.grid_point["k"] == 1
        assert spec.prereg is not None
        assert spec.power is not None
        assert spec.power.planned_n == 1

        # Read on-disk file
        file_path = out_dir / f"{spec.name}.json"
        assert file_path.is_file()
        loaded = ExperimentSpec.model_validate_json(file_path.read_text())
        assert loaded.name == spec.name
        assert loaded.task == spec.task


def test_stage1_generation_rejects_unregistered_task() -> None:
    """Stage 1 rejects tasks not in TaskRegistry with TaskNotRegisteredError."""
    repo_root = Path.cwd()
    screen_spec = ScreenSpec(
        screen_id="screen-invalid-task",
        tasks=["unregistered-fake-task-123"],
    )
    with pytest.raises(TaskNotRegisteredError):
        generate_stage1_screen(
            screen_spec,
            repo_root=repo_root,
            dry_run=True,
        )


def test_stage1_idempotent_spec_emission(tmp_path: Path) -> None:
    """Stage 1 is idempotent: subsequent runs write 0 files and dedupe existing points."""
    repo_root = Path.cwd()
    screen_spec = ScreenSpec(
        screen_id="screen-idempotent-test",
        tasks=["event-summary"],
    )
    out_dir = tmp_path / "queue" / "proposed"

    # Run 1: writes 2 specs (low, medium)
    res1 = generate_stage1_screen(
        screen_spec,
        repo_root=repo_root,
        output_dir=out_dir,
    )
    assert res1.total_specs == 2
    assert len(res1.written_paths) == 2
    assert len(list(out_dir.glob("*.json"))) == 2

    # Run 2: writes 0 specs, dedupes 2
    res2 = generate_stage1_screen(
        screen_spec,
        repo_root=repo_root,
        output_dir=out_dir,
    )
    assert res2.total_specs == 0
    assert len(res2.written_paths) == 0
    assert len(res2.deduped) == 2
    assert len(list(out_dir.glob("*.json"))) == 2


def test_analysis_event_summary_all_one_saturation() -> None:
    """Live evidence test: event-summary scoring 1.0 on Low/Medium is saturated-pass (stopped)."""
    screen_spec = ScreenSpec(
        screen_id="screen-event-summary-sat",
        tasks=["event-summary"],
    )
    # Simulated trial records matching live Gemini 3.7 Flash Low=1.0, Medium=1.0
    trial_records = [
        {
            "screen_id": "screen-event-summary-sat",
            "task": "event-summary",
            "agent": "antigravity-cli",
            "model": "gemini-3.7-flash-low",
            "model_level": "low",
            "stage": 1,
            "reward": 1.0,
            "error": None,
        },
        {
            "screen_id": "screen-event-summary-sat",
            "task": "event-summary",
            "agent": "antigravity-cli",
            "model": "gemini-3.7-flash-medium",
            "model_level": "medium",
            "stage": 1,
            "reward": 1.0,
            "error": None,
        },
    ]

    report = analyze_screen_results(
        "screen-event-summary-sat",
        spec=screen_spec,
        trial_records=trial_records,
    )

    assert report.total_tasks == 1
    assert report.classifications["saturated-pass"] == 1
    assert len(report.separating_tasks) == 0
    assert report.stopped_tasks == ["event-summary"]

    task_res = report.tasks[0]
    assert task_res.task_id == "event-summary"
    assert task_res.classification == "saturated-pass"
    assert not task_res.selected_for_followup
    assert "Ceiling saturation" in task_res.reason
    assert "Stopped: saturated-pass ceiling effect" in task_res.followup_reason


def test_analysis_all_zero_failure_saturation() -> None:
    """Tasks where all models score 0.0 are classified as saturated-fail (stopped)."""
    screen_spec = ScreenSpec(
        screen_id="screen-fail-sat",
        tasks=["terminal-bench-html-js-filter"],
    )
    trial_records = [
        {
            "screen_id": "screen-fail-sat",
            "task": "terminal-bench-html-js-filter",
            "agent": "antigravity-cli",
            "model": "gemini-3.7-flash-low",
            "model_level": "low",
            "stage": 1,
            "reward": 0.0,
            "error": None,
        },
        {
            "screen_id": "screen-fail-sat",
            "task": "terminal-bench-html-js-filter",
            "agent": "antigravity-cli",
            "model": "gemini-3.7-flash-medium",
            "model_level": "medium",
            "stage": 1,
            "reward": 0.0,
            "error": None,
        },
    ]

    report = analyze_screen_results(
        "screen-fail-sat",
        spec=screen_spec,
        trial_records=trial_records,
    )

    assert report.classifications["saturated-fail"] == 1
    assert len(report.separating_tasks) == 0
    assert report.stopped_tasks == ["terminal-bench-html-js-filter"]

    task_res = report.tasks[0]
    assert task_res.classification == "saturated-fail"
    assert not task_res.selected_for_followup
    assert "Floor saturation" in task_res.reason
    assert "Stopped: saturated-fail floor effect" in task_res.followup_reason


def test_analysis_low_medium_separation() -> None:
    """Tasks with Low=0.0 and Medium=1.0 are classified as separating (selected for k=3)."""
    screen_spec = ScreenSpec(
        screen_id="screen-separating-test",
        tasks=["transaction-reconciliation"],
    )
    trial_records = [
        {
            "screen_id": "screen-separating-test",
            "task": "transaction-reconciliation",
            "agent": "antigravity-cli",
            "model": "gemini-3.7-flash-low",
            "model_level": "low",
            "stage": 1,
            "reward": 0.0,
            "error": None,
        },
        {
            "screen_id": "screen-separating-test",
            "task": "transaction-reconciliation",
            "agent": "antigravity-cli",
            "model": "gemini-3.7-flash-medium",
            "model_level": "medium",
            "stage": 1,
            "reward": 1.0,
            "error": None,
        },
    ]

    report = analyze_screen_results(
        "screen-separating-test",
        spec=screen_spec,
        trial_records=trial_records,
    )

    assert report.classifications["separating"] == 1
    assert report.separating_tasks == ["transaction-reconciliation"]
    assert len(report.stopped_tasks) == 0

    task_res = report.tasks[0]
    assert task_res.classification == "separating"
    assert task_res.selected_for_followup
    assert "Separation observed" in task_res.reason
    assert "Selected for Stage 2 follow-up (k=3)" in task_res.followup_reason


def test_analysis_execution_errors() -> None:
    """Trials encountering execution errors or harness exceptions are classified as broken/error."""
    screen_spec = ScreenSpec(
        screen_id="screen-error-test",
        tasks=["query-optimize"],
    )
    trial_records = [
        {
            "screen_id": "screen-error-test",
            "task": "query-optimize",
            "agent": "antigravity-cli",
            "model": "gemini-3.7-flash-low",
            "model_level": "low",
            "stage": 1,
            "reward": None,
            "error": "Harbor ContainerCrashException: exit code 137 OOM",
        },
        {
            "screen_id": "screen-error-test",
            "task": "query-optimize",
            "agent": "antigravity-cli",
            "model": "gemini-3.7-flash-medium",
            "model_level": "medium",
            "stage": 1,
            "reward": 1.0,
            "error": None,
        },
    ]

    report = analyze_screen_results(
        "screen-error-test",
        spec=screen_spec,
        trial_records=trial_records,
    )

    assert report.classifications["broken/error"] == 1
    assert len(report.separating_tasks) == 0
    assert report.stopped_tasks == ["query-optimize"]

    task_res = report.tasks[0]
    assert task_res.classification == "broken/error"
    assert not task_res.selected_for_followup
    assert "Execution error observed" in task_res.reason
    assert "execution error / harness exception" in task_res.followup_reason


def test_analysis_missing_results() -> None:
    """Tasks missing trial results for required levels are classified as insufficient."""
    screen_spec = ScreenSpec(
        screen_id="screen-missing-test",
        tasks=["event-summary"],
    )
    # Only Low is present; Medium is missing
    trial_records = [
        {
            "screen_id": "screen-missing-test",
            "task": "event-summary",
            "agent": "antigravity-cli",
            "model": "gemini-3.7-flash-low",
            "model_level": "low",
            "stage": 1,
            "reward": 1.0,
            "error": None,
        },
    ]

    report = analyze_screen_results(
        "screen-missing-test",
        spec=screen_spec,
        trial_records=trial_records,
    )

    assert report.classifications["insufficient"] == 1
    assert len(report.separating_tasks) == 0
    assert report.stopped_tasks == ["event-summary"]

    task_res = report.tasks[0]
    assert task_res.classification == "insufficient"
    assert "Missing trial results for levels: medium" in task_res.reason
    assert "incomplete stage 1 results" in task_res.followup_reason


def test_cohort_isolation(tmp_path: Path) -> None:
    """Trials belonging to other screens or unrelated runs are ignored and not pooled."""
    screen_spec = ScreenSpec(
        screen_id="screen-isolated-cohort",
        tasks=["event-summary"],
    )
    # 1 matching record, 2 unrelated records from other grids / baselines
    trial_records = [
        {
            "screen_id": "screen-isolated-cohort",
            "task": "event-summary",
            "agent": "antigravity-cli",
            "model": "gemini-3.7-flash-low",
            "model_level": "low",
            "stage": 1,
            "reward": 1.0,
            "error": None,
        },
        {
            "screen_id": "unrelated-screen-xyz",
            "task": "event-summary",
            "agent": "antigravity-cli",
            "model": "gemini-3.7-flash-medium",
            "model_level": "medium",
            "stage": 1,
            "reward": 0.0,
            "error": None,
        },
        {
            "screen_id": "baseline-oracle-grid",
            "task": "event-summary",
            "agent": "oracle",
            "model_level": "oracle",
            "stage": 1,
            "reward": 1.0,
            "error": None,
        },
    ]

    # Because unrelated records are filtered out, screen-isolated-cohort is missing 'medium'
    report = analyze_screen_results(
        "screen-isolated-cohort",
        spec=screen_spec,
        trial_records=trial_records,
    )

    assert report.classifications["insufficient"] == 1
    assert "Missing trial results for levels: medium" in report.tasks[0].reason


def test_stage2_emits_k3_only_for_separating_tasks(tmp_path: Path) -> None:
    """Stage 2 emits k=3 specs only for a registered separating task."""
    repo_root = Path.cwd()
    screen_spec = ScreenSpec(
        screen_id="screen-staged-e2e",
        tasks=["event-summary"],
    )
    trial_records = [
        {
            "screen_id": "screen-staged-e2e",
            "task": "event-summary",
            "agent": "antigravity-cli",
            "model": "gemini-3.7-flash-low",
            "model_level": "low",
            "stage": 1,
            "reward": 0.0,
            "error": None,
        },
        {
            "screen_id": "screen-staged-e2e",
            "task": "event-summary",
            "agent": "antigravity-cli",
            "model": "gemini-3.7-flash-medium",
            "model_level": "medium",
            "stage": 1,
            "reward": 1.0,
            "error": None,
        },
    ]

    analysis = analyze_screen_results(
        "screen-staged-e2e",
        spec=screen_spec,
        trial_records=trial_records,
    )
    assert analysis.separating_tasks == ["event-summary"]
    assert analysis.stopped_tasks == []

    out_dir = tmp_path / "queue" / "proposed_stage2"
    result = generate_stage2_screen(
        analysis,
        screen_spec,
        repo_root=repo_root,
        output_dir=out_dir,
    )
    assert result.total_specs == 2
    assert result.total_trials == 6
    assert len(result.specs) == 2
    assert len(result.written_paths) == 2
    for generated_spec in result.specs:
        assert generated_spec.task == "event-summary"
        assert generated_spec.attempts == 3
        assert generated_spec.grid_point["stage"] == 2
        assert generated_spec.grid_point["k"] == 3

    result_rerun = generate_stage2_screen(
        analysis,
        screen_spec,
        repo_root=repo_root,
        output_dir=out_dir,
    )
    assert result_rerun.total_specs == 0
    assert len(result_rerun.written_paths) == 0
    assert len(result_rerun.deduped) == 2


def test_difficulty_variant_contract() -> None:
    """DifficultyVariantContract enforces verifier preservation and authoring boundaries."""
    contract = DifficultyVariantContract(
        task_id="event-summary",
        base_version="1.0.0",
    )
    assert contract.task_id == "event-summary"
    assert contract.verifier_preserved is True
    assert contract.authoring_boundary_enforced is True
    assert "preserve verifier ground-truth" in contract.contract_statement.lower()


def test_cli_ladder_screen_stage1_analyze_stage2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI ladder stages operate on the currently registered task set."""
    screen_file = tmp_path / "screen.yaml"
    screen_file.write_text(
        """
schema_version: 1
screen_id: cli-screen-demo
purpose: comparison
tasks:
  - event-summary
model_levels:
  - name: low
    agent: antigravity-cli
    model: gemini-3.7-flash-low
  - name: medium
    agent: antigravity-cli
    model: gemini-3.7-flash-medium
initial_k: 1
followup_k: 3
"""
    )
    out_dir = tmp_path / "output_specs"
    ret1 = run_cli(
        ["ladder", "screen", "stage1", str(screen_file), "-o", str(out_dir)],
        workspace=Path.cwd(),
    )
    assert ret1 == 0
    captured1 = capsys.readouterr()
    assert "LADDER Screen Stage 1 Generation: cli-screen-demo" in captured1.out
    assert "Generated 2 specs" in captured1.out
    assert "Human approval preserved" in captured1.out
    assert len(list(out_dir.glob("*.json"))) == 2

    jobs_dir = tmp_path / "runs"
    for level, reward in (("low", 0.0), ("medium", 1.0)):
        trial = jobs_dir / f"cli-screen-demo-event-summary-{level}-k1"
        trial.mkdir(parents=True)
        (trial / "spec.json").write_text(
            json.dumps(
                {
                    "name": trial.name,
                    "grid_id": "cli-screen-demo",
                    "grid_point": {
                        "screen_id": "cli-screen-demo",
                        "task": "event-summary",
                        "model_level": level,
                    },
                }
            )
        )
        (trial / "result.json").write_text(
            json.dumps(
                {
                    "task_name": "event-summary",
                    "verifier_result": {"reward": reward},
                }
            )
        )

    ret2 = run_cli(
        [
            "ladder",
            "screen",
            "analyze",
            str(screen_file),
            "--jobs-dir",
            str(jobs_dir),
        ],
        workspace=Path.cwd(),
    )
    assert ret2 == 0
    captured2 = capsys.readouterr()
    assert "Screen Analysis: cli-screen-demo" in captured2.out
    assert "event-summary: [separating] -> SELECTED (k=3 follow-up)" in captured2.out

    stage2_out = tmp_path / "stage2_specs"
    ret3 = run_cli(
        [
            "ladder",
            "screen",
            "stage2",
            str(screen_file),
            "-o",
            str(stage2_out),
            "--jobs-dir",
            str(jobs_dir),
        ],
        workspace=Path.cwd(),
    )
    assert ret3 == 0
    captured3 = capsys.readouterr()
    assert "LADDER Screen Stage 2 Follow-Up Generation: cli-screen-demo" in captured3.out
    assert "Separating tasks selected for follow-up (1): event-summary" in captured3.out
    assert "Stopped tasks (0): none" in captured3.out
    assert "Generated 2 follow-up specs" in captured3.out
    assert len(list(stage2_out.glob("*.json"))) == 2


def test_power_planning_and_spec() -> None:
    """Power calculations and plan_power_spec construct valid PowerSpec objects."""
    # Test pass@k transformation
    assert pass_at_k_probability(0.5, 1) == 0.5
    assert pass_at_k_probability(0.5, 2) == 0.75

    # Test requirements table
    rows = power_requirements(baseline=0.2, attempt_effect=0.3, max_k=3)
    assert len(rows) == 3
    assert rows[0]["k"] == 1

    # Test PowerSpec planning
    spec = plan_power_spec(n_tasks=4, k=1, baseline=0.0)
    assert spec.planned_n == 4
    assert spec.mdd is not None


def test_analysis_reads_catalog_facts_and_ignores_stage_two() -> None:
    """Catalog-style facts are accepted without pooling later-stage results."""
    screen_spec = ScreenSpec(
        screen_id="screen-catalog-facts",
        tasks=["transaction-reconciliation"],
    )
    trial_records = [
        {
            "grid_id": "screen-catalog-facts",
            "task_name": "transaction-reconciliation",
            "agent_name": "antigravity-cli",
            "model_name": "gemini-3.7-flash-low",
            "stage": 1,
            "primary_reward": 0.0,
            "exception_class": None,
        },
        {
            "grid_id": "screen-catalog-facts",
            "task_name": "transaction-reconciliation",
            "agent_name": "antigravity-cli",
            "model_name": "gemini-3.7-flash-medium",
            "stage": 1,
            "primary_reward": 1.0,
            "exception_class": None,
        },
        {
            "grid_id": "screen-catalog-facts",
            "task_name": "transaction-reconciliation",
            "agent_name": "antigravity-cli",
            "model_name": "gemini-3.7-flash-medium",
            "stage": 2,
            "primary_reward": 0.0,
            "exception_class": None,
        },
    ]

    report = analyze_screen_results(
        "screen-catalog-facts",
        spec=screen_spec,
        trial_records=trial_records,
    )

    assert report.tasks[0].classification == "separating"
    assert report.tasks[0].level_scores == {"low": 0.0, "medium": 1.0}
    assert report.tasks[0].trial_counts == {"low": 1, "medium": 1}


def test_difficulty_variant_contract_rejects_verifier_change() -> None:
    """Variant generation cannot bypass verifier-preserving authoring controls."""
    with pytest.raises(ValueError, match="verifier ground-truth"):
        DifficultyVariantContract(
            task_id="event-summary",
            base_version="1.0.0",
            verifier_preserved=False,
        )
