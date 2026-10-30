#!/usr/bin/env python3
"""SK-7 stats: parse rate, witness agreement (S1/S2), paired McNemar R1CS vs R1C, by half."""
from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path

SQUAD = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-10-29-ds-squad")
sys.path.insert(0, str(SQUAD))
sys.path.insert(0, str(SQUAD / "raw" / "SK-1" / "receipts"))

from classifier import CLASSES, UNCLASSIFIED, classify  # noqa: E402
from stats import mcnemar_exact, tertile_of, tertiles, wilson  # noqa: E402

BASE = SQUAD / "raw"
OUT = BASE / "SK-7" / "receipts"
ARMS = ["R1C", "R1CS"]
HOOK = {"framework_config_injection", "runner_exit_status_forgery"}


def pct(k, n):
    if not n:
        return {"k": k, "n": 0, "share": None, "wilson95": None}
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "share": round(k / n, 4), "wilson95": [round(lo, 4), round(hi, 4)]}


def bare(tid):
    return tid.split("/")[-1]


def witnesses():
    d = json.loads((BASE / "SK-1" / "receipts" / "mechanism-table.json").read_text())
    out = {}
    for r in d["rows"]:
        out.setdefault(bare(r["task_id"]), set()).update(r["classes"])
    return out


def load():
    rows = [json.loads(line) for line in (OUT / "attempts.jsonl").read_text().splitlines() if line.strip()]
    keep = {}
    for r in rows:  # one record per (task, arm, sample): prefer attested, then later attempt
        key = (bare(r["task_id"]), r["arm"], r["sample"])
        old = keep.get(key)
        if old is None or (r.get("attested") and not old.get("attested")) or \
                (old.get("attested") == r.get("attested") and r.get("attempt", 1) > old.get("attempt", 1)):
            keep[key] = r
    rows = list(keep.values())
    for r in rows:
        r["_bare"] = bare(r["task_id"])
        r["_text"] = (r.get("raw_response") or "").strip()
        r["_classes"] = ([c for c in classify([r["_text"]]) if c != UNCLASSIFIED]
                         if (r.get("attested") and r["_text"]) else None)
    return rows


def main() -> int:
    rows = load()
    tasks = [json.loads(line) for line in (BASE / "SK-2" / "receipts" / "tasks.jsonl").read_text().splitlines() if line.strip()]
    cohort = [t["task_id"] for t in tasks]
    W = witnesses()
    witnessed = [t for t in cohort if t in W]
    halves = {h: [t for t in cohort if half_of(t) == h] for h in ("heldout", "design")}
    res = {"prereg": "prereg.md", "attempts_file": "attempts.jsonl", "cohort_n": len(cohort),
           "witnessed_n": len(witnessed), "witnessed": witnessed,
           "halves": {h: len(v) for h, v in halves.items()}, "arms": {}}

    for arm in ARMS:
        a = [r for r in rows if r["arm"] == arm]
        att = [r for r in a if r["attested"]]
        ent = {
            "attempts": len(a), "attested": len(att), "unattested": len(a) - len(att),
            "tasks_attempted": len({r["_bare"] for r in a}),
            "tasks_attested": len({r["_bare"] for r in att}),
            "per_sample": {str(s): {"attempts": sum(1 for r in a if r["sample"] == s),
                                    "attested": sum(1 for r in att if r["sample"] == s)}
                           for s in sorted({r["sample"] for r in a})},
            "answer_status_attested": {s: sum(1 for r in att if r["answer_status"] == s)
                                       for s in ("ok", "empty", "malformed")},
            "parse_rate_attested": pct(sum(1 for r in att if r["answer_status"] == "ok"), len(att)),
            "parse_rate_by_half": {h: pct(sum(1 for r in att if r["answer_status"] == "ok" and r["half"] == h),
                                          sum(1 for r in att if r["half"] == h)) for h in halves},
            "files_ge_2": sum(1 for r in att if len(r.get("proposed_files") or []) >= 2),
            "unterminated_blocks": sum(1 for r in att if r.get("unterminated_blocks")),
        }
        toks = [r["output_tokens"] for r in att if isinstance(r.get("output_tokens"), (int, float))]
        ent["input_tokens_mean"] = round(st.mean([r["input_tokens"] for r in att
                                                  if isinstance(r.get("input_tokens"), (int, float))]), 1) if att else None
        ent["output_tokens_mean"] = round(st.mean(toks), 1) if toks else None
        ent["wall_seconds_mean"] = round(st.mean([r["wall_seconds"] for r in att]), 1) if att else None
        ent["dollars_total"] = round(sum(r.get("dollars") or 0 for r in a), 6)
        ent["dollars_mean"] = round(ent["dollars_total"] / len(a), 6) if a else None
        ent["tokens_total"] = {"input": sum(r.get("input_tokens") or 0 for r in a),
                               "output": sum(r.get("output_tokens") or 0 for r in a),
                               "cache_read": sum(r.get("cache_read_tokens") or 0 for r in a)}
        ent["mentions_missing_tools"] = sum(
            1 for r in att if r["_text"] and any(s in r["_text"].lower() for s in
                                                 ("manage_skill", "no shell", "no bash", "cannot inspect", "cannot run")))

        def task_hit(rs, t, stat):
            sel = [r for r in rs if r["_bare"] == t and r["_classes"] is not None]
            if stat == "S1":
                return any(set(r["_classes"] or []) & W.get(t, set()) for r in sel)
            if stat == "S2":
                return any(r["_classes"] for r in sel)
            return any(stat in (r["_classes"] or []) for r in sel)

        def measure(group, rs, stat):
            present = [t for t in group if any(r["_bare"] == t and r["_classes"] is not None for r in rs)]
            hits = [t for t in present if task_hit(rs, t, stat)]
            return {"measured": len(present), **pct(len(hits), len(present)), "hit_tasks": hits,
                    "over_all_tasks_in_group": pct(len(hits), len(group))}

        ent["S1_witness_match"] = {"all_witnessed": measure(witnessed, att, "S1"),
                                   "heldout": measure([t for t in witnessed if half_of(t) == "heldout"], att, "S1"),
                                   "design": measure([t for t in witnessed if half_of(t) == "design"], att, "S1")}
        ent["S2_any_class"] = {"witnessed_tasks": measure(witnessed, att, "S2"),
                               "all_44": measure(cohort, att, "S2"),
                               "heldout_25": measure(halves["heldout"], att, "S2"),
                               "design_19": measure(halves["design"], att, "S2")}
        ent["per_class_all_tasks"] = {c: measure(cohort, att, c) for c in CLASSES}
        ent["per_class_all_tasks_by_half"] = {
            c: {h: measure(halves[h], att, c) for h in halves} for c in CLASSES}
        tl = [r for r in att if r["_classes"] is not None]
        toks2 = [r["output_tokens"] or 0 for r in tl]
        if toks2:
            cuts = tertiles(toks2)
            buckets = {}
            for r in tl:
                b = tertile_of(r["output_tokens"] or 0, cuts)
                e = buckets.setdefault(b, {"n": 0, "k": 0})
                e["n"] += 1
                if set(r["_classes"] or []) & W.get(r["_bare"], set()):
                    e["k"] += 1
            ent["length_control"] = {"cuts_output_tokens": [round(c, 1) for c in cuts],
                                     "witness_class_by_tertile": {f"t{b}": {"n": v["n"], **pct(v["k"], v["n"])}
                                                                  for b, v in sorted(buckets.items())}}
        res["arms"][arm] = ent

    def paired(stat, group=None):
        A = [r for r in rows if r["arm"] == "R1C" and r["attested"] and r["_classes"] is not None]
        B = [r for r in rows if r["arm"] == "R1CS" and r["attested"] and r["_classes"] is not None]
        common_tasks = sorted({r["_bare"] for r in A} & {r["_bare"] for r in B})
        if group is not None:
            common_tasks = [t for t in common_tasks if t in group]

        def hit(rows_x, t):
            rs = [r for r in rows_x if r["_bare"] == t]
            if stat == "S1":
                return any(set(r["_classes"] or []) & W.get(t, set()) for r in rs)
            return any(r["_classes"] for r in rs)

        b = sum(1 for t in common_tasks if hit(A, t) and not hit(B, t))
        c = sum(1 for t in common_tasks if hit(B, t) and not hit(A, t))
        return {"statistic": stat, "group": len(common_tasks), "paired_tasks": len(common_tasks),
                "R1C_only": b, "R1CS_only": c,
                "R1C_hits": pct(sum(1 for t in common_tasks if hit(A, t)), len(common_tasks)),
                "R1CS_hits": pct(sum(1 for t in common_tasks if hit(B, t)), len(common_tasks)),
                "mcnemar_exact_p": round(mcnemar_exact(b, c), 4) if (b + c) else None}

    res["paired"] = [
        {"scope": "all_witnessed", **paired("S1", set(witnessed))},
        {"scope": "heldout_witnessed", **paired("S1", {t for t in witnessed if half_of(t) == "heldout"})},
        {"scope": "design_witnessed", **paired("S1", {t for t in witnessed if half_of(t) == "design"})},
        {"scope": "all_44", **paired("S2", set(cohort))},
        {"scope": "heldout_25", **paired("S2", set(halves["heldout"]))},
        {"scope": "design_19", **paired("S2", set(halves["design"]))},
    ]

    # non-hook proposals under R1CS, with scanner fields
    scans = {s["task_id"]: s for s in json.loads((BASE / "TQ-1" / "receipts" / "scan-all.json").read_text())}
    nonhook = {}
    for r in rows:
        if r["arm"] != "R1CS" or not r["attested"] or not r["_classes"]:
            continue
        for c in r["_classes"]:
            if c in HOOK:
                continue
            sc = scans[r["task_id"]]
            e = nonhook.setdefault(r["_bare"], {
                "task_id": r["task_id"], "half": r["half"], "classes": {},
                "witness_classes": sorted(W.get(r["_bare"], [])),
                "scanner_flags": sorted(k for k, v in sc["class_flags"].items() if v),
                "scanner_features": {k: (len(v) if isinstance(v, list) else v)
                                     for k, v in sc["features"].items() if v != [] and v is not False},
                "samples": []})
            e["classes"][c] = e["classes"].get(c, 0) + 1
            e["samples"].append({"sample": r["sample"], "answer_status": r["answer_status"],
                                 "files": r.get("proposed_files")})
    res["nonhook_proposals_R1CS"] = {"n_tasks": len(nonhook),
                                     "heldout": sum(1 for e in nonhook.values() if e["half"] == "heldout"),
                                     "design": sum(1 for e in nonhook.values() if e["half"] == "design"),
                                     "tasks": sorted(nonhook.values(), key=lambda e: (-len(e["classes"]), e["task_id"]))}

    res["spend"] = {"attempts": len(rows), "dollars_total": round(sum(r.get("dollars") or 0 for r in rows), 4),
                    "by_arm": {a: round(sum(r.get("dollars") or 0 for r in rows if r["arm"] == a), 4) for a in ARMS}}
    (OUT / "stats.json").write_text(json.dumps(res, indent=1) + "\n")
    for a in ARMS:
        e = res["arms"][a]
        print(a, "att", e["attested"], "parse", e["parse_rate_attested"]["share"],
              "S1", e["S1_witness_match"]["all_witnessed"]["k"], "/", e["S1_witness_match"]["all_witnessed"]["n"],
              "S2", e["S2_any_class"]["all_44"]["k"], "/", e["S2_any_class"]["all_44"]["n"],
              "heldout S1", e["S1_witness_match"]["heldout"]["k"], "/", e["S1_witness_match"]["heldout"]["n"],
              "design S1", e["S1_witness_match"]["design"]["k"], "/", e["S1_witness_match"]["design"]["n"])
    for p in res["paired"]:
        print(p["scope"], p["statistic"], p["paired_tasks"], "R1C_only", p["R1C_only"], "R1CS_only", p["R1CS_only"], "p", p["mcnemar_exact_p"])
    print("nonhook tasks", res["nonhook_proposals_R1CS"]["n_tasks"], "spend", res["spend"]["dollars_total"])
    return 0


_HALF_CACHE: dict[str, str] = {}


def half_of(tid: str) -> str:
    if not _HALF_CACHE:
        for s in json.loads((BASE / "TQ-1" / "receipts" / "scan-all.json").read_text()):
            _HALF_CACHE[s["task_id"]] = s["half"]
    return _HALF_CACHE[bare(tid)]


if __name__ == "__main__":
    raise SystemExit(main())
