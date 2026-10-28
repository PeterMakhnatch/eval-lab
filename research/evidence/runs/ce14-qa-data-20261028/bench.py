"""CE-14: tokens and latency per `evallab qa-data` call type, through the real CLI process.

Read-only against the live store. Inputs are sampled deterministically from admitted DEV rows and are
never printed: the output holds only aggregates per call type (n, ok, median/p90 estimated tokens,
median/p90 end-to-end latency, truncation rate, share of calls with an empty answer).

    bench.py --out results.json [--per-call 20] [--budget 4000]
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LIVE = Path.home() / ".local/state/evallab-prod-live/cloud-trace-db.sqlite"
SEED = 20261028


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(round(fraction * len(ordered) + 0.5)) - 1))]


def call(arguments: list[str], budget: int) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    completed = subprocess.run(
        ["uv", "run", "--no-sync", "evallab", "qa-data", *arguments, "--budget", str(budget)],
        capture_output=True, text=True, check=False, env={**__import__("os").environ, "EVALLAB_DISABLE_LOCAL_DOCKER": "1"},
    )
    elapsed = (time.perf_counter() - started) * 1000
    reply = json.loads(completed.stdout.strip().splitlines()[-1])
    return reply, elapsed


def sample(per_call: int) -> dict[str, list[list[str]]]:
    rng = random.Random(SEED)
    with sqlite3.connect(LIVE.as_uri() + "?mode=ro", uri=True) as connection:
        faceted = [row[0] for row in connection.execute(
            "SELECT f.run_id FROM run_facets f ORDER BY f.run_id")]
        runs = rng.sample(faceted, min(per_call, len(faceted)))
        tasks = [row[0] for row in connection.execute(
            "SELECT DISTINCT task_id FROM run_facets ORDER BY task_id")]
        issue_tasks = [row[0] for row in connection.execute(
            "SELECT DISTINCT i.task_id FROM task_issues i WHERE i.status='confirmed' "
            "AND i.task_id IN (SELECT task_id FROM run_facets) ORDER BY i.task_id")]
        bundle_tasks = [row[0] for row in connection.execute(
            "SELECT DISTINCT t.task_id FROM corpus_tasks t JOIN agent_traces a ON a.task_id = t.task_id "
            "WHERE t.dataset_id IN ('mimo-defect-map-v1','tb4-audit-v1') ORDER BY t.task_id")]
        clusters = [row[0] for row in connection.execute("SELECT cluster_id FROM run_clusters ORDER BY cluster_id")]
    pick = lambda pool: rng.sample(pool, min(per_call, len(pool)))  # noqa: E731
    return {
        "task": [[t] for t in pick(bundle_tasks)],
        "runs:facets": [["runs", "--facet", f"outcome={o}", "--facet", f"tests={t}", "--limit", "10"]
                        for o in ("pass", "fail") for t in ("modified", "read", "untouched", "deleted", "replaced")][:per_call],
        "runs:cluster": [["runs", "--cluster", c, "--limit", "10"] for c in pick(clusters)],
        "runs:like": [["runs", "--like", r, "--limit", "10"] for r in runs],
        "window": [["window", r, "--around", "5", "--radius", "3"] for r in runs],
        "history": [[t] for t in pick(issue_tasks + pick(tasks))],
        "findings:run": [["findings", "--run", r] for r in runs],
        "findings:task": [["findings", "--task", t] for t in pick(tasks)],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--per-call", type=int, default=20)
    parser.add_argument("--budget", type=int, default=4000)
    args = parser.parse_args()
    plan = sample(args.per_call)
    results: dict[str, Any] = {}
    for label, inputs in plan.items():
        tokens: list[float] = []
        latency: list[float] = []
        inner: list[float] = []
        ok = truncated = empty = over = 0
        for argv in inputs:
            full = argv if label.startswith(("runs", "window", "findings")) else (
                ["task", *argv] if label == "task" else ["history", *argv]
            )
            reply, elapsed = call(full, args.budget)
            ok += reply["ok"]
            if not reply["ok"]:
                continue
            tokens.append(reply["meta"]["tokens_est"])
            latency.append(elapsed)
            inner.append(reply["meta"]["latency_ms"])
            truncated += reply["meta"]["truncated"]
            over += reply["meta"]["tokens_est"] > args.budget
            data = reply["data"]
            empty += not any(bool(v) for key, v in data.items() if isinstance(v, (list, dict)) and key not in {"counts", "coverage"}) and not data.get("package")
        results[label] = {
            "n": len(inputs), "ok": ok, "truncated": truncated, "empty_answer": empty, "over_budget": over,
            **({"tokens_median": statistics.median(tokens), "tokens_p90": percentile(tokens, 0.9),
                "tokens_max": max(tokens), "latency_ms_median": round(statistics.median(latency)),
                "latency_ms_p90": round(percentile(latency, 0.9)),
                "call_ms_median": round(statistics.median(inner), 1), "call_ms_p90": round(percentile(inner, 0.9), 1)} if tokens else {}),
        }
        print(label, json.dumps(results[label], sort_keys=True), flush=True)
    report = {
        "schema": "evallab.ce14_qa_data_bench/v1", "recorded_at_utc": datetime.now(UTC).isoformat(),
        "budget": args.budget, "per_call": args.per_call, "seed": SEED,
        "note": "latency_ms is end-to-end `uv run evallab qa-data` process time including interpreter start and imports; call_ms is the in-process time reported in meta.latency_ms (open, admission, query, reply)",
        "results": results,
    }
    Path(args.out).write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
