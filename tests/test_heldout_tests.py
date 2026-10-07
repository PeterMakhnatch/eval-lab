"""Isolated real-Git fixture regressions for held-out suite extraction.

Every test builds a throwaway Git repository under ``tmp_path`` (a base
commit plus an upstream-fix commit, with hidden patches derived from side
branches via ``git diff`` so hunks are always well-formed), then runs
:func:`evallab.heldout_tests.extract_suite` against it. Fixture repository
code is parsed as AST only and never imported or executed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from evallab.heldout_tests import (
    HeldoutSuite,
    extract_suite,
    load_suite,
    validate_suite_path,
    write_suite,
)

IMAGE = "docker.io/example/demo@sha256:" + "0123abcd" * 8
TASK_NAME = "heldout-demo-000001"
WORKDIR = "/workspace/repo"


def _git_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="heldout",
        GIT_AUTHOR_EMAIL="heldout@example.invalid",
        GIT_COMMITTER_NAME="heldout",
        GIT_COMMITTER_EMAIL="heldout@example.invalid",
        GIT_AUTHOR_DATE="2020-01-01T00:00:00+00:00",
        GIT_COMMITTER_DATE="2020-01-01T00:00:00+00:00",
    )
    return env


def _git(repo: Path, *argv: str, raw: bool = False) -> str:
    proc = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=" + os.devnull,
            "-c",
            "commit.gpgSign=false",
            "-c",
            "init.templateDir=",
            *argv,
        ],
        cwd=str(repo),
        env=_git_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"git {' '.join(argv)} failed: {proc.stderr}"
    return proc.stdout if raw else proc.stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _write_bytes(repo: Path, rel: str, data: bytes) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "--allow-empty", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _repo_with_fix(
    tmp_path: Path,
    name: str,
    base_files: dict[str, str],
    *,
    fix_modify: dict[str, str] | None = None,
    fix_new: dict[str, str] | None = None,
    fix_delete: tuple[str, ...] = (),
    fix_binary: dict[str, bytes] | None = None,
) -> tuple[Path, str, str]:
    """Create a fixture repo; return ``(repo, base_sha, fix_sha)``."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for rel, text in base_files.items():
        _write(repo, rel, text)
    base = _commit(repo, "base")
    for rel, text in (fix_modify or {}).items():
        _write(repo, rel, text)
    for rel, text in (fix_new or {}).items():
        _write(repo, rel, text)
    for rel, data in (fix_binary or {}).items():
        _write_bytes(repo, rel, data)
    for rel in fix_delete:
        (repo / rel).unlink()
    fix = _commit(repo, "fix")
    return repo, base, fix


def _hidden_patch(
    repo: Path,
    base: str,
    out: Path,
    branch: str,
    *,
    modify: dict[str, str] | None = None,
    new: dict[str, str] | None = None,
    delete: tuple[str, ...] = (),
) -> Path:
    """Commit a hidden state on a side branch and export ``base..branch``."""
    _git(repo, "checkout", "-q", "-b", branch, base)
    for rel, text in (modify or {}).items():
        _write(repo, rel, text)
    for rel, text in (new or {}).items():
        _write(repo, rel, text)
    for rel in delete:
        (repo / rel).unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"hidden {branch}")
    out.write_text(
        _git(repo, "diff", "--no-color", "--no-ext-diff", base, branch, "--", raw=True),
        encoding="utf-8",
    )
    _git(repo, "checkout", "-q", "main")
    return out


def _extract(repo: Path, base: str, fix: str, patch: Path) -> HeldoutSuite:
    return extract_suite(
        git_dir=repo,
        base_commit=base,
        fix_commit=fix,
        hidden_patch=patch,
        task_name=TASK_NAME,
        image=IMAGE,
        workdir=WORKDIR,
    )


def _snapshot(repo: Path) -> dict[str, object]:
    objects = sorted(
        (
            str(path.relative_to(repo).as_posix()),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in (repo / ".git" / "objects").rglob("*")
        if path.is_file()
    )
    worktree = sorted(
        (
            str(path.relative_to(repo).as_posix()),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in repo.rglob("*")
        if path.is_file() and ".git/" not in path.as_posix()
    )
    return {
        "head": _git(repo, "rev-parse", "HEAD"),
        "refs": _git(repo, "for-each-ref", "--format=%(objectname) %(refname)"),
        "status": _git(repo, "status", "--porcelain"),
        "objects": objects,
        "worktree": worktree,
    }


BASE_CALC = """\
def add(a, b):
    return a + b
"""

BASE_TEST_CALC = """\
from calc import add


def test_add():
    assert add(1, 1) == 2
"""


def test_added_test_is_ready_with_exact_overlay(tmp_path: Path) -> None:
    repo, base, fix = _repo_with_fix(
        tmp_path,
        "added",
        {"calc.py": BASE_CALC, "tests/test_calc.py": BASE_TEST_CALC},
        fix_modify={
            "tests/test_calc.py": BASE_TEST_CALC
            + "\n\ndef test_multiply():\n    assert 2 * 3 == 6\n"
        },
    )
    patch = tmp_path / "empty.patch"
    patch.write_bytes(b"")
    suite = _extract(repo, base, fix, patch)
    assert suite.status == "ready"
    assert [item.node_id for item in suite.tests] == ["tests/test_calc.py::test_multiply"]
    assert suite.tests[0].change == "added"
    assert suite.tests[0].qualname == "test_multiply"
    assert suite.refusals == []
    by_path = {item.path: item for item in suite.files}
    assert set(by_path) == {"tests/test_calc.py"}
    payload = by_path["tests/test_calc.py"]
    assert payload.mode == "100644"
    assert payload.sha256 is not None and payload.content_base64 is not None
    raw = base64.b64decode(payload.content_base64)
    assert raw == (repo / "tests" / "test_calc.py").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == payload.sha256
    assert any(
        entry.node_id == "tests/test_calc.py::test_add" and entry.reason == "unchanged"
        for entry in suite.exclusions
    )
    out = tmp_path / "suite.json"
    write_suite(suite, out)
    assert load_suite(out) == suite


def test_modified_test_body_detected(tmp_path: Path) -> None:
    repo, base, fix = _repo_with_fix(
        tmp_path,
        "modified",
        {"calc.py": BASE_CALC, "tests/test_calc.py": BASE_TEST_CALC},
        fix_modify={
            "tests/test_calc.py": BASE_TEST_CALC.replace(
                "assert add(1, 1) == 2", "assert add(1, 1) == 2 and add(0, 0) == 0"
            )
        },
    )
    patch = tmp_path / "empty.patch"
    patch.write_bytes(b"")
    suite = _extract(repo, base, fix, patch)
    assert suite.status == "ready"
    assert len(suite.tests) == 1
    assert suite.tests[0].node_id == "tests/test_calc.py::test_add"
    assert suite.tests[0].change == "modified"


def test_comment_and_docstring_only_change_is_not_novel(tmp_path: Path) -> None:
    changed = (
        "# leading comment about addition\n"
        '"""Module docstring."""\n'
        "from calc import add\n"
        "\n\n"
        'def test_add():\n    """Now documented."""\n    # inline comment\n    assert add(1, 1) == 2\n'
    )
    repo, base, fix = _repo_with_fix(
        tmp_path,
        "comments",
        {"calc.py": BASE_CALC, "tests/test_calc.py": BASE_TEST_CALC},
        fix_modify={"tests/test_calc.py": changed},
    )
    patch = tmp_path / "empty.patch"
    patch.write_bytes(b"")
    suite = _extract(repo, base, fix, patch)
    assert suite.status == "no_extra_tests"
    assert suite.tests == [] and suite.files == [] and suite.refusals == []
    assert any(
        entry.node_id == "tests/test_calc.py::test_add" and entry.reason == "unchanged"
        for entry in suite.exclusions
    )


def test_renamed_copy_subtracts_as_base_duplicate(tmp_path: Path) -> None:
    renamed = BASE_TEST_CALC.replace("def test_add():", "def test_sum():")
    repo, base, fix = _repo_with_fix(
        tmp_path,
        "renamed",
        {"calc.py": BASE_CALC, "tests/test_calc.py": BASE_TEST_CALC},
        fix_modify={"tests/test_calc.py": renamed},
    )
    patch = tmp_path / "empty.patch"
    patch.write_bytes(b"")
    suite = _extract(repo, base, fix, patch)
    assert suite.status == "no_extra_tests"
    assert any(
        entry.node_id == "tests/test_calc.py::test_sum"
        and entry.reason == "base_duplicate"
        for entry in suite.exclusions
    )


def test_hidden_duplicate_subtracts_renamed_copy(tmp_path: Path) -> None:
    repo, base, fix = _repo_with_fix(
        tmp_path,
        "hidden-dup",
        {"calc.py": BASE_CALC, "tests/test_calc.py": BASE_TEST_CALC},
        fix_modify={
            "tests/test_calc.py": BASE_TEST_CALC + "\n\ndef test_new():\n    assert 2 * 3 == 6\n"
        },
    )
    patch = _hidden_patch(
        repo,
        base,
        tmp_path / "hidden.patch",
        "hidden-dup",
        modify={
            "tests/test_calc.py": BASE_TEST_CALC
            + "\n\ndef test_hidden_new():\n    # same body, other name and comment\n    assert 2 * 3 == 6\n"
        },
    )
    suite = _extract(repo, base, fix, patch)
    assert suite.status == "no_extra_tests"
    assert any(
        entry.node_id == "tests/test_calc.py::test_new"
        and entry.reason == "hidden_duplicate"
        for entry in suite.exclusions
    )


def test_decorator_only_change_is_novel(tmp_path: Path) -> None:
    decorated = (
        "import pytest\n\nfrom calc import add\n\n\n"
        "@pytest.mark.parametrize('a,b', [(1, 2)])\n"
        "def test_add(a, b):\n"
        "    assert add(a, b - 1) == 2\n"
    )
    repo, base, fix = _repo_with_fix(
        tmp_path,
        "decorated",
        {"calc.py": BASE_CALC, "tests/test_calc.py": BASE_TEST_CALC},
        fix_modify={"tests/test_calc.py": decorated},
    )
    patch = tmp_path / "empty.patch"
    patch.write_bytes(b"")
    suite = _extract(repo, base, fix, patch)
    assert suite.status == "ready"
    assert len(suite.tests) == 1
    assert suite.tests[0].node_id == "tests/test_calc.py::test_add"
    assert suite.tests[0].change == "modified"


def test_class_method_nodes_are_qualified(tmp_path: Path) -> None:
    base_tests = (
        "import unittest\nfrom calc import add\n\n"
        "class TestCalc:\n"
        "    def test_add(self):\n"
        "        assert True\n"
        "\n\n"
        "class TestThings(unittest.TestCase):\n"
        "    def test_a(self):\n"
        "        self.assertTrue(True)\n"
    )
    fixed_tests = (
        "import unittest\nfrom calc import add\n\n"
        "class TestCalc:\n"
        "    def test_add(self):\n"
        "        assert True\n"
        "\n"
        "    def test_div(self):\n"
        "        assert add(4, 2) == 6\n"
        "\n\n"
        "class TestThings(unittest.TestCase):\n"
        "    def test_a(self):\n"
        "        self.assertTrue(True)\n"
    )
    repo, base, fix = _repo_with_fix(
        tmp_path,
        "classes",
        {"calc.py": BASE_CALC, "tests/test_calc.py": base_tests},
        fix_modify={"tests/test_calc.py": fixed_tests},
    )
    patch = tmp_path / "empty.patch"
    patch.write_bytes(b"")
    suite = _extract(repo, base, fix, patch)
    assert suite.status == "ready"
    assert [item.node_id for item in suite.tests] == ["tests/test_calc.py::TestCalc::test_div"]
    assert suite.tests[0].qualname == "TestCalc.test_div"
    assert suite.tests[0].change == "added"
    assert any(
        entry.node_id == "tests/test_calc.py::TestCalc::test_add"
        and entry.reason == "unchanged"
        for entry in suite.exclusions
    )


def test_support_overlay_exports_conftest_not_production(tmp_path: Path) -> None:
    base_files = {
        "pkg/mod.py": "def value():\n    return 1\n",
        "tests/conftest.py": "import pytest\n\n\n@pytest.fixture\ndef dep():\n    return 1\n",
        "tests/test_mod.py": "def test_value(dep):\n    assert dep == 1\n",
    }
    repo, base, fix = _repo_with_fix(
        tmp_path,
        "support",
        base_files,
        fix_modify={
            "pkg/mod.py": "def value():\n    return 2\n",
            "tests/conftest.py": (
                "import pytest\n\n\n@pytest.fixture\ndef dep():\n    return 1\n"
                "\n\n@pytest.fixture\ndef other():\n    return 2\n"
            ),
            "tests/test_mod.py": (
                "def test_value(dep):\n    assert dep == 1\n"
                "\n\ndef test_other(other):\n    assert other == 2\n"
            ),
        },
        fix_binary={"tests/data.bin": b"\x00\x01\x02fixture\n"},
    )
    patch = tmp_path / "empty.patch"
    patch.write_bytes(b"")
    suite = _extract(repo, base, fix, patch)
    assert suite.status == "ready"
    assert [item.node_id for item in suite.tests] == ["tests/test_mod.py::test_other"]
    assert {item.path for item in suite.files} == {
        "tests/conftest.py",
        "tests/data.bin",
        "tests/test_mod.py",
    }
    assert "pkg/mod.py" not in {item.path for item in suite.files}
    blob = next(item for item in suite.files if item.path == "tests/data.bin")
    assert blob.mode == "100644"
    assert blob.content_base64 is not None
    assert base64.b64decode(blob.content_base64) == b"\x00\x01\x02fixture\n"
    assert any("production paths excluded" in note for note in suite.notes)


def test_production_only_fix_has_no_extra_tests(tmp_path: Path) -> None:
    repo, base, fix = _repo_with_fix(
        tmp_path,
        "prod-only",
        {"pkg/mod.py": "def value():\n    return 1\n"},
        fix_modify={"pkg/mod.py": "def value():\n    return 2\n"},
    )
    patch = tmp_path / "empty.patch"
    patch.write_bytes(b"")
    suite = _extract(repo, base, fix, patch)
    assert suite.status == "no_extra_tests"
    assert suite.exclusions == [] and suite.refusals == []


def test_deletion_overlay_uses_null_payload(tmp_path: Path) -> None:
    repo, base, fix = _repo_with_fix(
        tmp_path,
        "deletion",
        {
            "tests/test_main.py": BASE_TEST_CALC,
            "tests/test_old.py": "def test_gone():\n    assert True\n",
        },
        fix_modify={
            "tests/test_main.py": BASE_TEST_CALC + "\n\ndef test_new():\n    assert add(5, 6) == 11\n"
        },
        fix_delete=("tests/test_old.py",),
    )
    patch = tmp_path / "empty.patch"
    patch.write_bytes(b"")
    suite = _extract(repo, base, fix, patch)
    assert suite.status == "ready"
    assert [item.node_id for item in suite.tests] == ["tests/test_main.py::test_new"]
    by_path = {item.path: item for item in suite.files}
    assert set(by_path) == {"tests/test_main.py", "tests/test_old.py"}
    deleted = by_path["tests/test_old.py"]
    assert deleted.mode is None and deleted.content_base64 is None and deleted.sha256 is None


def test_source_git_immutable(tmp_path: Path) -> None:
    repo, base, fix = _repo_with_fix(
        tmp_path,
        "immutable",
        {"calc.py": BASE_CALC, "tests/test_calc.py": BASE_TEST_CALC},
        fix_modify={
            "tests/test_calc.py": BASE_TEST_CALC + "\n\ndef test_new():\n    assert True\n"
        },
    )
    patch = _hidden_patch(
        repo,
        base,
        tmp_path / "hidden.patch",
        "hidden-immutable",
        modify={"tests/test_calc.py": BASE_TEST_CALC + "# hidden touch\n"},
    )
    before = _snapshot(repo)
    suite = _extract(repo, base, fix, patch)
    after = _snapshot(repo)
    assert suite.status in ("ready", "no_extra_tests")
    assert before == after

def test_unknown_fix_commit_is_unavailable(tmp_path: Path) -> None:
    repo, base, _fix = _repo_with_fix(
        tmp_path,
        "missing-fix",
        {"calc.py": BASE_CALC, "tests/test_calc.py": BASE_TEST_CALC},
    )
    patch = tmp_path / "empty.patch"
    patch.write_bytes(b"")
    suite = _extract(repo, base, "1" * 40, patch)
    assert suite.status == "unavailable"
    assert suite.tests == [] and suite.files == []
    assert any("fix commit not present" in refusal for refusal in suite.refusals)
    assert suite.fix_parent is None


def test_unknown_base_commit_is_unavailable(tmp_path: Path) -> None:
    repo, _base, fix = _repo_with_fix(
        tmp_path,
        "missing-base",
        {"calc.py": BASE_CALC, "tests/test_calc.py": BASE_TEST_CALC},
    )
    patch = tmp_path / "empty.patch"
    patch.write_bytes(b"")
    suite = _extract(repo, "2" * 40, fix, patch)
    assert suite.status == "unavailable"
    assert any("base commit not present" in refusal for refusal in suite.refusals)


def test_garbage_hidden_patch_is_unavailable(tmp_path: Path) -> None:
    repo, base, fix = _repo_with_fix(
        tmp_path,
        "garbage",
        {"calc.py": BASE_CALC, "tests/test_calc.py": BASE_TEST_CALC},
        fix_modify={
            "tests/test_calc.py": BASE_TEST_CALC + "\n\ndef test_new():\n    assert True\n"
        },
    )
    patch = tmp_path / "hidden.patch"
    patch.write_text("this is not a patch\n", encoding="utf-8")
    suite = _extract(repo, base, fix, patch)
    assert suite.status == "unavailable"
    assert suite.tests == [] and suite.files == []
    assert any("hidden patch" in refusal for refusal in suite.refusals)


def test_merge_fix_is_unavailable(tmp_path: Path) -> None:
    repo = tmp_path / "merge"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "tests/test_calc.py", BASE_TEST_CALC)
    base = _commit(repo, "base")
    _git(repo, "checkout", "-q", "-b", "side", base)
    _write(repo, "tests/test_side.py", "def test_side():\n    assert True\n")
    _commit(repo, "side")
    _git(repo, "checkout", "-q", "main")
    _write(repo, "tests/test_main.py", "def test_main():\n    assert True\n")
    _commit(repo, "main")
    _git(repo, "merge", "-q", "--no-ff", "side", "-m", "merge side")
    fix = _git(repo, "rev-parse", "HEAD")
    patch = tmp_path / "empty.patch"
    patch.write_bytes(b"")
    suite = _extract(repo, base, fix, patch)
    assert suite.status == "unavailable"
    assert any("merge" in refusal for refusal in suite.refusals)
    assert suite.fix_parent is None


@pytest.mark.parametrize(
    "rel",
    ["../evil.py", "/abs/evil.py", ".git/hooks/evil.py", "tests/../../evil.py"],
)
def test_unsafe_hidden_paths_are_unavailable(tmp_path: Path, rel: str) -> None:
    repo, base, fix = _repo_with_fix(
        tmp_path,
        "unsafe-hidden",
        {"tests/test_calc.py": BASE_TEST_CALC},
        fix_modify={
            "tests/test_calc.py": BASE_TEST_CALC + "\n\ndef test_new():\n    assert True\n"
        },
    )
    patch = tmp_path / "hidden.patch"
    patch.write_text(
        f"diff --git a/{rel} b/{rel}\n"
        f"--- a/{rel}\n"
        f"+++ b/{rel}\n"
        "@@ -0,0 +1 @@\n"
        "+evil\n",
        encoding="utf-8",
    )
    suite = _extract(repo, base, fix, patch)
    assert suite.status == "unavailable"
    assert suite.tests == [] and suite.files == []


def test_missing_hidden_binary_bytes_are_unavailable(tmp_path: Path) -> None:
    repo, base, fix = _repo_with_fix(
        tmp_path, "missing-binary", {"tests/test_calc.py": BASE_TEST_CALC},
        fix_modify={
            "tests/test_calc.py": BASE_TEST_CALC + "\n\ndef test_new():\n    assert add(4, 5) == 9\n"
        },
    )
    patch = tmp_path / "hidden.patch"
    patch.write_text(
        "diff --git a/tests/missing.bin b/tests/missing.bin\n"
        "new file mode 100644\n"
        "index " + "0" * 40 + ".." + "a" * 40 + "\n"
        "Binary files /dev/null and b/tests/missing.bin differ\n",
        encoding="utf-8",
    )
    suite = _extract(repo, base, fix, patch)
    assert suite.status == "unavailable"
    assert suite.tests == [] and suite.files == []


def test_symlink_fix_entry_is_unavailable(tmp_path: Path) -> None:
    repo = tmp_path / "symlink"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "tests/test_calc.py", BASE_TEST_CALC)
    base = _commit(repo, "base")
    (repo / "tests" / "link_test.py").symlink_to("test_calc.py")
    fix = _commit(repo, "fix")
    patch = tmp_path / "empty.patch"
    patch.write_bytes(b"")
    suite = _extract(repo, base, fix, patch)
    assert suite.status == "unavailable"
    assert any("symlink/submodule" in refusal for refusal in suite.refusals)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "/abs/path.py",
        "../escape.py",
        "a/../../escape.py",
        ".git/hooks.py",
        "a/.git/b.py",
        "a//b.py",
        "a/b.py/",
        "a:b.py",
        "-flag.py",
        "a/b.py\x01",
    ],
)
def test_suite_path_validation_rejects_unsafe(bad: str) -> None:
    with pytest.raises(ValueError):
        validate_suite_path(bad)


def test_caller_argument_errors_raise(tmp_path: Path) -> None:
    repo, base, fix = _repo_with_fix(
        tmp_path, "args", {"tests/test_calc.py": BASE_TEST_CALC}
    )
    patch = tmp_path / "empty.patch"
    patch.write_bytes(b"")
    with pytest.raises(ValueError):
        extract_suite(
            git_dir=repo,
            base_commit="short",
            fix_commit=fix,
            hidden_patch=patch,
            task_name=TASK_NAME,
            image=IMAGE,
            workdir=WORKDIR,
        )
    with pytest.raises(ValueError):
        HeldoutSuite.model_validate(
            {**_extract(repo, base, fix, patch).model_dump(mode="json"), "image": "demo:latest"}
        )
    with pytest.raises(FileNotFoundError):
        _extract(repo, base, fix, tmp_path / "does-not-exist.patch")
    with pytest.raises(FileNotFoundError):
        extract_suite(
            git_dir=tmp_path / "does-not-exist",
            base_commit=base,
            fix_commit=fix,
            hidden_patch=patch,
            task_name=TASK_NAME,
            image=IMAGE,
            workdir=WORKDIR,
        )


def test_write_refuses_overwrite_and_load_validates(tmp_path: Path) -> None:
    repo, base, fix = _repo_with_fix(
        tmp_path,
        "roundtrip",
        {"tests/test_calc.py": BASE_TEST_CALC},
        fix_modify={
            "tests/test_calc.py": BASE_TEST_CALC + "\n\ndef test_new():\n    assert True\n"
        },
    )
    patch = tmp_path / "empty.patch"
    patch.write_bytes(b"")
    suite = _extract(repo, base, fix, patch)
    assert suite.status == "ready"
    out = tmp_path / "suite.json"
    write_suite(suite, out)
    with pytest.raises(FileExistsError):
        write_suite(suite, out)
    assert load_suite(out) == suite
    bad = json.loads(out.read_text(encoding="utf-8"))
    bad["schema_version"] = "heldout-suite/v9"
    poisoned = tmp_path / "poisoned.json"
    poisoned.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_suite(poisoned)
    forbidden = json.loads(out.read_text(encoding="utf-8"))
    forbidden["tests"][0]["node_id"] = "tests/test_calc.py::renamed"
    mismatched = tmp_path / "mismatched.json"
    mismatched.write_text(json.dumps(forbidden), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_suite(mismatched)


