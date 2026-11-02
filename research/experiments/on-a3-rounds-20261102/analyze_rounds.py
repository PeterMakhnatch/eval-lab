#!/usr/bin/env python3
"""A3 analysis from the per-cell receipts (local; no model or cloud calls).

Primary: among EVAL mutants the ORIGINAL grader accepted, kill rate of the arm-2 test vs the arm-1 test, paired by
mutant, `stats.paired` (exact McNemar from the discordant counts b and c). Arm k = the test accepted at round
min(k, last accepted round). Unmeasured cells (error, marker missing, cap) are excluded from pairs and listed.

    analyze_rounds.py            # writes results/summary.json, results/per-task.md and RAW/receipts copies
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUN = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight")
sys.path.insert(0, str(RUN))
import stats  # noqa: E402

OUT = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/A3")
RAW = RUN / "lanes/A3-raw"
APPLIED = "MUTANT_APPLIED"


def tag(m: dict) -> str:
    return f"{m['operator']}-L{m['line']}-{m['sha256'][:8]}"


def reward_of(cell: Path, m: dict) -> float | None:
    path = cell / "result.json"
    if not path.exists():
        return None
    result = json.loads(path.read_text())
    applied = f"{APPLIED} {m['operator']} {m['path']}:{m['line']} {m['sha256']}" in result.get(
        "agent_log", ""
    )
    if result["status"] != "executed" or not applied or result["reward"] not in (0.0, 1.0):
        return None
    return result["reward"]


def final_round(accepted: list[int], arm: int) -> int | None:
    ok = [r for r in accepted if r <= arm]
    return max(ok) if ok else None


def main() -> None:
    selection = json.loads((OUT / "selection.json").read_text())
    tasks = sorted(p.stem for p in (OUT / "rounds").glob("*.json"))
    rows = []
    pairs: dict[str, dict[str, list]] = {
        s: {} for s in ("all", "old", "round2_ran", "all_eval_base_or_not")
    }
    excluded: list[str] = []
    arm1_rows: list[tuple] = []
    for short in tasks:
        sel = selection["tasks"][short]
        record = json.loads((OUT / "rounds" / f"{short}.json").read_text())
        accepted = record["accepted_rounds"]
        guard = {r["round"]: r["guard"] for r in record["rounds"]}
        reference = {r["round"]: r["grades"].get("reference") for r in record["rounds"]}
        recorded = {int(k): v for k, v in record.get("keys", {}).items()}

        def key(rnd: int, guard: dict = guard, recorded: dict = recorded) -> str:
            # tasks finished before hash-suffixed keys existed keep the legacy names r<N> / r<N>f
            return recorded.get(rnd) or f"r{rnd}" + ("f" if guard.get(rnd) == "repaired" else "")

        evals = [(m, True) for m in sel["eval_old"]] + [(m, False) for m in sel["eval_extra"]]
        row = {
            "task": short,
            "stop": record["stop_reason"],
            "accepted_rounds": accepted,
            "guard": guard,
            "reference_reward_by_round": reference,
            "eval_total": len(evals),
            "base_accepted": 0,
            "kills": {1: 0, 2: 0, 3: 0},
            "unmeasured": 0,
        }
        blocked = min(
            (
                r["round"]
                for r in record["rounds"]
                if r["guard"] == "reverted" and r["grades"].get("reference") is None
            ),
            default=None,
        )
        for m, is_old in evals:
            base = reward_of(OUT / "cells" / short / "base" / f"eval-{tag(m)}", m)
            arm_kill: dict[int, bool | None] = {}
            for arm in (1, 2, 3):
                rnd = final_round(accepted, arm)
                # a round whose reference cell was never measured (spend cap, refused synthesis) is not an
                # early stop: arms from that round on are NOT MEASURED instead of carrying the older test
                if blocked is not None and arm >= blocked:
                    rnd = None
                reward = (
                    None
                    if rnd is None
                    else reward_of(OUT / "cells" / short / key(rnd) / f"eval-{tag(m)}", m)
                )
                arm_kill[arm] = None if reward is None else reward == 0.0
            if base is not None and arm_kill[1] is not None:
                arm1_rows.append((short, tag(m), base, arm_kill[1]))
            if base is None or any(v is None for v in arm_kill.values()):
                row["unmeasured"] += 1
                excluded.append(f"{short}/{tag(m)} base={base} arms={arm_kill}")
                continue
            if base == 1.0:
                row["base_accepted"] += 1
                for arm in (1, 2, 3):
                    row["kills"][arm] += int(arm_kill[arm])
                entry = (short, tag(m), arm_kill[1], arm_kill[2], arm_kill[3])
                pairs["all"].setdefault("rows", []).append(entry)
                if is_old:
                    pairs["old"].setdefault("rows", []).append(entry)
                if 2 in accepted:
                    pairs["round2_ran"].setdefault("rows", []).append(entry)
            pairs["all_eval_base_or_not"].setdefault("rows", []).append(
                (short, tag(m), arm_kill[1], arm_kill[2], arm_kill[3])
            )
        rows.append(row)

    def compare(rows_: list, a: int, b: int) -> dict:
        if not rows_:
            return {"n": 0}
        xs = [r[1 + a] for r in rows_]
        ys = [r[1 + b] for r in rows_]
        out = stats.paired(xs, ys)
        out["arm_a"], out["arm_b"] = f"arm{a}", f"arm{b}"
        out["a_kill"] = stats.fmt(sum(xs), len(xs))
        out["b_kill"] = stats.fmt(sum(ys), len(ys))
        out["meaning"] = f"b = killed only by arm {a}; c = killed only by arm {b}"
        return out

    summary: dict = {
        "tasks": tasks,
        "primary_arm2_vs_arm1_base_accepted_all_eval": compare(pairs["all"].get("rows", []), 1, 2),
        "arm3_vs_arm1_base_accepted": compare(pairs["all"].get("rows", []), 1, 3),
        "arm3_vs_arm2_base_accepted": compare(pairs["all"].get("rows", []), 2, 3),
        "arm2_vs_arm1_base_accepted_eval_old_only": compare(pairs["old"].get("rows", []), 1, 2),
        "arm2_vs_arm1_base_accepted_tasks_where_round2_ran": compare(
            pairs["round2_ran"].get("rows", []), 1, 2
        ),
        "arm2_vs_arm1_all_eval_regardless_of_base": compare(
            pairs["all_eval_base_or_not"].get("rows", []), 1, 2
        ),
        "arm1_only_base_accepted_kill_rate": (
            lambda rs: {
                "base_accepted_measured": len(rs),
                "killed": stats.fmt(sum(r[3] for r in rs), len(rs)),
                "mutants": [f"{r[0]}/{r[1]}" for r in rs],
            }
        )([r for r in arm1_rows if r[2] == 1.0]),
        "per_task": rows,
        "excluded_unmeasured": excluded,
    }
    probes = {"measured": 0, "survived": 0, "killed": 0, "unmeasured": 0}
    guards = {
        "accepted_passed": 0,
        "repaired": 0,
        "reverted": 0,
        "reference_rewards_after_accepted_rounds": [],
    }
    per_round: dict[int, dict[str, int]] = {}
    for short in tasks:
        record = json.loads((OUT / "rounds" / f"{short}.json").read_text())
        for r in record["rounds"]:
            slot = per_round.setdefault(
                r["round"], {"tasks": 0, "probes": 0, "survived": 0, "killed": 0, "unmeasured": 0}
            )
            slot["tasks"] += 1
            slot["probes"] += len(r["probe_shas"])
            slot["survived"] += len(r["survivors"])
            slot["killed"] += len(r["killed"])
            slot["unmeasured"] += len(r["unmeasured"])
            if r["guard"] == "passed":
                guards["accepted_passed"] += 1
            elif r["guard"] == "repaired":
                guards["repaired"] += 1
            else:
                guards["reverted"] += 1
            if r["guard"] != "reverted":
                guards["reference_rewards_after_accepted_rounds"].append(
                    r["grades"].get("reference")
                )
            for k in probes:
                if k != "measured":
                    probes[k] += len(
                        r[
                            {
                                "survived": "survivors",
                                "killed": "killed",
                                "unmeasured": "unmeasured",
                            }[k]
                        ]
                    )
    probes["measured"] = probes["survived"] + probes["killed"]
    summary["probes_by_round"] = per_round
    summary["probes_total"] = probes
    summary["false_reject_guard"] = guards

    served = 0
    other = 0
    synth_cost = 0.0
    for meta in (OUT / "synth").glob("*/*.json"):
        d = json.loads(meta.read_text())
        if d.get("served_model") == d.get("requested_model"):
            served += 1
        else:
            other += 1
        synth_cost += d.get("cost_usd", 0.0)
    summary["attestation"] = {
        "served_equals_requested": served,
        "other_or_fallback": other,
        "reported_model_cost_usd": round(synth_cost, 4),
    }

    gates: dict[str, list] = {}
    for p in (OUT / "cells").glob("*/gates-*/*/result.json"):  # one cell dir per gate
        r = json.loads(p.read_text())
        gates.setdefault(p.parent.parent.parent.name, []).append(
            (p.parent.parent.name, p.parent.name, r["reward"])
        )
    summary["gates"] = gates

    cost_by_task: dict[str, float] = {}
    total = 0.0
    for p in [*(OUT / "cells").glob("*/*/result.json"), *(OUT / "cells").glob("*/*/*/result.json")]:
        if "trial" in p.parts:
            continue
        e = json.loads(p.read_text()).get("est_usd", 0.0)
        cost_by_task[p.relative_to(OUT / "cells").parts[0]] = (
            cost_by_task.get(p.relative_to(OUT / "cells").parts[0], 0.0) + e
        )
        total += e
    retired = (
        sum(1 for _ in (OUT / "retired-infra-failed").glob("*/*.json"))
        if (OUT / "retired-infra-failed").exists()
        else 0
    )
    summary["cost"] = {
        "est_usd_total": round(total, 4),
        "est_usd_by_task": {k: round(v, 4) for k, v in sorted(cost_by_task.items())},
        "retired_infra_failed_cells": retired,
    }

    lines = [
        "| task | stop | accepted rounds | guard | EVAL | base-accepted | arm1 kills | arm2 kills | arm3 kills | unmeasured |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['task']} | {r['stop']} | {r['accepted_rounds']} | {r['guard']} | {r['eval_total']} | {r['base_accepted']} | {r['kills'][1]} | {r['kills'][2]} | {r['kills'][3]} | {r['unmeasured']} |"
        )
    target = HERE / "results"
    target.mkdir(exist_ok=True)
    (target / "summary.json").write_text(
        json.dumps(summary, indent=1, sort_keys=True, default=str) + "\n"
    )
    (target / "per-task.md").write_text("\n".join(lines) + "\n")
    (RAW / "receipts").mkdir(parents=True, exist_ok=True)
    for name in ("summary.json", "per-task.md"):
        shutil.copy(target / name, RAW / "receipts" / name)
    shutil.copy(target / "summary.json", OUT / "summary.json")
    csv_rows = ["task,cell,status,reward,applied_marker,est_usd,wall_s,evidence_sha256"]
    for p in sorted(
        [*(OUT / "cells").glob("*/*/result.json"), *(OUT / "cells").glob("*/*/*/result.json")]
    ):
        if "trial" in p.parts:
            continue
        r = json.loads(p.read_text())
        if "status" not in r:
            continue
        rel = p.relative_to(OUT / "cells")
        applied = "MUTANT_APPLIED" in r.get("agent_log", "") or "_APPLIED" in r.get("agent_log", "")
        csv_rows.append(
            f"{rel.parts[0]},{'/'.join(rel.parts[1:-1])},{r['status']},{r['reward']},{applied},"
            f"{r.get('est_usd', 0)},{r.get('wall_s', 0)},{r.get('evidence_sha256', '')}"
        )
    (target / "cells.csv").write_text("\n".join(csv_rows) + "\n")
    shutil.copy(target / "cells.csv", RAW / "receipts" / "cells.csv")
    print(
        json.dumps(
            {k: v for k, v in summary.items() if k not in ("per_task", "excluded_unmeasured")},
            indent=1,
            default=str,
        )[:4000]
    )
    print("\n".join(lines))


if __name__ == "__main__":
    main()
