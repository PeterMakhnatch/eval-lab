from __future__ import annotations

import pytest

from evallab.failure_library.bank.actors import ActorUnavailable
from evallab.failure_library.bank.actors.ablation import to_atif
from evallab.failure_library.records import RunRecord, TaskRef


def _run(harness: str, collection: str = "reward-hacking-ablation") -> RunRecord:
    return RunRecord(
        run_id="run-1",
        collection=collection,
        task=TaskRef(
            source_id="ablation-test",
            task_id="task-1",
            upstream_repo="https://example.invalid/dataset",
        ),
        harness=harness,
        outcome="unknown",
        transcript_ref="file:///synthetic/trajectories.jsonl#line=1",
    )


def _tool_row() -> dict:
    return {
        "messages": [
            {"role": "system", "step_id": 1, "content": "Solve the task."},
            {"role": "user", "step_id": 2, "content": "Make the test pass."},
            {
                "role": "assistant",
                "step_id": 5,
                "content": None,
                "reasoning_content": "I will read the file first.",
                "tool_calls": [
                    {
                        "id": "call-a",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-a", "content": "def f():\n    return 1\n"},
            {
                "role": "assistant",
                "step_id": 7,
                "content": [{"type": "text", "text": "Done."}],
                "reasoning": "Return value checked.",
            },
        ]
    }


def test_tools_harness_keeps_reasoning_calls_and_feedback_in_native_order() -> None:
    projection = to_atif(_tool_row(), _run("tools"))

    assert projection.scope == "source_native_run"
    assert projection.doc["schema_version"] == "ATIF-v1.6"
    assert projection.doc["session_id"] == "run-1"
    steps = projection.doc["steps"]
    assert [step["source"] for step in steps] == ["system", "user", "agent", "agent"]
    assert steps[2]["reasoning_content"] == "I will read the file first."
    assert steps[2]["tool_calls"] == [
        {"tool_call_id": "call-a", "function_name": "read_file", "arguments": {"path": "a.py"}}
    ]
    assert steps[2]["observation"] == {
        "results": [{"source_call_id": "call-a", "content": "def f():\n    return 1\n"}]
    }
    assert steps[3]["message"] == "Done."
    assert steps[3]["reasoning_content"] == "Return value checked."
    # Native integer step ids are kept only when every non-tool entry carries a unique one.
    assert [step["step_id"] for step in steps] == [1, 2, 5, 7]
    assert projection.origins == {
        1: "messages[0]",
        2: "messages[1]",
        5: "messages[2]",
        7: "messages[4]",
    }


def test_rhenvs_items_keep_typed_events_and_ordinal_origins() -> None:
    row = {
        "items": [
            {"role": "user", "content": "Write the module."},
            {"type": "agent_log", "text": "Starting the edit."},
            {"role": "assistant", "content": "Edited."},
            {"type": "agent_episode", "response": "Final answer."},
        ]
    }

    projection = to_atif(row, _run("rhenvs/grader"))

    steps = projection.doc["steps"]
    assert [step["step_id"] for step in steps] == [1, 2, 3, 4]
    assert [step["source"] for step in steps] == ["user", "agent", "agent", "agent"]
    assert [step["message"] for step in steps] == [
        "Write the module.",
        "Starting the edit.",
        "Edited.",
        "Final answer.",
    ]
    assert projection.origins == {
        1: "items[0]",
        2: "items[1]",
        3: "items[2]",
        4: "items[3]",
    }
    assert projection.scope == "source_native_run"


def test_single_step_is_scoped_as_single_response() -> None:
    row = {"messages": [{"role": "assistant", "content": "The answer is 4."}]}

    projection = to_atif(row, _run("single_step"))

    assert projection.scope == "single_response"
    assert projection.limitations


@pytest.mark.parametrize(
    "row",
    [
        {"messages": [{"role": "assistant", "content": "x", "token_ids": [1, 2]}]},
        {"token_ids": [1, 2, 3], "messages": [{"role": "assistant", "content": "x"}]},
        {"score": {"value": "C"}, "task_id": "t"},
        {"messages": []},
    ],
)
def test_token_only_and_summary_rows_are_refused(row: dict) -> None:
    with pytest.raises(ActorUnavailable):
        to_atif(row, _run("tools"))


def test_final_text_only_episode_is_refused_for_unsupported_harness() -> None:
    row = {"messages": [{"role": "assistant", "content": "final"}]}

    with pytest.raises(ActorUnavailable):
        to_atif(row, _run("rhenvs/concept"))


def test_non_ablation_collection_is_refused() -> None:
    with pytest.raises(ActorUnavailable):
        to_atif(_tool_row(), _run("tools", collection="olmo-reward-hacking"))


def test_tool_feedback_without_earlier_call_is_refused() -> None:
    row = {
        "messages": [
            {"role": "assistant", "content": "Working."},
            {"role": "tool", "tool_call_id": "call-z", "content": "orphan"},
        ]
    }

    with pytest.raises(ActorUnavailable):
        to_atif(row, _run("tools"))


def test_repeated_tool_call_id_is_refused_as_ambiguous() -> None:
    call = {"id": "dup", "function": {"name": "run", "arguments": "{}"}}
    row = {
        "messages": [
            {"role": "assistant", "content": "", "tool_calls": [call]},
            {"role": "assistant", "content": "", "tool_calls": [call]},
        ]
    }

    with pytest.raises(ActorUnavailable):
        to_atif(row, _run("tools"))


def test_unknown_role_is_refused_not_defaulted() -> None:
    row = {"messages": [{"role": "environment", "content": "log line"}]}

    with pytest.raises(ActorUnavailable):
        to_atif(row, _run("tools"))


def test_non_text_content_part_is_refused() -> None:
    row = {
        "messages": [
            {"role": "assistant", "content": [{"type": "image_url", "image_url": "x"}]},
        ]
    }

    with pytest.raises(ActorUnavailable):
        to_atif(row, _run("tools"))
