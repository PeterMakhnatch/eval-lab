"""Shared native-trajectory redaction used by every Harbor credential adapter.

This module has no Harbor import. Credential adapters and the read-only daemon
preflight share it without loading the native execution runtime.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from evallab.execution_contracts import (
    REDACTED_SECRET_VALUE,
    collected_secret_values,
    persist_private_bytes,
)

SENSITIVE_CONFIG_KEYS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "api-key",
        "api_key",
        "x-api-key",
        "access_token",
        "apikey",
    }
)


def _redact_sensitive_values(value: Any, secrets: frozenset[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                REDACTED_SECRET_VALUE
                if str(key).casefold() in SENSITIVE_CONFIG_KEYS
                else _redact_sensitive_values(item, secrets)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive_values(item, secrets) for item in value]
    if isinstance(value, str) and (value in secrets or any(s and s in value for s in secrets)):
        return REDACTED_SECRET_VALUE
    return value


def sanitize_native_trajectory(
    path: Path,
    secrets: frozenset[str] | None = None,
    default_secrets_fn: Callable[[], frozenset[str]] | None = None,
) -> None:
    """Rewrite a native trajectory on disk only after in-memory redaction."""
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        persist_private_bytes(
            path,
            (json.dumps({"redacted": "unparseable native trajectory removed"}) + "\n").encode(),
            secrets=(),
        )
        return
    if secrets is not None:
        known = secrets
    elif default_secrets_fn is not None:
        known = default_secrets_fn()
    else:
        known = collected_secret_values()
    sanitized = _redact_sensitive_values(payload, known)
    persist_private_bytes(
        path,
        (json.dumps(sanitized, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        secrets=tuple(secret.encode() for secret in known),
    )


def validate_docker_daemon(info: dict[str, Any]) -> None:
    """The shared, read-only prerequisite for Docker's isolation boundary."""
    if info.get("OSType") != "linux":
        raise RuntimeError("egress_lock requires a confirmed Linux Docker daemon")
