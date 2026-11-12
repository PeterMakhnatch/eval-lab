"""Read-only report rendering and secret redaction for evidence consumers."""

from __future__ import annotations

import re
from typing import Any

_SECRET_MARKERS = (
    "API_KEY",
    "API_TOKEN",
    "_SECRET",
    "ACCESS_KEY",
    "PASSWORD",
    "TOKEN",
    "OAUTH",
    "SESSION",
)
_SECRET_TEXT_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\b(?:sk|rk)-[A-Za-z0-9._-]+"),
    re.compile(
        r"""(?i)["']?\b(?:api[_ -]?key|access[_ -]?token|oauth[_ -]?token|"""
        r"""session[_ -]?token|password|secret)["']?\s*[:=]\s*"""
        r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;]+)"""
    ),
)


def redact_text(text: str) -> str:
    """Redact common bearer/key-shaped values before a UI can render them."""
    redacted = text
    for pattern in _SECRET_TEXT_PATTERNS:
        redacted = pattern.sub("[redacted]", redacted)
    return redacted


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    return value


def redact_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    """Redact credential-shaped keys and nested string values recursively."""
    clean: dict[str, Any] = {}
    for key, value in mapping.items():
        if any(marker in str(key).upper() for marker in _SECRET_MARKERS):
            clean[key] = "[redacted]"
        else:
            clean[key] = _redact_value(value)
    return clean


