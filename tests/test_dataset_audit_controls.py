from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from evallab.dataset_audit import _generic_finalize
from evallab.dataset_audit_contracts import AuditObservation, AuditRecord, AuditTask
from evallab.dataset_audit_execution import control_observation, run_history_oracle
from evallab.registry import harbor_task_digest, task_directory_digest


def _native_control(root: Path, reward) -> AuditTask:
    trial = root / "example__trial"
    trial.mkdir(parents=True)
    (root / "result.json").write_text(json.dumps({
        "id": "job", "n_total_trials": 1, "stats": {}, "finished_at": "2026-01-01T00:00:01Z",
    }))
    (trial / "result.json").write_text(json.dumps({
        "id": "trial", "trial_name": trial.name, "task_name": "example/task",
        "finished_at": "2026-01-01T00:00:01Z", "verifier_result": {"rewards": {"reward": reward}},
    }))
    (trial / "config.json").write_text(json.dumps({"agent": {"name": "oracle"}}))
    (trial / "lock.json").write_text(json.dumps({"task": {"digest": "b" * 64}}))
    return AuditTask(
        dataset_id="example", task_id="example/task", task_name="example/task",
        source_uri="test://example/task", package_digest="sha256:" + "a" * 64,
        harbor_digest="sha256:" + "b" * 64,
    )


@pytest.mark.parametrize("reward", [True, None, "1", float("nan"), float("inf")])
def test_invalid_native_grades_remain_unscored_and_serializable(tmp_path: Path, reward):
    task = _native_control(tmp_path, reward)
    observation = control_observation(task, "oracle", tmp_path)
    assert observation.status == "failed"
    assert observation.facts["reward"] is None
    assert "label" not in observation.facts
    assert json.loads(json.dumps(observation.model_dump(mode="json"), allow_nan=False))["status"] == "failed"


def test_completed_numeric_oracle_grade_is_bound(tmp_path: Path):
    task = _native_control(tmp_path, 1.0)
    observation = control_observation(task, "oracle", tmp_path)
    assert observation.status == "executed"
    assert observation.facts["label"] == "oracle:pass"
    other = task.model_copy(update={"harbor_digest": "sha256:" + "c" * 64})
    assert control_observation(other, "oracle", tmp_path).status == "failed"


def test_control_cannot_impersonate_an_exploit_and_foreign_probe_cannot_score(tmp_path: Path):
    from evallab.dataset_audit_execution import exploit_observation

    task = _native_control(tmp_path, 1.0)
    assert exploit_observation(task, tmp_path).status == "failed"
    trial = tmp_path / "example__trial"
    (trial / "config.json").write_text(json.dumps({"agent": {"name": "terminus-2"}}))
    actual = exploit_observation(task, tmp_path)
    assert actual.status == "executed"
    assert actual.facts["verdict"] == "cracked"
    foreign = task.model_copy(update={"harbor_digest": "sha256:" + "c" * 64})
    assert exploit_observation(foreign, tmp_path).status == "failed"


@pytest.mark.parametrize("native_matches", [True, False])
def test_history_control_must_bind_native_variant_before_parent_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_matches: bool,
):
    from evallab import dataset_audit_execution, dataset_audit_oracle

    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "task.toml").write_text('[task]\nname = "example/task"\n')
    (parent / "instruction.md").write_text("Repair the greeting.")
    variant = tmp_path / "reference"
    shutil.copytree(parent, variant)
    (variant / "solution").mkdir()
    (variant / "solution/solve.sh").write_text("#!/bin/sh\nprintf hello > /app/answer\n")
    task = AuditTask(
        dataset_id="example", task_id="example/task", task_name="example/task",
        source_uri="test://example/task", path=parent,
        package_digest=task_directory_digest(parent), harbor_digest=harbor_task_digest(parent),
    )
    subject = task.model_copy(update={
        "path": variant, "package_digest": task_directory_digest(variant),
        "harbor_digest": harbor_task_digest(variant),
    })
    job = tmp_path / "job"
    _native_control(job, 1.0)
    lock = job / "example__trial/lock.json"
    lock.write_text(json.dumps({"task": {"digest": (
        subject.harbor_digest if native_matches else "sha256:" + "f" * 64
    )}}))
    extraction = AuditObservation(
        stage="oracle", status="recorded",
        facts={"base": "a" * 40, "oracle_variant_digest": subject.package_digest},
    )
    monkeypatch.setattr(dataset_audit_oracle, "prepare_history_oracle", lambda *a, **k: (subject, extraction))
    monkeypatch.setattr(dataset_audit_execution, "run_local_control", lambda *a, **k: job)
    observed = run_history_oracle(task, repo_root=tmp_path, output_dir=tmp_path / "out")
    nop = AuditObservation(stage="nop", status="executed", facts={
        "agent": "nop", "completed": True, "exception": None, "reward": 0.0, "label": "sound",
        "package_digest": task.package_digest, "harbor_digest": task.harbor_digest,
    })
    routed = _generic_finalize(AuditRecord(task=task, stages={"oracle": observed, "nop": nop}))
    assert routed.verdict == ("keep" if native_matches else "unknown")
    assert observed.status == ("executed" if native_matches else "failed")
    assert observed.facts["native_control_package_digest"] == subject.package_digest
    assert observed.facts["native_control_harbor_digest"] == (
        subject.harbor_digest if native_matches else "sha256:" + "f" * 64
    )
    assert observed.facts["history_oracle"]["base"] == "a" * 40


def test_history_grade_failure_retains_extraction_evidence_without_a_control_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from evallab import dataset_audit_execution, dataset_audit_oracle
    from evallab.dataset_audit_sources import file_source

    task = _native_control(tmp_path / "unused-job", 1.0)
    patch = tmp_path / "solution.patch"
    patch.write_text("retained source reference")
    extraction = AuditObservation(
        stage="oracle", status="recorded", facts={"base": "a" * 40},
        sources=(file_source(patch, binding="history-source-patch"),),
    )

    def failed_control(*args, **kwargs):
        raise RuntimeError("native execution failed before grading")

    monkeypatch.setattr(dataset_audit_oracle, "prepare_history_oracle", lambda *a, **k: (task, extraction))
    monkeypatch.setattr(dataset_audit_execution, "run_local_control", failed_control)
    observed = run_history_oracle(task, repo_root=tmp_path, output_dir=tmp_path / "out")
    assert observed.status == "failed"
    assert observed.sources == extraction.sources
    assert observed.facts["history_oracle"]["base"] == "a" * 40
    assert not {"reward", "completed", "label"} & observed.facts.keys()
