#!/usr/bin/env python3
"""Download the hash-locked FastMCP sidecar wheelhouse when absent."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from evallab.mcp_substrate import FASTMCP_SIDECAR_REQUIREMENTS_TXT


def main() -> None:
    dest = Path(os.environ.get("FASTMCP_WHEELHOUSE", "/tmp/fastmcp3_wheelhouse"))
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.glob("*.whl")):
        print(f"wheelhouse already present: {dest}")
        return
    reqs = dest / "requirements.txt"
    reqs.write_text(FASTMCP_SIDECAR_REQUIREMENTS_TXT, encoding="utf-8")
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--require-hashes",
        "-r",
        str(reqs),
        "-d",
        str(dest),
    ]
    subprocess.run(cmd, check=True)
    print(f"downloaded wheelhouse to {dest} ({len(list(dest.glob('*.whl')))} wheels)")


if __name__ == "__main__":
    main()
