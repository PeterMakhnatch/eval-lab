"""Verify installed GEPA code, not a mutable package-version label."""

from __future__ import annotations

import hashlib
import importlib.metadata
from pathlib import Path

COMMIT = "0632cdb5dcc052e690eab439e1b4a7e3e9cfe407"
VERSION = "0.1.4"
SOURCE_TREE_SHA256 = "sha256:e8bc3facf961884faca73e7a434773231b3c1ba0eb79688072bf65bc1a268dd1"


def verify_release() -> dict[str, str]:
    import gepa  # ty: ignore[unresolved-import]

    root = Path(gepa.__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    observed = "sha256:" + digest.hexdigest()
    version = importlib.metadata.version("gepa")
    if version != VERSION or observed != SOURCE_TREE_SHA256:
        raise RuntimeError("Installed GEPA code does not match the qualified release pin")
    return {"commit": COMMIT, "version": version, "python_source_tree_sha256": observed}
