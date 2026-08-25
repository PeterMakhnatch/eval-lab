"""Tests for Synthetic Agent-Capability Certification Gate (src/evallab/synthetic_cert.py)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from evallab.synthetic_cert import (
    SyntheticCertificationGate,
    certify_synthetic_task,
)
from evallab.synthetic_contracts import (
    PerturbationFamily,
    SyntheticCertificate,
    SyntheticEvalSpec,
    create_synthetic_eval_spec,
)

SAMPLE_BASE_DIGEST = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SAMPLE_GEN_DIGEST = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


@pytest.fixture
def valid_spec() -> SyntheticEvalSpec:
    return create_synthetic_eval_spec(
        construct_name="tool_retry_resilience",
        family=PerturbationFamily.TOOL_UNRELIABILITY,
        perturbation_type="transient_network_flake",
        seed=42,
        source_task_ref="library/tasks/network-lookup",
        source_failure_evidence=["runs/failure_001/agent/trace.json"],
        base_task_digest=SAMPLE_BASE_DIGEST,
        generated_task_digest=SAMPLE_GEN_DIGEST,
        expected_behavior="Agent retries idempotent lookup upon encountering 503 error",
        capability_opportunity="Measure adaptive tool recovery under transient error",
        required_evidence=["tool_retry_recovery"],
        license_provenance="MIT License (Base Task Authors)",
        partition="dev",
        family_id="syn-tool-unreliability-v1",
        lineage_id="lin-tool-retry-42",
        parameters={"flake_probability": 0.5, "max_retries": 3},
    )


def test_certification_gate_valid_task_passes(valid_spec: SyntheticEvalSpec) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        evidence_dir = Path(tmpdir) / "evidence"
        gate = SyntheticCertificationGate(evidence_dir=evidence_dir)

        cert = gate.certify(
            valid_spec,
            oracle_runner=lambda: (True, "Oracle passed"),
            nop_runner=lambda: (False, "NOP agent failed verifier"),
            mutant_runners=[
                lambda: (False, "Mutant 1 (empty output) failed"),
                lambda: (False, "Mutant 2 (wrong port) failed"),
                lambda: (False, "Mutant 3 (corrupt payload) failed"),
            ],
            regenerator=lambda seed, params: (SAMPLE_BASE_DIGEST, SAMPLE_GEN_DIGEST),
        )

        assert isinstance(cert, SyntheticCertificate)
        assert cert.status == "experimental"
        assert cert.is_passing is True
        assert cert.static_reachability is True
        assert cert.clean_reset_passed is True
        assert cert.oracle_3x_passed is True
        assert cert.nop_failed is True
        assert cert.mutants_tested_count == 3
        assert cert.mutants_failed_count == 3
        assert cert.alignment_audit_passed is True
        assert cert.regeneration_idempotent is True
        assert cert.secret_isolation_passed is True
        assert len(cert.evidence_paths) == 1
        assert Path(cert.evidence_paths[0]).exists()


def test_certification_gate_check_static_reachability(valid_spec: SyntheticEvalSpec) -> None:
    gate = SyntheticCertificationGate()

    # Valid task directory
    with tempfile.TemporaryDirectory() as tmpdir:
        task_dir = Path(tmpdir)
        (task_dir / "instruction.md").write_text(
            "Valid instructions here for testing tool retries."
        )
        (task_dir / "tests").mkdir()
        (task_dir / "tests" / "test_verify.py").write_text("def test_it(): pass")

        res = gate.check_static_reachability(valid_spec, task_dir=task_dir)
        assert res.passed is True

    # Missing instruction / verifier
    with tempfile.TemporaryDirectory() as tmpdir:
        task_dir = Path(tmpdir)
        res = gate.check_static_reachability(valid_spec, task_dir=task_dir)
        assert res.passed is False
        assert any("Missing instruction" in d for d in res.diagnostics)


def test_certification_gate_check_clean_reset(valid_spec: SyntheticEvalSpec) -> None:
    gate = SyntheticCertificationGate()

    # Successful reset function
    res_ok = gate.check_clean_reset(valid_spec, reset_fn=lambda: (True, "clean"))
    assert res_ok.passed is True

    # Failing reset function
    res_fail = gate.check_clean_reset(valid_spec, reset_fn=lambda: (False, "lock held"))
    assert res_fail.passed is False

    # Leftover lock files in task environment
    with tempfile.TemporaryDirectory() as tmpdir:
        task_dir = Path(tmpdir)
        env_dir = task_dir / "environment"
        env_dir.mkdir()
        (env_dir / "state.lock").write_text("locked")

        res_dir = gate.check_clean_reset(valid_spec, task_dir=task_dir)
        assert res_dir.passed is False
        assert any("Transient" in d for d in res_dir.diagnostics)


def test_certification_gate_failing_oracle_rejects(valid_spec: SyntheticEvalSpec) -> None:
    gate = SyntheticCertificationGate()

    # Oracle fails on second iteration
    call_count = 0

    def flaky_oracle():
        nonlocal call_count
        call_count += 1
        return (call_count != 2, "flaky failure on 2nd run")

    cert = gate.certify(
        valid_spec,
        oracle_runner=flaky_oracle,
        nop_runner=lambda: (False, "NOP failed"),
        mutant_runners=[
            lambda: (False, "Mutant 1 failed"),
            lambda: (False, "Mutant 2 failed"),
            lambda: (False, "Mutant 3 failed"),
        ],
    )

    assert cert.status == "rejected"
    assert cert.is_passing is False
    assert cert.oracle_3x_passed is False


def test_certification_gate_oracle_execution_records(valid_spec: SyntheticEvalSpec) -> None:
    gate = SyntheticCertificationGate()

    # Passing execution records
    records_ok = [
        {"agent_kind": "oracle", "passed": True},
        {"agent_kind": "oracle", "passed": True},
        {"agent_kind": "oracle", "passed": True},
        {"agent_kind": "nop", "passed": False},
    ]
    res_ok = gate.check_oracle_3x(valid_spec, execution_records=records_ok)
    assert res_ok.passed is True

    # Insufficient records (< 3)
    records_insufficient = [
        {"agent_kind": "oracle", "passed": True},
        {"agent_kind": "oracle", "passed": True},
    ]
    res_insufficient = gate.check_oracle_3x(valid_spec, execution_records=records_insufficient)
    assert res_insufficient.passed is False


def test_certification_gate_vacuous_verifier_rejects(valid_spec: SyntheticEvalSpec) -> None:
    gate = SyntheticCertificationGate()

    # NOP unexpectedly passes
    cert = gate.certify(
        valid_spec,
        oracle_runner=lambda: (True, "Oracle passed"),
        nop_runner=lambda: (True, "NOP unexpectedly succeeded!"),
        mutant_runners=[
            lambda: (False, "Mutant 1 failed"),
            lambda: (False, "Mutant 2 failed"),
            lambda: (False, "Mutant 3 failed"),
        ],
    )

    assert cert.status == "rejected"
    assert cert.is_passing is False
    assert cert.nop_failed is False


def test_certification_gate_insufficient_mutants_rejects(valid_spec: SyntheticEvalSpec) -> None:
    gate = SyntheticCertificationGate()

    # Only 2 mutants provided (minimum required is 3)
    cert = gate.certify(
        valid_spec,
        oracle_runner=lambda: (True, "Oracle passed"),
        nop_runner=lambda: (False, "NOP failed"),
        mutant_runners=[
            lambda: (False, "Mutant 1 failed"),
            lambda: (False, "Mutant 2 failed"),
        ],
    )

    assert cert.status == "rejected"
    assert cert.is_passing is False
    assert cert.mutants_tested_count == 2
    assert cert.mutants_failed_count == 2


def test_certification_gate_surviving_mutant_rejects(valid_spec: SyntheticEvalSpec) -> None:
    gate = SyntheticCertificationGate()

    # Mutant 2 mistakenly passes the verifier
    cert = gate.certify(
        valid_spec,
        oracle_runner=lambda: (True, "Oracle passed"),
        nop_runner=lambda: (False, "NOP failed"),
        mutant_runners=[
            lambda: (False, "Mutant 1 failed"),
            lambda: (True, "Mutant 2 passed verifier!"),
            lambda: (False, "Mutant 3 failed"),
        ],
    )

    assert cert.status == "rejected"
    assert cert.is_passing is False
    assert cert.mutants_tested_count == 3
    assert cert.mutants_failed_count == 2


def test_certification_gate_alignment_audit(valid_spec: SyntheticEvalSpec) -> None:
    gate = SyntheticCertificationGate()

    res_ok = gate.check_alignment_audit(valid_spec)
    assert res_ok.passed is True

    # Misaligned construct (e.g. function_dag with tool retry tokens)
    misaligned_spec = create_synthetic_eval_spec(
        construct_name="unrelated_topic",
        family=PerturbationFamily.FUNCTION_DAG,
        perturbation_type="unrelated_type",
        seed=1,
        source_task_ref="base/task",
        base_task_digest=SAMPLE_BASE_DIGEST,
        generated_task_digest=SAMPLE_GEN_DIGEST,
        expected_behavior="some arbitrary behavior",
        capability_opportunity="measuring arbitrary memory retention",
        license_provenance="MIT",
        family_id="fam1",
        lineage_id="lin1",
    )
    res_mis = gate.check_alignment_audit(misaligned_spec)
    assert res_mis.passed is False


def test_certification_gate_secret_leak_detected(valid_spec: SyntheticEvalSpec) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        task_path = Path(tmpdir)
        inst_file = task_path / "instruction.md"
        inst_file.write_text("Here is your task. VERIFIER_SECRET = 'super_secret_key_123'")

        gate = SyntheticCertificationGate()
        cert = gate.certify(
            valid_spec,
            task_dir=task_path,
            oracle_runner=lambda: (True, "Oracle passed"),
            nop_runner=lambda: (False, "NOP failed"),
            mutant_runners=[
                lambda: (False, "Mutant 1 failed"),
                lambda: (False, "Mutant 2 failed"),
                lambda: (False, "Mutant 3 failed"),
            ],
        )

        assert cert.status == "rejected"
        assert cert.secret_isolation_passed is False


def test_certification_gate_non_deterministic_regeneration(valid_spec: SyntheticEvalSpec) -> None:
    gate = SyntheticCertificationGate()

    gen_call = 0

    def non_deterministic_gen(seed, params):
        nonlocal gen_call
        gen_call += 1
        return (
            SAMPLE_BASE_DIGEST,
            f"sha256:{'c' * 63}{gen_call}",
        )

    cert = gate.certify(
        valid_spec,
        regenerator=non_deterministic_gen,
    )

    assert cert.status == "rejected"
    assert cert.regeneration_idempotent is False


def test_convenience_certify_synthetic_task(valid_spec: SyntheticEvalSpec) -> None:
    cert = certify_synthetic_task(
        valid_spec,
        oracle_runner=lambda: (True, "pass"),
        nop_runner=lambda: (False, "fail"),
        mutant_runners=[
            lambda: (False, "m1 fail"),
            lambda: (False, "m2 fail"),
            lambda: (False, "m3 fail"),
        ],
    )
    assert cert.status == "experimental"
    assert cert.is_passing is True
