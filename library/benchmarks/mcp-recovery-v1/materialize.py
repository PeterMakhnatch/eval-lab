#!/usr/bin/env python3
"""CLI entry point for materializing mcp-recovery-v1 Harbor tasks."""
from __future__ import annotations

import argparse
from pathlib import Path

from materializer import materialize, output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Materialize MCP Recovery benchmark tasks.")
    parser.add_argument("--seed", type=int, default=42, help="Deterministic task seed")
    parser.add_argument("--output", type=Path, default=None, help="Explicit target directory")
    args = parser.parse_args()

    target = args.output or output_path(args.seed)
    materialize(target, seed=args.seed)
    print(f"Materialized MCP Recovery task at: {target}")


if __name__ == "__main__":
    main()
