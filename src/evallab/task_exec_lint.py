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
_PYTHON_HEAD = re.compile(r"python(?:[0-9]+(?:\.[0-9]+)*)?")
_INLINE_FLAGS = frozenset({"-c", "--command"})


def _matches_artifact(operand: str, artifacts: set[str]) -> bool:
    return any(
        operand == artifact or operand.startswith(artifact + "/")
        for artifact in artifacts
    )


def _artifact_operands(command: list[str], artifacts: set[str]) -> Iterator[str]:
    if not command:
        return
    head = PurePosixPath(command[0]).name
    if head in _PRIVILEGE_DROPS:
        return
    if command[0].startswith("/") and _matches_artifact(command[0], artifacts):
        yield command[0]
        return
    if head not in _INTERPRETERS and _PYTHON_HEAD.fullmatch(head) is None:
        return
    for operand in command[1:]:
        if operand.startswith("-") or not _matches_artifact(operand, artifacts):
            continue
        yield operand
        return


def _inline_body(command: list[str]) -> str | None:
    for index, token in enumerate(command[1:], 1):
        if token in _INLINE_FLAGS and index + 1 < len(command):
            return command[index + 1]
    return None


def _executed_artifacts(command: list[str], artifacts: set[str]) -> Iterator[str]:
    yield from _artifact_operands(command, artifacts)
    if not command:
        return
    if _PYTHON_HEAD.fullmatch(PurePosixPath(command[0]).name) is None:
        return
    body = _inline_body(command)
    if body is None:
        return
    for nested, _separator in shell_commands(split_shell(body)):
        yield from _artifact_operands(nested, artifacts)


def lint_executes(task_dir: Path, artifacts: Sequence[str] = ()) -> list[Finding]:
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
        reported: set[tuple[int, str]] = set()
        for number, line in logical_lines(
            path.read_text(encoding="utf-8", errors="replace")
        ):
            for command, _separator in shell_commands(split_shell(line)):
                for matched in _executed_artifacts(command, declared):
                    pair = (number, matched)
                    if pair in reported:
                        continue
                    reported.add(pair)
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
