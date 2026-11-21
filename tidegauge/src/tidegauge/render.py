"""Small text helpers shared by the instruments. No terminal colors, no dependencies."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any


def pct(value: float | None, digits: int = 1) -> str:
    """A fraction as a percentage: 0.629 -> '62.9%'."""
    return "n/a" if value is None else f"{100 * value:.{digits}f}%"


def pts(value: float | None, digits: int = 1) -> str:
    """A difference of fractions in percentage points, with its sign: 0.115 -> '+11.5'."""
    if value is None:
        return "n/a"
    rounded = round(100 * value, digits)
    return f"{rounded + 0.0:+.{digits}f}"  # adding 0.0 turns -0.0 into 0.0


def pvalue(p: float | None) -> str:
    if p is None:
        return "n/a"
    return "<0.0001" if p < 0.0001 else f"{p:.4f}"


def number(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    return f"{value:.{digits}f}"


def table(headers: Sequence[str], rows: Sequence[Sequence[Any]], right: Sequence[int] = ()) -> str:
    """A plain aligned table. ``right`` lists the columns to right-align."""
    cells = [[str(h) for h in headers], *[[str(c) for c in row] for row in rows]]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]

    def line(row: Sequence[str]) -> str:
        parts = [
            c.rjust(w) if i in right else c.ljust(w) for i, (c, w) in enumerate(zip(row, widths, strict=True))
        ]
        return "  ".join(parts).rstrip()

    rule = "  ".join("-" * w for w in widths)
    return "\n".join([line(cells[0]), rule, *[line(r) for r in cells[1:]]])


def to_json(report: Any) -> str:
    return json.dumps(report, indent=2, sort_keys=False, default=str)


def count(n: int, noun: str) -> str:
    """A count with its noun: 1 error, 2 errors."""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def shorten(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"
