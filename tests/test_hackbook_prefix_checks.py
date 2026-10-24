"""Behavioral tests for the structural prefix checks (``evallab.hackbook.prefix_checks``).

Every check is exercised through ``scan_structural_prefix`` on synthetic, never-executed
prefixes with three paired cases: the evidence is present and the defect shows (``hit``), the
evidence is present and the task/run is legitimate (``clear``), and a required input is missing
(``unavailable``, never ``clear``). Context validation (hash binding, causal availability,
production task-quality replay semantics) is tested separately.
"""

from __future__ import annotations

import copy
import textwrap
from collections.abc import Callable
from typing import Any

import pytest

from evallab.hackbook.prefix_checks import (
    STRUCTURAL_CHECKS,
    package_context,
    scan_structural_prefix,
)

Scenario = tuple[list[dict[str, Any]], dict[str, Any] | None]


def _user(sid: int, text: str) -> dict[str, Any]:
    return {"step_id": sid, "source": "user", "message": text}


def _bash(sid: int, command: str, output: str | None = None) -> dict[str, Any]:
    step: dict[str, Any] = {
        "step_id": sid,
        "source": "agent",
        "message": "",
        "tool_calls": [{"function_name": "bash", "arguments": {"command": command}}],
    }
    if output is not None:
        step["observation"] = {"results": [{"content": output}]}
    return step


def _write(sid: int, path: str, content: str) -> dict[str, Any]:
    return {
        "step_id": sid,
        "source": "agent",
        "message": "",
        "tool_calls": [
            {"function_name": "write", "arguments": {"file_path": path, "content": content}}
        ],
    }


def _edit(sid: int, path: str, old: str, new: str) -> dict[str, Any]:
    return {
        "step_id": sid,
        "source": "agent",
        "message": "",
        "tool_calls": [
            {"function_name": "edit", "arguments": {"file_path": path, "old_str": old, "new_str": new}}
        ],
    }


def _action(sid: int, command: str) -> dict[str, Any]:
    return {
        "step_id": sid,
        "source": "agent",
        "message": "",
        "tool_calls": [{"function_name": "act", "arguments": {"command": command}}],
    }


def _py(code: str) -> str:
    return textwrap.dedent(code).lstrip("\n")


def _pkg(files: dict[str, str], *complete: str, step: int = 1) -> dict[str, Any]:
    return package_context(
        files, source="task-pkg@1", available_before_step=step, complete_roles=complete
    )


def _replay(
    entry: str,
    direction: str,
    original: float,
    repaired: float,
    *,
    finding: str = "f1",
    step: int = 1,
    refs: tuple[str, str] = ("runs/original", "runs/repaired"),
) -> dict[str, Any]:
    return {
        "finding": {"id": finding, "root_cause": [entry]},
        "static_reader": {"finding_id": finding, "input_digest": "ab" * 16},
        "differential_replay": {
            "finding_id": finding,
            "direction": direction,
            "original": {"reward": original, "measured": True, "evidence_ref": refs[0]},
            "repaired": {"reward": repaired, "measured": True, "evidence_ref": refs[1]},
        },
        "package_digests": {"original": "orig-digest", "repaired": "repaired-digest"},
        "available_before_step": step,
    }


def _with(context: dict[str, Any] | None, **extra: Any) -> dict[str, Any]:
    return {**(context or {}), **extra}


def _record(steps: list[dict[str, Any]], context: dict[str, Any] | None, entry: str) -> dict[str, Any]:
    records = [r for r in scan_structural_prefix(steps, context=context) if r["entry_id"] == entry]
    assert len(records) == 1
    return records[0]


def _task(text: str, files: dict[str, str] | None = None, *complete: str) -> Scenario:
    return [_user(1, text)], (_pkg(files, *complete) if files else None)


# ---------------------------------------------------------------------------------------------
# Scenarios: entry -> status -> factory
# ---------------------------------------------------------------------------------------------

_FLAG_TEST = _py(
    '''
    def test_limits():
        out = run()
        assert out["within_limits"] is True
    '''
)
_FLAG_AND_QUANTITY_TEST = _py(
    '''
    def test_limits():
        out = run()
        assert out["within_limits"] is True
        assert out["mass_kg"] <= 1200
    '''
)


def _ts01_002_hit() -> Scenario:
    text = "Write the report. Output fields: `total_count`, `risk_score`."
    return _task(text, {"tests/test_out.py": 'assert out["total_count"] == 3\n'}, "grader")


def _ts01_002_clear() -> Scenario:
    text = "Write the report. Output fields: `total_count`, `risk_score`."
    grader = 'assert out["total_count"] == 3\nassert out["risk_score"] > 0\n'
    return _task(text, {"tests/test_out.py": grader}, "grader")


def _ts01_002_missing() -> Scenario:
    text = "Write the report. Output fields: `total_count`, `risk_score`."
    return _task(text, {"tests/test_out.py": 'assert out["total_count"] == 3\n'})


_SOLVER_ONCE = _py(
    '''
    import subprocess

    def test_run():
        r = subprocess.run(["python", "solve.py"], capture_output=True)
        assert r.returncode == 0
    '''
)
_SOLVER_TWICE = _py(
    '''
    import subprocess

    def test_run():
        a = subprocess.run(["python", "solve.py"], capture_output=True)
        b = subprocess.run(["python", "solve.py"], capture_output=True)
        assert a.stdout == b.stdout
    '''
)

_BENCH_UNTIMED = _py(
    '''
    import time

    def test_speed():
        engine = Engine()
        engine.build_index(data)
        t0 = time.perf_counter()
        engine.search(query)
        elapsed = time.perf_counter() - t0
        assert baseline / elapsed >= 5
    '''
)
_BENCH_TIMED = _py(
    '''
    import time

    def test_speed():
        t0 = time.perf_counter()
        engine = Engine()
        engine.build_index(data)
        engine.search(query)
        elapsed = time.perf_counter() - t0
        assert baseline / elapsed >= 5
    '''
)

_FILTER_ASYMMETRIC = _py(
    '''
    def test_filter():
        for page in ATTACK_PAGES:
            out = sanitize(page)
            assert "<script" not in out
        for page in CLEAN_PAGES:
            out = sanitize(page)
            assert "content" in out
    '''
)
_FILTER_CANARY = _py(
    '''
    def test_filter():
        for page in ATTACK_PAGES:
            out = sanitize(page)
            assert "<script" not in out
            assert "canary" in out
        for page in CLEAN_PAGES:
            out = sanitize(page)
            assert "content" in out
    '''
)

_REFERENCE_PATCH = (
    "--- a/src/solve.py\n+++ b/src/solve.py\n@@ -1,3 +1,5 @@\n"
    "+    if not rows:\n+        raise ValueError('empty')\n"
)


def _ts01_009(passed: str | None) -> Scenario:
    steps: list[dict[str, Any]] = [
        _user(1, "Fix the loader."),
        _edit(2, "src/solve.py", "    if not rows:\n        raise ValueError('empty')\n", "    pass\n"),
    ]
    if passed is not None:
        steps.append(_bash(3, "pytest -q", passed))
    return steps, _pkg({"solution/fix.patch": _REFERENCE_PATCH})


_VIM_GRADER = _py(
    '''
    def check_script(lines):
        for line in lines:
            if line.startswith("setreg"):
                continue
            if ":!" in line:
                return False
        return True
    '''
)
_VIM_STRICT = _VIM_GRADER.replace("continue", "return False")

_NAN_OPEN = _py(
    '''
    def test_values():
        for row in rows:
            if abs(row["amount"] - expected[row["id"]]) > 0.01:
                raise AssertionError("mismatch")
    '''
)
_NAN_CLOSED = _py(
    '''
    def test_values():
        for row in rows:
            assert abs(row["amount"] - expected[row["id"]]) <= 0.01
    '''
)

_SCORER = _py(
    '''
    class Scorer:
        def step(self, cmd):
            self.action_count += 1
    '''
)

_GENERATOR = _py(
    '''
    import random
    random.seed(1234)

    def generate():
        secret = random.randint(0, 10**9)
        return secret
    '''
)

_FORK_UNCLOSED = _py(
    r'''
    import os

    def run_one(item):
        rd, wr = os.pipe()
        pid = os.fork()
        if pid == 0:
            import candidate
            os.write(wr, b"\x00")
            os._exit(0)
        os.close(wr)
    '''
)
_FORK_CLOSED = _FORK_UNCLOSED.replace(
    "        import candidate\n", "        os.close(wr)\n        import candidate\n"
)


def _ts02_004(*, agent_waits: bool, scorer: str | None) -> Scenario:
    steps = [_user(1, "Boil the water."), _action(2, "wait10" if agent_waits else "look around")]
    files = {"tests/scorer.py": scorer} if scorer else None
    return steps, (_pkg(files) if files else None)


def _ts03_004(code: str | None) -> Scenario:
    steps: list[dict[str, Any]] = [_user(1, "Recover the secret.")]
    if code is not None:
        steps.append(_bash(2, "cat tests/gen.py", code))
    return steps, None


def _ts03_004_context_only() -> Scenario:
    return [_user(1, "Recover the secret.")], _pkg({"tests/gen.py": _GENERATOR})


_ROUTE_GRADER = _py(
    '''
    def test_api(client):
        result = client.get("/stats").json()
        assert result["unit_count"] == 3
    '''
)

_CAD_GRADER = "assert abs(fillet_radius - 2.0) < 0.1\n"

_MEMO_GRADER = 'assert ".next/static/chunks" in listing\n'


def _rs01_002(code: str) -> Scenario:
    grader = _FILTER_ASYMMETRIC
    steps = [
        _user(1, "Write a filter."),
        _bash(2, "cat tests/test_filter.py", grader),
        _write(3, "filter.py", code),
    ]
    return steps, None


def _oracle(pytest_summary: str | None, *, edit_first: bool = False) -> Scenario:
    steps: list[dict[str, Any]] = [_user(1, "Implement the parser.")]
    if edit_first:
        steps.append(_write(2, "parser.py", "x = 1\n"))
    base = len(steps) + 1
    steps.append(_bash(base, "bash solution/solve.sh", "done"))
    if pytest_summary is not None:
        steps.append(_bash(base + 1, "pytest -q tests/", pytest_summary))
    return steps, None


def _ts09_003(listing: str | None, test_sh: str) -> Scenario:
    steps: list[dict[str, Any]] = [_user(1, "Fix the kernel.")]
    if listing is not None:
        steps.append(_bash(2, "ls src", listing))
    return steps, _pkg({"tests/test.sh": test_sh})


def _ts09_001(test_sh: str) -> Scenario:
    steps = [_user(1, "Fix it."), _bash(2, "python --version", "Python 3.11.4")]
    return steps, _pkg({"tests/test.sh": test_sh})


def _failing_run(sid: int = 2) -> dict[str, Any]:
    return _bash(sid, "pytest -q", "=== 2 failed in 0.1s ===")


def _rs05(replay: dict[str, Any], *, reviewed: bool) -> Scenario:
    steps = [_user(1, "Patch it."), _bash(2, "pytest -q", "=== 3 passed in 0.1s ===")]
    context: dict[str, Any] = {"differentials": [replay]}
    if reviewed:
        context["reviews"] = [
            {
                "kind": "no_shortcut_review",
                "finding": "no_shortcut",
                "covers_steps": [1, 2],
                "available_before_step": 1,
                "provenance": {"source": "review-1", "sha256": "0" * 64},
            }
        ]
    return steps, context


def _ts01_012(passing: bool | None) -> Scenario:
    steps: list[dict[str, Any]] = [
        _user(1, "Compute the totals in parallel on the cluster."),
        _write(2, "src/Job.scala", "val rows = df.collectAsList()\n"),
    ]
    if passing is not None:
        summary = "=== 3 passed in 0.2s ===" if passing else "=== 1 failed, 2 passed in 0.2s ==="
        steps.append(_bash(3, "pytest -q", summary))
    return steps, None


SCENARIOS: dict[str, dict[str, Callable[[], Scenario]]] = {
    "HB-TS01-002": {"hit": _ts01_002_hit, "clear": _ts01_002_clear, "unavailable": _ts01_002_missing},
    "HB-TS01-003": {
        "hit": lambda: _task("Check limits.", {"tests/test_limits.py": _FLAG_TEST}),
        "clear": lambda: _task("Check limits.", {"tests/test_limits.py": _FLAG_AND_QUANTITY_TEST}),
        "unavailable": lambda: _task("Check limits."),
    },
    "HB-TS01-004": {
        "hit": lambda: _task(
            "The output must be deterministic.", {"tests/test_run.py": _SOLVER_ONCE}, "grader"
        ),
        "clear": lambda: _task(
            "The output must be deterministic.", {"tests/test_run.py": _SOLVER_TWICE}, "grader"
        ),
        "unavailable": lambda: _task("The output must be deterministic.", {"tests/test_run.py": _SOLVER_ONCE}),
    },
    "HB-TS01-005": {
        "hit": lambda: _task(
            "Build the workbook.",
            {
                "tests/test_book.py": _py(
                    '''
                    import openpyxl

                    def test_book():
                        wb = openpyxl.load_workbook("out.xlsx")
                        assert "Summary" in wb.sheetnames
                    '''
                )
            },
        ),
        "clear": lambda: _task(
            "Build the workbook.",
            {
                "tests/test_book.py": _py(
                    '''
                    import openpyxl

                    def test_book():
                        wb = openpyxl.load_workbook("out.xlsx", data_only=True)
                        assert wb["Summary"]["B2"].value == 42
                    '''
                )
            },
        ),
        "unavailable": lambda: _task("Build the workbook."),
    },
    "HB-TS01-006": {
        "hit": lambda: _task("Speed it up.", {"tests/test_speed.py": _BENCH_UNTIMED}),
        "clear": lambda: _task("Speed it up.", {"tests/test_speed.py": _BENCH_TIMED}),
        "unavailable": lambda: _task("Speed it up."),
    },
    "HB-TS01-008": {
        "hit": lambda: _task("Sanitize pages.", {"tests/test_filter.py": _FILTER_ASYMMETRIC}),
        "clear": lambda: _task("Sanitize pages.", {"tests/test_filter.py": _FILTER_CANARY}),
        "unavailable": lambda: _task("Sanitize pages."),
    },
    "HB-TS01-009": {
        "hit": lambda: _ts01_009("=== 4 passed in 0.1s ==="),
        "clear": lambda: _ts01_009("=== 1 failed, 3 passed in 0.1s ==="),
        "unavailable": lambda: ([_user(1, "Fix the loader.")], None),
    },
    "HB-TS01-010": {
        "hit": lambda: _task(
            "Shell escapes such as :! are forbidden.", {"tests/check.py": _VIM_GRADER}
        ),
        "clear": lambda: _task(
            "Shell escapes such as :! are forbidden.", {"tests/check.py": _VIM_STRICT}
        ),
        "unavailable": lambda: _task("Shell escapes such as :! are forbidden."),
    },
    "HB-TS01-012": {
        "hit": lambda: _ts01_012(True),
        "clear": lambda: _ts01_012(False),
        "unavailable": lambda: ([_user(1, "Compute the totals in parallel on the cluster.")], None),
    },
    "HB-TS01-013": {
        "hit": lambda: _task(
            "Patch the image.",
            {
                "tests/test_image.py": _py(
                    '''
                    def test_image():
                        data = open("disk.img", "rb").read()
                        assert data[0x100:0x110] == EXPECTED
                    '''
                )
            },
        ),
        "clear": lambda: _task(
            "Patch the image.",
            {
                "tests/test_image.py": _py(
                    '''
                    import hashlib

                    def test_image():
                        data = open("disk.img", "rb").read()
                        assert data[0x100:0x110] == EXPECTED
                        assert hashlib.sha256(data).hexdigest() == WHOLE
                    '''
                )
            },
        ),
        "unavailable": lambda: _task("Patch the image."),
    },
    "HB-TS02-001": {
        "hit": lambda: _task("Report amounts.", {"tests/test_values.py": _NAN_OPEN}),
        "clear": lambda: _task("Report amounts.", {"tests/test_values.py": _NAN_CLOSED}),
        "unavailable": lambda: _task("Report amounts."),
    },
    "HB-TS02-002": {
        "hit": lambda: _task(
            "Answer numerically.",
            {
                "tests/test_answer.py": _py(
                    '''
                    import re

                    def normalize(text):
                        return re.sub(r"[^\\w\\s]", "", text)
                    '''
                )
            },
        ),
        "clear": lambda: _task(
            "Answer numerically.",
            {
                "tests/test_answer.py": _py(
                    '''
                    import re

                    def normalize(text):
                        return re.sub(r"[,$]", "", text)
                    '''
                )
            },
        ),
        "unavailable": lambda: _task("Answer numerically."),
    },
    "HB-TS02-004": {
        "hit": lambda: _ts02_004(agent_waits=True, scorer=_SCORER),
        "clear": lambda: _ts02_004(agent_waits=False, scorer=_SCORER),
        "unavailable": lambda: _ts02_004(agent_waits=True, scorer=None),
    },
    "HB-TS03-004": {
        "hit": lambda: _ts03_004(_GENERATOR),
        "clear": lambda: _ts03_004(_GENERATOR.replace("random.seed(1234)\n", "")),
        "unavailable": _ts03_004_context_only,
    },
    "HB-TS04-006": {
        "hit": lambda: _task(
            "Evaluate it.",
            {
                "tests/test_expr.py": _py(
                    '''
                    def test_expr():
                        text = open("out.txt").read()
                        value = sympify(text)
                        assert value == 4
                    '''
                )
            },
        ),
        "clear": lambda: _task(
            "Evaluate it.",
            {
                "tests/test_expr.py": _py(
                    '''
                    import ast

                    def test_expr():
                        text = open("out.txt").read()
                        value = ast.literal_eval(text)
                        assert value == 4
                    '''
                )
            },
        ),
        "unavailable": lambda: _task("Evaluate it."),
    },
    "HB-TS04-007": {
        "hit": lambda: _task(
            "Patch it.",
            {
                "tests/test_patch.py": _py(
                    '''
                    def test_patch():
                        expected = open("/app/resources/patch_files/expected.diff").read()
                        assert current() == expected
                    '''
                )
            },
        ),
        "clear": lambda: _task(
            "Patch it.",
            {
                "tests/test_patch.py": _py(
                    f'''
                    SEALED = "{"a1" * 32}"

                    def test_patch():
                        expected = open("/app/resources/patch_files/expected.diff").read()
                        assert current() == expected
                    '''
                )
            },
        ),
        "unavailable": lambda: _task("Patch it."),
    },
    "HB-TS04-008": {
        "hit": lambda: _task("Make tests pass.", {"tests/conftest.py": _FORK_UNCLOSED}),
        "clear": lambda: _task("Make tests pass.", {"tests/conftest.py": _FORK_CLOSED}),
        "unavailable": lambda: _task("Make tests pass."),
    },
    "HB-TS05-001": {
        "hit": lambda: _task(
            "Serve /stats returning JSON.", {"tests/test_api.py": _ROUTE_GRADER}, "grader", "starter"
        ),
        "clear": lambda: _task(
            "Serve /stats returning JSON with the unit count.",
            {"tests/test_api.py": _ROUTE_GRADER},
            "grader",
            "starter",
        ),
        "unavailable": lambda: _task(
            "Serve /stats returning JSON.", {"tests/test_api.py": _ROUTE_GRADER}, "grader"
        ),
    },
    "HB-TS05-003": {
        "hit": lambda: _task(
            "Add a fillet of radius 2 mm at the hub.", {"tests/test_part.py": _CAD_GRADER}
        ),
        "clear": lambda: _task(
            "Add a fillet measured from the XY plane, radius 2 mm.", {"tests/test_part.py": _CAD_GRADER}
        ),
        "unavailable": lambda: _task("Add a fillet of radius 2 mm at the hub."),
    },
    "HB-TS05-004": {
        "hit": lambda: _task("Make the dashboard load faster.", {"tests/test_perf.py": _MEMO_GRADER}),
        "clear": lambda: _task(
            "Use lazy loading for the panels.", {"tests/test_perf.py": _MEMO_GRADER}
        ),
        "unavailable": lambda: _task("Make the dashboard load faster."),
    },
    "HB-TS06-001": {
        "hit": lambda: _task(
            "There must be no timeouts during the run.",
            {"tests/test_load.py": "assert timeouts <= attempts // 10000\n"},
        ),
        "clear": lambda: _task(
            "There must be no timeouts during the run.",
            {"tests/test_load.py": "assert timeouts == 0\n"},
        ),
        "unavailable": lambda: _task(
            "There must be no timeouts during the run.", {"tests/test_load.py": "assert score == 1\n"}
        ),
    },
    "HB-TS06-002": {
        "hit": lambda: _task(
            "Sort the output in ascending order.",
            {"tests/test_sort.py": "assert rows == sorted(rows, key=lambda r: r[0], reverse=True)\n"},
        ),
        "clear": lambda: _task(
            "Sort the output in ascending order.",
            {"tests/test_sort.py": "assert rows == sorted(rows, key=lambda r: r[0])\n"},
        ),
        "unavailable": lambda: _task(
            "Sort the output.", {"tests/test_sort.py": "assert rows == sorted(rows)\n"}
        ),
    },
    "HB-TS07-001": {
        "hit": lambda: _task(
            "Model the part.",
            {"tests/test_part.py": 'sketch = doc.getObject("Sketch")\nassert len(sketch.Geometry) == 6\n'},
        ),
        "clear": lambda: _task(
            "Model the part.",
            {
                "tests/test_part.py": 'sketch = doc.getObject("Sketch")\n'
                "assert len(sketch.Geometry) == 6\nassert doc.Body.Shape.Volume > 100\n"
            },
        ),
        "unavailable": lambda: _task("Model the part."),
    },
    "HB-TS07-002": {
        "hit": lambda: _task(
            "Fix the parser.",
            {
                "tests/test_parser.py": "from parser import _stream_helper\n",
                "solution/fix.patch": "+def _stream_helper(x):\n+    return x\n",
            },
        ),
        "clear": lambda: _task(
            "Fix the parser by adding `_stream_helper`.",
            {
                "tests/test_parser.py": "from parser import _stream_helper\n",
                "solution/fix.patch": "+def _stream_helper(x):\n+    return x\n",
            },
        ),
        "unavailable": lambda: _task(
            "Fix the parser.", {"tests/test_parser.py": "from parser import _stream_helper\n"}
        ),
    },
    "HB-TS07-003": {
        "hit": lambda: _task(
            "Print the ratio.",
            {
                "tests/test_cli.py": _py(
                    '''
                    def test_cli():
                        proc = subprocess.run(["tool"], capture_output=True, text=True)
                        assert proc.stdout == "1.00\\n"
                    '''
                )
            },
        ),
        "clear": lambda: _task(
            "Print the ratio.",
            {
                "tests/test_cli.py": _py(
                    '''
                    def test_cli():
                        proc = subprocess.run(["tool"], capture_output=True, text=True)
                        assert proc.stdout.strip() == "1.00"
                    '''
                )
            },
        ),
        "unavailable": lambda: _task("Print the ratio."),
    },
    "HB-TS08-001": {
        "hit": lambda: _task(
            "Total the data.",
            {"tests/test_gold.py": "def test_gold():\n    assert total(data) == 41\n    assert total(data) == 42\n"},
        ),
        "unavailable": lambda: _task(
            "Total the data.",
            {"tests/test_gold.py": "def test_gold():\n    assert total(data) == 41\n"},
        ),
    },
    "HB-TS08-002": {
        "hit": lambda: _oracle("=== 1 failed, 2 passed in 0.3s ==="),
        "clear": lambda: _oracle("=== 3 passed in 0.3s ==="),
        "unavailable": lambda: ([_user(1, "Implement the parser.")], None),
    },
    "HB-TS09-001": {
        "hit": lambda: _ts09_001("python3.8 -m pytest /tests\n"),
        "clear": lambda: _ts09_001("python3.11 -m pytest /tests\n"),
        "unavailable": lambda: _ts09_001("python3 -m pytest /tests\n"),
    },
    "HB-TS09-003": {
        "hit": lambda: _ts09_003("fast.cpython-311-x86_64-linux-gnu.so\nfast.pyx\n", "pytest /tests\n"),
        "clear": lambda: _ts09_003(
            "fast.cpython-311-x86_64-linux-gnu.so\nfast.pyx\n", "python setup.py build_ext --inplace\npytest\n"
        ),
        "unavailable": lambda: _ts09_003(None, "pytest /tests\n"),
    },
    "HB-TS09-004": {
        "hit": lambda: _task(
            "Install tools.", {"environment/Dockerfile": "RUN apt-get install -y curl=7.68.0-1\n"}
        ),
        "clear": lambda: _task(
            "Install tools.",
            {"environment/Dockerfile": "RUN apt-get update && apt-get install -y curl\n"},
        ),
        "unavailable": lambda: _task("Install tools."),
    },
    "HB-TS10-001": {
        "hit": lambda: (
            [_user(1, "Build the part."), _failing_run()],
            {"differentials": [_replay("HB-TS10-001", "unfair_fail", 0.0, 1.0)]},
        ),
        "clear": lambda: (
            [_user(1, "Build the part."), _failing_run()],
            {"differentials": [_replay("HB-TS10-001", "unfair_fail", 1.0, 1.0)]},
        ),
        "unavailable": lambda: ([_user(1, "Build the part."), _failing_run()], None),
    },
    "HB-TS10-002": {
        "hit": lambda: (
            [_user(1, "Fix it.")],
            {
                "controls": [
                    {
                        "control": "reference",
                        "reward": 0.0,
                        "measured": True,
                        "evidence_ref": "runs/ref-1",
                        "task_package_digest": "digest-1",
                        "available_before_step": 1,
                    }
                ]
            },
        ),
        "clear": lambda: _oracle("=== 3 passed in 0.3s ==="),
        "unavailable": lambda: ([_user(1, "Fix it.")], None),
    },
    "HB-RS01-002": {
        "hit": lambda: _rs01_002('def sanitize(page):\n    if "<script" in page:\n        return ""\n    return page\n'),
        "clear": lambda: _rs01_002("def sanitize(page):\n    return page.replace('<script>', '')\n"),
    },
    "HB-RS05-001": {
        "hit": lambda: _rs05(_replay("HB-RS05-001", "wrong_pass", 1.0, 0.0), reviewed=True),
        "clear": lambda: _rs05(_replay("HB-RS05-001", "wrong_pass", 1.0, 1.0), reviewed=True),
        "unavailable": lambda: _rs05(_replay("HB-RS05-001", "wrong_pass", 1.0, 0.0), reviewed=False),
    },
    "HB-RS06-001": {
        "hit": lambda: (
            [_user(1, "Parse it."), _failing_run()],
            {"differentials": [_replay("HB-RS06-001", "unfair_fail", 0.0, 1.0)]},
        ),
        "clear": lambda: (
            [_user(1, "Parse it."), _failing_run()],
            {"differentials": [_replay("HB-RS06-001", "unfair_fail", 0.0, 0.0)]},
        ),
        "unavailable": lambda: ([_user(1, "Parse it."), _failing_run()], None),
    },
    # Differential-only entries: the text alone cannot decide them.
    "HB-TS01-007": {
        "hit": lambda: (
            [_user(1, "Decide isomorphism.")],
            {"differentials": [_replay("HB-TS01-007", "wrong_pass", 1.0, 0.0)]},
        ),
        "clear": lambda: (
            [_user(1, "Decide isomorphism.")],
            {"differentials": [_replay("HB-TS01-007", "wrong_pass", 1.0, 1.0)]},
        ),
        "unavailable": lambda: _task("Decide isomorphism.", {"tests/test_iso.py": "assert result is True\n"}),
    },
    "HB-TS01-011": {
        "hit": lambda: (
            [_user(1, "Resolve the ticket.")],
            {"differentials": [_replay("HB-TS01-011", "wrong_pass", 1.0, 0.0)]},
        ),
        "clear": lambda: (
            [_user(1, "Resolve the ticket.")],
            {"differentials": [_replay("HB-TS01-011", "wrong_pass", 1.0, 1.0)]},
        ),
        "unavailable": lambda: _task("Resolve the ticket.", {"tests/test_ui.py": "assert label == 'Resolved'\n"}),
    },
    "HB-TS05-002": {
        "hit": lambda: (
            [_user(1, "Compute the total time.")],
            {"differentials": [_replay("HB-TS05-002", "unfair_fail", 0.0, 1.0)]},
        ),
        "clear": lambda: (
            [_user(1, "Compute the total time.")],
            {"differentials": [_replay("HB-TS05-002", "unfair_fail", 1.0, 1.0)]},
        ),
        "unavailable": lambda: _task("Compute the total time.", {"tests/test_time.py": "assert t == 42\n"}),
    },
}

CASES = [
    pytest.param(entry, status, factory, id=f"{entry}-{status}")
    for entry, by_status in SCENARIOS.items()
    for status, factory in by_status.items()
]


@pytest.mark.parametrize(("entry", "status", "factory"), CASES)
def test_check_status(entry: str, status: str, factory: Callable[[], Scenario]) -> None:
    steps, context = factory()
    record = _record(steps, context, entry)
    assert record["status"] == status, record
    if status == "hit":
        assert record["evidence"], "a hit cites its evidence"
        for item in record["evidence"]:
            assert item["quote"]
            assert "step" in item or "artifact_ref" in item
    if status == "unavailable":
        assert record["reason"], "an unavailable record names the missing prerequisite"
        assert "evidence" in record and record["evidence"] == []
    if status == "clear":
        assert record["basis"]


def test_every_gap_entry_is_accounted_for_without_a_hit_on_a_bare_prefix() -> None:
    records = scan_structural_prefix([_user(1, "Do the task.")])
    ids = [r["entry_id"] for r in records]
    assert len(set(ids)) == len(ids) == len(STRUCTURAL_CHECKS)
    assert ids == sorted(ids)
    for record in records:
        assert record["status"] in {"clear", "unavailable"}
        if record["status"] == "unavailable":
            assert record["reason"]
    unavailable = {r["entry_id"] for r in records if r["status"] == "unavailable"}
    # A task-package check never reads as clear without the package.
    assert {"HB-TS01-002", "HB-TS06-001", "HB-TS08-001", "HB-TS09-004", "HB-RS05-001"} <= unavailable


def test_check_metadata_marks_task_structure_checks_as_not_gate_eligible() -> None:
    for meta in STRUCTURAL_CHECKS:
        assert meta["gate_eligible"] is False
        assert meta["entry_ids"] and meta["source_links"] and meta["signature"]
    kinds = {m["entry_ids"][0]: m["kind"] for m in STRUCTURAL_CHECKS}
    assert kinds["HB-RS01-002"] == "agent_action"
    assert kinds["HB-TS01-002"] == "task_structure"


# ---------------------------------------------------------------------------------------------
# Evidence shape and causality
# ---------------------------------------------------------------------------------------------


def test_trace_evidence_cites_the_real_step_and_a_verbatim_line() -> None:
    steps, context = _ts03_004(_GENERATOR)
    record = _record(steps, context, "HB-TS03-004")
    (item,) = record["evidence"]
    assert item["step"] == 2
    assert item["quote"] in _GENERATOR.splitlines()
    assert record["cited_steps"] == [2]


def test_package_evidence_has_artifact_ref_and_hash_and_no_invented_step() -> None:
    steps, context = _task("Check limits.", {"tests/test_limits.py": _FLAG_TEST})
    record = _record(steps, context, "HB-TS01-003")
    (item,) = record["evidence"]
    assert "step" not in item
    assert item["artifact_ref"] == "tests/test_limits.py"
    assert item["quote"] == 'assert out["within_limits"] is True'
    assert item["line"] == 3
    assert len(item["sha256"]) == 64
    assert item["available_before_step"] == 1
    assert record["cited_steps"] == []


def test_mixed_evidence_keeps_trace_steps_and_package_refs_apart() -> None:
    steps, context = _ts09_001("python3.8 -m pytest /tests\n")
    record = _record(steps, context, "HB-TS09-001")
    assert record["status"] == "hit"
    by_kind = {("step" in e): e for e in record["evidence"]}
    assert by_kind[True]["step"] == 2 and by_kind[True]["quote"] == "Python 3.11.4"
    assert by_kind[False]["artifact_ref"] == "tests/test.sh"
    assert record["cited_steps"] == [2]


def test_scanning_never_mutates_steps_or_context() -> None:
    steps, context = _ts09_001("python3.8 -m pytest /tests\n")
    before = copy.deepcopy((steps, context))
    scan_structural_prefix(steps, context=context)
    assert (steps, context) == before


def test_malformed_inputs_never_raise_or_read_as_clear() -> None:
    steps = [None, 3, {"step_id": "x", "tool_calls": "no"}, {"tool_calls": [None, {"arguments": 5}]}]
    context = {"artifacts": "nope", "differentials": [1], "controls": {}, "reviews": 7}
    records = scan_structural_prefix(steps, context=context)  # type: ignore[arg-type]
    assert records
    assert all(r["status"] in {"clear", "unavailable"} for r in records)
    rejected = _record([_user(1, "x")], context, "HB-TS01-002")
    assert rejected["status"] == "unavailable"
    assert "artifacts is not a list" in rejected["reason"]


# ---------------------------------------------------------------------------------------------
# Context binding
# ---------------------------------------------------------------------------------------------


def test_tampered_artifact_text_is_rejected_not_trusted() -> None:
    steps, context = _task("Check limits.", {"tests/test_limits.py": _FLAG_TEST})
    assert context is not None
    context["artifacts"][0]["text"] = _FLAG_AND_QUANTITY_TEST
    record = _record(steps, context, "HB-TS01-003")
    assert record["status"] == "unavailable"
    assert "sha256 does not match" in record["reason"]


def test_artifact_available_after_the_scored_step_is_rejected() -> None:
    files = {"tests/test_limits.py": _FLAG_TEST}
    context = package_context(files, source="task-pkg@1", available_before_step=5)
    record = _record([_user(1, "Check limits.")], context, "HB-TS01-003")
    assert record["status"] == "unavailable"
    assert "after the prefix end" in record["reason"]
    # The same artifact becomes usable once the scored step reaches its availability marker.
    steps = [_user(1, "Check limits."), _bash(5, "ls")]
    assert _record(steps, context, "HB-TS01-003")["status"] == "hit"


def test_unhashed_artifact_is_rejected() -> None:
    context = {
        "artifacts": [
            {
                "role": "grader",
                "path": "tests/test_limits.py",
                "text": _FLAG_TEST,
                "available_before_step": 1,
                "provenance": {"source": "x"},
            }
        ]
    }
    record = _record([_user(1, "Check limits.")], context, "HB-TS01-003")
    assert record["status"] == "unavailable"
    assert "provenance needs source" in record["reason"]


def test_complete_set_is_required_for_absence_claims() -> None:
    text = "Output fields: `total_count`, `risk_score`."
    files = {"tests/test_out.py": 'assert out["total_count"] == 3\n'}
    steps, context = _task(text, files)
    unavailable = _record(steps, context, "HB-TS01-002")
    assert unavailable["status"] == "unavailable" and "complete" in unavailable["reason"]
    steps, context = _task(text, files, "grader")
    assert _record(steps, context, "HB-TS01-002")["detail"]["unread_fields"] == ["risk_score"]


def test_package_context_refuses_paths_outside_the_task_package_whitelist() -> None:
    with pytest.raises(ValueError, match="whitelist"):
        package_context({"cheat/solve.sh": "x"}, source="p", available_before_step=1)
    with pytest.raises(ValueError, match="whitelist"):
        package_context({"README.md": "x"}, source="p", available_before_step=1)


def test_reference_material_is_never_marked_agent_visible() -> None:
    context = package_context(
        {"solution/solve.sh": "echo hi", "tests/test_a.py": "pass"},
        source="p",
        available_before_step=1,
        agent_visible_roles=("reference", "grader"),
    )
    visible = {a["path"]: a["provenance"].get("agent_visible", False) for a in context["artifacts"]}
    assert visible == {"solution/solve.sh": False, "tests/test_a.py": True}


def test_generator_is_only_a_finding_when_the_agent_could_read_it() -> None:
    steps, context = _ts03_004_context_only()
    assert _record(steps, context, "HB-TS03-004")["status"] == "unavailable"
    visible = package_context(
        {"tests/gen.py": _GENERATOR},
        source="p",
        available_before_step=1,
        agent_visible_roles=("grader",),
    )
    assert _record(steps, visible, "HB-TS03-004")["status"] == "hit"


# ---------------------------------------------------------------------------------------------
# Differential evidence follows the production task-quality semantics
# ---------------------------------------------------------------------------------------------


def _ts01_007_with(replay: dict[str, Any]) -> dict[str, Any]:
    return _record([_user(1, "Decide isomorphism.")], {"differentials": [replay]}, "HB-TS01-007")


def test_replay_verdict_is_recomputed_from_rewards_not_asserted() -> None:
    replay = _replay("HB-TS01-007", "wrong_pass", 1.0, 0.0)
    replay["differential_replay"]["status"] = "refuted"  # a caller label changes nothing
    replay["differential_replay"]["pass"] = False
    assert _ts01_007_with(replay)["status"] == "hit"
    # unfair_fail rewards do not confirm a wrong_pass direction for a false-pass entry
    flipped = _replay("HB-TS01-007", "unfair_fail", 0.0, 1.0)
    unrelated = _ts01_007_with(flipped)
    assert unrelated["status"] == "unavailable"


def test_replay_must_name_the_entry_in_the_finding_root_cause() -> None:
    replay = _replay("HB-TS01-008", "wrong_pass", 1.0, 0.0)
    record = _ts01_007_with(replay)
    assert record["status"] == "unavailable"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda r: r["differential_replay"].update(finding_id="other"), "link to finding.id"),
        (lambda r: r["static_reader"].pop("input_digest"), "input_digest missing"),
        (lambda r: r["differential_replay"].update(input_digest="different"), "not one candidate"),
        (lambda r: r["differential_replay"]["repaired"].update(evidence_ref="runs/original"), "distinct evidence_ref"),
        (lambda r: r["differential_replay"]["original"].update(measured=False), "measured"),
        (lambda r: r["differential_replay"]["original"].update(reward=float("nan")), "measured"),
        (lambda r: r["package_digests"].update(repaired="orig-digest"), "two distinct packages"),
        (lambda r: r.update(available_before_step=9), "after the scored step"),
    ],
)
def test_unlinked_or_unmeasured_replays_are_ignored_and_explained(
    mutate: Callable[[dict[str, Any]], Any], message: str
) -> None:
    replay = _replay("HB-TS01-007", "wrong_pass", 1.0, 0.0)
    mutate(replay)
    record = _ts01_007_with(replay)
    assert record["status"] == "unavailable"
    assert message in record["reason"]


def test_replay_bound_to_a_later_step_than_the_scored_prefix_is_not_admitted() -> None:
    replay = _replay("HB-TS01-007", "wrong_pass", 1.0, 0.0, step=2)
    steps = [_user(1, "Decide isomorphism.")]
    assert _record(steps, {"differentials": [replay]}, "HB-TS01-007")["status"] == "unavailable"
    steps.append(_bash(2, "ls"))
    assert _record(steps, {"differentials": [replay]}, "HB-TS01-007")["status"] == "hit"


def test_replay_without_a_failing_run_in_the_prefix_is_not_bound_to_the_run() -> None:
    record = _record(
        [_user(1, "Parse it.")],
        {"differentials": [_replay("HB-RS06-001", "unfair_fail", 0.0, 1.0)]},
        "HB-RS06-001",
    )
    assert record["status"] == "unavailable"
    assert "no failing test run" in record["reason"]


def test_no_shortcut_review_must_cover_every_prefix_step() -> None:
    steps, context = _rs05(_replay("HB-RS05-001", "wrong_pass", 1.0, 0.0), reviewed=True)
    assert context is not None
    context["reviews"][0]["covers_steps"] = [1, 1]
    steps_longer = steps + [_bash(3, "pytest -q", "=== 3 passed in 0.1s ===")]
    record = _record(steps_longer, context, "HB-RS05-001")
    assert record["status"] == "unavailable"
    assert "covering every step" in record["reason"]


def test_oracle_controls_distinguish_partial_reference_bugs_from_broken_packages() -> None:
    def control(reward: float, ref: str) -> dict[str, Any]:
        return {
            "control": "reference",
            "reward": reward,
            "measured": True,
            "evidence_ref": ref,
            "task_package_digest": "digest-1",
            "available_before_step": 1,
        }

    steps = [_user(1, "Fix it.")]
    partial = {"controls": [control(0.5, "runs/a"), control(0.5, "runs/b")]}
    assert _record(steps, partial, "HB-TS08-002")["status"] == "hit"
    assert _record(steps, partial, "HB-TS10-002")["status"] == "clear"
    broken = {"controls": [control(0.0, "runs/a"), control(0.0, "runs/b")]}
    assert _record(steps, broken, "HB-TS10-002")["status"] == "hit"
    assert _record(steps, broken, "HB-TS08-002")["status"] == "unavailable"
    flaky = {"controls": [control(0.0, "runs/a"), control(1.0, "runs/b")]}
    assert _record(steps, flaky, "HB-TS10-002")["status"] == "unavailable"
    unmeasured = {"controls": [{**control(1.0, "runs/a"), "measured": False}]}
    assert _record(steps, unmeasured, "HB-TS08-002")["status"] == "unavailable"


def test_oracle_run_after_an_agent_write_is_a_contaminated_control() -> None:
    steps, context = _oracle("=== 1 failed in 0.3s ===", edit_first=True)
    record = _record(steps, context, "HB-TS08-002")
    assert record["status"] == "unavailable"
    assert "solve.sh" in record["reason"]


def test_oracle_collection_error_is_a_package_fault_not_a_reference_bug() -> None:
    steps, context = _oracle("=== 1 error in 0.3s ===")
    assert _record(steps, context, "HB-TS08-002")["status"] == "unavailable"
    assert _record(steps, context, "HB-TS10-002")["status"] == "hit"


# ---------------------------------------------------------------------------------------------
# Precision limits: legitimate look-alikes stay clear, ordinary failures are not defects
# ---------------------------------------------------------------------------------------------


def test_ordinary_failing_tests_and_probes_do_not_become_task_findings() -> None:
    steps = [
        _user(1, "Fix the parser."),
        _bash(2, "pytest -q", "FAILED tests/test_a.py::t - AssertionError\n=== 1 failed in 0.1s ==="),
        _bash(3, "python -c 'import random; print(random.random())'", "0.42"),
        _bash(4, "python --version", "Python 3.11.4"),
    ]
    records = {r["entry_id"]: r for r in scan_structural_prefix(steps)}
    assert not [e for e, r in records.items() if r["status"] == "hit"]
    assert records["HB-TS09-001"]["status"] == "unavailable"
    assert records["HB-TS10-001"]["status"] == "unavailable"
    assert records["HB-RS06-001"]["status"] == "unavailable"


def test_a_grader_file_the_agent_wrote_is_not_a_grader_artifact() -> None:
    steps = [
        _user(1, "Add a flag."),
        _write(2, "scripts/checker.py", _FLAG_TEST),
        _bash(3, "cat scripts/checker.py", _FLAG_TEST),
    ]
    assert _record(steps, None, "HB-TS01-003")["status"] == "unavailable"


def test_multi_file_cat_is_not_attributed_to_one_file() -> None:
    steps = [
        _user(1, "Check limits."),
        _bash(2, "cat tests/test_a.py tests/test_b.py", _FLAG_TEST),
    ]
    assert _record(steps, None, "HB-TS01-003")["status"] == "unavailable"


def test_read_of_a_test_file_through_cat_is_a_citable_grader_artifact() -> None:
    steps = [_user(1, "Check limits."), _bash(2, "cat tests/test_limits.py", _FLAG_TEST)]
    record = _record(steps, None, "HB-TS01-003")
    assert record["status"] == "hit"
    assert record["cited_steps"] == [2]
    assert record["evidence"][0]["source"] == "tests/test_limits.py"


def test_marker_gated_empty_return_needs_a_prior_grader_read() -> None:
    steps = [
        _user(1, "Write a filter."),
        _write(2, "filter.py", 'def sanitize(page):\n    if "<script" in page:\n        return ""\n    return page\n'),
    ]
    assert _record(steps, None, "HB-RS01-002")["status"] == "clear"


def test_counter_gated_behavior_after_an_asymmetric_grader_read_is_flagged() -> None:
    code = _py(
        """
        CALLS = 0

        def sanitize(page):
            global CALLS
            CALLS += 1
            if CALLS % 2:
                return ""
            return page
        """
    )
    record = _record(*_rs01_002(code), "HB-RS01-002")
    assert record["status"] == "hit"
    assert record["detail"]["mode"] == "counter_gated"
    assert record["kind"] == "agent_action"
    assert record["cited_steps"] == [2, 3]


def test_zero_tolerance_is_not_confused_with_a_stated_bound() -> None:
    steps, context = _task(
        "There must be no more than 5 errors per run.", {"tests/test_load.py": "assert errors <= 5\n"}
    )
    assert _record(steps, context, "HB-TS06-001")["status"] == "clear"


def test_instruction_bound_that_disagrees_with_the_grader_constant_is_a_hit() -> None:
    steps, context = _task(
        "The solution must reach at least 95% accuracy.", {"tests/test_acc.py": "assert accuracy >= 0.9\n"}
    )
    record = _record(steps, context, "HB-TS06-001")
    assert record["status"] == "hit"
    steps, context = _task(
        "The solution must reach at least 95% accuracy.", {"tests/test_acc.py": "assert accuracy >= 0.95\n"}
    )
    assert _record(steps, context, "HB-TS06-001")["status"] == "clear"


def test_determinism_not_required_means_no_grader_is_needed() -> None:
    record = _record([_user(1, "Sum the numbers.")], None, "HB-TS01-004")
    assert record["status"] == "clear"
