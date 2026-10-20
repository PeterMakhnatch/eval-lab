"""Join blind audit verdicts, refutation verdicts and run-level support into audit-results.json.

A claim is *supported* when the first auditor called it a defect and the independent
refutation pass upheld it. Run-level support counts, for the test the auditor named, the
failed leaderboard trials that contain it and those that fail only on it.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import run  # sibling module

HERE = Path(__file__).resolve().parent
AUDIT = run.RESULTS / "audit"
DEFECT = ("unstated_rule", "other_defect")


def read(path: Path) -> list[dict[str, Any]]:
    return run.jsonl(path) if path.is_file() else []


def support(task: str, failing_test: str | None, trials: list[dict[str, Any]]) -> dict[str, Any]:
    short = (failing_test or "").split("::")[-1].split(".")[-1].split("(")[0].strip()
    fails = [t for t in trials if t["task"] == task and t["reward"] == 0 and t["failed"]]
    match = {n for t in fails for n in t["failed"] if short and short in n}
    contain = [t for t in fails if match & set(t["failed"])]
    sole = [t for t in fails if set(t["failed"]) <= match]
    scored = [t for t in trials if t["task"] == task and t["reward"] is not None]
    return {
        "named_test": short or None, "matched_tests": len(match), "scored_trials": len(scored),
        "fails_with_evidence": len(fails), "fails_containing": len(contain), "fails_only_on_it": len(sole),
        "rows_only_on_it": len({t["row_id"] for t in sole}),
        "passes": sum(t["reward"] >= 1 for t in scored),
    }


def main() -> None:
    manifest = {m["aid"]: m for m in json.loads((AUDIT / "private-manifest.json").read_text())}
    verdicts = {r["aid"]: r for b in range(run_batches()) for r in read(AUDIT / f"verdicts-batch-{b}.jsonl")}
    refutes = {r["aid"]: r for name in ("a", "b") for r in read(AUDIT / f"refute-{name}.jsonl")}
    inventory = json.loads((run.RESULTS / "inventory-all.json").read_text())
    trials = run.load_trials(inventory)
    rows = []
    for aid, m in manifest.items():
        v, r = verdicts[aid], refutes.get(aid)
        claimed = v["verdict"] in DEFECT
        supported = claimed and r is not None and r["verdict"] == "upheld"
        rows.append({
            "task": m["task"], "role": m["role"], "audit_verdict": v["verdict"], "audit_confidence": v["confidence"],
            "refutation": r["verdict"] if r else None, "supported": supported,
            "failing_test": v["failing_test"], "rationale": v["rationale"],
            "refutation_reasoning": r["reasoning"] if r else None,
            "run_level": support(m["task"], v["failing_test"], trials) if claimed else None,
        })
    rows.sort(key=lambda x: (x["role"], x["audit_verdict"], x["task"]))
    cand = [x for x in rows if x["role"] == "candidate"]
    ctrl = [x for x in rows if x["role"] != "candidate"]

    def count(part: list[dict[str, Any]], pred: Any) -> dict[str, Any]:
        return run.rate(sum(1 for x in part if pred(x)), len(part))

    summary = {
        "candidates_pm_lists_no_ambiguity": len(cand), "controls_pm_lists_ambiguity": len(ctrl),
        "candidates": {
            "auditor_defect": count(cand, lambda x: x["audit_verdict"] in DEFECT),
            "auditor_unstated_rule": count(cand, lambda x: x["audit_verdict"] == "unstated_rule"),
            "supported_defect": count(cand, lambda x: x["supported"]),
            "refuted_after_claim": count(cand, lambda x: x["refutation"] == "refuted"),
            "partly_after_claim": count(cand, lambda x: x["refutation"] == "partly"),
        },
        "controls": {
            "auditor_defect": count(ctrl, lambda x: x["audit_verdict"] in DEFECT),
            "supported_defect": count(ctrl, lambda x: x["supported"]),
            "refuted_after_claim": count(ctrl, lambda x: x["refutation"] == "refuted"),
            "partly_after_claim": count(ctrl, lambda x: x["refutation"] == "partly"),
        },
        "verdict_counts": {role: dict(Counter(x["audit_verdict"] for x in part))
                           for role, part in (("candidate", cand), ("control", ctrl))},
        "refutation_counts": dict(Counter(x["refutation"] for x in rows if x["refutation"])),
    }
    extra = [x for x in cand if x["supported"]]
    summary["supported_candidates_fails_only_on_named_test"] = {
        "tasks": [x["task"] for x in extra],
        "fails": sum(x["run_level"]["fails_only_on_it"] for x in extra),
        "of_scored_trials": sum(x["run_level"]["scored_trials"] for x in extra),
        "of_fails_with_evidence": sum(x["run_level"]["fails_with_evidence"] for x in extra),
    }
    again = {r["aid"]: r for name in (0, 1) for r in read(AUDIT / f"verdicts-rerun-{name}.jsonl")}
    if again:
        summary["second_pass"] = second_pass(rows, manifest, again)
        by_aid = {m["task"]: aid for aid, m in manifest.items()}
        for x in rows:
            x["robust"] = x["supported"] and again[by_aid[x["task"]]]["verdict"] in DEFECT
        cand_robust = [x["task"] for x in rows if x["role"] == "candidate" and x["robust"]]
        summary["robust_candidates"] = {"rate": run.rate(len(cand_robust), len(cand)), "tasks": cand_robust,
                                        "definition": "defect in pass 1, upheld by refutation, and defect in pass 2"}
    (HERE / "audit-results.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1, sort_keys=True) + "\n")
    # compact verdicts without packet text, for the repository
    with (HERE / "audit-verdicts.jsonl").open("w") as fh:
        for x in rows:
            fh.write(json.dumps({k: x.get(k) for k in ("task", "role", "audit_verdict", "audit_confidence", "refutation",
                                                  "supported", "robust", "failing_test", "run_level")}, sort_keys=True) + "\n")
    (HERE / "AUDIT.md").write_text(render(summary, rows))
    print(render(summary, rows))


def kappa(pairs: list[tuple[bool, bool]]) -> float | None:
    n = len(pairs)
    if not n:
        return None
    observed = sum(a == b for a, b in pairs) / n
    pa, pb = sum(a for a, _ in pairs) / n, sum(b for _, b in pairs) / n
    chance = pa * pb + (1 - pa) * (1 - pb)
    return None if chance == 1 else (observed - chance) / (1 - chance)


def second_pass(rows: list[dict[str, Any]], manifest: dict[str, Any], again: dict[str, Any]) -> dict[str, Any]:
    """Agreement between the two independent audit passes on defect vs not-defect."""
    by_task = {x["task"]: x for x in rows}
    pairs, per_role = [], defaultdict(list)
    for aid, m in manifest.items():
        first = by_task[m["task"]]["audit_verdict"] in DEFECT
        second = again[aid]["verdict"] in DEFECT
        pairs.append((first, second))
        per_role[m["role"]].append((first, second))
    out: dict[str, Any] = {
        "n": len(pairs), "agree": sum(a == b for a, b in pairs), "kappa": kappa(pairs),
        "defect_in_both": sum(a and b for a, b in pairs), "defect_in_either": sum(a or b for a, b in pairs),
        "second_pass_verdicts": dict(Counter(again[a]["verdict"] for a in manifest)),
        "per_role": {role: {"n": len(p), "first_defect": sum(a for a, _ in p), "second_defect": sum(b for _, b in p),
                            "both": sum(a and b for a, b in p)} for role, p in per_role.items()},
        "defect_in_both_tasks": sorted(m["task"] for aid, m in manifest.items()
                                       if by_task[m["task"]]["audit_verdict"] in DEFECT and again[aid]["verdict"] in DEFECT),
    }
    return out


def run_batches() -> int:
    return len(list(AUDIT.glob("verdicts-batch-*.jsonl")))


def render(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    c, k = summary["candidates"], summary["controls"]
    f = run.fmt
    out = ["# Blind audit of PM-unlisted TB4 tasks: tables", "",
           "| group | auditor calls a defect | upheld by refutation pass (supported) | partly | refuted |", "|---|---|---|---|---|",
           f"| 20 tasks PM lists no ambiguity for | {f(c['auditor_defect'])} | {f(c['supported_defect'])} | {f(c['partly_after_claim'])} | {f(c['refuted_after_claim'])} |",
           f"| 10 control tasks PM lists an ambiguity for | {f(k['auditor_defect'])} | {f(k['supported_defect'])} | {f(k['partly_after_claim'])} | {f(k['refuted_after_claim'])} |",
           "", "## Per task", "",
           "| task | group | audit | refutation | named test | fails only on it / fails with evidence | passes / scored |", "|---|---|---|---|---|---|---|"]
    for x in rows:
        rl = x["run_level"]
        run_cell = f"{rl['fails_only_on_it']}/{rl['fails_with_evidence']}" if rl else "–"
        pass_cell = f"{rl['passes']}/{rl['scored_trials']}" if rl else "–"
        out.append(f"| {x['task']} | {x['role']} | {x['audit_verdict']} ({x['audit_confidence']}) | "
                   f"{x['refutation'] or '–'} | {x['failing_test'] or '–'} | {run_cell} | {pass_cell} |")
    sp = summary.get("second_pass")
    if sp:
        out += ["", "## Second independent audit pass", "",
                f"Agreement on defect vs not: {sp['agree']}/{sp['n']}, Cohen's kappa "
                f"{sp['kappa']:.2f}. Defect in both passes: {sp['defect_in_both']}; in either: {sp['defect_in_either']}.", "",
                "| group | tasks | defect in pass 1 | defect in pass 2 | in both |", "|---|---|---|---|---|"]
        for role, d in sp["per_role"].items():
            out.append(f"| {role} | {d['n']} | {d['first_defect']} | {d['second_defect']} | {d['both']} |")
        out += ["", "Defect in both: " + ", ".join(sp["defect_in_both_tasks"])]
        rb = summary["robust_candidates"]
        out += ["", f"Robust candidates ({rb['definition']}): {f(rb['rate'])}: " + ", ".join(rb["tasks"])]
    s = summary["supported_candidates_fails_only_on_named_test"]
    out += ["", f"Supported PM-unlisted candidates: {', '.join(s['tasks']) or 'none'}. Their failed trials whose only failed "
            f"tests are the named test: {s['fails']} (of {s['of_fails_with_evidence']} failed trials with evidence on those tasks)."]
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    main()
