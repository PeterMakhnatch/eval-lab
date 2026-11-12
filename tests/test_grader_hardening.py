"""Behavioural tests for ``grader-hardening@1``: run the generated grader on a toy repository."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from evallab.grader_hardening import (
    build_test_sh,
)
from evallab.task_variants import VariantInvalid

# The shape every upstream single-container MiMo package shares (path literals are rebased per test).
UPSTREAM_TEST_SH = """#!/bin/bash
M=/var/lib/mimo
CWD=/testbed
V=/logs/verifier
mkdir -p "$V"
BASE=$(cat "$M/base" 2>/dev/null) || { echo "setup never ran (no $M/base)" >&2; exit 1; }
cd "$CWD" || exit 1
if [ -d "$M/git-hidden" ]; then rm -rf "$CWD/.git"; mv "$M/git-hidden" "$CWD/.git"; fi
git add -A >/dev/null 2>&1 && git -c core.fileMode=false diff --cached "$BASE" > "$V/agent.diff"
git reset -q
if ! (
while IFS= read -r tf; do
  [ -z "$tf" ] && continue
  if git cat-file -e $BASE:"$tf" 2>/dev/null; then
    git checkout $BASE -- "$tf" 2>/dev/null || true
  else
    git rm -f --cached "$tf" >/dev/null 2>&1 || true
    rm -f "$tf"
  fi
done <<'EOF_RESET_TEST_FILES'
tests/test_calc.py
EOF_RESET_TEST_FILES

); then echo "could not reset the test files (testbed problem, not scored)" >&2; exit 1; fi
if ! git apply --verbose /tests/test.patch > "$V/apply.log" 2>&1; then
  cat "$V/apply.log" >&2
  echo "the hidden tests could not be applied (testbed problem, not scored)" >&2
  exit 1
fi
timeout 1800 sh -c "$(cat /tests/test_command.sh)" > "$V/test_output.log" 2>&1
RC=$?
tail -c 8000 "$V/test_output.log"
if [ $RC -eq 0 ]; then echo 1 > "$V/reward.txt"; else echo 0 > "$V/reward.txt"; fi
echo "test command exited $RC"
"""

HIDDEN_TESTS = (
    "from calc import add, mean\n\n\n"
    "def test_add():\n    assert add(2, 3) == 5\n\n\n"
    "def test_mean():\n    assert mean([2, 4]) == 3\n\n\n"
    "def test_hidden_fixture(hidden_value):\n    assert hidden_value == 7\n"
)
HIDDEN_CONFTEST = "import pytest\n\n\n@pytest.fixture\ndef hidden_value():\n    return 7\n"
FIXED = "def add(a, b):\n    return a + b\n\n\ndef mean(xs):\n    return sum(xs) / len(xs)\n"
BUGGY = "def add(a, b):\n    return a - b\n\n\ndef mean(xs):\n    return 0\n"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _add_file_patch(path: str, body: str) -> str:
    lines = body.splitlines()
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines)
    )


class Rig:
    """A toy testbed rebased into tmp_path, plus a way to grade it with a given test.sh."""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.testbed = tmp_path / "testbed"
        self.state = tmp_path / "mimo"
        self.verifier = tmp_path / "logs"
        self.tests = tmp_path / "tests-dir"
        self.testbed.mkdir()
        _git(self.testbed, "init", "-q")
        (self.testbed / "calc.py").write_text(BUGGY)
        (self.testbed / "pyproject.toml").write_text('[project]\nname = "calc"\n')
        (self.testbed / ".gitignore").write_text("node_modules/\n.venv/\nbuild/\n")
        _git(self.testbed, "add", "-A")
        _git(self.testbed, "commit", "-q", "-m", "base")
        self.state.mkdir()
        (self.state / "base").write_text(_git(self.testbed, "rev-parse", "HEAD"))
        self.tests.mkdir()
        (self.tests / "test.patch").write_text(
            _add_file_patch("tests/test_calc.py", HIDDEN_TESTS)
            + _add_file_patch("tests/conftest.py", HIDDEN_CONFTEST)
        )
        (self.tests / "test_command.sh").write_text(
            "python3 -m pytest tests/test_calc.py -q -p no:cacheprovider"
        )
        (self.tests / "expected_report.json").write_text(json.dumps({"tests": 3, "skipped": 0}))
        shim = tmp_path / "bin"
        shim.mkdir()
        (shim / "timeout").write_text('#!/bin/sh\nshift\nexec "$@"\n')
        (shim / "timeout").chmod(0o755)
        self.path = f"{shim}:{Path(sys.executable).parent}:{os.environ['PATH']}"

    def script(self, text: str) -> Path:
        rebased = (
            text.replace("M=/var/lib/mimo", f"M={self.state}")
            .replace("CWD=/testbed", f"CWD={self.testbed}")
            .replace("V=/logs/verifier", f"V={self.verifier}")
            .replace("/tests/", f"{self.tests}/")
            .replace(
                "EXPECTED_JSON=/tests/expected_report.json",
                f"EXPECTED_JSON={self.tests}/expected_report.json",
            )
        )
        path = self.root / "test.sh"
        path.write_text(rebased)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def grade(self, text: str) -> str | None:
        subprocess.run(
            ["bash", str(self.script(text))],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "PATH": self.path},
        )
        reward = self.verifier / "reward.txt"
        return reward.read_text().strip() if reward.exists() else None

    def reset_worktree(self) -> None:
        _git(self.testbed, "checkout", "-q", "-f", "HEAD")
        _git(self.testbed, "clean", "-fdxq")
        subprocess.run(["rm", "-rf", str(self.verifier)], check=True)

    def write(self, rel: str, body: str) -> None:
        target = self.testbed / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


@pytest.fixture
def hardened() -> str:
    return build_test_sh(UPSTREAM_TEST_SH, with_report_gate=True)


CONFTEST_FORCE_PASS = "def pytest_sessionfinish(session, exitstatus):\n    session.exitstatus = 0\n"
CONFTEST_SKIP_ALL = (
    "import pytest\n\n\ndef pytest_collection_modifyitems(config, items):\n"
    "    for item in items:\n        item.add_marker(pytest.mark.skip(reason='x'))\n"
)
EXIT_AT_INTERPRETER_END = "import atexit\nimport os\n\natexit.register(lambda: os._exit(0))\n"


def test_correct_solution_passes_and_unfixed_base_fails_under_the_hardened_grader(rig, hardened):
    rig.write("calc.py", FIXED)
    assert rig.grade(hardened) == "1"
    rig.reset_worktree()
    assert rig.grade(hardened) == "0"


def test_conftest_sessionfinish_forgery_passes_upstream_but_not_hardened(rig, hardened):
    rig.write("conftest.py", CONFTEST_FORCE_PASS)
    assert rig.grade(UPSTREAM_TEST_SH) == "1"  # the weakness is real on the unhardened grader
    rig.reset_worktree()
    rig.write("conftest.py", CONFTEST_FORCE_PASS)
    assert rig.grade(hardened) == "0"


def test_planted_config_and_plugin_attacks_are_neutralised_in_nested_paths(rig, hardened):
    rig.write(
        "tests/conftest.py", CONFTEST_SKIP_ALL
    )  # overrides nothing the list names; the hidden patch re-adds its own
    rig.write("pytest.ini", "[pytest]\naddopts = -p skipall\n")
    rig.write("skipall.py", CONFTEST_SKIP_ALL)
    assert rig.grade(hardened) == "0"
    assert "pytest.ini" in (rig.verifier / "l1-removed.txt").read_text()


def test_exit_forgery_by_the_code_under_test_fails_closed_on_the_report(rig, hardened):
    rig.write("calc.py", BUGGY + EXIT_AT_INTERPRETER_END)
    assert rig.grade(UPSTREAM_TEST_SH) == "1"
    rig.reset_worktree()
    rig.write("calc.py", BUGGY + EXIT_AT_INTERPRETER_END)
    assert rig.grade(hardened) == "0"


def test_hidden_tests_own_conftest_still_applies_and_dependency_dirs_are_untouched(rig, hardened):
    rig.write("calc.py", FIXED)
    rig.write("node_modules/dep/conftest.py", "# belongs to a dependency\n")
    rig.write(".venv/lib/site-packages/pkg/pyproject.toml", "# dependency metadata\n")
    assert (
        rig.grade(hardened) == "1"
    )  # the hidden fixture from test.patch's conftest.py was available
    assert (rig.testbed / "node_modules/dep/conftest.py").exists()
    assert (rig.testbed / ".venv/lib/site-packages/pkg/pyproject.toml").exists()


def test_a_gate_count_above_what_ran_rejects_even_a_green_run(rig, hardened):
    rig.write("calc.py", FIXED)
    (rig.tests / "expected_report.json").write_text(json.dumps({"tests": 4, "skipped": 0}))
    assert rig.grade(hardened) == "0"


def test_build_test_sh_refuses_a_second_application_and_a_foreign_shape():
    once = build_test_sh(UPSTREAM_TEST_SH, with_report_gate=False)
    with pytest.raises(VariantInvalid, match="already grader-hardened"):
        build_test_sh(once, with_report_gate=True)
    with pytest.raises(VariantInvalid, match="exactly once"):
        build_test_sh("#!/bin/bash\necho 1 > reward.txt\n", with_report_gate=True)


def test_pyproject_edits_survive_unless_they_touch_the_test_runner(rig, hardened):
    rig.write("calc.py", FIXED)
    rig.write("pyproject.toml", '[project]\nname = "calc"\ndependencies = ["requests"]\n')
    assert rig.grade(hardened) == "1"
    assert (
        "requests" in (rig.testbed / "pyproject.toml").read_text()
    )  # a legitimate dependency edit is kept

    rig.reset_worktree()
    rig.write("skipall.py", CONFTEST_SKIP_ALL)
    rig.write(
        "pyproject.toml",
        '[project]\nname = "calc"\n[tool.pytest.ini_options]\naddopts = "-p skipall"\n',
    )
    assert rig.grade(UPSTREAM_TEST_SH) == "1"  # the attack works without the reset
    rig.reset_worktree()
    rig.write("skipall.py", CONFTEST_SKIP_ALL)
    rig.write(
        "pyproject.toml",
        '[project]\nname = "calc"\n[tool.pytest.ini_options]\naddopts = "-p skipall"\n',
    )
    assert rig.grade(hardened) == "0"
    assert "addopts" not in (rig.testbed / "pyproject.toml").read_text()
