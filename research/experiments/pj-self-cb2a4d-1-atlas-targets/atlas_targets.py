"""pJ-SELF-cb2a4d-1: do label-free Run Atlas clusters pick keyed-defect tasks within a dataset, held out?

Rules, populations and the decision rule were preregistered in the bridge (opus-vs-swarm-qa.md, "pJ-SELF-cb2a4d-1")
before any label was joined. Zero model calls. Unkeyed tasks are not certified negatives, so AUC here measures
"known keyed defect", not "defect present". Labels enter only in `evaluate`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
from collections import defaultdict
from pathlib import Path

DB = Path.home() / ".local/state/evallab-prod-live/cloud-trace-db.sqlite"
FACETS = Path.home() / ".local/state/evallab-prod-live/run-atlas/facets-v1.jsonl"
POPULATION = Path(
    "/Users/petermakhnatch/Developer/eval-lab-results/2026-10-29/oq7-atlas-guided-audit/A/labels.jsonl"
)
FACET_RULES = {
    "tests_changed": lambda f: f["tests"] in ("modified", "deleted", "replaced"),
    "answer_material": lambda f: bool(f["answer_material_accessed"]),
    "grader_touched": lambda f: bool(f["grader_or_harness_touched"]),
    "suspicion_ge2": lambda f: f["suspicion"] >= 2,
}
MIN_TRAIN_TASKS = 3
BOOTSTRAP = 2000


def parity(task_id: str) -> int:
    return int(hashlib.sha256(task_id.encode()).hexdigest(), 16) % 2


def task_hash(task_id: str) -> str:
    return hashlib.sha256(task_id.encode()).hexdigest()


def auc(scores: dict[str, float], positives: set[str], tasks: list[str]) -> float | None:
    pos = [scores[t] for t in tasks if t in positives]
    neg = [scores[t] for t in tasks if t not in positives]
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def bootstrap_auc(scores: dict[str, float], positives: set[str], tasks: list[str]) -> list[float] | None:
    rng = random.Random(0)
    draws = []
    for _ in range(BOOTSTRAP):
        sample = [rng.choice(tasks) for _ in tasks]
        value = auc(scores, positives, sample)
        if value is not None:
            draws.append(value)
    if len(draws) < BOOTSTRAP // 2:
        return None
    draws.sort()
    return [round(draws[int(0.025 * len(draws))], 3), round(draws[int(0.975 * len(draws))], 3)]


def load_world() -> dict:
    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        run_task = dict(connection.execute("SELECT trace_id, task_id FROM agent_traces"))
        clusters = [
            (cid, lvl, set(json.loads(runs)))
            for cid, lvl, runs in connection.execute("SELECT cluster_id, level, run_ids FROM run_clusters")
            if lvl in (1, 2, 3)
        ]
    finally:
        connection.close()
    runs_per_task: dict[str, int] = defaultdict(int)
    for task in run_task.values():
        runs_per_task[task] += 1
    task_clusters: dict[str, set[str]] = defaultdict(set)
    for cid, _, members in clusters:
        for run in members:
            if run in run_task:
                task_clusters[run_task[run]].add(cid)
    latest: dict[str, dict] = {}
    for line in FACETS.open():
        if line.strip():
            record = json.loads(line)
            latest[record["run_id"]] = record
    facet_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for run, record in latest.items():
        task = run_task.get(run)
        if task is None:
            continue
        for name, rule in FACET_RULES.items():
            if rule(record):
                facet_counts[task][name] += 1
    return {"runs": runs_per_task, "clusters": task_clusters, "facets": facet_counts}


def fit_cluster_shares(train: list[str], keyed: set[str], world: dict) -> tuple[dict[str, float], float]:
    seen: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for task in train:
        for cid in world["clusters"].get(task, ()):
            seen[cid][0] += task in keyed
            seen[cid][1] += 1
    shares = {cid: (k + 1) / (n + 2) for cid, (k, n) in seen.items() if n >= MIN_TRAIN_TASKS}
    prior = (sum(t in keyed for t in train) + 1) / (len(train) + 2)
    return shares, prior


def scores_for(rule: str, test: list[str], world: dict, shares: dict, prior: float) -> dict[str, float]:
    result = {}
    for task in test:
        if rule == "cluster_share":
            values = [shares[c] for c in world["clusters"].get(task, ()) if c in shares]
            result[task] = max(values) if values else prior
        elif rule == "runs_exposure":
            result[task] = float(world["runs"].get(task, 0))
        else:
            result[task] = float(world["facets"].get(task, {}).get(rule, 0))
    return result


def top_k(scores: dict[str, float], positives: set[str], tasks: list[str], k: int) -> dict:
    ranked = sorted(tasks, key=lambda t: (-scores[t], task_hash(t)))[:k]
    hits = sum(t in positives for t in ranked)
    return {"k": len(ranked), "keyed_in_top_k": hits, "base_rate": round(len(positives & set(tasks)) / len(tasks), 3)}


def evaluate(dataset_rows: list[dict], world: dict) -> dict:
    tasks = sorted(row["task_id"] for row in dataset_rows)
    keyed = {row["task_id"] for row in dataset_rows if row["keyed"]}
    halves = {0: [t for t in tasks if parity(t) == 0], 1: [t for t in tasks if parity(t) == 1]}
    rules = ["cluster_share", "runs_exposure", *FACET_RULES]
    out: dict = {"tasks": len(tasks), "keyed": len(keyed), "directions": {}}
    for train_half, test_half in ((0, 1), (1, 0)):
        train, test = halves[train_half], halves[test_half]
        shares, prior = fit_cluster_shares(train, keyed, world)
        per_rule = {}
        for rule in rules:
            scores = scores_for(rule, test, world, shares, prior)
            per_rule[rule] = {
                "auc": None if auc(scores, keyed, test) is None else round(auc(scores, keyed, test), 3),
                "auc_ci95": bootstrap_auc(scores, keyed, test),
                "top10": top_k(scores, keyed, test, 10),
            }
        out["directions"][f"fit_parity{train_half}_test_parity{test_half}"] = {
            "train_tasks": len(train), "test_tasks": len(test), "test_keyed": len(keyed & set(test)),
            "fitted_clusters": len(shares), "rules": per_rule,
        }
    verdicts = {}
    for rule in rules:
        if rule == "runs_exposure":
            continue
        helps = True
        for direction in out["directions"].values():
            row, base = direction["rules"][rule], direction["rules"]["runs_exposure"]
            if (
                row["auc_ci95"] is None or row["auc"] is None or base["auc"] is None
                or not (row["auc_ci95"][0] > 0.5 and row["auc"] > base["auc"])
            ):
                helps = False
        verdicts[rule] = helps
    out["helps_by_preregistered_rule"] = verdicts
    return out


def goodfire_coverage(rows: list[dict], world: dict) -> dict:
    tasks = [r["task_id"] for r in rows]
    keyed = {r["task_id"] for r in rows if r["keyed"]}
    clustered = {t for t in tasks if world["clusters"].get(t)}
    faceted = {t for t in tasks if world["facets"].get(t) is not None and t in world["facets"]}
    return {
        "tasks": len(tasks), "keyed": len(keyed), "tasks_in_a_cluster": len(clustered),
        "keyed_in_a_cluster": len(keyed & clustered), "tasks_with_facets": len(faceted),
        "keyed_with_facets": len(keyed & faceted),
        "share_of_keyed_the_atlas_can_rank": round(len(keyed & clustered) / len(keyed), 3) if keyed else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    world = load_world()
    by_dataset: dict[str, list[dict]] = defaultdict(list)
    for line in POPULATION.open():
        if line.strip():
            row = json.loads(line)
            by_dataset[row["dataset_id"]].append(row)
    result = {
        "mimo": evaluate(by_dataset["mimo-defect-map-v1"], world),
        "goodfire_coverage_only": goodfire_coverage(by_dataset["goodfire-reward-hack-data"], world),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True))
    print(json.dumps({"mimo_verdicts": result["mimo"]["helps_by_preregistered_rule"],
                      "goodfire": result["goodfire_coverage_only"]}, indent=2))


if __name__ == "__main__":
    main()
