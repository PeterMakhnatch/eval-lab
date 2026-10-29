"""Step 0, label-free: how does ``grader_tamper`` fire? Firing rates and card structure only; no label is read.

Populations: strict-DEV Goodfire minus the 246 NP31 pair runs (``dev_rest``), CheatBench DEV, CM-18 established strata,
NP21 Flake. The 246 pair runs are not audited here. Writes ``audit.json`` (aggregates) and the per-run classified hits to
the private state directory.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gate  # noqa: E402

POPULATIONS = ("dev_rest", "cheatbench", "cm18", "np21")
RUNS: list = []


def one(index: int) -> dict:
    run = RUNS[index]
    hits = gate.classify(run.steps)
    return {"run_id": run.run_id, "steps": len(run.steps), "hits": hits, "match": gate.check(run.steps, hits)}


def tertile_edges(lengths: list[int]) -> tuple[int, int]:
    ordered = sorted(lengths)
    return ordered[len(ordered) // 3], ordered[2 * len(ordered) // 3]


def aggregate(rows: list[dict]) -> dict:
    n = len(rows)
    lo, hi = tertile_edges([r["steps"] for r in rows])
    out: dict = {"runs": n, "classifier_matches_production": sum(r["match"] for r in rows), "length_edges": [lo, hi]}
    fired = {name: [bool(fn(r["hits"])) for r in rows] for name, fn in gate.GATES.items()}
    out["fires"] = {name: sum(flags) for name, flags in fired.items()}
    out["fire_rate"] = {name: sum(flags) / n for name, flags in fired.items()}
    tert = [0 if r["steps"] <= lo else 1 if r["steps"] <= hi else 2 for r in rows]
    out["by_length_tertile"] = {
        name: [sum(f for f, t in zip(flags, tert, strict=True) if t == k) for k in range(3)] for name, flags in fired.items()}
    out["tertile_sizes"] = [tert.count(k) for k in range(3)]
    hit_runs = [r for r in rows if r["hits"]]
    out["hit_steps_per_run"] = dict(sorted(Counter(min(len(r["hits"]), 6) for r in hit_runs).items()))
    out["first_hit_route"] = dict(Counter(r["hits"][0]["paths"][0]["route"] for r in hit_runs))
    out["first_hit_surface"] = dict(Counter(r["hits"][0]["paths"][0]["surface"] for r in hit_runs))
    routes = Counter(p["route"] for r in rows for h in r["hits"] for p in h["paths"])
    out["path_routes"] = dict(routes)
    out["runs_with_route"] = dict(Counter(route for r in hit_runs for route in {p["route"] for h in r["hits"] for p in h["paths"]}))
    out["median_steps"] = {"hit": sorted(r["steps"] for r in hit_runs)[len(hit_runs) // 2] if hit_runs else None,
                           "no_hit": sorted(r["steps"] for r in rows if not r["hits"])[sum(1 for r in rows if not r["hits"]) // 2]
                           if any(not r["hits"] for r in rows) else None}
    return out


def sample_cards(rows: list[dict], count: int = 8) -> list[dict]:
    """Structure of a handful of fired cards (deterministic by sha256 of the run id): route, path, command excerpt."""
    by_id = {run.run_id: run for run in RUNS}
    hit_rows = sorted((r for r in rows if r["hits"]), key=lambda r: hashlib.sha256(r["run_id"].encode()).hexdigest())[:count]
    out = []
    for row in hit_rows:
        hit = row["hits"][0]
        step = next(s for s in by_id[row["run_id"]].steps if s["step_id"] == hit["step"])
        commands = [c for c in gate.tw._bash_commands(step)]
        out.append({"run_id": row["run_id"], "steps": row["steps"], "hit_steps": len(row["hits"]), "first_hit_step": hit["step"],
                    "paths": hit["paths"], "command_excerpt": (commands[0] if commands else "")[:260]})
    return out


def main() -> int:
    repo = Path.cwd()
    result: dict = {}
    for population in POPULATIONS:
        RUNS[:] = gate.load(population, repo)
        with mp.get_context("fork").Pool(8) as pool:
            rows = pool.map(one, range(len(RUNS)), chunksize=8)
        gate.save(f"audit-rows-{population}.pkl", rows)
        result[population] = aggregate(rows)
        if population == "dev_rest":
            result[population]["sample_cards"] = sample_cards(rows)
        print(population, json.dumps(result[population], sort_keys=True), flush=True)
    (gate.EVIDENCE / "audit.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
