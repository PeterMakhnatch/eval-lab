from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evallab.evidence.facts import (
    AnalyzerCallResult,
    run_trial_analysis,
)
from evallab.results import load_job
from evallab.schemas import TrialAnalysisSidecar

from .test_atif import _make_job
from .test_cohort import _synthetic_job

ROOT = Path(__file__).resolve().parents[3]
PROMPT = ROOT / "research/analysis/stage5-prompt.md"
RUBRIC = ROOT / "research/analysis/stage5-rubric.json"
STUB = ROOT / "research/analysis/stub-oracle-analysis.json"


def _tree_digests(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _saved_analyzer(payload: dict[str, object] | None = None):
    raw = json.dumps(payload) if payload is not None else STUB.read_text()

    def analyzer(_prompt: str, _schema: dict[str, object]) -> AnalyzerCallResult:
        return AnalyzerCallResult(
            raw_output=raw,
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.0,
        )

    return analyzer


def _run(
    job,
    trial,
    destination: Path,
    analyzer=None,
):
    return run_trial_analysis(
        job,
        trial,
        analyzer=analyzer or _saved_analyzer(),
        repo_root=ROOT,
        destination_root=destination,
        prompt_path=PROMPT,
        rubric_path=RUBRIC,
        agent="stub",
        agent_version="1",
        model="saved-response",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
    )


def test_saved_output_writes_new_sidecars_with_complete_provenance(tmp_path: Path) -> None:
    trial_path = (
        ROOT
        / "research/evidence/runs/event-summary-oracle-evidence"
        / "event-summary__FZg7pvq"
    )
    job = load_job(trial_path.parent)
    trial = next(item for item in job.trials if item.path == trial_path)
    before = _tree_digests(trial.path)

    first_path, first = _run(job, trial, tmp_path)
    second_path, second = _run(job, trial, tmp_path)

    assert first_path != second_path
    assert first.analysis_id != second.analysis_id
    assert first.validation_status == "valid"
    assert first.source_trial_id == second.source_trial_id
    assert first.source_digests.result.startswith("sha256:")
    assert first.source_digests.files["verifier/reward.json"].startswith("sha256:")
    assert first.analysis_provenance.prompt_digest.startswith("sha256:")
    assert first.analysis_provenance.rubric_digest.startswith("sha256:")
    assert first.analysis_provenance.output_schema_digest.startswith("sha256:")
    assert _tree_digests(trial.path) == before
    assert TrialAnalysisSidecar.model_validate_json(first_path.read_text()) == first


def test_schema_validation_retries_once(tmp_path: Path) -> None:
    job = load_job(ROOT / "research/evidence/runs/event-summary-oracle-evidence")
    trial = next(item for item in job.trials if item.path.name == "event-summary__FZg7pvq")
    responses = [
        json.dumps({"primary_category": "not-a-category"}),
        STUB.read_text(),
    ]
    prompts: list[str] = []

    def analyzer(prompt: str, _schema: dict[str, object]) -> AnalyzerCallResult:
        prompts.append(prompt)
        return AnalyzerCallResult(raw_output=responses[len(prompts) - 1])

    _, sidecar = _run(job, trial, tmp_path, analyzer=analyzer)

    assert sidecar.validation_status == "valid"
    assert len(prompts) == 2
    assert "failed schema validation" in prompts[1]


def test_unknown_failure_category_is_rejected_after_one_retry(tmp_path: Path) -> None:
    job = load_job(ROOT / "research/evidence/runs/event-summary-oracle-evidence")
    trial = next(item for item in job.trials if item.path.name == "event-summary__FZg7pvq")
    output = json.loads(STUB.read_text())
    output["primary_category"] = "invented-category"
    calls = 0

    def analyzer(_prompt: str, _schema: dict[str, object]) -> AnalyzerCallResult:
        nonlocal calls
        calls += 1
        return AnalyzerCallResult(raw_output=json.dumps(output))

    with pytest.raises(ValueError, match="after one retry"):
        _run(job, trial, tmp_path, analyzer=analyzer)
    assert calls == 2


def test_missing_file_step_and_tool_references_mark_sidecar_invalid(tmp_path: Path) -> None:
    job = load_job(_make_job(tmp_path / "raw"))
    trial = job.trials[0]
    output = {
        "validity": "valid_agent_attempt",
        "primary_category": "tool_use",
        "summary": "A structurally valid but deliberately unsupported fixture claim.",
        "earliest_failure_step_id": 999,
        "evidence": [
            {"path": "missing.txt", "supports": "missing"},
            {
                "path": "agent/trajectory.json",
                "step_id": 999,
                "supports": "missing step",
            },
            {
                "path": "agent/trajectory.json",
                "step_id": 2,
                "tool_call_id": "missing-call",
                "supports": "missing tool",
            },
        ],
        "alternative_explanations": [],
        "proposed_discriminator": "Inspect a valid citation.",
        "confidence": "low",
    }

    _, sidecar = _run(job, trial, tmp_path / "analyses", _saved_analyzer(output))

    assert sidecar.validation_status == "invalid"
    assert any("missing file" in error for error in sidecar.validation_errors)
    assert any("missing step" in error for error in sidecar.validation_errors)
    assert any("missing tool call" in error for error in sidecar.validation_errors)


def test_atif_path_step_and_tool_reference_resolves(tmp_path: Path) -> None:
    job = load_job(_make_job(tmp_path / "raw"))
    trial = job.trials[0]
    output = {
        "validity": "valid_agent_attempt",
        "primary_category": "tool_use",
        "summary": "The fixture command returned a structured nonzero exit code.",
        "earliest_failure_step_id": 2,
        "evidence": [
            {
                "path": "agent/trajectory.json",
                "step_id": 2,
                "tool_call_id": "call-1",
                "supports": "The cited tool call has a linked observation with exit_code 1.",
            }
        ],
        "alternative_explanations": ["The fixture intentionally seeded the failure."],
        "proposed_discriminator": "Repeat with only the command argument changed.",
        "confidence": "high",
    }

    _, sidecar = _run(job, trial, tmp_path / "analyses", _saved_analyzer(output))

    assert sidecar.validation_status == "valid"
    assert sidecar.source_digests.trajectory is not None


def test_harness_exception_cannot_be_mislabeled_as_agent_failure(tmp_path: Path) -> None:
    job_dir = _synthetic_job(
        tmp_path / "raw",
        suffix=9,
        agent="codex",
        reward=0.0,
        exception="AgentAuthenticationError",
    )
    job = load_job(job_dir)
    trial = job.trials[0]
    output = json.loads(STUB.read_text())
    output["primary_category"] = "planning"

    _, sidecar = _run(job, trial, tmp_path / "analyses", _saved_analyzer(output))

    assert sidecar.validation_status == "invalid"
    assert any("harness exception" in error for error in sidecar.validation_errors)






