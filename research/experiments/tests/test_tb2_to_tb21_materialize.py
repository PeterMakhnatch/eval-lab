from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "research" / "experiments" / "tb2-to-tb21-v1"))

import materialize  # noqa: E402

from evallab.registry import compute_task_digests  # noqa: E402


def _commit_task(repo: Path, task_id: str) -> str:
    package = repo / task_id
    (package / "environment").mkdir(parents=True)
    (package / "tests").mkdir()
    (package / "task.toml").write_text('version = "1.0"\n', encoding="utf-8")
    (package / "instruction.md").write_text("do the task\n", encoding="utf-8")
    (package / "environment" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (package / "tests" / "test.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", task_id], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Eval Lab Test",
            "-c",
            "user.email=eval-lab@example.invalid",
            "commit",
            "-qm",
            "pin task",
        ],
        cwd=repo,
        check=True,
    )
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()


def _item(task_id: str, digest: str) -> dict:
    return {
        "item_id": f"tb2-to-tb21-v1:tb2:{task_id}",
        "subject": {"kind": "task", "task_id": f"tb2/{task_id}", "task_digest": digest},
    }


def test_materialize_matches_key_digest_and_records_failures(tmp_path: Path) -> None:
    source = tmp_path / "source"
    task_id = "sample-task"
    commit = _commit_task(source, task_id)
    digest = compute_task_digests(source / task_id).package
    pin = materialize.Pin("tb2", source, commit, "")
    runs = tmp_path / "runs"

    matched = materialize.materialize_packages([_item(task_id, digest)], runs, pins={"tb2": pin})
    assert matched["counts"]["digest_match"] == {"tb2": 1}
    assert matched["mismatches"] == []
    assert (runs / "packages" / "tb2" / task_id / "task.toml").is_file()

    mismatched = materialize.materialize_packages(
        [_item(task_id, "sha256:" + "0" * 64)],
        tmp_path / "mismatch",
        pins={"tb2": pin},
    )
    assert mismatched["counts"]["digest_mismatch"] == {"tb2": 1}
    assert mismatched["mismatches"][0]["actual_digest"] == digest

    missing = materialize.materialize_packages(
        [_item("missing-task", digest)],
        tmp_path / "missing",
        pins={"tb2": pin},
    )
    assert missing["counts"]["unmaterialized"] == {"tb2": 1}
    assert missing["unmaterialized"][0]["task"] == "tb2/missing-task"
    assert missing["unmaterialized"][0]["error"]
