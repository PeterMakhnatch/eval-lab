#!/usr/bin/env python3
"""CLI utility to verify an MCP Recovery task."""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "library" / "benchmarks" / "mcp-recovery-v1"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(ROOT))

from verifier import verify_harbor_task  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify an MCP Recovery Harbor task directory.")
    parser.add_argument("task_dir", type=Path, help="Path to materialized task directory")
    parser.add_argument("--reward-dir", type=Path, default=None, help="Directory to save reward.txt and summary.json")
    args = parser.parse_args()

    res = verify_harbor_task(args.task_dir, reward_dir=args.reward_dir)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
