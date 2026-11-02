#!/usr/bin/env python3
"""fake_agent.py: Standard kill-test agent fixture matching _KILLTESTS.md specification.

Usage:
    fake_agent.py --id ID --ledger LEDGER.jsonl --report REPORT.md --seconds S [--quota-until EPOCH] [--heartbeat FILE]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


def append_ledger(ledger_path: Path, row: dict) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
        f.flush()


def has_effect_for_id(ledger_path: Path, item_id: str) -> bool:
    if not ledger_path.exists():
        return False
    with open(ledger_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if data.get("id") == item_id and data.get("kind") == "effect":
                    return True
            except json.JSONDecodeError:
                continue
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Standard fake agent for squad kill tests.")
    parser.add_argument("--id", required=True, help="Task ID")
    parser.add_argument("--ledger", required=True, type=Path, help="Path to JSONL ledger")
    parser.add_argument("--report", required=True, type=Path, help="Path to output report markdown")
    parser.add_argument("--seconds", type=float, default=20.0, help="Run duration in seconds")
    parser.add_argument("--quota-until", type=float, default=None, help="Epoch timestamp until which quota is exhausted")
    parser.add_argument("--heartbeat", type=Path, default=None, help="Optional heartbeat file to touch periodically")
    parser.add_argument("--fail-transient", action="store_true", help="Exit with code 1 immediately (for transient retry test)")
    parser.add_argument("--no-done-marker", action="store_true", help="Omit DS-DONE marker (for report-missing test)")

    args = parser.parse_args()
    now = time.time()

    # Check quota refusal
    if args.quota_until is not None and now < args.quota_until:
        iso_reset = datetime.fromtimestamp(args.quota_until, UTC).isoformat()
        append_ledger(args.ledger, {
            "id": args.id,
            "pid": os.getpid(),
            "t": now,
            "kind": "quota-refusal",
            "reset_at": iso_reset,
            "quota_until": args.quota_until,
        })
        msg = f"quota exhausted; resets at {iso_reset}"
        print(msg, flush=True)
        sys.stderr.write(msg + "\n")
        sys.exit(29)

    # Record execution attempt
    append_ledger(args.ledger, {
        "id": args.id,
        "pid": os.getpid(),
        "t": now,
        "kind": "exec",
    })

    if args.fail_transient:
        sys.stderr.write(f"simulated transient failure for {args.id}\n")
        sys.exit(1)

    # Sleep with periodic heartbeat touch
    t_end = time.time() + args.seconds
    while time.time() < t_end:
        if args.heartbeat is not None:
            args.heartbeat.parent.mkdir(parents=True, exist_ok=True)
            args.heartbeat.write_text(f"{time.time()}:{os.getpid()}\n", encoding="utf-8")
        time.sleep(min(0.5, max(0.05, t_end - time.time())))

    if args.heartbeat is not None:
        args.heartbeat.parent.mkdir(parents=True, exist_ok=True)
        args.heartbeat.write_text(f"{time.time()}:{os.getpid()}\n", encoding="utf-8")

    # Append idempotent effect if not already present
    if not has_effect_for_id(args.ledger, args.id):
        append_ledger(args.ledger, {
            "id": args.id,
            "pid": os.getpid(),
            "t": time.time(),
            "kind": "effect",
        })

    # Write report
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report_content = f"# Report for {args.id}\n\nCompleted execution after {args.seconds:.1f}s.\n"
    if not args.no_done_marker:
        report_content += f"\nDS-DONE: {args.id}\n"
    else:
        report_content += "\nExecution ended without marker.\n"
    args.report.write_text(report_content, encoding="utf-8")


if __name__ == "__main__":
    main()
