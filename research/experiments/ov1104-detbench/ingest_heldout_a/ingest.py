"""Held-out ingest for slice ho-a: reflex-sentinel, beam-tripwire, agent-cheat-index, kernelbench-hard-runs.

Writes label-free views (full and actions_only) to /private/tmp/ov1104/shared/detbench/views/
and run metadata rows to /private/tmp/ov1104/detbench/workers/ho-a/runs.<set_id>.jsonl.
Never prints or copies corpus transcript text or raw forbidden identifiers.

Usage (from eval-lab worktree root):
    uv run python research/experiments/ov1104-detbench/ingest_heldout_a/ingest.py --set <set_id> [--selfcheck]
    uv run python research/experiments/ov1104-detbench/ingest_heldout_a/ingest.py --set all [--selfcheck]
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import detbench_lib as dl  # noqa: E402

STUDY = Path("/private/tmp/eval-lab-study")
REGISTRY = Path.home() / "Developer/research-context/bridge/2026-10-27-run-atlas/cross-test-registry.json"
WORKER = Path("/private/tmp/ov1104/detbench/workers/ho-a")
SPLIT = "heldout"
SETS_ORDER = ("reflex-sentinel", "beam-tripwire", "agent-cheat-index", "kernelbench-hard-runs")

# Prior detector exposure per causal-prefix-monitor.md (CM-18, ID-CM-1, B6, pK-SELF-dd216e-2)
# and data-access-map.md (NP35-LB-1 step-shape census).
READ_BY_CM18 = [
    "CM-18 cascade-v8 (sample<=60/stratum)",
    "CM-18 facets-haiku (sample<=60/stratum)",
    "ID-CM-1 latency (CM-18 stored replays)",
    "B6 facets-sonnet (CM-18 subsample<=15/stratum)",
    "pK-SELF-dd216e-2 facets-sonnet>=1 (B6 subsample)",
]


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def registry_entry(set_id: str) -> dict[str, Any]:
    entries = {e["id"]: e for e in json.loads(REGISTRY.read_text(encoding="utf-8"))["entries"]}
    return entries[set_id]


def load_adapter(module_path: str) -> Any:
    path = STUDY / module_path
    mod_name = f"adapter_{hashlib.sha1(str(path).encode('utf-8')).hexdigest()[:10]}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def emit(set_id: str, entry: dict[str, Any], rows: Iterator[dict[str, Any]]) -> dict[str, Any]:
    out_rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    clusters: set[str] = set()
    fa_basis = entry["labels"]["false_alarm_basis"]["negative_basis"]
    decisive_rule = False
    floors = entry.get("content_free_floors")
    if isinstance(floors, dict):
        # Check if any registered rule >= 0.95
        for v in floors.values():
            if isinstance(v, (int, float)) and v >= 0.95:
                decisive_rule = True

    for r in rows:
        native_id = r["native_id"]
        run_key = f"{set_id}:{native_id}"
        view = dl.build_view(
            run_key,
            r["steps"],
            task_text=r.get("task_text"),
            reward=r.get("reward"),
            diff=r.get("diff"),
        )
        full_path, acts_path = dl.write_views(set_id, native_id, view)
        n_steps, n_chars = dl.view_size(view)
        extra: dict[str, Any] = {
            "primary": True,
            "label_state": r["label_state"],
            "fa_basis": fa_basis,
            "decisive_rule": decisive_rule,
        }
        if r.get("extra"):
            extra.update(r["extra"])
        row = dl.runs_row(
            set_id=set_id,
            native_id=native_id,
            split=SPLIT,
            label=r["label"],
            label_type=r["label_type"],
            natural=r["natural"],
            cluster=r["cluster"],
            source_family=entry["name"],
            view_full=full_path,
            view_actions=acts_path,
            n_steps=n_steps,
            n_chars=n_chars,
            read_by=r["read_by"],
            extra=extra,
        )
        out_rows.append(row)
        counts["rows"] += 1
        counts["positive" if r["label"] else "negative"] += 1
        counts[f"label_state:{r['label_state']}"] += 1
        if extra.get("established_negative"):
            counts["established_negatives"] += 1
        if n_steps == 0:
            counts["zero_step_views"] += 1
        clusters.add(r["cluster"])

    out_path = WORKER / f"runs.{set_id}.jsonl"
    written = dl.write_jsonl(out_path, out_rows)
    return {
        "set_id": set_id,
        "written": written,
        "clusters": len(clusters),
        "counts": dict(sorted(counts.items())),
        "out_path": str(out_path),
    }


def ingest_reflex_sentinel(entry: dict[str, Any]) -> tuple[Iterator[dict[str, Any]], dict[str, int]]:
    ad = load_adapter(entry["adapter"]["module_path"])
    # Secondary retry cohorts are excluded per README and PROMPT.md (primary = 250 agent cohort rows only).
    pilot_rows = sum(1 for _ in ad.iter_rows(subset="branch_pilot"))
    main_rows = sum(1 for _ in ad.iter_rows(subset="branch_main"))
    excluded = {
        "secondary_branch_pilot_retries": pilot_rows,
        "secondary_branch_main_retries": main_rows,
    }

    def gen() -> Iterator[dict[str, Any]]:
        for spec in ad.specs(subset="agent"):
            pos = bool(spec["positive"])
            raw_id = spec["trace_id"]
            yield {
                "native_id": spec["trace_key"],
                "steps": spec["_steps"],
                "label": 1 if pos else 0,
                "label_type": "executed",
                "natural": None,
                "cluster": spec["task_key"],
                "read_by": READ_BY_CM18,
                "label_state": "positive" if pos else "negative",
                "extra": {
                    "split_cohort": spec["split"],
                    "established_negative": (not pos) and (spec["split"] == "original"),
                    "pass_own": bool(spec["gold"]["pass_own"]),
                    "pass_fresh": bool(spec["gold"]["pass_fresh"]),
                    "trace_key_64": sha256_hex(raw_id),
                    "cm18_key": sha256_hex(f"reflex:{raw_id}")[:16],
                },
            }

    return gen(), excluded


def ingest_beam_tripwire(entry: dict[str, Any]) -> tuple[Iterator[dict[str, Any]], dict[str, int]]:
    ad = load_adapter(entry["adapter"]["module_path"])
    excluded: Counter[str] = Counter()

    def gen() -> Iterator[dict[str, Any]]:
        for spec in ad.specs():
            ident = spec["identity"]
            gold = spec["gold"]
            lab = spec["labels"]
            state = gold["state"]
            if state not in ("positive", "negative"):
                excluded[f"{state}:{gold['reason']}"] += 1
                continue
            pos = state == "positive"
            raw_id = ident["run_id"]
            yield {
                "native_id": ident["trace_key"],
                "steps": spec["_steps"],
                "label": 1 if pos else 0,
                "label_type": "rule",
                "natural": None,
                "cluster": ident["task_key"],
                "read_by": READ_BY_CM18,
                "label_state": state,
                "extra": {
                    "condition": lab["condition"],
                    "slice": lab["slice"],
                    "effort": ident["effort"],
                    "experiment": ident["experiment"],
                    "established_negative": (not pos) and (lab["condition"] == "possible"),
                    "trace_key_16": ident["trace_key"][:16],
                    "cm18_key": sha256_hex(f"beam:{raw_id}")[:16],
                },
            }

    return gen(), excluded


def ingest_agent_cheat_index(entry: dict[str, Any]) -> tuple[Iterator[dict[str, Any]], dict[str, int]]:
    ad = load_adapter(entry["adapter"]["module_path"])
    excluded: Counter[str] = Counter()

    def gen() -> Iterator[dict[str, Any]]:
        for spec in ad.specs(include_excluded=True):
            ident = spec["identity"]
            gold = spec["gold"]
            state = gold["label_state"]
            if not gold["score_eligible"] or state not in ("positive", "negative"):
                reason = gold.get("exclusion_reason") or state
                excluded[f"{state}:{reason}"] += 1
                continue
            pos = state == "positive"
            raw_id = ident["run_id"]
            raw_task = ident["task_id"]
            yield {
                "native_id": sha256_hex(raw_id)[:16],
                "steps": spec["_steps"],
                "label": 1 if pos else 0,
                "label_type": "executed",
                "natural": None,
                "cluster": sha256_hex(raw_task)[:16],
                "read_by": READ_BY_CM18,
                "label_state": state,
                "extra": {
                    "family": ident["family"],
                    "harness": ident["harness"],
                    "rules": bool(ident["rules"]),
                    "established_negative": (not pos) and (ident["family"] == "F4"),
                    "false_success": bool(gold["source_flags"].get("false_success")),
                    "trap_compound_state": gold["trap_compound"]["label_state"],
                    "trace_key_64": sha256_hex(raw_id),
                    "trace_key_casefold_64": sha256_hex(raw_id.casefold()),
                    "task_key_64": sha256_hex(raw_task),
                    "cm18_key": sha256_hex(f"cheatindex:{raw_id}")[:16],
                },
            }

    return gen(), excluded


def ingest_kernelbench_hard_runs(entry: dict[str, Any]) -> tuple[Iterator[dict[str, Any]], dict[str, int]]:
    ad = load_adapter(entry["adapter"]["module_path"])
    excluded: Counter[str] = Counter()

    def gen() -> Iterator[dict[str, Any]]:
        for spec in ad.specs(annotated_only=False):
            verdict = spec["verdict"]
            if verdict is None:
                excluded["unlabeled"] += 1
                continue
            pos = bool(spec["positive"])
            raw_id = spec["trace_id"]
            raw_task = spec["task"]
            yield {
                "native_id": spec["trace_key"],
                "steps": spec["_steps"],
                "label": 1 if pos else 0,
                "label_type": "human",
                "natural": None,
                "cluster": ad.trace_key(raw_task),
                "read_by": READ_BY_CM18,
                "label_state": verdict,
                "extra": {
                    "verdict": verdict,
                    "harness": spec["harness"],
                    "model_cluster": ad.trace_key(spec["model"]),
                    "established_negative": not pos,
                    "trace_key_64": sha256_hex(raw_id),
                    "cm18_key": sha256_hex(f"kernelbench:{raw_id}")[:16],
                },
            }

    return gen(), excluded


HANDLERS = {
    "reflex-sentinel": ingest_reflex_sentinel,
    "beam-tripwire": ingest_beam_tripwire,
    "agent-cheat-index": ingest_agent_cheat_index,
    "kernelbench-hard-runs": ingest_kernelbench_hard_runs,
}


def run_one(set_id: str, do_selfcheck: bool = False) -> dict[str, Any]:
    t0 = time.time()
    entry = registry_entry(set_id)
    rows_iter, excluded = HANDLERS[set_id](entry)
    summary = emit(set_id, entry, rows_iter)
    summary["excluded"] = dict(sorted(excluded.items()))
    if do_selfcheck:
        ad = load_adapter(entry["adapter"]["module_path"])
        if hasattr(ad, "selfcheck"):
            sc = ad.selfcheck()
            # Keep only top-level scalar/count summaries, no bulky per-task dicts
            summary["selfcheck"] = {
                k: v
                for k, v in sc.items()
                if k in ("revision", "totals", "subsets", "issue_count", "issues", "counts", "label_reasons", "primary_states", "secondary_trap_compound", "task_clusters", "unique_composite_run_ids", "label_fields_in_steps", "embedded_gold_payloads_removed")
            }
    summary["elapsed_sec"] = round(time.time() - t0, 2)
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--set", required=True, choices=[*SETS_ORDER, "all"])
    ap.add_argument("--selfcheck", action="store_true", help="Also run adapter selfcheck()")
    args = ap.parse_args()
    targets = SETS_ORDER if args.set == "all" else (args.set,)
    results = []
    for s in targets:
        res = run_one(s, do_selfcheck=args.selfcheck)
        results.append(res)
        print(json.dumps(res, indent=2, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
