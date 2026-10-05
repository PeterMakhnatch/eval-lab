from __future__ import annotations

import ast
import json

import pytest

from evallab.mimoagent_trajectory import native_to_atif


@pytest.mark.parametrize(
    ("child_usage", "expected_prompt", "expected_completion", "expected_cached"),
    [
        (None, None, None, None),
        ({"prompt_tokens": 20, "completion_tokens": 3}, 30, 5, None),
        ({"completion_tokens": 3, "prompt_tokens_details": {"cached_tokens": 5}}, None, 5, 7),
        ({"prompt_tokens": 20, "completion_tokens": 3, "prompt_tokens_details": {"cached_tokens": 5}}, 30, 5, 7),
    ],
)
def test_parent_totals_include_children_only_when_every_call_has_the_metric(
    child_usage, expected_prompt, expected_completion, expected_cached
):
    native = {
        "info": {"exit_status": "Idle"},
        "trajs": {
            "main": {"messages": [
                {"role": "user", "content": "Delegate the inspection."},
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "delegate", "function": {"name": "agent", "arguments": "{}"},
                }]},
                {"role": "tool", "tool_call_id": "delegate", "name": "agent", "content":
                 "Inspection completed.\n\nTool metadata: {'log_file': '/logs/child.log'}"},
            ]},
            "child": {"messages": [
                {"role": "user", "content": "Inspect the implementation."},
                {"role": "assistant", "content": "Inspection completed."},
            ]},
        },
    }
    calls = [
        {"name": "main", "assistant_index": 0, "usage": {
            "prompt_tokens": 10, "completion_tokens": 2,
            "prompt_tokens_details": {"cached_tokens": 2},
        }},
        {"name": "child", "assistant_index": 0, "usage": child_usage},
    ]
    atif = native_to_atif(native, calls, trajectory_id="trial", model_name="model")
    totals = atif["final_metrics"]
    assert totals.get("total_prompt_tokens") == expected_prompt
    assert totals.get("total_completion_tokens") == expected_completion
    assert totals.get("total_cached_tokens") == expected_cached
    assert totals["extra"]["model_requests"] == 2
    assert totals["extra"]["requests_without_usage"] == int(child_usage is None)
    # A child's unknown metric does not discard an independently known parent step.
    assert atif["steps"][1]["metrics"]["prompt_tokens"] == 10


@pytest.mark.parametrize("exit_status", ["ModelQueryError", "InfraError"])
def test_infrastructure_stop_is_metadata_and_retains_unanswered_requests(exit_status):
    infra_error = {
        "error_type": exit_status,
        "root_error_type": "APIConnectionError",
        "last_response_status": 503,
        "retry_window_seconds": 30,
        "retry_rounds": 2,
        "retry_window_exhausted": True,
        "budget_refusal": False,
    }
    native = {
        "info": {
            "exit_status": exit_status,
            "stop_reason": "infra_error",
            "infra_error": infra_error,
        },
        "trajs": {"main": {"messages": [
            {"role": "system", "content": "Solve the task."},
            {"role": "user", "content": "Fix /testbed/retry.py."},
        ]}},
    }
    calls = [
        {
            "name": "main",
            "assistant_index": 0,
            "response_status": status,
            "usage": None,
            "finish_reason": None,
            "proxy_budget_reason": None,
        }
        for status in (503, None, 503)
    ]
    calls[1]["transport_error_type"] = "ConnectError"
    atif = native_to_atif(native, calls, trajectory_id="trial", model_name="model")
    assert atif["steps"] == [
        {"step_id": 1, "source": "system", "message": "Solve the task."},
        {"step_id": 2, "source": "user", "message": "Fix /testbed/retry.py."},
    ]
    assert atif["extra"]["native_exit_status"] == exit_status
    assert atif["extra"]["stop_reason"] == "infra_error"
    assert atif["extra"]["infra_error"] == infra_error
    assert atif["extra"]["unanswered_model_calls"] == calls
    totals = atif["final_metrics"]
    assert totals["extra"]["model_requests"] == 3
    assert totals["extra"]["requests_without_usage"] == 3
    assert "total_prompt_tokens" not in totals
    assert "total_completion_tokens" not in totals


def test_retry_error_task_and_sandbox_observations_are_not_infrastructure_diagnostics():
    task = (
        "Fix RetryError in /testbed/retry.py.\n"
        "Traceback (most recent call last):\n"
        '  File "/app/retry.py", line 2\n'
        "RetryError: attempts exhausted"
    )
    follow_up = "Retain the Traceback example and /app/config.py in the docs."
    observation = (
        "/testbed/retry.py imports /app/config.py.\n\n"
        "Tool metadata: {'log_file': '/testbed/execution.log', 'returncode': 0}"
    )
    arguments = '{"command": "cat /testbed/retry.py /app/config.py"}'
    native = {
        "info": {"exit_status": "InfraError", "stop_reason": "infra_error",
                 "infra_error": {"error_type": "InfraError"}},
        "trajs": {"main": {"messages": [
            {"role": "user", "content": task},
            {"role": "assistant", "content": "Inspect /testbed/retry.py.", "tool_calls": [{
                "id": "inspect", "function": {"name": "bash", "arguments": arguments},
            }]},
            {"role": "tool", "tool_call_id": "inspect", "name": "bash", "content": observation},
            {"role": "user", "content": follow_up},
        ]}},
    }
    atif = native_to_atif(native, [], trajectory_id="trial", model_name="model")
    assert [step["message"] for step in atif["steps"]] == [
        task, "Inspect /testbed/retry.py.", follow_up,
    ]
    assistant = atif["steps"][1]
    assert assistant["tool_calls"][0]["arguments"] == json.loads(arguments)
    assert assistant["tool_calls"][0]["extra"]["native_arguments"] == arguments
    assert assistant["observation"]["results"] == [
        {"source_call_id": "inspect", "content": observation},
    ]


@pytest.mark.parametrize("log_file", [
    "/Users/controller/eval-lab/runs/trial/mimoagent/agent_msgs/explore_1.log",
    "/tmp/native-trial/mimoagent/agent_msgs/explore_1.log",
    "explore_1.log",
])
def test_child_log_is_logical_and_linked_without_rewriting_sandbox_output(log_file):
    output = "Read /testbed/module.py and wrote /app/report.txt."
    metadata = {"subagent_type": "explore", "exit_status": "Idle", "steps": 1,
                "log_file": log_file}
    content = f"{output}\n\nTool metadata: {metadata!r}"
    native = {
        "info": {"exit_status": "Idle"},
        "trajs": {
            "main": {"messages": [
                {"role": "user", "content": "Inspect /testbed/module.py."},
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "delegate-17", "function": {"name": "agent", "arguments": "{}"},
                }]},
                {"role": "tool", "tool_call_id": "delegate-17", "name": "agent", "content": content},
            ]},
            "explore_1": {"messages": [
                {"role": "user", "content": "Inspect /testbed/module.py."},
                {"role": "assistant", "content": output},
            ]},
        },
    }
    atif = native_to_atif(native, [], trajectory_id="trial", model_name="model")
    result = atif["steps"][1]["observation"]["results"][0]
    exported_output, _, exported_metadata = result["content"].rpartition("\n\nTool metadata: ")
    assert exported_output == output
    assert ast.literal_eval(exported_metadata) == metadata | {"log_file": "childlog://explore_1"}
    assert result["source_call_id"] == "delegate-17"
    assert result["subagent_trajectory_ref"] == [{"trajectory_id": "trial:explore_1"}]
    child = atif["subagent_trajectories"][0]
    assert child["trajectory_id"] == "trial:explore_1"
    assert child["steps"][-1]["message"] == output
    serialized = json.dumps(atif)
    assert "/Users/controller" not in serialized
    assert "/tmp/native-trial" not in serialized
    assert native["trajs"]["main"]["messages"][-1]["content"] == content


def test_failed_next_query_preserves_answer_usage_and_budget_refusal_evidence():
    native = {
        "info": {"exit_status": "ModelQueryError", "stop_reason": "ceiling:tokens"},
        "trajs": {"main": {"messages": [
            {"role": "user", "content": "Fix the bug."},
            {"role": "assistant", "content": "The parser needs inspection."},
        ]}},
    }
    answered = {"name": "main", "assistant_index": 0, "response_status": 200,
                "usage": {"prompt_tokens": 10, "completion_tokens": 2}}
    refused = {"name": "main", "assistant_index": 1, "response_status": 429,
               "usage": None, "proxy_budget_reason": "ceiling:tokens"}
    atif = native_to_atif(native, [answered, refused], trajectory_id="trial", model_name="model")
    assistant = atif["steps"][1]
    assert assistant["llm_call_count"] == 1
    assert assistant["metrics"]["extra"]["calls"] == [answered]
    assert assistant["metrics"]["prompt_tokens"] == 10
    assert assistant["metrics"]["completion_tokens"] == 2
    assert atif["extra"]["unanswered_model_calls"] == [refused]
    assert atif["extra"]["stop_reason"] == "ceiling:tokens"
    assert atif["final_metrics"]["extra"]["model_requests"] == 2
    assert atif["final_metrics"]["extra"]["requests_without_usage"] == 1
    assert "total_prompt_tokens" not in atif["final_metrics"]


def test_request_evidence_without_registered_messages_is_not_dropped():
    native = {
        "info": {"exit_status": "InfraError", "stop_reason": "infra_error"},
        "trajs": {
            "main": {"messages": [{"role": "user", "content": "Inspect the task."}]},
            "unprimed": {"messages": []},
        },
    }
    calls = [
        {"name": name, "assistant_index": 0, "response_status": 503, "usage": None}
        for name in ("unprimed", "unregistered")
    ]
    atif = native_to_atif(native, calls, trajectory_id="trial", model_name="model")
    assert atif["extra"]["unassigned_model_calls"] == calls
    assert atif["final_metrics"]["extra"]["model_requests"] == 2
    assert atif["final_metrics"]["extra"]["requests_without_usage"] == 2
    assert atif["subagent_trajectories"] == []


def test_child_infrastructure_stop_does_not_override_recovered_root_idle():
    infra_error = {
        "error_type": "ModelQueryError",
        "root_error_type": "APIConnectionError",
        "last_response_status": 503,
        "retry_window_seconds": 30,
        "retry_rounds": 2,
        "budget_refusal": False,
    }
    child_stop = {"exit_status": "ModelQueryError", "stop_reason": "infra_error",
                  "infra_error": infra_error}
    native = {
        "info": {"exit_status": "Idle", "agent_stops": {"explore_1": child_stop}},
        "trajs": {
            "main": {"messages": [
                {"role": "user", "content": "Inspect and fix /testbed/parser.py."},
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "delegate", "function": {"name": "agent", "arguments": "{}"},
                }]},
                {"role": "tool", "tool_call_id": "delegate", "name": "agent", "content":
                 "Native ModelQueryError (APIConnectionError).\n\n"
                 "Tool metadata: {'exit_status': 'ModelQueryError', 'log_file': 'explore_1.log'}"},
                {"role": "assistant", "content": "Inspection failed; I completed the fix locally."},
            ]},
            "explore_1": {"messages": [
                {"role": "user", "content": "Inspect /testbed/parser.py."},
            ]},
        },
    }
    answered = [
        {"name": "main", "assistant_index": index, "response_status": 200,
         "usage": {"prompt_tokens": 10, "completion_tokens": 2}}
        for index in (0, 1)
    ]
    failed = {"name": "explore_1", "assistant_index": 0, "response_status": 503, "usage": None}
    atif = native_to_atif(native, [answered[0], failed, answered[1]],
                          trajectory_id="trial", model_name="model")
    assert atif["extra"]["native_exit_status"] == "Idle"
    assert "stop_reason" not in atif["extra"]
    assert "infra_error" not in atif["extra"]
    assert atif["steps"][-1]["message"] == "Inspection failed; I completed the fix locally."
    child = atif["subagent_trajectories"][0]
    assert child["extra"]["native_exit_status"] == "ModelQueryError"
    assert child["extra"]["stop_reason"] == "infra_error"
    assert child["extra"]["infra_error"] == infra_error
    assert child["extra"]["unanswered_model_calls"] == [failed]
    assert child["steps"] == [
        {"step_id": 1, "source": "user", "message": "Inspect /testbed/parser.py."},
    ]
    result = atif["steps"][1]["observation"]["results"][0]
    assert result["subagent_trajectory_ref"] == [{"trajectory_id": child["trajectory_id"]}]
    assert atif["final_metrics"]["extra"]["model_requests"] == 3
    assert atif["final_metrics"]["extra"]["requests_without_usage"] == 1
