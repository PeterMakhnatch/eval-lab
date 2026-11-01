"""full-hardening@1 (``evallab harden``): package transforms and the grade-time payload, without containers."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest

from evallab import full_hardening_grade as grade
from evallab.full_hardening import HardeningRefused, harden, plan_hardening

GRADE = Path(grade.__file__)

MIMO_TEST_SH = """\
#!/bin/bash
M=/var/lib/mimo
CWD=/workspace/repo
V=/logs/verifier
mkdir -p "$V"
BASE=$(cat "$M/base" 2>/dev/null) || { echo "setup never ran (no $M/base)" >&2; exit 1; }
cd "$CWD" || exit 1
if ! (
while IFS= read -r tf; do
  git checkout $BASE -- "$tf" 2>/dev/null || true
done <<'EOF_RESET_TEST_FILES'
tests/test_x.py
mimo_test_command.sh
EOF_RESET_TEST_FILES

); then echo "could not reset the test files (testbed problem, not scored)" >&2; exit 1; fi
if ! git apply --verbose /tests/test.patch > "$V/apply.log" 2>&1; then exit 1; fi
timeout 1800 sh -c "$(cat /tests/test_command.sh)" > "$V/test_output.log" 2>&1
RC=$?
tail -c 12000 "$V/test_output.log"
if [ $RC -eq 0 ]; then echo 1 > "$V/reward.txt"; else echo 0 > "$V/reward.txt"; fi
echo "test command exited $RC"
"""

MIMO_PATCH = """\
diff --git a/tests/test_x.py b/tests/test_x.py
new file mode 100644
--- /dev/null
+++ b/tests/test_x.py
@@ -0,0 +1,2 @@
+def test_x():
+    assert True
diff --git a/mimo_test_command.sh b/mimo_test_command.sh
new file mode 100755
--- /dev/null
+++ b/mimo_test_command.sh
@@ -0,0 +1,3 @@
+export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
+unset PYTEST_ADDOPTS
+exec python -m pytest -c /dev/null /workspace/repo/tests/test_x.py -v
"""


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")
    return root


def _general_package(root: Path) -> Path:
    """C1 + C2 + C3 + C5 in one package: graded workspace tests, a trusted helper, an image leak, a submission check."""
    return _write(
        root,
        {
            "task.toml": 'schema_version = "1.4"\n\n[verifier]\ntimeout_sec = 60.0\n\n[environment]\nworkdir = "/app"\n',
            "instruction.md": "Fix the tool.\nThe existing visible tests in `tests/` show the calling conventions.\n",
            "environment/Dockerfile": (
                "FROM python:3.12-slim\nWORKDIR /app\nCOPY app/ /app/\nCOPY leaked/ /opt/task-harness/\n"
            ),
            "environment/app/tests/test_render.py": "def test_render():\n    assert 1 == 2\n",
            "environment/app/tests/conftest.py": "BASELINE = True\n",
            "environment/app/tools/check.py": "print('FAIL')\n",
            "environment/app/pkg/solve.py": "ANSWER = None\n",
            "environment/leaked/test_accept.py": "EXPECTED = [3, 1, 2]\n",
            "tests/test_accept.py": "import subprocess, sys\nsubprocess.run([sys.executable, 'tools/check.py'])\n",
            "tests/test.sh": (
                "#!/bin/bash\nV=/logs/verifier\n[ -f /app/pkg/solve.py ] || exit 0\ncd /app\n"
                'python3 -m pytest -q tests /tests/test_accept.py > "$V/out.log" 2>&1\nRC=$?\n'
                'if [ $RC -eq 0 ]; then echo 1 > "$V/reward.txt"; else echo 0 > "$V/reward.txt"; fi\n'
            ),
        },
    )


def _mimo_package(root: Path) -> Path:
    return _write(
        root,
        {
            "task.toml": (
                'schema_version = "1.4"\n\n[task]\nname = "mimo/x"\n\n[verifier]\ntimeout_sec = 60.0\n\n'
                '[environment]\nworkdir = "/workspace/repo"\ndocker_image = "example/mimo@sha256:00"\n'
            ),
            "instruction.md": "Fix it.\n",
            "environment/Dockerfile": "FROM example/mimo@sha256:00\n",
            "environment/setup/setup.sh": "true\n",
            "tests/test.sh": MIMO_TEST_SH,
            "tests/test_command.sh": "bash /workspace/repo/mimo_test_command.sh\n",
            "tests/test.patch": MIMO_PATCH,
        },
    )


def test_general_package_closes_test_edit_helper_mutation_and_image_leak(tmp_path):
    package = _general_package(tmp_path / "pkg")
    manifest = harden(package, tmp_path / "out", expected_tests=2)
    out = tmp_path / "out"
    plan = json.loads((out / "tests/_evallab/plan.json").read_text())

    # L2: the graded workspace tests and the helper the grader executes are restored; the submission is not
    assert set(plan["pristine"]) == {
        "/app/tests/test_render.py",
        "/app/tests/conftest.py",
        "/app/tools/check.py",
    }
    assert (out / "tests/_evallab/pristine/app/tools/check.py").read_text() == "print('FAIL')\n"
    # L1 baseline: an auto-load file shipped by the package is restored by sha256, not deleted
    assert set(plan["baseline"]) == {"/app/tests/conftest.py"}
    # L3: the original grader asks pytest for a report directly on its command line
    assert '--junitxml="$EVALLAB_REPORT"' in (out / "tests/_evallab/orig_test.sh").read_text()
    assert plan["expected"] == {"tests": 2, "skipped": 0}
    # L4: the leaked acceptance file is gone from the image and the build context, grading is separate
    dockerfile = (out / "environment/Dockerfile").read_text()
    assert "COPY leaked/" not in dockerfile.replace("removed `COPY leaked/", "")
    assert not (out / "environment/leaked").exists()
    config = tomllib.loads((out / "task.toml").read_text())
    assert config["verifier"]["environment_mode"] == "separate"
    assert config["artifacts"] == ["/app"]
    kinds = {(f.kind, f.path) for f in manifest.findings}
    assert ("image-leak", "/opt/task-harness/test_accept.py") in kinds
    assert ("instruction-funnel", "instruction.md:2") in kinds
    assert (
        package / "environment/leaked/test_accept.py"
    ).exists()  # the source package is never changed


def test_report_gate_needs_a_reference_count(tmp_path):
    package = _general_package(tmp_path / "pkg")
    with pytest.raises(HardeningRefused, match="reference test count"):
        plan_hardening(package)
    manifest, _ = plan_hardening(package, calibrate=True)
    assert manifest.expected_report is None and manifest.calibration


def test_mimo_single_container_injects_past_unset_addopts(tmp_path):
    package = _mimo_package(tmp_path / "pkg")
    manifest = harden(package, tmp_path / "out", single_container=True, expected_tests=1)
    test_sh = (tmp_path / "out/tests/test.sh").read_text()
    assert manifest.runner == "pytest"
    assert (
        "full-hardening@1 L1 (grader-hardening@1 block)" in test_sh and "vitest.config.*" in test_sh
    )
    run = test_sh.index('timeout 1800 sh -c "$(cat /tests/test_command.sh)"')
    assert (
        test_sh.index(
            "grade.py inject --plan /tests/_evallab/plan.json --file /workspace/repo/mimo_test_command.sh"
        )
        < run
    )
    assert test_sh.index("grade.py finish") > test_sh.index('echo 1 > "$V/reward.txt"')
    assert {layer.layer: layer.status for layer in manifest.layers}["L4"] == "open"


def test_mimo_default_output_is_the_separate_verifier(tmp_path):
    manifest, changes = plan_hardening(_mimo_package(tmp_path / "pkg"))
    assert manifest.separate_verifier
    assert (
        "tests/Dockerfile" in changes and b'environment_mode = "separate"' in changes["task.toml"]
    )


@pytest.mark.parametrize(
    ("runner", "line", "expected"),
    [
        (
            "pytest",
            "unset PYTEST_ADDOPTS\nexec python -m pytest -c /dev/null t.py -v\n",
            'unset PYTEST_ADDOPTS\nexec python -m pytest --junitxml="$EVALLAB_REPORT" -c /dev/null t.py -v\n',
        ),
        (
            "vitest",
            "if [ ! -x ./node_modules/.bin/vitest ]; then exit 127; fi\nexec ./node_modules/.bin/vitest run a.ts\n",
            "if [ ! -x ./node_modules/.bin/vitest ]; then exit 127; fi\n"
            'exec ./node_modules/.bin/vitest run --reporter=json --outputFile="$EVALLAB_REPORT" a.ts\n',
        ),
        (
            "unittest",
            "python -m unittest tests.test_cli -v\n",
            'python /tests/_evallab/grade.py unittest-run --report "$EVALLAB_REPORT" -- tests.test_cli -v\n',
        ),
        (
            "pytest",
            "pip install pytest\n# python -m pytest\n",
            "pip install pytest\n# python -m pytest\n",
        ),
    ],
)
def test_inject_report(runner, line, expected):
    assert grade.inject_report(line, runner)[0] == expected


def _junit(cases: list[str]) -> str:
    return "<testsuites><testsuite>" + "".join(cases) + "</testsuite></testsuites>"


@pytest.mark.parametrize(
    ("report", "ok"),
    [
        (_junit(['<testcase name="a"/>', '<testcase name="b"/>']), True),
        (_junit(['<testcase name="a"/>']), False),  # fewer tests than the reference
        (_junit(['<testcase name="a"/>', '<testcase name="b"><skipped/></testcase>']), False),
        (_junit(['<testcase name="a"/>', '<testcase name="b"><failure/></testcase>']), False),
        ("not xml", False),
    ],
)
def test_pytest_gate(tmp_path, report, ok):
    path = tmp_path / "r.xml"
    path.write_text(report)
    assert grade.gate("pytest", str(path), {"tests": 2, "skipped": 0})["ok"] is ok


def test_gate_fails_closed_without_a_report_and_reads_vitest_json(tmp_path):
    assert (
        grade.gate("vitest", str(tmp_path / "missing.json"), {"tests": 1, "skipped": 0})["ok"]
        is False
    )
    path = tmp_path / "v.json"
    path.write_text(
        json.dumps({"numTotalTests": 3, "numFailedTests": 0, "numPendingTests": 0, "success": True})
    )
    assert grade.gate("vitest", str(path), {"tests": 3, "skipped": 0})["ok"] is True
    path.write_text(json.dumps({"numTotalTests": 0, "numFailedTests": 0, "success": True}))
    assert grade.gate("vitest", str(path), {"tests": 3, "skipped": 0})["ok"] is False


def _container(root: Path, files: dict[str, str]) -> None:
    _write(root, files)


def test_prepare_purges_agent_hooks_and_restores_baseline_and_pristine(tmp_path):
    package = _general_package(tmp_path / "pkg")
    harden(package, tmp_path / "out", expected_tests=2)
    root = tmp_path / "root"
    (root / "tests").mkdir(parents=True)
    bundle = root / "tests/_evallab"
    import shutil

    shutil.copytree(tmp_path / "out/tests/_evallab", bundle)
    _container(
        root,
        {
            "app/tests/test_render.py": "def test_render():\n    assert 1 == 1\n",  # C1: edited graded test
            "app/tests/conftest.py": "import os; os._exit(0)\n",  # edited baseline auto-load file
            "app/tools/check.py": "print('PASS')\n",  # C3: mutated grader helper
            "app/conftest.py": "def pytest_sessionfinish(session, exitstatus): session.exitstatus = 0\n",
            "app/pkg/sitecustomize.py": "import os\n",
            "app/node_modules/x/conftest.py": "kept\n",
            "app/pkg/solve.py": "ANSWER = 42\n",
        },
    )
    log = tmp_path / "log.json"
    assert (
        grade.main(
            ["--root", str(root), "prepare", "--plan", str(bundle / "plan.json"), "--log", str(log)]
        )
        == 0
    )
    record = json.loads(log.read_text())
    assert sorted(record["removed"]) == ["/app/conftest.py", "/app/pkg/sitecustomize.py"]
    assert (root / "app/tests/conftest.py").read_text() == "BASELINE = True\n"
    assert (
        root / "app/tests/test_render.py"
    ).read_text() == "def test_render():\n    assert 1 == 2\n"
    assert (root / "app/tools/check.py").read_text() == "print('FAIL')\n"
    assert (root / "app/pkg/solve.py").read_text() == "ANSWER = 42\n"  # the submission survives
    assert (root / "app/node_modules/x/conftest.py").exists()  # dependency trees are not walked


@pytest.mark.parametrize(
    ("original", "report_ok", "final"), [("1", True, "1"), ("1", False, "0"), ("0", True, "0")]
)
def test_finish_needs_both_the_original_pass_and_the_report(tmp_path, original, report_ok, final):
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps({"runner": "pytest", "l3": True, "expected": {"tests": 1, "skipped": 0}})
    )
    verifier = tmp_path / "logs/verifier"
    verifier.mkdir(parents=True)
    (verifier / "reward.txt").write_text(original + "\n")
    report = verifier / "r.xml"
    report.write_text(_junit(['<testcase name="a"/>']) if report_ok else "")
    grade.main(
        ["--root", str(tmp_path), "finish", "--plan", str(plan), "--report", "/logs/verifier/r.xml"]
    )
    assert (verifier / "reward.txt").read_text().strip() == final


def test_unittest_runner_reports_counts_and_exit_status(tmp_path):
    _write(
        tmp_path,
        {
            "suite/__init__.py": "",
            "suite/test_a.py": (
                "import unittest\n\nclass T(unittest.TestCase):\n"
                "    def test_ok(self):\n        pass\n\n"
                "    def test_bad(self):\n        self.fail()\n\n"
                "    @unittest.skip('x')\n    def test_skip(self):\n        pass\n"
            ),
        },
    )
    report = tmp_path / "report.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(GRADE),
            "unittest-run",
            "--report",
            str(report),
            "--",
            "suite.test_a",
            "-v",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert json.loads(report.read_text()) == {"tests": 3, "failures": 1, "errors": 0, "skipped": 1}


def _repaired_package(tmp_path: Path) -> Path:
    """A MiMo package whose test.sh appends a behavioral phase after the base run."""
    pkg = tmp_path / "pkg"
    (pkg / "tests").mkdir(parents=True)
    (pkg / "environment").mkdir()
    (pkg / "task.toml").write_text(
        '[environment]\nworkdir = "/testbed"\ndocker_image = "python:3.9"\n'
        '[verifier]\nenvironment_mode = "shared"\n'
    )
    (pkg / "environment" / "Dockerfile").write_text("FROM python:3.9\n")
    (pkg / "tests" / "test.sh").write_text(
        "#!/bin/bash\n"
        "V=/logs/verifier\n"
        "if ! (\n"
        "  while IFS= read -r tf; do :; done <<'EOF_RESET_TEST_FILES'\n"
        "tests/test_outputs.py\n"
        "EOF_RESET_TEST_FILES\n"
        '); then echo "could not reset the test files (testbed problem, not scored)" >&2; exit 1; fi\n' 
        'timeout 1800 sh -c "$(cat /tests/test_command.sh)" > "$V/test_output.log" 2>&1\n'
        "RC=$?\n"
        'if [ "$RC" -eq 0 ]; then\n'
        '  ( cd "$CWD" && cd /testbed && PYTHONPATH=/testbed python3 /tests/test_outputs.py ) >> "$V/test_output.log" 2>&1\n'
        "  RC=$?\n"
        "fi\n"
        'if [ $RC -eq 0 ]; then echo 1 > "$V/reward.txt"; else echo 0 > "$V/reward.txt"; fi\n'
    )
    (pkg / "tests" / "test_command.sh").write_text("python -m pytest -q\n")
    (pkg / "tests" / "test.patch").write_text("diff --git a/mimo_test_command.sh b/mimo_test_command.sh\n")
    return pkg


def test_supplementary_blocks_are_extracted_and_rewritten(tmp_path: Path) -> None:
    from evallab.separate_verifier import supplementary_test_blocks

    blocks = supplementary_test_blocks(_repaired_package(tmp_path), workdir="/work")
    assert len(blocks) == 1
    assert "/tests/test_outputs.py" in blocks[0]
    assert "/testbed" not in blocks[0]  # rewritten to the verifier workdir


def test_v10_wrapper_runs_supplementary_phases_and_fails_with_them() -> None:
    from evallab.separate_verifier import _V10_RUN_LINE, render_wrapper_test_sh_v10

    block = '( cd "/testbed" && PYTHONPATH=/testbed python3 /tests/test_outputs.py )'
    text = render_wrapper_test_sh_v10("/testbed", extra_blocks=(block,))
    assert block in text
    # the phase must run after the base RC is captured, and its own status must replace RC
    after = text.split(_V10_RUN_LINE, 1)[1]
    assert after.index("RC=$?") < after.index(block)
    assert after.index(block) < after.index("RC=$?", after.index(block))
    assert 'if [ "$RC" -eq 0 ]; then\n  ' + block in text

    plain = render_wrapper_test_sh_v10("/testbed")
    assert block not in plain
