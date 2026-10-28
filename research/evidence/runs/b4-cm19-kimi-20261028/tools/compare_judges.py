#!/usr/bin/env python3
"""B4: Kimi vs CM-13 terra and vs CM-19 Opus on the same frozen windows.

CM-19's ``compare_terra.py`` run twice, once per reference judge (terra: the CM-13 cache and result;
Opus: the CM-19 cache and committed result). Aggregates only; reads three private judge caches.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from math import comb
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
sys.path.insert(0, str(REPO / "src"))
spec = importlib.util.spec_from_file_location("cm19_kimi", HERE / "cm19_kimi.py")
assert spec and spec.loader
b4 = importlib.util.module_from_spec(spec)
sys.modules["cm19_kimi"] = b4
spec.loader.exec_module(b4)
fw = b4.fw

from evallab.production import window_cascade as wc  # noqa: E402

REFERENCES = {
    "terra": (b4.CM13_STATE / "judge-cache.jsonl", b4.CM13_RESULTS / "result.json"),
    "opus": (
        b4.CM13_STATE.parent / "cm19/judge-cache.jsonl",
        REPO / "research/evidence/runs/cm19-opus-windows-20261028/results/result.json",
    ),
}


def cache(path: Path, suffix: str = "") -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for line in path.read_text().splitlines():
        row = json.loads(line)
        if (
            row["key"].endswith(suffix)
            and "#" not in row["key"]
            and (row["ok"] or row["key"] not in rows)
        ):
            rows[row["key"]] = row
    return rows


def sign_p(a: int, b: int) -> float | None:
    n = a + b
    if not n:
        return None
    return round(min(1.0, 2 * sum(comb(n, i) for i in range(min(a, b) + 1)) / 2**n), 4)


def compare(
    name: str, ref: dict[str, dict], ref_result: dict, kimi: dict[str, dict], prompts: dict
) -> dict[str, Any]:
    both = [k for k in prompts if ref.get(k, {}).get("ok") and kimi.get(k, {}).get("ok")]
    confirm = {
        judge: {k: wc.verdict(c[k]["answer"], fw.window_of(prompts[k])).confirmed for k in both}
        for judge, c in ((name, ref), ("kimi", kimi))
    }
    window = {
        "prompts_total": len(prompts),
        "judged_by_both": len(both),
        "kimi_failed_refused": sum(not kimi.get(k, {}).get("ok") for k in prompts),
        f"{name}_failed_refused": sum(not ref.get(k, {}).get("ok") for k in prompts),
        f"confirmed_{name}": sum(confirm[name].values()),
        "confirmed_kimi": sum(confirm["kimi"].values()),
        "confirmed_both": sum(confirm[name][k] and confirm["kimi"][k] for k in both),
        f"{name}_only": sum(confirm[name][k] and not confirm["kimi"][k] for k in both),
        "kimi_only": sum(confirm["kimi"][k] and not confirm[name][k] for k in both),
        "label_counts": {
            judge: dict(Counter(c[k]["answer"]["label"] for k in both))
            for judge, c in ((name, ref), ("kimi", kimi))
        },
    }
    mine = json.loads((b4.RESULTS / "result.json").read_text())
    runs: dict[str, dict] = {}
    for arm in ("A", "B", "C"):
        pos = {f"{name}_only": 0, "kimi_only": 0, "both": 0, "n": 0}
        neg = {f"{name}_only": 0, "kimi_only": 0, "both": 0, "n": 0}
        for key, row in mine["per_run"].items():
            a, b = ref_result["per_run"][key][arm]["alarm"], row[arm]["alarm"]
            if a is None or b is None:
                continue
            bucket = pos if row["positive"] else neg
            bucket["n"] += 1
            if a and b:
                bucket["both"] += 1
            elif a:
                bucket[f"{name}_only"] += 1
            elif b:
                bucket["kimi_only"] += 1
        for bucket in (pos, neg):
            bucket["sign_test_p"] = sign_p(bucket[f"{name}_only"], bucket["kimi_only"])
        runs[arm] = {"positives": pos, "negatives": neg}
    tokens = {
        "kimi_input_per_call": round(sum(kimi[k]["input_tokens"] for k in both) / len(both), 1),
        "kimi_output_per_call": round(sum(kimi[k]["output_tokens"] for k in both) / len(both), 1),
        f"{name}_input_per_call": round(sum(ref[k]["input_tokens"] for k in both) / len(both), 1),
        f"{name}_output_per_call": round(sum(ref[k]["output_tokens"] for k in both) / len(both), 1),
    }
    return {"windows": window, f"paired_run_alarms_{name}_vs_kimi": runs, "tokens": tokens}


def main() -> None:
    kimi = cache(b4.STATE / "judge-cache.jsonl")
    prompts = json.loads((b4.STATE / "prompts.json").read_text())
    out: dict[str, Any] = {
        name: compare(name, cache(path), json.loads(result.read_text()), kimi, prompts)
        for name, (path, result) in REFERENCES.items()
    }
    out["kimi_tokens_all_ok"] = {
        "calls_ok": sum(bool(r["ok"]) for r in kimi.values()),
        "input_total": sum(r["input_tokens"] for r in kimi.values() if r["ok"]),
        "output_total": sum(r["output_tokens"] for r in kimi.values() if r["ok"]),
    }
    (b4.RESULTS / "compare-judges.json").write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
