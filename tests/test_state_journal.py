from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq

from evallab.evidence.facts import load_state_journal, rebuild_from_raw
from evallab.harbor_state_journal import compose_project_name, monitor_command
from evallab.results import load_job


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _job_with_journal(root: Path) -> Path:
    job = root / "journal-job"
    trial = job / "sample-task__abc123"
    _write_json(job / "config.json", {"job_name": "journal-job"})
    _write_json(job / "lock.json", {"harbor": {"version": "0.21.0"}})
    _write_json(
        job / "result.json",
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "started_at": "2026-08-19T12:00:00Z",
            "finished_at": "2026-08-19T12:00:02Z",
            "n_total_trials": 1,
            "stats": {"n_completed_trials": 1, "n_errored_trials": 0},
        },
    )
    _write_json(trial / "config.json", {"agent": {"name": "oracle"}})
    _write_json(trial / "lock.json", {"schema_version": 2})
    _write_json(
        trial / "result.json",
        {
            "id": "00000000-0000-0000-0000-000000000002",
            "trial_name": trial.name,
            "task_name": "local-lab/sample-task",
            "task_checksum": "abc",
            "started_at": "2026-08-19T12:00:00Z",
            "finished_at": "2026-08-19T12:00:01Z",
            "agent_info": {"name": "oracle", "version": "1.0.0", "model_info": None},
            "agent_result": {
                "n_input_tokens": None,
                "n_cache_tokens": None,
                "n_output_tokens": None,
                "cost_usd": None,
            },
            "verifier_result": {"rewards": {"reward": 1.0}},
            "exception_info": None,
        },
    )
    journal = trial / "state-journal"
    _write_json(
        journal / "status.json",
        {
            "schema_version": 1,
            "status": "available",
            "reason": None,
            "started_at": "2026-08-19T12:00:00Z",
            "finished_at": "2026-08-19T12:00:02Z",
            "root": "/app",
            "target_pid": 123,
            "event_count": 2,
            "dropped_event_count": 0,
            "change_count": 1,
        },
    )
    _write_json(
        journal / "state-diff.json",
        {
            "schema_version": 1,
            "status": "available",
            "reason": None,
            "root": "/app",
            "before_captured_at": "2026-08-19T12:00:00Z",
            "after_captured_at": "2026-08-19T12:00:02Z",
            "event_count": 2,
            "change_count": 1,
            "dropped_event_count": 0,
            "changes": [
                {
                    "path": "output/answer.txt",
                    "change_type": "added",
                    "before": None,
                    "after": {
                        "path": "output/answer.txt",
                        "mode": "-rw-r--r--",
                        "size_bytes": 3,
                        "mtime_ns": 1_755_604_802_000_000_000,
                        "type": "file",
                        "sha256": "sha256:" + "a" * 64,
                        "hash_status": "complete",
                    },
                    "event_count": 2,
                    "first_event_at": "2026-08-19T12:00:00.1Z",
                    "last_event_at": "2026-08-19T12:00:00.2Z",
                }
            ],
        },
    )
    return job


def test_state_journal_projects_to_parquet(tmp_path: Path) -> None:
    job = load_job(_job_with_journal(tmp_path))
    derived = tmp_path / "derived"

    result = rebuild_from_raw([job], derived)

    trial_dir = derived / f"job_id={job.id}" / f"trial_id={job.trials[0].id}"
    trial_row = pq.read_table(trial_dir / "trial_facts.parquet").to_pylist()[0]
    changes = pq.read_table(trial_dir / "state_changes.parquet").to_pylist()
    assert result.fact_export.row_counts["state_changes"] == 1
    assert trial_row["state_journal_status"] == "available"
    assert trial_row["state_change_count"] == 1
    assert changes == [
        {
            "experiment_id": None,
            "job_id": job.id,
            "trial_id": job.trials[0].id,
            "path": "output/answer.txt",
            "change_type": "added",
            "before_sha256": None,
            "after_sha256": "sha256:" + "a" * 64,
            "before_size_bytes": None,
            "after_size_bytes": 3,
            "event_count": 2,
            "first_event_at": "2026-08-19T12:00:00.1Z",
            "last_event_at": "2026-08-19T12:00:00.2Z",
            "journal_status": "available",
        }
    ]


def test_absent_and_malformed_journals_are_accounted(tmp_path: Path) -> None:
    job = load_job(_job_with_journal(tmp_path))
    trial = job.trials[0]
    journal = trial.path / "state-journal"
    (journal / "status.json").unlink()
    assert load_state_journal(trial).status == "absent"

    (journal / "status.json").write_text("not-json", encoding="utf-8")
    malformed = load_state_journal(trial)
    assert malformed.status == "invalid"
    assert malformed.reason == "status_unreadable:JSONDecodeError"

    _write_json(journal / "status.json", [])
    structurally_malformed = load_state_journal(trial)
    assert structurally_malformed.status == "invalid"
    assert structurally_malformed.reason == "status_invalid"


def test_structurally_malformed_diff_degrades_without_stopping_rebuild(
    tmp_path: Path,
) -> None:
    job = load_job(_job_with_journal(tmp_path))
    trial = job.trials[0]
    _write_json(trial.path / "state-journal/state-diff.json", [])

    journal = load_state_journal(trial)
    derived = tmp_path / "derived"
    rebuild_from_raw([job], derived)

    trial_dir = derived / f"job_id={job.id}" / f"trial_id={trial.id}"
    trial_row = pq.read_table(trial_dir / "trial_facts.parquet").to_pylist()[0]
    assert journal.status == "invalid"
    assert journal.reason == "state_diff_invalid"
    assert journal.changes == ()
    assert trial_row["state_journal_status"] == "invalid"
    assert trial_row["state_journal_reason"] == "state_diff_invalid"
    assert trial_row["state_change_count"] == 0


def test_malformed_state_change_fails_closed_without_projection(tmp_path: Path) -> None:
    job = load_job(_job_with_journal(tmp_path))
    trial = job.trials[0]
    _write_json(
        trial.path / "state-journal/state-diff.json",
        {
            "schema_version": 999,
            "changes": [
                {
                    "path": "../outside",
                    "change_type": "made",
                    "before": {},
                    "after": {},
                    "event_count": "bogus",
                }
            ],
        },
    )

    journal = load_state_journal(trial)
    result = rebuild_from_raw([job], tmp_path / "derived")

    assert journal.status == "invalid"
    assert journal.reason == "state_diff_invalid"
    assert journal.changes == ()
    assert result.fact_export.row_counts["state_changes"] == 0



def test_producer_valid_unhashed_snapshot_kinds_are_projected(tmp_path: Path) -> None:
    job = load_job(_job_with_journal(tmp_path))
    trial = job.trials[0]
    diff_path = trial.path / "state-journal/state-diff.json"
    payload = json.loads(diff_path.read_text(encoding="utf-8"))
    change = payload["changes"][0]
    snapshots = [
        {
            "path": "output",
            "mode": "drwxr-xr-x",
            "size_bytes": 64,
            "mtime_ns": 1_755_604_802_000_000_000,
            "type": "directory",
        },
        {
            "path": "output/latest",
            "mode": "lrwxr-xr-x",
            "size_bytes": 10,
            "mtime_ns": 1_755_604_802_000_000_000,
            "type": "symlink",
            "target": "answer.txt",
        },
        {
            "path": "output/large.bin",
            "mode": "-rw-r--r--",
            "size_bytes": 1_048_577,
            "mtime_ns": 1_755_604_802_000_000_000,
            "type": "file",
            "sha256": None,
            "hash_status": "size_limit",
        },
    ]

    for snapshot in snapshots:
        change["path"] = snapshot["path"]
        change["after"] = snapshot
        _write_json(diff_path, payload)

        journal = load_state_journal(trial)

        assert journal.status == "available"
        assert journal.reason is None
        assert journal.changes[0]["after"] == {
            key: value for key, value in snapshot.items() if value is not None
        }


def test_snapshot_path_mismatch_fails_closed(tmp_path: Path) -> None:
    job = load_job(_job_with_journal(tmp_path))
    trial = job.trials[0]
    diff_path = trial.path / "state-journal/state-diff.json"
    payload = json.loads(diff_path.read_text(encoding="utf-8"))
    payload["changes"][0]["after"]["path"] = "output/other.txt"
    _write_json(diff_path, payload)

    journal = load_state_journal(trial)

    assert journal.status == "invalid"
    assert journal.reason == "state_diff_invalid"
    assert journal.changes == ()


def test_missing_snapshot_side_fails_closed(tmp_path: Path) -> None:
    job = load_job(_job_with_journal(tmp_path))
    trial = job.trials[0]
    diff_path = trial.path / "state-journal/state-diff.json"
    payload = json.loads(diff_path.read_text(encoding="utf-8"))
    payload["changes"][0].pop("before")
    _write_json(diff_path, payload)

    journal = load_state_journal(trial)

    assert journal.status == "invalid"
    assert journal.reason == "state_diff_invalid"
    assert journal.changes == ()


def test_duplicate_state_change_path_fails_closed(tmp_path: Path) -> None:
    job = load_job(_job_with_journal(tmp_path))
    trial = job.trials[0]
    diff_path = trial.path / "state-journal/state-diff.json"
    payload = json.loads(diff_path.read_text(encoding="utf-8"))
    payload["changes"].append(dict(payload["changes"][0]))
    _write_json(diff_path, payload)

    journal = load_state_journal(trial)

    assert journal.status == "invalid"
    assert journal.reason == "state_diff_invalid"
    assert journal.changes == ()


def test_timezone_naive_state_change_timestamp_fails_closed(tmp_path: Path) -> None:
    job = load_job(_job_with_journal(tmp_path))
    trial = job.trials[0]
    diff_path = trial.path / "state-journal/state-diff.json"
    payload = json.loads(diff_path.read_text(encoding="utf-8"))
    payload["changes"][0]["first_event_at"] = "2026-08-19T12:00:00"
    _write_json(diff_path, payload)

    journal = load_state_journal(trial)

    assert journal.status == "invalid"
    assert journal.reason == "state_diff_invalid"
    assert journal.changes == ()


def test_out_of_range_state_change_integer_fails_closed(tmp_path: Path) -> None:
    job = load_job(_job_with_journal(tmp_path))
    trial = job.trials[0]
    diff_path = trial.path / "state-journal/state-diff.json"
    payload = json.loads(diff_path.read_text(encoding="utf-8"))
    payload["changes"][0]["event_count"] = 1 << 100
    _write_json(diff_path, payload)

    journal = load_state_journal(trial)

    assert journal.status == "invalid"
    assert journal.reason == "state_diff_invalid"
    assert journal.changes == ()


def test_out_of_range_snapshot_integer_fails_closed(tmp_path: Path) -> None:
    job = load_job(_job_with_journal(tmp_path))
    trial = job.trials[0]
    diff_path = trial.path / "state-journal/state-diff.json"
    payload = json.loads(diff_path.read_text(encoding="utf-8"))
    payload["changes"][0]["after"]["size_bytes"] = 1 << 100
    _write_json(diff_path, payload)

    journal = load_state_journal(trial)

    assert journal.status == "invalid"
    assert journal.reason == "state_diff_invalid"
    assert journal.changes == ()


def test_unknown_state_journal_status_schema_fails_closed(tmp_path: Path) -> None:
    job = load_job(_job_with_journal(tmp_path))
    trial = job.trials[0]
    status_path = trial.path / "state-journal/status.json"
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    payload["schema_version"] = 999
    _write_json(status_path, payload)

    journal = load_state_journal(trial)

    assert journal.status == "invalid"
    assert journal.reason == "status_schema_invalid"
    assert journal.changes == ()


def test_boolean_state_diff_schema_version_fails_closed(tmp_path: Path) -> None:
    job = load_job(_job_with_journal(tmp_path))
    trial = job.trials[0]
    diff_path = trial.path / "state-journal/state-diff.json"
    payload = json.loads(diff_path.read_text(encoding="utf-8"))
    payload["schema_version"] = True
    _write_json(diff_path, payload)

    journal = load_state_journal(trial)

    assert journal.status == "invalid"
    assert journal.reason == "state_diff_invalid"
    assert journal.changes == ()


def test_noncanonical_state_change_path_fails_closed(tmp_path: Path) -> None:
    job = load_job(_job_with_journal(tmp_path))
    trial = job.trials[0]
    diff_path = trial.path / "state-journal/state-diff.json"
    payload = json.loads(diff_path.read_text(encoding="utf-8"))
    alias = dict(payload["changes"][0])
    alias["path"] = "output//answer.txt"
    alias["after"] = dict(alias["after"], path=alias["path"])
    payload["changes"].append(alias)
    _write_json(diff_path, payload)

    journal = load_state_journal(trial)

    assert journal.status == "invalid"
    assert journal.reason == "state_diff_invalid"
    assert journal.changes == ()


def test_coerced_snapshot_integer_fails_closed(tmp_path: Path) -> None:
    job = load_job(_job_with_journal(tmp_path))
    trial = job.trials[0]
    diff_path = trial.path / "state-journal/state-diff.json"
    payload = json.loads(diff_path.read_text(encoding="utf-8"))
    payload["changes"][0]["after"]["size_bytes"] = "3"
    _write_json(diff_path, payload)

    journal = load_state_journal(trial)

    assert journal.status == "invalid"
    assert journal.reason == "state_diff_invalid"
    assert journal.changes == ()

def test_status_json_preserves_unavailable_final_status_over_stale_diff(
    tmp_path: Path,
) -> None:
    job = load_job(_job_with_journal(tmp_path))
    trial = job.trials[0]
    _write_json(
        trial.path / "state-journal/status.json",
        {
            "schema_version": 1,
            "status": "unavailable",
            "reason": "observer_stopped_before_final_snapshot",
        },
    )

    journal = load_state_journal(trial)
    derived = tmp_path / "derived"
    rebuild_from_raw([job], derived)

    trial_dir = derived / f"job_id={job.id}" / f"trial_id={trial.id}"
    trial_row = pq.read_table(trial_dir / "trial_facts.parquet").to_pylist()[0]
    changes = pq.read_table(trial_dir / "state_changes.parquet").to_pylist()
    assert journal.status == "unavailable"
    assert journal.reason == "observer_stopped_before_final_snapshot"
    assert trial_row["state_journal_status"] == "unavailable"
    assert changes[0]["journal_status"] == "unavailable"


def test_sidecar_command_has_no_target_workspace_mount(tmp_path: Path) -> None:
    command = monitor_command(
        image="evallab-state-journal:test",
        monitor_name="journal-test",
        target_pid=123,
        output_dir=tmp_path,
        watch_root="/app",
        max_hash_bytes=1024,
    )

    assert "--pid=host" in command
    assert "--cap-add=SYS_PTRACE" in command
    assert f"{tmp_path.resolve()}:/journal" in command
    assert not any(value.endswith(":/app") for value in command)
    assert compose_project_name("Sample.Task__abc") == "sample-task__abc__env"
