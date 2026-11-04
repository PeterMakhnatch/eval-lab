"""Preflight every target before deleting any worktree or branch."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from evallab.ship import Ship


CACHE_ROOTS = {".venv", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".hypothesis"}


def inspect_tree(ship: Ship, tree: Path, head: str) -> None:
    from evallab.ship import Blocked

    actual = ship.run(["git", "rev-parse", "HEAD"], cwd=tree).stdout.strip()
    if actual != head:
        raise Blocked(f"Cleanup refused: changed HEAD in {tree}")
    dirty = ship.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=tree).stdout
    if dirty:
        raise Blocked(f"Cleanup refused: dirty worktree {tree}\n{dirty}")
    for operation in ("index.lock", "HEAD.lock", "rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD"):
        path = ship.run(["git", "rev-parse", "--git-path", operation], cwd=tree).stdout.strip()
        if (tree / path).exists():
            raise Blocked(f"Cleanup refused: Git operation {operation} in {tree}")
    ignored = ship.run(["git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z"], cwd=tree).stdout
    unknown = [name for name in ignored.split("\0") if name and not (
        Path(name).parts[0] in CACHE_ROOTS or "__pycache__" in Path(name).parts
        or name in {".coverage", ".DS_Store"})]
    if unknown:
        raise Blocked(f"Cleanup refused: unique ignored files in {tree}: {unknown[:20]}")
    processes = ship.run(["lsof", "+D", str(tree)], check=False)
    if processes.returncode != 1 or processes.stdout.strip() or processes.stderr.strip():
        raise Blocked(f"Cleanup refused: running process or failed lsof probe for {tree}\n{processes.stdout}")
    if sys.platform == "darwin":
        loaded = ship.run(["launchctl", "dumpstate"], check=False)
        referenced = str(tree) in loaded.stdout or str(tree.resolve()) in loaded.stdout
        ship.save("launchd-probed", launchd_reference=referenced)
        if loaded.returncode or referenced:
            raise Blocked(f"Cleanup refused: loaded launchd reference or failed probe for {tree}")


def inspect_cleanup(ship: Ship, pr: dict[str, Any]) -> list[str]:
    from evallab.ship import Blocked

    branch, head = pr["headRefName"], pr["headRefOid"]
    if branch in {"main", "master"} or branch.startswith("integrate/"):
        raise Blocked("Refusing to retire an integration branch")
    inventory = ship.run(["git", "worktree", "list", "--porcelain", "-z"]).stdout
    targets = []
    for block in inventory.split("\0\0"):
        fields = {}
        for row in block.split("\0"):
            if row:
                key, _, value = row.partition(" ")
                fields[key] = value
        if fields.get("branch") != f"refs/heads/{branch}":
            continue
        tree = Path(fields["worktree"])
        if "locked" in fields or "prunable" in fields:
            raise Blocked(f"Cleanup refused: locked/prunable worktree {tree}")
        if fields.get("HEAD") != head:
            raise Blocked(f"Cleanup refused: unpushed or changed HEAD in {tree}")
        common = Path(ship.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"]).stdout.strip())
        if tree == common.parent:
            raise Blocked("Cleanup refused: branch is in the primary checkout")
        inspect_tree(ship, tree, head)
        targets.append(str(tree))
    local = ship.run(["git", "show-ref", "--verify", f"refs/heads/{branch}"], check=False)
    if local.returncode not in {0, 1, 128}:
        raise Blocked("Cannot inspect local branch")
    if local.returncode == 0 and local.stdout.split()[0] != head:
        raise Blocked("Cleanup refused: local branch has unpushed or changed commits")
    remote = ship.run(["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"]).stdout
    if remote and remote.split()[0] != head:
        raise Blocked("Cleanup refused: remote branch moved")
    return targets


def retire(ship: Ship, pr: dict[str, Any], targets: list[str]) -> None:
    # All safety checks precede the first deletion. Recheck immediately before removal.
    if inspect_cleanup(ship, pr) != targets:
        raise RuntimeError("Cleanup inventory changed")
    for tree in targets:
        ship.run(["git", "worktree", "remove", tree])
        ship.save("worktree-removed", removed_worktrees=ship.data.get("removed_worktrees", []) + [tree])
    branch = pr["headRefName"]
    remote = ship.run(["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"]).stdout
    if remote:
        ship.run(["git", "push", f"--force-with-lease=refs/heads/{branch}:{pr['headRefOid']}",
                  "origin", f":refs/heads/{branch}"])
    local = ship.run(["git", "show-ref", "--verify", f"refs/heads/{branch}"], check=False)
    if local.returncode == 0:
        # Squash merging does not establish ancestry. Delete only the exact recorded PR head.
        ship.run(["git", "update-ref", "-d", f"refs/heads/{branch}", pr["headRefOid"]])
    if ship.run(["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"]).stdout:
        raise RuntimeError("Remote branch still exists")
    if ship.run(["git", "show-ref", "--verify", f"refs/heads/{branch}"], check=False).returncode == 0:
        raise RuntimeError("Local branch still exists")
    ship.save("retired", retired_branch=branch)
