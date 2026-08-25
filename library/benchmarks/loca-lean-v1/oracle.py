"""Shared Harbor solution wrapper around the single LOCA oracle template."""
from __future__ import annotations

import argparse
from pathlib import Path

from templates import oracle

parser = argparse.ArgumentParser()
parser.add_argument("--task-dir", type=Path, required=True)
parser.add_argument("--workspace", type=Path, default=None)
args = parser.parse_args()
print(oracle(args.task_dir, args.workspace))
