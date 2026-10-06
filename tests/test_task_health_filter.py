"""Behavioral coverage for TaskHealthFilter (HAR-172).

Every case builds real Harbor-shaped job/trial directories and a real
``evallab.task_health_tags/v1`` manifest, with package digests computed by the
same helpers the Lab uses. Nothing is mocked; names alone never bind.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from evallab.registry import harbor_task_digest, task_directory_digest
from evallab.task_health_filter import SCHEMA, TaskHealthFilter

SOUND_MIXED = ["health:sound", "solve:mixed"]


def _package(root: Path, name: str, instruction: str) -> Path:
    pkg = root / name
    (pkg / "tests").mkdir(parents=True)
    (pkg / "task.toml").write_text(
        f'[task]\nname = "{name}"\n[metadata]\ncategory = "Python"\n',
        encoding="utf-8",
    )
    (pkg / "instruction.md").write_text(instruction, encoding="utf-8")
    (pkg / "tests/test.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    return pkg


def _row(task_id: str, parent: Path, variant: Path, tags: list[str]) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "parent_digest": task_directory_digest(parent),
        "parent_harbor_digest": harbor_task_digest(parent),
        "variant_digest": task_directory_digest(variant),
        "variant_harbor_digest": harbor_task_digest(variant),
        "tags": list(tags),
    }


def _manifest(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text(json.dumps({"schema": SCHEMA, "tasks": rows}), encoding="utf-8")
    return path


def _trial(
    job: Path,
    name: str,
    *,
    staged: Path,
    digest: str,
    lock_path: str | None = None,
    trial_lock: bool = True,
) -> Path:
    trial = job / name
    trial.mkdir(parents=True)
    (trial / "config.json").write_text(
        json.dumps({"task": {"path": str(staged)}}), encoding="utf-8"
    )
    if trial_lock:
        (trial / "lock.json").write_text(
            json.dumps(
                {
                    "task": {
                        "name": name,
                        "type": "local",
                        "digest": digest,
                        "path": lock_path or str(staged),
                    }
                }
            ),
            encoding="utf-8",
        )
    return trial


def _job_lock(job: Path, entries: list[dict[str, Any]]) -> None:
    (job / "lock.json").write_text(
        json.dumps({"trials": [{"task": entry} for entry in entries]}), encoding="utf-8"
    )


def _sidecar(job: Path, **staging: Any) -> None:
    (job / "lab-metadata.json").write_text(
        json.dumps({"task_staging": dict(staging)}), encoding="utf-8"
    )


def _select(manifest: Path, tags: list[str], mapping: dict[Path, list[Path]]):
    return TaskHealthFilter(manifest, tags).select(mapping)


def test_staged_to_source_join_uses_package_evidence_not_names(tmp_path: Path):
    root = tmp_path / "lab"
    source = _package(root / "sources", "mimo-v2-6-rl-format-code-task-000084-dd79af", "Do it.\n")
    variant = _package(root / "variants", "tagged-copy", "Do it.\n[health]\n")
    staged = root / "runs" / ".exec-stage" / "har157-mimo-base-000084"
    staged.parent.mkdir(parents=True)
    shutil.copytree(variant, staged)
    job = root / "jobs" / "har157-mimo-base-000084"
    job.mkdir(parents=True)
    trial = _trial(
        job, "har157-mimo-base-000084__NUg9Yjo", staged=staged, digest=harbor_task_digest(staged)
    )
    _sidecar(
        job,
        source_package_digest=task_directory_digest(variant),
        source_harbor_digest=harbor_task_digest(variant),
        staged_harbor_digest=harbor_task_digest(staged),
        declared_task_name="mimo-v2.6-rl/format-code-task-000084",
        staged_task_name="har157-mimo-base-000084",
    )
    manifest = _manifest(
        root / "manifest.json",
        [_row("format-code-task-000084", source, variant, SOUND_MIXED)],
    )
    selected, report = _select(manifest, ["health:sound"], {job: [trial]})
    assert selected == {job: [trial]}
    entry = report["trials"][str(trial.absolute())]
    assert entry["status"] == "included"
    assert entry["task_id"] == "format-code-task-000084"
    assert entry["tags"] == SOUND_MIXED
    assert entry["binding"]["via"] == "staged-source"
    assert report["totals"] == {
        "trials": 1,
        "included": 1,
        "tag_mismatch": 0,
        "unbound": 0,
        "digest_mismatch": 0,
        "ambiguous": 0,
    }


def test_direct_native_lock_match_without_sidecar(tmp_path: Path):
    root = tmp_path / "lab"
    parent = _package(root / "sources", "task-a", "Alpha.\n")
    staged = root / "staged" / "job-task"
    staged.parent.mkdir(parents=True)
    shutil.copytree(parent, staged)
    job = root / "jobs" / "job-a"
    job.mkdir(parents=True)
    trial = _trial(job, "trial-1", staged=staged, digest=harbor_task_digest(parent))
    manifest = _manifest(
        root / "manifest.json", [_row("format-code-task-000001", parent, parent, SOUND_MIXED)]
    )
    selected, report = _select(manifest, ["solve:mixed"], {job: [trial]})
    assert selected == {job: [trial]}
    entry = report["trials"][str(trial.absolute())]
    assert entry["status"] == "included"
    assert entry["binding"]["via"] == "native-lock"


def test_repeat_tags_are_and(tmp_path: Path):
    root = tmp_path / "lab"
    parent = _package(root / "sources", "task-a", "Alpha.\n")
    job = root / "jobs" / "job-a"
    job.mkdir(parents=True)
    trial = _trial(job, "trial-1", staged=parent, digest=harbor_task_digest(parent))
    manifest = _manifest(
        root / "manifest.json", [_row("format-code-task-000001", parent, parent, SOUND_MIXED)]
    )
    selected, _ = _select(manifest, ["health:sound", "health:sound", "solve:mixed"], {job: [trial]})
    assert selected == {job: [trial]}
    selected, report = _select(manifest, ["health:sound", "solve:always"], {job: [trial]})
    assert selected == {job: []}
    entry = report["trials"][str(trial.absolute())]
    assert entry["status"] == "tag_mismatch"
    assert entry["task_id"] == "format-code-task-000001"
    assert report["totals"]["tag_mismatch"] == 1


def test_same_name_different_package_is_excluded(tmp_path: Path):
    root = tmp_path / "lab"
    recorded = _package(root / "sources", "task-a", "Recorded behavior.\n")
    executed = _package(root / "sources", "task-a-fork", "Different behavior.\n")
    assert task_directory_digest(recorded) != task_directory_digest(executed)
    job = root / "jobs" / "job-a"
    job.mkdir(parents=True)
    trial = _trial(
        job,
        "trial-1",
        staged=executed,
        digest=harbor_task_digest(executed),
        lock_path=str(executed),
    )
    # Trial lock reuses the recorded task name; only digests may bind.
    lock = json.loads((trial / "lock.json").read_text(encoding="utf-8"))
    lock["task"]["name"] = "task-a"
    (trial / "lock.json").write_text(json.dumps(lock), encoding="utf-8")
    manifest = _manifest(
        root / "manifest.json", [_row("format-code-task-000001", recorded, recorded, SOUND_MIXED)]
    )
    selected, report = _select(manifest, ["health:sound"], {job: [trial]})
    assert selected == {job: []}
    entry = report["trials"][str(trial.absolute())]
    assert entry["status"] == "digest_mismatch"
    assert "task_id" not in entry
    assert report["totals"]["digest_mismatch"] == 1


def test_missing_identity_stays_unbound_and_keeps_job_keys(tmp_path: Path):
    root = tmp_path / "lab"
    parent = _package(root / "sources", "task-a", "Alpha.\n")
    job = root / "jobs" / "job-a"
    job.mkdir(parents=True)
    bare = job / "trial-bare"
    bare.mkdir()
    (bare / "config.json").write_text(json.dumps({"agent": "x"}), encoding="utf-8")
    manifest = _manifest(
        root / "manifest.json", [_row("format-code-task-000001", parent, parent, SOUND_MIXED)]
    )
    other = root / "jobs" / "job-empty"
    other.mkdir()
    selected, report = _select(manifest, ["health:sound"], {job: [bare], other: []})
    assert selected == {job: [], other: []}
    assert report["trials"][str(bare.absolute())]["status"] == "unbound"
    assert report["totals"]["unbound"] == 1


def test_job_lock_fallback_binds_unique_path_match(tmp_path: Path):
    root = tmp_path / "lab"
    parent = _package(root / "sources", "task-a", "Alpha.\n")
    job = root / "jobs" / "job-a"
    job.mkdir(parents=True)
    trial = _trial(
        job, "trial-1", staged=parent, digest=harbor_task_digest(parent), trial_lock=False
    )
    _job_lock(
        job,
        [
            {"path": str(parent), "digest": harbor_task_digest(parent), "name": "task-a"},
            {"path": "/elsewhere/other", "digest": "sha256:" + "1" * 64, "name": "other"},
        ],
    )
    manifest = _manifest(
        root / "manifest.json", [_row("format-code-task-000001", parent, parent, SOUND_MIXED)]
    )
    selected, report = _select(manifest, ["health:sound"], {job: [trial]})
    assert selected == {job: [trial]}
    assert report["trials"][str(trial.absolute())]["binding"]["lock"] == "job-lock"


def test_contradictory_verified_source_never_falls_back(tmp_path: Path):
    root = tmp_path / "lab"
    favored = _package(root / "sources", "task-good", "Good.\n")
    job = root / "jobs" / "job-a"
    job.mkdir(parents=True)
    trial = _trial(job, "trial-1", staged=favored, digest=harbor_task_digest(favored))
    # Verified staged sidecar agrees with the lock but its source points nowhere.
    _sidecar(
        job,
        source_package_digest="sha256:" + "0" * 64,
        source_harbor_digest="sha256:" + "0" * 64,
        staged_harbor_digest=harbor_task_digest(favored),
    )
    manifest = _manifest(
        root / "manifest.json",
        [_row("format-code-task-000002", favored, favored, SOUND_MIXED)],
    )
    selected, report = _select(manifest, ["health:sound"], {job: [trial]})
    assert selected == {job: []}
    entry = report["trials"][str(trial.absolute())]
    assert entry["status"] == "digest_mismatch"
    # A sidecar that disagrees with the lock is ignored; the native path works.
    other = root / "jobs" / "job-b"
    other.mkdir()
    second = _trial(other, "trial-2", staged=favored, digest=harbor_task_digest(favored))
    _sidecar(
        other, staged_harbor_digest="sha256:" + "2" * 64, source_package_digest="sha256:" + "3" * 64
    )
    selected, report = _select(manifest, ["health:sound"], {other: [second]})
    assert selected == {other: [second]}


def test_job_sidecar_cannot_label_another_task(tmp_path: Path):
    root = tmp_path / "lab"
    first = _package(root / "sources", "task-first", "First.\n")
    second = _package(root / "sources", "task-second", "Second.\n")
    job = root / "jobs" / "job-multi"
    job.mkdir(parents=True)
    trial_one = _trial(job, "trial-one", staged=first, digest=harbor_task_digest(first))
    trial_two = _trial(job, "trial-two", staged=second, digest=harbor_task_digest(second))
    _job_lock(
        job,
        [
            {"path": str(first), "digest": harbor_task_digest(first), "name": "task-first"},
            {"path": str(second), "digest": harbor_task_digest(second), "name": "task-second"},
        ],
    )
    _sidecar(
        job,
        source_package_digest=task_directory_digest(first),
        source_harbor_digest=harbor_task_digest(first),
        staged_harbor_digest=harbor_task_digest(first),
    )
    manifest = _manifest(
        root / "manifest.json",
        [_row("format-code-task-000001", first, first, SOUND_MIXED)],
    )
    selected, report = _select(manifest, ["health:sound"], {job: [trial_one, trial_two]})
    assert selected == {job: [trial_one]}
    assert report["trials"][str(trial_one.absolute())]["status"] == "included"
    assert report["trials"][str(trial_two.absolute())]["status"] == "digest_mismatch"
    assert report["totals"]["included"] == 1
    assert report["totals"]["digest_mismatch"] == 1


def test_duplicate_digest_is_ambiguous_not_first_match(tmp_path: Path):
    root = tmp_path / "lab"
    parent = _package(root / "sources", "task-a", "Alpha.\n")
    job = root / "jobs" / "job-a"
    job.mkdir(parents=True)
    trial = _trial(job, "trial-1", staged=parent, digest=harbor_task_digest(parent))
    rows = [
        _row("format-code-task-000001", parent, parent, SOUND_MIXED),
        _row("format-code-task-000002", parent, parent, ["health:review", "solve:mixed"]),
    ]
    manifest = _manifest(root / "manifest.json", rows)
    selected, report = _select(manifest, ["health:sound"], {job: [trial]})
    assert selected == {job: []}
    entry = report["trials"][str(trial.absolute())]
    assert entry["status"] == "ambiguous"
    assert entry["binding"]["candidates"] == ["format-code-task-000001", "format-code-task-000002"]
    assert report["totals"]["ambiguous"] == 1


def test_invalid_manifest_and_tags_fail_fast(tmp_path: Path):
    root = tmp_path / "lab"
    parent = _package(root / "sources", "task-a", "Alpha.\n")
    good = _manifest(
        root / "good.json", [_row("format-code-task-000001", parent, parent, SOUND_MIXED)]
    )
    with pytest.raises(ValueError):
        TaskHealthFilter(good, [])
    with pytest.raises(ValueError):
        TaskHealthFilter(good, ["health:bogus"])
    bad_schema = root / "bad-schema.json"
    bad_schema.write_text(json.dumps({"schema": "other/v1", "tasks": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        TaskHealthFilter(bad_schema, ["health:sound"])
    bad_digest = root / "bad-digest.json"
    rows = [_row("format-code-task-000001", parent, parent, SOUND_MIXED)]
    rows[0]["parent_digest"] = "sha256:xyz"
    bad_digest.write_text(json.dumps({"schema": SCHEMA, "tasks": rows}), encoding="utf-8")
    with pytest.raises(ValueError):
        TaskHealthFilter(bad_digest, ["health:sound"])
    empty_tasks = root / "empty.json"
    empty_tasks.write_text(json.dumps({"schema": SCHEMA, "tasks": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        TaskHealthFilter(empty_tasks, ["health:sound"])


@pytest.mark.parametrize("contradictory", [False, True])
def test_job_lock_attempts_require_one_distinct_task_digest(tmp_path: Path, contradictory: bool):
    parent = _package(tmp_path / "sources", "task-a", "Alpha.\n")
    job = tmp_path / "job"
    job.mkdir()
    digest = harbor_task_digest(parent)
    trial = _trial(job, "attempt", staged=parent, digest=digest, trial_lock=False)
    _job_lock(
        job,
        [
            {"path": str(parent), "digest": digest},
            {"path": str(parent), "digest": "sha256:" + "0" * 64 if contradictory else digest},
        ],
    )
    manifest = _manifest(
        tmp_path / "health.json", [_row("format-code-task-000001", parent, parent, SOUND_MIXED)]
    )
    selected, report = _select(manifest, ["health:sound"], {job: [trial]})
    assert selected[job] == ([] if contradictory else [trial])
    assert report["trials"][str(trial)]["status"] == ("unbound" if contradictory else "included")


@pytest.mark.parametrize("relative", [False, True])
def test_contradictory_trial_lock_cannot_fall_back_or_guess_reader_cwd(
    tmp_path: Path, relative: bool
):
    parent = _package(tmp_path / "sources", "task-a", "Alpha.\n")
    job = tmp_path / "job"
    job.mkdir()
    digest = harbor_task_digest(parent)
    config_path = Path("task-a") if relative else parent
    lock_path = str(Path.cwd() / "task-a") if relative else "/other-task"
    trial = _trial(job, "attempt", staged=config_path, digest=digest, lock_path=lock_path)
    _job_lock(job, [{"path": str(config_path), "digest": digest}])
    manifest = _manifest(
        tmp_path / "health.json", [_row("format-code-task-000001", parent, parent, SOUND_MIXED)]
    )
    selected, report = _select(manifest, ["health:sound"], {job: [trial]})
    assert selected[job] == []
    assert report["trials"][str(trial)]["status"] == "unbound"
