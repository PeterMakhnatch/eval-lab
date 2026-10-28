"""Strictly admitted, own-first, resumable private trace-store synchronization."""

from __future__ import annotations

import fcntl
import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from evallab.trace_store.core import load_trace, select_traces
from evallab.trace_store.secrets import SecretScanRejected, secret_hits
from evallab.trace_store.state import (
    SyncState,
    append_jsonl,
    byte_reserver,
    content_digest,
    now,
    record_spend,
    state_root,
)


class SyncStopped(RuntimeError):
    """Quota/size/admission failures leave all successful receipts resumable."""


def _upload(target: Any, trace: Any, sleep: Callable[[float], None], *, batch: bool = False) -> Any:
    for attempt in range(4):
        try:
            return target.upload_batch(trace) if batch else target.upload(trace)
        except SecretScanRejected:
            raise
        except HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            if exc.code in {402, 403, 413, 429}:
                raise SyncStopped(f"HTTP {exc.code}: quota/access/payload limit; upload paused") from None
            if exc.code not in {500, 502, 503, 504} or attempt == 3:
                # Never persist provider bodies that may echo content/secrets.
                raise SyncStopped(f"Upload rejected: HTTP {exc.code}; paused") from None
            if "quota" in detail.lower():
                raise SyncStopped(f"HTTP {exc.code}: quota exhausted; upload paused") from None
        except (URLError, TimeoutError, ConnectionError):
            if attempt == 3:
                raise SyncStopped("Upload transport unavailable after four attempts") from None
        except Exception as exc:
            # SDK exceptions sometimes contain entire request payloads.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in {500, 502, 503, 504} and attempt < 3:
                sleep(2 ** attempt)
                continue
            raise SyncStopped(f"{type(exc).__name__}: hosted upload refused; paused") from None
        sleep(2 ** attempt)
    raise AssertionError("retry loop exhausted")


def sync(
    target: str, *, repo_root: Path, dataset: str | None = None, limit: int | None = None,
    db_path: Path | None = None, root: Path | None = None, uploader: Any = None,
    evidence_path: Path | None = None, cas_root: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Upload admitted traces in bounded, source-homogeneous batches."""
    if target not in {"laminar", "docent"}:
        raise ValueError("target must be laminar or docent")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    root = root or state_root()
    root.mkdir(parents=True, exist_ok=True)
    database = db_path or Path.home() / ".local/state/evallab-prod-live/cloud-trace-db.sqlite"
    started = time.monotonic()
    report: dict[str, Any] = {
        "target": target, "at": now(), "selected": 0, "synced": 0, "unchanged": 0,
        "rejected": [], "sources": {}, "payload_bytes": 0, "hydrated_bytes": 0,
        "stopped": None, "cursor": None, "cost_usd": None, "model_cost_usd": 0,
        "storage_tariff": "unknown; ingest-only, no paid-storage activation",
    }
    counts: Counter[str] = Counter()
    # The common lock serializes the ledger; each provider has its own byte cap.
    with (root / "sync.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = SyncState(root / f"{target}-state.jsonl")
        if uploader is None:
            if target == "laminar":
                from evallab.trace_store.laminar import LaminarTarget
                uploader = LaminarTarget()
            else:
                from evallab.trace_store.docent import DocentTarget
                uploader = DocentTarget()
        uploader.reserve_bytes = byte_reserver(root, target)
        batched = callable(getattr(uploader, "upload_batch", None))
        pending: list[tuple[Any, str]] = []
        pending_bytes = 0

        def flush() -> bool:
            nonlocal pending_bytes
            if not pending:
                return True
            try:
                while pending:
                    try:
                        if batched:
                            results = _upload(uploader, [trace for trace, _ in pending], sleep, batch=True)
                        else:
                            results = [_upload(uploader, pending[0][0], sleep)]
                        break
                    except SecretScanRejected as exc:
                        index = next(
                            (i for i, (trace, _) in enumerate(pending) if trace.trace_id == exc.trace_id),
                            None,
                        )
                        if index is None:
                            raise SyncStopped("Encoded scanner returned an unknown trace; paused") from None
                        trace, _ = pending.pop(index)
                        rejection = {"trace_id": trace.trace_id, "dataset": trace.dataset_id,
                                     "kind": "secret_scan", "stage": "encoded"}
                        report["rejected"].append(rejection)
                        append_jsonl(root / f"{target}-errors.jsonl", {"at": now(), **rejection})
                        pending_bytes = sum(
                            len(message["content"].encode()) + len((message.get("reasoning") or "").encode())
                            for item, _ in pending for message in item.messages
                        )
                else:
                    return True
                if not isinstance(results, list) or len(results) != len(pending):
                    raise SyncStopped("Hosted batch returned incomplete receipts; paused")
                for (trace, digest), result in zip(pending, results, strict=True):
                    state.record(trace.trace_id, digest, result, trace.dataset_id)
                    report["synced"] += 1
                    report["payload_bytes"] += result["payload_bytes"]
                    report["hydrated_bytes"] += trace.hydrated_bytes
                    counts[trace.dataset_id] += 1
            except SyncStopped as exc:
                first = pending[0][0]
                report["stopped"] = str(exc)
                report["cursor"] = first.trace_id
                append_jsonl(root / f"{target}-errors.jsonl", {
                    "at": now(), "trace_id": first.trace_id, "dataset": first.dataset_id,
                    "kind": "hosted_limit", "error": str(exc), "batch_runs": len(pending),
                })
                return False
            pending.clear()
            pending_bytes = 0
            return True

        try:
            with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as con:
                refs = select_traces(con, repo_root=repo_root, dataset=dataset, evidence_path=evidence_path)
                report["selected"] = len(refs)
                attempts = 0
                for ref in refs:
                    if limit is not None and attempts >= limit:
                        break
                    try:
                        trace = load_trace(con, ref, cas_root=cas_root)
                        digest = content_digest(trace)
                    except (ValueError, FileNotFoundError) as exc:
                        rejection = {
                            "trace_id": ref.trace_id, "dataset": ref.dataset_id,
                            "kind": "payload", "error_type": type(exc).__name__,
                            "category": getattr(exc, "category", "invalid_payload"),
                            "step_index": getattr(exc, "step_index", None),
                        }
                        report["rejected"].append(rejection)
                        append_jsonl(root / f"{target}-errors.jsonl", {"at": now(), **rejection})
                        continue
                    if secret_hits(asdict(trace)):
                        rejection = {"trace_id": ref.trace_id, "dataset": ref.dataset_id, "kind": "secret_scan"}
                        report["rejected"].append(rejection)
                        append_jsonl(root / f"{target}-errors.jsonl", {"at": now(), **rejection})
                        continue
                    if state.unchanged(trace.trace_id, digest):
                        report["unchanged"] += 1
                        continue
                    size = sum(
                        len(message["content"].encode()) + len((message.get("reasoning") or "").encode())
                        for message in trace.messages
                    )
                    if pending and (
                        pending[0][0].dataset_id != trace.dataset_id
                        or pending_bytes + size > 8 * 1024 * 1024
                    ) and not flush():
                        break
                    pending.append((trace, digest))
                    pending_bytes += size
                    attempts += 1
                    if (not batched or len(pending) >= 16) and not flush():
                        break
                if not report["stopped"]:
                    flush()
        finally:
            report["sources"] = dict(counts)
            report["wall_seconds"] = round(time.monotonic() - started, 3)
            report["sent_bytes"] = getattr(uploader, "sent_bytes", report["payload_bytes"])
            report["requests"] = getattr(uploader, "requests", None)
            report["sent_gb"] = report["sent_bytes"] / 1_000_000_000
            append_jsonl(root / "sync-runs.jsonl", report)
            record_spend(
                target + "-storage", synced=report["synced"], sources=report["sources"],
                payload_bytes=report["sent_bytes"], wall_seconds=report["wall_seconds"],
                cost_usd=None, model_cost_usd=0, storage_tariff=report["storage_tariff"],
                basis="ingest within plan allowance; Signals disabled",
                stopped=report["stopped"],
            )
    return report
