#!/usr/bin/env python3
"""Fake agent runner for deterministic testing of temporal-squad."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--item-id", required=True)
    parser.add_argument("--raw-dir", required=True)
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--cost", type=float, default=0.0)
    parser.add_argument("--fail-marker", action="store_true")
    parser.add_argument("--missing-report", action="store_true")
    parser.add_argument("--blocked", default="")
    parser.add_argument("--ledger", default="")
    parser.add_argument("--spawn-children", action="store_true")
    args = parser.parse_args()

    raw_path = Path(args.raw_dir)
    raw_path.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    pid = os.getpid()

    # Record invocation in ledger
    if args.ledger:
        ledger_path = Path(args.ledger)
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "item_id": args.item_id,
            "pid": pid,
            "t0": t0,
            "event": "start",
        }
        with open(ledger_path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")

    # If spawn-children requested, launch a sleeper child in same process group
    child_proc = None
    if args.spawn_children:
        child_proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        child_pid_file = raw_path / "child.pid"
        child_pid_file.write_text(str(child_proc.pid))

    # Simulate activity by sleeping and logging
    step = min(0.2, args.sleep)
    slept = 0.0
    while slept < args.sleep:
        time.sleep(step)
        slept += step
        print(f"[{time.time():.2f}] {args.item_id} working... (pid {pid})", flush=True)

    t1 = time.time()
    if args.ledger:
        rec = {
            "item_id": args.item_id,
            "pid": pid,
            "t0": t0,
            "t1": t1,
            "event": "finish",
        }
        with open(Path(args.ledger), "a") as fh:
            fh.write(json.dumps(rec) + "\n")

    # Write report.md unless missing-report is set
    report_file = raw_path / "report.md"
    if not args.missing_report:
        lines = [
            f"# Report for {args.item_id}",
            f"Execution time: {t1 - t0:.2f}s",
            f"Cost: ${args.cost:.2f}",
        ]
        if args.blocked:
            lines.append(f"DS-BLOCKED: {args.item_id} {args.blocked}")
        elif args.fail_marker:
            lines.append(f"UNKNOWN-STATUS: {args.item_id}")
        else:
            lines.append(f"DS-DONE: {args.item_id}")
        report_file.write_text("\n".join(lines) + "\n")

    if child_proc:
        try:
            child_proc.terminate()
            child_proc.wait(timeout=1.0)
        except OSError:
            pass


if __name__ == "__main__":
    main()
