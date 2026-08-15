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

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "fleet-status.sh"

BOARD = """# Mission board

## Now

- M001 governance rewrite in flight.

## Review

- nothing open.

## Next

- M002 ty ratchet.

## Needs Peter

- M001 PR review.

---

| ID | Outcome | Lane | Agent | Branch | Paths | Deps | Acceptance | PR | State | Owner |
|---|---|---|---|---|---|---|---|---|---|---|
| M001 | x | x | x | `role/m001-governance` | x | x | x | — | active | x |
| LEGACY | x | x | x | `role/program` | x | x | x | — | active | x |
| DIRTY | x | x | x | `role/dirty-zero` | x | x | x | — | active | x |
| GHOST | x | x | x | `role/ghost` | x | x | x | — | active | x |
| FUTURE | x | x | x | `role/future` | x | x | x | — | ready | x |
"""

FAKE_GIT = r"""#!/bin/bash
# Injected git: answers from canned branch data, records nothing on the host.
args="$*"
case "$args" in
    "for-each-ref --format=%(refname:short) refs/heads/")
        printf 'role/m001-governance\nrole/program\nrole/spent-zero\n'
        printf 'role/spent-tree\nrole/dirty-zero\nrole/rogue\nmain\n' ;;
    "rev-list --count origin/main..role/m001-governance") echo 3 ;;
    "rev-list --count origin/main..role/program")         echo 2 ;;
    "rev-list --count origin/main..role/spent-zero")      echo 0 ;;
    "rev-list --count origin/main..role/spent-tree")      echo 7 ;;
    "rev-list --count origin/main..role/dirty-zero")      echo 0 ;;
    "rev-list --count origin/main..role/rogue")           echo 1 ;;
    "diff --quiet origin/main...role/spent-tree") exit 0 ;;
    "diff --quiet origin/main..."*)               exit 1 ;;
    "log -1 --format=%ct role/m001-governance") date +%s ;;
    "log -1 --format=%ct role/program")         echo $(( $(date +%s) - 60*60*100 )) ;;
    "log -1 --format=%ct role/rogue")           date +%s ;;
    "worktree list --porcelain")
        printf 'worktree %s/.worktrees/dirty-zero\n' "$FLEET_ROOT"
        printf 'HEAD 0000000\nbranch refs/heads/role/dirty-zero\n' ;;
    *"/.worktrees/dirty-zero status --short") echo ' M src/live.py' ;;
    *) exit 0 ;;
esac
"""

FAKE_GH = r"""#!/bin/bash
args="$*"
case "$args" in
    *"--state merged"*) printf 'role/spent-tree\nrole/old-merged\n' ;;
    *"--state open"*)   printf '#41  Some open PR  role/program\n' ;;
    *) exit 0 ;;
esac
"""


def make_fixture(tmp_path: Path) -> dict[str, str]:
    root = tmp_path / "repo"
    (root / "agents" / "missions").mkdir(parents=True)
    (root / "agents" / "missions" / "ACTIVE.md").write_text(BOARD)
    (root / ".worktrees" / "dirty-zero").mkdir(parents=True)
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


def test_board_sections_are_shown(tmp_path):
    out = run_fleet(tmp_path)
    for heading in ("## Now", "## Review", "## Next", "## Needs Peter"):
        assert heading in out
    # nothing after the --- separator (the mission table) is echoed here
    assert "| LEGACY |" not in out.split("## branches")[0]


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


def test_squash_merged_tree_is_spent_not_active(tmp_path):
    out = run_fleet(tmp_path)
    assert "role/spent-tree  SPENT" in out
    assert "squash-merged" in out
    assert "role/spent-tree  active" not in out


def test_unregistered_branch_is_flagged(tmp_path):
    out = run_fleet(tmp_path)
    assert "role/rogue" in out
    assert "UNREGISTERED" in out.split("role/rogue", 1)[1].splitlines()[0]


def test_registered_active_branch_not_flagged_unregistered(tmp_path):
    out = run_fleet(tmp_path)
    m001_line = next(
        line for line in out.splitlines() if line.strip().startswith("role/m001-governance")
    )
    assert "UNREGISTERED" not in m001_line
    assert "active, +3" in m001_line


def test_stale_active_branch_is_flagged(tmp_path):
    out = run_fleet(tmp_path)
    program_line = next(
        line for line in out.splitlines() if line.strip().startswith("role/program ")
    )
    assert "STALE" in program_line


def test_board_hygiene_flags_missing_branch(tmp_path):
    # role/ghost is on the board but exists as no branch in the fake git;
    # the hygiene section must call out board/reality drift.
    out = run_fleet(tmp_path)
    assert "## board hygiene" in out
    assert "board lists role/ghost but no such local branch" in out


def test_board_hygiene_allows_unallocated_ready_branch(tmp_path):
    out = run_fleet(tmp_path)
    assert "board lists role/future" not in out


def test_missing_board_is_loud(tmp_path):
    env = make_fixture(tmp_path)
    board = Path(env["FLEET_ROOT"]) / "agents" / "missions" / "ACTIVE.md"
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
