"""Probe behavior over real disposable Git repositories, not script text."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from evallab.dataset_audit_checks import build_leak_probe_script, parse_probe_output


def _git(root: Path, *args: str) -> str:
    env = {
        **os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Audit test", "GIT_AUTHOR_EMAIL": "audit@example.invalid",
        "GIT_COMMITTER_NAME": "Audit test", "GIT_COMMITTER_EMAIL": "audit@example.invalid",
    }
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args], cwd=root, env=env,
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _repository(root: Path) -> str:
    root.mkdir()
    _git(root, "init", "-q")
    (root / "code.txt").write_text("base\n")
    _git(root, "add", "code.txt")
    _git(root, "commit", "-qm", "base")
    return _git(root, "rev-parse", "HEAD")


def _probe(root: Path, output: Path, **overrides: str) -> dict:
    env = {**os.environ, "HOME": str(output.parent), "AUDIT_LEAK_OUT_DIR": str(output), **overrides}
    result = subprocess.run(
        ["bash", "-c", build_leak_probe_script()], cwd=root, env=env,
        check=True, capture_output=True, text=True,
    )
    return parse_probe_output(result.stdout)


def test_probe_reads_space_paths_future_refs_and_unreachable_without_mutation(tmp_path: Path):
    root = tmp_path / "repository with spaces"
    base = _repository(root)
    (root / "code.txt").write_text("future ref\n")
    _git(root, "commit", "-qam", "future")
    _git(root, "checkout", "-q", "--detach", base)
    (root / "code.txt").write_text("unreachable future\n")
    _git(root, "commit", "-qam", "unreachable")
    _git(root, "checkout", "-q", "--detach", base)
    before = {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    result = _probe(root, tmp_path / "logs")
    repository = next(row for row in result["repos"] if row["path"] == str(root))
    assert repository["base"] == base
    assert repository["beyond_base_refs"] == 1
    assert repository["unreachable_commits"] == 1
    assert repository["complete"] is True
    assert result["done"] == len(result["repos"])
    assert {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


def test_failed_fsck_is_unknown_not_clean(tmp_path: Path):
    root = tmp_path / "repository"
    _repository(root)
    real_git = shutil.which("git")
    assert real_git is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "git"
    wrapper.write_text(
        '#!/bin/sh\nif [ "$3" = "fsck" ]; then exit 1; fi\n'
        f'exec "{real_git}" "$@"\n',
    )
    wrapper.chmod(0o755)
    result = _probe(root, tmp_path / "logs", PATH=str(bin_dir) + os.pathsep + os.environ["PATH"])
    repository = next(row for row in result["repos"] if row["path"] == str(root))
    assert repository["beyond_base_refs"] == 0
    assert repository["unreachable_commits"] == "unknown"
    assert repository["complete"] is False
