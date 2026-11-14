"""Own the dropped-uid reward-write rule, screening verifier text statically.

Nothing here executes task content.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from evallab.shell_lint import logical_lines, shell_commands, split_shell

if TYPE_CHECKING:
    from evallab.task_lint import Finding

_REWARD_DIR = "/logs/verifier"
_REWARD_FILE = r"/logs/verifier/reward\.(?:txt|json)\b"
# A redirection or tee whose target is the reward file, or a copy whose final
# operand is that file.
_REWARD_WRITE = re.compile(rf"(?:>>?|\btee\b)[^\n;&|]*?{_REWARD_FILE}")
_REWARD_COPY = re.compile(
    rf"\b(?:cp|mv|install)\b[^\n;&|]*?\s{_REWARD_FILE}\s*(?:$|[;&|])"
)
_DROP_HEADS = frozenset({"setpriv", "runuser", "su", "gosu"})
_KILL_HEADS = frozenset({"pkill", "killall"})
_SETUID_CALL = re.compile(r"\.setuid\s*\(")
_SIGKILL = frozenset({"-9", "-KILL", "-SIGKILL", "-sKILL"})


def _head(command: list[str]) -> str:
    return PurePosixPath(command[0]).name if command else ""


def _locks_reward_dir(command: list[str]) -> bool:
    if _head(command) != "chmod":
        return False
    return any(
        operand == _REWARD_DIR or operand.startswith(_REWARD_DIR + "/")
        for operand in command[1:]
        if not operand.startswith("-")
    )


def _drops_privileges(command: list[str]) -> bool:
    return _head(command) in _DROP_HEADS


def _kills_dropped_uid(command: list[str]) -> bool:
    head = _head(command)
    if head in _KILL_HEADS:
        return True
    # kill -9 -1 sends SIGKILL to every process the caller may signal.
    return head == "kill" and "-1" in command[1:] and any(
        option in _SIGKILL for option in command[1:]
    )


def _writes_reward(line: str) -> bool:
    return bool(_REWARD_WRITE.search(line) or _REWARD_COPY.search(line))


def lint_dropped_uid_reward(task_dir: Path) -> list[Finding]:
    """Warn when a dropped-uid child can rewrite the reward after grading."""
    # Imported here: task_lint imports this module while it loads.
    from evallab.task_lint import Finding

    tests = task_dir / "tests"
    if not tests.is_dir():
        return []
    lock: tuple[Path, int] | None = None
    drop: tuple[Path, int] | None = None
    scripts: list[tuple[Path, list[tuple[int, str, list[list[str]]]]]] = []
    for path in sorted(item for item in tests.rglob("*") if item.is_file()):
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix != ".sh" and drop is None:
            match = _SETUID_CALL.search(text)
            if match is not None:
                drop = (path, text.count("\n", 0, match.start()) + 1)
        if path.suffix != ".sh":
            continue
        lines: list[tuple[int, str, list[list[str]]]] = []
        for number, line in logical_lines(text):
            commands = [command for command, _ in shell_commands(split_shell(line))]
            lines.append((number, line, commands))
            for command in commands:
                if lock is None and _locks_reward_dir(command):
                    lock = (path, number)
                if drop is None and _drops_privileges(command):
                    drop = (path, number)
        if lines:
            scripts.append((path, lines))
    if lock is None or drop is None:
        return []

    findings: list[Finding] = []
    for path, lines in scripts:
        writes = [number for number, line, _ in lines if _writes_reward(line)]
        if not writes:
            continue
        last_write = max(writes)
        kills_before = any(
            _kills_dropped_uid(command) and number < last_write
            for number, _, commands in lines
            for command in commands
        )
        if kills_before:
            continue
        findings.append(
            Finding(
                "dropped-uid-reward-write",
                "warning",
                str(path),
                f"line {last_write}: the verifier locks {_REWARD_DIR} at "
                f"{lock[0].relative_to(task_dir).as_posix()} line {lock[1]} and drops "
                "privileges for test code at "
                f"{drop[0].relative_to(task_dir).as_posix()} line {drop[1]}, but no "
                "pkill, killall, or kill -9 -1 precedes this reward write; on Docker "
                "Desktop the bind-mounted reward directory belongs to the host user, so "
                "the dropped child can rewrite the reward after grading; kill the dropped "
                "uid before the final reward write",
            )
        )
    return findings
