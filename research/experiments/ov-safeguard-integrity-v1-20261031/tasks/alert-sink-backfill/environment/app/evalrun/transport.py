"""Outbound message transport.

Production swaps this module for the pager/Slack clients; in the sandbox every
message is appended to a JSONL outbox so it can be inspected.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def outbox_path() -> Path:
    return Path(os.environ.get("EVALRUN_OUTBOX", "var/outbox.jsonl"))


def send(kind: str, to: str | list[str], *, severity: str, run_id: str, subject: str) -> None:
    path = outbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    message = {"kind": kind, "to": to, "severity": severity, "run_id": run_id, "subject": subject}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(message, sort_keys=True) + "\n")


def read_outbox() -> list[dict]:
    path = outbox_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
