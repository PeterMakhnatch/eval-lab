"""Consumer-visible repair boundaries; all model, quota and runtime I/O is injected."""

from __future__ import annotations

import os
import subprocess

import pytest

from evallab.verifier_repair import (
    build_command_wrapper,
)

MUTANT = "abc123def4"


@pytest.mark.parametrize("interpreter", [None, "/opt/test_venv/bin/python3"])
@pytest.mark.parametrize(
    ("original", "new_code", "expected"),
    [("exit 0", 0, 0), ("exit 9", 0, 9), ("exit 0", 7, 1), ("set -e; false", 0, 1)],
)
def test_wrapper_runs_additions_despite_original_exit_and_preserves_failures(
    tmp_path, original, new_code, expected, interpreter
):
    wrapper = build_command_wrapper(
        original, f"sh -c 'touch ran; exit {new_code}'", workdir=str(tmp_path), interpreter=interpreter,
    )
    result = subprocess.run(["sh", "-c", wrapper], cwd=tmp_path, check=False)
    assert result.returncode == expected
    assert (tmp_path / "ran").exists()


def test_wrapper_runs_bare_python_with_the_task_interpreter(tmp_path):
    bindir = tmp_path / "venv" / "bin"
    bindir.mkdir(parents=True)
    interpreter = bindir / "python3"
    interpreter.write_text("#!/bin/sh\nprintf '%s\\n' task-venv > \"$1\"\n", encoding="utf-8")
    interpreter.chmod(0o755)
    marker = tmp_path / "which"
    wrapper = build_command_wrapper(
        "exit 0",
        f"python {marker}",
        workdir=str(tmp_path),
        interpreter=str(interpreter),
    )
    result = subprocess.run(["sh", "-c", wrapper], cwd=tmp_path, check=False)
    assert result.returncode == 0
    assert marker.read_text(encoding="utf-8") == "task-venv\n"


def _grade_contract_fixture(root, package, base, implementation):
    (root / "tests").mkdir(parents=True)
    (root / "tests/test_contract.py").write_text(base)
    (root / "answer.py").write_text(implementation)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "apply", str(package / "tests/test.patch")],
        cwd=root, check=True, capture_output=True, text=True,
    )
    return subprocess.run(
        ["sh", "-c", (package / "tests/test_command.sh").read_text()],
        cwd=root,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
        check=False,
    )


