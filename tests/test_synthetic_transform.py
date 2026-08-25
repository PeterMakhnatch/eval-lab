"""Tests for the deterministic synthetic task transformation engine (src/evallab/synthetic_transform.py)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from evallab.synthetic_contracts import PerturbationFamily, SyntheticEvalSpec
from evallab.synthetic_transform import (
    ContextPressureConfig,
    ContextPressureInjector,
    EpistemicRestraintPairer,
    PreconditionSpec,
    PreconditionType,
    PressureChannel,
    ToolFaultConfig,
    ToolFaultInjector,
    ToolFaultMode,
    ToolFaultState,
    compute_deterministic_dir_digest,
    estimate_token_count,
    transform_task,
)


@pytest.fixture
def temp_task_dir(tmp_path: Path) -> Path:
    """Create a minimal valid base task directory for testing."""
    base_dir = tmp_path / "sample_base_task"
    base_dir.mkdir(parents=True, exist_ok=True)

    task_toml = """schema_version = "1.4"
artifacts = ["/app/output/result.txt"]

[task]
name = "synthetic/base-sample"
version = "1.0.0"
description = "Base task for synthetic transformation tests"
keywords = ["synthetic", "test", "deterministic"]

[[task.authors]]
name = "Eval Lab"
email = "eval-lab@example.invalid"

[metadata]
difficulty = "easy"
category = "synthetic-eval"
tags = ["test", "fixture"]

[agent]
timeout_sec = 30.0

[verifier]
timeout_sec = 30.0
environment_mode = "separate"

[environment]
network_mode = "no-network"
build_timeout_sec = 120.0
cpus = 1
memory_mb = 256
storage_mb = 512
"""
    (base_dir / "task.toml").write_text(task_toml, encoding="utf-8")

    instruction_md = (
        "# Process Data\nConvert /app/input.txt to uppercase and save to /app/output/result.txt\n"
    )
    (base_dir / "instruction.md").write_text(instruction_md, encoding="utf-8")

    env_dir = base_dir / "environment"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "Dockerfile").write_text("FROM alpine:3.19\n", encoding="utf-8")
    (env_dir / "input.txt").write_text("hello world base data\n", encoding="utf-8")

    tests_dir = base_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (tests_dir / "verify.sh").write_text(
        "#!/bin/sh\ntest -f /app/output/result.txt\n", encoding="utf-8"
    )

    sol_dir = base_dir / "solution"
    sol_dir.mkdir(parents=True, exist_ok=True)
    (sol_dir / "solve.sh").write_text(
        "#!/bin/sh\n"
        "mkdir -p /app/output\n"
        "tr '[:lower:]' '[:upper:]' < /app/input.txt > /app/output/result.txt\n",
        encoding="utf-8",
    )

    return base_dir


# =============================================================================
# Test Suite 1: Tool Unreliability (Family A)
# =============================================================================


def test_tool_fault_state_transient_recovery():
    """Test transient fault logic: fails on first N touches, then recovers."""
    cfg = ToolFaultConfig(
        tool_name="database_cli",
        fault_mode=ToolFaultMode.TRANSIENT,
        fault_count=3,
        error_type="ConnectionTimeoutError",
        error_message="database lock timeout",
    )
    state = ToolFaultState(config=cfg)

    assert not state.is_recovered
    assert state.touches == 0

    # Touch 1: Failure
    success, msg, step = state.record_invocation(command="SELECT * FROM table")
    assert not success
    assert step == 1
    assert "ConnectionTimeoutError" in msg
    assert "touch 1/3" in msg
    assert not state.is_recovered

    # Touch 2: Failure
    success, msg, step = state.record_invocation(command="SELECT * FROM table")
    assert not success
    assert step == 2
    assert "touch 2/3" in msg
    assert not state.is_recovered

    # Touch 3: Failure (boundary)
    success, msg, step = state.record_invocation(command="SELECT * FROM table")
    assert not success
    assert step == 3
    assert "touch 3/3" in msg
    assert not state.is_recovered

    # Touch 4: Recovery
    success, msg, step = state.record_invocation(command="SELECT * FROM table")
    assert success
    assert step == 4
    assert "recovered" in msg.lower()
    assert state.is_recovered

    # Touch 5: Stays recovered
    success, msg, step = state.record_invocation(command="SELECT * FROM table")
    assert success
    assert step == 5
    assert state.is_recovered

    # Reset
    state.reset()
    assert state.touches == 0
    assert not state.is_recovered


def test_tool_fault_state_persistent_fault():
    """Test persistent fault logic: always fails and points to alternative tool."""
    cfg = ToolFaultConfig(
        tool_name="legacy_fetch",
        fault_mode=ToolFaultMode.PERSISTENT,
        error_type="EndpointDeprecatedError",
        error_message="API v1 retired permanently",
        alternative_tool="modern_fetch",
    )
    state = ToolFaultState(config=cfg)

    for i in range(1, 6):
        success, msg, step = state.record_invocation(command="fetch-item")
        assert not success
        assert step == i
        assert "EndpointDeprecatedError" in msg
        assert "alternative tool: modern_fetch" in msg
        assert not state.is_recovered


def test_tool_fault_state_env_override():
    """Test fault bypass via environment variable override."""
    cfg = ToolFaultConfig(
        tool_name="api_tool",
        fault_mode=ToolFaultMode.PERSISTENT,
        env_var_override="MY_OVERRIDE",
    )
    state = ToolFaultState(config=cfg)

    # Without override -> fails
    success, _, _ = state.record_invocation(command="call")
    assert not success

    # With override -> succeeds
    success, msg, _ = state.record_invocation(command="call", env={"MY_OVERRIDE": "bypass"})
    assert success
    assert "bypassed" in msg


def test_tool_fault_injector_file_generation(temp_task_dir: Path, tmp_path: Path):
    """Test task directory transformation by ToolFaultInjector."""
    out_dir = tmp_path / "perturbed_tool_task"
    cfg = ToolFaultConfig(
        tool_name="curl_proxy",
        fault_mode=ToolFaultMode.TRANSIENT,
        fault_count=2,
        error_type="RateLimitExceeded",
        error_message="HTTP 429 Too Many Requests",
    )
    injector = ToolFaultInjector(config=cfg)
    spec = injector.transform(base_task_dir=temp_task_dir, output_dir=out_dir, config=cfg, seed=101)

    assert spec.family == PerturbationFamily.TOOL_UNRELIABILITY
    assert spec.perturbation_type == "transient_fault"
    assert spec.verify_spec_id()

    # Check generated shims
    shim_bash = out_dir / "environment" / "shims" / "curl_proxy"
    shim_py = out_dir / "environment" / "shims" / "curl_proxy.py"
    assert shim_bash.is_file()
    assert shim_py.is_file()
    assert "RateLimitExceeded" in shim_bash.read_text(encoding="utf-8")
    assert "RateLimitExceeded" in shim_py.read_text(encoding="utf-8")

    # Check spec file
    spec_json = out_dir / "synthetic_spec.json"
    assert spec_json.is_file()
    loaded_spec = SyntheticEvalSpec.model_validate_json(spec_json.read_text(encoding="utf-8"))
    assert loaded_spec.spec_id == spec.spec_id


def test_tool_fault_bash_shim_execution(tmp_path: Path):
    """Test execution of generated bash tool shim script."""
    state_file = tmp_path / "test_shim_state.json"
    cfg = ToolFaultConfig(
        tool_name="test_tool",
        fault_mode=ToolFaultMode.TRANSIENT,
        fault_count=2,
        error_type="ServiceUnavailable",
        error_message="retry later",
        state_file_path=str(state_file),
    )
    injector = ToolFaultInjector(config=cfg)
    shim_content = injector.generate_bash_shim(cfg)
    shim_script = tmp_path / "test_tool_shim.sh"
    shim_script.write_text(shim_content, encoding="utf-8")
    shim_script.chmod(0o755)

    # Invocation 1: Exit code 75 (transient fail)
    res1 = subprocess.run(["sh", str(shim_script)], capture_output=True, text=True)
    assert res1.returncode == 75
    assert "ServiceUnavailable" in res1.stderr

    # Invocation 2: Exit code 75 (transient fail)
    res2 = subprocess.run(["sh", str(shim_script)], capture_output=True, text=True)
    assert res2.returncode == 75

    # Invocation 3: Exit code 0 (recovered)
    res3 = subprocess.run(["sh", str(shim_script)], capture_output=True, text=True)
    assert res3.returncode == 0
    assert "recovered" in res3.stdout.lower()


# =============================================================================
# Test Suite 2: Epistemic Restraint (Family B)
# =============================================================================


def test_epistemic_restraint_pair_generation(temp_task_dir: Path, tmp_path: Path):
    """Test paired generation producing symmetrical <task>__act and <task>__abstain tasks."""
    out_base = tmp_path / "epistemic_pairs"
    prec = PreconditionSpec(
        precondition_id="auth_token_presence",
        precondition_type=PreconditionType.MISSING_FILE,
        description="Required secret token /app/secrets/token.jwt must exist.",
        target_path="environment/token.jwt",
        act_content="JWT_SECRET_VALID_HEADER.PAYLOAD.SIGNATURE\n",
        abstention_token="ABSTAIN: PRECONDITION_UNSATISFIED: auth_token_presence",
        verdict_path="/app/output/verdict.txt",
        forbidden_mutations=["/app/output/result.txt"],
    )
    pairer = EpistemicRestraintPairer(precondition=prec)
    act_spec, abstain_spec = pairer.generate_pair(
        base_task_dir=temp_task_dir,
        output_base_dir=out_base,
        precondition=prec,
        seed=2026,
    )

    # Lineage and Family pairing symmetry
    assert act_spec.lineage_id == abstain_spec.lineage_id
    assert act_spec.family_id == abstain_spec.family_id
    assert act_spec.family == PerturbationFamily.EPISTEMIC_RESTRAINT
    assert abstain_spec.family == PerturbationFamily.EPISTEMIC_RESTRAINT
    assert act_spec.verify_spec_id()
    assert abstain_spec.verify_spec_id()

    act_dir = out_base / f"{temp_task_dir.name}__act"
    abstain_dir = out_base / f"{temp_task_dir.name}__abstain"
    assert act_dir.is_dir()
    assert abstain_dir.is_dir()

    # In ACT task: precondition file is present
    assert (act_dir / "environment" / "token.jwt").is_file()
    assert (
        act_dir / "environment" / "token.jwt"
    ).read_text() == "JWT_SECRET_VALID_HEADER.PAYLOAD.SIGNATURE\n"

    # In ABSTAIN task: precondition file is absent
    assert not (abstain_dir / "environment" / "token.jwt").exists()

    # In ABSTAIN task: verifier contains zero-forbidden-mutation and token check
    abstain_verify = (abstain_dir / "tests" / "verify.sh").read_text(encoding="utf-8")
    assert "ABSTAIN: PRECONDITION_UNSATISFIED: auth_token_presence" in abstain_verify
    assert "/app/output/result.txt" in abstain_verify
    assert "MUTATION_ERROR" in abstain_verify


def test_epistemic_restraint_contradictory_requirement(temp_task_dir: Path, tmp_path: Path):
    """Test epistemic restraint with contradictory instruction requirements."""
    out_dir = tmp_path / "epistemic_contradictory"
    prec = PreconditionSpec(
        precondition_id="mutual_exclusion_rule",
        precondition_type=PreconditionType.CONTRADICTORY_REQUIREMENT,
        description="Instructions contain mutually incompatible constraints.",
        abstention_token="ABSTAIN: CONTRADICTION_DETECTED",
        verdict_path="/app/output/verdict.txt",
    )
    pairer = EpistemicRestraintPairer(precondition=prec)
    spec = pairer.transform_abstain(
        base_task_dir=temp_task_dir,
        output_dir=out_dir,
        precondition=prec,
        seed=777,
    )

    assert spec.perturbation_type == "abstain_pair"
    instruction_text = (out_dir / "instruction.md").read_text(encoding="utf-8")
    assert "Mandatory Constraint Conflict" in instruction_text
    assert "ABSTAIN: CONTRADICTION_DETECTED" in instruction_text


def test_epistemic_restraint_verifier_script_execution(tmp_path: Path):
    """Test execution behavior of generated epistemic restraint verifier script."""
    verdict_file = tmp_path / "verdict.txt"
    forbidden_file = tmp_path / "forbidden_result.txt"
    token = "ABSTAIN: PRECONDITION_UNSATISFIED: schema_error"

    prec = PreconditionSpec(
        precondition_id="schema_error",
        abstention_token=token,
        verdict_path=str(verdict_file),
        forbidden_mutations=[str(forbidden_file)],
    )
    pairer = EpistemicRestraintPairer(precondition=prec)

    # Generate verifier in a dummy task
    dummy_out = tmp_path / "dummy_abstain"
    pairer.transform_abstain(
        base_task_dir=tmp_path, output_dir=dummy_out, precondition=prec, seed=1
    )
    verify_script = dummy_out / "tests" / "verify.sh"
    assert verify_script.is_file()

    # Scenario 1: Verdict file missing -> FAIL
    if verdict_file.exists():
        verdict_file.unlink()
    res1 = subprocess.run(["sh", str(verify_script)], capture_output=True, text=True)
    assert res1.returncode != 0
    assert "not found" in res1.stderr

    # Scenario 2: Verdict file has wrong token -> FAIL
    verdict_file.write_text("WRONG TOKEN\n", encoding="utf-8")
    res2 = subprocess.run(["sh", str(verify_script)], capture_output=True, text=True)
    assert res2.returncode != 0
    assert "verdict mismatch" in res2.stderr

    # Scenario 3: Correct token, but forbidden mutation exists -> FAIL
    verdict_file.write_text(token + "\n", encoding="utf-8")
    forbidden_file.write_text("FORBIDDEN OUTPUT CREATED\n", encoding="utf-8")
    res3 = subprocess.run(["sh", str(verify_script)], capture_output=True, text=True)
    assert res3.returncode != 0
    assert "MUTATION_ERROR" in res3.stderr

    # Scenario 4: Correct token AND zero forbidden mutations -> PASS (0)
    forbidden_file.unlink()
    res4 = subprocess.run(["sh", str(verify_script)], capture_output=True, text=True)
    assert res4.returncode == 0
    assert "OK: abstention token validated" in res4.stdout


# =============================================================================
# Test Suite 3: Context Pressure (Family C)
# =============================================================================


def test_context_pressure_token_tracking_and_provenance(temp_task_dir: Path, tmp_path: Path):
    """Test context volume expansion, token count tracking, and labeled provenance."""
    out_dir = tmp_path / "context_pressure_task"
    cfg = ContextPressureConfig(
        target_token_count=1200,
        channels=[
            PressureChannel.SYSTEM_TRACE,
            PressureChannel.ENV_METRICS,
            PressureChannel.AUDIT_LOG,
        ],
        output_subdir="environment/logs",
        provenance_prefix="synthetic/context_pressure",
    )
    injector = ContextPressureInjector(config=cfg)
    spec = injector.apply(base_task_dir=temp_task_dir, output_dir=out_dir, config=cfg, seed=54321)

    assert spec.family == PerturbationFamily.CONTEXT_PRESSURE
    assert spec.perturbation_type == "observation_volume_expansion"
    assert spec.verify_spec_id()

    realized_tokens = spec.parameters["realized_token_count"]
    assert realized_tokens >= 1200
    assert spec.parameters["block_count"] > 0
    assert len(spec.parameters["source_blocks"]) == spec.parameters["block_count"]

    # Verify that log files exist and contain explicit labeled provenance headers
    logs_dir = out_dir / "environment" / "logs"
    assert logs_dir.is_dir()
    log_files = list(logs_dir.glob("*.log"))
    assert len(log_files) > 0

    total_observed_tokens = 0
    for lf in log_files:
        content = lf.read_text(encoding="utf-8")
        assert "<!-- PROVENANCE: synthetic/context_pressure" in content
        total_observed_tokens += estimate_token_count(content)

    assert total_observed_tokens >= realized_tokens

    # Verify task semantics preservation: base input and solution remain untouched
    assert (out_dir / "environment" / "input.txt").read_text() == "hello world base data\n"
    assert (out_dir / "solution" / "solve.sh").is_file()


def test_context_pressure_preserves_task_solvability(temp_task_dir: Path, tmp_path: Path):
    """Test that context pressure perturbation preserves original task solvability."""
    out_dir = tmp_path / "solvable_ctx_task"
    cfg = ContextPressureConfig(target_token_count=600)
    injector = ContextPressureInjector(config=cfg)
    injector.apply(base_task_dir=temp_task_dir, output_dir=out_dir, config=cfg, seed=123)

    # Execute original solve.sh logic in the transformed environment
    env_input = out_dir / "environment" / "input.txt"
    app_output = tmp_path / "mock_app" / "output"
    app_output.mkdir(parents=True, exist_ok=True)
    result_txt = app_output / "result.txt"

    # Simulate solve.sh
    result_txt.write_text(env_input.read_text().upper(), encoding="utf-8")
    assert result_txt.read_text() == "HELLO WORLD BASE DATA\n"


# =============================================================================
# Test Suite 4: Determinism & Seed Reproducibility
# =============================================================================


def test_seed_determinism_identical_digests(temp_task_dir: Path, tmp_path: Path):
    """Test that identical seeds produce byte-identical files and identical SHA-256 digests."""
    dir_run1 = tmp_path / "run_seed_42_a"
    dir_run2 = tmp_path / "run_seed_42_b"
    dir_run3 = tmp_path / "run_seed_99"

    spec_dict = {
        "family": "context_pressure",
        "target_token_count": 800,
        "channels": ["system_trace", "audit_log"],
    }

    spec1 = transform_task(
        base_task_dir=temp_task_dir, perturbation_spec=spec_dict, output_dir=dir_run1, seed=42
    )
    spec2 = transform_task(
        base_task_dir=temp_task_dir, perturbation_spec=spec_dict, output_dir=dir_run2, seed=42
    )
    spec3 = transform_task(
        base_task_dir=temp_task_dir, perturbation_spec=spec_dict, output_dir=dir_run3, seed=99
    )

    # Identical seed -> identical digests and spec_id
    assert spec1.spec_id == spec2.spec_id
    assert spec1.generated_task_digest == spec2.generated_task_digest
    assert spec1.lineage_id == spec2.lineage_id

    # Directory digests match
    digest1 = compute_deterministic_dir_digest(dir_run1)
    digest2 = compute_deterministic_dir_digest(dir_run2)
    digest3 = compute_deterministic_dir_digest(dir_run3)
    assert digest1 == digest2
    assert digest1 != digest3
    assert spec1.spec_id != spec3.spec_id


def test_transform_task_dispatcher_all_families(temp_task_dir: Path, tmp_path: Path):
    """Test high-level transform_task dispatcher across all three capability perturbation families."""
    # 1. Tool Unreliability
    out_tool = tmp_path / "disp_tool"
    spec_tool = transform_task(
        base_task_dir=temp_task_dir,
        perturbation_spec={
            "family": "tool_unreliability",
            "tool_name": "http_client",
            "fault_mode": "transient",
            "fault_count": 2,
        },
        output_dir=out_tool,
        seed=10,
    )
    assert spec_tool.family == PerturbationFamily.TOOL_UNRELIABILITY
    assert spec_tool.verify_spec_id()

    # 2. Epistemic Restraint
    out_epistemic = tmp_path / "disp_epistemic"
    spec_epistemic = transform_task(
        base_task_dir=temp_task_dir,
        perturbation_spec={
            "family": "epistemic_restraint",
            "pair_variant": "abstain",
            "precondition_id": "schema_spec",
        },
        output_dir=out_epistemic,
        seed=20,
    )
    assert spec_epistemic.family == PerturbationFamily.EPISTEMIC_RESTRAINT
    assert spec_epistemic.verify_spec_id()

    # 3. Context Pressure
    out_ctx = tmp_path / "disp_context"
    spec_ctx = transform_task(
        base_task_dir=temp_task_dir,
        perturbation_spec={
            "family": "context_pressure",
            "target_token_count": 500,
        },
        output_dir=out_ctx,
        seed=30,
    )
    assert spec_ctx.family == PerturbationFamily.CONTEXT_PRESSURE
    assert spec_ctx.verify_spec_id()
