#!/usr/bin/env python3
"""Aggregate A4B per-task results into the pre-registered outcomes, the committed manifest and the shared gate files.

Inputs (local, derived from third-party task repositories, never committed): ``<A4B>/tasks/<task>/{accept,a4b-r1,a4b-r2}.json``.
Outputs: ``summary.json`` and ``a4b-manifest.json`` (ids, statuses, counts only) next to this script, plus the shared
``gold-v1-manifest.json`` (``a4b`` block per task) and ``gold-v1.1-certified.json`` when ``--publish`` is given.

Final record per task: the ``rounds=2`` run when it exists (the shipped round is the pre-registered arm 2), otherwise the
``rounds=1`` run with ``round2_measured = false`` (the Modal cap ended the batch before round 2 could run).
Significance and intervals come from the shared ``stats.py`` (imported lazily, so the pure helpers need no extra path).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
A4B = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/A4B")
GATES = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/gates")
STATS_DIR = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver")
TEST_NAME = "test_gold_contract.py"
GOLD = ("gold_structural", "gold_heldout")


def load(path: Path) -> dict[str, Any] | None:
    return json.loads(path.read_text()) if path.is_file() else None


def final_record(r1: dict[str, Any] | None, r2: dict[str, Any] | None) -> dict[str, Any] | None:
    """The rounds=2 record when it exists, else the rounds=1 record flagged as round-2-unmeasured."""
    if r2 is not None:
        return {**r2, "round2_measured": True}
    if r1 is not None:
        return {**r1, "round2_measured": False}
    return None


def eval_pool(recs: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Pooled unseen-EVAL kill counts over base-accepted EVAL mutants (shipped round)."""
    n = k = 0
    for r in recs:
        c = r.get("counts") or {}
        n += int(c.get("eval_base_accepted", 0))
        k += int(c.get("eval_base_accepted_killed_arm2", 0))
    return {"k": k, "n": n}


def paired_lists(recs: Iterable[dict[str, Any]]) -> tuple[list[bool], list[bool]]:
    """(arm2 killed, arm1 killed) per base-accepted EVAL mutant that both arms graded."""
    a: list[bool] = []
    b: list[bool] = []
    for r in recs:
        if not r.get("round2_measured"):
            continue
        for p in r.get("paired_eval_base_accepted") or []:
            if p.get("arm1_killed") is None or p.get("arm2_killed") is None:
                continue
            a.append(bool(p["arm2_killed"]))
            b.append(bool(p["arm1_killed"]))
    return a, b


def a2_counts(recs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """A2 survivors exercised/killed by the shipped accepted test, and demonstrated weak graders."""
    exercised = killed = 0
    weak_with_test = weak_demo = 0
    weak_ids: list[str] = []
    for r in recs:
        surv = [s for s in (r.get("a2_survivors") or []) if s.get("killed") is not None]
        exercised += len(surv)
        kk = sum(1 for s in surv if s["killed"])
        killed += kk
        if r.get("is_weak") and r.get("accepted_test"):
            weak_with_test += 1
            if kk > 0:
                weak_demo += 1
                weak_ids.append(r["task_id"])
    return {"a2_survivors_exercised": exercised, "a2_survivors_killed": killed,
            "weak_tasks_with_accepted_test": weak_with_test, "weak_demonstrated": weak_demo, "weak_demonstrated_ids": weak_ids}


def sha256_file(p: Path) -> str | None:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


def collect(a4b: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for d in sorted((a4b / "tasks").iterdir()):
        acc = load(d / "accept.json")
        fin = final_record(load(d / "a4b-r1.json"), load(d / "a4b-r2.json"))
        if acc is None or fin is None:
            continue
        accepted = bool(acc.get("accepted"))
        rec = {**fin, "task_id": d.name, "accepted_test": accepted,
               "accept": {k: acc.get(k) for k in ("reason", "asked", "kept", "repair_used", "by_attempt")}}
        if accepted:
            shipped = rec.get("arm2_round") or 1
            pkg = a4b / "packages" / d.name / f"r{shipped}"
            rec["shipped_round"] = shipped
            rec["package_dir"] = str(pkg)
            rec["upgraded_package_sha256"] = rec.get("upgraded_package_sha256") or sha256_file(pkg / "tests" / TEST_NAME)
        rows.append(rec)
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    sys.path.insert(0, str(STATS_DIR))
    import stats  # noqa: PLC0415

    n = len(rows)
    accepted = [r for r in rows if r["accepted_test"]]
    status: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for r in rows:
        status[r["status"]] = status.get(r["status"], 0) + 1
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    graded = [r for r in accepted if r.get("counts")]
    pool = eval_pool(graded)
    a, b = paired_lists(graded)
    changed = [r for r in graded if r.get("round2_measured") and r.get("arm2_round") != r.get("arm1_round")]
    ca, cb = paired_lists(changed)
    asked = sum(r["accept"]["asked"] or 0 for r in accepted)
    kept = sum(r["accept"]["kept"] or 0 for r in accepted)
    gs = [r for r in rows if r["status"] in GOLD]
    gh = [r for r in rows if r["status"] == "gold_heldout"]
    a2 = a2_counts(rows)
    weak_n = sum(1 for r in rows if r.get("is_weak"))
    return {
        "cohort": n,
        "accepted_test": {"k": len(accepted), "n": n, "fmt": stats.fmt(len(accepted), n)},
        "gold_structural_or_better": {"k": len(gs), "n": n, "fmt": stats.fmt(len(gs), n)},
        "gold_heldout": {"k": len(gh), "n": n, "fmt": stats.fmt(len(gh), n)},
        "status_counts": status,
        "reason_counts": reasons,
        "functions": {"asked_total": asked, "kept_total": kept, "tasks": len(accepted)},
        "eval_unseen_kill_pooled": {**pool, "fmt": stats.fmt(pool["k"], pool["n"])},
        "a2": {**a2, "weak_cohort": weak_n, "weak_fmt": stats.fmt(a2["weak_demonstrated"], weak_n),
               "survivor_fmt": stats.fmt(a2["a2_survivors_killed"], a2["a2_survivors_exercised"])},
        "rounds2_vs_1": {
            "round2_measured_tasks": sum(1 for r in accepted if r.get("round2_measured")),
            "round2_unmeasured_tasks": sorted(r["task_id"] for r in accepted if not r.get("round2_measured")
                                              and r["status"] != "not_run"),
            "paired_all": stats.paired(a, b) if a else None,
            "tasks_with_new_round2_test": len(changed),
            "paired_new_round2_test": stats.paired(ca, cb) if ca else None,
        },
    }


def manifest_row(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": r["task_id"], "is_weak": r.get("is_weak"), "status": r["status"], "reason": r["reason"],
        "accepted_test": r["accepted_test"],
        "functions": {"asked": r["accept"]["asked"], "kept": r["accept"]["kept"], "repair_used": r["accept"]["repair_used"]},
        "gates": r.get("gates"), "counts": r.get("counts"), "round2_measured": r["round2_measured"],
        "shipped_round": r.get("shipped_round"), "stop_reason": r.get("stop_reason"),
        "a2_survivors_exercised": len([s for s in r.get("a2_survivors") or [] if s.get("killed") is not None]),
        "a2_survivors_killed": sum(1 for s in r.get("a2_survivors") or [] if s.get("killed")),
        "upgraded_package_sha256": r.get("upgraded_package_sha256"),
    }


def publish(rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    manifest_path = GATES / "gold-v1-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    by_id = {r["task_id"]: r for r in rows}
    for t in manifest["tasks"]:
        r = by_id.get(t["task_id"])
        t["a4b"] = manifest_row(r) if r else {"status": "not_run", "reason": "no_a4b_record"}
    manifest["a4b_summary"] = summary
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    certified = [
        {"task_id": r["task_id"], "status": r["status"], "reason": r["reason"],
         "gold_structural": r["status"] in GOLD, "gold_heldout": r["status"] == "gold_heldout",
         "functions_kept": r["accept"]["kept"], "functions_asked": r["accept"]["asked"],
         "shipped_round": r["shipped_round"], "package_dir": r["package_dir"], "test_file": TEST_NAME,
         "upgraded_package_sha256": r["upgraded_package_sha256"]}
        for r in rows if r["accepted_test"]
    ]
    out = {
        "schema_version": "gold-v1.1/certified/v1", "source": "A4B-FILTERED-GOLD (reference-filtered synthesis; exploratory)",
        "definition": "tasks_with_accepted_test: test file keeps >=3 reference-passing functions and scores 1.0 on the "
                      "reference. gold_structural/gold_heldout flags as A4 defined them. Tests pass the reference only; "
                      "they may encode reference-specific behavior (measured downstream by A8B).",
        "n_accepted_test": len(certified), "n_gold_structural_or_better": sum(1 for c in certified if c["gold_structural"]),
        "n_gold_heldout": sum(1 for c in certified if c["gold_heldout"]),
        "tasks": certified,
    }
    (GATES / "gold-v1.1-certified.json").write_text(json.dumps(out, indent=2) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a4b", type=Path, default=A4B)
    ap.add_argument("--publish", action="store_true")
    args = ap.parse_args()
    rows = collect(args.a4b)
    summary = summarize(rows)
    (HERE / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (HERE / "a4b-manifest.json").write_text(json.dumps([manifest_row(r) for r in rows], indent=2) + "\n")
    if args.publish:
        publish(rows, summary)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
