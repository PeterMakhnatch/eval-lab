from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from evallab.task_workbench import WorkbenchError, compare_quality_audits, run_cli


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def _trial(path: Path, reward: float, resolved: bool) -> None:
    _write(
        path / "result.json",
        {
            "id": str(uuid5(NAMESPACE_URL, str(path))),
            "trial_name": path.name,
            "config": {"job_id": str(uuid5(NAMESPACE_URL, str(path.parent)))},
            "finished_at": "2026-09-08T12:00:00Z",
            "verifier_result": {"rewards": {"reward": reward}},
            "exception_info": None,
        },
    )
    _write(path / "verifier/reward-details.json", {"reward": reward, "resolved": resolved})
    files = {
        "src/product.py": {
            "sha256": hashlib.sha256(b"captured source").hexdigest(),
            "size": 15,
            "uid": 0,
            "gid": 0,
            "mode": "0o644",
        }
    }
    for phase in ("pre", "post"):
        _write(path / f"agent/quality-{phase}-action.json", {"files": files})


@pytest.fixture
def native_pair(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    original, repaired = tmp_path / "original", tmp_path / "repaired"
    _trial(original, 1.0, True)
    _trial(repaired, 0.8, False)
    review, contract = tmp_path / "review.json", tmp_path / "contract.json"
    _write(
        review,
        {
            "comparison": [
                {
                    "arm": "probe",
                    "original_trial": str(original),
                    "repaired_trial": str(repaired),
                    "original_reward": 1.0,
                    "repaired_reward": 1.0,
                    "same_captured_pre_action_and_post_action_state": True,
                    "runtime_identities": {
                        "original": {"image_id": "sha256:" + "a" * 64},
                        "repaired": {"image_id": "sha256:" + "b" * 64},
                    },
                }
            ]
        },
    )
    _write(contract, {"source_findings": str(review), "expected_rewards": {"probe": 1.0}})
    return review, contract, original, repaired


def _compare(pair: tuple[Path, Path, Path, Path]) -> dict:
    return compare_quality_audits(review_result_path=pair[0], repair_contract_path=pair[1])


def test_raw_partial_credit_survives_cached_full_credit(
    native_pair: tuple[Path, Path, Path, Path],
) -> None:
    arm = _compare(native_pair)["arms"]["probe"]
    assert arm["repaired"]["observed_reward"] == 0.8
    assert arm["repaired"]["observed_resolved"] is False
    assert arm["observed_reward_delta"] == pytest.approx(-0.2)
    assert arm["partial_credit"] is True
    assert arm["pairing"]["status"] == "unqualified"
    assert "image_identity_changed" in arm["pairing"]["reasons"]


@pytest.mark.parametrize(
    "record", [None, {}, [], {"id": "present", "finished_at": "done", "verifier_result": {}}]
)
def test_missing_reward_cannot_borrow_diagnostics_or_declaration(
    native_pair: tuple[Path, Path, Path, Path],
    record: object,
) -> None:
    result = native_pair[3] / "result.json"
    if record is None:
        result.unlink()
    else:
        _write(result, record)
    arm = _compare(native_pair)["arms"]["probe"]
    assert arm["repaired"]["observed_reward"] is None
    assert arm["observed_reward_delta"] is None


def test_recorded_state_drift_overrules_declared_equality(
    native_pair: tuple[Path, Path, Path, Path],
) -> None:
    review = json.loads(native_pair[0].read_text())
    row = review["comparison"][0]
    row["runtime_identities"]["repaired"] = row["runtime_identities"]["original"]
    _write(native_pair[0], review)
    path = native_pair[3] / "agent/quality-post-action.json"
    changed = json.loads(path.read_text())
    changed["files"]["src/product.py"]["sha256"] = hashlib.sha256(b"changed").hexdigest()
    _write(path, changed)
    arm = _compare(native_pair)["arms"]["probe"]
    assert arm["captured_state"]["post_action_files_match"] is False
    assert arm["pairing"]["status"] == "unqualified"
    assert "post_action_state_changed" in arm["pairing"]["reasons"]


def test_failed_execution_retains_observed_reward_but_has_no_delta(
    native_pair: tuple[Path, Path, Path, Path],
) -> None:
    path = native_pair[3] / "result.json"
    raw = json.loads(path.read_text())
    raw["exception_info"] = {"exception_type": "VerifierTimeoutError"}
    _write(path, raw)
    arm = _compare(native_pair)["arms"]["probe"]
    assert arm["repaired"]["observed_reward"] == 0.8
    assert arm["repaired"]["execution_status"] == "failed"
    assert arm["observed_reward_delta"] is None


def test_repair_contract_cannot_bind_another_review(
    native_pair: tuple[Path, Path, Path, Path],
) -> None:
    _write(native_pair[1], {"source_findings": str(native_pair[0].with_name("other.json"))})
    with pytest.raises(WorkbenchError):
        _compare(native_pair)


def test_infrastructure_uses_original_exception_not_inferred_default(
    native_pair: tuple[Path, Path, Path, Path],
) -> None:
    root = native_pair[0].parent
    failure = root / "failed-job" / "failed-trial"
    _write(
        failure / "result.json",
        {
            "id": str(uuid5(NAMESPACE_URL, "failed")),
            "config": {"job_id": "failed"},
            "verifier_result": None,
            "exception_info": {"exception_type": "VerifierTimeoutError"},
        },
    )
    _write(
        root / "evidence/instrumentation-failures.json",
        {
            "classification": "instrumentation",
            "failures": [{"job": str(failure.parent)}],
        },
    )
    review = json.loads(native_pair[0].read_text())
    review["evidence_root"] = str(root / "evidence")
    _write(native_pair[0], review)
    report = _compare(native_pair)
    assert report["populations"]["control_trials_count"] == 2
    assert report["populations"]["infrastructure_failures_count"] == 1
    assert (
        report["infrastructure_failures"][0]["observed"]["exception_info"]["exception_type"]
        == "VerifierTimeoutError"
    )
    assert report["infrastructure_failures"][0]["observed"]["observed_reward"] is None
    (failure / "result.json").unlink()
    missing = _compare(native_pair)
    assert missing["populations"]["infrastructure_failures_count"] == 0
    assert missing["infrastructure_failures"][0]["observed"]["exception_info"] is None


def test_native_mode_rejects_mixed_or_incomplete_arguments(
    native_pair: tuple[Path, Path, Path, Path],
) -> None:
    review, contract, original, repaired = native_pair
    assert run_cli(["audit-compare", "--review-result", str(review)]) == 2
    assert (
        run_cli(
            [
                "audit-compare",
                str(original),
                str(repaired),
                "--review-result",
                str(review),
                "--repair-contract",
                str(contract),
            ]
        )
        == 2
    )
