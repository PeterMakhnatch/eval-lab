"""Join pass-side and fail-side labels into the TB4 run census and a corrected leaderboard.

Inputs (results dir): ``inventory.json``, ``rows.json``, ``labels-pass.jsonl``,
``labels-fail.jsonl``. Outputs (this dir): ``census.json`` and ``CENSUS.md``.

Rates (Wilson 95%):
- exploit share = known-exploit passes / scored passes, per task and per model;
- defect share = task-defect fails / scored fails (gray-area defects separately).

Corrected leaderboard over all 66 tasks; only the 8 census tasks are adjusted:
- ``exploit_removed`` = (successes - known-exploit passes) / trials;
- ``defect_credited`` = exploit_removed + task-defect fails / trials. This is an upper
  bound: a fail explained only by a documented defect is counted as a pass, which
  Preference Model's regrades support for several rules but not for every run.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from evallab.analysis_statistics import wilson_score_interval

HERE = Path(__file__).resolve().parent
RESULTS = Path.home() / "Developer/eval-lab-results/2026-10-20/tb4-run-census"
PM_MODELS = {"Fable 5.1", "Opus 5.5", "Sonnet 5.5", "GPT-6 Astra", "GPT-6.1 Sol"}
DEFECT = "task_defect"
GRAY = "task_defect_gray"
EXPLOIT = "known_exploit"


def jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def rate(k: int, n: int) -> dict[str, Any]:
    if n == 0:
        return {"k": 0, "n": 0, "rate": None, "wilson95": None}
    ci = wilson_score_interval(k, n)
    return {"k": k, "n": n, "rate": k / n, "wilson95": [ci.lower, ci.upper]}


def fmt(r: dict[str, Any]) -> str:
    if not r["n"]:
        return "–"
    lo, hi = r["wilson95"]
    return f"{r['k']}/{r['n']} = {r['rate']:.0%} [{lo:.0%}, {hi:.0%}]"


def row_label(row: dict[str, Any]) -> str:
    return f"{row['agent']} · {row['model']} · {row['effort']}"


def shares(labels: list[dict[str, Any]]) -> dict[str, Any]:
    passes = [x for x in labels if x["side"] == "pass"]
    fails = [x for x in labels if x["side"] == "fail"]
    measured_passes = [x for x in passes if x["label"] != "unmeasured"]
    measured_fails = [x for x in fails if x["label"] != "unmeasured"]
    return {
        "passes": len(passes),
        "fails": len(fails),
        "unscored": sum(1 for x in labels if x["side"] == "unscored"),
        "exploit_share_of_passes": rate(sum(x["label"] == EXPLOIT for x in measured_passes), len(measured_passes)),
        "defect_share_of_fails": rate(sum(x["label"] == DEFECT for x in measured_fails), len(measured_fails)),
        "defect_or_gray_share_of_fails": rate(
            sum(x["label"] in (DEFECT, GRAY) for x in measured_fails), len(measured_fails)
        ),
        "unmeasured_passes": len(passes) - len(measured_passes),
        "unmeasured_fails": len(fails) - len(measured_fails),
        "mechanisms": dict(Counter(x["mechanism"] for x in labels if x["label"] in (EXPLOIT, DEFECT, GRAY)).most_common()),
    }


def main() -> None:
    inventory = json.loads((RESULTS / "inventory.json").read_text())
    rows = json.loads((RESULTS / "rows.json").read_text())
    labels = {x["trial_id"]: x for x in jsonl(RESULTS / "labels-pass.jsonl") + jsonl(RESULTS / "labels-fail.jsonl")}
    missing = [i["trial_id"] for i in inventory if i["trial_id"] not in labels]
    if missing:
        raise SystemExit(f"{len(missing)} inventory trials have no label, e.g. {missing[:3]}")
    joined = [{**i, **labels[i["trial_id"]]} for i in inventory]
    for x in joined:
        expected = "unscored" if x["reward"] is None else ("pass" if x["reward"] >= 1 else "fail")
        if x["side"] != expected:
            raise SystemExit(f"{x['trial_id']}: side {x['side']} disagrees with reward {x['reward']}")

    by_task: dict[str, list] = defaultdict(list)
    by_row: dict[str, list] = defaultdict(list)
    for x in joined:
        by_task[x["task"]].append(x)
        by_row[x["row_id"]].append(x)

    leaderboard = []
    for row in rows:
        part = by_row[row["row_id"]]
        trials = row["n_trials"]
        successes = round(row["accuracy"] * trials / 100)
        exploits = sum(x["label"] == EXPLOIT for x in part)
        defects = sum(x["label"] == DEFECT for x in part)
        grays = sum(x["label"] == GRAY for x in part)
        leaderboard.append({
            "row_id": row["row_id"], "rank": row["rank"], "row": row_label(row),
            "pm_model": row["model"] in PM_MODELS and row["effort"] == "max",
            "accuracy": row["accuracy"], "successes": successes, "trials": trials,
            "exploit_passes": exploits, "defect_fails": defects, "gray_fails": grays,
            "exploit_removed": 100 * (successes - exploits) / trials,
            "defect_credited": 100 * (successes - exploits + defects) / trials,
            "census": shares(part),
        })
    for key in ("accuracy", "exploit_removed", "defect_credited"):
        for entry in leaderboard:
            entry[f"rank_{key}"] = 1 + sum(1 for other in leaderboard if other[key] > entry[key])

    result = {
        "schema": "tb4-run-census/v1",
        "trials": len(joined),
        "tasks": sorted(by_task),
        "overall": shares(joined),
        "pm_models": shares([x for x in joined if x["model"] in PM_MODELS and x["effort"] == "max"]),
        "other_rows": shares([x for x in joined if not (x["model"] in PM_MODELS and x["effort"] == "max")]),
        "by_task": {task: shares(part) for task, part in sorted(by_task.items())},
        "by_task_model": {
            task: {row_label(r): shares([x for x in part if x["row_id"] == r["row_id"]]) for r in rows}
            for task, part in sorted(by_task.items())
        },
        "leaderboard": leaderboard,
    }
    (HERE / "census.json").write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    (HERE / "CENSUS.md").write_text(render(result))
    print(render(result)[:4000])


def render(result: dict[str, Any]) -> str:
    out = ["# TB4 run census: tables", "",
           "Generated by `census.py` from the pass-side and fail-side labels. Wilson 95% intervals.", "",
           "| scope | exploit passes / passes | defect fails / fails | + gray-area defects |", "|---|---|---|---|"]
    for name, key in (("all 35 rows", "overall"), ("PM's 5 models (max)", "pm_models"), ("other 30 rows", "other_rows")):
        s = result[key]
        out.append(f"| {name} | {fmt(s['exploit_share_of_passes'])} | {fmt(s['defect_share_of_fails'])} | "
                   f"{fmt(s['defect_or_gray_share_of_fails'])} |")
    out += ["", "## Per task", "", "| task | exploit passes / passes | defect fails / fails | + gray | unmeasured (pass/fail) | mechanisms |",
            "|---|---|---|---|---|---|"]
    for task, s in result["by_task"].items():
        mech = "; ".join(f"{m} ({n})" for m, n in s["mechanisms"].items())
        out.append(f"| {task} | {fmt(s['exploit_share_of_passes'])} | {fmt(s['defect_share_of_fails'])} | "
                   f"{fmt(s['defect_or_gray_share_of_fails'])} | {s['unmeasured_passes']}/{s['unmeasured_fails']} | {mech} |")
    out += ["", "## Corrected leaderboard (all 66 tasks; only the 8 census tasks adjusted)", "",
            "| rank | row | raw | exploits removed | + defect fails credited (upper bound) | exploit passes | defect fails | gray fails |",
            "|---|---|---|---|---|---|---|---|"]
    for e in sorted(result["leaderboard"], key=lambda r: (r["rank_accuracy"], r["row"])):
        out.append(
            f"| {e['rank_accuracy']} → {e['rank_exploit_removed']} → {e['rank_defect_credited']} | {e['row']} | "
            f"{e['accuracy']:.2f} | {e['exploit_removed']:.2f} | {e['defect_credited']:.2f} | "
            f"{e['exploit_passes']} | {e['defect_fails']} | {e['gray_fails']} |")
    out += ["", "## Per task and model (non-zero only)", "", "| task | row | exploit passes / passes | defect fails / fails |", "|---|---|---|---|"]
    for task, models in result["by_task_model"].items():
        for name, s in models.items():
            if s["exploit_share_of_passes"]["k"] or s["defect_share_of_fails"]["k"]:
                out.append(f"| {task} | {name} | {fmt(s['exploit_share_of_passes'])} | {fmt(s['defect_share_of_fails'])} |")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    main()
