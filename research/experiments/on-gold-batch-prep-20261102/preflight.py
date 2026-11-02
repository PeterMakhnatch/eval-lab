#!/usr/bin/env python3
"""A4 Preflight Check Script: run before launching the gold certification batch.

Checks:
1. Fresh-image Modal probe (~/.local/share/uv/tools/modal/bin/python .../probe.py)
2. Modal workspace deployed app count (`modal app list --json | count`) <= 200
3. Spend ledger state (~/.local/state/evallab-failure-library/a4-gold-batch-ledger.json)
4. Upstream PR states for PR #1744 (A3 rounds) and PR #1738 (S1 modal_cells)
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

MODAL_PROBE_SCRIPT = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/modal-probe/probe.py")
MODAL_UV_PYTHON = Path.home() / ".local/share/uv/tools/modal/bin/python"
DEFAULT_LEDGER = Path.home() / ".local/state/evallab-failure-library/a4-gold-batch-ledger.json"
MODAL_APP_LIMIT = 200
WARN_APP_THRESHOLD = 150


def check_pr_state(pr_number: int) -> dict[str, Any]:
    """Query GitHub PR state via gh CLI."""
    if not shutil.which("gh"):
        return {"pr": pr_number, "status": "unknown", "error": "gh CLI not found on PATH"}
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "state,number,title,headRefName,mergedAt"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return json.loads(proc.stdout)
    except subprocess.CalledProcessError as exc:
        return {"pr": pr_number, "status": "error", "error": exc.stderr.strip()}
    except Exception as exc:
        return {"pr": pr_number, "status": "error", "error": str(exc)}


def check_modal_app_count() -> dict[str, Any]:
    """Query Modal deployed app count."""
    if not shutil.which("modal"):
        return {"count": 0, "status": "error", "error": "modal CLI not found on PATH"}
    try:
        proc = subprocess.run(
            ["modal", "app", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=45,
            check=True,
        )
        apps = json.loads(proc.stdout)
        count = len(apps)
        return {
            "count": count,
            "limit": MODAL_APP_LIMIT,
            "status": "ok" if count < MODAL_APP_LIMIT else "exceeded",
            "warning": count >= WARN_APP_THRESHOLD,
        }
    except Exception as exc:
        return {"count": 0, "status": "error", "error": str(exc)}


def check_modal_probe(run_probe: bool = True) -> dict[str, Any]:
    """Execute fresh-image Modal probe."""
    if not run_probe:
        return {"status": "skipped", "message": "Probe skipped by flag"}

    if not MODAL_PROBE_SCRIPT.is_file():
        return {"status": "error", "error": f"Probe script not found at {MODAL_PROBE_SCRIPT}"}

    py_bin = MODAL_UV_PYTHON if MODAL_UV_PYTHON.is_file() else sys.executable

    try:
        proc = subprocess.run(
            [str(py_bin), str(MODAL_PROBE_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=180,
        )
        success = (proc.returncode == 0) and ("DONE" in proc.stdout or "sandbox ok" in proc.stdout)
        return {
            "status": "passed" if success else "failed",
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "error": "Modal probe timed out after 180s"}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def check_spend_ledger(ledger_path: Path = DEFAULT_LEDGER) -> dict[str, Any]:
    """Check spend ledger state."""
    if not ledger_path.is_file():
        return {
            "exists": False,
            "status": "uninitialized",
            "settled_usd": 0.0,
            "reserved_usd": 0.0,
            "remaining_usd": 3.00,
            "cap_usd": 3.00,
        }
    try:
        data = json.loads(ledger_path.read_text(encoding="utf-8"))
        settled = float(data.get("settled_usd", 0.0))
        reserved = float(data.get("reserved_usd", 0.0))
        cap = float(data.get("max_usd", 3.00))
        remaining = max(0.0, cap - (settled + reserved))
        return {
            "exists": True,
            "status": "active",
            "approval": data.get("approval"),
            "cap_usd": cap,
            "settled_usd": settled,
            "reserved_usd": reserved,
            "effective_usd": round(settled + reserved, 6),
            "remaining_usd": round(remaining, 6),
            "executions": int(data.get("executions", 0)),
        }
    except Exception as exc:
        return {"exists": True, "status": "error", "error": str(exc)}


def run_preflight(run_probe: bool = True, ledger_path: Path = DEFAULT_LEDGER) -> int:
    """Run all preflight checks and print formatted report."""
    print("=" * 80)
    print("A4 PREFLIGHT CHECK REPORT")
    print("=" * 80)

    # 1. Upstream PR States
    print("\n[1] Checking Upstream PRs:")
    pr1744 = check_pr_state(1744)
    pr1738 = check_pr_state(1738)

    pr1744_state = pr1744.get("state", "UNKNOWN")
    pr1738_state = pr1738.get("state", "UNKNOWN")

    print(f"  - PR #1744 (A3 rounds feedback loop): {pr1744_state}")
    print(f"    Title: {pr1744.get('title', 'N/A')}")
    print(f"  - PR #1738 (S1 modal_cells reserve/settle runner): {pr1738_state}")
    print(f"    Title: {pr1738.get('title', 'N/A')}")

    prs_merged = (pr1744_state == "MERGED" and pr1738_state == "MERGED")

    # 2. Modal Deployed App Count
    print("\n[2] Checking Modal Workspace Deployed App Limit:")
    app_info = check_modal_app_count()
    app_count = app_info.get("count", 0)
    app_status = app_info.get("status", "unknown")
    print(f"  - Deployed apps: {app_count} / {MODAL_APP_LIMIT} (status: {app_status})")
    if app_info.get("warning"):
        print(f"    WARNING: App count >= {WARN_APP_THRESHOLD}. Consider stopping stale apps before batch launch.")
    if app_status == "exceeded":
        print("    ERROR: Modal app limit reached! Batch cannot deploy containers.")

    # 3. Spend Ledger State
    print("\n[3] Checking Spend Ledger State:")
    ledger_info = check_spend_ledger(ledger_path)
    if ledger_info["exists"]:
        print(f"  - Ledger path: {ledger_path}")
        print(f"  - Approval: {ledger_info.get('approval', 'N/A')}")
        print(f"  - Cap: ${ledger_info.get('cap_usd', 0.0):.2f}")
        print(f"  - Settled Spend: ${ledger_info.get('settled_usd', 0.0):.4f}")
        print(f"  - Reserved In-Flight: ${ledger_info.get('reserved_usd', 0.0):.4f}")
        print(f"  - Remaining Budget: ${ledger_info.get('remaining_usd', 0.0):.4f}")
    else:
        print(f"  - Ledger not yet initialized at {ledger_path} (fresh start with full $3.00 cap)")

    ledger_ok = ledger_info.get("remaining_usd", 3.00) > 0.05

    # 4. Modal Probe
    print("\n[4] Fresh-Image Modal Sandbox Probe:")
    probe_info = check_modal_probe(run_probe=run_probe)
    probe_status = probe_info.get("status", "unknown")
    print(f"  - Probe Status: {probe_status.upper()}")
    if probe_status == "passed":
        print(f"  - Details: {probe_info.get('stdout', '')}")
    elif probe_status == "skipped":
        print("  - Probe execution was skipped.")
    else:
        print(f"  - Probe Error: {probe_info.get('error', probe_info.get('stderr', ''))}")

    # Summary Verdict
    print("\n" + "=" * 80)
    print("VERDICT:")
    ready = True
    reasons: list[str] = []

    if not prs_merged:
        reasons.append(f"PR #1744 ({pr1744_state}) or PR #1738 ({pr1738_state}) not yet merged")
        # In pre-launch wait mode, this is the primary expected gate
    if app_count >= MODAL_APP_LIMIT:
        ready = False
        reasons.append(f"Modal app limit exceeded ({app_count}/{MODAL_APP_LIMIT})")
    if not ledger_ok:
        ready = False
        reasons.append("Spend ledger budget exhausted")
    if run_probe and probe_status != "passed":
        ready = False
        reasons.append(f"Modal probe failed ({probe_status})")

    if ready and prs_merged:
        print("  READY TO LAUNCH A4-GOLD-BATCH (All gates open)")
        rc = 0
    elif ready and not prs_merged:
        print("  WAITING ON GATES: PR #1744 and/or PR #1738 open (Infrastructure ready, waiting for merges)")
        print(f"  Blockers: {'; '.join(reasons)}")
        rc = 2
    else:
        print("  BLOCKED: Infrastructure or budget issues require attention")
        print(f"  Blockers: {'; '.join(reasons)}")
        rc = 1

    print("=" * 80 + "\n")
    return rc


def main() -> None:
    parser = argparse.ArgumentParser(description="Preflight check for A4 Gold Batch")
    parser.add_argument("--skip-probe", action="store_true", help="Skip executing the live Modal probe")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER, help="Path to spend ledger")
    args = parser.parse_args()

    rc = run_preflight(run_probe=(not args.skip_probe), ledger_path=args.ledger)
    sys.exit(rc)


if __name__ == "__main__":
    main()
