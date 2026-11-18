"""Tests for evallab.digest_drift (read-only lock-vs-disk digest check).

Fixtures are synthetic Harbor-shaped job/task directories under tmp_path.
The digest oracle is independent of the module under test: expected digests
are recomputed here from the documented Harbor framing, and one test pins
the module's digest to ``evallab.registry.harbor_task_digest`` when the
registry is importable.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evallab.digest_drift import harbor_task_digest, verify_job_digests


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_task(root: Path, name: str = "demo-task") -> Path:
    task = root / name
    _write(task / "task.toml", 'name = "demo-task"\nversion = "1.0.0"\n')
    _write(task / "instruction.md", "# Do the demo thing\n")
    _write(task / "README.md", "Demo task.\n")
    _write(task / "environment" / "Dockerfile", "FROM python:3.12\n")
    _write(task / "tests" / "test_outputs.py", "def test_ok():\n    assert True\n")
    _write(task / "solution" / "solve.sh", "#!/bin/sh\ntrue\n")
    return task


def _expected_harbor_digest(task_dir: Path) -> str:
    """Independent reimplementation of the documented Harbor framing."""
    files: list[Path] = []
    for name in ("task.toml", "instruction.md", "README.md"):
        if (task_dir / name).is_file():
            files.append(task_dir / name)
    for name in ("environment", "tests", "solution", "steps"):
        sub = task_dir / name
        if sub.exists():
            files.extend(p for p in sub.rglob("*") if p.is_file())
    kept = [
        p
        for p in files
        if "__pycache__" not in p.relative_to(task_dir).parts
        and p.name != ".DS_Store"
        and p.suffix not in {".pyc", ".swp", ".swo"}
        and not p.name.endswith("~")
    ]
    aggregate = hashlib.sha256()
    for path in sorted(kept, key=lambda p: p.relative_to(task_dir).as_posix()):
        rel = path.relative_to(task_dir).as_posix()
        aggregate.update(f"{rel}\0{hashlib.sha256(path.read_bytes()).hexdigest()}\n".encode())
    return f"sha256:{aggregate.hexdigest()}"


def _make_job(
    root: Path,
    lock_tasks: list[dict],
    *,
    trial_names: list[str] | None = None,
) -> Path:
    job = root / "job"
    job.mkdir(parents=True, exist_ok=True)
    lock = {
        "schema_version": 3,
        "harbor": {"version": "0.21.0", "is_editable": False},
        "trials": [{"schema_version": 2, "task": task} for task in lock_tasks],
    }
    (job / "lock.json").write_text(json.dumps(lock), encoding="utf-8")
    for index, trial_name in enumerate(trial_names or []):
        trial_dir = job / trial_name
        trial_dir.mkdir()
        result = {
            "trial_name": trial_name,
            "task_name": "local-lab/demo-task",
            # Deliberately bogus: proves the module never joins on this
            # different namespace.
            "task_checksum": "0" * 64,
        }
        (trial_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
        trial_lock = {"schema_version": 2, "task": lock_tasks[min(index, len(lock_tasks) - 1)]}
        (trial_dir / "lock.json").write_text(json.dumps(trial_lock), encoding="utf-8")
    return job


def _lock_task(name: str, digest: str | None, path: str | Path) -> dict:
    task: dict = {"name": name, "version": "1.0.0", "type": "local", "path": str(path)}
    if digest is not None:
        task["digest"] = digest
    return task


def test_digest_matches_documented_harbor_framing(tmp_path: Path) -> None:
    task = _make_task(tmp_path / "tasks")
    assert harbor_task_digest(task) == _expected_harbor_digest(task)


def test_digest_ignores_harbor_noise_but_sees_real_files(tmp_path: Path) -> None:
    task = _make_task(tmp_path / "tasks")
    before = harbor_task_digest(task)
    _write(task / "tests" / "__pycache__" / "test_outputs.cpython-312.pyc", "noise")
    _write(task / "environment" / ".DS_Store", "noise")
    _write(task / "tests" / "scratch.py~", "noise")
    _write(task / "notes-outside-harbor-set.txt", "not in the Harbor file set")
    assert harbor_task_digest(task) == before
    _write(task / "instruction.md", "# Changed instruction\n")
    assert harbor_task_digest(task) != before


def test_digest_agrees_with_registry_oracle(tmp_path: Path) -> None:
    registry = pytest.importorskip("evallab.registry")
    task = _make_task(tmp_path / "tasks")
    assert harbor_task_digest(task) == registry.harbor_task_digest(task)
    # The registry whole-tree digest is a different value for the same tree:
    # pinning the distinction guards against silently switching namespaces.
    assert harbor_task_digest(task) != registry.task_directory_digest(task)


def test_match_when_task_unchanged(tmp_path: Path) -> None:
    task = _make_task(tmp_path / "tasks")
    digest = _expected_harbor_digest(task)
    job = _make_job(tmp_path, [_lock_task("demo-task", digest, task)], trial_names=["demo-task__Ab1"])
    report = verify_job_digests(job)
    assert report["ok"] is True
    assert report["algorithm"] == "harbor_task_digest"
    (entry,) = report["trials"]
    assert entry["trial_name"] == "demo-task__Ab1"
    assert entry["task_name"] == "demo-task"
    assert entry["lock_digest"] == digest
    assert entry["current_digest"] == digest
    assert entry["status"] == "match"
    assert entry["algorithm"] == "harbor_task_digest"
    assert report["summary"] == {"match": 1, "drift": 0, "task_missing": 0, "no_lock_digest": 0}


def test_drift_when_task_edited_after_the_run(tmp_path: Path) -> None:
    task = _make_task(tmp_path / "tasks")
    digest = _expected_harbor_digest(task)
    job = _make_job(tmp_path, [_lock_task("demo-task", digest, task)], trial_names=["demo-task__Ab1"])
    _write(task / "tests" / "test_outputs.py", "def test_ok():\n    assert 1 + 1 == 2\n")
    report = verify_job_digests(job)
    (entry,) = report["trials"]
    assert entry["status"] == "drift"
    assert entry["current_digest"] == _expected_harbor_digest(task)
    assert entry["current_digest"] != entry["lock_digest"]
    assert report["ok"] is False


def test_task_missing_when_directory_is_gone(tmp_path: Path) -> None:
    digest = "sha256:" + "a" * 64
    job = _make_job(tmp_path, [_lock_task("gone-task", digest, tmp_path / "nope")])
    report = verify_job_digests(job)
    (entry,) = report["trials"]
    assert entry["status"] == "task_missing"
    assert entry["current_digest"] is None
    assert entry["resolved_task_path"] is None
    assert entry["trial_name"] is None  # no trial directories in this fixture


def test_no_lock_digest_still_reports_current_digest(tmp_path: Path) -> None:
    task = _make_task(tmp_path / "tasks")
    job = _make_job(tmp_path, [_lock_task("demo-task", None, task)])
    report = verify_job_digests(job)
    (entry,) = report["trials"]
    assert entry["status"] == "no_lock_digest"
    assert entry["lock_digest"] is None
    assert entry["current_digest"] == _expected_harbor_digest(task)


def test_task_root_resolves_relative_lock_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    task = _make_task(repo / "library" / "tasks")
    digest = _expected_harbor_digest(task)
    job = _make_job(
        tmp_path,
        [_lock_task("demo-task", digest, "library/tasks/demo-task")],
    )
    report = verify_job_digests(job, task_root=repo)
    (entry,) = report["trials"]
    assert entry["status"] == "match"
    assert entry["resolved_task_path"] == str(task)


def test_task_root_resolves_stale_absolute_path_by_task_name(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    task = _make_task(tasks_root)
    digest = _expected_harbor_digest(task)
    stale = "/Users/someone-else/old-checkout/library/tasks/demo-task"
    job = _make_job(tmp_path, [_lock_task("demo-task", digest, stale)])
    report = verify_job_digests(job, task_root=tasks_root)
    (entry,) = report["trials"]
    assert entry["status"] == "match"
    assert entry["resolved_task_path"] == str(task)


def test_multi_trial_job_reports_one_entry_per_lock_trial(tmp_path: Path) -> None:
    task = _make_task(tmp_path / "tasks")
    digest = _expected_harbor_digest(task)
    lock_task = _lock_task("demo-task", digest, task)
    names = ["demo-task__Aa1", "demo-task__Bb2", "demo-task__Cc3"]
    job = _make_job(tmp_path, [lock_task, lock_task, lock_task], trial_names=names)
    report = verify_job_digests(job)
    assert [entry["trial_name"] for entry in report["trials"]] == names
    assert {entry["status"] for entry in report["trials"]} == {"match"}
    assert report["summary"]["match"] == 3


def test_missing_lock_is_reported_not_raised(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    report = verify_job_digests(job)
    assert report["trials"] == []
    assert report["ok"] is False
    assert "lock.json" in report["error"]


def test_malformed_lock_is_reported_not_raised(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    (job / "lock.json").write_text("{not json", encoding="utf-8")
    report = verify_job_digests(job)
    assert report["trials"] == []
    assert "error" in report


def test_check_is_read_only(tmp_path: Path) -> None:
    task = _make_task(tmp_path / "tasks")
    digest = _expected_harbor_digest(task)
    job = _make_job(tmp_path, [_lock_task("demo-task", digest, task)], trial_names=["demo-task__Ab1"])

    def snapshot(root: Path) -> dict[str, tuple[int, float]]:
        return {
            str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime)
            for p in sorted(root.rglob("*"))
            if p.is_file()
        }

    before = snapshot(tmp_path)
    verify_job_digests(job)
    assert snapshot(tmp_path) == before
