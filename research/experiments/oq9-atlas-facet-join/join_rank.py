"""OQ-9: zero-model test of facet-ranked auditing against the 44 keyed OQ-6 DEV tasks.

Rules, population and decision rule were preregistered in the bridge file
(opus-vs-swarm-qa.md, "OQ-9 ... PREREGISTERED") before this script scored anything.
Labels (OQ-6 keyed tasks) are joined only in `score`; ranking never reads them.
Unkeyed tasks are not certified negatives; precision here is the share of the
top-k tasks that carry a keyed OQ-6 defect annotation, a lower bound.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path

DB = Path.home() / ".local/state/evallab-prod-live/cloud-trace-db.sqlite"
FACETS = Path.home() / ".local/state/evallab-prod-live/run-atlas/facets-v1.jsonl"
POPULATION = Path(
    "/Users/petermakhnatch/Developer/eval-lab-results/2026-10-29/oq7-atlas-guided-audit/A/labels.jsonl"
)
KS = (10, 20, 44)

RULES = {
    "a_tests_changed": lambda f: f["tests"] in ("modified", "deleted", "replaced"),
    "b_answer_material": lambda f: bool(f["answer_material_accessed"]),
    "c_grader_touched": lambda f: bool(f["grader_or_harness_touched"]),
    "d_suspicion_ge2": lambda f: f["suspicion"] >= 2,
}


def task_hash(task_id: str) -> str:
    return hashlib.sha256(task_id.encode()).hexdigest()


def wilson(successes: int, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if n == 0:
        return (math.nan, math.nan)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (centre - half, centre + half)


def load_facets() -> tuple[dict[str, dict], int]:
    """Last record per run_id in file order."""
    latest: dict[str, dict] = {}
    records = 0
    with FACETS.open() as stream:
        for line in stream:
            if line.strip():
                record = json.loads(line)
                latest[record["run_id"]] = record
                records += 1
    return latest, records


def run_index() -> dict[str, tuple[str, float | None]]:
    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT trace_id, task_id, reward FROM agent_traces")
        return {trace: (task, reward) for trace, task, reward in rows}
    finally:
        connection.close()


def clusters() -> list[dict]:
    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT cluster_id, name, level, run_ids FROM run_clusters")
        return [
            {"cluster_id": c, "name": n, "level": lv, "run_ids": set(json.loads(r))}
            for c, n, lv, r in rows
        ]
    finally:
        connection.close()


def build_features() -> dict:
    """Label-free per-task event counts and population (no labels read here)."""
    facets, n_records = load_facets()
    index = run_index()
    population = {
        json.loads(line)["task_id"]
        for line in POPULATION.open()
        if line.strip()
    }
    per_task: dict[str, dict] = defaultdict(
        lambda: {"faceted_runs": 0, "pass_faceted_runs": 0, "counts": defaultdict(int),
                 "pass_counts": defaultdict(int), "runs": []}
    )
    dropped_no_task = 0
    dropped_not_population = 0
    for run_id, facet in facets.items():
        found = index.get(run_id)
        if found is None:
            dropped_no_task += 1
            continue
        task_id, reward = found
        if task_id not in population:
            dropped_not_population += 1
            continue
        row = per_task[task_id]
        row["faceted_runs"] += 1
        row["runs"].append(run_id)
        passed = reward is not None and reward >= 1
        row["pass_faceted_runs"] += passed
        for name, rule in RULES.items():
            if rule(facet):
                row["counts"][name] += 1
                if passed:
                    row["pass_counts"][name] += 1
    return {
        "records": n_records,
        "distinct_runs": len(facets),
        "dropped_no_task": dropped_no_task,
        "dropped_not_population": dropped_not_population,
        "population_size": len(population),
        "tasks": per_task,
        "population": population,
    }


PLAN = Path(
    "/Users/petermakhnatch/Developer/eval-lab-results/2026-10-27/oq6-tq-gate-rerun-v4/plan.json"
)


def cohort_tasks(population: set[str]) -> set[str]:
    """OQ-6 DEV cohort cases that map uniquely to eligible store tasks (same rule as OQ-7 labels.py)."""
    from evallab.production.corpus_queue import CorpusRegistry  # noqa: PLC0415

    registry = CorpusRegistry(Path(__file__).resolve().parents[3])
    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        tasks = dict(
            connection.execute("SELECT task_id, dataset_id FROM corpus_tasks WHERE lower(split)='dev'")
        )
    finally:
        connection.close()
    names: dict[tuple[str, str], list[str]] = defaultdict(list)
    for task, dataset in tasks.items():
        names[(dataset, registry.task_name(task))].append(task)
    found = set()
    for case in json.loads(PLAN.read_text())["cases"]:
        if case.get("stage") != "dev":
            continue
        matches = names.get((case["dataset_id"], registry.task_name(case.get("task_id") or case["item_id"])), [])
        if len(matches) == 1 and matches[0] in population:
            found.add(matches[0])
    return found


def keyed_oq6() -> set[str]:
    """Tasks with a non-empty OQ-6 admitted key cache (joined only at scoring)."""
    keyed = set()
    for line in POPULATION.open():
        row = json.loads(line)
        if row["keyed"] and any("scorer-only-keys/D" in s for s in row["key_sources"]):
            keyed.add(row["task_id"])
    return keyed


def order(tasks: list[str], score: dict[str, float]) -> list[str]:
    return sorted(tasks, key=lambda t: (-score.get(t, 0.0), task_hash(t)))


def precision_at(ranked: list[str], keyed: set[str]) -> dict:
    out = {}
    for k in KS:
        if k > len(ranked):
            continue
        hits = sum(t in keyed for t in ranked[:k])
        lo, hi = wilson(hits, k)
        out[str(k)] = {"hits": hits, "precision": hits / k, "wilson95": [lo, hi]}
    return out


def fisher_greater(a: int, b: int, c: int, d: int) -> float:
    """One-sided exact p for a >= observed in [[a,b],[c,d]]."""
    n = a + b + c + d
    row1, col1 = a + b, a + c

    def pmf(x: int) -> float:
        return math.comb(col1, x) * math.comb(n - col1, row1 - x) / math.comb(n, row1)

    return sum(pmf(x) for x in range(a, min(row1, col1) + 1))


def score(features: dict, keyed: set[str], population_name: str, tasks: list[str]) -> dict:
    n = len(tasks)
    positives = sorted(t for t in tasks if t in keyed)
    base = len(positives) / n if n else math.nan
    schemes: dict[str, dict] = {}
    schemes["baseline_task_hash"] = precision_at(order(tasks, {}), keyed)
    faceted = features["tasks"]
    for name in RULES:
        primary = {t: float(faceted[t]["counts"][name]) for t in tasks if t in faceted}
        schemes[f"{name}__count_all_runs"] = precision_at(order(tasks, primary), keyed)
        passed = {t: float(faceted[t]["pass_counts"][name]) for t in tasks if t in faceted}
        schemes[f"{name}__count_pass_runs"] = precision_at(order(tasks, passed), keyed)
        share = {
            t: faceted[t]["counts"][name] / faceted[t]["faceted_runs"]
            for t in tasks
            if t in faceted and faceted[t]["faceted_runs"]
        }
        schemes[f"{name}__share_of_faceted_runs"] = precision_at(order(tasks, share), keyed)
    exposure = {t: float(faceted[t]["faceted_runs"]) for t in tasks if t in faceted}
    schemes["control_most_faceted_runs"] = precision_at(order(tasks, exposure), keyed)
    verdicts = {}
    for name in RULES:
        r = schemes[f"{name}__count_all_runs"]
        ok = (
            "20" in r
            and "44" in r
            and r["20"]["wilson95"][0] > base
            and r["44"]["precision"] > base
        )
        beats_exposure = (
            ok
            and r["20"]["precision"] > schemes["control_most_faceted_runs"]["20"]["precision"]
            and r["44"]["precision"] > schemes["control_most_faceted_runs"]["44"]["precision"]
        )
        verdicts[name] = {"beats_random": bool(ok), "beats_exposure_control": bool(beats_exposure)}
    return {
        "population": population_name,
        "tasks": n,
        "keyed_tasks": len(positives),
        "keyed_without_facet_record": sorted(t for t in positives if t not in faceted),
        "base_rate_random_expectation": base,
        "tasks_without_facet_record": sum(t not in faceted for t in tasks),
        "schemes": schemes,
        "verdicts": verdicts,
    }


def cluster_enrichment(features: dict, keyed: set[str], tasks: list[str]) -> list[dict]:
    faceted = features["tasks"]
    task_set = set(tasks)
    run_to_task = {r: t for t, row in faceted.items() for r in row["runs"]}
    rows = []
    for cluster in clusters():
        members = {run_to_task[r] for r in cluster["run_ids"] if r in run_to_task} & task_set
        if len(members) < 5:
            continue
        a = len(members & keyed)
        b = len(members) - a
        c = len(keyed & task_set) - a
        d = len(task_set) - len(members) - c
        rows.append(
            {
                "cluster_id": cluster["cluster_id"],
                "name": cluster["name"],
                "level": cluster["level"],
                "tasks": len(members),
                "keyed": a,
                "keyed_share": a / len(members),
                "p_one_sided": fisher_greater(a, b, c, d),
            }
        )
    rows.sort(key=lambda r: r["p_one_sided"])
    m = len(rows)
    running = 1.0
    for rank in range(m, 0, -1):
        row = rows[rank - 1]
        running = min(running, row["p_one_sided"] * m / rank)
        row["q_bh"] = running
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    features = build_features()
    keyed = keyed_oq6()
    faceted_tasks = sorted(features["tasks"])
    cohort = sorted(cohort_tasks(features["population"]))
    result = {
        "inputs": {
            "facet_records": features["records"],
            "distinct_runs": features["distinct_runs"],
            "dropped_runs_not_in_store": features["dropped_no_task"],
            "dropped_runs_outside_population": features["dropped_not_population"],
            "population_tasks": features["population_size"],
            "tasks_with_facet_record": len(faceted_tasks),
            "oq6_keyed_tasks": len(keyed),
        },
        "primary_population_faceted_tasks": score(features, keyed, "faceted strict-DEV", faceted_tasks),
        "secondary_oq6_cohort_tasks": score(features, keyed, "OQ-6 DEV cohort tasks mapped to the store", cohort),
        "cluster_enrichment_exploratory": cluster_enrichment(features, keyed, faceted_tasks),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True, default=list))
    primary = result["primary_population_faceted_tasks"]
    print(json.dumps({"inputs": result["inputs"], "verdicts": primary["verdicts"]}, indent=2))
    print(json.dumps({"secondary_verdicts": result["secondary_oq6_cohort_tasks"]["verdicts"]}, indent=2))


if __name__ == "__main__":
    main()
