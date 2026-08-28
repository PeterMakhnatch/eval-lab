#!/usr/bin/env python3
"""CLI utility to materialize MCP Recovery tasks."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "library" / "benchmarks" / "mcp-recovery-v1"
sys.path.insert(0, str(ROOT))

from materialize import main  # noqa: E402

if __name__ == "__main__":
    main()
