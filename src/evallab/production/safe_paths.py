"""Confine journal-supplied filesystem paths to explicitly allowed roots.

Event payloads are data, not authority: the control plane may only read or copy
task packages, trial directories and exploit bundles that live under a root the
operator configured. Symlinks are resolved first, so a link cannot escape.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


class PathOutsideRootsError(PermissionError):
    """A payload path resolved outside every allowed root."""


def resolve_within(roots: Sequence[str | Path], value: str | Path) -> Path:
    """Return the resolved existing-or-not path, requiring it to sit under a root.

    Relative values are tried against each root in order. An absolute value must
    already be inside a root. Raises PathOutsideRootsError otherwise.
    """
    if not roots:
        raise PathOutsideRootsError("no allowed roots are configured")
    text = str(value)
    if not text.strip() or "\x00" in text:
        raise PathOutsideRootsError("path is blank or contains NUL")
    resolved_roots = [Path(root).resolve() for root in roots]
    candidate = Path(text).expanduser()
    candidates = (
        [candidate.resolve()]
        if candidate.is_absolute()
        else [(root / candidate).resolve() for root in resolved_roots]
    )
    for path in candidates:
        if any(path == root or root in path.parents for root in resolved_roots):
            return path
    raise PathOutsideRootsError(f"path is outside the allowed roots: {text}")


def read_bounded_text(path: Path, limit: int = 65536) -> tuple[str, bool]:
    """Read at most ``limit`` bytes as UTF-8 (replacing bad bytes); report truncation."""
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    return data[:limit].decode("utf-8", errors="replace"), len(data) > limit
