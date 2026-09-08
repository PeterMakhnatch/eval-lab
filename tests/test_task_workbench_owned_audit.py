"""Owned-format quality-audit evidence loading (metadata.json + observed-summary.json).

Portable fixtures isolate real failures (unbound vs mismatched vs verified) from
otherwise valid evidence; real consumer paths are covered behind skip guards so
the suite stays green where the cohort checkout is absent.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from evallab.task_workbench import load_quality_audit_evidence

CONSUMER = Path(
    "/Users/petermakhnatch/Developer/harbor-rl-exploration/artifacts/quality-cohort-20260908"
)
LANE = Path("/Users/petermakhnatch/Developer/harbor-rl-exploration/lanes/quality/cohort-20260908")

needs_consumer = pytest.mark.skipif(
    not (CONSUMER / "before-owned" / "000003" / "metadata.json").is_file(),
    reason="consumer cohort fixtures absent",
)
needs_lane = pytest.mark.skipif(
    not (LANE / "runner.py").is_file(), reason="lane source bytes absent"
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _manifest_entry(data: bytes) -> dict[str, Any]:
    return {"sha256": _sha(data), "size": len(data), "copied_mode": "0o644"}


def _write_owned_audit(
    root: Path,
    *,
    task_id: str = "task_000007",
    short_id: str = "000007",
    phase: str = "before-owned",
    verifier_files: dict[str, bytes] | None = None,
    package_files: dict[str, bytes] | None = None,
    arms: tuple[str, ...] = ("oracle", "nop"),
) -> Path:
    """Write a minimal portable owned-format audit directory (read-only fixture)."""
    audit = root / phase / short_id
    verifier_files = (
        verifier_files
        if verifier_files is not None
        else {
            "test.sh": b"#!/bin/sh\n",
            "test_state.py": b"def test_x():\n    pass\n",
        }
    )
    package_files = (
        package_files
        if package_files is not None
        else {
            "task.toml": b"[task]\n",
            "tests/test_state.py": verifier_files["test_state.py"],
        }
    )
    runtime_files = {"task_file/inputs/a.json": b'{"k": 1}\n'}
    metadata = {
        "task_id": short_id,
        "phase": phase,
        "image_id": "sha256:" + "0" * 64,
        "declaration": {
            "task_id": task_id,
            "source_path": "/nonexistent/claim/task_000007",
            "arms": {
                "oracle": {
                    "action_command": ["bash", "/solution/solve.sh"],
                    "declared_validity": "reference_unadjudicated",
                    "expected_reward": "1",
                    "rationale": "reference",
                },
                "nop": {
                    "action_command": ["true"],
                    "declared_validity": "invalid",
                    "expected_reward": "0",
                    "rationale": "no-op",
                },
            },
        },
        "source_task_manifest": {rel: _manifest_entry(data) for rel, data in package_files.items()},
        "controls_manifest": {"control.py": _manifest_entry(b"# control\n")},
        "verifier_manifest": {rel: _manifest_entry(data) for rel, data in verifier_files.items()},
        "runner_sha256": _sha(b"# runner\n"),
        "ownership_runner_sha256": _sha(b"# ownership\n"),
        "runtime_environment_manifest": {
            rel: _manifest_entry(data) for rel, data in runtime_files.items()
        },
        "verifier_task_file_readonly": False,
        "verification_mode": "fixture",
    }
    observed = {
        "oracle": {
            "arm": "oracle",
            "reward": "1",
            "action_exit": 0,
            "verifier_exit": 0,
            "ctrf_summary": {"tests": 2, "passed": 2, "failed": 0},
            "output_digests": {},
            "outcome": "accepted",
            "elapsed_seconds": 0.5,
            "declared_validity": "reference_unadjudicated",
        },
        "nop": {
            "arm": "nop",
            "reward": "0",
            "action_exit": 0,
            "verifier_exit": 0,
            "ctrf_summary": {"tests": 2, "passed": 0, "failed": 2},
            "output_digests": {},
            "outcome": "rejected",
            "elapsed_seconds": 0.4,
            "declared_validity": "invalid",
        },
    }
    _json(audit / "metadata.json", metadata)
    _json(audit / "observed-summary.json", observed)
    for arm in arms:
        arm_dir = audit / arm
        _json(arm_dir / "observed.json", observed[arm])
        _json(arm_dir / "digests.json", {"inputs": {}, "outputs": {}})
    return audit


def _write_source_root(
    root: Path,
    *,
    task_id: str = "task_000007",
    verifier_files: dict[str, bytes] | None = None,
    package_files: dict[str, bytes] | None = None,
) -> Path:
    lane = root / "lane"
    verifier_files = (
        verifier_files
        if verifier_files is not None
        else {
            "test.sh": b"#!/bin/sh\n",
            "test_state.py": b"def test_x():\n    pass\n",
        }
    )
    package_files = (
        package_files
        if package_files is not None
        else {
            "task.toml": b"[task]\n",
            "tests/test_state.py": verifier_files["test_state.py"],
        }
    )
    for rel, data in verifier_files.items():
        target = lane / "tasks" / task_id / "offline-tests" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    for rel, data in package_files.items():
        target = lane / "tasks" / task_id / "task" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (lane / "tasks" / task_id / "controls" / "control.py").parent.mkdir(parents=True, exist_ok=True)
    (lane / "tasks" / task_id / "controls" / "control.py").write_bytes(b"# control\n")
    runtime_target = (
        lane / "tasks" / task_id / "runtime-environment" / "task_file" / "inputs" / "a.json"
    )
    runtime_target.parent.mkdir(parents=True, exist_ok=True)
    runtime_target.write_bytes(b'{"k": 1}\n')
    (lane / "runner.py").write_bytes(b"# runner\n")
    (lane / "ownership_runner.py").write_bytes(b"# ownership\n")
    return lane


def _walk_keys(value: Any) -> Any:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def test_portable_owned_loads_without_summary_and_binds(tmp_path: Path) -> None:
    audit = _write_owned_audit(tmp_path / "consumer")
    lane = _write_source_root(tmp_path / "src")
    evidence = load_quality_audit_evidence(audit, source_root=lane)
    assert evidence["evidence_format"] == "quality_owned"
    assert evidence["origin"] == "external_quality_audit"
    assert evidence["run_uuid"] is None
    assert set(evidence["arms"]) == {"oracle", "nop"}
    oracle, nop = evidence["arms"]["oracle"], evidence["arms"]["nop"]
    # Neutral observed reward: completion reflects raw exits, not the annotation.
    assert oracle["observed_reward"] == 1.0
    assert nop["observed_reward"] == 0.0
    assert oracle["execution_status"] == "completed"
    assert nop["execution_status"] == "completed"
    assert oracle["observed_outcome"] == "accepted"
    assert nop["observed_outcome"] == "rejected"
    assert oracle["declared_label"] == "reference_unadjudicated"
    assert oracle["declared_expected_reward"] == 1.0
    assert nop["declared_label"] == "invalid"
    binding = evidence["provenance_binding"]
    assert binding["status"] == "verified"
    assert binding["package_snapshot_status"] == "verified"
    assert binding["executed_verifier_status"] == "verified"
    assert binding["recorded_verifier_sha256"] == binding["executed_verifier_sha256"]
    assert binding["executed_verifier_path"].endswith("offline-tests")
    assert binding["runner_binding"]["status"] == "verified"
    assert binding["ownership_runner_binding"]["status"] == "verified"
    assert binding["controls_binding"]["status"] == "verified"
    assert binding["runtime_environment_binding"]["status"] == "verified"


def test_portable_missing_source_identity_stays_unbound(tmp_path: Path) -> None:
    audit = _write_owned_audit(tmp_path / "consumer")
    for source_root in (None, tmp_path / "empty-lane"):
        evidence = load_quality_audit_evidence(audit, source_root=source_root)
        # Observations still render without companion source bytes.
        assert set(evidence["arms"]) == {"oracle", "nop"}
        assert evidence["arms"]["oracle"]["observed_reward"] == 1.0
        binding = evidence["provenance_binding"]
        assert binding["status"] == "unbound"
        assert binding["package_snapshot_status"] == "unbound"
        assert binding["executed_verifier_status"] == "unbound"
        assert binding["executed_verifier_path"] is None
        assert binding["executed_verifier_sha256"] is None
        assert binding["runner_binding"]["status"] == "unbound"
        assert binding["controls_binding"]["status"] == "unbound"
        assert binding["runtime_environment_binding"]["status"] == "unbound"


def test_portable_wrong_verifier_bytes_are_mismatched_not_unbound(
    tmp_path: Path,
) -> None:
    recorded = {"test.sh": b"#!/bin/sh\n", "test_state.py": b"recorded\n"}
    audit = _write_owned_audit(tmp_path / "consumer", verifier_files=recorded)
    # Same names retained, different bytes: names alone never bind.
    lane = _write_source_root(
        tmp_path / "src",
        verifier_files={"test.sh": b"#!/bin/sh\n", "test_state.py": b"tampered\n"},
    )
    evidence = load_quality_audit_evidence(audit, source_root=lane)
    binding = evidence["provenance_binding"]
    assert binding["executed_verifier_status"] == "mismatched"
    assert binding["status"] == "mismatched"
    assert binding["executed_verifier_path"] is None


def test_portable_loader_claims_no_host_ownership(tmp_path: Path) -> None:
    audit = _write_owned_audit(tmp_path / "consumer")
    lane = _write_source_root(tmp_path / "src")
    evidence = load_quality_audit_evidence(audit, source_root=lane)
    assert "uid" not in set(_walk_keys(evidence))
    assert "gid" not in set(_walk_keys(evidence))
    for arm in evidence["arms"].values():
        assert arm["runtime_metadata_required"] is True
        # Initial runtime paths from the recorded manifest, task_file/ stripped.
        assert arm["runtime_input_paths"] == ["inputs/a.json"]


@needs_consumer
def test_real_before_after_owned_expose_complete_arm_union() -> None:
    expectations = {
        ("before-owned", "000003"): {
            "oracle",
            "valid_alternative",
            "nop",
            "invalid_value",
            "invalid_security",
            "markdown_heading",
            "fabricated_markdown",
            "truncated_markdown",
            "header_only_audio",
            "tamper_source_and_output",
            "tamper_source_format_only",
        },
        ("after-owned", "000003"): {
            "oracle",
            "valid_alternative",
            "nop",
            "invalid_value",
            "invalid_security",
            "markdown_heading",
            "fabricated_markdown",
            "truncated_markdown",
            "header_only_audio",
            "tamper_source_and_output",
            "tamper_source_format_only",
        },
        ("before-owned", "000011"): {
            "oracle",
            "reverse_email_context_keys",
            "nop",
            "wrong_report_text",
            "swap_summaries",
            "reverse_root_keys",
            "missing_required_key",
            "extra_email_key",
        },
        ("after-owned", "000011"): {
            "oracle",
            "reverse_email_context_keys",
            "nop",
            "wrong_report_text",
            "swap_summaries",
            "reverse_root_keys",
            "missing_required_key",
            "extra_email_key",
        },
    }
    for (phase, short_id), expected_arms in expectations.items():
        evidence = load_quality_audit_evidence(CONSUMER / phase / short_id)
        assert evidence["evidence_format"] == "quality_owned"
        assert evidence["origin"] == "external_quality_audit"
        assert evidence["run_uuid"] is None
        assert set(evidence["arms"]) == expected_arms
        oracle, nop = evidence["arms"]["oracle"], evidence["arms"]["nop"]
        assert oracle["observed_reward"] == 1.0
        assert nop["observed_reward"] == 0.0
        assert oracle["execution_status"] == "completed"
        assert oracle["observed_outcome"] == "accepted"
        assert nop["observed_outcome"] == "rejected"
        assert nop["declared_label"] == "invalid"
        # Missing companion source keeps identity unbound without losing arms.
        assert evidence["provenance_binding"]["status"] == "unbound"


@needs_consumer
@needs_lane
def test_real_source_root_binds_full_verifier_manifest() -> None:
    primaries = {
        ("before-owned", "000003"): "offline-tests",
        ("before-owned", "000011"): "offline-tests",
        ("after-owned", "000003"): "repaired-tests",
        ("after-owned", "000011"): "repaired-tests",
    }
    for (phase, short_id), primary in primaries.items():
        evidence = load_quality_audit_evidence(CONSUMER / phase / short_id, source_root=LANE)
        binding = evidence["provenance_binding"]
        assert binding["package_snapshot_status"] == "verified"
        assert binding["executed_verifier_status"] == "verified"
        assert binding["status"] == "verified"
        assert binding["executed_verifier_path"].endswith(primary)
        assert binding["recorded_verifier_sha256"] == binding["executed_verifier_sha256"]
        assert set(binding["executed_verifier_manifest"]) == set(
            binding["recorded_verifier_manifest"]
        )
        assert binding["runner_binding"]["status"] == "verified"
        assert binding["ownership_runner_binding"]["status"] == "verified"
        assert binding["controls_binding"]["status"] == "verified"
        assert binding["runtime_environment_binding"]["status"] == "verified"
