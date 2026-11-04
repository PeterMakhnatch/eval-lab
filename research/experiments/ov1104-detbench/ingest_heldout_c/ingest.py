#!/usr/bin/env python3
"""Held-out ingestion for slice ho-c (ov1104-detbench).

Ingests the five assigned cross-test registry sets into DetBench views and runs.*.jsonl:
  1. resolvebench-support-agents (decisive_rule=True, natural=True)
  2. latent-agent-sandbox-episodes (decisive_rule=True, natural=True)
  3. cape-bench (decisive_rule=True, natural=True)
  4. trusted-monitoring-logs (decisive_rule=True, natural=False)
  5. caught-not-taught (decisive_rule=False, natural=False)

No model calls, no network calls, read-only access to staged cross-test bundles.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
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
PYTHON314 = "/opt/homebrew/bin/python3"


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def ingest_resolvebench() -> dict[str, Any]:
    t0 = time.time()
    set_id = "resolvebench-support-agents"
    source_family = "Hubble42/ResolveBench"
    mod = load_adapter_module(
        "adapter_resolvebench",
        "research/evidence/runs/corpus-expansion-pq8-20261029/corpora/Hubble42__ResolveBench/tools/adapter.py",
    )
    records = mod.population()
    pop_states = Counter(r["state"] for r in records)
    pop_reasons = Counter(f"{r['state']}:{r['reason']}" for r in records)
    pop_groups = Counter(r["group"] for r in records if r["state"] == "negative")
    in_cap_pos = sum(1 for r in records if r["state"] == "positive" and r["in_cap_subset"])

    rows_out: list[dict[str, Any]] = []
    clusters: set[str] = set()
    pos_count = neg_count = 0

    for spec in mod.specs(population_name="primary_sample", records=records):
        state = spec["gold"]["state"]
        if state not in ("positive", "negative"):
            continue
        label = 1 if state == "positive" else 0
        if label == 1:
            pos_count += 1
        else:
            neg_count += 1

        native_id = str(spec["identity"]["row_id"])
        cluster = str(spec["identity"]["task_key"])
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
            label_type="rule",
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
                "fa_basis": "established",
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
        "population_negative_groups": dict(pop_groups),
        "in_cap_positives": in_cap_pos,
        "elapsed_s": round(time.time() - t0, 2),
    }


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


def ingest_cape_bench() -> dict[str, Any]:
    t0 = time.time()
    set_id = "cape-bench"
    source_family = "Afayyazi/CAPE-Bench"
    mod = load_adapter_module(
        "adapter_cape_bench",
        "research/evidence/runs/corpus-expansion-pq7-20261029/corpora/Afayyazi__CAPE-Bench/tools/adapter.py",
    )
    records = mod.population()
    pop_states = Counter(r["state"] for r in records)
    plain_states = Counter(r["state"] for r in records if r["slice"] == "plain")
    pop_reasons = Counter(f"{r['state']}:{r['reason']}" for r in records)

    rows_out: list[dict[str, Any]] = []
    clusters: set[str] = set()
    pos_count = neg_count = 0
    leak_hits_sample = 0

    for rec, spec, _audit in mod.iter_episodes(mod.ROOT, records, "primary_sample"):
        state = spec["gold"]["state"]
        if state not in ("positive", "negative"):
            continue
        if mod.leak_hits(spec["_steps"], mod.needles_for(rec, spec)):
            leak_hits_sample += 1
        label = 1 if state == "positive" else 0
        if label == 1:
            pos_count += 1
        else:
            neg_count += 1

        native_id = str(spec["identity"]["row_id"])
        cluster = str(spec["identity"]["scenario_id_sha256"])
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
                "fa_basis": "established",
                "decisive_rule": True,
            },
        )
        rows_out.append(row)

    if leak_hits_sample:
        raise RuntimeError(f"cape-bench primary_sample had {leak_hits_sample} leak hits")

    out_path = WORKER_DIR / f"runs.{set_id}.jsonl"
    write_jsonl(out_path, rows_out)
    return {
        "set_id": set_id,
        "rows_written": len(rows_out),
        "positive": pos_count,
        "negative": neg_count,
        "clusters": len(clusters),
        "leak_hits_primary_sample": leak_hits_sample,
        "population_states": dict(pop_states),
        "plain_slice_states": dict(plain_states),
        "population_reasons": dict(pop_reasons),
        "elapsed_s": round(time.time() - t0, 2),
    }


def _normalize_tml_steps(raw_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map trusted-monitoring-logs tool_calls[{function, arguments}] -> function_name for detbench_lib."""
    out: list[dict[str, Any]] = []
    for s in raw_steps:
        step = dict(s)
        if step.get("tool_calls"):
            calls = []
            for c in step["tool_calls"]:
                call = dict(c)
                if "function_name" not in call and isinstance(call.get("function"), str):
                    call["function_name"] = call.pop("function")
                calls.append(call)
            step["tool_calls"] = calls
        out.append(step)
    return out


def _load_tml_specs_via_py314() -> list[dict[str, Any]]:
    """Load trusted-monitoring-logs specs using Python 3.14 stdlib zipfile (zstd support)."""
    adapter_dir = (
        REPO_ROOT
        / "research/evidence/runs/corpus-expansion-pq5-20261029/corpora/dang-w__trusted-monitoring-logs/tools"
    )
    code = (
        "import json, sys; "
        f"sys.path.insert(0, {str(adapter_dir)!r}); "
        "import adapter; "
        "print(json.dumps(adapter.specs()))"
    )
    proc = subprocess.run(
        [PYTHON314, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout)


def ingest_trusted_monitoring_logs() -> dict[str, Any]:
    t0 = time.time()
    set_id = "trusted-monitoring-logs"
    source_family = "dang-w/trusted-monitoring-logs"
    all_specs = _load_tml_specs_via_py314()

    pop_states = Counter(s["labels"]["label_state"] for s in all_specs)
    pop_reasons = Counter(
        f"{s['labels']['label_state']}:{s['labels']['label_reason']}" for s in all_specs
    )
    attempted_states = Counter(s["labels"]["label_state_attempted"] for s in all_specs)

    rows_out: list[dict[str, Any]] = []
    clusters: set[str] = set()
    pos_count = neg_count = 0

    for spec in all_specs:
        state = spec["labels"]["label_state"]
        if state not in ("positive", "negative"):
            continue
        label = 1 if state == "positive" else 0
        if label == 1:
            pos_count += 1
        else:
            neg_count += 1

        # Use adapter's persisted trace_sha256 rather than raw file/sample_id which encode run_dir mode.
        native_id = "tml_" + str(spec["labels"]["trace_sha256"])[:24]
        raw_task_id = str(spec["identity"]["task_id"]).split("@")[0].rsplit("/", 1)[-1].casefold()
        cluster = "tml_task_" + sha256_hex(raw_task_id)[:16]
        clusters.add(cluster)
        run_key = f"{set_id}:{native_id}"

        norm_steps = _normalize_tml_steps(spec["_steps"])
        view = build_view(
            run_key=run_key,
            atif_steps=norm_steps,
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
            natural=False,
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
                "fa_basis": "established",
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
        "attempted_slice_states": dict(attempted_states),
        "elapsed_s": round(time.time() - t0, 2),
    }


def _convert_cnt_steps(raw_steps: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    """Convert caught-not-taught role/text steps into ATIF steps + final diff for build_view."""
    out: list[dict[str, Any]] = []
    last_agent: dict[str, Any] | None = None
    diff_text: str | None = None

    for raw in raw_steps:
        role = raw.get("role")
        text = raw.get("text") or ""
        if role == "final_diff":
            diff_text = text
            last_agent = None
            continue
        if role == "assistant":
            calls = []
            for idx, c in enumerate(raw.get("tool_calls") or []):
                calls.append(
                    {
                        "tool_call_id": f"s{len(out)}c{idx}",
                        "function_name": c.get("function") or c.get("name") or "",
                        "arguments": c.get("arguments"),
                    }
                )
            step: dict[str, Any] = {
                "step_id": len(out),
                "source": "agent",
                "message": text,
            }
            if calls:
                step["tool_calls"] = calls
            out.append(step)
            last_agent = step
        elif role == "tool":
            if last_agent is not None:
                last_agent.setdefault("observation", {"results": []})["results"].append(
                    {"content": text}
                )
            else:
                out.append(
                    {
                        "step_id": len(out),
                        "source": "tool",
                        "message": None,
                        "observation": text,
                    }
                )
        else:
            out.append(
                {
                    "step_id": len(out),
                    "source": str(role or "user"),
                    "message": text,
                }
            )
            last_agent = None
    return out, diff_text


def ingest_caught_not_taught() -> dict[str, Any]:
    t0 = time.time()
    set_id = "caught-not-taught"
    source_family = "amirgroup-codes/caught_not_taught"
    mod = load_adapter_module(
        "adapter_caught_not_taught",
        "research/evidence/runs/corpus-expansion-pq4-20261028/corpora/amirgroup-codes__caught_not_taught/tools/adapter.py",
    )

    pop_states: Counter[str] = Counter()
    pop_reasons: Counter[str] = Counter()
    for ep in mod.iter_episodes():
        st, rsn = mod.label_state(ep)
        pop_states[st] += 1
        pop_reasons[f"{st}:{rsn}"] += 1

    rows_out: list[dict[str, Any]] = []
    clusters: set[str] = set()
    pos_count = neg_count = 0
    neg_by_arm: Counter[str] = Counter()

    for spec in mod.specs():
        state = spec["gold"]["state"]
        if state not in ("positive", "negative"):
            continue
        label = 1 if state == "positive" else 0
        if label == 1:
            pos_count += 1
        else:
            neg_count += 1
            neg_by_arm[str(spec["identity"]["arm"])] += 1

        # Hash raw episode id and main task name per profile.py so mode/arm/pair tags never enter run_key.
        raw_id = str(spec["identity"]["id"]).casefold()
        native_id = "cnt_" + sha256_hex(raw_id)[:24]
        main_name = str(spec["identity"]["main_name"]).casefold()
        cluster = "cnt_task_" + sha256_hex(main_name)[:16]
        clusters.add(cluster)
        run_key = f"{set_id}:{native_id}"

        atif_steps, diff_text = _convert_cnt_steps(spec["_steps"])
        view = build_view(
            run_key=run_key,
            atif_steps=atif_steps,
            task_text=None,
            reward=None,
            diff=diff_text,
        )
        n_steps, n_chars = view_size(view)
        full_path, acts_path = write_views(set_id, native_id, view, root=VIEWS)

        # In NP33-CM-1, all 126 untrusted-arm episodes (46 positive + 80 untrusted-honest negative)
        # were scored by Haiku 5.5 facets and Sonnet 5.5 cascade_v8; the 80 trusted-arm honest
        # negatives were not read in NP33-CM-1.
        read_by = (
            ["NP33-CM-1:facets", "NP33-CM-1:cascade_v8"]
            if spec["identity"]["arm"] == "untrusted"
            else []
        )

        row = runs_row(
            set_id=set_id,
            native_id=native_id,
            split="heldout",
            label=label,
            label_type="executed",
            natural=False,
            cluster=cluster,
            source_family=source_family,
            view_full=full_path,
            view_actions=acts_path,
            n_steps=n_steps,
            n_chars=n_chars,
            read_by=read_by,
            extra={
                "primary": True,
                "label_state": state,
                "fa_basis": "established",
                "decisive_rule": False,
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
        "negative_by_arm": dict(neg_by_arm),
        "clusters": len(clusters),
        "population_states": dict(pop_states),
        "population_reasons": dict(pop_reasons),
        "elapsed_s": round(time.time() - t0, 2),
    }


def main() -> int:
    WORKER_DIR.mkdir(parents=True, exist_ok=True)
    summary = {}
    for fn in (
        ingest_resolvebench,
        ingest_latent_sandbox,
        ingest_cape_bench,
        ingest_trusted_monitoring_logs,
        ingest_caught_not_taught,
    ):
        res = fn()
        summary[res["set_id"]] = res
        print(json.dumps(res, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
