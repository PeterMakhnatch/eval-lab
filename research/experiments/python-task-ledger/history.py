#!/usr/bin/env python3
"""Per-task run history for the Python ledger (HAR-158).

One row per ledger task: how many stored agent runs it has and how they
ended. Runs are every trial under ``~/Developer/eval-lab-results`` whose
config or result names exactly one ``format-code-task-*`` and whose agent is
not a control (``nop``/``oracle``) or a HAR-161 exploit probe. Outcome per trial, as in the HAR-143
relabel (``../har143-copy-check/relabel.py``):

* ``clean_pass``: verifier reward 1 and :func:`evallab.copy_check.copy_check`
  finds no copy;
* ``copied_pass``: reward 1, copy found (unknown, not a failure);
* ``fail``: reward below 1;
* ``infra``: no verifier reward.

``last_run`` is the latest trial ``finished_at`` (else ``started_at``).

Usage (from the checkout root):
    uv run python research/experiments/python-task-ledger/history.py
Writes ``task_history.csv`` next to this file.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from evallab.copy_check import copy_check
from evallab.exploit_probe import PREAMBLE

HERE = Path(__file__).resolve().parent
RESULTS = Path.home() / "Developer/eval-lab-results"
CONTROLS = {"nop", "oracle"}
OUTCOMES = ("clean_pass", "copied_pass", "fail", "infra")
COLUMNS = ("task_id", "status", "runs", *OUTCOMES, "last_run", "agents", "trials")


def load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def reward(trial: Path, result: dict) -> float | None:
    try:
        return float((trial / "verifier" / "reward.txt").read_text().strip())
    except (OSError, ValueError):
        pass
    value = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def trials() -> list[dict]:
    out = []
    for config_path in sorted(RESULTS.glob("*/*/*/config.json")):
        trial = config_path.parent
        config, result = load(config_path), load(trial / "result.json")
        text = json.dumps(config) + json.dumps(
            {k: result.get(k) for k in ("task_name", "task_id", "config")}
        )
        numbers = set(re.findall(r"format-code-task-(\d{6})", text))
        if len(numbers) != 1:
            continue
        agent = (
            (config.get("agent") or {}).get("name")
            or (result.get("agent_info") or {}).get("name")
            or ""
        )
        if agent in CONTROLS:
            continue
        # Exploit probes (HAR-161) are told not to solve the task: not solve evidence.
        if load(trial.parent / "experiment-spec.json").get("extra_instruction_path") == PREAMBLE:
            continue
        score = reward(trial, result)
        if score is None:
            outcome = "infra"
        elif score < 1.0:
            outcome = "fail"
        else:
            outcome = "copied_pass" if copy_check(trial) else "clean_pass"
        out.append(
            {
                "task_id": f"format-code-task-{numbers.pop()}",
                "agent": agent,
                "outcome": outcome,
                "when": result.get("finished_at") or result.get("started_at") or "",
                "trial": str(trial.relative_to(RESULTS)),
            }
        )
    return out


def main() -> None:
    with (HERE / "ledger.csv").open(newline="") as handle:
        ledger = {row["task_id"]: row["status"] for row in csv.DictReader(handle)}
    by_task: dict[str, list[dict]] = defaultdict(list)
    for item in trials():
        by_task[item["task_id"]].append(item)
    rows = []
    for task_id, status in sorted(ledger.items()):
        runs = sorted(by_task.get(task_id, []), key=lambda item: item["when"])
        counts = Counter(item["outcome"] for item in runs)
        rows.append(
            {
                "task_id": task_id,
                "status": status,
                "runs": len(runs),
                **{outcome: counts[outcome] for outcome in OUTCOMES},
                "last_run": runs[-1]["when"] if runs else "",
                "agents": " ".join(sorted({item["agent"] for item in runs})),
                "trials": " ".join(item["trial"] for item in runs),
            }
        )
    with (HERE / "task_history.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    outside = sorted(set(by_task) - set(ledger))
    totals = Counter(item["outcome"] for task in ledger for item in by_task.get(task, []))
    print(
        json.dumps(
            {
                "tasks": len(rows),
                "tasks_with_runs": sum(1 for row in rows if row["runs"]),
                "runs": dict(totals),
                "tasks_with_clean_pass": sum(1 for row in rows if row["clean_pass"]),
                "run_tasks_outside_ledger": outside,
            }
        )
    )


if __name__ == "__main__":
    main()
