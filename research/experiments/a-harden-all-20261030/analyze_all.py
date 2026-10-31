#!/usr/bin/env python3
"""A2/A3 gate and per-task tables from the per-cell results (local; no cloud or model calls).

    analyze_all.py --mode all  [--out-name a-harden-all]   # A2: every full-eligible task, L0/L1/H
    analyze_all.py --mode l1   [--out-name a-harden-l1]    # A3: L1-only tasks, L0 vs L1

Unmeasured cells (no native reward, attack not applied, error) count as NOT passed and are listed.
Writes `results/<mode>/summary.json` and `results/<mode>/per-task.md` next to this file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "h1-grader-hardening-20261030"))
import analyze  # noqa: E402

RESULTS_ROOT = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-10-30")
CLASSIFICATION = Path(
    "/Users/petermakhnatch/Developer/research-context/bridge/2026-10-30-factory/raw/A/classification.json"
)
IN_CONTAINER = ("A", "B", "C", "D", "E2", "F1", "F2")


def reward(cells: dict, task: str, level: str, arm: str) -> float | str | None:
    cell = cells.get((task, level, arm))
    if cell is None:
        return None  # not run (E2 without a non-test source file)
    return cell["reward"] if analyze.measured(cell) else "?"


def show(value: float | str | None) -> str:
    return "n/a" if value is None else "?" if value == "?" else str(int(value))


def chain(cells: dict, task: str, arm: str, sep: str, levels: tuple = ("L0", "L1", "H")) -> str:
    return sep.join(show(reward(cells, task, lv, arm)) for lv in levels)


def gate_all(cells: dict, tasks: list[str]) -> dict:
    false_rejects, live_at_h, unmeasured = [], [], []
    for task in tasks:
        correct, empty = reward(cells, task, "H", "correct"), reward(cells, task, "H", "empty")
        if correct != 1.0:
            false_rejects.append({"task": task, "cell": "H/correct", "reward": correct})
        if empty != 0.0:
            false_rejects.append({"task": task, "cell": "H/empty", "reward": empty})
        for arm in IN_CONTAINER:
            value = reward(cells, task, "H", arm)
            if value == 1.0:
                live_at_h.append({"task": task, "arm": arm})
            elif value == "?":
                unmeasured.append(f"{task}/H/{arm}")
    return {
        "false_rejects": false_rejects,
        "attacks_live_at_H": live_at_h,
        "unmeasured_in_gate": unmeasured,
        "pass": not false_rejects and not live_at_h and not unmeasured,
    }


def table_all(cells: dict, tasks: list[str]) -> str:
    head = "| task | correct L0/L1/H | empty L0/L1/H | " + " | ".join(
        f"{a} L0→L1→H" for a in IN_CONTAINER
    )
    head += " | F3 L0→L1→H (known survivor) |"
    lines = [head, "|" + "---|" * (head.count("|") - 1)]
    for task in tasks:
        row = [
            task.removeprefix("format-code-task-"),
            chain(cells, task, "correct", "/"),
            chain(cells, task, "empty", "/"),
        ]
        row += [chain(cells, task, a, "→") for a in (*IN_CONTAINER, "F3")]
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines) + "\n"


def table_l1(cells: dict, tasks: list[str]) -> str:
    head = "| task | correct L0/L1 | empty L0/L1 | A L0→L1 | B L0→L1 | C L0→L1 | D L0→L1 | A–D live at L0 | surviving L1 |"
    lines = [head, "|" + "---|" * (head.count("|") - 1)]
    for task in tasks:
        live = [a for a in "ABCD" if reward(cells, task, "L0", a) == 1.0]
        survive = [a for a in live if reward(cells, task, "L1", a) != 0.0]
        row = [
            task.removeprefix("format-code-task-"),
            "/".join(show(reward(cells, task, lv, "correct")) for lv in ("L0", "L1")),
            "/".join(show(reward(cells, task, lv, "empty")) for lv in ("L0", "L1")),
            *[chain(cells, task, a, "→", ("L0", "L1")) for a in "ABCD"],
            ",".join(live) or "none (all inert)",
            ",".join(survive) or "none",
        ]
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("all", "l1"), required=True)
    parser.add_argument("--out-name", required=True)
    args = parser.parse_args()
    analyze.OUT = RESULTS_ROOT / args.out_name
    analyze.LEVELS = ("L0", "L1", "H")
    cells = analyze.load()
    classification = json.loads(CLASSIFICATION.read_text())
    tasks = sorted({t for t, _, _ in cells})
    summary: dict = {
        "mode": args.mode,
        "tasks": tasks,
        "n_tasks": len(tasks),
        "classified": classification["n"],
        "class_counts": classification["counts"],
        "cost_est_usd_cells": round(sum(c["est_usd"] for c in cells.values()), 4),
        "cells": len(cells),
    }
    per_attack: dict = {}
    levels = ("L0", "L1", "H") if args.mode == "all" else ("L0", "L1")
    arms = ("A", "B", "C", "D", "E2", "F1", "F2", "F3") if args.mode == "all" else tuple("ABCD")
    for arm in arms:
        row = {}
        live = [t for t in tasks if reward(cells, t, "L0", arm) == 1.0]
        row["live_at_L0"] = f"{len(live)}/{len(tasks)}"
        row["inert_at_L0"] = [t for t in tasks if reward(cells, t, "L0", arm) == 0.0]
        row["not_run_or_unmeasured"] = [
            t for t in tasks if reward(cells, t, "L0", arm) in (None, "?")
        ]
        for level in levels[1:]:
            neutral = [t for t in live if reward(cells, t, level, arm) == 0.0]
            row[f"neutralised_at_{level}"] = f"{len(neutral)}/{len(live)}"
            row[f"neutralised_at_{level}_wilson95"] = analyze.wilson(len(neutral), len(live))
            row[f"survivors_at_{level}"] = [
                t.removeprefix("format-code-task-")
                for t in live
                if reward(cells, t, level, arm) != 0.0
            ]
        per_attack[arm] = row
    summary["per_attack"] = per_attack
    if args.mode == "all":
        summary["gate"] = gate_all(cells, tasks)
        table = table_all(cells, tasks)
    else:
        summary["controls"] = {
            f"{arm}@{level}": [
                t
                for t in tasks
                if reward(cells, t, level, arm) != (1.0 if arm == "correct" else 0.0)
            ]
            for arm in ("correct", "empty")
            for level in ("L0", "L1")
        }
        table = table_l1(cells, tasks)
    target = HERE / "results" / args.mode
    target.mkdir(parents=True, exist_ok=True)
    (target / "summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    (target / "per-task.md").write_text(table)
    rows = ["task,level,arm,reward,attack_applied,gate,est_usd,evidence_sha256"]
    for (task, level, arm), cell in sorted(cells.items()):
        rows.append(
            f"{task},{level},{arm},{cell['reward']},{cell['attack_applied']},{cell['gate'] or ''},{cell['est_usd']},{cell['evidence_sha256']}"
        )
    (target / "cells.csv").write_text("\n".join(rows) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "per_attack"}, indent=1)[:3000])
    for arm, row in per_attack.items():
        print(arm, row)


if __name__ == "__main__":
    main()
