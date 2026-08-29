#!/usr/bin/env python3
"""Stage a target-specific FastMCP wheelhouse and trusted resolver provenance.

Network prepackaging is intentionally separate from task-image builds. The staged
bytes are recorded with ``ResolverProvenance``; the materializer derives the
strict ``--require-hashes`` offline lock from those exact bytes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from evallab.mcp_substrate import (
    FASTMCP_VERSION_CONSTRAINTS,
    ResolverProvenance,
    WheelhouseTarget,
    record_prepackaging_provenance,
)

PROVENANCE_FILENAME = "resolver-provenance.json"
LINUX_CP312_X86_64 = WheelhouseTarget("cp312", "manylinux_2_17_x86_64")
MACOS_CP312_ARM64 = WheelhouseTarget("cp312", "macosx_11_0_arm64")


def default_target() -> WheelhouseTarget:
    return LINUX_CP312_X86_64 if sys.platform.startswith("linux") else MACOS_CP312_ARM64


def stage_command(dest: Path, target: WheelhouseTarget) -> list[str]:
    """Return managed resolver command for the explicit runtime target."""
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
        "--only-binary=:all:",
        "--platform",
        target.platform_tag,
        "--implementation",
        "cp",
        "--python-version",
        target.python_tag.removeprefix("cp"),
        "--abi",
        target.python_tag,
        "--dest",
        str(dest),
        *FASTMCP_VERSION_CONSTRAINTS,
    ]


def ensure_wheelhouse(
    dest: Path,
    *,
    target: WheelhouseTarget | None = None,
    run: Callable[..., object] = subprocess.run,
) -> ResolverProvenance:
    target = target or default_target()
    dest.mkdir(parents=True, exist_ok=True)
    provenance_path = dest / PROVENANCE_FILENAME
    if any(dest.glob("*.whl")) and provenance_path.is_file():
        provenance = ResolverProvenance.from_json(
            json.loads(provenance_path.read_text(encoding="utf-8"))
        )
        if provenance.target == target:
            print(f"wheelhouse already staged for {target}: {dest}")
            return provenance
        for wheel in dest.glob("*.whl"):
            wheel.unlink()
        provenance_path.unlink()

    if any(dest.glob("*.whl")):
        for wheel in dest.glob("*.whl"):
            wheel.unlink()
    run(stage_command(dest, target), check=True)
    provenance = record_prepackaging_provenance(dest, target)
    provenance_path.write_text(
        json.dumps(provenance.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"staged target wheelhouse {target} at {dest} ({len(provenance.wheels)} wheels)")
    return provenance


def main() -> None:
    dest = Path(os.environ.get("FASTMCP_WHEELHOUSE", "/tmp/fastmcp3_wheelhouse"))
    ensure_wheelhouse(dest)


if __name__ == "__main__":
    main()
