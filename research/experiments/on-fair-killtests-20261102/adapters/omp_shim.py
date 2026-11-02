#!/usr/bin/env python3
"""omp_shim.py: Fake omp CLI shim for intercepting ds_squad/ds_squad_v2 invocations and routing to fake_agent.py."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

EXPERIMENT_DIR = Path(__file__).resolve().parent.parent
FAKE_AGENT_PATH = EXPERIMENT_DIR / "fake_agent.py"


def handle_usage():
    usage_data = {
        "reports": [
            {
                "provider": "deepseek",
                "limits": [
                    {
                        "amount": {
                            "remaining": 999.0,
                            "total": 1000.0,
                        }
                    }
                ],
            }
        ]
    }
    print(json.dumps(usage_data))
    sys.exit(0)


def handle_run():
    args = sys.argv[1:]
    prompt_file = None
    session_dir = None
    model_name = "fake-model"

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--session-dir" and i + 1 < len(args):
            session_dir = Path(args[i + 1])
            i += 2
        elif arg == "--model" and i + 1 < len(args):
            model_name = args[i + 1]
            i += 2
        elif arg.startswith("@"):
            prompt_file = Path(arg[1:])
            i += 1
        else:
            i += 1

    if not prompt_file or not prompt_file.exists():
        sys.stderr.write("omp_shim: Missing prompt file\n")
        sys.exit(1)

    prompt_content = prompt_file.read_text(encoding="utf-8")
    match = re.search(r"# YOUR ITEM:\s*([^\s-]+)", prompt_content)
    item_id = match.group(1).strip() if match else prompt_file.parent.name

    ledger_path = Path(os.environ.get("FAKE_AGENT_LEDGER", prompt_file.parents[2] / "ledger.jsonl"))
    report_path = prompt_file.parent / "report.md"
    seconds = float(os.environ.get("FAKE_AGENT_SECONDS", "20.0"))
    mode = os.environ.get("FAKE_AGENT_MODE", "guarded")
    quota_until = os.environ.get("FAKE_AGENT_QUOTA_UNTIL")
    fail_transient = os.environ.get("FAKE_AGENT_FAIL_TRANSIENT", "0") == "1"
    hang_silent = os.environ.get("FAKE_AGENT_HANG_SILENT", "0") == "1"

    cmd = [
        sys.executable,
        str(FAKE_AGENT_PATH),
        "--id", item_id,
        "--ledger", str(ledger_path),
        "--report", str(report_path),
        "--seconds", str(seconds),
        "--mode", mode,
    ]
    if quota_until:
        cmd.extend(["--quota-until", str(quota_until)])
    if fail_transient:
        cmd.append("--fail-transient")
    if hang_silent:
        cmd.append("--hang-silent")

    # In ds_squad, worker runs in background session.
    proc = subprocess.run(cmd, capture_output=True, text=True)

    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)

    if session_dir:
        session_dir.mkdir(parents=True, exist_ok=True)
        sess_file = session_dir / f"session_{item_id}_{int(time.time())}.jsonl"
        provider = model_name.split("/")[0] if "/" in model_name else "mock"
        model = model_name.split("/")[1] if "/" in model_name else model_name
        sess_record = {
            "type": "message",
            "message": {
                "role": "assistant",
                "provider": provider,
                "model": model,
                "usage": {
                    "input": 100,
                    "output": 100,
                    "cacheRead": 0,
                    "cost": {"total": 0.0},
                },
            },
        }
        with open(sess_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(sess_record) + "\n")

    sys.exit(proc.returncode)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "usage":
        handle_usage()
    elif len(sys.argv) > 1 and sys.argv[1] in ("-p", "prompt"):
        handle_run()
    else:
        if "usage" in sys.argv:
            handle_usage()
        else:
            handle_run()


if __name__ == "__main__":
    main()
