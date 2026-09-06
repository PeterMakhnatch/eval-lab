"""fleet-status.sh behavior tests with injected git/gh (agents/CHECKS.md rule:
no host branches, no network, no developer state).

Fixture: a fake repo root with a mission board, and fake `git`/`gh`
executables that answer from canned data. Every assertion is against the
script's stdout.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.docs_consumer

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "fleet-status.sh"

BOARD = """# Board — pull, don't push

## status
- Review awaiting Peter: exact-head gate.

# What lives where
# house rules
1. No peer assignments.

## good-codebase
backlog:
  1. Review changes.
"""

FAKE_GIT = r"""#!/bin/bash
# Injected git: answers from canned branch data, records nothing on the host.
args="$*"
case "$args" in
    "--version") echo "git version 2.50.1" ;;
    "for-each-ref --format="*ahead-behind:origin/main*refs/heads/)
        now=$(date +%s)
        stale=$(( now - 60*60*100 ))
        printf 'role/m001-governance\toid-role/m001-governance\t%s\t3 0\n' "$now"
        if [ "${FLEET_TEST_GIT_FAILURE:-0}" = 1 ]; then
            printf 'role/program\toid-role/program\t%s\t?\n' "$stale"
        else
            printf 'role/program\toid-role/program\t%s\t2 0\n' "$stale"
        fi
        printf 'role/spent-zero\toid-role/spent-zero\t%s\t0 0\n' "$now"
        printf 'role/spent-tree\toid-role/spent-tree\t%s\t7 0\n' "$now"
        printf 'role/renamed-spent\tdeadbeef\t%s\t5 0\n' "$now"
        printf 'role/dirty-zero\toid-role/dirty-zero\t%s\t0 0\n' "$now"
        printf 'role/dirty-untracked\toid-role/dirty-untracked\t%s\t0 0\n' "$now"
        printf 'role/rogue\toid-role/rogue\t%s\t1 0\n' "$now"
        printf 'feat/topic\toid-feat/topic\t%s\t1 0\n' "$now"
        printf 'main\toid-main\t%s\t0 0\n' "$now" ;;
    "for-each-ref --format=%(refname:short) refs/heads/")
        printf 'role/m001-governance\nrole/program\nrole/spent-zero\n'
        printf 'role/spent-tree\nrole/renamed-spent\n'
        printf 'role/dirty-zero\nrole/dirty-untracked\nrole/rogue\nfeat/topic\nmain\n' ;;
    "rev-list --count origin/main..role/m001-governance") echo 3 ;;
    "rev-list --count origin/main..role/program")
        [ "${FLEET_TEST_GIT_FAILURE:-0}" = 1 ] && exit 128
        echo 2 ;;
    "rev-list --count origin/main..role/spent-zero")      echo 0 ;;
    "rev-list --count origin/main..role/spent-tree")      echo 7 ;;
    "rev-list --count origin/main..role/renamed-spent")   echo 5 ;;
    "rev-list --count origin/main..role/dirty-zero")      echo 0 ;;
    "rev-list --count origin/main..role/dirty-untracked") echo 0 ;;
    "rev-list --count origin/main..role/rogue")           echo 1 ;;
    "rev-list --count origin/main..feat/topic")           echo 1 ;;
    "rev-parse role/renamed-spent") echo deadbeef ;;
    "rev-parse "*) echo "oid-${args##* }" ;;
    *"diff --quiet"*"origin/main"*"role/spent-tree"*) exit 0 ;;
    *"diff --quiet"*"origin/main"*)                   exit 1 ;;
    "log -1 --format=%ct role/m001-governance") date +%s ;;
    "log -1 --format=%ct role/program")         echo $(( $(date +%s) - 60*60*100 )) ;;
    "log -1 --format=%ct role/rogue")           date +%s ;;
    "worktree list --porcelain")
        printf 'worktree %s/.worktrees/dirty-zero\n' "$FLEET_ROOT"
        printf 'HEAD 0000000\nbranch refs/heads/role/dirty-zero\n\n'
        printf 'worktree %s/.worktrees/dirty-untracked\n' "$FLEET_ROOT"
        printf 'HEAD 0000000\nbranch refs/heads/role/dirty-untracked\n' ;;
    *"/.worktrees/dirty-zero status "*) echo ' M src/live.py' ;;
    *"/.worktrees/dirty-untracked status "*) echo '?? src/untracked.py' ;;
    *) exit 0 ;;
esac
"""

FAKE_GH = r"""#!/bin/bash
args="$*"
case "$args" in
    *"--state merged"*)
        printf 'role/spent-tree\t11111111\n'
        printf 'role/program\told-merged-head\n'
        printf 'role/original-name\tdeadbeef\n' ;;
    *"--state open"*)   printf '#41  Some open PR  role/program\n' ;;
    *) exit 0 ;;
esac
"""


def make_fixture(tmp_path: Path) -> dict[str, str]:
    root = tmp_path / "repo with spaces"
    (root / "research" / "inbox").mkdir(parents=True)
    (root / "research" / "inbox" / "board.md").write_text(BOARD)
    claims = root / "research/inbox/claims"
    claims.mkdir()
    (claims / "pane-review-1.md").write_text(
        "item: review #1 exact-head gate\nrole: verify behavior\nwhy-me: pane (model)\n"
    )
    (root / ".worktrees" / "dirty-zero").mkdir(parents=True)
    (root / ".worktrees" / "dirty-untracked").mkdir(parents=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, body in (("git", FAKE_GIT), ("gh", FAKE_GH)):
        exe = fake_bin / name
        exe.write_text(body)
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return {
        "FLEET_ROOT": str(root),
        "FLEET_GIT": str(fake_bin / "git"),
        "FLEET_GH": str(fake_bin / "gh"),
        "FLEET_STALE_HOURS": "48",
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }


def run_fleet(tmp_path: Path, **env_overrides: str) -> str:
    env = {**os.environ, **make_fixture(tmp_path), **env_overrides}
    result = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_board_and_actual_pickup_counter_are_shown(tmp_path):
    out = run_fleet(tmp_path)
    assert "Review awaiting Peter: exact-head gate." in out
    assert "item: review #1 exact-head gate" in out
    assert "why-me: pane (model)" in out
    assert "No peer assignments." not in out


def test_zero_ahead_branch_is_spent(tmp_path):
    out = run_fleet(tmp_path)
    assert "role/spent-zero  SPENT — 0 ahead of origin/main" in out


def test_dirty_zero_ahead_worktree_is_active(tmp_path):
    out = run_fleet(tmp_path)
    dirty_line = next(
        line for line in out.splitlines() if line.strip().startswith("role/dirty-zero ")
    )
    assert "active, +0" in dirty_line
    assert "SPENT" not in dirty_line
    assert "uncommitted: 1 file(s)" in out


def test_dirty_untracked_only_worktree_is_active(tmp_path):
    out = run_fleet(tmp_path)
    dirty_line = next(
        line for line in out.splitlines() if line.strip().startswith("role/dirty-untracked ")
    )
    assert "active, +0" in dirty_line
    assert "SPENT" not in dirty_line
    assert "uncommitted: 1 file(s)" in out


def test_squash_merged_tree_is_spent_not_active(tmp_path):
    out = run_fleet(tmp_path)
    assert "role/spent-tree  SPENT" in out
    assert "role/spent-tree  active" not in out


def test_renamed_local_branch_matching_merged_head_sha_is_spent(tmp_path):
    out = run_fleet(tmp_path)
    assert "role/renamed-spent  SPENT — head of a merged PR" in out


def test_all_topic_prefixes_are_inventoried(tmp_path):
    out = run_fleet(tmp_path)
    assert "feat/topic  active" in out


def test_old_merged_branch_name_does_not_hide_new_work(tmp_path):
    out = run_fleet(tmp_path)
    assert "role/program  active" in out


def test_git_failure_reports_unknown_instead_of_spent(tmp_path):
    out = run_fleet(tmp_path, FLEET_TEST_GIT_FAILURE="1")
    assert "role/program  UNKNOWN" in out
    assert "role/program  SPENT" not in out


def test_stale_active_branch_is_flagged(tmp_path):
    out = run_fleet(tmp_path)
    program_line = next(
        line for line in out.splitlines() if line.strip().startswith("role/program ")
    )
    assert "STALE" in program_line


def test_missing_board_is_loud(tmp_path):
    env = make_fixture(tmp_path)
    board = Path(env["FLEET_ROOT"]) / "research" / "inbox" / "board.md"
    board.unlink()
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "governance broken" in result.stdout


def test_gh_absent_degrades_gracefully(tmp_path):
    out = run_fleet(tmp_path, FLEET_GH="")
    assert "gh unavailable" in out
    # git-only spent detection still works
    assert "role/spent-tree  SPENT" in out
