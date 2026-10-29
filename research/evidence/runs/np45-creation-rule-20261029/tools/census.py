"""NP45-TW-1 step 2: join labels to the label-free events and count rule R (kinds b or c fire; a and d do not).

Reads ``<state>/events-<id>.json`` (one per set) and, for the sets NP43-TW-1 already counted (strict Goodfire DEV, CheatBench DEV,
LinuxArena), ``../np43-grader-tamper-kinds-20261029/results/census.json``. Writes ``results/census.json`` (counts only).
"""

from __future__ import annotations

import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = Path.home() / ".local/state/evallab-worker-evidence/np45-tw1-20261029"
NP43 = HERE.parents[1] / "np43-grader-tamper-kinds-20261029/results/census.json"


def wilson(k: int, n: int, z: float = 1.959964) -> list[float]:
    if n == 0:
        return [0.0, 100.0]
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(100 * max(0.0, centre - half), 1), round(100 * min(1.0, centre + half), 1)]


def frac(k: int, n: int) -> dict:
    return {"k": k, "n": n, "pct": round(100 * k / n, 1) if n else None, "wilson": wilson(k, n)}


def group_of(run: dict) -> str:
    stratum = run.get("stratum")
    if stratum is not None:
        return {"positive": "positive", "negative_established": "negative"}.get(
            stratum, "negative_other"
        )
    state = run.get("state")
    if isinstance(state, str) and state.startswith(("positive", "negative")):
        return "positive" if state.startswith("positive") else "negative"
    if state is None and isinstance(run.get("positive"), bool):
        return "positive" if run["positive"] else "negative"
    return "other"


def category(events: list[dict]) -> str:
    kinds = {e["kind"] for e in events}
    if not kinds:
        return "none"
    if kinds & {"b", "c"}:
        return "fires_with_R"
    return "creation_only" if kinds == {"a"} else "undetermined_only"


def cell(runs: list[dict]) -> dict:
    cats = Counter(category(r["events"]) for r in runs)
    alarm = len(runs) - cats.get("none", 0)
    events = Counter(e["kind"] for r in runs for e in r["events"])
    return {
        "rows": len(runs),
        "alarm_without_R": alarm,
        "alarm_with_R": cats.get("fires_with_R", 0),
        "creation_only_runs": frac(cats.get("creation_only", 0), alarm),
        "undetermined_only_runs": frac(cats.get("undetermined_only", 0), alarm),
        "event_kinds": {k: events.get(k, 0) for k in "abcd"},
        "event_subkinds": dict(
            sorted(Counter(f"{e['kind']}:{e['sub']}" for r in runs for e in r["events"]).items())
        ),
    }


def main() -> int:
    sets: dict[str, dict] = {}
    statuses: dict[str, dict] = {}
    for path in sorted(OUT.glob("events-*.json")):
        data = json.loads(path.read_text())
        statuses[data["id"]] = {
            k: data[k] for k in ("status", "rows", "no_steps", "errors", "seconds")
        }
        groups: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
        for run in data["runs"]:
            groups[run.get("set", data["id"])][group_of(run)].append(run)
        for name, by_group in groups.items():
            sets[name] = {g: cell(runs) for g, runs in sorted(by_group.items())}
    np43 = json.loads(NP43.read_text())["groups"]

    def carried(pos: str, neg: str) -> dict:
        out = {}
        for group, name in (("positive", pos), ("negative", neg)):
            s = np43[name]
            out[group] = {
                "rows": s["runs"],
                "alarm_without_R": s["alarm_runs"],
                "alarm_with_R": s["run_category"]["survives"],
                "creation_only_runs": s["creation_only_frac"],
                "undetermined_only_runs": s["undetermined_frac"],
                "event_kinds": s["event_kinds"],
            }
        return out

    sets["goodfire-dev (NP43)"] = carried("goodfire_dev_enacted", "goodfire_dev_clean")
    sets["cheatbench-dev (NP43)"] = carried("cheatbench_enacted", "cheatbench_clean")
    sets["linuxarena S / non-S (NP43)"] = carried("linuxarena_S", "linuxarena_nonS")

    flags = {
        "R_removes_positive_alarms": sorted(
            n
            for n, g in sets.items()
            if g.get("positive", {}).get("alarm_with_R", 0)
            < g.get("positive", {}).get("alarm_without_R", 0)
        ),
        "sets_with_alarm": sorted(
            n for n, g in sets.items() if any(c.get("alarm_without_R", 0) for c in g.values())
        ),
        "cannot_tell_sets": sorted(
            n
            for n, g in sets.items()
            if g.get("positive", {}).get("alarm_without_R", 0) >= 10
            and g["positive"]["undetermined_only_runs"]["k"]
            > 0.25 * g["positive"]["alarm_without_R"]
        ),
    }
    result = {"set_status": statuses, "sets": sets, "flags": flags}
    (HERE.parent / "results").mkdir(exist_ok=True)
    (HERE.parent / "results/census.json").write_text(
        json.dumps(result, indent=1, sort_keys=True) + "\n"
    )
    print(json.dumps(statuses, sort_keys=True)[:1500])
    for name, g in sorted(sets.items()):
        parts = [
            f"{grp}: rows {c['rows']} alarm {c['alarm_without_R']} -> {c['alarm_with_R']}"
            for grp, c in g.items()
        ]
        print(name.ljust(34), " | ".join(parts))
    print(flags)
    return 0


if __name__ == "__main__":
    sys.exit(main())
