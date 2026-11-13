"""Shared JSONL encoding, atomic writes, and Failure Library state location."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

STATE_ENV = "EVALLAB_FAILURE_LIBRARY_STATE"


def default_state_root() -> Path:
    configured = os.environ.get(STATE_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state" / "evallab-failure-library"


def _line(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
