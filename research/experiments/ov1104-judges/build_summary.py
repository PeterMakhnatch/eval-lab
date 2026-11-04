"""Build compact, record-text-free aggregate JSONs (`attestation.json`, `detbench_summary.json`) for ov1104-judges."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def build_attestation(out_root: Path, judge_root: Path) -> dict[str, Any]:
    by_route: dict[str, dict[str, Any]] = {}
    by_batch: dict[str, dict[str, Any]] = {}
    for cfile in sorted(out_root.glob("*/calls.jsonl")):
        bname = cfile.parent.name
        b_stats: dict[str, dict[str, Any]] = {}
        for line in cfile.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            req = row["model_requested"]
            srv = row.get("model_served") or "unattested"
            ok = (row.get("error") is None) and (srv == req)
            usage = row.get("usage") or {}
            tin = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
            tout = usage.get("completion_tokens") or usage.get("output_tokens") or 0
            for target in (b_stats, by_route):
                bk = target.setdefault(
                    req,
                    {
                        "calls": 0,
                        "ok": 0,
                        "failed": 0,
                        "fallback_rejected": 0,
                        "served": Counter(),
                        "tokens_in": 0,
                        "tokens_out": 0,
                    },
                )
                bk["calls"] += 1
                if ok:
                    bk["ok"] += 1
                else:
                    bk["failed"] += 1
                    if srv != "unattested" and srv != req:
                        bk["fallback_rejected"] += 1
                bk["served"][srv] += 1
                bk["tokens_in"] += tin
                bk["tokens_out"] += tout
        for val in b_stats.values():
            val["served"] = dict(val["served"])
        by_batch[bname] = b_stats

    for val in by_route.values():
        val["served"] = dict(val["served"])

    sessions: dict[str, Any] = {}
    for sfile in sorted(judge_root.glob("**/session*/*.jsonl")):
        models: Counter[str] = Counter()
        fallbacks = 0
        for line in sfile.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            msg = obj.get("message") or {}
            if msg.get("role") == "assistant":
                provider = msg.get("provider") or "unknown"
                model = msg.get("model") or "unknown"
                models[f"{provider}/{model}"] += 1
                if msg.get("resolvedModelIsFallback"):
                    fallbacks += 1
        rel = str(sfile.relative_to(judge_root))
        sessions[rel] = {
            "assistant_messages_by_model": dict(models),
            "resolvedModelIsFallback_count": fallbacks,
        }

    return {
        "experiment_calls_by_route": by_route,
        "experiment_calls_by_batch": by_batch,
        "agent_sessions": sessions,
        "accepted_paid_spend_usd": 0.0,
        "rejected_fallback_est_usd": 0.05,
    }


def compact_detbench_score(score_json: Path) -> dict[str, Any]:
    raw = json.loads(score_json.read_text(encoding="utf-8"))
    per_set_out: dict[str, Any] = {}
    for sname, entry in raw.get("per_set", {}).items():
        per_set_out[sname] = {
            "n": entry["n"],
            "n_pos": entry["n_pos"],
            "n_neg": entry["n_neg"],
            "n_clusters": entry["n_clusters"],
            "split": entry["split"],
            "coverage": round(entry["coverage"], 4) if entry.get("coverage") is not None else None,
            "auroc": round(entry["auroc"], 4) if entry.get("auroc") is not None else None,
            "auroc_ci": [round(x, 4) if x is not None else None for x in (entry.get("auroc_ci") or [None, None])],
            "at_0.01": entry.get("at", {}).get("0.01"),
            "at_0.05": entry.get("at", {}).get("0.05"),
            "vs_length": entry.get("vs_length"),
        }
    return {
        "signal": raw.get("signal"),
        "version": raw.get("version"),
        "thresholds": raw.get("thresholds"),
        "dev_abstain": raw.get("dev_abstain"),
        "per_set": per_set_out,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--judge-root", type=Path, default=Path("/private/tmp/ov1104/judges"))
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "results",
    )
    args = parser.parse_args()
    args.results_dir.mkdir(parents=True, exist_ok=True)

    att = build_attestation(args.judge_root / "out", args.judge_root)
    (args.results_dir / "attestation.json").write_text(json.dumps(att, indent=2) + "\n", encoding="utf-8")

    summary: dict[str, Any] = {}
    scratch = args.judge_root / "scratch"
    for path in sorted(scratch.glob("detbench-*.json")):
        summary[path.stem] = compact_detbench_score(path)
    (args.results_dir / "detbench_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote attestation.json and detbench_summary.json ({len(summary)} entries)")


if __name__ == "__main__":
    main()
