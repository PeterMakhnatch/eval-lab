"""In-memory and file-based transport outbox."""

from __future__ import annotations

import json
import os
from pathlib import Path


def outbox_path() -> Path:
    return Path(os.environ.get("EVALRUN_OUTBOX", "var/outbox.jsonl"))


def send(kind: str, to: str | list[str], **payload) -> None:
    path = outbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"kind": kind, "to": [to] if isinstance(to, str) else list(to), **payload}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def read_outbox() -> list[dict]:
    path = outbox_path()
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
