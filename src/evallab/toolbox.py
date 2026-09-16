"""Python toolbox packaging and validation for Harbor agent environments.

Provides AST-based validation, invariant SKILL.md packaging, and content-addressed
staging for candidate `repl_tools.py` artifacts without host execution.
"""

from __future__ import annotations

import ast
import hashlib
import os
import shutil
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
    """Compute the deterministic skill digest identical to Harbor's compute_skill_digest.

    Hashes sorted relative paths and file content sha256 digests separated by null bytes.
    """
    hasher = hashlib.sha256()
    for file_path in sorted(path for path in skill_dir.rglob("*") if path.is_file()):
        relative_path = file_path.relative_to(skill_dir).as_posix()
        content_digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        hasher.update(relative_path.encode())
        hasher.update(b"\0")
        hasher.update(content_digest.encode())
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
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    missing = TOOLBOX_REQUIRED_CALLABLES - callables
    if missing:
        raise ValueError(
            f"toolbox source missing required callables: {sorted(missing)}"
        )


def validate_toolbox_source(
    source_path: Path | str,
    expected_sha256: str | None = None,
    *,
    repo_root: Path | None = None,
) -> tuple[bytes, str]:
    """Validate a toolbox source file on disk without host execution.

    Verifies that the file:
    1. Is not a symlink.
    2. Exists and is a regular file.
    3. Has a '.py' extension.
    4. Does not escape repo_root if provided.
    5. Is within TOOLBOX_MAX_BYTES and decodes as valid UTF-8.
    6. Passes validate_toolbox_code.
    7. Matches expected_sha256 if provided.

    Returns:
        tuple[bytes, str]: (raw_bytes, computed_sha256_digest)
    """
    path = Path(source_path)
    if path.is_symlink():
        raise ValueError(f"toolbox source path must not be a symlink: {source_path}")
    if not path.exists():
        raise ValueError(f"toolbox source file does not exist: {source_path}")
    if not path.is_file():
        raise ValueError(f"toolbox source path is not a regular file: {source_path}")
    if path.suffix != ".py":
        raise ValueError(f"toolbox source must have a .py extension: {source_path}")

    if repo_root is not None:
        root_resolved = repo_root.resolve()
        path_resolved = path.resolve()
        if path_resolved != root_resolved and root_resolved not in path_resolved.parents:
            raise ValueError(f"toolbox path escapes repository root: {source_path}")

    raw_bytes = path.read_bytes()
    if len(raw_bytes) > TOOLBOX_MAX_BYTES:
        raise ValueError(
            f"toolbox source exceeds maximum allowed size ({TOOLBOX_MAX_BYTES} bytes): "
            f"{len(raw_bytes)} bytes"
        )
    try:
        source_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"toolbox source must be valid UTF-8: {exc}") from exc

    validate_toolbox_code(source_text)

    actual_sha256 = f"sha256:{hashlib.sha256(raw_bytes).hexdigest()}"
    if expected_sha256 is not None and actual_sha256 != expected_sha256:
        raise ValueError(
            f"toolbox digest mismatch: expected {expected_sha256}, got {actual_sha256}"
        )

    return raw_bytes, actual_sha256


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
    hex_digest = actual_sha256.removeprefix("sha256:")
    bundle_dir = staging_root / f"sha256-{hex_digest}" / TOOLBOX_SKILL_NAME
    bundle_dir.mkdir(parents=True, exist_ok=True)

    descriptor_path = bundle_dir / TOOLBOX_DESCRIPTOR_NAME
    script_path = bundle_dir / TOOLBOX_SCRIPT_NAME

    descriptor_bytes = TOOLBOX_SKILL_MD.encode("utf-8")
    descriptor_path.write_bytes(descriptor_bytes)
    script_path.write_bytes(raw_bytes)

    # Make files read-only to ensure immutability
    os.chmod(descriptor_path, 0o444)
    os.chmod(script_path, 0o444)

    skill_digest = compute_skill_digest(bundle_dir)

    metadata: dict[str, Any] = {
        "schema_version": 1,
        "skill_name": TOOLBOX_SKILL_NAME,
        "script_name": TOOLBOX_SCRIPT_NAME,
        "descriptor_name": TOOLBOX_DESCRIPTOR_NAME,
        "container_path": TOOLBOX_CONTAINER_PATH,
        "artifact_path": TOOLBOX_JOB_RELATIVE_PATH,
        "toolbox_path": str(source_path),
        "toolbox_sha256": actual_sha256,
        "sha256": actual_sha256,
        "content_digest": actual_sha256,
        "skill_digest": skill_digest,
        "byte_count": len(raw_bytes),
        "artifact_bytes": raw_bytes.decode("utf-8"),
        "skill_descriptor": TOOLBOX_SKILL_MD,
        "staged_bundle_path": str(bundle_dir),
    }

    return bundle_dir, metadata


def retain_toolbox_evidence(
    job_dir: Path,
    staged_bundle: Path,
    metadata: dict[str, Any],
) -> Path:
    """Retain the staged toolbox skill bundle under the job directory for CAS evidence.

    Copies the descriptor and script into `job_dir / "toolbox" / "repl-tools"`
    and marks them read-only.
    """
    target_dir = job_dir / "toolbox" / TOOLBOX_SKILL_NAME
    target_dir.mkdir(parents=True, exist_ok=True)
    target_descriptor = target_dir / TOOLBOX_DESCRIPTOR_NAME
    target_script = target_dir / TOOLBOX_SCRIPT_NAME

    target_descriptor.write_text(metadata.get("skill_descriptor", TOOLBOX_SKILL_MD), encoding="utf-8")
    target_script.write_text(metadata.get("artifact_bytes", ""), encoding="utf-8")

    os.chmod(target_descriptor, 0o444)
    os.chmod(target_script, 0o444)

    return target_dir
