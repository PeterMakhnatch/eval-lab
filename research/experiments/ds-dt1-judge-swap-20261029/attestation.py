#!/usr/bin/env python3
"""DT-1 attestation: what was actually served and what it cost, per pass, from the call cache and the ledger.

Every row exists only because the pinned transport accepted it: one assistant turn, `stopReason == "stop"`,
served `provider/model` equal to `deepseek/deepseek-flash`, no fallback event, no tool call, conforming answer
schema. A row that failed any of those is recorded as failed with its error code and is never a negative.

    attestation.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("cm19_deepseek", HERE / "cm19_deepseek.py")
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
sys.modules["cm19_deepseek"] = mod
spec.loader.exec_module(mod)
fw = mod.fw


def main() -> int:
    rows = [
        json.loads(line)
        for line in (mod.STATE / "judge-cache.jsonl").read_text().splitlines()
        if line.strip()
    ]
    passes: dict[str, dict] = {}
    for tag in ("r1", "r2", "r3", "r4", "r5"):
        chosen = {}
        for row in rows:
            key = row["key"]
            this = key.split("#")[1] if "#" in key else "r1"
            if this != tag:
                continue
            if row["ok"] or key not in chosen:
                chosen[key] = row
        if not chosen:
            continue
        ok = {k: v for k, v in chosen.items() if v["ok"]}
        passes[tag] = {
            "prompts": len(chosen),
            "ok": len(ok),
            "failed": len(chosen) - len(ok),
            "error_codes": Counter(
                error for v in chosen.values() if not v["ok"] for error in v.get("errors") or []
            ),
            "answers": Counter(v["answer"]["label"] for v in ok.values()),
            "input_tokens": sum(v.get("input_tokens") or 0 for v in chosen.values()),
            "output_tokens": sum(v.get("output_tokens") or 0 for v in chosen.values()),
            "cache_read_tokens": sum(v.get("cache_read_tokens") or 0 for v in chosen.values()),
            "cache_write_tokens": sum(v.get("cache_write_tokens") or 0 for v in chosen.values()),
            "usd": round(sum(v.get("cost_usd") or 0.0 for v in chosen.values()), 6),
        }
        passes[tag]["input_tokens_per_prompt"] = round(
            passes[tag]["input_tokens"] / max(1, len(chosen)), 1
        )
        passes[tag]["usd_per_prompt"] = round(passes[tag]["usd"] / max(1, len(chosen)), 6)
    ledger = mod.ledger_total()
    out = {
        "schema": "ds-dt1-attestation/v1",
        "judge": mod.MODEL,
        "route": "paid BYOK via omp -p --mode json, no tools, fallback disabled by overlay",
        "served_identity_enforced": (
            "per call: the only accepted assistant record must carry provider/model = deepseek/deepseek-flash, "
            "stopReason = stop, exactly one assistant turn, no tool block, no fallback/model_changed event; "
            "otherwise the call is recorded as failed"
        ),
        "prompts_sha256": fw.sha((mod.STATE / "prompts.json").read_bytes()),
        "plan_sha256": fw.sha((mod.STATE / "plan.json").read_bytes()),
        "passes": passes,
        "totals": {
            "calls_logged": sum(p["prompts"] for p in passes.values()),
            "ok": sum(p["ok"] for p in passes.values()),
            "input_tokens": sum(p["input_tokens"] for p in passes.values()),
            "output_tokens": sum(p["output_tokens"] for p in passes.values()),
            "cache_read_tokens": sum(p["cache_read_tokens"] for p in passes.values()),
            "usd_cache_sum": round(sum(p["usd"] for p in passes.values()), 6),
            "usd_ledger": round(ledger, 6),
        },
        "spend_ledger": str(mod.SPEND),
    }
    fw.write_json(mod.RESULTS / "attestation.json", out)
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
