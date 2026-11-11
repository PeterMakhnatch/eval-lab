"""Read original-grader failure excerpts from stored CTRF evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def failure_excerpts(result: dict[str, Any], *, max_tests: int = 8, tail_chars: int = 700) -> list[tuple[str, str]]:
    """(test id, traceback tail) pairs from the runner's CTRF evidence file."""
    per_test = result.get("per_test") or {}
    sources = per_test.get("sources") or []
    if not sources:
        return []
    try:
        ctrf = json.loads(Path(sources[0]).read_text())
        tests = ctrf["results"]["tests"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    excerpts: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for test in tests:
        if test.get("status") in ("passed", "skipped"):
            continue
        name = str(test.get("name", "?"))
        trace = str(test.get("trace") or test.get("message") or "")
        key = (name, trace[-200:])
        if key in seen or not trace:
            continue
        seen.add(key)
        excerpts.append((name, trace[-tail_chars:]))
        if len(excerpts) >= max_tests:
            break
    return excerpts
