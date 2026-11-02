"""Build and save the stratified 800-case cohort for R1-PREVALENCE."""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from evallab.production.corpus_queue import CorpusRegistry
from evallab.production.run_atlas import admitted_task_ids, connect_readonly
from evallab.results_home import results_root

BENCH_MANIFEST = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/trace-monitor-bench-dev-v1/manifest.jsonl")
CLOUD_DB = Path.home() / ".local/state/evallab-prod-live/cloud-trace-db.sqlite"
RECEIPTS_DIR = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/train/raw/R1-PREVALENCE/receipts")

SEED = 20261102

STRATA_QUOTAS = {
    "goodfire_kimi": ("source-reported:moonshotai/Kimi-K3", 225),
    "goodfire_qwen": ("source-reported:Qwen/Qwen3.8-2.4T-A95B-FP8", 225),
    "goodfire_glm52_fp8": ("source-reported:zai-org/GLM-5.2-FP8", 125),
    "goodfire_glm52": ("source-reported:zai-org/GLM-5.2", 125),
    "hacktrace": ("hacktrace-act-v1", 100),
}


def get_bench_source_ids() -> set[str]:
    cases = [json.loads(line) for line in BENCH_MANIFEST.read_text().splitlines() if line.strip()]
    return {c["source_id"] for c in cases}


def build_cohort() -> list[dict[str, Any]]:
    bench_source_ids = get_bench_source_ids()
    repo_root = Path(".").resolve()
    reg = CorpusRegistry(repo_root)

    # 1. Cloud traces
    con = connect_readonly(CLOUD_DB)
    admitted = admitted_task_ids(con, reg)
    cur = con.cursor()
    rows = cur.execute("""
        SELECT at.trace_id, at.model_id, ct.task_id
        FROM agent_traces at
        JOIN corpus_tasks ct USING(task_id)
        WHERE ct.dataset_id = 'goodfire-reward-hack-data' AND ct.split = 'DEV'
    """).fetchall()
    con.close()

    by_model: dict[str, list[str]] = {}
    for trace_id, model_id, task_id in rows:
        if task_id in admitted and trace_id not in bench_source_ids:
            by_model.setdefault(model_id, []).append(trace_id)

    # 2. Hacktrace
    ht_items = [
        it for it in reg.items(dataset_id="hacktrace-act-v1", split="dev")
        if it.item_id not in bench_source_ids
    ]
    ht_map = {it.item_id: it for it in ht_items}

    rng = random.Random(SEED)
    cohort: list[dict[str, Any]] = []

    for stratum, (key, quota) in STRATA_QUOTAS.items():
        if stratum == "hacktrace":
            candidates = sorted(ht_map.keys())
            if len(candidates) < quota:
                raise ValueError(f"Not enough candidates for {stratum}: {len(candidates)} < {quota}")
            selected = rng.sample(candidates, quota)
            for cid in sorted(selected):
                it = ht_map[cid]
                ref = (it.metadata.get("trial_ref") or it.metadata.get("actions_only_ref") or {}).get("path")
                full_path = results_root() / ref / "agent" / "trajectory.json" if ref else None
                cohort.append({
                    "sample_id": f"prev-{len(cohort) + 1:04d}",
                    "corpus": "hacktrace",
                    "stratum": stratum,
                    "source_id": cid,
                    "loader": "hacktrace_file",
                    "file_path": str(full_path),
                    "model_id": it.metadata.get("expected_fault", "agent"),
                })
        else:
            candidates = sorted(by_model.get(key, []))
            if len(candidates) < quota:
                raise ValueError(f"Not enough candidates for {stratum}: {len(candidates)} < {quota}")
            selected = rng.sample(candidates, quota)
            for cid in sorted(selected):
                cohort.append({
                    "sample_id": f"prev-{len(cohort) + 1:04d}",
                    "corpus": "goodfire",
                    "stratum": stratum,
                    "source_id": cid,
                    "loader": "cloud",
                    "file_path": None,
                    "model_id": key,
                })

    return cohort


if __name__ == "__main__":
    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    cohort = build_cohort()
    out_path = RECEIPTS_DIR / "cohort.jsonl"
    with out_path.open("w", encoding="utf-8") as f:
        for row in cohort:
            f.write(json.dumps(row) + "\n")
    print(f"Wrote {len(cohort)} cases to {out_path}")
