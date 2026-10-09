"""Build item/task-bound ledger and summary from retained measured receipts."""

from __future__ import annotations

import csv
import json
import statistics
from collections import Counter

from build import GENERAL, HERE, WEBDEV, read_meta


def main() -> None:
    rules = {r["task"]: r for r in (json.loads(line) for line in (HERE / "rule-census.jsonl").read_text().splitlines())}
    packages = [json.loads(line) for line in (HERE / "packages.jsonl").read_text().splitlines()]
    rows = []
    for package in packages:
        name = package["task"]
        if package["domain"] == "general":
            row = rules[name]
            post_errors = [item_id for item_id, result in row["post"].items() if result.get("execution_error")]
            decision = "quarantine-rule-runtime" if post_errors else "candidate-judge-validation-required"
            rows.append({"domain": "general", "task": name, "decision": decision,
                         "floor_pre": row["floor_pre"], "floor_g2": row["floor_g2"], "floor_g3": row["floor_g3"],
                         "zeroed_rule_atoms": len(row["zeroed_ids"]), "rule_runtime_errors": ";".join(post_errors),
                         "query_chars": "", "record": package["record"], "package_digest": package["package_digest"]})
        else:
            cfg = json.loads((WEBDEV / "tasks" / name / "tests/grade.json").read_text())
            rows.append({"domain": "webdev", "task": name, "decision": "staged-version-aware-judge-required",
                         "floor_pre": "", "floor_g2": "", "floor_g3": "", "zeroed_rule_atoms": 0,
                         "rule_runtime_errors": "", "query_chars": len(cfg["query"]),
                         "record": package["record"], "package_digest": package["package_digest"]})
    with (HERE / "ledger.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary = {"evidence_tier": "MEASURED", "command": "uv run python research/experiments/judge-variants-night/summarize.py",
               "counts": {"general_final_packages": sum(p["domain"] == "general" for p in packages),
                          "webdev_final_packages": sum(p["domain"] == "webdev" for p in packages),
                          "decisions": dict(Counter(r["decision"] for r in rows))},
               "general": {
                   "rule_atoms": sum(len(r["post"]) for r in rules.values()),
                   "rule_tasks": sum(bool(r["post"]) for r in rules.values()),
                   "no_rule_tasks": sum(not r["post"] for r in rules.values()),
                   "g2_references_tasks": sum("pinned_backup" in read_meta(GENERAL / "tasks" / task).get("check_code", "") for task in rules),
                   "g2_floor_changed_tasks": sum(r["floor_pre"] != r["floor_g2"] for r in rules.values()),
                   "g3_tasks": sum(bool(r["zeroed_ids"]) for r in rules.values()),
                   "zeroed_rule_atoms": sum(len(r["zeroed_ids"]) for r in rules.values()),
                   "remaining_post_rule_error_atoms": sum(bool(result.get("execution_error")) for r in rules.values() for result in r["post"].values()),
                   "floors": {s: {"positive_tasks": sum(r[s] > 0 for r in rules.values()),
                                    "mean_all_925": statistics.mean(r[s] for r in rules.values()),
                                    "max": max(r[s] for r in rules.values())}
                              for s in ("floor_pre", "floor_g2", "floor_g3")},
                   "limit": "Rule-only pristine-state lower bound with text-judge scores fixed to zero; not an observed full nop reward. Remaining rule runtime failures are quarantined, not claimed fixed."},
               "webdev": {"long_briefs_fixed": sum(int(r["query_chars"]) > 1500 for r in rows if r["domain"] == "webdev"),
                          "max_query_chars": max(int(r["query_chars"]) for r in rows if r["domain"] == "webdev"),
                          "oracle_validation": "staged; no shipped oracle", "judge_validation": "staged; no paid calls"}}
    (HERE / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    cohort = {"status": "staged", "general": [], "webdev": []}
    for domain in ("general", "webdev"):
        eligible = sorted((r for r in rows if r["domain"] == domain and not r["rule_runtime_errors"]), key=lambda r: r["task"])
        for i in range(50):
            row = eligible[i * len(eligible) // 50]
            sample = {"task": row["task"], "record": row["record"], "package_digest": row["package_digest"]}
            if domain == "general":
                meta = read_meta(GENERAL / "tasks" / row["task"])
                sample["text_atom_ids"] = [it["id"] for it in meta["items"]
                                           if it.get("method") == "llm" and it.get("judge") != "vision"][:4]
            cohort[domain].append(sample)
    (HERE / "reliability-cohort.json").write_text(json.dumps(cohort, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
