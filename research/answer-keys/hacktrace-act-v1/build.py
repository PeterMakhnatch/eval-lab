"""Rebuild this pinned external trace key; --fetch downloads public data only."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from external_traces import build_main  # noqa: E402

if __name__ == "__main__":
    build_main(HERE)
