#!/usr/bin/env python3
"""DT-1: DeepSeek (single and vote) against the stored Opus 5.5 and terra judgments, complete cases only.

Modelled on CM-19's ``compare_terra.py``: run-level paired alarms read from the published per-run rows, plus
window-level agreement recomputed from the private judge caches. Exact two-sided sign test over discordant pairs.

    compare.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from math import comb
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("cm19_deepseek", HERE / "cm19_deepseek.py")
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
sys.modules["cm19_deepseek"] = mod
spec.loader.exec_module(mod)
fw = mod.fw
wc = fw.wc

EVIDENCE = mod.REPO / "research/evidence/runs"
OPUS_DIR = EVIDENCE / "cm19-opus-windows-20261028"
TERRA_DIR = EVIDENCE / "cm13-facet-windows-20261028"
STATES = mod.CM13_STATE.parent


def cache(path: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        key = row["key"]
        if "#" in key:
            continue
        if row["ok"] or key not in rows:
            rows[key] = row
    return rows


def sign_p(a: int, b: int) -> float | None:
    n = a + b
    if not n:
        return None
    return round(min(1.0, 2 * sum(comb(n, i) for i in range(min(a, b) + 1)) / 2**n), 4)


def vote(caches: list[dict], prompts: dict, key: str, threshold: int) -> bool | None:
    verdicts = []
    for c in caches:
        row = c.get(key)
        if row and row.get("ok"):
            verdicts.append(wc.verdict(row["answer"], fw.window_of(prompts[key])).confirmed)
    if not verdicts:
        return None
    return sum(verdicts) >= threshold


def main() -> int:
    prompts = json.loads((mod.STATE / "prompts.json").read_text())
    mine = cache(mod.STATE / "judge-cache.jsonl")
    mine_passes = []
    for r in range(1, 6):
        path = mod.STATE / "judge-cache.jsonl"
        rows: dict[str, dict] = {}
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row["key"].endswith(f"#r{r}" if r > 1 else "") and (
                "#" not in row["key"] if r == 1 else True
            ):
                if row["ok"] or row["key"] not in rows:
                    rows[row["key"].removesuffix(f"#r{r}" if r > 1 else "")] = row
        mine_passes.append(rows)
    opus_cache = cache(STATES / "cm19/judge-cache.jsonl")
    terra_cache = cache(STATES / "cm13/judge-cache.jsonl")

    judges: dict[str, dict[str, bool | None]] = {
        "deepseek_single": {},
        "deepseek_vote3": {},
        "opus_single": {},
        "terra_single": {},
    }
    for key in prompts:
        judges["deepseek_single"][key] = vote([mine_passes[0]], prompts, key, 1)
        judges["deepseek_vote3"][key] = vote(mine_passes, prompts, key, 3)
        judges["opus_single"][key] = vote([opus_cache], prompts, key, 1)
        judges["terra_single"][key] = vote([terra_cache], prompts, key, 1)

    window = {"prompts_total": len(prompts)}
    for name, table in judges.items():
        ok = [k for k in prompts if table[k] is not None]
        window[name] = {
            "judged": len(ok),
            "unknown": len(prompts) - len(ok),
            "confirmed": sum(table[k] is True for k in ok),
        }
    pairs: dict[str, dict] = {}
    for other in ("opus_single", "terra_single"):
        a, b = judges["deepseek_single"], judges[other]
        both = [k for k in prompts if a[k] is not None and b[k] is not None]
        pairs[f"deepseek_single_vs_{other}"] = {
            "judged_by_both": len(both),
            "confirmed_both": sum(a[k] and b[k] for k in both),
            "deepseek_only": sum(a[k] and not b[k] for k in both),
            "other_only": sum(b[k] and not a[k] for k in both),
            "sign_test_p": sign_p(
                sum(a[k] and not b[k] for k in both), sum(b[k] and not a[k] for k in both)
            ),
        }
        a = judges["deepseek_vote3"]
        both = [k for k in prompts if a[k] is not None and b[k] is not None]
        pairs[f"deepseek_vote3_vs_{other}"] = {
            "judged_by_both": len(both),
            "confirmed_both": sum(a[k] and b[k] for k in both),
            "deepseek_only": sum(a[k] and not b[k] for k in both),
            "other_only": sum(b[k] and not a[k] for k in both),
            "sign_test_p": sign_p(
                sum(a[k] and not b[k] for k in both), sum(b[k] and not a[k] for k in both)
            ),
        }

    mine_result = json.loads((mod.RESULTS / "result-single.json").read_text())
    mine_vote = (
        json.loads((mod.RESULTS / "result-vote.json").read_text())
        if (mod.RESULTS / "result-vote.json").exists()
        else {"variants": {"vote_ge_3": {"per_run": {}}}}
    )
    others = {
        "opus_single": json.loads((OPUS_DIR / "results/result.json").read_text()),
        "terra_single": json.loads((TERRA_DIR / "results/result.json").read_text()),
    }
    runs: dict[str, dict] = {}
    for other_name, other in others.items():
        for mine_name, mine_rows in (
            ("deepseek_single", mine_result["per_run"]),
            ("deepseek_vote3", mine_vote["variants"]["vote_ge_3"]["per_run"]),
        ):
            for arm in ("A", "B", "C"):
                pos = Counter()
                neg = Counter()
                for key, row in mine_rows.items():
                    if key not in other["per_run"]:
                        continue
                    a, b = row[arm]["alarm"], other["per_run"][key][arm]["alarm"]
                    if a is None or b is None:
                        continue
                    bucket = pos if row["positive"] else neg
                    bucket["n"] += 1
                    if a and b:
                        bucket["both"] += 1
                    elif a:
                        bucket["deepseek_only"] += 1
                    elif b:
                        bucket["other_only"] += 1
                    else:
                        bucket["neither"] += 1
                runs[f"{mine_name}_vs_{other_name}_{arm}"] = {
                    "positives": dict(pos) | {"sign_test_p": sign_p(pos["deepseek_only"], pos["other_only"])},
                    "negatives": dict(neg) | {"sign_test_p": sign_p(neg["deepseek_only"], neg["other_only"])},
                }
    out = {"windows": window, "window_pairs": pairs, "paired_run_alarms": runs}
    fw.write_json(mod.RESULTS / "compare.json", out)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
