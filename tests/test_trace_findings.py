"""``trace_findings`` storage: admission, actor-transcript rule, and key semantics."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from evallab.production.cloud_trace_db import CloudTraceDatabase
from evallab.production.corpus_queue import CorpusRegistry
from evallab.production.trace_findings import import_trace_findings, upsert_trace_findings


class SyntheticRegistry(CorpusRegistry):
    """Real admission policy with synthetic assignment authority and a TEST lock on ``locked``."""

    def __init__(self) -> None:
        authority = b"key_id,item_id,split\nsynthetic-authority,source,dev\n"
        self.split_assignments_digest = "sha256:" + hashlib.sha256(authority).hexdigest()
        self._assignment_keys = frozenset({"synthetic-authority"})
        self._assignments = {("synthetic-authority", "source"): "dev"}
        self._assignment_names = {}
        self._assigned_test_ids = set()
        self._assigned_test_names = {"locked"}
        self._sealed_names = set()
        self._sealed_digests = set()
        self._sealed_ids = set()


def finding(run_id: str = "alpha/agentic", **changes: Any) -> dict[str, Any]:
    return {
        "run_id": run_id, "detector": "tripwire", "detector_version": "v1", "reader_model": None,
        "verdict": "alarm", "score": 0.9, "kind": "cheat", "evidence_steps": [2],
        "summary": "reads the hidden tests", "input_tokens": 10, "output_tokens": 2, "cost_usd": 0.001,
        "latency_ms": 40, "produced_at": "2026-10-28T10:00:00Z", **changes,
    }


def add_run(db: CloudTraceDatabase, task_id: str, name: str, steps: list[dict[str, Any]]) -> str:
    run_id = f"{task_id}/{name}"
    db.upsert_trace({
        "trace_id": run_id, "run_id": "batch", "task_id": task_id, "model_id": "secret-model",
        "reward": 1.0,
    }, steps=steps)
    return run_id


AGENT_STEPS = [
    {"step_index": 0, "role": "user", "content_text": "fix the parser"},
    {"step_index": 1, "role": "assistant", "content_text": "looking", "tool_name": "bash",
     "command_or_action": "ls"},
    {"step_index": 2, "role": "tool", "content_text": "tests/ src/"},
    {"step_index": 4, "role": "assistant", "content_text": "peek", "tool_name": "bash",
     "command_or_action": "git log -p --all -S solution"},
]
NO_ACTOR_STEPS = [
    {"step_index": 0, "role": "user", "content_text": "fix the parser"},
    {"step_index": 1, "role": "assistant", "content_text": "   "},
    {"step_index": 2, "role": "tool", "content_text": "replayed receipt"},
]


@pytest.fixture
def store(tmp_path: Path) -> Iterator[CloudTraceDatabase]:
    with CloudTraceDatabase(tmp_path / "cloud.sqlite", sqlite_fts=False) as database:
        for task_id, split in (("alpha", "dev"), ("replay", "dev"), ("locked", "dev"), ("held-out", "test")):
            database.upsert_task({
                "task_id": task_id, "dataset_id": "secret-dataset", "split": split,
                "instruction_text": "repair the parser",
            })
        add_run(database, "alpha", "agentic", AGENT_STEPS)
        add_run(database, "replay", "journal", NO_ACTOR_STEPS)
        add_run(database, "locked", "one", AGENT_STEPS)
        add_run(database, "held-out", "one", AGENT_STEPS)
        yield database


def rows(store: CloudTraceDatabase) -> list[dict[str, Any]]:
    return store._rows("SELECT * FROM trace_findings ORDER BY run_id, detector, detector_version, reader_model")


def store_findings(store: CloudTraceDatabase, *records: Any) -> dict[str, Any]:
    return upsert_trace_findings(store, records, registry=SyntheticRegistry())


def test_only_admitted_dev_runs_are_stored_and_every_other_run_is_refused_alike(
    store: CloudTraceDatabase,
) -> None:
    receipt = store_findings(
        store, finding(), finding("held-out/one"), finding("locked/one"), finding("missing/run"),
    )
    assert (receipt["stored"], receipt["refused"]) == (1, 3)
    assert receipt["refused_by_reason"] == {
        "run is not an admitted DEV run": 2, "run_id names no stored trace": 1,
    }
    assert [row["run_id"] for row in rows(store)] == ["alpha/agentic"]


def test_reimport_is_idempotent_and_the_newest_record_of_a_key_wins_in_any_order(
    store: CloudTraceDatabase, tmp_path: Path,
) -> None:
    old = finding(summary="old", produced_at="2026-10-28T09:00:00Z")
    new = finding(summary="new", produced_at="2026-10-28T11:00:00+00:00")
    path = tmp_path / "findings.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in (new, old)) + "\nnot json\n")
    first = import_trace_findings(store, path, registry=SyntheticRegistry())
    assert (first["stored"], first["superseded_in_input"], first["invalid_json_lines"]) == (1, 1, 1)
    snapshot = rows(store)
    assert snapshot[0]["summary"] == "new"
    again = import_trace_findings(store, path, registry=SyntheticRegistry())
    assert again["sha256"] == first["sha256"] and rows(store) == snapshot
    late_old = store_findings(store, old)
    assert late_old["kept_newer_stored"] == 1 and rows(store) == snapshot
    newest = store_findings(store, finding(summary="newest", produced_at="2026-10-29T00:00:00Z"))
    assert newest["stored"] == 1 and rows(store)[0]["summary"] == "newest"


def test_a_key_is_run_detector_version_and_reader(store: CloudTraceDatabase) -> None:
    store_findings(
        store,
        finding(), finding(detector_version="v2"), finding(reader_model="anthropic/claude-haiku-5-5"),
        finding(detector="scout", verdict="clear", kind="other", summary=""),
    )
    assert [
        (row["detector"], row["detector_version"], row["reader_model"]) for row in rows(store)
    ] == [
        ("scout", "v1", ""), ("tripwire", "v1", ""), ("tripwire", "v1", "anthropic/claude-haiku-5-5"),
        ("tripwire", "v2", ""),
    ]


def test_a_run_with_no_actor_transcript_is_unknown_never_clean(store: CloudTraceDatabase) -> None:
    receipt = store_findings(
        store,
        finding("replay/journal", verdict="clear", evidence_steps=[]),
        finding("replay/journal", detector="cascade", verdict="unknown", kind="other", evidence_steps=[]),
        finding("alpha/agentic", detector="cascade", verdict="clear", kind="other", actor_inputs_available=False),
    )
    assert receipt["refused_by_reason"] == {"a run with no actor transcript cannot be recorded clear": 1}
    assert receipt["actor_inputs_overrides"] == 1  # a record's own claim never decides
    stored = {(row["run_id"], row["detector"]): row for row in rows(store)}
    assert stored[("replay/journal", "cascade")]["actor_inputs_available"] is False or (
        stored[("replay/journal", "cascade")]["actor_inputs_available"] == 0
    )
    assert bool(stored[("alpha/agentic", "cascade")]["actor_inputs_available"]) is True


@pytest.mark.parametrize(("changes", "reason"), [
    ({"evidence_steps": [3]}, "evidence_steps must name stored steps of the run"),
    ({"evidence_steps": [2, 2]}, "evidence_steps must not repeat a step index"),
    ({"evidence_steps": list(range(21))}, "at most 20"),
    ({"verdict": "maybe"}, "verdict must be one of"),
    ({"kind": "vibes"}, "kind must be one of"),
    ({"summary": "word " * 41}, "at most 40 words"),
    ({"produced_at": "2026-10-28T10:00:00"}, "must carry a timezone"),
    ({"produced_at": "yesterday"}, "UTC ISO timestamp"),
    ({"score": float("nan")}, "score must be a finite number"),
    ({"cost_usd": -1}, "cost_usd must be at least"),
    ({"latency_ms": 1.5}, "latency_ms must be an integer"),
    ({"detector": "Not A Detector"}, "detector must be a short identifier"),
])
def test_invalid_records_are_refused_whole(
    store: CloudTraceDatabase, changes: dict[str, Any], reason: str,
) -> None:
    receipt = store_findings(store, finding(**changes))
    assert receipt["stored"] == 0 and rows(store) == []
    assert reason in next(iter(receipt["refused_by_reason"]))


def test_a_missing_required_field_names_the_field(store: CloudTraceDatabase) -> None:
    record = finding()
    del record["verdict"]
    assert "missing fields: verdict" in store_findings(store, record)["refused_by_reason"]


def test_finding_rows_leave_with_their_run(store: CloudTraceDatabase) -> None:
    store_findings(store, finding())
    store._execute("DELETE FROM agent_traces WHERE trace_id = 'alpha/agentic'")
    assert rows(store) == []



def test_a_newer_refused_record_does_not_hide_an_older_valid_one(store: CloudTraceDatabase) -> None:
    receipt = store_findings(
        store,
        finding(summary="valid", produced_at="2026-10-28T09:00:00Z"),
        finding(summary="newer but cites a step the run does not have", evidence_steps=[3],
                produced_at="2026-10-28T10:00:00Z"),
    )
    assert (receipt["stored"], receipt["refused"], receipt["superseded_in_input"]) == (1, 1, 0)
    assert rows(store)[0]["summary"] == "valid"


def test_hostile_values_refuse_their_own_record_and_never_abort_the_batch(store: CloudTraceDatabase) -> None:
    receipt = store_findings(
        store,
        finding(produced_at="0001-01-01T00:00:00+01:00"),
        finding(detector="scout", produced_at="9999-12-31T23:59:59-01:00"),
        finding(detector="cascade", input_tokens=10**20),
        finding(detector="docent", summary="nul\x00byte"),
        finding(detector="metr", summary="x" * 601),
        finding("alpha/agentic\x00"),
        finding(detector="run_health.loop", kind="loop", summary="valid neighbour"),
    )
    assert (receipt["stored"], receipt["refused"]) == (1, 6)
    assert [row["detector"] for row in rows(store)] == ["run_health.loop"]


def test_an_annotation_only_agent_step_is_not_an_actor_transcript(store: CloudTraceDatabase) -> None:
    add_run(store, "replay", "annotated", [
        {"step_index": 0, "role": "assistant", "content_text": "[CheatBench annotation - not part of the original trace]"},
        {"step_index": 1, "role": "tool", "content_text": "output"},
    ])
    receipt = store_findings(store, finding("replay/annotated", verdict="clear", evidence_steps=[]))
    assert receipt["refused_by_reason"] == {"a run with no actor transcript cannot be recorded clear": 1}
