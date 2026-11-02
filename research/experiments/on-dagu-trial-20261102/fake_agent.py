#!/usr/bin/env python3
"""Shared fixture: fake_agent.py (engine-neutral protocol).

CLI: fake_agent.py --id ID --ledger LEDGER.jsonl --report REPORT.md --seconds S [--quota-until EPOCH]
- At start append {"id": ID, "pid": ..., "t": ..., "kind": "exec"} to the ledger (this counts EXECUTIONS).
- Sleep S seconds (default 20). Then append {"kind": "effect"} only if the ledger holds no effect row for ID yet
  (an idempotent side effect keyed by ID), and write REPORT ending with the line DS-DONE: ID.
- With --quota-until T: if now < T, append {"kind": "quota-refusal"}, print 'quota exhausted; resets at <ISO of T>'
  and exit 29 without doing anything else.
"""

import argparse
import datetime
import fcntl
import json
import os
import sys
import time
from pathlib import Path


def append_ledger(ledger_path: Path, entry: dict) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry) + "\n"
    with open(ledger_path, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def has_effect(ledger_path: Path, task_id: str) -> bool:
    if not ledger_path.exists():
        return False
    with open(ledger_path, encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if row.get("id") == task_id and row.get("kind") == "effect":
                        return True
                except Exception:
                    pass
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return False


def main():
    parser = argparse.ArgumentParser(description="Engine-neutral fake agent fixture")
    parser.add_argument("--id", required=True, help="Task ID")
    parser.add_argument("--ledger", required=True, help="Path to ledger.jsonl")
    parser.add_argument("--report", required=True, help="Path to report.md")
    parser.add_argument("--seconds", type=float, default=20.0, help="Sleep duration in seconds")
    parser.add_argument("--quota-until", type=float, default=None, help="Epoch timestamp until quota reset")
    args = parser.parse_args()

    ledger_path = Path(args.ledger).resolve()
    report_path = Path(args.report).resolve()

    now = time.time()
    if args.quota_until is not None and now < args.quota_until:
        append_ledger(ledger_path, {
            "id": args.id,
            "pid": os.getpid(),
            "t": now,
            "kind": "quota-refusal",
        })
        iso_reset = datetime.datetime.fromtimestamp(args.quota_until, tz=datetime.UTC).isoformat()
        print(f"quota exhausted; resets at {iso_reset}", file=sys.stderr)
        sys.exit(29)

    # Record execution
    append_ledger(ledger_path, {
        "id": args.id,
        "pid": os.getpid(),
        "t": time.time(),
        "kind": "exec",
    })

    # Sleep S seconds
    time.sleep(args.seconds)

    # Check for existing effect
    if not has_effect(ledger_path, args.id):
        append_ledger(ledger_path, {
            "id": args.id,
            "pid": os.getpid(),
            "t": time.time(),
            "kind": "effect",
        })
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(f"# Fake Agent Run: {args.id}\n\nCompleted successfully at {time.time()}.\n\nDS-DONE: {args.id}\n")
    else:
        # Side effect already recorded
        pass


if __name__ == "__main__":
    main()
