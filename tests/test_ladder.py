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


def test_generate_grid_cartesian_expansion(tmp_path: Path) -> None:
    # 2 tasks * 2 agents * 2 preambles * 2 attempts = 16 specs
    (tmp_path / "brief-discipline").write_text("treatment\n")
    grid = LadderGridSpec(
        name="cartesian-grid",
        purpose="elicitation",
        tasks=["task-a", "task-b"],
        agents=["oracle", "nop"],
        preambles=["none", "brief-discipline"],
        attempts=[1, 3],
        check_quota_headroom=False,
    )
    result = generate_grid(grid, repo_root=tmp_path)
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


def test_generate_grid_custom_hypothesis_template(tmp_path: Path) -> None:
    (tmp_path / "brief-discipline").write_text("treatment\n")
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
    result = generate_grid(grid, repo_root=tmp_path)
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


def test_generate_grid_does_not_publish_partial_spec_when_atomic_publish_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_dir = tmp_path / "queue" / "proposed"
    grid = LadderGridSpec(
        name="atomic-write-grid",
        purpose="practice",
        tasks=["tasks/event-summary"],
        agents=["oracle"],
        check_quota_headroom=False,
    )
    destination: Path | None = None

    def fail_publish(source: Path, target: Path) -> None:
        nonlocal destination
        destination = Path(target)
        assert not destination.exists()
        raise OSError("simulated publish failure")

    monkeypatch.setattr("evallab.ladder.os.link", fail_publish)
    with pytest.raises(OSError, match="simulated publish failure"):
        generate_grid(grid, output_dir=out_dir)

    assert destination is not None
    assert not destination.exists()
    assert not list(out_dir.glob(".*.tmp"))


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


def test_grid_expands_exact_point_set_minus_constraints(tmp_path: Path) -> None:
    """A fixture grid expands to the exact expected cross-product minus constraints."""
    (tmp_path / "p1.md").write_text("treatment\n")
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
    result = generate_grid(grid, repo_root=tmp_path)

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
        ("tasks/task-a", "oracle", None, 1),
        ("tasks/task-a", "oracle", None, 3),
        ("tasks/task-a", "nop", None, 1),
        ("tasks/task-b", "oracle", None, 1),
        ("tasks/task-b", "oracle", None, 3),
        ("tasks/task-b", "oracle", "p1.md", 1),
        ("tasks/task-b", "oracle", "p1.md", 3),
        ("tasks/task-b", "nop", None, 1),
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
                "preamble": [
                    "none",
                    "research/experiments/preambles/brief-discipline.md",
                ],
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
    result = generate_grid(
        grid,
        output_dir=out_dir,
        repo_root=example_path.parents[1],
        check_quota_headroom=False,
    )
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
        grid, output_dir=out_dir, repo_root=example_path.parents[1], check_quota_headroom=False
    )
    assert result_run2.total_specs == 0
    assert len(result_run2.written_paths) == 0
    assert len(result_run2.deduped) == 12
    assert len(list(out_dir.glob("*.json"))) == 12

    # Run 3: Writes 0, dedupes N=12, files on disk remains 12 (convergence confirmed)
    result_run3 = generate_grid(
        grid, output_dir=out_dir, repo_root=example_path.parents[1], check_quota_headroom=False
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



def test_named_arms_factors_compile_to_bounded_restartable_shards(tmp_path: Path) -> None:
    grid = GridSpec.model_validate({
        "schema_version": 1,
        "grid_id": "agent-behavior-plan",
        "purpose": "elicitation",
        "axes": {
            "task_refs": ["task/a", "task/b"],
            "arms": [
                {
                    "arm_id": "baseline",
                    "agent": "oracle",
                    "factor_overrides": {"context_budget": 1200},
                },
                {
                    "arm_id": "treatment",
                    "agent": {"agent": "nop"},
                },
            ],
            "factors": {
                "context_budget": {
                    "binding": "timeout_seconds",
                    "levels": [1200, 1800],
                },
                "feedback": {"binding": "concurrency", "levels": [1, 2]},
            },
            "k": [1],
        },
        "shard_size": 3,
        "check_quota_headroom": False,
    })
    output = tmp_path / "plans"

    result = generate_grid(grid, output_dir=output, repo_root=tmp_path)

    # baseline fixes context_budget and crosses feedback: 2 tasks * 2 = 4.
    # treatment crosses both factors: 2 tasks * 2 * 2 = 8.
    assert result.total_specs == 12
    assert len(result.shards) == 4
    assert all(len(shard.spec_names) <= 3 for shard in result.shards)
    assert sum(shard.trial_count for shard in result.shards) == 12
    assert len({spec.grid_point["point_id"] for spec in result.specs}) == 12
    assert {
        spec.grid_point["arm_id"] for spec in result.specs
    } == {"baseline", "treatment"}
    manifest = json.loads(
        (output / "_plan/manifest-agent-behavior-plan.json").read_text()
    )
    assert manifest["spec_count"] == 12
    assert len(manifest["shards"]) == 4

    resumed = generate_grid(grid, output_dir=output, repo_root=tmp_path)
    assert resumed.total_specs == 0
    assert len(resumed.deduped) == 12


def test_factor_constraints_and_validate_cli(tmp_path: Path, capsys) -> None:
    plan = {
        "schema_version": 1,
        "grid_id": "factor-constraint-plan",
        "purpose": "elicitation",
        "axes": {
            "task_refs": ["task/a"],
            "arms": [
                {"arm_id": "baseline", "agent": "oracle"},
                {"arm_id": "treatment", "agent": "nop"},
            ],
            "factors": {
                "feedback": {"binding": "concurrency", "levels": [1, 2]}
            },
            "k": [1],
        },
        "constraints": [{"arm": "treatment", "factor.feedback": 1}],
        "shard_size": 2,
        "check_quota_headroom": False,
    }
    path = tmp_path / "plan.yaml"
    path.write_text(yaml.safe_dump(plan))

    result = generate_grid(plan, repo_root=tmp_path, dry_run=True)
    assert result.total_specs == 3
    assert len(result.shards) == 2

    rc = cli.run_cli(["ladder", "validate", str(path), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["spec_count"] == 3
    assert payload["shard_count"] == 2


def test_partial_resume_merges_manifest_with_continued_shard_indices(
    tmp_path: Path,
) -> None:
    grid = GridSpec.model_validate({
        "grid_id": "partial-plan",
        "purpose": "elicitation",
        "axes": {
            "task_refs": ["task/a", "task/b", "task/c", "task/d"],
            "agents": ["oracle"],
        },
        "limits": {"max_specs": 2},
        "shard_size": 1,
        "check_quota_headroom": False,
    })
    output = tmp_path / "queue" / "proposed"

    first = generate_grid(grid, output_dir=output, repo_root=tmp_path)
    second = generate_grid(grid, output_dir=output, repo_root=tmp_path)

    assert [shard.index for shard in first.shards] == [0, 1]
    assert [shard.index for shard in second.shards] == [2, 3]
    manifest = json.loads(
        (output / "_plan" / "manifest-partial-plan.json").read_text()
    )
    assert manifest["spec_count"] == 4
    assert [shard["index"] for shard in manifest["shards"]] == [0, 1, 2, 3]
    assert {
        path.name for path in (output / "_plan").glob("*.json")
    } == {
        "manifest-partial-plan.json",
        *(shard["path"] for shard in manifest["shards"]),
    }


def test_partial_resume_rejects_incompatible_manifest(tmp_path: Path) -> None:
    grid = GridSpec.model_validate({
        "grid_id": "incompatible-plan",
        "purpose": "elicitation",
        "axes": {"task_refs": ["task/a"], "agents": ["oracle"]},
        "shard_size": 1,
        "check_quota_headroom": False,
    })
    output = tmp_path / "queue" / "proposed"
    generate_grid(grid, output_dir=output, repo_root=tmp_path)
    manifest_path = output / "_plan" / "manifest-incompatible-plan.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["shard_size"] = 2
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match="incompatible plan manifest"):
        generate_grid(grid, output_dir=output, repo_root=tmp_path)


@pytest.mark.parametrize(
    "arm_id",
    ["has.dot", "has_under", "Uppercase", "-leading", "trailing-", "two--hyphens"],
)
def test_arm_id_is_slug_stable(arm_id: str) -> None:
    with pytest.raises(ValidationError):
        GridSpec.model_validate({
            "grid_id": "bad-arm",
            "purpose": "elicitation",
            "axes": {
                "task_refs": ["task/a"],
                "arms": [{"arm_id": arm_id, "agent": "oracle"}],
            },
        })


@pytest.mark.parametrize(
    "grid_order",
    [
        ("plan-a", "plan-a-stage2"),
        ("plan-a-stage2", "plan-a"),
    ],
)
def test_prefix_related_grids_share_plan_directory_in_both_orders(
    tmp_path: Path,
    grid_order: tuple[str, str],
) -> None:
    output = tmp_path / "queue" / "proposed"
    shard_names: set[str] = set()
    for grid_id in grid_order:
        grid = GridSpec.model_validate({
            "grid_id": grid_id,
            "purpose": "elicitation",
            "axes": {"task_refs": ["task/a"], "agents": ["oracle"]},
            "shard_size": 1,
            "check_quota_headroom": False,
        })
        result = generate_grid(grid, output_dir=output, repo_root=tmp_path)
        assert [shard.index for shard in result.shards] == [0]
        assert result.shards[0].path is not None
        shard_names.add(result.shards[0].path.name)

    plan_dir = output / "_plan"
    assert (plan_dir / "manifest-plan-a.json").is_file()
    assert (plan_dir / "manifest-plan-a-stage2.json").is_file()
    assert len(shard_names) == 2
    assert len(list(plan_dir.glob("*.json"))) == 4


def test_arms_reject_explicit_preamble_axis_and_invalid_factor_overrides() -> None:
    base = {
        "grid_id": "bad-axes",
        "purpose": "elicitation",
        "axes": {
            "task_refs": ["task/a"],
            "arms": [{"arm_id": "control", "agent": "oracle"}],
        },
    }
    with pytest.raises(ValidationError, match="preamble cannot be declared"):
        GridSpec.model_validate({
            **base,
            "axes": {**base["axes"], "preamble": ["none"]},
        })
    with pytest.raises(ValidationError, match="undeclared factor"):
        GridSpec.model_validate({
            **base,
            "axes": {
                **base["axes"],
                "arms": [{
                    "arm_id": "control",
                    "agent": "oracle",
                    "factor_overrides": {"missing": "value"},
                }],
            },
        })
    with pytest.raises(ValidationError, match="undeclared level"):
        GridSpec.model_validate({
            **base,
            "axes": {
                **base["axes"],
                "arms": [{
                    "arm_id": "control",
                    "agent": "oracle",
                    "factor_overrides": {"budget": 2},
                }],
                "factors": {
                    "budget": {"binding": "concurrency", "levels": [1]}
                },
            },
        })


@pytest.mark.parametrize(
    ("constraint", "message"),
    [
        ({"factor.missing": True}, "undeclared factor"),
        ({"factor.feedback": "unknown"}, "undeclared levels"),
        ({"arm": "unknown"}, "undeclared arms"),
    ],
)
def test_constraints_reject_undeclared_arm_and_factor_coordinates(
    constraint: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        GridSpec.model_validate({
            "grid_id": "bad-constraint",
            "purpose": "elicitation",
            "axes": {
                "task_refs": ["task/a"],
                "arms": [
                    {"arm_id": "control", "agent": "oracle"},
                    {"arm_id": "treatment", "agent": "nop"},
                ],
                "factors": {
                    "feedback": {"binding": "concurrency", "levels": [1, 2]}
                },
            },
            "constraints": [constraint],
        })


def test_skipped_arm_coordinates_are_preserved_and_serialized(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = {
        "grid_id": "withheld-coordinates",
        "purpose": "elicitation",
        "axes": {
            "task_refs": ["task/a", "task/b"],
            "arms": [{
                "arm_id": "treatment",
                "agent": "oracle",
                "factor_overrides": {"feedback": 2},
            }],
            "factors": {
                "feedback": {"binding": "concurrency", "levels": [1, 2]}
            },
        },
        "limits": {"max_specs": 1},
        "check_quota_headroom": False,
    }
    path = tmp_path / "plan.yaml"
    path.write_text(yaml.safe_dump(plan))

    rc = cli.run_cli(["ladder", "generate", str(path), "--json"], workspace=tmp_path)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped"] == [{
        "name": payload["skipped"][0]["name"],
        "task": "task/b",
        "agent": "oracle",
        "preamble": "none",
        "attempts": 1,
        "reason": "global max_specs limit (1) reached",
        "arm_id": "treatment",
        "factor_values": {"feedback": 2},
    }]


def test_validate_json_reports_resume_counts_and_invalid_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = {
        "grid_id": "validate-counts",
        "purpose": "elicitation",
        "axes": {
            "task_refs": ["task/a", "task/b"],
            "agents": ["oracle"],
        },
        "limits": {"max_specs": 1},
        "check_quota_headroom": False,
    }
    path = tmp_path / "plan.yaml"
    path.write_text(yaml.safe_dump(plan))
    generate_grid(
        plan,
        output_dir=tmp_path / "queue" / "proposed",
        repo_root=tmp_path,
    )

    rc = cli.run_cli(
        ["ladder", "validate", str(path), "--json"], workspace=tmp_path
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["declared_spec_count"] == 2
    assert payload["remaining_spec_count"] == 1
    assert payload["deduped_spec_count"] == 1
    assert payload["skipped_spec_count"] == 0

    bad_path = tmp_path / "bad.yaml"
    bad_path.write_text("purpose: elicitation\naxes: nope\n")
    rc = cli.run_cli(
        ["ladder", "validate", str(bad_path), "--json"], workspace=tmp_path
    )
    error_payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert error_payload["valid"] is False
    assert error_payload["errors"]


def test_module_entry_json_serializes_skipped_and_deduped_arm_coordinates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = {
        "grid_id": "module-coordinates",
        "purpose": "elicitation",
        "axes": {
            "task_refs": ["task/a", "task/b", "task/c"],
            "arms": [{
                "arm_id": "treatment",
                "agent": "oracle",
                "factor_overrides": {"feedback": 2},
            }],
            "factors": {
                "feedback": {"binding": "concurrency", "levels": [1, 2]}
            },
        },
        "limits": {"max_specs": 1},
        "check_quota_headroom": False,
    }
    path = tmp_path / "plan.yaml"
    path.write_text(yaml.safe_dump(plan))
    generate_grid(
        plan,
        output_dir=tmp_path / "queue" / "proposed",
        repo_root=tmp_path,
    )
    monkeypatch.chdir(tmp_path)

    rc = main(["generate", str(path), "--json", "--no-quota-check"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    for record in [*payload["skipped"], *payload["deduped"]]:
        assert record["arm_id"] == "treatment"
        assert record["factor_values"] == {"feedback": 2}
    assert len(payload["skipped"]) == 1
    assert len(payload["deduped"]) == 1


def test_bound_factor_levels_change_resolved_execution_field(tmp_path: Path) -> None:
    grid = GridSpec.model_validate({
        "grid_id": "bound-timeout",
        "purpose": "elicitation",
        "axes": {
            "task_refs": ["task/a"],
            "agents": ["oracle"],
            "factors": {
                "wall_clock": {
                    "binding": "timeout_seconds",
                    "levels": [60, 120],
                }
            },
        },
        "check_quota_headroom": False,
    })

    specs = generate_grid(grid, repo_root=tmp_path).specs

    assert [spec.timeout_seconds for spec in specs] == [60, 120]
    assert [spec.grid_point["factors"] for spec in specs] == [
        {"wall_clock": 60},
        {"wall_clock": 120},
    ]
    assert [spec.grid_point["bindings"] for spec in specs] == [
        {"timeout_seconds": 60},
        {"timeout_seconds": 120},
    ]


def test_unbound_factor_and_wrong_level_type_fail_closed() -> None:
    base = {
        "grid_id": "invalid-factor",
        "purpose": "elicitation",
        "axes": {"task_refs": ["task/a"], "agents": ["oracle"]},
    }
    with pytest.raises(ValidationError):
        GridSpec.model_validate({
            **base,
            "axes": {**base["axes"], "factors": {"annotation_only": [1, 2]}},
        })
    with pytest.raises(ValidationError, match="requires integer levels"):
        GridSpec.model_validate({
            **base,
            "axes": {
                **base["axes"],
                "factors": {
                    "wall_clock": {
                        "binding": "timeout_seconds",
                        "levels": ["short", "long"],
                    }
                },
            },
        })


def test_preamble_reaches_spec_with_content_provenance(tmp_path: Path) -> None:
    preamble = tmp_path / "instructions" / "treatment.txt"
    preamble.parent.mkdir()
    preamble.write_text("Use the treatment protocol.\n")
    grid = GridSpec(
        grid_id="preamble-binding",
        purpose="elicitation",
        axes=GridAxes(
            task_refs=["task/a"],
            agents=["oracle"],
            preamble=["none", "instructions/treatment.txt"],
        ),
        check_quota_headroom=False,
    )

    specs = generate_grid(grid, repo_root=tmp_path).specs

    assert [spec.extra_instruction_path for spec in specs] == [
        None,
        "instructions/treatment.txt",
    ]
    treatment = specs[1]
    assert treatment.grid_point["preamble_sha256"] == (
        "sha256:" + hashlib.sha256(preamble.read_bytes()).hexdigest()
    )


def test_missing_preamble_refuses_before_spec_emission(tmp_path: Path) -> None:
    grid = GridSpec(
        grid_id="missing-preamble",
        purpose="elicitation",
        axes=GridAxes(
            task_refs=["task/a"],
            agents=["oracle"],
            preamble=["instructions/missing.txt"],
        ),
        check_quota_headroom=False,
    )

    with pytest.raises(ValueError, match="preamble file does not exist"):
        generate_grid(grid, repo_root=tmp_path)


def test_factor_rebinding_changes_point_and_spec_identity(tmp_path: Path) -> None:
    def generate(binding: str):
        return generate_grid(
            GridSpec.model_validate({
                "grid_id": "rebind-identity",
                "purpose": "elicitation",
                "axes": {
                    "task_refs": ["task/a"],
                    "agents": ["oracle"],
                    "factors": {
                        "level": {"binding": binding, "levels": [2]}
                    },
                },
                "check_quota_headroom": False,
            }),
            repo_root=tmp_path,
        ).specs[0]

    concurrency = generate("concurrency")
    timeout = generate("timeout_seconds")

    assert concurrency.grid_point["factor_bindings"] == {"level": "concurrency"}
    assert timeout.grid_point["factor_bindings"] == {"level": "timeout_seconds"}
    assert concurrency.grid_point["point_id"] != timeout.grid_point["point_id"]
    assert concurrency.name != timeout.name


def test_resume_recomputes_and_ignores_stale_stored_point_id(tmp_path: Path) -> None:
    grid = GridSpec.model_validate({
        "grid_id": "resume-canonical-point",
        "purpose": "elicitation",
        "axes": {
            "task_refs": ["task/a"],
            "agents": ["oracle"],
            "factors": {
                "wall_clock": {
                    "binding": "timeout_seconds",
                    "levels": [60],
                }
            },
        },
        "check_quota_headroom": False,
    })
    output = tmp_path / "plans"
    first = generate_grid(grid, output_dir=output, repo_root=tmp_path)
    payload_path = first.written_paths[0]
    payload = json.loads(payload_path.read_text())
    payload["grid_point"]["point_id"] = "sha256:" + "0" * 64
    payload_path.write_text(json.dumps(payload))

    resumed = generate_grid(grid, output_dir=output, repo_root=tmp_path)

    assert resumed.total_specs == 0
    assert len(resumed.deduped) == 1


def test_preamble_axis_normalizes_before_point_identity(tmp_path: Path) -> None:
    preamble = tmp_path / "instructions" / "treatment.txt"
    preamble.parent.mkdir()
    preamble.write_text("treatment\n")

    def generate(path: str) -> ExperimentSpec:
        grid = GridSpec(
            grid_id="normalized-preamble",
            purpose="elicitation",
            axes=GridAxes(
                task_refs=["task/a"],
                agents=["oracle"],
                preamble=[path],
            ),
            check_quota_headroom=False,
        )
        return generate_grid(grid, repo_root=tmp_path).specs[0]

    raw = generate("./instructions//treatment.txt")
    canonical = generate("instructions/treatment.txt")

    assert raw.extra_instruction_path == "instructions/treatment.txt"
    assert raw.grid_point["preamble"] == "instructions/treatment.txt"
    assert raw.grid_point["point_id"] == canonical.grid_point["point_id"]
    assert raw.name == canonical.name