#!/usr/bin/env python3
"""CLI helper to materialize MCP FuncDAG tasks."""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BENCH_ROOT = ROOT / "library" / "benchmarks" / "mcp-funcdag-v1"


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, BENCH_ROOT / f"{filename}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {name} from {filename}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


contract_mod = _load_module("mcp_funcdag_contract", "contract")
materializer_mod = _load_module("mcp_funcdag_materializer", "materializer")


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
