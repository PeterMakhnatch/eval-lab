from __future__ import annotations

import copy
import json
import runpy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPERIMENTS_ROOT = REPO_ROOT / "research" / "experiments"
FIXTURES = Path(__file__).parent / "fixtures"
VALIDATOR = runpy.run_path(str(EXPERIMENTS_ROOT / "validate_program.py"))
count_failed_command_observations = VALIDATOR["count_failed_command_observations"]
validate = VALIDATOR["validate"]


def _write_program(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "PROGRAM.json"
    path.write_text(json.dumps(payload))
    return path


def _current_program() -> dict[str, object]:
    return json.loads((EXPERIMENTS_ROOT / "PROGRAM.json").read_text())


def test_current_program_is_valid() -> None:
    assert validate(EXPERIMENTS_ROOT / "PROGRAM.json") == []


@pytest.mark.parametrize(
    ("case"),
    json.loads((FIXTURES / "invalid-programs.json").read_text()),
    ids=lambda case: case["name"],
)
def test_known_bad_programs_are_rejected(case: dict[str, object], tmp_path: Path) -> None:
    path = _write_program(tmp_path, case["program"])
    errors = validate(path, repo_root=REPO_ROOT)
    assert any(str(case["expected_error"]) in error for error in errors), errors


def test_observation_text_failures_are_counted_without_exit_codes() -> None:
    payload = json.loads((FIXTURES / "observation-failure-counts.json").read_text())
    actual = {
        case["name"]: count_failed_command_observations(case["trajectory"])
        for case in payload["trajectories"]
    }
    expected = {case["name"]: case["expected_failures"] for case in payload["trajectories"]}
    assert actual == expected == {"5rgjEEt": 3, "D3GZpFU": 1, "kzGxL7Q": 3}


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda item: item.update(k=0), ".k must be a positive integer"),
        (lambda item: item.update(status="almost-done"), ".status 'almost-done' not in enum"),
        (lambda item: item.update(model=7), ".model must be null or a non-empty string"),
        (lambda item: item.update(unexpected=True), "has unknown fields ['unexpected']"),
        (
            lambda item: item["references"]["spec"].append("research/experiments/missing"),
            "does not exist",
        ),
    ],
)
def test_meaningful_types_enums_unknowns_and_paths(
    mutation: Callable[[dict[str, Any]], None],
    expected: str,
    tmp_path: Path,
) -> None:
    payload = copy.deepcopy(_current_program())
    item = payload["experiments"][0]
    mutation(item)
    errors = validate(_write_program(tmp_path, payload), repo_root=REPO_ROOT)
    assert any(expected in error for error in errors), errors


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("root", "PROGRAM has unknown fields ['unexpected']"),
        ("references", ".references has unknown fields ['unexpected']"),
        ("decision_rule", ".decision_rule has unknown fields ['unexpected']"),
        ("evidence_provenance", ".evidence_provenance has unknown fields ['unexpected']"),
    ],
)
def test_unknown_fields_are_rejected_at_every_schema_level(
    level: str,
    expected: str,
    tmp_path: Path,
) -> None:
    payload = copy.deepcopy(_current_program())
    item = payload["experiments"][0]
    target = payload if level == "root" else item[level]
    target["unexpected"] = True
    errors = validate(_write_program(tmp_path, payload), repo_root=REPO_ROOT)
    assert any(expected in error for error in errors), errors


def test_representative_attempts_must_match_referenced_spec(tmp_path: Path) -> None:
    payload = copy.deepcopy(_current_program())
    item = next(
        item
        for item in payload["experiments"]
        if item["id"] == "EXP-S05-curated-nominees"
    )
    item["representative_attempts"] = 4
    item["decision_rule"]["representative_attempts"] = 4
    errors = validate(_write_program(tmp_path, payload), repo_root=REPO_ROOT)
    assert any("spec attempts [3]" in error and "count 4" in error for error in errors), errors


def test_reference_path_cannot_escape_repository(tmp_path: Path) -> None:
    payload = copy.deepcopy(_current_program())
    payload["experiments"][0]["references"]["spec"] = ["../outside"]
    errors = validate(_write_program(tmp_path, payload), repo_root=REPO_ROOT)
    assert any("must stay inside the repository" in error for error in errors), errors
