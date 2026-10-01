#!/usr/bin/env python3
"""Relabel every stored agent run on a Python MiMo task with the copy check (HAR-143).

The run list is Research-Harbor's fetch scan (216 trials,
``mimo-fetch-scan-20261001.json``); its regex fetch columns are kept for
reference only. Each trial gets one label:

* ``pass``: verifier reward 1 and :func:`evallab.copy_check.copy_check`
  finds no copy;
* ``copied pass (unknown)``: reward 1, copy found. Unknown, not a failure;
* ``fail``: reward below 1;
* ``no score``: no verifier reward (infrastructure error).

Usage (from the checkout root):
    uv run python research/experiments/har143-copy-check/relabel.py [SCAN_JSON]
Writes ``relabel.csv`` next to this file and prints the label counts.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path

from evallab.copy_check import copy_check

HERE = Path(__file__).resolve().parent
RESULTS = Path.home() / "Developer/eval-lab-results"
SCAN = (
    Path.home()
    / "Developer/research-context/inbox/sft-overnight-20260918/mimo-fetch-scan-20261001.json"
)
COLUMNS = (
    "job",
    "trial",
    "card",
    "reward",
    "label",
    "copy_matched_lines",
    "copy_added_lines",
    "copy_source_steps",
    "scan_fetch_attempts",
    "scan_fetch_succeeded",
    "trial_dir",
)


def trial_dir(job: str) -> Path:
    (job_dir,) = RESULTS.glob(f"*/{job}")
    (trial,) = [path for path in job_dir.iterdir() if (path / "agent").is_dir()]
    return trial


def reward(trial: Path) -> float | None:
    try:
        return float((trial / "verifier" / "reward.txt").read_text().strip())
    except (OSError, ValueError):
        pass
    try:
        rewards = (
            json.loads((trial / "result.json").read_text()).get("verifier_result") or {}
        ).get("rewards")
    except (OSError, json.JSONDecodeError):
        return None
    value = (rewards or {}).get("reward")
    return float(value) if isinstance(value, (int, float)) else None


def main() -> None:
    scan = json.loads(Path(sys.argv[1] if len(sys.argv) > 1 else SCAN).read_text())
    rows = []
    for item in scan["trials"]:
        trial = trial_dir(item["job"])
        score = reward(trial)
        flag = copy_check(trial) if score is not None and score >= 1.0 else None
        if score is None:
            label = "no score"
        elif score < 1.0:
            label = "fail"
        else:
            label = "copied pass (unknown)" if flag else "pass"
        rows.append(
            {
                "job": item["job"],
                "trial": trial.name,
                "card": item["card"],
                "reward": "" if score is None else score,
                "label": label,
                "copy_matched_lines": flag["matched_lines"] if flag else "",
                "copy_added_lines": flag["added_lines"] if flag else "",
                "copy_source_steps": " ".join(str(step["step"]) for step in flag["source_steps"])
                if flag
                else "",
                "scan_fetch_attempts": item["fetch_attempts"],
                "scan_fetch_succeeded": item["fetch_succeeded"],
                "trial_dir": str(trial.relative_to(RESULTS)),
            }
        )
    rows.sort(key=lambda row: (row["label"], row["job"]))
    with (HERE / "relabel.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(dict(Counter(row["label"] for row in rows)), len(rows))


if __name__ == "__main__":
    main()
