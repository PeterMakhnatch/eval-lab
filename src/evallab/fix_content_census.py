"""Test and harness path classification used by task QA."""

from __future__ import annotations

import re

_TEST_RES = (
    re.compile(r"(^|/)(tests?|testing|test_|_test|spec|specs|e2e)(/|$)"),
    re.compile(r"\.(spec|test)\.[a-z]+$"),
    re.compile(r"conftest\.py$"),
    re.compile(r"mimo_test_command\.sh$"),
    re.compile(r"test_commands\.json$"),
    re.compile(r"test\.patch$"),
)


_HARNESS_BASENAMES = frozenset(
    {"test_commands.json", "mimo_test_command.sh", "test.patch", "test_command.sh"}
)


def is_test_path(path: str) -> bool:
    """Whether ``path`` is a test/harness file (never a fix-content carrier)."""
    base = path.rsplit("/", 1)[-1]
    if base in _HARNESS_BASENAMES:
        return True
    return any(rx.search(path) for rx in _TEST_RES)
