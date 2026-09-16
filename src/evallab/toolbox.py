"""Python toolbox packaging and validation for Harbor agent environments.

Provides AST-based validation, invariant SKILL.md packaging, and content-addressed
staging for candidate `repl_tools.py` artifacts without host execution.
"""

from __future__ import annotations

import ast
import hashlib
import os
import stat
from pathlib import Path
from typing import Any

TOOLBOX_SKILL_NAME = "repl-tools"
TOOLBOX_SCRIPT_NAME = "repl_tools.py"
TOOLBOX_DESCRIPTOR_NAME = "SKILL.md"
TOOLBOX_CONTAINER_PATH = f"/harbor/skills/{TOOLBOX_SKILL_NAME}/{TOOLBOX_SCRIPT_NAME}"
TOOLBOX_JOB_RELATIVE_PATH = f"toolbox/{TOOLBOX_SKILL_NAME}/{TOOLBOX_SCRIPT_NAME}"
TOOLBOX_MAX_BYTES = 65536  # 64 KiB
TOOLBOX_REQUIRED_CALLABLES = frozenset({"smart_grep", "read_window", "check_output"})
TOOLBOX_SUPPORTED_AGENTS = frozenset({"oracle", "nop", "zai-opencode"})

TOOLBOX_SKILL_MD = f"""---
name: {TOOLBOX_SKILL_NAME}
description: Standard library Python toolbox for bounded file inspection, regex grep, and output checking.
---

# {TOOLBOX_SKILL_NAME}

Standard library Python toolbox available at `{TOOLBOX_CONTAINER_PATH}`.

## CLI Usage
- `python3 {TOOLBOX_CONTAINER_PATH} read <file> [--start N] [--end M]`
- `python3 {TOOLBOX_CONTAINER_PATH} grep <pattern> [path] [--max-matches N] [--context N]`
- `python3 {TOOLBOX_CONTAINER_PATH} check <file> [--format auto|json|text]`

## Python API
```python
import sys
sys.path.insert(0, "/harbor/skills/{TOOLBOX_SKILL_NAME}")
from repl_tools import read_window, smart_grep, check_output
```
"""


def compute_skill_digest(skill_dir: Path) -> str:
    """Digest a two-file toolbox bundle using Harbor's native skill algorithm."""
    names = {TOOLBOX_DESCRIPTOR_NAME, TOOLBOX_SCRIPT_NAME}
    if any(path.is_symlink() for path in (skill_dir, *skill_dir.parents)):
        raise ValueError("toolbox bundle path must not contain symlinks")
    found = set()
    for path in skill_dir.iterdir():
        if path.name not in names or path.is_symlink() or not path.is_file():
            raise ValueError("toolbox bundle contains unexpected files")
        found.add(path.name)
    if found != names:
        raise ValueError("toolbox bundle is incomplete")
    hasher = hashlib.sha256()
    for name in sorted(names):
        with (skill_dir / name).open("rb") as stream:
            data = stream.read(TOOLBOX_MAX_BYTES + 1)
        if len(data) > TOOLBOX_MAX_BYTES:
            raise ValueError("toolbox bundle member exceeds size limit")
        hasher.update(name.encode())
        hasher.update(b"\0")
        hasher.update(hashlib.sha256(data).hexdigest().encode())
        hasher.update(b"\0")
    return f"sha256:{hasher.hexdigest()}"


def validate_toolbox_code(source: str) -> None:
    """Validate candidate toolbox Python source code via AST analysis only.

    Ensures the code:
    1. Is a string.
    2. Encodes to <= 65536 bytes UTF-8.
    3. Is non-empty.
    4. Parses as valid Python syntax without host execution.
    5. Defines the required top-level callables: smart_grep, read_window, check_output.

    Raises:
        TypeError: If source is not a string.
        ValueError: If size exceeds limit, syntax is invalid, or required API is missing.
    """
    if not isinstance(source, str):
        raise TypeError(f"toolbox source must be a string, got {type(source).__name__}")
    encoded = source.encode("utf-8")
    if len(encoded) > TOOLBOX_MAX_BYTES:
        raise ValueError(
            f"toolbox source exceeds maximum allowed size ({TOOLBOX_MAX_BYTES} bytes): "
            f"{len(encoded)} bytes"
        )
    if not source.strip():
        raise ValueError("toolbox source must be non-empty")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(f"toolbox syntax error: {exc}") from exc

    callables = {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    missing = TOOLBOX_REQUIRED_CALLABLES - callables
    if missing:
        raise ValueError(f"toolbox source missing required callables: {sorted(missing)}")


def validate_toolbox_source(
    source_path: Path | str,
    expected_sha256: str | None = None,
    *,
    repo_root: Path | None = None,
) -> tuple[bytes, str]:
    """Read bounded, non-symlink source bytes without importing candidate code."""
    path = Path(source_path)
    if path.suffix != ".py":
        raise ValueError("toolbox source must have a .py extension")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("toolbox source path must not be a symlink")
    if repo_root is not None and not path.resolve().is_relative_to(repo_root.resolve()):
        raise ValueError("toolbox path escapes repository root")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("toolbox source must be a regular file")
            raw_bytes = stream.read(TOOLBOX_MAX_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"toolbox source unavailable: {exc}") from exc
    if len(raw_bytes) > TOOLBOX_MAX_BYTES:
        raise ValueError("toolbox source exceeds maximum allowed size")
    try:
        validate_toolbox_code(raw_bytes.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError("toolbox source must be valid UTF-8") from exc
    digest = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError("toolbox digest mismatch")
    return raw_bytes, digest


def stage_toolbox(
    source_path: Path | str,
    expected_sha256: str | None = None,
    *,
    staging_root: Path,
    repo_root: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Stage an immutable content-addressed repl-tools skill bundle for Harbor.

    Validates the source file and packages it alongside the invariant SKILL.md
    descriptor into a directory named `repl-tools` under the staging root.

    Returns:
        tuple[Path, dict[str, Any]]: (staged_bundle_dir, metadata_dict)
    """
    raw_bytes, actual_sha256 = validate_toolbox_source(
        source_path, expected_sha256, repo_root=repo_root
    )
    bundle_dir = staging_root / actual_sha256.removeprefix("sha256:") / TOOLBOX_SKILL_NAME
    _retain_bundle(
        bundle_dir,
        {
            TOOLBOX_DESCRIPTOR_NAME: TOOLBOX_SKILL_MD.encode("utf-8"),
            TOOLBOX_SCRIPT_NAME: raw_bytes,
        },
    )
    return bundle_dir, {
        "schema_version": 1,
        "skill_name": TOOLBOX_SKILL_NAME,
        "container_path": TOOLBOX_CONTAINER_PATH,
        "artifact_path": TOOLBOX_JOB_RELATIVE_PATH,
        "sha256": actual_sha256,
        "skill_digest": compute_skill_digest(bundle_dir),
    }


def retain_toolbox_evidence(
    job_dir: Path,
    staged_bundle: Path,
    metadata: dict[str, Any],
) -> Path:
    """Retain the exact executed skill bytes, never reconstruct them from metadata."""
    if compute_skill_digest(staged_bundle) != metadata["skill_digest"]:
        raise ValueError("staged toolbox changed before evidence retention")
    files = {
        name: (staged_bundle / name).read_bytes()
        for name in (TOOLBOX_DESCRIPTOR_NAME, TOOLBOX_SCRIPT_NAME)
    }
    if "sha256:" + hashlib.sha256(files[TOOLBOX_SCRIPT_NAME]).hexdigest() != metadata["sha256"]:
        raise ValueError("staged Python bytes differ from approved toolbox")
    target = job_dir / "toolbox" / TOOLBOX_SKILL_NAME
    _retain_bundle(target, files)
    return target


def _retain_bundle(directory: Path, files: dict[str, bytes]) -> None:
    if any(path.is_symlink() for path in (directory, *directory.parents)):
        raise ValueError("toolbox bundle path must not be a symlink")
    if directory.exists():
        if {path.name for path in directory.iterdir()} != set(files):
            raise ValueError("immutable toolbox bundle has unexpected files")
        for name, data in files.items():
            path = directory / name
            if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
                raise ValueError("immutable toolbox bundle content mismatch")
        return
    directory.mkdir(parents=True)
    for name, data in files.items():
        with (directory / name).open("xb") as stream:
            stream.write(data)
        (directory / name).chmod(0o444)
