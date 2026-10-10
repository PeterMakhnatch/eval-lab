"""Operational-replay regression tests: C-quoted paths, fsck tolerance,
.git-nested fixtures, gitlink/case-collision unpack gates, failure base attach.
Appended for the reference-fix slice (local repro of sweep failure classes).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "research/experiments/leak-oracle/extract.py"
spec = importlib.util.spec_from_file_location("leak_oracle_regress", SCRIPT)
oracle = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = oracle
spec.loader.exec_module(oracle)


class History:
    """All mutating Git commands are confined to pytest's disposable repo."""

    def __init__(self, path: Path):
        self.path = path
        path.mkdir()
        self.clock = 0
        self.git("init", "--quiet")
        self.git("symbolic-ref", "HEAD", "refs/heads/main")
        self.git("config", "user.name", "Leak oracle regression")
        self.git("config", "user.email", "regress@example.invalid")

    @property
    def git_dir(self) -> str:
        return str(self.path / ".git")

    def git(self, *args: str, input: str | None = None) -> str:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_AUTHOR_DATE": f"2024-01-01T00:{self.clock:02d}:00+00:00",
                "GIT_COMMITTER_DATE": f"2024-01-01T00:{self.clock:02d}:00+00:00",
            }
        )
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=" + os.devnull, *args],
            cwd=self.path,
            env=env,
            input=input,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return result.stdout.strip()

    def commit(self, changes: dict[str, str], subject: str) -> str:
        for name, body in changes.items():
            path = self.path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        self.clock += 1
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", subject)
        return self.git("rev-parse", "HEAD")

    def checkout(self, sha: str) -> None:
        self.git("checkout", "--quiet", "--detach", sha)


def package(tmp_path: Path, name: str, instruction: str, paths: list[str]) -> Path:
    task = tmp_path / name
    (task / "tests").mkdir(parents=True)
    (task / "task.toml").write_text('title = "regression fixture"\n')
    (task / "instruction.md").write_text(instruction)
    added = "from package.core import VALUE\nassert VALUE == 2\n"
    patch = ""
    for path in paths:
        patch += (
            f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -0,0 +1,2 @@\n+{added}\n"
        )
    (task / "tests/test.patch").write_text(patch)
    return task


def test_split_diff_git_literal_and_c_quoted():
    assert oracle._split_diff_git("diff --git a/plain.py b/plain.py") == (
        "plain.py",
        "plain.py",
    )
    assert oracle._split_diff_git('diff --git "a/sp ace.py" "b/sp ace.py"') == (
        "sp ace.py",
        "sp ace.py",
    )
    assert oracle._split_diff_git('diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"') == (
        "caf\u00e9.py",
        "caf\u00e9.py",
    )
    assert oracle._split_diff_git('diff --git "a/unclosed b/x') is None
    assert oracle._split_diff_git("diff --git a/x b/y extra") == ("x", "y extra")


def test_c_quoted_test_patch_selects_fix(tmp_path):
    history = History(tmp_path / "git")
    model, test = "package/core.py", "tests/sp ace.py"
    base = history.commit({model: "VALUE = 1\n", test: "# baseline\n"}, "baseline")
    fix = history.commit(
        {model: "VALUE = 2\n", test: "# baseline\n# fix\n"}, "correct value"
    )
    history.checkout(base)
    task = package(tmp_path, "quoted", "Correct value in package/core.py.", [test])
    (task / "tests/test.patch").write_text(
        'diff --git "a/tests/sp ace.py" "b/tests/sp ace.py"\n'
        '--- "a/tests/sp ace.py"\n'
        '+++ "b/tests/sp ace.py"\n'
        "@@ -1 +1,2 @@\n"
        " # baseline\n"
        "+# fix\n"
    )
    out = tmp_path / "out"
    result = oracle.extract(task, history.git_dir, out)
    assert result["status"] == "ok"
    assert result["fix"]["sha"] == fix
    assert result["task_test_files"] == [test]


def test_c_quoted_control_char_still_fails_closed(tmp_path):
    history = History(tmp_path / "git")
    history.commit({"package/core.py": "VALUE = 1\n"}, "baseline")
    task = package(tmp_path, "quoted-bad", "behavior", ["tests/test_new.py"])
    (task / "tests/test.patch").write_text(
        'diff --git "a/test\\tname.py" "b/test\\tname.py"\n'
    )
    assert (
        oracle.extract(task, history.git_dir, tmp_path / "out")["status"]
        == "unsupported-path"
    )


def test_corrupt_unreachable_object_does_not_censor_future(tmp_path):
    history = History(tmp_path / "git")
    model, test = "package/core.py", "tests/test_core.py"
    base = history.commit({model: "VALUE = 1\n", test: "# baseline\n"}, "baseline")
    fix = history.commit(
        {model: "VALUE = 2\n", test: "# baseline\n# fix\n"}, "correct value"
    )
    history.git("checkout", "--quiet", "-b", "side", fix)
    doomed = history.commit({"scratch.txt": "abandoned\n"}, "abandoned")
    history.git("checkout", "--quiet", "main")
    history.git("update-ref", "-d", "refs/heads/side")
    loose = Path(history.git_dir) / "objects" / doomed[:2] / doomed[2:]
    assert loose.is_file()
    loose.chmod(0o644)
    loose.write_bytes(b"corrupt: not a git object")
    probe = subprocess.run(
        ["git", "--git-dir", history.git_dir, "fsck", "--unreachable", "--no-reflogs"],
        capture_output=True,
        timeout=60,
    )
    if probe.returncode == 0:
        pytest.skip("platform git does not flag the corrupted loose object")
    commits, note = oracle.future_commits(history.git_dir, base)
    assert note is not None and note["returncode"] != 0
    assert fix in {c["sha"] for c in commits}
    history.git("checkout", "--quiet", base)
    task = package(tmp_path, "fsck", "Correct value in package/core.py.", [test])
    result = oracle.extract(task, history.git_dir, tmp_path / "out")
    assert result["status"] == "ok"
    assert result["fix"]["sha"] == fix
    assert result["fsck_note"] is not None


def _plumb_commit(history: History, entries: list[str], subject: str) -> str:
    """Commit entries via plumbing (gitlinks, colliding names). No checkout."""
    for entry in entries:
        mode, blob, path = entry.split(",", 2)
        history.git("update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}")
    tree = history.git("write-tree")
    head = history.git("rev-parse", "HEAD")
    history.clock += 1
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = f"2024-01-01T00:{history.clock:02d}:00+00:00"
    env["GIT_COMMITTER_DATE"] = f"2024-01-01T00:{history.clock:02d}:00+00:00"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    result = subprocess.run(
        ["git", "commit-tree", tree, "-p", head, "-m", subject],
        cwd=history.path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    sha = result.stdout.strip()
    history.git("update-ref", "refs/heads/main", sha)
    return sha


def test_git_nested_fixture_skips_not_fails(tmp_path, monkeypatch):
    # Modern git refuses to even hash .git-nested trees, so the ancient-tree
    # shape (dulwich fixtures) is injected at the ls-tree seam; the skip
    # logic itself is what is under test. Real-world proof comes from reruns.
    history = History(tmp_path / "git")
    model, test = "package/core.py", "tests/test_core.py"
    base = history.commit({model: "VALUE = 1\n", test: "# baseline\n"}, "baseline")
    blob = history.git("hash-object", "-w", "--stdin", input="[fixture]\n")
    nested = "tests/data/repos/a/.git/HEAD"
    real_run_git = oracle.run_git

    def fake_ls_tree(git_dir: str, *args: str, **kwargs: object) -> str:
        out = real_run_git(git_dir, *args, **kwargs)
        if args[:2] == ("ls-tree", "-rz") and "--name-only" in args:
            return out + "\x00" + nested + "\x00"
        if args[:2] == ("ls-tree", "-rz"):
            return out + f"\x00100644 blob {blob}\t{nested}\x00"
        return out

    monkeypatch.setattr(oracle, "run_git", fake_ls_tree)
    paths, skipped = oracle.base_tree_paths(history.git_dir, base)
    assert skipped == 1
    assert nested not in paths
    assert test in paths
    target = tmp_path / "unpacked"
    target.mkdir()
    seen: list[str] = []
    oracle.unpack_base(history.git_dir, base, target, skipped=seen)
    assert seen == [nested]
    assert (target / test).is_file()


def test_gitlink_base_is_unsupported_tree_with_base_attached(tmp_path):
    history = History(tmp_path / "git")
    model, test = "package/core.py", "tests/test_core.py"
    history.commit({model: "VALUE = 1\n", test: "# baseline\n"}, "baseline")
    blob = history.git("hash-object", "-w", "--stdin", input="submodule pointer\n")
    base = _plumb_commit(history, [f"160000,{blob},vendor/submod"], "vendor submodule")
    history.commit({model: "VALUE = 2\n", test: "# baseline\n# fix\n"}, "correct value")
    history.checkout(base)
    task = package(tmp_path, "gitlink", "Correct value in package/core.py.", [test])
    out = tmp_path / "out"
    result = oracle.extract(task, history.git_dir, out)
    assert result["status"] == "unsupported-tree"
    assert "submodules" in result["rationale"]
    assert result["base"] == base
    assert result["fix"] is None
    assert not (out / "solution.patch").exists()
    target = tmp_path / "unpacked"
    target.mkdir()
    with pytest.raises(oracle.ExtractionError):
        oracle.unpack_base(history.git_dir, base, target)


def test_case_colliding_base_needs_sensitive_fs(tmp_path):
    # Built via plumbing: no worktree on any host fs can hold both names.
    history = History(tmp_path / "git")
    history.commit({"seed.py": "x\n"}, "baseline")
    blob_a = history.git("hash-object", "-w", "--stdin", input="A = 1\n")
    blob_b = history.git("hash-object", "-w", "--stdin", input="B = 2\n")
    base = _plumb_commit(
        history, [f"100644,{blob_a},Foo.py", f"100644,{blob_b},foo.py"], "collision"
    )
    sensitive_target = tmp_path / "sensitive"
    sensitive_target.mkdir()
    oracle.unpack_base(history.git_dir, base, sensitive_target, case_sensitive=True)
    assert (sensitive_target / "Foo.py").is_file()
    assert (sensitive_target / "foo.py").is_file()
    with pytest.raises(oracle.ExtractionError) as excinfo:
        oracle.unpack_base(
            history.git_dir, base, tmp_path / "folded", case_sensitive=False
        )
    assert excinfo.value.status == "unsupported-tree"
    assert isinstance(oracle._fs_case_sensitive(tmp_path / "probe"), bool)
    assert not (tmp_path / "probe" / ".oracle-case-probe-AA").exists()


def test_ref_tips_attribute_identifiers_across_refs(tmp_path):
    history = History(tmp_path / "git")
    base = history.commit({"main.py": "x\n"}, "baseline")
    history.git("checkout", "--quiet", "-b", "alpha", base)
    history.commit({"pkg/a.py": "ALPHA_MARKER = 1\n"}, "alpha work")
    history.git("checkout", "--quiet", "main")
    history.git("checkout", "--quiet", "-b", "beta", base)
    history.commit({"pkg/b.py": "BETA_MARKER = 2\n"}, "beta work")
    history.git("checkout", "--quiet", "main")
    tips = oracle.ref_tips_with(history.git_dir, ["ALPHA_MARKER", "BETA_MARKER"], base)
    by_ref = {t["ref"]: t for t in tips}
    assert by_ref["refs/heads/alpha"]["identifiers_found"] == ["ALPHA_MARKER"]
    assert by_ref["refs/heads/beta"]["identifiers_found"] == ["BETA_MARKER"]
    assert oracle.ref_tips_with(history.git_dir, ["NO_SUCH_MARKER"], base) == []


def test_go_and_js_test_files_recognized():
    assert oracle.is_test_path("state/transition_recoverable_test.go")
    assert oracle.is_test_path("txpool/decrease_nonce_test.go")
    assert oracle.is_test_path("src/comp.test.ts")
    assert oracle.is_test_path("src/comp.spec.tsx")
    assert oracle.is_test_path("__tests__/comp.js")
    assert not oracle.is_test_path("state/transition.go")
    assert not oracle.is_test_path("src/comp.ts")
    assert not oracle.is_test_path("package/core.py")
    assert oracle.is_test_path("tests/test_core.py")
