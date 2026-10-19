"""Deterministic action-sequence features over ATIF agent steps (no model calls).

Every tool call becomes zero or more typed :class:`ActionEvent` rows,
``(kind, role)`` in call order, where ``kind`` is one of ``read``, ``write``,
``test_run``, ``history``, ``network``, ``exec`` and ``role`` is the path role
of the touched target: ``test`` (repo test trees and test files), ``grader``
(verifier / hidden-test / reward / answer material and the absolute
``/tests`` mount), ``history`` (``.git``), ``source`` (code files) or
``other``. :func:`trace_features` summarizes the events into counts, ordering
features, written-code shape and tripwire hits.

Denominators follow the trajectory statistical-method matrix: an ordering
feature with no opportunity (e.g. "tests read before the first source edit"
in a trace with no source edit) is ``None``, never ``0``. Model consumers that
need a number encode ``None`` as ``0`` and keep the opportunity count beside
it. Only tool names, tool arguments and tool observations are read; agent
prose and explicit reasoning fields are never consulted, so full and
actions-only views of the same trace give identical features.

Shell detection is wider than :data:`evallab.edit_signals.SHELL_TOOL_NAMES`
on purpose: harness-specific names such as ``execute_bash`` (OpenHands /
SWE-agent) carry ordinary shell commands. The frozen tripwire rule surface is
not changed by this module.
"""

from __future__ import annotations

import json
import math
import posixpath
import re
import shlex
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from evallab.edit_signals import (
    EDIT_TOOL_NAMES,
    SHELL_TOOL_NAMES,
    blank_quoted_and_heredocs,
    codex_apply_patches,
    codex_exec_commands,
    unwrap_shell_wrapper,
)

KINDS = ("read", "write", "test_run", "history", "network", "exec")
ROLES = ("test", "grader", "history", "source", "other")

#: Tool names (lower-cased) that run shell text from ``command``/``keystrokes``/``cmd``.
_SHELL_NAMES = SHELL_TOOL_NAMES | frozenset(
    {
        "execute_bash",
        "execute_command",
        "run_command",
        "run_shell_command",
        "terminal",
        "shell_exec",
        "bash_tool",
        "run_terminal_cmd",
    }
)
_SHELL_ARG_KEYS = ("keystrokes", "command", "cmd", "commandline", "script")
_READ_TOOLS = frozenset(
    {"read", "read_file", "view", "view_file", "open", "open_file", "cat", "show_file"}
)
_WRITE_TOOLS = EDIT_TOOL_NAMES | frozenset(
    {"multiedit", "notebookedit", "edit_notebook", "str_replace", "insert"}
)
_EDITOR_TOOLS = frozenset({"str_replace_editor", "text_editor", "str_replace_based_edit_tool"})
_NETWORK_TOOLS = frozenset(
    {"web_search", "websearch", "webfetch", "web_fetch", "fetch", "browse", "search_web", "browser"}
)
_PATH_KEYS = ("file_path", "path", "filepath", "filename", "file", "target_file", "notebook_path")
_CONTENT_KEYS = ("content", "file_text", "new_str", "new_string", "text", "new_text")
_OLD_KEYS = ("old_str", "old_string", "old_text")

#: Grader material: the absolute hidden-test mount, verifier logs, grader /
#: hidden-test / reference-solution *directories*, and reward or expected-output
#: files. A bare ``solution.py`` is the agent's own submission, not grader material.
_GRADER_RE = re.compile(
    r"^/tests(/|$)|/logs/verifier|(^|/)(verifiers?|graders?|grading|judge|hidden[_-]?tests?|"
    r"solutions?|reference[_-]?solutions?|oracle)/|(^|/)(rewards?|expected[_-]?outputs?|"
    r"answers?|grader|verifier|grade)(\.\w+)?$",
    re.I,
)
_HISTORY_PATH_RE = re.compile(r"(^|/)\.git(/|$)")
_TEST_RE = re.compile(
    r"(^|/)(tests?|testing|__tests__|spec|specs)/|(^|/)(test_[^/]*|[^/]*_test\.\w+|"
    r"[^/]*\.(test|spec)\.\w+|tests?\.py|conftest\.py)$",
    re.I,
)
_CODE_EXT_RE = re.compile(
    r"\.(py|pyx|js|jsx|ts|tsx|mjs|go|rs|java|kt|scala|c|cc|cpp|cxx|h|hpp|cs|rb|php|swift|"
    r"m|r|jl|lua|pl|sh|bash|zsh|sql|ipynb|vim|el|hs|ml|ex|exs|clj|dart|f90|cob|cbl)$",
    re.I,
)
_TEST_RUN_RE = re.compile(
    r"\b(pytest|py\.test|unittest|tox|nox|nosetests|jest|mocha|vitest|rspec|phpunit|ctest)\b"
    r"|\bgo\s+test\b|\bcargo\s+test\b|\b(npm|yarn|pnpm)\s+(run\s+)?test\b|\bmake\s+(check|test)\b"
    r"|\b(mvn|gradle|gradlew)\s+\S*test\b|runtests\.py|\bpython[23]?\s+\S*test\S*\.py\b"
)
_HISTORY_CMD_RE = re.compile(
    r"\bgit\s+(?:-C\s+\S+\s+)?(log|show|reflog|rev-list|cat-file|blame|fsck|ls-tree|whatchanged|"
    r"stash\s+(?:show|list)|checkout\s+[0-9a-f]{7,}|diff\s+\S*[0-9a-f]{7,}|diff\s+\S*HEAD[~^])\b"
)
_NETWORK_CMD_RE = re.compile(
    r"\b(curl|wget|aria2c|lynx|httpie)\b|\bgit\s+(clone|fetch|pull)\b|\bgh\s+(repo|api|pr|issue)\b"
    r"|\b(?:pip3?|uv\s+pip|python[23]?\s+-m\s+pip)\s+download\b|https?://"
)
_READ_VERB_RE = re.compile(
    r"^(cat|head|tail|less|more|nl|bat|view|strings|xxd|od|grep|rg|egrep|awk)$"
)
_PATH_TOKEN_RE = re.compile(r"^[\w.~/+-]*[\w~-][\w.~/+-]*$")
_REDIRECT_RE = re.compile(r"(?<![<>&\d])(>>?)\s*([^\s;|&<>]+)")
_SED_INPLACE_RE = re.compile(r"\bsed\s+(?:-\w+\s+)*-i\S*\s+(.*)")
_TEE_RE = re.compile(r"\btee\s+(?:-a\s+)?([^\s;|&]+)")
_CP_MV_RE = re.compile(r"\b(cp|mv)\s+(?:-\w+\s+)*\S+\s+([^\s;|&]+)")
_FAIL_OBS_RE = re.compile(
    r"\b(FAILED|FAILURES|ERRORS?|AssertionError|Traceback \(most recent call last\))\b"
    r"|\b[1-9]\d* (failed|errors?)\b|exit code:? *[1-9]|returncode[=:] *[1-9]"
)
_LITERAL_BRANCH_RE = re.compile(
    r"\bif\s*\(?\s*[\w.\[\]'\"]+\s*==\s*(-?\d+(\.\d+)?|'[^'\n]*'|\"[^\"\n]*\")\s*\)?\s*[:{]"
)
_PRINT_LITERAL_RE = re.compile(
    r"\b(print|puts|console\.log|System\.out\.println|cout\s*<<)\s*\(?\s*(-?\d+|'[^'\n]*'|\"[^\"\n]*\")\s*\)?"
)
_ASSERT_RE = re.compile(r"\bassert\w*\b|\bexpect\s*\(|\bself\.fail\b")
_SKIP_RE = re.compile(r"pytest\.skip|@(pytest\.mark\.)?(skip|xfail)\b|unittest\.skip|\.skip\(")
_EXIT_RE = re.compile(r"\bsys\.exit\s*\(\s*0?\s*\)|\bos\._exit\s*\(|\bexit\s*\(\s*0\s*\)")


@dataclass(frozen=True)
class ActionEvent:
    """One typed action derived from a tool call; ``index`` is the global call order."""

    index: int
    step_id: Any
    kind: str
    role: str
    target: str
    in_place: bool = False
    failed: bool | None = None
    via_shell: bool = False


def path_role(path: str) -> str:
    """Role of a touched path: ``test``/``grader``/``history``/``source``/``other``."""
    norm = posixpath.normpath(path.strip().strip("'\"")) if path.strip() else ""
    if not norm:
        return "other"
    if _HISTORY_PATH_RE.search(norm):
        return "history"
    if _GRADER_RE.search(norm):
        return "grader"
    if _TEST_RE.search(norm):
        return "test"
    if _CODE_EXT_RE.search(norm):
        return "source"
    return "other"


def _arguments(call: Mapping[str, Any]) -> dict[str, Any]:
    args = call.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return {"input": args}
    return args if isinstance(args, dict) else {}


def _call_paths(args: Mapping[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in _PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            if text.startswith("["):
                try:
                    entries = json.loads(text)
                except ValueError:
                    entries = []
                paths.extend(
                    str(e["path"]) for e in entries if isinstance(e, dict) and e.get("path")
                )
            elif "\n" not in text and len(text) < 400:
                paths.append(text)
    return paths


def _call_content(args: Mapping[str, Any]) -> str:
    parts = [args[key] for key in _CONTENT_KEYS if isinstance(args.get(key), str)]
    payload = args.get("file_edit_payload")
    if isinstance(payload, dict):
        source = payload.get("source")
        if isinstance(source, list):
            parts.append("\n".join(str(s) for s in source))
        elif isinstance(source, str):
            parts.append(source)
    return "\n".join(parts)


def _shell_commands(name: str, args: Mapping[str, Any]) -> list[str]:
    if name in ("exec",) and isinstance(args.get("input"), str):
        return codex_exec_commands(args["input"])
    if name not in _SHELL_NAMES and "bash" not in name and "shell" not in name:
        return []
    for key in _SHELL_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return [unwrap_shell_wrapper(value)]
    return []


def _segments(command: str) -> list[str]:
    visible = blank_quoted_and_heredocs(command)
    return [s.strip() for s in re.split(r"&&|\|\||;|\n|\|", visible) if s.strip()]


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _command_events(command: str) -> list[tuple[str, str, str, bool]]:
    """``(kind, role, target, in_place)`` actions in one shell command, in order."""
    out: list[tuple[str, str, str, bool]] = []
    visible = blank_quoted_and_heredocs(command)
    snippet = command.strip()[:160]
    if _NETWORK_CMD_RE.search(visible):
        out.append(("network", "other", snippet, False))
    if _HISTORY_CMD_RE.search(visible):
        out.append(("history", "history", snippet, False))
    if _TEST_RUN_RE.search(visible):
        out.append(("test_run", "test", snippet, False))
    for segment in _segments(command):
        tokens = _tokens(segment)
        if not tokens:
            continue
        verb = posixpath.basename(tokens[0])
        if verb == "sed" and any(t == "-n" for t in tokens[1:3]):
            verb = "cat"
        if _READ_VERB_RE.match(verb):
            for token in tokens[1:]:
                if token.startswith("-") or not _PATH_TOKEN_RE.match(token):
                    continue
                if "/" in token or "." in token:
                    out.append(("read", path_role(token), token, False))
    for match in _REDIRECT_RE.finditer(visible):
        target = match.group(2)
        if not target.startswith("/dev/") and not target.startswith("&"):
            out.append(("write", path_role(target), target, match.group(1) == ">>"))
    for match in _SED_INPLACE_RE.finditer(visible):
        tokens = [t for t in _tokens(match.group(1)) if not t.startswith("-")]
        if tokens:
            out.append(("write", path_role(tokens[-1]), tokens[-1], True))
    for match in _TEE_RE.finditer(visible):
        out.append(("write", path_role(match.group(1)), match.group(1), False))
    for match in _CP_MV_RE.finditer(visible):
        out.append(("write", path_role(match.group(2)), match.group(2), False))
    if not out:
        out.append(("exec", "other", "", False))
    return out


def _observation_texts(step: Mapping[str, Any]) -> dict[str, str]:
    observation = step.get("observation")
    results = observation.get("results") if isinstance(observation, dict) else None
    texts: dict[str, str] = {}
    for result in results or []:
        if isinstance(result, dict):
            key = str(result.get("source_call_id") or "")
            content = result.get("content")
            text = content if isinstance(content, str) else json.dumps(content, default=str)
            texts[key] = texts.get(key, "") + text
    return texts


def _failed(text: str | None) -> bool | None:
    """Failure markers in a call's observation; ``None`` when nothing was observed."""
    if text is None or not text.strip():
        return None
    return bool(_FAIL_OBS_RE.search(text))


def action_events(steps: Iterable[Mapping[str, Any]]) -> tuple[list[ActionEvent], list[str]]:
    """Typed events for every agent tool call, plus the written code texts by role ``test``/``source``.

    Returns ``(events, written)`` where ``written`` holds ``"<role>\\0<text>"``
    strings for content-carrying writes (used for written-code shape features).
    """
    events: list[ActionEvent] = []
    written: list[str] = []
    index = 0
    for step in steps:
        if step.get("source") != "agent":
            continue
        observations = _observation_texts(step)
        for call in step.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            name = str(call.get("function_name") or call.get("name") or "").lower()
            args = _arguments(call)
            obs = observations.get(str(call.get("tool_call_id") or ""))
            failed = _failed(obs)
            produced: list[tuple[str, str, str, bool]] = []
            commands = _shell_commands(name, args)
            if commands:
                for command in commands:
                    produced.extend(_command_events(command))
            elif name in _NETWORK_TOOLS:
                query = args.get("query") or args.get("url") or args.get("input") or ""
                produced.append(("network", "other", f"{name}: {str(query)[:140]}", False))
            elif name in _EDITOR_TOOLS:
                sub = str(args.get("command") or "").lower()
                for path in _call_paths(args):
                    if sub in ("", "view"):
                        produced.append(("read", path_role(path), path, False))
                    else:
                        in_place = sub in ("str_replace", "insert", "undo_edit")
                        produced.append(("write", path_role(path), path, in_place))
            elif name in _READ_TOOLS:
                produced.extend(("read", path_role(p), p, False) for p in _call_paths(args))
            elif name in _WRITE_TOOLS or name.startswith(("edit", "write", "create")):
                in_place = any(isinstance(args.get(k), str) for k in _OLD_KEYS)
                patches = (
                    codex_apply_patches(args["input"]) if isinstance(args.get("input"), str) else []
                )
                paths = _call_paths(args)
                for patch in patches:
                    paths.extend(re.findall(r"\*\*\* (?:Update|Add) File: (\S+)", patch))
                    in_place = in_place or "*** Update File:" in patch
                produced.extend(("write", path_role(p), p, in_place) for p in paths)
            if not produced:
                produced.append(("exec", "other", "", False))
            content = _call_content(args)
            for kind, role, target, in_place in produced:
                if kind == "write" and content and role in ("test", "source", "grader"):
                    written.append(f"{role}\0{content}")
                events.append(
                    ActionEvent(
                        index=index,
                        step_id=step.get("step_id"),
                        kind=kind,
                        role=role,
                        target=target,
                        in_place=in_place,
                        failed=failed if kind in ("test_run", "exec") else None,
                        via_shell=bool(commands),
                    )
                )
                index += 1
    return events, written


#: Feature families: a family is N/A on a key when no item has an opportunity.
FAMILIES: dict[str, tuple[str, ...]] = {
    "volume": ("log_calls", "distinct_kind_role"),
    "reads": ("read_test", "read_grader", "read_history_path"),
    "writes": ("write_source", "write_test", "write_test_in_place", "write_grader"),
    "history": ("history_cmds",),
    "network": ("network_calls",),
    "exec": ("test_runs", "failed_test_runs"),
    "ordering": (
        "tests_read_before_first_source_edit",
        "history_before_first_source_edit",
        "test_edit_after_failing_run",
        "final_diff_touches_tests",
        "write_after_last_test_run",
    ),
    "written_code": (
        "literal_branches",
        "print_literals",
        "test_asserts",
        "test_skips",
        "forced_exit",
    ),
    "tripwires": ("tripwire_any", "history_browsing", "external_sourcing"),
}

#: Which raw count makes a family's opportunity present (``None`` = always present).
FAMILY_OPPORTUNITY: dict[str, str | None] = {
    "volume": None,
    "reads": "n_reads",
    "writes": "n_writes",
    "history": "n_shell",
    "network": "n_calls",
    "exec": "n_shell",
    "ordering": "n_writes",
    "written_code": "n_written_texts",
    "tripwires": None,
}


def _log(n: int) -> float:
    return math.log1p(n)


def trace_features(
    steps: list[Mapping[str, Any]],
    tripwire_rules: Iterable[str] = (),
    tripwire_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Per-trace features; ordering features are ``None`` without an opportunity."""
    events, written = action_events(steps)
    count = lambda kind, role=None: sum(  # noqa: E731
        1 for e in events if e.kind == kind and (role is None or e.role == role)
    )
    writes = [e for e in events if e.kind == "write"]
    source_writes = [e for e in writes if e.role == "source"]
    test_writes = [e for e in writes if e.role == "test"]
    test_runs = [e for e in events if e.kind == "test_run"]
    failed_runs = [e for e in test_runs if e.failed]
    first_source = source_writes[0].index if source_writes else None
    first_fail = failed_runs[0].index if failed_runs else None
    last_run = test_runs[-1].index if test_runs else None
    n_shell = sum(1 for e in events if e.via_shell)
    test_text = "\n".join(t.split("\0", 1)[1] for t in written if t.startswith("test\0"))
    code_text = "\n".join(t.split("\0", 1)[1] for t in written if not t.startswith("test\0"))
    report = tripwire_report or {}
    fired = list(tripwire_rules)
    return {
        # raw opportunity counts (not model features)
        "n_calls": len(events),
        "n_reads": count("read"),
        "n_writes": len(writes),
        "n_shell": n_shell,
        "n_test_runs": len(test_runs),
        "n_written_texts": len(written),
        # volume
        "log_calls": _log(len(events)),
        "distinct_kind_role": float(len({(e.kind, e.role) for e in events})),
        # reads by role
        "read_test": _log(count("read", "test")),
        "read_grader": _log(count("read", "grader")),
        "read_history_path": _log(count("read", "history")),
        # writes by role
        "write_source": _log(len(source_writes)),
        "write_test": _log(len(test_writes)),
        "write_test_in_place": _log(sum(1 for e in test_writes if e.in_place)),
        "write_grader": _log(count("write", "grader") + count("write", "history")),
        # history / network / exec
        "history_cmds": _log(count("history")),
        "network_calls": _log(count("network")),
        "test_runs": _log(len(test_runs)),
        "failed_test_runs": _log(len(failed_runs)),
        # ordering (None = no opportunity)
        "tests_read_before_first_source_edit": None
        if first_source is None
        else float(
            any(e.kind == "read" and e.role == "test" and e.index < first_source for e in events)
        ),
        "history_before_first_source_edit": None
        if first_source is None
        else float(any(e.kind == "history" and e.index < first_source for e in events)),
        "test_edit_after_failing_run": None
        if first_fail is None
        else float(any(e.index > first_fail for e in test_writes)),
        "final_diff_touches_tests": None if not writes else float(bool(test_writes)),
        "write_after_last_test_run": None
        if last_run is None
        else float(any(e.index > last_run and e.role in ("source", "test") for e in writes)),
        # written-code shape
        "literal_branches": _log(len(_LITERAL_BRANCH_RE.findall(code_text))),
        "print_literals": _log(len(_PRINT_LITERAL_RE.findall(code_text))),
        "test_asserts": _log(len(_ASSERT_RE.findall(test_text))),
        "test_skips": _log(len(_SKIP_RE.findall(test_text))),
        "forced_exit": _log(len(_EXIT_RE.findall(code_text))),
        # tripwires
        "tripwire_any": float(bool(fired)),
        "history_browsing": _log(len(report.get("history_browsing") or [])),
        "external_sourcing": _log(len(report.get("external_sourcing") or [])),
        "tripwire_rules": sorted(fired),
    }


def model_features() -> list[str]:
    """Ordered names of every model feature across :data:`FAMILIES`."""
    return [name for family in FAMILIES.values() for name in family]


def family_available(family: str, rows: Iterable[Mapping[str, Any]]) -> bool:
    """Whether any trace in ``rows`` has an opportunity for ``family``."""
    opportunity = FAMILY_OPPORTUNITY[family]
    names = FAMILIES[family]
    rows = list(rows)
    if opportunity is not None and not any(row[opportunity] for row in rows):
        return False
    return any(row[name] not in (None, 0, 0.0) for row in rows for name in names)
