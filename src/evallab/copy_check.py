"""Deterministic copy check for a finished trial (HAR-143).

A pass is ``copied`` when lines the agent added (``verifier/agent.diff``)
match code the agent read from outside the base checkout before it wrote
them. "Outside" is any command that reads or obtains code that is not the
task's own source at the base commit:

* downloaded or unpacked packages (``pip download``/``install``, wheels,
  tarballs, ``unzip``/``tar x``, files under ``/tmp/<dir>/``);
* build outputs and installed copies (``build/lib``, ``site-packages``,
  ``dist-packages``, ``*.egg-info``, the interpreter's ``lib/python*``);
* git objects other than the working tree (``git show``/``cat-file``/
  ``clone``/``fetch``, ``git log -p``, ``git diff <rev>``);
* network reads (``curl``, ``wget``).

The agent's own text never counts as outside: lines it typed in any command
up to that step, and terminal echo of heredoc input (``> `` continuation
lines after a prompt that opened a heredoc or quote), are removed first.
Only substantive added lines in non-test files are compared (at least
``MIN_LINE_CHARS`` characters after whitespace folding, not bare keywords
or punctuation). A trial is ``copied`` at ``MIN_MATCHED_LINES`` matched
lines. No model judgment.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

RULE = "copy_check/v1"
MIN_MATCHED_LINES = 5
MIN_LINE_CHARS = 16

_OUTSIDE = re.compile(
    r"build/lib|site-packages|dist-packages|\.whl\b|\.tar\.gz|\.tgz\b|\.zip\b"
    r"|\bpip3? (?:download|show|install)|\buv pip\b"
    r"|\bgit (?:show|cat-file|fetch|clone)\b|\bgit log\b[^\n;|&]*\s-p\b"
    r"|\bgit diff\b[^\n;|&]*\b(?:origin|FETCH_HEAD|[0-9a-f]{7,40})\b"
    r"|\bcurl\b|\bwget\b|/root/\.cache|/usr(?:/local)?/lib/python|\.egg-info"
    r"|\bunzip\b|\btar\b[^\n]*\s-?x"
    r"|/tmp/\S*/\S+\.(?:py|pyi|txt|cfg|toml|md|rst|json)\b"
)
_TEST_FILE = re.compile(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*\.py$|_test\.py$")
_TRIVIAL = re.compile(
    r"^[\W_]*$|^(?:return|pass|else:|try:|finally:|break|continue|\)|\]|\}|\"\"\"|''')\W*$"
)
#: grep -n / grep -rn, cat -n, then diff markers; each stripped once, in order.
_PREFIXES = (
    re.compile(r"^[^\s:]+:\d+[:\-]"),
    re.compile(r"^\s*\d+(?:\t| {2,}|:|-|$)"),
    re.compile(r"^[+\-<>!](?: |\t|$)"),
)
_PROMPT = re.compile(r"^\S*@\S+:.*[#$] ")
_SPILL = re.compile(r"/logs/agent/evallab-output/(step-\d{4,}\.txt)")


def _fold(line: str) -> str:
    return re.sub(r"\s+", " ", line.rstrip("$").strip())


def _strip_prefixes(line: str) -> str:
    for prefix in _PREFIXES:
        line = prefix.sub("", line, count=1)
    return _fold(line)


def added_lines(diff_text: str) -> set[str]:
    """Substantive lines the agent added to non-test files."""
    out: set[str] = set()
    path = None
    for line in diff_text.splitlines():
        if line.startswith("+++"):
            path = line[4:].strip()
            continue
        if not line.startswith("+") or path is None or _TEST_FILE.search(path):
            continue
        folded = _fold(line[1:])
        if (
            len(folded) >= MIN_LINE_CHARS
            and not _TRIVIAL.match(folded)
            and not folded.startswith("#!")
        ):
            out.add(folded)
    return out


def _typed(command: str) -> set[str]:
    """Every way the agent's own command text can show up as a line."""
    out: set[str] = set()
    unescaped = command.replace("\\n", "\n").replace("\\t", "\t")
    for line in f"{command}\n{unescaped}".splitlines():
        out.add(_fold(line))
        out.add(_strip_prefixes(line))
        out.update(_fold(piece) for piece in re.split(r"[\"']{1,3}", line))
    return out


def _output_lines(text: str) -> Iterable[str]:
    """Terminal output lines minus heredoc/quote continuation echo."""
    echo = False
    for line in text.splitlines():
        if _PROMPT.match(line):
            echo = (
                "<<" in line
                or line.rstrip().endswith("\\")
                or line.count('"') % 2 == 1
                or line.count("'") % 2 == 1
            )
            continue
        if echo and line.startswith(">"):
            continue
        echo = False
        yield line


def _agent_steps(trial_dir: Path) -> list[tuple[Any, str, str]]:
    """(step_id, commands, observation incl. spilled full output) per agent step."""
    try:
        trajectory = json.loads(
            (trial_dir / "agent" / "trajectory.json").read_text(errors="replace")
        )
    except (OSError, json.JSONDecodeError):
        return []
    spill_dir = trial_dir / "agent" / "evallab-output"
    steps = []
    for step in trajectory.get("steps") or []:
        if not isinstance(step, dict) or step.get("source") != "agent":
            continue
        commands = []
        for call in step.get("tool_calls") or []:
            arguments = (call or {}).get("arguments") or {}
            command = arguments.get("keystrokes") or arguments.get("command")
            commands.append(command if isinstance(command, str) else json.dumps(arguments))
        results = ((step.get("observation") or {}).get("results")) or []
        observation = "\n".join(str((result or {}).get("content", "")) for result in results)
        for name in dict.fromkeys(_SPILL.findall(observation)):
            with contextlib.suppress(OSError):
                observation += "\n" + (spill_dir / name).read_text(errors="replace")
        steps.append((step.get("step_id"), "\n".join(commands), observation))
    return steps


def copy_check(trial_dir: Path) -> dict[str, Any] | None:
    """A ``copied_code`` taint flag when the agent's added lines came from outside."""
    try:
        diff_text = (trial_dir / "verifier" / "agent.diff").read_text(errors="replace")
    except OSError:
        return None
    added = added_lines(diff_text)
    if not added:
        return None
    typed: set[str] = set()
    matched: dict[str, Any] = {}
    sources: dict[Any, str] = {}
    for step_id, commands, observation in _agent_steps(trial_dir):
        typed |= _typed(commands)
        if not _OUTSIDE.search(commands):
            continue
        for line in _output_lines(observation):
            folded = _strip_prefixes(line)
            if folded in added and folded not in typed and folded not in matched:
                matched[folded] = step_id
                sources.setdefault(step_id, commands.strip().splitlines()[0][:200])
    if len(matched) < MIN_MATCHED_LINES:
        return None
    return {
        "kind": "copied_code",
        "rule": RULE,
        "matched_lines": len(matched),
        "added_lines": len(added),
        "source_steps": [
            {"step": step, "command": command}
            for step, command in sorted(sources.items(), key=lambda item: str(item[0]).zfill(8))
        ],
        "examples": sorted(matched)[:3],
    }
