"""Behaviour tests for scripts/sync-primary-checkout.sh.

Every case builds a local bare remote plus throwaway clones, so no network is
involved. The script under test is driven only through its public interface
(env vars + exit status + sync-status.json), the same way launchd drives it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync-primary-checkout.sh"

GIT_IDENTITY = ["-c", "user.name=Sync Test", "-c", "user.email=sync-test@example.com"]


def run_git(*args: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return completed.stdout.strip()


def commit_file(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content + "\n")
    run_git("add", name, cwd=repo)
    run_git(*GIT_IDENTITY, "commit", "-m", name, cwd=repo)
    return run_git("rev-parse", "HEAD", cwd=repo)


@pytest.fixture
def lab(tmp_path: Path) -> dict[str, Path]:
    """A bare remote, a seed clone holding the remote's main, and a primary clone."""
    remote = tmp_path / "remote.git"
    run_git("init", "--bare", "--initial-branch=main", str(remote))
    seed = tmp_path / "seed"
    run_git("clone", str(remote), str(seed))
    commit_file(seed, "note.txt", "v1")
    run_git("push", "-u", "origin", "main", cwd=seed)
    primary = tmp_path / "primary"
    run_git("clone", str(remote), str(primary))
    state = tmp_path / "state"
    return {"remote": remote, "seed": seed, "primary": primary, "state": state}


def run_sync(repo: Path, state: Path) -> dict:
    env = {
        "EVALLAB_SYNC_REPO": str(repo),
        "EVALLAB_SYNC_STATE_DIR": str(state),
        "EVALLAB_SYNC_NO_NOTIFY": "1",
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    }
    completed = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads((state / "sync-status.json").read_text())


def advance_remote(lab: dict[str, Path], name: str = "note.txt", content: str = "v2") -> str:
    sha = commit_file(lab["seed"], name, content)
    run_git("push", "origin", "main", cwd=lab["seed"])
    return sha


def test_sync_fast_forwards_clean_behind_clone(lab: dict[str, Path]) -> None:
    remote_head = advance_remote(lab)
    status = run_sync(lab["primary"], lab["state"])
    assert status["outcome"] == "synced"
    assert status["behind"] == 0
    assert run_git("rev-parse", "HEAD", cwd=lab["primary"]) == remote_head


def test_sync_reports_up_to_date_when_nothing_to_do(lab: dict[str, Path]) -> None:
    status = run_sync(lab["primary"], lab["state"])
    assert (status["outcome"], status["reason"]) == ("up-to-date", "up-to-date")


def test_sync_skips_dirty_tree_and_counts_files(lab: dict[str, Path]) -> None:
    advance_remote(lab)
    before = run_git("rev-parse", "HEAD", cwd=lab["primary"])
    (lab["primary"] / "note.txt").write_text("local edit\n")
    (lab["primary"] / "scratch.txt").write_text("untracked\n")
    status = run_sync(lab["primary"], lab["state"])
    assert (status["outcome"], status["reason"]) == ("skipped", "dirty")
    assert status["dirty_files"] == 2
    assert "2 files" in status["message"]
    # The checkout is untouched: still behind, same HEAD.
    assert run_git("rev-parse", "HEAD", cwd=lab["primary"]) == before
    assert status["behind"] == 1


def test_sync_skips_checkout_on_another_branch(lab: dict[str, Path]) -> None:
    advance_remote(lab)
    run_git("checkout", "-b", "side-work", cwd=lab["primary"])
    status = run_sync(lab["primary"], lab["state"])
    assert (status["outcome"], status["reason"]) == ("skipped", "off-branch")
    assert "side-work" in status["message"]
    assert run_git("branch", "--show-current", cwd=lab["primary"]) == "side-work"


def test_sync_skips_diverged_tree_with_ahead_behind_counts(lab: dict[str, Path]) -> None:
    commit_file(lab["primary"], "local.txt", "local commit")
    advance_remote(lab)
    before = run_git("rev-parse", "HEAD", cwd=lab["primary"])
    status = run_sync(lab["primary"], lab["state"])
    assert (status["outcome"], status["reason"]) == ("skipped", "diverged")
    assert (status["ahead"], status["behind"]) == (1, 1)
    assert "ahead 1, behind 1" in status["message"]
    # Local commit preserved, no merge attempted.
    assert run_git("rev-parse", "HEAD", cwd=lab["primary"]) == before


def test_sync_skips_on_fetch_failure(lab: dict[str, Path]) -> None:
    run_git("remote", "set-url", "origin", str(lab["remote"]) + "-missing", cwd=lab["primary"])
    status = run_sync(lab["primary"], lab["state"])
    assert (status["outcome"], status["reason"]) == ("skipped", "fetch-failed")
    assert status["fetch_ok"] is False
    assert status["fetch_error"]


def test_sync_skips_merge_in_progress(lab: dict[str, Path]) -> None:
    advance_remote(lab)
    (lab["primary"] / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n")
    try:
        status = run_sync(lab["primary"], lab["state"])
    finally:
        (lab["primary"] / ".git" / "MERGE_HEAD").unlink()
    assert (status["outcome"], status["reason"]) == ("skipped", "in-progress")
