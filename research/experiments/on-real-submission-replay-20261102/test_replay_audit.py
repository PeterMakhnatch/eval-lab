"""Tests for A8 Real Submission Replay Audit."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "replay_audit", Path(__file__).parent / "replay_audit.py"
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["replay_audit"] = _mod
_spec.loader.exec_module(_mod)

perform_audit = _mod.perform_audit


def test_perform_audit_synthetic_fixtures(tmp_path: Path) -> None:
    # 1. Synthetic Universe
    universe_data = {
        "schema_version": "gold-universe/v1",
        "tasks": [
            {"task_id": "synth-task-001", "gold_ok": True},
            {"task_id": "synth-task-002", "gold_ok": True},
            {"task_id": "synth-task-003", "gold_ok": False},
        ],
    }
    universe_file = tmp_path / "universe.json"
    universe_file.write_text(json.dumps(universe_data), encoding="utf-8")

    # 2. Synthetic Manifest (both failed reference validation)
    manifest_data = {
        "schema_version": "gold-v1/manifest/v1",
        "tasks": [
            {
                "task_id": "synth-task-001",
                "status": "not_gold",
                "reason": "ref_failed_no_repair",
                "gates": {"reference_passed": False},
            },
            {
                "task_id": "synth-task-002",
                "status": "not_gold",
                "reason": "ref_failed_no_repair",
                "gates": {"reference_passed": False},
            },
        ],
    }
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    # 3. Synthetic Submissions Inventory
    inv_data = {
        "schema_version": "submissions-inventory/v1",
        "submissions": [
            {
                "trial_id": "trial-1",
                "task_id": "synth-task-001",
                "original_reward": 1.0,
                "patch_bytes": 128,
                "patch_sha256": "abcdef123456",
            },
            {
                "trial_id": "trial-2",
                "task_id": "synth-task-001",
                "original_reward": 0.0,
                "patch_bytes": 100,
                "patch_sha256": "123456abcdef",
            },
            {
                "trial_id": "trial-3",
                "task_id": "synth-task-003",
                "original_reward": 1.0,
                "patch_bytes": 50,
                "patch_sha256": "999999abcdef",
            },
        ],
    }
    inv_file = tmp_path / "inv.json"
    inv_file.write_text(json.dumps(inv_data), encoding="utf-8")

    # Run audit
    audit = perform_audit(inv_file, manifest_file, universe_file, prereg_sha256_12="test12345678")

    assert audit.target_item == "A8-REAL-SUBMISSION-REPLAY"
    assert audit.prereg_sha256_12 == "test12345678"
    assert audit.counts["gold_universe_candidate_tasks"] == 2
    assert audit.counts["gold_structural_tasks"] == 0
    assert audit.counts["gold_heldout_tasks"] == 0
    assert audit.counts["prereg_cohort_qualifying_submissions"] == 0
    assert audit.counts["candidate_accepted_submissions"] == 1
    assert audit.controls["reference_control_satisfied"] is False
    assert "reference control failed" in audit.blocking_reason
    assert "ref_failed_no_repair" in audit.controls["explanation"]


def test_perform_audit_with_certified_gold(tmp_path: Path) -> None:
    # Test positive scenario where a package is certified
    universe_data = {
        "tasks": [{"task_id": "gold-task-001", "gold_ok": True}],
    }
    universe_file = tmp_path / "universe.json"
    universe_file.write_text(json.dumps(universe_data), encoding="utf-8")

    manifest_data = {
        "tasks": [
            {
                "task_id": "gold-task-001",
                "status": "gold_structural",
                "gates": {"reference_passed": True},
            }
        ],
    }
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    inv_data = {
        "submissions": [
            {
                "trial_id": "trial-1",
                "task_id": "gold-task-001",
                "original_reward": 1.0,
                "patch_bytes": 200,
                "patch_sha256": "aaa111",
            }
        ],
    }
    inv_file = tmp_path / "inv.json"
    inv_file.write_text(json.dumps(inv_data), encoding="utf-8")

    audit = perform_audit(inv_file, manifest_file, universe_file)
    assert audit.counts["gold_structural_tasks"] == 1
    assert audit.counts["prereg_cohort_qualifying_submissions"] == 1
    assert audit.controls["reference_control_satisfied"] is True
