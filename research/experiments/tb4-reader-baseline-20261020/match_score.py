"""Score the semantic matching: recall of key findings per arm, paired comparison, unmatched sample.

Run after ``match_inputs.py`` and the judges. Writes ``results-semantic.json`` and
``report-semantic.md`` here, and ``unmatched-sample.json`` under the results root.
"""

from __future__ import annotations

import json
import random
import sys
from collections import Counter
from typing import Any

import build_inputs as bi
import score as sc

ROOT = bi.ROOT
HERE = bi.HERE
UNMATCHED_PER_ARM = 20
PASS = sys.argv[1] if len(sys.argv) > 1 else ""


def load_matches(private: dict[str, Any], valid_ids: dict[str, set[str]], sizes: dict[tuple[str, str], int]):
    """Per arm: {key item id: strength}, matched reader indexes, and problems found."""
    per_arm: dict[str, dict[str, Any]] = {arm: {"same": {}, "partial": {}, "reader_same": set(),
                                                "reader_partial": set()} for arm in bi.ARMS}
    problems: list[str] = []
    for aid, meta in sorted(private.items()):
        path = ROOT / f"match-out{PASS}" / f"{aid}.json"
        if not path.is_file():
            problems.append(f"missing {aid}")
            continue
        data = json.loads(path.read_text())
        for letter in ("A", "B"):
            arm = meta[letter]
            seen_idx: set[int] = set()
            seen_key: set[str] = set()
            for row in data.get(letter, []):
                idx, key, strength = row["idx"], row["key_item_id"], row["strength"]
                if (idx in seen_idx or key in seen_key or key not in valid_ids[aid]
                        or not 0 <= idx < sizes[(aid, arm)] or strength not in ("same", "partial")):
                    problems.append(f"{aid} {letter} {row}")
                    continue
                seen_idx.add(idx)
                seen_key.add(key)
                per_arm[arm][strength][key] = aid
                per_arm[arm]["reader_" + strength].add((aid, idx))
    return per_arm, problems


def main() -> None:
    manifest = json.loads((ROOT / "private-manifest.json").read_text())
    private = json.loads((ROOT / f"match{PASS}-private.json").read_text())
    items = [json.loads(line) for line in (sc.KEY / "items.jsonl").read_text().splitlines() if line.strip()]
    items = [i for i in items if i["scope"] == "task"]
    by_task: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_task.setdefault(item["task"], []).append(item)
    task_aid = {m["task"]: aid for aid, m in manifest["tasks"].items()}
    valid = {task_aid[t]: {i["item_id"] for i in its} for t, its in by_task.items()}
    for aid in manifest["tasks"]:
        valid.setdefault(aid, set())
    rows: dict[str, list[dict[str, Any]]] = {}
    sizes: dict[tuple[str, str], int] = {}
    for arm in bi.ARMS:
        rows[arm], _ = sc.load_arm(arm, manifest)
        for aid in manifest["tasks"]:
            sizes[(aid, arm)] = sum(1 for r in rows[arm] if r["aid"] == aid)
    matches, problems = load_matches(private, valid, sizes)
    result: dict[str, Any] = {"schema": "tb4-reader-baseline/semantic/v1", "problems": problems, "arms": {}}
    cats = sorted({i["expected_category"] for i in items})
    for arm in bi.ARMS:
        same = set(matches[arm]["same"])
        both = same | set(matches[arm]["partial"])
        n_pred = len(rows[arm])
        by_split = {s: [i for i in items if i["split"] == s] for s in ("dev", "test")}
        result["arms"][arm] = {
            "recall_same": sc.clusters.rate(len(same), len(items)),
            "recall_same_or_partial": sc.clusters.rate(len(both), len(items)),
            "recall_same_by_split": {s: sc.clusters.rate(sum(i["item_id"] in same for i in v), len(v))
                                     for s, v in by_split.items()},
            "recall_same_by_category": {c: sc.clusters.rate(sum(i["item_id"] in same for i in items
                                                                if i["expected_category"] == c),
                                                            sum(1 for i in items if i["expected_category"] == c))
                                        for c in cats},
            "recall_same_by_pm_type": {
                ty: sc.clusters.rate(sum(i["item_id"] in same for i in items if ty in i["pm_types"]),
                                     sum(1 for i in items if ty in i["pm_types"]))
                for ty in sorted({t for i in items for t in i["pm_types"]})},
            "recall_same_by_label_strength": {
                s: sc.clusters.rate(sum(i["item_id"] in same for i in items if i["label_strength"] == s),
                                    sum(1 for i in items if i["label_strength"] == s))
                for s in sorted({i["label_strength"] for i in items})},
            "precision_same": sc.clusters.rate(len(matches[arm]["reader_same"]), n_pred),
            "predictions": n_pred,
            "same_item_ids": sorted(same), "same_matches": len(same), "partial_matches": len(set(matches[arm]["partial"])),
        }
    rng = random.Random(bi.SEED)
    per_task = {}
    for task, its in by_task.items():
        ids = {i["item_id"] for i in its}
        per_task[task] = (len(its), len(ids & set(matches["static"]["same"])),
                          len(ids & set(matches["leaderboard"]["same"])))
    result["paired_leaderboard_minus_static_same"] = sc.paired_bootstrap(per_task, rng)
    union = set(matches["static"]["same"]) | set(matches["leaderboard"]["same"])
    inter = set(matches["static"]["same"]) & set(matches["leaderboard"]["same"])
    result["union_same"] = sc.clusters.rate(len(union), len(items))
    result["intersection_same"] = sc.clusters.rate(len(inter), len(items))
    result["tasks_with_all_findings_found_static"] = sum(
        1 for t, (n, s, _l) in per_task.items() if n and s == n)
    # key findings found by neither arm: what kind are they?
    missed = [i for i in items if i["item_id"] not in union]
    result["missed_by_both"] = {
        "count": len(missed),
        "by_category": dict(Counter(i["expected_category"] for i in missed)),
        "by_pm_type": dict(Counter(t for i in missed for t in i["pm_types"])),
        "by_execution_claim": dict(Counter(str((i.get("pm_execution") or {}).get("execution_claim")) for i in missed)),
    }
    # unmatched high-confidence reader findings, for the new-defect check
    sample: dict[str, list[dict[str, Any]]] = {}
    for arm in bi.ARMS:
        pool = [r for r in rows[arm] if r["confidence"] == "high"
                and (r["aid"], r["rank"]) not in matches[arm]["reader_same"]
                and (r["aid"], r["rank"]) not in matches[arm]["reader_partial"]]
        picked = random.Random(f"{bi.SEED}:unmatched:{arm}").sample(pool, min(UNMATCHED_PER_ARM, len(pool)))
        sample[arm] = [{"arm": arm, "aid": r["aid"], "rank": r["rank"], "class": r["taxonomy_class"],
                        "pool": len(pool)} for r in picked]
        result["arms"][arm]["unmatched_high_confidence_pool"] = len(pool)
    (ROOT / f"unmatched-sample{PASS}.json").write_text(json.dumps(sample, indent=1) + "\n")
    (HERE / f"results-semantic{PASS}.json").write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    (HERE / f"report-semantic{PASS}.md").write_text(render(result))
    print(render(result))


def render(result: dict[str, Any]) -> str:
    f = sc.fmt
    arms = result["arms"]
    out = ["# TB4 reader baseline: semantic match tables", "",
           "## Recall of the key's 238 task findings (judge matched the same defect, 1-to-1)", "",
           "| arm | same | same or partial | dev | test | precision (same / predictions) |", "|---|---|---|---|---|---|"]
    for arm, a in arms.items():
        out.append(f"| {arm} | {f(a['recall_same'])} | {f(a['recall_same_or_partial'])} | "
                   f"{f(a['recall_same_by_split']['dev'])} | {f(a['recall_same_by_split']['test'])} | "
                   f"{f(a['precision_same'])} of {a['predictions']} |")
    d = result["paired_leaderboard_minus_static_same"]
    out += ["", f"Paired difference (leaderboard minus static), same matches: {d['difference']:+.1%} "
                f"[{d['ci95'][0]:+.1%}, {d['ci95'][1]:+.1%}]", "",
            f"Found by either arm: {f(result['union_same'])}; by both: {f(result['intersection_same'])}.", "",
            "## Recall by key category (same matches)", "", "| category | " + " | ".join(arms) + " |",
            "|---|" + "---|" * len(arms)]
    for cat in next(iter(arms.values()))["recall_same_by_category"]:
        out.append(f"| {cat} | " + " | ".join(f(a["recall_same_by_category"][cat]) for a in arms.values()) + " |")
    out += ["", "## Recall by PM finding type (a finding can carry several)", "",
            "| type | " + " | ".join(arms) + " |", "|---|" + "---|" * len(arms)]
    for ty in next(iter(arms.values()))["recall_same_by_pm_type"]:
        out.append(f"| {ty} | " + " | ".join(f(a["recall_same_by_pm_type"][ty]) for a in arms.values()) + " |")
    out += ["", "## Recall by label strength", "", "| strength | " + " | ".join(arms) + " |", "|---|" + "---|" * len(arms)]
    for s in next(iter(arms.values()))["recall_same_by_label_strength"]:
        out.append(f"| {s} | " + " | ".join(f(a["recall_same_by_label_strength"][s]) for a in arms.values()) + " |")
    m = result["missed_by_both"]
    out += ["", f"## Key findings neither arm matched: {m['count']}", "", f"- by category: {m['by_category']}",
            f"- by PM type: {m['by_pm_type']}", f"- by PM execution claim: {m['by_execution_claim']}", "",
            f"Match problems: {len(result['problems'])}"]
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    main()
