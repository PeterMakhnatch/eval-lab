"""Fail if a committed unit-test module is omitted from default collection."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DECLARED_UNIT_TEST_DIRS = (
    "tests",
    "dashboard/tests",
    "research/analysis/tests",
    "research/calibration/tests",
)

def committed_unit_test_modules(root: Path) -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "--", *DECLARED_UNIT_TEST_DIRS],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return [
        root / relative
        for relative in completed.stdout.splitlines()
        if Path(relative).name.startswith("test_") and Path(relative).suffix == ".py"
    ]


def modules_missing_from_collection(
    collected_nodeids: list[str], required_modules: list[Path], *, root: Path
) -> list[str]:
    collected_modules = {nodeid.partition("::")[0] for nodeid in collected_nodeids}
    missing: list[str] = []
    for path in required_modules:
        posix = path.relative_to(root).as_posix()
        if posix not in collected_modules:
            missing.append(posix)
    return missing


def _collect_default_nodeids() -> list[str]:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-o", "addopts="],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode not in {0}:
        raise AssertionError(
            f"pytest --collect-only failed:\n{completed.stdout}\n{completed.stderr}"
        )
    return [line for line in completed.stdout.splitlines() if "::" in line]


def test_collection_contract_detects_an_omitted_module() -> None:
    required = [
        ROOT / "tests" / "test_ci_coverage.py",
        ROOT / "tests" / "test_does_not_exist.py",
    ]
    missing = modules_missing_from_collection(
        ["tests/test_ci_coverage.py::test_collection_contract_detects_an_omitted_module"],
        required,
        root=ROOT,
    )
    assert missing == ["tests/test_does_not_exist.py"]


def test_declared_unit_test_modules_are_collected() -> None:
    required = committed_unit_test_modules(ROOT)
    assert required, "declared unit-test directories contained no test_*.py files"
    missing = modules_missing_from_collection(
        _collect_default_nodeids(), required, root=ROOT
    )
    assert missing == [], f"default pytest omitted {missing}"
