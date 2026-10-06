"""Harbor 0.24 lock-version gate: a 0.21 lock vs a 0.24 lock is a declared
version change, never a digest mismatch (HAR-167).

Lock-content digest comparisons key on the job lock's ``harbor.version``
first. Trial locks carry no version stamp; the job lock does. Readers
(counts, process-job, features, ATIF projection) accept both shapes.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from evallab.analysis_worker import AdmissionContext, AnalysisRequest, RequestStore, admit
from evallab.evidence.facts import (
    lock_harbor_version,
    lock_version_change_reason,
)

REPO_021_JOB = (
    Path(__file__).resolve().parent.parent
    / "research/evidence/runs/gepa-zai-opencode-event-summary-98b229178d8e76bd14f2364f"
)


def test_lock_harbor_version_reads_job_stamp() -> None:
    assert lock_harbor_version({"harbor": {"version": "0.21.0"}}) == "0.21.0"
    assert lock_harbor_version({"harbor": {"version": "0.24.0"}}) == "0.24.0"
    # Trial locks carry no stamp; 0.21-era locks may lack the field entirely.
    assert lock_harbor_version({"task": {"digest": "sha256:abc"}}) is None
    assert lock_harbor_version({}) is None
    assert lock_harbor_version(None) is None
    assert lock_harbor_version({"harbor": {"version": ""}}) is None
    assert lock_harbor_version({"harbor": "0.21.0"}) is None


def test_lock_version_change_reason_declares_upgrade_only() -> None:
    assert lock_version_change_reason("0.21.0", "0.24.0") == "harbor_version_change:0.21.0->0.24.0"
    assert lock_version_change_reason("0.24.0", "0.24.0") is None
    assert lock_version_change_reason(None, None) is None
    assert lock_version_change_reason(None, "0.24.0") == "harbor_version_change:unknown->0.24.0"


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _request_body(root: Path, trial_dir: Path, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "request_id": "a" * 16,
        "created_at": datetime(2026, 10, 5, tzinfo=UTC),
        "experiment_id": None,
        "job_id": "job-x",
        "trial_id": "trial-1",
        "job_name": "job-x",
        "trial_name": "trial-1",
        "trial_path": trial_dir.relative_to(root).as_posix(),
        "profile_id": "p",
        "adapter": "oracle",
        "model": "",
        "result_sha256": _sha(trial_dir / "result.json"),
        "trajectory_sha256": _sha(trial_dir / "agent" / "trajectory.json"),
        "lock_sha256": _sha(trial_dir / "lock.json"),
        "lock_harbor_version": "0.21.0",
        "task_digest": None,
        "verifier_digest": None,
        "rubric_sha256": "sha256:" + "0" * 64,
        "prompt_sha256": "sha256:" + "0" * 64,
        "profile_digest": "sha256:" + "0" * 64,
    }
    body.update(overrides)
    return body


def _context() -> AdmissionContext:
    return AdmissionContext(
        stop_present=lambda: False,
        policy=None,  # type: ignore[arg-type]
        profile=None,  # type: ignore[arg-type]
        probe=None,
        spent_today_usd=lambda: 0.0,
        est_call_cost_usd=0.01,
        services_healthy=lambda: True,
        requirement_checks={},
    )


def _trial_tree(root: Path) -> Path:
    trial_dir = root / "jobs" / "job-x" / "trial-1"
    (trial_dir / "agent").mkdir(parents=True)
    (trial_dir / "result.json").write_text(json.dumps({"id": "trial-1"}))
    (trial_dir / "agent" / "trajectory.json").write_text(json.dumps({"steps": []}))
    (trial_dir / "lock.json").write_text(json.dumps({"task": {"digest": "sha256:abc"}}))
    (trial_dir.parent / "lock.json").write_text(json.dumps({"harbor": {"version": "0.21.0"}}))
    return trial_dir


def test_admit_declares_harbor_upgrade_not_tampering(tmp_path: Path) -> None:
    trial_dir = _trial_tree(tmp_path)
    request = AnalysisRequest.model_validate(_request_body(tmp_path, trial_dir))
    # Harbor 0.24 rewrites the lock bytes and stamps its own version.
    (trial_dir / "lock.json").write_text(json.dumps({"task": {"digest": "sha256:abc"}, "v": 2}))
    (trial_dir.parent / "lock.json").write_text(json.dumps({"harbor": {"version": "0.24.0"}}))
    store = RequestStore(tmp_path / "store")
    decision = admit(request, store, _context(), tmp_path)
    assert decision.kind == "defer"
    assert decision.reason == "harbor_version_change:0.21.0->0.24.0"


def test_admit_same_version_content_change_still_quarantines(tmp_path: Path) -> None:
    trial_dir = _trial_tree(tmp_path)
    request = AnalysisRequest.model_validate(_request_body(tmp_path, trial_dir))
    (trial_dir / "lock.json").write_text(json.dumps({"task": {"digest": "sha256:def"}}))
    store = RequestStore(tmp_path / "store")
    decision = admit(request, store, _context(), tmp_path)
    assert decision.kind == "quarantine"
    assert decision.reason == "evidence_tampered:lock.json"


def _write_version_case(
    root: Path, task_id: str, *, recorded: str | None, current: str | None
) -> Any:
    from evallab.registry import compute_task_digests, harbor_task_digest
    from evallab.schemas import (
        ControlEvidenceRef,
        TaskControlEvidence,
        TaskLimits,
        TaskRegistryRecord,
    )

    task_dir = root / "library/tasks/version-task"
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text(
        f'schema_version = "1.4"\n[task]\nname = "{task_id}"\nfamily = "sample-family"\n'
    )
    (task_dir / "instruction.md").write_text("Solve this task.")
    task_digests = compute_task_digests(task_dir)
    harbor_digest = harbor_task_digest(task_dir)

    def make_ref(agent: str, reward: float) -> ControlEvidenceRef:
        job_name = f"{task_id}-{agent}-evidence"
        trial_name = f"{task_id}__{agent}"
        trial_dir = root / "research/evidence/runs" / job_name / trial_name
        trial_dir.mkdir(parents=True, exist_ok=True)
        observed_at = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
        payload = {
            "id": f"{agent}-trial",
            "task_name": task_id,
            "trial_name": trial_name,
            "task_id": {"path": str(task_dir)},
            "config": {"task": {"path": str(task_dir)}, "agent": {"name": agent}},
            "agent_info": {"name": agent, "version": "1.0.0"},
            "verifier_result": {"rewards": {"reward": reward}},
            "finished_at": observed_at.isoformat(),
        }
        lock = {
            "schema_version": 2,
            "task": {
                "name": task_id,
                "version": "1.0.0",
                "type": "local",
                "digest": harbor_digest,
                "path": str(task_dir),
            },
            "agent": {"name": agent},
        }
        result_file = trial_dir / "result.json"
        lock_file = trial_dir / "lock.json"
        result_file.write_text(json.dumps(payload, indent=2))
        lock_file.write_text(json.dumps(lock, indent=2))
        (trial_dir.parent / "lock.json").write_text(
            json.dumps({"harbor": {"version": recorded or "0.21.0"}})
        )
        if current is not None:
            # A Harbor upgrade rewrites the trial lock bytes (new stamp-only
            # fields) while task identity is untouched.
            lock["schema_version"] = 3
            lock_file.write_text(json.dumps(lock, indent=2))
            (trial_dir.parent / "lock.json").write_text(
                json.dumps({"harbor": {"version": current}})
            )
        return ControlEvidenceRef(
            job_name=job_name,
            trial_name=trial_name,
            reward=reward,
            evidence_path=result_file.relative_to(root).as_posix(),
            declared_task_name=task_id,
            evidence_digest=f"sha256:{hashlib.sha256(result_file.read_bytes()).hexdigest()}",
            lock_digest=(
                f"sha256:{hashlib.sha256(lock_file.read_bytes()).hexdigest()}"
                if current is None
                else "sha256:" + "0" * 64
            ),
            observed_at=observed_at,
            task_id=task_id,
            task_version="1.0.0",
            task_digests=task_digests,
            harbor_task_digest=harbor_digest,
            harbor_version=recorded,
        )

    return TaskRegistryRecord(
        schema_version=2,
        task_id=task_id,
        task_family="sample-family",
        version="1.0.0",
        task_path="library/tasks/version-task",
        digests=task_digests,
        source_uri=f"local/{task_id}@1.0.0",
        source_ref="main",
        license="MIT",
        provenance_zone="02-local-evidence",  # type: ignore[arg-type]
        is_synthetic=False,
        limits=TaskLimits(timeout_seconds=1800),
        control_evidence=TaskControlEvidence(
            oracle=make_ref("oracle", 1.0),
            nop=make_ref("nop", 0.0),
        ),
        state="registered",  # type: ignore[arg-type]
        allowed_uses=["measurement", "training"],  # type: ignore[arg-type]
        approved_by="Peter Makhnatch",
        approved_at=datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
    )


def test_control_evidence_declares_harbor_upgrade(tmp_path: Path) -> None:
    from evallab.registry import TaskControlEvidenceError, verify_control_evidence

    record = _write_version_case(tmp_path, "version-task", recorded="0.21.0", current="0.24.0")
    with pytest.raises(TaskControlEvidenceError, match=r"harbor_version_change:0\.21\.0->0\.24\.0"):
        verify_control_evidence(tmp_path, record)


def test_control_evidence_same_version_mismatch_unchanged(tmp_path: Path) -> None:
    from evallab.registry import TaskControlEvidenceError, verify_control_evidence

    record = _write_version_case(tmp_path, "version-task", recorded="0.24.0", current="0.24.0")
    with pytest.raises(TaskControlEvidenceError, match="lock digest mismatch"):
        verify_control_evidence(tmp_path, record)


def test_021_trial_reads_through_atif_process_features() -> None:
    if not REPO_021_JOB.is_dir():
        pytest.skip("0.21 evidence job is not checked out here")
    from evallab.evidence.atif import SUPPORTED_SCHEMA_VERSIONS, project_trial
    from evallab.interpretation.features import trial_features
    from evallab.process_job import _process_trial
    from evallab.results import load_job

    assert "ATIF-v1.8" in SUPPORTED_SCHEMA_VERSIONS
    job = load_job(REPO_021_JOB)
    assert job.harbor_version == "0.21.0"
    assert job.trials, "0.21 fixture job must carry trials"
    trial = job.trials[0]
    projection = project_trial(job, trial)
    assert projection.trajectories, "0.21 ATIF must still project"
    record = _process_trial(trial.path, job.path)
    assert record["trial_name"] == trial.path.name
    features = trial_features(job.path, trial.path)
    assert features["trial"] == trial.path.name
    assert features["job"] == job.path.name
