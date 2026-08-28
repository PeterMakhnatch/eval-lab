#!/usr/bin/env python3
"""Download the hash-locked FastMCP sidecar wheelhouse when absent.

The project uv venv does not include pip. Downloads go through uv's managed
pip (`uv run --with pip --no-project`) so they never call `sys.executable -m pip`.
"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from evallab.mcp_substrate import FASTMCP_SIDECAR_REQUIREMENTS_TXT


def download_command(requirements: Path, dest: Path) -> list[str]:
    """Return a uv-managed pip download argv that keeps --require-hashes."""
    return [
        "uv",
        "run",
        "--with",
        "pip",
        "--no-project",
        "python",
        "-m",
        "pip",
        "download",
        "--require-hashes",
        "-r",
        str(requirements),
        "-d",
        str(dest),
    ]


def ensure_wheelhouse(
    dest: Path,
    *,
    run: Callable[..., object] = subprocess.run,
) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.glob("*.whl")):
        print(f"wheelhouse already present: {dest}")
        return dest
    reqs = dest / "requirements.txt"
    reqs.write_text(FASTMCP_SIDECAR_REQUIREMENTS_TXT, encoding="utf-8")
    cmd = download_command(reqs, dest)
    run(cmd, check=True)
    print(f"downloaded wheelhouse to {dest} ({len(list(dest.glob('*.whl')))} wheels)")
    return dest


def main() -> None:
    dest = Path(os.environ.get("FASTMCP_WHEELHOUSE", "/tmp/fastmcp3_wheelhouse"))
    ensure_wheelhouse(dest)


if __name__ == "__main__":
    main()
