from __future__ import annotations

import hashlib
import json
from pathlib import Path

import duckdb

from evallab.atif import export_trajectories, project_trial
from evallab.results import load_job


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _trajectory(
    *,
    session_id: str,
    trajectory_id: str | None = None,
    message: str = "ordinary",
) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "ATIF-v1.7",
        "session_id": session_id,
        "agent": {"name": "stub-agent", "version": "1.0", "model_name": "stub-model"},
        "steps": [{"step_id": 1, "source": "user", "message": message}],
        "final_metrics": {"total_steps": 1},
    }
    if trajectory_id is not None:
        value["trajectory_id"] = trajectory_id
    return value


def _make_job(root: Path, *, with_trajectory: bool = True) -> Path:
    job = root / "sample-job"
    trial = job / "sample-task__abc123"
    _write_json(job / "config.json", {"job_name": job.name})
    _write_json(job / "lock.json", {"harbor": {"version": "0.21.0"}})
    _write_json(
        job / "result.json",
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "n_total_trials": 1,
            "stats": {"n_completed_trials": 1, "n_errored_trials": 0},
        },
    )
    _write_json(trial / "config.json", {"agent": {"name": "stub-agent"}})
    _write_json(
        trial / "lock.json",
        {
            "schema_version": 2,
            "task": {"digest": "sha256:task"},
            "agent": {"name": "stub-agent"},
            "environment": {"type": "docker"},
            "verifier": {"environment_mode": "separate"},
        },
    )
    _write_json(
        trial / "result.json",
        {
            "id": "00000000-0000-0000-0000-000000000002",
            "trial_name": trial.name,
            "task_name": "sample-task",
            "task_checksum": "task-checksum",
            "agent_info": {
                "name": "stub-agent",
                "version": "1.0",
                "model_info": {"name": "stub-model"},
            },
            "agent_result": {
                "n_input_tokens": 11,
                "n_cache_tokens": 3,
                "n_output_tokens": 5,
                "cost_usd": 0.01,
            },
            "verifier_result": {"rewards": {"reward": 0.0}},
            "exception_info": None,
        },
    )
    if not with_trajectory:
        return job

    root_trajectory = {
        "schema_version": "ATIF-v1.7",
        "session_id": "root-session",
        "trajectory_id": "root-document",
        "agent": {"name": "stub-agent", "version": "1.0", "model_name": "stub-model"},
        "steps": [
            {
                "step_id": 1,
                "source": "user",
                "message": "SECRET-PROMPT-MUST-NOT-BE-PROJECTED",
                "is_copied_context": True,
            },
            {
                "step_id": 2,
                "source": "agent",
                "message": "",
                "llm_call_count": 1,
                "metrics": {
                    "prompt_tokens": 11,
                    "completion_tokens": 5,
                    "cached_tokens": 3,
                    "cost_usd": 0.01,
                },
                "tool_calls": [
                    {
                        "tool_call_id": "call-1",
                        "function_name": "exec",
                        "arguments": {"command": "SECRET-ARGUMENT"},
                    }
                ],
                "observation": {
                    "results": [
                        {
                            "source_call_id": "call-1",
                            "content": "SECRET-OBSERVATION",
                            "extra": {"exit_code": 1},
                            "subagent_trajectory_ref": [
                                {
                                    "trajectory_id": "external-child",
                                    "trajectory_path": "subagent.json",
                                }
                            ],
                        }
                    ]
                },
            },
        ],
        "continued_trajectory_ref": "continuation.json",
        "subagent_trajectories": [
            _trajectory(
                session_id="root-session",
                trajectory_id="embedded-child",
                message="embedded secret",
            )
        ],
        "final_metrics": {
            "total_prompt_tokens": 11,
            "total_completion_tokens": 5,
            "total_cached_tokens": 3,
            "total_cost_usd": 0.01,
            "total_steps": 2,
        },
    }
    _write_json(trial / "agent/trajectory.json", root_trajectory)
    _write_json(
        trial / "agent/subagent.json",
        _trajectory(session_id="child-session", trajectory_id="external-child"),
    )
    _write_json(
        trial / "agent/continuation.json",
        _trajectory(session_id="root-session", trajectory_id="continuation"),
    )
    return job


def _tree_digests(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_projection_discovers_continuations_subagents_and_structural_facts(
    tmp_path: Path,
) -> None:
    job = load_job(_make_job(tmp_path))

    projection = project_trial(job, job.trials[0])

    assert len(projection.trajectories) == 4
    assert {item.validation_status for item in projection.trajectories} == {"valid"}
    assert len(projection.steps) == 5
    assert projection.steps[0].is_copied_context is True
    assert projection.steps[1].prompt_tokens == 11
    assert projection.tool_calls[0].function_name == "exec"
    assert projection.observations[0].command_exit_code == 1
    assert projection.observations[0].subagent_ref_count == 1


def test_export_is_rebuildable_queryable_and_does_not_copy_sensitive_content(
    tmp_path: Path,
) -> None:
    job_dir = _make_job(tmp_path / "raw")
    before = _tree_digests(job_dir)
    job = load_job(job_dir)
    output = tmp_path / "derived"

    first = export_trajectories([job], output)
    first_hashes = {item.path.relative_to(output).as_posix(): item.sha256 for item in first.tables}
    second = export_trajectories([job], output)
    second_hashes = {
        item.path.relative_to(output).as_posix(): item.sha256 for item in second.tables
    }

    assert first.row_counts == {"trajectories": 4, "steps": 5, "tool_calls": 1, "observations": 1}
    assert first_hashes == second_hashes
    assert _tree_digests(job_dir) == before
    steps_glob = (output / "**/steps.parquet").as_posix()
    rows = duckdb.sql(
        f"SELECT sum(prompt_tokens), sum(tool_call_count) FROM read_parquet('{steps_glob}')"
    ).fetchone()
    assert rows == (11, 1)
    for exported in first.tables:
        columns = duckdb.sql(
            f"DESCRIBE SELECT * FROM read_parquet('{exported.path.as_posix()}')"
        ).fetchall()
        names = {row[0] for row in columns}
        assert "message" not in names
        assert "arguments" not in names
        assert "content" not in names


def test_invalid_broken_reference_and_no_trajectory_are_nonfatal(tmp_path: Path) -> None:
    broken_job = load_job(_make_job(tmp_path / "broken"))
    trajectory_path = broken_job.trials[0].path / "agent/trajectory.json"
    payload = json.loads(trajectory_path.read_text())
    payload["continued_trajectory_ref"] = "missing.json"
    _write_json(trajectory_path, payload)

    broken_projection = project_trial(broken_job, broken_job.trials[0])
    root = next(item for item in broken_projection.trajectories if item.embedded_path is None)
    assert root.validation_status == "invalid"
    assert "missing" in str(root.validation_error)

    empty_job = load_job(_make_job(tmp_path / "empty", with_trajectory=False))
    empty_projection = project_trial(empty_job, empty_job.trials[0])
    assert empty_projection.trajectories == ()
    exported = export_trajectories([empty_job], tmp_path / "empty-derived")
    assert exported.row_counts == {
        "trajectories": 0,
        "steps": 0,
        "tool_calls": 0,
        "observations": 0,
    }


def test_unsupported_schema_is_recorded(tmp_path: Path) -> None:
    job = load_job(_make_job(tmp_path))
    trajectory_path = job.trials[0].path / "agent/trajectory.json"
    payload = json.loads(trajectory_path.read_text())
    payload["schema_version"] = "ATIF-v99"
    _write_json(trajectory_path, payload)

    projection = project_trial(job, job.trials[0])

    root = next(item for item in projection.trajectories if item.embedded_path is None)
    assert root.validation_status == "unsupported"
