"""purge-installed-copies@2 neutralizes copies in place and nothing else."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from evallab.purge_container import (
    v2_action_neutralize,
    v2_action_verify,
    v2_base_map,
    v2_collect_matches,
    v2_match_rel,
    v2_neutralize_matches,
    v2_project_tops,
)
from evallab.purge_installed_copies import (
    MARKER,
    MARKER_V2,
    V1_BLOCK_START,
    build_setup_sh,
    build_setup_sh_v2,
    shell_block,
    shell_block_v2,
)
from evallab.task_variants import VariantInvalid

TOP = "v2acme_pkg"


def _repo(root: Path, files: dict[str, str]) -> Path:
    cwd = root / "repo"
    for rel, body in files.items():
        path = cwd / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return cwd


def _tracked(files: dict[str, str]) -> list[str]:
    return sorted(files)


def _pkg_files(base_body: str = "BASE = True\n") -> dict[str, str]:
    return {
        f"{TOP}/__init__.py": base_body,
        f"{TOP}/core.py": "VALUE = 1\n",
        f"{TOP}/data/config.json": "{}\n",
        f"{TOP}/tests/test_core.py": "def test_x(): pass\n",
        "pyproject.toml": '[project]\nname = "v2acme-pkg"\n',
        "setup.py": "from setuptools import setup; setup()\n",
    }


def _leak(site: Path, body: str = "LEAK = True\n") -> Path:
    package = site / TOP
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(body, encoding="utf-8")
    (package / "core.py").write_text(body, encoding="utf-8")
    (site / f"{TOP}-1.0.dist-info").mkdir()
    (site / f"{TOP}-1.0.dist-info" / "METADATA").write_text("Name: v2acme\n", encoding="utf-8")
    return package


def _roots(site: Path) -> tuple[list[Path], list[Path], list[tuple[Path, str]], list[Path]]:
    return [site], [], [], []


def test_tops_split_packages_modules_and_skip_tests_and_setup() -> None:
    files = _pkg_files()
    packages, modules = v2_project_tops(Path("/none"), tracked=_tracked(files))
    assert packages == {TOP}
    assert modules == set()

    single = {"pyupgrade.py": "X = 1\n", "setup.py": "setup()\n", "tests/test_p.py": "pass\n"}
    packages, modules = v2_project_tops(Path("/none"), tracked=_tracked(single))
    assert packages == set()
    assert modules == {"pyupgrade"}


def test_src_layout_maps_to_installed_top() -> None:
    tracked = ["src/mypkg/__init__.py", "src/mypkg/core.py", "src/mypkg/tests/test_c.py"]
    packages, modules = v2_project_tops(Path("/none"), tracked=tracked)
    assert packages == {"mypkg"}
    assert modules == set()


def test_base_map_covers_tests_and_package_data(tmp_path: Path) -> None:
    files = _pkg_files()
    cwd = _repo(tmp_path, files)
    packages, modules = v2_project_tops(cwd, tracked=_tracked(files))
    base = v2_base_map(cwd, packages, modules, tracked=_tracked(files))
    assert base[f"{TOP}/__init__.py"] == b"BASE = True\n"
    assert base[f"{TOP}/data/config.json"] == b"{}\n"
    # Installed trees ship tests: overwriting them with BASE beats deleting
    # them as misclassified fix-added files (measured on theano).
    assert base[f"{TOP}/tests/test_core.py"] == b"def test_x(): pass\n"
    assert "setup.py" not in base


def test_doc_trees_are_not_tops() -> None:
    tracked = [
        f"{TOP}/__init__.py",
        f"{TOP}/core.py",
        "doc/conf.py",
        "doc/ext/thing.py",
        "examples/demo.py",
    ]
    packages, _ = v2_project_tops(Path("/none"), tracked=tracked)
    assert packages == {TOP}


def test_match_rel_requires_top_at_root() -> None:
    packages = {TOP}
    assert v2_match_rel(f"{TOP}/core.py", packages, set()) == f"{TOP}/core.py"
    assert v2_match_rel(f"setuptools/_vendor/{TOP}/more.py", packages, set()) is None
    assert v2_match_rel("other/mod.py", packages, set()) is None
    assert v2_match_rel("pyupgrade.py", set(), {"pyupgrade"}) == "pyupgrade.py"
    assert v2_match_rel(f"{TOP}/pyupgrade.py", set(), {"pyupgrade"}) is None


def test_neutralize_overwrites_and_drops_pyc_but_keeps_metadata(tmp_path: Path) -> None:
    files = _pkg_files()
    cwd = _repo(tmp_path, files)
    site = tmp_path / "site-packages"
    site.mkdir()
    package = _leak(site)
    cache = package / "__pycache__"
    cache.mkdir()
    (cache / "__init__.cpython-312.pyc").write_bytes(b"stale")
    (cache / "other.cpython-312.pyc").write_bytes(b"keep")
    (site / "requests").mkdir()
    (site / "requests" / "__init__.py").write_text("THIRD = True\n", encoding="utf-8")

    packages, modules = v2_project_tops(cwd, tracked=_tracked(files))
    base = v2_base_map(cwd, packages, modules, tracked=_tracked(files))
    matches = v2_collect_matches(cwd, packages, modules, *_roots(site))
    assert {path.as_posix() for path, _, _ in matches} == {
        (package / "__init__.py").as_posix(),
        (package / "core.py").as_posix(),
    }
    log, changed = v2_neutralize_matches(cwd, matches, base)
    assert changed == 2
    assert (package / "__init__.py").read_bytes() == b"BASE = True\n"
    assert (package / "core.py").read_bytes() == b"VALUE = 1\n"
    assert not (cache / "__init__.cpython-312.pyc").exists()
    assert (cache / "other.cpython-312.pyc").exists()
    assert (site / "requests" / "__init__.py").read_text(encoding="utf-8") == "THIRD = True\n"
    assert (site / f"{TOP}-1.0.dist-info" / "METADATA").is_file()
    assert any("neutralized" in line for line in log)


def test_fix_added_py_deleted_and_compiled_artifact_kept(tmp_path: Path) -> None:
    files = _pkg_files()
    cwd = _repo(tmp_path, files)
    site = tmp_path / "site-packages"
    site.mkdir()
    _leak(site)
    added = site / TOP / "fix_added.py"
    added.write_text("FIX = True\n", encoding="utf-8")
    compiled = site / TOP / "_fast.cpython-312-x86_64-linux-gnu.so"
    compiled.write_bytes(b"\x7fELF fake")

    packages, modules = v2_project_tops(cwd, tracked=_tracked(files))
    base = v2_base_map(cwd, packages, modules, tracked=_tracked(files))
    matches = v2_collect_matches(cwd, packages, modules, *_roots(site))
    log, _ = v2_neutralize_matches(cwd, matches, base)
    assert not added.exists()
    assert compiled.is_file()
    assert any("deleted fix-added" in line for line in log)
    assert any("kept compiled artifact" in line for line in log)


def test_vendored_copy_inside_another_package_untouched(tmp_path: Path) -> None:
    files = _pkg_files()
    cwd = _repo(tmp_path, files)
    site = tmp_path / "site-packages"
    vendor = site / "setuptools" / "_vendor" / TOP
    vendor.mkdir(parents=True)
    (vendor / "more.py").write_text("LEAK = True\n", encoding="utf-8")

    packages, modules = v2_project_tops(cwd, tracked=_tracked(files))
    matches = v2_collect_matches(cwd, packages, modules, *_roots(site))
    assert matches == []


def test_single_module_matched_under_site_and_build_roots(tmp_path: Path) -> None:
    files = {"pyupgrade.py": "BASE = True\n", "setup.py": "setup()\n"}
    cwd = _repo(tmp_path, files)
    site = tmp_path / "site-packages"
    site.mkdir()
    (site / "pyupgrade.py").write_text("LEAK = True\n", encoding="utf-8")
    build_lib = cwd / "build" / "lib"
    build_lib.mkdir(parents=True)
    (build_lib / "pyupgrade.py").write_text("LEAK = True\n", encoding="utf-8")

    packages, modules = v2_project_tops(cwd, tracked=_tracked(files))
    assert modules == {"pyupgrade"}
    base = v2_base_map(cwd, packages, modules, tracked=_tracked(files))
    matches = v2_collect_matches(cwd, packages, modules, [site], [build_lib], [], [])
    assert len(matches) == 2
    log, changed = v2_neutralize_matches(cwd, matches, base)
    assert changed == 2
    assert (site / "pyupgrade.py").read_bytes() == b"BASE = True\n"
    assert (build_lib / "pyupgrade.py").read_bytes() == b"BASE = True\n"
    assert any("neutralized" in line for line in log)


def test_stray_checkout_parent_neutralized(tmp_path: Path) -> None:
    files = _pkg_files()
    cwd = _repo(tmp_path, files)
    stray_parent = tmp_path / "testbed"
    stray = stray_parent / TOP
    stray.mkdir(parents=True)
    (stray / "__init__.py").write_text("LEAK = True\n", encoding="utf-8")
    (stray / "core.py").write_text("LEAK = True\n", encoding="utf-8")

    packages, modules = v2_project_tops(cwd, tracked=_tracked(files))
    base = v2_base_map(cwd, packages, modules, tracked=_tracked(files))
    matches = v2_collect_matches(cwd, packages, modules, [], [], [(stray_parent, TOP)], [])
    assert len(matches) == 2
    _, changed = v2_neutralize_matches(cwd, matches, base)
    assert changed == 2
    assert (stray / "__init__.py").read_bytes() == b"BASE = True\n"


def test_symlink_into_worktree_is_skipped(tmp_path: Path) -> None:
    files = _pkg_files()
    cwd = _repo(tmp_path, files)
    site = tmp_path / "site-packages"
    site.mkdir()
    (site / TOP).symlink_to(cwd / TOP, target_is_directory=True)

    packages, modules = v2_project_tops(cwd, tracked=_tracked(files))
    matches = v2_collect_matches(cwd, packages, modules, *_roots(site))
    assert matches == []


def _fake_imports(results: Mapping[str, tuple[int, str]]) -> Callable[[str], tuple[int, str, str]]:
    """Fake isolated-runner: ``top -> (rc, stderr)`` for ``import <top>`` scripts."""

    def run(script: str) -> tuple[int, str, str]:
        assert script.startswith("import ")
        top = script[len("import ") :]
        code, stderr = results[top]
        return code, "", stderr

    return run


def test_action_roundtrip_verifies_and_gates_import(tmp_path: Path) -> None:
    files = _pkg_files()
    cwd = _repo(tmp_path, files)
    site = tmp_path / "site-packages"
    site.mkdir()
    _leak(site)
    state = tmp_path / "state"
    state.mkdir()

    v2_action_neutralize(
        cwd,
        tracked=_tracked(files),
        roots=_roots(site),
        probe_run=_fake_imports({TOP: (0, "")}),
        state_dir=state,
    )
    imported: list[str] = []

    def fake_import(script: str) -> tuple[int, str, str]:
        imported.append(script)
        return 0, "", ""

    v2_action_verify(
        cwd,
        tracked=_tracked(files),
        roots=_roots(site),
        import_run=fake_import,
        state_dir=state,
    )
    assert imported == ["import " + TOP]


def test_action_noop_skips_import_check(tmp_path: Path) -> None:
    files = _pkg_files()
    cwd = _repo(tmp_path, files)
    site = tmp_path / "site-packages"
    site.mkdir()
    state = tmp_path / "state"
    state.mkdir()

    v2_action_neutralize(
        cwd,
        tracked=_tracked(files),
        roots=_roots(site),
        probe_run=_fake_imports({TOP: (1, "ModuleNotFoundError")}),
        state_dir=state,
    )

    def exploding_import(script: str) -> tuple[int, str, str]:
        raise AssertionError("import must not run when nothing changed")

    v2_action_verify(
        cwd,
        tracked=_tracked(files),
        roots=_roots(site),
        import_run=exploding_import,
        state_dir=state,
    )


def test_action_verify_fails_on_residual_non_base(tmp_path: Path) -> None:
    files = _pkg_files()
    cwd = _repo(tmp_path, files)
    site = tmp_path / "site-packages"
    site.mkdir()
    package = _leak(site)
    state = tmp_path / "state"
    state.mkdir()

    v2_action_neutralize(
        cwd,
        tracked=_tracked(files),
        roots=_roots(site),
        probe_run=_fake_imports({TOP: (1, "ModuleNotFoundError")}),
        state_dir=state,
    )
    (package / "core.py").write_text("LEAK = True\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="non-base bytes remain"):
        v2_action_verify(
            cwd,
            tracked=_tracked(files),
            roots=_roots(site),
            state_dir=state,
        )


def test_action_verify_import_failure_fails_closed(tmp_path: Path) -> None:
    files = _pkg_files()
    cwd = _repo(tmp_path, files)
    site = tmp_path / "site-packages"
    site.mkdir()
    _leak(site)
    state = tmp_path / "state"
    state.mkdir()
    v2_action_neutralize(
        cwd,
        tracked=_tracked(files),
        roots=_roots(site),
        probe_run=_fake_imports({TOP: (0, "")}),
        state_dir=state,
    )

    def failing_import(script: str) -> tuple[int, str, str]:
        return 1, "", "ModuleNotFoundError: No module named 'v2acme_pkg'"

    with pytest.raises(SystemExit, match="worked before but fails after"):
        v2_action_verify(
            cwd,
            tracked=_tracked(files),
            roots=_roots(site),
            import_run=failing_import,
            state_dir=state,
        )


def test_action_verify_skips_import_when_pre_import_failed(tmp_path: Path) -> None:
    """The theano shape: base never parses, so post-import failure is skipped."""
    files = _pkg_files()
    cwd = _repo(tmp_path, files)
    site = tmp_path / "site-packages"
    site.mkdir()
    _leak(site)
    state = tmp_path / "state"
    state.mkdir()
    v2_action_neutralize(
        cwd,
        tracked=_tracked(files),
        roots=_roots(site),
        probe_run=_fake_imports({TOP: (1, "SyntaxError: multiple exception types")}),
        state_dir=state,
    )

    def failing_import(script: str) -> tuple[int, str, str]:
        raise AssertionError("import must not run when pre-import failed")

    v2_action_verify(
        cwd,
        tracked=_tracked(files),
        roots=_roots(site),
        import_run=failing_import,
        state_dir=state,
    )


def test_installed_tests_are_overwritten_not_deleted(tmp_path: Path) -> None:
    files = _pkg_files()
    cwd = _repo(tmp_path, files)
    site = tmp_path / "site-packages"
    site.mkdir()
    package = _leak(site)
    installed_tests = package / "tests"
    installed_tests.mkdir()
    (installed_tests / "__init__.py").write_text("LEAK = True\n", encoding="utf-8")
    (installed_tests / "test_core.py").write_text("LEAK = True\n", encoding="utf-8")

    packages, modules = v2_project_tops(cwd, tracked=_tracked(files))
    base = v2_base_map(cwd, packages, modules, tracked=_tracked(files))
    matches = v2_collect_matches(cwd, packages, modules, *_roots(site))
    _, changed = v2_neutralize_matches(cwd, matches, base)
    assert (installed_tests / "test_core.py").read_bytes() == b"def test_x(): pass\n"
    assert installed_tests.is_dir()
    assert changed == len(matches)


def test_action_no_tops_is_a_noop(tmp_path: Path) -> None:
    cwd = _repo(tmp_path, {"README.md": "hi\n"})
    state = tmp_path / "state"
    state.mkdir()
    v2_action_neutralize(cwd, tracked=["README.md"], roots=([], [], [], []), state_dir=state)
    v2_action_verify(cwd, tracked=["README.md"], roots=([], [], [], []), state_dir=state)
    assert (
        json.loads((state / "purge-installed-copies-v2-prestate.json").read_text())["changed"] == 0
    )


def test_setup_block_v2_inserts_once_and_supersedes_v1() -> None:
    parent = "#!/bin/bash\nfail() { exit 1; }\nwrite_blocklist() { :; }\nwrite_blocklist\n"
    updated, superseded = build_setup_sh_v2(parent)
    assert MARKER_V2 in updated
    assert superseded is False
    with pytest.raises(VariantInvalid, match="already carries"):
        build_setup_sh_v2(updated)
    with pytest.raises(VariantInvalid, match="write_blocklist"):
        build_setup_sh_v2("#!/bin/bash\nfail() { exit 1; }\n")

    carrying = build_setup_sh(parent)
    assert MARKER in carrying
    migrated, superseded = build_setup_sh_v2(carrying)
    assert superseded is True
    assert V1_BLOCK_START not in migrated
    assert MARKER_V2 in migrated


def test_v2_shell_block_has_both_steps() -> None:
    block = shell_block_v2()
    assert '"$CWD" neutralize' in block
    assert '"$CWD" verify2' in block
    assert "pip install" not in block


def test_v2_block_survives_bash_syntax_check(tmp_path: Path) -> None:
    parent = "#!/bin/bash\nfail() { exit 1; }\nwrite_blocklist() { :; }\nwrite_blocklist\n"
    updated, _ = build_setup_sh_v2(parent)
    script = tmp_path / "setup.sh"
    script.write_text(updated, encoding="utf-8")
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - CI always ships bash
        pytest.skip()
    import subprocess

    proc = subprocess.run([bash, "-n", str(script)], capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")


def test_v1_block_shape_still_detected() -> None:
    assert shell_block().startswith(V1_BLOCK_START)
