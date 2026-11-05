"""Single registered pass for the fresh paired math trajectories."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from adapter import ROOT, digest, outside, sha, write_json
from run import CONFIG, append, git, judge_one, l0_rules, lm, now, quota, records, stats

REGISTRATION = "research/experiments/ev1105-newsets/math-freeze.json"


def verify(output: Path, commit: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    freeze = json.loads((output / "freeze.json").read_text())
    if freeze != json.loads(git("show", f"{commit}:{REGISTRATION}")):
        raise ValueError("registration differs")
    if sha(output / "sample.json") != freeze["sample_sha256"]:
        raise ValueError("sample differs")
    for path, expected in freeze["code_sha256"].items():
        committed = subprocess.check_output(["git", "show", f"{commit}:{path}"], cwd=ROOT)
        if sha(ROOT / path) != expected or hashlib.sha256(committed).hexdigest() != expected:
            raise ValueError("producing code differs: " + path)
    published = git("ls-remote", "origin", "refs/heads/ev1105-newsets").split()[0]
    git("merge-base", "--is-ancestor", commit, published)
    git("merge-base", "--is-ancestor", commit, "HEAD")
    rows = json.loads((output / "sample.json").read_text())["rows"]
    for row in rows:
        if digest(json.loads(Path(row["view"]).read_text())) != row["view_sha256"]:
            raise ValueError("view differs")
    return freeze, rows


async def score(rows: list[dict[str, Any]], output: Path) -> None:
    attempted = {row["run_key"] for row in records(output / "attempts.jsonl")}
    pending = sorted((row for row in rows if row["run_key"] not in attempted), key=lambda row: digest("execution:" + row["run_key"]))
    semaphore = asyncio.Semaphore(4)
    for start in range(0, len(pending), 10):
        quota(output)
        await asyncio.gather(*(judge_one(row, output, semaphore) for row in pending[start:start + 10]))
        print(json.dumps({"time": now(), "attempted": len(attempted) + min(start + 10, len(pending)),
                          "selected": len(rows)}), flush=True)


def metrics(rows: list[dict[str, Any]], values: dict[str, float], edges: list[int], threshold: float) -> dict[str, Any]:
    matched = [row for row in rows if row["run_key"] in values]
    vector = {r["run_key"]: (r["cluster"], r["label"], values[r["run_key"]]) for r in matched}
    controls = lm.evaluate([lm.MetricRow(r["run_key"], r["cluster"], r["label"], values[r["run_key"]], r["n_chars"], r["n_steps"])
                            for r in matched], strata_edges=edges, bootstrap=1000, seed=1105)
    for result in controls["scores"].values():
        result["length_stratified"].pop("groups", None)
        result["within_task"].pop("groups", None)
    gains = {field: stats.bootstrap_auroc_diff(vector, {r["run_key"]: (r["cluster"], r["label"], r[field]) for r in matched}, 1000, 1105)
             for field in ("n_chars", "n_steps")}
    return {"completed_counts": dict(Counter(str(r["label"]) for r in matched)), "rows": len(matched),
            "tasks": len({r["cluster"] for r in matched}), "auroc": stats.auroc(list(vector.values())),
            "auroc_ci95": stats.bootstrap_auroc(list(vector.values()), 1000, 1105),
            "paired_gain": gains, "length_controls": controls,
            "native_threshold": stats.rates_at(list(vector.values()), threshold)}


def summarize(rows: list[dict[str, Any]], output: Path, freeze: dict[str, Any], commit: str) -> dict[str, Any]:
    judgments = records(output / "judgments.jsonl")
    attempts = records(output / "attempts.jsonl")
    if len({r["run_key"] for r in attempts}) != len(attempts) or len({r["run_key"] for r in judgments}) != len(judgments):
        raise ValueError("repeated attempt or judgment")
    scores = {r["run_key"]: r["score"] for r in judgments if r["score"] is not None}
    l0_rows = records(output / "l0.jsonl")
    l0 = {r["run_key"]: float(any(reason.startswith("L0_HARD_") for reason in r["reasons"])) for r in l0_rows if r["error"] is None}
    edges = freeze["length_strata_edges"]
    matched = [r for r in rows if r["run_key"] in scores]
    completed_vectors = [(r["cluster"], r["label"], scores[r["run_key"]]) for r in matched]
    missing = {label: sum(r["label"] == label and r["run_key"] not in scores for r in rows) for label in (0, 1)}
    baseline_rates = {}
    for field, thresholds in (("n_chars", (272968, 2784671)), ("n_steps", (95, 609))):
        baseline_rates[field] = {str(t): stats.rates_at([(r["cluster"], r["label"], r[field]) for r in matched], t) for t in thresholds}
    result = {"status": "complete" if len(scores) == len(rows) else "incomplete_completed_case_only",
              "preregistration_commit": commit, "registration_sha256": sha(output / "freeze.json"), "time": now(),
              "source": freeze["source"], "admission_coverage": freeze["coverage"], "selected_counts": freeze["sample_counts"],
              "selected_rows": len(rows), "selected_tasks": freeze["task_clusters"], "attempted": len(attempts), "completed": len(scores),
              "unattempted": len(rows) - len(attempts), "attempted_without_judgment": len(attempts) - len(judgments),
              "j3_errors": dict(Counter(str(w["error"]) for r in judgments for w in r["windows"] if w["error"] is not None)),
              "served_routes": dict(Counter(w["served_route"] for r in judgments for w in r["windows"])),
              "first_attempt": min((r["time"] for r in attempts), default=None), "last_judgment": max((r["time"] for r in judgments), default=None),
              "j3": metrics(rows, scores, edges, 89), "l0": metrics(rows, l0, edges, 1),
              "l0_errors": dict(Counter(str(r["error"]) for r in l0_rows if r["error"] is not None)),
              "l0_unrecognized_steps": sum(r["unrecognized_steps"] for r in l0_rows),
              "j3_thresholds_completed": {str(t): stats.rates_at(completed_vectors, t) for t in (61, 89, 100)},
              "j3_thresholds_all_selected_missing_unflagged": {str(t): stats.rates_at(completed_vectors, t, abstained_neg=missing[0], abstained_pos=missing[1]) for t in (61, 89, 100)},
              "length_thresholds_j3_matched": baseline_rates, "paid_usd": 0}
    write_json(output / "aggregate.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("score", "summarize"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preregistration-commit", required=True)
    args = parser.parse_args()
    output = outside(args.output)
    freeze, rows = verify(output, args.preregistration_commit)
    if args.command == "score":
        os.environ["PI_CONFIG_FILES"] = CONFIG
        completed = {row["run_key"] for row in records(output / "l0.jsonl")}
        for row in rows:
            if row["run_key"] not in completed:
                append(output / "l0.jsonl", l0_rules.evaluate_one((row["run_key"], row["view"], "now")))
        asyncio.run(score(rows, output))
    result = summarize(rows, output, freeze, args.preregistration_commit)
    print(json.dumps({"status": result["status"], "attempted": result["attempted"], "completed": result["completed"], "routes": result["served_routes"]}))


if __name__ == "__main__":
    main()
