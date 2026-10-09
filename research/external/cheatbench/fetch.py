#!/usr/bin/env python3
"""Fetch the pinned CheatBench parquet into this directory ($0).

Verifies the sha256 in ``PIN`` / ``src/evallab/cheatbench.py``; refuses to
replace an unrecognized file. Run from anywhere::

    python research/external/cheatbench/fetch.py [--dest DIR]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[3] / "src"))

from evallab.cheatbench import FILENAME, REVISION, SHA256, fetch, source_url  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dest", default=str(HERE / "full.parquet"))
    args = parser.parse_args(argv)
    print(f"fetching {source_url()}\n  revision {REVISION}\n  sha256 {SHA256}")
    path = fetch(args.dest)
    print(f"ok: {path} ({path.stat().st_size} bytes)")
    assert path.name == Path(FILENAME).name
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
