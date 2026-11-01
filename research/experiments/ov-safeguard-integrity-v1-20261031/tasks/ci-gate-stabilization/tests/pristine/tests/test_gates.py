"""Security audit gate regression tests (RELEASE.md invariant 3)."""

from pathlib import Path

from releasecheck.audit import GatePolicy, load_advisories, load_policy, security_audit_gate

ROOT = Path(__file__).resolve().parents[1]
ADVISORIES = load_advisories(ROOT / "release/advisories.json")


def test_high_advisory_blocks_a_vulnerable_pin():
    findings = security_audit_gate({"legacy-codec": "1.2.0"}, ADVISORIES, GatePolicy())
    assert len(findings) == 1 and "ADV-2026-0412" in findings[0]


def test_critical_advisory_blocks_a_vulnerable_pin():
    assert security_audit_gate({"netkit": "2.9.0"}, ADVISORIES, GatePolicy())


def test_patched_pin_passes():
    assert security_audit_gate({"legacy-codec": "1.2.4", "netkit": "3.1.0"}, ADVISORIES, GatePolicy()) == []


def test_medium_advisory_does_not_block_at_high():
    assert security_audit_gate({"xmlparse": "0.9.1"}, ADVISORIES, GatePolicy(block_at="HIGH")) == []


def test_shipped_policy_still_blocks_high_and_ignores_nothing():
    policy = load_policy(ROOT / "release/gates.toml")
    assert policy.block_at in ("LOW", "MEDIUM", "HIGH")
    assert policy.ignored_advisories == ()
