from __future__ import annotations

import re
from pathlib import Path

HEADER_LINE_LIMIT = 20

_EXPECT_HEADER = re.compile(r"#\s*expect:\s*([01])(?:\.0)?\s*")


def parse_expect_header(line: str) -> float | None:
    match = _EXPECT_HEADER.fullmatch(line.strip())
    if match is None:
        return None
    return float(match.group(1))


def declared_expectation(script: Path) -> float | None:
    try:
        lines = script.read_text(encoding="utf-8").splitlines()[:HEADER_LINE_LIMIT]
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        expectation = parse_expect_header(line)
        if expectation is not None:
            return expectation
    return None
