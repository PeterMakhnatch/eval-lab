"""Shared runtime entrypoint referenced by the generated Harbor canary."""
from __future__ import annotations

import argparse
from pathlib import Path

from materializer import materialize

parser = argparse.ArgumentParser()
parser.add_argument("--task-dir", type=Path, required=True)
args = parser.parse_args()
# Harbor normally prepares state before entrypoint; this path is a deterministic
# compatibility hook and never fetches or writes outside the task directory.
if not (args.task_dir / "files" / "environment_description.json").is_file():
    raise SystemExit("prepared LOCA task state is missing")
print(args.task_dir)
