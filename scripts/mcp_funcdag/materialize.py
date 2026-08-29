#!/usr/bin/env python3
"""CLI helper to materialize MCP FuncDAG tasks."""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BENCH_ROOT = ROOT / "library" / "benchmarks" / "mcp-funcdag-v1"


def _load_module(name: str):
    module_name = f"mcp_funcdag_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    orig_path = list(sys.path)
    sys.path.insert(0, str(BENCH_ROOT))
    try:
        spec = importlib.util.spec_from_file_location(module_name, BENCH_ROOT / f"{name}.py")
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path[:] = orig_path


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
