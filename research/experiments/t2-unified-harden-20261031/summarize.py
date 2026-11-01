#!/usr/bin/env python3
"""Summarize the T2 cells into ``results/summary.json`` and a markdown matrix (no provider call)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
CELLS = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-01/t2-unified-harden/cells")


def main() -> None:
    rows = []
    for path in sorted(CELLS.glob("*/*/result.json")):
        if ".failed-" in path.parent.name:
            continue
        r = json.loads(path.read_text())
        gate = (r.get("gate") or {}).get("gate") or {}
        rows.append(
            {
                "cell": r["cell"],
                "status": r["status"],
                "reward": r["reward"],
                "applied": r["applied"],
                "gate_counts": gate.get("counts"),
                "gate_why": gate.get("why"),
                "restored_pristine": (r.get("prepare") or {}).get("restored_pristine"),
                "removed": (r.get("prepare") or {}).get("removed"),
                "est_usd": r["est_usd"],
                "package_digest": r["package_digest"],
                "result_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    failed = sorted(p.parent.name for p in CELLS.glob("*/*.failed-*"))
    summary = {
        "cells": rows,
        "executed": sum(r["status"] == "executed" for r in rows),
        "est_usd_executed": round(sum(r["est_usd"] for r in rows), 4),
        "set_aside_spend_gate_v1": len(failed),
    }
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    lines = ["| cell | reward | landed | gate | restored / removed |", "|---|---:|---|---|---|"]
    for r in rows:
        restored = ", ".join((r["restored_pristine"] or []) + (r["removed"] or [])) or ""
        lines.append(
            f"| {r['cell']} | {r['reward']} | {r['applied']} | {r['gate_why'] or ''} | {restored} |"
        )
    (out / "matrix.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "cells"}))


if __name__ == "__main__":
    main()
