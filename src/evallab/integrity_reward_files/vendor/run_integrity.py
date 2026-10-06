#!/usr/bin/env python3
"""Stdlib-only fallback scorer for the integrity reward (no RewardKit import).

Runs the same vendored criteria as the ``tests/<dim>/*.py`` files and writes
RewardKit-compatible ``reward.json`` + ``reward-details.json``. Used by the
variant ``test.sh`` when ``rewardkit`` is not installed in the task image
(the common case: images carry Python 3.7-3.12, RewardKit needs >=3.12 plus
``litellm``/``pydantic``, uninstallable under the egress lock).
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import integrity_core as core  # ty: ignore[unresolved-import]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Score integrity dims without RewardKit.")
    parser.add_argument("--workspace", default=os.environ.get("INTEGRITY_WORKSPACE", "/testbed"))
    parser.add_argument("--logs-dir", default=os.environ.get("INTEGRITY_LOGS_DIR", "/logs"))
    parser.add_argument("--tests-dir", default=os.environ.get("INTEGRITY_TESTS_DIR", "/tests"))
    parser.add_argument("--output", default="/logs/verifier/reward.json")
    args = parser.parse_args(argv)
    result = core.evaluate(
        workspace=args.workspace, logs_dir=args.logs_dir, tests_dir=args.tests_dir
    )
    if result["raw_missing"]:
        print(
            "integrity: unscored (no verifier/reward.txt); leaving reward.json absent",
            file=sys.stderr,
        )
        return 0
    dims = core.write_outputs(result, args.output)
    for name in ("reward", "integrity", "reward_gated"):
        print(f"{name}: {dims[name]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
