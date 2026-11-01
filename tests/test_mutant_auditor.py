"""Reference-patch semantic mutants: patch geometry, operator validity, selection, and the audit loop."""

from __future__ import annotations

import ast
import base64
import json
from pathlib import Path

import pytest

from evallab.mutant_auditor import (
    APPLIED_MARKER,
    MUTATOR_ORDER,
    PROBE_DONE_MARKER,
    PROBE_MARKER,
    Mutant,
    SandboxMeter,
    added_lines,
    audit_task,
    generate_mutants,
    mutant_applied,
    mutant_script,
    mutants_for_patch,
    parse_probe,
    probe_script,
    python_targets,
)

PATCH = """diff --git a/pkg/mod.py b/pkg/mod.py
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -1,3 +1,5 @@
 import os
+x = 1
+y = 2
 z = 3
-w = 4
@@ -10,2 +11,3 @@
 a = 1
+b = 2
 c = 3
diff --git a/pkg/old.py b/pkg/old.py
deleted file mode 100644
--- a/pkg/old.py
+++ /dev/null
@@ -1,1 +0,0 @@
-gone = True
diff --git a/tests/test_mod.py b/tests/test_mod.py
new file mode 100644
--- /dev/null
+++ b/tests/test_mod.py
@@ -0,0 +1,2 @@
+def test():
+    pass
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,1 +1,2 @@
 # title
+added
"""

SOURCE = """import os


def parse(items, limit=3, strict=True):
    out = []
    for i, item in enumerate(items):
        if i >= limit:
            break
        try:
            value = int(item)
        except ValueError:
            continue
        if value == 0 and strict:
            raise ValueError("zero")
        out.append(value)
    log(out, verbose=True)
    return out


def log(values, verbose=False):
    if verbose:
        print(values)
"""


def test_added_lines_tracks_post_image_numbers_and_skips_deletions() -> None:
    lines = added_lines(PATCH)
    assert lines["pkg/mod.py"] == {2, 3, 12}
    assert lines["tests/test_mod.py"] == {1, 2}
    assert lines["README.md"] == {2}
    assert "pkg/old.py" not in lines


def test_python_targets_excludes_tests_and_non_python() -> None:
    assert set(python_targets(PATCH)) == {"pkg/mod.py"}
    assert python_targets("--- a/x/conftest.py\n+++ b/x/conftest.py\n@@ -1 +1 @@\n+a=1\n") == {}


def test_generate_mutants_are_valid_distinct_and_anchored_on_added_lines() -> None:
    added = set(range(4, 18))
    mutants = generate_mutants("pkg/mod.py", SOURCE, added)
    assert len(mutants) == 3
    reference = ast.dump(ast.parse(SOURCE))
    operators = [m.operator for m in mutants]
    assert len(set(operators)) == 3, "distinct operators preferred"
    assert len({m.line for m in mutants}) == 3, "distinct lines preferred"
    for mutant in mutants:
        assert mutant.line in added
        assert ast.dump(ast.parse(mutant.source)) != reference
        assert mutant.diff.startswith("--- a/pkg/mod.py")
        assert mutant.operator in MUTATOR_ORDER
    # operator priority is deterministic: comparisons first
    assert operators[0] == "boundary_flip"
    assert "if i > limit:" in mutants[0].source


def test_generate_mutants_never_touches_unadded_lines() -> None:
    # only the `log` helper (lines 21-23) is "added": the parse body stays untouched
    mutants = generate_mutants("pkg/mod.py", SOURCE, {21, 22, 23})
    assert mutants
    for mutant in mutants:
        assert mutant.line >= 21
        head = "".join(SOURCE.splitlines(keepends=True)[:18])
        assert mutant.source.startswith(head)


@pytest.mark.parametrize(
    ("source", "added", "operator", "expect"),
    [
        (
            "def f(a):\n    if a is None:\n        return 0\n    return a\n",
            {2},
            "guard_drop",
            "    pass\n    return a\n",
        ),
        (
            "try:\n    g()\nexcept KeyError:\n    h()\n",
            {3},
            "handler_drop",
            "except KeyError:\n    raise\n",
        ),
        ("def f():\n    setup(x=1)\n    return 2\n", {2}, "call_drop", "    pass\n    return 2\n"),
        ("r = call(a, b=1, c=2)\n", {1}, "keyword_drop", "r = call(a, c=2)\n"),
        ("flag = True\n", {1}, "bool_flip", "flag = False\n"),
        ("n = 10\n", {1}, "int_shift", "n = 11\n"),
        ("ok = a != b\n", {1}, "equality_flip", "ok = a == b\n"),
    ],
)
def test_each_operator_produces_the_expected_edit(source, added, operator, expect) -> None:
    mutants = generate_mutants("m.py", source, added, limit=8)
    matching = [m for m in mutants if m.operator == operator]
    assert matching, [m.operator for m in mutants]
    assert expect in matching[0].source


def test_invalid_or_identical_candidates_are_dropped() -> None:
    # `except: raise` is already a re-raise (identical AST); dropping an `elif`
    # guard that precedes an `else` yields a syntax error: neither may surface.
    source = (
        "def f(a):\n"
        "    try:\n"
        "        g()\n"
        "    except KeyError:\n"
        "        raise\n"
        "    if a:\n"
        "        h()\n"
        "    elif a is None:\n"
        "        return 1\n"
        "    else:\n"
        "        k()\n"
    )
    mutants = generate_mutants("m.py", source, set(range(1, 12)), limit=8)
    assert all(m.operator not in {"handler_drop", "guard_drop"} for m in mutants)
    for mutant in mutants:
        ast.parse(mutant.source)


def test_mutants_for_patch_round_robins_across_target_files() -> None:
    patch = (
        "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n+x = 1 if n > 2 else 3\n"
        "--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n+y = m <= 4\n"
    )
    sources = {"a.py": "x = 1 if n > 2 else 3\n", "b.py": "y = m <= 4\n"}
    mutants = mutants_for_patch(patch, sources, limit=3)
    assert [m.path for m in mutants] == ["a.py", "b.py", "a.py"]
    assert mutants_for_patch(patch, {"a.py": sources["a.py"]}, limit=3)[0].path == "a.py"


def test_scripts_apply_reference_then_prove_the_payload() -> None:
    mutant = generate_mutants("pkg/m.py", "n = 1\n", {1})[0]
    script = mutant_script(workdir="/testbed", reference_patch="PATCH", mutant=mutant)
    assert script.startswith("set -u\ncd /testbed || exit 1\n")
    assert "git apply --whitespace=nowarn /tmp/mutant-reference.patch" in script
    assert f"| base64 -d > {mutant.path}" in script
    assert f"sha256sum {mutant.path}" in script and mutant.sha256 in script
    marker = f"{APPLIED_MARKER} int_shift pkg/m.py:1 {mutant.sha256}"
    assert script.rstrip().endswith(f"echo {marker}")
    assert mutant_applied(f"junk\n{marker}\n", mutant)
    assert not mutant_applied("junk\n", mutant)
    probe = probe_script(workdir="/w", reference_patch="PATCH", targets=["a.py", "b/c.py"])
    assert f"echo {PROBE_MARKER} a.py $(base64 < a.py" in probe
    assert probe.rstrip().endswith(f"echo {PROBE_DONE_MARKER}")


def test_parse_probe_requires_done_marker_and_decodes_sources() -> None:
    payload = base64.b64encode(b"x = 1\n").decode()
    log = f"{PROBE_MARKER} a.py {payload}\n"
    assert parse_probe(log) == {}
    assert parse_probe(log + PROBE_DONE_MARKER + "\n") == {"a.py": "x = 1\n"}
    assert parse_probe(f"{PROBE_MARKER} bad !!!\n{PROBE_DONE_MARKER}\n") == {}


def test_sandbox_meter_enforces_cap_on_projected_cost(tmp_path: Path) -> None:
    (tmp_path / "task.toml").write_text("[environment]\ncpus = 2\nmemory_mb = 8192\n")
    meter = SandboxMeter(0.01)
    assert meter.estimate(tmp_path, 100) == pytest.approx((2 * 0.00003942 + 8 * 0.00000667) * 125)
    meter.admit(0.005)
    meter.add(0.009, 60)
    with pytest.raises(RuntimeError, match="cap"):
        meter.admit(0.005)
    assert (meter.cells, meter.seconds) == (1, 60)


def _package(tmp_path: Path, name: str) -> Path:
    package = tmp_path / name
    package.mkdir()
    (package / "task.toml").write_text('[environment]\nworkdir = "/testbed"\n')
    return package


def test_audit_task_counts_survivors_only_with_marker_and_passing_control(tmp_path: Path) -> None:
    source = "def f(a):\n    if a < 0:\n        return 0\n    return a\n"
    patch = "--- a/pkg/m.py\n+++ b/pkg/m.py\n@@ -1,0 +1,4 @@\n" + "".join(
        "+" + line for line in source.splitlines(keepends=True)
    )
    original = _package(tmp_path, "orig")
    repaired = _package(tmp_path, "repaired")
    calls: list[tuple[str, str]] = []

    def fake_run(cell_dir: Path, package: Path, script: str) -> dict:
        calls.append((cell_dir.name, package.name))
        if cell_dir.name == "probe":
            payload = base64.b64encode(source.encode()).decode()
            return {
                "reward": 1.0,
                "status": "executed",
                "agent_log": f"{PROBE_MARKER} pkg/m.py {payload}\n{PROBE_DONE_MARKER}\n",
                "est_usd": 0.001,
                "wall_s": 5,
            }
        marker_line = next(
            ln for ln in script.splitlines() if ln.startswith("echo " + APPLIED_MARKER)
        )
        applied = marker_line[len("echo ") :]
        # original grader: every mutant passes; one of them never landed (no marker)
        if package.name == "orig":
            log = "" if "boundary_flip" in cell_dir.name else applied + "\n"
            return {
                "reward": 1.0,
                "status": "executed",
                "agent_log": log,
                "est_usd": 0.001,
                "wall_s": 5,
            }
        # repaired grader kills everything
        return {
            "reward": 0.0,
            "status": "executed",
            "agent_log": applied + "\n",
            "est_usd": 0.001,
            "wall_s": 5,
        }

    record = audit_task(
        task_id="t1",
        package=original,
        reference_patch=patch,
        out_dir=tmp_path / "out",
        run_cell=fake_run,
        repaired_package=repaired,
    )
    assert record["outcome"] == "audited"
    assert [m["operator"] for m in record["mutants"]] == [
        "boundary_flip",
        "int_shift",
        "guard_drop",
    ]
    assert record["control_original_ok"] is True
    assert record["weak_original"] is True
    assert record["survivors_original"] == ["original-m1-int_shift-L3", "original-m2-guard_drop-L2"]
    assert record["weak_repaired"] is False and record["survivors_repaired"] == []
    unapplied = next(r for r in record["original"] if "boundary_flip" in r["cell"])
    assert unapplied["applied"] is False and unapplied["reward"] == 1.0
    assert calls[0] == ("probe", "orig")
    assert sorted(p for _, p in calls[1:]) == ["orig"] * 3 + ["repaired"] * 3
    written = json.loads((tmp_path / "out" / "audit.json").read_text())
    assert written["survivors_original"] == record["survivors_original"]
    assert (tmp_path / "out" / "guard_drop-L2.diff").read_text().startswith("--- a/pkg/m.py")


def test_audit_task_with_failed_control_is_not_a_weak_grader(tmp_path: Path) -> None:
    source = "n = 1\n"
    patch = "--- a/m.py\n+++ b/m.py\n@@ -0,0 +1 @@\n+n = 1\n"

    def fake_run(cell_dir: Path, package: Path, script: str) -> dict:
        payload = base64.b64encode(source.encode()).decode()
        log = (
            f"{PROBE_MARKER} m.py {payload}\n{PROBE_DONE_MARKER}\n"
            if cell_dir.name == "probe"
            else (script.splitlines()[-1][len("echo ") :] + "\n")
        )
        return {
            "reward": 0.0 if cell_dir.name == "probe" else 1.0,
            "status": "executed",
            "agent_log": log,
        }

    record = audit_task(
        task_id="t2",
        package=_package(tmp_path, "p"),
        reference_patch=patch,
        out_dir=tmp_path / "o",
        run_cell=fake_run,
    )
    assert record["survivors_original"] == ["original-m0-int_shift-L1"]
    assert record["control_original_ok"] is False and record["weak_original"] is False
    assert record["weak_repaired"] is None


def test_audit_task_without_python_target_or_probe_runs_no_mutant_cells(tmp_path: Path) -> None:
    runs: list[str] = []

    def fake_run(cell_dir: Path, package: Path, script: str) -> dict:
        runs.append(cell_dir.name)
        return {"reward": 1.0, "status": "executed", "agent_log": "no markers"}

    package = _package(tmp_path, "p")
    record = audit_task(
        task_id="t3",
        package=package,
        reference_patch="--- a/x.ts\n+++ b/x.ts\n@@ -0,0 +1 @@\n+a\n",
        out_dir=tmp_path / "a",
        run_cell=fake_run,
    )
    assert record["outcome"] == "no python target in reference patch" and runs == []
    record = audit_task(
        task_id="t4",
        package=package,
        reference_patch="--- a/x.py\n+++ b/x.py\n@@ -0,0 +1 @@\n+a = 1\n",
        out_dir=tmp_path / "b",
        run_cell=fake_run,
    )
    assert record["outcome"] == "probe failed" and runs == ["probe"]
    assert isinstance(Mutant, type)
