"""Cheap delivery refusals before spending time on CI."""
from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any

from evallab.ship.cleanup import process_probe, topic_worktrees, unknown_ignored

if TYPE_CHECKING:
    from evallab.ship import Ship

# These control delivery itself. Gate commands run from exact-head checkouts,
# not from the delivery checkout, so unrelated application paths are excluded.
DELIVERY_INPUTS = (
    "src/evallab/ship", "src/evallab/entry.py",
    "agents/ship-gates.toml", "agents/CHECKS.md",
)


def fresh_delivery_command(ship: Ship, topic: Path | None = None, *, preflight_only: bool = False) -> str:
    quote = shlex.quote
    directory = ship.directory
    if topic is not None and directory.is_relative_to(topic.resolve()):
        directory = Path.home() / ".local/state/evallab-wf/ship"
    command = (f"mkdir -p {quote(str(directory.parent))} && "
               f"delivery=$(mktemp -d {quote(str(directory.parent / 'delivery.XXXXXX'))}) && "
               f"git -C {quote(str(ship.root))} worktree add --detach \"$delivery\" origin/main && "
               "uv sync --directory \"$delivery\" --locked --no-group observability --group benchmarks --group inspect && "
               f"cd {quote(str(directory.parent))} && uv run --directory \"$delivery\" --no-sync "
               f"evallab ship {ship.pr} --state-dir {quote(str(directory))} --no-rerun-failed-once")
    if ship.smoke:
        command += f" --smoke {quote(ship.smoke)}"
    if preflight_only:
        command += " --preflight-only"
    return command


def inspect_preflight(ship: Ship, pr: dict[str, Any], *, preflight_only: bool = False) -> None:
    from evallab.ship import Blocked

    quote = shlex.quote
    resume = (f"cd {quote(str(ship.directory.parent))} && uv run --directory {quote(str(ship.root))} "
              f"--no-sync evallab ship {ship.pr} --state-dir {quote(str(ship.directory))} "
              f"--no-rerun-failed-once")
    if ship.smoke:
        resume += f" --smoke {quote(ship.smoke)}"
    if preflight_only:
        resume += " --preflight-only"
    targets = topic_worktrees(ship, pr["headRefName"])
    for fields in targets:
        tree = Path(fields["worktree"])
        for label, location in (("ship working directory", Path.cwd()), ("delivery checkout", ship.root),
                                ("receipt directory", ship.directory)):
            if location.resolve().is_relative_to(tree.resolve()):
                raise Blocked(f"Delivery preflight refused: {label} uses {tree}; "
                              f"start ship outside the topic: {fresh_delivery_command(ship, tree, preflight_only=preflight_only)}")
    changed = ship.run(["git", "diff", "--name-only", "origin/main", "--", *DELIVERY_INPUTS]).stdout.splitlines()
    untracked = ship.run(["git", "ls-files", "--others", "--exclude-standard", "--", *DELIVERY_INPUTS]).stdout.splitlines()
    runtime_paths = ship.run(["git", "ls-tree", "-r", "--name-only", "origin/main", "--",
                              "src/evallab/ship", "src/evallab/entry.py"]).stdout.splitlines()
    for relative in runtime_paths:
        if not relative.endswith(".py"):
            continue
        source = Path(__file__).parent.parent / Path(relative).relative_to("src/evallab")
        expected = ship.run(["git", "rev-parse", f"origin/main:{relative}"]).stdout.strip()
        actual = ship.run(["git", "hash-object", "--", str(source)], check=False)
        if actual.returncode or actual.stdout.strip() != expected:
            changed.append(f"runtime:{relative}")
    if changed or untracked:
        paths = ", ".join(sorted(set(changed + untracked)))
        # A fresh detached checkout preserves local edits and stale commits.
        repair = fresh_delivery_command(ship, preflight_only=preflight_only)
        raise Blocked(f"Delivery preflight refused: delivery inputs differ from origin/main: {paths}; use a fresh delivery checkout: {repair}")
    for fields in targets:
        tree = Path(fields["worktree"])
        probe = process_probe(ship, tree)
        if probe.returncode != 1 or probe.stdout.strip() or probe.stderr.strip():
            # lsof's table includes PID, command and FD. Keep every cwd owner.
            owners = []
            for row in probe.stdout.splitlines():
                columns = row.split()
                if len(columns) >= 4 and columns[1].isdigit() and columns[3] == "cwd":
                    owners.append(f"process {columns[1]} ({columns[0]})")
            problem = ", ".join(owners) + f" uses {tree}" if owners else f"active process or failed lsof probe for {tree}"
            if ship.root == tree or tree in ship.root.parents:
                resume = fresh_delivery_command(ship, preflight_only=preflight_only)
            raise Blocked(f"Delivery preflight refused: {problem}; start ship outside the topic: {resume}")
        unknown = unknown_ignored(ship, tree)
        if unknown:
            # Move, never delete, unclassified bytes. A unique destination avoids
            # replacing a prior recovery copy. Preserve relative paths, too.
            commands = []
            tool_env = "tools/mimoagent-harbor/.venv"
            tool_files = [name for name in unknown if name == tool_env or name.startswith(tool_env + "/")]
            move_paths = unknown
            if tool_files and not ship.run(["git", "ls-files", "--", tool_env], cwd=tree).stdout.strip():
                move_paths = [name for name in unknown if name not in tool_files] + [tool_env]
            for name in move_paths:
                source = tree / name
                commands.append(f"mkdir -p \"$backup\"/{quote(str(Path(name).parent))} && "
                                f"mv {quote(str(source))} \"$backup\"/{quote(name)}")
            repair = f"backup=$(mktemp -d {quote(str(ship.directory / 'preserved.XXXXXX'))}) && " + " && ".join(commands)
            known = " (includes locked tool environment tools/mimoagent-harbor/.venv; preserve unverified local bytes)" if any(
                name == tool_env or name.startswith(tool_env + "/") for name in unknown) else ""
            raise Blocked(f"Delivery preflight refused: ignored files in {tree}: "
                          f"{', '.join(quote(name) for name in unknown)}{known}; preserve them: {repair}")
    ship.save("delivery-preflight-passed")
