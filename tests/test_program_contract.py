"""Gate the shipped PROGRAM.json validator, including negative inputs."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from evallab.results import load_job

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "research/experiments/validate_program.py"
PROGRAM = ROOT / "research/experiments/PROGRAM.json"


def _load_validator():
    spec = importlib.util.spec_from_file_location("eval_lab_validate_program", VALIDATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_validate_program_accepts_the_committed_ledger() -> None:
    module = _load_validator()
    errors = module.validate(PROGRAM)
    assert errors == []


def test_validate_program_rejects_empty_and_unversioned_ledgers(tmp_path: Path) -> None:
    module = _load_validator()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema_version": 2, "experiments": []}))
    errors = module.validate(bad)
    assert errors
    assert any("schema_version" in error for error in errors)
    assert any("experiments" in error for error in errors)


def test_validate_program_rejects_an_experiment_missing_required_keys(
    tmp_path: Path,
) -> None:
    module = _load_validator()
    bad = tmp_path / "incomplete.json"
    bad.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "experiments": [{"id": "EXP-INCOMPLETE", "status": "idea"}],
            }
        )
    )
    errors = module.validate(bad)
    assert any("missing" in error for error in errors)


def test_load_job_still_rejects_a_job_missing_finished_at(tmp_path: Path) -> None:
    job = tmp_path / "incomplete-job"
    job.mkdir()
    (job / "result.json").write_text(
        json.dumps(
            {
                "id": "00000000-0000-0000-0000-000000000099",
                "n_total_trials": 1,
                "stats": {"n_completed_trials": 1},
            }
        )
    )
    (job / "config.json").write_text("{}")
    (job / "lock.json").write_text("{}")
    (job / "lab-metadata.json").write_text("{}")
    with pytest.raises(ValueError, match="Not a completed Harbor job directory"):
        load_job(job)
