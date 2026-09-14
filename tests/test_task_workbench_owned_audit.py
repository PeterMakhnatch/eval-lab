"""Portable regressions for owned-format quality evidence and provenance."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from evallab.task_workbench import compare_quality_audits, load_quality_audit_evidence, run_cli


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
        _json(arm_dir / "result.json", observed[arm])
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


@pytest.mark.parametrize("command", ["audit-evidence", "experiment"])
def test_missing_owned_verifier_manifest_keeps_default_text_inspectable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    audit = _write_owned_audit(tmp_path / "consumer")
    lane = _write_source_root(tmp_path / "src")
    metadata_path = audit / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    del metadata["verifier_manifest"]
    _json(metadata_path, metadata)
    evidence = load_quality_audit_evidence(audit, source_root=lane)
    assert evidence["provenance_binding"]["recorded_verifier_sha256"] is None
    assert evidence["provenance_binding"]["status"] == "mismatched"
    args = [command, str(audit)] if command == "audit-evidence" else [
        command, "--audit-dir", str(audit)
    ]
    assert run_cli([*args, "--repo-root", str(tmp_path), "--source-root", str(lane)]) == 0
    text = capsys.readouterr().out
    assert "MISMATCHED" in text
    assert "oracle" in text and "reward=1.0" in text
    assert "nop" in text and "reward=0.0" in text


def test_original_result_overrides_cache_and_cannot_be_replaced(tmp_path: Path) -> None:
    audit = _write_owned_audit(tmp_path / "consumer")
    result = audit / "oracle" / "result.json"
    _json(result, {"arm": "oracle", "reward": "0", "action_exit": 0, "verifier_exit": 0})
    evidence = load_quality_audit_evidence(audit)
    assert evidence["arms"]["oracle"]["observed_reward"] == 0
    result.unlink()
    missing = load_quality_audit_evidence(audit)["arms"]["oracle"]
    assert missing["execution_status"] == "missing"
    assert missing["observed_reward"] is None
    _json(result, {})
    incomplete = load_quality_audit_evidence(audit)["arms"]["oracle"]
    assert incomplete["execution_status"] == "unassessed"
    assert incomplete["observed_reward"] is None


def test_unrecorded_verifier_sidecar_prevents_binding(tmp_path: Path) -> None:
    audit = _write_owned_audit(tmp_path / "consumer")
    lane = _write_source_root(tmp_path / "src")
    (lane / "tasks/task_000007/offline-tests/extra.json").write_text("{}")
    evidence = load_quality_audit_evidence(audit, source_root=lane)
    assert evidence["provenance_binding"]["executed_verifier_status"] == "mismatched"


@pytest.mark.parametrize("changed_path", ["test.sh", "helper.py"])
def test_owned_verifier_bundle_change_requires_full_declaration(
    tmp_path: Path, changed_path: str
) -> None:
    before_files = {
        "test_state.py": b"from helper import check\n",
        "test.sh": b"python test_state.py\n",
        "helper.py": b"def check(): return True\n",
    }
    after_files = {**before_files, changed_path: b"changed executed member\n"}
    before = _write_owned_audit(tmp_path / "consumer", verifier_files=before_files)
    after = _write_owned_audit(
        tmp_path / "consumer", phase="after-owned", verifier_files=after_files
    )
    lane = _write_source_root(tmp_path / "source", verifier_files=before_files)
    for rel, data in after_files.items():
        target = lane / "tasks/task_000007/repaired-tests" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    report = compare_quality_audits(before, after, source_root=lane)
    assert report["before"]["provenance_binding"]["executed_verifier_status"] == "verified"
    assert report["after"]["provenance_binding"]["executed_verifier_status"] == "verified"
    assert report["experiment_axis"]["status"] == "undeclared_verifier_change"
    assert "undeclared_verifier_change" in report["arms"]["oracle"]["pairing"]["reasons"]
    declaration = tmp_path / "comparison.json"
    declared: dict[str, Any] = {
        "upstream_verifier_sha256": _sha(before_files["test_state.py"]),
        "repaired_verifier_sha256": _sha(after_files["test_state.py"]),
    }
    _json(declaration, declared)
    sha_only = compare_quality_audits(
        before, after, source_root=lane, declaration_path=declaration
    )
    assert sha_only["experiment_axis"]["status"] == "undeclared_verifier_change"

    declared.update({
        "upstream_verifier_manifest": {
            rel: _manifest_entry(data) for rel, data in before_files.items()
        },
        "repaired_verifier_manifest": {
            rel: _manifest_entry(data) for rel, data in after_files.items()
        },
    })
    _json(declaration, declared)
    covered = compare_quality_audits(
        before, after, source_root=lane, declaration_path=declaration
    )
    assert covered["experiment_axis"]["status"] == "declared_verifier_change"
    del declared["repaired_verifier_manifest"][changed_path]
    _json(declaration, declared)
    incomplete = compare_quality_audits(
        before, after, source_root=lane, declaration_path=declaration
    )
    assert incomplete["experiment_axis"]["status"] == "undeclared_verifier_change"


@pytest.mark.parametrize("manifest_text", ["{", "[]"])
def test_corrupt_runtime_manifest_does_not_hide_owned_rewards(
    tmp_path: Path, manifest_text: str
) -> None:
    before = _write_owned_audit(tmp_path / "consumer")
    after = _write_owned_audit(tmp_path / "consumer", phase="after-owned")
    for audit in (before, after):
        for arm in ("oracle", "nop"):
            _json(audit / arm / "task_file/inputs/a.json", {"k": 1})
            data = (audit / arm / "task_file/inputs/a.json").read_bytes()
            _json(
                audit / arm / "task-file-manifest.json",
                {"inputs/a.json": _manifest_entry(data)},
            )
    (after / "oracle/task-file-manifest.json").write_text(manifest_text)
    evidence = load_quality_audit_evidence(after)
    assert evidence["arms"]["oracle"]["observed_reward"] == 1
    assert evidence["arms"]["oracle"]["runtime_manifest_issue"] == "runtime_manifest_unreadable"
    report = compare_quality_audits(before, after)
    oracle = report["arms"]["oracle"]
    assert oracle["pairing"]["status"] == "unqualified"
    assert oracle["observed_reward_delta"] == 0
    assert oracle["input_identity"]["after_issues"] == ["runtime_manifest_unreadable"]
    assert report["arms"]["nop"]["observed_reward_delta"] == 0
    assert report["arms"]["nop"]["input_identity"]["status"] == "same"
