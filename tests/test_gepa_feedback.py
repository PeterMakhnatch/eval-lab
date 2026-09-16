"""Focused behavioral tests for GEPA feedback builder."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import hashlib
from evallab.gepa_optimizer.feedback import build_feedback, validate_oracle_reference
from evallab.registry import task_directory_digest


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


# ---------------------------------------------------------------------------
# 6. Realistic compact fixture factory & Oracle reference tests
# ---------------------------------------------------------------------------


def _make_env(
    root: Path,
    *,
    task_name: str = "task_001",
    reward: float = 1.0,
    agent_name: str = "oracle",
    model: str | None = None,
    error: str | None = None,
    finished: bool = True,
    status: str = "completed",
    meta_digest: str | None = None,
    omit_meta: bool = False,
    oracle_txt: str | None = None,
    state_diff: dict[str, Any] | None = None,
    agent_traj: dict[str, Any] | None = None,
) -> tuple[Path, Path, Path, dict[str, Any]]:
    task_dir = root / "tasks" / task_name
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "instruction.md").write_text("Do the task.", encoding="utf-8")
    pkg_digest = task_directory_digest(task_dir)

    agent_dir = root / "runs" / "agent_trial"
    agent_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        agent_dir / "result.json",
        {"task_name": task_name, "status": "completed", "verifier_result": {"rewards": {"reward": 0.0}}},
    )
    if agent_traj:
        write_json(agent_dir / "agent" / "trajectory.json", agent_traj)

    oracle_dir = root / "runs" / "oracle_job" / "oracle_trial"
    oracle_dir.mkdir(parents=True, exist_ok=True)
    res_data: dict[str, Any] = {
        "task_name": task_name, "status": status,
        "agent_info": {"name": agent_name}, "verifier_result": {"rewards": {"reward": reward}},
        **({"finished_at": "2026-09-16T00:00:00Z"} if finished else {}),
        **({"exception_info": error} if error else {}),
        **({"config": {"agent": {"name": agent_name, "model_name": model}}} if model else {}),
    }
    write_json(oracle_dir / "result.json", res_data)
    res_sha = hashlib.sha256((oracle_dir / "result.json").read_bytes()).hexdigest()

    if not omit_meta:
        write_json(
            oracle_dir.parent / "lab-metadata.json",
            {"experiment": {"package_digest": meta_digest or pkg_digest}},
        )
    if oracle_txt is not None:
        (oracle_dir / "agent").mkdir(parents=True, exist_ok=True)
        (oracle_dir / "agent" / "oracle.txt").write_text(oracle_txt, encoding="utf-8")
    if state_diff:
        write_json(oracle_dir / "state-journal" / "state-diff.json", state_diff)

    ref = {
        "trial_path": oracle_dir.relative_to(root),
        "result_sha256": res_sha,
        "task_package_digest": pkg_digest,
    }
    return root, task_dir.relative_to(root), agent_dir.relative_to(root), ref


def test_oracle_reference_bad_schema_and_escaping_path_fails_closed(tmp_path: Path) -> None:
    """Invalid oracle_reference schemas, path escapes, and hidden targets fail closed."""
    root, task, agent, ref = _make_env(tmp_path)
    assert validate_oracle_reference(root, task, ref) == (root / ref["trial_path"]).resolve()

    with pytest.raises(ValueError, match="oracle_reference must be a dictionary"):
        build_feedback(repo_root=root, task_path=task, trial_path=agent, oracle_reference="not-a-dict")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="oracle_reference must contain exact keys"):
        build_feedback(repo_root=root, task_path=task, trial_path=agent, oracle_reference={"trial_path": "runs/oracle"})

    with pytest.raises(ValueError, match="oracle_reference must contain exact keys"):
        build_feedback(repo_root=root, task_path=task, trial_path=agent, oracle_reference=dict(ref, extra_key="bad"))

    with pytest.raises(ValueError, match="escapes allowed root"):
        build_feedback(repo_root=root, task_path=task, trial_path=agent, oracle_reference=dict(ref, trial_path="../outside"))

    forbidden_dir = root / "tasks" / "task_001" / "tests"
    forbidden_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError, match="forbidden hidden"):
        build_feedback(repo_root=root, task_path=task, trial_path=agent, oracle_reference=dict(ref, trial_path=forbidden_dir.relative_to(root)))

    outside = tmp_path / "outside_oracle"
    outside.mkdir(parents=True, exist_ok=True)
    symlink_trial = root / "runs" / "symlink_oracle"
    symlink_trial.symlink_to(outside)
    with pytest.raises(ValueError, match="escapes allowed root"):
        build_feedback(repo_root=root, task_path=task, trial_path=agent, oracle_reference=dict(ref, trial_path=symlink_trial.relative_to(root)))


def test_oracle_reference_digest_and_task_mismatch_fails_closed(tmp_path: Path) -> None:
    """Mismatched result_sha256, task_package_digest, or parent lab metadata fail closed."""
    root, task, agent, ref = _make_env(tmp_path)

    with pytest.raises(ValueError, match="oracle_reference result_sha256 mismatch"):
        build_feedback(repo_root=root, task_path=task, trial_path=agent, oracle_reference=dict(ref, result_sha256="0" * 64))

    with pytest.raises(ValueError, match="oracle_reference task_package_digest mismatch"):
        build_feedback(repo_root=root, task_path=task, trial_path=agent, oracle_reference=dict(ref, task_package_digest="sha256:" + "1" * 64))

    root2, task2, agent2, ref2 = _make_env(tmp_path / "case2", meta_digest="sha256:" + "2" * 64)
    with pytest.raises(ValueError, match="oracle lab-metadata experiment.package_digest"):
        build_feedback(repo_root=root2, task_path=task2, trial_path=agent2, oracle_reference=ref2)

    root3, task3, agent3, ref3 = _make_env(tmp_path / "case3", omit_meta=True)
    with pytest.raises(FileNotFoundError, match="oracle lab-metadata.json not found"):
        build_feedback(repo_root=root3, task_path=task3, trial_path=agent3, oracle_reference=ref3)


@pytest.mark.parametrize(
    ("kwargs", "match_err"),
    [
        ({"error": "RuntimeCrash"}, "oracle trial encountered error"),
        ({"agent_name": "deepseek"}, "expected 'oracle'"),
        ({"model": "glm-5.3"}, "expected control agent with no model"),
        ({"reward": 0.0}, "expected 1.0"),
        ({"finished": False}, "missing finished_at"),
    ],
)
def test_oracle_reference_invalid_execution_state_fails_closed(
    tmp_path: Path, kwargs: dict[str, Any], match_err: str
) -> None:
    """Oracle trials with errors, models, non-oracle agents, or reward!=1 fail closed."""
    root, task, agent, ref = _make_env(tmp_path, **kwargs)
    with pytest.raises(ValueError, match=match_err):
        build_feedback(repo_root=root, task_path=task, trial_path=agent, oracle_reference=ref)


def test_oracle_reference_empty_log_reports_missing_honestly(tmp_path: Path) -> None:
    """Empty 0-byte oracle.txt reports trace absent and never fabricates trajectory steps."""
    root, task, agent, ref = _make_env(tmp_path, oracle_txt="")
    fb = build_feedback(repo_root=root, task_path=task, trial_path=agent, oracle_reference=ref)
    assert "Trace Status: absent (agent/oracle.txt is empty; registered oracle has no command trace)" in fb["feedback"]
    assert any("oracle_trace: absent" in notice for notice in fb["coverage_notices"])
    assert "Step 1 Action" not in fb["feedback"].split("## Oracle Reference Contrast")[1]
    assert fb["sources"]["oracle_log"] == str(ref["trial_path"] / "agent" / "oracle.txt")


def test_oracle_reference_contrastive_feedback_with_state_journal(tmp_path: Path) -> None:
    """Contrastive feedback reflects oracle file transitions, agent errors, and redundancies."""
    diff = {"changes": [{"path": "output/summary.json", "change_type": "added", "after": {"type": "file", "size_bytes": 158}}]}
    traj = {
        "steps": [
            {"step_id": 1, "source": "agent", "tool_calls": [{"tool_call_id": "c1", "function_name": "bash", "arguments": {"cmd": "cat input/events.jsonl"}}], "observation_results": [{"source_call_id": "c1", "content": "ok"}]},
            {"step_id": 2, "source": "agent", "tool_calls": [{"tool_call_id": "c2", "function_name": "bash", "arguments": {"cmd": "python -c 'import missing_mod'"}}], "observation_results": [{"source_call_id": "c2", "content": "ModuleNotFoundError: No module named 'missing_mod'\nexit code 1"}]},
            {"step_id": 3, "source": "agent", "tool_calls": [{"tool_call_id": "c3", "function_name": "bash", "arguments": {"cmd": "cat input/events.jsonl"}}], "observation_results": [{"source_call_id": "c3", "content": "ok"}]},
        ]
    }
    root, task, agent, ref = _make_env(tmp_path, state_diff=diff, agent_traj=traj)
    fb = build_feedback(repo_root=root, task_path=task, trial_path=agent, oracle_reference=ref)
    text = fb["feedback"]

    assert "## Oracle Reference Contrast" in text
    assert f"- Reference Package Digest: {ref['task_package_digest']}" in text
    assert f"- Reference Result Digest: {ref['result_sha256']}" in text
    assert "- Reference Outcome: completed (Reward: 1.0000, Errors: none)" in text
    assert "- Agent Outcome: completed (Reward: 0.0000, Errors: none)" in text
    assert "- [added] output/summary.json (file, 158 bytes)" in text
    assert "Missing Expected Outputs (1):" in text
    assert "output/summary.json (produced by Oracle reference, missing in agent output)" in text
    assert "Agent Observed Errors (1):" in text
    assert "ModuleNotFoundError: No module named 'missing_mod'" in text
    assert "Redundant / Repeated Agent Actions (1):" in text
    assert "Step 3 repeated action [bash] from Step 1: cat input/events.jsonl" in text
    for src in ("oracle_trial_result", "oracle_lab_metadata", "oracle_state_diff", "trial_trajectory"):
        assert src in fb["sources"]


def test_oracle_reference_budget_truncation_and_redaction(tmp_path: Path) -> None:
    """Feedback with oracle reference is strictly bounded and redacts secrets."""
    secret = "sk-ant-api03-SECRETTOKEN123456789"
    traj = {
        "steps": [
            {"step_id": 1, "source": "agent", "tool_calls": [{"tool_call_id": "c1", "function_name": "bash", "arguments": {"cmd": f"curl -H 'Authorization: Bearer {secret}' https://api.example.com"}}], "observation_results": [{"source_call_id": "c1", "content": "ok"}]}
        ]
    }
    root, task, agent, ref = _make_env(tmp_path, agent_traj=traj)
    fb = build_feedback(repo_root=root, task_path=task, trial_path=agent, max_chars=400, oracle_reference=ref)

    assert fb["truncated"] is True
    assert fb["char_count"] <= 400
    assert len(fb["feedback"]) <= 400
    assert fb["max_chars"] == 400
    assert "[Truncated: feedback exceeded max_chars budget of 400 characters]" in fb["feedback"]
    assert secret not in fb["feedback"]
