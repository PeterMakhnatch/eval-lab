#!/usr/bin/env python3
"""Stage a target-specific FastMCP wheelhouse and trusted resolver provenance.

Network prepackaging is intentionally separate from task-image builds. The staged
bytes are recorded with ``ResolverProvenance``; the materializer derives the
strict ``--require-hashes`` offline lock from those exact bytes.

For the trusted Linux CPython 3.12 manylinux target, staging requests the exact
reviewed inventory (every ``name==version`` pin from the checked-in trusted wheel
manifest) so PyPI cannot silently resolve a newer transitive wheel than the trust
root. Any non-trusted target has no reviewed manifest and is refused explicitly.
"""
from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from evallab.mcp_substrate import (
    SubstrateError,
    ResolverProvenance,
    WheelhouseTarget,
    load_trusted_wheel_manifest,
    record_prepackaging_provenance,
    verify_provenance_wheelhouse,
)

PROVENANCE_FILENAME = "resolver-provenance.json"
LINUX_CP312_X86_64 = WheelhouseTarget("cp312", "manylinux_2_17_x86_64")
MACOS_CP312_ARM64 = WheelhouseTarget("cp312", "macosx_11_0_arm64")


def default_target() -> WheelhouseTarget:
    return LINUX_CP312_X86_64


def trusted_manifest_requirements() -> tuple[str, ...]:
    """Return every ``name==version`` pin from the checked-in trusted wheel manifest.

    Ordering is deterministic (sorted by wheel filename), matching the recorded
    resolver provenance. Requesting this full inventory (not merely ``fastmcp``)
    prevents the resolver from silently selecting a newer transitive wheel than
    the reviewed trust root (e.g. joserfc 1.7.5 when the manifest pins 1.7.4).
    """
    manifest = load_trusted_wheel_manifest()
    return tuple(
        f"{entry['name']}=={entry['version']}"
        for entry in sorted(manifest["wheels"], key=lambda w: w["filename"])
    )


def stage_command(dest: Path, target: WheelhouseTarget) -> list[str]:
    """Return managed resolver command for the explicit runtime target.

    The trusted Linux CPython 3.12 manylinux target is staged from the exact
    reviewed manifest inventory. Any other target has no trusted manifest to pin
    against and is refused explicitly rather than producing an unpinned wheelhouse.
    """
    if target != LINUX_CP312_X86_64:
        raise SubstrateError(
            f"no trusted wheel manifest exists for non-trusted target "
            f"{target.python_tag}/{target.platform_tag}; refusing to stage unpinned wheelhouse"
        )
    requirements = trusted_manifest_requirements()
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
        *requirements,
    ]


def _clear_wheelhouse(dest: Path) -> None:
    """Remove all staged wheels and any recorded provenance from a destination."""
    for wheel in dest.glob("*.whl"):
        wheel.unlink()
    provenance_path = dest / PROVENANCE_FILENAME
    if provenance_path.is_file():
        provenance_path.unlink()


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
            try:
                verify_provenance_wheelhouse(dest, provenance)
            except SubstrateError:
                # Fail closed: an incomplete or drifted staged wheelhouse must not
                # be trusted on the target tag alone; re-stage from the manifest.
                _clear_wheelhouse(dest)
            else:
                print(f"wheelhouse already staged for {target}: {dest}")
                return provenance
        else:
            _clear_wheelhouse(dest)

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
