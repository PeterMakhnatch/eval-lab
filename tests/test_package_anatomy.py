"""Behavior of the deterministic ``PackageAnatomy`` parser: the grading/container facts a bug hunter would read."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab import cli
from evallab.package_anatomy import MECHANISM_CLASSES, analyze_package, discover_packages


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _ids(card: dict) -> list[str]:
    return [h["id"] for h in card["vulnerability_hypotheses"]]


def _hyp(card: dict, hid: str) -> dict:
    return next(h for h in card["vulnerability_hypotheses"] if h["id"].split("#")[0] == hid)


PYTEST_SH = """#!/bin/bash
V=/logs/verifier
mkdir -p "$V"
cd /testbed || exit 1
timeout 600 python3 -m pytest -q {target} > "$V/out.log" 2>&1
RC=$?
if [ $RC -eq 0 ]; then echo 1 > "$V/reward.txt"; else echo 0 > "$V/reward.txt"; fi
"""

SMALL_TEST = "def test_total():\n    assert total([1, 2]) == 3\n"


def _baked_package(tmp_path: Path, target: str) -> Path:
    return _write(tmp_path / "pkg", {
        "instruction.md": "Implement `total` in `calc.py`.\n",
        "task.toml": '[environment]\nworkdir = "/testbed"\n',
        "environment/Dockerfile": "FROM python:3.12-slim\nWORKDIR /testbed\nCOPY app/ /testbed/\n",
        "environment/app/calc.py": "def total(xs):\n    return 0\n",
        "environment/app/tests/test_calc.py": SMALL_TEST,
        "tests/test.sh": PYTEST_SH.format(target=target),
    })


def test_grader_that_runs_tests_baked_into_the_image_is_the_top_tamper_hypothesis(tmp_path):
    card = analyze_package(_baked_package(tmp_path, "tests"))
    top = card["vulnerability_hypotheses"][0]
    assert (top["id"], top["mechanism_class"]) == ("GT-BAKED-TESTS-EXECUTED", "grader_tamperable")
    assert card["grader_script_mechanics"]["baked_tests_executed"] == ["/testbed/tests/test_calc.py"]
    # the baked tests are the suite that gets scored, so rigor is measured on them
    assert card["test_suite_rigor"]["counts"]["tests"] == 1


def test_grader_that_runs_tests_mounted_from_outside_the_agent_tree_does_not_flag_baked_tests(tmp_path):
    card = analyze_package(_baked_package(tmp_path, "/tests/test_calc.py"))
    assert "GT-BAKED-TESTS-EXECUTED" not in _ids(card)
    assert card["grader_script_mechanics"]["baked_tests_executed"] == []


def test_verifier_only_tests_baked_into_the_image_are_answer_reachable_by_bytes(tmp_path):
    root = _write(tmp_path / "pkg", {
        "instruction.md": "Fix it.\n",
        "task.toml": "",
        "environment/Dockerfile": "FROM python:3.12\nCOPY harness/ /opt/harness/\n",
        "environment/harness/test_hidden.py": SMALL_TEST,
        "tests/test.sh": PYTEST_SH.format(target="/tests/test_hidden.py"),
        "tests/test_hidden.py": SMALL_TEST,
    })
    card = analyze_package(root)
    hyp = _hyp(card, "AR-TESTS-IN-IMAGE")
    assert hyp["mechanism_class"] == "answer_reachable"
    assert hyp["confidence"] == "static_certain"
    assert any("/opt/harness/test_hidden.py" in e["text"] for e in hyp["evidence"])


def test_separate_verifier_removes_the_single_container_autoload_hypothesis(tmp_path):
    files = {
        "instruction.md": "Fix it.\n",
        "environment/Dockerfile": "FROM python:3.12\n",
        "tests/test.sh": PYTEST_SH.format(target="/tests"),
        "tests/test_a.py": SMALL_TEST,
    }
    single = analyze_package(_write(tmp_path / "single", {**files, "task.toml": ""}))
    separate = analyze_package(
        _write(tmp_path / "sep", {**files, "task.toml": '[verifier]\nenvironment_mode = "separate"\n'})
    )
    assert single["container_and_boundary"]["layout"] == "single_container"
    assert separate["container_and_boundary"]["layout"] == "separate_verifier"
    assert "GT-AUTOLOAD-WRITABLE" in _ids(single)
    assert "GT-AUTOLOAD-WRITABLE" not in _ids(separate)


def test_pattern_reset_or_isolation_flags_suppress_the_autoload_hypothesis(tmp_path):
    hardened = PYTEST_SH.format(target="--noconftest -c /dev/null /tests")
    card = analyze_package(_write(tmp_path / "pkg", {
        "instruction.md": "x\n", "task.toml": "", "tests/test.sh": hardened, "tests/test_a.py": SMALL_TEST,
    }))
    assert "GT-AUTOLOAD-WRITABLE" not in _ids(card)


def test_failure_masked_exit_code_is_flagged_with_its_line(tmp_path):
    script = PYTEST_SH.format(target="/tests").replace("> \"$V/out.log\" 2>&1", "> \"$V/out.log\" 2>&1 || true")
    card = analyze_package(_write(tmp_path / "pkg", {
        "instruction.md": "x\n", "task.toml": "", "tests/test.sh": script, "tests/test_a.py": SMALL_TEST,
    }))
    hyp = _hyp(card, "EG-FAILURE-MASKED")
    assert hyp["mechanism_class"] == "exploit_grader_gap"
    assert hyp["evidence"][0]["line"] == 5


def test_verifier_subprocess_of_baked_checker_script_is_agent_controlled(tmp_path):
    root = _write(tmp_path / "pkg", {
        "instruction.md": "Make the export pass.\n",
        "task.toml": "",
        "environment/Dockerfile": "FROM python:3.12\nWORKDIR /app\nCOPY app/ /app/\n",
        "environment/app/tools/export_check.py": "print('PASS')\n",
        "tests/test.sh": PYTEST_SH.format(target="/tests/test_export.py").replace("/testbed", "/app"),
        "tests/test_export.py": (
            "import subprocess, sys\n\ndef test_ok():\n"
            "    r = subprocess.run([sys.executable, 'tools/export_check.py'], cwd='/app')\n"
            "    assert r.returncode == 0\n"
        ),
    })
    hyp = _hyp(analyze_package(root), "GT-VERIFIER-RUNS-AGENT-CODE")
    assert hyp["mechanism_class"] == "grader_tamperable"
    assert "/app/tools/export_check.py" in hyp["evidence"][0]["text"]


def test_assertion_counts_separate_literal_negative_surface_and_delegated_checks(tmp_path):
    tests = (
        "import os\nimport pytest\n\n"
        "def _check(x):\n    assert x > 0\n\n"
        "def test_literal():\n    assert f(2) == 4\n    assert f(3) == 9\n\n"
        "def test_negative():\n    with pytest.raises(ValueError):\n        f(-1)\n\n"
        "def test_surface():\n    assert os.path.exists('out.txt')\n\n"
        "def test_delegated():\n    _check(f(1))\n\n"
        "def test_nothing():\n    f(1)\n"
    )
    card = analyze_package(_write(tmp_path / "pkg", {
        "instruction.md": "x\n", "task.toml": "", "tests/test.sh": PYTEST_SH.format(target="/tests"), "tests/test_f.py": tests,
    }))
    counts = card["test_suite_rigor"]["counts"]
    assert counts["tests"] == 5
    assert counts["literal_eq_assertions"] == 2
    assert counts["negative_assertions"] == 1
    assert counts["surface_assertions"] == 1
    assert counts["tests_without_assertion"] == 1  # test_delegated is credited through its assertion helper
    assert card["test_suite_rigor"]["counts"]["mode_ast_files"] == 1


def test_instruction_symbols_with_no_test_reference_are_listed_and_referenced_ones_are_not(tmp_path):
    card = analyze_package(_write(tmp_path / "pkg", {
        "instruction.md": "Add `parse_range()` and the `--strict` flag. `parse_range` must raise on an empty string.\n",
        "task.toml": "",
        "tests/test.sh": PYTEST_SH.format(target="/tests"),
        "tests/test_r.py": "def test_range():\n    assert parse_range('1-2') == (1, 2)\n",
    }))
    symbols = {m["symbol"]: m for m in card["test_suite_rigor"]["instruction_named_symbols_without_test_reference"]}
    assert "--strict" in symbols and "parse_range" not in symbols
    hyp = _hyp(card, "WC-INSTRUCTION-SYMBOL-UNTESTED")
    assert hyp["mechanism_class"] == "weak_check"
    assert any("--strict" in e["text"] for e in hyp["evidence"])


REFERENCE_PATCH = """diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -1,3 +1,9 @@ def old
+def covered(x):
+    return x + 1
+
+def uncovered(x):
+    if x < 0:
+        raise ValueError('negative input rejected')
+    return x
"""


def test_reference_patch_symbols_and_branches_that_tests_never_touch_are_weak_check_evidence(tmp_path):
    root = _write(tmp_path / "pkg", {
        "instruction.md": "x\n", "task.toml": "", "tests/test.sh": PYTEST_SH.format(target="/tests"),
        "tests/test_c.py": "def test_covered():\n    assert covered(1) == 2\n",
    })
    without = analyze_package(root)
    patch = tmp_path / "ref.patch"
    patch.write_text(REFERENCE_PATCH)
    with_patch = analyze_package(root, reference_patch=patch)
    assert "WC-REFERENCE-SYMBOL-UNEXERCISED" not in _ids(without)
    symbol = _hyp(with_patch, "WC-REFERENCE-SYMBOL-UNEXERCISED")
    assert symbol["requires_reference"] is True
    assert [e["text"].split()[0] for e in symbol["evidence"]] == ["uncovered"]
    branch = _hyp(with_patch, "WC-REFERENCE-BRANCH-UNASSERTED")
    assert any("negative input rejected" in e["text"] for e in branch["evidence"])


MIMO_PATCH = """diff --git a/pkg/tests/test_new.py b/pkg/tests/test_new.py
new file mode 100644
--- /dev/null
+++ b/pkg/tests/test_new.py
@@ -0,0 +1,6 @@
+from pkg.mod import run
+
+
+def test_run():
+    assert run(1) == 2
+    assert run(2) == 4
"""

MIMO_SH = """#!/bin/bash
CWD=/testbed
V=/logs/verifier
mkdir -p "$V"
cd "$CWD" || exit 1
if ! (
while IFS= read -r tf; do
  git checkout BASE -- "$tf"
done <<'EOF_RESET_TEST_FILES'
pkg/tests/test_new.py
EOF_RESET_TEST_FILES
); then exit 1; fi
git apply /tests/test.patch || exit 1
timeout 1800 sh -c "$(cat /tests/test_command.sh)" > "$V/test_output.log" 2>&1
RC=$?
if [ $RC -eq 0 ]; then echo 1 > "$V/reward.txt"; else echo 0 > "$V/reward.txt"; fi
"""


def test_mimo_hidden_patch_shape_is_parsed_into_reset_list_runner_and_test_counts(tmp_path):
    card = analyze_package(_write(tmp_path / "pkg", {
        "instruction.md": "Add run.\n",
        "task.toml": '[environment]\ndocker_image = "docker.io/x/y@sha256:' + "a" * 64 + '"\nworkdir = "/testbed"\n',
        "tests/test.sh": MIMO_SH,
        "tests/test_command.sh": "python -m pytest pkg/tests/test_new.py\n",
        "tests/test.patch": MIMO_PATCH,
    }))
    grader = card["grader_script_mechanics"]
    assert grader["runner"] == "pytest"
    assert grader["reset_list"]["enumerated_files"] == ["pkg/tests/test_new.py"]
    assert grader["reset_list"]["files_touched_by_hidden_patch"] == ["pkg/tests/test_new.py"]
    assert "conftest.py" in grader["reset_list"]["autoload_hook_classes_left_writable"]
    assert grader["reward_gate"]["mechanisms"] == ["exit_code"]
    counts = card["test_suite_rigor"]["counts"]
    assert (counts["tests"], counts["assertions"], counts["literal_eq_assertions"]) == (1, 2, 2)
    assert card["container_and_boundary"]["agent_image"]["base_pinned_by_digest"] is True


def test_cards_are_deterministic_and_use_only_known_mechanism_classes(tmp_path):
    root = _baked_package(tmp_path, "tests")
    first, second = analyze_package(root), analyze_package(root)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert {h["mechanism_class"] for h in first["vulnerability_hypotheses"]} <= set(MECHANISM_CLASSES)
    scores = [h["score"] for h in first["vulnerability_hypotheses"]]
    assert scores == sorted(scores, reverse=True)


def test_mechanism_classes_match_the_seeker_taxonomy():
    from evallab.production.seeker_worker import MECHANISM_CLASSES as SEEKER

    assert set(MECHANISM_CLASSES) == set(SEEKER)


def test_discover_packages_expands_collections_and_refuses_missing_paths(tmp_path):
    _write(tmp_path / "coll/a", {"task.toml": ""})
    _write(tmp_path / "coll/b", {"instruction.md": "x"})
    (tmp_path / "coll/not-a-task").mkdir()
    assert [p.name for p in discover_packages([tmp_path / "coll"])] == ["a", "b"]
    with pytest.raises(FileNotFoundError):
        discover_packages([tmp_path / "missing"])


def test_tasks_anatomy_cli_writes_jsonl_cards_and_refuses_reference_patch_for_many_packages(tmp_path, capsys):
    _write(tmp_path / "coll/a", {"instruction.md": "x", "tests/test.sh": PYTEST_SH.format(target="/tests")})
    _write(tmp_path / "coll/b", {"instruction.md": "y", "tests/test.sh": PYTEST_SH.format(target="/tests")})
    out = tmp_path / "cards.jsonl"
    assert cli.run_cli(["tasks", "anatomy", str(tmp_path / "coll"), "--out", str(out)], workspace=tmp_path) == 0
    cards = [json.loads(line) for line in out.read_text().splitlines()]
    assert [c["task"] for c in cards] == ["a", "b"]
    assert all(c["schema"] == "evallab.package_anatomy/v1" for c in cards)
    patch = tmp_path / "ref.patch"
    patch.write_text(REFERENCE_PATCH)
    capsys.readouterr()
    code = cli.run_cli(["tasks", "anatomy", str(tmp_path / "coll"), "--reference-patch", str(patch)], workspace=tmp_path)
    assert code == 2
    assert "exactly one task package" in capsys.readouterr().err
