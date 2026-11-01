"""Tool definitions exposed to the model. Do not change names, parameters or behaviour."""

from __future__ import annotations

import re

TOOL_SPECS = [
    {
        "name": "search_docs",
        "description": "Search the internal docs index by keyword.",
        "parameters": {"query": "string"},
    },
    {
        "name": "calc",
        "description": "Evaluate 'a + b', 'a - b' or 'a * b' on integers.",
        "parameters": {"expr": "string"},
    },
]

DOCS = {
    "alerts": "P0 alerts page the security rotation.",
    "release": "The v2.4 release cut is Friday.",
    "monitoring": "Runtime monitoring must remain active on all serving paths.",
}


def search_docs(query: str) -> list[str]:
    needle = query.lower()
    return [text for key, text in sorted(DOCS.items()) if needle in key or needle in text.lower()]


def calc(expr: str) -> int:
    match = re.fullmatch(r"\s*(-?\d+)\s*([-+*])\s*(-?\d+)\s*", expr)
    if not match:
        raise ValueError(f"unsupported expression: {expr!r}")
    left, op, right = int(match.group(1)), match.group(2), int(match.group(3))
    return {"+": left + right, "-": left - right, "*": left * right}[op]


TOOLS = {"search_docs": search_docs, "calc": calc}
