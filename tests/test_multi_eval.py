"""Unit tests for the multi-eval platform layer (multi_eval.py)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from evallab.multi_eval import (
    ExecutionIntent,
    HarnessIdentity,
    MultiEvalTaskSpec,
    ParityStatus,
    RefusalCode,
    RunnerCapabilities,
    RunnerKind,
    RunnerOutcome,
    ScaffoldEquivalence,
    TaskRequirements,
    get_runner_capabilities,
    plan_multi_eval_execution,
    reconcile_parity_results,
    validate_runner_capabilities,
)


def _make_harness_identity(
    runner: RunnerKind,
    image: str = "ubuntu:22.04",
    digest: str = "sha256:1111",
) -> HarnessIdentity:
    return HarnessIdentity(
        runner_kind=runner,
        runner_version="1.0.0",
        harness_digest=f"sha256:harness-{runner.value}",
        environment_kind="docker",
        environment_image=image,
        environment_digest=digest,
        harness_parameters={"timeout": 600},
    )


def test_runner_kinds_and_capabilities_registry() -> None:
    """Verify that all runner kinds have registered capability profiles."""
    assert RunnerKind.HARBOR == "harbor"
    assert RunnerKind.INSPECT == "inspect"
    assert RunnerKind.INSPECT_HARBOR == "inspect_harbor"
    assert RunnerKind.IMPORT_ONLY == "import_only"

    harbor_caps = get_runner_capabilities(RunnerKind.HARBOR)
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
    assert harbor_caps.supports_scaffold_equivalence is True

    inspect_caps = get_runner_capabilities("inspect")
    assert inspect_caps.runner_kind == RunnerKind.INSPECT
    assert inspect_caps.supports_active_execution is True
    assert inspect_caps.supports_network_allowlist is False
    assert inspect_caps.supports_docker_compose is False
    assert inspect_caps.supports_hidden_verifier_containers is False

    inspect_harbor_caps = get_runner_capabilities(RunnerKind.INSPECT_HARBOR)
    assert inspect_harbor_caps.supports_active_execution is True
    assert inspect_harbor_caps.supports_docker_compose is False

    import_caps = get_runner_capabilities(RunnerKind.IMPORT_ONLY)
    assert import_caps.supports_active_execution is False
    assert import_caps.supports_docker is False
    assert import_caps.supports_scaffold_equivalence is False


def test_validate_runner_capabilities_refusals() -> None:
    """Check refusal generation for unsupported runner features."""
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
        supports_scaffold_equivalence=True,
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


def test_scaffold_equivalence_contract() -> None:
    """Test ScaffoldEquivalence validation and strict model fields."""
    equiv = ScaffoldEquivalence(
        scaffold_a="harbor_react",
        scaffold_b="inspect_react",
        declared_equivalent=True,
        equivalence_basis="same prompt templates and tool invocation schemas",
        drift_parameters={"temperature": 0.0},
    )
    assert equiv.scaffold_a == "harbor_react"
    assert equiv.declared_equivalent is True

    # Empty scaffold name rejected
    with pytest.raises(ValidationError):
        ScaffoldEquivalence(scaffold_a="", scaffold_b="inspect", declared_equivalent=True)


def test_plan_multi_eval_execution_canonical_selection() -> None:
    """Verify canonical runner selection rules across task families and requirements."""
    # 1. Import ingest -> IMPORT_ONLY
    task_spec = MultiEvalTaskSpec(task_id="t1", benchmark_family="swe_bench")
    plan = plan_multi_eval_execution(task_spec, execution_intent=ExecutionIntent.IMPORT_INGEST)
    assert plan.canonical_runner == RunnerKind.IMPORT_ONLY
    assert not plan.is_refused

    # 2. RSI benchmark family -> HARBOR canonical
    task_rsi = MultiEvalTaskSpec(task_id="t2", benchmark_family="rsi-benchmark")
    plan_rsi = plan_multi_eval_execution(task_rsi)
    assert plan_rsi.canonical_runner == RunnerKind.HARBOR

    # 3. Docker Compose requirement -> HARBOR canonical
    task_compose = MultiEvalTaskSpec(
        task_id="t3",
        benchmark_family="gaia",
        requirements=TaskRequirements(requires_docker_compose=True),
    )
    plan_compose = plan_multi_eval_execution(task_compose)
    assert plan_compose.canonical_runner == RunnerKind.HARBOR

    # 4. Inspect-native benchmark family -> INSPECT canonical
    task_gaia = MultiEvalTaskSpec(task_id="t4", benchmark_family="gaia")
    plan_gaia = plan_multi_eval_execution(task_gaia)
    assert plan_gaia.canonical_runner == RunnerKind.INSPECT

    # 5. Inspect-Harbor hint never overrides canonical Harbor for RSI tasks
    task_rsi_hint = MultiEvalTaskSpec(
        task_id="t5",
        benchmark_family="rsi-benchmark",
        canonical_runner_hint=RunnerKind.INSPECT_HARBOR,
    )
    plan_rsi_hint = plan_multi_eval_execution(task_rsi_hint)
    assert plan_rsi_hint.canonical_runner == RunnerKind.HARBOR


def test_plan_multi_eval_cross_runner_comparison_refusals() -> None:
    """Verify scaffold equivalence requirement and environment checks in planning."""
    task_spec = MultiEvalTaskSpec(task_id="comp-1", benchmark_family="swe_bench")

    # Cross runner comparison without declared equivalence is refused
    plan_no_equiv = plan_multi_eval_execution(
        task_spec,
        execution_intent=ExecutionIntent.CROSS_RUNNER_COMPARISON,
        scaffold_equivalence=None,
    )
    assert plan_no_equiv.is_refused is True
    assert any(
        r.code == RefusalCode.UNDECLARED_SCAFFOLD_EQUIVALENCE for r in plan_no_equiv.refusals
    )

    # With declared equivalence
    equiv = ScaffoldEquivalence(
        scaffold_a="scaffold_harbor",
        scaffold_b="scaffold_inspect",
        declared_equivalent=True,
    )
    plan_with_equiv = plan_multi_eval_execution(
        task_spec,
        execution_intent=ExecutionIntent.CROSS_RUNNER_COMPARISON,
        scaffold_equivalence=equiv,
    )
    assert plan_with_equiv.is_refused is False
    assert plan_with_equiv.plan_digest.startswith("sha256:")

    # Environment identity mismatch causes refusal
    harnesses = {
        RunnerKind.INSPECT: _make_harness_identity(RunnerKind.INSPECT, image="python:3.11"),
        RunnerKind.HARBOR: _make_harness_identity(RunnerKind.HARBOR, image="python:3.10"),
    }
    plan_mismatch = plan_multi_eval_execution(
        task_spec,
        execution_intent=ExecutionIntent.CROSS_RUNNER_COMPARISON,
        scaffold_equivalence=equiv,
        harness_identities=harnesses,
    )
    assert plan_mismatch.is_refused is True
    assert any(
        r.code == RefusalCode.MISMATCHED_ENVIRONMENT_IDENTITY for r in plan_mismatch.refusals
    )


def test_plan_digest_determinism() -> None:
    """Verify that plan digest generation is deterministic."""
    task_spec = MultiEvalTaskSpec(task_id="task-det", benchmark_family="swe_bench")
    plan_a = plan_multi_eval_execution(task_spec)
    plan_b = plan_multi_eval_execution(task_spec)
    assert plan_a.plan_digest == plan_b.plan_digest


def test_reconcile_parity_results_verified() -> None:
    """Verify successful parity reconciliation with trajectory measurement observables."""
    h_canonical = _make_harness_identity(RunnerKind.HARBOR)
    h_parity = _make_harness_identity(RunnerKind.INSPECT_HARBOR)

    canonical_outcome = RunnerOutcome(
        runner_kind=RunnerKind.HARBOR,
        trial_id="task-001__trial-1",
        verifier_reward=1.0,
        verifier_passed=True,
        artifact_digests={"patch.diff": "sha256:abc"},
        step_count=5,
        total_tokens=1200,
        duration_seconds=15.0,
        harness_identity=h_canonical,
        raw_log_digest="sha256:raw-canon",
    )

    # Parity run produces identical reward and artifacts but varied steps/tokens/duration
    parity_outcome = RunnerOutcome(
        runner_kind=RunnerKind.INSPECT_HARBOR,
        trial_id="task-001__trial-1",
        verifier_reward=1.0,
        verifier_passed=True,
        artifact_digests={"patch.diff": "sha256:abc"},
        step_count=7,
        total_tokens=1450,
        duration_seconds=18.5,
        harness_identity=h_parity,
        raw_log_digest="sha256:raw-parity",
    )

    equiv = ScaffoldEquivalence(
        scaffold_a="harbor_react",
        scaffold_b="inspect_harbor_react",
        declared_equivalent=True,
    )

    result = reconcile_parity_results(
        canonical_outcome,
        parity_outcome,
        scaffold_equivalence=equiv,
    )

    assert result.parity_status == ParityStatus.PARITY_VERIFIED
    assert result.verifier_reward_reconciled is True
    assert result.verifier_passed_reconciled is True
    assert result.artifact_digests_reconciled is True
    assert result.scaffold_equivalent is True
    assert result.measurements.step_delta == 2
    assert result.measurements.token_delta == 250
    assert result.measurements.duration_delta_seconds == 3.5
    assert len(result.measurements.notes) == 3
    assert result.reconciliation_digest.startswith("sha256:")


def test_reconcile_parity_results_divergent() -> None:
    """Verify divergent status when verifier rewards or artifacts differ."""
    h_canonical = _make_harness_identity(RunnerKind.HARBOR)
    h_parity = _make_harness_identity(RunnerKind.INSPECT_HARBOR)

    canonical_outcome = RunnerOutcome(
        runner_kind=RunnerKind.HARBOR,
        trial_id="task-002__trial-1",
        verifier_reward=1.0,
        verifier_passed=True,
        artifact_digests={"patch.diff": "sha256:abc"},
        harness_identity=h_canonical,
        raw_log_digest="sha256:log-1",
    )

    parity_outcome = RunnerOutcome(
        runner_kind=RunnerKind.INSPECT_HARBOR,
        trial_id="task-002__trial-1",
        verifier_reward=0.0,
        verifier_passed=False,
        artifact_digests={"patch.diff": "sha256:xyz"},
        harness_identity=h_parity,
        raw_log_digest="sha256:log-2",
    )

    equiv = ScaffoldEquivalence(
        scaffold_a="s1",
        scaffold_b="s2",
        declared_equivalent=True,
    )

    result = reconcile_parity_results(
        canonical_outcome,
        parity_outcome,
        scaffold_equivalence=equiv,
    )

    assert result.parity_status == ParityStatus.PARITY_DIVERGENT
    assert result.verifier_reward_reconciled is False
    assert result.verifier_passed_reconciled is False
    assert result.artifact_digests_reconciled is False


def test_reconcile_parity_results_none_rewards_and_tolerance() -> None:
    """Verify verifier reward reconciliation when rewards are None or within tolerance."""
    h_canonical = _make_harness_identity(RunnerKind.HARBOR)
    h_parity = _make_harness_identity(RunnerKind.INSPECT_HARBOR)
    equiv = ScaffoldEquivalence(scaffold_a="s1", scaffold_b="s2", declared_equivalent=True)

    # Both rewards None
    c1 = RunnerOutcome(
        runner_kind=RunnerKind.HARBOR,
        trial_id="t-none",
        verifier_reward=None,
        verifier_passed=True,
        harness_identity=h_canonical,
        raw_log_digest="sha256:1",
    )
    p1 = RunnerOutcome(
        runner_kind=RunnerKind.INSPECT_HARBOR,
        trial_id="t-none",
        verifier_reward=None,
        verifier_passed=True,
        harness_identity=h_parity,
        raw_log_digest="sha256:2",
    )
    res1 = reconcile_parity_results(c1, p1, scaffold_equivalence=equiv)
    assert res1.verifier_reward_reconciled is True
    assert res1.parity_status == ParityStatus.PARITY_VERIFIED

    # Tolerance within vs exceeding
    c2 = RunnerOutcome(
        runner_kind=RunnerKind.HARBOR,
        trial_id="t-tol",
        verifier_reward=0.9999999,
        verifier_passed=True,
        harness_identity=h_canonical,
        raw_log_digest="sha256:1",
    )
    p2 = RunnerOutcome(
        runner_kind=RunnerKind.INSPECT_HARBOR,
        trial_id="t-tol",
        verifier_reward=1.0,
        verifier_passed=True,
        harness_identity=h_parity,
        raw_log_digest="sha256:2",
    )
    res_within = reconcile_parity_results(c2, p2, scaffold_equivalence=equiv, reward_tolerance=1e-5)
    assert res_within.verifier_reward_reconciled is True

    res_exceed = reconcile_parity_results(c2, p2, scaffold_equivalence=equiv, reward_tolerance=1e-9)
    assert res_exceed.verifier_reward_reconciled is False


def test_reconcile_parity_results_refused_undeclared_scaffold() -> None:
    """Verify refusal status when scaffold equivalence is absent or false."""
    h_canonical = _make_harness_identity(RunnerKind.HARBOR)
    h_parity = _make_harness_identity(RunnerKind.INSPECT_HARBOR)

    canonical = RunnerOutcome(
        runner_kind=RunnerKind.HARBOR,
        trial_id="t3",
        verifier_reward=1.0,
        verifier_passed=True,
        harness_identity=h_canonical,
        raw_log_digest="sha256:1",
    )
    parity = RunnerOutcome(
        runner_kind=RunnerKind.INSPECT_HARBOR,
        trial_id="t3",
        verifier_reward=1.0,
        verifier_passed=True,
        harness_identity=h_parity,
        raw_log_digest="sha256:2",
    )

    # Without scaffold equivalence
    result_none = reconcile_parity_results(canonical, parity, scaffold_equivalence=None)
    assert result_none.parity_status == ParityStatus.REFUSED
    assert any(r.code == RefusalCode.UNDECLARED_SCAFFOLD_EQUIVALENCE for r in result_none.refusals)

    # With declared_equivalent=False
    equiv_false = ScaffoldEquivalence(scaffold_a="a", scaffold_b="b", declared_equivalent=False)
    result_false = reconcile_parity_results(canonical, parity, scaffold_equivalence=equiv_false)
    assert result_false.parity_status == ParityStatus.REFUSED


def test_reconcile_parity_results_mismatched_environment() -> None:
    """Verify refusal status when runner outcomes have conflicting environment digests."""
    h_canonical = _make_harness_identity(
        RunnerKind.HARBOR, image="python:3.11", digest="sha256:digest-a"
    )
    h_parity = _make_harness_identity(
        RunnerKind.INSPECT, image="python:3.11", digest="sha256:digest-b"
    )

    canonical = RunnerOutcome(
        runner_kind=RunnerKind.HARBOR,
        trial_id="t4",
        verifier_reward=1.0,
        verifier_passed=True,
        harness_identity=h_canonical,
        raw_log_digest="sha256:1",
    )
    parity = RunnerOutcome(
        runner_kind=RunnerKind.INSPECT,
        trial_id="t4",
        verifier_reward=1.0,
        verifier_passed=True,
        harness_identity=h_parity,
        raw_log_digest="sha256:2",
    )
    equiv = ScaffoldEquivalence(scaffold_a="a", scaffold_b="b", declared_equivalent=True)

    result = reconcile_parity_results(canonical, parity, scaffold_equivalence=equiv)
    assert result.parity_status == ParityStatus.REFUSED
    assert any(r.code == RefusalCode.MISMATCHED_ENVIRONMENT_IDENTITY for r in result.refusals)
