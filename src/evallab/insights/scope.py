"""Worker read-scope defaults, prompt text and deterministic session audit."""

from __future__ import annotations

import json
import os
import re
import shlex
from collections.abc import Iterator, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from evallab.insights.config import EngineConfig
from evallab.insights.models import ScopeAudit

DEFAULT_MIRROR = Path("~/Developer/eval-lab/.worktrees/insights-readonly")
_PATH_KEYS = frozenset({"path", "paths", "file", "files", "dir", "directory", "root", "folder"})
_COMMAND_KEYS = frozenset({"command", "cmd", "script", "code", "keystrokes"})
_WRITE_TOOLS = frozenset({"write", "write_file", "edit", "ast_edit", "memory_edit"})


def _resolved(value: str | Path) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser().resolve(strict=False)


def _glob_root(pattern: str) -> Path:
    expanded = os.path.expandvars(pattern)
    wildcard = min((index for char in "*?[" if (index := expanded.find(char)) >= 0),
                   default=len(expanded))
    raw_prefix = expanded[:wildcard]
    prefix = Path(raw_prefix).expanduser()
    # A prefix ending in a partial filename contributes its parent, not that filename.
    return (prefix if raw_prefix.endswith(("/", os.sep)) else prefix.parent).resolve(strict=False)


def allowed_read_roots(config: EngineConfig, pass_dir: Path) -> list[Path]:
    roots = [_resolved(pass_dir), _resolved(DEFAULT_MIRROR)]
    for family in config.sources:
        roots.extend(_glob_root(pattern) for pattern in family.globs)
    roots.extend(_resolved(override.path).parent for override in config.overrides)
    if config.repo_mirror is not None:
        roots.append(_resolved(config.repo_mirror.path))
    roots.extend(_resolved(root) for root in config.allowed_read_roots)
    return sorted(set(roots), key=str)


def scope_prompt(roots: Sequence[Path]) -> str:
    listed = "\n".join(f"- {root}" for root in roots)
    return (
        "\n## Filesystem read scope\n"
        "Read only your assigned pass directory and these source roots. Do not read the live "
        "state directory, any other pass directory, engine files, or paths outside this list. "
        "Use ./q for ledger evidence; it reads your sealed snapshot.\n"
        f"{listed}\n"
        "The engine audits absolute and ~ paths in non-write tool calls at finalize.\n"
    )


def _objects(value: object) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


def _tool_calls(event: dict[str, Any]) -> Iterator[tuple[str, object]]:
    for obj in _objects(event):
        function = obj.get("function")
        function = function if isinstance(function, dict) else {}
        raw_name = obj.get(
            "toolName",
            obj.get(
                "tool_name",
                obj.get("function_name", obj.get("tool", obj.get("name", function.get("name")))),
            ),
        )
        if not isinstance(raw_name, str):
            continue
        arguments = obj.get("arguments", obj.get("args", obj.get("input", function.get("arguments"))))
        if arguments is None:
            continue
        name = raw_name.rsplit("/", 1)[-1].lower()
        if name in _WRITE_TOOLS:
            continue
        if isinstance(arguments, str):
            with suppress(ValueError):
                arguments = json.loads(arguments)
        yield name, arguments


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _shell_paths(command: str) -> Iterator[str]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    for token in tokens:
        # Recipe JSON pointers are not filesystem paths. Inspect only actual path
        # fields in structured arguments; plain absolute shell tokens are paths.
        if token.startswith(("{", "[")):
            try:
                value = json.loads(token)
            except ValueError:
                continue
            yield from _argument_paths(value)
            continue
        for match in re.finditer(r"(?:^|[\s>=<(])(~[^\s;|&<>]*|/[^\s;|&<>]*)", token):
            path = match.group(1).strip("'\"),]")
            if path and path != "/":
                yield path


def _argument_paths(value: object, key: str = "") -> Iterator[str]:
    if isinstance(value, dict):
        for child_key, child in value.items():
            lowered = str(child_key).lower()
            yield from _argument_paths(child, lowered)
    elif isinstance(value, list):
        for child in value:
            yield from _argument_paths(child, key)
    elif isinstance(value, str):
        is_path = key in _PATH_KEYS or key.endswith(("_path", "_paths", "_dir", "_root", "_file"))
        if is_path and (value.startswith(("/", "~"))):
            yield value
        elif key in _COMMAND_KEYS:
            yield from _shell_paths(value)


def audit_session(
    session_dir: Path,
    *,
    state_root: Path,
    pass_dir: Path,
    roots: Sequence[Path],
) -> ScopeAudit:
    """Find unique out-of-scope reads; malformed/running JSONL lines are ignored."""
    state = _resolved(state_root)
    own = _resolved(pass_dir)
    allowed = [_resolved(root) for root in roots]
    violations: dict[tuple[str, str], str] = {}
    for jsonl in sorted(session_dir.rglob("*.jsonl")):
        try:
            stream = jsonl.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                for tool, arguments in _tool_calls(event):
                    candidates = (
                        _shell_paths(arguments) if isinstance(arguments, str)
                        else _argument_paths(arguments)
                    )
                    for raw in candidates:
                        path = _resolved(raw)
                        in_state = _within(path, state) and not _within(path, own)
                        outside = not any(_within(path, root) for root in allowed)
                        if in_state or outside:
                            key = (tool, str(path))
                            violations[key] = f"{tool}: {path}"
    examples = [violations[key] for key in sorted(violations)[:5]]
    return ScopeAudit(count=len(violations), examples=examples)
