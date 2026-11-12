"""One parser for the control-author's ``# expect:`` header.

Task lint and the reward-hack runner both read control headers through
:func:`declared_expectation`, so a header lint accepts is the header the runner
reads. The header must be an exact ``# expect: 0`` or ``# expect: 1`` line
(``0.0`` and ``1.0`` are also accepted) within the first 20 lines.
"""

from __future__ import annotations

import re
from pathlib import Path

HEADER_LINE_LIMIT = 20

_EXPECT_HEADER = re.compile(r"#\s*expect:\s*([01])(?:\.0)?\s*")


def parse_expect_header(line: str) -> float | None:
    """Return 0.0 or 1.0 for one exact header line, or None."""
    match = _EXPECT_HEADER.fullmatch(line.strip())
    if match is None:
        return None
    return float(match.group(1))


def declared_expectation(script: Path) -> float | None:
    """Return the first parseable header in the script's first 20 lines."""
    try:
        lines = script.read_text(encoding="utf-8").splitlines()[:HEADER_LINE_LIMIT]
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        expectation = parse_expect_header(line)
        if expectation is not None:
            return expectation
    return None
