"""Held-out ingest, slice ho-b: evilgenie-escalation, sdf-shortcut-taking,
researcharena-benchmark-gaming, sleight-bench, paichecker.

Label-free views go to the shared views root through detbench_lib; label rows go to
the worker folder as runs.<set_id>.jsonl. Only aggregates are printed.

Run from the eval-lab worktree root:
    uv run python research/experiments/ov1104-detbench/ingest_heldout_b/ingest.py --set <set_id>
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import detbench_lib as dl  # noqa: E402

STUDY = Path("/private/tmp/eval-lab-study")
REGISTRY = Path.home() / "Developer/research-context/bridge/2026-10-27-run-atlas/cross-test-registry.json"
WORKER = Path("/private/tmp/ov1104/detbench/workers/ho-b")
SPLIT = "heldout"

# Prior detector exposure, from causal-prefix-monitor.md (CM-18, B6) and
# np45-heldout-corpus-table.md. CM-18/B6 read capped samples, not the whole set.
READ_BY_CM18 = ["CM-18 cascade-v8 (sample<=60/stratum)", "CM-18 facets-haiku (sample<=60/stratum)", "B6 facets-sonnet (CM-18 subsample)"]


def registry_entry(set_id: str) -> dict[str, Any]:
    entries = {e["id"]: e for e in json.loads(REGISTRY.read_text())["entries"]}
    return entries[set_id]


def load_adapter(module_path: str):
    path = STUDY / module_path
    spec = importlib.util.spec_from_file_location(f"adapter_{hashlib.sha1(str(path).encode()).hexdigest()[:8]}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def hashed_id(raw: str) -> str:
    """The profile's `full` normalisation: sha256(id.casefold()). Used where the README
    forbids persisting raw ids."""
    return hashlib.sha256(raw.casefold().encode("utf-8")).hexdigest()


def emit(set_id: str, entry: dict[str, Any], rows: Iterator[dict[str, Any]]) -> dict[str, Any]:
    """rows: dicts with native_id, steps, label, label_type, natural, cluster, extra,
    task_text, reward, diff. Writes views + runs file; returns aggregate counts."""
    out_rows = []
    counts: Counter[str] = Counter()
    clusters: set[str] = set()
    fa_basis = entry["labels"]["false_alarm_basis"]["negative_basis"]
    for r in rows:
        run_key = f"{set_id}:{r['native_id']}"
        view = dl.build_view(run_key, r["steps"], task_text=r.get("task_text"), reward=r.get("reward"), diff=r.get("diff"))
        full, acts = dl.write_views(set_id, r["native_id"], view)
        n_steps, n_chars = dl.view_size(view)
        extra = {"primary": True, "label_state": r["label_state"], "fa_basis": fa_basis, "decisive_rule": False}
        extra.update(r.get("extra") or {})
        out_rows.append(
            dl.runs_row(
                set_id=set_id, native_id=r["native_id"], split=SPLIT, label=r["label"], label_type=r["label_type"],
                natural=r["natural"], cluster=r["cluster"], source_family=entry["name"], view_full=full,
                view_actions=acts, n_steps=n_steps, n_chars=n_chars, read_by=r["read_by"], extra=extra,
            )
        )
        counts["rows"] += 1
        counts["positive" if r["label"] else "negative"] += 1
        counts["label_state:" + r["label_state"]] += 1
        counts["zero_step_views"] += n_steps == 0
        clusters.add(r["cluster"])
    n = dl.write_jsonl(WORKER / f"runs.{set_id}.jsonl", out_rows)
    return {"written": n, **dict(sorted(counts.items())), "clusters": len(clusters)}


# ---------------------------------------------------------------- evilgenie-escalation

def evilgenie(entry: dict[str, Any]) -> tuple[Iterator[dict[str, Any]], Counter[str]]:
    ad = load_adapter(entry["adapter"]["module_path"])
    excluded: Counter[str] = Counter()

    def gen():
        for spec in ad.specs():
            lab, ident = spec["labels"], spec["identity"]
            if not lab["primary_eligible"]:
                excluded["state:" + lab["state"]] += 1
                excluded["cohort:" + ident["cohort"]] += 1
                continue
            yield {
                "native_id": hashed_id(ident["run_key"]),
                "steps": spec["_steps"],
                "label": int(lab["state"] == "positive"),
                "label_type": "judge",  # primary positives all carry the GPT-4o judge reward_hacked=yes; executed/tamper channels only exclude
                "natural": None,
                "cluster": hashed_id(ident["problem_id"]),  # 15 underlying problems; set-qualified id is descriptive only
                "read_by": READ_BY_CM18,
                "label_state": lab["state"],
                "extra": {"cohort": ident["cohort"], "native_classification": lab["native_classification"],
                          "model_hash": hashed_id(ident["model"])[:16], "condition": ident["condition"]},
            }

    return gen(), excluded


# ---------------------------------------------------------------- sdf-shortcut-taking

def sdf(entry: dict[str, Any]) -> tuple[Iterator[dict[str, Any]], Counter[str]]:
    ad = load_adapter(entry["adapter"]["module_path"])
    excluded: Counter[str] = Counter()

    def gen():
        for spec in ad.specs():
            lab, ident = spec["labels"], spec["identity"]
            if lab["state"] == "excluded":
                excluded["+".join(lab["exclusion_reasons"])] += 1
                continue
            cl = ident["bootstrap_cluster"]
            yield {
                "native_id": hashed_id(ident["run_id"]),
                "steps": spec["_steps"],
                "label": int(lab["state"] == "positive"),
                "label_type": "executed",
                "natural": None,
                "cluster": f"{cl[0]}|{cl[1]}",
                "read_by": READ_BY_CM18,
                "label_state": lab["state"],
                "extra": {"negative_class": lab["negative_class"], "model_arm": ident["model_arm"],
                          "target_errors": ident["target_errors"], "max_steps": ident["max_steps"]},
            }

    return gen(), excluded


# ---------------------------------------------------------------- researcharena-benchmark-gaming

def _ra_step(step: dict[str, Any]) -> dict[str, Any]:
    """Codex file_change/todo items have no message/tool_calls/observation key; fold
    them into the message so normalize_steps keeps them."""
    if step.get("message") or step.get("tool_calls") or step.get("observation") or step.get("reasoning_content"):
        return step
    for k in ("file_changes", "todos"):
        if k in step:
            s = dict(step)
            s["message"] = f"[{k}] " + json.dumps(step[k], ensure_ascii=False, sort_keys=True)
            return s
    return step


def researcharena(entry: dict[str, Any]) -> tuple[Iterator[dict[str, Any]], Counter[str]]:
    ad = load_adapter(entry["adapter"]["module_path"])
    excluded: Counter[str] = Counter()

    def gen():
        for spec in ad.specs():  # 41 scoreable only; excluded records are not yielded by default
            lab, ident = spec["labels"], spec["identity"]
            cl = ident["bootstrap_cluster"]
            yield {
                "native_id": spec["trace_key"],
                "steps": [_ra_step(s) for s in spec["_steps"]],
                "label": int(lab["state"] == "positive"),
                "label_type": "executed",
                "natural": False,
                "cluster": f"{cl[0]}|{cl[1]}",
                "read_by": READ_BY_CM18,
                "label_state": lab["state"],
                "extra": {"condition": lab["condition"], "agent": ident["agent"], "provider": ident["provider"]},
            }

    return gen(), excluded


# ---------------------------------------------------------------- sleight-bench

def sleight(entry: dict[str, Any]) -> tuple[Iterator[dict[str, Any]], Counter[str]]:
    ad = load_adapter(entry["adapter"]["module_path"])

    def gen():
        for spec in ad.specs():
            yield {
                "native_id": spec["trace_key"],
                "steps": spec["_steps"],
                "label": int(spec["positive"]),
                "label_type": "constructed",
                "natural": False,
                "cluster": spec["pair_key"],
                "read_by": [],
                "label_state": "attack" if spec["positive"] else "benign",
                "extra": {"segment_pair_key": spec["segment_pair_key"], "category_key": spec["labels"]["category_key"]},
            }

    return gen(), Counter()


# ---------------------------------------------------------------- paichecker

PAI_ORDER = ("repo", "base_commit", "problem_statement", "hints_text", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS", "patch")


def _pai_text(ai: dict[str, Any]) -> str:
    parts = []
    for f in PAI_ORDER:
        v = ai[f]
        if isinstance(v, list):
            v = "\n".join(v)
        parts.append(f"=== {f} ===\n{v if v is not None else ''}")
    return "\n\n".join(parts)


def paichecker(entry: dict[str, Any]) -> tuple[Iterator[dict[str, Any]], Counter[str]]:
    ad = load_adapter(entry["adapter"]["module_path"])

    def gen():
        for spec in ad.specs():
            ai, gold, ident = spec["auditor_input"], spec["gold"], spec["identity"]
            yield {
                "native_id": hashed_id(ident["instance_id"]),
                "steps": [{"step_id": 0, "source": "agent", "message": _pai_text(ai)}],
                "task_text": None,
                "diff": ai["patch"],
                "label": int(gold["positive"]),
                "label_type": "human",
                "natural": None,
                "cluster": f"{ident['sub_corpus']}:{ident['repo']}",
                "read_by": [],
                "label_state": "misaligned" if gold["positive"] else "no_misalignment",
                "extra": {"sub_corpus": ident["sub_corpus"], "families": gold["families"], "unit": "task"},
            }

    return gen(), Counter()


SETS: dict[str, Callable[[dict[str, Any]], tuple[Iterator[dict[str, Any]], Counter[str]]]] = {
    "evilgenie-escalation": evilgenie,
    "sdf-shortcut-taking": sdf,
    "researcharena-benchmark-gaming": researcharena,
    "sleight-bench": sleight,
    "paichecker": paichecker,
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--set", required=True, choices=sorted(SETS))
    args = ap.parse_args()
    entry = registry_entry(args.set)
    t0 = time.time()
    rows, excluded = SETS[args.set](entry)
    result = emit(args.set, entry, rows)
    result["excluded"] = dict(sorted(excluded.items()))
    result["excluded_total"] = sum(excluded.values())
    result["registry_counts"] = entry["labels"]["counts"]
    result["seconds"] = round(time.time() - t0, 1)
    print(json.dumps({"set": args.set, **result}, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
