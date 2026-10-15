from __future__ import annotations

import argparse
import csv
import json
import runpy
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evallab.task_variants import task_directory_digest

DRIVER = (
    Path(__file__).resolve().parents[1]
    / "research/experiments/mimo-clean-v6/resume_controls.py"
)


@pytest.fixture
def driver() -> dict[str, Any]:
    return runpy.run_path(str(DRIVER))


def cell() -> dict[str, Any]:
    return {
        "rewards": [0.0, 0.0, 0.0],
        "test_execution": [True, True, True],
        "run_errors": [None, None, None],
        "exception_info": None,
        "returncode": 0,
    }


def test_scored_negative_control_is_reused(driver: dict[str, Any]) -> None:
    assert driver["scored_cell"](cell())


@pytest.mark.parametrize(
    "change",
    [
        {"rewards": [0.0]},
        {"test_execution": [True, False, True]},
        {"run_errors": [None, "error", None]},
        {"exception_info": {"exception_type": "SetupError"}},
        {"returncode": 1},
    ],
)
def test_incomplete_controls_are_not_reused(
    driver: dict[str, Any], change: dict[str, Any]
) -> None:
    assert not driver["scored_cell"](cell() | change)


def test_csv_reuse_keeps_digest_and_repeat_count(
    driver: dict[str, Any], tmp_path: Path
) -> None:
    row = {"task_id": "format-code-task-000048", "final_digest": "sha256:old"}
    for agent, rewards in (("oracle", [1.0] * 3), ("nop", [0.0])):
        row[f"{agent}_repeat_rewards"] = json.dumps(rewards)
        row[f"{agent}_repeat_execution"] = json.dumps([True] * len(rewards))
        row[f"{agent}_repeat_errors"] = json.dumps([None] * len(rewards))
        row[f"{agent}_exception"] = "null"
        row[f"{agent}_process_rc"] = "null"
    receipts = tmp_path / "results.csv"
    with receipts.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    done = driver["reusable_cells"](receipts, tmp_path / "absent.jsonl")
    assert done == {(row["task_id"], "sha256:old", "oracle")}
    assert (row["task_id"], "sha256:new", "oracle") not in done


def test_fifo_acquisition_is_bounded_and_timeout_stops_without_docker(
    driver: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "task.toml").write_text(
        '[environment]\ndocker_image="example.invalid/unused"\n', encoding="utf-8"
    )
    row = {
        "task_id": "format-code-task-000048", "final_digest": task_directory_digest(package),
        "status": "built", "package_path": str(package), "reference_fix": "none",
    }
    manifest = tmp_path / "manifest.csv"
    with manifest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    pending = tmp_path / "pending.json"
    pending.write_text(json.dumps({"tasks": [row | {"runnable": True}]}), encoding="utf-8")
    waits = []

    def timeout_slot(**kwargs: Any) -> None:
        waits.append(kwargs["timeout"])
        raise driver["DockerLockTimeout"]("test FIFO deadline")

    monkeypatch.setitem(driver["execute"].__globals__, "docker_slot", timeout_slot)
    args = argparse.Namespace(
        manifest=manifest, pending=pending, receipts=tmp_path / "absent.csv",
        journal=tmp_path / "journal.jsonl", jobs_dir=tmp_path / "jobs",
        package_root=tmp_path,
        seconds=7200, lock_wait_seconds=3,
    )
    driver["execute"](args)
    assert waits == [3]
    record = json.loads(args.journal.read_text(encoding="utf-8"))
    assert record["kind"] == "fifo-timeout"
    assert record["agent"] == "nop"


def test_changed_materialized_package_is_rejected_before_docker(
    driver: dict[str, Any], tmp_path: Path
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    config = package / "task.toml"
    config.write_text('[environment]\ndocker_image="example.invalid/unused"\n', encoding="utf-8")
    digest = task_directory_digest(package)
    row = {
        "task_id": "format-code-task-000048", "final_digest": digest,
        "status": "built", "package_path": str(package), "reference_fix": "none",
    }
    manifest = tmp_path / "manifest.csv"
    with manifest.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    pending = tmp_path / "pending.json"
    pending.write_text(json.dumps({"tasks": [row | {"runnable": True}]}), encoding="utf-8")
    config.write_text('[environment]\ndocker_image="example.invalid/changed"\n', encoding="utf-8")
    args = argparse.Namespace(
        manifest=manifest, pending=pending, receipts=tmp_path / "absent.csv",
        journal=tmp_path / "journal.jsonl", jobs_dir=tmp_path / "jobs",
        package_root=tmp_path, seconds=7200, lock_wait_seconds=3,
    )
    with pytest.raises(ValueError, match="materialized package digest changed"):
        driver["execute"](args)
    assert not args.journal.exists()


@pytest.mark.parametrize("inspection_finished_at", [9.0, 10.0, 11.0])
def test_image_inspection_cannot_admit_a_trial_after_deadline(
    driver: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inspection_finished_at: float,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "task.toml").write_text(
        '[environment]\ndocker_image="example.invalid/unused"\n', encoding="utf-8"
    )
    row = {
        "task_id": "format-code-task-000048", "final_digest": task_directory_digest(package),
        "status": "built", "package_path": str(package), "reference_fix": "none",
    }
    manifest = tmp_path / "manifest.csv"
    with manifest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    pending = tmp_path / "pending.json"
    pending.write_text(json.dumps({"tasks": [row | {"runnable": True}]}), encoding="utf-8")
    clock = [0.0]
    launched_at: list[float] = []

    def inspect_image(_image: str) -> bool:
        clock[0] = inspection_finished_at
        return True

    class RecordingExecutor:
        @staticmethod
        def from_repo(_root: Path) -> RecordingExecutor:
            return RecordingExecutor()

        def execute_direct(self, _request: Any, *, ingest: bool) -> None:
            assert ingest is False
            launched_at.append(clock[0])

    namespace = driver["execute"].__globals__
    monkeypatch.setitem(namespace, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setitem(namespace, "docker_slot", lambda **_kwargs: nullcontext())
    monkeypatch.setitem(namespace, "image_cached", inspect_image)
    monkeypatch.setitem(namespace, "Executor", RecordingExecutor)
    args = argparse.Namespace(
        manifest=manifest, pending=pending, receipts=tmp_path / "absent.csv",
        journal=tmp_path / "journal.jsonl", jobs_dir=tmp_path / "jobs",
        package_root=tmp_path, seconds=10, lock_wait_seconds=3,
    )
    driver["execute"](args)
    assert launched_at == ([inspection_finished_at] if inspection_finished_at < 10 else [])
    assert args.journal.exists() is bool(launched_at)
