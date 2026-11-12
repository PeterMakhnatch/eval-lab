"""Host contract for the three syn-funcdag hidden verifiers, without Docker.

Each package ships a stdlib-only verifier (tests/verify.py) and a reference
solver (solution/solve.py). The synthetic generator stack that produced them is
retired, so this file is their only behavioral test outside a Harbor job. It
fails when a verifier edit starts accepting an answer-only or wrong-trace
submission that the shipped contract rejects.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
TASK_ROOT = ROOT / "library" / "tasks" / "experimental"
PACKAGES = ("syn-funcdag-easy", "syn-funcdag-medium", "syn-funcdag-hard")
# Modules of the retired synthetic generator stack. The host contract must not
# resurrect them as an accidental dependency.
RETIRED_GENERATOR_PREFIX = "evallab.synthetic"


def package_dir(package: str) -> Path:
    return TASK_ROOT / package


def package_digest(package: str) -> str:
    """Digest over every committed file, so a grader run cannot rewrite a package."""
    root = package_dir(package)
    digest = hashlib.sha256()
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def load_shipped_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with pytest.MonkeyPatch.context() as patch:
        # Keep bytecode out of the byte-pinned task package.
        patch.setattr(sys, "dont_write_bytecode", True)
        spec.loader.exec_module(module)
    return module


def solve(package: str, tmp_path: Path) -> tuple[dict[str, Any], Path]:
    """Run the package's solution/solve.py with its /app paths redirected into tmp_path."""
    module = load_shipped_module(
        f"funcdag_{package.removeprefix('syn-funcdag-')}_solve",
        package_dir(package) / "solution" / "solve.py",
    )
    real_path = Path

    def host_path(value: str | Path) -> Path:
        text = str(value)
        if text == "/app" or text.startswith("/app/"):
            return real_path(str(tmp_path / text.lstrip("/")))
        return real_path(text)

    # solve.py reads /app/src and writes /app/output unconditionally; the input
    # files fall back to the package's environment/ copy when /app/src is absent.
    module.Path = host_path
    module.solve()
    result_path = tmp_path / "app" / "output" / "result.json"
    return json.loads(result_path.read_text(encoding="utf-8")), result_path


def grade(package: str, result_path: Path, log_dir: Path) -> tuple[int, dict[str, Any]]:
    """Run the package's tests/verify.py, not the verifier/ or oracle/ copy."""
    module = load_shipped_module(
        f"funcdag_{package.removeprefix('syn-funcdag-')}_verify",
        package_dir(package) / "tests" / "verify.py",
    )
    module.RESULT_PATH = result_path
    module.LOG_DIR = log_dir
    exit_code = module.verify()
    payload = json.loads((log_dir / "result.json").read_text(encoding="utf-8"))
    return exit_code, payload


def reward(log_dir: Path) -> str:
    return (log_dir / "reward.txt").read_text(encoding="utf-8").strip()


def assert_rejected(package: str, tmp_path: Path, result_path: Path, digest: str) -> dict[str, Any]:
    log_dir = tmp_path / "logs" / "verifier"
    exit_code, payload = grade(package, result_path, log_dir)
    assert exit_code == 1
    assert reward(log_dir) == "0"
    assert payload["passed"] is False
    assert package_digest(package) == digest
    return payload


def differing_fields(expected: dict[str, Any], observed: dict[str, Any]) -> set[str]:
    absent = object()
    return {
        key
        for key in expected.keys() | observed.keys()
        if expected.get(key, absent) != observed.get(key, absent)
    }


@pytest.mark.parametrize("package", PACKAGES)
def test_solver_output_passes(package: str, tmp_path: Path) -> None:
    digest = package_digest(package)
    imported_before = set(sys.modules)

    result, result_path = solve(package, tmp_path)
    golden = json.loads(
        (package_dir(package) / "tests" / "golden.json").read_text(encoding="utf-8")
    )
    assert result == golden

    log_dir = tmp_path / "logs" / "verifier"
    exit_code, payload = grade(package, result_path, log_dir)
    assert exit_code == 0
    assert reward(log_dir) == "1"
    assert payload["passed"] is True
    assert payload["observed_target"] == golden["target"]
    assert payload["observed_value"] == golden["value"]
    assert not {
        name
        for name in set(sys.modules) - imported_before
        if name.startswith(RETIRED_GENERATOR_PREFIX)
    }
    assert package_digest(package) == digest


@pytest.mark.parametrize("package", PACKAGES)
def test_missing_result_is_rejected(package: str, tmp_path: Path) -> None:
    digest = package_digest(package)
    _, result_path = solve(package, tmp_path)
    result_path.unlink()

    payload = assert_rejected(package, tmp_path, result_path, digest)
    assert "Result file does not exist" in payload["reason"]
    assert str(result_path) in payload["reason"]


@pytest.mark.parametrize("package", PACKAGES)
def test_empty_result_is_rejected(package: str, tmp_path: Path) -> None:
    digest = package_digest(package)
    _, result_path = solve(package, tmp_path)
    result_path.write_text("", encoding="utf-8")

    payload = assert_rejected(package, tmp_path, result_path, digest)
    assert "empty" in payload["reason"].lower()


@pytest.mark.parametrize("package", PACKAGES)
def test_malformed_json_is_rejected(package: str, tmp_path: Path) -> None:
    digest = package_digest(package)
    _, result_path = solve(package, tmp_path)
    result_path.write_text('{"target": ', encoding="utf-8")

    payload = assert_rejected(package, tmp_path, result_path, digest)
    assert "Invalid JSON" in payload["reason"]


@pytest.mark.parametrize("package", PACKAGES)
def test_answer_only_result_is_rejected(package: str, tmp_path: Path) -> None:
    digest = package_digest(package)
    result, result_path = solve(package, tmp_path)
    result_path.write_text(
        json.dumps({"target": result["target"], "value": result["value"]}),
        encoding="utf-8",
    )

    payload = assert_rejected(package, tmp_path, result_path, digest)
    assert "dependency_trace" in payload["reason"]
    assert set(payload["observed_keys"]) == {"target", "value"}
    assert "dependency_trace" in payload["expected_keys"]
    assert differing_fields(
        dict.fromkeys(payload["expected_keys"]), dict.fromkeys(payload["observed_keys"])
    ) == {"dependency_trace"}


@pytest.mark.parametrize("package", PACKAGES)
def test_wrong_target_is_rejected(package: str, tmp_path: Path) -> None:
    digest = package_digest(package)
    result, result_path = solve(package, tmp_path)
    wrong_target = "n_99_99"
    assert wrong_target != result["target"]
    result_path.write_text(json.dumps({**result, "target": wrong_target}), encoding="utf-8")

    payload = assert_rejected(package, tmp_path, result_path, digest)
    assert "contract" in payload["reason"]
    assert payload["observed"]["target"] == wrong_target
    assert differing_fields(payload["expected"], payload["observed"]) == {"target"}


@pytest.mark.parametrize("package", PACKAGES)
def test_wrong_value_is_rejected(package: str, tmp_path: Path) -> None:
    digest = package_digest(package)
    result, result_path = solve(package, tmp_path)
    result_path.write_text(json.dumps({**result, "value": result["value"] + 1}), encoding="utf-8")

    payload = assert_rejected(package, tmp_path, result_path, digest)
    assert "contract" in payload["reason"]
    assert payload["observed"]["value"] == result["value"] + 1
    assert differing_fields(payload["expected"], payload["observed"]) == {"value"}
