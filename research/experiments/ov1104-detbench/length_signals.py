"""Run-length baselines for every row of runs.jsonl: length_steps and length_chars.

Usage (from the eval-lab worktree):
  uv run python research/experiments/ov1104-detbench/length_signals.py
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from detbench_lib import RUNS, SIGNALS, read_jsonl, signal_row, write_jsonl  # noqa: E402


def main() -> int:
    rows = read_jsonl(RUNS)
    version = f"length@{hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]}"
    n1 = write_jsonl(SIGNALS / "length_steps.jsonl", (signal_row(r["run_key"], "length_steps", version, float(r["n_steps"])) for r in rows))
    n2 = write_jsonl(SIGNALS / "length_chars.jsonl", (signal_row(r["run_key"], "length_chars", version, float(r["n_chars"])) for r in rows))
    print(f"length_steps {n1} rows, length_chars {n2} rows, version {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
