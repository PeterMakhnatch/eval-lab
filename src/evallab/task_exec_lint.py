"""Own the verifier-executes-artifact rule, screening verifier shell text statically.

Nothing here executes task content.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from evallab.shell_lint import logical_lines, shell_commands, split_shell

if TYPE_CHECKING:
    from evallab.task_lint import Finding


_PRIVILEGE_DROPS = frozenset({"setpriv", "runuser", "su", "gosu"})
_INTERPRETERS = frozenset({"node", "bash", "sh", "dash"})


def _executed_artifacts(
    command: list[str], artifacts: set[str], *, inline: bool = True
) -> Iterator[str]:
    """Yield one matching path per command, inspecting inline code one level deep."""
    if not command:
        return
    head = PurePosixPath(command[0]).name
    if head in _PRIVILEGE_DROPS:
        return
    if command[0].startswith("/") and any(
        command[0] == artifact or command[0].startswith(artifact + "/")
        for artifact in artifacts
    ):
        yield command[0]
        return
    if (
        head not in _INTERPRETERS
        and re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", head) is None
    ):
        return
    for operand in command[1:]:
        if not operand.startswith("-") and any(
            operand == artifact or operand.startswith(artifact + "/")
            for artifact in artifacts
        ):
            yield operand
            return
    if inline:
        for index, token in enumerate(command[1:], 1):
            if token in {"-c", "--command"} and index + 1 < len(command):
                for nested, _ in shell_commands(split_shell(command[index + 1])):
                    yield from _executed_artifacts(nested, artifacts, inline=False)


def lint_executes(task_dir: Path, artifacts: Sequence[str] = ()) -> list[Finding]:
    """Warn when verifier shell commands run declared agent-written artifacts."""
    # Imported here: task_lint imports this module while it loads.
    from evallab.task_lint import Finding

    findings: list[Finding] = []
    declared = {
        artifact.rstrip("/") or "/"
        for artifact in artifacts
        if isinstance(artifact, str) and artifact.startswith("/")
    }
    for path in sorted(task_dir.joinpath("tests").rglob("*.sh")):
        if not path.is_file():
            continue
        for number, line in logical_lines(
            path.read_text(encoding="utf-8", errors="replace")
        ):
            for command, _ in shell_commands(split_shell(line)):
                for matched in _executed_artifacts(command, declared):
                    findings.append(
                        Finding(
                            "verifier-executes-artifact",
                            "warning",
                            str(path),
                            f"line {number}: the verifier runs the agent-written artifact "
                            f"{matched} as root before grading; lock /logs/verifier and run "
                            "agent code as nobody with setpriv, runuser, su, or gosu so it "
                            "cannot rewrite the tests or the reward file",
                        )
                    )
    return findings
