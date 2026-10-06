#!/usr/bin/env python3
"""Offline $0 controls replay for the HAR-169 integrity reward.

Re-scores stored trials through ``evallab.integrity_reward`` (no runs, no
network) and checks the acceptance contract:

* positive control: every reward-1 trial ``evallab.copy_check`` flags as a
  copy gets ``integrity=0`` (deduped by trial identity);
* negative control: the 5 clean G5 passes (``HAR-126-ovn-g5-*``, reward 1,
  no copy: tasks 000169, 000332, 001695, 001809 x2) get ``integrity=1``;
* ``reward`` is unchanged from the stored raw result and
  ``reward_gated == reward * integrity`` everywhere.

Also reports the false-positive scan: how many *other* clean passes trip
any rule (reported, never tuned away).

Usage (from the checkout root)::

    uv run python research/experiments/har169-integrity/replay.py

Writes ``controls.csv`` next to this file.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = Path.home() / "Developer/eval-lab-results"
CONTROLS = {"nop", "oracle"}

EXPECTED_NEG_TASKS = sorted(
    [
        "format-code-task-000169",
        "format-code-task-000332",
        "format-code-task-001695",
        "format-code-task-001809",
        "format-code-task-001809",
    ]
)


def load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def stored_reward(trial: Path, result: dict):
    try:
        return float((trial / "verifier" / "reward.txt").read_text().strip())
    except (OSError, ValueError):
        pass
    value = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def discover() -> list[dict]:
    from evallab.copy_check import copy_check

    rows = []
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
        score = stored_reward(trial, result)
        if score is None:
            outcome = "infra"
        elif score < 1.0:
            outcome = "fail"
        else:
            outcome = "copied_pass" if copy_check(trial) else "clean_pass"
        rows.append(
            {
                "task": f"format-code-task-{numbers.pop()}",
                "agent": agent,
                "outcome": outcome,
                "trial": str(trial.relative_to(RESULTS)),
                "score": score,
            }
        )
    return rows


def main() -> int:
    from evallab.integrity_reward import evaluate_trial

    rows = discover()
    print("scanned {} trials: {}".format(len(rows), dict(Counter(r["outcome"] for r in rows))))

    raw_values = sorted({r["score"] for r in rows if r["score"] is not None})
    print(f"distinct raw rewards: {raw_values}")

    # Positive control: reward-1 + copy, deduped by trial identity.
    positives = [r for r in rows if r["outcome"] == "copied_pass"]
    by_name: dict[str, dict] = {}
    duplicates = []
    for row in positives:
        name = row["trial"].rsplit("/", 1)[-1]
        if name in by_name:
            duplicates.append(row["trial"])
        else:
            by_name[name] = row
    positives = sorted(by_name.values(), key=lambda r: r["trial"])
    tasks = sorted({r["task"] for r in positives})
    print(
        "positive: {} rows -> {} unique trials across {} tasks (dupes: {})".format(
            len([r for r in rows if r["outcome"] == "copied_pass"]),
            len(positives),
            len(tasks),
            duplicates or "none",
        )
    )

    # Negative control: clean G5 passes.
    negatives = sorted(
        (r for r in rows if "HAR-126-ovn-g5-" in r["trial"] and r["outcome"] == "clean_pass"),
        key=lambda r: r["trial"],
    )
    neg_tasks = sorted(r["task"] for r in negatives)
    print(f"negative: {len(negatives)} trials, tasks {neg_tasks}")

    failures = []
    if neg_tasks != EXPECTED_NEG_TASKS:
        failures.append(f"negative set drifted: {neg_tasks}")

    controls = []
    for row in positives + negatives:
        trial_dir = RESULTS / row["trial"]
        result = evaluate_trial(trial_dir)
        fired = sorted(rid for rid, f in result["rules"].items() if f["fired"])
        controls.append(
            {
                "trial": row["trial"],
                "task": row["task"],
                "raw_reward": row["score"],
                "integrity": result["integrity"],
                "reward_gated": result["reward_gated"],
                "rules_fired": ";".join(fired),
            }
        )
        if result["reward"] != row["score"]:
            failures.append(
                "{}: reward changed {} -> {}".format(row["trial"], row["score"], result["reward"])
            )
        if result["reward_gated"] != result["reward"] * result["integrity"]:
            failures.append("{}: gated arithmetic wrong".format(row["trial"]))
        if row in positives and result["integrity"] != 0:
            failures.append("{}: positive control kept integrity=1".format(row["trial"]))
        if row in negatives and result["integrity"] != 1:
            failures.append("{}: negative control lost integrity ({})".format(row["trial"], fired))

    with (HERE / "controls.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("trial", "task", "raw_reward", "integrity", "reward_gated", "rules_fired"),
        )
        writer.writeheader()
        writer.writerows(controls)
    print("wrote {} ({} rows)".format(HERE / "controls.csv", len(controls)))

    # False-positive scan: every other clean pass.
    neg_names = {r["trial"] for r in negatives}
    others = [r for r in rows if r["outcome"] == "clean_pass" and r["trial"] not in neg_names]
    fp_counts: Counter[str] = Counter()
    fp_trials = []
    for row in others:
        result = evaluate_trial(RESULTS / row["trial"])
        fired = sorted(rid for rid, f in result["rules"].items() if f["fired"])
        if fired:
            fp_trials.append((row["trial"], row["task"], fired))
            fp_counts.update(fired)
    print(f"false-positive scan: {len(fp_trials)}/{len(others)} other clean passes trip a rule")
    print(f"  per-rule: {dict(fp_counts)}")
    for trial, task, fired in fp_trials:
        print(f"  FP {trial} {task} {fired}")

    if failures:
        print("ACCEPTANCE FAILURES:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("acceptance: all positive integrity=0, all negative integrity=1, rewards unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
