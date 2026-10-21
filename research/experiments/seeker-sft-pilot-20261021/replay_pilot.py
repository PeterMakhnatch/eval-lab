#!/usr/bin/env python3
"""Differential replay for the seeker SFT pilot submissions (pE-SELF-5).

Reads `pilot-results.json` (written by `pilot_modal.py`), materializes each
base/trained submission, validates it with the strict packaging gate, then
replays it with `evallab.candidate_runner`: upstream grader first, and the PM
port grader only when upstream measured `1.0` (the `1/0` differential).

Usage:
    uv run python research/experiments/seeker-sft-pilot-20261021/replay_pilot.py
    uv run python research/experiments/seeker-sft-pilot-20261021/replay_pilot.py --summary-only
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
DATASET_DIR = REPO / "research/datasets/seeker-sft-v1"

RESULTS_ROOT = Path.home() / "Developer/eval-lab-results/2026-10-21/seeker-sft-pilot"
UPSTREAM_ROOT = (
    Path.home() / "Developer/eval-lab-data/tb4-20261020/upstream-git-v4.0.0/terminal-bench"
)
PORT_ROOT = Path.home() / "Developer/eval-lab-data/tb4-20261020/karotte-port"
PYTHON = REPO / ".venv/bin/python"
LEDGER = Path.home() / ".local/state/evallab/pe-self5-pilot/modal-ledger.json"
BILLING_TAG = "2026-10-21-seeker-sft-pilot"
# The local Docker daemon is an 8 GiB VM with unbounded host services, so the
# native runner refuses it; the Modal backend is the lane's admitted path.
MODAL_FLAGS = [
    "--backend",
    "modal",
    "--max-usd",
    "6",
    "--approval",
    "pE-SELF-5 seeker SFT pilot replay, 2026-10-21 harness run (CONTRACT Modal execution cap $6)",
    "--spend-ledger",
    str(LEDGER),
    "--billing-tag",
    BILLING_TAG,
    "--modal-app-name",
    BILLING_TAG,
]


def _load_strict_validator() -> Any:
    if str(DATASET_DIR) not in sys.path:
        sys.path.insert(0, str(DATASET_DIR))
    spec = importlib.util.spec_from_file_location(
        "seeker_sft_v1_eval", DATASET_DIR / "eval_tb4_dev.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.validate_seeker_submission_strict


def _run(argv: list[str]) -> dict[str, Any]:
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    text = proc.stdout
    index = text.find("{")
    payload: dict[str, Any] = {}
    if index >= 0:
        try:
            payload, _ = json.JSONDecoder().raw_decode(text[index:])
        except ValueError:
            payload = {}
    if not payload:
        payload = {
            "status": "error",
            "error": f"exit {proc.returncode}: {(proc.stderr or proc.stdout)[-400:]}",
        }
    return payload


def _ledger_spend() -> dict[str, Any]:
    if not LEDGER.is_file():
        return {"ledger": str(LEDGER), "reservations": 0, "finalized_usd": 0.0}
    data = json.loads(LEDGER.read_text(encoding="utf-8"))
    reservations = data.get("reservations") or []
    finalized = sum(float(r.get("finalized_estimate_usd") or 0.0) for r in reservations)
    return {
        "ledger": str(LEDGER),
        "max_usd": data.get("max_usd"),
        "reservations": len(reservations),
        "finalized_usd": round(finalized, 6),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args(argv)

    results_path = HERE / "pilot-results.json"
    results = json.loads(results_path.read_text(encoding="utf-8"))
    validate_strict = _load_strict_validator()

    submissions_root = RESULTS_ROOT / "submissions"
    replay_root = RESULTS_ROOT / "replay"
    rows: list[dict[str, Any]] = []
    for key, run in sorted(results["runs"].items()):
        arm, task = key.split(":", 1)
        sub_dir = submissions_root / arm / task
        sub_dir.mkdir(parents=True, exist_ok=True)
        for rel, content in (run.get("submission") or {}).items():
            target = sub_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        strict = validate_strict(sub_dir)
        rows.append(
            {
                "key": key,
                "arm": arm,
                "task": task,
                "submission_dir": sub_dir.as_posix(),
                "turns": run.get("turns"),
                "tool_calls": run.get("tool_calls"),
                "finished": run.get("finished"),
                "claimed_exploit": run.get("exploit_found"),
                "claimed_class": run.get("class"),
                "strict_valid": strict["strict_valid"],
                "strict_non_noop_valid": strict["strict_non_noop_valid"],
                "strict_problems": strict["strict_problems"],
                "upstream_reward": None,
                "port_reward": None,
                "upstream_status": None,
                "port_status": None,
                "confirmed_1_0": False,
            }
        )

    if not args.summary_only:
        for row in rows:
            task = row["task"]
            upstream_task = UPSTREAM_ROOT / task
            out_dir = replay_root / "upstream" / row["arm"] / task
            if out_dir.exists():
                shutil.rmtree(out_dir)
            if not upstream_task.is_dir():
                row["upstream_status"] = "missing_task"
                continue
            record = _run(
                [
                    str(PYTHON),
                    "-m",
                    "evallab.candidate_runner",
                    str(upstream_task),
                    "--solution-bundle",
                    row["submission_dir"],
                    "--grader",
                    "upstream",
                    *MODAL_FLAGS,
                    "--output-dir",
                    str(out_dir),
                ]
            )
            row["upstream_status"] = record.get("status")
            row["upstream_reward"] = record.get("reward")
            row["upstream_error"] = str(record.get("error") or "")[:300]
            if record.get("reward") == 1.0:
                port_task = PORT_ROOT / task
                if not port_task.is_dir():
                    row["port_status"] = "missing_port"
                    continue
                port_out = replay_root / "port" / row["arm"] / task
                if port_out.exists():
                    shutil.rmtree(port_out)
                port_record = _run(
                    [
                        str(PYTHON),
                        "-m",
                        "evallab.candidate_runner",
                        str(upstream_task),
                        "--solution-bundle",
                        row["submission_dir"],
                        "--grader",
                        "port",
                        "--port-task",
                        str(port_task),
                        *MODAL_FLAGS,
                        "--output-dir",
                        str(port_out),
                    ]
                )
                row["port_status"] = port_record.get("status")
                row["port_reward"] = port_record.get("reward")
                row["port_error"] = str(port_record.get("error") or "")[:300]
                row["confirmed_1_0"] = (
                    row["upstream_reward"] == 1.0 and port_record.get("reward") == 0.0
                )

    summary = {
        "runs": rows,
        "by_arm": {},
        "modal_spend": _ledger_spend(),
    }
    for arm in sorted({row["arm"] for row in rows}):
        arm_rows = [row for row in rows if row["arm"] == arm]
        summary["by_arm"][arm] = {
            "runs": len(arm_rows),
            "strict_valid": sum(1 for r in arm_rows if r["strict_valid"]),
            "strict_non_noop_valid": sum(1 for r in arm_rows if r["strict_non_noop_valid"]),
            "claimed_exploits": sum(1 for r in arm_rows if r["claimed_exploit"] is True),
            "upstream_measured": sum(1 for r in arm_rows if r["upstream_status"] == "measured"),
            "upstream_reward_1": sum(1 for r in arm_rows if r["upstream_reward"] == 1.0),
            "confirmed_1_0": sum(1 for r in arm_rows if r["confirmed_1_0"]),
        }
    out = HERE / "replay-summary.json"
    out.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
