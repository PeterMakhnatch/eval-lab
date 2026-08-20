from __future__ import annotations

import json
import shutil
from pathlib import Path

from evallab.explorer import (
    TrajectoryView,
    _resolve_citation,
    build_index,
    jail,
)
from evallab.traj import outline_trajectory, render_outline

FIXTURES = Path(__file__).parent / "fixtures" / "explorer"
JOBS = FIXTURES / "jobs"
PASS = JOBS / "job-pass" / "t1"


def _write_analysis(
    directory: Path,
    *,
    analysis_id: str = "a1",
    trial_id: str = "00000000-0000-4000-8000-000000000001",
    evidence: list[dict] | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{analysis_id}.json"
    path.write_text(
        json.dumps(
            {
                "analysis_id": analysis_id,
                "trial_id": trial_id,
                "model": "stub",
                "category": "inference",
                "summary": "Stored analyst conclusion, not verifier truth.",
                "confidence": {"level": "high"},
                "rubric_digest": "sha256:" + "a" * 64,
                "evidence": evidence or [],
                "created_at": "2026-08-19T00:00:00Z",
            }
        )
    )
    return path


def test_real_atif_outline_renders_phases_and_step_highlights() -> None:
    outline = outline_trajectory(PASS, repo_root=FIXTURES, explicit_runs_root=JOBS)
    rendered = render_outline(outline)

    assert outline.status == "featured"
    assert outline.phases
    assert "ORDERED PHASES:" in rendered
    assert "STEP HIGHLIGHTS:" in rendered
    assert "run_pytest" in rendered


def test_no_trajectory_agy_fallback_is_explicit(tmp_path: Path) -> None:
    trial = tmp_path / "runs" / "agy-job" / "t1"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps(
            {
                "id": "agy-trial",
                "task_name": "lab/demo",
                "agent_info": {"name": "antigravity-cli", "model_info": {"name": "gemini"}},
                "verifier_result": {"rewards": {"reward": 1.0}},
            }
        )
    )

    view = build_index([tmp_path / "runs"], review_queue_limit=0).trials["agy-job/t1"]

    assert view.trajectory_outline is not None
    assert view.trajectory_outline.status == "accounted_unavailable"
    assert view.trajectory_fallback is not None
    assert "final response only" in (view.trajectory_fallback.reason or "")


def test_truth_and_analysis_are_separate_and_transcript_missing_is_honest(tmp_path: Path) -> None:
    analysis_dir = tmp_path / "research" / "analysis"
    _write_analysis(analysis_dir)
    index = build_index(
        [JOBS],
        repo_root=tmp_path,
        analyst_dir=analysis_dir,
        review_queue_limit=0,
    )
    trial = index.trials["job-pass/t1"]
    analysis = index.analyst_analyses[0]

    assert trial.verifier_output is not None
    assert trial.verifier_output.provenance == "observed"
    assert analysis.summary.provenance == "draft"
    assert "not ground truth" in (analysis.summary.reason or "")
    assert analysis.transcript.provenance == "unavailable"
    assert "only the stored conclusion/final response" in (analysis.transcript.reason or "")


def test_stored_analyst_source_citation_resolves(tmp_path: Path) -> None:
    analysis_dir = tmp_path / "analysis"
    _write_analysis(
        analysis_dir,
        evidence=[
            {
                "path": "agent/trajectory.json",
                "step": 1,
                "supports": "recorded tool use",
            }
        ],
    )
    index = build_index([JOBS], repo_root=tmp_path, analyst_dir=analysis_dir, review_queue_limit=0)

    (citation,) = index.analyst_analyses[0].citations
    assert citation.resolution.value == "resolved"
    assert citation.content.provenance == "observed"


def test_path_escape_and_hidden_verifier_content_are_unavailable(tmp_path: Path) -> None:
    trial_root = tmp_path / "jobs" / "job-pass" / "t1"
    trial_root.parent.mkdir(parents=True)
    shutil.copytree(PASS, trial_root)
    hidden = trial_root / "tests"
    hidden.mkdir()
    (hidden / "secret.py").write_text("oauth_token = 'do-not-render'")
    index = build_index([tmp_path / "jobs"], review_queue_limit=0)
    trial = index.trials["job-pass/t1"]

    assert jail(trial_root, "../result.json") is None
    assert jail(trial_root, "tests/secret.py") is None
    citation = _resolve_citation({"path": "tests/secret.py", "supports": "hidden"}, trial)
    assert citation.resolution.provenance == "unavailable"
    assert all("secret.py" not in artifact.relative_path for artifact in trial.artifacts)


def test_nested_key_shaped_fields_are_redacted(tmp_path: Path) -> None:
    trial_root = tmp_path / "jobs" / "job-pass" / "t1"
    trial_root.parent.mkdir(parents=True)
    shutil.copytree(PASS, trial_root)
    (trial_root / "config.json").write_text(
        json.dumps(
            {
                "agent": {"name": "codex"},
                "env": {"oauth_session_token": "raw-oauth-token"},
            }
        )
    )
    trial = build_index([tmp_path / "jobs"], review_queue_limit=0).trials["job-pass/t1"]
    assert "raw-oauth-token" not in str(trial.config.value)


def test_m030_legacy_trajectory_remains_available_for_existing_surface() -> None:
    trial = build_index([JOBS], review_queue_limit=0).trials["job-pass/t1"]
    assert isinstance(trial.trajectory, TrajectoryView)
