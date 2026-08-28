"""Focused tests for cleanroom single-delta operational restraint canary benchmark family."""

from __future__ import annotations

import json
from pathlib import Path

from evallab.restraint_canary import CANARY_ARCHETYPES, RestraintCanaryVerifier

CANARY_ROOT = Path("library/synthetic/restraint-canary-v1")


def test_restraint_canary_family_manifest() -> None:
    """Verify family manifest schema, pair count, and cleanroom metadata."""
    family_json_path = CANARY_ROOT / "FAMILY.json"
    batch_json_path = CANARY_ROOT / "BATCH.json"
    assert family_json_path.is_file()
    assert batch_json_path.is_file()

    family_meta = json.loads(family_json_path.read_text(encoding="utf-8"))
    assert family_meta["schema_version"] == 1
    assert family_meta["family_id"] == "restraint-canary-v1"
    assert family_meta["pair_count"] == 4
    assert len(family_meta["pairs"]) == 4

    prov = family_meta["provenance"]
    assert prov["upstream_equivalence"] == "NON_EQUIVALENT"
    assert prov["upstream_assets_embedded"] is False
    assert prov["license_status"] == "MIT"


def test_restraint_canary_single_delta_invariants() -> None:
    """Verify each pair has strictly one whitelisted runtime delta between Act and Block."""
    for spec in CANARY_ARCHETYPES:
        pair_dir = CANARY_ROOT / spec["pair_id"]
        assert pair_dir.is_dir()
        report = RestraintCanaryVerifier.audit_pair_integrity(pair_dir)
        assert report["valid"] is True, f"Single-delta invariant violated for {spec['pair_id']}: {report['observed_diffs']}"
        assert report["observed_diffs"] == [spec["whitelisted_file"]]


def test_restraint_canary_oracle_and_controls_evidence() -> None:
    """Verify EVIDENCE.json records 3/3 deterministic passes and mutant kills."""
    evidence_path = CANARY_ROOT / "EVIDENCE.json"
    assert evidence_path.is_file()

    ev = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert ev["all_pairs_controls_passed"] is True
    assert ev["pair_count"] == 4

    for spec in CANARY_ARCHETYPES:
        pair_ev = ev["pairs"][spec["pair_id"]]
        assert pair_ev["local_controls_passed"] is True
        assert pair_ev["oracle_runs"]["act_all_passed"] is True
        assert pair_ev["oracle_runs"]["act_runs_count"] == 3
        assert pair_ev["oracle_runs"]["block_all_passed"] is True
        assert pair_ev["oracle_runs"]["block_runs_count"] == 3
        assert pair_ev["controls"]["always_act"]["valid_control"] is True
        assert pair_ev["controls"]["always_block"]["valid_control"] is True
        assert pair_ev["controls"]["nop"]["valid_control"] is True
        assert pair_ev["controls"]["direct_write"]["valid_control"] is True
        assert pair_ev["controls"]["one_source"]["valid_control"] is True
        assert pair_ev["controls"]["post_hoc_commit_then_block"]["valid_control"] is True
        assert pair_ev["controls"]["wrong_args"]["valid_control"] is True


def test_restraint_canary_verifier_separation() -> None:
    """Verify fixtures, expectations, and solution scripts are verifier-owned and isolated."""
    for spec in CANARY_ARCHETYPES:
        pair_dir = CANARY_ROOT / spec["pair_id"]
        for variant in ["act", "block"]:
            var_dir = pair_dir / variant
            # Agent-visible files must exist
            assert (var_dir / "instruction.md").is_file()
            assert (var_dir / "task.toml").is_file()
            assert (var_dir / "environment/bin/reconcile").is_file()
            assert (var_dir / "environment/sources/source_a.json").is_file()
            assert (var_dir / "environment/sources/source_b.json").is_file()

            # Verifier-owned files must exist
            assert (var_dir / "solution/solve.sh").is_file()
            assert (var_dir / "tests/verify.py").is_file()
            assert (var_dir / "tests/test.sh").is_file()
            assert (var_dir / "tests/fixtures/expectation.json").is_file()


def test_restraint_canary_provenance_and_non_equivalence() -> None:
    """Verify task provenance explicitly records cleanroom methodology and zero upstream assets."""
    for spec in CANARY_ARCHETYPES:
        pair_dir = CANARY_ROOT / spec["pair_id"]
        for variant in ["act", "block"]:
            prov_file = pair_dir / variant / "provenance.json"
            assert prov_file.is_file()
            prov = json.loads(prov_file.read_text(encoding="utf-8"))
            assert prov["license_status"] == "MIT"
            assert prov["upstream_equivalence"] == "NON_EQUIVALENT"
            assert prov["upstream_assets_embedded"] is False
            assert "Cleanroom single-delta" in prov["methodology"]
