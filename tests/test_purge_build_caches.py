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


def test_v2_supersedes_v1_and_refuses_bare_or_marked_parents() -> None:
    from evallab.purge_build_caches import build_setup_sh_v2

    updated = build_setup_sh_v2(PARENT_SETUP)
    assert "purge-build-caches@2" in updated
    assert "purge-build-caches@1" in updated  # @2 embeds the @1 sweep
    with pytest.raises(VariantInvalid, match="already carries purge-build-caches@2"):
        build_setup_sh_v2(updated)
    with pytest.raises(VariantInvalid, match="already carries purge-build-caches@1"):
        build_setup_sh_v2(build_setup_sh(PARENT_SETUP))
    with pytest.raises(VariantInvalid, match="write_blocklist"):
        build_setup_sh_v2("#!/bin/bash\nfail() { exit 1; }\n")


def test_v2_block_targets_project_entries_not_shared_deps() -> None:
    from evallab.purge_build_caches import LANGUAGE_SECTIONS, shell_block_v2

    block = shell_block_v2()
    for token in (
        "GOMODCACHE",
        "GOCACHE",
        "cargo/registry",
        ".m2/repository",
        "npm_config_cache",
        "YARN_CACHE_FOLDER",
        "pnpm store",
        "pip cache remove",
    ):
        assert token in block
    assert set(LANGUAGE_SECTIONS) == {
        "python-pip",
        "go-mod",
        "go-build",
        "rust-target",
        "rust-registry",
        "maven-m2",
        "gradle-project",
        "npm",
        "yarn",
        "pnpm",
    }
    # Shared dependency dirs are never removed wholesale.
    assert 'rm -rf "$CWD/.venv"' not in block
    assert 'rm -rf "$CWD/node_modules"' not in block
    assert 'rm -rf "$CWD/vendor"' not in block
    for line in block.splitlines():
        if "site-packages" in line:
            assert "rm -rf" not in line
    # Every language section fails setup when its entries survive.
    assert block.count("left ") >= 6


def test_v2_generated_setup_parses_as_shell(tmp_path: Path) -> None:
    import subprocess

    from evallab.purge_build_caches import build_setup_sh_v2

    script = tmp_path / "setup.sh"
    script.write_text(build_setup_sh_v2(PARENT_SETUP), encoding="utf-8")
    proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_v2_block_purges_fixture_caches_without_touching_deps(tmp_path: Path) -> None:
    """Execute the @2 language sections against fixture dirs (no Docker)."""
    import shutil
    import subprocess

    from evallab.purge_build_caches import LANG_BLOCK

    if shutil.which("python3") is None:
        pytest.skip("needs python3 for the project-name probes")
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (cwd / "go.mod").write_text("module example.com/fixtureproj\n\ngo 1.21\n", encoding="utf-8")
    # Virtual workspace root: member crate names resolve for the registry purge.
    (cwd / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/fixtureproj"]\n', encoding="utf-8")
    (cwd / "crates" / "fixtureproj").mkdir(parents=True)
    (cwd / "crates" / "fixtureproj" / "Cargo.toml").write_text('[package]\nname = "fixtureproj"\n', encoding="utf-8")
    (cwd / "package.json").write_text('{"name": "fixtureproj"}\n', encoding="utf-8")
    # Poetry-style project (no [project] name): the pip section skips when the
    # pip cache dir is absent instead of failing closed on the name.
    (cwd / "pyproject.toml").write_text('[tool.poetry]\nname = "fixtureproj"\n', encoding="utf-8")
    gomod = tmp_path / "gomod"
    (gomod / "fixtureproj@v1.0.0").mkdir(parents=True)
    (gomod / "otherdep@v2.0.0").mkdir(parents=True)
    gocache = tmp_path / "gocache"
    gocache.mkdir()
    target = cwd / "target"
    target.mkdir()
    reg = tmp_path / "cargo" / "registry" / "src" / "index"
    reg.mkdir(parents=True)
    (reg / "fixtureproj-1.0.0").mkdir()
    (reg / "otherdep-2.0.0").mkdir()
    npm_idx = tmp_path / "npm" / "_cacache" / "index-v5" / "aa"
    npm_idx.mkdir(parents=True)
    (npm_idx / "entry").write_text(
        "make-fetch-happen:request-cache:https://r/fixtureproj/-/fixtureproj-1.tgz\n",
        encoding="utf-8",
    )
    runner = tmp_path / "run.sh"
    runner.write_text(
        "#!/bin/bash\nCWD="
        + str(cwd)
        + '\nfail() { echo "setup: $*" >&2; exit 1; }\n'
        + LANG_BLOCK
        + "\n",
        encoding="utf-8",
    )
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(tmp_path / "home"),
        "GOMODCACHE": str(gomod),
        "GOCACHE": str(gocache),
        "CARGO_HOME": str(tmp_path / "cargo"),
        "npm_config_cache": str(tmp_path / "npm"),
        "PIP_CACHE_DIR": str(tmp_path / "no-pip-cache"),
    }
    (tmp_path / "home").mkdir()
    proc = subprocess.run(
        ["bash", str(runner)], capture_output=True, text=True, timeout=120, env=env
    )
    assert proc.returncode == 0, proc.stderr
    assert not (gomod / "fixtureproj@v1.0.0").exists()
    assert (gomod / "otherdep@v2.0.0").exists()
    assert not gocache.exists()
    assert not target.exists()
    assert not (reg / "fixtureproj-1.0.0").exists()
    assert (reg / "otherdep-2.0.0").exists()
    assert not (tmp_path / "npm" / "_cacache").exists()


def test_v2_block_fails_closed_on_unresolvable_python_name(tmp_path: Path) -> None:
    """A present pip cache with no resolvable project name stops the block."""
    import shutil
    import subprocess

    from evallab.purge_build_caches import LANG_BLOCK

    if shutil.which("python3") is None:
        pytest.skip("needs python3 for the project-name probes")
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (cwd / "pyproject.toml").write_text('[tool.poetry]\nname = "fixtureproj"\n', encoding="utf-8")
    pipcache = tmp_path / "pipcache"
    pipcache.mkdir()
    runner = tmp_path / "run.sh"
    runner.write_text(
        "#!/bin/bash\nCWD="
        + str(cwd)
        + '\nfail() { echo "setup: $*" >&2; exit 1; }\n'
        + LANG_BLOCK
        + "\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["bash", str(runner)],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "PIP_CACHE_DIR": str(pipcache),
        },
    )
    assert proc.returncode != 0
    assert "cannot identify the Python project name" in proc.stderr


def test_v2_block_fails_closed_on_unresolvable_names(tmp_path: Path) -> None:
    """A go.mod without a module line stops the block (fail closed)."""
    import subprocess

    from evallab.purge_build_caches import LANG_BLOCK

    cwd = tmp_path / "repo"
    cwd.mkdir()
    (cwd / "go.mod").write_text("// no module line\n", encoding="utf-8")
    runner = tmp_path / "run.sh"
    runner.write_text(
        "#!/bin/bash\nCWD="
        + str(cwd)
        + '\nfail() { echo "setup: $*" >&2; exit 1; }\n'
        + LANG_BLOCK
        + "\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["bash", str(runner)],
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)},
    )
    assert proc.returncode != 0
    assert "cannot read the module path" in proc.stderr
