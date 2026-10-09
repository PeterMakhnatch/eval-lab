"""purge-build-caches@1 removes regenerable caches and flags foreign pointers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from evallab.purge_build_caches import (
    build_setup_sh,
    foreign_pth_leaks,
    shell_block,
)
from evallab.task_variants import VariantInvalid

PARENT_SETUP = "#!/bin/bash\nfail() { exit 1; }\nwrite_blocklist() { :; }\nwrite_blocklist\n"


def test_block_inserts_once_after_the_blocklist_anchor() -> None:
    updated = build_setup_sh(PARENT_SETUP)
    assert updated.index("purge-build-caches@1") > updated.index("write_blocklist() { :; }")
    assert updated.count("purge-build-caches@1") >= 1
    with pytest.raises(VariantInvalid, match="already carries"):
        build_setup_sh(updated)


def test_setup_without_the_anchor_is_refused() -> None:
    with pytest.raises(VariantInvalid, match="write_blocklist"):
        build_setup_sh("#!/bin/bash\nfail() { exit 1; }\n")


def test_block_targets_caches_not_dependencies_or_project_copies() -> None:
    block = shell_block()
    for name in ("__pycache__", ".pytest_cache", ".mypy_cache", ".vitest"):
        assert name in block
    assert "*.pyc" in block
    # Dependency dirs and project copies are never removed wholesale.
    assert 'rm -rf "$CWD/.venv"' not in block
    assert 'rm -rf "$CWD/node_modules"' not in block
    assert 'rm -rf "$CWD/build"' not in block
    assert "*egg-info*" not in block
    # The verify step fails setup when a cache survives.
    assert "_cache_left" in block
    assert "left caches" in block


def test_generated_setup_parses_as_shell(tmp_path: Path) -> None:
    script = tmp_path / "setup.sh"
    script.write_text(build_setup_sh(PARENT_SETUP), encoding="utf-8")
    proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_worktree_pointing_pth_is_clean_but_foreign_targets_flagged(tmp_path: Path) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    site = tmp_path / "site-packages"
    site.mkdir()
    (site / "python_miio.pth").write_text("/repo\n".replace("/repo", str(cwd)), encoding="utf-8")
    assert foreign_pth_leaks(cwd, [site]) == []
    (site / "stale.pth").write_text("/opt/fixed-copy\n", encoding="utf-8")
    (site / "old.egg-link").write_text("/opt/old\n", encoding="utf-8")
    (site / "local.egg-link").write_text(str(cwd) + "\n", encoding="utf-8")
    leaks = foreign_pth_leaks(cwd, [site])
    assert any("stale.pth:/opt/fixed-copy" in leak for leak in leaks)
    assert any("old.egg-link:/opt/old" in leak for leak in leaks)
    assert not any("local.egg-link" in leak for leak in leaks)


def test_import_lines_and_relative_pth_entries_are_ignored(tmp_path: Path) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    site = tmp_path / "site-packages"
    site.mkdir()
    (site / "hook.pth").write_text("import sitecustomize\n./relative\n", encoding="utf-8")
    assert foreign_pth_leaks(cwd, [site]) == []
    assert foreign_pth_leaks(cwd, [tmp_path / "missing"]) == []
