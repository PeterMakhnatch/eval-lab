#!/usr/bin/env python3
"""Held-out ingestion for slice ho-c (ov1104-detbench).

Ingests only latent-agent-sandbox-episodes. Excluded policy and sabotage sources
no longer produce reward-hacking trace views.

No model calls, no network calls, read-only access to staged cross-test bundles.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DETBENCH_DIR = HERE.parent
REPO_ROOT = DETBENCH_DIR.parents[2]
sys.path.insert(0, str(DETBENCH_DIR))

from detbench_lib import (  # noqa: E402
    VIEWS,
    build_view,
    runs_row,
    view_size,
    write_jsonl,
    write_views,
)

WORKER_DIR = Path("/private/tmp/ov1104/detbench/workers/ho-c")

def load_adapter_module(module_name: str, rel_path: str) -> Any:
    path = REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load adapter at {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(mod)
    finally:
        if sys.path and sys.path[0] == str(path.parent):
            sys.path.pop(0)
    return mod


def ingest_latent_sandbox() -> dict[str, Any]:
    t0 = time.time()
    set_id = "latent-agent-sandbox-episodes"
    source_family = "runlatent/latent-agent-sandbox-episodes"
    mod = load_adapter_module(
        "adapter_latent_sandbox",
        "research/evidence/runs/corpus-expansion-pq8-20261029/corpora/runlatent__latent-agent-sandbox-episodes/tools/adapter.py",
    )
    records = mod.population()
    pop_states = Counter(r["state"] for r in records)
    pop_reasons = Counter(f"{r['state']}:{r['reason']}" for r in records)

    rows_out: list[dict[str, Any]] = []
    clusters: set[str] = set()
    pos_count = neg_count = 0

    for spec in mod.specs(population_name="scoring", records=records):
        state = spec["gold"]["state"]
        if state not in ("positive", "negative"):
            continue
        label = 1 if state == "positive" else 0
        if label == 1:
            pos_count += 1
        else:
            neg_count += 1

        native_id = str(spec["identity"]["row_id"])
        cluster = str(spec["identity"]["template"])
        clusters.add(cluster)
        run_key = f"{set_id}:{native_id}"

        view = build_view(
            run_key=run_key,
            atif_steps=spec["_steps"],
            task_text=None,
            reward=None,
            diff=None,
        )
        n_steps, n_chars = view_size(view)
        full_path, acts_path = write_views(set_id, native_id, view, root=VIEWS)

        row = runs_row(
            set_id=set_id,
            native_id=native_id,
            split="heldout",
            label=label,
            label_type="executed",
            natural=True,
            cluster=cluster,
            source_family=source_family,
            view_full=full_path,
            view_actions=acts_path,
            n_steps=n_steps,
            n_chars=n_chars,
            read_by=[],
            extra={
                "primary": True,
                "label_state": state,
                "fa_basis": "mixed",
                "decisive_rule": True,
            },
        )
        rows_out.append(row)

    out_path = WORKER_DIR / f"runs.{set_id}.jsonl"
    write_jsonl(out_path, rows_out)
    return {
        "set_id": set_id,
        "rows_written": len(rows_out),
        "positive": pos_count,
        "negative": neg_count,
        "clusters": len(clusters),
        "population_states": dict(pop_states),
        "population_reasons": dict(pop_reasons),
        "elapsed_s": round(time.time() - t0, 2),
    }


def main() -> int:
    WORKER_DIR.mkdir(parents=True, exist_ok=True)
    summary = {}
    for fn in (
        ingest_latent_sandbox,
    ):
        res = fn()
        summary[res["set_id"]] = res
        print(json.dumps(res, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
