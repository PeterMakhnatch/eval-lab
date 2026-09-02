from __future__ import annotations

import json
import shutil
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from evallab.evidence.atif import project_trial
from evallab.evidence.facts import rebuild_from_raw
from evallab.evidence.llm_request import LlmRequestProjectionError, project_llm_requests
from evallab.results import load_job


def _job(
    tmp_path: Path,
    *,
    preserve_atif: bool = False,
    mark_atif_llm_calls: bool = False,
):
    source = Path(__file__).parent / "fixtures/explorer/jobs/job-pass"
    job_path = tmp_path / "runs/job-pass"
    shutil.copytree(source, job_path)
    trajectory_path = job_path / "t1/agent/trajectory.json"
    trajectory = json.loads(trajectory_path.read_text())
    if not preserve_atif:
        trajectory["steps"] = []
    elif mark_atif_llm_calls:
        for step in trajectory["steps"]:
            step["llm_call_count"] = 1
    trajectory_path.write_text(json.dumps(trajectory))
    return load_job(job_path)


def _assistant(call_id: str, name: str, arguments: dict[str, object]):
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
        "content": None,
    }


def _write_request(
    trial: Path,
    index: int,
    messages: list[dict[str, object]],
    *,
    offered: tuple[str, ...] = ("shell", "todo_write"),
    usage: tuple[int, int, int] = (0, 0, 0),
) -> Path:
    path = trial / f"agent/goose/logs/llm_request.{index}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    request = {
        "model_config": {"model_name": "glm-4.7"},
        "input": {
            "model": "glm-4.7",
            "messages": messages,
            "tools": [
                {"type": "function", "function": {"name": name, "parameters": {}}}
                for name in offered
            ],
        },
    }
    event = {
        "data": {
            "created": 1_756_000_000 + index,
            "usage": {
                "prompt_tokens": usage[0],
                "completion_tokens": usage[1],
                "cached_tokens": usage[2],
            },
        }
    }
    path.write_text(json.dumps(request) + "\n" + json.dumps(event) + "\n")
    return path


def test_latest_reverse_numbered_history_avoids_duplicate_turns(tmp_path: Path) -> None:
    job = _job(tmp_path)
    trial = job.trials[0]
    first = _assistant("call-1", "shell", {"command": "pwd"})
    second = _assistant("call-2", "todo_write", {"content": "done"})
    _write_request(
        trial.path,
        1,
        [{"role": "system", "content": "s"}, first],
        offered=("older_only",),
    )
    _write_request(
        trial.path,
        0,
        [
            {"role": "system", "content": "s"},
            first,
            {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
            second,
            {"role": "tool", "tool_call_id": "call-2", "content": "ok"},
        ],
        offered=("shell", "todo_write"),
    )

    projection = project_llm_requests(job, trial)
    assert projection is not None
    assert [row.function_name for row in projection.tool_calls] == [
        "shell",
        "todo_write",
    ]
    assert [row.call_index for row in projection.tool_calls] == [0, 1]
    fact = projection.trajectories[0]
    assert fact.retained_request_paths is not None
    assert fact.retained_request_paths[0].endswith("llm_request.0.jsonl")
    assert fact.tools_offered == ("shell", "todo_write")
    assert fact.inferred_total_call_lower_bound == 3
    assert fact.unknown_prefix is True


def test_zero_usage_is_unavailable_and_missing_credentials_are_fixed_class(
    tmp_path: Path,
) -> None:
    job = _job(tmp_path)
    trial = job.trials[0]
    call = _assistant("call-1", "tau3-runtime__start_conversation", {})
    _write_request(
        trial.path,
        0,
        [
            {"role": "system", "content": "secret prompt must not persist"},
            call,
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": (
                    "Error calling tool: litellm.InternalServerError "
                    "Missing credentials. Please pass an api_key"
                ),
            },
        ],
    )

    projection = project_llm_requests(job, trial)
    assert projection is not None
    fact = projection.trajectories[0]
    assert fact.prompt_tokens is None
    assert fact.harness_fault_signature == "missing_credentials"
    assert {"prompt_tokens", "completion_tokens", "cached_tokens"} <= set(
        fact.unavailable_call_metadata or ()
    )
    assert projection.tool_calls[0].result_error_flag is True
    assert projection.observations[0].error_classification == "missing_credentials"
    assert "Missing credentials" not in repr(projection)


def test_ten_file_ring_marks_unknown_metadata_prefix(tmp_path: Path) -> None:
    job = _job(tmp_path)
    trial = job.trials[0]
    messages: list[dict[str, object]] = [{"role": "system", "content": "s"}]
    for index in range(12):
        messages.append(_assistant(f"call-{index}", "shell", {"n": index}))
        messages.append({"role": "tool", "tool_call_id": f"call-{index}", "content": "ok"})
    for index in range(10):
        retained_messages = messages[: len(messages) - (index * 2)]
        _write_request(trial.path, index, retained_messages)

    projection = project_llm_requests(job, trial)
    assert projection is not None
    fact = projection.trajectories[0]
    assert fact.retained_request_count == 10
    assert fact.inferred_total_call_lower_bound == 13
    assert fact.ring_buffer_truncated is True
    assert sum(not row.llm_metadata_available for row in projection.steps) == 3


def test_request_ring_supplements_only_incomplete_atif(tmp_path: Path) -> None:
    partial_job = _job(tmp_path / "partial", preserve_atif=True)
    partial_trial = partial_job.trials[0]
    _write_request(
        partial_trial.path,
        0,
        [
            {"role": "system", "content": "s"},
            _assistant("c1", "write_file", {"path": "a.py"}),
        ],
    )

    partial = project_trial(partial_job, partial_trial)

    assert [row.capture_source for row in partial.trajectories] == [
        None,
        "llm_request_ring",
    ]

    complete_job = _job(
        tmp_path / "complete",
        preserve_atif=True,
        mark_atif_llm_calls=True,
    )
    complete_trial = complete_job.trials[0]
    _write_request(
        complete_trial.path,
        0,
        [
            {"role": "system", "content": "s"},
            _assistant("c1", "write_file", {"path": "a.py"}),
        ],
    )

    complete = project_trial(complete_job, complete_trial)

    assert [row.capture_source for row in complete.trajectories] == [None]


def test_redacts_raw_payloads_and_refuses_secret_structural_fields(
    tmp_path: Path,
) -> None:
    job = _job(tmp_path)
    trial = job.trials[0]
    secret = "s" + "k-" + "proj-abcdefghijklmnopqrstuvwxyz123456"
    call = _assistant("call-1", "shell", {"token": secret})
    _write_request(
        trial.path,
        0,
        [
            {"role": "system", "content": secret},
            call,
            {"role": "tool", "tool_call_id": "call-1", "content": secret},
        ],
    )
    projection = project_llm_requests(job, trial)
    assert projection is not None
    assert secret not in repr(projection)

    shutil.rmtree(trial.path / "agent/goose")
    _write_request(
        trial.path,
        0,
        [{"role": "system", "content": "s"}],
        offered=(secret,),
    )
    with pytest.raises(LlmRequestProjectionError, match="unsafe tool name"):
        project_llm_requests(job, trial)


def test_malformed_jsonl_fails_closed(tmp_path: Path) -> None:
    job = _job(tmp_path)
    trial = job.trials[0]
    path = trial.path / "agent/goose/logs/llm_request.0.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"input":\\n')

    with pytest.raises(LlmRequestProjectionError, match="malformed llm_request"):
        project_llm_requests(job, trial)


def test_bfcl_tools_are_mechanical_evidence_without_fidelity_judgment(
    tmp_path: Path,
) -> None:
    job = _job(tmp_path)
    trial = job.trials[0]
    offered = (
        "computer",
        "memory",
        "shell",
        "text_editor",
        "todo",
        "web",
        "write_file",
    )
    _write_request(
        trial.path,
        0,
        [
            {
                "role": "system",
                "content": (
                    "Call the functions described in this instruction and write only JSON "
                    "to /app/result.json"
                ),
            }
        ],
        offered=offered,
    )

    projection = project_llm_requests(job, trial)
    assert projection is not None
    fact = projection.trajectories[0]
    assert fact.tools_offered == tuple(sorted(offered))
    assert fact.harness_fault_signature is None
    assert not hasattr(fact, "bfcl_fidelity")


def test_rebuild_populates_existing_mechanical_tables(tmp_path: Path) -> None:
    job = _job(tmp_path)
    trial = job.trials[0]
    call = _assistant("call-1", "shell", {"command": "pwd"})
    _write_request(
        trial.path,
        0,
        [
            {"role": "system", "content": "s"},
            call,
            {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
        ],
        usage=(12, 3, 2),
    )
    derived = tmp_path / "derived"

    rebuild_from_raw([job], derived)

    partition = derived / f"job_id={job.id}" / f"trial_id={trial.id}"
    llm_calls = pq.read_table(partition / "llm_calls.parquet").to_pylist()
    tool_calls = pq.read_table(partition / "tool_calls.parquet").to_pylist()
    tool_usage = pq.read_table(partition / "tool_usage.parquet").to_pylist()
    assert len(llm_calls) == 2
    assert len(tool_calls) == 1
    assert tool_usage == [
        {
            "experiment_id": None,
            "job_id": job.id,
            "trial_id": trial.id,
            "function_name": "shell",
            "call_count": 1,
        }
    ]
    assert llm_calls[-1]["metadata_available"] is True
    assert llm_calls[-1]["usage_status"] == "reported"
