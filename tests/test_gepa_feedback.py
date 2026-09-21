"""Focused behavioral tests for GEPA feedback builder."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from evallab.gepa_optimizer.evaluator import ExampleDeclarationError, validate_example_dict
from evallab.gepa_optimizer.feedback import (
    build_feedback,
    build_prior_run_feedback,
    validate_oracle_reference,
    validate_prior_run_reference,
)
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
        {
            "task_name": task_name,
            "status": "completed",
            "verifier_result": {"rewards": {"reward": 0.0}},
        },
    )
    if agent_traj:
        write_json(agent_dir / "agent" / "trajectory.json", agent_traj)

    oracle_dir = root / "runs" / "oracle_job" / "oracle_trial"
    oracle_dir.mkdir(parents=True, exist_ok=True)
    res_data: dict[str, Any] = {
        "task_name": task_name,
        "status": status,
        "agent_info": {"name": agent_name},
        "verifier_result": {"rewards": {"reward": reward}},
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
        build_feedback(
            repo_root=root, task_path=task, trial_path=agent, oracle_reference="not-a-dict"
        )  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="oracle_reference must contain exact keys"):
        build_feedback(
            repo_root=root,
            task_path=task,
            trial_path=agent,
            oracle_reference={"trial_path": "runs/oracle"},
        )

    with pytest.raises(ValueError, match="oracle_reference must contain exact keys"):
        build_feedback(
            repo_root=root,
            task_path=task,
            trial_path=agent,
            oracle_reference=dict(ref, extra_key="bad"),
        )

    with pytest.raises(ValueError, match="escapes allowed root"):
        build_feedback(
            repo_root=root,
            task_path=task,
            trial_path=agent,
            oracle_reference=dict(ref, trial_path="../outside"),
        )

    forbidden_dir = root / "tasks" / "task_001" / "tests"
    forbidden_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError, match="forbidden hidden"):
        build_feedback(
            repo_root=root,
            task_path=task,
            trial_path=agent,
            oracle_reference=dict(ref, trial_path=forbidden_dir.relative_to(root)),
        )

    outside = tmp_path / "outside_oracle"
    outside.mkdir(parents=True, exist_ok=True)
    symlink_trial = root / "runs" / "symlink_oracle"
    symlink_trial.symlink_to(outside)
    with pytest.raises(ValueError):
        build_feedback(
            repo_root=root,
            task_path=task,
            trial_path=agent,
            oracle_reference=dict(ref, trial_path=symlink_trial.relative_to(root)),
        )


def test_oracle_reference_digest_and_task_mismatch_fails_closed(tmp_path: Path) -> None:
    """Mismatched result_sha256, task_package_digest, or parent lab metadata fail closed."""
    root, task, agent, ref = _make_env(tmp_path)

    with pytest.raises(ValueError, match="oracle_reference result_sha256 mismatch"):
        build_feedback(
            repo_root=root,
            task_path=task,
            trial_path=agent,
            oracle_reference=dict(ref, result_sha256="0" * 64),
        )

    with pytest.raises(ValueError, match="oracle_reference task_package_digest mismatch"):
        build_feedback(
            repo_root=root,
            task_path=task,
            trial_path=agent,
            oracle_reference=dict(ref, task_package_digest="sha256:" + "1" * 64),
        )

    root2, task2, agent2, ref2 = _make_env(tmp_path / "case2", meta_digest="sha256:" + "2" * 64)
    with pytest.raises(ValueError, match="oracle lab-metadata experiment.package_digest"):
        build_feedback(repo_root=root2, task_path=task2, trial_path=agent2, oracle_reference=ref2)

    root3, task3, agent3, ref3 = _make_env(tmp_path / "case3", omit_meta=True)
    with pytest.raises(FileNotFoundError, match="oracle lab-metadata.json not found"):
        build_feedback(repo_root=root3, task_path=task3, trial_path=agent3, oracle_reference=ref3)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"error": "RuntimeCrash"},
        {"agent_name": "deepseek"},
        {"model": "glm-5.3"},
        {"reward": 0.0},
        {"reward": True},
        {"agent_name": "oracle-impostor"},
        {"finished": False},
    ],
)
def test_oracle_reference_invalid_execution_state_fails_closed(
    tmp_path: Path, kwargs: dict[str, Any]
) -> None:
    """Oracle trials with errors, models, non-oracle agents, or reward!=1 fail closed."""
    root, task, agent, ref = _make_env(tmp_path, **kwargs)
    with pytest.raises(ValueError):
        build_feedback(repo_root=root, task_path=task, trial_path=agent, oracle_reference=ref)


def test_oracle_reference_contrastive_feedback_with_state_journal(tmp_path: Path) -> None:
    """Contrastive feedback reflects oracle file transitions, agent errors, and redundancies."""
    diff = {
        "status": "available",
        "changes": [
            {
                "path": "output/summary.json",
                "change_type": "added",
                "after": {"type": "file", "size_bytes": 158},
            }
        ],
    }
    traj = {
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "tool_calls": [
                    {
                        "tool_call_id": "c1",
                        "function_name": "bash",
                        "arguments": {"cmd": "cat input/events.jsonl"},
                    }
                ],
                "observation_results": [{"source_call_id": "c1", "content": "ok"}],
            },
            {
                "step_id": 2,
                "source": "agent",
                "tool_calls": [
                    {
                        "tool_call_id": "c2",
                        "function_name": "bash",
                        "arguments": {"cmd": "python -c 'import missing_mod'"},
                    }
                ],
                "observation_results": [
                    {
                        "source_call_id": "c2",
                        "content": "ModuleNotFoundError: No module named 'missing_mod'\nexit code 1",
                    }
                ],
            },
            {
                "step_id": 3,
                "source": "agent",
                "tool_calls": [
                    {
                        "tool_call_id": "c3",
                        "function_name": "bash",
                        "arguments": {"cmd": "cat input/events.jsonl"},
                    }
                ],
                "observation_results": [{"source_call_id": "c3", "content": "ok"}],
            },
        ]
    }
    root, task, agent, ref = _make_env(tmp_path, state_diff=diff, agent_traj=traj)
    fb = build_feedback(repo_root=root, task_path=task, trial_path=agent, oracle_reference=ref)
    text = fb["feedback"]

    assert ref["task_package_digest"] in text
    assert ref["result_sha256"] in text
    assert "output/summary.json" in text
    assert "missing_mod" in text
    assert "cat input/events.jsonl" in text
    for src in (
        "oracle_trial_result",
        "oracle_lab_metadata",
        "oracle_state_diff",
        "trial_trajectory",
    ):
        assert src in fb["sources"]


def test_oracle_reference_budget_truncation_and_redaction(tmp_path: Path) -> None:
    """Feedback with oracle reference is strictly bounded and redacts secrets."""
    secret = "rk-proj-REDACTIONTEST0123456789abcd"
    traj = {
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "tool_calls": [
                    {
                        "tool_call_id": "c1",
                        "function_name": "bash",
                        "arguments": {
                            "cmd": f"curl -H 'Authorization: Bearer {secret}' https://api.example.com"
                        },
                    }
                ],
                "observation_results": [{"source_call_id": "c1", "content": "ok"}],
            }
        ]
    }
    root, task, agent, ref = _make_env(tmp_path, agent_traj=traj)
    fb = build_feedback(
        repo_root=root, task_path=task, trial_path=agent, max_chars=400, oracle_reference=ref
    )

    assert fb["truncated"] is True
    assert fb["char_count"] <= 400
    assert len(fb["feedback"]) <= 400
    assert fb["max_chars"] == 400
    assert "[Truncated: feedback exceeded max_chars budget of 400 characters]" in fb["feedback"]
    assert secret not in fb["feedback"]


# ---------------------------------------------------------------------------
# 7. Prior-run historical trajectory intake
# ---------------------------------------------------------------------------

_CANARY_TRIAL = Path(
    "research/evidence/runs/canary-event-summary-codex-20260815/event-summary__5E3btLv"
)
_CANARY_JOB = Path("research/evidence/runs/canary-event-summary-codex-20260815")
_CANARY_TASK = Path("library/tasks/event-summary")
_CANARY_TRAJECTORY = (
    "research/evidence/runs/canary-event-summary-codex-20260815/"
    "event-summary__5E3btLv/agent/trajectory.json"
)


def _write_history_trial(
    root: Path,
    *,
    job_name: str = "history_job",
    trial_name: str = "history_trial",
    task_rel: str = "tasks/task_001",
    recorded_task_path: str | None = None,
    extra_trials: int = 0,
    job_shaped: bool = True,
    meta_task_path: str | None = None,
) -> tuple[Path, Path, Path]:
    task_dir = root / task_rel
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "instruction.md").write_text("Do the task.", encoding="utf-8")
    job_dir = root / "runs" / job_name
    job_dir.mkdir(parents=True, exist_ok=True)
    names = [trial_name, *[f"{trial_name}_{i}" for i in range(extra_trials)]]
    first: Path | None = None
    abs_task = recorded_task_path if recorded_task_path is not None else str(task_dir.resolve())
    for name in names:
        trial = job_dir / name
        trial.mkdir(parents=True, exist_ok=True)
        write_json(
            trial / "result.json",
            {
                "task_name": "task_001",
                "trial_name": name,
                "status": "completed",
                "finished_at": "2026-09-16T00:00:00Z",
                "config": {"task": {"path": abs_task}},
                "verifier_result": {"rewards": {"reward": 0.5}},
                "agent_info": {"name": "codex"},
            },
        )
        write_json(trial / "config.json", {"task": {"path": abs_task}})
        write_json(
            trial / "agent" / "trajectory.json",
            {
                "steps": [
                    {
                        "step_id": 1,
                        "source": "agent",
                        "tool_calls": [
                            {
                                "tool_call_id": "c1",
                                "function_name": "exec",
                                "arguments": {"cmd": "echo prior-history-marker"},
                            }
                        ],
                        "observation_results": [
                            {"source_call_id": "c1", "content": "prior-history-marker"}
                        ],
                    }
                ]
            },
        )
        if first is None:
            first = trial
    assert first is not None
    if job_shaped:
        write_json(
            job_dir / "result.json",
            {
                "n_total_trials": len(names),
                "stats": {"n_completed_trials": len(names)},
                "finished_at": "2026-09-16T00:00:00Z",
            },
        )
    if meta_task_path is not None:
        write_json(
            job_dir / "lab-metadata.json",
            {"experiment": {"task_path": meta_task_path}},
        )
    return task_dir.relative_to(root), first.relative_to(root), job_dir.relative_to(root)


def test_prior_runs_section_uses_real_fixture_trial_content() -> None:
    """Real committed fixture trace content reaches the Prior Runs section."""
    repo_root = Path(__file__).resolve().parent.parent
    if not (repo_root / _CANARY_TRIAL).exists():
        pytest.skip("canary-event-summary fixture not present")

    result = build_feedback(
        repo_root=repo_root,
        task_path=_CANARY_TASK,
        trial_path=_CANARY_TRIAL,
        max_chars=32000,
        prior_trial_paths=(_CANARY_TRIAL,),
    )
    feedback_text = result["feedback"]
    prior_idx = feedback_text.find("## Prior Runs")
    task_idx = feedback_text.find("## Task Instruction")
    assert 0 <= prior_idx < task_idx
    prior_section = feedback_text[prior_idx:task_idx]
    assert "### event-summary__5E3btLv" in prior_section
    assert "wc -l /app/input/events.jsonl" in prior_section
    assert "Trace Status:" in prior_section
    assert "- Status:" in prior_section
    assert "- Primary Reward:" in prior_section
    assert result["sources"]["trial_trajectory"] == _CANARY_TRAJECTORY
    assert result["sources"]["prior_run/event-summary__5E3btLv/trial_trajectory"] == _CANARY_TRAJECTORY
    assert (
        result["sources"]["prior_run/event-summary__5E3btLv/trial_result"]
        == "research/evidence/runs/canary-event-summary-codex-20260815/event-summary__5E3btLv/result.json"
    )


def test_absent_prior_run_keeps_existing_feedback_byte_identical() -> None:
    """Omitting prior_trial_paths leaves existing fixture feedback byte-identical."""
    repo_root = Path(__file__).resolve().parent.parent
    if not (repo_root / _CANARY_TRIAL).exists():
        pytest.skip("canary-event-summary fixture not present")

    baseline = build_feedback(
        repo_root=repo_root,
        task_path=_CANARY_TASK,
        trial_path=_CANARY_TRIAL,
        max_chars=32000,
    )
    explicit_empty = build_feedback(
        repo_root=repo_root,
        task_path=_CANARY_TASK,
        trial_path=_CANARY_TRIAL,
        max_chars=32000,
        prior_trial_paths=(),
    )
    assert baseline == explicit_empty
    assert "## Prior Runs" not in baseline["feedback"]
    assert "wc -l /app/input/events.jsonl" in baseline["feedback"]
    assert baseline["sources"]["trial_trajectory"] == _CANARY_TRAJECTORY


def test_build_prior_run_feedback_multi_trial_job_raises() -> None:
    """A job directory with multiple trials is an ambiguity, not a silent pick."""
    repo_root = Path(__file__).resolve().parent.parent
    if not (repo_root / _CANARY_JOB).exists():
        pytest.skip("canary-event-summary fixture not present")

    with pytest.raises(ValueError, match="multiple trials.*event-summary__5E3btLv"):
        build_prior_run_feedback(repo_root, _CANARY_JOB)


def test_build_prior_run_feedback_resolves_single_trial_job(tmp_path: Path) -> None:
    """A job dir with exactly one trial is resolved and delegated to build_feedback."""
    task, trial, job = _write_history_trial(tmp_path)
    result = build_prior_run_feedback(tmp_path, job)
    assert "prior-history-marker" in result["feedback"]
    assert result["sources"]["trial_trajectory"] == (trial / "agent" / "trajectory.json").as_posix()
    # Passing the trial dir directly is equivalent for a single trial.
    via_trial = build_prior_run_feedback(tmp_path, trial)
    assert via_trial["feedback"] == result["feedback"]


def test_build_prior_run_feedback_uses_lab_metadata_when_recorded_path_outside(
    tmp_path: Path,
) -> None:
    """When the trial's recorded task path is outside the repo, use lab-metadata."""
    task, trial, job = _write_history_trial(
        tmp_path,
        recorded_task_path="/definitely/outside/this/repo/task",
        meta_task_path="tasks/task_001",
    )
    result = build_prior_run_feedback(tmp_path, job)
    assert result["sources"]["task_instruction"] == "tasks/task_001/instruction.md"
    assert "prior-history-marker" in result["feedback"]
    assert trial.name in str(result["sources"]["trial_result"])


def test_prior_run_result_sha256_mismatch_raises(tmp_path: Path) -> None:
    """Drift of the bound prior-run result hash is a hard failure."""
    root, task, agent, ref = _make_env(tmp_path)
    prior_ref = {
        "trial_path": Path(ref["trial_path"]).as_posix(),
        "result_sha256": ref["result_sha256"],
        "task_package_digest": ref["task_package_digest"],
    }
    assert validate_prior_run_reference(root, task, prior_ref) == (
        root / prior_ref["trial_path"]
    ).resolve()

    with pytest.raises(ValueError, match="prior_run_reference result_sha256 mismatch"):
        validate_prior_run_reference(
            root, task, dict(prior_ref, result_sha256="0" * 64)
        )

    example = {
        "task_id": "task_001",
        "task_path": Path(task).as_posix(),
        "task_package_digest": prior_ref["task_package_digest"],
        "split": "development",
        "prior_run_reference": dict(prior_ref, result_sha256="0" * 64),
    }
    with pytest.raises(ValueError, match="prior_run_reference result_sha256 mismatch"):
        validate_example_dict(root, example)


def test_prior_run_jail_traversal_rejected(tmp_path: Path) -> None:
    """Prior-run paths cannot escape the repo or enter hidden tests/solution dirs."""
    task, trial, job = _write_history_trial(tmp_path)

    with pytest.raises(ValueError, match="escapes allowed root"):
        build_feedback(
            repo_root=tmp_path,
            task_path=task,
            trial_path=trial,
            prior_trial_paths=(Path("../outside"),),
        )

    with pytest.raises(ValueError, match="escapes allowed root"):
        build_prior_run_feedback(tmp_path, Path("../outside"))

    forbidden = tmp_path / "tasks" / "task_001" / "tests" / "hidden_trial"
    forbidden.mkdir(parents=True, exist_ok=True)
    write_json(
        forbidden / "result.json",
        {
            "task_name": "task_001",
            "trial_name": "hidden_trial",
            "status": "completed",
            "finished_at": "2026-09-16T00:00:00Z",
            "verifier_result": {"rewards": {"reward": 1.0}},
        },
    )
    with pytest.raises(ValueError, match="forbidden hidden"):
        build_feedback(
            repo_root=tmp_path,
            task_path=task,
            trial_path=trial,
            prior_trial_paths=(forbidden.relative_to(tmp_path),),
        )

    root, task2, agent, ref = _make_env(tmp_path / "oracle_root")
    with pytest.raises(ValueError, match="escapes allowed root"):
        validate_prior_run_reference(
            root, task2, dict(ref, trial_path="../outside")
        )

    with pytest.raises(ValueError, match="forbidden hidden"):
        validate_prior_run_reference(
            tmp_path,
            task,
            {
                "trial_path": forbidden.relative_to(tmp_path).as_posix(),
                "result_sha256": "0" * 64,
                "task_package_digest": task_directory_digest(tmp_path / task),
            },
        )

    nested = tmp_path / "nested"
    _write_history_trial(nested, recorded_task_path="tasks/task_001/tests")
    with pytest.raises(ValueError, match="forbidden hidden verification"):
        build_prior_run_feedback(nested, Path("runs/history_job"))


def test_validate_example_dict_prior_run_reference_exact_keys(tmp_path: Path) -> None:
    """prior_run_reference is optional, exact-keyed, deep-copied, and package-bound."""
    task, trial, _job = _write_history_trial(tmp_path)
    digest = task_directory_digest(tmp_path / task)
    sha = hashlib.sha256((tmp_path / trial / "result.json").read_bytes()).hexdigest()
    reference = {
        "trial_path": Path(trial).as_posix(),
        "result_sha256": sha,
        "task_package_digest": digest,
    }
    example = {
        "task_id": "task_001",
        "task_path": Path(task).as_posix(),
        "task_package_digest": digest,
        "split": "development",
        "prior_run_reference": reference,
    }
    validated = validate_example_dict(tmp_path, example)
    assert validated["prior_run_reference"] == reference
    reference["trial_path"] = "mutated"
    assert validated["prior_run_reference"]["trial_path"] == Path(trial).as_posix()

    with pytest.raises(ExampleDeclarationError, match="Prior run reference must bind"):
        validate_example_dict(
            tmp_path,
            dict(example, prior_run_reference=dict(validated["prior_run_reference"], extra="bad")),
        )

    with pytest.raises(ExampleDeclarationError, match="Prior run reference must bind"):
        validate_example_dict(
            tmp_path,
            dict(
                example,
                prior_run_reference=dict(
                    validated["prior_run_reference"],
                    task_package_digest="sha256:" + "ab" * 32,
                ),
            ),
        )
