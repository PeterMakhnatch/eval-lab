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
