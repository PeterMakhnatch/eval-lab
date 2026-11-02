"""Tests for F10-ROADMAP-FINAL metrics, receipts, and manifest integrity."""

import hashlib
import json
from pathlib import Path


def sha256_file(filepath: Path) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def test_receipt_files_exist_and_match_manifest():
    receipt_dir = Path("research/experiments/on-workflow-roadmap-20261102/receipts")
    manifest_path = receipt_dir / "manifest.json"
    assert manifest_path.is_file(), "manifest.json must exist"

    with open(manifest_path) as f:
        manifest = json.load(f)

    expected_files = {
        "detailed_evidence.json",
        "engine_comparison.json",
        "evidence_summary.json",
        "summary.json",
    }
    assert set(manifest.keys()) == expected_files

    for filename, meta in manifest.items():
        fpath = receipt_dir / filename
        assert fpath.is_file(), f"{filename} missing on disk"
        real_sha = sha256_file(fpath)
        assert real_sha == meta["sha256"], f"Hash mismatch for {filename}"
        assert real_sha[:12] == meta["sha256_12"]
        assert fpath.stat().st_size == meta["size_bytes"]


def test_summary_metrics_consistency():
    receipt_dir = Path("research/experiments/on-workflow-roadmap-20261102/receipts")
    with open(receipt_dir / "summary.json") as f:
        summary = json.load(f)
    with open(receipt_dir / "evidence_summary.json") as f:
        evidence = json.load(f)

    assert evidence["total_evidence_points"] == 84
    km = summary["key_metrics"]
    assert km["total_tonight_friction_items"] == 56
    assert km["total_census_documented_incidents"] == 28
    assert km["total_empirical_evidence_points"] == 84
    assert km["tooling_environment_friction_count"] == 14
    assert km["workflow_pain_points_count"] == 70

    assert km["friction_covered_by_top_5"] == 32
    assert km["census_covered_by_top_5"] == 16
    assert km["top_5_workflows_covered_pain_points"] == 48

    # Assert exact ratio and percentages
    assert km["top_5_workflows_pain_point_coverage_ratio"] == "48/70 (68.6%)"
    assert km["friction_coverage_ratio_all"] == "32/56 (57.1%)"
    assert km["friction_coverage_ratio_workflow"] == "32/42 (76.2%)"
    assert km["census_coverage_ratio"] == "16/28 (57.1%)"

    # Verify Wilson intervals are ordered and bounded
    for key in [
        "top_5_workflows_pain_point_coverage_95_wilson",
        "friction_coverage_all_95_wilson",
        "friction_coverage_workflow_95_wilson",
        "census_coverage_95_wilson",
    ]:
        low, high = km[key]
        assert 0.0 <= low <= high <= 1.0


def test_engine_scorecard_and_killtests():
    receipt_dir = Path("research/experiments/on-workflow-roadmap-20261102/receipts")
    with open(receipt_dir / "engine_comparison.json") as f:
        engine = json.load(f)

    # All evaluated systems present
    systems = engine["systems_evaluated"]
    assert "DBOS" in systems
    assert "Temporal" in systems
    assert "Dagu" in systems
    assert "Baseline (ds_squad.py)" in systems

    kt = engine["kill_test_scorecard"]
    # DBOS passes all 5 kill tests safely (4 PASS + 1 FAILS LOUDLY)
    assert kt["KT1_crash_durability"]["DBOS"]["status"] == "PASS"
    assert kt["KT2_human_approval_across_reboot"]["DBOS"]["status"] == "PASS"
    assert kt["KT3_dynamic_quota_parking"]["DBOS"]["status"] == "PASS"
    assert kt["KT3_dynamic_quota_parking"]["DBOS"]["attempts_before_T"] == 0
    assert kt["KT4_live_code_change"]["DBOS"]["status"] == "FAILS LOUDLY"
    assert kt["KT5_independent_caps"]["DBOS"]["status"] == "PASS"

    # Temporal fails KT3 native quota parking
    assert kt["KT1_crash_durability"]["Temporal"]["status"] == "PASS"
    assert kt["KT2_human_approval_across_reboot"]["Temporal"]["status"] == "PASS"
    assert kt["KT3_dynamic_quota_parking"]["Temporal"]["status"] == "FAIL"
    assert kt["KT3_dynamic_quota_parking"]["Temporal"]["attempts_before_T"] == 3
    assert kt["KT4_live_code_change"]["Temporal"]["status"] == "FAILS LOUDLY"
    assert kt["KT5_independent_caps"]["Temporal"]["status"] == "PASS"

    # Baseline fails KT2 and KT3, silently diverges on KT4
    assert kt["KT2_human_approval_across_reboot"]["Baseline"]["status"] == "FAIL"
    assert kt["KT3_dynamic_quota_parking"]["Baseline"]["status"] == "FAIL"
    assert kt["KT4_live_code_change"]["Baseline"]["status"] == "SILENT DIVERGENCE"
