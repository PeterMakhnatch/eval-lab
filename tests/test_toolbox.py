"""Artifact integrity and host non-execution boundaries for toolbox candidates."""

from __future__ import annotations

import hashlib

import pytest

from evallab.toolbox import (
    TOOLBOX_MAX_BYTES,
    retain_toolbox_evidence,
    stage_toolbox,
    validate_toolbox_code,
    validate_toolbox_source,
)

CODE = "def read_window(*args): pass\ndef smart_grep(*args): pass\ndef check_output(*args): pass\n"


def source(tmp_path):
    path = tmp_path / "repl_tools.py"
    path.write_text(CODE)
    return path, "sha256:" + hashlib.sha256(CODE.encode()).hexdigest()


@pytest.mark.parametrize(
    "code", ["", "def broken(:", "def read_window(): pass", CODE + "#" * TOOLBOX_MAX_BYTES]
)
def test_static_validation_refuses_unusable_or_oversized_code(code):
    with pytest.raises(ValueError):
        validate_toolbox_code(code)


def test_validation_never_imports_or_executes_candidate(tmp_path):
    marker = tmp_path / "must-not-exist"
    candidate = CODE + f"\nfrom pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
    validate_toolbox_code(candidate)
    assert not marker.exists()


@pytest.mark.parametrize("parent_link", [False, True])
def test_source_symlink_is_rejected_even_inside_repo(tmp_path, parent_link):
    directory = tmp_path / "real"
    directory.mkdir()
    path, digest = source(directory)
    link = tmp_path / ("link" if parent_link else "link.py")
    link.symlink_to(directory if parent_link else path)
    with pytest.raises(ValueError):
        validate_toolbox_source(
            link / "repl_tools.py" if parent_link else link, digest, repo_root=tmp_path
        )


def test_source_cannot_escape_repo_or_change_after_binding(tmp_path):
    path, digest = source(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ValueError):
        validate_toolbox_source(path, digest, repo_root=repo)
    path.write_text(CODE + "# changed\n")
    with pytest.raises(ValueError):
        validate_toolbox_source(path, digest, repo_root=tmp_path)


def test_existing_staging_is_reused_without_rewriting_or_repairing_it(tmp_path):
    path, digest = source(tmp_path)
    bundle, metadata = stage_toolbox(path, digest, staging_root=tmp_path / "staging")
    script = bundle / "repl_tools.py"
    before = script.stat().st_mtime_ns
    repeated, _ = stage_toolbox(path, digest, staging_root=tmp_path / "staging")
    assert repeated == bundle and script.stat().st_mtime_ns == before
    script.chmod(0o644)
    script.write_text("tampered")
    with pytest.raises(ValueError):
        stage_toolbox(path, digest, staging_root=tmp_path / "staging")
    with pytest.raises(ValueError):
        retain_toolbox_evidence(tmp_path / "job", bundle, metadata)
    assert script.read_text() == "tampered"


def test_retention_preserves_executed_snapshot_not_later_source(tmp_path):
    path, digest = source(tmp_path)
    bundle, metadata = stage_toolbox(path, digest, staging_root=tmp_path / "staging")
    path.write_text(CODE + "# different later source\n")
    retained = retain_toolbox_evidence(tmp_path / "job", bundle, metadata)
    assert (retained / "repl_tools.py").read_text() == CODE
    assert (
        "sha256:" + hashlib.sha256((retained / "repl_tools.py").read_bytes()).hexdigest() == digest
    )
