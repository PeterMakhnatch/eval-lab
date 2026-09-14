from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evallab.lego_capture import CaptureAssessment, assess_file, main


def _record() -> dict[str, object]:
    return {
        "session_id": "synthetic-session",
        "traj_acc_ids": [1, 2, 3, 4],
        "initial_prompt_token_len": 2,
        "traj_response_mask": [1, 0],
        "traj_response_logprobs": [0.0, 0.0],
        "disable_proxy_trajectory": False,
        "trajectory_logprobs_error": None,
        "diagnostic_logprobs_complete": None,
        "context_overflow": False,
        "min_global_steps": 5,
        "max_global_steps": 6,
    }


def _save(tmp_path: Path, record: dict[str, object]) -> Path:
    path = tmp_path / "capture.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _codes(result: CaptureAssessment) -> set[str]:
    issues = list(result.issues)
    if result.structure:
        issues.extend(result.structure.issues)
    if result.weight:
        issues.extend(result.weight.issues)
    return {issue.code for issue in issues}


def test_zero_probability_and_excluded_slots_preserve_boundaries(tmp_path: Path) -> None:
    path = _save(tmp_path, _record())
    original = path.read_bytes()
    result = assess_file(path, record_origin="cpu_proxy_session_synthetic")
    assert result.assessment_passed
    assert result.structure is not None
    assert result.structure.trained_response_tokens == 1
    assert result.structure.excluded_response_tokens == 1
    assert result.structure.routing_present is False
    assert result.structure.routing_rows is None
    assert result.session_id == "synthetic-session"
    assert result.declared_provenance.cli_record_origin == "cpu_proxy_session_synthetic"
    assert result.declared_provenance.verified is False
    assert result.training_authorization == "not_assessed"
    assert result.source_sha256 == "sha256:" + hashlib.sha256(original).hexdigest()
    assert path.read_bytes() == original


def test_overflow_preserves_valid_partial_capture_and_absent_routing(tmp_path: Path) -> None:
    record = _record()
    record.update({"context_overflow": True, "traj_response_routing": []})
    result = assess_file(_save(tmp_path, record))
    assert result.assessment_passed
    assert result.context_overflow is True
    assert result.structure is not None
    assert result.structure.trained_response_tokens == 1
    assert not result.structure.routing_present


@pytest.mark.parametrize("value", [None, {}, True, float("nan"), float("inf")])
def test_excluded_logprobs_must_survive_numeric_ingestion(tmp_path: Path, value: object) -> None:
    record = _record()
    record["traj_response_logprobs"] = [0.0, value]
    result = assess_file(_save(tmp_path, record))
    assert not result.assessment_passed
    assert "excluded_logprob_invalid" in _codes(result)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True, 0.1, None])
def test_trained_logprobs_cannot_be_coerced(tmp_path: Path, value: object) -> None:
    record = _record()
    record["traj_response_logprobs"] = [value, 0.0]
    result = assess_file(_save(tmp_path, record))
    assert not result.assessment_passed
    assert "trained_logprob_invalid" in _codes(result)


@pytest.mark.parametrize(
    ("name", "value", "reason"),
    [
        ("traj_acc_ids", [1, True, 3, 4], "token_id_invalid"),
        ("traj_acc_ids", [1, -1, 3, 4], "token_id_invalid"),
        ("traj_acc_ids", [1, 2.0, 3, 4], "token_id_invalid"),
        ("traj_response_mask", [True, 0], "response_mask_nonbinary"),
        ("traj_response_mask", [2, 0], "response_mask_nonbinary"),
        ("traj_response_mask", [1.0, 0], "response_mask_nonbinary"),
        ("traj_response_mask", [0, 0], "no_trained_response_tokens"),
        ("traj_response_logprobs", [], "trained_logprob_invalid"),
        ("traj_response_logprobs", [0.0], "logprob_alignment_mismatch"),
        ("initial_prompt_token_len", 1, "response_split_mismatch"),
        ("initial_prompt_token_len", 0, "prompt_empty"),
        ("initial_prompt_token_len", True, "prompt_length_invalid"),
        ("initial_prompt_token_len", 5, "prompt_length_exceeds_tokens"),
        ("traj_response_routing", [None], "routing_alignment_mismatch"),
        ("traj_response_routing", False, "routing_malformed"),
        ("disable_proxy_trajectory", "false", "required_boolean_invalid"),
        ("context_overflow", None, "required_boolean_invalid"),
        ("trajectory_logprobs_error", False, "capture_error_field_invalid"),
        ("diagnostic_logprobs_complete", 0, "diagnostic_flag_invalid"),
        ("diagnostic_logprobs_complete", False, "diagnostic_recomputation"),
        ("sampling_lineage", "diagnostic_recomputation", "diagnostic_recomputation"),
    ],
)
def test_malformed_evidence_fails_closed(
    tmp_path: Path, name: str, value: object, reason: str
) -> None:
    record = _record()
    record[name] = value
    result = assess_file(_save(tmp_path, record))
    assert not result.assessment_passed
    assert reason in _codes(result)


@pytest.mark.parametrize(
    "name",
    [
        "disable_proxy_trajectory",
        "context_overflow",
        "trajectory_logprobs_error",
        "diagnostic_logprobs_complete",
    ],
)
def test_missing_required_status_never_defaults_to_healthy(tmp_path: Path, name: str) -> None:
    record = _record()
    del record[name]
    assert not assess_file(_save(tmp_path, record)).assessment_passed


def test_disabled_error_and_empty_abort_have_bounded_reasons(tmp_path: Path) -> None:
    record = _record()
    record.update(
        {
            "traj_acc_ids": [1, 2],
            "traj_response_mask": [],
            "traj_response_logprobs": [],
            "disable_proxy_trajectory": True,
            "trajectory_logprobs_error": "private-source-text" * 1000,
        }
    )
    result = assess_file(_save(tmp_path, record))
    assert {"proxy_trajectory_disabled", "capture_logprobs_error", "response_empty"} <= _codes(
        result
    )
    assert "private-source-text" not in repr(result)
    record.update({"disable_proxy_trajectory": False, "trajectory_logprobs_error": None})
    result = assess_file(_save(tmp_path, record))
    assert not result.assessment_passed
    assert "response_empty" in _codes(result)


@pytest.mark.parametrize(
    ("minimum", "maximum", "status", "reason"),
    [
        (None, None, "missing", "weight_span_missing"),
        (5, None, "malformed", "weight_span_malformed"),
        (True, 6, "malformed", "weight_span_malformed"),
        (-1, 6, "malformed", "weight_span_malformed"),
        (7, 6, "reversed", "weight_span_reversed"),
        (5, 11, "complete", "weight_span_future"),
        (4, 10, "complete", "weight_span_stale"),
    ],
)
def test_weight_policy_checks_oldest_and_newest_declared_steps(
    tmp_path: Path, minimum: object, maximum: object, status: str, reason: str
) -> None:
    record = _record()
    record.update({"min_global_steps": minimum, "max_global_steps": maximum, "dispatch_step": 10})
    result = assess_file(_save(tmp_path, record), learner_step=10, max_weight_lag=5)
    assert result.weight is not None
    assert result.weight.span_status == status
    assert result.weight.policy_result == "rejected"
    assert reason in _codes(result)
    assert not result.assessment_passed


def test_weight_boundary_and_no_implicit_dispatch_fallback(tmp_path: Path) -> None:
    record = _record()
    record.update({"max_global_steps": 10, "dispatch_step": 100})
    result = assess_file(_save(tmp_path, record), learner_step=10, max_weight_lag=5)
    assert result.assessment_passed
    assert result.weight is not None
    assert result.weight.oldest_weight_lag == 5
    record.pop("min_global_steps")
    record.pop("max_global_steps")
    result = assess_file(_save(tmp_path, record))
    assert result.weight is not None
    assert result.weight.span_status == "missing"
    assert result.weight.policy_result == "not_requested"
    assert result.weight.min_global_steps is None


def test_live_declaration_cannot_upgrade_or_hide_diagnostics(tmp_path: Path) -> None:
    record = _record()
    path = _save(tmp_path, record)
    result = assess_file(path, record_origin="live_trial")
    assert result.declared_provenance.verified is False
    assert result.training_authorization == "not_assessed"
    record["record_origin"] = "diagnostic_recompute"
    result = assess_file(_save(tmp_path, record), record_origin="live_trial")
    assert not result.assessment_passed
    assert {"record_origin_conflict", "diagnostic_recomputation"} <= _codes(result)
    assert result.declared_provenance.source_record_origin == "diagnostic_recompute"


def test_cli_reports_all_files_and_hashes_malformed_bytes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = _save(tmp_path, _record())
    bad = tmp_path / "duplicate.json"
    bad.write_bytes(b'{"disable_proxy_trajectory":true,"disable_proxy_trajectory":false}')
    missing = tmp_path / "missing.json"
    assert main([str(good), str(bad), str(missing)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["passed"] == 1
    assert report["summary"]["rejected"] == 2
    assert report["assessments"][1]["issues"][0]["code"] == "source_json_invalid"
    assert (
        report["assessments"][1]["source_sha256"]
        == "sha256:" + hashlib.sha256(bad.read_bytes()).hexdigest()
    )
    assert report["assessments"][2]["issues"][0]["code"] == "source_read_error"
    with pytest.raises(SystemExit) as exc:
        main([str(good), "--learner-step", "10"])
    assert exc.value.code == 2
