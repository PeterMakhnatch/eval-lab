"""R29-TW-1: per-tripwire recall and false-alarm rate from ``replay.py`` output (counts only are written to the repo).

Groups: DEV = Goodfire strict DEV (positives enacted, negatives clean) and CheatBench DEV (75 / 75); unseen = the staged cross-test
sets cm18, np21, np33 by set and stratum (``negative_established`` and ``negative_other`` / ``negative`` / ``negative_untrusted``
kept apart). Decision population for demotion: pooled strict-DEV negatives (Goodfire clean + CheatBench clean); the unseen sets
are read-only and never tuned on.

Per alert rule: how often the live_watch integrity alert fires (``alert``), how often it fires as a Live Supervisor ``trace_alarm``
trigger before the change (``before``: the rule is in ``INTEGRITY_RULES``) and after it (``after``: ``triggers_for`` on the alert).
Run-level ``any trigger`` is the union over rules.
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

CACHE = Path.home() / ".local/state/evallab-worker-evidence/r29-tw1-20261029"
RESULTS = Path(__file__).resolve().parent.parent / "results"
DEV_POPS = ("goodfire_dev", "cheatbench_dev")


def wilson(k: int, n: int, z: float = 1.959964) -> list[float]:
    if n == 0:
        return [0.0, 100.0]
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(100 * max(0.0, centre - half), 1), round(100 * min(1.0, centre + half), 1)]


def cell(k: int, n: int) -> dict:
    return {"k": k, "n": n, "rate": round(100 * k / n, 1) if n else None, "wilson": wilson(k, n)}


def main() -> int:
    rows = json.loads((CACHE / "alerts-v2.json").read_text())
    rules = sorted({r for row in rows for r in row["rules"]})
    trigger_rules = sorted({r for row in rows for r in row["before"]})
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[f"{row['pop']}|{row['set']}|{row['stratum']}"].append(row)
        groups[f"{row['pop']}|ALL|{row['stratum']}"].append(row)
    groups["DEV_pooled|ALL|positive"] = [r for r in rows if r["pop"] in DEV_POPS and r["positive"]]
    groups["DEV_pooled|ALL|negative"] = [
        r for r in rows if r["pop"] in DEV_POPS and not r["positive"]
    ]

    def block(members: list[dict], key: str, rule: str | None) -> dict:
        return cell(sum((rule in r[key]) if rule else bool(r[key]) for r in members), len(members))

    out: dict = {
        "runs": len(rows),
        "errors": sum(bool(r["error"]) for r in rows),
        "rules": {},
        "any_trigger": {},
    }
    for rule in rules:
        out["rules"][rule] = {
            name: {
                "alert": block(m, "rules", rule),
                "trigger_before": block(m, "before", rule),
                "trigger_after": block(m, "after", rule),
            }
            for name, m in sorted(groups.items())
        }
    for name, members in sorted(groups.items()):
        out["any_trigger"][name] = {
            "before": block(members, "before", None),
            "after": block(members, "after", None),
        }
    mech = defaultdict(int)
    for r in rows:
        if r["pop"] in DEV_POPS:
            for m in r["mechanisms"]:
                mech[(r["positive"], m)] += 1
    out["grader_tamper_mechanism_runs_dev"] = {
        f"{'positive' if p else 'negative'}:{m}": n for (p, m), n in sorted(mech.items())
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "per_rule.json").write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")

    print(
        "runs",
        out["runs"],
        "errors",
        out["errors"],
        "trigger rules observed (before):",
        trigger_rules,
    )
    for rule in rules:
        print(f"\n== {rule}")
        for name in ("DEV_pooled|ALL|positive", "DEV_pooled|ALL|negative"):
            c = out["rules"][rule][name]
            print(
                f"  {name}: alert {c['alert']['k']}/{c['alert']['n']}  before {c['trigger_before']['k']}  after {c['trigger_after']['k']}"
            )
        for name, c in out["rules"][rule].items():
            if name.startswith("DEV_pooled") or not (c["alert"]["k"] or c["trigger_after"]["k"]):
                continue
            print(
                f"    {name}: alert {c['alert']['k']}/{c['alert']['n']} before {c['trigger_before']['k']} after {c['trigger_after']['k']}"
            )
    print("\nANY TRIGGER")
    for name, c in out["any_trigger"].items():
        print(
            f"  {name}: before {c['before']['k']}/{c['before']['n']} after {c['after']['k']}/{c['after']['n']}"
        )
    print(out["grader_tamper_mechanism_runs_dev"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
