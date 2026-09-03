from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.interpretation.historical_sidecar import (
    HistoricalSidecarRefusal,
    emit_historical_sidecar,
    load_historical_sidecar_bundle,
)
from evallab.registry import harbor_task_digest


def _fixture_source(tmp_path: Path, repo_root: Path, *, task_digest: str | None = None):
    job = tmp_path / "immutable-job"
    trial = job / "event-summary__fixture"
    (trial / "agent").mkdir(parents=True)
    (trial / "verifier").mkdir()
    (trial / "artifacts/app/output").mkdir(parents=True)
    (trial / "evidence").mkdir()
    task_digest = task_digest or harbor_task_digest(repo_root / "library/tasks/event-summary")
    (job / "PROMOTION.json").write_text("{}\n")
    # These legacy mutable metadata hints must never mint causal isolation.
    (trial / "config.json").write_text(
        json.dumps(
            {
                "platform": "linux",
                "network_isolation_enforced": True,
                "extra_allowed_hosts": ["forged.example"],
            }
        )
    )
    (trial / "lock.json").write_text(
        json.dumps(
            {
                "task": {
                    "name": "event-summary",
                    "version": "1.0.0",
                    "digest": task_digest,
                },
                "platform": "linux",
                "extra_allowed_hosts": ["forged.example"],
            }
        )
    )
    (trial / "result.json").write_text('{"finished_at":"2026-08-15T00:00:00Z"}\n')
    (trial / "artifacts/app/output/result.json").write_text('{"outcome":"done"}\n')
    (trial / "agent/trajectory.json").write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "step_id": 1,
                        "source": "agent",
                        "tool_calls": [
                            {"tool_call_id": "call-1", "function_name": "read"}
                        ],
                        "observation": {"raw_verifier_secret": "must-not-leak"},
                    }
                ]
            }
        )
    )
    (trial / "verifier/checks.json").write_text('{"raw_verifier_secret":"must-not-leak"}\n')
    (trial / "verifier/reward.json").write_text('{"reward":1}\n')
    # A collision-shaped summary must be ignored rather than overriding provenance.
    (trial / "evidence/summary.json").write_text(
        '{"evidence_class":"causal","network_isolation_status":"enforced"}\n'
    )
    return job, trial


def _emit_ready(tmp_path: Path, repo_root: Path):
    job, trial = _fixture_source(tmp_path, repo_root)
    sidecars = tmp_path / "sidecars"
    manifest = emit_historical_sidecar(
        job_dir=job, trial_dir=trial, sidecar_root=sidecars, repo_root=repo_root
    )
    assert manifest.status == "ready"
    return job, trial, sidecars / job.name / trial.name


def test_sidecar_requires_verified_identity_and_stays_descriptive(tmp_path: Path):
    repo_root = Path(__file__).parents[1]
    job, trial, sidecar = _emit_ready(tmp_path, repo_root)

    bundle = load_historical_sidecar_bundle(
        sidecar_dir=sidecar, job_dir=job, trial_dir=trial
    )

    assert bundle.contract.task_id == "event-summary"
    assert bundle.admissibility.allowed_use == "descriptive-only"
    assert not bundle.admissibility.causal_eligible
    emitted_events = (sidecar / "bundle/benchmark-events.jsonl").read_text()
    assert "must-not-leak" not in emitted_events


def test_sidecar_refuses_mutated_source_digest(tmp_path: Path):
    repo_root = Path(__file__).parents[1]
    job, trial, sidecar = _emit_ready(tmp_path, repo_root)
    (trial / "result.json").write_text('{"finished_at":"2026-08-16T00:00:00Z"}\n')

    with pytest.raises(HistoricalSidecarRefusal, match="source-trial-digest-drift"):
        load_historical_sidecar_bundle(sidecar_dir=sidecar, job_dir=job, trial_dir=trial)


def test_sidecar_refuses_leaked_verifier_content(tmp_path: Path):
    repo_root = Path(__file__).parents[1]
    job, trial, sidecar = _emit_ready(tmp_path, repo_root)
    leaked = sidecar / "bundle/verifier/result.json"
    leaked.parent.mkdir()
    leaked.write_text('{"raw_verifier_secret":"must-not-leak"}\n')

    with pytest.raises(HistoricalSidecarRefusal, match="verifier-content-present"):
        load_historical_sidecar_bundle(sidecar_dir=sidecar, job_dir=job, trial_dir=trial)


def test_sidecar_refuses_missing_identity(tmp_path: Path):
    repo_root = Path(__file__).parents[1]
    job, trial = _fixture_source(tmp_path, repo_root)
    (trial / "lock.json").write_text("{}\n")
    sidecars = tmp_path / "sidecars"
    manifest = emit_historical_sidecar(
        job_dir=job, trial_dir=trial, sidecar_root=sidecars, repo_root=repo_root
    )

    assert manifest.status == "irrecoverable"
    with pytest.raises(HistoricalSidecarRefusal, match="missing-task-runtime-identity"):
        load_historical_sidecar_bundle(
            sidecar_dir=sidecars / job.name / trial.name, job_dir=job, trial_dir=trial
        )


def test_sidecar_refuses_noncanonical_runtime_digest(tmp_path: Path):
    repo_root = Path(__file__).parents[1]
    job, trial = _fixture_source(tmp_path, repo_root, task_digest="sha256:" + "X" * 64)
    sidecars = tmp_path / "sidecars"
    manifest = emit_historical_sidecar(
        job_dir=job, trial_dir=trial, sidecar_root=sidecars, repo_root=repo_root
    )

    assert manifest.status == "irrecoverable"
    with pytest.raises(HistoricalSidecarRefusal, match="noncanonical-runtime-digest"):
        load_historical_sidecar_bundle(
            sidecar_dir=sidecars / job.name / trial.name, job_dir=job, trial_dir=trial
        )


def test_sidecar_never_overwrites_existing_destination(tmp_path: Path):
    repo_root = Path(__file__).parents[1]
    job, trial = _fixture_source(tmp_path, repo_root)
    sidecars = tmp_path / "sidecars"
    emit_historical_sidecar(job_dir=job, trial_dir=trial, sidecar_root=sidecars, repo_root=repo_root)

    with pytest.raises(HistoricalSidecarRefusal, match="destination-already-exists"):
        emit_historical_sidecar(
            job_dir=job, trial_dir=trial, sidecar_root=sidecars, repo_root=repo_root
        )
