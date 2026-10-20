"""Convert reader outputs to qa.finding/v1 rows and score them against tb4-audit-v1.

Usage (repository root)::

    .venv/bin/python research/experiments/tb4-reader-baseline-20261020/score.py

Reads ``outputs/<arm>/<aid>.json`` under the results root, writes predictions and scorer JSON
there, and ``results.json`` plus ``report.md`` next to this file.
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
from collections import Counter
from typing import Any

import build_inputs as bi

sys.path.insert(0, str(bi.HERE.parent / "tb4-failure-clusters-20261020"))
import run as clusters  # noqa: E402

HERE = bi.HERE
ROOT = bi.ROOT
KEY = HERE.parents[1] / "answer-keys/tb4-audit-v1"
MODES = ("category_1to1", "locus_and_class", "task_any_1to1")
VARIANTS = {"all": None, "high_confidence": "high", "top3": 3}
BOOTSTRAP = 10_000
CONFIDENCE = ("high", "medium", "low")


def load_arm(arm: str, manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validated findings of one arm, plus a tally of what was missing or malformed."""
    rows: list[dict[str, Any]] = []
    tally: dict[str, Any] = {"files": 0, "missing": [], "invalid": [], "verdicts": Counter()}
    for aid, meta in sorted(manifest["tasks"].items()):
        path = ROOT / "outputs" / arm / f"{aid}.json"
        if not path.is_file():
            tally["missing"].append(aid)
            continue
        try:
            data = json.loads(path.read_text())
            findings = data["findings"]
            tally["verdicts"][data["task_verdict"]] += 1
        except (ValueError, KeyError, TypeError):
            tally["invalid"].append(aid)
            continue
        tally["files"] += 1
        for rank, finding in enumerate(findings):
            cls = finding.get("class")
            if cls not in bi.CATEGORY:
                tally["invalid"].append(f"{aid}#{rank}:{cls}")
                continue
            quotes = [finding.get("instruction_quote"), finding.get("grader_quote")]
            summary = "\n".join(str(x) for x in [finding.get("title"), finding.get("summary"), *quotes] if x)
            rows.append({
                "schema": "qa.finding/v1", "id": f"{arm}:{aid}:{rank}", "verdict": "fail",
                "subject": {"kind": "task", "task_id": f"terminal-bench/{meta['task']}"},
                "category": bi.CATEGORY[cls], "taxonomy_class": cls,
                "evidence_refs": finding.get("evidence_refs") or [], "summary": summary,
                "confidence": finding.get("confidence"), "rank": rank, "aid": aid,
            })
    return rows, tally


def variant(rows: list[dict[str, Any]], rule: Any) -> list[dict[str, Any]]:
    if rule is None:
        return rows
    if isinstance(rule, int):
        return [r for r in rows if r["rank"] < rule]
    return [r for r in rows if r["confidence"] == rule]


def run_scorer(rows: list[dict[str, Any]], name: str) -> dict[str, Any]:
    pred = ROOT / f"predictions-{name}.jsonl"
    pred.write_text("".join(json.dumps(r) + "\n" for r in rows))
    out = ROOT / f"score-{name}.json"
    subprocess.run([sys.executable, str(KEY / "score_audit.py"), "--predictions", str(pred), "--out", str(out)],
                   check=True, capture_output=True)
    return json.loads(out.read_text())


def paired_bootstrap(per_task: dict[str, tuple[int, int, int]], rng: random.Random) -> dict[str, Any]:
    """Recall difference (second arm minus first) over tasks; tuples are (findings, hits_a, hits_b)."""
    tasks = sorted(per_task)
    total = sum(per_task[t][0] for t in tasks)
    point = (sum(per_task[t][2] for t in tasks) - sum(per_task[t][1] for t in tasks)) / total
    diffs = []
    for _ in range(BOOTSTRAP):
        sample = [per_task[rng.choice(tasks)] for _ in tasks]
        n = sum(s[0] for s in sample)
        diffs.append((sum(s[2] for s in sample) - sum(s[1] for s in sample)) / n if n else 0.0)
    diffs.sort()
    return {"difference": point, "ci95": [diffs[int(0.025 * BOOTSTRAP)], diffs[int(0.975 * BOOTSTRAP)]]}


def main() -> None:
    manifest = json.loads((ROOT / "private-manifest.json").read_text())
    items = [json.loads(line) for line in (KEY / "items.jsonl").read_text().splitlines() if line.strip()]
    items = [i for i in items if i["scope"] == "task"]
    item_task = {i["item_id"]: i["task"] for i in items}
    item_category = {i["item_id"]: i["expected_category"] for i in items}
    per_task_n = Counter(i["task"] for i in items)
    result: dict[str, Any] = {"schema": "tb4-reader-baseline/v1", "seed": bi.SEED, "arms": {}}
    hits: dict[str, dict[str, dict[str, int]]] = {}
    for arm in bi.ARMS:
        rows, tally = load_arm(arm, manifest)
        per_variant = {}
        for name, rule in VARIANTS.items():
            sub = variant(rows, rule)
            score = run_scorer(sub, f"{arm}-{name}")
            cells = {}
            for mode in MODES:
                head = score["modes"][mode]["headline"]
                cells[mode] = {cell: {"recall": head[cell]["recall"], "precision": head[cell]["precision"]}
                               for cell in ("all", "dev", "test")}
            per_variant[name] = {"predictions": len(sub), "modes": cells}
            if name == "all":
                full = score
        by_cat: dict[str, dict[str, int]] = {}
        matched_ids = {mode: {f for _, f in full["matches"][mode]} for mode in MODES}
        for cat in sorted(set(item_category.values())):
            ids = [i for i, c in item_category.items() if c == cat]
            by_cat[cat] = {mode: clusters.rate(sum(i in matched_ids[mode] for i in ids), len(ids)) for mode in MODES}
        hits[arm] = {mode: Counter(item_task[f] for f in matched_ids[mode]) for mode in MODES}
        claimed = {r["aid"] for r in rows}
        tasks_with_claim = len(claimed)
        result["arms"][arm] = {
            "files_read": tally["files"], "missing": tally["missing"], "invalid": tally["invalid"],
            "task_verdicts": dict(tally["verdicts"]), "findings": len(rows),
            "findings_per_task": len(rows) / max(1, tally["files"]),
            "findings_by_class": dict(Counter(r["taxonomy_class"] for r in rows)),
            "findings_by_confidence": dict(Counter(r["confidence"] for r in rows)),
            "tasks_with_claim": clusters.rate(tasks_with_claim, tally["files"]),
            "variants": per_variant, "recall_by_category": by_cat,
        }
    rng = random.Random(bi.SEED)
    result["paired_leaderboard_minus_static"] = {}
    for mode in MODES:
        per_task = {t: (per_task_n[t], hits["static"][mode][t], hits["leaderboard"][mode][t]) for t in per_task_n}
        result["paired_leaderboard_minus_static"][mode] = paired_bootstrap(per_task, rng)
    (HERE / "results.json").write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    (HERE / "report.md").write_text(render(result))
    print(render(result))


def fmt(cell: dict[str, Any] | None) -> str:
    if not cell:
        return "–"
    lo, hi = cell.get("wilson_95") or cell["wilson95"]
    return f"{cell['k']}/{cell['n']} = {cell['rate']:.0%} [{lo:.0%}, {hi:.0%}]"


def render(result: dict[str, Any]) -> str:
    out = ["# TB4 reader baseline: tables", ""]
    for mode in MODES:
        out += [f"## Recall of the key's task findings, `{mode}`", "",
                "| arm | all 66 tasks | dev | test |", "|---|---|---|---|"]
        for arm, a in result["arms"].items():
            m = a["variants"]["all"]["modes"][mode]
            out.append(f"| {arm} | {fmt(m['all']['recall'])} | {fmt(m['dev']['recall'])} | {fmt(m['test']['recall'])} |")
        out.append("")
    out += ["## Precision lower bound (matched predictions / predictions), `category_1to1`", "",
            "| arm | predictions | all | high confidence only | top 3 per task |", "|---|---|---|---|---|"]
    for arm, a in result["arms"].items():
        v = a["variants"]
        out.append(f"| {arm} | {v['all']['predictions']} | {fmt(v['all']['modes']['category_1to1']['all']['precision'])} | "
                   f"{fmt(v['high_confidence']['modes']['category_1to1']['all']['precision'])} "
                   f"({v['high_confidence']['predictions']}) | "
                   f"{fmt(v['top3']['modes']['category_1to1']['all']['precision'])} ({v['top3']['predictions']}) |")
    out += ["", "## Recall by key category, `category_1to1`", "",
            "| category | " + " | ".join(result["arms"]) + " |", "|---|" + "---|" * len(result["arms"])]
    cats = sorted(next(iter(result["arms"].values()))["recall_by_category"])
    for cat in cats:
        out.append(f"| {cat} | " + " | ".join(fmt(a["recall_by_category"][cat]["category_1to1"])
                                              for a in result["arms"].values()) + " |")
    out += ["", "## Paired difference (leaderboard minus static), bootstrap over tasks", "",
            "| mode | recall difference | 95% interval |", "|---|---|---|"]
    for mode, d in result["paired_leaderboard_minus_static"].items():
        out.append(f"| {mode} | {d['difference']:+.1%} | [{d['ci95'][0]:+.1%}, {d['ci95'][1]:+.1%}] |")
    out += ["", "## Output health", ""]
    for arm, a in result["arms"].items():
        out.append(f"- {arm}: {a['files_read']}/66 files, {a['findings']} findings "
                   f"({a['findings_per_task']:.1f} per task), tasks with a claim {fmt(a['tasks_with_claim'])}; "
                   f"missing {len(a['missing'])}, invalid {len(a['invalid'])}; verdicts {a['task_verdicts']}")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    main()
