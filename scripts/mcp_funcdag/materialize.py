#!/usr/bin/env python3
"""CLI helper to materialize MCP FuncDAG tasks."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BENCH_ROOT = ROOT / "library" / "benchmarks" / "mcp-funcdag-v1"
sys.path.insert(0, str(BENCH_ROOT))

from contract import CAMPAIGN_0_CELLS
from materializer import materialize_task


def main():
    parser = argparse.ArgumentParser(description="Materialize MCP FuncDAG tasks")
    parser.add_argument("--cell", default=None, help="Specific cell name")
    parser.add_argument("--output-root", type=Path, default=None, help="Output directory")
    args = parser.parse_args()

    cells = CAMPAIGN_0_CELLS
    if args.cell:
        cells = [c for c in CAMPAIGN_0_CELLS if c.get("name") == args.cell]

    for c in cells:
        p = materialize_task(c, output_root=args.output_root)
        print(f"Materialized {c.get('name')}: {p}")


if __name__ == "__main__":
    main()
