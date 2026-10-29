#!/usr/bin/env python3
"""NP29-CM-1 phase 1 (label-free): group sizes of the mechanical definition by corpus and class; writes results/membership.json.

Reads steps, strata and publisher outcomes only. No cascade row, card, facet ``suspicion`` or alarm is inspected or tabulated.
Prints nothing but a one-line status (the committed file is the only output, so no suppressed cell reaches a log).
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import data
import stats
import test_edits as te

CORPORA = ("goodfire", "cheatbench")
FLAGS = ("S", "SX", "SI", "SD", "L")


def run_record(steps: list[dict[str, Any]]) -> dict[str, Any]:
    full = te.analyse_steps(steps)
    rec = {"full": full, "G": te.g_flag(steps)}
    rec["tuning"] = {"S0": te.analyse_steps(steps, te.S0)["flags"]["S"]}
    rec["tuning"].update(
        {k: te.analyse_steps(steps, o)["flags"]["S"] for k, o in te.ADJUSTMENTS.items()}
    )
    return rec


def class_block(
    rids: list[str], recs: dict[str, dict[str, Any]], log: list[dict[str, Any]], name: str
) -> dict[str, Any]:
    n = len(rids)
    missing = sum(recs[r]["full"]["calls"] == 0 for r in rids)
    entry: dict[str, Any] = {
        "runs": n,
        "runs_without_tool_calls": missing if missing == 0 or missing >= 3 else stats.SUPPRESSED,
    }
    for flag in FLAGS:
        entry[flag] = stats.cell(
            sum(recs[r]["full"]["flags"][flag] for r in rids), n, log, f"{name}:{flag}"
        )
    entry["G"] = stats.cell(sum(recs[r]["G"] for r in rids), n, log, f"{name}:G")
    sg = Counter((recs[r]["full"]["flags"]["S"], recs[r]["G"]) for r in rids)
    entry["S_by_G"] = stats.partition(
        {
            "S&G": sg[(True, True)],
            "S&notG": sg[(True, False)],
            "notS&G": sg[(False, True)],
            "notS&notG": sg[(False, False)],
        },
        log,
        f"{name}:SxG",
    )
    entry["tuning_S_counts"] = {
        k: stats.cell(sum(recs[r]["tuning"][k] for r in rids), n, log, f"{name}:tuning:{k}")
        for k in ("S0", *te.ADJUSTMENTS)
    }
    entry["tuning_S_counts"]["S_primary"] = entry["S"]
    return entry


def task_block(
    cohort: dict[str, dict[str, Any]], recs: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for corpus in CORPORA:
        runs = {
            r: v
            for r, v in cohort.items()
            if v["corpus"] == corpus and v["class"] != "considered_only"
        }
        sizes = Counter(v["task"] for v in runs.values())
        with_prompt = {r for r, v in runs.items() if v["has_prompt"]}
        tasks_with_prompt = {v["task"] for r, v in runs.items() if r in with_prompt}
        both: dict[str, set[str]] = {}
        for v in runs.values():
            both.setdefault(v["task"], set()).add(v["class"])
        out[corpus] = {
            "scored_runs": len(runs),
            "runs_with_task_prompt": len(with_prompt),
            "runs_without_task_prompt_each_its_own_cluster": len(runs) - len(with_prompt),
            "distinct_tasks_upper_bound": len(sizes),
            "distinct_tasks_among_runs_with_prompt": len(tasks_with_prompt),
            "max_runs_per_task": max(sizes.values()),
            "tasks_with_at_least_3_runs": sum(n >= 3 for n in sizes.values()),
            "tasks_with_both_classes": sum(len(c) > 1 for c in both.values()),
        }
    return out


def clean_s_check(
    cohort: dict[str, dict[str, Any]], recs: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Exact task counts of the clean S runs and the task-key check (a task count identifies no run)."""
    out: dict[str, Any] = {}
    for corpus, clean in (("goodfire", "clean_no"), ("cheatbench", "negative")):
        s_runs = [
            r
            for r, v in cohort.items()
            if v["corpus"] == corpus and v["class"] == clean and recs[r]["full"]["flags"]["S"]
        ]
        tasks = {cohort[r]["task"] for r in s_runs}
        # every run of those tasks, to see how much the shared prompt could be boilerplate
        members = [
            r
            for r, v in cohort.items()
            if v["corpus"] == corpus and v["task"] in tasks and v["has_prompt"]
        ]
        first_actions = set()
        for r in members:
            for step in cohort[r]["doc"]["steps"]:
                if step.get("source") == "agent":
                    first_actions.add(
                        hashlib.sha256(
                            json.dumps(step.get("tool_calls"), sort_keys=True, default=str).encode()
                        ).hexdigest()[:8]
                    )
                    break
        out[corpus] = {
            "clean_S_runs_without_task_prompt": sum(not cohort[r]["has_prompt"] for r in s_runs),
            "clean_S_exact_distinct_tasks": len(tasks),
            "runs_sharing_those_prompts_all_classes": len(members),
            "distinct_first_agent_actions_among_those_runs": len(first_actions),
            "clean_S_distinct_write_targets_hashed": len(
                {t for r in s_runs for t in recs[r]["full"]["targets"]}
            ),
            "independent_task_source": "none stored: registry metadata holds item_id and a blind projection id only; trial dir holds trajectory.json only",
        }
    return out


def facet_record_sets(
    cohort: dict[str, dict[str, Any]], store: Path, log: list[dict[str, Any]]
) -> dict[str, Any]:
    ids = set(cohort)
    lines = data.facet_lines(store, ids, ("run_id", "facet_model", "tests"))
    attested = data.attested_runs(store.parent / "facet-calls-v1.jsonl", ids)
    last = {r["run_id"]: r for r in lines if r["_valid"] and r["facet_model"] == data.HAIKU}
    edits = {"modified", "deleted", "replaced"}
    out: dict[str, Any] = {}
    for corpus in CORPORA:
        out[corpus] = {}
        for cls in sorted({v["class"] for v in cohort.values() if v["corpus"] == corpus}):
            rids = [r for r, v in cohort.items() if v["corpus"] == corpus and v["class"] == cls]
            primary = [r for r in rids if r in last and r in attested]
            every = [r for r in rids if r in last]
            out[corpus][cls] = {
                "identities": len(rids),
                "G_Haiku_all": len(every),
                "G_Haiku_primary_attested_v2_form": len(primary),
                "reader_tests_field_edit_on_primary": stats.cell(
                    sum(last[r]["tests"] in edits for r in primary),
                    len(primary),
                    log,
                    f"{corpus}:{cls}:R_primary",
                ),
            }
    return out


def flake_block(log: list[dict[str, Any]]) -> dict[str, Any]:
    runs, outcome = data.flake_runs()
    recs = {r["key"]: run_record(r["steps"]) for r in runs}
    rows: dict[str, Any] = {}
    groups: dict[str, list[str]] = {}
    for r in runs:
        groups.setdefault(f"{r['stratum']}/{outcome.get(r['key'], 'none')}", []).append(r["key"])
    for name, rids in sorted(groups.items()):
        rows[name] = (
            class_block(rids, recs, log, f"flake:{name}")
            if len(rids) >= 3
            else {"runs": stats.SUPPRESSED}
        )
    established = [r for r in runs if r["stratum"] == "negative_established"]
    s_est = [r for r in established if recs[r["key"]]["full"]["flags"]["S"]]
    return {
        "role": "definition-tuning sample, descriptive; the hypothesis-generating sample",
        "runs": len(runs),
        "distinct_tasks": len({r["task"] for r in runs}),
        "distinct_tasks_among_established_negatives": len({r["task"] for r in established}),
        "distinct_tasks_among_S_established_negatives": len({r["task"] for r in s_est}),
        "by_stratum_and_publisher_outcome": rows,
        "runs_pickle_sha256": data.file_sha(data.FLAKE_STATE / "runs.pkl"),
        "reasons_sha256": data.file_sha(data.FLAKE_STATE / "reasons.json"),
    }


def main() -> int:
    cohort, meta = data.load_cohorts()
    recs = {r: run_record((v["doc"] or {}).get("steps") or []) for r, v in cohort.items()}
    log: list[dict[str, Any]] = []
    membership: dict[str, Any] = {}
    forms: dict[str, Counter[str]] = {c: Counter() for c in CORPORA}
    for corpus in CORPORA:
        membership[corpus] = {}
        for cls in sorted({v["class"] for v in cohort.values() if v["corpus"] == corpus}):
            rids = [r for r, v in cohort.items() if v["corpus"] == corpus and v["class"] == cls]
            membership[corpus][cls] = class_block(rids, recs, log, f"{corpus}:{cls}")
            for r in rids:
                forms[corpus].update(recs[r]["full"]["forms"])
    store = meta.pop("facet_store")
    result = {
        "revision": 2,
        "cohort": meta | {"facet_store_sha256": data.file_sha(store)},
        "membership": membership,
        "task_clusters_scored_runs": task_block(cohort, recs),
        "clean_S_task_check": clean_s_check(cohort, recs),
        "runs_per_strict_form_pooled_over_classes": {
            c: {k: stats.sup(v) for k, v in sorted(f.items())} for c, f in forms.items()
        },
        "facet_record_sets": facet_record_sets(cohort, store, log),
        "flake_descriptive": flake_block(log),
        "complement_log": log,
        "tool_sha256": {
            n: data.file_sha(data.HERE / f"{n}.py")
            for n in ("test_edits", "stats", "data", "membership")
        },
    }
    data.RESULTS.mkdir(parents=True, exist_ok=True)
    (data.RESULTS / "membership.json").write_text(
        json.dumps(result, indent=1, sort_keys=True) + "\n"
    )
    print("wrote results/membership.json")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
