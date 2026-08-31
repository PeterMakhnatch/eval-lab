"""Multi-Eval platform layer for multi-runner execution, capabilities, planning, and parity reconciliation.

This module provides first-class contracts and algorithms for:
- Typed runner kind taxonomies (Harbor, Inspect, Inspect-Harbor parity lane, Import-only).
- Capability profile validation and refusal generation against task requirements.
- Execution planning distinguishing canonical runners from parity lanes.
- Scaffold equivalence declaration contracts.
- Deterministic parity reconciliation between runner outcomes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any

from pydantic import Field

from evallab.schemas import ContractModel

__all__ = [
    "ExecutionIntent",
    "HarnessIdentity",
    "MultiEvalParityResult",
    "MultiEvalPlan",
    "MultiEvalTaskSpec",
    "ParityStatus",
    "PlanningRefusal",
    "RefusalCode",
    "RunnerCapabilities",
    "RunnerKind",
    "RunnerOutcome",
    "ScaffoldEquivalence",
    "TaskRequirements",
    "TrajectoryMeasurements",
    "get_runner_capabilities",
    "plan_multi_eval_execution",
    "reconcile_parity_results",
    "validate_runner_capabilities",
]


class RunnerKind(StrEnum):
    """Authoritative execution runner kinds supported across Eval Lab."""

    HARBOR = "harbor"
    INSPECT = "inspect"
    INSPECT_HARBOR = "inspect_harbor"
    IMPORT_ONLY = "import_only"


class RunnerCapabilities(ContractModel):
    """Hardware, orchestration, and feature capabilities supported by a runner."""

    runner_kind: RunnerKind
    supports_active_execution: bool
    supports_multi_step: bool
    supports_prior_trajectories: bool
    supports_mcp_servers: bool
    supports_skills_dir: bool
    supports_network_allowlist: bool
    supports_windows: bool
    supports_docker: bool
    supports_docker_compose: bool
    supports_hidden_verifier_containers: bool
    supports_scaffold_equivalence: bool


RUNNER_CAPABILITIES: dict[RunnerKind, RunnerCapabilities] = {
    RunnerKind.HARBOR: RunnerCapabilities(
        runner_kind=RunnerKind.HARBOR,
        supports_active_execution=True,
        supports_multi_step=True,
        supports_prior_trajectories=True,
        supports_mcp_servers=True,
        supports_skills_dir=True,
        supports_network_allowlist=True,
        supports_windows=False,
        supports_docker=True,
        supports_docker_compose=True,
        supports_hidden_verifier_containers=True,
        supports_scaffold_equivalence=True,
    ),
    RunnerKind.INSPECT: RunnerCapabilities(
        runner_kind=RunnerKind.INSPECT,
        supports_active_execution=True,
        supports_multi_step=True,
        supports_prior_trajectories=True,
        supports_mcp_servers=True,
        supports_skills_dir=True,
        supports_network_allowlist=False,
        supports_windows=False,
        supports_docker=True,
        supports_docker_compose=False,
        supports_hidden_verifier_containers=False,
        supports_scaffold_equivalence=True,
    ),
    RunnerKind.INSPECT_HARBOR: RunnerCapabilities(
        runner_kind=RunnerKind.INSPECT_HARBOR,
        supports_active_execution=True,
        supports_multi_step=True,
        supports_prior_trajectories=True,
        supports_mcp_servers=True,
        supports_skills_dir=True,
        supports_network_allowlist=False,
        supports_windows=False,
        supports_docker=True,
        supports_docker_compose=False,
        supports_hidden_verifier_containers=False,
        supports_scaffold_equivalence=True,
    ),
    RunnerKind.IMPORT_ONLY: RunnerCapabilities(
        runner_kind=RunnerKind.IMPORT_ONLY,
        supports_active_execution=False,
        supports_multi_step=False,
        supports_prior_trajectories=False,
        supports_mcp_servers=False,
        supports_skills_dir=False,
        supports_network_allowlist=False,
        supports_windows=False,
        supports_docker=False,
        supports_docker_compose=False,
        supports_hidden_verifier_containers=False,
        supports_scaffold_equivalence=False,
    ),
}


def get_runner_capabilities(runner: RunnerKind | str) -> RunnerCapabilities:
    """Return the static default capability profile for a runner kind."""
    if isinstance(runner, str):
        runner = RunnerKind(runner)
    return RUNNER_CAPABILITIES[runner]


class TaskRequirements(ContractModel):
    """Resource and execution requirements demanded by a benchmark task."""

    requires_multi_step: bool = True
    requires_prior_trajectories: bool = False
    requires_mcp_servers: bool = False
    requires_skills_dir: bool = False
    requires_network_allowlist: bool = False
    requires_windows: bool = False
    requires_docker_compose: bool = False
    requires_hidden_verifier_containers: bool = False
    scaffold_family: str | None = None
    target_os: str = "linux"


class RefusalCode(StrEnum):
    """Taxonomy of refusals when planning or reconciling multi-eval execution."""

    UNSUPPORTED_WINDOWS = "unsupported_windows"
    UNSUPPORTED_DOCKER_COMPOSE = "unsupported_docker_compose"
    UNSUPPORTED_HIDDEN_VERIFIER = "unsupported_hidden_verifier"
    UNSUPPORTED_NETWORK_ALLOWLIST = "unsupported_network_allowlist"
    UNSUPPORTED_MCP_SERVERS = "unsupported_mcp_servers"
    UNSUPPORTED_SKILLS_DIR = "unsupported_skills_dir"
    UNSUPPORTED_PRIOR_TRAJECTORIES = "unsupported_prior_trajectories"
    IMPORT_ONLY_NO_EXECUTION = "import_only_no_execution"
    UNDECLARED_SCAFFOLD_EQUIVALENCE = "undeclared_scaffold_equivalence"
    MISMATCHED_ENVIRONMENT_IDENTITY = "mismatched_environment_identity"
    UNSUPPORTED_MULTI_STEP = "unsupported_multi_step"


class PlanningRefusal(ContractModel):
    """Structured refusal indicating an incompatible runner or missing capability."""

    code: RefusalCode
    message: str
    runner_kind: RunnerKind | None = None
    missing_capabilities: list[str] = Field(default_factory=list)


def validate_runner_capabilities(
    runner_caps: RunnerCapabilities,
    task_reqs: TaskRequirements,
) -> list[PlanningRefusal]:
    """Validate that a runner's capabilities satisfy task requirements."""
    refusals: list[PlanningRefusal] = []

    if not runner_caps.supports_active_execution:
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.IMPORT_ONLY_NO_EXECUTION,
                message=f"Runner '{runner_caps.runner_kind.value}' does not support active execution",
                runner_kind=runner_caps.runner_kind,
                missing_capabilities=["supports_active_execution"],
            )
        )
        return refusals

    if (
        task_reqs.requires_windows or task_reqs.target_os.strip().lower() == "windows"
    ) and not runner_caps.supports_windows:
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.UNSUPPORTED_WINDOWS,
                message=f"Runner '{runner_caps.runner_kind.value}' does not support Windows target OS",
                runner_kind=runner_caps.runner_kind,
                missing_capabilities=["supports_windows"],
            )
        )

    if task_reqs.requires_docker_compose and not runner_caps.supports_docker_compose:
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.UNSUPPORTED_DOCKER_COMPOSE,
                message=f"Runner '{runner_caps.runner_kind.value}' does not support Docker Compose multi-container environments",
                runner_kind=runner_caps.runner_kind,
                missing_capabilities=["supports_docker_compose"],
            )
        )

    if (
        task_reqs.requires_hidden_verifier_containers
        and not runner_caps.supports_hidden_verifier_containers
    ):
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.UNSUPPORTED_HIDDEN_VERIFIER,
                message=f"Runner '{runner_caps.runner_kind.value}' does not support hidden verifier containers",
                runner_kind=runner_caps.runner_kind,
                missing_capabilities=["supports_hidden_verifier_containers"],
            )
        )

    if task_reqs.requires_network_allowlist and not runner_caps.supports_network_allowlist:
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.UNSUPPORTED_NETWORK_ALLOWLIST,
                message=f"Runner '{runner_caps.runner_kind.value}' does not support network allowlisting",
                runner_kind=runner_caps.runner_kind,
                missing_capabilities=["supports_network_allowlist"],
            )
        )

    if task_reqs.requires_mcp_servers and not runner_caps.supports_mcp_servers:
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.UNSUPPORTED_MCP_SERVERS,
                message=f"Runner '{runner_caps.runner_kind.value}' does not support MCP servers",
                runner_kind=runner_caps.runner_kind,
                missing_capabilities=["supports_mcp_servers"],
            )
        )

    if task_reqs.requires_skills_dir and not runner_caps.supports_skills_dir:
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.UNSUPPORTED_SKILLS_DIR,
                message=f"Runner '{runner_caps.runner_kind.value}' does not support skills directories",
                runner_kind=runner_caps.runner_kind,
                missing_capabilities=["supports_skills_dir"],
            )
        )

    if task_reqs.requires_prior_trajectories and not runner_caps.supports_prior_trajectories:
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.UNSUPPORTED_PRIOR_TRAJECTORIES,
                message=f"Runner '{runner_caps.runner_kind.value}' does not support prior trajectories",
                runner_kind=runner_caps.runner_kind,
                missing_capabilities=["supports_prior_trajectories"],
            )
        )

    if task_reqs.requires_multi_step and not runner_caps.supports_multi_step:
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.UNSUPPORTED_MULTI_STEP,
                message=f"Runner '{runner_caps.runner_kind.value}' does not support multi-step trajectories",
                runner_kind=runner_caps.runner_kind,
                missing_capabilities=["supports_multi_step"],
            )
        )

    return refusals


class ScaffoldEquivalence(ContractModel):
    """Explicit declaration of equivalence between two runner agent scaffolds."""

    scaffold_a: str = Field(min_length=1)
    scaffold_b: str = Field(min_length=1)
    declared_equivalent: bool
    equivalence_basis: str | None = None
    drift_parameters: dict[str, Any] = Field(default_factory=dict)


class ExecutionIntent(StrEnum):
    """Intent for executing or ingesting an evaluation trial."""

    CANONICAL_RUN = "canonical_run"
    PARITY_RUN = "parity_run"
    CROSS_RUNNER_COMPARISON = "cross_runner_comparison"
    IMPORT_INGEST = "import_ingest"


class HarnessIdentity(ContractModel):
    """Durable identity and environment configuration of an execution harness."""

    runner_kind: RunnerKind
    runner_version: str
    harness_digest: str
    environment_kind: str
    environment_image: str | None = None
    environment_digest: str | None = None
    harness_parameters: dict[str, Any] = Field(default_factory=dict)


class MultiEvalTaskSpec(ContractModel):
    """Specification of a task submitted for multi-eval execution planning."""

    task_id: str = Field(min_length=1)
    benchmark_family: str = Field(min_length=1)
    requirements: TaskRequirements = Field(default_factory=TaskRequirements)
    canonical_runner_hint: RunnerKind | None = None
    task_digest: str | None = None


class MultiEvalPlan(ContractModel):
    """Execution plan resolving canonical runner, parity runners, and validation refusals."""

    task_id: str
    benchmark_family: str
    execution_intent: ExecutionIntent
    canonical_runner: RunnerKind
    parity_runners: tuple[RunnerKind, ...] = ()
    harness_identities: dict[str, HarnessIdentity] = Field(default_factory=dict)
    scaffold_equivalence: ScaffoldEquivalence | None = None
    is_refused: bool = False
    refusals: tuple[PlanningRefusal, ...] = ()
    plan_digest: str


def _canonical_json(data: Any) -> str:
    """Encode structured data into deterministic canonical JSON."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _compute_plan_digest(
    task_id: str,
    benchmark_family: str,
    execution_intent: ExecutionIntent,
    canonical_runner: RunnerKind,
    parity_runners: tuple[RunnerKind, ...],
    harness_identities: dict[str, HarnessIdentity],
    scaffold_equivalence: ScaffoldEquivalence | None,
    refusals: tuple[PlanningRefusal, ...],
) -> str:
    payload = {
        "task_id": task_id,
        "benchmark_family": benchmark_family,
        "execution_intent": execution_intent.value,
        "canonical_runner": canonical_runner.value,
        "parity_runners": [r.value for r in parity_runners],
        "harness_identities": {
            k: v.model_dump(mode="json") for k, v in sorted(harness_identities.items())
        },
        "scaffold_equivalence": (
            scaffold_equivalence.model_dump(mode="json") if scaffold_equivalence else None
        ),
        "refusals": [r.model_dump(mode="json") for r in refusals],
    }
    encoded = _canonical_json(payload).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def plan_multi_eval_execution(
    task_spec: MultiEvalTaskSpec,
    execution_intent: ExecutionIntent = ExecutionIntent.CANONICAL_RUN,
    parity_runners: Sequence[RunnerKind | str] | None = None,
    harness_identities: Mapping[RunnerKind | str, HarnessIdentity] | None = None,
    scaffold_equivalence: ScaffoldEquivalence | None = None,
    custom_capabilities: Mapping[RunnerKind | str, RunnerCapabilities] | None = None,
) -> MultiEvalPlan:
    """Plan multi-eval execution by resolving canonical runner and parity lanes."""
    # 1. Determine canonical runner
    if execution_intent == ExecutionIntent.IMPORT_INGEST:
        canonical_runner = RunnerKind.IMPORT_ONLY
    elif (
        task_spec.requirements.requires_docker_compose
        or task_spec.requirements.requires_hidden_verifier_containers
        or task_spec.requirements.requires_network_allowlist
        or task_spec.benchmark_family.startswith("rsi-")
        or task_spec.benchmark_family.startswith("harbor-")
        or task_spec.benchmark_family in ("rsi", "harbor")
    ):
        canonical_runner = RunnerKind.HARBOR
    elif task_spec.canonical_runner_hint is not None:
        if task_spec.canonical_runner_hint == RunnerKind.INSPECT_HARBOR:
            canonical_runner = RunnerKind.HARBOR
        else:
            canonical_runner = task_spec.canonical_runner_hint
    elif task_spec.benchmark_family.startswith("inspect") or task_spec.benchmark_family in {
        "gaia",
        "swe_bench",
        "cybermetric",
        "intercode",
    }:
        canonical_runner = RunnerKind.INSPECT
    else:
        canonical_runner = RunnerKind.HARBOR

    # 2. Determine parity runners
    raw_parity_runners: list[RunnerKind] = []
    if parity_runners is not None:
        for r in parity_runners:
            kind = RunnerKind(r) if isinstance(r, str) else r
            raw_parity_runners.append(kind)
    else:
        if execution_intent == ExecutionIntent.PARITY_RUN:
            if canonical_runner in (RunnerKind.HARBOR, RunnerKind.INSPECT):
                raw_parity_runners = [RunnerKind.INSPECT_HARBOR]
        elif execution_intent == ExecutionIntent.CROSS_RUNNER_COMPARISON:
            if canonical_runner == RunnerKind.HARBOR:
                raw_parity_runners = [RunnerKind.INSPECT]
            elif canonical_runner == RunnerKind.INSPECT:
                raw_parity_runners = [RunnerKind.HARBOR]

    seen_parity: set[RunnerKind] = set()
    final_parity_runners: list[RunnerKind] = []
    for r in raw_parity_runners:
        if r != canonical_runner and r not in seen_parity:
            seen_parity.add(r)
            final_parity_runners.append(r)

    # 3. Capability and requirement validation
    refusals: list[PlanningRefusal] = []

    def _resolve_caps(kind: RunnerKind) -> RunnerCapabilities:
        if custom_capabilities is not None:
            if kind in custom_capabilities:
                return custom_capabilities[kind]
            if kind.value in custom_capabilities:
                return custom_capabilities[kind.value]
        return get_runner_capabilities(kind)

    if execution_intent != ExecutionIntent.IMPORT_INGEST:
        canonical_caps = _resolve_caps(canonical_runner)
        refusals.extend(validate_runner_capabilities(canonical_caps, task_spec.requirements))
        for p_kind in final_parity_runners:
            p_caps = _resolve_caps(p_kind)
            refusals.extend(validate_runner_capabilities(p_caps, task_spec.requirements))

    # Scaffold equivalence requirement for cross runner comparison
    if execution_intent == ExecutionIntent.CROSS_RUNNER_COMPARISON and (
        scaffold_equivalence is None or not scaffold_equivalence.declared_equivalent
    ):
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.UNDECLARED_SCAFFOLD_EQUIVALENCE,
                message="Cross-runner comparison requires declared scaffold equivalence",
                runner_kind=None,
                missing_capabilities=["declared_equivalent"],
            )
        )

    # Normalize harness identities
    normalized_identities: dict[str, HarnessIdentity] = {}
    if harness_identities is not None:
        for k, v in harness_identities.items():
            k_str = k.value if isinstance(k, RunnerKind) else str(k)
            normalized_identities[k_str] = v

        if canonical_runner.value in normalized_identities:
            c_ident = normalized_identities[canonical_runner.value]
            for p_kind in final_parity_runners:
                if p_kind.value in normalized_identities:
                    p_ident = normalized_identities[p_kind.value]
                    if (
                        c_ident.environment_image
                        and p_ident.environment_image
                        and c_ident.environment_image != p_ident.environment_image
                    ) or (
                        c_ident.environment_digest
                        and p_ident.environment_digest
                        and c_ident.environment_digest != p_ident.environment_digest
                    ):
                        refusals.append(
                            PlanningRefusal(
                                code=RefusalCode.MISMATCHED_ENVIRONMENT_IDENTITY,
                                message=(
                                    f"Mismatched environment identity between canonical '{canonical_runner.value}' "
                                    f"and parity '{p_kind.value}'"
                                ),
                                runner_kind=p_kind,
                                missing_capabilities=["environment_identity"],
                            )
                        )

    parity_tuple = tuple(final_parity_runners)
    refusals_tuple = tuple(refusals)
    plan_digest = _compute_plan_digest(
        task_id=task_spec.task_id,
        benchmark_family=task_spec.benchmark_family,
        execution_intent=execution_intent,
        canonical_runner=canonical_runner,
        parity_runners=parity_tuple,
        harness_identities=normalized_identities,
        scaffold_equivalence=scaffold_equivalence,
        refusals=refusals_tuple,
    )

    return MultiEvalPlan(
        task_id=task_spec.task_id,
        benchmark_family=task_spec.benchmark_family,
        execution_intent=execution_intent,
        canonical_runner=canonical_runner,
        parity_runners=parity_tuple,
        harness_identities=normalized_identities,
        scaffold_equivalence=scaffold_equivalence,
        is_refused=len(refusals_tuple) > 0,
        refusals=refusals_tuple,
        plan_digest=plan_digest,
    )


class RunnerOutcome(ContractModel):
    """Raw trial outcome produced by a single runner execution."""

    runner_kind: RunnerKind
    trial_id: str
    verifier_reward: float | None = None
    verifier_passed: bool
    artifact_digests: dict[str, str] = Field(default_factory=dict)
    step_count: int = 0
    total_tokens: int = 0
    duration_seconds: float = 0.0
    harness_identity: HarnessIdentity
    raw_log_digest: str


class ParityStatus(StrEnum):
    """Status classification of cross-runner or parity reconciliation."""

    PARITY_VERIFIED = "parity_verified"
    PARITY_DIVERGENT = "parity_divergent"
    REFUSED = "refused"


class TrajectoryMeasurements(ContractModel):
    """Observables and measurement differences between runner trajectories."""

    step_delta: int = 0
    token_delta: int = 0
    duration_delta_seconds: float = 0.0
    notes: list[str] = Field(default_factory=list)


class MultiEvalParityResult(ContractModel):
    """Reconciliation result evaluating parity between canonical and parity runners."""

    task_id: str
    canonical_runner: RunnerKind
    parity_runner: RunnerKind
    parity_status: ParityStatus
    verifier_reward_reconciled: bool
    verifier_passed_reconciled: bool
    artifact_digests_reconciled: bool
    scaffold_equivalent: bool
    measurements: TrajectoryMeasurements
    refusals: tuple[PlanningRefusal, ...] = ()
    reconciliation_digest: str


def _compute_reconciliation_digest(
    task_id: str,
    canonical_runner: RunnerKind,
    parity_runner: RunnerKind,
    parity_status: ParityStatus,
    verifier_reward_reconciled: bool,
    verifier_passed_reconciled: bool,
    artifact_digests_reconciled: bool,
    scaffold_equivalent: bool,
    measurements: TrajectoryMeasurements,
    refusals: tuple[PlanningRefusal, ...],
) -> str:
    payload = {
        "task_id": task_id,
        "canonical_runner": canonical_runner.value,
        "parity_runner": parity_runner.value,
        "parity_status": parity_status.value,
        "verifier_reward_reconciled": verifier_reward_reconciled,
        "verifier_passed_reconciled": verifier_passed_reconciled,
        "artifact_digests_reconciled": artifact_digests_reconciled,
        "scaffold_equivalent": scaffold_equivalent,
        "measurements": measurements.model_dump(mode="json"),
        "refusals": [r.model_dump(mode="json") for r in refusals],
    }
    encoded = _canonical_json(payload).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def reconcile_parity_results(
    canonical_outcome: RunnerOutcome,
    parity_outcome: RunnerOutcome,
    scaffold_equivalence: ScaffoldEquivalence | None = None,
    reward_tolerance: float = 1e-6,
    task_id: str | None = None,
) -> MultiEvalParityResult:
    """Reconcile trial outcomes between canonical and parity runners."""
    tid = task_id if task_id is not None else canonical_outcome.trial_id
    refusals: list[PlanningRefusal] = []

    # Check scaffold equivalence
    if scaffold_equivalence is None or not scaffold_equivalence.declared_equivalent:
        scaffold_equivalent = False
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.UNDECLARED_SCAFFOLD_EQUIVALENCE,
                message="Scaffold equivalence is not declared or not equivalent",
                runner_kind=parity_outcome.runner_kind,
                missing_capabilities=["declared_equivalent"],
            )
        )
    else:
        scaffold_equivalent = True

    # Check environment identity
    c_harness = canonical_outcome.harness_identity
    p_harness = parity_outcome.harness_identity
    if (
        c_harness.environment_image
        and p_harness.environment_image
        and c_harness.environment_image != p_harness.environment_image
    ) or (
        c_harness.environment_digest
        and p_harness.environment_digest
        and c_harness.environment_digest != p_harness.environment_digest
    ):
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.MISMATCHED_ENVIRONMENT_IDENTITY,
                message=(
                    f"Mismatched environment identity between {canonical_outcome.runner_kind.value} "
                    f"and {parity_outcome.runner_kind.value}"
                ),
                runner_kind=parity_outcome.runner_kind,
                missing_capabilities=["environment_identity"],
            )
        )

    # Reconcile verifier rewards
    if canonical_outcome.verifier_reward is None and parity_outcome.verifier_reward is None:
        verifier_reward_reconciled = True
    elif canonical_outcome.verifier_reward is None or parity_outcome.verifier_reward is None:
        verifier_reward_reconciled = False
    else:
        verifier_reward_reconciled = (
            abs(canonical_outcome.verifier_reward - parity_outcome.verifier_reward)
            <= reward_tolerance
        )

    # Reconcile verifier passed status
    verifier_passed_reconciled = canonical_outcome.verifier_passed == parity_outcome.verifier_passed

    # Reconcile artifact digests
    artifact_digests_reconciled = (
        canonical_outcome.artifact_digests == parity_outcome.artifact_digests
    )

    # Trajectory measurements (observables, not failures)
    step_delta = parity_outcome.step_count - canonical_outcome.step_count
    token_delta = parity_outcome.total_tokens - canonical_outcome.total_tokens
    duration_delta = round(parity_outcome.duration_seconds - canonical_outcome.duration_seconds, 6)

    notes: list[str] = []
    if step_delta != 0:
        notes.append(
            f"Step count delta: {step_delta:+d} "
            f"(canonical={canonical_outcome.step_count}, parity={parity_outcome.step_count})"
        )
    if token_delta != 0:
        notes.append(
            f"Token count delta: {token_delta:+d} "
            f"(canonical={canonical_outcome.total_tokens}, parity={parity_outcome.total_tokens})"
        )
    if duration_delta != 0.0:
        notes.append(
            f"Duration delta: {duration_delta:+.3f}s "
            f"(canonical={canonical_outcome.duration_seconds:.3f}s, parity={parity_outcome.duration_seconds:.3f}s)"
        )

    measurements = TrajectoryMeasurements(
        step_delta=step_delta,
        token_delta=token_delta,
        duration_delta_seconds=duration_delta,
        notes=notes,
    )

    # Parity status determination
    if not scaffold_equivalent or len(refusals) > 0:
        parity_status = ParityStatus.REFUSED
    elif verifier_reward_reconciled and verifier_passed_reconciled and artifact_digests_reconciled:
        parity_status = ParityStatus.PARITY_VERIFIED
    else:
        parity_status = ParityStatus.PARITY_DIVERGENT

    refusals_tuple = tuple(refusals)
    reconciliation_digest = _compute_reconciliation_digest(
        task_id=tid,
        canonical_runner=canonical_outcome.runner_kind,
        parity_runner=parity_outcome.runner_kind,
        parity_status=parity_status,
        verifier_reward_reconciled=verifier_reward_reconciled,
        verifier_passed_reconciled=verifier_passed_reconciled,
        artifact_digests_reconciled=artifact_digests_reconciled,
        scaffold_equivalent=scaffold_equivalent,
        measurements=measurements,
        refusals=refusals_tuple,
    )

    return MultiEvalParityResult(
        task_id=tid,
        canonical_runner=canonical_outcome.runner_kind,
        parity_runner=parity_outcome.runner_kind,
        parity_status=parity_status,
        verifier_reward_reconciled=verifier_reward_reconciled,
        verifier_passed_reconciled=verifier_passed_reconciled,
        artifact_digests_reconciled=artifact_digests_reconciled,
        scaffold_equivalent=scaffold_equivalent,
        measurements=measurements,
        refusals=refusals_tuple,
        reconciliation_digest=reconciliation_digest,
    )
