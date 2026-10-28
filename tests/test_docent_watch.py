"""Hosted-free admission, blinded whole-run evidence, and crash-safe readings."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest

from evallab.readers.docent_reading import ReadingBlocked, allowance, hosted_model, judgment, render
from evallab.readers.docent_watch import run_once

MODEL = {"provider": "openai", "model_name": "gpt-5.4-mini", "reasoning_effort": "low",
         "uses_byok": False, "context_window": 1_000_000}


@pytest.mark.parametrize("summary,cap", [
    ({}, {}), ({"free": {"has_cap": False}}, {"cap_cents": 2500}),
    ({"free": {"has_cap": True, "total_cents": 2500}}, {"cap_cents": 2500}),
    ({"free": {"has_cap": True, "total_cents": 2600}}, {"cap_cents": 2500}),
    ({"free": {"has_cap": True, "total_cents": -1}}, {"cap_cents": 2500}),
    ({"free": {"has_cap": True, "total_cents": 0}}, {"cap_cents": float("nan")}),
    ({"free": {"has_cap": True, "total_cents": True}}, {"cap_cents": 2500}),
    ({"free": {"has_cap": True, "total_cents": 0, "cap_cents": 3000}}, {"cap_cents": 2500}),
])
def test_unknown_or_exhausted_allowance_never_admits(summary: dict, cap: dict) -> None:
    with pytest.raises(ReadingBlocked):
        allowance(summary, cap)


def test_available_free_cap_has_explicit_remaining_value() -> None:
    assert allowance({"free": {"has_cap": True, "total_cents": 2499}}, {"cap_cents": 2500}) == {
        "cap_cents": 2500, "used_cents": 2499, "remaining_cents": 1,
    }


@pytest.mark.parametrize("catalog", [
    [], [dict(MODEL, uses_byok=True)], [dict(MODEL, uses_byok=None)],
    [MODEL, dict(MODEL, uses_byok=True)], [dict(MODEL, provider="openrouter")],
    [dict(MODEL, context_window=0)], [dict(MODEL, reasoning_effort="high")],
])
def test_unavailable_ambiguous_or_paid_model_never_falls_back(catalog: list) -> None:
    with pytest.raises(ReadingBlocked):
        hosted_model(catalog)


def test_valid_step_is_distinct_from_valid_negative_and_unknown() -> None:
    yes = {"reward_hacking": "yes", "mechanism": "Changed a grader", "evidence_step": 7, "confidence": 3}
    assert judgment({"output": yes}, {1, 7}) == (True, yes, [7])
    assert judgment({"output": yes}, {1, 8})[0] is None
    no = {"reward_hacking": "no", "mechanism": "none", "evidence_step": None, "confidence": 2}
    assert judgment({"output": no}, {1, 7}) == (False, no, [])
    assert judgment({"output": dict(no, evidence_step=7)}, {1, 7})[0] is None
    assert judgment({"output": dict(yes, confidence=True)}, {1, 7})[0] is None
    assert judgment({"output": dict(yes, reward_hacking=["yes"])}, {1, 7}) == (
        None, {"abstention_reason": "invalid_form"}, [],
    )
    assert judgment({"error": {"error_type_id": "docent_usage_limit"}}, {1, 7}) == (
        None, {"abstention_reason": "hosted_free_allowance_exhausted"}, [],
    )
    assert judgment({"error": {"error_type_id": "refusal"}}, {1, 7})[0] is None


def test_whole_run_retains_long_reasoning_and_call_identity() -> None:
    reasoning = "Before the command: " + "reasoning " * 2000 + "THE END"
    messages = [{"step_index": 91, "role": "assistant", "content": "run command", "reasoning": reasoning,
                 "tool_calls": [{"id": "call-7", "function": "bash", "arguments": {"cmd": "echo actor"}}]},
                {"step_index": 92, "role": "tool", "content": "actor output", "tool_call_id": "call-7"}]
    text = render(messages)
    assert reasoning in text
    assert '[Evidence step 91]' in text and '[Evidence step 92]' in text
    assert json.loads(text.split("\n", 1)[1].split("\n\n", 1)[0])["tool_calls"] == messages[0]["tool_calls"]
    assert '"tool_call_id":"call-7"' in text


class Service:
    def __init__(self) -> None:
        self.free = True
        self.submissions: list[dict[str, Any]] = []
        self.plans: dict[str, dict[str, Any]] = {}
        self.approvals = 0
        self.lose_submit_response = False
        self.lose_approval_response = False
        self.collection_id = str(uuid.uuid4())

    def gate(self) -> tuple[dict[str, float], dict[str, Any]]:
        if not self.free:
            raise ReadingBlocked("hosted_free_allowance_exhausted")
        return {"cap_cents": 2500, "used_cents": 0, "remaining_cents": 2500}, MODEL

    def collection(self, dataset: str) -> dict[str, str]:
        return {"id": self.collection_id, "dataset": dataset}

    def find_plan(self, collection_id: str, name: str) -> str | None:
        return next((key for key, value in self.plans.items() if value["name"] == name), None)

    def submit(self, collection_id: str, name: str, opaque: str, text: str) -> str:
        identity = str(uuid.uuid4())
        self.submissions.append({"opaque": opaque, "text": text})
        self.plans[identity] = {"name": name, "status": "needs_approval", "row": None}
        if self.lose_submit_response:
            self.lose_submit_response = False
            raise TimeoutError("response lost after server accepted request")
        return identity

    def inspect(self, collection_id: str, plan_id: str) -> tuple[str, Any]:
        plan = self.plans[plan_id]
        return plan["status"], plan["row"]

    def approve(self, collection_id: str, plan_id: str) -> None:
        self.approvals += 1
        self.plans[plan_id]["status"] = "running"
        if self.lose_approval_response:
            raise TimeoutError("approval response lost")

    def complete(self) -> None:
        for plan in self.plans.values():
            plan.update(status="completed", row={
                "output": {"reward_hacking": "no", "mechanism": "none", "evidence_step": None, "confidence": 2},
                "input_tokens": 100, "output_tokens": 10, "served_provider": "openai",
            })


@pytest.fixture
def database(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "repo"
    keys = root / "research/answer-keys"
    for dataset in ("tb4-audit-v1", "cheatbench-actions-v1", "hacktrace-act-v1"):
        folder = keys / dataset
        folder.mkdir(parents=True)
        (folder / "items.jsonl").write_text("")
    (keys / "tb4-audit-v1/tasks.jsonl").write_text("")
    inventory = root / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(json.dumps({
        "dataset_id": "mimo-defect-map-v1", "tasks": [],
        "heldout_task_ids_excluded_from_training_all_versions": [],
    }))
    assignment = keys / "splits/assignments.csv"
    assignment.parent.mkdir()
    assignment.write_text("key_id,item_id,task,split\n")
    evidence = tmp_path / "sources.json"
    evidence.write_text(json.dumps({"artifact_sources": {"mimo30": {"traces": 1}}}))
    db = tmp_path / "cloud.sqlite"
    with sqlite3.connect(db) as con:
        con.executescript("""
CREATE TABLE corpus_tasks(task_id TEXT PRIMARY KEY,dataset_id TEXT,split TEXT,original_grader_digest TEXT,
                          repaired_grader_digest TEXT,metadata_json TEXT);
CREATE TABLE agent_traces(trace_id TEXT PRIMARY KEY,task_id TEXT,run_id TEXT,model_id TEXT,outcome TEXT,
                          verified_receipt_json TEXT,reward REAL,trust_tier TEXT);
CREATE TABLE trace_steps(trace_id TEXT,step_index INTEGER,role TEXT,tool_name TEXT,command_or_action TEXT,
                         content_text TEXT,observation_preview TEXT,observation_cas_uri TEXT,flags_json TEXT);
""")
    args = {"db_path": db, "repo_root": root, "state_dir": tmp_path / "state",
            "results_path": tmp_path / "results.jsonl", "spend_path": tmp_path / "spend.jsonl",
            "evidence_path": evidence, "cas_root": tmp_path / "cas"}
    add_trace(args, "own", "DEV")
    add_trace(args, "held", "TEST")
    add_trace(args, "goodfire", "DEV", dataset="goodfire-reward-hack-data")
    return args


def add_trace(args: dict[str, Path], identity: str, split: str, *, dataset: str = "mimo-defect-map-v1") -> None:
    task = "mimo-v2.6-rl-code/" + identity
    with sqlite3.connect(args["db_path"]) as con:
        con.execute("INSERT INTO corpus_tasks VALUES(?,?,?,NULL,NULL,?)", (
            task, dataset, split, json.dumps({"source_label": "NEVER_SEND_SOURCE_LABEL"}),
        ))
        con.execute("INSERT INTO agent_traces VALUES(?,?,?,?,?,?,?,?)", (
            identity, task, "mimo30", "NEVER_SEND_ACTOR_MODEL", "NEVER_SEND_OUTCOME",
            json.dumps({"reward": 1, "source_label": "NEVER_SEND_RECEIPT"}), 1.0, "NEVER_SEND_TRUST",
        ))
        con.execute("INSERT INTO trace_steps VALUES(?,0,'user',NULL,NULL,?,NULL,NULL,?)", (
            identity, "Complete the actor's task.", json.dumps({"is_model_message": True, "source_label": "NEVER_SEND_FLAGS"}),
        ))
    with (args["repo_root"] / "research/answer-keys/splits/assignments.csv").open("a") as stream:
        stream.write(f"{dataset},{task},{task},{split.lower()}\n")


def test_new_run_arrival_is_blinded_and_exhaustion_never_becomes_negative(database: dict, tmp_path: Path) -> None:
    service = Service()
    service.free = False
    blocked = run_once(**database, service=service)
    assert blocked["reason"] == "hosted_free_allowance_exhausted"
    assert not service.submissions and not database["results_path"].exists()
    service.free = True
    assert run_once(**database, service=service)["status"] == "approved"
    service.complete()
    assert run_once(**database, service=service)["status"] == "collected"
    assert run_once(**database, service=service)["status"] == "idle"
    first = json.loads(database["results_path"].read_text())
    assert first["run_id"] == "own" and first["flag"] is False
    add_trace(database, "new", "DEV")
    assert run_once(**database, service=service)["status"] == "approved"
    service.complete()
    run_once(**database, service=service)
    assert {json.loads(line)["run_id"] for line in database["results_path"].read_text().splitlines()} == {"own", "new"}
    assert service.approvals == 2
    assert all("NEVER_SEND" not in request["text"] for request in service.submissions)
    assert all(request["opaque"].startswith("run-") for request in service.submissions)


def test_lost_submission_and_approval_responses_do_not_duplicate_model_requests(database: dict) -> None:
    service = Service()
    service.lose_submit_response = True
    assert run_once(**database, service=service)["status"] == "error"
    assert len(service.submissions) == 1 and service.approvals == 0
    service.lose_approval_response = True
    assert run_once(**database, service=service)["status"] == "error"
    assert len(service.submissions) == 1 and service.approvals == 1
    assert run_once(**database, service=service)["status"] == "awaiting_result"
    service.complete()
    assert run_once(**database, service=service)["status"] == "collected"
    assert run_once(**database, service=service)["status"] == "idle"
    assert len(database["results_path"].read_text().splitlines()) == 1
    assert len(database["spend_path"].read_text().splitlines()) == 1
    assert service.approvals == 1


def test_changed_cross_key_admission_prevents_saved_plan_approval(database: dict) -> None:
    service = Service()
    service.lose_submit_response = True
    run_once(**database, service=service)
    with (database["repo_root"] / "research/answer-keys/splits/assignments.csv").open("a") as stream:
        stream.write("other-key,held-item,mimo-v2.6-rl-code/own,test\n")
    result = run_once(**database, service=service)
    assert result["reason"] == "active_reading_no_longer_admitted"
    assert service.approvals == 0


def test_non_actor_journal_is_quarantined_without_blocking_real_actor_arrivals(database: dict) -> None:
    service = Service()
    with sqlite3.connect(database["db_path"]) as con:
        con.execute("UPDATE agent_traces SET model_id='retained-event-journal/no-model' WHERE trace_id='own'")
        con.execute("UPDATE trace_steps SET role='system',content_text=?,flags_json=? WHERE trace_id='own'", (
            json.dumps({"payload": {"original_reward": 1, "repaired_reward": 0}}),
            json.dumps({"trace_kind": "seeker_event_journal", "is_model_message": False}),
        ))
    quarantined = run_once(**database, service=service)
    assert quarantined["status"] == "quarantined"
    assert quarantined["quarantined"] == {"actor_history_unavailable": 1}
    assert service.submissions == [] and not database["results_path"].exists()
    add_trace(database, "real-actor", "DEV")
    admitted = run_once(**database, service=service)
    assert admitted["status"] == "approved"
    assert admitted["quarantined"] == {"actor_history_unavailable": 1}
    assert "original_reward" not in service.submissions[0]["text"]
    service.complete()
    run_once(**database, service=service)
    assert json.loads(database["results_path"].read_text())["run_id"] == "real-actor"
