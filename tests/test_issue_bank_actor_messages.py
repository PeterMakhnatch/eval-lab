from __future__ import annotations

import pytest

from evallab.failure_library.bank.actors import ActorUnavailable
from evallab.failure_library.bank.actors.messages import to_atif
from evallab.failure_library.records import RunRecord, TaskRef


def run(collection: str = "hacktrace") -> RunRecord:
    return RunRecord(
        run_id=f"{collection}:synthetic-1",
        collection=collection,
        task=TaskRef(source_id="synthetic", task_id="t1", upstream_repo="synthetic"),
        outcome="unknown",
        model="synthetic-model",
        harness="synthetic",
        transcript_ref="file:///synthetic.parquet#line=1&field=messages",
        license="synthetic",
    )


def call(call_id: str, name: str = "run", arguments: str = "{}") -> dict:
    return {"id": call_id, "function": {"name": name, "arguments": arguments}}


def test_tool_call_is_bound_to_matching_tool_reply() -> None:
    messages = [
        {"role": "user", "content": "solve it"},
        {
            "role": "assistant",
            "content": "checking",
            "reasoning_content": "think first",
            "tool_calls": [call("c1", "shell", '{"cmd":"ls"}')],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "file.txt"},
        {"role": "assistant", "content": "done"},
    ]
    projection = to_atif(messages, run())
    steps = projection.doc["steps"]
    assert projection.doc["schema_version"] == "ATIF-v1.6"
    assert steps[1]["source"] == "agent"
    assert steps[1]["reasoning_content"] == "think first"
    assert steps[1]["tool_calls"] == [
        {"tool_call_id": "c1", "function_name": "shell", "arguments": '{"cmd":"ls"}'}
    ]
    assert steps[2]["observation"]["results"] == [{"source_call_id": "c1", "content": "file.txt"}]
    assert projection.origins == {1: 0, 2: 1, 3: 2, 4: 3}  # ordinal -> native message index


def test_tool_reply_without_matching_call_is_refused() -> None:
    messages = [
        {"role": "assistant", "content": "hi"},
        {"role": "tool", "tool_call_id": "missing", "content": "x"},
    ]
    with pytest.raises(ActorUnavailable):
        to_atif(messages, run())


def test_unknown_role_is_refused_not_assistant() -> None:
    messages = [
        {"role": "assistant", "content": "hi"},
        {"role": "judge", "content": "verdict text"},
    ]
    with pytest.raises(ActorUnavailable):
        to_atif(messages, run("olmo-reward-hacking"))


def test_system_and_user_roles_keep_their_source() -> None:
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "answer"},
    ]
    steps = to_atif(messages, run("olmo-reward-hacking")).doc["steps"]
    assert [step["source"] for step in steps] == ["system", "user", "agent"]


def test_label_fields_are_excluded_from_actor_input() -> None:
    raw = [{"role": "assistant", "content": "answer", "ACT": 1, "reward_hacked": True}]
    projection = to_atif(raw, run())
    rendered = repr(projection.doc)
    assert "ACT" not in rendered
    assert "reward_hacked" not in rendered
    assert "answer" in rendered


def test_unsupported_collection_is_refused() -> None:
    with pytest.raises(ActorUnavailable):
        to_atif([{"role": "assistant", "content": "x"}], run("metr-malt"))
