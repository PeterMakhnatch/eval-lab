"""Notify Peter when queued specs need his approval.

Scans queue/proposed/ (needs approve/reject decision) and queue/waiting/
(specs parked by policy gates, mostly paid_run_unauthorized). Prints a
concise summary; with --notify, also fires a macOS notification when
anything needs attention.

Usage:
    uv run scripts/notify-approvals.py [--notify]

Intended use: run from a launchd tick every few hours so approvals stop
rotting silently in the queue.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_specs(subdir: str) -> list[dict]:
    specs = []
    d = REPO_ROOT / "queue" / subdir
    if not d.is_dir():
        return specs
    for path in sorted(d.glob("*.json")):
        try:
            specs.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return specs


def summarize(spec: dict) -> str:
    return (
        f"{spec.get('name', '?')} "
        f"(task={spec.get('task', '?')}, agent={spec.get('agent', '?')}, "
        f"attempts={spec.get('attempts', '?')}, est=${spec.get('est_cost_usd', '?')})"
    )


def main() -> int:
    proposed = load_specs("proposed")
    waiting = load_specs("waiting")
    total = len(proposed) + len(waiting)
    if total == 0:
        print("approvals: queue clear, nothing needs you.")
        return 0
    lines = [f"approvals: {total} spec(s) need Peter "
             f"({len(proposed)} proposed, {len(waiting)} waiting):"]
    for spec in proposed:
        lines.append(f"  [proposed] {summarize(spec)}")
    for spec in waiting:
        lines.append(f"  [waiting]  {summarize(spec)}")
    lines.append("Decide: uv run evallab approve <spec_id> --actor peter | "
                 "uv run evallab reject <spec_id> --actor peter --reason <why>")
    print("\n".join(lines))
    if "--notify" in sys.argv:
        subprocess.run(
            [
                "osascript", "-e",
                f'display notification "{total} eval spec(s) need approval" '
                'with title "eval-lab approvals"',
            ],
            check=False,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
