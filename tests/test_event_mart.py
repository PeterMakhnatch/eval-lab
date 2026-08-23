from __future__ import annotations

import json
import shutil
from pathlib import Path

import pyarrow.parquet as pq

from evallab.facts import rebuild_from_raw
from evallab.parquet_compaction import deduplicate_and_sort
from evallab.results import load_job


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_event_mart_pairs_actions_observations_and_temporal_effects(tmp_path: Path) -> None:
    source = Path(__file__).parent / "fixtures/explorer/jobs/job-pass"
    job_path = tmp_path / "runs/job-pass"
    shutil.copytree(source, job_path)
    trial_path = job_path / "t1"
    _write_json(
        trial_path / "state-journal/status.json",
        {
            "schema_version": 1,
            "status": "available",
            "reason": None,
            "observer": {
                "mode": "external-sidecar",
                "target_mutated": False,
                "model_visible_output": False,
            },
        },
    )
    _write_json(
        trial_path / "state-journal/state-diff.json",
        {
            "schema_version": 1,
            "status": "available",
            "reason": None,
            "changes": [
                {
                    "path": "a.py",
                    "change_type": "added",
                    "before": None,
                    "after": {"sha256": "sha256:abc", "size_bytes": 7},
                    "event_count": 1,
                    "first_event_at": "2026-08-15T00:00:01.500Z",
                    "last_event_at": "2026-08-15T00:00:01.500Z",
                }
            ],
        },
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


def test_event_mart_never_invents_an_action_effect_without_timestamps(tmp_path: Path) -> None:
    source = Path(__file__).parent / "fixtures/explorer/jobs/job-pass"
    job_path = tmp_path / "runs/job-pass"
    shutil.copytree(source, job_path)
    trial_path = job_path / "t1"
    _write_json(
        trial_path / "state-journal/status.json",
        {"schema_version": 1, "status": "available", "reason": None},
    )
    _write_json(
        trial_path / "state-journal/state-diff.json",
        {
            "schema_version": 1,
            "status": "available",
            "reason": None,
            "changes": [
                {
                    "path": "unknown.txt",
                    "change_type": "added",
                    "before": None,
                    "after": {"sha256": "sha256:def", "size_bytes": 1},
                    "event_count": 0,
                    "first_event_at": None,
                    "last_event_at": None,
                }
            ],
        },
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
    _write_json(
        trial_path / "state-journal/status.json",
        {"schema_version": 1, "status": "available", "reason": None},
    )
    _write_json(
        trial_path / "state-journal/state-diff.json",
        {
            "schema_version": 1,
            "status": "available",
            "reason": None,
            "changes": [
                {
                    "path": "same-time.txt",
                    "change_type": "added",
                    "before": None,
                    "after": {"sha256": "sha256:def", "size_bytes": 1},
                    "event_count": 1,
                    "first_event_at": "2026-08-15T00:00:01Z",
                    "last_event_at": "2026-08-15T00:00:01Z",
                }
            ],
        },
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