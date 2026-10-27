"""Append-only, crash-resumable trace-store receipts and spend accounting."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.trace_store.core import TracePayload


def state_root() -> Path:
    return Path.home() / ".local/state/evallab-prod-live/trace-store"


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode()
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell():
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b"\n":
                encoded = b"\n" + encoded
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o600)


def now() -> str:
    return datetime.now(UTC).isoformat()


def content_digest(trace: TracePayload) -> str:
    # Hydration accounting is local provenance, not uploaded content.
    content = asdict(trace)
    content.pop("hydrated_bytes")
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(("evallab.trace-store/v1:" + encoded).encode()).hexdigest()


class SyncState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.receipts: dict[str, dict[str, Any]] = {}
        if path.exists():
            offset = 0
            with path.open("rb") as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        # Only an incomplete last record may be recovered. Keep
                        # its bytes as evidence, then resume at the valid prefix.
                        if line.endswith(b"\n") or stream.read():
                            raise ValueError(f"Corrupt sync state: {path}") from None
                        recovery = path.with_name(path.name + ".incomplete")
                        recovery.write_bytes(line)
                        recovery.chmod(0o600)
                        with path.open("r+b") as repair:
                            repair.truncate(offset)
                        break
                    self.receipts[row["trace_id"]] = row
                    offset += len(line)

    def unchanged(self, trace_id: str, digest: str) -> bool:
        return self.receipts.get(trace_id, {}).get("content_digest") == digest

    def record(self, trace_id: str, digest: str, result: dict[str, Any], dataset: str) -> None:
        row = {
            "trace_id": trace_id,
            "content_digest": digest,
            "remote_id": result["remote_id"],
            "at": now(),
            "dataset": dataset,
            **{key: result[key] for key in ("payload_bytes", "collection_id") if key in result},
        }
        append_jsonl(self.path, row)
        self.receipts[trace_id] = row


def record_spend(target: str, **details: Any) -> None:
    append_jsonl(
        Path.home() / ".local/state/evallab-failure-library/spend.jsonl",
        {
            "at": now(),
            "route": target,
            "batch": "TT-6-trace-store",
            "cost_usd": 0,
            "basis": "Storage/read APIs within existing allowance; no model or paid Signal calls",
            **details,
        },
    )


MONTHLY_PAYLOAD_BYTES = 2_500_000_000


def byte_reserver(root: Path, target: str) -> Callable[[int], None]:
    """Reserve each wire attempt durably before sending, including retries.

    Called with the common sync lock held. An interrupted send can overcount,
    never undercount, the authorized monthly byte ceiling.
    """
    path = root / "upload-bytes.jsonl"
    month = now()[:7]
    used = 0
    if path.exists():
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                if row["at"].startswith(month):
                    used += row["payload_bytes"]

    def reserve(size: int) -> None:
        nonlocal used
        if used + size > MONTHLY_PAYLOAD_BYTES:
            raise ValueError("2.5 GB monthly payload-byte allowance reached")
        append_jsonl(path, {"at": now(), "target": target, "payload_bytes": size})
        used += size

    return reserve
