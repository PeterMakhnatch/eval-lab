"""pJ-SELF-cb2a4d-4: key-judge prompts and metrics for the finding-budget arms.

`build` writes one judge prompt per (arm, task) from each audit's gate-eligible findings alone, against the ID-OQ-1
enriched key text; baselines B0 (Sonnet 5.5 B, OQ-6 v4) and A0 (terra A, OQ-8) are rebuilt the same way so that every
arm is judged identically. `metrics` scores sol judge outputs and applies the preregistered decision rule. No model calls here.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
from pathlib import Path

RESULTS = Path("/Users/petermakhnatch/Developer/eval-lab-results")
ROOT = RESULTS / "2026-10-29/pj-self-cb2a4d-4-finding-cap"
BUNDLES = RESULTS / "2026-10-29/id-oq1-miss-analysis/enriched-bundles"
OQ8 = RESULTS / "2026-10-29/oq8-data-access-ab/cells/dev"
V4 = RESULTS / "2026-10-27/oq6-tq-gate-rerun-v4/calls/anthropic--claude-sonnet-5-5/B/dev"
BUILDER = Path(__file__).resolve().parents[1] / "id-oq1-miss-analysis/build_judge_prompts.py"
ARMS = ("B0", "A0", "T3", "T6", "S6")


def _builder():
    spec = importlib.util.spec_from_file_location("idoq1_builder", BUILDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(index: int, finding: dict) -> dict:
    return {
        "id": f"C{index}",
        "direction": finding.get("direction"),
        "mechanism": finding.get("mechanism"),
        "citations": finding.get("citations"),
    }


def audit_rows(arm: str, case: str, bundle: dict) -> tuple[list[dict], int | None, int]:
    """Eligible finding rows, audit tokens, raw findings produced."""
    if arm == "B0":
        rows = [
            {k: f[k] for k in ("id", "direction", "mechanism", "citations")}
            for f in bundle["findings"]
            if f["eligible_new_gate"]
        ]
        cell = json.loads((V4 / case / "cell.json").read_text())["audit"]
        return rows, cell["tokens_in"] + cell["tokens_out"], len(bundle["findings"])
    if arm == "A0":
        cell = json.loads((OQ8 / "A" / case / "cell.json").read_text())
        eligible = set(cell["paired"]["memberships"]["new_verified_0_5"])
        raw = (cell["auditor"].get("answer") or {}).get("findings") or []
        rows = [_row(i, f) for i, f in enumerate(raw, 1) if f"C{i}" in eligible]
        a = cell["auditor"]
        return rows, a["tokens_in"] + a["tokens_out"], len(raw)
    cell = json.loads((ROOT / "cells" / arm / case / "cell.json").read_text())
    if cell["findings"] is None:
        return [], None, None
    eligible = set(cell["eligible_ids"] or [])
    rows = [_row(i, f) for i, f in enumerate(cell["findings"], 1) if f["id"] in eligible]
    a = cell["auditor"]
    tokens = (
        None
        if a["tokens_in"] is None or a["tokens_out"] is None
        else a["tokens_in"] + a["tokens_out"]
    )
    return rows, tokens, cell["raw_findings"]


def build() -> None:
    builder = _builder()
    head = builder.header()
    (ROOT / "judge/prompts").mkdir(parents=True, exist_ok=True)
    meta: dict = {}
    for path in sorted(BUNDLES.glob("D*.json")):
        bundle = json.loads(path.read_text())
        case = bundle["case_id"]
        for arm in ARMS:
            rows, tokens, raw = audit_rows(arm, case, bundle)
            meta[f"{arm}:{case}"] = {
                "arm": arm,
                "case": case,
                "parity": bundle["task_parity"],
                "tokens": tokens,
                "raw": raw,
                "eligible_ids": [r["id"] for r in rows],
            }
            if rows:
                synthetic = {
                    "keys": bundle["keys"],
                    "prompt_path": bundle["prompt_path"],
                    "findings": rows,
                }
                (ROOT / "judge/prompts" / f"{arm}-{case}.txt").write_text(
                    builder.render(synthetic, head)
                )
    (ROOT / "judge/meta.json").write_text(json.dumps(meta, indent=2))
    print(
        json.dumps(
            {"cells": len(meta), "prompts": sum(bool(m["eligible_ids"]) for m in meta.values())}
        )
    )


def wilson(k: int, n: int, z: float = 1.959963984540054) -> list[float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(c - h, 3), round(c + h, 3)]


def arm_metrics(meta: dict, judged: dict, arm: str, common: set[str]) -> dict:
    keys: set[tuple[str, str]] = set()
    tasks_hit: set[str] = set()
    eligible = matched = raw = 0
    tokens: int | None = 0
    binding = 0
    cap = {"B0": 3, "A0": 3, "T3": 3, "T6": 6, "S6": 6}[arm]
    for row in meta.values():
        if row["arm"] != arm or row["case"] not in common:
            continue
        raw += row["raw"]
        binding += row["raw"] >= cap
        tokens = None if tokens is None or row["tokens"] is None else tokens + row["tokens"]
        verdicts = judged.get(f"{arm}-{row['case']}", {})
        for finding_id in row["eligible_ids"]:
            eligible += 1
            ids = verdicts.get(finding_id, {}).get("matched_key_ids", [])
            if ids:
                matched += 1
                tasks_hit.add(row["case"])
                keys.update((row["case"], k) for k in ids)
    parity = {
        p: len({k for k in keys if meta[f"{arm}:{k[0]}"]["parity"] == p}) for p in ("even", "odd")
    }
    return {
        "distinct_keys": len(keys),
        "tasks_with_hit": len(tasks_hit),
        "raw_findings": raw,
        "eligible_findings": eligible,
        "matched_findings": matched,
        "key_backed_precision": None if not eligible else round(matched / eligible, 3),
        "precision_wilson95": wilson(matched, eligible),
        "cells_at_cap": binding,
        "audit_tokens": tokens,
        "keys_per_1m_tokens": None if not tokens else round(len(keys) / tokens * 1e6, 1),
        "by_parity": parity,
    }


def metrics() -> dict:
    meta = json.loads((ROOT / "judge/meta.json").read_text())
    judged = {
        p.stem: {r["id"]: r for r in json.loads(p.read_text())["judgments"]}
        for p in sorted((ROOT / "judge/out").glob("*.json"))
    }
    cases = {row["case"] for row in meta.values()}
    common = {c for c in cases if all(meta[f"{a}:{c}"]["raw"] is not None for a in ARMS)}
    arms = {arm: arm_metrics(meta, judged, arm, common) for arm in ARMS}
    noise = abs(arms["T3"]["distinct_keys"] - arms["A0"]["distinct_keys"])
    need = max(3, noise)

    def helps(six: str, three: str) -> dict:
        gain = arms[six]["distinct_keys"] - arms[three]["distinct_keys"]
        p6, p3 = arms[six]["key_backed_precision"], arms[three]["key_backed_precision"]
        ok_precision = p6 is not None and p3 is not None and p6 >= 0.8 * p3
        return {
            "key_gain": gain,
            "required_gain": need,
            "precision_ratio": None if not p3 else round(p6 / p3, 3),
            "helps": gain >= need and ok_precision,
        }

    return {
        "tasks_compared": len(common),
        "tasks_excluded_invalid_audit": sorted(cases - common),
        "arms": arms,
        "terra_noise_keys": noise,
        "terra": helps("T6", "T3"),
        "sonnet": helps("S6", "B0"),
        "raising_the_budget_helps": helps("T6", "T3")["helps"] and helps("S6", "B0")["helps"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("build", "metrics"))
    args = parser.parse_args()
    if args.mode == "build":
        build()
    else:
        result = metrics()
        (ROOT / "results.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
