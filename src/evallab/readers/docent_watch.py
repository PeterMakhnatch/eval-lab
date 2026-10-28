"""Periodic hosted-free Docent triage of attested, admitted own DEV actors.

One whole run is approved per tick, serially. Exhausted/unknown allowance,
privacy failure, unknown model routing, or ambiguous submission never generates.
The existing own-run rubric is operational triage, not a TT-7 model selection.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
import time
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.readers.clouddb_blind import opaque_id
from evallab.readers.docent_reading import (
    DETECTOR,
    MODEL,
    PROMPT,
    TERMINAL,
    VERSION,
    HostedReading,
    ReadingBlocked,
    judgment,
    render,
)
from evallab.trace_store.core import GOODFIRE_DATASET, HydrationError, load_trace, select_traces
from evallab.trace_store.secrets import secret_hits

DEFAULT_STATE = Path.home() / ".local/state/evallab-prod-live/trace-tools"
DEFAULT_DB = DEFAULT_STATE.parent / "cloud-trace-db.sqlite"
DEFAULT_SPEND = Path.home() / ".local/state/evallab-failure-library/spend.jsonl"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _append(path: Path, record: dict[str, Any], key: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        for line in stream:
            if line.strip():
                existing = json.loads(line)
                if existing.get(key) == record[key]:
                    if any(existing.get(field) != record.get(field)
                           for field in ("flag", "value", "evidence_steps") if field in record):
                        raise ValueError("immutable reading record conflicts")
                    return False
        stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return True


def _finish(
    job: dict[str, Any], row: dict[str, Any] | None, *, results_path: Path, spend_path: Path,
) -> None:
    flag, value, steps = judgment(row, set(job["steps"]))
    scan = f"https://docent.transluce.org/dashboard/{job['collection_id']}/analysis-plan/{job['plan_id']}"
    at = job.setdefault("finished_at", _now())
    _append(results_path, {
        "schema": "evallab.trace_tool_result/v1", "run_id": job["run_id"],
        "source": "cloud-trace-db", "tool": "docent", "detector": DETECTOR,
        "detector_version": VERSION, "flag": flag, "value": value,
        "evidence_steps": steps, "scan": scan, "at": at,
    }, "scan")
    _append(spend_path, {
        "at": at, "tool": "docent", "route": scan, "model": MODEL, "runs": 1,
        "input_tokens": row.get("input_tokens") if row else None,
        "output_tokens": row.get("output_tokens") if row else None,
        "usd": 0, "batch_id": "docent-watch:" + job["plan_id"],
        "basis": "Verified native hosted-only catalog route and live capped free allowance; no BYOK/fallback",
    }, "batch_id")
    job.update(status="terminal", flag=flag, abstention_reason=value.get("abstention_reason"))


def _advance(
    service: HostedReading, job: dict[str, Any], journal: dict[str, Any], journal_path: Path,
    *, results_path: Path, spend_path: Path,
) -> dict[str, Any]:
    collection = service.collection(job["dataset_id"])
    if collection["id"] != job["collection_id"]:
        raise ReadingBlocked("private_collection_identity_changed")
    if not job.get("plan_id"):
        job["plan_id"] = service.find_plan(job["collection_id"], job["plan_name"])
        _save(journal_path, journal)
        if not job["plan_id"]:
            # A lost POST response cannot establish that the server did nothing.
            # Reconcile on later ticks; never resubmit an ambiguous request.
            raise ReadingBlocked("submission_unresolved_no_automatic_retry")
    status, row = service.inspect(job["collection_id"], job["plan_id"])
    if status in TERMINAL:
        _finish(job, row, results_path=results_path, spend_path=spend_path)
        _save(journal_path, journal)
        return {"status": "collected", "result_lines": 1, "model_approvals": 0}
    if status == "needs_approval":
        if job.get("approval_requested_at"):
            raise ReadingBlocked("approval_unresolved_no_automatic_retry")
        free, _ = service.gate()
        job["approval_requested_at"] = _now()
        job["allowance_before_approval"] = free
        _save(journal_path, journal)
        service.approve(job["collection_id"], job["plan_id"])
        return {"status": "approved", "result_lines": 0, "model_approvals": 1,
                "allowance": free}
    if status not in {"running", "queued", "pending", "unresolved"}:
        raise ReadingBlocked("unknown_reading_status")
    return {"status": "awaiting_result", "result_lines": 0, "model_approvals": 0}


def run_once(
    *, db_path: Path, repo_root: Path, state_dir: Path, results_path: Path, spend_path: Path,
    evidence_path: Path | None = None, cas_root: Path | None = None,
    service: HostedReading | None = None,
) -> dict[str, Any]:
    state_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    journal_path = state_dir / "docent-watch-jobs.json"
    with (state_dir / "docent-watch.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"at": _now(), "status": "busy", "model_approvals": 0}
        report: dict[str, Any] = {"at": _now(), "status": "idle", "model_approvals": 0,
                                  "result_lines": 0, "model": MODEL, "detector_version": VERSION}
        try:
            journal: dict[str, Any] = json.loads(journal_path.read_text()) if journal_path.exists() else {
                "schema": VERSION, "jobs": {},
            }
            if journal.get("schema") != VERSION:
                raise ReadingBlocked("journal_version_changed")
            uri = db_path.expanduser().resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as con:
                refs = [ref for ref in select_traces(con, repo_root=repo_root, evidence_path=evidence_path)
                        if ref.dataset_id != GOODFIRE_DATASET]
                by_id = {ref.trace_id: ref for ref in refs}
                jobs = journal["jobs"]
                active = [job for job in jobs.values() if job["status"] != "terminal"]
                if len(active) > 1:
                    raise ReadingBlocked("multiple_active_readings")
                report.update(eligible=len(refs), completed=sum(
                    jobs.get(ref.trace_id, {}).get("status") == "terminal" for ref in refs))
                if service is None:
                    from evallab.trace_store.docent import new_client

                    service = HostedReading(new_client())
                if active:
                    job = active[0]
                    # Repeat source admission and privacy projection before any
                    # approval; a task may acquire a cross-key TEST seal meanwhile.
                    if job["run_id"] not in by_id:
                        raise ReadingBlocked("active_reading_no_longer_admitted")
                    trace = load_trace(con, by_id[job["run_id"]], cas_root=cas_root)
                    text = render(trace.messages)
                    if secret_hits(text) or hashlib.sha256(text.encode()).hexdigest() != job["payload_sha256"]:
                        raise ReadingBlocked("active_reading_payload_changed_or_private")
                    report.update(_advance(service, job, journal, journal_path,
                                           results_path=results_path, spend_path=spend_path))
                else:
                    pending = [ref for ref in refs if ref.trace_id not in jobs]
                    report["pending"] = len(pending)
                    if pending:
                        free, model = service.gate()
                        report["allowance"] = free
                        quarantines: Counter[str] = Counter()
                        for ref in pending:
                            try:
                                trace = load_trace(con, ref, cas_root=cas_root)
                            except HydrationError as error:
                                quarantines[error.category] += 1
                                continue
                            text = render(trace.messages)
                            if secret_hits(text):
                                quarantines["secret_pattern"] += 1
                                continue
                            # Whole input only: a conservative UTF-8 byte upper
                            # bound cannot prove a context overflow; it abstains.
                            if len((PROMPT + text).encode()) + 8192 > model["context_window"]:
                                quarantines["context_upper_bound"] += 1
                                continue
                            collection = service.collection(ref.dataset_id)
                            payload_sha = hashlib.sha256(text.encode()).hexdigest()
                            opaque = opaque_id(ref.trace_id)
                            job = {
                                "run_id": ref.trace_id, "dataset_id": ref.dataset_id,
                                "collection_id": collection["id"], "payload_sha256": payload_sha,
                                "steps": sorted({message["step_index"] for message in trace.messages}),
                                "plan_name": f"docent-watch-v1-{opaque}-{payload_sha[:16]}",
                                "status": "submitting", "created_at": _now(),
                            }
                            jobs[ref.trace_id] = job
                            # No automatic resubmission after a killed/ambiguous
                            # POST. Deterministic plan name permits reconciliation.
                            _save(journal_path, journal)
                            job["plan_id"] = service.submit(collection["id"], job["plan_name"], opaque, text)
                            _save(journal_path, journal)
                            report.update(_advance(service, job, journal, journal_path,
                                                   results_path=results_path, spend_path=spend_path))
                            break
                        report["quarantined"] = dict(quarantines)
                        if report["status"] == "idle" and quarantines:
                            report["status"] = "quarantined"
        except (ReadingBlocked, HydrationError) as error:
            report.update(status="blocked", reason=str(error) if isinstance(error, ReadingBlocked)
                          else error.category)
        except Exception as error:
            # SDK/HTTP exception text can contain request content. Never expose
            # it to launchd logs; retain the safe type and fail without retries.
            report.update(status="error", error_type=type(error).__name__)
        report["wall_seconds"] = round(time.monotonic() - started, 3)
        _save(state_dir / "docent-watch-last.json", report)
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--results-path", type=Path, default=DEFAULT_STATE / "results-v1.jsonl")
    parser.add_argument("--spend-path", type=Path, default=DEFAULT_SPEND)
    parser.add_argument("--evidence-path", type=Path)
    parser.add_argument("--cas-root", type=Path)
    report = run_once(**vars(parser.parse_args(argv)))
    print(json.dumps(report, sort_keys=True))
    return int(report["status"] == "error")


if __name__ == "__main__":
    raise SystemExit(main())
