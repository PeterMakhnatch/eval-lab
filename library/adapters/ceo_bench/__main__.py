"""Operator entrypoint: bridge one CEO-Bench run into a Harbor trial dir.

Usage:
    python -m library.adapters.ceo_bench <run_dir> [--out <trial_dir>]

Prints the written trial directory. Exit 1 with an ``error:`` message when
the run directory cannot be bridged.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from library.adapters.ceo_bench.bridge import CeoBenchBridgeError, bridge_ceo_bench_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bridge a CEO-Bench harness run into a Harbor-shaped trial directory.",
    )
    parser.add_argument("run_dir", help="CEO-Bench run directory (bash_agent_runs/run_<id>/)")
    parser.add_argument(
        "--out",
        default=None,
        help="Trial directory to write (default: <run_dir>.trial sibling)",
    )
    args = parser.parse_args(argv)
    run = Path(args.run_dir)
    trial = Path(args.out) if args.out else Path(str(run) + ".trial")
    try:
        summary = bridge_ceo_bench_run(run, trial)
    except CeoBenchBridgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(summary["trial_dir"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
