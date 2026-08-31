#!/usr/bin/env python3
"""CLI entry point for materializing mcp-recovery-v1 Harbor tasks."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "src"))
sys.path.insert(0, str(HERE))

from materializer import materialize, output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Materialize MCP Recovery benchmark tasks.")
    parser.add_argument("--seed", type=int, default=42, help="Deterministic task seed")
    parser.add_argument("--fault-mode", default="persistent_signature_error")
    parser.add_argument("--persistence", type=int, default=1)
    parser.add_argument(
        "--clean-twin",
        action="store_true",
        help="Materialize the no-injection member of the matched fault pair",
    )
    parser.add_argument("--output", type=Path, default=None, help="Explicit target directory")
    args = parser.parse_args()
    target = args.output or output_path(
        args.seed,
        args.fault_mode,
        args.persistence,
        args.clean_twin,
    )
    materialize(
        target,
        seed=args.seed,
        fault_mode=args.fault_mode,
        persistence=args.persistence,
        is_clean_twin=args.clean_twin,
    )
    print(f"Materialized MCP Recovery task at: {target}")


if __name__ == "__main__":
    main()
