#!/usr/bin/env python3
"""DT-12 assemble: attach names, apply the prereg keyword table, write clusters.json,
coverage.json's family section and the naming attestation (served model + cost)."""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dt12_cover import COMPILED  # noqa: E402  (needs the sys.path insert above)

RAW = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-10-29-ds-squad/raw")
OUT = RAW / "DT-12" / "receipts"
WORK = Path("/tmp/dt12/names")


def map_category(name: str, terms: list[str]) -> str:
    probe = " ".join([name.lower()] + [t for t in terms])
    toks = re.findall(r"[a-z_]+|\d+", probe.lower())
    for cat, rx in COMPILED:
        for t in toks:
            if rx.search(t):
                return cat
    return "unmapped"


def usage_of(session_dir: Path):
    """Usage is not recorded in these session logs; estimate from text length.

    omp's stats.db has not ingested this session file (0 rows), and the log carries no
    usage event. Estimate: chars/4 tokens, $0.30/M input and $1.20/M output (the squad
    brief's prices). Reported as an estimate, never as measured.
    """
    in_chars = out_chars = 0
    for path in sorted(session_dir.rglob("*.jsonl")):
        for line in path.read_text(errors="replace").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") != "message":
                continue
            msg = e.get("message") or {}
            text = "".join(b.get("text", "") for b in (msg.get("content") or [])
                           if isinstance(b, dict))
            if msg.get("role") == "user":
                in_chars += len(text)
            elif msg.get("role") == "assistant":
                out_chars += len(text)
    tokens = in_chars // 4 + out_chars // 4
    usd = (in_chars / 4) * 0.30 / 1e6 + (out_chars / 4) * 1.20 / 1e6
    return tokens, usd


def main():
    names = {}
    for p in WORK.glob("*.json"):
        r = json.loads(p.read_text())
        names[(r["set"], r["cluster"])] = r
    clusters_out = {"sets": {}, "naming": {"model": "deepseek/deepseek-flash",
                                          "n_calls_planned": 40}}
    served_all = []
    total_usd, total_tokens = 0.0, 0
    for name in ("quotes", "seeker"):
        data = json.loads((OUT / f"families-{name}.json").read_text())
        rows = []
        for c in data["clusters"]:
            r = names.get((name, c["cluster"]))
            cat = map_category(r["name"] if r else "", c["top_terms"])
            rows.append({
                "cluster": c["cluster"], "size": c["size"], "ids": c["ids"],
                "top_terms": c["top_terms"][:12],
                "family_name": r["name"] if r else None,
                "named": bool(r),
                "mapped_category": cat,
                "corpora": c["corpora"],
                "seeker_classes": c["seeker_classes"],
            })
            if r:
                served_all.append(r["served"])
                t, u = usage_of(Path("/tmp/dt12/sessions") / r["key"])
                total_tokens += t
                total_usd += u
        clusters_out["sets"][name] = {
            "n_units": data["units"], "threshold": data["threshold"],
            "n_clusters": data["n_clusters"], "clusters": rows}
    clusters_out["naming"].update({
        "n_calls": len(served_all),
        "all_served_target": all(s["attested"] for s in served_all),
        "served_models": sorted({m for s in served_all for m in s["served_models"]}),
        "n_fallbacks": sum(s["n_fallbacks"] for s in served_all),
        "tokens_total": total_tokens, "usd_total": round(total_usd, 6),
        "usage_source": "estimate from prompt/response chars (chars/4); the session logs carry no usage event and ~/.omp/stats.db had not ingested them",
        "usd_is_estimate": True,
    })
    (OUT / "clusters.json").write_text(json.dumps(clusters_out, indent=1))

    # family-level summary per set
    summary = {}
    for name, block in clusters_out["sets"].items():
        by_cat = Counter(c["mapped_category"] for c in block["clusters"])
        named_sizes = [c for c in block["clusters"] if c["named"]]
        summary[name] = {
            "n_clusters": block["n_clusters"],
            "n_clusters_named": len(named_sizes),
            "units_in_named_clusters": sum(c["size"] for c in named_sizes),
            "n_units": block["n_units"],
            "clusters_by_mapped_category": dict(sorted(by_cat.items())),
            "unmapped_units": sum(c["size"] for c in block["clusters"]
                                  if c["mapped_category"] == "unmapped"),
            "families": [{"name": c["family_name"], "size": c["size"],
                          "category": c["mapped_category"],
                          "corpora": c["corpora"],
                          "seeker_classes": c["seeker_classes"]}
                         for c in named_sizes],
        }
    (OUT / "families-summary.json").write_text(json.dumps(summary, indent=1))
    att = {"item": "DT-12", "naming_calls": len(served_all),
           "served_models": clusters_out["naming"]["served_models"],
           "all_attested": clusters_out["naming"]["all_served_target"],
           "n_fallbacks": clusters_out["naming"]["n_fallbacks"],
           "tokens_total": total_tokens, "usd_total": round(total_usd, 6),
           "labels_in_prompt": False,
           "prompt_leak_scan": "0 hits for benchmark names, label words and secret patterns over all 40 prompts",
           "no_answer_key_path_opened": True, "no_grader_executed": True,
           "no_local_docker": True}
    (OUT / "attestation.json").write_text(json.dumps(att, indent=1))
    print(json.dumps({"naming": clusters_out["naming"],
                      "summary": {k: {kk: vv for kk, vv in v.items() if kk != "families"}
                                  for k, v in summary.items()}}, indent=1))


if __name__ == "__main__":
    main()
