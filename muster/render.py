from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any


def to_json(report: Any) -> str:
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"


def number(value: float | None, signed: bool = False) -> str:
    if value is None:
        return "-"
    if value == 0:
        return "0"
    return format(value, "+.4g" if signed else ".4g")


def table(headers: Sequence[str], rows: Sequence[Sequence[Any]], right: Sequence[int] = ()) -> str:
    cells = [[str(cell) for cell in row] for row in [headers, *rows]]
    widths = [max(len(row[index]) for row in cells) for index in range(len(headers))]
    lines = []
    for row in cells:
        parts = [
            cell.rjust(width) if index in right else cell.ljust(width)
            for index, (cell, width) in enumerate(zip(row, widths, strict=True))
        ]
        lines.append("  ".join(parts).rstrip())
    return "\n".join(lines)
