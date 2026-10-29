from __future__ import annotations

import pytest

from evallab.failure_library.bank.actors import ActorUnavailable
from evallab.failure_library.bank.actors.goodfire import to_atif
from evallab.failure_library.records import RunRecord, TaskRef

RUN_ID = "goodfire-reward-hack-data:synthetic-stream:trace-1"


def _run() -> RunRecord:
    return RunRecord(
        run_id=RUN_ID,
        collection="goodfire-reward-hack-data",
        task=TaskRef(
            source_id="synthetic-env",
            task_id="synthetic-task",
            upstream_repo="https://example.invalid/synthetic",
        ),
        harness="synthetic",
        reward=None,
        outcome="unknown",
        labels=[],
        transcript_ref="file:///synthetic.jsonl#line=1&field=traces",
        license=None,
    )


def _node(message: dict) -> dict:
    return {"message": message}


def test_projects_native_nodes_with_reasoning_calls_and_feedback():
    raw = {
        "nodes": [
            _node({"role": "user", "content": "Synthetic task."}),
            _node(
                {
                    "role": "assistant",
                    "content": "Looking.",
                    "reasoning": "Plan the synthetic check.",
                    "tool_calls": [
                        {"id": "call-1", "function": "bash", "arguments": {"cmd": "echo a"}}
                    ],
                }
            ),
            _node({"role": "tool", "tool_call_id": "call-1", "content": "a"}),
            _node({"role": "assistant", "content": "Done."}),
        ]
    }
    projection = to_atif(raw, _run())
    steps = projection.doc["steps"]
    assert projection.doc["schema_version"] == "ATIF-v1.6"
    assert projection.doc["session_id"] == RUN_ID
    assert [s["source"] for s in steps] == ["user", "agent", "system", "agent"]
    assert steps[1]["reasoning_content"] == "Plan the synthetic check."
    assert steps[1]["tool_calls"][0]["tool_call_id"] == "call-1"
    assert steps[2]["observation"]["results"][0]["source_call_id"] == "call-1"
    assert projection.origins == {1: "nodes[0]", 2: "nodes[1]", 3: "nodes[2]", 4: "nodes[3]"}


def test_annotations_outside_message_are_excluded():
    raw = {
        "nodes": [
            {"message": {"role": "assistant", "content": "Hi."}, "judge": "monitor_verdict:yes"},
        ]
    }
    projection = to_atif(raw, _run())
    assert "monitor_verdict" not in repr(projection.doc)


def test_refuses_orphan_tool_feedback():
    raw = {
        "nodes": [
            _node({"role": "assistant", "content": "x"}),
            _node({"role": "tool", "tool_call_id": "missing", "content": "y"}),
        ]
    }
    with pytest.raises(ActorUnavailable):
        to_atif(raw, _run())


def test_refuses_unknown_role():
    with pytest.raises(ActorUnavailable):
        to_atif({"nodes": [_node({"role": "judge", "content": "x"})]}, _run())


def test_resampled_alternative_is_excluded_and_its_reused_call_id_kept_on_final_path():
    call = [{"id": "call-1", "function": "bash", "arguments": {"cmd": "echo a"}}]
    raw = {
        "nodes": [
            {"message": {"role": "user", "content": "Synthetic task."}},
            {
                "message": {"role": "assistant", "content": "draft", "tool_calls": call},
                "parent": 0,
                "sampled": True,
            },
            {
                "message": {"role": "assistant", "content": "chosen", "tool_calls": call},
                "parent": 0,
            },
            {"message": {"role": "tool", "tool_call_id": "call-1", "content": "a"}, "parent": 2},
        ]
    }
    projection = to_atif(raw, _run())
    steps = projection.doc["steps"]
    assert [step["message"] for step in steps] == ["Synthetic task.", "chosen", ""]
    assert projection.origins == {1: "nodes[0]", 2: "nodes[2]", 3: "nodes[3]"}
    assert "draft" not in str(projection.doc)
    assert any("1 alternative nodes excluded" in note for note in projection.limitations)


def test_refuses_non_trace_and_annotation_only_rows():
    with pytest.raises(ActorUnavailable):
        to_atif({"judgment": "yes"}, _run())
    with pytest.raises(ActorUnavailable):
        to_atif({"nodes": [{"judgment": "yes"}]}, _run())
    with pytest.raises(ActorUnavailable):
        to_atif({"nodes": [_node({"role": "user", "content": "only prompt"})]}, _run())
