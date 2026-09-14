"""Focused behavioral tests for GEPA feedback builder."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab.gepa_optimizer.feedback import build_feedback


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Actual fixture trajectory content reaches output
# ---------------------------------------------------------------------------


def test_actual_fixture_trajectory_content_reaches_output() -> None:
    """Real committed ATIF fixture trajectory content and provenance reach feedback output."""
    repo_root = Path(__file__).resolve().parent.parent
    task_path = Path("library/tasks/event-summary")
    trial_path = Path(
        "research/evidence/runs/canary-event-summary-codex-20260815/event-summary__5E3btLv"
    )

    if not (repo_root / trial_path).exists():
        pytest.skip("canary-event-summary fixture not present")

    feedback = build_feedback(
        repo_root=repo_root,
        task_path=task_path,
        trial_path=trial_path,
        max_chars=32000,
    )

    # Core structure and trace status
    assert feedback["trace_status"] == "present"
    assert "event-summary" in str(feedback["task_id"])
    assert feedback["truncated"] is False
    assert feedback["char_count"] == len(feedback["feedback_text"])

    # Task instruction reached output
    assert feedback["task_instruction"] is not None
    assert "event summary" in feedback["task_instruction"].lower()
    assert "## Task Instruction" in feedback["feedback_text"]

    # Real actions and tool calls reached output
    assert len(feedback["actions"]) >= 5
    first_action = feedback["actions"][0]
    assert first_action["tool_name"] == "exec"
    assert "wc -l /app/input/events.jsonl" in str(first_action["tool_command"])
    assert "wc -l /app/input/events.jsonl" in feedback["feedback_text"]

    # Real observations reached output
    assert len(feedback["observations"]) >= 5
    assert any("Script completed" in str(obs["content"]) for obs in feedback["observations"])
    assert "Script completed" in feedback["feedback_text"]

    # Real final response reached output
    assert feedback["final_response"] is not None
    assert "summary.json" in feedback["final_response"]
    assert feedback["final_response"] in feedback["feedback_text"]

    # Sources and provenance
    assert feedback["sources"]["task_instruction"] == "library/tasks/event-summary/instruction.md"
    assert (
        feedback["sources"]["trial_trajectory"]
        == "research/evidence/runs/canary-event-summary-codex-20260815/event-summary__5E3btLv/agent/trajectory.json"
    )
    assert (
        feedback["sources"]["trial_result"]
        == "research/evidence/runs/canary-event-summary-codex-20260815/event-summary__5E3btLv/result.json"
    )


# ---------------------------------------------------------------------------
# 2. Malicious path and symlink rejection
# ---------------------------------------------------------------------------


def test_malicious_path_and_symlink_cannot_exfiltrate(tmp_path: Path) -> None:
    """Path traversal and symlink escapes outside declared roots are rejected."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    secret_file = outside_dir / "secret.txt"
    secret_file.write_text("SUPER_SECRET_HOST_DATA")

    task_dir = repo_root / "tasks" / "task_001"
    task_dir.mkdir(parents=True)
    (task_dir / "instruction.md").write_text("Legitimate task instruction.")

    trial_dir = repo_root / "runs" / "trial_001"
    trial_dir.mkdir(parents=True)
    write_json(trial_dir / "result.json", {"task_name": "task_001", "verifier_result": {"rewards": {"reward": 1.0}}})

    # Traversal in task_path escaping repo_root
    with pytest.raises(ValueError, match="escapes allowed root"):
        build_feedback(
            repo_root=repo_root,
            task_path=Path("../outside"),
            trial_path=Path("runs/trial_001"),
        )

    # Traversal in trial_path escaping repo_root
    with pytest.raises(ValueError, match="escapes allowed root"):
        build_feedback(
            repo_root=repo_root,
            task_path=Path("tasks/task_001"),
            trial_path=Path("../../outside"),
        )

    # Task path attempting to access hidden verifier / solution directories
    hidden_tests_dir = task_dir / "tests"
    hidden_tests_dir.mkdir()
    with pytest.raises(ValueError, match="forbidden hidden verification"):
        build_feedback(
            repo_root=repo_root,
            task_path=Path("tasks/task_001/tests"),
            trial_path=Path("runs/trial_001"),
        )

    hidden_sol_dir = task_dir / "solution"
    hidden_sol_dir.mkdir()
    with pytest.raises(ValueError, match="forbidden hidden verification"):
        build_feedback(
            repo_root=repo_root,
            task_path=Path("tasks/task_001/solution"),
            trial_path=Path("runs/trial_001"),
        )

    # Symlink escaping task root via instruction.md
    escaping_inst_task = repo_root / "tasks" / "symlink_escape_task"
    escaping_inst_task.mkdir(parents=True)
    (escaping_inst_task / "instruction.md").symlink_to(secret_file)

    with pytest.raises(ValueError, match="escapes allowed root"):
        build_feedback(
            repo_root=repo_root,
            task_path=Path("tasks/symlink_escape_task"),
            trial_path=Path("runs/trial_001"),
        )

    # Symlink escaping trial root via agent/trajectory.json
    escaping_trial = repo_root / "runs" / "symlink_escape_trial"
    (escaping_trial / "agent").mkdir(parents=True)
    write_json(escaping_trial / "result.json", {"task_name": "task_001"})
    (escaping_trial / "agent" / "trajectory.json").symlink_to(secret_file)

    with pytest.raises(ValueError, match="escapes allowed root"):
        build_feedback(
            repo_root=repo_root,
            task_path=Path("tasks/task_001"),
            trial_path=Path("runs/symlink_escape_trial"),
        )

    # Symlink escaping trial root via result.json
    escaping_result_trial = repo_root / "runs" / "symlink_result_trial"
    escaping_result_trial.mkdir(parents=True)
    (escaping_result_trial / "result.json").symlink_to(secret_file)

    with pytest.raises(ValueError, match="escapes allowed root"):
        build_feedback(
            repo_root=repo_root,
            task_path=Path("tasks/task_001"),
            trial_path=Path("runs/symlink_result_trial"),
        )


# ---------------------------------------------------------------------------
# 3. Budget truncation coverage is honest
# ---------------------------------------------------------------------------


def test_budget_truncation_coverage_is_honest(tmp_path: Path) -> None:
    """Character budget limits truncate gracefully and report coverage honestly."""
    repo_root = tmp_path / "repo"
    task_dir = repo_root / "tasks" / "task_001"
    task_dir.mkdir(parents=True)
    (task_dir / "instruction.md").write_text("Long task instruction text explaining requirements in detail.")

    trial_dir = repo_root / "runs" / "trial_001"
    (trial_dir / "agent").mkdir(parents=True)
    write_json(
        trial_dir / "result.json",
        {
            "task_name": "task_001",
            "verifier_result": {"rewards": {"reward": 0.5, "accuracy": 0.5}},
        },
    )
    trajectory = {
        "schema_version": "ATIF-v1.7",
        "agent": {"name": "test-agent", "version": "1.0"},
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "tool_calls": [
                    {
                        "tool_call_id": "c1",
                        "function_name": "bash",
                        "arguments": {"cmd": "echo 'running preliminary inspection'"},
                    }
                ],
                "observation": {
                    "results": [
                        {
                            "source_call_id": "c1",
                            "content": "Inspection line 1: file exists\nInspection line 2: permissions ok\n",
                        }
                    ]
                },
            },
            {
                "step_id": 2,
                "source": "agent",
                "message": "Completed processing and recorded results.",
            },
        ],
    }
    write_json(trial_dir / "agent" / "trajectory.json", trajectory)

    # Generous budget: not truncated
    fb_generous = build_feedback(
        repo_root=repo_root,
        task_path=Path("tasks/task_001"),
        trial_path=Path("runs/trial_001"),
        max_chars=24000,
    )
    assert fb_generous["truncated"] is False
    assert fb_generous["char_count"] == len(fb_generous["feedback_text"])
    assert not any("truncated" in notice for notice in fb_generous["coverage_notices"])

    # Tight budget: truncated honestly
    fb_tight = build_feedback(
        repo_root=repo_root,
        task_path=Path("tasks/task_001"),
        trial_path=Path("runs/trial_001"),
        max_chars=400,
    )
    assert fb_tight["truncated"] is True
    assert len(fb_tight["feedback_text"]) <= 400
    assert fb_tight["char_count"] <= 400
    assert any("truncated to max_chars=400" in notice for notice in fb_tight["coverage_notices"])
    assert "[Truncated" in fb_tight["feedback_text"]

    # Micro budget: strictly bounds length
    fb_micro = build_feedback(
        repo_root=repo_root,
        task_path=Path("tasks/task_001"),
        trial_path=Path("runs/trial_001"),
        max_chars=25,
    )
    assert fb_micro["truncated"] is True
    assert len(fb_micro["feedback_text"]) <= 25

    # Invalid non-positive budget
    with pytest.raises(ValueError, match="positive integer"):
        build_feedback(
            repo_root=repo_root,
            task_path=Path("tasks/task_001"),
            trial_path=Path("runs/trial_001"),
            max_chars=0,
        )


# ---------------------------------------------------------------------------
# 4. Secrets redacted
# ---------------------------------------------------------------------------


def test_secrets_redacted(tmp_path: Path) -> None:
    """Credentials, Bearer headers, and API keys are redacted across all feedback sections."""
    repo_root = tmp_path / "repo"
    task_dir = repo_root / "tasks" / "task_sec"
    task_dir.mkdir(parents=True)
    (task_dir / "instruction.md").write_text(
        "Deploy application using Bearer eyJhbGciOiJIUzI1NiJ9.secretpayload and api_key: sk-ant-api03-abcdef1234567890"
    )

    trial_dir = repo_root / "runs" / "trial_sec"
    (trial_dir / "agent").mkdir(parents=True)
    write_json(
        trial_dir / "result.json",
        {
            "task_name": "task_sec",
            "agent_info": {"name": "secret-agent"},
            "verifier_result": {"rewards": {"reward": 0.0}},
        },
    )
    write_json(
        trial_dir / "verifier" / "checks.json",
        {
            "auth_check": {
                "message": "Auth failed with token Bearer eyJhbGciOiJIUzI1NiJ9.secretpayload",
                "passed": False,
            }
        },
    )

    trajectory = {
        "schema_version": "ATIF-v1.7",
        "agent": {"name": "secret-agent"},
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "tool_calls": [
                    {
                        "tool_call_id": "c1",
                        "function_name": "bash",
                        "arguments": {"cmd": "curl -H 'Authorization: Bearer my-secret-bearer-999' http://api/auth"},
                    }
                ],
                "observation": {
                    "results": [
                        {
                            "source_call_id": "c1",
                            "content": "401 Unauthorized: password=SuperSecretPassword123 is invalid",
                        }
                    ]
                },
            },
            {
                "step_id": 2,
                "source": "agent",
                "message": "Attempted login with password=SuperSecretPassword123 and failed.",
            },
        ],
    }
    write_json(trial_dir / "agent" / "trajectory.json", trajectory)

    fb = build_feedback(
        repo_root=repo_root,
        task_path=Path("tasks/task_sec"),
        trial_path=Path("runs/trial_sec"),
        max_chars=24000,
    )

    full_output = fb["feedback_text"]

    # Verify no raw secrets appear anywhere in feedback text
    assert "eyJhbGciOiJIUzI1NiJ9" not in full_output
    assert "sk-ant-api03-abcdef1234567890" not in full_output
    assert "my-secret-bearer-999" not in full_output
    assert "SuperSecretPassword123" not in full_output

    # Verify redacted placeholder is present
    assert "[redacted]" in full_output

    # Verify structured action and observation arguments are also redacted
    action_args = str(fb["actions"][0]["arguments"])
    assert "my-secret-bearer-999" not in action_args

    obs_content = fb["observations"][0]["content"]
    assert "SuperSecretPassword123" not in obs_content

    final_resp = fb["final_response"]
    assert final_resp is not None
    assert "SuperSecretPassword123" not in final_resp


# ---------------------------------------------------------------------------
# 5. Missing trace does not become success evidence
# ---------------------------------------------------------------------------


def test_missing_trace_does_not_become_success_evidence(tmp_path: Path) -> None:
    """Missing model trace in control agents stays absent and never fabricates success."""
    repo_root = tmp_path / "repo"
    task_dir = repo_root / "tasks" / "task_nop"
    task_dir.mkdir(parents=True)
    (task_dir / "instruction.md").write_text("Perform data transformation.")

    trial_dir = repo_root / "runs" / "trial_nop"
    trial_dir.mkdir(parents=True)
    # Control agent nop with zero reward and no trajectory.json
    write_json(
        trial_dir / "result.json",
        {
            "task_name": "task_nop",
            "agent_info": {"name": "nop"},
            "verifier_result": {
                "rewards": {
                    "correctness": 0.0,
                    "reward": 0.0,
                }
            },
        },
    )
    write_json(
        trial_dir / "verifier" / "checks.json",
        {
            "correctness": {"message": "expected output missing", "passed": False},
        },
    )

    fb = build_feedback(
        repo_root=repo_root,
        task_path=Path("tasks/task_nop"),
        trial_path=Path("runs/trial_nop"),
        max_chars=24000,
    )

    # Trace is explicitly absent
    assert fb["trace_status"] == "absent"
    assert fb["actions"] == []
    assert fb["observations"] == []
    assert fb["final_response"] is None

    # Outcome is faithfully 0.0, never turned into success
    assert fb["outcome"]["primary_reward"] == 0.0
    assert fb["outcome"]["rewards"]["correctness"] == 0.0
    assert fb["outcome"]["verifier_diagnostics"]["correctness"]["passed"] is False

    # Honest text representation
    assert "Trace Status: absent" in fb["feedback_text"]
    assert "No agent model trajectory was recorded" in fb["feedback_text"]
    assert any("absent" in n for n in fb["coverage_notices"])


# ---------------------------------------------------------------------------
# 6. Actual prior control jobs under gepa-learning runs
# ---------------------------------------------------------------------------


def test_prior_control_jobs_gepa_learning() -> None:
    """Validate helper against actual prior control jobs under gepa-learning-20260914/runs."""
    runs_dir = Path(
        "/Users/petermakhnatch/Developer/eval-lab/.worktrees/gepa-learning-20260914/runs"
    )
    if not runs_dir.exists():
        pytest.skip("gepa-learning-20260914 worktree runs not present")

    repo_root = runs_dir.parent
    task_path = Path("library/tasks/event-summary")

    # 1. Nop control trial
    nop_trial = Path(
        "runs/gepa-nop-nomodel-event-summary-ef18429fceeae24d/gepa-nop-nomodel-event-summary-e__7WVcjyr"
    )
    if (repo_root / nop_trial).exists():
        fb_nop = build_feedback(
            repo_root=repo_root,
            task_path=task_path,
            trial_path=nop_trial,
            max_chars=24000,
        )
        assert fb_nop["trace_status"] == "absent"
        assert fb_nop["outcome"]["primary_reward"] == 0.0
        assert fb_nop["sources"]["task_instruction"] == "library/tasks/event-summary/instruction.md"
        assert "wrong summary" in str(fb_nop["outcome"]["verifier_diagnostics"])
        assert "Trace Status: absent" in fb_nop["feedback_text"]

    # 2. Oracle control trial
    oracle_trial = Path(
        "runs/gepa-oracle-nomodel-event-summary-ef18429fceeae24d/gepa-oracle-nomodel-event-summary-e__ANEqh2m"
    )
    if (repo_root / oracle_trial).exists():
        fb_oracle = build_feedback(
            repo_root=repo_root,
            task_path=task_path,
            trial_path=oracle_trial,
            max_chars=24000,
        )
        assert fb_oracle["trace_status"] == "absent"
        assert fb_oracle["outcome"]["primary_reward"] == 1.0
        assert fb_oracle["sources"]["task_instruction"] == "library/tasks/event-summary/instruction.md"
        assert "Trace Status: absent" in fb_oracle["feedback_text"]
