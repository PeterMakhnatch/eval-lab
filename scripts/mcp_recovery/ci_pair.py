#!/usr/bin/env python3
"""Narrow, deterministic assertions for recovery paired CI workflow."""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "library" / "benchmarks" / "mcp-recovery-v1"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(ROOT))

from verifier import load_fixed_policy_evidence  # noqa: E402

from evallab.registry import discover_control_evidence  # noqa: E402


def controls(task: Path) -> None:
    evidence = discover_control_evidence(task, REPO)
    if evidence.oracle.reward != 1.0 or evidence.nop.reward != 0.0:
        raise ValueError(f"oracle/nop rewards invalid: {evidence.oracle.reward}/{evidence.nop.reward}")


def same_policy(fault_task: Path, clean_task: Path) -> None:
    fault = fault_task / "controls" / "fixed-policy" / "blind-retry.sh"
    clean = clean_task / "controls" / "fixed-policy" / "blind-retry.sh"
    if not fault.is_file() or not clean.is_file():
        raise ValueError("missing fixed-policy script")
    if hashlib.sha256(fault.read_bytes()).digest() != hashlib.sha256(clean.read_bytes()).digest():
        raise ValueError("fixed-policy scripts differ between matched arms")


def fixed(job_dir: Path, staged_task: Path, expected: float) -> None:
    evidence = load_fixed_policy_evidence(job_dir, staged_task)
    if evidence.reward != expected:
        raise ValueError(f"fixed-policy reward {evidence.reward} != {expected}")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    controls_p = sub.add_parser("controls")
    controls_p.add_argument("task", type=Path)
    pair_p = sub.add_parser("same-policy")
    pair_p.add_argument("fault_task", type=Path)
    pair_p.add_argument("clean_task", type=Path)
    fixed_p = sub.add_parser("fixed")
    fixed_p.add_argument("job_dir", type=Path)
    fixed_p.add_argument("staged_task", type=Path)
    fixed_p.add_argument("expected", type=float)
    args = parser.parse_args()
    if args.command == "controls":
        controls(args.task)
    elif args.command == "same-policy":
        same_policy(args.fault_task, args.clean_task)
    else:
        fixed(args.job_dir, args.staged_task, args.expected)


if __name__ == "__main__":
    main()
