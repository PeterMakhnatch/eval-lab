"""Reference-subset probe: change blocks, minimisation, severity, patches and the trace check."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import pytest

from evallab.ref_subset import (
    TRACE_SLACK,
    Block,
    FileChange,
    Grade,
    Probe,
    Status,
    block_kind,
    compose,
    edited_ranges,
    file_change,
    patch_code_blocks,
    summarize,
    trace_check,
    unified_patch,
)

_NUMBERED = [f"line{i}\n" for i in range(40)]


def _edited(*indexes: int) -> tuple[bytes, bytes]:
    """A 40-line file and a copy with each of ``indexes`` rewritten (separate blocks)."""
    fixed = [f"fixed{i}\n" if i in indexes else line for i, line in enumerate(_NUMBERED)]
    return "".join(_NUMBERED).encode(), "".join(fixed).encode()


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
