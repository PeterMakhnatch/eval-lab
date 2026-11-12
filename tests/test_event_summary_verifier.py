"""Host contract for the shipped event-summary hidden verifier.

The verifier runs in a separate verifier image and reads four absolute paths.
This module imports it unchanged, redirects those globals into ``tmp_path``,
runs a redirected copy of the shipped solution with ``python -I``, and reads the
reward from ``reward.json``. It fails when an edit starts accepting a wrong
summary, a stray output file, or a rewritten input, and when the shipped
solution stops earning 1.0.

The task package bytes are frozen by the gym-v1 manifest, so the contract lives
here instead of ``library/tasks/event-summary``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "library" / "tasks" / "event-summary"
VERIFIER = PACKAGE / "tests" / "verify.py"
SOLUTION = PACKAGE / "solution" / "solve.py"
TRUSTED_FIXTURE = PACKAGE / "tests" / "fixtures" / "events.jsonl"


def package_digest() -> str:
    """Digest over every file in the package, so importing cannot rewrite bytes."""
    digest = hashlib.sha256()
    for path in sorted(candidate for candidate in PACKAGE.rglob("*") if candidate.is_file()):
        digest.update(path.relative_to(PACKAGE).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


@pytest.fixture(autouse=True)
def unchanged_package() -> Iterator[None]:
    before = package_digest()
    yield
    assert package_digest() == before
    assert not list(PACKAGE.rglob("__pycache__"))


@pytest.fixture
def verifier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import the shipped verifier and point its absolute paths at tmp_path."""
    # Keep bytecode out of the byte-pinned task package.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    spec = importlib.util.spec_from_file_location("event_summary_verifier", VERIFIER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    agent_input = tmp_path / "app" / "input" / "events.jsonl"
    agent_input.parent.mkdir(parents=True)
    shutil.copyfile(TRUSTED_FIXTURE, agent_input)
    monkeypatch.setattr(module, "TRUSTED_INPUT", TRUSTED_FIXTURE)
    monkeypatch.setattr(module, "AGENT_INPUT", agent_input)
    monkeypatch.setattr(module, "AGENT_OUTPUT", tmp_path / "app" / "output" / "summary.json")
    monkeypatch.setattr(module, "LOG_DIR", tmp_path / "logs" / "verifier")
    return module


@pytest.fixture
def shipped_solution(verifier: ModuleType, tmp_path: Path) -> Path:
    """Run the shipped solution with its two /app literals redirected into tmp_path."""
    source = SOLUTION.read_text(encoding="utf-8")
    assert source.count('"/app/input/events.jsonl"') == 1
    assert source.count('"/app/output/summary.json"') == 1
    redirected = source.replace(
        '"/app/input/events.jsonl"', json.dumps(str(verifier.AGENT_INPUT))
    ).replace('"/app/output/summary.json"', json.dumps(str(verifier.AGENT_OUTPUT)))
    script = tmp_path / "solve.py"
    script.write_text(redirected, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-I", str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert verifier.AGENT_OUTPUT.is_file()
    return verifier.AGENT_OUTPUT


def summary(verifier: ModuleType) -> dict[str, Any]:
    return json.loads(verifier.AGENT_OUTPUT.read_text(encoding="utf-8"))


def write_summary(verifier: ModuleType, candidate: dict[str, Any], *, newline: bool = True) -> None:
    text = json.dumps(candidate) + ("\n" if newline else "")
    verifier.AGENT_OUTPUT.write_text(text, encoding="utf-8")


def grade(verifier: ModuleType) -> dict[str, float]:
    verifier.main()
    return json.loads((verifier.LOG_DIR / "reward.json").read_text(encoding="utf-8"))


def test_shipped_solution_scores_one(verifier: ModuleType, shipped_solution: Path) -> None:
    rewards = grade(verifier)
    assert rewards == {
        "reward": 1.0,
        "correctness": 1.0,
        "input_preservation": 1.0,
        "output_hygiene": 1.0,
    }


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        (
            "wrong_total_events",
            {"reward": 0.0, "correctness": 0.0, "input_preservation": 1.0, "output_hygiene": 1.0},
        ),
        (
            "extra_key",
            {"reward": 0.0, "correctness": 0.0, "input_preservation": 1.0, "output_hygiene": 1.0},
        ),
        (
            "missing_key",
            {"reward": 0.0, "correctness": 0.0, "input_preservation": 1.0, "output_hygiene": 1.0},
        ),
        (
            "p95_as_mean",
            {"reward": 0.0, "correctness": 0.0, "input_preservation": 1.0, "output_hygiene": 1.0},
        ),
        (
            "second_output_file",
            {"reward": 0.0, "correctness": 1.0, "input_preservation": 1.0, "output_hygiene": 0.0},
        ),
        (
            "modified_input",
            {"reward": 0.0, "correctness": 1.0, "input_preservation": 0.0, "output_hygiene": 1.0},
        ),
    ],
)
def test_wrong_submission_scores_zero(
    verifier: ModuleType,
    shipped_solution: Path,
    case: str,
    expected: dict[str, float],
) -> None:
    candidate = summary(verifier)
    if case == "wrong_total_events":
        candidate["total_events"] += 1
    elif case == "extra_key":
        candidate["unexpected"] = 0
    elif case == "missing_key":
        del candidate["counts"]
    elif case == "p95_as_mean":
        candidate["p95_duration_ms"] = candidate["total_duration_ms"] / candidate["total_events"]
    elif case == "second_output_file":
        (verifier.AGENT_OUTPUT.parent / "extra.json").write_text("{}\n", encoding="utf-8")
    elif case == "modified_input":
        verifier.AGENT_INPUT.write_bytes(verifier.AGENT_INPUT.read_bytes() + b"\n")
    else:
        raise AssertionError(case)
    write_summary(verifier, candidate)
    assert grade(verifier) == expected


@pytest.mark.parametrize(
    "case",
    ["boolean_schema_version", "float_schema_version", "reversed_counts", "no_trailing_newline"],
)
@pytest.mark.xfail(
    strict=True,
    reason="verifier accepts a submission that violates instruction.md lines 8-17",
)
def test_instruction_gap_scores_zero(
    verifier: ModuleType, shipped_solution: Path, case: str
) -> None:
    """The instruction requires an integer schema_version, alphabetical counts keys,
    and a trailing newline (instruction.md lines 8-17). The verifier checks none of
    those three properties, so each case currently earns reward 1.0 and this strict
    xfail records the gap. When the verifier tightens, the case passes and the
    marker must be removed.
    """
    candidate = summary(verifier)
    newline = True
    if case == "boolean_schema_version":
        candidate["schema_version"] = True
    elif case == "float_schema_version":
        candidate["schema_version"] = 1.0
    elif case == "reversed_counts":
        candidate["counts"] = dict(reversed(list(candidate["counts"].items())))
    elif case == "no_trailing_newline":
        newline = False
    else:
        raise AssertionError(case)
    write_summary(verifier, candidate, newline=newline)
    rewards = grade(verifier)
    assert rewards["input_preservation"] == 1.0
    assert rewards["output_hygiene"] == 1.0
    assert rewards["reward"] == 0.0, f"{case} is still accepted by the verifier"
