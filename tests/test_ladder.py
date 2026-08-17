"""Tests for LADDER evaluation grid generator (src/evallab/ladder.py)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from evallab.ladder import (
    AgentSpec,
    GridLimits,
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


def test_sanitize_slug() -> None:
    assert sanitize_slug("tasks/event-summary") == "event-summary"
    assert sanitize_slug("research/experiments/preambles/brief-discipline.md") == "brief-discipline"
    assert sanitize_slug("Agent_With-Special.Chars!#") == "agent-with-special-chars"
    assert sanitize_slug("---leading-and-trailing---") == "leading-and-trailing"
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
    # Ensure it validates against ExperimentSpec name pattern
    spec = ExperimentSpec(
        name=name,
        hypothesis="test hypothesis",
        purpose="elicitation",
        task="event-summary",
        agent="codex",
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
        grid_name="very-long-grid-name",
        task_slug=long_task,
        agent_slug=long_agent,
        preamble_slug="none",
        attempts=5,
        max_len=80,
    )
    assert len(name) <= 80
    assert name.endswith("-k5")
    assert not name.endswith("--k5")
    # Validates against schema
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
            tasks=["canary/event-summary"],
            agents=[AgentSpec(agent="oracle", model="gpt-4")],
        )


def test_grid_spec_rejects_empty_tasks_or_agents() -> None:
    with pytest.raises(ValidationError):
        LadderGridSpec(name="empty-tasks", tasks=[], agents=["oracle"])

    with pytest.raises(ValidationError):
        LadderGridSpec(name="empty-agents", tasks=["canary/event-summary"], agents=[])


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

    # Verify all generated specs are valid ExperimentSpec models with purpose
    for s in result.specs:
        assert isinstance(s, ExperimentSpec)
        assert s.purpose == "elicitation"
        assert s.agent in {"oracle", "nop"}
        assert s.attempts in {1, 3}
        assert s.est_cost_usd == 0.0  # controls are free


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


def test_generate_grid_respects_global_max_specs() -> None:
    grid = LadderGridSpec(
        name="limit-specs-grid",
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
        tasks=["task-1", "task-2"],
        agents=["oracle"],
        attempts=[5],  # 2 specs * 5 trials = 10 trials
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
        tasks=["task-1", "task-2", "task-3"],
        agents=[AgentSpec(agent="codex", est_cost_per_trial_usd=1.0)],
        attempts=[2],  # Each spec costs $2.00
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
                "claude-code": ProviderLimit(max_cost_usd=0.4),  # $0.50 cost > $0.40 limit
            }
        ),
        check_quota_headroom=False,
    )
    result = generate_grid(grid)
    # codex gets 1 spec, claude-code gets 0 (exceeds cost), oracle gets 2 specs
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
        tasks=["canary/event-summary"],
        agents=["codex", "oracle"],
        attempts=[1],
        check_quota_headroom=True,
    )
    result = generate_grid(grid, headroom_override=exhausted_headroom)
    # Paid codex skipped, free oracle admitted
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
    assert (out_dir / "cli-test-grid-event-summary-oracle-k1.json").is_file()


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
    assert parsed["specs"][0]["name"] == "json-output-grid-event-summary-oracle-k1"
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
            tasks=["task1"],
            agents=["oracle"],
            jobs_dir="/etc/runs",
        )


def test_grid_spec_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        LadderGridSpec.model_validate({
            "name": "extra-field-grid",
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