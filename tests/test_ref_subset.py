"""Reference-subset probe: change blocks, minimisation, severity, patches and the trace check."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest

from evallab import ref_subset
from evallab.ref_subset import (
    TRACE_SLACK,
    Block,
    FileChange,
    Grade,
    Package,
    Probe,
    Status,
    _dockerfile_bases,
    block_kind,
    compose,
    edited_ranges,
    file_change,
    modal_execution_bound,
    mutation_pass,
    patch_code_blocks,
    probe_task,
    summarize,
    trace_check,
    unified_patch,
)
from evallab.regrade import SpendCapReached

_NUMBERED = [f"line{i}\n" for i in range(40)]


def _edited(*indexes: int) -> tuple[bytes, bytes]:
    """A 40-line file and a copy with each of ``indexes`` rewritten (separate blocks)."""
    fixed = [f"fixed{i}\n" if i in indexes else line for i, line in enumerate(_NUMBERED)]
    return "".join(_NUMBERED).encode(), "".join(fixed).encode()


_SEPARATE_TOML = """
artifacts = ["/app/out/", {source = "/app/main.py"}]
[task]
name = "bench/demo"
[verifier]
environment_mode = "separate"
timeout_sec = 120.0
[environment]
gpus = 0
"""


def _separate_package(root: Path, toml: str = _SEPARATE_TOML) -> Path:
    for name in (
        "environment/Dockerfile",
        "tests/Dockerfile",
        "tests/test.sh",
        "solution/solve.sh",
    ):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("#\n")
    (root / "task.toml").write_text(toml)
    return root


def _mimo_package(root: Path) -> Path:
    for name in ("environment/setup/setup.sh", "tests/test.sh"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("#\n")
    (root / "task.toml").write_text('[environment]\ndocker_image = "repo/img@sha256:00"\n')
    return root


def test_separate_verifier_task_loads_its_artifacts_and_harbor_name(tmp_path: Path) -> None:
    package = Package.load(_separate_package(tmp_path))
    assert package.separate
    assert package.task_id == "bench/demo"
    assert package.artifacts == ("/app/out", "/app/main.py")
    assert package.verifier_timeout == 120.0
    assert not Package.load(_mimo_package(tmp_path / "mimo")).separate


@pytest.mark.parametrize(
    "toml",
    [
        _SEPARATE_TOML.replace(
            '{source = "/app/main.py"}', '{source = "/app/main.py", destination = "x"}'
        ),
        _SEPARATE_TOML.replace('"/app/out/", ', '"relative/out", '),
        _SEPARATE_TOML.replace('artifacts = ["/app/out/", {source = "/app/main.py"}]', ""),
    ],
    ids=["remapped", "relative", "no-artifacts"],
)
def test_separate_verifier_task_rejects_artifacts_it_cannot_reproduce(
    tmp_path: Path, toml: str
) -> None:
    with pytest.raises(ValueError):
        Package.load(_separate_package(tmp_path, toml))


def test_reference_source_must_match_the_package_layout(tmp_path: Path) -> None:
    patch = tmp_path / "ref.patch"
    patch.write_text("")
    with pytest.raises(ValueError, match="derives its reference from its oracle"):
        probe_task(_separate_package(tmp_path / "sep"), patch, tmp_path / "out1")
    with pytest.raises(ValueError, match="needs its reference --patch"):
        probe_task(_mimo_package(tmp_path / "mimo"), None, tmp_path / "out2")


def test_dockerfile_bases_skip_stage_aliases_scratch_and_build_args(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "ARG BASE=python:3.12\n"
        "FROM --platform=linux/amd64 golang:1.22 AS build\n"
        "FROM build AS test\n"
        "from python:3.13-slim\n"
        "FROM ${BASE}\n"
        "FROM scratch\n"
        "FROM golang:1.22\n"
    )
    # Only registry images the build pulls may be removed afterwards; never a stage alias.
    assert _dockerfile_bases(dockerfile) == ["golang:1.22", "python:3.13-slim"]


# ---------------------------------------------------------------------------
# file_change / compose


def test_compose_applies_exactly_the_kept_blocks() -> None:
    base, fixed = _edited(1, 7)
    change = file_change("pkg/f.go", base, fixed, set())
    first, second = [block.id for block in change.blocks]
    assert [(b.base_start, b.base_end) for b in change.blocks] == [(1, 2), (7, 8)]

    only_first = compose(change, {first})
    assert only_first is not None
    lines = only_first.decode().splitlines()
    assert lines[1] == "fixed1" and lines[7] == "line7"
    only_second = compose(change, {second})
    assert only_second is not None
    lines = only_second.decode().splitlines()
    assert lines[1] == "line1" and lines[7] == "fixed7"

    assert compose(change, {first, second}) == fixed
    assert compose(change, set()) == base
    assert compose(change, {"unrelated#9"}) == base


def test_compose_new_and_deleted_files_toggle_between_absent_and_present() -> None:
    new = file_change("new.go", None, b"a\nb\n", set())
    assert [(b.removed, b.added) for b in new.blocks] == [(0, 2)]
    assert compose(new, {new.blocks[0].id}) == b"a\nb\n"
    assert compose(new, set()) is None

    gone = file_change("old.go", b"a\nb\n", None, set())
    assert [(b.removed, b.added) for b in gone.blocks] == [(2, 0)]
    assert compose(gone, {gone.blocks[0].id}) is None
    assert compose(gone, set()) == b"a\nb\n"


def test_binary_file_is_one_opaque_block() -> None:
    change = file_change("blob.bin", b"\xff\x00old", b"\xff\x00new", set())
    assert len(change.blocks) == 1 and change.blocks[0].kind == "code"
    assert compose(change, {change.blocks[0].id}) == b"\xff\x00new"
    assert compose(change, set()) == b"\xff\x00old"


# ---------------------------------------------------------------------------
# Block kinds


def test_block_kind_by_path_and_comment_syntax() -> None:
    tests = {"tests/test_a.py"}
    # Comment-only runs depend on the language's comment prefix.
    assert block_kind("a.py", ["# old\n"], ["# new\n", "\n"], tests) == "comment"
    assert block_kind("a.go", ["// old\n"], ["// new\n"], tests) == "comment"
    assert block_kind("a.go", ["// old\n"], ["x := 1 // new\n"], tests) == "code"
    assert block_kind("a.go", ["# not a go comment\n"], [], tests) == "code"
    assert block_kind("a.py", ["// not a python comment\n"], [], tests) == "code"
    # Documentation paths are noncode whatever the lines say.
    assert block_kind("docs/x.md", ["x = 1\n"], ["x = 2\n"], tests) == "noncode"
    # Interpreter byte-code an oracle run leaves behind is never graded code, even when opaque.
    cache = file_change("app/__pycache__/m.cpython-311.pyc", b"\x00\xff", b"\x01\xfe", tests)
    assert [block.kind for block in cache.blocks] == ["noncode"]
    assert block_kind("CHANGELOG.md", [], ["- fix\n"], tests) == "noncode"
    assert block_kind("docs/guide/run.py", ["x = 1\n"], ["x = 2\n"], tests) == "noncode"
    assert block_kind("recipes/examples/demo.py", [], ["x = 1\n"], tests) == "noncode"
    assert block_kind("MANIFEST.in", [], ["include x.py\n"], tests) == "noncode"
    # A module merely named like a doc file is code.
    assert block_kind("pkg/history.py", ["x = 1\n"], ["x = 2\n"], tests) == "code"
    assert block_kind("HISTORY", [], ["1.0: fix\n"], tests) == "noncode"
    # Release-note fragments count only at the repository root; a package named `news` is code.
    assert block_kind("news/6795.bugfix", [], ["Fix auth.\n"], tests) == "noncode"
    assert block_kind("src/news/feed.py", ["x = 1\n"], ["x = 2\n"], tests) == "code"
    assert block_kind(".gitignore", [], ["build/\n"], tests) == "noncode"
    # A reset-test file is a test even though its lines are code.
    assert block_kind("tests/test_a.py", ["x = 1\n"], ["x = 2\n"], tests) == "test"
    assert block_kind("tests/test_b.py", ["x = 1\n"], ["x = 2\n"], tests) == "code"


def test_python_docstring_only_block_is_reclassified_comment() -> None:
    base = b'def f(x):\n    """Old doc."""\n    return x + 1\n'
    docstring = b'def f(x):\n    """New doc."""\n    return x + 1\n'
    statement = b'def f(x):\n    """Old doc."""\n    return x + 2\n'
    assert [b.kind for b in file_change("m.py", base, docstring, set()).blocks] == ["comment"]
    assert [b.kind for b in file_change("m.py", base, statement, set()).blocks] == ["code"]

    both = b'def f(x):\n    """New doc."""\n    return x + 2\n'
    change = file_change("m.py", base, both, set())
    assert len(change.blocks) == 1  # adjacent edits are one run: the statement keeps it code
    assert change.blocks[0].kind == "code"

    # Separated docstring and statement edits split, and only the docstring becomes a comment.
    base = b'def f(x):\n    """Old doc."""\n    y = x\n    z = y\n    return z + 1\n'
    both = b'def f(x):\n    """New doc."""\n    y = x\n    z = y\n    return z + 2\n'
    assert [b.kind for b in file_change("m.py", base, both, set()).blocks] == ["comment", "code"]


# ---------------------------------------------------------------------------
# Probe


def _files(*names: str) -> tuple[list[FileChange], list[str]]:
    """One code block per file."""
    changes = [file_change(f"{name}.go", b"x := 0\n", b"x := 1\n", set()) for name in names]
    return changes, [change.blocks[0].id for change in changes]


def _one_file() -> tuple[list[FileChange], list[str]]:
    """Three code blocks in one file."""
    base, fixed = _edited(2, 10, 20)
    change = file_change("f.go", base, fixed, set())
    return [change], [block.id for block in change.blocks]


_LAYOUTS = {
    "files": lambda: _files("a", "b", "c"),
    "one-file": _one_file,
}


def _fake(
    reward_for: Callable[[set[str]], float],
) -> tuple[Callable[[str, set[str]], Grade], list[Grade]]:
    seen: list[Grade] = []

    def grade(label: str, keep: set[str]) -> Grade:
        result = Grade(label, sorted(keep), reward_for(keep), 0, 0.0, "scored")
        seen.append(result)
        return result

    return grade, seen


@pytest.mark.parametrize(
    ("full_reward", "nop_reward", "nop_status"),
    [(0.0, 0.0, "scored"), (1.0, 1.0, "scored"), (1.0, None, "error")],
    ids=["full-fails", "nop-passes", "nop-unscored"],
)
def test_probe_stops_when_baselines_disagree(
    full_reward: float,
    nop_reward: float | None,
    nop_status: Literal["scored", "unscored", "timeout", "error"],
) -> None:
    changes, ids = _files("a", "b", "c")
    seen: list[Grade] = []

    def grade(label: str, keep: set[str]) -> Grade:
        full = label == "full"
        result = Grade(
            label,
            sorted(keep),
            full_reward if full else nop_reward,
            0,
            0.0,
            "scored" if full else nop_status,
        )
        seen.append(result)
        return result

    status, keep, reason = Probe(changes, grade).run()
    assert status == "baseline_mismatch"
    assert keep == set(ids)
    assert [g.label for g in seen] == ["full", "nop"]
    assert "expected 1 and 0" in reason
    assert summarize("t", changes, status, keep, reason, seen)["verdict"] == "unknown"


@pytest.mark.parametrize("layout", _LAYOUTS)
def test_probe_keeps_only_the_block_the_tests_need(layout: str) -> None:
    changes, ids = _LAYOUTS[layout]()
    needed = ids[1]
    grade, seen = _fake(lambda keep: 1.0 if needed in keep else 0.0)
    probe = Probe(changes, grade)
    status, keep, _ = probe.run()

    assert (status, keep) == ("measured", {needed})
    assert probe.grades == seen
    assert seen[0].label == "full" and seen[1].label == "nop"
    assert seen[-1].label == "confirm" and seen[-1].kept == [needed]
    report = summarize("t", changes, status, keep, "", seen)
    assert report["verdict"] == "fail"
    assert report["dropped_code"] == [ids[0], ids[2]]
    assert report["kept"] == [needed]


@pytest.mark.parametrize("layout", _LAYOUTS)
def test_probe_needing_every_block_keeps_all_and_skips_confirmation(layout: str) -> None:
    changes, ids = _LAYOUTS[layout]()
    grade, seen = _fake(lambda keep: 1.0 if set(ids) <= keep else 0.0)
    status, keep, _ = Probe(changes, grade).run()

    assert (status, keep) == ("measured", set(ids))
    assert all(g.label != "confirm" for g in seen)
    report = summarize("t", changes, status, keep, "", seen)
    assert report["verdict"] == "pass"
    assert report["severity"] is None
    assert report["dropped_code"] == []


@pytest.mark.parametrize("layout", _LAYOUTS)
def test_probe_budget_exhaustion_still_returns_a_passing_subset(layout: str) -> None:
    changes, ids = _LAYOUTS[layout]()
    needed = ids[1]
    grade, seen = _fake(lambda keep: 1.0 if needed in keep else 0.0)
    status, keep, reason = Probe(changes, grade, max_grades=4).run()

    assert status == "budget_exhausted"
    assert needed in keep and keep < set(ids)  # progress was made and the keep set passes
    assert len(seen) <= 4
    assert seen[-1].label == "confirm" and seen[-1].passed
    assert "4 grades" in reason
    # Budget-limited results are still reported as measured findings.
    assert summarize("t", changes, status, keep, reason, seen)["verdict"] == "fail"


def test_probe_without_two_code_blocks_is_not_applicable_and_never_grades() -> None:
    code = file_change("a.go", b"x := 0\n", b"x := 1\n", set())
    docs = file_change("docs/a.md", b"old\n", b"new\n", set())
    tests = file_change("tests/t.go", b"x := 0\n", b"x := 1\n", {"tests/t.go"})
    assert [b.kind for b in (*code.blocks, *docs.blocks, *tests.blocks)] == [
        "code",
        "noncode",
        "test",
    ]

    def grade(label: str, keep: set[str]) -> Grade:
        raise AssertionError(f"graded {label}")

    status, keep, _ = Probe([code, docs, tests], grade).run()
    assert status == "not_applicable"
    assert keep == {code.blocks[0].id, docs.blocks[0].id}  # test blocks are never part of a subset


def test_probe_flags_a_subset_that_fails_its_confirmation_as_flaky() -> None:
    changes, ids = _files("a", "b", "c")
    needed = ids[1]
    graded: set[frozenset[str]] = set()

    def reward(keep: set[str]) -> float:
        key = frozenset(keep)
        first_time = key not in graded
        graded.add(key)
        # Only the minimal subset is unreliable: it passes once, then fails.
        return 1.0 if needed in keep and (key != {needed} or first_time) else 0.0

    grade, seen = _fake(reward)
    status, keep, reason = Probe(changes, grade).run()
    assert status == "flaky"
    assert keep == {needed}
    assert [g.reward for g in seen if g.kept == [needed]] == [1.0, 0.0]
    assert seen[-1].label == "confirm"
    assert summarize("t", changes, status, keep, reason, seen)["verdict"] == "unknown"


# ---------------------------------------------------------------------------
# summarize


def _sized(kept: int, dropped: int) -> tuple[list[FileChange], set[str]]:
    blocks = [
        Block("k", "a.go", "code", 0, 1, 0, kept),
        Block("d", "b.go", "code", 0, 1, 0, dropped),
        Block("doc", "docs/x.md", "noncode", 0, 1, 0, 500),  # noncode lines never count
    ]
    changes = [FileChange(b.path, None, None, [b]) for b in blocks]
    return changes, {"k"}


@pytest.mark.parametrize(
    ("kept", "dropped", "severity"),
    [
        (1, 3, "high"),  # exactly 25%
        (26, 74, "medium"),  # just above 25%
        (1, 1, "medium"),  # exactly 50%
        (51, 49, "low"),  # just above 50%
        (99, 1, "low"),
    ],
)
def test_summarize_severity_boundaries(kept: int, dropped: int, severity: str) -> None:
    changes, keep = _sized(kept, dropped)
    report = summarize("t", changes, "measured", keep, "", [])
    assert report["verdict"] == "fail"
    assert report["severity"] == severity
    assert report["kept_fraction"] == round(kept / (kept + dropped), 4)
    assert report["code_lines"] == kept + dropped


@pytest.mark.parametrize("status", ["baseline_mismatch", "flaky", "not_applicable", "unavailable"])
def test_summarize_verdict_is_unknown_unless_measured(status: Status) -> None:
    changes, keep = _sized(1, 9)  # a dropped block would be "fail" if the run were measured
    report = summarize("t", changes, status, keep, "why", [])
    assert report["verdict"] == "unknown"
    assert report["severity"] is None
    assert report["reason"] == "why"


# ---------------------------------------------------------------------------
# Line-level pass


def _grade_f5(label: str, files: dict[str, bytes | None]) -> Grade:
    """A hidden test that checks only f(5) == 6."""
    namespace: dict[str, Any] = {}
    exec(files["m.py"] or b"", namespace)  # noqa: S102 - the probe's own test fixture code
    return Grade(label, [], 1.0 if namespace["f"](5) == 6 else 0.0, 0, 0.0, "scored")


def test_mutation_pass_flags_an_unpinned_branch_inside_a_needed_block() -> None:
    base = b"def f(x):\n    return x\n"
    fixed = b"def f(x):\n    if x > 0:\n        return x + 1\n    return x\n"
    changes = [file_change("m.py", base, fixed, set())]
    grades: list[Grade] = []
    summary, survivors = mutation_pass(changes, _grade_f5, grades, limit=50, seed="t")
    assert [g.label for g in grades[:2]] == ["full", "nop"]
    # The `+ 1` is pinned by the test; the `> 0` boundary (one block with it) is not.
    assert summary["verdict"] == "fail"
    assert summary["killed"] >= 1
    assert {s["line"] for s in summary["survivors"]} == {2}
    assert set(survivors) == {s["id"] for s in summary["survivors"]}
    for mutated in survivors.values():
        assert _grade_f5("again", {"m.py": mutated}).reward == 1.0 and mutated != fixed


def test_mutation_pass_grades_no_mutant_when_the_baselines_do_not_hold() -> None:
    changes = [file_change("m.py", b"x = 0\n", b"x = 1 + 1\n", set())]
    grades: list[Grade] = []

    def always(label: str, files: dict[str, bytes | None]) -> Grade:
        return Grade(label, [], 1.0, 0, 0.0, "scored")

    summary, survivors = mutation_pass(changes, always, grades, limit=10, seed="t")
    assert summary["status"] == "baseline_mismatch" and summary["verdict"] == "unknown"
    assert [g.label for g in grades] == ["full", "nop"] and survivors == {}


# ---------------------------------------------------------------------------
# Patches


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_unified_patch_applies_with_git_and_reproduces_kept_contents(tmp_path: Path) -> None:
    base_a, fixed_a = _edited(1, 7)
    base: dict[str, bytes | None] = {
        "pkg/a.go": base_a,
        "pkg/new.go": None,
        "old.txt": b"gone\n",
        "untouched.go": b"same\n",
    }
    fixed: dict[str, bytes | None] = {
        "pkg/a.go": fixed_a,
        "pkg/new.go": b"fresh\n",
        "old.txt": None,
        "untouched.go": b"same\n",
    }
    changes = [file_change(path, base[path], fixed[path], set()) for path in base]
    # Keep one of a.go's two edits, the new file and the deletion.
    keep = {changes[0].blocks[1].id, changes[1].blocks[0].id, changes[2].blocks[0].id}
    after = {change.path: compose(change, keep) for change in changes}
    assert after["pkg/a.go"] not in {base_a, fixed_a}

    assert unified_patch(base, base) == ""
    patch = unified_patch(base, after)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for path, data in base.items():
        if data is not None:
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_bytes(data)
    (tmp_path / "kept.patch").write_text(patch)
    _git(repo, "apply", "--whitespace=nowarn", str(tmp_path / "kept.patch"))

    for path, expected in after.items():
        target = repo / path
        assert (target.read_bytes() if target.is_file() else None) == expected


_PATCH = """\
diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1,9 +1,9 @@
 import os
-x = 1
+x = 2
 y = 0
 z = 0
 # keep
 w = 0
 v = 0
-return_value = 1
+return_value = 2
 end = 0
diff --git a/docs/x.md b/docs/x.md
--- a/docs/x.md
+++ b/docs/x.md
@@ -1,2 +1,2 @@
-old
+new
 tail
diff --git a/src/b.py b/src/b.py
--- a/src/b.py
+++ b/src/b.py
@@ -1,3 +1,3 @@
 head
-# old comment
+# new comment
 tail
diff --git a/tests/test_a.py b/tests/test_a.py
--- a/tests/test_a.py
+++ b/tests/test_a.py
@@ -1,2 +1,2 @@
-assert f() == 1
+assert f() == 2
 tail
diff --git a/src/c.go b/src/c.go
--- a/src/c.go
+++ b/src/c.go
@@ -1,2 +1,3 @@
 func f() {
+	return
 }
"""


def test_patch_code_blocks_counts_only_code_runs() -> None:
    # Two separated runs in a.py + one in c.go; docs, a comment-only run and a reset test excluded.
    assert patch_code_blocks(_PATCH, {"tests/test_a.py"}) == 3
    # Without the reset list the test file's edit is ordinary code.
    assert patch_code_blocks(_PATCH) == 4
    assert patch_code_blocks("") == 0


# ---------------------------------------------------------------------------
# Trace T0


def test_edited_ranges_maps_hunks_to_base_lines() -> None:
    diff = (
        "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n"
        "@@ -10,0 +11,2 @@\n+added1\n+added2\n tail\n"
        "@@ -20,3 +22,2 @@ def f():\n ctx\n-gone1\n-gone2\n+new\n tail\n"
        "diff --git a/g.py b/g.py\n--- a/g.py\n+++ b/g.py\n"
        "@@ -7 +7 @@\n-old\n+new\n"
    )
    assert edited_ranges(diff) == {
        # Insert after base line 10 (0-based: before index 10): reported on the next line.
        "f.py": [(10, 11), (20, 22)],
        "g.py": [(6, 7)],
    }


def test_edited_ranges_keeps_an_edit_that_ends_its_hunk() -> None:
    diff = (
        "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n"
        "@@ -10,0 +11,2 @@\n+added1\n+added2\n"
        "@@ -30,2 +32,2 @@\n ctx\n-old\n+new\n"
        "diff --git a/g.py b/g.py\n--- a/g.py\n+++ b/g.py\n"
        "@@ -3,2 +3,2 @@\n ctx\n-old\n+new\n"
        "diff --git a/h.py b/h.py\n--- a/h.py\n+++ b/h.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    assert edited_ranges(diff) == {"f.py": [(10, 11), (30, 31)], "g.py": [(3, 4)], "h.py": [(0, 1)]}


def _report() -> dict[str, Any]:
    """Task report: two separated code blocks (lines 4 and 30), only the first one tested."""
    base, fixed = _edited(4, 30)
    changes = [file_change("src/m.go", base, fixed, set())]
    tested, untested = [block.id for block in changes[0].blocks]
    report = summarize("task-1", changes, "measured", {tested}, "", [])
    assert report["dropped_code"] == [untested] and report["severity"] == "medium"
    return report


def _trial(root: Path, *, reward: str | None, diff: str | None, name: str = "org/task-1") -> Path:
    trial = root / "trial-1"
    (trial / "verifier").mkdir(parents=True)
    (trial / "result.json").write_text(json.dumps({"task_name": name}))
    if reward is not None:
        (trial / "verifier" / "reward.txt").write_text(reward)
    if diff is not None:
        (trial / "verifier" / "agent.diff").write_text(diff)
    return trial


def _hunk(first_line: int) -> str:
    """A diff editing base line ``first_line`` (1-based) of src/m.go."""
    return (
        "diff --git a/src/m.go b/src/m.go\n--- a/src/m.go\n+++ b/src/m.go\n"
        f"@@ -{first_line},1 +{first_line},1 @@\n-line{first_line - 1}\n+changed\n"
    )


def test_trace_check_flags_a_pass_that_skips_the_untested_block(tmp_path: Path) -> None:
    trial = _trial(tmp_path, reward="1\n", diff=_hunk(5))  # base index 4: the tested block
    result = trace_check(trial, _report())
    assert result["verdict"] == "fail"
    assert result["category"] == "false_positive"
    assert result["severity"] == "medium"
    assert result["fault"] == "eval"
    assert result["subject"] == {
        "kind": "trace",
        "task_id": "task-1",
        "task_digest": None,
        "trial_id": "trial-1",
    }
    assert result["metrics"] == {
        "tested_blocks": 1,
        "tested_touched": 1,
        "untested_blocks": 1,
        "untested_touched": 0,
        "untested_lines": 2,
    }


def test_trace_check_passes_when_the_diff_touches_the_untested_block(tmp_path: Path) -> None:
    trial = _trial(tmp_path, reward="1", diff=_hunk(5) + _hunk(31))  # base indexes 4 and 30
    result = trace_check(trial, _report())
    assert result["verdict"] == "pass"
    assert result["severity"] is None and result["fault"] is None
    assert result["metrics"]["untested_touched"] == 1


@pytest.mark.parametrize(
    ("index", "verdict"),
    [
        (30 - TRACE_SLACK - 1, "fail"),
        (30 - TRACE_SLACK, "pass"),
        (30 + TRACE_SLACK, "pass"),
        (30 + TRACE_SLACK + 1, "fail"),
    ],
)
def test_trace_check_matches_edits_within_the_slack_of_a_block(
    tmp_path: Path, index: int, verdict: str
) -> None:
    # The untested block is base line index 30; an edit this far away does (not) count as touching it.
    trial = _trial(tmp_path, reward="1", diff=_hunk(index + 1))
    assert trace_check(trial, _report())["verdict"] == verdict


def test_trace_check_is_unknown_without_a_passing_reward_or_a_usable_report(
    tmp_path: Path,
) -> None:
    failed = _trial(tmp_path / "a", reward="0", diff=_hunk(5))
    assert trace_check(failed, _report())["verdict"] == "unknown"
    unscored = _trial(tmp_path / "b", reward=None, diff=_hunk(5))
    assert trace_check(unscored, _report())["verdict"] == "unknown"

    passed = _trial(tmp_path / "c", reward="1", diff=_hunk(5))
    assert trace_check(passed, None)["verdict"] == "unknown"
    unmeasured = _report() | {"verdict": "unknown", "status": "flaky"}
    assert trace_check(passed, unmeasured)["verdict"] == "unknown"

    nodiff = _trial(tmp_path / "d", reward="1", diff=None)
    assert trace_check(nodiff, _report())["verdict"] == "unknown"


def _ref_args(*arguments: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    ref_subset.add_ref_subset_parser(parser.add_subparsers(required=True))
    return parser.parse_args(["ref-subset", *arguments])


def _forbid_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a local plan must not construct a backend or execute anything")

    for name in (
        "Sandbox",
        "SeparateSandbox",
        "ImageCache",
        "_docker",
        "probe_task",
        "sweep",
        "lab_harbor_problem",
    ):
        monkeypatch.setattr(ref_subset, name, forbidden)
    monkeypatch.setitem(
        sys.modules, "evallab.ref_subset_modal", SimpleNamespace(ModalSandbox=forbidden)
    )


@pytest.mark.parametrize("environment", ["docker", "modal"])
@pytest.mark.parametrize("separate", [False, True])
def test_cli_plan_is_zero_execution_and_keeps_package_bytes_immutable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    environment: str,
    separate: bool,
) -> None:
    package = (
        _separate_package(tmp_path / "package") if separate else _mimo_package(tmp_path / "package")
    )
    patch = tmp_path / "reference.patch"
    patch.write_text(_PATCH)
    output = tmp_path / "must-not-be-created"
    arguments = [
        "run",
        str(package),
        "--output-dir",
        str(output),
        "--plan",
        "--environment",
        environment,
        "--max-usd",
        "0",
        "--usd-per-execution",
        "0.05",
    ]
    if not separate:
        arguments += ["--patch", str(patch)]
    before = {p.relative_to(package): p.read_bytes() for p in package.rglob("*") if p.is_file()}
    _forbid_execution(monkeypatch)
    args = _ref_args(*arguments)
    assert args.func(args, tmp_path) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["plan_cost_usd"] == 0 and plan["executed"] is False
    assert plan["backend"] == environment
    assert len(plan["tasks"]) == 1 and plan["tasks"][0]["task_id"] == "package"
    assert plan["tasks"][0]["package_digest"]
    bound = modal_execution_bound(separate=separate) if environment == "modal" else 0
    assert plan["reservation_units_upper_bound"] == bound
    assert plan["estimated_reservation_usd"] == bound * 0.05
    assert plan["fits_cap"] is (environment == "docker")
    assert plan["execution_refused"] is (environment == "modal")
    assert not output.exists()
    assert before == {
        p.relative_to(package): p.read_bytes() for p in package.rglob("*") if p.is_file()
    }


def test_modal_sweep_plan_lists_all_selected_tasks_and_resume_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tasks = tmp_path / "tasks"
    for name in ("one", "two", "not-selected"):
        _mimo_package(tasks / name)
    patch = tmp_path / "reference.patch"
    patch.write_text(_PATCH)
    index = tmp_path / "index.csv"
    index.write_text(
        "task_id,label,patch_path\n"
        + "".join(
            f"{name},oracle:pass+nop:fail,{patch}\n" for name in ("one", "two", "not-selected")
        )
    )
    output = tmp_path / "out"
    report = output / "one" / "report.json"
    report.parent.mkdir(parents=True)
    report.write_text('{"status": "not_applicable"}\n')
    _forbid_execution(monkeypatch)
    args = _ref_args(
        "sweep",
        "--index",
        str(index),
        "--tasks-root",
        str(tasks),
        "--output-dir",
        str(output),
        "--environment",
        "modal",
        "--lanes",
        "16",
        "--plan",
        "--task",
        "one",
        "--task",
        "two",
        "--max-usd",
        "100",
        "--approve",
        "plan approval example",
    )
    assert args.func(args, tmp_path) == 0
    plan = json.loads(capsys.readouterr().out)
    assert [task["task_id"] for task in plan["tasks"]] == ["one", "two"]
    assert [task["action"] for task in plan["tasks"]] == ["resume_skip", "probe"]
    assert plan["lanes"] == 16 and plan["reservation_units_upper_bound"] == 51
    assert plan["fits_cap"] is True and plan["execution_refused"] is False
    assert report.read_text() == '{"status": "not_applicable"}\n'
    assert not (output / "two").exists() and not (output / "run-receipt.json").exists()


@pytest.mark.parametrize(
    ("rate", "cap_delta"), [(0.0001255, 0.0), (0.000101, -2e-8)]
)
def test_modal_cli_subcent_plan_cap_matches_unrounded_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    rate: float,
    cap_delta: float,
) -> None:
    package = _separate_package(tmp_path / "package")
    units = modal_execution_bound(max_grades=4)
    exposure = units * rate
    cap = exposure + cap_delta
    _forbid_execution(monkeypatch)
    args = _ref_args(
        "run",
        str(package),
        "--output-dir",
        str(tmp_path / "out"),
        "--environment",
        "modal",
        "--max-grades",
        "4",
        "--max-usd",
        str(cap),
        "--usd-per-execution",
        str(rate),
        "--approve",
        "inert sub-cent boundary",
        "--plan",
    )
    assert args.func(args, tmp_path) == 0
    plan = json.loads(capsys.readouterr().out)
    fits = cap_delta == 0
    assert 0 < plan["estimated_reservation_usd"] < 0.01
    assert plan["estimated_reservation_usd"] == exposure
    assert plan["fits_cap"] is fits and plan["execution_refused"] is not fits
    gate = ref_subset._command_spend_gate(args)
    assert gate is not None
    for _ in range(units if fits else units - 1):
        gate.charge()
    with pytest.raises(SpendCapReached):
        gate.charge()


@pytest.mark.parametrize("estimate", ["0", "-1", "nan", "inf", "-inf"])
def test_cli_rejects_nonpositive_or_nonfinite_reservation_estimates(estimate: str) -> None:
    with pytest.raises(SystemExit) as exc:
        _ref_args("run", "unused", "--output-dir", "unused", "--usd-per-execution", estimate)
    assert exc.value.code == 2


def test_real_modal_requires_approval_before_backend_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _forbid_execution(monkeypatch)
    args = _ref_args(
        "run",
        "unused",
        "--output-dir",
        str(tmp_path / "out"),
        "--environment",
        "modal",
        "--max-usd",
        "1",
    )
    assert args.func(args, tmp_path) == 2
    assert "--approve" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_modal_api_refuses_missing_gate_or_unpinned_harbor_before_any_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab.regrade import CloudSpendGate

    output = tmp_path / "out"
    with pytest.raises(ValueError, match="spend gate"):
        probe_task(tmp_path / "unused", None, output, environment="modal")
    gate = CloudSpendGate("test approval", 1, 0.1)
    monkeypatch.setattr(ref_subset, "lab_harbor_problem", lambda: "wrong Harbor binary")
    with pytest.raises(ValueError, match="wrong Harbor binary"):
        probe_task(tmp_path / "unused", None, output, environment="modal", spend_gate=gate)
    assert gate.executions == 0 and not output.exists()


def test_unpriced_gpu_is_refused_without_provider_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab.regrade import CloudSpendGate

    package = _mimo_package(tmp_path / "package")
    with (package / "task.toml").open("a") as handle:
        handle.write("gpus = 1\n")
    patch = tmp_path / "reference.patch"
    patch.write_text("")
    _forbid_execution(monkeypatch)
    gate = CloudSpendGate("test approval", 1, 0.1)
    monkeypatch.setattr(ref_subset, "lab_harbor_problem", lambda: None)
    report = probe_task(
        package, patch, tmp_path / "out", environment="modal", spend_gate=gate, log=lambda _: None
    )
    assert report["status"] == "unavailable" and report["verdict"] == "unknown"
    assert "price estimate" in report["reason"]
    assert gate.executions == 0 and report["execution"]["events"] == []


def test_modal_reservation_bound_counts_preparation_and_both_native_attempts() -> None:
    assert modal_execution_bound() == 54
    assert modal_execution_bound(separate=False) == 51
    assert modal_execution_bound(max_grades=4, mutants=3, separate=False) == 17
    assert modal_execution_bound(max_grades=1, mutants=0, separate=True) == 14


@pytest.mark.parametrize(
    "shape,reason",
    [
        ("steps", "declared task steps"),
        ("agent-windows", "Linux"),
        ("verifier-windows", "Linux"),
        ("agent-compose", "Compose"),
        ("verifier-compose", "Compose"),
    ],
)
def test_modal_unsupported_shapes_refuse_plans_and_execution_before_provider_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    shape: str,
    reason: str,
) -> None:
    from evallab.regrade import CloudSpendGate

    package = _mimo_package(tmp_path / "package")
    config = package / "task.toml"
    if shape == "steps":
        config.write_text('steps = [{name = "one", instruction = "finish"}]\n' + config.read_text())
    elif shape.endswith("windows"):
        with config.open("a") as handle:
            handle.write(
                '\nos = "windows"\n'
                if shape == "agent-windows"
                else '\n[verifier.environment]\nos = "windows"\n'
            )
    else:
        directory = package / ("environment" if shape == "agent-compose" else "tests")
        (directory / "compose.yaml").write_text("services: {}\n")
    patch = tmp_path / "reference.patch"
    patch.write_text(_PATCH)
    _forbid_execution(monkeypatch)
    args = _ref_args(
        "run",
        str(package),
        "--patch",
        str(patch),
        "--output-dir",
        str(tmp_path / "plan-must-not-write"),
        "--environment",
        "modal",
        "--max-usd",
        "100",
        "--approve",
        "inert admission",
        "--plan",
    )
    assert args.func(args, tmp_path) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["execution_refused"] is True
    assert plan["tasks"][0]["action"] == "refused"
    assert reason in plan["tasks"][0]["reason"]
    assert plan["reservation_units_upper_bound"] == 0
    assert not (tmp_path / "plan-must-not-write").exists()
    monkeypatch.setattr(ref_subset, "lab_harbor_problem", lambda: None)
    gate = CloudSpendGate("inert execution", 100, 0.05)
    report = probe_task(
        package,
        patch,
        tmp_path / "refused",
        environment="modal",
        spend_gate=gate,
        log=lambda _: None,
    )
    assert report["status"] == "unavailable" and report["verdict"] == "unknown"
    assert reason in report["reason"]
    assert gate.executions == 0 and report["execution"]["events"] == []


# ---------------------------------------------------------------------------
# Native shared tasks: real script/file behaviour behind a local matrix boundary


def _shared_package(root: Path) -> Path:
    for name in ("environment/Dockerfile", "tests/test.sh", "solution/solve.sh"):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("#\n")
    (root / "task.toml").write_text(
        '[task]\nname = "bench/native"\n'
        '[agent]\ntimeout_sec = 900.0\n'
        '[verifier]\ntimeout_sec = 120.0\n'
        '[environment]\ndocker_image = "fixture/no-download"\n'
        'cpus = 1\nmemory_mb = 128\nstorage_mb = 128\ngpus = 0\n'
    )
    return root


def _host_native(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    one_file: bool = False,
    require_effect: bool = False,
    simple_fix: bool = False,
    fault_at: int | None = None,
    nonfinite: bool = False,
) -> SimpleNamespace:
    """Execute real override/helper/verifier scripts, replacing only Docker admission/transport."""
    from evallab import campaign_execution, docker_lock, runner, verifier_mutation

    repo = tmp_path.resolve()
    package = _shared_package(repo / "package")
    sandbox = repo / "sandbox"
    capture = repo / "container-capture"
    marker = sandbox / "unselected-reference-effect"
    base: dict[str, bytes | None] = {"m.py": b"def f(x):\n    return x\n"}
    fixed: dict[str, bytes | None] = {
        "m.py": (
            b"def f(x):\n    return x + 1\n" if simple_fix
            else b"def f(x):\n    if x > 0:\n        return x + 1\n    return x\n"
        )
    }
    if not one_file:
        base.update({"unused.py": b"unused = 0\n", "gone.go": b"package old\n", "new.sh": None})
        fixed.update({"unused.py": b"unused = 1\n", "gone.go": None, "new.sh": b"#!/bin/sh\nexit 0"})
    modes = {name: 0o755 if name.endswith(".sh") else 0o644 for name in fixed}
    helper = ["#!/usr/bin/env bash\nset -e\n", f"mkdir -p {shlex.quote(str(sandbox))}\n"]
    for name, data in fixed.items():
        path = shlex.quote(str(sandbox / name))
        if data is None:
            helper.append(f"rm -f -- {path}\n")
        else:
            helper.append(f"printf '%s' {shlex.quote(data.decode())} > {path}\nchmod {modes[name]:o} -- {path}\n")
    helper.append(f"printf 'reference-only' > {shlex.quote(str(marker))}\n")
    (package / "solution/helper.sh").write_text("".join(helper))
    (package / "solution/solve.sh").write_text(
        '#!/usr/bin/env bash\nbash "$(dirname "${BASH_SOURCE[0]}")/helper.sh"\nexit $?\n'
    )
    verifier = (
        "from pathlib import Path\n"
        "namespace = {}\n"
        "try:\n"
        f"    exec(Path({str(sandbox / 'm.py')!r}).read_bytes(), namespace)\n"
        "    passed = namespace['f'](5) == 6\n"
        f"    passed = passed and (Path({str(marker)!r}).is_file() if {require_effect!r} else True)\n"
        "except Exception:\n"
        "    passed = False\n"
        "print(float(passed))\n"
    )
    (package / "tests/test.sh").write_text(
        f"#!/usr/bin/env bash\n{shlex.quote(sys.executable)} - <<'PY'\n{verifier}PY\n"
    )
    # Native task images are Linux; this test boundary also works on macOS's BSD stat.
    binaries = repo / "host-test-bin"
    binaries.mkdir()
    (binaries / "stat").write_text(
        "#!/bin/sh\nexec " + shlex.quote(sys.executable)
        + " -c 'import os,sys; print(format(os.stat(sys.argv[-1]).st_mode & 0o7777, \"o\"))' \"$@\"\n"
    )
    (binaries / "stat").chmod(0o755)
    monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(verifier_mutation, "CAPTURE_ROOT", str(capture))
    monkeypatch.setattr(campaign_execution, "docker_available_resources", lambda: (8.0, 16_384))
    active = False
    admissions: list[dict[str, Any]] = []
    invocations: list[dict[str, Any]] = []

    @contextmanager
    def slot(**kwargs: Any) -> Any:
        nonlocal active
        assert not active
        admissions.append(kwargs)
        active = True
        try:
            yield
        finally:
            active = False

    def reset() -> None:
        shutil.rmtree(sandbox, ignore_errors=True)
        shutil.rmtree(capture, ignore_errors=True)
        sandbox.mkdir()
        for name, content in base.items():
            if content is not None:
                path = sandbox / name
                path.write_bytes(content)
                path.chmod(modes[name])

    def evaluate() -> float:
        result = subprocess.run(["bash", str(package / "tests/test.sh")], check=True, capture_output=True, text=True)
        return float(result.stdout)

    def matrix_runner(matrix: Any, *, root: Path) -> Any:
        assert active and matrix.concurrency == 1 and len(matrix.runs) == 1
        reset()
        run = matrix.runs[0]
        with runner.staged_matrix_request(matrix, run, repo_root=root) as (request, provenance):
            result = subprocess.run(
                ["bash", str(request.task / "solution/solve.sh")],
                cwd=sandbox, capture_output=True, text=True,
            )
            reward = evaluate()
        job = root / matrix.jobs_dir / run.name
        trial = job / "trial"
        (trial / "agent").mkdir(parents=True)
        (trial / "agent/oracle.txt").write_text(result.stdout + result.stderr)
        (trial / "agent/exit-code.txt").write_text(str(result.returncode))
        (trial / "result.json").write_text(json.dumps({"reward": reward, "agent_rc": result.returncode}))
        if capture.is_dir():
            shutil.copytree(capture, trial / "artifacts/logs/artifacts/envcheck-capture")
        row = {
            "name": run.name, "status": "ok" if reward == run.expect_reward else "mismatch",
            "rewards": [reward], "error": None, **provenance,
        }
        if result.returncode:
            row.update(status="infra", rewards=[], error=f"oracle script exited {result.returncode}")
        if fault_at == len(invocations):
            if nonfinite:
                row.update(status="ok", rewards=[float("nan")])
            else:
                row.update(status="infra", rewards=[], error="injected transport failure")
        invocations.append({
            "matrix": matrix, "reference_effect": marker.exists(), "row": row,
            "files": {name: (sandbox / name).read_bytes() if (sandbox / name).is_file() else None for name in base},
        })
        receipt = root / matrix.jobs_dir / ".executor" / f"{matrix.matrix_id}.matrix.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps({"matrix": matrix.model_dump(mode="json"), "results": [row]}))
        invocation = receipt.with_suffix(".invocations.jsonl")
        invocation.write_text(json.dumps(row) + "\n")
        return SimpleNamespace(results=[row], receipt_path=receipt, invocation_path=invocation)

    monkeypatch.setattr(docker_lock, "docker_slot", slot)
    monkeypatch.setattr(runner, "run_matrix", matrix_runner)
    return SimpleNamespace(
        package=package, sandbox=sandbox, base=base, fixed=fixed, modes=modes,
        targets=[str(sandbox / name) for name in base], marker=marker,
        reset=reset, evaluate=evaluate, invocations=invocations, admissions=admissions,
    )


def test_empty_file_creation_and_deletion_are_real_change_blocks() -> None:
    for base, fixed in ((None, b""), (b"", None)):
        change = file_change("empty.go", base, fixed, set())
        assert len(change.blocks) == 1 and change.blocks[0].kind == "code"
        assert compose(change, set()) == base
        assert compose(change, {change.blocks[0].id}) == fixed


def test_native_writer_preserves_exact_bytes_modes_absence_and_quoted_paths(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    special = root / "quote ' $(touch injected)"
    gone = root / "gone"
    gone.write_bytes(b"old")
    files = {str(special): bytes(range(256)) * 20 + b"no-final-newline", str(root / "empty"): b"", str(gone): None}
    modes = {path: None if data is None else 0o755 for path, data in files.items()}
    result = subprocess.run(["bash"], input=ref_subset._native_overlay_script(files, modes), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert special.read_bytes() == files[str(special)]
    assert (root / "empty").read_bytes() == b""
    assert stat.S_IMODE(special.stat().st_mode) == 0o755
    assert not gone.exists() and not (root / "injected").exists()


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_native_writer_refuses_non_regular_targets_without_deleting_them(tmp_path: Path, kind: str) -> None:
    root = tmp_path.resolve()
    target = root / "target"
    if kind == "directory":
        target.mkdir()
        (target / "keep").write_bytes(b"keep")
    else:
        (root / "keep").write_bytes(b"keep")
        target.symlink_to(root / "keep")
    result = subprocess.run(
        ["bash"], input=ref_subset._native_overlay_script({str(target): None}, {str(target): None}),
        text=True, capture_output=True,
    )
    assert result.returncode == 97
    assert target.exists() and (root / "keep" if kind == "symlink" else target / "keep").read_bytes() == b"keep"


def test_native_captures_distinguish_absent_empty_deleted_and_incomplete(tmp_path: Path) -> None:
    run = SimpleNamespace(name="capture", job_dir="runs/capture")
    capture = tmp_path / "runs/capture/trial/artifacts/logs/artifacts/envcheck-capture"
    states = capture / ".ref-subset-states"
    states.mkdir(parents=True)
    (states / "complete").write_bytes(b"complete\n")
    for index, present in enumerate((False, True, False)):
        (states / str(index)).write_bytes(b"present\n" if present else b"absent\n")
        if present:
            (states / f"{index}.mode").write_text("755\n")
    (capture / "app").mkdir()
    (capture / "app/empty").write_bytes(b"")
    files, modes = ref_subset._native_captured_files(tmp_path, run, ["/app/new", "/app/empty", "/app/deleted"])
    assert files == {"app/new": None, "app/empty": b"", "app/deleted": None}
    assert modes == {"app/new": None, "app/empty": 0o755, "app/deleted": None}
    (states / "complete").unlink()
    with pytest.raises(RuntimeError, match="did not complete"):
        ref_subset._native_captured_files(tmp_path, run, ["/app/new"])


def test_native_shared_reconstructs_on_base_and_preserves_complete_mutant_witnesses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _host_native(tmp_path, monkeypatch)
    original = {p.relative_to(host.package): p.read_bytes() for p in host.package.rglob("*") if p.is_file()}
    output = tmp_path / "out"
    report = probe_task(
        host.package, None, output, targets=host.targets, repo_root=tmp_path,
        mutants=8, mutation_seed="tb21-weak-tests-v1", log=lambda _: None,
    )
    assert report["verdict"] == "fail" and report["mutants"]["verdict"] == "fail"
    assert [g["label"] for g in report["grades"][:3]] == ["oracle", "nop", "full"]
    assert [g["reward"] for g in report["grades"][:3]] == [1.0, 0.0, 1.0]
    assert host.invocations[0]["reference_effect"]
    assert all(not run["reference_effect"] for run in host.invocations[1:])
    assert all(entry["slots"] == 2 and entry["timeout"] == 900 for entry in host.admissions)
    assert report["limits"]["agent_timeout_multiplier"] == 1.0
    assert report["limits"]["effective_agent_timeout_sec"] == 900
    assert report["limits"]["requested_legacy_limits"]["grade_timeout_s"] == 1800
    assert report["limits"]["mutation_seed"] == "tb21-weak-tests-v1"
    assert "new file mode 100755" in (output / "reference.patch").read_text()
    assert original == {p.relative_to(host.package): p.read_bytes() for p in host.package.rglob("*") if p.is_file()}
    for grade in report["grades"]:
        native = grade["native"]
        assert grade["rc"] == 0 and grade["status"] == "scored"
        script = tmp_path / native["script"]
        assert native["script_sha256"] == "sha256:" + hashlib.sha256(script.read_bytes()).hexdigest()
        assert (tmp_path / native["matrix"]).is_file() and (tmp_path / native["receipt"]).is_file()
        assert native["job_dir"] and len(native["trials"]) == 1
    assert sum(g["label"].startswith("mutant:") for g in report["grades"]) == report["mutants"]["tried"]
    assert len(report["mutants"]["runs"]) == report["mutants"]["tried"]
    assert all(g["label"] in (output / "grades.log").read_text() for g in report["grades"])
    host.reset()
    subprocess.run(["bash", str(output / "minimal.sh")], check=True)
    assert host.evaluate() == 1.0 and not host.marker.exists()
    assert (host.sandbox / "unused.py").read_bytes() == host.base["unused.py"]
    assert (host.sandbox / "gone.go").read_bytes() == host.base["gone.go"]
    assert not (host.sandbox / "new.sh").exists()
    for survivor in report["mutants"]["survivors"]:
        host.reset()
        subprocess.run(["bash", survivor["witness"]], check=True)
        assert host.evaluate() == 1.0 and not host.marker.exists()
        assert (host.sandbox / "unused.py").read_bytes() == host.fixed["unused.py"]
        assert not (host.sandbox / "gone.go").exists()
        assert stat.S_IMODE((host.sandbox / "new.sh").stat().st_mode) == 0o755
        replay = tmp_path / f"patch-{survivor['id']}"
        replay.mkdir()
        for path, data in host.base.items():
            if data is not None:
                target = replay / str(host.sandbox / path).lstrip("/")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        subprocess.run(["git", "apply", survivor["patch"]], cwd=replay, check=True)
        for path in host.base:
            patched = replay / str(host.sandbox / path).lstrip("/")
            actual = host.sandbox / path
            assert (patched.read_bytes() if patched.exists() else None) == (actual.read_bytes() if actual.exists() else None)
        assert stat.S_IMODE((replay / str(host.sandbox / "new.sh").lstrip("/")).stat().st_mode) & 0o100


def test_native_shared_refuses_reference_side_effects_missing_from_full_target_reconstruction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _host_native(tmp_path, monkeypatch, one_file=True, require_effect=True)
    report = probe_task(
        host.package, None, tmp_path / "out", targets=host.targets, repo_root=tmp_path,
        mutants=8, log=lambda _: None,
    )
    assert report["status"] == "not_applicable" and report["verdict"] == "unknown"
    assert "cannot reconstruct" in report["reason"]
    assert [g["reward"] for g in report["grades"]] == [1.0, 0.0, 0.0]
    assert len(host.invocations) == 3 and report["mutants"]["tried"] == 0
    assert not any(run["reference_effect"] for run in host.invocations[1:])


@pytest.mark.parametrize("nonfinite", [False, True])
def test_native_shared_aborts_unknown_on_line_grade_infra_or_nonfinite_reward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nonfinite: bool,
) -> None:
    host = _host_native(tmp_path, monkeypatch, one_file=True, fault_at=3, nonfinite=nonfinite)
    report = probe_task(
        host.package, None, tmp_path / "out", targets=host.targets, repo_root=tmp_path,
        mutants=8, log=lambda _: None,
    )
    assert report["status"] == "unavailable" and report["verdict"] == "unknown"
    assert len(host.invocations) == 4 and len(report["grades"]) == 4
    assert report["grades"][-1]["label"].startswith("mutant:")
    assert report["grades"][-1]["status"] == "error" and report["grades"][-1]["reward"] is None
    assert report["mutants"]["runs"][0]["outcome"] == "unknown"
    assert Path(report["grades"][-1]["native"]["witness"]).is_file()
    assert report["coverage"] == {"subset": "unknown", "mutations": "unknown"}


def test_native_shared_untried_mutants_cannot_produce_a_clean_certificate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _host_native(tmp_path, monkeypatch, one_file=True, simple_fix=True)
    report = probe_task(
        host.package, None, tmp_path / "out", targets=host.targets, repo_root=tmp_path,
        mutants=1, log=lambda _: None,
    )
    summary = report["mutants"]
    assert summary["tried"] == 1 and summary["generated"] > 1
    assert summary["killed"] == 1 and summary["untried"] == summary["generated"] - 1
    assert summary["status"] == "budget_exhausted" and summary["verdict"] == "unknown"
    assert summary["coverage"] == "unknown"


def test_native_shared_subset_budget_exhaustion_remains_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _host_native(tmp_path, monkeypatch)
    report = probe_task(
        host.package, None, tmp_path / "out", targets=host.targets, repo_root=tmp_path,
        max_grades=4, log=lambda _: None,
    )
    assert report["status"] == "budget_exhausted" and report["verdict"] == "unknown"
    assert report["coverage"]["subset"] == "unknown"
    assert len(report["grades"]) <= 5  # Four subset grades, plus the distinct original-oracle capture.


def test_native_shared_resource_refusal_never_lowers_task_ram_or_starts_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evallab import campaign_execution

    host = _host_native(tmp_path, monkeypatch, one_file=True)
    manifest = host.package / "task.toml"
    manifest.write_text(manifest.read_text().replace("memory_mb = 128", "memory_mb = 8192"))
    original = manifest.read_bytes()
    monkeypatch.setattr(campaign_execution, "docker_available_resources", lambda: (8.0, 7933))
    report = probe_task(
        host.package, None, tmp_path / "out", targets=host.targets, repo_root=tmp_path,
        memory="1g", log=lambda _: None,
    )
    assert report["status"] == "unavailable" and report["verdict"] == "unknown"
    assert "resource-ineligible" in report["reason"] and not host.invocations
    assert manifest.read_bytes() == original
    assert report["grades"][0]["native"]["resources"]["required_memory_mb"] == 8192


@pytest.mark.parametrize("targets", [[], ["/app/m.py"], ["/app/../m.py"], ["/logs/reward.txt"]])
def test_native_shared_plan_covers_explicit_files_and_refuses_unsupported_paths_without_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], targets: list[str],
) -> None:
    package = _shared_package(tmp_path / "package")
    with pytest.raises(ValueError):
        Package.load(package)  # Legacy consumers must not mistake shared tasks for raw MiMo.
    _forbid_execution(monkeypatch)
    arguments = ["run", str(package), "--output-dir", str(tmp_path / "out"), "--plan"]
    for target in targets:
        arguments += ["--target", target]
    args = _ref_args(*arguments)
    assert args.func(args, tmp_path) == 0
    plan = json.loads(capsys.readouterr().out)
    item = plan["tasks"][0]
    assert item["package_type"] == "native-shared" and not (tmp_path / "out").exists()
    if targets == ["/app/m.py"]:
        assert not plan["execution_refused"] and item["targets"] == targets
        assert item["required_controls"] == {"oracle": 1, "nop": 0, "full": 1}
        assert item["effective_agent_timeout_sec"] == 900 and item["agent_timeout_multiplier"] == 1.0
    else:
        assert plan["execution_refused"] and item["action"] == "refused"


@pytest.mark.parametrize(
    ("shape", "reason"),
    [
        ("steps", "steps"),
        ("compose", "Compose"),
        ("gpu", "GPU"),
        ("fractional-timeout", "integral"),
        ("external-patch", "own oracle"),
        ("modal", "require Docker"),
    ],
)
def test_native_shared_unsupported_shapes_refuse_zero_execution_plans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    shape: str, reason: str,
) -> None:
    package = _shared_package(tmp_path / "package")
    config = package / "task.toml"
    arguments = ["run", str(package), "--target", "/app/m.py", "--output-dir", str(tmp_path / "out"), "--plan"]
    if shape == "steps":
        config.write_text('steps = [{name = "unsupported"}]\n' + config.read_text())
    elif shape == "compose":
        (package / "environment/compose.yaml").write_text("services: {}\n")
    elif shape == "gpu":
        config.write_text(config.read_text().replace("gpus = 0", "gpus = 1"))
    elif shape == "fractional-timeout":
        config.write_text(config.read_text().replace("900.0", "900.5"))
    elif shape == "external-patch":
        patch = tmp_path / "external.patch"
        patch.write_text(_PATCH)
        arguments += ["--patch", str(patch)]
    else:
        arguments += ["--environment", "modal"]
    original = {p.relative_to(package): p.read_bytes() for p in package.rglob("*") if p.is_file()}
    _forbid_execution(monkeypatch)
    args = _ref_args(*arguments)
    assert args.func(args, tmp_path) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["execution_refused"] and plan["reservation_units_upper_bound"] == 0
    assert reason in plan["tasks"][0]["reason"] and not (tmp_path / "out").exists()
    assert original == {p.relative_to(package): p.read_bytes() for p in package.rglob("*") if p.is_file()}


def test_native_shared_plan_accepts_repeated_targets_and_frozen_mutation_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    package = _shared_package(tmp_path / "package")
    _forbid_execution(monkeypatch)
    args = _ref_args(
        "run", str(package), "--target", "/app/main.py", "--target", "/app/new.sh",
        "--seed", "tb21-weak-tests-v1", "--mutants", "8", "--max-grades", "24",
        "--output-dir", str(tmp_path / "out"), "--plan",
    )
    assert args.func(args, tmp_path) == 0
    plan = json.loads(capsys.readouterr().out)
    assert not plan["execution_refused"]
    assert plan["tasks"][0]["targets"] == ["/app/main.py", "/app/new.sh"]
    assert plan["tasks"][0]["mutation_seed"] == "tb21-weak-tests-v1"
