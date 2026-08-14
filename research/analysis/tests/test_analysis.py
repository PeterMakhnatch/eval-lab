from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evallab.facts import (
    AnalyzerCallResult,
    analysis_plan,
    failure_taxonomy_agreement,
    load_analysis_source,
    run_trial_analysis,
    validate_queue_authorization,
    write_analysis_review,
    write_failure_taxonomy_agreement,
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


def test_plan_is_bounded_and_makes_no_model_call(tmp_path: Path) -> None:
    trial_path = (
        ROOT
        / "evidence/runs/event-summary-oracle-evidence"
        / "event-summary__FZg7pvq"
    )
    job, trial = load_analysis_source(trial_path)

    plan = analysis_plan(
        job,
        trial,
        repo_root=ROOT,
        destination_root=tmp_path,
        prompt_path=PROMPT,
        rubric_path=RUBRIC,
        agent="codex",
        agent_version="local",
        model="queue-selected",
    )

    assert plan.source_trial_id == trial.id
    assert plan.estimated_model_calls == 1
    assert plan.maximum_model_calls == 2
    assert plan.queue_policy_rule == "researcher-followups"
    assert plan.prompt_digest.startswith("sha256:")
    assert plan.rubric_digest.startswith("sha256:")


def test_saved_output_writes_new_sidecars_with_complete_provenance(tmp_path: Path) -> None:
    trial_path = (
        ROOT
        / "evidence/runs/event-summary-oracle-evidence"
        / "event-summary__FZg7pvq"
    )
    job, trial = load_analysis_source(trial_path)
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
    job, trial = load_analysis_source(
        ROOT
        / "evidence/runs/event-summary-oracle-evidence"
        / "event-summary__FZg7pvq"
    )
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
    job, trial = load_analysis_source(
        ROOT
        / "evidence/runs/event-summary-oracle-evidence"
        / "event-summary__FZg7pvq"
    )
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


def test_review_is_append_only_and_preserves_original_analysis(tmp_path: Path) -> None:
    job, trial = load_analysis_source(
        ROOT
        / "evidence/runs/event-summary-oracle-evidence"
        / "event-summary__FZg7pvq"
    )
    sidecar_path, sidecar = _run(job, trial, tmp_path)
    original = sidecar_path.read_bytes()

    review_path, review = write_analysis_review(
        sidecar_path,
        disposition="accepted",
        rationale="The cited reward file supports this control-only finding.",
        reviewer="fixture-reviewer",
        reviewed_at=datetime(2026, 8, 14, tzinfo=UTC),
    )

    assert review.analysis_id == sidecar.analysis_id
    assert review_path.is_file()
    assert sidecar_path.read_bytes() == original


def test_live_adapter_requires_matching_running_queue_authorization(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    proposed = root / "queue/proposed/request.json"
    proposed.parent.mkdir(parents=True)
    proposed.write_text("{}")

    with pytest.raises(ValueError, match="queue/running"):
        validate_queue_authorization(
            proposed,
            repo_root=root,
            source_trial_id="trial-1",
        )

    running = root / "queue/running/request.json"
    running.parent.mkdir(parents=True)
    running.write_text(
        json.dumps(
            {
                "kind": "researcher-followup",
                "policy_rule": "researcher-followups",
                "source_trial_id": "trial-1",
                "max_model_calls": 2,
            }
        )
    )
    authorization = validate_queue_authorization(
        running,
        repo_root=root,
        source_trial_id="trial-1",
    )
    assert authorization["max_model_calls"] == 2


def test_failure_taxonomy_agreement_uses_only_valid_sidecars(tmp_path: Path) -> None:
    job, trial = load_analysis_source(
        ROOT
        / "evidence/runs/event-summary-oracle-evidence"
        / "event-summary__FZg7pvq"
    )
    valid_path, valid = _run(job, trial, tmp_path / "analyses")
    invalid_payload = valid.model_dump(mode="json")
    invalid_payload["analysis_id"] = "00000000-0000-0000-0000-000000000001"
    invalid_payload["validation_status"] = "invalid"
    invalid_payload["validation_errors"] = ["fixture-invalid citation"]
    invalid_dir = tmp_path / "analyses/invalid"
    invalid_dir.mkdir()
    invalid_path = invalid_dir / "analysis.json"
    invalid_path.write_text(json.dumps(invalid_payload, indent=2, sort_keys=True) + "\n")

    labels_root = ROOT / "research/calibration/trajectory-labels"
    before = _tree_digests(labels_root)
    report = failure_taxonomy_agreement(
        [valid_path, invalid_path],
        labels_root=labels_root,
        reference_root=ROOT,
    )

    assert report["n_labels"] == 25
    assert report["n_sidecars"] == 2
    assert report["n_matched_valid"] == 1
    assert report["n_invalid_analyses"] == 1
    assert report["exact_matches"] == 1
    assert report["exact_agreement"] == 1.0
    assert report["label_coverage"] == 1 / 25
    assert len(report["labels_without_valid_analysis"]) == 24
    assert report["comparisons"][0]["label_sha256"].startswith("sha256:")
    assert report["comparisons"][0]["sidecar_sha256"].startswith("sha256:")
    assert _tree_digests(labels_root) == before

    report_path = tmp_path / "reports/agreement.json"
    written_path, written = write_failure_taxonomy_agreement(
        [tmp_path / "analyses"],
        labels_root=labels_root,
        output_path=report_path,
        reference_root=ROOT,
    )
    first_bytes = written_path.read_bytes()
    write_failure_taxonomy_agreement(
        [tmp_path / "analyses"],
        labels_root=labels_root,
        output_path=report_path,
        reference_root=ROOT,
    )
    assert written == report
    assert report_path.read_bytes() == first_bytes
