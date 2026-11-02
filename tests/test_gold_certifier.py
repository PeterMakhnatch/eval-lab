"""Tests for gold_certifier: contract-complete behavioral synthesis and certification matrix."""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import pytest

from evallab.gold_certifier import (
    CONFTEST_TAMPER_PAYLOAD,
    GOLD_CONTRACT_TEST_NAME,
    GoldMatrixSummary,
    TaskCertificationResult,
    _certify_gold_command,
    build_certify_gold_parser,
    build_synthesis_prompt,
    exit0_pth_script,
    extract_code_block,
    format_supplementary_hook,
    format_wilson,
    mutant_diff,
    nop_script,
    reference_script,
    split_mutants,
    stage_and_harden_package,
    validate_contract_test,
    wilson_interval,
    witness_script,
)
from evallab.mutant_auditor import Mutant
from evallab.separate_verifier import supplementary_test_blocks


def test_wilson_interval_and_formatting() -> None:
    lo_nan, hi_nan = wilson_interval(0, 0)
    assert math.isnan(lo_nan) and math.isnan(hi_nan)
    assert format_wilson(0, 0) == "0/0"

    lo, hi = wilson_interval(8, 8)
    assert lo > 0.60 and hi == 1.0
    formatted = format_wilson(8, 8)
    assert formatted.startswith("8/8 = 100.0%")

    lo0, hi0 = wilson_interval(0, 8)
    assert lo0 == 0.0 and hi0 < 0.40
    assert format_wilson(0, 8).startswith("0/8 = 0.0%")


def test_split_mutants_deterministic_sha256_order() -> None:
    # Construct mutants with distinct sha256 hashes
    m1 = Mutant(path="a.py", operator="bool_flip", line=10, description="1", source="x=1", diff="d1", sha256="cccc")
    m2 = Mutant(path="a.py", operator="int_shift", line=20, description="2", source="x=2", diff="d2", sha256="aaaa")
    m3 = Mutant(path="a.py", operator="guard_drop", line=30, description="3", source="x=3", diff="d3", sha256="bbbb")
    m4 = Mutant(path="a.py", operator="call_drop", line=40, description="4", source="x=4", diff="d4", sha256="dddd")

    # In sha256 order: m2 (aaaa), m3 (bbbb), m1 (cccc), m4 (dddd)
    train, eval_m = split_mutants([m1, m2, m3, m4])
    assert [m.sha256 for m in train] == ["aaaa", "bbbb"]
    assert [m.sha256 for m in eval_m] == ["cccc", "dddd"]

    # Odd count (3 items): first 2 train, 1 eval
    train3, eval3 = split_mutants([m1, m2, m3])
    assert [m.sha256 for m in train3] == ["aaaa", "bbbb"]
    assert [m.sha256 for m in eval3] == ["cccc"]

    # Single item
    train1, eval1 = split_mutants([m1])
    assert train1 == [m1] and eval1 == []

    # Empty
    assert split_mutants([]) == ([], [])


def test_mutant_diff() -> None:
    orig = "def f(x):\n    return x > 0\n"
    mutated = "def f(x):\n    return x >= 0\n"
    mutant = Mutant(path="pkg/mod.py", operator="boundary_flip", line=2, description="flip", source=mutated, diff="d", sha256="1234")
    diff = mutant_diff(mutant, orig)
    assert "--- a/pkg/mod.py" in diff
    assert "+++ b/pkg/mod.py" in diff
    assert "-    return x > 0" in diff
    assert "+    return x >= 0" in diff


def test_validate_contract_test() -> None:
    valid_test = (
        "import unittest\n"
        "class TestContract(unittest.TestCase):\n"
        "    def test_basic(self):\n"
        "        self.assertTrue(True)\n"
    )
    ok, reason = validate_contract_test(valid_test)
    assert ok and reason == "valid"

    # Missing unittest
    no_unittest = "def test_foo(): assert True\n"
    ok, reason = validate_contract_test(no_unittest)
    assert not ok and "missing unittest" in reason

    # Forbidden source inspection
    for pattern in ("inspect.getsource", "ast.parse", "import ast", "from ast import", "import tokenize", "from tokenize import", "linecache", "__code__"):
        bad_code = valid_test + f"\n# check: {pattern}\n"
        ok, reason = validate_contract_test(bad_code)
        assert not ok and pattern in reason


def test_extract_code_block() -> None:
    text = "Here is the test:\n```python\nimport unittest\nclass T(unittest.TestCase): pass\n```\nDone."
    code = extract_code_block(text)
    assert code is not None
    assert "class T(unittest.TestCase): pass" in code

    # No block
    assert extract_code_block("just plain text") is None


def test_build_synthesis_prompt_blind_to_eval_mutants() -> None:
    m_train = Mutant(path="m.py", operator="boundary_flip", line=10, description="train", source="y=1", diff="d", sha256="1111")
    train_diff = "--- a/m.py\n+++ b/m.py\n@@ -10 +10 @@\n-x < 0\n+x <= 0\n"
    eval_diff = "--- a/m.py\n+++ b/m.py\n@@ -20 +20 @@\n-foo(k=1)\n+foo()\n"

    prompt = build_synthesis_prompt(
        instruction="Fix the bug in widget parser.",
        hidden_tests="def test_existing(): pass",
        reference_patch="--- a/m.py\n+++ b/m.py\n+def foo(): return 42\n",
        train_mutants=[m_train],
        train_diffs=[train_diff],
        interpreter_command='cd "$CWD" && python3',
        python_version="3",
        feedback="Previous test failed: AssertionError: False is not true",
    )

    # Train mutant diff must be present
    assert "x <= 0" in prompt
    assert "boundary_flip" not in prompt  # Only raw diff shown in train_diffs
    # Eval mutant must NEVER appear
    assert "foo(k=1)" not in prompt
    assert eval_diff not in prompt
    assert "9999" not in prompt

    # Required instructions
    assert "contract-complete" in prompt
    assert "branch, guard, boundary condition" in prompt
    assert "PUBLIC API" in prompt
    assert "inspect.getsource" in prompt  # in strict rules list
    assert "AssertionError: False is not true" in prompt  # feedback included


def test_format_supplementary_hook_matches_separate_verifier() -> None:
    hook = format_supplementary_hook('cd "$CWD" && cd /testbed && PYTHONPATH=/testbed python3')
    assert hook.startswith('if [ "$RC" -eq 0 ]; then\n')
    assert f"/tests/{GOLD_CONTRACT_TEST_NAME}" in hook
    assert hook.endswith("RC=$?\nfi\n")

    # Verify that supplementary_test_blocks recognizes it
    pkg = Path("/tmp/test_hook_pkg")
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "task.toml").write_text('[environment]\nworkdir = "/testbed"\n')
    tests_dir = pkg / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test.sh").write_text(f"run_tests\nRC=$?\n{hook}\ntail -c 12000 log\n")

    blocks = supplementary_test_blocks(pkg)
    assert len(blocks) == 1
    assert f"/tests/{GOLD_CONTRACT_TEST_NAME}" in blocks[0]


def test_arm_scripts_contain_expected_markers() -> None:
    ref = reference_script("/testbed", "diff --git a/x b/x\n")
    assert "cd /testbed || exit 1" in ref
    assert "REFERENCE_APPLIED" in ref

    nop = nop_script("/testbed")
    assert "cd /testbed || exit 1" in nop
    assert "NOP_APPLIED" in nop

    wit_solve = witness_script("/testbed", "bash /solve.sh")
    assert "WITNESS_APPLIED solve_sh" in wit_solve

    wit_conftest = witness_script("/testbed", None)
    assert "WITNESS_APPLIED conftest" in wit_conftest
    assert "pytest_sessionfinish" in CONFTEST_TAMPER_PAYLOAD

    exit0 = exit0_pth_script("/testbed")
    assert "EXIT0_PTH_APPLIED" in exit0
    assert "zz_cert.pth" in exit0


def test_stage_and_harden_package(tmp_path: Path) -> None:
    pkg = tmp_path / "base_pkg"
    pkg.mkdir()
    (pkg / "task.toml").write_text('[environment]\nworkdir = "/testbed"\n')
    tests_dir = pkg / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_command.sh").write_text("python -m unittest tests/test_base.py\n")
    (tests_dir / "test.patch").write_text("diff --git a/test.py b/test.py\n")
    test_sh = (
        '#!/bin/bash\n'
        'M=/var/lib/mimo\n'
        'CWD=/workspace/repo\n'
        'V=/logs/verifier\n'
        'mkdir -p "$V"\n'
        'BASE=$(cat "$M/base" 2>/dev/null) || { echo "setup never ran" >&2; exit 1; }\n'
        'cd "$CWD" || exit 1\n'
        'if ! (\n'
        'while IFS= read -r tf; do\n'
        '  git checkout $BASE -- "$tf" 2>/dev/null || true\n'
        'done <<\'EOF_RESET_TEST_FILES\'\n'
        'tests/test_x.py\n'
        'EOF_RESET_TEST_FILES\n'
        '\n'
        '); then echo "could not reset the test files (testbed problem, not scored)" >&2; exit 1; fi\n'
        'if ! git apply --verbose /tests/test.patch > "$V/apply.log" 2>&1; then exit 1; fi\n'
        'timeout 1800 sh -c "$(cat /tests/test_command.sh)" > "$V/test_output.log" 2>&1\n'
        'RC=$?\n'
        'tail -c 12000 "$V/test_output.log"\n'
        'if [ $RC -eq 0 ]; then echo 1 > "$V/reward.txt"; else echo 0 > "$V/reward.txt"; fi\n'
        'echo "test command exited $RC"\n'
    )
    (tests_dir / "test.sh").write_text(test_sh)

    contract_code = "import unittest\nclass T(unittest.TestCase): pass\n"
    out_dir = tmp_path / "hardened_pkg"

    manifest, hook_block = stage_and_harden_package(
        pkg,
        out_dir,
        contract_code,
        hook_command='cd "$CWD" && python3',
        single_container=True,  # test single container locally without docker
        allow_degraded=True,
    )

    assert out_dir.exists()
    assert (out_dir / "tests" / GOLD_CONTRACT_TEST_NAME).read_text() == contract_code
    test_sh_content = (out_dir / "tests/test.sh").read_text()
    assert GOLD_CONTRACT_TEST_NAME in test_sh_content
    assert manifest.runner is not None


def test_gold_matrix_summary_and_reporting() -> None:
    t1 = TaskCertificationResult(
        task_id="format-code-task-000079",
        kind="repaired",
        upgraded_package="/path/79",
        test_lines=45,
        train_mutants_total=4,
        train_mutants_killed=4,
        eval_mutants_total=4,
        eval_mutants_killed=3,
        eval_mutants_base_accepted=2,
        eval_mutants_base_accepted_killed=2,
        gate1_reference_passed=True,
        gate2_nop_passed=True,
        gate3_witness_passed=True,
        gate4_exit0_pth_passed=True,
        reference_reward=1.0,
        nop_reward=0.0,
        witness_reward=0.0,
        exit0_pth_reward=0.0,
    )
    t2 = TaskCertificationResult(
        task_id="format-code-task-000682",
        kind="original",
        upgraded_package="/path/682",
        test_lines=30,
        train_mutants_total=1,
        train_mutants_killed=1,
        eval_mutants_total=1,
        eval_mutants_killed=1,
        eval_mutants_base_accepted=1,
        eval_mutants_base_accepted_killed=1,
        gate1_reference_passed=True,
        gate2_nop_passed=True,
        gate3_witness_passed=True,
        gate4_exit0_pth_passed=True,
        reference_reward=1.0,
        nop_reward=0.0,
        witness_reward=0.0,
        exit0_pth_reward=0.0,
    )

    summary = GoldMatrixSummary(tasks=[t1, t2])
    rep = summary.to_report_dict()

    assert rep["total_tasks"] == 2
    assert rep["gate1_reference"]["passed"] == 2
    assert rep["gate2_nop"]["passed"] == 2
    assert rep["gate3_witness"]["passed"] == 2
    assert rep["gate4_exit0_pth"]["passed"] == 2
    assert rep["gate5_train_mutants"]["killed"] == 5
    assert rep["gate5_train_mutants"]["total"] == 5
    assert rep["gate6_eval_mutants_base_accepted"]["killed"] == 3
    assert rep["gate6_eval_mutants_base_accepted"]["total"] == 3


def test_cli_plan_offline(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "task.toml").write_text('[environment]\nworkdir = "/testbed"\n')
    patch = tmp_path / "ref.patch"
    patch.write_text("--- a/foo.py\n+++ b/foo.py\n@@ -0,0 +1 @@\n+x = 1\n")

    top = argparse.ArgumentParser()
    build_certify_gold_parser(top.add_subparsers())
    args = top.parse_args(
        [
            "certify-gold",
            "--package",
            str(pkg),
            "--reference-patch",
            str(patch),
            "--task-id",
            "format-code-task-000079",
            "--plan",
        ]
    )
    assert args.rounds == 1  # default keeps the one-round behavior

    rc = _certify_gold_command(args, tmp_path)
    assert rc == 0
    captured = capsys.readouterr()
    assert "Plan for format-code-task-000079" in captured.out
    assert "foo.py" in captured.out
    assert "$0.95" in captured.out
