#!/usr/bin/env python3
"""Summarise the H1 executed matrix: layer matrix (L0, L1 only, L1+L3), false rejects, attribution, cost.

Reads the per-cell `result.json` files under the evidence directory, not `matrix.json`, so a rerun of single cells is
picked up. Writes `results/matrix-summary.json` next to this file. No cloud or model calls.
"""

from __future__ import annotations

import json
import math
import shutil
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-10-30/h1-grader-hardening")
ATTACKS = ("A", "B", "C", "D", "E2", "F1", "F2", "F3")
LEVELS = ("L0", "L1", "H")


def wilson(k: int, n: int, z: float = 1.959963984540054) -> list[float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(centre - half, 3), round(centre + half, 3)]


def load() -> dict[tuple[str, str, str], dict]:
    cells = {}
    for path in sorted((OUT / "cells").glob("*/*/result.json")):
        task, name = path.parent.parent.name, path.parent.name
        if "-" not in name:
            continue
        level, arm = name.split("-", 1)
        if level not in LEVELS:
            continue
        result = json.loads(path.read_text())
        stdout = path.parent / "trial/verifier/test-stdout.txt"
        gate = None
        if stdout.exists():
            for line in stdout.read_text(errors="replace").splitlines():
                if "(gate " in line:
                    gate = line.split("gate ")[-1].strip(" )")
        result["gate"] = gate
        cells[(task, level, arm)] = result
    return cells


def measured(cell: dict | None) -> bool:
    return (
        bool(cell)
        and cell["status"] == "executed"
        and cell["reward"] in (0.0, 1.0)
        and cell["attack_applied"]
    )


def summarise() -> dict:
    cells = load()
    tasks = sorted({task for task, _, _ in cells})
    out: dict = {"tasks": tasks, "n_tasks": len(tasks), "attacks": {}, "controls": {}}
    for arm in ("correct", "empty"):
        row = {}
        for level in LEVELS:
            rewards = [
                cells[(t, level, arm)]["reward"]
                for t in tasks
                if measured(cells.get((t, level, arm)))
            ]
            row[level] = {"measured": len(rewards), "scored_1": sum(r == 1.0 for r in rewards)}
        out["controls"][arm] = row
    false_rejects = [
        t
        for t in tasks
        if measured(cells.get((t, "H", "correct"))) and cells[(t, "H", "correct")]["reward"] != 1.0
    ]
    out["false_rejects_at_H"] = false_rejects
    for attack in ATTACKS:
        per_level = {}
        for level in LEVELS:
            rewards = {
                t: cells[(t, level, attack)]["reward"]
                for t in tasks
                if measured(cells.get((t, level, attack)))
            }
            per_level[level] = {
                "measured": len(rewards),
                "scored_1": sum(r == 1.0 for r in rewards.values()),
                "by_task": rewards,
            }
        live = [t for t, r in per_level["L0"]["by_task"].items() if r == 1.0]
        neutralised_h = [t for t in live if per_level["H"]["by_task"].get(t) == 0.0]
        neutralised_l1 = [t for t in live if per_level["L1"]["by_task"].get(t) == 0.0]
        neutralised_by_l3_only = [t for t in neutralised_h if t not in neutralised_l1]
        survivors = [t for t in live if per_level["H"]["by_task"].get(t) == 1.0]
        out["attacks"][attack] = {
            "live_at_L0": f"{len(live)}/{per_level['L0']['measured']}",
            "inert_at_L0": [t for t, r in per_level["L0"]["by_task"].items() if r == 0.0],
            "L1_only_scored_1_of_live": sum(per_level["L1"]["by_task"].get(t) == 1.0 for t in live),
            "H_scored_1_of_live": len(survivors),
            "neutralised_by_L1": len(neutralised_l1),
            "neutralised_only_with_L3": len(neutralised_by_l3_only),
            "neutralised_total": f"{len(neutralised_h)}/{len(live)}",
            "neutralised_wilson95": wilson(len(neutralised_h), len(live)),
            "survivor_tasks": survivors,
            "per_level": {
                lv: {k: v for k, v in d.items() if k != "by_task"} for lv, d in per_level.items()
            },
        }
    gates = defaultdict(int)
    for (_, level, arm), cell in cells.items():
        if level == "H" and arm in ATTACKS:
            gates[f"{arm}:{cell['gate']}:{cell['reward']}"] += 1
    out["H_gate_outcomes"] = dict(sorted(gates.items()))
    out["cost"] = {
        "cells": len(cells),
        "est_usd_cells": round(sum(c["est_usd"] for c in cells.values()), 4),
        "unmeasured": [f"{t}/{lv}/{a}" for (t, lv, a), c in cells.items() if not measured(c)],
    }
    return out


def main() -> None:
    summary = summarise()
    target = HERE / "results/matrix-summary.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    rows = [
        "task,level,arm,reward,attack_applied,gate,junit_tests_failures_errors_skipped,est_usd,evidence_sha256"
    ]
    for (task, level, arm), cell in sorted(load().items()):
        counts = "/".join(map(str, cell["junit_counts"])) if cell["junit_counts"] else ""
        rows.append(
            f"{task},{level},{arm},{cell['reward']},{cell['attack_applied']},{cell['gate'] or ''},{counts},{cell['est_usd']},{cell['evidence_sha256']}"
        )
    (target.parent / "cells.csv").write_text("\n".join(rows) + "\n")
    shutil.copy(OUT / "selection.json", target.parent / "selection.json")
    print(json.dumps({k: v for k, v in summary.items() if k not in ("attacks", "tasks")}, indent=1))
    for attack, row in summary["attacks"].items():
        print(
            attack,
            row["live_at_L0"],
            "L1-only still 1:",
            row["L1_only_scored_1_of_live"],
            "H still 1:",
            row["H_scored_1_of_live"],
            "neutralised",
            row["neutralised_total"],
            row["neutralised_wilson95"],
        )


if __name__ == "__main__":
    main()
