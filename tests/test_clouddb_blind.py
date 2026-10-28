"""Synthetic DB/ATIF boundary tests; never read the shared corpus or labels."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from evallab.readers.clouddb_blind import (
    ID_SALT,
    ActorHistoryUnavailable,
    atif_blind_messages,
    blind_messages,
    dev_trace_refs,
    opaque_id,
)

REASONING = "完整 reasoning\n" * 20_000
OBSERVATION = "完整 observation\n" * 20_000
FORBIDDEN = (
    "goodfire-reward-hack-data", "tb4-audit-v1", "mimo-defect-map-v1", "tb2-to-tb21-v1",
    "source_passages", "fixture-publisher-label", "fixture-receipt-label",
    "model-private", "run-private", "trace-goodfire", "trace-tb4", "trace-mimo", "trace-tb2",
    "DEV", "TEST", "fixture-metadata-source",
)
ACTOR_INPUT = json.dumps({
    "prior_feedback": {"label": "previous attempt", "reward": 0.0, "is_exploit": False},
    "source_label": "a field in the supplied configuration",
    "receipt": {"message": "Keep the actual earlier feedback: café 完整."},
    "answer_key": {"labels": ["input", "output"]},
}, ensure_ascii=False)


def insert_step(
    con: sqlite3.Connection,
    trace_id: str,
    index: int,
    role: str,
    *,
    content: str = "",
    command: str | None = None,
    observation: str | None = None,
    flags: dict[str, Any] | None = None,
) -> None:
    con.execute(
        "INSERT OR IGNORE INTO agent_traces "
        "(trace_id, task_id, run_id, model_id, outcome, verified_receipt_json) "
        "VALUES (?, 'task-goodfire', 'fixture-run', 'fixture/native-model', 'unknown', '{}')",
        (trace_id,),
    )
    con.execute("INSERT INTO trace_steps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
        trace_id, index, role, "bash", command, content, observation, None,
        json.dumps(flags or {}),
    ))


@pytest.fixture
def corpus(tmp_path: Path) -> Iterator[tuple[Path, Path, sqlite3.Connection]]:
    root = tmp_path / "repo"
    keys = root / "research/answer-keys"
    for dataset in ("tb4-audit-v1", "cheatbench-actions-v1", "hacktrace-act-v1"):
        path = keys / dataset / "items.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("", encoding="utf-8")
    (keys / "tb4-audit-v1/tasks.jsonl").write_text("", encoding="utf-8")
    inventory = root / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(json.dumps({
        "dataset_id": "mimo-defect-map-v1", "tasks": [],
        "heldout_task_ids_excluded_from_training_all_versions": [],
    }), encoding="utf-8")
    tasks = [
        ("task-goodfire", "goodfire-reward-hack-data", "DEV"),
        ("task-tb4", "tb4-audit-v1", "DEV"),
        ("task-mimo", "mimo-defect-map-v1", "DEV"),
        ("task-tb2", "tb2-to-tb21-v1", "DEV"),
        ("task-non-dev", "goodfire-reward-hack-data", "TEST"),
        ("task-lowercase", "goodfire-reward-hack-data", "dev"),
        ("task-held", "goodfire-reward-hack-data", "DEV"),
        ("task-unadmitted", "tb4-audit-v1", "DEV"),
        ("task-cross-sealed", "goodfire-reward-hack-data", "DEV"),
    ]
    assignments = keys / "splits/assignments.csv"
    assignments.parent.mkdir()
    assignments.write_text(
        "key_id,item_id,task,split\n" + "".join(
            f"{dataset},{task},{task},{'test' if task == 'task-held' else 'dev'}\n"
            for task, dataset, _ in tasks if task != "task-unadmitted"
        ) + "tb4-audit-v1,cross-key-item,task-cross-sealed,test\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "cloud.sqlite"
    con = sqlite3.connect(db_path)
    con.executescript("""
        CREATE TABLE corpus_tasks (
            task_id TEXT PRIMARY KEY, dataset_id TEXT, split TEXT,
            original_grader_digest TEXT, repaired_grader_digest TEXT, metadata_json TEXT
        );
        CREATE TABLE agent_traces (
            trace_id TEXT PRIMARY KEY, task_id TEXT, run_id TEXT, model_id TEXT,
            outcome TEXT, verified_receipt_json TEXT
        );
        CREATE TABLE trace_steps (
            trace_id TEXT, step_index INTEGER, role TEXT, tool_name TEXT,
            command_or_action TEXT, content_text TEXT, observation_preview TEXT,
            observation_cas_uri TEXT, flags_json TEXT
        );
    """)
    for task, dataset, split in tasks:
        trace_id = task.replace("task-", "trace-")
        con.execute("INSERT INTO corpus_tasks VALUES (?, ?, ?, NULL, NULL, ?)", (
            task, dataset, split,
            json.dumps({"item_id": task, "source": "fixture-metadata-source"}),
        ))
        con.execute("INSERT INTO agent_traces VALUES (?, ?, ?, ?, ?, ?)", (
            trace_id, task, "run-private", "model-private", "fixture-receipt-label",
            json.dumps({"source_label": "fixture-receipt-label"}),
        ))
    calls = [{"id": "call-native", "name": "bash_command", "arguments": '{"command":"printf café"}'}]
    flags = {
        "source_passages": [{"text": "fixture-publisher-label"}],
        "passage_positive": True, "source_judge_flags": "fixture-publisher-label",
        "native_node_residual": {"message": {
            "tool_calls": calls, "model_id": "model-private", "source_passages": "fixture-publisher-label",
        }},
    }

    def step(trace_id: str, index: int, role: str, **values: Any) -> None:
        step_flags = {key: value for key, value in flags.items()
                      if trace_id == "trace-goodfire" or key != "native_node_residual"}
        con.execute("INSERT INTO trace_steps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            trace_id, index, role, values.get("tool_name"), values.get("command"),
            values.get("content", ""), values.get("observation"), "cas://private",
            json.dumps({**step_flags, **values.get("flags", {})}),
        ))

    step("trace-goodfire", 0, "system", content="System instructions.")
    step("trace-goodfire", 1, "user", content="Inspect the local output.")
    step("trace-goodfire", 3, "assistant", content="I will inspect.", command=json.dumps(calls),
         flags={"reasoning_content": REASONING})
    step("trace-goodfire", 4, "tool", content=OBSERVATION, flags={"tool_call_id": "call-native"})
    step("trace-tb4", 7, "agent", tool_name="Bash", command="printf 'hello'", content="Inspect.",
         observation="hello\nMissing required fields\n")
    step("trace-mimo", 0, "user", content="A different instruction.")
    step("trace-mimo", 2, "assistant", content="Native proposal: <tool_call><function=bash>.")
    step("trace-mimo", 3, "tool", content="No valid JSON", flags={"tool_call_id": "call-native"})
    step("trace-tb2", 0, "system", content="Other system instructions.")
    step("trace-tb2", 1, "assistant", content="Keep this raw content.")
    con.commit()
    try:
        yield root, db_path, con
    finally:
        con.close()


def test_opaque_id_is_contract_hash_and_does_not_expose_identity() -> None:
    assert opaque_id("trace-private") == "run-" + hashlib.sha256(
        f"{ID_SALT}:trace-private".encode()
    ).hexdigest()[:24]
    assert opaque_id("other") != opaque_id("trace-private")


def test_exact_dev_gate_intersects_cross_key_admission(corpus: tuple) -> None:
    root, _, con = corpus
    refs = dev_trace_refs(con, repo_root=root)
    assert [ref.trace_id for ref in refs] == ["trace-goodfire", "trace-mimo", "trace-tb2", "trace-tb4"]
    assert {ref.task_id for ref in refs} == {"task-goodfire", "task-mimo", "task-tb2", "task-tb4"}
    assert len({ref.dataset_id for ref in refs}) == 4
    # Admission must not depend on the caller's SQLite row factory.
    con.row_factory = sqlite3.Row
    assert dev_trace_refs(con, repo_root=root) == refs


def test_missing_assignment_authority_cannot_admit_db_dev(corpus: tuple) -> None:
    root, _, con = corpus
    (root / "research/answer-keys/splits/assignments.csv").unlink()
    assert dev_trace_refs(con, repo_root=root) == []


def test_all_schemas_preserve_pairing_reasoning_and_full_content(corpus: tuple) -> None:
    _, _, con = corpus
    native = blind_messages(con, "trace-goodfire")
    assert [message["role"] for message in native] == ["system", "user", "assistant", "tool"]
    assistant, tool = native[2:]
    assert assistant["reasoning"] == REASONING
    assert tool["content"] == OBSERVATION
    assert assistant["tool_calls"] == [{
        "id": "call-native", "function": "bash_command", "arguments": {"command": "printf café"},
    }]
    assert tool["tool_call_id"] == assistant["tool_calls"][0]["id"]
    agent, observation = blind_messages(con, "trace-tb4")
    assert agent["role"] == "assistant" and observation["role"] == "tool"
    assert agent["step_index"] == observation["step_index"] == 7
    assert agent["tool_calls"][0]["arguments"] == {"command": "printf 'hello'"}
    assert observation["tool_call_id"] == agent["tool_calls"][0]["id"]
    assert observation["content"] == "hello\nMissing required fields\n"
    assert blind_messages(con, "trace-mimo")[2]["content"] == "No valid JSON"
    assert blind_messages(con, "trace-tb2")[1]["content"] == "Keep this raw content."


def test_publisher_labels_and_all_identity_columns_are_not_projected(corpus: tuple) -> None:
    root, _, con = corpus
    before = [blind_messages(con, ref.trace_id) for ref in dev_trace_refs(con, repo_root=root)]
    serialized = json.dumps(before, ensure_ascii=False)
    for forbidden in FORBIDDEN:
        assert forbidden not in serialized
    con.execute("""
        UPDATE trace_steps SET flags_json = json_set(
            flags_json, '$.source_passages', 'different-label',
            '$.source_judge_flags', 'different-verdict', '$.passage_positive', 0,
            '$.native_node_residual.message.model_id', 'different-model'
        )
    """)
    after = [blind_messages(con, ref.trace_id) for ref in dev_trace_refs(con, repo_root=root)]
    assert after == before


def test_native_residual_calls_and_non_object_arguments_are_preserved(corpus: tuple) -> None:
    _, _, con = corpus
    con.execute("UPDATE trace_steps SET command_or_action = NULL WHERE trace_id = 'trace-goodfire'")
    assert blind_messages(con, "trace-goodfire")[2]["tool_calls"][0]["id"] == "call-native"
    calls = [{"id": "raw-call", "name": "custom", "arguments": "not JSON"},
             {"id": "array-call", "name": "custom", "arguments": "[1, 2]"}]
    con.execute("UPDATE trace_steps SET command_or_action = ? WHERE trace_id = 'trace-goodfire' AND role = 'assistant'",
                (json.dumps(calls),))
    projected = blind_messages(con, "trace-goodfire")[2]["tool_calls"]
    assert projected[0]["arguments"] == {"raw": "not JSON"}
    assert projected[1]["arguments"] == {"raw": [1, 2]}


@pytest.mark.parametrize("trace_kind", [
    "seeker_attempt_event_transcript",
    "seeker_event_journal",
    "historical_rejected_task_quality_journal",
    "selected_task_quality_event_journal",
    "native_execution_receipt_record",
    "retained_differential_receipt",
    "retained_native_artifact_observation",
])
@pytest.mark.parametrize("feedback", [1.0, "grader unavailable"])
def test_wholly_nonmodel_control_history_is_explicitly_unavailable(
    corpus: tuple, trace_kind: str, feedback: float | str,
) -> None:
    _, _, con = corpus
    content = json.dumps({"event_type": "seeker.grader_feedback", "feedback": feedback})
    for index, role in ((2, "system"), (9, "assistant"), (14, "tool")):
        insert_step(
            con, "control-only", index, role, content=content,
            flags={"trace_kind": trace_kind, "is_model_message": False},
        )
    with pytest.raises(ActorHistoryUnavailable) as caught:
        blind_messages(con, "control-only")
    assert caught.value.category == "actor_history_unavailable"
    assert caught.value.trace_id == "control-only"
    assert caught.value.reason
    assert content not in str(caught.value)


def test_mixed_history_projects_only_actor_rows_with_original_indices(corpus: tuple) -> None:
    _, _, con = corpus
    con.execute(
        "UPDATE trace_steps SET content_text = ? WHERE trace_id = ? AND step_index = 1",
        (ACTOR_INPUT, "trace-goodfire"),
    )
    actor_messages = blind_messages(con, "trace-goodfire")
    for index, role, trace_kind, content in (
        (2, "system", "seeker_attempt_event_transcript", ACTOR_INPUT),
        (5, "assistant", "seeker_event_journal", "controller-only numeric feedback: 1.0"),
        (8, "tool", "native_execution_receipt_record", "controller-only unavailable feedback"),
    ):
        insert_step(
            con, "trace-goodfire", index, role, content=content,
            command='[{"id":"controller-only","name":"bash","arguments":{}}]',
            flags={
                "trace_kind": trace_kind, "is_model_message": False,
                "reasoning_content": "controller-only reasoning",
            },
        )
    projected = blind_messages(con, "trace-goodfire")
    assert projected == actor_messages
    assert [message["step_index"] for message in projected] == [0, 1, 3, 4]
    assert projected[1]["content"] == ACTOR_INPUT
    assert projected[2]["reasoning"] == REASONING
    assert projected[3]["content"] == OBSERVATION
    assert projected[3]["tool_call_id"] == projected[2]["tool_calls"][0]["id"]
    assert "controller-only" not in json.dumps(projected)


@pytest.mark.parametrize("role", ["system", "user", "assistant", "tool", "agent"])
@pytest.mark.parametrize("is_model_message", [False, True])
@pytest.mark.parametrize("provenance", [{}, {"trace_kind": None}], ids=["absent-kind", "null-kind"])
def test_source_native_rows_do_not_depend_on_model_producer_marker(
    corpus: tuple, role: str, is_model_message: bool, provenance: dict[str, Any],
) -> None:
    _, _, con = corpus
    call = {"id": "actor-call", "name": "bash", "arguments": {"label": "output", "reward": 0}}
    insert_step(
        con, "native-provenance", 11, role, content=ACTOR_INPUT,
        command=json.dumps([call]), observation=OBSERVATION if role == "agent" else None,
        flags={
            **provenance, "is_model_message": is_model_message,
            "reasoning_content": REASONING, "tool_call_id": "actor-call",
        },
    )
    messages = blind_messages(con, "native-provenance")
    assert messages[0] == {
        "step_index": 11, "role": "assistant" if role == "agent" else role,
        "content": ACTOR_INPUT, "reasoning": REASONING,
        "tool_calls": [{
            "id": "actor-call", "function": "bash", "arguments": call["arguments"],
        }] if role in {"assistant", "agent"} else [],
        "tool_call_id": "actor-call" if role == "tool" else None,
    }
    if role == "agent":
        assert len(messages) == 2
        assert messages[1] == {
            "step_index": 11, "role": "tool", "content": OBSERVATION,
            "reasoning": None, "tool_calls": [], "tool_call_id": "actor-call",
        }
    else:
        assert len(messages) == 1


@pytest.mark.parametrize(("trace_kind", "role", "is_model_message"), [
    ("retained_headless_prompt", "user", False),
    ("retained_complete_headless_stdout", "assistant", False),
    ("retained_model_message", "assistant", True),
])
def test_explicit_retained_actor_sources_preserve_supplied_feedback(
    corpus: tuple, trace_kind: str, role: str, is_model_message: bool,
) -> None:
    _, _, con = corpus
    reasoning = ACTOR_INPUT + "\n" + REASONING
    insert_step(
        con, "retained-actor", 17, role, content=ACTOR_INPUT,
        flags={
            "trace_kind": trace_kind, "is_model_message": is_model_message,
            "reasoning_content": reasoning,
        },
    )
    if trace_kind == "retained_headless_prompt":
        insert_step(
            con, "retained-actor", 18, "assistant", content="Complete retained actor response.",
            flags={"trace_kind": "retained_complete_headless_stdout", "is_model_message": False},
        )
    elif trace_kind == "retained_complete_headless_stdout":
        insert_step(
            con, "retained-actor", 16, "user", content=ACTOR_INPUT,
            flags={"trace_kind": "retained_headless_prompt", "is_model_message": False},
        )
    messages = blind_messages(con, "retained-actor")
    assert next(message for message in messages if message["step_index"] == 17) == {
        "step_index": 17, "role": role, "content": ACTOR_INPUT,
        "reasoning": reasoning, "tool_calls": [], "tool_call_id": None,
    }
    assert len(messages) == (1 if trace_kind == "retained_model_message" else 2)


@pytest.mark.parametrize(("trace_kind", "role"), [
    ("retained_headless_prompt", "user"),
    ("retained_complete_headless_stdout", "assistant"),
])
def test_incomplete_retained_headless_capture_is_unknown(
    corpus: tuple, trace_kind: str, role: str,
) -> None:
    _, _, con = corpus
    insert_step(
        con, "incomplete-headless", 1, role, content=ACTOR_INPUT,
        flags={"trace_kind": trace_kind},
    )
    with pytest.raises(ActorHistoryUnavailable) as caught:
        blind_messages(con, "incomplete-headless")
    assert caught.value.category == "actor_history_unavailable"


@pytest.mark.parametrize("layers", [1, 2, 3])
def test_quoted_actor_json_and_reasoning_are_not_censored(corpus: tuple, layers: int) -> None:
    _, _, con = corpus
    content = ACTOR_INPUT
    for _ in range(layers):
        content = json.dumps(content, ensure_ascii=False)
    reasoning = content + "\n" + REASONING
    insert_step(con, "quoted-actor", 4, "user", content=content)
    insert_step(
        con, "quoted-actor", 9, "assistant", content="Using the supplied prior feedback.",
        flags={"reasoning_content": reasoning, "is_model_message": True},
    )
    insert_step(con, "quoted-actor", 12, "tool", content=content)
    messages = blind_messages(con, "quoted-actor")
    assert [message["step_index"] for message in messages] == [4, 9, 12]
    assert messages[0]["content"] == messages[2]["content"] == content
    assert messages[1]["reasoning"] == reasoning


@pytest.mark.parametrize("is_model_message", [False, True])
def test_unknown_source_kind_is_unavailable_even_among_valid_actor_rows(
    corpus: tuple, is_model_message: bool,
) -> None:
    _, _, con = corpus
    insert_step(
        con, "trace-goodfire", 10, "system", content="unknown provenance body",
        flags={"trace_kind": "future_unknown_source_kind", "is_model_message": is_model_message},
    )
    with pytest.raises(ActorHistoryUnavailable) as caught:
        blind_messages(con, "trace-goodfire")
    assert caught.value.category == "actor_history_unavailable"
    assert caught.value.trace_id == "trace-goodfire"
    assert caught.value.step_index == 10
    assert caught.value.reason
    assert "unknown provenance body" not in str(caught.value)


@pytest.mark.parametrize("trace_id", ["trace-tb2", "no-such-trace"])
def test_missing_actor_steps_do_not_become_an_empty_conversation(corpus: tuple, trace_id: str) -> None:
    _, _, con = corpus
    con.execute("DELETE FROM trace_steps WHERE trace_id = ?", (trace_id,))
    with pytest.raises(ActorHistoryUnavailable) as caught:
        blind_messages(con, trace_id)
    assert caught.value.category == "actor_history_unavailable"
    assert caught.value.trace_id == trace_id
    assert caught.value.reason


def test_atif_blinds_annotations_before_lossless_projection() -> None:
    doc = {
        "agent": {"model": "model-private"},
        "metadata": {"split": "DEV", "source_passages": "fixture-publisher-label"},
        "extra": {"cheatbench": {"label": "fixture-publisher-label"}},
        "steps": [
            {"step_id": 99, "source": "system", "message": "System instructions."},
            {"step_id": 101, "source": "user", "message": "Please inspect."},
            {"step_id": 102, "source": "agent", "message": "Inspecting.",
             "reasoning_content": REASONING,
             "extra": {"source_passages": "fixture-publisher-label"},
             "tool_calls": [{"tool_call_id": "atif-call", "function_name": "bash_command",
                             "arguments": {"command": "printf café"}}],
             "observation": {"results": [{"source_call_id": "atif-call", "content": OBSERVATION}]}},
            {"step_id": 103, "source": "user", "message": "[CheatBench annotation] fixture-publisher-label"},
        ],
    }
    messages = atif_blind_messages(doc, item_id="private-item-id")
    assert [message["role"] for message in messages] == ["system", "user", "assistant", "tool"]
    assert [message["step_index"] for message in messages] == [0, 1, 2, 2]
    assert messages[2]["reasoning"] == REASONING
    assert messages[3]["content"] == OBSERVATION
    assert messages[3]["tool_call_id"] == messages[2]["tool_calls"][0]["id"] == "atif-call"
    for forbidden in (*FORBIDDEN, "private-item-id", "CheatBench annotation"):
        assert forbidden not in json.dumps(messages, ensure_ascii=False)
    assert doc["steps"][2]["step_id"] == 102
