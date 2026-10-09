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
    (cwd / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["crates/fixtureproj"]\n', encoding="utf-8"
    )
    (cwd / "crates" / "fixtureproj").mkdir(parents=True)
    (cwd / "crates" / "fixtureproj" / "Cargo.toml").write_text(
        '[package]\nname = "fixtureproj"\n', encoding="utf-8"
    )
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


def test_v2_block_skips_go_without_caches(tmp_path: Path) -> None:
    """A valid go.mod with no cache directories present skips the section."""
    import subprocess

    from evallab.purge_build_caches import LANG_BLOCK

    cwd = tmp_path / "repo"
    cwd.mkdir()
    (cwd / "go.mod").write_text("module example.com/fixtureproj\n", encoding="utf-8")
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
            "GOMODCACHE": str(tmp_path / "no-gomod"),
            "GOCACHE": str(tmp_path / "no-gocache"),
        },
    )
    assert proc.returncode == 0, proc.stderr


def test_v2_block_removes_project_wheels_and_verifies(tmp_path: Path) -> None:
    """The pip section removes listed project wheels (stub pip, no network)."""
    import os
    import shutil
    import subprocess

    from evallab.purge_build_caches import LANG_BLOCK

    real_python = shutil.which("python3")
    if real_python is None:
        pytest.skip("needs python3 for the project-name probes")
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (cwd / "pyproject.toml").write_text('[project]\nname = "fixtureproj"\n', encoding="utf-8")
    entries = tmp_path / "entries.txt"
    entries.write_text(
        "fixtureproj-1.0-py3-none-any.whl\notherdep-2.0-py3-none-any.whl\n", encoding="utf-8"
    )
    stub = tmp_path / "bin" / "python3"
    stub.parent.mkdir()
    stub.write_text(
        "#!/bin/bash\n"
        'if [ "$1 $2 $3" = "-m pip cache" ]; then\n'
        '  case "$4" in\n'
        '    dir) echo "$FAKE_PIP_CACHE";;\n'
        '    list) cat "$FAKE_PIP_ENTRIES";;\n'
        '    remove) grep -v -F -- "${5%\\*}" "$FAKE_PIP_ENTRIES" > "$FAKE_PIP_ENTRIES.new" && mv "$FAKE_PIP_ENTRIES.new" "$FAKE_PIP_ENTRIES";;\n'
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        'exec "$REAL_PYTHON3" "$@"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
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
        "PATH": str(stub.parent) + ":/usr/bin:/bin:/usr/local/bin",
        "HOME": str(tmp_path),
        "REAL_PYTHON3": real_python,
        "FAKE_PIP_CACHE": str(tmp_path),
        "FAKE_PIP_ENTRIES": str(entries),
    }
    (tmp_path / "home").mkdir()
    proc = subprocess.run(
        ["bash", str(runner)], capture_output=True, text=True, timeout=120, env=env
    )
    assert proc.returncode == 0, proc.stderr
    remaining = entries.read_text(encoding="utf-8")
    assert "fixtureproj" not in remaining
    assert "otherdep" in remaining
    # A stub that refuses removal trips the fail-closed verify.
    entries.write_text("fixtureproj-1.0-py3-none-any.whl\n", encoding="utf-8")
    stub.write_text(
        "#!/bin/bash\n"
        'if [ "$1 $2 $3" = "-m pip cache" ]; then\n'
        '  case "$4" in\n'
        '    dir) echo "$FAKE_PIP_CACHE";;\n'
        '    list) cat "$FAKE_PIP_ENTRIES";;\n'
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        'exec "$REAL_PYTHON3" "$@"\n',
        encoding="utf-8",
    )
    os.chmod(stub, 0o755)
    proc = subprocess.run(
        ["bash", str(runner)], capture_output=True, text=True, timeout=120, env=env
    )
    assert proc.returncode != 0
    assert "left pip cache entries" in proc.stderr


def _node_fixture(
    tmp_path: Path, *, test_import: str, scripts: str, asset_ref: bool = False
) -> Path:
    """Git repo fixture: ignored lib/ with stale output, src/, one test."""
    import subprocess

    cwd = tmp_path / "repo"
    (cwd / "src").mkdir(parents=True)
    (cwd / "test").mkdir()
    (cwd / "lib").mkdir()
    (cwd / "lib" / "stale.js").write_text("// stale fixed build output\n", encoding="utf-8")
    src = "export const a = 1;\n"
    if asset_ref:
        src += "export const asset = require('path').join(__dirname, '..', 'lib', 'data.json');\n"
    (cwd / "src" / "a.ts").write_text(src, encoding="utf-8")
    (cwd / "test" / "t.test.ts").write_text(
        f"import {{ a }} from '{test_import}';\nconsole.log(a);\n", encoding="utf-8"
    )
    (cwd / "package.json").write_text(
        '{"name": "fixtureproj", "scripts": {' + scripts + "}}\n", encoding="utf-8"
    )
    (cwd / ".gitignore").write_text("/lib\n", encoding="utf-8")
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q", str(cwd)], check=True, env=env, timeout=60)
    subprocess.run(["git", "-C", str(cwd), "add", "-A"], check=True, env=env, timeout=60)
    subprocess.run(
        ["git", "-C", str(cwd), "commit", "-qm", "base"], check=True, env=env, timeout=60
    )
    return cwd


def _run_node_block(
    tmp_path: Path, cwd: Path, extra_path: str = ""
) -> subprocess.CompletedProcess[str]:
    import subprocess

    from evallab.purge_build_caches import NODE_BUILD_BLOCK

    runner = tmp_path / "run-node.sh"
    runner.write_text(
        "#!/bin/bash\nCWD="
        + str(cwd)
        + '\nfail() { echo "setup: $*" >&2; exit 1; }\n'
        + NODE_BUILD_BLOCK
        + "\n",
        encoding="utf-8",
    )
    return subprocess.run(
        ["bash", str(runner)],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            "PATH": extra_path + "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
        },
    )


def test_v3_supersedes_v2_and_refuses_marked_parents() -> None:
    from evallab.purge_build_caches import build_setup_sh_v2, build_setup_sh_v3

    updated = build_setup_sh_v3(PARENT_SETUP)
    assert "purge-build-caches@3" in updated
    assert "purge-build-caches@2" in updated  # @3 embeds @2
    with pytest.raises(VariantInvalid, match="already carries purge-build-caches@3"):
        build_setup_sh_v3(updated)
    with pytest.raises(VariantInvalid, match="already carries purge-build-caches@2"):
        build_setup_sh_v3(build_setup_sh_v2(PARENT_SETUP))
    with pytest.raises(VariantInvalid, match="already carries purge-build-caches@1"):
        build_setup_sh_v3(build_setup_sh(PARENT_SETUP))
    with pytest.raises(VariantInvalid, match="write_blocklist"):
        build_setup_sh_v3("#!/bin/bash\nfail() { exit 1; }\n")


def test_v3_block_names_build_outputs_and_fail_closed_verifies() -> None:
    from evallab.purge_build_caches import shell_block_v3

    block = shell_block_v3()
    for token in ("check-ignore", "did not regenerate", "needs a build script", "npm run"):
        assert token in block
    for dirname in ("lib", "dist", "build", "out"):
        assert dirname in block


def test_v3_generated_setup_parses_as_shell(tmp_path: Path) -> None:
    import subprocess

    from evallab.purge_build_caches import build_setup_sh_v3

    script = tmp_path / "setup.sh"
    script.write_text(build_setup_sh_v3(PARENT_SETUP), encoding="utf-8")
    proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_v3_deletes_src_tested_build_output(tmp_path: Path) -> None:
    """Tests import from src/: ignored lib/ is deleted, sources stay."""
    import shutil

    if shutil.which("git") is None or shutil.which("python3") is None:
        pytest.skip("needs git and python3")
    cwd = _node_fixture(tmp_path, test_import="../src/a", scripts='"build": "exit 0"')
    proc = _run_node_block(tmp_path, cwd)
    assert proc.returncode == 0, proc.stderr
    assert not (cwd / "lib").exists()
    assert (cwd / "src" / "a.ts").is_file()


def test_v3_rebuilds_grader_used_output(tmp_path: Path) -> None:
    """Tests resolve through lib/: stale output is rebuilt, not kept."""
    import shutil

    if shutil.which("git") is None or shutil.which("python3") is None:
        pytest.skip("needs git and python3")
    cwd = _node_fixture(tmp_path, test_import="../lib/stale", scripts='"build": "make-lib"')
    stub = tmp_path / "bin" / "npm"
    stub.parent.mkdir()
    stub.write_text(
        "#!/bin/bash\n"
        'D="$PWD"\n'
        'prev=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "--prefix" ]; then D="$a"; fi\n'
        '  prev="$a"\n'
        "done\n"
        'mkdir -p "$D/lib"\n'
        'echo "// rebuilt from base" > "$D/lib/stale.js"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    proc = _run_node_block(tmp_path, cwd, extra_path=str(stub.parent) + ":")
    assert proc.returncode == 0, proc.stderr
    rebuilt = (cwd / "lib" / "stale.js").read_text(encoding="utf-8")
    assert "rebuilt from base" in rebuilt
    assert "stale fixed" not in rebuilt


def test_v3_rebuilds_on_runtime_asset_reference(tmp_path: Path) -> None:
    """Tracked src/ reading lib/ at runtime forces the rebuild path."""
    import shutil

    if shutil.which("git") is None or shutil.which("python3") is None:
        pytest.skip("needs git and python3")
    cwd = _node_fixture(
        tmp_path, test_import="../src/a", scripts='"build": "make-lib"', asset_ref=True
    )
    stub = tmp_path / "bin" / "npm"
    stub.parent.mkdir()
    stub.write_text(
        "#!/bin/bash\n"
        'D="$PWD"\n'
        'prev=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "--prefix" ]; then D="$a"; fi\n'
        '  prev="$a"\n'
        "done\n"
        'mkdir -p "$D/lib"\n'
        'echo "// rebuilt from base" > "$D/lib/stale.js"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    proc = _run_node_block(tmp_path, cwd, extra_path=str(stub.parent) + ":")
    assert proc.returncode == 0, proc.stderr
    rebuilt = (cwd / "lib" / "stale.js").read_text(encoding="utf-8")
    assert "rebuilt from base" in rebuilt
    assert "stale fixed" not in rebuilt


def test_v3_fails_closed_without_build_script(tmp_path: Path) -> None:
    """Grader-used output with no build script stops the block."""
    import shutil

    if shutil.which("git") is None or shutil.which("python3") is None:
        pytest.skip("needs git and python3")
    cwd = _node_fixture(tmp_path, test_import="../lib/stale", scripts="")
    proc = _run_node_block(tmp_path, cwd)
    assert proc.returncode != 0
    assert "needs a build script" in proc.stderr
    assert (cwd / "lib" / "stale.js").is_file()  # untouched on failure


def test_v3_skips_without_ignored_output(tmp_path: Path) -> None:
    """No lib/dist/build/out dirs: the section is a silent no-op."""
    import shutil
    import subprocess

    from evallab.purge_build_caches import NODE_BUILD_BLOCK

    if shutil.which("git") is None:
        pytest.skip("needs git")
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (cwd / "package.json").write_text('{"name": "fixtureproj"}\n', encoding="utf-8")
    runner = tmp_path / "run-node.sh"
    runner.write_text(
        "#!/bin/bash\nCWD="
        + str(cwd)
        + '\nfail() { echo "setup: $*" >&2; exit 1; }\n'
        + NODE_BUILD_BLOCK
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
    assert proc.returncode == 0, proc.stderr
