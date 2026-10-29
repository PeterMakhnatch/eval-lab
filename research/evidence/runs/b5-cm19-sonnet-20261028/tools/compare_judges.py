#!/usr/bin/env python3
"""B5: Sonnet vs CM-13 terra and vs CM-19 Opus on the same frozen windows.

Copied from CM-19's compare_terra.py; the only change is that the reference judge is a parameter, so the same
window- and run-level agreement is written once against terra (CM-13 cache and result) and once against Opus
(CM-19 cache and result). Aggregates only; reads private judge caches.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from math import comb
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
sys.path.insert(0, str(REPO / "src"))
spec = importlib.util.spec_from_file_location("cm19_sonnet", HERE / "cm19_sonnet.py")
assert spec and spec.loader
b5 = importlib.util.module_from_spec(spec)
sys.modules["cm19_sonnet"] = b5
spec.loader.exec_module(b5)
fw = b5.fw

from evallab.production import window_cascade as wc  # noqa: E402

CM19_RESULTS = REPO / "research/evidence/runs/cm19-opus-windows-20261028/results"
REFERENCES = {
    "terra": (b5.CM13_STATE / "judge-cache.jsonl", b5.CM13_RESULTS / "result.json"),
    "opus": (b5.CM13_STATE.parent / "cm19" / "judge-cache.jsonl", CM19_RESULTS / "result.json"),
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


def compare(name: str, prompts: dict, sonnet: dict, sonnet_result: dict) -> dict:
    cache_path, result_path = REFERENCES[name]
    ref = cache(cache_path)
    both = [k for k in prompts if ref.get(k, {}).get("ok") and sonnet.get(k, {}).get("ok")]
    confirm = {
        judge: {k: wc.verdict(c[k]["answer"], fw.window_of(prompts[k])).confirmed for k in both}
        for judge, c in ((name, ref), ("sonnet", sonnet))
    }
    labels = {
        judge: dict(Counter(c[k]["answer"]["label"] for k in both))
        for judge, c in ((name, ref), ("sonnet", sonnet))
    }
    window = {
        "prompts_total": len(prompts),
        "judged_by_both": len(both),
        "sonnet_failed_refused": sum(not sonnet.get(k, {}).get("ok") for k in prompts),
        f"{name}_failed_refused": sum(not ref.get(k, {}).get("ok") for k in prompts),
        f"confirmed_{name}": sum(confirm[name].values()),
        "confirmed_sonnet": sum(confirm["sonnet"].values()),
        "confirmed_both": sum(confirm[name][k] and confirm["sonnet"][k] for k in both),
        f"{name}_only": sum(confirm[name][k] and not confirm["sonnet"][k] for k in both),
        "sonnet_only": sum(confirm["sonnet"][k] and not confirm[name][k] for k in both),
        "label_counts": labels,
    }
    reference = json.loads(result_path.read_text())
    runs: dict[str, dict] = {}
    for arm in ("A", "B", "C"):
        pos = {f"{name}_only": 0, "sonnet_only": 0, "both": 0, "n": 0}
        neg = {f"{name}_only": 0, "sonnet_only": 0, "both": 0, "n": 0}
        for key, row in sonnet_result["per_run"].items():
            a, b = reference["per_run"][key][arm]["alarm"], row[arm]["alarm"]
            if a is None or b is None:
                continue
            bucket = pos if row["positive"] else neg
            bucket["n"] += 1
            if a and b:
                bucket["both"] += 1
            elif a:
                bucket[f"{name}_only"] += 1
            elif b:
                bucket["sonnet_only"] += 1
        for bucket in (pos, neg):
            bucket["sign_test_p"] = sign_p(bucket[f"{name}_only"], bucket["sonnet_only"])
        runs[arm] = {"positives": pos, "negatives": neg}
    tokens = {
        "sonnet_input_per_call": round(sum(sonnet[k]["input_tokens"] for k in both) / len(both), 1),
        "sonnet_output_per_call": round(
            sum(sonnet[k]["output_tokens"] for k in both) / len(both), 1
        ),
        f"{name}_input_per_call": round(sum(ref[k]["input_tokens"] for k in both) / len(both), 1),
        f"{name}_output_per_call": round(sum(ref[k]["output_tokens"] for k in both) / len(both), 1),
    }
    return {"windows": window, f"paired_run_alarms_{name}_vs_sonnet": runs, "tokens": tokens}


def main() -> None:
    prompts = json.loads((b5.STATE / "prompts.json").read_text())
    sonnet = cache(b5.STATE / "judge-cache.jsonl")
    sonnet_result = json.loads((b5.RESULTS / "result.json").read_text())
    out = {name: compare(name, prompts, sonnet, sonnet_result) for name in REFERENCES}
    out["sonnet_tokens_all_ok"] = {
        "calls_ok": sum(bool(r["ok"]) for r in sonnet.values()),
        "input": sum(r["input_tokens"] for r in sonnet.values() if r["ok"]),
        "output": sum(r["output_tokens"] for r in sonnet.values() if r["ok"]),
    }
    (b5.RESULTS / "compare-judges.json").write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
