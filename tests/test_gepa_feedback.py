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

    result = build_feedback(
        repo_root=repo_root,
        task_path=task_path,
        trial_path=trial_path,
        max_chars=32000,
    )

    feedback_text = result["feedback"]

    # Real actions and tool calls reached output
    assert "wc -l /app/input/events.jsonl" in feedback_text

    # Real observations reached output
    assert "Script completed" in feedback_text

    # Real final response reached output
    assert "Created and validated [summary.json](/app/output/summary.json)." in feedback_text

    # Sources and provenance
    assert result["sources"]["task_instruction"] == "library/tasks/event-summary/instruction.md"
    assert (
        result["sources"]["trial_trajectory"]
        == "research/evidence/runs/canary-event-summary-codex-20260815/event-summary__5E3btLv/agent/trajectory.json"
    )
    assert (
        result["sources"]["trial_result"]
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
    write_json(
        trial_dir / "result.json",
        {"task_name": "task_001", "verifier_result": {"rewards": {"reward": 1.0}}},
    )

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
    (task_dir / "instruction.md").write_text(
        "Long task instruction text explaining requirements in detail."
    )

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
    assert fb_generous["char_count"] == len(fb_generous["feedback"])
    assert not any("truncated" in notice for notice in fb_generous["coverage_notices"])

    # Tight budget: truncated honestly
    fb_tight = build_feedback(
        repo_root=repo_root,
        task_path=Path("tasks/task_001"),
        trial_path=Path("runs/trial_001"),
        max_chars=400,
    )
    assert fb_tight["truncated"] is True
    assert len(fb_tight["feedback"]) <= 400
    assert fb_tight["char_count"] <= 400
    assert any("truncated to max_chars=400" in notice for notice in fb_tight["coverage_notices"])
    assert "[Truncated" in fb_tight["feedback"]

    # Micro budget: strictly bounds length
    fb_micro = build_feedback(
        repo_root=repo_root,
        task_path=Path("tasks/task_001"),
        trial_path=Path("runs/trial_001"),
        max_chars=25,
    )
    assert fb_micro["truncated"] is True
    assert len(fb_micro["feedback"]) <= 25

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
    """Credentials, Bearer headers, and API keys are redacted from the full string."""
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
                        "arguments": {
                            "cmd": "curl -H 'Authorization: Bearer my-secret-bearer-999' http://api/auth"
                        },
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

    full_output = fb["feedback"]

    # Verify no raw secrets appear anywhere in feedback text
    assert "eyJhbGciOiJIUzI1NiJ9" not in full_output
    assert "sk-ant-api03-abcdef1234567890" not in full_output
    assert "my-secret-bearer-999" not in full_output
    assert "SuperSecretPassword123" not in full_output

    # Verify redacted placeholder is present
    assert "[redacted]" in full_output


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

    feedback_text = fb["feedback"]

    # Honest text representation
    assert "Trace Status: absent" in feedback_text
    assert "No agent model trajectory was recorded" in feedback_text
    assert any("absent" in n for n in fb["coverage_notices"])

    # Outcome is faithfully 0.0, never turned into success
    assert "Primary Reward: 0.0" in feedback_text
    assert '"correctness": {"message": "expected output missing", "passed": false}' in feedback_text
