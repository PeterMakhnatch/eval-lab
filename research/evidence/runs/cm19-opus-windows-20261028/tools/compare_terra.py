#!/usr/bin/env python3
"""CM-19: Opus vs CM-13 terra on the same frozen windows. Aggregates only; reads two private judge caches."""

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
spec = importlib.util.spec_from_file_location("cm19_opus", HERE / "cm19_opus.py")
assert spec and spec.loader
cm19 = importlib.util.module_from_spec(spec)
sys.modules["cm19_opus"] = cm19
spec.loader.exec_module(cm19)
fw = cm19.fw

from evallab.production import window_cascade as wc  # noqa: E402


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


def main() -> None:
    terra = cache(cm19.CM13_STATE / "judge-cache.jsonl")
    opus = cache(cm19.STATE / "judge-cache.jsonl")
    prompts = json.loads((cm19.STATE / "prompts.json").read_text())
    both = [k for k in prompts if terra.get(k, {}).get("ok") and opus.get(k, {}).get("ok")]
    confirm = {}
    for name, c in (("terra", terra), ("opus", opus)):
        confirm[name] = {
            k: wc.verdict(c[k]["answer"], fw.window_of(prompts[k])).confirmed for k in both
        }
    labels = {
        n: Counter(c[k]["answer"]["label"] for k in both)
        for n, c in (("terra", terra), ("opus", opus))
    }
    window = {
        "prompts_total": len(prompts),
        "judged_by_both": len(both),
        "opus_failed_refused": sum(not opus.get(k, {}).get("ok") for k in prompts),
        "terra_failed": sum(not terra.get(k, {}).get("ok") for k in prompts),
        "confirmed_terra": sum(confirm["terra"].values()),
        "confirmed_opus": sum(confirm["opus"].values()),
        "confirmed_both": sum(confirm["terra"][k] and confirm["opus"][k] for k in both),
        "terra_only": sum(confirm["terra"][k] and not confirm["opus"][k] for k in both),
        "opus_only": sum(confirm["opus"][k] and not confirm["terra"][k] for k in both),
        "label_counts": {n: dict(v) for n, v in labels.items()},
    }
    t = json.loads((cm19.CM13_RESULTS / "result.json").read_text())
    o = json.loads((cm19.RESULTS / "result.json").read_text())
    runs: dict[str, dict] = {}
    for arm in ("A", "B", "C"):
        pos = {"terra_only": 0, "opus_only": 0, "both": 0, "n": 0}
        neg = {"terra_only": 0, "opus_only": 0, "both": 0, "n": 0}
        for key, row in o["per_run"].items():
            a, b = t["per_run"][key][arm]["alarm"], row[arm]["alarm"]
            if a is None or b is None:
                continue
            bucket = pos if row["positive"] else neg
            bucket["n"] += 1
            bucket["both" if a and b else "terra_only" if a else "opus_only" if b else "n"] += (
                0 if not (a or b) else 1
            )
        for bucket in (pos, neg):
            bucket["sign_test_p"] = sign_p(bucket["terra_only"], bucket["opus_only"])
        runs[arm] = {"positives": pos, "negatives": neg}
    prompts_tokens = {
        "opus_input_per_call": round(sum(opus[k]["input_tokens"] for k in both) / len(both), 1),
        "opus_output_per_call": round(sum(opus[k]["output_tokens"] for k in both) / len(both), 1),
        "terra_input_per_call": round(sum(terra[k]["input_tokens"] for k in both) / len(both), 1),
        "terra_output_per_call": round(sum(terra[k]["output_tokens"] for k in both) / len(both), 1),
        "opus_input_total_all_ok": sum(r["input_tokens"] for k, r in opus.items() if r["ok"]),
        "opus_output_total_all_ok": sum(r["output_tokens"] for k, r in opus.items() if r["ok"]),
    }
    out = {"windows": window, "paired_run_alarms_terra_vs_opus": runs, "tokens": prompts_tokens}
    (cm19.RESULTS / "compare-terra.json").write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
