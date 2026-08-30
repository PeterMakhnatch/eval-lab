#!/usr/bin/env python3
"""Materialize MCP Function-DAG benchmark tasks."""
from __future__ import annotations

import argparse
from pathlib import Path

from contract import CAMPAIGN_0_CELLS, DIFFICULTY_CELLS
from materializer import materialize_task


def main():
    parser = argparse.ArgumentParser(description="Materialize MCP FuncDAG task(s)")
    parser.add_argument("--cell", default=None, help="Name of specific cell to materialize")
    parser.add_argument("--difficulty-axis", action="store_true", help="Materialize certified difficulty axis cells")
    parser.add_argument("--output-root", type=Path, default=None, help="Custom output root directory")
    args = parser.parse_args()

    all_cells = CAMPAIGN_0_CELLS + DIFFICULTY_CELLS
    if args.cell:
        cells = [c for c in all_cells if c.get("name") == args.cell]
        if not cells:
            raise ValueError(f"Unknown cell: {args.cell}")
    elif args.difficulty_axis:
        cells = DIFFICULTY_CELLS
    else:
        cells = CAMPAIGN_0_CELLS

    for cell in cells:
        task_dir = materialize_task(cell, output_root=args.output_root)
        print(f"Materialized {cell.get('name')}: {task_dir}")


if __name__ == "__main__":
    main()
