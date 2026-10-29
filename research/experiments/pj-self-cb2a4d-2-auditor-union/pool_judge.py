"""pJ-SELF-cb2a4d-2: does pooling existing audits raise confirmed-key yield per audited task?

Pools the gate-eligible findings of three existing audits on the 44 keyed DEV tasks (Sonnet 5.5 B from OQ-6 v4;
terra A and terra B from OQ-8), anonymised and shuffled, so a judge sees one finding list per task against the
enriched key text from ID-OQ-1. `build` writes judge prompts and the finding->audit map; `metrics` scores the judge
outputs. Preregistered in the bridge before any pooled finding was judged. DEV only; no model calls here.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import itertools
import json
import math
from pathlib import Path

RESULTS = Path("/Users/petermakhnatch/Developer/eval-lab-results")
BUNDLES = RESULTS / "2026-10-29/id-oq1-miss-analysis/enriched-bundles"
OQ8 = RESULTS / "2026-10-29/oq8-data-access-ab/cells/dev"
AUDITS = ("S", "TA", "TB")
JUDGE_BUILDER = Path(__file__).resolve().parents[1] / "id-oq1-miss-analysis/build_judge_prompts.py"


def _load_builder():
    spec = importlib.util.spec_from_file_location("idoq1_build_judge_prompts", JUDGE_BUILDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def terra_findings(case: str, arm: str) -> tuple[list[dict], int | None]:
    cell = json.loads((OQ8 / arm / case / "cell.json").read_text())
    audit = cell["auditor"]
    eligible = set(cell["paired"]["memberships"]["new_verified_0_5"])
    findings = ((audit.get("answer") or {}).get("findings")) or []
    rows = [
        {"orig": f"C{i}", "direction": f.get("direction"), "mechanism": f.get("mechanism"), "citations": f.get("citations")}
        for i, f in enumerate(findings, start=1)
        if f"C{i}" in eligible
    ]
    tokens = None if audit.get("tokens_in") is None or audit.get("tokens_out") is None else audit["tokens_in"] + audit["tokens_out"]
    return rows, tokens


def sonnet_findings(bundle: dict) -> list[dict]:
    return [
        {"orig": f["id"], "direction": f["direction"], "mechanism": f["mechanism"], "citations": f["citations"]}
        for f in bundle["findings"]
        if f["eligible_new_gate"]
    ]


def sonnet_tokens(case: str) -> int | None:
    path = (
        RESULTS / "2026-10-27/oq6-tq-gate-rerun-v4/calls/anthropic--claude-sonnet-5-5/B/dev" / case / "cell.json"
    )
    audit = json.loads(path.read_text())["audit"]
    if audit.get("tokens_in") is None or audit.get("tokens_out") is None:
        return None
    return audit["tokens_in"] + audit["tokens_out"]


def build(output: Path) -> None:
    builder = _load_builder()
    head = builder.header()
    (output / "prompts").mkdir(parents=True, exist_ok=True)
    mapping, tokens = {}, {}
    for path in sorted(BUNDLES.glob("D*.json")):
        bundle = json.loads(path.read_text())
        case = bundle["case_id"]
        per_audit = {"S": sonnet_findings(bundle)}
        tokens[case] = {"S": sonnet_tokens(case)}
        for name, arm in (("TA", "A"), ("TB", "B")):
            per_audit[name], tokens[case][name] = terra_findings(case, arm)
        pooled = [
            {"audit": audit, **row}
            for audit in AUDITS
            for row in per_audit[audit]
        ]
        pooled.sort(key=lambda r: sha(f"{case}:{r['audit']}:{r['orig']}"))
        for index, row in enumerate(pooled, start=1):
            row["id"] = f"C{index}"
        mapping[case] = {
            "task_parity": bundle["task_parity"], "keys": [k["key_id"] for k in bundle["keys"]],
            "pooled": [{k: r[k] for k in ("id", "audit", "orig")} for r in pooled],
        }
        if pooled:
            synthetic = {
                "keys": bundle["keys"], "prompt_path": bundle["prompt_path"],
                "findings": [
                    {"id": r["id"], "direction": r["direction"], "mechanism": r["mechanism"], "citations": r["citations"]}
                    for r in pooled
                ],
            }
            (output / "prompts" / f"{case}.txt").write_text(builder.render(synthetic, head))
    (output / "pool-map.json").write_text(json.dumps({"mapping": mapping, "tokens": tokens}, indent=2))
    print(json.dumps({
        "cases": len(mapping), "cases_with_pool": sum(bool(m["pooled"]) for m in mapping.values()),
        "pooled_findings": sum(len(m["pooled"]) for m in mapping.values()),
        "by_audit": {a: sum(r["audit"] == a for m in mapping.values() for r in m["pooled"]) for a in AUDITS},
    }))


def wilson(k: int, n: int, z: float = 1.959963984540054) -> list[float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(c - h, 3), round(c + h, 3)]


def load_judgments(directory: Path) -> dict[str, dict[str, dict]]:
    return {
        p.stem: {row["id"]: row for row in json.loads(p.read_text())["judgments"]}
        for p in sorted(directory.glob("D*.json"))
    }


def matches(mapping: dict, judged: dict, subset: tuple[str, ...], parity: str | None) -> dict:
    keys_hit, tasks_hit, eligible, matched_findings = set(), set(), 0, 0
    for case, entry in mapping.items():
        if parity and entry["task_parity"] != parity:
            continue
        rows = judged.get(case, {})
        for finding in entry["pooled"]:
            if finding["audit"] not in subset:
                continue
            eligible += 1
            verdict = rows.get(finding["id"])
            if verdict and verdict["matched_key_ids"]:
                matched_findings += 1
                keys_hit.update((case, k) for k in verdict["matched_key_ids"])
                tasks_hit.add(case)
    return {
        "keys": keys_hit, "tasks_hit": tasks_hit, "eligible": eligible, "matched_findings": matched_findings,
    }


def metrics(root: Path, judge_dirs: dict[str, Path]) -> dict:
    pool = json.loads((root / "pool-map.json").read_text())
    mapping, tokens = pool["mapping"], pool["tokens"]
    judged = {name: load_judgments(path) for name, path in judge_dirs.items()}
    subsets = [c for n in range(1, 4) for c in itertools.combinations(AUDITS, n)]
    out: dict = {"tasks": len(mapping), "sets": {}}
    for subset in subsets:
        label = "+".join(subset)
        total_tokens = sum(
            tokens[c][a] for c in mapping for a in subset if tokens[c][a] is not None
        )
        unknown = any(tokens[c][a] is None for c in mapping for a in subset)
        row = {"audits": len(subset), "audit_tokens": None if unknown else total_tokens}
        per_judge = {}
        for name, jd in judged.items():
            per_judge[name] = matches(mapping, jd, subset, None)
        both = per_judge["sol"]["keys"] & per_judge["terra"]["keys"] if {"sol", "terra"} <= set(per_judge) else None
        for name, value in per_judge.items():
            keys = value["keys"]
            row[name] = {
                "distinct_keys": len(keys), "tasks_with_hit": len(value["tasks_hit"]),
                "eligible_findings": value["eligible"], "matched_findings": value["matched_findings"],
                "key_backed_precision": None if not value["eligible"] else round(value["matched_findings"] / value["eligible"], 3),
                "precision_wilson95": wilson(value["matched_findings"], value["eligible"]),
                "keys_per_audited_task": round(len(keys) / len(mapping), 3),
                "keys_per_1m_tokens": None if unknown else round(len(keys) / total_tokens * 1e6, 1),
                "even_half_keys": len({k for k in keys if mapping[k[0]]["task_parity"] == "even"}),
                "odd_half_keys": len({k for k in keys if mapping[k[0]]["task_parity"] == "odd"}),
            }
        if both is not None:
            row["both_judges_agree_keys"] = len(both)
        out["sets"][label] = row
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)
    build_parser = sub.add_parser("build")
    build_parser.add_argument("--output", type=Path, required=True)
    metric_parser = sub.add_parser("metrics")
    metric_parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "build":
        build(args.output)
    else:
        result = metrics(args.root, {"sol": args.root / "judge-out/sol", "terra": args.root / "judge-out/terra"})
        (args.root / "results.json").write_text(json.dumps(result, indent=2))
        for label, row in result["sets"].items():
            print(label, {k: row[k] for k in ("audit_tokens", "both_judges_agree_keys") if k in row},
                  "sol", row["sol"]["distinct_keys"], row["sol"]["tasks_with_hit"],
                  "terra", row["terra"]["distinct_keys"])


if __name__ == "__main__":
    main()
