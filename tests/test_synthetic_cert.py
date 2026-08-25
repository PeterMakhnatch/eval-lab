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
            reset_fn=lambda: (True, "Reset clean"),
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

    # Missing reset function fails by evidence-strict rule
    res_no_fn = gate.check_clean_reset(valid_spec)
    assert res_no_fn.passed is False
    assert any("No clean reset function provided" in d for d in res_no_fn.diagnostics)

    # Successful reset function
    res_ok = gate.check_clean_reset(valid_spec, reset_fn=lambda: (True, "clean"))
    assert res_ok.passed is True
    assert res_ok.details == "Repeated reset executions succeeded"

    # Failing reset function
    res_fail = gate.check_clean_reset(valid_spec, reset_fn=lambda: (False, "lock held"))
    assert res_fail.passed is False

    # Leftover lock files in task environment detected when reset_fn is provided
    with tempfile.TemporaryDirectory() as tmpdir:
        task_dir = Path(tmpdir)
        env_dir = task_dir / "environment"
        env_dir.mkdir()
        (env_dir / "state.lock").write_text("locked")

        res_dir = gate.check_clean_reset(
            valid_spec,
            task_dir=task_dir,
            reset_fn=lambda: (True, "clean"),
        )
        assert res_dir.passed is False
        assert any("Transient" in d for d in res_dir.diagnostics)


def test_clean_reset_invokes_twice_and_fails_on_either_run(valid_spec: SyntheticEvalSpec) -> None:
    """Clean reset must execute reset_fn exactly twice and fail if either run fails or raises."""
    gate = SyntheticCertificationGate()

    # Case 1: Proving two invocations succeed
    call_count = 0

    def counting_reset():
        nonlocal call_count
        call_count += 1
        return (True, f"clean run {call_count}")

    res_two_runs = gate.check_clean_reset(valid_spec, reset_fn=counting_reset)
    assert res_two_runs.passed is True
    assert call_count == 2
    assert res_two_runs.details == "Repeated reset executions succeeded"

    # Case 2: Fails on second execution
    call_count_fail = 0

    def fail_on_second():
        nonlocal call_count_fail
        call_count_fail += 1
        if call_count_fail == 2:
            return (False, "Residual state persisted on 2nd reset")
        return (True, "clean on 1st run")

    res_fail_2 = gate.check_clean_reset(valid_spec, reset_fn=fail_on_second)
    assert res_fail_2.passed is False
    assert call_count_fail == 2
    assert any("failed on run 2/2" in d for d in res_fail_2.diagnostics)
    assert any("Residual state persisted on 2nd reset" in d for d in res_fail_2.diagnostics)

    # Case 3: Raises exception on first execution
    call_count_raise = 0

    def raise_on_first():
        nonlocal call_count_raise
        call_count_raise += 1
        raise RuntimeError("Teardown script crashed")

    res_raise_1 = gate.check_clean_reset(valid_spec, reset_fn=raise_on_first)
    assert res_raise_1.passed is False
    assert call_count_raise == 1
    assert any("raised exception on run 1/2" in d for d in res_raise_1.diagnostics)
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
        reset_fn=lambda: (True, "clean"),
        regenerator=lambda seed, params: (SAMPLE_BASE_DIGEST, SAMPLE_GEN_DIGEST),
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
        reset_fn=lambda: (True, "clean"),
        regenerator=lambda seed, params: (SAMPLE_BASE_DIGEST, SAMPLE_GEN_DIGEST),
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
        reset_fn=lambda: (True, "clean"),
        regenerator=lambda seed, params: (SAMPLE_BASE_DIGEST, SAMPLE_GEN_DIGEST),
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
        reset_fn=lambda: (True, "clean"),
        regenerator=lambda seed, params: (SAMPLE_BASE_DIGEST, SAMPLE_GEN_DIGEST),
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
            reset_fn=lambda: (True, "clean"),
            regenerator=lambda seed, params: (SAMPLE_BASE_DIGEST, SAMPLE_GEN_DIGEST),
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
        oracle_runner=lambda: (True, "Oracle passed"),
        nop_runner=lambda: (False, "NOP failed"),
        mutant_runners=[
            lambda: (False, "Mutant 1 failed"),
            lambda: (False, "Mutant 2 failed"),
            lambda: (False, "Mutant 3 failed"),
        ],
        reset_fn=lambda: (True, "clean"),
        regenerator=non_deterministic_gen,
    )

    assert cert.status == "rejected"
    assert cert.regeneration_idempotent is False


def test_regeneration_idempotency_base_drift_and_determinism(valid_spec: SyntheticEvalSpec) -> None:
    """Regeneration check must enforce both base and generated digest idempotency and matching spec."""
    gate = SyntheticCertificationGate()

    # Case 1: Base digest differs between run 1 and run 2
    base_run = 0

    def drifting_base_gen(seed, params):
        nonlocal base_run
        base_run += 1
        return (
            f"sha256:{'a' * 63}{base_run}",
            SAMPLE_GEN_DIGEST,
        )

    res_base_drift = gate.check_regeneration_idempotency(valid_spec, regenerator=drifting_base_gen)
    assert res_base_drift.passed is False
    assert any("Non-deterministic base generation" in d for d in res_base_drift.diagnostics)

    # Case 2: Base digest is deterministic across runs but differs from spec.base_task_digest
    wrong_base = "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"

    def wrong_base_gen(seed, params):
        return (wrong_base, SAMPLE_GEN_DIGEST)

    res_wrong_base = gate.check_regeneration_idempotency(valid_spec, regenerator=wrong_base_gen)
    assert res_wrong_base.passed is False
    assert any("Regenerated base digest" in d for d in res_wrong_base.diagnostics)

    # Case 3: Generated digest differs from spec.generated_task_digest
    wrong_gen = "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"

    def wrong_gen_gen(seed, params):
        return (SAMPLE_BASE_DIGEST, wrong_gen)

    res_wrong_gen = gate.check_regeneration_idempotency(valid_spec, regenerator=wrong_gen_gen)
    assert res_wrong_gen.passed is False
    assert any("Regenerated digest" in d for d in res_wrong_gen.diagnostics)

    # Case 4: Complete matching base and generated digests succeed
    def valid_gen(seed, params):
        return (SAMPLE_BASE_DIGEST, SAMPLE_GEN_DIGEST)

    res_ok = gate.check_regeneration_idempotency(valid_spec, regenerator=valid_gen)
    assert res_ok.passed is True
    assert res_ok.details == "Seed-based regeneration idempotency verified"

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
        reset_fn=lambda: (True, "reset pass"),
        regenerator=lambda seed, params: (SAMPLE_BASE_DIGEST, SAMPLE_GEN_DIGEST),
    )
    assert cert.status == "experimental"
    assert cert.is_passing is True


def test_metadata_only_spec_rejected(valid_spec: SyntheticEvalSpec) -> None:
    """Metadata-only spec without runners or records must fail all execution gates."""
    gate = SyntheticCertificationGate()

    cert = gate.certify(valid_spec)

    assert cert.status == "rejected"
    assert cert.is_passing is False
    assert cert.static_reachability is True
    assert cert.clean_reset_passed is False
    assert cert.oracle_3x_passed is False
    assert cert.nop_failed is False
    assert cert.mutants_tested_count == 0
    assert cert.mutants_failed_count == 0
    assert cert.regeneration_idempotent is False

    audit = gate.audit_task(valid_spec)
    assert len(audit.check_results) == 8
    assert [result.name for result in audit.check_results] == [
        "static_reachability",
        "clean_reset",
        "oracle_3x",
        "nop_failed",
        "mutants_tested",
        "alignment_audit",
        "regeneration_idempotent",
        "secret_isolation",
    ]

    # Check individual gate diagnostics name the missing evidence
    res_oracle = gate.check_oracle_3x(valid_spec)
    assert res_oracle.passed is False
    assert any("No oracle runner or execution records provided" in d for d in res_oracle.diagnostics)

    res_nop = gate.check_nop_failed(valid_spec)
    assert res_nop.passed is False
    assert any("No NOP runner or execution records provided" in d for d in res_nop.diagnostics)

    res_mutants, tested, failed = gate.check_mutants(valid_spec)
    assert res_mutants.passed is False
    assert tested == 0
    assert failed == 0
    assert any("No mutant runners or mutant records provided" in d for d in res_mutants.diagnostics)

    res_reset = gate.check_clean_reset(valid_spec)
    assert res_reset.passed is False
    assert any("No clean reset function provided" in d for d in res_reset.diagnostics)

    res_regen = gate.check_regeneration_idempotency(valid_spec)
    assert res_regen.passed is False
    assert any("No regenerator function provided" in d for d in res_regen.diagnostics)


def test_exception_as_rejection_for_invoked_runners(valid_spec: SyntheticEvalSpec) -> None:
    """Exception during execution of nop or mutant runners counts as verifier rejection."""
    gate = SyntheticCertificationGate()

    def crashing_nop():
        raise RuntimeError("Agent crashed immediately (verifier rejected)")

    def crashing_mutant():
        raise AssertionError("Mutant produced assertion error in verifier")

    # When nop runner raises, check_nop_failed passes (verifier rejected nop)
    res_nop = gate.check_nop_failed(valid_spec, nop_runner=crashing_nop)
    assert res_nop.passed is True

    # When mutant runner raises, mutant is counted as rejected
    res_mut, tested, failed = gate.check_mutants(
        valid_spec,
        mutant_runners=[
            crashing_mutant,
            lambda: (False, "m2 failed"),
            lambda: (False, "m3 failed"),
        ],
    )
    assert res_mut.passed is True
    assert tested == 3
    assert failed == 3

    # Consolidated certification passes
    cert = gate.certify(
        valid_spec,
        oracle_runner=lambda: (True, "oracle passed"),
        nop_runner=crashing_nop,
        mutant_runners=[
            crashing_mutant,
            lambda: (False, "m2 failed"),
            lambda: (False, "m3 failed"),
        ],
        reset_fn=lambda: (True, "clean"),
        regenerator=lambda seed, params: (SAMPLE_BASE_DIGEST, SAMPLE_GEN_DIGEST),
    )
    assert cert.status == "experimental"
    assert cert.is_passing is True
    assert cert.nop_failed is True
    assert cert.mutants_tested_count == 3
    assert cert.mutants_failed_count == 3


def test_execution_records_path_enforced_and_validated(valid_spec: SyntheticEvalSpec) -> None:
    """Execution-records path enforces sufficient passing oracle, failing nop, and failing mutants."""
    gate = SyntheticCertificationGate()

    valid_exec_records = [
        {"agent_kind": "oracle", "passed": True},
        {"agent_kind": "oracle", "passed": True},
        {"agent_kind": "oracle", "passed": True},
        {"agent_kind": "nop", "passed": False},
    ]
    valid_mutant_records = [
        {"name": "mutant_bad_port", "passed": False},
        {"name": "mutant_corrupt_payload", "passed": False},
        {"name": "mutant_empty_response", "passed": False},
    ]

    # Full pass with execution records
    cert = gate.certify(
        valid_spec,
        execution_records=valid_exec_records,
        mutant_records=valid_mutant_records,
        reset_fn=lambda: (True, "clean"),
        regenerator=lambda seed, params: (SAMPLE_BASE_DIGEST, SAMPLE_GEN_DIGEST),
    )
    assert cert.status == "experimental"
    assert cert.is_passing is True
    assert cert.oracle_3x_passed is True
    assert cert.nop_failed is True
    assert cert.mutants_tested_count == 3
    assert cert.mutants_failed_count == 3

    # Failing oracle in execution records (< 3 runs)
    bad_oracle_count = [
        {"agent_kind": "oracle", "passed": True},
        {"agent_kind": "oracle", "passed": True},
    ]
    res_oracle_insufficient = gate.check_oracle_3x(valid_spec, execution_records=bad_oracle_count)
    assert res_oracle_insufficient.passed is False

    # Failing oracle run in execution records
    failing_oracle = [
        {"agent_kind": "oracle", "passed": True},
        {"agent_kind": "oracle", "passed": False},
        {"agent_kind": "oracle", "passed": True},
    ]
    res_oracle_fail = gate.check_oracle_3x(valid_spec, execution_records=failing_oracle)
    assert res_oracle_fail.passed is False

    # Missing NOP in execution records
    res_nop_missing = gate.check_nop_failed(valid_spec, execution_records=valid_exec_records[:3])
    assert res_nop_missing.passed is False

    # NOP unexpectedly passing in execution records
    bad_nop_records = [
        {"agent_kind": "nop", "passed": True},
    ]
    res_nop_pass = gate.check_nop_failed(valid_spec, execution_records=bad_nop_records)
    assert res_nop_pass.passed is False

    # Insufficient mutant records (< 3)
    res_mut_insufficient, _, _ = gate.check_mutants(
        valid_spec, mutant_records=valid_mutant_records[:2]
    )
    assert res_mut_insufficient.passed is False

    # Escaping mutant in mutant records
    escaping_mutant_records = [
        {"name": "m1", "passed": False},
        {"name": "m2", "passed": True},
        {"name": "m3", "passed": False},
    ]
    res_mut_escape, tested, failed = gate.check_mutants(
        valid_spec, mutant_records=escaping_mutant_records
    )
    assert res_mut_escape.passed is False
    assert tested == 3
    assert failed == 2
