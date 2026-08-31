"""Unit and adversarial tests for fail-closed multi-eval platform layer (multi_eval.py)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from evallab.multi_eval import (
    ExecutionIntent,
    HarnessIdentity,
    MultiEvalTaskSpec,
    ParityBinding,
    ParityStatus,
    RefusalCode,
    RunnerCapabilities,
    RunnerKind,
    RunnerOutcome,
    TaskRequirements,
    compute_harness_digest,
    compute_parity_binding_digest,
    get_runner_capabilities,
    plan_multi_eval_execution,
    reconcile_parity_results,
    validate_runner_capabilities,
)

SHA256_A = "sha256:" + "a" * 64
SHA256_B = "sha256:" + "b" * 64
SHA256_C = "sha256:" + "c" * 64
SHA256_D = "sha256:" + "d" * 64
SHA256_E = "sha256:" + "e" * 64
SHA256_F = "sha256:" + "f" * 64
SHA256_G = "sha256:" + "1" * 64
SHA256_H = "sha256:" + "0" * 64


def _make_harness(
    runner: RunnerKind,
    version: str = "1.0.0",
    image: str = "ubuntu:22.04",
    env_digest: str = SHA256_A,
    prompt_digest: str = SHA256_B,
    tool_digest: str = SHA256_C,
    scaffold_digest: str = SHA256_D,
    scaffold_version: str = "v1",
    model_config_digest: str = SHA256_E,
    verifier_digest: str = SHA256_F,
    parameters: dict | None = None,
) -> HarnessIdentity:
    return HarnessIdentity(
        runner_kind=runner,
        runner_version=version,
        environment_kind="docker",
        environment_image=image,
        environment_digest=env_digest,
        prompt_digest=prompt_digest,
        tool_schema_digest=tool_digest,
        scaffold_digest=scaffold_digest,
        scaffold_version=scaffold_version,
        model_config_digest=model_config_digest,
        verifier_digest=verifier_digest,
        harness_parameters=parameters or {"timeout": 600},
    )


def _make_binding(
    task_digest: str = SHA256_A,
    pair_id: str = "pair-001",
    expected_lanes: tuple[RunnerKind, RunnerKind] = (RunnerKind.HARBOR, RunnerKind.INSPECT_HARBOR),
    allowed_delta: tuple[str, ...] = ("runner_lane",),
    outcome_namespace: str = "benchmark",
    outcome_name: str = "accuracy",
    verifier_digest: str = SHA256_F,
) -> ParityBinding:
    return ParityBinding(
        task_digest=task_digest,
        pair_id=pair_id,
        expected_lanes=expected_lanes,
        allowed_delta=allowed_delta,
        outcome_namespace=outcome_namespace,
        outcome_name=outcome_name,
        verifier_digest=verifier_digest,
    )


def _make_outcome(
    runner: RunnerKind,
    trial_id: str = "trial-001",
    task_digest: str = SHA256_A,
    pair_id: str = "pair-001",
    attempt_id: str = "attempt-001",
    outcome_namespace: str = "benchmark",
    outcome_name: str = "accuracy",
    producer_kind: str = "verifier_exit_code",
    verifier_digest: str = SHA256_F,
    verifier_reward: float | None = 1.0,
    verifier_passed: bool = True,
    artifact_digests: dict[str, str] | None = None,
    step_count: int = 5,
    total_tokens: int = 1000,
    duration_seconds: float = 12.0,
    harness: HarnessIdentity | None = None,
    source_revision: str = "git-rev-abc",
    raw_log_digest: str = SHA256_G,
) -> RunnerOutcome:
    if harness is None:
        harness = _make_harness(runner)
    if artifact_digests is None:
        artifact_digests = {"patch.diff": SHA256_A}
    return RunnerOutcome(
        runner_kind=runner,
        trial_id=trial_id,
        task_digest=task_digest,
        parity_pair_id=pair_id,
        attempt_id=attempt_id,
        outcome_namespace=outcome_namespace,
        outcome_name=outcome_name,
        deterministic_producer_kind=producer_kind,
        verifier_digest=verifier_digest,
        verifier_reward=verifier_reward,
        verifier_passed=verifier_passed,
        artifact_digests=artifact_digests,
        step_count=step_count,
        total_tokens=total_tokens,
        duration_seconds=duration_seconds,
        harness_identity=harness,
        source_revision=source_revision,
        raw_log_digest=raw_log_digest,
    )


def test_runner_kinds_and_capabilities_registry() -> None:
    """Verify that all runner kinds have registered capability profiles with correct flags."""
    assert RunnerKind.HARBOR == "harbor"
    assert RunnerKind.INSPECT == "inspect"
    assert RunnerKind.INSPECT_HARBOR == "inspect_harbor"
    assert RunnerKind.IMPORT_ONLY == "import_only"

    harbor_caps = get_runner_capabilities(RunnerKind.HARBOR)
    assert harbor_caps.schema_version == "runner-capabilities/v1"
    assert harbor_caps.profile_version == "v1"
    assert harbor_caps.runner_kind == RunnerKind.HARBOR
    assert harbor_caps.supports_active_execution is True
    assert harbor_caps.supports_multi_step is True
    assert harbor_caps.supports_prior_trajectories is True
    assert harbor_caps.supports_mcp_servers is True
    assert harbor_caps.supports_skills_dir is True
    assert harbor_caps.supports_network_allowlist is True
    assert harbor_caps.supports_windows is False
    assert harbor_caps.supports_docker is True
    assert harbor_caps.supports_docker_compose is True
    assert harbor_caps.supports_hidden_verifier_containers is True
    assert harbor_caps.supports_parity_lane is True

    inspect_caps = get_runner_capabilities("inspect")
    assert inspect_caps.runner_kind == RunnerKind.INSPECT
    assert inspect_caps.supports_active_execution is True
    assert inspect_caps.supports_network_allowlist is False
    assert inspect_caps.supports_docker_compose is False
    assert inspect_caps.supports_hidden_verifier_containers is False
    assert inspect_caps.supports_parity_lane is False

    inspect_harbor_caps = get_runner_capabilities(RunnerKind.INSPECT_HARBOR)
    assert inspect_harbor_caps.runner_kind == RunnerKind.INSPECT_HARBOR
    assert inspect_harbor_caps.supports_active_execution is True
    assert inspect_harbor_caps.supports_docker_compose is False
    assert inspect_harbor_caps.supports_hidden_verifier_containers is False
    assert inspect_harbor_caps.supports_parity_lane is True

    import_caps = get_runner_capabilities(RunnerKind.IMPORT_ONLY)
    assert import_caps.runner_kind == RunnerKind.IMPORT_ONLY
    assert import_caps.supports_active_execution is False
    assert import_caps.supports_docker is False
    assert import_caps.supports_parity_lane is False


def test_validate_runner_capabilities_refusals() -> None:
    """Check refusal generation for unsupported runner features against task requirements."""
    harbor_caps = get_runner_capabilities(RunnerKind.HARBOR)
    inspect_caps = get_runner_capabilities(RunnerKind.INSPECT)
    import_caps = get_runner_capabilities(RunnerKind.IMPORT_ONLY)

    # Standard task against Harbor: no refusals
    std_reqs = TaskRequirements()
    assert validate_runner_capabilities(harbor_caps, std_reqs) == []

    # Import-only runner refuses active execution
    import_refusals = validate_runner_capabilities(import_caps, std_reqs)
    assert len(import_refusals) == 1
    assert import_refusals[0].code == RefusalCode.IMPORT_ONLY_NO_EXECUTION
    assert "supports_active_execution" in import_refusals[0].missing_capabilities

    # Docker compose requirement refuses Inspect
    compose_reqs = TaskRequirements(requires_docker_compose=True)
    compose_refusals = validate_runner_capabilities(inspect_caps, compose_reqs)
    assert any(r.code == RefusalCode.UNSUPPORTED_DOCKER_COMPOSE for r in compose_refusals)

    # Hidden verifier containers refuse Inspect
    hidden_reqs = TaskRequirements(requires_hidden_verifier_containers=True)
    hidden_refusals = validate_runner_capabilities(inspect_caps, hidden_reqs)
    assert any(r.code == RefusalCode.UNSUPPORTED_HIDDEN_VERIFIER for r in hidden_refusals)

    # Network allowlist refuses Inspect
    net_reqs = TaskRequirements(requires_network_allowlist=True)
    net_refusals = validate_runner_capabilities(inspect_caps, net_reqs)
    assert any(r.code == RefusalCode.UNSUPPORTED_NETWORK_ALLOWLIST for r in net_refusals)

    # Windows target OS refuses Harbor and Inspect
    win_reqs = TaskRequirements(requires_windows=True, target_os="windows")
    win_refusals = validate_runner_capabilities(harbor_caps, win_reqs)
    assert any(r.code == RefusalCode.UNSUPPORTED_WINDOWS for r in win_refusals)

    # Custom capability checks (MCP, Skills, Prior trajectories, Multi-step)
    restricted_caps = RunnerCapabilities(
        runner_kind=RunnerKind.HARBOR,
        supports_active_execution=True,
        supports_multi_step=False,
        supports_prior_trajectories=False,
        supports_mcp_servers=False,
        supports_skills_dir=False,
        supports_network_allowlist=False,
        supports_windows=False,
        supports_docker=True,
        supports_docker_compose=False,
        supports_hidden_verifier_containers=False,
        supports_parity_lane=True,
    )
    all_reqs = TaskRequirements(
        requires_multi_step=True,
        requires_prior_trajectories=True,
        requires_mcp_servers=True,
        requires_skills_dir=True,
    )
    custom_refusals = validate_runner_capabilities(restricted_caps, all_reqs)
    codes = {r.code for r in custom_refusals}
    assert RefusalCode.UNSUPPORTED_MULTI_STEP in codes
    assert RefusalCode.UNSUPPORTED_PRIOR_TRAJECTORIES in codes
    assert RefusalCode.UNSUPPORTED_MCP_SERVERS in codes
    assert RefusalCode.UNSUPPORTED_SKILLS_DIR in codes


def test_harness_identity_contract_and_digests() -> None:
    """Test HarnessIdentity automatic digest resolution, explicit validation, and error cases."""
    harness = _make_harness(RunnerKind.HARBOR)
    assert harness.harness_digest.startswith("sha256:")
    assert len(harness.harness_digest) == 71

    # Deterministic digest generation
    computed = compute_harness_digest(
        runner_kind=RunnerKind.HARBOR,
        runner_version="1.0.0",
        environment_kind="docker",
        environment_image="ubuntu:22.04",
        environment_digest=SHA256_A,
        prompt_digest=SHA256_B,
        tool_schema_digest=SHA256_C,
        scaffold_digest=SHA256_D,
        scaffold_version="v1",
        model_config_digest=SHA256_E,
        verifier_digest=SHA256_F,
        harness_parameters={"timeout": 600},
    )
    assert harness.harness_digest == computed

    # Explicit matching harness_digest succeeds
    harness_explicit = HarnessIdentity(
        runner_kind=RunnerKind.HARBOR,
        runner_version="1.0.0",
        environment_kind="docker",
        environment_image="ubuntu:22.04",
        environment_digest=SHA256_A,
        prompt_digest=SHA256_B,
        tool_schema_digest=SHA256_C,
        scaffold_digest=SHA256_D,
        scaffold_version="v1",
        model_config_digest=SHA256_E,
        verifier_digest=SHA256_F,
        harness_parameters={"timeout": 600},
        harness_digest=computed,
    )
    assert harness_explicit.harness_digest == computed

    # Explicit mismatched harness_digest raises ValidationError
    with pytest.raises(ValidationError):
        HarnessIdentity(
            runner_kind=RunnerKind.HARBOR,
            runner_version="1.0.0",
            environment_kind="docker",
            environment_image="ubuntu:22.04",
            environment_digest=SHA256_A,
            prompt_digest=SHA256_B,
            tool_schema_digest=SHA256_C,
            scaffold_digest=SHA256_D,
            scaffold_version="v1",
            model_config_digest=SHA256_E,
            verifier_digest=SHA256_F,
            harness_parameters={"timeout": 600},
            harness_digest="sha256:" + "0" * 64,
        )

    # Invalid sha256 pattern raises ValidationError
    with pytest.raises(ValidationError):
        _make_harness(RunnerKind.HARBOR, env_digest="not-a-sha")


def test_parity_binding_contract_and_digests() -> None:
    """Test ParityBinding validation, immutable expected_lanes constraint, and digests."""
    binding = _make_binding()
    assert binding.binding_digest.startswith("sha256:")
    assert binding.expected_lanes == (RunnerKind.HARBOR, RunnerKind.INSPECT_HARBOR)

    computed = compute_parity_binding_digest(
        task_digest=SHA256_A,
        pair_id="pair-001",
        expected_lanes=(RunnerKind.HARBOR, RunnerKind.INSPECT_HARBOR),
        allowed_delta=("runner_lane",),
        outcome_namespace="benchmark",
        outcome_name="accuracy",
        verifier_digest=SHA256_F,
    )
    assert binding.binding_digest == computed

    # Mismatched binding_digest raises ValidationError
    with pytest.raises(ValidationError):
        ParityBinding(
            task_digest=SHA256_A,
            pair_id="pair-001",
            expected_lanes=(RunnerKind.HARBOR, RunnerKind.INSPECT_HARBOR),
            allowed_delta=("runner_lane",),
            outcome_namespace="benchmark",
            outcome_name="accuracy",
            verifier_digest=SHA256_F,
            binding_digest="sha256:" + "9" * 64,
        )

    # Invalid expected_lanes raises ValidationError
    with pytest.raises(ValidationError):
        ParityBinding(
            task_digest=SHA256_A,
            pair_id="pair-001",
            expected_lanes=(RunnerKind.HARBOR, RunnerKind.INSPECT),
            outcome_namespace="benchmark",
            outcome_name="accuracy",
            verifier_digest=SHA256_F,
        )


def test_multi_eval_task_spec_validation() -> None:
    """Test MultiEvalTaskSpec explicit canonical_runner rules and RSI constraints."""
    # Valid TaskSpec
    spec = MultiEvalTaskSpec(
        task_id="task-1",
        benchmark_family="swe_bench",
        canonical_runner=RunnerKind.INSPECT,
        task_digest=SHA256_A,
    )
    assert spec.task_id == "task-1"
    assert spec.canonical_runner == RunnerKind.INSPECT

    # Missing task_digest raises ValidationError
    with pytest.raises(ValidationError):
        MultiEvalTaskSpec(
            task_id="task-1",
            benchmark_family="swe_bench",
            canonical_runner=RunnerKind.HARBOR,
            task_digest="bad-digest",
        )

    # INSPECT_HARBOR can never be canonical_runner
    with pytest.raises(ValidationError, match="INSPECT_HARBOR is a parity lane only"):
        MultiEvalTaskSpec(
            task_id="task-2",
            benchmark_family="swe_bench",
            canonical_runner=RunnerKind.INSPECT_HARBOR,
            task_digest=SHA256_A,
        )

    # RSI benchmark family requires canonical_runner=HARBOR
    with pytest.raises(ValidationError, match="requires canonical_runner=RunnerKind.HARBOR"):
        MultiEvalTaskSpec(
            task_id="task-3",
            benchmark_family="rsi-secops",
            canonical_runner=RunnerKind.INSPECT,
            task_digest=SHA256_A,
        )

    # RSI with HARBOR succeeds
    rsi_spec = MultiEvalTaskSpec(
        task_id="task-4",
        benchmark_family="rsi-benchmark",
        canonical_runner=RunnerKind.HARBOR,
        task_digest=SHA256_A,
    )
    assert rsi_spec.canonical_runner == RunnerKind.HARBOR


def test_plan_multi_eval_execution_canonical_and_import() -> None:
    """Verify execution planning for canonical runs and import ingestion."""
    # Canonical run
    spec = MultiEvalTaskSpec(
        task_id="t1",
        benchmark_family="swe_bench",
        canonical_runner=RunnerKind.HARBOR,
        task_digest=SHA256_A,
    )
    plan = plan_multi_eval_execution(spec, execution_intent=ExecutionIntent.CANONICAL_RUN)
    assert plan.canonical_runner == RunnerKind.HARBOR
    assert plan.parity_runners == ()
    assert plan.is_refused is False
    assert plan.plan_digest.startswith("sha256:")

    # Import ingest with IMPORT_ONLY
    import_spec = MultiEvalTaskSpec(
        task_id="t2",
        benchmark_family="gaia",
        canonical_runner=RunnerKind.IMPORT_ONLY,
        task_digest=SHA256_A,
        requirements=TaskRequirements(requires_multi_step=False),
    )
    import_plan = plan_multi_eval_execution(
        import_spec, execution_intent=ExecutionIntent.IMPORT_INGEST
    )
    assert import_plan.canonical_runner == RunnerKind.IMPORT_ONLY
    assert import_plan.is_refused is False

    # Import ingest with non-IMPORT_ONLY canonical runner is refused
    bad_import_plan = plan_multi_eval_execution(
        spec, execution_intent=ExecutionIntent.IMPORT_INGEST
    )
    assert bad_import_plan.is_refused is True
    assert any(r.code == RefusalCode.INVALID_CANONICAL_RUNNER for r in bad_import_plan.refusals)


def test_plan_multi_eval_execution_parity_run() -> None:
    """Verify execution planning for parity runs with contracts and refusals."""
    spec = MultiEvalTaskSpec(
        task_id="t-parity",
        benchmark_family="harbor-tasks",
        canonical_runner=RunnerKind.HARBOR,
        task_digest=SHA256_A,
    )
    binding = _make_binding(task_digest=SHA256_A)
    h_canonical = _make_harness(RunnerKind.HARBOR)
    h_parity = _make_harness(RunnerKind.INSPECT_HARBOR)
    harnesses = {
        RunnerKind.HARBOR: h_canonical,
        RunnerKind.INSPECT_HARBOR: h_parity,
    }

    # Successful parity run planning
    plan = plan_multi_eval_execution(
        spec,
        execution_intent=ExecutionIntent.PARITY_RUN,
        parity_binding=binding,
        harness_identities=harnesses,
    )
    assert plan.is_refused is False
    assert plan.parity_runners == (RunnerKind.INSPECT_HARBOR,)
    assert plan.parity_binding == binding

    # Missing parity binding is refused
    plan_no_binding = plan_multi_eval_execution(
        spec,
        execution_intent=ExecutionIntent.PARITY_RUN,
        parity_binding=None,
    )
    assert plan_no_binding.is_refused is True
    assert any(r.code == RefusalCode.MISSING_PARITY_BINDING for r in plan_no_binding.refusals)

    # Mismatched task digest between task spec and binding is refused
    bad_binding = _make_binding(task_digest=SHA256_B)
    plan_bad_task = plan_multi_eval_execution(
        spec,
        execution_intent=ExecutionIntent.PARITY_RUN,
        parity_binding=bad_binding,
    )
    assert plan_bad_task.is_refused is True
    assert any(r.code == RefusalCode.BINDING_TASK_MISMATCH for r in plan_bad_task.refusals)

    # Parity run against non-HARBOR canonical runner is refused
    spec_inspect = MultiEvalTaskSpec(
        task_id="t-inspect",
        benchmark_family="inspect-tasks",
        canonical_runner=RunnerKind.INSPECT,
        task_digest=SHA256_A,
    )
    plan_bad_lane = plan_multi_eval_execution(
        spec_inspect,
        execution_intent=ExecutionIntent.PARITY_RUN,
        parity_binding=binding,
    )
    assert plan_bad_lane.is_refused is True
    assert any(r.code == RefusalCode.INVALID_PARITY_LANE for r in plan_bad_lane.refusals)

    # Mismatched harness identity between canonical and parity is refused
    h_parity_diff = _make_harness(RunnerKind.INSPECT_HARBOR, prompt_digest=SHA256_H)
    plan_mismatch_harness = plan_multi_eval_execution(
        spec,
        execution_intent=ExecutionIntent.PARITY_RUN,
        parity_binding=binding,
        harness_identities={
            RunnerKind.HARBOR: h_canonical,
            RunnerKind.INSPECT_HARBOR: h_parity_diff,
        },
    )
    assert plan_mismatch_harness.is_refused is True
    assert any(
        r.code == RefusalCode.MISMATCHED_ENVIRONMENT_IDENTITY
        for r in plan_mismatch_harness.refusals
    )


def test_plan_digest_determinism_and_sensitivity() -> None:
    """Verify that plan digest generation is deterministic and sensitive to changes."""
    spec_a = MultiEvalTaskSpec(
        task_id="task-det",
        benchmark_family="swe_bench",
        canonical_runner=RunnerKind.HARBOR,
        task_digest=SHA256_A,
    )
    spec_b = MultiEvalTaskSpec(
        task_id="task-det",
        benchmark_family="swe_bench",
        canonical_runner=RunnerKind.HARBOR,
        task_digest=SHA256_A,
    )
    plan_a = plan_multi_eval_execution(spec_a)
    plan_b = plan_multi_eval_execution(spec_b)
    assert plan_a.plan_digest == plan_b.plan_digest

    # Changing task_digest alters plan_digest
    spec_c = MultiEvalTaskSpec(
        task_id="task-det",
        benchmark_family="swe_bench",
        canonical_runner=RunnerKind.HARBOR,
        task_digest=SHA256_B,
    )
    plan_c = plan_multi_eval_execution(spec_c)
    assert plan_a.plan_digest != plan_c.plan_digest


def test_runner_outcome_contract_and_validation() -> None:
    """Test RunnerOutcome contract invariants, digest patterns, and validation."""
    outcome = _make_outcome(RunnerKind.HARBOR)
    assert outcome.runner_kind == RunnerKind.HARBOR
    assert outcome.artifact_digests == {"patch.diff": SHA256_A}

    # Invalid artifact digest pattern raises ValidationError
    with pytest.raises(ValidationError):
        _make_outcome(RunnerKind.HARBOR, artifact_digests={"patch.diff": "invalid-digest"})

    # Invalid raw log digest pattern raises ValidationError
    with pytest.raises(ValidationError):
        _make_outcome(RunnerKind.HARBOR, raw_log_digest="not-a-digest")


def test_reconcile_parity_results_verified() -> None:
    """Verify successful parity reconciliation with trajectory measurement observables."""
    h_canonical = _make_harness(RunnerKind.HARBOR)
    h_parity = _make_harness(RunnerKind.INSPECT_HARBOR)
    binding = _make_binding()

    canonical_outcome = _make_outcome(
        RunnerKind.HARBOR,
        trial_id="task-001__harbor",
        verifier_reward=1.0,
        verifier_passed=True,
        step_count=5,
        total_tokens=1200,
        duration_seconds=15.0,
        harness=h_canonical,
    )
    parity_outcome = _make_outcome(
        RunnerKind.INSPECT_HARBOR,
        trial_id="task-001__inspect_harbor",
        verifier_reward=1.0,
        verifier_passed=True,
        step_count=7,
        total_tokens=1450,
        duration_seconds=18.5,
        harness=h_parity,
    )

    result = reconcile_parity_results(canonical_outcome, parity_outcome, binding)

    assert result.parity_status == ParityStatus.PARITY_VERIFIED
    assert result.verifier_reward_reconciled is True
    assert result.verifier_passed_reconciled is True
    assert result.artifact_digests_reconciled is True
    assert result.harness_identity_reconciled is True
    assert result.binding_reconciled is True
    assert result.measurements.step_delta == 2
    assert result.measurements.token_delta == 250
    assert result.measurements.duration_delta_seconds == 3.5
    assert len(result.measurements.notes) == 3
    assert result.reconciliation_digest.startswith("sha256:")
    assert len(result.refusals) == 0


def test_reconcile_parity_results_divergent() -> None:
    """Verify divergent status when verifier rewards, pass status, or artifacts differ."""
    binding = _make_binding()

    # Divergent reward
    c_reward = _make_outcome(RunnerKind.HARBOR, verifier_reward=1.0, verifier_passed=True)
    p_reward = _make_outcome(RunnerKind.INSPECT_HARBOR, verifier_reward=0.0, verifier_passed=True)
    res_reward = reconcile_parity_results(c_reward, p_reward, binding)
    assert res_reward.parity_status == ParityStatus.PARITY_DIVERGENT
    assert res_reward.verifier_reward_reconciled is False

    # Divergent passed flag
    c_pass = _make_outcome(RunnerKind.HARBOR, verifier_reward=1.0, verifier_passed=True)
    p_pass = _make_outcome(RunnerKind.INSPECT_HARBOR, verifier_reward=1.0, verifier_passed=False)
    res_pass = reconcile_parity_results(c_pass, p_pass, binding)
    assert res_pass.parity_status == ParityStatus.PARITY_DIVERGENT
    assert res_pass.verifier_passed_reconciled is False

    # Divergent artifacts
    c_art = _make_outcome(
        RunnerKind.HARBOR,
        verifier_reward=1.0,
        verifier_passed=True,
        artifact_digests={"file.txt": SHA256_A},
    )
    p_art = _make_outcome(
        RunnerKind.INSPECT_HARBOR,
        verifier_reward=1.0,
        verifier_passed=True,
        artifact_digests={"file.txt": SHA256_B},
    )
    res_art = reconcile_parity_results(c_art, p_art, binding)
    assert res_art.parity_status == ParityStatus.PARITY_DIVERGENT
    assert res_art.artifact_digests_reconciled is False


def test_reconcile_parity_results_reward_tolerance() -> None:
    """Verify reward tolerance evaluation."""
    binding = _make_binding()
    c = _make_outcome(RunnerKind.HARBOR, verifier_reward=0.9999999)
    p = _make_outcome(RunnerKind.INSPECT_HARBOR, verifier_reward=1.0)

    # Within tolerance 1e-5
    res_within = reconcile_parity_results(c, p, binding, reward_tolerance=1e-5)
    assert res_within.verifier_reward_reconciled is True
    assert res_within.parity_status == ParityStatus.PARITY_VERIFIED

    # Exceeding tolerance 1e-8
    res_exceed = reconcile_parity_results(c, p, binding, reward_tolerance=1e-8)
    assert res_exceed.verifier_reward_reconciled is False
    assert res_exceed.parity_status == ParityStatus.PARITY_DIVERGENT


def test_reconcile_parity_results_fail_closed_adversarial() -> None:
    """Comprehensive adversarial tests verifying all fail-closed refusal rules."""
    binding = _make_binding()

    # 1. Same runner (Harbor <-> Harbor)
    c_harbor = _make_outcome(RunnerKind.HARBOR)
    p_harbor = _make_outcome(RunnerKind.HARBOR)
    res_same = reconcile_parity_results(c_harbor, p_harbor, binding)
    assert res_same.parity_status == ParityStatus.REFUSED
    assert any(r.code == RefusalCode.SAME_RUNNER_PARITY for r in res_same.refusals)

    # 2. Invalid parity lane (Harbor <-> Inspect)
    p_inspect = _make_outcome(RunnerKind.INSPECT)
    res_lane = reconcile_parity_results(c_harbor, p_inspect, binding)
    assert res_lane.parity_status == ParityStatus.REFUSED
    assert any(r.code == RefusalCode.INVALID_PARITY_LANE for r in res_lane.refusals)

    # 3. Mismatched pair IDs
    p_pair_diff = _make_outcome(RunnerKind.INSPECT_HARBOR, pair_id="pair-other")
    res_pair = reconcile_parity_results(c_harbor, p_pair_diff, binding)
    assert res_pair.parity_status == ParityStatus.REFUSED
    assert any(r.code == RefusalCode.BINDING_PAIR_MISMATCH for r in res_pair.refusals)

    # 4. Mismatched task digest
    p_task_diff = _make_outcome(RunnerKind.INSPECT_HARBOR, task_digest=SHA256_B)
    res_task = reconcile_parity_results(c_harbor, p_task_diff, binding)
    assert res_task.parity_status == ParityStatus.REFUSED
    assert any(r.code == RefusalCode.BINDING_TASK_MISMATCH for r in res_task.refusals)

    # 5. Mismatched verifier digest
    p_verifier_diff = _make_outcome(RunnerKind.INSPECT_HARBOR, verifier_digest=SHA256_B)
    res_verifier = reconcile_parity_results(c_harbor, p_verifier_diff, binding)
    assert res_verifier.parity_status == ParityStatus.REFUSED
    assert any(r.code == RefusalCode.BINDING_VERIFIER_MISMATCH for r in res_verifier.refusals)

    # 6. Mismatched outcome namespace or outcome name
    p_outcome_diff = _make_outcome(RunnerKind.INSPECT_HARBOR, outcome_name="different_metric")
    res_outcome = reconcile_parity_results(c_harbor, p_outcome_diff, binding)
    assert res_outcome.parity_status == ParityStatus.REFUSED
    assert any(r.code == RefusalCode.BINDING_OUTCOME_MISMATCH for r in res_outcome.refusals)

    # 7. Null verifier rewards: None/None NEVER verifies and fails closed
    c_null = _make_outcome(RunnerKind.HARBOR, verifier_reward=None)
    p_null = _make_outcome(RunnerKind.INSPECT_HARBOR, verifier_reward=None)
    res_null = reconcile_parity_results(c_null, p_null, binding)
    assert res_null.parity_status == ParityStatus.REFUSED
    assert any(r.code == RefusalCode.NULL_VERIFIER_REWARD for r in res_null.refusals)

    # One reward None
    c_valid = _make_outcome(RunnerKind.HARBOR, verifier_reward=1.0)
    res_one_null = reconcile_parity_results(c_valid, p_null, binding)
    assert res_one_null.parity_status == ParityStatus.REFUSED
    assert any(r.code == RefusalCode.NULL_VERIFIER_REWARD for r in res_one_null.refusals)

    # 8. Missing artifact digests (empty artifact dict fails closed)
    c_no_art = _make_outcome(RunnerKind.HARBOR, artifact_digests={})
    p_valid = _make_outcome(RunnerKind.INSPECT_HARBOR)
    res_no_art = reconcile_parity_results(c_no_art, p_valid, binding)
    assert res_no_art.parity_status == ParityStatus.REFUSED
    assert any(r.code == RefusalCode.MISSING_EVIDENCE for r in res_no_art.refusals)

    # 9. Mismatched harness environments (e.g. prompt_digest mismatch)
    h_diff = _make_harness(RunnerKind.INSPECT_HARBOR, prompt_digest=SHA256_H)
    p_harness_diff = _make_outcome(RunnerKind.INSPECT_HARBOR, harness=h_diff)
    res_env_diff = reconcile_parity_results(c_harbor, p_harness_diff, binding)
    assert res_env_diff.parity_status == ParityStatus.REFUSED
    assert any(r.code == RefusalCode.MISMATCHED_ENVIRONMENT_IDENTITY for r in res_env_diff.refusals)

    # Mismatched model_config_digest
    h_diff_model = _make_harness(RunnerKind.INSPECT_HARBOR, model_config_digest=SHA256_H)
    p_model_diff = _make_outcome(RunnerKind.INSPECT_HARBOR, harness=h_diff_model)
    res_model_diff = reconcile_parity_results(c_harbor, p_model_diff, binding)
    assert res_model_diff.parity_status == ParityStatus.REFUSED
    assert any(
        r.code == RefusalCode.MISMATCHED_ENVIRONMENT_IDENTITY for r in res_model_diff.refusals
    )

    # Mismatched tool_schema_digest
    h_diff_tool = _make_harness(RunnerKind.INSPECT_HARBOR, tool_digest=SHA256_H)
    p_tool_diff = _make_outcome(RunnerKind.INSPECT_HARBOR, harness=h_diff_tool)
    res_tool_diff = reconcile_parity_results(c_harbor, p_tool_diff, binding)
    assert res_tool_diff.parity_status == ParityStatus.REFUSED
    assert any(
        r.code == RefusalCode.MISMATCHED_ENVIRONMENT_IDENTITY for r in res_tool_diff.refusals
    )

    # Mismatched environment_digest
    h_diff_env = _make_harness(RunnerKind.INSPECT_HARBOR, env_digest=SHA256_H)
    p_env_diff = _make_outcome(RunnerKind.INSPECT_HARBOR, harness=h_diff_env)
    res_env_mismatch = reconcile_parity_results(c_harbor, p_env_diff, binding)
    assert res_env_mismatch.parity_status == ParityStatus.REFUSED
    assert any(
        r.code == RefusalCode.MISMATCHED_ENVIRONMENT_IDENTITY for r in res_env_mismatch.refusals
    )

    # 10. Reconciliation digest sensitivity
    res_verified_1 = reconcile_parity_results(c_valid, p_valid, binding)
    res_verified_2 = reconcile_parity_results(c_valid, p_valid, binding)
    assert res_verified_1.reconciliation_digest == res_verified_2.reconciliation_digest

    # Changing trial_id changes digest
    p_trial_diff = _make_outcome(RunnerKind.INSPECT_HARBOR, trial_id="trial-999")
    res_trial_diff = reconcile_parity_results(c_valid, p_trial_diff, binding)
    assert res_verified_1.reconciliation_digest != res_trial_diff.reconciliation_digest
