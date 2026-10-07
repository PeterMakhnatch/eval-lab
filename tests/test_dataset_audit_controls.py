from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.dataset_audit_contracts import AuditTask
from evallab.dataset_audit_execution import control_observation


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
