"""CheatBench raw-trace parsing and field-path helpers."""

from __future__ import annotations

import json
import re
from typing import Any


class CheatbenchError(Exception):
    """Pinned-fetch or conversion failure for the CheatBench corpus."""


_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")
RAW_SCHEMA = "cheatbench.raw_trace.v1"


def resolve_field_path(obj: Any, path: str) -> Any:
    """Resolve ``messages[8].content``-style paths; raise LookupError."""
    if not path:
        raise LookupError("empty field path")
    current = obj
    for match in _PATH_TOKEN.finditer(path):
        name, index = match.group(1), match.group(2)
        try:
            current = current[name] if name is not None else current[int(index)]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LookupError(f"unresolvable field path {path!r}") from exc
    return current


def parse_raw_trace(payload: str) -> dict:
    """Parse a serialized trace; reject a mismatched declared schema."""
    try:
        trace = json.loads(payload)
    except ValueError as exc:
        raise CheatbenchError(f"trace is not JSON: {exc}") from exc
    if not isinstance(trace, dict):
        raise CheatbenchError("trace payload must be a JSON object")
    declared = trace.get("schema_version", trace.get("schema"))
    if declared is not None and declared != RAW_SCHEMA:
        raise CheatbenchError(f"unsupported trace schema {declared!r}")
    return trace
