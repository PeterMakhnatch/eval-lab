"""Native verifier patch, command, and variant contracts."""

from __future__ import annotations

import difflib
import os
import shlex
import subprocess
import sys

import pytest

from evallab.registry import compute_task_digests
from evallab.task_variants import derive_task, materialize
from evallab.verifier_repair import (
    RepairTest,
    build_command_wrapper,
    build_patch_appendix,
)


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


def _overconstrained_fixture(tmp_path, repaired_test):
    package = tmp_path / "overconstrained"
    (package / "tests").mkdir(parents=True)
    (package / "solution").mkdir()
    (package / "task.toml").write_text(
        '[task]\nname="demo/overconstrained"\n[environment]\nworkdir="/testbed"\n'
    )
    (package / "instruction.md").write_text(
        "Return sorted unique integers as a list or tuple, without changing the input.\n"
    )
    (package / "solution/solve.sh").write_text("#!/bin/sh\ntrue\n")
    (package / "tests/test.sh").write_text("git apply /tests/test.patch\n")
    imports = "from answer import normalize\n\n"
    unrelated = (
        "\ndef test_unrelated_input_is_not_changed():\n"
        "    values = [3, 1]\n"
        "    normalize(values)\n"
        "    assert values == [3, 1]\n"
    )
    base = imports + unrelated
    before = (
        "def test_unique_sorted():\n"
        "    assert normalize([3, 1, 3, 2]) == [1, 2, 3]\n"
    )
    patches = []
    for test in (before, repaired_test):
        patches.append(
            "diff --git a/tests/test_contract.py b/tests/test_contract.py\n"
            + "".join(difflib.unified_diff(
                base.splitlines(keepends=True),
                (imports + test + unrelated).splitlines(keepends=True),
                fromfile="a/tests/test_contract.py",
                tofile="b/tests/test_contract.py",
            ))
        )
    (package / "tests/test.patch").write_text(patches[0])
    (package / "tests/test_command.sh").write_text(
        f"{shlex.quote(sys.executable)} -m pytest tests/test_contract.py -q\n"
        "CHECK_RC=$?\nprintf 'full command ran\\n'\nexit \"$CHECK_RC\"\n"
    )
    return package, base, patches[1]


def test_replacement_accepts_prompt_valid_alternate_but_rejects_wrong_behavior(tmp_path):
    package, base, patch = _overconstrained_fixture(
        tmp_path,
        "def test_unique_sorted():\n"
        "    assert list(normalize([3, 1, 3, 2])) == [1, 2, 3]\n",
    )
    parent_digest = compute_task_digests(package)
    alternate = "def normalize(values):\n    return tuple(sorted(set(values)))\n"
    original = _grade_contract_fixture(tmp_path / "original", package, base, alternate)
    assert original.returncode != 0, original.stdout + original.stderr
    record = derive_task(
        package,
        changes={"tests/test.patch": patch.encode()},
        transform="test-contract@1",
        rationale="Accept both prompt-valid return types.",
        created_by="test",
        repo_root=tmp_path,
        variants_root=tmp_path / "variants",
    )
    variant = materialize(
        record, package, repo_root=tmp_path, variants_root=tmp_path / "rebuilt"
    )
    for name, implementation, expected in (
        ("reference", "def normalize(values):\n    return sorted(set(values))\n", 0),
        ("alternate", alternate, 0),
        ("wrong", "def normalize(values):\n    return tuple(sorted(values))\n", 1),
        (
            "break-unrelated",
            "def normalize(values):\n"
            "    result = sorted(set(values))\n"
            "    values.clear()\n"
            "    return tuple(result)\n",
            1,
        ),
    ):
        result = _grade_contract_fixture(tmp_path / name, variant, base, implementation)
        assert result.returncode == expected, result.stdout + result.stderr
        assert "full command ran" in result.stdout
    assert compute_task_digests(package) == parent_digest
    assert (variant / "tests/test_command.sh").read_bytes() == (
        package / "tests/test_command.sh"
    ).read_bytes()


def test_edits_and_additions_both_run_with_original_command_and_failure_semantics(tmp_path):
    package, base, patch = _overconstrained_fixture(
        tmp_path,
        "def test_unique_sorted():\n"
        "    values = [3, 1, 3, 2]\n"
        "    try:\n"
        "        assert list(normalize(values)) == [1, 2, 3]\n"
        "    finally:\n"
        "        values.clear()\n",
    )
    root = tmp_path / "mixed-grade"
    addition = RepairTest(
        id="negatives",
        repo_path="tests/test_negative.py",
        code=(
            "from answer import normalize\n\n"
            "def test_negative_values():\n"
            "    assert list(normalize([-3, -1, -3])) == [-3, -1]\n"
        ),
    )
    original_command = (package / "tests/test_command.sh").read_text()
    wrapper = build_command_wrapper(
        original_command,
        f"{shlex.quote(sys.executable)} -m pytest tests/test_negative.py -q",
        workdir=str(root),
        interpreter=sys.executable,
    )
    record = derive_task(
        package,
        changes={
            "tests/test.patch": (patch + build_patch_appendix([addition])).encode(),
            "tests/test_command.sh": wrapper.encode(),
        },
        transform="test-contract@1",
        rationale="Run the original assertions and the additional assertions.",
        created_by="test",
        repo_root=tmp_path,
        variants_root=tmp_path / "variants",
    )
    variant = materialize(
        record, package, repo_root=tmp_path, variants_root=tmp_path / "rebuilt"
    )
    good = _grade_contract_fixture(
        root, variant, base, "def normalize(values):\n    return tuple(sorted(set(values)))\n"
    )
    assert good.returncode == 0, good.stdout + good.stderr
    (root / "answer.py").write_text(
        "def normalize(values):\n    return tuple(sorted({abs(value) for value in values}))\n"
    )
    wrong = subprocess.run(
        ["sh", "-c", (variant / "tests/test_command.sh").read_text()],
        cwd=root,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
        check=False,
    )
    assert wrong.returncode != 0, wrong.stdout + wrong.stderr
    assert "full command ran" in wrong.stdout
