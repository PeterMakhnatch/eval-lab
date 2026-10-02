from __future__ import annotations

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
