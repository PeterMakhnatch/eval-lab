#!/usr/bin/env python3
"""Stage a target-specific FastMCP wheelhouse and trusted resolver provenance.

Network prepackaging is intentionally separate from task-image builds. The staged
bytes are recorded with ``ResolverProvenance``; the materializer derives the
strict ``--require-hashes`` offline lock from those exact bytes.

For the trusted Linux CPython 3.12 manylinux target, staging requests the exact
reviewed inventory (every ``name==version`` pin from the checked-in trusted wheel
manifest) so PyPI cannot silently resolve a newer transitive wheel than the trust
root. Any non-trusted target has no reviewed manifest and is refused explicitly.

The resolver is the project's locked ``pip`` (pinned exactly in ``pyproject.toml``
and ``uv.lock``); the command runs that locked environment's ``python -m pip`` via
``sys.executable`` instead of live-resolving ``pip`` at staging time, and the
executing resolver version is verified fail-closed before any staging. The
destination and provenance paths are guarded against symlinks and
attacker-owned/group-or-world-writable directories before any cache reuse or
write.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from evallab.mcp_substrate import (
    ResolverProvenance,
    SubstrateError,
    WheelhouseTarget,
    load_trusted_wheel_manifest,
    record_prepackaging_provenance,
    verify_provenance_wheelhouse,
)

PROVENANCE_FILENAME = "resolver-provenance.json"
LINUX_CP312_X86_64 = WheelhouseTarget("cp312", "manylinux_2_17_x86_64")
MACOS_CP312_ARM64 = WheelhouseTarget("cp312", "macosx_11_0_arm64")

# Locked resolver pinned exactly in the dev dependency group (uv.lock commits its
# wheel hash). We execute the locked environment's pip, never a live resolution.
RESOLVER_PIP_PIN = "pip==26.1.2"


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


def _require_trusted_target(target: WheelhouseTarget) -> None:
    """Reject any target without a reviewed trusted wheel manifest."""
    if target != LINUX_CP312_X86_64:
        raise SubstrateError(
            f"no trusted wheel manifest exists for non-trusted target "
            f"{target.python_tag}/{target.platform_tag}; refusing to stage unpinned wheelhouse"
        )


def _require_locked_resolver() -> None:
    """Fail closed unless the executing pip is exactly the locked resolver version.

    A direct invocation must not bypass the locked-resolver claim: if the
    environment's pip is missing or a different version than the pinned
    ``RESOLVER_PIP_PIN``, staging is refused before any command is produced or run.
    """
    import importlib.metadata as metadata

    expected = RESOLVER_PIP_PIN.split("==", 1)[1]
    try:
        installed = metadata.version("pip")
    except metadata.PackageNotFoundError:
        raise SubstrateError(
            f"locked resolver {RESOLVER_PIP_PIN} is not installed in this environment"
        ) from None
    if installed != expected:
        raise SubstrateError(
            f"resolver pip version mismatch: installed {installed!r}, expected {expected!r} "
            f"({RESOLVER_PIP_PIN}); refusing to stage with an unverified resolver"
        )


def stage_command(dest: Path, target: WheelhouseTarget) -> list[str]:
    """Return the locked-resolver download command for the explicit runtime target.

    The trusted Linux CPython 3.12 manylinux target is staged from the exact
    reviewed manifest inventory via the locked environment's ``python -m pip``
    (``sys.executable``), so the resolver is immutable rather than live-resolved.
    Any other target has no trusted manifest to pin against and is refused.
    """
    _require_trusted_target(target)
    _require_locked_resolver()
    requirements = trusted_manifest_requirements()
    return [
        sys.executable,
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


def _validate_destination(dest: Path) -> None:
    """Fail closed on a symlinked, attacker-owned, or group/world-writable destination.

    An absent destination is created atomically with owner-only (0700) permissions
    via ``exist_ok=False``; if creation races with ``FileExistsError``, the now
    existing path is re-validated by ``lstat`` before any chmod. An existing
    destination must be a real directory owned by the current euid and not group-
    or world-writable, so a predictable ``/tmp`` pre-created directory (or a
    symlink) cannot be used to redirect or tamper with staging.
    """
    for _ in range(2):
        try:
            st = os.lstat(dest)
        except FileNotFoundError:
            try:
                os.makedirs(dest, mode=0o700, exist_ok=False)
            except FileExistsError:
                continue  # raced with creation; re-validate the existing path
            st = os.lstat(dest)
        if stat.S_ISLNK(st.st_mode):
            raise SubstrateError(f"wheelhouse destination is a symlink: {dest.as_posix()!r}")
        if not stat.S_ISDIR(st.st_mode):
            raise SubstrateError(
                f"wheelhouse destination is not a real directory: {dest.as_posix()!r}"
            )
        if st.st_uid != os.geteuid():
            raise SubstrateError(
                f"wheelhouse destination is not owned by the current user: {dest.as_posix()!r}"
            )
        if st.st_mode & 0o022:
            raise SubstrateError(
                f"wheelhouse destination is group/world-writable: {dest.as_posix()!r}"
            )
        if not (st.st_mode & 0o200):
            raise SubstrateError(
                f"wheelhouse destination is not owner-writable: {dest.as_posix()!r}"
            )
        return
    raise SubstrateError(
        f"wheelhouse destination creation raced repeatedly: {dest.as_posix()!r}"
    )


def _validate_provenance_path(provenance_path: Path) -> None:
    """Reject a symlinked resolver-provenance final component before read/write."""
    if provenance_path.is_symlink():
        raise SubstrateError(
            f"resolver-provenance path is a symlink: {provenance_path.as_posix()!r}"
        )


def _clear_wheelhouse(dest: Path) -> None:
    """Remove all staged wheels and any recorded provenance from a destination."""
    for wheel in dest.glob("*.whl"):
        if wheel.is_symlink():
            raise SubstrateError(f"wheelhouse entry is a symlink: {wheel.name!r}")
        wheel.unlink()
    provenance_path = dest / PROVENANCE_FILENAME
    if provenance_path.is_file():
        if provenance_path.is_symlink():
            raise SubstrateError(
                f"resolver-provenance path is a symlink: {provenance_path.as_posix()!r}"
            )
        provenance_path.unlink()


def ensure_wheelhouse(
    dest: Path,
    *,
    target: WheelhouseTarget | None = None,
    run: Callable[..., object] = subprocess.run,
) -> ResolverProvenance:
    target = target or default_target()
    # Reject every non-trusted target up front, before any cache branch, so a
    # crafted cached provenance cannot bypass stage_command's rejection.
    _require_trusted_target(target)
    # Enforce the locked resolver fail-closed before any cache reuse/clear/write.
    _require_locked_resolver()
    _validate_destination(dest)
    provenance_path = dest / PROVENANCE_FILENAME
    _validate_provenance_path(provenance_path)
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
            if wheel.is_symlink():
                raise SubstrateError(f"wheelhouse entry is a symlink: {wheel.name!r}")
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
