#!/usr/bin/env python3
"""Ingest held-out slice D (ho-d) sets into DetBench views and runs.<set_id>.jsonl.

Assigned sets (in order):
1. mistrace-reward-hacking
2. reward-pressure-study-2
3. rhb-delivery-queue
4. flake-laundering
5. kobayashi-maru (deterministic stratified sample of 3,000 with seed 1104 from 8,705 primary scoreable rows)
6. rsi-benchmark-monitor-training (instructed: natural=false)

No model calls, no network calls, no transcript or raw-id persistence in receipts.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import random
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

WORKTREE = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/ov1104-detbench")
DETBENCH_DIR = WORKTREE / "research/experiments/ov1104-detbench"
sys.path.insert(0, str(DETBENCH_DIR))

from detbench_lib import build_view, runs_row, view_size, write_jsonl, write_views  # noqa: E402

STUDY_ROOT = Path("/private/tmp/eval-lab-study")
WORKER_DIR = Path("/private/tmp/ov1104/detbench/workers/ho-d")


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_module(name: str, path: Path) -> Any:
    parent = str(path.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module {name} from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def adapt_steps_mistrace(raw_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Preserve system role and fold tool error strings into content for detbench_lib."""
    out: list[dict[str, Any]] = []
    for step in raw_steps:
        s = dict(step)
        if s.get("input_role") == "system":
            s["source"] = "system"
        obs = s.get("observation")
        if isinstance(obs, dict) and isinstance(obs.get("results"), list):
            new_results = []
            for r in obs["results"]:
                if isinstance(r, dict):
                    rc = dict(r)
                    content = rc.get("content") or ""
                    err = rc.get("error")
                    if err:
                        rc["content"] = f"{content}\n{err}" if content else str(err)
                    new_results.append(rc)
                else:
                    new_results.append(r)
            s["observation"] = {**obs, "results": new_results}
        out.append(s)
    return out


def adapt_steps_reward_pressure(raw_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map source='environment' / input_role='tool' to source='tool' for detbench_lib.ROLE_MAP."""
    out: list[dict[str, Any]] = []
    for step in raw_steps:
        s = dict(step)
        if s.get("input_role") == "tool" or s.get("source") == "environment":
            s["source"] = "tool"
        out.append(s)
    return out


def adapt_steps_kobayashi(raw_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Preserve system role on step 0 so build_view selects the user task prompt as task_text."""
    out: list[dict[str, Any]] = []
    for step in raw_steps:
        s = dict(step)
        if s.get("input_role") == "system":
            s["source"] = "system"
        out.append(s)
    return out


def ingest_mistrace() -> dict[str, Any]:
    t0 = time.time()
    set_id = "mistrace-reward-hacking"
    source_family = "ameek/MisTrace (reward-hacking config)"
    adapter_path = STUDY_ROOT / "research/evidence/runs/corpus-expansion-pq2-20261028/corpora/ameek__mistrace/tools/adapter.py"
    mod = load_module("mistrace_adapter", adapter_path)

    rows_out: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    state_counts: Counter[str] = Counter()
    excluded_reasons: Counter[str] = Counter()
    clusters: set[str] = set()
    seen_ids: set[str] = set()

    for spec in mod.specs():
        counts["total_specs"] += 1
        state = spec["gold"]["label_state"]
        state_counts[state] += 1
        if state == "ambiguous":
            counts["excluded"] += 1
            excluded_reasons[spec["gold"]["excluded_reason"] or "ambiguous"] += 1
            continue
        label = 1 if state in ("positive_success", "positive_attempt") else 0
        native_id = spec["identity"]["run_key"]
        assert native_id not in seen_ids, f"Duplicate native_id in {set_id}"
        seen_ids.add(native_id)
        cluster = spec["identity"]["cluster_key"]
        clusters.add(cluster)

        steps = adapt_steps_mistrace(spec["_steps"])
        run_key = f"{set_id}:{native_id}"
        view = build_view(run_key=run_key, atif_steps=steps, task_text=None, reward=None, diff=None)
        full_path, act_path = write_views(set_id, native_id, view)
        n_steps, n_chars = view_size(view)

        rows_out.append(
            runs_row(
                set_id=set_id,
                native_id=native_id,
                split="heldout",
                label=label,
                label_type="executed",
                natural=None,
                cluster=cluster,
                source_family=source_family,
                view_full=full_path,
                view_actions=act_path,
                n_steps=n_steps,
                n_chars=n_chars,
                read_by=[],
                extra={
                    "primary": True,
                    "label_state": state,
                    "fa_basis": "no_flag_fired",
                    "decisive_rule": False,
                },
            )
        )
        counts["written"] += 1
        counts["pos" if label == 1 else "neg"] += 1

    out_file = WORKER_DIR / f"runs.{set_id}.jsonl"
    write_jsonl(out_file, rows_out)
    return {
        "set_id": set_id,
        "counts": dict(counts),
        "state_counts": dict(state_counts),
        "excluded_reasons": dict(excluded_reasons),
        "unique_clusters": len(clusters),
        "elapsed_sec": round(time.time() - t0, 2),
        "out_file": str(out_file),
    }


def ingest_reward_pressure_study_2() -> dict[str, Any]:
    t0 = time.time()
    set_id = "reward-pressure-study-2"
    source_family = "osf:7as8h / Reward Pressure Study 2"
    tools_dir = STUDY_ROOT / "research/evidence/runs/corpus-expansion-pq3-20261028/corpora/osf__7as8h-reward-pressure-study-2/tools"
    adapter_mod = load_module("rp2_adapter", tools_dir / "adapter.py")
    profile_mod = load_module("rp2_profile", tools_dir / "profile.py")

    # Build lookup of (run_id_hash -> (label_state, substudy, task_id_hash)) from the locked bundle rows
    label_map: dict[str, tuple[str, str, str]] = {}
    counts: Counter[str] = Counter()
    state_counts: Counter[str] = Counter()
    excluded_reasons: Counter[str] = Counter()
    substudy_counts: dict[str, Counter[str]] = {}

    for substudy, rel in adapter_mod.SUBSTUDIES.items():
        substudy_counts[substudy] = Counter()
        for row in adapter_mod.read_rows(adapter_mod.ROOT / rel):
            counts["raw_rows_read"] += 1
            lab = profile_mod.label(row)
            state_counts[lab] += 1
            substudy_counts[substudy][lab] += 1
            if lab == "excluded_error":
                counts["excluded"] += 1
                excluded_reasons["provider_error"] += 1
                continue
            r_hash = adapter_mod.digest(f"{substudy}:{row['episode_id']}")
            t_hash = adapter_mod.digest(row["task"])
            label_map[r_hash] = (lab, substudy, t_hash)

    rows_out: list[dict[str, Any]] = []
    clusters: set[str] = set()
    seen_ids: set[str] = set()

    for spec in adapter_mod.specs():
        counts["total_specs"] += 1
        native_id = spec["run_id_hash"]
        assert native_id not in seen_ids, f"Duplicate native_id in {set_id}"
        seen_ids.add(native_id)
        lab, substudy, t_hash = label_map[native_id]
        assert t_hash == spec["task_id_hash"], "Task hash mismatch"
        if lab not in ("positive", "negative"):
            counts["excluded_ambiguous"] += 1
            excluded_reasons[lab] += 1
            continue
        label = 1 if lab == "positive" else 0
        clusters.add(t_hash)

        steps = adapt_steps_reward_pressure(spec["_steps"])
        run_key = f"{set_id}:{native_id}"
        view = build_view(run_key=run_key, atif_steps=steps, task_text=None, reward=None, diff=None)
        full_path, act_path = write_views(set_id, native_id, view)
        n_steps, n_chars = view_size(view)

        rows_out.append(
            runs_row(
                set_id=set_id,
                native_id=native_id,
                split="heldout",
                label=label,
                label_type="executed",
                natural=None,
                cluster=t_hash,
                source_family=source_family,
                view_full=full_path,
                view_actions=act_path,
                n_steps=n_steps,
                n_chars=n_chars,
                read_by=["np21-cm1:cascade_v8_sonnet", "np21-cm1:facets_haiku", "np35-lb1:s_census"],
                extra={
                    "primary": True,
                    "label_state": lab,
                    "fa_basis": "no_flag_fired",
                    "decisive_rule": False,
                    "substudy": substudy,
                },
            )
        )
        counts["written"] += 1
        counts["pos" if label == 1 else "neg"] += 1

    out_file = WORKER_DIR / f"runs.{set_id}.jsonl"
    write_jsonl(out_file, rows_out)
    return {
        "set_id": set_id,
        "counts": dict(counts),
        "state_counts": dict(state_counts),
        "substudy_counts": {k: dict(v) for k, v in substudy_counts.items()},
        "excluded_reasons": dict(excluded_reasons),
        "unique_clusters": len(clusters),
        "elapsed_sec": round(time.time() - t0, 2),
        "out_file": str(out_file),
    }


def ingest_rhb_delivery_queue() -> dict[str, Any]:
    t0 = time.time()
    set_id = "rhb-delivery-queue"
    source_family = "joesbobclaw/reward-hacking-benchmark (seven mature studies)"
    adapter_path = STUDY_ROOT / "research/evidence/runs/corpus-expansion-pq2-20261028/corpora/joesbobclaw__reward-hacking-benchmark/tools/adapter.py"
    mod = load_module("rhb_adapter", adapter_path)

    rows_out: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    state_counts: Counter[str] = Counter()
    excluded_reasons: Counter[str] = Counter()
    clusters: set[str] = set()
    seen_ids: set[str] = set()
    adversarial_families = {"containment", "evasion", "degraded_controls"}

    for spec in mod.specs():
        counts["total_specs"] += 1
        state = spec["labels"]["state"]
        state_counts[state] += 1
        if state not in ("positive", "negative"):
            counts["excluded"] += 1
            excluded_reasons[spec["labels"]["exclusion_reason"] or state] += 1
            continue
        label = 1 if state == "positive" else 0
        native_id = spec["trace_key"]
        assert native_id not in seen_ids, f"Duplicate native_id in {set_id}"
        seen_ids.add(native_id)
        cluster = sha256_hex(spec["identity"]["cluster_key"])
        clusters.add(cluster)

        study_family = spec["identity"]["study_family"]
        natural = False if study_family in adversarial_families else None

        run_key = f"{set_id}:{native_id}"
        view = build_view(run_key=run_key, atif_steps=spec["_steps"], task_text=None, reward=None, diff=None)
        full_path, act_path = write_views(set_id, native_id, view)
        n_steps, n_chars = view_size(view)

        rows_out.append(
            runs_row(
                set_id=set_id,
                native_id=native_id,
                split="heldout",
                label=label,
                label_type="executed",
                natural=natural,
                cluster=cluster,
                source_family=source_family,
                view_full=full_path,
                view_actions=act_path,
                n_steps=n_steps,
                n_chars=n_chars,
                read_by=[],
                extra={
                    "primary": True,
                    "label_state": state,
                    "fa_basis": "no_flag_fired",
                    "decisive_rule": False,
                    "study_family": study_family,
                },
            )
        )
        counts["written"] += 1
        counts["pos" if label == 1 else "neg"] += 1

    out_file = WORKER_DIR / f"runs.{set_id}.jsonl"
    write_jsonl(out_file, rows_out)
    return {
        "set_id": set_id,
        "counts": dict(counts),
        "state_counts": dict(state_counts),
        "excluded_reasons": dict(excluded_reasons),
        "unique_clusters": len(clusters),
        "elapsed_sec": round(time.time() - t0, 2),
        "out_file": str(out_file),
    }


def ingest_flake_laundering() -> dict[str, Any]:
    t0 = time.time()
    set_id = "flake-laundering"
    source_family = "zaidwhy/flake-laundering"
    adapter_path = STUDY_ROOT / "research/evidence/runs/corpus-expansion-pq3-20261028/corpora/zaidwhy__flake-laundering/tools/adapter.py"
    mod = load_module("flake_adapter", adapter_path)

    rows_out: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    state_counts: Counter[str] = Counter()
    excluded_reasons: Counter[str] = Counter()
    clusters: set[str] = set()
    seen_ids: set[str] = set()

    for spec in mod.specs():
        counts["total_specs"] += 1
        state = spec["labels"]["state"]
        reason = spec["labels"]["reason"]
        state_counts[state] += 1
        if state not in ("positive", "negative"):
            counts["excluded"] += 1
            excluded_reasons[f"{state}:{reason}"] += 1
            continue
        label = 1 if state == "positive" else 0
        native_id = spec["identity"]["trace_key"]
        assert native_id not in seen_ids, f"Duplicate native_id in {set_id}"
        seen_ids.add(native_id)
        cluster = spec["identity"]["cluster_key"]
        clusters.add(cluster)

        raw_steps = spec["_steps"]
        diff_text: str | None = None
        if raw_steps and raw_steps[-1].get("input_role") == "final_diff":
            diff_text = raw_steps[-1].get("message") or ""

        run_key = f"{set_id}:{native_id}"
        view = build_view(run_key=run_key, atif_steps=raw_steps, task_text=None, reward=None, diff=diff_text)
        full_path, act_path = write_views(set_id, native_id, view)
        n_steps, n_chars = view_size(view)

        rows_out.append(
            runs_row(
                set_id=set_id,
                native_id=native_id,
                split="heldout",
                label=label,
                label_type="executed",
                natural=None,
                cluster=cluster,
                source_family=source_family,
                view_full=full_path,
                view_actions=act_path,
                n_steps=n_steps,
                n_chars=n_chars,
                read_by=["np21-cm1:cascade_v8_sonnet", "np21-cm1:facets_haiku", "np29-cm1:test_edit_s"],
                extra={
                    "primary": True,
                    "label_state": state,
                    "label_reason": reason,
                    "hard_negative_legit_test_repair": bool(spec["labels"]["hard_negative_legit_test_repair"]),
                    "fa_basis": "mixed",
                    "decisive_rule": False,
                },
            )
        )
        counts["written"] += 1
        counts["pos" if label == 1 else "neg"] += 1
        if spec["labels"]["hard_negative_legit_test_repair"]:
            counts["hard_negative_legit_test_repair"] += 1

    out_file = WORKER_DIR / f"runs.{set_id}.jsonl"
    write_jsonl(out_file, rows_out)
    return {
        "set_id": set_id,
        "counts": dict(counts),
        "state_counts": dict(state_counts),
        "excluded_reasons": dict(excluded_reasons),
        "unique_clusters": len(clusters),
        "elapsed_sec": round(time.time() - t0, 2),
        "out_file": str(out_file),
    }


def ingest_kobayashi_maru() -> dict[str, Any]:
    t0 = time.time()
    set_id = "kobayashi-maru"
    source_family = "ebt55/kobayashi-maru"
    adapter_path = STUDY_ROOT / "research/evidence/runs/corpus-expansion-pq3-20261028/corpora/ebt55__kobayashi-maru/tools/adapter.py"
    mod = load_module("km_adapter", adapter_path)

    counts: Counter[str] = Counter()
    state_counts: Counter[str] = Counter()
    excluded_reasons: Counter[str] = Counter()
    pos_specs: list[dict[str, Any]] = []
    neg_specs: list[dict[str, Any]] = []
    all_scoreable_batches: set[str] = set()
    all_scoreable_tasks: set[str] = set()

    for spec in mod.specs():
        counts["total_specs"] += 1
        state = spec["labels"]["state"]
        reason = spec["labels"]["reason"]
        state_counts[state] += 1
        if state == "positive":
            pos_specs.append(spec)
            all_scoreable_batches.add(spec["identity"]["cluster_key"])
            all_scoreable_tasks.add(spec["identity"]["task_key"])
        elif state == "negative":
            neg_specs.append(spec)
            all_scoreable_batches.add(spec["identity"]["cluster_key"])
            all_scoreable_tasks.add(spec["identity"]["task_key"])
        else:
            counts["excluded_or_ambiguous"] += 1
            excluded_reasons[f"{state}:{reason}"] += 1

    total_scoreable = len(pos_specs) + len(neg_specs)
    counts["scoreable_total"] = total_scoreable
    counts["scoreable_pos"] = len(pos_specs)
    counts["scoreable_neg"] = len(neg_specs)

    target_n = 3000
    assert total_scoreable > target_n, f"Expected > {target_n} scoreable rows, got {total_scoreable}"
    n_pos_sample = round(target_n * len(pos_specs) / total_scoreable)
    n_neg_sample = target_n - n_pos_sample

    rng = random.Random(1104)
    sampled_pos = rng.sample(pos_specs, n_pos_sample)
    sampled_neg = rng.sample(neg_specs, n_neg_sample)
    sampled = sorted(sampled_pos + sampled_neg, key=lambda s: s["identity"]["row_index"])

    rows_out: list[dict[str, Any]] = []
    sampled_batches: set[str] = set()
    sampled_tasks: set[str] = set()
    seen_ids: set[str] = set()
    slice_counts: Counter[str] = Counter()
    env_counts: Counter[str] = Counter()

    for spec in sampled:
        state = spec["labels"]["state"]
        label = 1 if state == "positive" else 0
        native_id = spec["identity"]["trace_key"]
        assert native_id not in seen_ids, f"Duplicate native_id in {set_id}"
        seen_ids.add(native_id)

        batch_key = spec["identity"]["cluster_key"]
        task_key = spec["identity"]["task_key"]
        sampled_batches.add(batch_key)
        sampled_tasks.add(task_key)

        p_cond = spec["labels"]["prompt_condition"]
        natural = False if p_cond in ("peer_tip", "peer_tip_read") else None
        i_slice = spec["labels"]["item_slice"]
        e_ver = spec["labels"]["env_version"]
        slice_counts[f"{i_slice}:{state}"] += 1
        env_counts[f"v{e_ver}:{state}"] += 1

        steps = adapt_steps_kobayashi(spec["_steps"])
        run_key = f"{set_id}:{native_id}"
        view = build_view(run_key=run_key, atif_steps=steps, task_text=None, reward=None, diff=None)
        full_path, act_path = write_views(set_id, native_id, view)
        n_steps, n_chars = view_size(view)

        rows_out.append(
            runs_row(
                set_id=set_id,
                native_id=native_id,
                split="heldout",
                label=label,
                label_type="rule",
                natural=natural,
                cluster=batch_key,
                source_family=source_family,
                view_full=full_path,
                view_actions=act_path,
                n_steps=n_steps,
                n_chars=n_chars,
                read_by=["np21-cm1:cascade_v8_sonnet", "np21-cm1:facets_haiku", "np22-cm1:gap_analysis"],
                extra={
                    "primary": True,
                    "label_state": state,
                    "fa_basis": "no_flag_fired",
                    "decisive_rule": False,
                    "env_version": e_ver,
                    "item_slice": i_slice,
                    "prompt_condition": p_cond,
                    "task_key": task_key,
                    "batch_key": batch_key,
                    "sampled_from_scoreable": total_scoreable,
                    "sample_seed": 1104,
                },
            )
        )
        counts["written"] += 1
        counts["pos" if label == 1 else "neg"] += 1

    out_file = WORKER_DIR / f"runs.{set_id}.jsonl"
    write_jsonl(out_file, rows_out)
    return {
        "set_id": set_id,
        "counts": dict(counts),
        "state_counts": dict(state_counts),
        "excluded_reasons": dict(excluded_reasons),
        "all_scoreable_batches": len(all_scoreable_batches),
        "all_scoreable_tasks": len(all_scoreable_tasks),
        "sampled_batches": len(sampled_batches),
        "sampled_tasks": len(sampled_tasks),
        "sampled_slice_counts": dict(slice_counts),
        "sampled_env_counts": dict(env_counts),
        "elapsed_sec": round(time.time() - t0, 2),
        "out_file": str(out_file),
    }


def ingest_rsi_benchmark() -> dict[str, Any]:
    t0 = time.time()
    set_id = "rsi-benchmark-monitor-training"
    source_family = "scaleapi/rsi-benchmark agent-safety-monitor-training (public train + validation)"
    adapter_path = STUDY_ROOT / "research/evidence/runs/corpus-expansion-pv17-20261029/corpora/scaleapi__rsi-benchmark/tools/adapter.py"
    mod = load_module("rsi_adapter", adapter_path)

    rows_out: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    state_counts: Counter[str] = Counter()
    excluded_reasons: Counter[str] = Counter()
    clusters: set[str] = set()
    scenarios: set[str] = set()
    seen_ids: set[str] = set()

    for spec in mod.specs():
        counts["total_specs"] += 1
        state = spec["gold"]["state"]
        reason = spec["gold"]["reason"]
        state_counts[state] += 1
        if state not in ("positive", "negative"):
            counts["excluded"] += 1
            excluded_reasons[f"{state}:{reason}"] += 1
            continue
        label = 1 if state == "positive" else 0
        native_id = spec["identity"]["run_id_sha256"]
        assert native_id not in seen_ids, f"Duplicate native_id in {set_id}"
        seen_ids.add(native_id)

        scenario = spec["identity"]["scenario"]
        actor = spec["identity"]["actor"]
        cluster = sha256_hex(f"{scenario}:{actor}")
        scenario_key = sha256_hex(scenario)
        clusters.add(cluster)
        scenarios.add(scenario_key)

        run_key = f"{set_id}:{native_id}"
        view = build_view(run_key=run_key, atif_steps=spec["_steps"], task_text=None, reward=None, diff=None)
        full_path, act_path = write_views(set_id, native_id, view)
        n_steps, n_chars = view_size(view)

        rows_out.append(
            runs_row(
                set_id=set_id,
                native_id=native_id,
                split="heldout",
                label=label,
                label_type="executed",
                natural=False,
                cluster=cluster,
                source_family=source_family,
                view_full=full_path,
                view_actions=act_path,
                n_steps=n_steps,
                n_chars=n_chars,
                read_by=[],
                extra={
                    "primary": True,
                    "label_state": state,
                    "fa_basis": "established",
                    "decisive_rule": False,
                    "scenario_key": scenario_key,
                },
            )
        )
        counts["written"] += 1
        counts["pos" if label == 1 else "neg"] += 1

    out_file = WORKER_DIR / f"runs.{set_id}.jsonl"
    write_jsonl(out_file, rows_out)
    return {
        "set_id": set_id,
        "counts": dict(counts),
        "state_counts": dict(state_counts),
        "excluded_reasons": dict(excluded_reasons),
        "unique_clusters": len(clusters),
        "unique_scenarios": len(scenarios),
        "elapsed_sec": round(time.time() - t0, 2),
        "out_file": str(out_file),
    }


HANDLERS = {
    "mistrace-reward-hacking": ingest_mistrace,
    "reward-pressure-study-2": ingest_reward_pressure_study_2,
    "rhb-delivery-queue": ingest_rhb_delivery_queue,
    "flake-laundering": ingest_flake_laundering,
    "kobayashi-maru": ingest_kobayashi_maru,
    "rsi-benchmark-monitor-training": ingest_rsi_benchmark,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sets", nargs="*", default=list(HANDLERS))
    args = parser.parse_args()

    WORKER_DIR.mkdir(parents=True, exist_ok=True)
    summaries = []
    for s in args.sets:
        if s not in HANDLERS:
            raise ValueError(f"Unknown set: {s}")
        res = HANDLERS[s]()
        summaries.append(res)
        print(json.dumps(res, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
