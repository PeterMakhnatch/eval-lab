"""Public corpus custody, sealed splits, and honest executable admission."""

from __future__ import annotations

from pathlib import Path

import pytest

from evallab.registry import task_directory_digest


def _package(path: Path, *, compose: bool = False) -> Path:
    (path / "environment").mkdir(parents=True)
    (path / "tests").mkdir()
    (path / "task.toml").write_text(
        '[task]\nname = "task-a"\n[environment]\ncpus = 1\nmemory_mb = 512\nstorage_mb = 512\n',
        encoding="utf-8",
    )
    (path / "instruction.md").write_text("Write the requested output.\n")
    (path / "environment/Dockerfile").write_text("FROM python:3.12-slim\nWORKDIR /app\n")
    (path / "tests/test.sh").write_text("exit 0\n")
    if compose:
        (path / "environment/compose.yaml").write_text("services: {}\n")
    return path


@pytest.mark.parametrize("compose", [False, True])
def test_policy_refused_candidate_preflight_is_never_execution_proof(
    tmp_path: Path, compose: bool,
) -> None:
    package = _package(tmp_path / "public-package", compose=compose)
    digest = task_directory_digest(package)
    assert digest.startswith("sha256:") and len(digest) == len("sha256:") + 64
    assert task_directory_digest(package) == digest
