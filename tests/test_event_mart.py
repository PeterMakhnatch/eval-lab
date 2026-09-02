from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pyarrow.parquet as pq

from evallab.evidence.facts import rebuild_from_raw
from evallab.results import load_job
from evallab.storage.parquet_compaction import deduplicate_and_sort


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_state_journal(
    trial_path: Path,
    *,
    path: str,
    content: bytes,
    event_timestamp: str | None,
) -> None:
    before_captured_at = "2026-08-15T00:00:00Z"
    after_captured_at = "2026-08-15T00:00:02Z"
    event_count = int(event_timestamp is not None)
    snapshot = {
        "path": path,
        "mode": "-rw-r--r--",
        "size_bytes": len(content),
        "mtime_ns": 1_755_216_002_000_000_000,
        "type": "file",
        "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
        "hash_status": "complete",
    }
    _write_json(
        trial_path / "state-journal/status.json",
        {
            "schema_version": 1,
            "status": "available",
            "reason": None,
            "started_at": before_captured_at,
            "finished_at": after_captured_at,
            "root": "/app",
            "target_pid": 123,
            "event_count": event_count,
            "dropped_event_count": 0,
            "change_count": 1,
        },
    )
    _write_json(
        trial_path / "state-journal/state-diff.json",
        {
            "schema_version": 1,
            "status": "available",
            "reason": None,
            "root": "/app",
            "before_captured_at": before_captured_at,
            "after_captured_at": after_captured_at,
            "event_count": event_count,
            "change_count": 1,
            "dropped_event_count": 0,
            "changes": [
                {
                    "path": path,
                    "change_type": "added",
                    "before": None,
                    "after": snapshot,
                    "event_count": event_count,
                    "first_event_at": event_timestamp,
                    "last_event_at": event_timestamp,
                }
            ],
        },
    )


def test_event_mart_pairs_actions_observations_and_temporal_effects(tmp_path: Path) -> None:
    source = Path(__file__).parent / "fixtures/explorer/jobs/job-pass"
    job_path = tmp_path / "runs/job-pass"
    shutil.copytree(source, job_path)
    trial_path = job_path / "t1"
    _write_state_journal(
        trial_path,
        path="a.py",
        content=b"updated",
        event_timestamp="2026-08-15T00:00:01.500Z",
    )
    job = load_job(job_path)
    derived = tmp_path / "derived"

    rebuilt = rebuild_from_raw([job], derived)

    partition = derived / f"job_id={job.id}" / f"trial_id={job.trials[0].id}"
    actions = pq.read_table(partition / "agent_actions.parquet").to_pylist()
    effects = pq.read_table(partition / "action_effects.parquet").to_pylist()
    events = pq.read_table(partition / "trajectory_events.parquet").to_pylist()
    assert rebuilt.event_mart_export.row_counts == {
        "trajectory_events": 5,
        "agent_actions": 2,
        "llm_calls": 0,
        "trajectory_phases": 1,
        "action_effects": 1,
    }
    assert [(row["function_name"], row["action_family"], row["outcome"]) for row in actions] == [
        ("write_file", "edit", "unknown"),
        ("run_pytest", "test", "success"),
    ]
    assert effects[0]["action_id"] == actions[0]["action_id"]
    assert effects[0]["link_status"] == "temporally_preceded"
    assert actions[0]["effect_count"] == 1
    assert any(row["event_type"] == "tool_result" for row in events)


def test_event_mart_llm_calls_keep_matching_atif_source_digest(tmp_path: Path) -> None:
    source = Path(__file__).parent / "fixtures/explorer/jobs/job-pass"
    job_path = tmp_path / "runs/job-pass"
    shutil.copytree(source, job_path)
    trajectory_path = job_path / "t1/agent/trajectory.json"
    trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
    for step in trajectory["steps"]:
        step["llm_call_count"] = 1
    trajectory_path.write_text(json.dumps(trajectory), encoding="utf-8")
    expected_digest = f"sha256:{hashlib.sha256(trajectory_path.read_bytes()).hexdigest()}"
    job = load_job(job_path)
    derived = tmp_path / "derived"

    rebuild_from_raw([job], derived)

    partition = derived / f"job_id={job.id}" / f"trial_id={job.trials[0].id}"
    llm_calls = pq.read_table(partition / "llm_calls.parquet").to_pylist()
    assert len(llm_calls) == 2
    assert {
        (row["source_path"], row["source_sha256"])
        for row in llm_calls
    } == {("agent/trajectory.json", expected_digest)}


def test_event_mart_never_invents_an_action_effect_without_timestamps(tmp_path: Path) -> None:
    source = Path(__file__).parent / "fixtures/explorer/jobs/job-pass"
    job_path = tmp_path / "runs/job-pass"
    shutil.copytree(source, job_path)
    trial_path = job_path / "t1"
    _write_state_journal(
        trial_path,
        path="unknown.txt",
        content=b"x",
        event_timestamp=None,
    )
    job = load_job(job_path)
    derived = tmp_path / "derived"

    rebuild_from_raw([job], derived)

    partition = derived / f"job_id={job.id}" / f"trial_id={job.trials[0].id}"
    effect = pq.read_table(partition / "action_effects.parquet").to_pylist()[0]
    assert effect["action_id"] is None
    assert effect["link_status"] == "unattributed"



def test_event_flow_keeps_repeated_call_ids_distinct_through_compaction(
    tmp_path: Path,
) -> None:
    source = Path(__file__).parent / "fixtures/explorer/jobs/job-pass"
    job_path = tmp_path / "runs/job-pass"
    shutil.copytree(source, job_path)
    trial_path = job_path / "t1"
    trajectory_path = trial_path / "agent/trajectory.json"
    trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
    steps = trajectory["steps"]
    for step in steps:
        step["timestamp"] = "2026-08-15T00:00:01Z"
        step["tool_calls"][0]["tool_call_id"] = "repeated-call"
    steps[0]["tool_calls"][0]["function_name"] = "manage_task"
    steps[0]["observation"] = {
        "results": [
            {
                "source_call_id": "repeated-call",
                "content": "first result",
                "extra": {"exit_code": 0},
            }
        ]
    }
    steps[1]["observation"]["results"][0]["source_call_id"] = "repeated-call"
    trajectory_path.write_text(json.dumps(trajectory), encoding="utf-8")
    _write_state_journal(
        trial_path,
        path="same-time.txt",
        content=b"x",
        event_timestamp="2026-08-15T00:00:01Z",
    )
    job = load_job(job_path)
    derived = tmp_path / "derived"

    rebuild_from_raw([job], derived)

    partition = derived / f"job_id={job.id}" / f"trial_id={job.trials[0].id}"
    actions_table = pq.read_table(partition / "agent_actions.parquet")
    actions = actions_table.to_pylist()
    compacted_actions = deduplicate_and_sort(actions_table, "agent_actions").to_pylist()
    events = pq.read_table(partition / "trajectory_events.parquet").to_pylist()
    effects = pq.read_table(partition / "action_effects.parquet").to_pylist()
    phases = pq.read_table(partition / "trajectory_phases.parquet").to_pylist()

    assert len(actions) == len(compacted_actions) == 2
    assert actions[0]["action_id"] != actions[1]["action_id"]
    assert [row["observation_size_bytes"] for row in actions] == [12, 8]
    assert [row["event_type"] for row in events] == [
        "agent_step",
        "tool_call",
        "tool_result",
        "agent_step",
        "tool_call",
        "tool_result",
    ]
    for call, result in ((events[1], events[2]), (events[4], events[5])):
        assert result["parent_event_id"] == call["event_id"]
    assert effects[0]["action_id"] == actions[1]["action_id"]
    assert actions[0]["action_family"] == "other"
    assert phases[0]["source_path"] == "agent/trajectory.json"