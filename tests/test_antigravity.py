from __future__ import annotations

import json
from pathlib import Path

from evallab.antigravity import (
    UNAVAILABLE_PRINT_MODE_REASON,
    create_fallback_atif_for_print_mode,
    parse_stream_json_to_atif,
    sanitize_stream_json,
)
from evallab.atif import project_trial
from evallab.facts import extract_trial_fact
from evallab.results import load_job

STREAM_FIXTURE = "\n".join(
    [
        json.dumps(
            {
                "event": "init",
                "conversation_id": "agy-session-1",
                "init": {"conversation_id": "agy-session-1", "model": "gemini-3.7-flash-high"},
            }
        ),
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "agy-session-1",
                    "step_index": 0,
                    "state": "DONE",
                    "step_type": "user_input",
                },
            }
        ),
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "agy-session-1",
                    "step_index": 3,
                    "state": "ACTIVE",
                    "step_type": "agent_response",
                    "text_delta": "I will write the file.",
                },
            }
        ),
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "agy-session-1",
                    "step_index": 3,
                    "state": "DONE",
                    "step_type": "agent_response",
                    "text_delta": "",
                    "usage": {"input_tokens": 10, "output_tokens": 4, "cache_read_tokens": 2},
                },
            }
        ),
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "agy-session-1",
                    "step_index": 4,
                    "state": "DONE",
                    "step_type": "tool",
                    "tool_name": "run_command",
                    "tool_info": {
                        "name": "run_command",
                        "parameters": {"CommandLine": "printf hello"},
                        "output": "hello\n",
                    },
                },
            }
        ),
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "agy-session-1",
                    "step_index": 5,
                    "state": "DONE",
                    "step_type": "tool",
                    "tool_info": {
                        "name": "run_command",
                        "parameters": {"CommandLine": "false"},
                        "error": {"type": "CommandError", "message": "exit status 1"},
                    },
                },
            }
        ),
        json.dumps(
            {
                "event": "result",
                "result": {
                    "conversation_id": "agy-session-1",
                    "status": "SUCCESS",
                    "response": "Done.",
                    "usage": {"input_tokens": 10, "output_tokens": 4, "cache_read_tokens": 2},
                },
            }
        ),
    ]
)


def test_stream_events_become_ordered_atif_steps() -> None:
    atif = parse_stream_json_to_atif(
        STREAM_FIXTURE,
        agent_version="1.1.15",
        model_name="google/gemini-3.7-flash-high",
        job_id="job-1",
        trial_id="trial-1",
        raw_source="agent/antigravity-cli.stream.jsonl",
    )

    assert atif is not None
    assert atif["session_id"] == "agy-session-1"
    assert atif["agent"]["model_name"] == "google/gemini-3.7-flash-high"
    assert [step["step_id"] for step in atif["steps"]] == [1, 2, 3, 4, 5]
    assert [step["source"] for step in atif["steps"]] == [
        "user",
        "agent",
        "agent",
        "agent",
        "agent",
    ]
    assert atif["steps"][1]["message"] == "I will write the file."
    assert atif["steps"][2]["tool_calls"][0]["function_name"] == "run_command"
    assert atif["steps"][2]["observation"]["results"][0]["content"] == "hello\n"
    assert atif["final_metrics"] == {
        "total_prompt_tokens": 10,
        "total_completion_tokens": 4,
        "total_cached_tokens": 2,
        "total_steps": 5,
    }
    assert atif["steps"][3]["observation"]["results"][0]["extra"]["error"] is True
    assert atif["extra"]["identity"] == {
        "job_id": "job-1",
        "trial_id": "trial-1",
        "agent": "antigravity-cli",
        "model": "google/gemini-3.7-flash-high",
    }


def test_stream_trajectory_ingests_nonzero_facts(tmp_path: Path) -> None:
    job_dir = tmp_path / "job-1"
    trial_dir = job_dir / "trial-1"
    agent_dir = trial_dir / "agent"
    agent_dir.mkdir(parents=True)
    atif = parse_stream_json_to_atif(
        STREAM_FIXTURE,
        agent_version="1.1.15",
        model_name="google/gemini-3.7-flash-high",
    )
    assert atif is not None
    (agent_dir / "trajectory.json").write_text(json.dumps(atif))
    (trial_dir / "result.json").write_text(
        json.dumps(
            {
                "id": "trial-id",
                "task_name": "agy-fixture",
                "trial_name": "trial-1",
                "agent_info": {
                    "name": "antigravity-cli",
                    "version": "1.1.15",
                    "model_info": {"name": "gemini-3.7-flash-high", "provider": "google"},
                },
                "verifier_result": {"rewards": {"reward": 1.0}},
                "exception_info": None,
            }
        )
    )
    (trial_dir / "lock.json").write_text("{}")
    (job_dir / "result.json").write_text(
        json.dumps(
            {
                "id": "job-id",
                "name": "job-1",
                "finished_at": "2026-08-20T00:00:10Z",
                "n_total_trials": 1,
                "stats": {"n_completed_trials": 1, "n_errored_trials": 0},
            }
        )
    )

    job = load_job(job_dir)
    projection = project_trial(job, job.trials[0])
    fact = extract_trial_fact(job, job.trials[0])
    assert len(projection.trajectories) == 1
    assert projection.trajectories[0].validation_status == "valid"
    assert len(projection.steps) == 5
    assert len(projection.tool_calls) == 2
    assert len(projection.observations) == 2
    assert fact.step_count == 5
    assert fact.tool_call_count == 2
    assert fact.trajectory_count == 1


def test_print_mode_is_explicitly_final_response_only() -> None:
    atif = create_fallback_atif_for_print_mode(
        "Final response",
        model_name="google/gemini-3.7-flash-high",
        user_prompt="prompt",
    )
    assert atif["notes"] == UNAVAILABLE_PRINT_MODE_REASON
    assert atif["extra"]["capture"] == "final-response-only"
    assert "raw_source" in atif["extra"]
    assert len(atif["steps"]) == 2
    assert not atif["steps"][1].get("tool_calls")


def test_stream_sanitization_drops_credentials() -> None:
    raw = json.dumps({"event": "result", "result": {"access_token": "secret-token"}})
    sanitized = sanitize_stream_json(raw + "\nnot-json\n")
    assert "secret-token" not in sanitized
    assert "<redacted>" in sanitized
