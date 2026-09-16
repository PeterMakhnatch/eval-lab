"""Tests for the python toolbox packaging and validation module."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from evallab.toolbox import (
    TOOLBOX_CONTAINER_PATH,
    TOOLBOX_DESCRIPTOR_NAME,
    TOOLBOX_JOB_RELATIVE_PATH,
    TOOLBOX_MAX_BYTES,
    TOOLBOX_REQUIRED_CALLABLES,
    TOOLBOX_SCRIPT_NAME,
    TOOLBOX_SKILL_MD,
    TOOLBOX_SKILL_NAME,
    compute_skill_digest,
    retain_toolbox_evidence,
    stage_toolbox,
    validate_toolbox_code,
    validate_toolbox_source,
)

VALID_TOOLBOX_CODE = """
def read_window(file, start=1, end=None):
    return {"file": file, "start": start, "end": end}

def smart_grep(pattern, path=".", max_matches=20, context=2):
    return {"pattern": pattern, "path": path}

def check_output(file):
    return {"file": file, "valid": True}
"""


def test_validate_toolbox_code_valid():
    """Valid toolbox code defining required callables passes validation."""
    validate_toolbox_code(VALID_TOOLBOX_CODE)


def test_validate_toolbox_code_missing_callable():
    """Missing any required callable raises ValueError."""
    code_missing_check = """
def read_window(file, start=1, end=None):
    return {}

def smart_grep(pattern, path=".", max_matches=20, context=2):
    return {}
"""
    with pytest.raises(ValueError, match="missing required callables"):
        validate_toolbox_code(code_missing_check)


def test_validate_toolbox_code_non_string():
    """Passing non-string raises TypeError."""
    with pytest.raises(TypeError, match="must be a string"):
        validate_toolbox_code(12345)  # type: ignore[arg-type]


def test_validate_toolbox_code_empty():
    """Empty or whitespace-only code raises ValueError."""
    with pytest.raises(ValueError, match="non-empty"):
        validate_toolbox_code("   \n\t  ")


def test_validate_toolbox_code_too_large():
    """Code exceeding 65536 bytes raises ValueError."""
    padded = VALID_TOOLBOX_CODE + "\n# " + "A" * (TOOLBOX_MAX_BYTES + 10)
    with pytest.raises(ValueError, match="exceeds maximum allowed size"):
        validate_toolbox_code(padded)


def test_validate_toolbox_code_syntax_error():
    """Code with syntax error raises ValueError."""
    broken = "def smart_grep(: broken syntax"
    with pytest.raises(ValueError, match="syntax error"):
        validate_toolbox_code(broken)


def test_validate_toolbox_code_no_host_execution():
    """Validating code never executes it on the host."""
    host_bomb = VALID_TOOLBOX_CODE + """
# If executed on host, this would raise RuntimeError
raise RuntimeError("HOST CODE EXECUTION DETECTED!")
"""
    # Validation must NOT execute the code; it parses AST only.
    validate_toolbox_code(host_bomb)


def test_validate_toolbox_source_valid(tmp_path: Path):
    """Valid toolbox source file is read, validated, and hashed."""
    script = tmp_path / "repl_tools.py"
    script.write_text(VALID_TOOLBOX_CODE, encoding="utf-8")
    expected_sha = f"sha256:{hashlib.sha256(VALID_TOOLBOX_CODE.encode('utf-8')).hexdigest()}"

    raw_bytes, actual_sha = validate_toolbox_source(script, expected_sha)
    assert raw_bytes == VALID_TOOLBOX_CODE.encode("utf-8")
    assert actual_sha == expected_sha


def test_validate_toolbox_source_symlink_rejected(tmp_path: Path):
    """Symlinks to toolbox source files are strictly rejected."""
    real_script = tmp_path / "real_tools.py"
    real_script.write_text(VALID_TOOLBOX_CODE, encoding="utf-8")
    symlink_script = tmp_path / "symlink_tools.py"
    symlink_script.symlink_to(real_script)

    with pytest.raises(ValueError, match="must not be a symlink"):
        validate_toolbox_source(symlink_script)


def test_validate_toolbox_source_missing(tmp_path: Path):
    """Missing file raises ValueError."""
    with pytest.raises(ValueError, match="does not exist"):
        validate_toolbox_source(tmp_path / "absent.py")


def test_validate_toolbox_source_not_py_extension(tmp_path: Path):
    """Non-.py extension raises ValueError."""
    txt = tmp_path / "repl_tools.txt"
    txt.write_text(VALID_TOOLBOX_CODE, encoding="utf-8")
    with pytest.raises(ValueError, match="must have a .py extension"):
        validate_toolbox_source(txt)


def test_validate_toolbox_source_escapes_repo(tmp_path: Path):
    """Toolbox file escaping repo_root raises ValueError."""
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text(VALID_TOOLBOX_CODE, encoding="utf-8")

    with pytest.raises(ValueError, match="escapes repository root"):
        validate_toolbox_source(outside, repo_root=repo)


def test_validate_toolbox_source_sha256_mismatch(tmp_path: Path):
    """Digest mismatch raises ValueError."""
    script = tmp_path / "repl_tools.py"
    script.write_text(VALID_TOOLBOX_CODE, encoding="utf-8")
    wrong_sha = "sha256:" + "0" * 64

    with pytest.raises(ValueError, match="digest mismatch"):
        validate_toolbox_source(script, wrong_sha)


def test_stage_toolbox_creates_immutable_content_addressed_bundle(tmp_path: Path):
    """stage_toolbox creates read-only bundle and returns correct metadata."""
    script = tmp_path / "repl_tools.py"
    script.write_text(VALID_TOOLBOX_CODE, encoding="utf-8")
    expected_sha = f"sha256:{hashlib.sha256(VALID_TOOLBOX_CODE.encode('utf-8')).hexdigest()}"

    staging_root = tmp_path / "staging"
    bundle_dir, metadata = stage_toolbox(script, expected_sha, staging_root=staging_root)

    assert bundle_dir.name == TOOLBOX_SKILL_NAME
    assert bundle_dir.parent.name == f"sha256-{expected_sha.removeprefix('sha256:')}"
    assert (bundle_dir / TOOLBOX_DESCRIPTOR_NAME).is_file()
    assert (bundle_dir / TOOLBOX_SCRIPT_NAME).is_file()

    # Invariant SKILL.md content
    assert (bundle_dir / TOOLBOX_DESCRIPTOR_NAME).read_text(encoding="utf-8") == TOOLBOX_SKILL_MD
    assert (bundle_dir / TOOLBOX_SCRIPT_NAME).read_text(encoding="utf-8") == VALID_TOOLBOX_CODE

    # Check files are read-only (0o444)
    desc_mode = os.stat(bundle_dir / TOOLBOX_DESCRIPTOR_NAME).st_mode & 0o777
    script_mode = os.stat(bundle_dir / TOOLBOX_SCRIPT_NAME).st_mode & 0o777
    assert desc_mode == 0o444
    assert script_mode == 0o444

    # Metadata validation
    assert metadata["skill_name"] == TOOLBOX_SKILL_NAME
    assert metadata["script_name"] == TOOLBOX_SCRIPT_NAME
    assert metadata["artifact_path"] == TOOLBOX_JOB_RELATIVE_PATH
    assert metadata["container_path"] == TOOLBOX_CONTAINER_PATH
    assert metadata["toolbox_sha256"] == expected_sha
    assert metadata["sha256"] == expected_sha
    assert metadata["content_digest"] == expected_sha
    assert metadata["artifact_bytes"] == VALID_TOOLBOX_CODE
    assert metadata["skill_digest"].startswith("sha256:")
    assert metadata["staged_bundle_path"] == str(bundle_dir)


def test_compute_skill_digest_deterministic(tmp_path: Path):
    """compute_skill_digest produces exact deterministic digest."""
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("desc\n", encoding="utf-8")
    (skill_dir / "repl_tools.py").write_text("code\n", encoding="utf-8")

    digest1 = compute_skill_digest(skill_dir)
    digest2 = compute_skill_digest(skill_dir)
    assert digest1 == digest2
    assert digest1.startswith("sha256:")


def test_retain_toolbox_evidence(tmp_path: Path):
    """retain_toolbox_evidence copies skill bundle under job directory."""
    job_dir = tmp_path / "job-1"
    job_dir.mkdir()

    script = tmp_path / "repl_tools.py"
    script.write_text(VALID_TOOLBOX_CODE, encoding="utf-8")
    bundle_dir, metadata = stage_toolbox(script, staging_root=tmp_path / "staging")

    retained = retain_toolbox_evidence(job_dir, bundle_dir, metadata)
    assert retained == job_dir / "toolbox" / TOOLBOX_SKILL_NAME
    assert (retained / TOOLBOX_DESCRIPTOR_NAME).is_file()
    assert (retained / TOOLBOX_SCRIPT_NAME).is_file()
    assert (retained / TOOLBOX_SCRIPT_NAME).read_text(encoding="utf-8") == VALID_TOOLBOX_CODE
