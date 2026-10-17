"""purge-installed-copies@2 neutralizes copies in place and nothing else."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from evallab.purge_container import (
    V2PackageOwnership,
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
    SETUP_PYTHON_PATH,
    SETUP_REL,
    V1_BLOCK_START,
    build_setup_sh,
    build_setup_sh_v2,
    derive_purge_installed_copies_v2,
    shell_block,
    shell_block_v2,
)
from evallab.strip_future_history import pack_setup, unpack_setup_blob
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


def test_tops_split_packages_modules_and_skip_tests_and_setup(tmp_path: Path) -> None:
    files = _pkg_files()
    cwd = _repo(tmp_path, files)
    packages, modules = v2_project_tops(cwd, tracked=_tracked(files))
    assert TOP in packages.roots
    assert packages.tops == {TOP}
    assert modules == set()

    single = {"pyupgrade.py": "X = 1\n", "setup.py": "setup()\n", "tests/test_p.py": "pass\n"}
    packages, modules = v2_project_tops(Path("/none"), tracked=_tracked(single))
    assert packages.tops == set()
    assert modules == {"pyupgrade"}


def test_src_layout_maps_to_installed_top(tmp_path: Path) -> None:
    files = {
        "src/mypkg/__init__.py": "BASE = True\n",
        "src/mypkg/core.py": "VALUE = 1\n",
        "src/mypkg/tests/test_c.py": "def test_c(): pass\n",
    }
    cwd = _repo(tmp_path, files)
    packages, modules = v2_project_tops(cwd, tracked=_tracked(files))
    assert packages.roots == {"mypkg"}
    assert packages.tops == {"mypkg"}
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


def test_doc_trees_are_not_tops(tmp_path: Path) -> None:
    files = {
        f"{TOP}/__init__.py": "BASE = True\n",
        f"{TOP}/core.py": "VALUE = 1\n",
        "doc/conf.py": "CONFIG = True\n",
        "doc/ext/thing.py": "EXTENSION = True\n",
        "examples/demo.py": "DEMO = True\n",
    }
    cwd = _repo(tmp_path, files)
    packages, _ = v2_project_tops(cwd, tracked=_tracked(files))
    assert packages.tops == {TOP}


def test_match_rel_requires_top_at_root() -> None:
    packages = V2PackageOwnership(frozenset({TOP}))
    assert v2_match_rel(f"{TOP}/core.py", packages, set()) == f"{TOP}/core.py"
    assert v2_match_rel(f"setuptools/_vendor/{TOP}/more.py", packages, set()) is None
    assert v2_match_rel("other/mod.py", packages, set()) is None
    empty = V2PackageOwnership(frozenset())
    assert v2_match_rel("pyupgrade.py", empty, {"pyupgrade"}) == "pyupgrade.py"
    assert v2_match_rel(f"{TOP}/pyupgrade.py", empty, {"pyupgrade"}) is None


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


@pytest.mark.parametrize("layout", ["", "src/"], ids=["flat", "src"])
@pytest.mark.parametrize("style", ["pep420", "pkgutil", "pkg_resources", "setup.py", "setup.cfg"])
def test_namespace_action_roundtrip_preserves_sibling_distributions(
    tmp_path: Path, layout: str, style: str
) -> None:
    namespace = "sharedco_ns"
    owned = f"{namespace}/services/events"
    installed_base = {
        f"{owned}/__init__.py": "BASE = True\n",
        f"{owned}/core.py": "VALUE = 1\n",
        f"{owned}/data/config.json": '{"base": true}\n',
        f"{owned}/tests/test_core.py": "def test_core(): pass\n",
        f"{owned}_v2/__init__.py": "BASE = True\n",
        f"{owned}_v2/client.py": "VALUE = 2\n",
        f"{namespace}/services/helpers.py": "BASE = True\n",
    }
    if style != "pep420":
        initializer = ""
        if style == "pkgutil":
            initializer = (
                "from pkgutil import extend_path as widen\n"
                "__path__ = widen(__path__, __name__)\n"
            )
        elif style == "pkg_resources":
            initializer = (
                "try:\n"
                "    __import__('pkg_resources').declare_namespace(__name__)\n"
                "except ImportError:\n"
                "    from pkgutil import extend_path\n"
                "    __path__ = extend_path(__path__, __name__)\n"
            )
        installed_base[f"{namespace}/__init__.py"] = initializer
        installed_base[f"{namespace}/services/__init__.py"] = initializer
    files = {layout + rel: body for rel, body in installed_base.items()}
    if style == "setup.py":
        files["setup.py"] = (
            'raise RuntimeError("setup code must not execute")\n'
            f'NAMES = ("{namespace}", "{namespace}.services")\n'
            "setup(namespace_packages=NAMES)\n"
        )
    elif style == "setup.cfg":
        files["setup.cfg"] = (
            "[options]\nnamespace_packages =\n"
            f"    {namespace}\n    {namespace}.services\n"
        )
    cwd = _repo(tmp_path, files)
    site = tmp_path / "site-packages"
    installed = {rel: b"FIX = True\n" for rel in installed_base}
    added = {
        f"{owned}/fix_added.py",
        f"{owned}/new_package/__init__.py",
        f"{owned}_v2/fix_added.py",
    }
    project_caches: set[str] = set()
    for rel in sorted(set(installed_base) | added):
        if rel in added:
            installed[rel] = b"FIX = True\n"
        if rel.endswith(".py"):
            path = Path(rel)
            for cache in (
                path.parent / "__pycache__" / (path.stem + ".cpython-312.pyc"),
                path.with_suffix(".pyc"),
                path.with_suffix(".pyo"),
            ):
                installed[cache.as_posix()] = b"stale project bytecode"
                project_caches.add(cache.as_posix())
    preserved = {
        f"{namespace}/api_core/__init__.py": b'DEPENDENCY = "api_core"\n',
        f"{namespace}/api_core/client_options.py": b'VALUE = "client options"\n',
        f"{namespace}/auth/__init__.py": b'DEPENDENCY = "auth"\n',
        f"{namespace}/auth/credentials.py": b'VALUE = "credentials"\n',
        f"{namespace}/services/core/__init__.py": b'DEPENDENCY = "cloud core"\n',
        f"{namespace}/services/core/client.py": b'VALUE = "core client"\n',
        f"{namespace}/other_distribution.py": b'VALUE = "shared namespace module"\n',
        f"{namespace}/services/other_distribution.py": b'VALUE = "shared child module"\n',
        f"setuptools/_vendor/{owned}/fix_added.py": b'VALUE = "vendored copy"\n',
        f"{owned}/_fast.cpython-312-x86_64-linux-gnu.so": b"\x7fELF compiled residual",
        "events-1.0.dist-info/METADATA": b"Name: events\n",
        "api_core-1.0.dist-info/METADATA": b"Name: api-core\n",
        "auth-1.0.dist-info/METADATA": b"Name: auth\n",
        "service_core-1.0.dist-info/METADATA": b"Name: service-core\n",
    }
    for rel in list(preserved):
        if rel.endswith(".py"):
            path = Path(rel)
            preserved[
                (path.parent / "__pycache__" / (path.stem + ".cpython-312.pyc")).as_posix()
            ] = b"unchanged dependency bytecode"
            preserved[path.with_suffix(".pyc").as_posix()] = b"unchanged legacy bytecode"
    for rel, body in {**installed, **preserved}.items():
        path = site / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    packages, modules = v2_project_tops(cwd, tracked=_tracked(files))
    assert packages.tops == {namespace}
    assert packages.roots == {owned, owned + "_v2"}
    assert packages.namespaces == {namespace, f"{namespace}/services"}
    assert modules == set()
    matches = v2_collect_matches(cwd, packages, modules, *_roots(site))
    assert not {path.relative_to(site).as_posix() for path, _, _ in matches} & (
        set(preserved) - {f"{owned}/_fast.cpython-312-x86_64-linux-gnu.so"}
    )

    def real_import(script: str) -> tuple[int, str, str]:
        # -B keeps bytecode receipts stable while importing real installed
        # project and dependency files, outside the fixture worktree.
        code = (
            f"import sys; sys.path.insert(0, {str(site)!r})\n"
            + script
            + f"\nimport {namespace}.services.events.core\n"
            + f"import {namespace}.services.events_v2.client\n"
            + f"import {namespace}.services.helpers\n"
            + f"import {namespace}.api_core.client_options\n"
            + f"import {namespace}.auth.credentials\n"
            + f"import {namespace}.services.core.client\n"
        )
        proc = subprocess.run(
            [sys.executable, "-B", "-c", code],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        return proc.returncode, proc.stdout, proc.stderr

    state = tmp_path / "state"
    state.mkdir()
    v2_action_neutralize(
        cwd,
        tracked=_tracked(files),
        roots=_roots(site),
        probe_run=real_import,
        state_dir=state,
    )
    for rel, body in installed_base.items():
        assert (site / rel).read_bytes() == body.encode()
    assert all(not (site / rel).exists() for rel in added | project_caches)
    assert {rel: (site / rel).read_bytes() for rel in preserved} == preserved
    prestate = json.loads((state / "purge-installed-copies-v2-prestate.json").read_text())
    assert prestate["tops"] == [namespace]
    assert prestate["pre_ok"] == {namespace: True}
    v2_action_verify(
        cwd,
        tracked=_tracked(files),
        roots=_roots(site),
        import_run=real_import,
        state_dir=state,
    )
    assert {rel: (site / rel).read_bytes() for rel in preserved} == preserved
    (site / owned / "fix_added.py").write_bytes(b"FIX = True\n")
    with pytest.raises(SystemExit, match="untracked copy remains"):
        v2_action_verify(cwd, tracked=_tracked(files), roots=_roots(site), state_dir=state)


def test_nested_namespace_blocks_an_ordinary_package_ancestor(tmp_path: Path) -> None:
    files = {
        f"{TOP}/__init__.py": '"""extend_path in documentation is not a namespace."""\n',
        f"{TOP}/plugins/__init__.py": (
            "from pkgutil import extend_path\n__path__ = extend_path(__path__, __name__)\n"
        ),
        f"{TOP}/plugins/ours/__init__.py": "BASE = True\n",
        f"{TOP}/plugins/ours/core.py": "VALUE = 1\n",
    }
    cwd = _repo(tmp_path, files)
    site = tmp_path / "site-packages"
    for rel in files:
        path = site / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"FIX = True\n")
    added = site / TOP / "plugins" / "ours" / "fix_added.py"
    added.write_bytes(b"FIX = True\n")
    root_added = site / TOP / "fix_added.py"
    root_added.write_bytes(b"FIX = True\n")
    foreign = site / TOP / "plugins" / "other" / "core.py"
    foreign.parent.mkdir()
    foreign.write_bytes(b"DEPENDENCY = True\n")
    foreign_cache = foreign.with_suffix(".pyc")
    foreign_cache.write_bytes(b"dependency bytecode")
    packages, modules = v2_project_tops(cwd, tracked=_tracked(files))
    assert TOP in packages.roots
    assert packages.namespaces == {f"{TOP}/plugins"}
    assert v2_match_rel(f"{TOP}/plugins/other/core.py", packages, modules) is None
    base = v2_base_map(cwd, packages, modules, tracked=_tracked(files))
    matches = v2_collect_matches(cwd, packages, modules, *_roots(site))
    v2_neutralize_matches(cwd, matches, base)
    assert not added.exists()
    assert not root_added.exists()
    assert foreign.read_bytes() == b"DEPENDENCY = True\n"
    assert foreign_cache.read_bytes() == b"dependency bytecode"
    assert (site / TOP / "plugins" / "ours" / "core.py").read_bytes() == b"VALUE = 1\n"


@pytest.mark.parametrize("unreadable_init", [False, True], ids=["dynamic-metadata", "missing-init"])
def test_uncertain_namespace_ownership_fails_before_mutation(
    tmp_path: Path, unreadable_init: bool
) -> None:
    files = {
        "sharedco_ns/__init__.py": "",
        "sharedco_ns/ours/__init__.py": "BASE = True\n",
        "setup.py": "setup(namespace_packages=discover_namespaces())\n",
    }
    cwd = _repo(tmp_path, files)
    if unreadable_init:
        (cwd / "setup.py").write_text("setup()\n", encoding="utf-8")
        (cwd / "sharedco_ns" / "__init__.py").unlink()
    site = tmp_path / "site-packages"
    ours = site / "sharedco_ns" / "ours" / "__init__.py"
    ours.parent.mkdir(parents=True)
    ours.write_bytes(b"FIX = True\n")
    dependency = site / "sharedco_ns" / "dependency.py"
    dependency.write_bytes(b"DEPENDENCY = True\n")
    state = tmp_path / "state"
    state.mkdir()
    with pytest.raises(SystemExit, match="cannot determine namespace ownership"):
        v2_action_neutralize(
            cwd, tracked=_tracked(files), roots=_roots(site), state_dir=state
        )
    assert ours.read_bytes() == b"FIX = True\n"
    assert dependency.read_bytes() == b"DEPENDENCY = True\n"


def test_linked_shared_namespace_fails_without_removing_siblings(tmp_path: Path) -> None:
    files = {"sharedco_ns/ours/__init__.py": "BASE = True\n"}
    cwd = _repo(tmp_path, files)
    site = tmp_path / "site-packages"
    site.mkdir()
    target = tmp_path / "installed-namespace"
    ours = target / "ours" / "__init__.py"
    ours.parent.mkdir(parents=True)
    ours.write_bytes(b"FIX = True\n")
    dependency = target / "dependency.py"
    dependency.write_bytes(b"DEPENDENCY = True\n")
    linked = site / "sharedco_ns"
    linked.symlink_to(target, target_is_directory=True)
    packages, modules = v2_project_tops(cwd, tracked=_tracked(files))
    with pytest.raises(SystemExit, match="linked shared namespace"):
        v2_collect_matches(cwd, packages, modules, *_roots(site))
    assert linked.is_symlink()
    assert ours.read_bytes() == b"FIX = True\n"
    assert dependency.read_bytes() == b"DEPENDENCY = True\n"


def test_linked_owned_package_under_namespace_materializes_only_project(tmp_path: Path) -> None:
    files = {"sharedco_ns/ours/__init__.py": "BASE = True\n"}
    cwd = _repo(tmp_path, files)
    site = tmp_path / "site-packages"
    namespace = site / "sharedco_ns"
    namespace.mkdir(parents=True)
    target = tmp_path / "installed-project"
    target.mkdir()
    (target / "__init__.py").write_bytes(b"FIX = True\n")
    (target / "fix_added.py").write_bytes(b"FIX = True\n")
    linked = namespace / "ours"
    linked.symlink_to(target, target_is_directory=True)
    dependency = namespace / "dependency.py"
    dependency.write_bytes(b"DEPENDENCY = True\n")
    packages, modules = v2_project_tops(cwd, tracked=_tracked(files))
    base = v2_base_map(cwd, packages, modules, tracked=_tracked(files))
    matches = v2_collect_matches(cwd, packages, modules, *_roots(site))
    assert matches == [(linked, "sharedco_ns/ours/", "linkdir")]
    v2_neutralize_matches(cwd, matches, base)
    assert not linked.is_symlink()
    assert (linked / "__init__.py").read_bytes() == b"BASE = True\n"
    assert not (linked / "fix_added.py").exists()
    assert (target / "__init__.py").read_bytes() == b"FIX = True\n"
    assert dependency.read_bytes() == b"DEPENDENCY = True\n"


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

    proc = subprocess.run([bash, "-n", str(script)], capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")


def test_v1_block_shape_still_detected() -> None:
    assert shell_block().startswith(V1_BLOCK_START)


def test_v2_healthcheck_payload_pins_helpers_but_preserves_project_interpreter(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"
    setup_dir = parent / "environment" / "setup"
    setup_dir.mkdir(parents=True)
    original_setup = (
        "#!/bin/bash\n"
        'fail() { echo "$*" >&2; exit 1; }\n'
        'CWD="$HOME/repo"\n'
        "write_blocklist() { :; }\n"
        'python3 - "$CWD" prelude <<\'PY\' || fail "prelude failed"\n'
        "from __future__ import annotations\n"
        "PY\n"
        "write_blocklist\n"
        'python3 - "$CWD" tail <<\'PY\' || fail "tail failed"\n'
        "from __future__ import annotations\n"
        "PY\n"
    )
    (setup_dir / "setup.sh").write_text(original_setup, encoding="utf-8")
    tests = parent / "tests"
    tests.mkdir()
    project_test = b"#!/bin/sh\npython3 --project-test\n"
    (tests / "test.sh").write_bytes(project_test)
    (parent / "instruction.md").write_text("Fix the project.", encoding="utf-8")
    blob = pack_setup(setup_dir)
    (parent / "task.toml").write_text(
        '[task]\nname = "setup-path"\n'
        '[environment]\nworkdir = "/testbed"\ndocker_image = "example/image"\n'
        "[environment.healthcheck]\n"
        f'command = "bash -c \'echo {blob} | base64 -d | tar -xzf - '
        '-C /var/lib/mimo && bash /var/lib/mimo/setup.sh\'"\n',
        encoding="utf-8",
    )
    record = derive_purge_installed_copies_v2(
        parent,
        repairs_digest="sha256:" + "0" * 64,
        repo_root=tmp_path,
        variants_root=tmp_path / "store",
    )
    package = tmp_path / "store" / record.task_slug / record.digest12
    setup = (package / SETUP_REL).read_bytes()
    config = tomllib.loads((package / "task.toml").read_text(encoding="utf-8"))
    command = config["environment"]["healthcheck"]["command"]
    payload = unpack_setup_blob(command.split("echo ", 1)[1].split(" |", 1)[0])
    assert payload["setup.sh"] == setup
    assert {change.path for change in record.files} == {SETUP_REL, "task.toml"}
    assert (package / "tests" / "test.sh").read_bytes() == project_test
    assert record.inputs["setup_python_path"] == SETUP_PYTHON_PATH
    assert record.inputs["setup_before_sha256"] == (
        "sha256:" + hashlib.sha256(original_setup.encode()).hexdigest()
    )
    assert record.inputs["setup_after_sha256"] == "sha256:" + hashlib.sha256(setup).hexdigest()
    assert record.inputs["setup_before_sha256"] != record.inputs["setup_after_sha256"]

    # Relocate the image's absolute /usr tree rather than using or mutating
    # host interpreters. These executables distinguish helpers from project
    # tests; the legacy interpreter must never receive a setup heredoc.
    image = tmp_path / "image"
    modern = image / "usr" / "bin"
    legacy = image / "usr" / "local" / "bin"
    home = tmp_path / "home"
    for path in (modern, legacy, home):
        path.mkdir(parents=True)
    (modern / "python3").write_text(
        "#!/bin/sh\n"
        '[ "$1" = "-" ] || exit 87\n'
        'printf "%s\\n" "$3" >> "$HELPER_LOG"\n'
        "while IFS= read -r line; do :; done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (legacy / "python3").write_text(
        "#!/bin/sh\n"
        '[ "$1" = "--project-test" ] || exit 86\n'
        'printf "legacy\\n%s\\n" "$PATH" > "$RUNNER_LOG"\n',
        encoding="utf-8",
    )
    for path in (modern / "python3", legacy / "python3"):
        path.chmod(0o700)
    healthcheck_setup = tmp_path / "healthcheck-setup.sh"
    healthcheck_setup.write_bytes(
        payload["setup.sh"].replace(b"/usr/", f"{image}/usr/".encode())
    )
    inherited_path = f"{legacy}:/usr/local/bin:/usr/bin:/bin"
    helper_log = tmp_path / "helpers.log"
    runner_log = tmp_path / "runner.log"
    bash = shutil.which("bash")
    assert bash is not None
    run = subprocess.run(
        [bash, "-c", 'set -e; bash "$SETUP_SCRIPT"; bash "$PROJECT_TEST_SCRIPT"'],
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": inherited_path,
            "SETUP_SCRIPT": str(healthcheck_setup),
            "PROJECT_TEST_SCRIPT": str(package / "tests" / "test.sh"),
            "HELPER_LOG": str(helper_log),
            "RUNNER_LOG": str(runner_log),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert helper_log.read_text(encoding="utf-8").splitlines() == [
        "prelude",
        "neutralize",
        "verify2",
        "tail",
    ]
    assert runner_log.read_text(encoding="utf-8").splitlines() == ["legacy", inherited_path]
