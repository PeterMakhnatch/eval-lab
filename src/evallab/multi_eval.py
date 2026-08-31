"""Multi-Eval platform layer for fail-closed, digest-bound execution planning and parity reconciliation.

This module provides authoritative, strictly-typed contracts and algorithms for:
- Typed runner kind taxonomies (Harbor, Inspect, Inspect-Harbor parity lane, Import-only).
- Capability profile validation and refusal generation against task requirements.
- Fail-closed execution planning distinguishing canonical runners from parity lanes.
- Cryptographically digest-bound ParityBinding and HarnessIdentity contracts.
- Deterministic parity reconciliation between Harbor canonical and Inspect-Harbor parity runs.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from evallab.schemas import ContractModel

__all__ = [
    "ExecutionIntent",
    "HarnessIdentity",
    "MultiEvalParityResult",
    "MultiEvalPlan",
    "MultiEvalTaskSpec",
    "ParityBinding",
    "ParityStatus",
    "PlanningRefusal",
    "RefusalCode",
    "RunnerCapabilities",
    "RunnerKind",
    "RunnerOutcome",
    "RUNNER_CAPABILITIES",
    "TaskRequirements",
    "TrajectoryMeasurements",
    "compute_harness_digest",
    "compute_parity_binding_digest",
    "compute_parity_pair_id",
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

    schema_version: Literal["runner-capabilities/v1"] = "runner-capabilities/v1"
    runner_kind: RunnerKind
    profile_version: str = "v1"
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
    supports_parity_lane: bool


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
        supports_parity_lane=True,
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
        supports_parity_lane=False,
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
        supports_parity_lane=True,
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
        supports_parity_lane=False,
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
    UNSUPPORTED_MULTI_STEP = "unsupported_multi_step"
    IMPORT_ONLY_NO_EXECUTION = "import_only_no_execution"
    INVALID_CANONICAL_RUNNER = "invalid_canonical_runner"
    INVALID_PARITY_LANE = "invalid_parity_lane"
    MISSING_PARITY_BINDING = "missing_parity_binding"
    BINDING_TASK_MISMATCH = "binding_task_mismatch"
    BINDING_VERIFIER_MISMATCH = "binding_verifier_mismatch"
    BINDING_PAIR_MISMATCH = "binding_pair_mismatch"
    BINDING_TRIAL_MISMATCH = "binding_trial_mismatch"
    BINDING_ATTEMPT_MISMATCH = "binding_attempt_mismatch"
    BINDING_OUTCOME_MISMATCH = "binding_outcome_mismatch"
    BINDING_PRODUCER_MISMATCH = "binding_producer_mismatch"
    MISMATCHED_ENVIRONMENT_IDENTITY = "mismatched_environment_identity"
    MISSING_HARNESS_IDENTITY = "missing_harness_identity"
    SAME_RUNNER_PARITY = "same_runner_parity"
    MISSING_EVIDENCE = "missing_evidence"
    NULL_VERIFIER_REWARD = "null_verifier_reward"
    REWARD_MISMATCH = "reward_mismatch"
    VERIFIER_STATUS_MISMATCH = "verifier_status_mismatch"
    ARTIFACT_DIGEST_MISMATCH = "artifact_digest_mismatch"


class PlanningRefusal(ContractModel):
    """Structured refusal indicating an incompatible runner or missing capability."""

    code: RefusalCode
    message: str
    runner_kind: RunnerKind | None = None
    missing_capabilities: list[str] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)


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


def _canonical_json(data: Any) -> str:
    """Encode structured data into deterministic canonical JSON."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_harness_digest(
    runner_kind: RunnerKind | str,
    runner_version: str,
    environment_kind: str,
    environment_image: str,
    environment_digest: str,
    prompt_digest: str,
    tool_schema_digest: str,
    scaffold_digest: str,
    scaffold_version: str,
    model_config_digest: str,
    verifier_digest: str,
    harness_parameters: dict[str, Any] | None = None,
) -> str:
    """Compute deterministic cryptographic SHA256 digest over normalized harness identity fields."""
    payload = {
        "environment_digest": environment_digest,
        "environment_image": environment_image,
        "environment_kind": environment_kind,
        "harness_parameters": harness_parameters or {},
        "model_config_digest": model_config_digest,
        "prompt_digest": prompt_digest,
        "runner_kind": runner_kind.value
        if isinstance(runner_kind, RunnerKind)
        else str(runner_kind),
        "runner_version": runner_version,
        "scaffold_digest": scaffold_digest,
        "scaffold_version": scaffold_version,
        "tool_schema_digest": tool_schema_digest,
        "verifier_digest": verifier_digest,
    }
    encoded = _canonical_json(payload).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class HarnessIdentity(ContractModel):
    """Cryptographically bound durable identity and environment configuration of a runner harness."""

    runner_kind: RunnerKind
    runner_version: str = Field(min_length=1)
    environment_kind: str = Field(min_length=1)
    environment_image: str = Field(min_length=1)
    environment_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    prompt_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    tool_schema_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    scaffold_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    scaffold_version: str = Field(min_length=1)
    model_config_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    verifier_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    harness_parameters: dict[str, Any] = Field(default_factory=dict)
    harness_digest: str = Field(default="", pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="before")
    @classmethod
    def _resolve_and_validate_harness_digest(cls, data: Any) -> Any:
        if isinstance(data, dict):
            rk = data.get("runner_kind")
            rv = data.get("runner_version")
            ek = data.get("environment_kind")
            ei = data.get("environment_image")
            ed = data.get("environment_digest")
            pd = data.get("prompt_digest")
            td = data.get("tool_schema_digest")
            sd = data.get("scaffold_digest")
            sv = data.get("scaffold_version")
            md = data.get("model_config_digest")
            vd = data.get("verifier_digest")
            hp = data.get("harness_parameters", {})
            if all(v is not None for v in (rk, rv, ek, ei, ed, pd, td, sd, sv, md, vd)):
                expected = compute_harness_digest(
                    runner_kind=rk,
                    runner_version=str(rv),
                    environment_kind=str(ek),
                    environment_image=str(ei),
                    environment_digest=str(ed),
                    prompt_digest=str(pd),
                    tool_schema_digest=str(td),
                    scaffold_digest=str(sd),
                    scaffold_version=str(sv),
                    model_config_digest=str(md),
                    verifier_digest=str(vd),
                    harness_parameters=hp,
                )
                provided = data.get("harness_digest")
                if provided and provided != expected:
                    raise ValueError(
                        f"harness_digest {provided!r} does not match computed digest {expected!r}"
                    )
                data["harness_digest"] = expected
        return data


def compute_parity_pair_id(
    task_digest: str,
    task_instance_digest: str,
    planned_trial_key: str,
    replicate_key: str,
    factors_digest: str,
    canonical_trial_id: str,
    canonical_attempt_id: str,
    parity_trial_id: str,
    parity_attempt_id: str,
) -> str:
    """Derive deterministic cryptographic pair ID from task, trial, and replicate identities."""
    payload = {
        "canonical_attempt_id": canonical_attempt_id,
        "canonical_trial_id": canonical_trial_id,
        "factors_digest": factors_digest,
        "parity_attempt_id": parity_attempt_id,
        "parity_trial_id": parity_trial_id,
        "planned_trial_key": planned_trial_key,
        "replicate_key": replicate_key,
        "task_digest": task_digest,
        "task_instance_digest": task_instance_digest,
    }
    encoded = _canonical_json(payload).encode("utf-8")
    return f"pair-{hashlib.sha256(encoded).hexdigest()[:32]}"


def compute_parity_binding_digest(
    task_digest: str,
    task_instance_digest: str,
    planned_trial_key: str,
    replicate_key: str,
    factors_digest: str,
    canonical_trial_id: str,
    canonical_attempt_id: str,
    parity_trial_id: str,
    parity_attempt_id: str,
    pair_id: str,
    expected_lanes: tuple[RunnerKind | str, RunnerKind | str],
    allowed_delta: tuple[str, ...],
    outcome_namespace: str,
    outcome_name: str,
    deterministic_producer_kind: str,
    reward_tolerance: float,
    verifier_digest: str,
) -> str:
    """Compute deterministic cryptographic SHA256 digest over parity binding contract fields."""
    payload = {
        "allowed_delta": list(allowed_delta),
        "canonical_attempt_id": canonical_attempt_id,
        "canonical_trial_id": canonical_trial_id,
        "deterministic_producer_kind": deterministic_producer_kind,
        "expected_lanes": [
            r.value if isinstance(r, RunnerKind) else str(r) for r in expected_lanes
        ],
        "factors_digest": factors_digest,
        "outcome_name": outcome_name,
        "outcome_namespace": outcome_namespace,
        "pair_id": pair_id,
        "parity_attempt_id": parity_attempt_id,
        "parity_trial_id": parity_trial_id,
        "planned_trial_key": planned_trial_key,
        "replicate_key": replicate_key,
        "reward_tolerance": reward_tolerance,
        "task_digest": task_digest,
        "task_instance_digest": task_instance_digest,
        "verifier_digest": verifier_digest,
    }
    encoded = _canonical_json(payload).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class ParityBinding(ContractModel):
    """Digest-bound, immutable contract for Harbor <-> Inspect-Harbor parity execution."""

    task_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    task_instance_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    planned_trial_key: str = Field(min_length=1)
    replicate_key: str = Field(min_length=1)
    factors_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    canonical_trial_id: str = Field(min_length=1)
    canonical_attempt_id: str = Field(min_length=1)
    parity_trial_id: str = Field(min_length=1)
    parity_attempt_id: str = Field(min_length=1)
    expected_lanes: tuple[RunnerKind, RunnerKind] = (RunnerKind.HARBOR, RunnerKind.INSPECT_HARBOR)
    allowed_delta: tuple[Literal["runner_lane"], ...] = ("runner_lane",)
    outcome_namespace: str = Field(min_length=1)
    outcome_name: str = Field(min_length=1)
    deterministic_producer_kind: Literal["deterministic_verifier", "deterministic_scorer"] = (
        "deterministic_verifier"
    )
    reward_tolerance: float = Field(default=1e-6, ge=0.0, le=1e-3)
    verifier_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    pair_id: str = Field(default="", min_length=1)
    binding_digest: str = Field(default="", pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("reward_tolerance")
    @classmethod
    def _validate_reward_tolerance(cls, v: float) -> float:
        if not math.isfinite(v) or v < 0.0 or v > 1e-3:
            raise ValueError(f"reward_tolerance must be finite in [0.0, 1e-3], got {v!r}")
        return v

    @model_validator(mode="before")
    @classmethod
    def _validate_and_compute_binding(cls, data: Any) -> Any:
        if isinstance(data, dict):
            raw_lanes = data.get("expected_lanes", (RunnerKind.HARBOR, RunnerKind.INSPECT_HARBOR))
            lanes_tuple = tuple(RunnerKind(r) if isinstance(r, str) else r for r in raw_lanes)
            if lanes_tuple != (RunnerKind.HARBOR, RunnerKind.INSPECT_HARBOR):
                raise ValueError(
                    f"expected_lanes must be (RunnerKind.HARBOR, RunnerKind.INSPECT_HARBOR), got {lanes_tuple!r}"
                )
            data["expected_lanes"] = lanes_tuple

            raw_ad = data.get("allowed_delta", ("runner_lane",))
            ad_tuple = tuple(raw_ad)
            if ad_tuple != ("runner_lane",):
                raise ValueError(
                    f"allowed_delta must be strictly ('runner_lane',), got {ad_tuple!r}"
                )
            data["allowed_delta"] = ad_tuple

            td = data.get("task_digest")
            tid = data.get("task_instance_digest")
            ptk = data.get("planned_trial_key")
            rk = data.get("replicate_key")
            fd = data.get("factors_digest")
            ct_id = data.get("canonical_trial_id")
            ca_id = data.get("canonical_attempt_id")
            pt_id = data.get("parity_trial_id")
            pa_id = data.get("parity_attempt_id")
            ons = data.get("outcome_namespace")
            oname = data.get("outcome_name")
            dpk = data.get("deterministic_producer_kind", "deterministic_verifier")
            rt = data.get("reward_tolerance", 1e-6)
            vd = data.get("verifier_digest")

            if all(
                v is not None
                for v in (td, tid, ptk, rk, fd, ct_id, ca_id, pt_id, pa_id, ons, oname, dpk, vd)
            ):
                expected_pair_id = compute_parity_pair_id(
                    task_digest=str(td),
                    task_instance_digest=str(tid),
                    planned_trial_key=str(ptk),
                    replicate_key=str(rk),
                    factors_digest=str(fd),
                    canonical_trial_id=str(ct_id),
                    canonical_attempt_id=str(ca_id),
                    parity_trial_id=str(pt_id),
                    parity_attempt_id=str(pa_id),
                )
                provided_pair_id = data.get("pair_id")
                if provided_pair_id and provided_pair_id != expected_pair_id:
                    raise ValueError(
                        f"pair_id {provided_pair_id!r} does not match derived pair_id {expected_pair_id!r}"
                    )
                data["pair_id"] = expected_pair_id

                expected = compute_parity_binding_digest(
                    task_digest=str(td),
                    task_instance_digest=str(tid),
                    planned_trial_key=str(ptk),
                    replicate_key=str(rk),
                    factors_digest=str(fd),
                    canonical_trial_id=str(ct_id),
                    canonical_attempt_id=str(ca_id),
                    parity_trial_id=str(pt_id),
                    parity_attempt_id=str(pa_id),
                    pair_id=expected_pair_id,
                    expected_lanes=lanes_tuple,
                    allowed_delta=ad_tuple,
                    outcome_namespace=str(ons),
                    outcome_name=str(oname),
                    deterministic_producer_kind=str(dpk),
                    reward_tolerance=float(rt),
                    verifier_digest=str(vd),
                )
                provided_digest = data.get("binding_digest")
                if provided_digest and provided_digest != expected:
                    raise ValueError(
                        f"binding_digest {provided_digest!r} does not match computed digest {expected!r}"
                    )
                data["binding_digest"] = expected
        return data


class MultiEvalTaskSpec(ContractModel):
    """Specification of a task submitted for multi-eval execution planning."""

    task_id: str = Field(min_length=1)
    benchmark_family: str = Field(min_length=1)
    canonical_runner: RunnerKind
    task_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    requirements: TaskRequirements = Field(default_factory=TaskRequirements)

    @model_validator(mode="after")
    def _validate_canonical_runner(self) -> MultiEvalTaskSpec:
        if self.canonical_runner == RunnerKind.INSPECT_HARBOR:
            raise ValueError(
                "INSPECT_HARBOR is a parity lane only and can never be configured as canonical_runner"
            )
        is_rsi = self.benchmark_family.startswith("rsi-") or self.benchmark_family == "rsi"
        if is_rsi and self.canonical_runner != RunnerKind.HARBOR:
            raise ValueError(
                f"Benchmark family {self.benchmark_family!r} requires canonical_runner=RunnerKind.HARBOR, "
                f"got {self.canonical_runner.value!r}"
            )
        return self


class ExecutionIntent(StrEnum):
    """Intent for executing or ingesting an evaluation trial."""

    CANONICAL_RUN = "canonical_run"
    PARITY_RUN = "parity_run"
    IMPORT_INGEST = "import_ingest"


class MultiEvalPlan(ContractModel):
    """Execution plan resolving canonical runner, parity runners, and validation refusals."""

    task_id: str
    benchmark_family: str
    execution_intent: ExecutionIntent
    canonical_runner: RunnerKind
    task_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    requirements: TaskRequirements
    parity_runners: tuple[RunnerKind, ...] = ()
    harness_identities: dict[str, HarnessIdentity] = Field(default_factory=dict)
    parity_binding: ParityBinding | None = None
    capability_profile_version: str = "v1"
    is_refused: bool = False
    refusals: tuple[PlanningRefusal, ...] = ()
    plan_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


def _compute_plan_digest(
    task_id: str,
    benchmark_family: str,
    canonical_runner: RunnerKind,
    task_digest: str,
    requirements: TaskRequirements,
    execution_intent: ExecutionIntent,
    parity_runners: tuple[RunnerKind, ...],
    harness_identities: dict[str, HarnessIdentity],
    parity_binding: ParityBinding | None,
    capability_profile_version: str,
    refusals: tuple[PlanningRefusal, ...],
) -> str:
    payload = {
        "benchmark_family": benchmark_family,
        "canonical_runner": canonical_runner.value,
        "capability_profile_version": capability_profile_version,
        "execution_intent": execution_intent.value,
        "harness_identities": {
            k: v.model_dump(mode="json") for k, v in sorted(harness_identities.items())
        },
        "parity_binding": (parity_binding.model_dump(mode="json") if parity_binding else None),
        "parity_runners": [r.value for r in parity_runners],
        "refusals": [r.model_dump(mode="json") for r in refusals],
        "requirements": requirements.model_dump(mode="json"),
        "task_digest": task_digest,
        "task_id": task_id,
    }
    encoded = _canonical_json(payload).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def plan_multi_eval_execution(
    task_spec: MultiEvalTaskSpec,
    execution_intent: ExecutionIntent = ExecutionIntent.CANONICAL_RUN,
    parity_binding: ParityBinding | None = None,
    harness_identities: Mapping[RunnerKind | str, HarnessIdentity] | None = None,
    custom_capabilities: Mapping[RunnerKind | str, RunnerCapabilities] | None = None,
) -> MultiEvalPlan:
    """Plan multi-eval execution with strict fail-closed capability and parity contract validation."""
    refusals: list[PlanningRefusal] = []

    def _resolve_caps(kind: RunnerKind) -> RunnerCapabilities:
        if custom_capabilities is not None:
            if kind in custom_capabilities:
                return custom_capabilities[kind]
            if kind.value in custom_capabilities:
                return custom_capabilities[kind.value]
        return get_runner_capabilities(kind)

    canonical_runner = task_spec.canonical_runner
    canonical_caps = _resolve_caps(canonical_runner)

    # 1. Validate canonical runner against requirements
    if execution_intent == ExecutionIntent.IMPORT_INGEST:
        if canonical_runner != RunnerKind.IMPORT_ONLY:
            refusals.append(
                PlanningRefusal(
                    code=RefusalCode.INVALID_CANONICAL_RUNNER,
                    message=f"Import ingest requires canonical_runner=RunnerKind.IMPORT_ONLY, got '{canonical_runner.value}'",
                    runner_kind=canonical_runner,
                    context={"execution_intent": execution_intent.value},
                )
            )
    else:
        refusals.extend(validate_runner_capabilities(canonical_caps, task_spec.requirements))

    # 2. Determine parity runner configuration and validate
    parity_runners: tuple[RunnerKind, ...] = ()
    if execution_intent == ExecutionIntent.PARITY_RUN:
        if canonical_runner != RunnerKind.HARBOR:
            refusals.append(
                PlanningRefusal(
                    code=RefusalCode.INVALID_PARITY_LANE,
                    message=f"Parity run requires canonical_runner=RunnerKind.HARBOR, got '{canonical_runner.value}'",
                    runner_kind=canonical_runner,
                    context={"canonical_runner": canonical_runner.value},
                )
            )

        parity_runners = (RunnerKind.INSPECT_HARBOR,)
        inspect_harbor_caps = _resolve_caps(RunnerKind.INSPECT_HARBOR)
        refusals.extend(validate_runner_capabilities(inspect_harbor_caps, task_spec.requirements))

        if parity_binding is None:
            refusals.append(
                PlanningRefusal(
                    code=RefusalCode.MISSING_PARITY_BINDING,
                    message="Parity run requires an explicit ParityBinding contract",
                    runner_kind=RunnerKind.INSPECT_HARBOR,
                )
            )
        else:
            if parity_binding.task_digest != task_spec.task_digest:
                refusals.append(
                    PlanningRefusal(
                        code=RefusalCode.BINDING_TASK_MISMATCH,
                        message=(
                            f"ParityBinding task_digest '{parity_binding.task_digest}' does not match "
                            f"task_spec.task_digest '{task_spec.task_digest}'"
                        ),
                        runner_kind=RunnerKind.INSPECT_HARBOR,
                        context={
                            "binding_task_digest": parity_binding.task_digest,
                            "spec_task_digest": task_spec.task_digest,
                        },
                    )
                )

    # 3. Normalize harness identities and validate requirements for parity run
    normalized_identities: dict[str, HarnessIdentity] = {}
    if harness_identities is not None:
        for k, v in harness_identities.items():
            k_str = k.value if isinstance(k, RunnerKind) else str(k)
            # Validate mapping key matches identity.runner_kind
            expected_k_str = v.runner_kind.value
            if k_str != expected_k_str:
                refusals.append(
                    PlanningRefusal(
                        code=RefusalCode.MISMATCHED_ENVIRONMENT_IDENTITY,
                        message=(
                            f"Harness identity mapping key '{k_str}' does not match "
                            f"identity.runner_kind '{expected_k_str}'"
                        ),
                        runner_kind=v.runner_kind,
                    )
                )
            normalized_identities[k_str] = v

    if execution_intent == ExecutionIntent.PARITY_RUN:
        c_id = normalized_identities.get(RunnerKind.HARBOR.value)
        p_id = normalized_identities.get(RunnerKind.INSPECT_HARBOR.value)

        if c_id is None or p_id is None:
            missing_lanes = []
            if c_id is None:
                missing_lanes.append(RunnerKind.HARBOR.value)
            if p_id is None:
                missing_lanes.append(RunnerKind.INSPECT_HARBOR.value)
            refusals.append(
                PlanningRefusal(
                    code=RefusalCode.MISSING_HARNESS_IDENTITY,
                    message=f"PARITY_RUN planning requires explicit harness identities for: {', '.join(missing_lanes)}",
                    runner_kind=RunnerKind.INSPECT_HARBOR,
                    missing_capabilities=missing_lanes,
                )
            )
        else:
            # Check verifier digest match with parity_binding
            if parity_binding is not None:
                if c_id.verifier_digest != parity_binding.verifier_digest:
                    refusals.append(
                        PlanningRefusal(
                            code=RefusalCode.BINDING_VERIFIER_MISMATCH,
                            message=(
                                f"Canonical harness verifier_digest '{c_id.verifier_digest}' does not match "
                                f"parity_binding verifier_digest '{parity_binding.verifier_digest}'"
                            ),
                            runner_kind=RunnerKind.HARBOR,
                        )
                    )
                if p_id.verifier_digest != parity_binding.verifier_digest:
                    refusals.append(
                        PlanningRefusal(
                            code=RefusalCode.BINDING_VERIFIER_MISMATCH,
                            message=(
                                f"Parity harness verifier_digest '{p_id.verifier_digest}' does not match "
                                f"parity_binding verifier_digest '{parity_binding.verifier_digest}'"
                            ),
                            runner_kind=RunnerKind.INSPECT_HARBOR,
                        )
                    )

            # Check complete equivalence across all non-lane fields
            mismatches = []
            for field_name in (
                "environment_kind",
                "environment_image",
                "environment_digest",
                "prompt_digest",
                "tool_schema_digest",
                "scaffold_digest",
                "scaffold_version",
                "model_config_digest",
                "verifier_digest",
            ):
                val_c = getattr(c_id, field_name)
                val_p = getattr(p_id, field_name)
                if val_c != val_p:
                    mismatches.append(f"{field_name}: '{val_c}' != '{val_p}'")
            if c_id.harness_parameters != p_id.harness_parameters:
                mismatches.append(
                    f"harness_parameters: '{_canonical_json(c_id.harness_parameters)}' != '{_canonical_json(p_id.harness_parameters)}'"
                )
            if mismatches:
                refusals.append(
                    PlanningRefusal(
                        code=RefusalCode.MISMATCHED_ENVIRONMENT_IDENTITY,
                        message=f"Canonical and parity harness identities mismatch: {', '.join(mismatches)}",
                        runner_kind=RunnerKind.INSPECT_HARBOR,
                        missing_capabilities=["environment_identity"],
                        context={"mismatches": mismatches},
                    )
                )

    refusals_tuple = tuple(refusals)
    plan_digest = _compute_plan_digest(
        task_id=task_spec.task_id,
        benchmark_family=task_spec.benchmark_family,
        canonical_runner=canonical_runner,
        task_digest=task_spec.task_digest,
        requirements=task_spec.requirements,
        execution_intent=execution_intent,
        parity_runners=parity_runners,
        harness_identities=normalized_identities,
        parity_binding=parity_binding,
        capability_profile_version=canonical_caps.profile_version,
        refusals=refusals_tuple,
    )

    return MultiEvalPlan(
        task_id=task_spec.task_id,
        benchmark_family=task_spec.benchmark_family,
        execution_intent=execution_intent,
        canonical_runner=canonical_runner,
        task_digest=task_spec.task_digest,
        requirements=task_spec.requirements,
        parity_runners=parity_runners,
        harness_identities=normalized_identities,
        parity_binding=parity_binding,
        capability_profile_version=canonical_caps.profile_version,
        is_refused=len(refusals_tuple) > 0,
        refusals=refusals_tuple,
        plan_digest=plan_digest,
    )


class RunnerOutcome(ContractModel):
    """Full trial execution outcome produced by a single runner execution."""

    runner_kind: RunnerKind
    trial_id: str = Field(min_length=1)
    task_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    parity_pair_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    outcome_namespace: str = Field(min_length=1)
    outcome_name: str = Field(min_length=1)
    deterministic_producer_kind: Literal["deterministic_verifier", "deterministic_scorer"]
    verifier_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    verifier_reward: float | None = None
    verifier_passed: bool
    artifact_digests: dict[str, str] = Field(default_factory=dict)
    step_count: int = Field(ge=0, default=0)
    total_tokens: int = Field(ge=0, default=0)
    duration_seconds: float = Field(ge=0.0, default=0.0)
    harness_identity: HarnessIdentity
    source_revision_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    raw_log_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("artifact_digests")
    @classmethod
    def _validate_artifact_digests_patterns(cls, v: dict[str, str]) -> dict[str, str]:
        for k, digest in v.items():
            if not isinstance(digest, str) or not re.match(r"^sha256:[0-9a-f]{64}$", digest):
                raise ValueError(
                    f"artifact digest for {k!r} must match sha256:<64 hex chars>, got {digest!r}"
                )
        return v


class ParityStatus(StrEnum):
    """Status classification of cross-runner parity reconciliation."""

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

    task_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    pair_id: str
    canonical_runner: RunnerKind
    parity_runner: RunnerKind
    parity_status: ParityStatus
    verifier_reward_reconciled: bool
    verifier_passed_reconciled: bool
    artifact_digests_reconciled: bool
    harness_identity_reconciled: bool
    binding_reconciled: bool
    measurements: TrajectoryMeasurements
    refusals: tuple[PlanningRefusal, ...] = ()
    reconciliation_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


def _compute_reconciliation_digest(
    canonical_outcome: RunnerOutcome,
    parity_outcome: RunnerOutcome,
    parity_binding: ParityBinding,
    parity_status: ParityStatus,
    verifier_reward_reconciled: bool,
    verifier_passed_reconciled: bool,
    artifact_digests_reconciled: bool,
    harness_identity_reconciled: bool,
    binding_reconciled: bool,
    measurements: TrajectoryMeasurements,
    refusals: tuple[PlanningRefusal, ...],
) -> str:
    payload = {
        "artifact_digests_reconciled": artifact_digests_reconciled,
        "binding_reconciled": binding_reconciled,
        "canonical_outcome": canonical_outcome.model_dump(mode="json"),
        "comparison_policy": {
            "allowed_delta": list(parity_binding.allowed_delta),
            "deterministic_producer_kind": parity_binding.deterministic_producer_kind,
            "expected_lanes": [r.value for r in parity_binding.expected_lanes],
            "outcome_name": parity_binding.outcome_name,
            "outcome_namespace": parity_binding.outcome_namespace,
            "reward_tolerance": parity_binding.reward_tolerance,
            "verifier_digest": parity_binding.verifier_digest,
        },
        "harness_identity_reconciled": harness_identity_reconciled,
        "measurements": measurements.model_dump(mode="json"),
        "parity_binding": parity_binding.model_dump(mode="json"),
        "parity_outcome": parity_outcome.model_dump(mode="json"),
        "parity_status": parity_status.value,
        "refusals": [r.model_dump(mode="json") for r in refusals],
        "verifier_passed_reconciled": verifier_passed_reconciled,
        "verifier_reward_reconciled": verifier_reward_reconciled,
    }
    encoded = _canonical_json(payload).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def reconcile_parity_results(
    canonical_outcome: RunnerOutcome,
    parity_outcome: RunnerOutcome,
    parity_binding: ParityBinding,
) -> MultiEvalParityResult:
    """Reconcile trial outcomes between canonical Harbor and Inspect-Harbor parity runner with fail-closed rules."""
    refusals: list[PlanningRefusal] = []

    # 1. Lanes check
    if canonical_outcome.runner_kind == parity_outcome.runner_kind:
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.SAME_RUNNER_PARITY,
                message=f"Cannot perform parity reconciliation between identical runner kind '{canonical_outcome.runner_kind.value}'",
                runner_kind=parity_outcome.runner_kind,
            )
        )
    elif (
        canonical_outcome.runner_kind != RunnerKind.HARBOR
        or parity_outcome.runner_kind != RunnerKind.INSPECT_HARBOR
    ):
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.INVALID_PARITY_LANE,
                message=(
                    f"Invalid parity lane pair: canonical '{canonical_outcome.runner_kind.value}', "
                    f"parity '{parity_outcome.runner_kind.value}'. Expected (harbor, inspect_harbor)"
                ),
                runner_kind=parity_outcome.runner_kind,
                context={
                    "canonical_runner": canonical_outcome.runner_kind.value,
                    "parity_runner": parity_outcome.runner_kind.value,
                },
            )
        )

    # 1b. Internal outcome runner_kind vs harness_identity runner_kind check
    if canonical_outcome.harness_identity.runner_kind != canonical_outcome.runner_kind:
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.MISMATCHED_ENVIRONMENT_IDENTITY,
                message=(
                    f"Canonical outcome runner_kind '{canonical_outcome.runner_kind.value}' does not match "
                    f"harness_identity.runner_kind '{canonical_outcome.harness_identity.runner_kind.value}'"
                ),
                runner_kind=canonical_outcome.runner_kind,
                missing_capabilities=["environment_identity"],
            )
        )
    if parity_outcome.harness_identity.runner_kind != parity_outcome.runner_kind:
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.MISMATCHED_ENVIRONMENT_IDENTITY,
                message=(
                    f"Parity outcome runner_kind '{parity_outcome.runner_kind.value}' does not match "
                    f"harness_identity.runner_kind '{parity_outcome.harness_identity.runner_kind.value}'"
                ),
                runner_kind=parity_outcome.runner_kind,
                missing_capabilities=["environment_identity"],
            )
        )

    # 2. Task, Pair, Trial, and Attempt exact-match checks
    if (
        canonical_outcome.task_digest != parity_binding.task_digest
        or parity_outcome.task_digest != parity_binding.task_digest
    ):
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.BINDING_TASK_MISMATCH,
                message=(
                    f"Task digest mismatch: canonical='{canonical_outcome.task_digest}', "
                    f"parity='{parity_outcome.task_digest}', binding='{parity_binding.task_digest}'"
                ),
                runner_kind=parity_outcome.runner_kind,
                context={
                    "canonical_task_digest": canonical_outcome.task_digest,
                    "parity_task_digest": parity_outcome.task_digest,
                    "binding_task_digest": parity_binding.task_digest,
                },
            )
        )

    if (
        canonical_outcome.parity_pair_id != parity_binding.pair_id
        or parity_outcome.parity_pair_id != parity_binding.pair_id
        or canonical_outcome.parity_pair_id != parity_outcome.parity_pair_id
    ):
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.BINDING_PAIR_MISMATCH,
                message=(
                    f"Parity pair ID mismatch: canonical='{canonical_outcome.parity_pair_id}', "
                    f"parity='{parity_outcome.parity_pair_id}', binding='{parity_binding.pair_id}'"
                ),
                runner_kind=parity_outcome.runner_kind,
                context={
                    "canonical_pair_id": canonical_outcome.parity_pair_id,
                    "parity_pair_id": parity_outcome.parity_pair_id,
                    "binding_pair_id": parity_binding.pair_id,
                },
            )
        )

    # Exact bound trial_id checks
    if canonical_outcome.trial_id != parity_binding.canonical_trial_id:
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.BINDING_TRIAL_MISMATCH,
                message=(
                    f"Canonical outcome trial_id '{canonical_outcome.trial_id}' does not match "
                    f"binding.canonical_trial_id '{parity_binding.canonical_trial_id}'"
                ),
                runner_kind=canonical_outcome.runner_kind,
            )
        )
    if parity_outcome.trial_id != parity_binding.parity_trial_id:
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.BINDING_TRIAL_MISMATCH,
                message=(
                    f"Parity outcome trial_id '{parity_outcome.trial_id}' does not match "
                    f"binding.parity_trial_id '{parity_binding.parity_trial_id}'"
                ),
                runner_kind=parity_outcome.runner_kind,
            )
        )

    # Exact bound attempt_id checks
    if canonical_outcome.attempt_id != parity_binding.canonical_attempt_id:
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.BINDING_ATTEMPT_MISMATCH,
                message=(
                    f"Canonical outcome attempt_id '{canonical_outcome.attempt_id}' does not match "
                    f"binding.canonical_attempt_id '{parity_binding.canonical_attempt_id}'"
                ),
                runner_kind=canonical_outcome.runner_kind,
            )
        )
    if parity_outcome.attempt_id != parity_binding.parity_attempt_id:
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.BINDING_ATTEMPT_MISMATCH,
                message=(
                    f"Parity outcome attempt_id '{parity_outcome.attempt_id}' does not match "
                    f"binding.parity_attempt_id '{parity_binding.parity_attempt_id}'"
                ),
                runner_kind=parity_outcome.runner_kind,
            )
        )

    # 3. Verifier, Outcome, and Producer kind checks
    if (
        canonical_outcome.verifier_digest != parity_binding.verifier_digest
        or parity_outcome.verifier_digest != parity_binding.verifier_digest
    ):
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.BINDING_VERIFIER_MISMATCH,
                message=(
                    f"Verifier digest mismatch: canonical='{canonical_outcome.verifier_digest}', "
                    f"parity='{parity_outcome.verifier_digest}', binding='{parity_binding.verifier_digest}'"
                ),
                runner_kind=parity_outcome.runner_kind,
                context={
                    "canonical_verifier_digest": canonical_outcome.verifier_digest,
                    "parity_verifier_digest": parity_outcome.verifier_digest,
                    "binding_verifier_digest": parity_binding.verifier_digest,
                },
            )
        )

    # Outcome verifier digest vs harness_identity verifier digest check
    if canonical_outcome.harness_identity.verifier_digest != canonical_outcome.verifier_digest:
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.BINDING_VERIFIER_MISMATCH,
                message=(
                    f"Canonical outcome verifier_digest '{canonical_outcome.verifier_digest}' does not match "
                    f"harness_identity.verifier_digest '{canonical_outcome.harness_identity.verifier_digest}'"
                ),
                runner_kind=canonical_outcome.runner_kind,
            )
        )
    if parity_outcome.harness_identity.verifier_digest != parity_outcome.verifier_digest:
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.BINDING_VERIFIER_MISMATCH,
                message=(
                    f"Parity outcome verifier_digest '{parity_outcome.verifier_digest}' does not match "
                    f"harness_identity.verifier_digest '{parity_outcome.harness_identity.verifier_digest}'"
                ),
                runner_kind=parity_outcome.runner_kind,
            )
        )

    # Outcome namespace / name check
    if (
        canonical_outcome.outcome_namespace != parity_binding.outcome_namespace
        or parity_outcome.outcome_namespace != parity_binding.outcome_namespace
        or canonical_outcome.outcome_name != parity_binding.outcome_name
        or parity_outcome.outcome_name != parity_binding.outcome_name
    ):
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.BINDING_OUTCOME_MISMATCH,
                message=(
                    f"Outcome namespace/name mismatch with binding: "
                    f"canonical='{canonical_outcome.outcome_namespace}.{canonical_outcome.outcome_name}', "
                    f"parity='{parity_outcome.outcome_namespace}.{parity_outcome.outcome_name}', "
                    f"binding='{parity_binding.outcome_namespace}.{parity_binding.outcome_name}'"
                ),
                runner_kind=parity_outcome.runner_kind,
                context={
                    "canonical_outcome": f"{canonical_outcome.outcome_namespace}.{canonical_outcome.outcome_name}",
                    "parity_outcome": f"{parity_outcome.outcome_namespace}.{parity_outcome.outcome_name}",
                    "binding_outcome": f"{parity_binding.outcome_namespace}.{parity_binding.outcome_name}",
                },
            )
        )

    # Deterministic producer kind check
    if (
        canonical_outcome.deterministic_producer_kind != parity_binding.deterministic_producer_kind
        or parity_outcome.deterministic_producer_kind != parity_binding.deterministic_producer_kind
    ):
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.BINDING_PRODUCER_MISMATCH,
                message=(
                    f"Deterministic producer kind mismatch with binding: "
                    f"canonical='{canonical_outcome.deterministic_producer_kind}', "
                    f"parity='{parity_outcome.deterministic_producer_kind}', "
                    f"binding='{parity_binding.deterministic_producer_kind}'"
                ),
                runner_kind=parity_outcome.runner_kind,
            )
        )

    # 4. Evidence check
    if (
        not canonical_outcome.raw_log_digest
        or not canonical_outcome.raw_log_digest.strip()
        or not parity_outcome.raw_log_digest
        or not parity_outcome.raw_log_digest.strip()
    ):
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.MISSING_EVIDENCE,
                message="Raw log digest is missing or empty",
                runner_kind=parity_outcome.runner_kind,
            )
        )

    if canonical_outcome.verifier_reward is None or parity_outcome.verifier_reward is None:
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.NULL_VERIFIER_REWARD,
                message="Verifier reward is None. Null rewards fail closed and cannot verify parity.",
                runner_kind=parity_outcome.runner_kind,
                context={
                    "canonical_reward": canonical_outcome.verifier_reward,
                    "parity_reward": parity_outcome.verifier_reward,
                },
            )
        )

    if not canonical_outcome.artifact_digests or not parity_outcome.artifact_digests:
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.MISSING_EVIDENCE,
                message="Artifact digests cannot be empty; at least one artifact digest is required.",
                runner_kind=parity_outcome.runner_kind,
            )
        )

    # 5. Harness equality check across ALL non-lane fields
    c_harness = canonical_outcome.harness_identity
    p_harness = parity_outcome.harness_identity
    harness_mismatches = []
    for field_name in (
        "environment_kind",
        "environment_image",
        "environment_digest",
        "prompt_digest",
        "tool_schema_digest",
        "scaffold_digest",
        "scaffold_version",
        "model_config_digest",
        "verifier_digest",
    ):
        val_c = getattr(c_harness, field_name)
        val_p = getattr(p_harness, field_name)
        if val_c != val_p:
            harness_mismatches.append(f"{field_name}: '{val_c}' != '{val_p}'")
    if c_harness.harness_parameters != p_harness.harness_parameters:
        harness_mismatches.append(
            f"harness_parameters: '{_canonical_json(c_harness.harness_parameters)}' != '{_canonical_json(p_harness.harness_parameters)}'"
        )

    if harness_mismatches:
        refusals.append(
            PlanningRefusal(
                code=RefusalCode.MISMATCHED_ENVIRONMENT_IDENTITY,
                message=f"Canonical and parity harness identities mismatch: {', '.join(harness_mismatches)}",
                runner_kind=parity_outcome.runner_kind,
                missing_capabilities=["environment_identity"],
                context={"mismatches": harness_mismatches},
            )
        )

    # 6. Reconciliation comparison
    if canonical_outcome.verifier_reward is None or parity_outcome.verifier_reward is None:
        verifier_reward_reconciled = False
    else:
        verifier_reward_reconciled = (
            abs(canonical_outcome.verifier_reward - parity_outcome.verifier_reward)
            <= parity_binding.reward_tolerance
        )

    verifier_passed_reconciled = canonical_outcome.verifier_passed == parity_outcome.verifier_passed
    artifact_digests_reconciled = (
        bool(canonical_outcome.artifact_digests)
        and bool(parity_outcome.artifact_digests)
        and canonical_outcome.artifact_digests == parity_outcome.artifact_digests
    )

    binding_reconciled = not any(
        r.code
        in {
            RefusalCode.BINDING_TASK_MISMATCH,
            RefusalCode.BINDING_PAIR_MISMATCH,
            RefusalCode.BINDING_TRIAL_MISMATCH,
            RefusalCode.BINDING_ATTEMPT_MISMATCH,
            RefusalCode.BINDING_VERIFIER_MISMATCH,
            RefusalCode.BINDING_OUTCOME_MISMATCH,
            RefusalCode.BINDING_PRODUCER_MISMATCH,
            RefusalCode.MISSING_PARITY_BINDING,
        }
        for r in refusals
    )
    harness_identity_reconciled = not any(
        r.code == RefusalCode.MISMATCHED_ENVIRONMENT_IDENTITY for r in refusals
    )

    # 7. Trajectory measurements
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

    # 8. Status assignment
    if len(refusals) > 0:
        parity_status = ParityStatus.REFUSED
    elif (
        verifier_reward_reconciled
        and verifier_passed_reconciled
        and artifact_digests_reconciled
        and harness_identity_reconciled
        and binding_reconciled
    ):
        parity_status = ParityStatus.PARITY_VERIFIED
    else:
        parity_status = ParityStatus.PARITY_DIVERGENT

    refusals_tuple = tuple(refusals)

    # 9. Reconciliation digest
    reconciliation_digest = _compute_reconciliation_digest(
        canonical_outcome=canonical_outcome,
        parity_outcome=parity_outcome,
        parity_binding=parity_binding,
        parity_status=parity_status,
        verifier_reward_reconciled=verifier_reward_reconciled,
        verifier_passed_reconciled=verifier_passed_reconciled,
        artifact_digests_reconciled=artifact_digests_reconciled,
        harness_identity_reconciled=harness_identity_reconciled,
        binding_reconciled=binding_reconciled,
        measurements=measurements,
        refusals=refusals_tuple,
    )

    return MultiEvalParityResult(
        task_digest=parity_binding.task_digest,
        pair_id=parity_binding.pair_id,
        canonical_runner=canonical_outcome.runner_kind,
        parity_runner=parity_outcome.runner_kind,
        parity_status=parity_status,
        verifier_reward_reconciled=verifier_reward_reconciled,
        verifier_passed_reconciled=verifier_passed_reconciled,
        artifact_digests_reconciled=artifact_digests_reconciled,
        harness_identity_reconciled=harness_identity_reconciled,
        binding_reconciled=binding_reconciled,
        measurements=measurements,
        refusals=refusals_tuple,
        reconciliation_digest=reconciliation_digest,
    )
