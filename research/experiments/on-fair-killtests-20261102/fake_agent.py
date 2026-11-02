#!/usr/bin/env python3
"""fake_agent.py: Standard kill-test agent fixture for F11-FAIR-KILLTESTS.

Supports:
- GUARDED mode: idempotent side effects (records effect only once per task id).
- UNGUARDED mode: non-idempotent side effects (records unguarded-effect every execution).
- Event ledger recording: start, heartbeat, quota-refusal, effect, exit.
- Hang simulation (0% CPU silent stall for KT6).
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


def append_ledger(ledger_path: Path, row: dict) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row) + "\n"
    with open(ledger_path, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def has_effect_for_id(ledger_path: Path, item_id: str) -> bool:
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
                    if row.get("kind") == "effect" and row.get("id") == item_id:
                        return True
                except Exception:
                    continue
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Standard fake agent for squad kill tests.")
    parser.add_argument("--id", required=True, help="Task ID")
    parser.add_argument("--ledger", required=True, type=Path, help="Path to JSONL ledger")
    parser.add_argument("--report", required=True, type=Path, help="Path to output report markdown")
    parser.add_argument("--seconds", type=float, default=20.0, help="Run duration in seconds")
    parser.add_argument(
        "--mode",
        choices=["guarded", "unguarded"],
        default="guarded",
        help="Guarded (idempotent) vs Unguarded (side-effects recorded on every execution)",
    )
    parser.add_argument(
        "--quota-until",
        type=float,
        default=None,
        help="Epoch timestamp until which quota is exhausted",
    )
    parser.add_argument(
        "--fail-transient",
        action="store_true",
        help="Exit with code 1 immediately (for iteration cap test)",
    )
    parser.add_argument(
        "--no-done-marker",
        action="store_true",
        help="Omit DS-DONE marker (for report failure test)",
    )
    parser.add_argument(
        "--hang-silent",
        action="store_true",
        help="Simulate indefinite silent hang at 0 percent CPU with no output (for KT6)",
    )
    parser.add_argument(
        "--heartbeat",
        type=Path,
        default=None,
        help="Optional heartbeat file to touch periodically",
    )

    args = parser.parse_args()
    now = time.time()
    pid = os.getpid()

    # 1. Record start row
    append_ledger(
        args.ledger,
        {
            "kind": "start",
            "id": args.id,
            "pid": pid,
            "t": now,
            "mode": args.mode,
            "seconds": args.seconds,
        },
    )

    # 2. Check quota refusal
    if args.quota_until is not None and now < args.quota_until:
        iso_reset = datetime.fromtimestamp(args.quota_until, UTC).isoformat()
        append_ledger(
            args.ledger,
            {
                "kind": "quota-refusal",
                "id": args.id,
                "pid": pid,
                "t": now,
                "reset_at": args.quota_until,
                "iso_reset": iso_reset,
            },
        )
        msg = f"quota exhausted; resets at {iso_reset}"
        sys.stderr.write(msg + "\n")
        print(msg)
        append_ledger(
            args.ledger,
            {
                "kind": "exit",
                "id": args.id,
                "pid": pid,
                "t": time.time(),
                "rc": 29,
            },
        )
        sys.exit(29)

    # 3. Check transient failure
    if args.fail_transient:
        sys.stderr.write(f"simulated transient failure for {args.id}\n")
        append_ledger(
            args.ledger,
            {
                "kind": "exit",
                "id": args.id,
                "pid": pid,
                "t": time.time(),
                "rc": 1,
            },
        )
        sys.exit(1)

    # 4. Check silent hang (KT6)
    if args.hang_silent:
        append_ledger(
            args.ledger,
            {
                "kind": "hang-start",
                "id": args.id,
                "pid": pid,
                "t": now,
            },
        )
        # Indefinite sleep at 0% CPU with zero stdout/stderr output and zero network calls
        try:
            while True:
                time.sleep(3600)
        except (KeyboardInterrupt, SystemExit):
            append_ledger(
                args.ledger,
                {
                    "kind": "exit",
                    "id": args.id,
                    "pid": pid,
                    "t": time.time(),
                    "rc": 137,
                },
            )
            raise

    # 5. Normal execution loop
    t_end = time.time() + args.seconds
    while time.time() < t_end:
        if args.heartbeat is not None:
            args.heartbeat.parent.mkdir(parents=True, exist_ok=True)
            hb_row = json.dumps({"t": time.time(), "pid": pid, "id": args.id}) + "\n"
            args.heartbeat.write_text(hb_row, encoding="utf-8")
        time.sleep(min(0.2, max(0.02, t_end - time.time())))

    # 6. Record side effects
    if args.mode == "unguarded":
        append_ledger(
            args.ledger,
            {
                "kind": "unguarded-effect",
                "id": args.id,
                "pid": pid,
                "t": time.time(),
            },
        )

    # In guarded mode (or as baseline idempotent effect):
    if not has_effect_for_id(args.ledger, args.id):
        append_ledger(
            args.ledger,
            {
                "kind": "effect",
                "id": args.id,
                "pid": pid,
                "t": time.time(),
            },
        )

    # 7. Write report markdown
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report_content = f"# Report for {args.id}\n\nCompleted execution after {args.seconds:.1f}s.\n"
    if not args.no_done_marker:
        report_content += f"\nDS-DONE: {args.id}\n"
    else:
        report_content += "\nExecution ended without marker.\n"
    args.report.write_text(report_content, encoding="utf-8")

    # 8. Record clean exit
    append_ledger(
        args.ledger,
        {
            "kind": "exit",
            "id": args.id,
            "pid": pid,
            "t": time.time(),
            "rc": 0,
        },
    )


if __name__ == "__main__":
    main()
