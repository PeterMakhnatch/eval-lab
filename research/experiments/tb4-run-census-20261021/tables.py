"""Corrected per-model/effort tables and the DEV-only labelled-trajectory export.

All tables share ONE explicit denominator: the 22 DEV tasks x 5 attempts of a board row (110
memberships), restricted to a provenance stratum (``canonical`` primary; ``quarantine`` supplementary).
Every membership falls in exactly one bucket; ``check_conservation`` fails closed otherwise.

Scenarios (all over the SAME scored denominator n = passes + fails of the row in the DEV tasks):
  raw                      k / n
  exploit_removed          (k - known_exploit) / n                 known-exploit passes removed
  exploit_candidates_out   (k - known_exploit - candidates) / n    candidate-inclusive lower scenario
  defect_credit_UB         (k - known_exploit + task_defect) / n   UPPER BOUND, not regraded passes
  defect_credit_UB_gray    ... + task_defect_gray                  loose upper bound
Unmeasured / unknown memberships are NEVER counted as clean or as defects: their counts and the resulting
coverage are printed next to every rate. Corrections outside the DEV tasks are zero/unknown, not
measured; nothing here extrapolates to the other 44 tasks or to a whole-board score.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any

import cheap_judge
import index as idx
from census_core import (
    DEV_TASKS,
    EXPLOIT_CANDIDATE,
    HERE,
    JUDGE_DIR,
    KNOWN_EXPLOIT,
    SYSTEM,
    TASK_DEFECT,
    TASK_DEFECT_GRAY,
    fmt,
    out_dir,
    rate,
    read_jsonl,
    sha256_file,
    write_jsonl,
)

PM_MODELS = {"Fable 5.1", "Opus 5.5", "Sonnet 5.5", "GPT-6 Astra", "GPT-6.1 Sol"}


def row_name(meta: dict[str, Any]) -> str:
    return f"{meta.get('agent', '?')} · {meta['model']} · {meta['effort']}"


def load_labels(root) -> list[dict[str, Any]]:
    labels = read_jsonl(root / "labels-all.jsonl")
    bad = sorted({r["task"] for r in labels if r["task"] not in DEV_TASKS or r.get("split") != "dev"})
    if bad:
        raise SystemExit(f"non-DEV tasks in labels: {bad}")
    return labels


def bucket(r: dict[str, Any]) -> str:
    """Single partition of memberships used by every table."""
    if r["side"] == "unscored":
        return "unscored"
    side, lab = r["side"], r["label"]
    return f"{side}_{lab}"  # neither | no_documented_mechanism (nothing documented/operationalized)


def summarize(labels: list[dict[str, Any]]) -> dict[str, Any]:
    c = Counter(bucket(r) for r in labels)
    k = sum(v for b, v in c.items() if b.startswith("pass_"))
    f = sum(v for b, v in c.items() if b.startswith("fail_"))
    n = k + f
    e, cand = c["pass_known_exploit"], c["pass_exploit_candidate"]
    d, g = c["fail_task_defect"], c["fail_task_defect_gray"]
    out = {
        "memberships": len(labels), "scored": n, "unscored": c["unscored"], "passes": k, "fails": f,
        "pass_buckets": {b[5:]: v for b, v in c.items() if b.startswith("pass_")},
        "fail_buckets": {b[5:]: v for b, v in c.items() if b.startswith("fail_")},
        "pass_measured": k - c["pass_unmeasured"], "fail_measured": f - c["fail_unmeasured"],
        "raw": rate(k, n), "exploit_removed": rate(k - e, n),
        "exploit_candidates_out": rate(k - e - cand, n),
        "defect_credit_UB": rate(k - e + d, n), "defect_credit_UB_gray": rate(k - e + d + g, n),
        "exploit_share_of_passes": rate(e, k), "defect_share_of_fails": rate(d, f),
    }
    if sum(v for b, v in c.items()) != out["memberships"] or k + f + c["unscored"] != len(labels):
        raise SystemExit("bucket partition does not conserve memberships")
    return out


def rank(values: dict[str, float]) -> dict[str, int]:
    return {key: 1 + sum(1 for o in values.values() if o > v) for key, v in values.items()}


def build(labels: list[dict[str, Any]], stratum: str) -> dict[str, Any]:
    meta = idx.board_rows()
    part = [r for r in labels if r["provenance"]["stratum"] == stratum]
    by_row: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in part:
        by_row[r["row_id"]].append(r)
        by_task[r["task"]].append(r)
    rows = []
    for rid, items in by_row.items():
        s = summarize(items)
        head = items[0]
        m = {"model": head["model"], "effort": head["effort"], **{k: meta.get(rid, {}).get(k) for k in ("agent",)}}
        rows.append({"row_id": rid, "row": row_name(m), "board_rank": head["board_rank"],
                     "official_overall_accuracy_context_only": meta.get(rid, {}).get("accuracy"),
                     "pm_model_max": head["model"] in PM_MODELS and head["effort"] == "max", **s})
    for key in ("raw", "exploit_removed", "defect_credit_UB"):
        ranks = rank({r["row_id"]: (r[key]["rate"] or 0.0) for r in rows})
        for r in rows:
            r[f"rank_{key}"] = ranks[r["row_id"]]
    rows.sort(key=lambda r: (r["rank_raw"], r["row"]))
    tasks = {t: summarize(v) for t, v in sorted(by_task.items())}
    return {"stratum": stratum, "memberships": len(part), "overall": summarize(part) if part else None,
            "pm_max_cohort": summarize([r for r in part if r["model"] in PM_MODELS and r["effort"] == "max"])
            if part else None, "rows": rows, "tasks": tasks,
            "by_mechanism": mechanism_counts(part)}


def mechanism_counts(part: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    out: dict[str, Counter] = defaultdict(Counter)
    for r in part:
        if r["label"] in {KNOWN_EXPLOIT, EXPLOIT_CANDIDATE, TASK_DEFECT, TASK_DEFECT_GRAY}:
            for m in r["mechanisms"]:
                out[m][r["label"]] += 1
    return {m: dict(c) for m, c in sorted(out.items())}


def provenance_coverage(labels: list[dict[str, Any]]) -> dict[str, Any]:
    by_row: dict[str, Counter] = defaultdict(Counter)
    names: dict[str, str] = {}
    for r in labels:
        by_row[r["row_id"]][r["provenance"]["stratum"]] += 1
        names[r["row_id"]] = f"{r['model']} · {r['effort']}"
    return {"rows": {names[k]: dict(v) for k, v in sorted(by_row.items(), key=lambda kv: names[kv[0]])},
            "overall": dict(Counter(r["provenance"]["stratum"] for r in labels)),
            "acquisition_states": dict(Counter(r["provenance"]["acquisition_state"] for r in labels))}


def check_conservation(result: dict[str, Any]) -> None:
    for r in result["rows"]:
        if sum(r["pass_buckets"].values()) != r["passes"] or sum(r["fail_buckets"].values()) != r["fails"]:
            raise SystemExit(f"row {r['row']}: bucket sums differ from passes/fails")
        if r["passes"] + r["fails"] + r["unscored"] != r["memberships"]:
            raise SystemExit(f"row {r['row']}: memberships not conserved")
    total = sum(t["memberships"] for t in result["tasks"].values())
    if total != result["memberships"] or sum(r["memberships"] for r in result["rows"]) != total:
        raise SystemExit("task/row denominators disagree")


def render(canon: dict[str, Any], supp: dict[str, Any] | None, coverage: dict[str, Any],
           calib: dict[str, Any] | None) -> str:
    o = canon["overall"]
    out = [f"# TB4 DEV run census ({SYSTEM}): corrected per-model/effort tables", "",
           "Generated by `run_census.py tables`. **Scope: the 22 DEV tasks of `tb4-audit-v1` only** (one "
           "denominator for every table: DEV tasks x 5 attempts per board row, canonical-provenance stratum). "
           "Wilson 95% intervals. Not a rate for the 66-task benchmark; corrections outside DEV are "
           "zero/unknown, never measured. `neither` = no documented mechanism found, not proven clean. "
           "Defect credit is an **upper bound, not regraded passes**. `known_exploit` means deterministic "
           "signature/mechanism evidence in the submission or actions; no differential original-passes/fixed-"
           "fails replay was run, so nothing here is replay-confirmed. `exploit_candidate` is a flagged wrong-"
           "pass signature or a verified-quote judge yes, not proven. Rank lists cover only rows with a "
           "canonical DEV stratum; rows that are fully quarantined/incomplete appear only in the provenance "
           "coverage table.", ""]
    if o:
        out += [
            f"Canonical stratum: {canon['memberships']} memberships; {o['passes']} passes, {o['fails']} fails, "
            f"{o['unscored']} unscored.", "",
            "| scope | exploit passes / passes | defect fails / fails | exploit candidates (not counted) |",
            "|---|---|---|---|",
            f"| all canonical DEV rows | {fmt(o['exploit_share_of_passes'])} | {fmt(o['defect_share_of_fails'])} | "
            f"{o['pass_buckets'].get('exploit_candidate', 0)} |"]
        p = canon["pm_max_cohort"]
        if p and p["memberships"]:
            out.append(f"| PM's 5 models, max effort | {fmt(p['exploit_share_of_passes'])} | "
                       f"{fmt(p['defect_share_of_fails'])} | {p['pass_buckets'].get('exploit_candidate', 0)} |")
        out += ["", f"Measured coverage: passes {o['pass_measured']}/{o['passes']} "
                f"(unmeasured {o['pass_buckets'].get('unmeasured', 0)}), fails {o['fail_measured']}/{o['fails']} "
                f"(unmeasured {o['fail_buckets'].get('unmeasured', 0)}); gray defects "
                f"{o['fail_buckets'].get('task_defect_gray', 0)}.", ""]
    out += ["## Corrected DEV-only pass rates per board row", "",
            "Ranks: raw -> exploit removed -> defect credit (upper bound). Rates share the row's scored "
            "DEV denominator n. `unm P/F` = unmeasured passes/fails (excluded from every numerator).", "",
            "| rank | row | n | raw | exploits removed | candidate-inclusive lower | defect-credit UB (not regraded) | "
            "+gray UB | known exploits | candidates | defects | gray | unm P/F | official overall (context) |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in canon["rows"]:
        out.append(
            f"| {r['rank_raw']}→{r['rank_exploit_removed']}→{r['rank_defect_credit_UB']} | {r['row']} | "
            f"{r['scored']} | {fmt(r['raw'])} | {fmt(r['exploit_removed'])} | {fmt(r['exploit_candidates_out'])} | "
            f"{fmt(r['defect_credit_UB'])} | {fmt(r['defect_credit_UB_gray'])} | "
            f"{r['pass_buckets'].get('known_exploit', 0)} | {r['pass_buckets'].get('exploit_candidate', 0)} | "
            f"{r['fail_buckets'].get('task_defect', 0)} | {r['fail_buckets'].get('task_defect_gray', 0)} | "
            f"{r['pass_buckets'].get('unmeasured', 0)}/{r['fail_buckets'].get('unmeasured', 0)} | "
            f"{r['official_overall_accuracy_context_only'] if r['official_overall_accuracy_context_only'] is not None else 'n/a'} |")
    out += ["", "## Per DEV task (canonical stratum)", "",
            "| task | memberships | passes: exploit / candidate / unmeasured | fails: defect / gray / unmeasured |",
            "|---|---|---|---|"]
    for t, s in canon["tasks"].items():
        pb, fb = s["pass_buckets"], s["fail_buckets"]
        out.append(f"| {t} | {s['memberships']} | {pb.get('known_exploit', 0)} / {pb.get('exploit_candidate', 0)} / "
                   f"{pb.get('unmeasured', 0)} of {s['passes']} | {fb.get('task_defect', 0)} / "
                   f"{fb.get('task_defect_gray', 0)} / {fb.get('unmeasured', 0)} of {s['fails']} |")
    out += ["", "## Labelled mechanisms (canonical stratum)", "", "| mechanism | labels |", "|---|---|"]
    for m, c in canon["by_mechanism"].items():
        out.append(f"| {m} | {', '.join(f'{k} {v}' for k, v in sorted(c.items()))} |")
    out += ["", "## Provenance coverage (all DEV memberships, so an absent source cannot look clean)", "",
            "| row | memberships by stratum |", "|---|---|"]
    for name, c in coverage["rows"].items():
        out.append(f"| {name} | {', '.join(f'{k} {v}' for k, v in sorted(c.items()))} |")
    out += ["", f"Overall strata: {coverage['overall']}; acquisition states: {coverage['acquisition_states']}.", ""]
    if supp and supp["memberships"]:
        s = supp["overall"]
        out += [f"Supplementary quarantine stratum ({supp['memberships']} memberships): retained public archives "
                f"from different-source or drifted locks, NOT canonical and never mixed above. Mechanism "
                f"signatures are not qualified for these sources, so none is classified: passes "
                f"{s['passes']}, fails {s['fails']}, all `unmeasured`.", ""]
    if calib:
        out += ["## Calibration", "", calib.get("summary_md", ""), ""]
    return "\n".join(out) + "\n"


def cmd_tables(args) -> None:
    root = out_dir(args.bootstrap)
    labels = load_labels(root)
    canon = build(labels, "canonical")
    check_conservation(canon)
    supp = build(labels, "quarantine")
    if supp["memberships"]:
        check_conservation(supp)
    coverage = provenance_coverage(labels)
    calib_path = HERE / "calibration.json"
    calib = json.loads(calib_path.read_text()) if calib_path.exists() else None
    result = {"system": SYSTEM, "canonical": canon, "quarantine": supp, "coverage": coverage}
    (HERE / "census.json").write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    (HERE / "CENSUS.md").write_text(render(canon, supp, coverage, calib))
    print((HERE / "CENSUS.md").read_text()[:3500])


EXPORT_FIELDS = ("system", "membership_id", "task", "split", "row_id", "board_rank", "model", "effort",
                 "hub_trial_id", "archive_trial_id", "job_id", "attempt", "reward", "side", "label",
                 "mechanisms", "mechanisms_checked", "mechanisms_unchecked", "method", "judge_route",
                 "prompt_version", "evidence", "confidence", "facts", "catalog_source", "provenance")


def cmd_export(args) -> None:
    root = out_dir(args.bootstrap)
    labels = load_labels(root)
    out = []
    for r in labels:
        if r["task"] not in DEV_TASKS or r["split"] != "dev":
            raise SystemExit(f"fail-closed: {r['task']} is not DEV")
        prov = r["provenance"]
        row = {k: r[k] for k in EXPORT_FIELDS}
        row["group_id"] = r["task"]  # no-leakage splits group by task, then by producer
        row["producer_id"] = r["row_id"]
        row["source_identity"] = {"admission": prov["admission"], "native_lock_stratum_1120": prov["native_lock_stratum_1120"],
                                  "archive_tree_sha256": prov["archive_tree_sha256"]}
        out.append(row)
    out.sort(key=lambda r: r["membership_id"])
    write_jsonl(HERE / "labels-dev.jsonl", out)
    tasks = Counter(r["task"] for r in out)
    ledger = cheap_judge.spend(JUDGE_DIR / "judge-ledger.jsonl")
    manifest = {
        "system": SYSTEM, "split_key": "tb4-audit-v1", "records": len(out), "tasks": dict(sorted(tasks.items())),
        "assertions": {"every_task_is_dev": all(t in DEV_TASKS for t in tasks),
                       "dev_tasks_covered": sorted(DEV_TASKS - set(tasks)) == [],
                       "test_or_unassigned_tasks": sorted(set(tasks) - DEV_TASKS)},
        "labels": dict(Counter(r["label"] for r in out)),
        "strata": dict(Counter(r["provenance"]["stratum"] for r in out)),
        "labels_sha256": sha256_file(HERE / "labels-dev.jsonl"),
        "freeze_sha256": sha256_file(HERE / "FREEZE.json") if (HERE / "FREEZE.json").exists() else None,
        "index": idx.index_receipt(), "judge_spend": ledger,
        "raw_trajectories": "external, never in git: ~/Developer/eval-lab-results/tb4-hub (see archive_tree_sha256)",
        "schema": {"fields": list(EXPORT_FIELDS) + ["group_id", "producer_id", "source_identity"],
                   "unknown_is_not_clean": "label unmeasured/unscored/no_documented_mechanism never means clean"},
    }
    if not manifest["assertions"]["every_task_is_dev"] or not manifest["assertions"]["dev_tasks_covered"]:
        raise SystemExit(f"export assertions failed: {manifest['assertions']}")
    (HERE / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    print(json.dumps({k: manifest[k] for k in ("records", "labels", "strata", "assertions")}, indent=1))


def cmd_calibrate(args) -> None:
    import calibrate

    calibrate.run(args)
