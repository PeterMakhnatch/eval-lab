"""Ported Traces probe-03 rules behave on real-shaped Terminus trials."""

from __future__ import annotations

import json
from pathlib import Path

from evallab import probe03

MESSAGE = json.dumps(
    {"analysis": "", "plan": "", "commands": [{"keystrokes": "echo stuck"}]}
)


def _step(step_id: int, message: str = MESSAGE) -> dict:
    return {
        "step_id": step_id,
        "source": "agent",
        "message": message,
        "observation": "output\n",
        "metrics": {"prompt_tokens": 1000 + step_id, "completion_tokens": 10},
    }


def _write_trial(
    root: Path, name: str, *, n_loop: int, reward: float, ceiling: bool
) -> Path:
    trial = root / name
    agent = trial / "agent"
    agent.mkdir(parents=True)
    steps = [_step(i) for i in range(1, n_loop + 1)]
    head: dict = {"steps": steps}
    result: dict = {
        "task_name": "task",
        "trial_name": name,
        "verifier_result": {"rewards": {"reward": reward}},
    }
    if ceiling:
        prompt = sum(step["metrics"]["prompt_tokens"] for step in steps)
        result["exception_info"] = {"exception_type": "TrialBudgetExhaustedError"}
        result["agent_result"] = {"n_input_tokens": prompt, "n_output_tokens": 120}
    (agent / "trajectory.json").write_text(json.dumps(head), encoding="utf-8")
    (trial / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return trial


def test_ceiling_loop_trial_stop_runs_and_reward(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    (job / "lab-metadata.json").write_text(
        json.dumps(
            {"trial_budget": {"max_input_tokens": 12078, "max_output_tokens": 200000}}
        ),
        encoding="utf-8",
    )
    trial = _write_trial(job, "trial-loop", n_loop=12, reward=0.0, ceiling=True)

    reward, scored, _ = probe03.read_reward(trial)
    assert (reward, scored) == (0.0, True)
    analysis = probe03.analyze_trial_core(trial, job)
    assert analysis["stop_reason"] == "ceiling:input_tokens"
    assert [(r["start"], r["end"], r["length"]) for r in analysis["runs"]] == [
        (1, 12, 12)
    ]
    # A mechanically clean identical loop with no claim is not a failure.
    assert analysis["first_failure"] is None


def test_native_loop_break_preserves_scored_verifier_outcome(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    for reward in (0.0, 1.0):
        trial = _write_trial(
            job, f"loop-break-{reward}", n_loop=12, reward=reward, ceiling=False
        )
        result_path = trial / "result.json"
        result = json.loads(result_path.read_text())
        result["exception_info"] = {
            "exception_type": "LoopBreakStop",
            "exception_message": "loop break after the nudge grace interval",
        }
        result_path.write_text(json.dumps(result))

        analysis = probe03.analyze_trial_core(trial, job)
        assert analysis["stop_reason"] == "loop_break"
        assert (analysis["reward"], analysis["scored"]) == (reward, True)


def test_distinct_steps_are_no_run(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    (job / "lab-metadata.json").write_text("{}", encoding="utf-8")
    trial = tmp_path / "job" / "trial-ok"
    trial.mkdir(parents=True)
    agent = trial / "agent"
    agent.mkdir()
    steps = [
        _step(i, json.dumps({"commands": [{"keystrokes": f"echo {i}"}]}))
        for i in range(1, 4)
    ]
    (agent / "trajectory.json").write_text(json.dumps({"steps": steps}), encoding="utf-8")
    (trial / "result.json").write_text(
        json.dumps(
            {
                "task_name": "task",
                "trial_name": "trial-ok",
                "verifier_result": {"rewards": {"reward": 1.0}},
            }
        ),
        encoding="utf-8",
    )
    analysis = probe03.analyze_trial_core(trial, job)
    assert analysis["runs"] == []
    assert analysis["stop_reason"] == "unknown"


def _write_verifier_trial(root: Path, name: str, tests: list[dict]) -> Path:
    trial = root / name
    (trial / "verifier").mkdir(parents=True)
    (trial / "verifier" / "ctrf.json").write_text(
        json.dumps({"results": {"tests": tests}}), encoding="utf-8"
    )
    return trial


def test_verifier_passage_failing_test_limit(tmp_path: Path) -> None:
    tests = [{"name": f"test_{i:02d}", "status": "failed"} for i in range(10)]
    tests.insert(3, {"name": "test_ok", "status": "passed"})
    trial = _write_verifier_trial(tmp_path, "trial-limits", tests)
    all_names = probe03._verifier_passage(trial, failing_test_limit=None)
    assert all_names["failing_tests"] == [f"test_{i:02d}" for i in range(10)]
    assert (all_names["passes"], all_names["total"], all_names["fails"]) == (1, 11, 10)
    capped = probe03._verifier_passage(trial, failing_test_limit=3)
    assert capped["failing_tests"] == ["test_00", "test_01", "test_02"]


def _source_test(name: str, trace: str) -> dict:
    return {"name": name, "status": "failed", "trace": trace}


def test_verifier_asserted_literals_source_membership() -> None:
    trace = 'src = open(path).read()\n>   assert "needle" in src\nE   AssertionError'
    assert probe03.verifier_asserted_literals(
        _source_test("test_x", trace), "do the thing with needle inside"
    ) == [
        {
            "literal": "needle",
            "in_instruction": True,
            "kind": "source_membership",
            "evidence": 'assert "needle" in src',
        }
    ]
    assert (
        probe03.verifier_asserted_literals(_source_test("test_x", trace), "unrelated text")[0][
            "in_instruction"
        ]
        is False
    )
    # A missing instruction never reads as absent.
    assert (
        probe03.verifier_asserted_literals(_source_test("test_x", trace), None)[0]["in_instruction"]
        is None
    )


def test_verifier_asserted_literals_escaped_quote() -> None:
    trace = 'src = p.read_text()\n>   assert "a\\"b" in src\nE   AssertionError'
    records = probe03.verifier_asserted_literals(
        _source_test("test_esc", trace), 'instruction mentions a"b here'
    )
    assert [(r["literal"], r["kind"], r["in_instruction"]) for r in records] == [
        ('a"b', "source_membership", True)
    ]
    assert records[0]["evidence"] == 'assert "a\\"b" in src'


def test_verifier_asserted_literals_equality_and_membership() -> None:
    left = probe03.verifier_asserted_literals(
        _source_test("t", '>   assert result == "expected"\nE   AssertionError'),
        "produce expected output",
    )
    assert left == [
        {
            "literal": "expected",
            "in_instruction": True,
            "kind": "literal_comparison",
            "evidence": 'assert result == "expected"',
        }
    ]
    right = probe03.verifier_asserted_literals(
        _source_test("t", '>   assert "other" == result\nE   AssertionError'),
        "nothing relevant",
    )
    assert [(r["literal"], r["in_instruction"]) for r in right] == [("other", False)]
    number = probe03.verifier_asserted_literals(
        _source_test("t", ">   assert status == 42\nE   assert 0 == 42"),
        "retry up to 42 times",
    )
    assert [(r["literal"], r["kind"], r["in_instruction"]) for r in number] == [
        (42, "literal_comparison", True)
    ]
    member = probe03.verifier_asserted_literals(
        _source_test("t", '>   assert "pinned" in output\nE   AssertionError'),
        "nothing relevant",
    )
    assert [(r["literal"], r["kind"]) for r in member] == [("pinned", "literal_comparison")]


def test_verifier_asserted_literals_ignores_unsupported() -> None:
    traces = [
        ">   assert result == compute()\nE   AssertionError",  # computed
        ">   assert result == OTHER_CONST\nE   AssertionError",  # bare name
        ">   assert result is True\nE   AssertionError",  # not == / in
        '>   assert "a" == "a"\nE   AssertionError',  # both sides literal
        '>   assert x in "container"\nE   AssertionError',  # literal container
        '>   assert "x" not in y\nE   AssertionError',  # negated membership
        "E   AssertionError: 'stray' not in output",  # traceback literal only
        "no traceback at all",
    ]
    for trace in traces:
        assert probe03.verifier_asserted_literals(_source_test("t", trace), "anything") == [], trace
    assert probe03.verifier_asserted_literals("nope", "anything") == []  # type: ignore[arg-type]


def test_multiple_failing_tests_and_source_formatter_first_hit(tmp_path: Path) -> None:
    first = _source_test("test_first", 'src = open(p).read()\n>   assert "first-pin" in src\nE ...')
    second = _source_test("test_second", '>   assert got == "second-pin"\nE   AssertionError')
    trial = _write_verifier_trial(tmp_path, "trial-multi", [first, second])
    assert [r["literal"] for r in probe03.verifier_asserted_literals(first, "")] == ["first-pin"]
    assert [(r["literal"], r["kind"]) for r in probe03.verifier_asserted_literals(second, "")] == [
        ("second-pin", "literal_comparison")
    ]
    note = probe03.source_text_assertion(trial)
    assert note is not None
    assert "test_first" in note and "test_second" not in note


def _write_stdout_trial(
    root: Path,
    name: str,
    *,
    stdout: str | None = None,
    log: str | None = None,
    tests: list[dict] | None = None,
) -> Path:
    trial = root / name
    (trial / "verifier").mkdir(parents=True)
    if tests is not None:
        (trial / "verifier" / "ctrf.json").write_text(
            json.dumps({"results": {"tests": tests}}), encoding="utf-8"
        )
    if stdout is not None:
        (trial / "verifier" / "test-stdout.txt").write_text(stdout, encoding="utf-8")
    if log is not None:
        (trial / "verifier" / "test_output.log").write_text(log, encoding="utf-8")
    return trial


PYTEST_ONE_FAIL = """\
============================= test session starts ==============================
platform linux -- Python 3.10.18, pytest-7.4.2 -- /usr/local/bin/python
collecting ... collected 3 items

tests/test_task.py::TestWSGITask::test_head FAILED [ 33%]
tests/test_task.py::TestWSGITask::test_get_a PASSED [ 66%]
tests/test_task.py::TestWSGITask::test_get_b PASSED [100%]

=================================== FAILURES ===================================
__________ TestWSGITask.test_head __________

>       self.assertEqual(inst.close_on_finish, False)
E       AssertionError: True != False

tests/test_task.py:622: AssertionError
=========================== short test summary info ============================
FAILED tests/test_task.py::TestWSGITask::test_head
========================= 1 failed, 2 passed in 0.07s ==========================
test command exited 1
"""


def test_test_records_absent_by_default(tmp_path: Path) -> None:
    trial = _write_stdout_trial(tmp_path, "trial-default", stdout=PYTEST_ONE_FAIL)
    passage = probe03._verifier_passage(trial)
    assert "test_records" not in passage
    assert "test_records_source" not in passage
    assert "test_records_complete" not in passage


def test_test_records_prefers_ctrf(tmp_path: Path) -> None:
    trial = _write_stdout_trial(
        tmp_path,
        "trial-ctrf",
        stdout=PYTEST_ONE_FAIL,
        tests=[
            {"name": "test_ok", "status": "passed"},
            {"name": "test_bad", "status": "failed", "trace": "> assert x\nE boom"},
            {"name": "test_skip", "status": "skipped"},
        ],
    )
    passage = probe03._verifier_passage(trial, include_test_records=True)
    assert passage["test_records_source"] == "ctrf"
    assert passage["test_records_complete"] is True
    assert [(r["name"], r["status"]) for r in passage["test_records"]] == [
        ("test_ok", "passed"),
        ("test_bad", "failed"),
        ("test_skip", "skipped"),
    ]
    assert "boom" in (passage["test_records"][1]["trace"] or "")
    assert passage["test_records"][0]["trace"] is None


def test_test_records_pytest_failed(tmp_path: Path) -> None:
    trial = _write_stdout_trial(tmp_path, "trial-pytest", stdout=PYTEST_ONE_FAIL)
    passage = probe03._verifier_passage(trial, include_test_records=True)
    assert passage["test_records_source"] == "test-stdout.pytest"
    assert passage["test_records_complete"] is True
    assert passage["test_records"] == [
        {
            "name": "tests/test_task.py::TestWSGITask::test_head",
            "status": "failed",
            "session": 1,
            "trace": passage["test_records"][0]["trace"],
        }
    ]
    assert "assertEqual" in (passage["test_records"][0]["trace"] or "")


def test_test_records_pytest_errors_are_not_failures(tmp_path: Path) -> None:
    stdout = """\
============================= test session starts ==============================
collecting ... collected 3 items

==================================== ERRORS ====================================
______________________ ERROR at setup of test_setup ________________________
E       fixture 'db' not found

=================================== FAILURES ===================================
__________ test_x __________

>       assert got == "want"
E       AssertionError

=========================== short test summary info ============================
FAILED tests/test_a.py::test_x
ERROR tests/test_a.py::test_setup - fixture 'db' not found
ERROR tests/test_b.py - collection error
========================= 1 failed, 2 errors in 0.50s ==========================
"""
    trial = _write_stdout_trial(tmp_path, "trial-errors", stdout=stdout)
    passage = probe03._verifier_passage(trial, include_test_records=True)
    assert passage["test_records_complete"] is True
    by_status = {}
    for record in passage["test_records"]:
        by_status.setdefault(record["status"], []).append(record)
    assert [r["name"] for r in by_status["failed"]] == ["tests/test_a.py::test_x"]
    assert len(by_status["error"]) == 2
    # Setup/collection errors carry no trace: never assert-fail evidence.
    assert all(r["trace"] is None for r in by_status["error"])
    assert "assert got" in (by_status["failed"][0]["trace"] or "")


def test_test_records_pytest_multi_session(tmp_path: Path) -> None:
    stdout = """\
..
2 passed in 0.46s
============================= test session starts ==============================
collecting ... collected 2 items

tests/test_a.py::test_x FAILED [ 50%]
tests/test_a.py::test_y PASSED [100%]

=================================== FAILURES ===================================
__________ test_x __________

>       assert 1 == 2
E       assert 1 == 2

=========================== short test summary info ============================
FAILED tests/test_a.py::test_x
========================= 1 failed, 1 passed in 0.86s ==========================
"""
    trial = _write_stdout_trial(tmp_path, "trial-sessions", stdout=stdout)
    passage = probe03._verifier_passage(trial, include_test_records=True)
    assert passage["test_records_complete"] is True
    assert [(r["name"], r["session"]) for r in passage["test_records"]] == [
        ("tests/test_a.py::test_x", 2)
    ]
    assert "assert 1 == 2" in (passage["test_records"][0]["trace"] or "")


def test_test_records_pytest_unreconciled_is_incomplete(tmp_path: Path) -> None:
    stdout = PYTEST_ONE_FAIL.replace(
        "========================= 1 failed, 2 passed in 0.07s ==========================",
        "========================= 2 failed, 2 passed in 0.07s ==========================",
    )
    trial = _write_stdout_trial(tmp_path, "trial-unreconciled", stdout=stdout)
    passage = probe03._verifier_passage(trial, include_test_records=True)
    assert passage["test_records_complete"] is False
    # Observed names are still returned; only completeness is withheld.
    assert [r["name"] for r in passage["test_records"]] == [
        "tests/test_task.py::TestWSGITask::test_head"
    ]


UNITTEST_TWO = """\
======================================================================
ERROR: test_alpha (pkg.TestC.test_alpha)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/testbed/tests/test_x.py", line 10, in test_alpha
    self.assertTrue(False)
AssertionError: False is not true

======================================================================
FAIL: test_beta (pkg.TestC.test_beta) (param=1)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/testbed/tests/test_x.py", line 20, in test_beta
    self.assertEqual(a, b)
AssertionError: 1 != 2

----------------------------------------------------------------------
Ran 2 tests in 0.006s

FAILED (failures=1, errors=1)
test command exited 1
"""


def test_test_records_unittest(tmp_path: Path) -> None:
    trial = _write_stdout_trial(tmp_path, "trial-unit", stdout=UNITTEST_TWO)
    passage = probe03._verifier_passage(trial, include_test_records=True)
    assert passage["test_records_source"] == "test-stdout.unittest"
    assert passage["test_records_complete"] is True
    assert [(r["name"], r["status"]) for r in passage["test_records"]] == [
        ("test_alpha (pkg.TestC.test_alpha)", "error"),
        ("test_beta (pkg.TestC.test_beta) (param=1)", "failed"),
    ]
    assert "AssertionError" in (passage["test_records"][0]["trace"] or "")
    assert "1 != 2" in (passage["test_records"][1]["trace"] or "")


def test_test_records_unittest_truncated_is_incomplete(tmp_path: Path) -> None:
    stdout = UNITTEST_TWO.replace("FAILED (failures=1, errors=1)", "FAILED (failures=1, errors=2)")
    trial = _write_stdout_trial(tmp_path, "trial-unit-trunc", stdout=stdout)
    passage = probe03._verifier_passage(trial, include_test_records=True)
    assert passage["test_records_complete"] is False
    assert len(passage["test_records"]) == 2


def test_test_records_unittest_all_passed(tmp_path: Path) -> None:
    trial = _write_stdout_trial(
        tmp_path,
        "trial-unit-ok",
        stdout="----------------------------------------------------------------------\nRan 3 tests in 0.001s\n\nOK (skipped=2)\n",
    )
    passage = probe03._verifier_passage(trial, include_test_records=True)
    assert passage["test_records"] == []
    assert passage["test_records_complete"] is True


def test_test_records_missing(tmp_path: Path) -> None:
    trial = _write_stdout_trial(tmp_path, "trial-missing")
    passage = probe03._verifier_passage(trial, include_test_records=True)
    assert passage["test_records"] is None
    assert passage["test_records_source"] == "missing"
    assert passage["test_records_complete"] is False


def test_test_records_output_log_fallback(tmp_path: Path) -> None:
    trial = _write_stdout_trial(tmp_path, "trial-log", log=PYTEST_ONE_FAIL)
    passage = probe03._verifier_passage(trial, include_test_records=True)
    assert passage["test_records_source"] == "test_output.pytest"
    assert passage["test_records_complete"] is True
    assert [r["name"] for r in passage["test_records"]] == [
        "tests/test_task.py::TestWSGITask::test_head"
    ]


def test_verifier_asserted_literals_skips_error_and_skipped() -> None:
    trace = '>   assert result == "expected"\nE   AssertionError'
    assert (
        probe03.verifier_asserted_literals(
            {"name": "t", "status": "error", "trace": trace}, "expected"
        )
        == []
    )
    assert (
        probe03.verifier_asserted_literals(
            {"name": "t", "status": "skipped", "trace": trace}, "expected"
        )
        == []
    )


def test_pytest_failure_details_do_not_change_parameterized_test_identity(tmp_path: Path) -> None:
    stdout = """\
============================= FAILURES =============================
____________________ test_value[a - b] ____________________
>   assert result == "required"
E   AssertionError: wrong result
===================== short test summary info =====================
FAILED tests/test_value.py::test_value[a - b] - AssertionError: wrong result
========================= 1 failed in 0.01s =========================
"""
    trial = _write_stdout_trial(tmp_path, "details", stdout=stdout)
    passage = probe03._verifier_passage(trial, include_test_records=True)
    record = passage["test_records"][0]
    assert record["name"] == "tests/test_value.py::test_value[a - b]"
    assert probe03.verifier_asserted_literals(record, "Return a value.")[0]["literal"] == "required"


def test_failure_assertion_after_long_context_is_not_lost(tmp_path: Path) -> None:
    stdout = (
        "================== FAILURES ==================\n"
        "__________________ test_late __________________\n"
        + "    context = 'earlier test setup'\n"
        * 150
        + '>   assert result == "late expected"\nE   AssertionError\n'
        "============== short test summary info ==============\n"
        "FAILED tests/test_value.py::test_late\n"
        "================= 1 failed in 0.01s =================\n"
    )
    trial = _write_stdout_trial(tmp_path, "long-context", stdout=stdout)
    record = probe03._verifier_passage(trial, include_test_records=True)["test_records"][0]
    assert (
        probe03.verifier_asserted_literals(record, "Return a value.")[0]["literal"]
        == "late expected"
    )


def test_optional_or_chained_comparison_does_not_pin_an_expected_literal() -> None:
    for line in (
        'assert "optional" in source or fallback',
        'assert "optional" == result == alternate',
    ):
        test = {"status": "failed", "trace": f"source = p.read_text()\n>   {line}\n"}
        assert probe03.verifier_asserted_literals(test, "Return a value.") == []
    test = {"status": "failed", "trace": '>   assert "a in b" in result\n'}
    assert probe03.verifier_asserted_literals(test, "Return a value.")[0]["literal"] == "a in b"


def test_mixed_framework_output_cannot_claim_complete_single_framework_coverage(
    tmp_path: Path,
) -> None:
    trial = _write_stdout_trial(tmp_path, "mixed", stdout=PYTEST_ONE_FAIL + UNITTEST_TWO)
    passage = probe03._verifier_passage(trial, include_test_records=True)
    assert passage["test_records_source"] == "test-stdout.mixed"
    assert passage["test_records_complete"] is False


def test_malformed_ctrf_rows_are_unknown_not_successfully_empty(tmp_path: Path) -> None:
    trial = _write_stdout_trial(tmp_path, "malformed", tests=[None])
    passage = probe03._verifier_passage(trial, include_test_records=True)
    assert passage["test_records_complete"] is False
    (trial / "verifier" / "ctrf.json").write_text("[]")
    assert (
        probe03._verifier_passage(trial, include_test_records=True)["test_records_complete"]
        is False
    )
