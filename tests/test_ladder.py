"""Tests for LADDER evaluation grid generator (src/evallab/ladder.py)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from evallab import cli
from evallab.ladder import (
    AgentSpec,
    GridAxes,
    GridLimits,
    GridSpec,
    LadderGridSpec,
    ProviderLimit,
    TaskSpec,
    generate_grid,
    generate_spec_name,
    load_grid_spec,
    main,
    sanitize_slug,
)
from evallab.quota import Headroom
from evallab.schemas import ExperimentSpec


def _dir_hash(directory: Path) -> str:
    """Compute sha256 digest of all files in a directory tree."""
    if not directory.exists():
        return "empty"
    hasher = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            hasher.update(str(path.relative_to(directory)).encode("utf-8"))
            hasher.update(path.read_bytes())
    return hasher.hexdigest()


def test_sanitize_slug() -> None:
    assert sanitize_slug("tasks/event-summary") == "tasks-event-summary"
    assert sanitize_slug("canary/transaction-reconciliation") == "canary-transaction-reconciliation"
    assert (
        sanitize_slug("research/experiments/preambles/brief-discipline.md")
        == "research-experiments-preambles-brief-discipline"
    )
    assert sanitize_slug("Special_Characters!@#") == "special-characters"
    assert sanitize_slug("") == "item"
def test_generate_spec_name_conforms_to_regex_and_length() -> None:
    name = generate_spec_name(
        grid_name="grid-01",
        task_slug="event-summary",
        agent_slug="codex",
        preamble_slug="brief-discipline",
        attempts=3,
    )
    assert name == "grid-01-event-summary-codex-brief-discipline-k3"
    spec = ExperimentSpec(
        name=name,
        hypothesis="test hypothesis",
        purpose="elicitation",
        task="tasks/test",
        agent="oracle",
        submitted_by="test",
    )
    assert spec.name == name


def test_generate_spec_name_omits_none_preamble() -> None:
    name = generate_spec_name(
        grid_name="grid-01",
        task_slug="event-summary",
        agent_slug="oracle",
        preamble_slug="none",
        attempts=1,
    )
    assert name == "grid-01-event-summary-oracle-k1"


def test_generate_spec_name_truncation_preserves_length_and_suffix() -> None:
    long_task = "a" * 50
    long_agent = "b" * 50
    name = generate_spec_name(
        grid_name="grid-very-long-name-exceeding-standard-limits",
        task_slug=long_task,
        agent_slug=long_agent,
        preamble_slug="preamble",
        attempts=5,
        max_len=80,
    )
    assert len(name) <= 80
    assert name.endswith("-k5")
    assert name.startswith("grid-very-long")
    spec = ExperimentSpec(
        name=name,
        hypothesis="test hypothesis",
        purpose="comparison",
        task="tasks/test",
        agent="oracle",
        submitted_by="test",
    )
    assert spec.name == name


def test_load_grid_spec_from_yaml(tmp_path: Path) -> None:
    spec_yaml = """
schema_version: 1
name: grid-sample
purpose: elicitation
tasks:
  - canary/event-summary
  - task: canary/transaction-reconciliation
    task_path: tasks/transaction-reconciliation
agents:
  - oracle
  - agent: codex
    model: gpt-5.6-terra
preambles:
  - none
  - research/experiments/preambles/brief-discipline.md
attempts:
  - 1
  - 3
limits:
  max_specs: 20
  per_provider:
    codex:
      max_cost_usd: 5.0
"""
    yaml_file = tmp_path / "sample_grid.yaml"
    yaml_file.write_text(spec_yaml, encoding="utf-8")

    grid = load_grid_spec(yaml_file)
    assert grid.name == "grid-sample"
    assert grid.purpose == "elicitation"
    assert len(grid.tasks) == 2
    assert isinstance(grid.tasks[0], TaskSpec)
    assert grid.tasks[0].task == "canary/event-summary"
    assert grid.tasks[1].task_path == "tasks/transaction-reconciliation"

    assert len(grid.agents) == 2
    assert isinstance(grid.agents[0], AgentSpec)
    assert grid.agents[0].agent == "oracle"
    assert grid.agents[1].agent == "codex"
    assert grid.agents[1].model == "gpt-5.6-terra"

    assert grid.attempts == [1, 3]
    assert len(grid.preambles) == 2


def test_load_grid_spec_resolves_builtin_profile() -> None:
    grid = LadderGridSpec(
        name="profile-grid",
        purpose="elicitation",
        tasks=["canary/event-summary"],
        agents=["codex-gpt-5.6-terra"],
    )
    assert len(grid.agents) == 1
    assert grid.agents[0].agent == "codex"
    assert grid.agents[0].model == "gpt-5.6-terra"


def test_grid_spec_rejects_control_with_model() -> None:
    with pytest.raises(ValidationError):
        LadderGridSpec(
            name="bad-control-grid",
            purpose="practice",
            tasks=["canary/event-summary"],
            agents=[AgentSpec(agent="oracle", model="gpt-4")],
        )


def test_grid_spec_rejects_empty_tasks_or_agents() -> None:
    with pytest.raises(ValidationError):
        LadderGridSpec(name="empty-tasks", purpose="practice", tasks=[], agents=["oracle"])

    with pytest.raises(ValidationError):
        LadderGridSpec(
            name="empty-agents",
            purpose="practice",
            tasks=["canary/event-summary"],
            agents=[],
        )


def test_generate_grid_cartesian_expansion() -> None:
    # 2 tasks * 2 agents * 2 preambles * 2 attempts = 16 specs
    grid = LadderGridSpec(
        name="cartesian-grid",
        purpose="elicitation",
        tasks=["task-a", "task-b"],
        agents=["oracle", "nop"],
        preambles=["none", "brief-discipline"],
        attempts=[1, 3],
        check_quota_headroom=False,
    )
    result = generate_grid(grid)
    assert result.total_specs == 16
    assert len(result.specs) == 16
    assert len(result.skipped) == 0

    for s in result.specs:
        assert isinstance(s, ExperimentSpec)
        assert s.purpose == "elicitation"
        assert s.agent in {"oracle", "nop"}
        assert s.attempts in {1, 3}
        assert s.est_cost_usd == 0.0
        assert s.grid_id == "cartesian-grid"
        assert s.grid_point is not None


def test_generate_grid_custom_hypothesis_template() -> None:
    grid = LadderGridSpec(
        name="hypo-grid",
        purpose="comparison",
        tasks=["canary/event-summary"],
        agents=["codex"],
        preambles=["brief-discipline"],
        attempts=[3],
        hypothesis_template="Testing {agent} on {task} with {preamble} at k={k} for {purpose}",
        check_quota_headroom=False,
    )
    result = generate_grid(grid)
    assert len(result.specs) == 1
    spec = result.specs[0]
    assert spec.hypothesis == (
        "Testing codex on canary/event-summary with brief-discipline at k=3 for comparison"
    )
    assert spec.purpose == "comparison"


def test_generate_grid_writes_valid_json_files(tmp_path: Path) -> None:
    out_dir = tmp_path / "queue" / "proposed"
    grid = LadderGridSpec(
        name="write-grid",
        purpose="practice",
        tasks=["tasks/event-summary"],
        agents=["oracle", "nop"],
        attempts=[1],
        check_quota_headroom=False,
    )
    result = generate_grid(grid, output_dir=out_dir)
    assert len(result.written_paths) == 2
    for p in result.written_paths:
        assert p.is_file()
        loaded = ExperimentSpec.model_validate_json(p.read_text(encoding="utf-8"))
        assert loaded.purpose == "practice"
        assert loaded.name == p.stem
        assert loaded.grid_id == "write-grid"


def test_generate_grid_respects_global_max_specs() -> None:
    grid = LadderGridSpec(
        name="limit-specs-grid",
        purpose="practice",
        tasks=["task-1", "task-2", "task-3"],
        agents=["oracle", "nop"],
        attempts=[1],
        limits=GridLimits(max_specs=3),
        check_quota_headroom=False,
    )
    result = generate_grid(grid)
    assert result.total_specs == 3
    assert len(result.specs) == 3
    assert len(result.skipped) == 3
    assert "global max_specs limit (3) reached" in result.skipped[0].reason


def test_generate_grid_respects_global_max_trials() -> None:
    grid = LadderGridSpec(
        name="limit-trials-grid",
        purpose="practice",
        tasks=["task-1", "task-2"],
        agents=["oracle"],
        attempts=[5],
        limits=GridLimits(max_trials=7),
        check_quota_headroom=False,
    )
    result = generate_grid(grid)
    assert result.total_specs == 1
    assert result.total_trials == 5
    assert len(result.skipped) == 1
    assert "global max_trials limit (7) would be exceeded" in result.skipped[0].reason


def test_generate_grid_respects_global_max_cost() -> None:
    grid = LadderGridSpec(
        name="limit-cost-grid",
        purpose="practice",
        tasks=["task-1", "task-2", "task-3"],
        agents=[AgentSpec(agent="codex", est_cost_per_trial_usd=1.0)],
        attempts=[2],
        limits=GridLimits(max_cost_usd=3.0),
        check_quota_headroom=False,
    )
    result = generate_grid(grid)
    assert result.total_specs == 1
    assert result.total_estimated_cost_usd == 2.0
    assert len(result.skipped) == 2
    assert "global max_cost_usd limit ($3.00) would be exceeded" in result.skipped[0].reason


def test_generate_grid_respects_per_provider_limits() -> None:
    grid = LadderGridSpec(
        name="provider-limit-grid",
        purpose="practice",
        tasks=["task-1", "task-2"],
        agents=[
            AgentSpec(agent="codex", est_cost_per_trial_usd=0.5),
            AgentSpec(agent="claude-code", est_cost_per_trial_usd=0.5),
            AgentSpec(agent="oracle"),
        ],
        attempts=[1],
        limits=GridLimits(
            per_provider={
                "codex": ProviderLimit(max_specs=1),
                "claude-code": ProviderLimit(max_cost_usd=0.4),
            }
        ),
        check_quota_headroom=False,
    )
    result = generate_grid(grid)
    assert result.by_provider["codex"].specs_count == 1
    assert result.by_provider["claude-code"].specs_count == 0
    assert result.by_provider["oracle"].specs_count == 2
    assert result.total_specs == 3
    assert len(result.skipped) == 3


def test_generate_grid_skips_paid_agents_on_exhausted_headroom() -> None:
    exhausted_headroom = Headroom(
        availability="observed",
        used_percent=100.0,
        rate_limit_reached_type="tokens",
    )
    grid = LadderGridSpec(
        name="quota-headroom-grid",
        purpose="practice",
        tasks=["canary/event-summary"],
        agents=["codex", "oracle"],
        attempts=[1],
        check_quota_headroom=True,
    )
    result = generate_grid(grid, headroom_override=exhausted_headroom)
    assert result.total_specs == 1
    assert result.specs[0].agent == "oracle"
    assert len(result.skipped) == 1
    assert result.skipped[0].agent == "codex"
    assert "provider reported quota exhausted" in result.skipped[0].reason


def test_cli_generate_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    spec_data = {
        "schema_version": 1,
        "name": "cli-test-grid",
        "purpose": "baseline",
        "tasks": ["canary/event-summary"],
        "agents": ["oracle"],
        "attempts": [1],
        "check_quota_headroom": False,
    }
    spec_file = tmp_path / "cli_grid.yaml"
    spec_file.write_text(yaml.dump(spec_data), encoding="utf-8")
    out_dir = tmp_path / "out_specs"

    ret = main(["generate", str(spec_file), "-o", str(out_dir)])
    assert ret == 0

    captured = capsys.readouterr()
    assert "LADDER Grid Generation: 1 specs generated" in captured.out
    assert (out_dir / "cli-test-grid-canary-event-summary-oracle-k1.json").is_file()


def test_cli_generate_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    spec_data = {
        "name": "json-output-grid",
        "purpose": "elicitation",
        "tasks": ["canary/event-summary"],
        "agents": ["oracle"],
        "attempts": [1],
        "check_quota_headroom": False,
    }
    spec_file = tmp_path / "json_grid.json"
    spec_file.write_text(json.dumps(spec_data), encoding="utf-8")

    ret = main(["generate", str(spec_file), "--json", "--no-quota-check"])
    assert ret == 0

    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["total_specs"] == 1
    assert parsed["specs"][0]["name"] == "json-output-grid-canary-event-summary-oracle-k1"
    assert parsed["specs"][0]["purpose"] == "elicitation"


def test_module_execution_via_python_m(tmp_path: Path) -> None:
    spec_data = {
        "name": "subprocess-grid",
        "purpose": "elicitation",
        "tasks": ["canary/event-summary"],
        "agents": ["oracle"],
        "attempts": [1],
        "check_quota_headroom": False,
    }
    spec_file = tmp_path / "sub_grid.yaml"
    spec_file.write_text(yaml.dump(spec_data), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "evallab.ladder", "generate", str(spec_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "LADDER Grid Generation: 1 specs generated" in proc.stdout


def test_load_grid_spec_errors(tmp_path: Path) -> None:
    non_existent = tmp_path / "does_not_exist.yaml"
    with pytest.raises(FileNotFoundError):
        load_grid_spec(non_existent)

    invalid_yaml = tmp_path / "invalid.yaml"
    invalid_yaml.write_text("foo: [unclosed bracket", encoding="utf-8")
    with pytest.raises(ValueError, match="Failed to parse YAML"):
        load_grid_spec(invalid_yaml)

    non_dict = tmp_path / "list.yaml"
    non_dict.write_text("- item1\n- item2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a dictionary/mapping"):
        load_grid_spec(non_dict)


def test_grid_spec_jobs_dir_validation() -> None:
    with pytest.raises(ValueError):
        LadderGridSpec(
            name="bad-jobs-dir",
            purpose="practice",
            tasks=["task1"],
            agents=["oracle"],
            jobs_dir="/etc/runs",
        )


def test_grid_spec_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        LadderGridSpec.model_validate({
            "name": "extra-field-grid",
            "purpose": "practice",
            "tasks": ["task1"],
            "agents": ["oracle"],
            "unexpected_extra_field": "value",
        })


def test_all_purposes_accepted() -> None:
    for purpose in (
        "baseline",
        "comparison",
        "elicitation",
        "drift",
        "calibration",
        "craft",
        "practice",
    ):
        grid = LadderGridSpec(
            name=f"grid-{purpose}",
            purpose=purpose,  # type: ignore[arg-type]
            tasks=["task1"],
            agents=["oracle"],
            check_quota_headroom=False,
        )
        res = generate_grid(grid)
        assert res.specs[0].purpose == purpose


def test_cli_error_handling(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ret = main(["generate", str(tmp_path / "missing.yaml")])
    assert ret == 1
    captured = capsys.readouterr()
    assert "Error generating grid:" in captured.err

    ret_no_args = main([])
    assert ret_no_args == 1


# ---------------------------------------------------------------------------
# Specific test requirements from v2 §4 and Mission Brief
# ---------------------------------------------------------------------------


def test_grid_spec_rejects_missing_purpose(tmp_path: Path) -> None:
    """A grid without purpose is rejected at load with a message naming the field."""
    raw_yaml = """
schema_version: 1
grid_id: grid-no-purpose
axes:
  task_refs:
    - canary/event-summary
  agents:
    - oracle
  preamble:
    - none
  k:
    - 1
"""
    grid_file = tmp_path / "no_purpose.yaml"
    grid_file.write_text(raw_yaml, encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        load_grid_spec(grid_file)
    assert "purpose" in str(excinfo.value)

    # Also via dict
    with pytest.raises(ValueError) as excinfo_dict:
        load_grid_spec({
            "grid_id": "grid-no-purpose",
            "axes": {"task_refs": ["t1"], "agents": ["oracle"]},
        })
    assert "purpose" in str(excinfo_dict.value)


def test_grid_expands_exact_point_set_minus_constraints() -> None:
    """A fixture grid expands to the exact expected cross-product minus constraints."""
    grid = GridSpec(
        grid_id="grid-axes-test",
        purpose="elicitation",
        axes=GridAxes(
            task_refs=["tasks/task-a", "tasks/task-b"],
            agents=["oracle", "nop"],
            preamble=["none", "p1.md"],
            k=[1, 3],
        ),
        constraints=[
            {"agent": "nop", "k": 3},  # Excludes 4 points
            {"task_ref": "tasks/task-a", "preamble": "p1.md"},  # Excludes 3 points
        ],
        check_quota_headroom=False,
    )
    result = generate_grid(grid)

    # Full cross-product: 2 * 2 * 2 * 2 = 16 points.
    # Excluded points:
    # 1) agent=nop, k=3 -> 4 points
    # 2) task=task-a, preamble=p1.md -> 3 points
    # Total excluded: 7 points -> Expected emitted: 16 - 7 = 9 points.
    # Expected emitted: 16 - 7 = 9 points.

    assert result.total_specs == 9
    assert len(result.specs) == 9

    emitted_points = {
        (
            s.grid_point["task_ref"],
            s.grid_point["agent"],
            s.grid_point["preamble"],
            s.grid_point["k"],
        )
        for s in result.specs
    }

    expected_points = {
        ("tasks/task-a", "oracle", "none", 1),
        ("tasks/task-a", "oracle", "none", 3),
        ("tasks/task-a", "nop", "none", 1),
        ("tasks/task-b", "oracle", "none", 1),
        ("tasks/task-b", "oracle", "none", 3),
        ("tasks/task-b", "oracle", "p1.md", 1),
        ("tasks/task-b", "oracle", "p1.md", 3),
        ("tasks/task-b", "nop", "none", 1),
        ("tasks/task-b", "nop", "p1.md", 1),
    }

    assert emitted_points == expected_points


def test_resume_not_duplicate_emits_only_missing_points(tmp_path: Path) -> None:
    """With existing points, a second generate emits only missing ones."""
    grid = GridSpec(
        grid_id="grid-resume-test",
        purpose="elicitation",
        axes=GridAxes(
            task_refs=["tasks/task-1", "tasks/task-2"],
            agents=["oracle", "nop"],
            preamble=["none"],
            k=[1, 3],
        ),
        check_quota_headroom=False,
    )
    # Total points: 2 * 2 * 1 * 2 = 8 points.

    out_dir = tmp_path / "queue" / "proposed"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Pre-populate 3 points in queue directory
    existing_spec_1 = ExperimentSpec(
        name="grid-resume-test-tasks-task-1-oracle-k1",
        hypothesis="hypothesis",
        purpose="elicitation",
        task="tasks/task-1",
        agent="oracle",
        attempts=1,
        submitted_by="test",
        grid_id="grid-resume-test",
        grid_point={"task_ref": "tasks/task-1", "agent": "oracle", "preamble": "none", "k": 1},
    )
    existing_spec_2 = ExperimentSpec(
        name="grid-resume-test-tasks-task-2-oracle-k3",
        hypothesis="hypothesis",
        purpose="elicitation",
        task="tasks/task-2",
        agent="oracle",
        attempts=3,
        submitted_by="test",
        grid_id="grid-resume-test",
        grid_point={"task_ref": "tasks/task-2", "agent": "oracle", "preamble": "none", "k": 3},
    )
    existing_spec_3 = ExperimentSpec(
        name="grid-resume-test-tasks-task-1-nop-k1",
        hypothesis="hypothesis",
        purpose="elicitation",
        task="tasks/task-1",
        agent="nop",
        attempts=1,
        submitted_by="test",
        grid_id="grid-resume-test",
        grid_point={"task_ref": "tasks/task-1", "agent": "nop", "preamble": "none", "k": 1},
    )

    for s in (existing_spec_1, existing_spec_2, existing_spec_3):
        (out_dir / f"{s.name}.json").write_text(s.model_dump_json(indent=2), encoding="utf-8")

    # Run generation with output_dir set
    result = generate_grid(grid, output_dir=out_dir, repo_root=tmp_path)

    # 8 total - 3 existing = 5 emitted
    assert result.total_specs == 5
    assert len(result.specs) == 5
    assert len(result.deduped) == 3

    emitted_points = {
        (
            s.grid_point["task_ref"],
            s.grid_point["agent"],
            s.grid_point["preamble"],
            s.grid_point["k"],
        )
        for s in result.specs
    }
    existing_point_set = {
        ("tasks/task-1", "oracle", "none", 1),
        ("tasks/task-2", "oracle", "none", 3),
        ("tasks/task-1", "nop", "none", 1),
    }

    # Zero overlap / zero duplicates
    assert emitted_points.isdisjoint(existing_point_set)

    # Re-running again when all 8 points are now written emits 0 specs and 8 deduped
    result_again = generate_grid(grid, output_dir=out_dir, repo_root=tmp_path)
    assert result_again.total_specs == 0
    assert len(result_again.specs) == 0
    assert len(result_again.deduped) == 8
    assert len(list(out_dir.glob("*.json"))) == 8

    # Third consecutive run: writes 0, dedupes 8, files on disk remains 8
    result_third = generate_grid(grid, output_dir=out_dir, repo_root=tmp_path)
    assert result_third.total_specs == 0
    assert len(result_third.specs) == 0
    assert len(result_third.deduped) == 8
    assert len(list(out_dir.glob("*.json"))) == 8


def test_daily_budget_units_truncation_and_withholding_report() -> None:
    """A small budget emits a prefix and reports withheld points with reason."""
    grid = GridSpec(
        grid_id="grid-budget-test",
        purpose="baseline",
        axes=GridAxes(
            task_refs=["tasks/task-1", "tasks/task-2", "tasks/task-3"],
            agents=["oracle", "nop"],
            preamble=["none"],
            k=[1, 2],
        ),
        daily_budget_units=4,  # Only 4 attempts fit
        check_quota_headroom=False,
    )
    result = generate_grid(grid)

    assert result.total_trials <= 4
    assert len(result.specs) < 12
    assert len(result.skipped) > 0

    # Assert every withheld spec carries a clear reason
    for skipped in result.skipped:
        assert "daily_budget_units limit (4) would be exceeded" in skipped.reason

    summary = result.summary()
    assert "Withheld specs" in summary
    assert "daily_budget_units limit (4) would be exceeded" in summary


def test_dry_run_is_default_and_writes_nothing(tmp_path: Path) -> None:
    """--dry-run writes nothing: digest of queue directory is unchanged before and after."""
    queue_dir = tmp_path / "queue"
    for state in ("proposed", "pending", "approved", "waiting", "running", "done", "failed"):
        (queue_dir / state).mkdir(parents=True, exist_ok=True)

    initial_digest = _dir_hash(queue_dir)

    grid_file = tmp_path / "grid.yaml"
    grid_file.write_text(
        yaml.dump({
            "schema_version": 1,
            "grid_id": "dry-run-grid",
            "purpose": "practice",
            "axes": {
                "task_refs": ["canary/event-summary"],
                "agents": ["oracle"],
                "preamble": ["none"],
                "k": [1],
            },
        }),
        encoding="utf-8",
    )

    # Run ladder via cli in workspace tmp_path (default dry run)
    exit_code = cli.run_cli(["ladder", "generate", str(grid_file)], workspace=tmp_path)
    assert exit_code == 0

    post_digest = _dir_hash(queue_dir)
    assert initial_digest == post_digest


def test_output_is_byte_identical_across_two_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Output is byte-identical across two runs."""
    grid_file = tmp_path / "identical_grid.yaml"
    grid_file.write_text(
        yaml.dump({
            "schema_version": 1,
            "grid_id": "byte-identical-grid",
            "purpose": "elicitation",
            "axes": {
                "task_refs": ["canary/event-summary", "tasks/transaction-reconciliation"],
                "agents": ["oracle", "nop"],
                "preamble": ["none", "brief-discipline.md"],
                "k": [1, 3],
            },
            "constraints": [{"agent": "nop", "k": 3}],
            "daily_budget_units": 10,
        }),
        encoding="utf-8",
    )

    # Run 1
    ret1 = main(["generate", str(grid_file), "--json", "--no-quota-check"])
    assert ret1 == 0
    out1 = capsys.readouterr().out

    # Run 2
    ret2 = main(["generate", str(grid_file), "--json", "--no-quota-check"])
    assert ret2 == 0
    out2 = capsys.readouterr().out

    assert out1 == out2
    assert out1.encode("utf-8") == out2.encode("utf-8")


def test_example_grid_file_in_grids_directory_validates(tmp_path: Path) -> None:
    """The committed example grid under grids/ is valid and expands cleanly."""
    example_path = Path(__file__).resolve().parents[1] / "grids" / "event-summary-elicitation.yaml"
    assert example_path.is_file()

    grid = load_grid_spec(example_path)
    assert grid.grid_id == "grid-event-summary-elicitation"
    assert grid.purpose == "elicitation"
    assert grid.daily_budget_units == 20

    out_dir = tmp_path / "queue" / "proposed"

    # Run 1: Writes N=12 files, 0 deduped, 12 files on disk
    result = generate_grid(grid, output_dir=out_dir, repo_root=tmp_path, check_quota_headroom=False)
    assert result.total_specs == 12
    assert len(result.specs) == 12
    assert len(result.written_paths) == 12
    assert len(result.deduped) == 0

    # Cardinality assertion: len(set(names)) == len(points)
    spec_names = [s.name for s in result.specs]
    assert len(set(spec_names)) == len(result.specs) == 12

    # Files on disk matches total_specs after first run
    files_on_disk = list(out_dir.glob("*.json"))
    assert len(files_on_disk) == 12

    # Run 2: Writes 0, dedupes N=12, files on disk remains 12
    result_run2 = generate_grid(
        grid, output_dir=out_dir, repo_root=tmp_path, check_quota_headroom=False
    )
    assert result_run2.total_specs == 0
    assert len(result_run2.written_paths) == 0
    assert len(result_run2.deduped) == 12
    assert len(list(out_dir.glob("*.json"))) == 12

    # Run 3: Writes 0, dedupes N=12, files on disk remains 12 (convergence confirmed)
    result_run3 = generate_grid(
        grid, output_dir=out_dir, repo_root=tmp_path, check_quota_headroom=False
    )
    assert result_run3.total_specs == 0
    assert len(result_run3.written_paths) == 0
    assert len(result_run3.deduped) == 12
    assert len(list(out_dir.glob("*.json"))) == 12

def test_grid_axes_differing_only_in_path_coordinate_produce_distinct_files(tmp_path: Path) -> None:
    """A grid whose axes differ only in task path coordinates produces distinct files."""
    grid = GridSpec(
        grid_id="grid-path-coord-test",
        purpose="elicitation",
        axes=GridAxes(
            task_refs=["canary/event-summary", "tasks/event-summary"],
            agents=["oracle"],
            preamble=["none"],
            k=[1],
        ),
        check_quota_headroom=False,
    )
    out_dir = tmp_path / "queue" / "proposed"
    result = generate_grid(grid, output_dir=out_dir, repo_root=tmp_path)
    assert result.total_specs == 2
    assert len(result.specs) == 2
    assert len(result.written_paths) == 2
    assert result.specs[0].name != result.specs[1].name
    assert len(list(out_dir.glob("*.json"))) == 2
