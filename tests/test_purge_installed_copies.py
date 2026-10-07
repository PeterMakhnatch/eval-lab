"""purge-installed-copies@1 deletes the project's copies and nothing else."""

from __future__ import annotations

from pathlib import Path

import pytest

from evallab.purge_container import (
    is_our_editable,
    link_worktree,
    project_name,
    remaining_leaks,
    remove_copies,
)
from evallab.purge_installed_copies import build_setup_sh, shell_block
from evallab.task_variants import VariantInvalid


def _project(root: Path, name: str = "responses") -> Path:
    cwd = root / "repo"
    package = cwd / name
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("BASE = True\n", encoding="utf-8")
    (cwd / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "1.2.3"\n',
        encoding="utf-8",
    )
    return cwd


def _leak(site: Path, name: str, body: str = "LEAK = True\n") -> None:
    package = site / name
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(body, encoding="utf-8")
    (site / f"{name}-1.0.dist-info").mkdir()


def test_purge_removes_build_and_installed_copy_and_keeps_other_packages(tmp_path: Path) -> None:
    cwd = _project(tmp_path)
    build = cwd / "build" / "lib" / "responses"
    build.mkdir(parents=True)
    (build / "__init__.py").write_text("LEAK = True\n", encoding="utf-8")
    (cwd / "responses.egg-info").mkdir()
    site = tmp_path / "site-packages"
    _leak(site, "responses")
    (site / "responses.egg-link").write_text("/opt/old\n", encoding="utf-8")
    _leak(site, "requests", "THIRD = True\n")

    removed = remove_copies(cwd, [site])

    assert not (cwd / "build").exists()
    assert not (cwd / "responses.egg-info").exists()
    assert (cwd / "responses" / "__init__.py").read_text(encoding="utf-8") == "BASE = True\n"
    assert not (site / "responses").exists()
    assert not (site / "responses-1.0.dist-info").exists()
    assert not (site / "responses.egg-link").exists()
    assert (site / "requests" / "__init__.py").read_text(encoding="utf-8") == "THIRD = True\n"
    assert (site / "requests-1.0.dist-info").is_dir()
    assert remaining_leaks(cwd, [site]) == []
    assert any("responses" in path for path in removed)
    assert not any(
        path.endswith("/requests") or path.endswith("/requests-1.0.dist-info") for path in removed
    )


def test_editable_metadata_is_not_a_leak_and_a_real_copy_is(tmp_path: Path) -> None:
    cwd = _project(tmp_path)
    site = tmp_path / "site-packages"
    site.mkdir()
    editable = site / "responses-0.20.0.dist-info"
    editable.mkdir()
    (editable / "direct_url.json").write_text(
        '{"url": "file://' + str(cwd.resolve()) + '", "dir_info": {"editable": true}}\n',
        encoding="utf-8",
    )
    assert is_our_editable(editable, cwd)
    assert remaining_leaks(cwd, [site]) == []
    leaked = site / "responses-9.9.dist-info"
    leaked.mkdir()
    assert any(path.endswith("responses-9.9.dist-info") for path in remaining_leaks(cwd, [site]))


def test_link_fallback_points_site_packages_at_the_worktree(tmp_path: Path) -> None:
    cwd = _project(tmp_path, "pre_commit")
    (cwd / "pyproject.toml").write_text(
        '[project]\nname = "pre-commit"\nversion = "2.14.1"\n',
        encoding="utf-8",
    )
    site = tmp_path / "site-packages"
    site.mkdir()
    linked = link_worktree(cwd, [site])
    assert Path(linked).resolve() == (cwd / "pre_commit").resolve()
    meta = (site / "pre_commit-2.14.1.dist-info" / "METADATA").read_text(encoding="utf-8")
    assert "Name: pre-commit\n" in meta
    assert "Version: 2.14.1\n" in meta
    assert is_our_editable(site / "pre_commit-2.14.1.dist-info", cwd)
    assert remaining_leaks(cwd, [site]) == []
    assert (cwd / "pre_commit" / "__init__.py").read_text(encoding="utf-8") == "BASE = True\n"


def test_purge_matches_hyphenated_distribution_metadata(tmp_path: Path) -> None:
    cwd = _project(tmp_path, "pre_commit")
    (cwd / "pyproject.toml").write_text('[project]\nname = "pre-commit"\n', encoding="utf-8")
    site = tmp_path / "site-packages"
    _leak(site, "pre_commit")
    (site / "pre_commit-2.14.1.dist-info").mkdir()
    (site / "pre-commit-2.14.1.dist-info").mkdir()

    remove_copies(cwd, [site])

    assert project_name(cwd) == "pre-commit"
    assert not (site / "pre_commit").exists()
    assert not (site / "pre_commit-2.14.1.dist-info").exists()
    assert not (site / "pre-commit-2.14.1.dist-info").exists()
    assert (cwd / "pre_commit" / "__init__.py").is_file()


def test_missing_project_name_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="cannot identify the project name"):
        remove_copies(tmp_path, [])


def test_setup_block_inserts_once_and_installs_offline() -> None:
    parent = "#!/bin/bash\nfail() { exit 1; }\nwrite_blocklist() { :; }\nwrite_blocklist\n"
    updated = build_setup_sh(parent)
    assert updated.index("purge-installed-copies@1") < updated.index("\nwrite_blocklist\n")
    block = shell_block()
    assert "--no-deps" in block
    assert "--no-index" in block
    assert "--no-build-isolation" in block
    assert "def remove_copies" in block
    assert 'python3 - "$CWD" link' in block
    assert block.count('python3 - "$CWD" remove') == 1
    assert block.count('python3 - "$CWD" verify') == 1
    with pytest.raises(VariantInvalid, match="already carries"):
        build_setup_sh(updated)


def test_setup_without_the_anchor_is_refused() -> None:
    with pytest.raises(VariantInvalid, match="write_blocklist"):
        build_setup_sh("#!/bin/bash\nfail() { exit 1; }\n")
