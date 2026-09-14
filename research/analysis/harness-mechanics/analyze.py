#!/usr/bin/env python3
"""Entrypoint for Harness Mechanics Lab analysis command."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

# Add the directory containing harness_mechanics to sys.path
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# Add repo root and repo root / src to sys.path if needed
_REPO_ROOT = _HERE.parents[2]
if str(_REPO_ROOT / "src") not in sys.path and (_REPO_ROOT / "src").is_dir():
    sys.path.insert(0, str(_REPO_ROOT / "src"))
if str(_REPO_ROOT) not in sys.path and _REPO_ROOT.is_dir():
    sys.path.insert(0, str(_REPO_ROOT))


def main() -> int:
    """Run the CLI entrypoint."""
    cli = importlib.import_module("harness_mechanics.cli")
    return cli.main()


if __name__ == "__main__":
    sys.exit(main())
