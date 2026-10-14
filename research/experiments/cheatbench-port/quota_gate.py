#!/usr/bin/env python3
"""Gate subscription-agent trials on the account's weekly quota.

Subscription quota (Claude Max, ChatGPT/Codex) is shared by every agent on the
account, so a batch must stop before it eats the week. This reads OMP's live
usage snapshot (``omp usage --json``, the same numbers ``omp usage`` prints)
and exits 1 when any weekly (7-day) window of the provider is at or above
``--max-used`` (fraction, default 0.60), 2 when the reading is unavailable
(treated as a stop, never as "plenty left"), 0 otherwise.

Usage:
  uv run --no-sync python research/experiments/cheatbench-port/quota_gate.py codex
  uv run --no-sync python research/experiments/cheatbench-port/quota_gate.py claude --max-used 0.6
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

#: run_batch provider name -> OMP usage provider id.
OMP_PROVIDER = {"codex": "openai-codex", "claude": "anthropic"}


def weekly_used(provider: str) -> tuple[float | None, str]:
    """Highest used fraction over the provider's 7-day windows, plus a note."""
    try:
        out = subprocess.run(
            ["omp", "usage", "--json", "--redact", "-p", OMP_PROVIDER[provider]],
            capture_output=True, text=True, timeout=120, check=False).stdout
        doc = json.loads(out)
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return None, f"omp usage unavailable: {type(e).__name__}"
    used = [
        float(limit["amount"]["usedFraction"])
        for report in doc.get("reports", [])
        if report.get("provider") == OMP_PROVIDER[provider]
        for limit in report.get("limits", [])
        if (limit.get("window") or {}).get("id") == "7d"
        and isinstance((limit.get("amount") or {}).get("usedFraction"), (int, float))
    ]
    if not used:
        return None, "no 7-day window in omp usage"
    return max(used), f"7-day windows used: {', '.join(f'{u:.0%}' for u in used)}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("provider", choices=sorted(OMP_PROVIDER))
    ap.add_argument("--max-used", type=float, default=0.60)
    args = ap.parse_args()
    used, note = weekly_used(args.provider)
    if used is None:
        print(f"quota_gate {args.provider}: STOP ({note})")
        return 2
    verdict = "ok" if used < args.max_used else "STOP"
    print(f"quota_gate {args.provider}: {verdict} ({note}; limit {args.max_used:.0%})")
    return 0 if verdict == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
