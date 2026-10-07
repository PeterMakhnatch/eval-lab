"""HAR-185 built-copy scan: behavioural tests on synthetic fixtures (no network)."""

from __future__ import annotations

import gzip
import importlib.util
import io
import sys
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_SCAN = REPO_ROOT / "research" / "experiments" / "har185-built-copy" / "scan.py"


def load_scan():
    spec = importlib.util.spec_from_file_location("har185_built_copy_scan", _SCAN)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scan = load_scan()

DOCKERFILE = (
    "# The task's image, pinned by digest.\n"
    "FROM docker.io/xiaomimimo/mimo-v2.6-rl-oss"
    "@sha256:e4feae817a2d6660ebb8ea03f3487af0acaa2431fd2cf3763738395d67a76709\n"
)
SETUP = "#!/bin/bash\nCWD=/testbed\ngit rev-parse HEAD\n"


def test_parse_image_digest() -> None:
    """The Dockerfile digest is the scan's image identity."""
    assert scan.parse_image_digest(DOCKERFILE) == (
        "sha256:e4feae817a2d6660ebb8ea03f3487af0acaa2431fd2cf3763738395d67a76709"
    )
    assert scan.parse_image_digest("FROM ubuntu:22.04\n") is None


def test_parse_roots() -> None:
    """Setup CWD and task workdir locate the in-image working tree."""
    assert scan.parse_setup_cwd(SETUP) == "/testbed"
    assert scan.parse_setup_cwd("#!/bin/bash\necho hi\n") == "/testbed"
    assert scan.parse_task_workdir('[environment]\nworkdir = "/testbed"\n') == "/testbed"
    assert scan.parse_task_workdir("[task]\nname = \"x\"\n") is None


def test_parse_manifest_single_layer() -> None:
    """Manifest parsing keeps the layer list the streamer walks."""
    body = (
        b'{"schemaVersion":2,"config":{"digest":"sha256:aaaa","size":1},'
        b'"layers":[{"digest":"sha256:bbbb","size":2}]}'
    )
    assert scan.parse_manifest(body) == {"config": "sha256:aaaa", "layers": ["sha256:bbbb"]}
    try:
        scan.parse_manifest(b'{"schemaVersion":2,"layers":[]}')
    except ValueError:
        pass
    else:
        raise AssertionError("layerless manifest must raise")


def test_patch_path_split() -> None:
    """test.patch paths split into source vs test; infra files drop out."""
    patch = (
        "diff --git a/responses/__init__.py b/responses/__init__.py\n"
        "--- a/responses/__init__.py\n"
        "+++ b/responses/__init__.py\n"
        "diff --git a/responses/tests/test_responses.py b/responses/tests/test_responses.py\n"
        "--- a/responses/tests/test_responses.py\n"
        "+++ b/responses/tests/test_responses.py\n"
        "diff --git a/test_commands.json b/test_commands.json\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/test_commands.json\n"
        "diff --git a/mimo_test_command.sh b/mimo_test_command.sh\n"
        "--- /dev/null\n"
        "+++ b/mimo_test_command.sh\n"
    )
    paths = scan.parse_patch_paths(patch)
    assert paths == [
        "responses/__init__.py",
        "responses/tests/test_responses.py",
        "test_commands.json",
        "mimo_test_command.sh",
    ]
    source, tests = scan.split_patch_paths(paths)
    assert source == ["responses/__init__.py"]
    assert tests == ["responses/tests/test_responses.py"]


def test_is_test_path() -> None:
    """Test detection covers test dirs and test_/_test basenames."""
    assert scan.is_test_path("tests/commands/hook_impl_test.py")
    assert scan.is_test_path("pkg/test_foo.py")
    assert scan.is_test_path("pkg/foo_test.py")
    assert not scan.is_test_path("pre_commit/commands/hook_impl.py")
    assert not scan.is_test_path("responses/__init__.py")


def test_imports_from_patch() -> None:
    """Added-line imports are collected deduplicated at top level."""
    patch = (
        "@@ +++\n"
        "+import os, pre_commit.commands\n"
        "+from responses import registries\n"
        "+import pytest\n"
        " context line\n"
        "-removed import line\n"
    )
    assert scan.imports_from_patch(patch) == ["os", "pre_commit", "responses", "pytest"]


def test_implied_fix_names() -> None:
    """Test names imply fixed-module basenames and package dirnames."""
    basenames, dirnames = scan.implied_fix_names(["tests/commands/hook_impl_test.py"])
    assert basenames == ["hook_impl.py"]
    assert dirnames == []
    basenames, dirnames = scan.implied_fix_names(["responses/tests/test_responses.py"])
    assert basenames == ["responses.py"]
    assert dirnames == ["responses"]


def test_guess_project_prefers_source() -> None:
    """Explicit source paths name the project directly."""
    name, basis = scan.guess_project(["pre_commit/commands/run.py"], [], [])
    assert (name, basis) == ("pre_commit", "patch-source-path")
    name, basis = scan.guess_project(["src/mypkg/mod.py"], [], [])
    assert (name, basis) == ("mypkg", "patch-src-layout")


def test_guess_project_test_dir_confirmed_by_import() -> None:
    """The test-dir package wins, confirmed when an import agrees."""
    name, basis = scan.guess_project(
        [], ["responses/tests/test_responses.py"], ["responses", "pytest"]
    )
    assert (name, basis) == ("responses", "test-dir+import")
    name, basis = scan.guess_project([], ["responses/tests/test_responses.py"], [])
    assert (name, basis) == ("responses", "test-dir-package")


def test_guess_project_import_fallback_skips_test_deps() -> None:
    """Without a test-dir package, the first non-test import names the project."""
    name, basis = scan.guess_project([], ["tests/test_x.py"], ["pytest", "mylib"])
    assert (name, basis) == ("mylib", "patch-import")
    assert scan.guess_project([], ["tests/test_x.py"], ["pytest"]) == ("", "unknown")


def test_clean_member_name() -> None:
    """Tar prefixes are stripped so roots compare cleanly."""
    assert scan.clean_member_name("./testbed/build/lib/x.py") == "testbed/build/lib/x.py"
    assert scan.clean_member_name("/testbed/x.py") == "testbed/x.py"
    assert scan.clean_member_name("a/b.py") == "a/b.py"


def test_is_build_lib() -> None:
    """Setuptools build output dirs match with and without platform tags."""
    assert scan.is_build_lib(["testbed", "build", "lib", "x.py"])
    assert scan.is_build_lib(["usr", "build", "lib.linux-x86_64-3.11", "x.py"])
    assert not scan.is_build_lib(["testbed", "build", "scripts", "x.py"])
    assert not scan.is_build_lib(["testbed", "lib", "x.py"])


def test_project_component_matching() -> None:
    """Dist-info version suffixes and -/_ spellings fold to the project."""
    variants = scan.name_variants("pre-commit")
    assert scan.component_matches_project("pre_commit-2.15.0.dist-info", variants)
    assert scan.component_matches_project("pre_commit", variants)
    assert not scan.component_matches_project("pytest-8.0.dist-info", variants)
    assert scan.path_matches_project("usr/lib/site-packages/pre_commit/x.py", variants)
    assert not scan.path_matches_project("usr/lib/site-packages/pytest/x.py", variants)


def _classify(clean: str, project: str = "", basenames=(), dirnames=()) -> set[str]:
    return scan.classify_member(
        clean,
        repo_root="testbed",
        variants=scan.name_variants(project) if project else set(),
        fix_basenames=set(basenames),
        fix_dirnames=set(dirnames),
    )


def test_classify_site_packages_needs_relevance() -> None:
    """Unrelated site-packages files are not evidence; project ones are."""
    kinds = _classify(
        "usr/local/lib/python3.11/site-packages/responses/__init__.py",
        project="responses",
    )
    assert kinds == {"site-packages"}
    assert _classify("usr/local/lib/python3.11/site-packages/pytest/x.py") == set()


def test_classify_fix_basename_matches_without_project() -> None:
    """A fix-suffixed module under site-packages matches even hintless."""
    kinds = _classify(
        "usr/local/lib/python3.11/site-packages/pre_commit/commands/hook_impl.py",
        basenames={"hook_impl.py"},
    )
    assert kinds == {"site-packages"}


def test_classify_build_lib_in_and_out_of_tree() -> None:
    """build/lib counts on both sides of the working-tree boundary."""
    assert _classify("testbed/build/lib/responses/__init__.py", project="responses") == {
        "build-lib"
    }
    assert _classify(
        "opt/build/lib.linux-x86_64-3.11/hook_impl.py", basenames={"hook_impl.py"}
    ) == {"build-lib"}


def test_classify_dist_info_always_indexed() -> None:
    """Dist-info/egg-info dirs are indexed so the project resolves later."""
    assert _classify("usr/lib/python3/dist-packages/pip-24.dist-info") == {"dist-info"}
    assert _classify("testbed/pkg.egg-info") == {"egg-info"}
    kinds = _classify("usr/lib/python3/site-packages/p/METADATA")
    assert kinds == set()  # bare METADATA without a dist-info parent is skipped


def test_classify_metadata_parent_mapping() -> None:
    """METADATA under a dist-info parent inherits the dist kind."""
    assert _classify("usr/lib/python3/site-packages/p-1.dist-info/METADATA") == {"dist-info"}
    assert _classify("testbed/p-1.egg-info/PKG-INFO") == {"egg-info"}


def test_classify_archives_and_caches() -> None:
    """All archives are inspected; reduction filters out third-party packages."""
    assert _classify("root/.cache/pip/wheels/x/pre_commit-2.15.0-py3-none-any.whl",
                     project="pre-commit") == {"wheel", "pip-cache"}
    assert _classify("tmp/pc/pre_commit-2.15.0-py3-none-any.whl",
                     dirnames={"pre_commit"}) == {"wheel"}
    assert _classify("tmp/pc/pytest-8-py3-none-any.whl", dirnames={"pre_commit"}) == {"wheel"}
    assert _classify("root/.cache/pip/http/ab/cd", project="pre-commit") == set()


def test_classify_tox_vendored_pth() -> None:
    """Tox envs, vendored copies, and path hooks classify when relevant."""
    assert _classify(".tox/py311/lib/python3.11/site-packages/x.py",
                     basenames={"x.py"}) == {"tox-nox", "site-packages"}
    assert _classify("opt/vendor/x.py", basenames={"x.py"}) == {"vendored"}
    assert _classify("usr/lib/python3.11/site-packages/mypkg.egg-link",
                     project="mypkg") == {"pth-egglink", "site-packages"}


def test_classify_worktree_fix() -> None:
    """Working-tree fix files match by basename or one-level package dir."""
    assert _classify("testbed/pre_commit/commands/hook_impl.py",
                     basenames={"hook_impl.py"}) == {"worktree-fix"}
    assert _classify("testbed/responses/__init__.py", dirnames={"responses"}) == {
        "worktree-fix"
    }
    assert _classify("testbed/responses/deep/nested.py", dirnames={"responses"}) == set()
    assert _classify("testbed/setup.cfg") == {"worktree-pkgmeta"}
    assert _classify("testbed/.git/HEAD") == set()


def test_version_parsing_and_compare() -> None:
    """Only Name/Version lines and version assignments are parsed."""
    name, version = scan.version_from_metadata("Name: pre-commit\nVersion: 2.15.0\n")
    assert (name, version) == ("pre-commit", "2.15.0")
    assert scan.version_from_packaging_file("setup.cfg", "[metadata]\nversion = 1.2\n") == "1.2"
    assert scan.compare_versions("2.15.0", "2.14.0") == "newer"
    assert scan.compare_versions("2.14.0", "2.15.0") == "older"
    assert scan.compare_versions("v1.2", "1.2.0") == "same"
    assert scan.compare_versions("abc", "1.2") == "unverifiable"
    assert scan.compare_versions("", "1.2") == "unverifiable"


def test_wilson_reference_values() -> None:
    """Wilson bounds match the textbook reference points."""
    assert scan.wilson(0, 0) == (0.0, 0.0)
    lo, hi = scan.wilson(100, 100)
    assert abs(lo - 0.963) < 0.005 and abs(hi - 1.0) < 1e-9
    lo, hi = scan.wilson(0, 10)
    assert lo == 0.0 and 0.0 < hi < 0.35


def test_whiteout_removes_overlay_entries() -> None:
    """OCI whiteouts delete earlier layers' entries from the index."""
    overlay = {"a/b.py": {"kinds": ["site-packages"]}, "a/c.py": {"kinds": ["site-packages"]}}
    assert scan.apply_whiteout(overlay, "a/.wh.b.py") is True
    assert "a/b.py" not in overlay and "a/c.py" in overlay
    assert scan.apply_whiteout(overlay, "a/.wh..wh..opq") is True
    assert overlay == {}
    assert scan.apply_whiteout({}, "a/b.py") is False


def _layer_stream(files: dict[str, bytes]) -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    raw = buf.getvalue()
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb") as gz:
        gz.write(raw)
    out.seek(0)
    return out


def _state(**kwargs):
    defaults = {
        "repo_root": "testbed",
        "variants": set(),
        "fix_paths": set(),
        "fix_basenames": set(),
        "fix_dirnames": set(),
    }
    defaults.update(kwargs)
    return scan.StreamState(**defaults)


def test_stream_end_to_end_build_lib_differs() -> None:
    """A differing build/lib copy of the fixed module verdicts yes/yes."""
    base = b"BASE-activate\n"
    fixed = b"FIXED-activate-strict\n"
    stream = _layer_stream(
        {
            "testbed/responses/__init__.py": base,
            "testbed/build/lib/responses/__init__.py": fixed,
            "usr/local/lib/python3.11/site-packages/responses-4.2.0.dist-info/METADATA": (
                b"Name: responses\nVersion: 4.2.0\n"
            ),
        }
    )
    state = _state(fix_basenames={"responses.py"}, fix_dirnames={"responses"})
    scan.scan_layer_stream(stream, state)
    row = scan.summarize_state(
        state,
        task_id="format-code-task-001269",
        run="original",
        ledger_digest="",
        image_digest="sha256:x",
        hint="responses",
        hint_basis="test-dir-package",
        method="test-stream",
    )
    assert row["has_built_copy"] == "yes"
    assert "build-lib" in row["built_copy_kinds"].split(";")
    assert row["fix_differs"] == "yes"
    assert "responses/__init__.py" in row["fix_files"]
    assert row["installed_version"] == "4.2.0"
    assert row["project"] == "responses"


def test_stream_identical_copy_verdicts_no_diff() -> None:
    """An identical installed copy is still a copy, but the fix does not differ."""
    same = b"SAME\n"
    stream = _layer_stream(
        {
            "testbed/pkg/mod.py": same,
            "usr/local/lib/python3.11/site-packages/pkg/mod.py": same,
        }
    )
    state = _state(
        variants=scan.name_variants("pkg"),
        fix_basenames={"mod.py"},
        fix_dirnames=set(),
    )
    scan.scan_layer_stream(stream, state)
    row = scan.summarize_state(
        state,
        task_id="t",
        run="original",
        ledger_digest="",
        image_digest="sha256:x",
        hint="pkg",
        hint_basis="patch-import",
        method="test-stream",
    )
    assert row["has_built_copy"] == "yes"
    assert row["fix_differs"] == "no"
    assert row["needs_repair"] == "false"


def test_stream_hintless_project_resolves_from_worktree() -> None:
    """With no package hint, the worktree fix-file owner names the project."""
    stream = _layer_stream(
        {
            "testbed/pre_commit/commands/hook_impl.py": b"BASE\n",
            "usr/local/lib/python3.11/site-packages/pre_commit/commands/hook_impl.py": (
                b"FIXED\n"
            ),
        }
    )
    state = _state(fix_basenames={"hook_impl.py"})
    scan.scan_layer_stream(stream, state)
    row = scan.summarize_state(
        state,
        task_id="format-code-task-002308",
        run="original",
        ledger_digest="",
        image_digest="sha256:x",
        hint="",
        hint_basis="unknown",
        method="test-stream",
    )
    assert row["project"] == "pre_commit"
    assert row["has_built_copy"] == "yes"
    assert "site-packages" in row["built_copy_kinds"].split(";")
    assert row["fix_differs"] == "yes"


def test_stream_build_lib_never_seeds_base() -> None:
    """A build/lib copy is compared against the true source, never itself."""
    stream = _layer_stream(
        {
            "testbed/build/lib/pkg/mod.py": b"FIXED\n",
            "testbed/pkg/mod.py": b"BASE\n",
            "usr/local/lib/python3.11/site-packages/pkg/mod.py": b"FIXED\n",
        }
    )
    state = _state(
        variants=scan.name_variants("pkg"),
        fix_basenames={"mod.py"},
        fix_dirnames=set(),
    )
    scan.scan_layer_stream(stream, state)
    row = scan.summarize_state(
        state,
        task_id="t",
        run="original",
        ledger_digest="",
        image_digest="sha256:x",
        hint="pkg",
        hint_basis="patch-import",
        method="test-stream",
    )
    assert row["fix_files"] == "pkg/mod.py"
    assert row["fix_differs"] == "yes"


def test_stream_clean_image_verdicts_no() -> None:
    """No built copies anywhere verdicts a clean no/unverifiable."""
    stream = _layer_stream({"testbed/pkg/mod.py": b"BASE\n"})
    state = _state(
        variants=scan.name_variants("pkg"),
        fix_basenames={"mod.py"},
        fix_dirnames=set(),
    )
    scan.scan_layer_stream(stream, state)
    row = scan.summarize_state(
        state,
        task_id="t",
        run="original",
        ledger_digest="",
        image_digest="sha256:x",
        hint="pkg",
        hint_basis="patch-import",
        method="test-stream",
    )
    assert row["has_built_copy"] == "no"
    assert row["needs_repair"] == "false"
    assert row["fix_differs"] == "unverifiable"


def test_build_row_vocabulary() -> None:
    """The exact CSV vocabulary the verdict consumer reads."""
    assert scan.verdict(True) == ("yes", "false")
    assert scan.verdict(False) == ("no", "false")
    assert scan.verdict(None) == ("unscanned", "false")
    row = scan.build_row(task_id="t", run="r", ledger_digest="", image_digest="",
                         error="boom")
    assert row["has_built_copy"] == "unscanned" and row["needs_repair"] == "false"


def test_prevalence_counts() -> None:
    """Prevalence counts scanned rows and bounds the rate."""
    rows = [
        {"has_built_copy": "yes", "needs_repair": "true"},
        {"has_built_copy": "yes", "needs_repair": "false"},
        {"has_built_copy": "no", "needs_repair": "false"},
        {"has_built_copy": "unscanned", "needs_repair": "false"},
    ]
    report = scan.prevalence(rows)
    assert report["counts"] == {"yes": 2, "no": 1, "unscanned": 1}
    assert report["scanned"] == 3 and report["yes"] == 1 and report["n"] == 4
    assert 0.0 < report["lo"] < report["hi"] <= 1.0


def test_task_fix_spec_from_package_dir(tmp_path: Path) -> None:
    """A synthetic task package yields the fix spec the streamer needs."""
    package = tmp_path / "format-code-task-002308"
    (package / "tests").mkdir(parents=True)
    (package / "environment").mkdir(parents=True)
    (package / "tests" / "test.patch").write_text(
        "diff --git a/tests/commands/hook_impl_test.py b/tests/commands/hook_impl_test.py\n"
        "--- a/tests/commands/hook_impl_test.py\n"
        "+++ b/tests/commands/hook_impl_test.py\n"
        "@@\n"
        "+def test_run_ns_post_rewrite():\n"
    )
    (package / "task.toml").write_text("[environment]\nworkdir = \"/testbed\"\n")
    (package / "environment" / "Dockerfile").write_text(DOCKERFILE)
    spec = scan.task_fix_spec(str(package))
    assert spec["error"] == ""
    assert spec["fix_basenames"] == ["hook_impl.py"]
    assert spec["prefer_root"] == "testbed"
    assert spec["image"] == scan.parse_image_digest(DOCKERFILE)


def test_validate_result_flags_misses() -> None:
    """Validation disagreements name the exact missed expectation."""
    good = {
        "has_built_copy": "yes",
        "built_copy_kinds": "build-lib;dist-info",
        "built_copy_paths": "testbed/build/lib/responses/__init__.py",
        "project": "responses",
        "project_basis": "test-dir-package",
        "fix_differs": "yes",
        "fix_files": "responses/__init__.py",
        "error": "",
    }
    assert scan.validate_result("001269", good) == []
    bad = dict(good, has_built_copy="no", error="timeout")
    problems = scan.validate_result("001269", bad)
    assert len(problems) == 1 and "has_built_copy=yes" in problems[0]
    wrong_kind = dict(good, built_copy_kinds="wheel")
    assert any("build-lib" in p for p in scan.validate_result("001269", wrong_kind))


def test_nested_worktree_and_suffix_pairing() -> None:
    state = _state(repo_root="workspace/repo", variants={"pkg"},
                   fix_basenames={"mod.py"})
    scan.scan_layer_stream(_layer_stream({
        "workspace/repo/pkg/mod.py": b"base",
        "workspace/repo/other/mod.py": b"unrelated",
        "workspace/repo/.tox/py/lib/site-packages/pkg/mod.py": b"fixed",
    }), state)
    row = scan.summarize_state(state, task_id="t", run="r", ledger_digest="",
                               image_digest="sha256:x", hint="pkg",
                               hint_basis="patch-source-path", method="fixture")
    assert row["fix_differs"] == "yes"
    assert "tox-nox" in row["built_copy_kinds"]
    assert "other/mod.py" not in row["comparisons"]


def test_unidentified_project_does_not_count_dependencies() -> None:
    state = _state()
    scan.scan_layer_stream(_layer_stream({
        "usr/lib/site-packages/dependency-1.dist-info/METADATA":
            b"Name: dependency\nVersion: 1\n",
    }), state)
    row = scan.summarize_state(state, task_id="t", run="r", ledger_digest="",
                               image_digest="sha256:x", hint="",
                               hint_basis="unknown", method="fixture")
    assert row["has_built_copy"] == "unscanned"
    assert row["needs_repair"] == "false"


def test_opaque_pip_cache_wheel_is_hashed() -> None:
    import zipfile
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as wheel:
        wheel.writestr("pkg/mod.py", b"fixed")
        wheel.writestr("pkg-2.dist-info/METADATA", "Name: pkg\nVersion: 2\n")
    state = _state(variants={"pkg"}, fix_basenames={"mod.py"})
    scan.scan_layer_stream(_layer_stream({
        "testbed/pkg/mod.py": b"base",
        "testbed/setup.cfg": b"[metadata]\nversion = 1\n",
        "root/.cache/pip/http-v2/ab/c.body": archive.getvalue(),
    }), state)
    row = scan.summarize_state(state, task_id="t", run="r", ledger_digest="",
                               image_digest="sha256:x", hint="pkg",
                               hint_basis="patch-source-path", method="fixture")
    assert row["fix_differs"] == "yes"
    assert row["version_cmp"] == "newer"
    assert set(row["built_copy_kinds"].split(";")) == {"wheel", "pip-cache"}


def test_metadata_overlay_version_is_not_stale() -> None:
    state = _state(variants={"pkg"})
    path = "usr/lib/site-packages/pkg-1.dist-info/METADATA"
    scan.scan_layer_stream(_layer_stream({path: b"Name: pkg\nVersion: 2\n"}), state)
    scan.scan_layer_stream(_layer_stream({path: b"Name: pkg\nVersion: 1\n"}), state)
    assert [item["version"] for item in state.dists] == ["1"]
    scan.scan_layer_stream(_layer_stream({
        "usr/lib/site-packages/.wh.pkg-1.dist-info": b"",
    }), state)
    assert state.dists == []


def test_prerelease_version_order() -> None:
    assert scan.compare_versions("1.0", "1.0rc1") == "newer"
    assert scan.compare_versions("1.0rc1", "1.0") == "older"


def test_project_components_exclude_other_distributions() -> None:
    assert scan.component_matches_project("pkg.py", {"pkg"})
    assert scan.component_matches_project("__editable__.pkg-1.pth", {"pkg"})
    assert scan.component_matches_project("__editable___pkg_1_finder.py", {"pkg"})
    assert not scan.component_matches_project("pkg-extra-1.dist-info", {"pkg"})


def test_dynamic_version_is_never_retained_as_metadata() -> None:
    assert scan.version_from_packaging_file("setup.py", "version=get_version(),\n") == ""
    assert scan.version_from_packaging_file("setup.cfg", "version = 1.2rc1\n") == "1.2rc1"


def test_dependency_import_does_not_identify_own_project() -> None:
    state = _state(variants={"dependency"})
    scan.scan_layer_stream(_layer_stream({
        "usr/lib/site-packages/dependency-1.dist-info/METADATA":
            b"Name: dependency\nVersion: 1\n",
        "testbed/setup.cfg": b"[metadata]\nname = own-project\nversion = 1\n",
    }), state)
    project, basis = scan.resolve_project("dependency", "patch-import", state)
    assert (project, basis) == ("own-project", "root-packaging-name")
    state.base_meta.clear()
    assert scan.resolve_project("dependency", "patch-import", state) == ("", "unknown")


def test_modal_resource_projection_stays_below_authorization() -> None:
    assert scan.modal_ceiling(100) < 0.4
    assert scan.modal_ceiling(120) > 0.4


def test_local_build_labels_do_not_prove_a_newer_release() -> None:
    assert scan.compare_versions("0.8.0a0+674a71d", "0.8.0a0") == "same"
    assert scan.compare_versions("1.0+abc", "1.0+def") == "same"
    assert scan.compare_versions("1.1+abc", "1.0") == "newer"


def test_equal_public_version_without_hash_pairs_is_unverifiable() -> None:
    state = _state(variants={"pkg"})
    scan.scan_layer_stream(_layer_stream({
        "testbed/setup.cfg": b"[metadata]\nname = pkg\nversion = 1\n",
        "usr/lib/site-packages/pkg-1.dist-info/METADATA":
            b"Name: pkg\nVersion: 1+basehash\n",
    }), state)
    row = scan.summarize_state(state, task_id="t", run="r", ledger_digest="",
                               image_digest="sha256:x", hint="pkg",
                               hint_basis="patch-source-path", method="fixture")
    assert row["has_built_copy"] == "yes"
    assert row["version_cmp"] == "same"
    assert row["needs_repair"] == "false"
    assert row["comparison_status"] == "unverifiable"


def test_dynamic_python_name_is_not_project_identity() -> None:
    state = _state()
    scan.scan_layer_stream(_layer_stream({
        "testbed/setup.py": b'name = PACKAGE_NAME,\nversion = "1",\n',
    }), state)
    assert scan.resolve_project("", "unknown", state) == ("", "unknown")
    scan.scan_layer_stream(_layer_stream({
        "testbed/setup.py": b'name = "own-project",\nversion = "1",\n',
    }), state)
    assert scan.resolve_project("", "unknown", state) == (
        "own-project", "root-packaging-name")


def test_truncated_scan_does_not_clear_equal_fix_region() -> None:
    state = _state(variants={"pkg"}, fix_basenames={"mod.py"})
    scan.scan_layer_stream(_layer_stream({
        "testbed/setup.cfg": b"[metadata]\nname = pkg\nversion = 1\n",
        "testbed/pkg/mod.py": b"base",
        "usr/lib/site-packages/pkg/mod.py": b"base",
        "usr/lib/site-packages/pkg-1.dist-info/METADATA":
            b"Name: pkg\nVersion: 1\n",
    }), state)
    arguments = dict(task_id="t", run="r", ledger_digest="", image_digest="sha256:x",
                     hint="pkg", hint_basis="patch-source-path", method="fixture")
    assert scan.summarize_state(state, **arguments)["comparison_status"] == "clean"
    state.truncated = True
    row = scan.summarize_state(state, **arguments)
    assert row["fix_differs"] == "no"
    assert row["comparison_status"] == "unverifiable"
