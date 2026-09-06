#!/usr/bin/env python3
"""CLI helper to materialize MCP FuncDAG tasks."""

from __future__ import annotations

import argparse
from pathlib import Path

try:
    from scripts.mcp_funcdag.common import _load_module
except ImportError:
    from common import _load_module  # type: ignore[no-redef]

contract_mod = _load_module("contract")
materializer_mod = _load_module("materializer")


def main():
    parser = argparse.ArgumentParser(description="Materialize MCP FuncDAG tasks")
    parser.add_argument("--cell", default=None, help="Specific cell name")
    parser.add_argument("--output-root", type=Path, default=None, help="Output directory")
    args = parser.parse_args()

    cells = contract_mod.CAMPAIGN_0_CELLS
    if args.cell:
        cells = [c for c in contract_mod.CAMPAIGN_0_CELLS if c.get("name") == args.cell]

    for c in cells:
        p = materializer_mod.materialize_task(c, output_root=args.output_root)
        print(f"Materialized {c.get('name')}: {p}")


if __name__ == "__main__":
    main()
