"""Offline contracts against the supported Harbor release, using retained fixtures."""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from harbor.models.task.config import NetworkMode, TaskConfig, VerifierEnvironmentMode
from harbor.models.trial.result import TrialResult
from pydantic import ValidationError, create_model

from evallab.results import duration_seconds, load_trial

FIXTURES = Path(__file__).parent / "fixtures"
RECORDED_TRIAL = FIXTURES / "operability/complete/jobs/operability-join/join-trial/result.json"

# Historical recordings omit newer custody fields. Validate the fields the
# result reader consumes; do not invent a native task identity or checksum.
ConsumedTrial = create_model("ConsumedTrial", **{
    name: (TrialResult.model_fields[name].annotation, TrialResult.model_fields[name])
    for name in ("id", "task_name", "trial_name", "verifier_result", "started_at", "finished_at")
})


def test_recorded_task_preserves_no_network_and_separate_verifier_boundary():
    payload = tomllib.loads((FIXTURES / "task_workbench/valid/task.toml").read_text())
    task = TaskConfig.model_validate(payload)
    assert task.environment.network_mode == NetworkMode.NO_NETWORK
    assert task.verifier.environment_mode == VerifierEnvironmentMode.SEPARATE
    payload["environment"]["allowed_hosts"] = ["example.com"]
    with pytest.raises(ValidationError):
        TaskConfig.model_validate(payload)


@pytest.mark.parametrize("verifier,expected", [
    ({"rewards": {"reward": 1.0}}, 1.0),
    ({"rewards": {"reward": 0}}, 0.0),
    ({"rewards": None}, None),
    (None, None),
])
def test_pinned_result_and_verifier_contract_keep_unavailable_distinct_from_zero(
    tmp_path, verifier, expected,
):
    recorded = json.loads(RECORDED_TRIAL.read_bytes())
    recorded["verifier_result"] = verifier
    native = ConsumedTrial.model_validate(recorded)
    (tmp_path / "result.json").write_text(native.model_dump_json())
    trial = load_trial(tmp_path)
    assert trial.primary_reward == expected
    assert duration_seconds(trial.result["started_at"], trial.result["finished_at"]) == 1.0


def test_pinned_verifier_rejects_a_reward_payload_that_cannot_be_numeric():
    recorded = json.loads(RECORDED_TRIAL.read_bytes())
    recorded["verifier_result"] = {"rewards": {"reward": {"unavailable": True}}}
    with pytest.raises(ValidationError):
        ConsumedTrial.model_validate(recorded)
