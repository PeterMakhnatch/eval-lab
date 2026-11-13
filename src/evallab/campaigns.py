"""Immutable, policy-gated orchestration for bounded billable campaigns.

Campaigns materialize one queue ``ExperimentSpec`` per declared attempt. They do
not call Harbor directly: submission, human approval, credential preflight,
leases, dispatch, ingestion, and retry refusal remain owned by ``PolicyGate`` and
``Executor``. Program family/cell/opportunity/campaign identities are owned by
``evallab.benchmark_program_contracts``; the private execution binding below is
the isolated import-adapter seam for that shared foundation.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ConfigDict, Field, model_validator

from evallab.benchmark_program_contracts import (
    CampaignCalibrationLedger,
    CampaignMeasurementLedger,
)
from evallab.execution_contracts import (
    DEEPSEEK_MODEL_SELECTOR,
    MIMO_AGENT,
    MIMO_SELFHOSTED_MODEL_SELECTORS,
    TERMINUS_AGENT,
    ZAI_OPENAPI_MODEL_SELECTOR,
    ZAI_OPENAPI_TERMINUS_MODEL_SELECTORS,
    is_breaker_selfhosted_model,
    is_mimo_selfhosted_model,
    is_openrouter_model,
    is_tinker_terminus_model,
)
from evallab.queue import (
    Executor,
)
from evallab.schemas import ContractModel, ExperimentSpec

CampaignLedger = CampaignCalibrationLedger | CampaignMeasurementLedger


_SCHEMA_MANIFEST = "campaign-manifest/v3"
_SCHEMA_EVENT = "campaign-event/v1"
_SCHEMA_STATUS = "campaign-status/v1"
CAMPAIGN_STATE_ROOT = Path("runs/campaigns")
_TRANSIENT_PREFIX = "transient_harness:"
_SPEC_DIGEST_EXCLUDES = {
    "submitted_at",
    "policy_rule",
    "campaign_manifest_digest",
    "campaign_spec_digest",
}
_CREDENTIAL_NAMES = (
    "DEEPSEEK_API_KEY",
    "MSWEA_API_KEY",
    "ZAI_OPENAPI_API_KEY",
)


class CampaignError(RuntimeError):
    """Base campaign orchestration failure."""








def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode()).hexdigest()




def _repo_relative(value: str) -> str:
    if value.startswith("/") or ".." in value.split("/"):
        raise ValueError("campaign paths must stay relative to the repository")
    return value




def experiment_spec_digest(spec: ExperimentSpec) -> str:
    """Digest immutable execution inputs while excluding queue-owned fields."""
    payload = spec.model_dump(mode="json", exclude_none=True)
    for field in _SPEC_DIGEST_EXCLUDES:
        payload.pop(field, None)
    return _digest(payload)


class _FrozenContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CampaignLimits(_FrozenContract):
    max_requests: int = Field(ge=0)
    max_cost_usd: float = Field(ge=0)
    max_input_tokens: int = Field(ge=0)
    max_output_tokens: int = Field(ge=0)
    max_total_tokens: int = Field(ge=0)
    max_wall_clock_seconds: int = Field(ge=1)
    max_concurrency: int = Field(default=1, ge=1)
    max_consecutive_transient_failures: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def total_token_limit_is_coherent(self) -> CampaignLimits:
        if self.max_total_tokens > self.max_input_tokens + self.max_output_tokens:
            raise ValueError("campaign total-token ceiling exceeds input plus output ceilings")
        return self


class TrialLimits(_FrozenContract):
    max_requests: int = Field(ge=0)
    max_cost_usd: float = Field(ge=0)
    max_input_tokens: int = Field(ge=0)
    max_output_tokens: int = Field(ge=0)
    max_total_tokens: int = Field(ge=0)
    max_wall_clock_seconds: int = Field(ge=1, le=28_800)

    @model_validator(mode="after")
    def total_token_limit_is_coherent(self) -> TrialLimits:
        if self.max_total_tokens > self.max_input_tokens + self.max_output_tokens:
            raise ValueError("trial total-token ceiling exceeds input plus output ceilings")
        return self


class CampaignTaskContract(_FrozenContract):
    task_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]+$")
    task_ref: str = Field(min_length=1)
    task_path: str = Field(min_length=1)
    task_family: str = Field(min_length=1)
    task_version: str = Field(min_length=1)
    verifier_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    package_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class CampaignMatrixContract(_FrozenContract):
    matrix_id: str = Field(pattern=r"^[0-9A-HJKMNP-TV-Z]{26}$")
    matrix_path: str = Field(min_length=1)
    matrix_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    benchmark_family: str = Field(min_length=1)
    task_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]+$")
    task_path: str = Field(min_length=1)
    verifier_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    package_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class CampaignAnalysisCell(_FrozenContract):
    model: str = Field(min_length=1)
    agent: str = Field(min_length=1)
    task_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]+$")
    harness: str = Field(min_length=1)
    scaffold: str = Field(min_length=1)
    dose_axis: str = Field(min_length=1)
    dose_value: int | float | str
    dose_unit: str = Field(min_length=1)
    alphabet: str = Field(min_length=1)
    base_task_pair_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def dose_is_finite(self) -> CampaignAnalysisCell:
        if isinstance(self.dose_value, bool):
            raise ValueError("analysis-cell dose value cannot be boolean")
        if isinstance(self.dose_value, float) and not math.isfinite(self.dose_value):
            raise ValueError("analysis-cell dose value must be finite")
        return self


class CampaignDefinitionAttempt(_FrozenContract):
    cell_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]+$")
    task_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]+$")
    attempt: int = Field(ge=1)
    spec: ExperimentSpec
    limits: TrialLimits
    analysis_cell: CampaignAnalysisCell | None = None
    repeat_seed: int | str | None = None

    @model_validator(mode="after")
    def source_spec_is_unbound(self) -> CampaignDefinitionAttempt:
        if self.spec.submitted_at is not None or self.spec.policy_rule is not None:
            raise ValueError("campaign definitions cannot contain queue-owned spec state")
        if any(
            value is not None
            for value in (
                self.spec.campaign_ledger,
                self.spec.campaign_cell_id,
                self.spec.campaign_attempt_id,
                self.spec.campaign_attempt_index,
                self.spec.campaign_manifest_digest,
                self.spec.campaign_spec_digest,
                self.spec.campaign_evidence_store,
            )
        ):
            raise ValueError("campaign definitions cannot contain pre-bound provenance")
        if self.spec.attempts != 1 or self.spec.concurrency != 1:
            raise ValueError("each campaign definition row must describe one attempt")
        if self.spec.task_id is not None and self.spec.task_id != self.task_id:
            raise ValueError("campaign task identity disagrees with ExperimentSpec.task_id")
        if self.analysis_cell is not None:
            if self.repeat_seed is None or self.spec.generator_seed != self.repeat_seed:
                raise ValueError("analysis-cell repeat seed must equal the declared generator seed")
            expected_cell = (
                self.analysis_cell.model == self.spec.model,
                self.analysis_cell.agent == self.spec.agent,
                self.analysis_cell.task_id == self.task_id,
            )
            if not all(expected_cell):
                raise ValueError("analysis-cell identity disagrees with its ExperimentSpec")
        elif self.repeat_seed is not None:
            raise ValueError("repeat_seed requires an analysis-cell identity")
        if self.spec.timeout_seconds > self.limits.max_wall_clock_seconds:
            raise ValueError("spec timeout exceeds the trial wall-clock ceiling")
        if self.spec.billable:
            if self.spec.agent not in {"mini-swe-agent", TERMINUS_AGENT, MIMO_AGENT}:
                raise ValueError(
                    "billable campaigns require a metered mini-swe-agent, terminus-2 or mimoagent adapter"
                )
            if self.spec.agent == MIMO_AGENT:
                allowed_models = set(MIMO_SELFHOSTED_MODEL_SELECTORS)
            elif self.spec.agent == TERMINUS_AGENT:
                allowed_models = set(ZAI_OPENAPI_TERMINUS_MODEL_SELECTORS)
            else:
                allowed_models = {DEEPSEEK_MODEL_SELECTOR, ZAI_OPENAPI_MODEL_SELECTOR}
            model_ok = self.spec.model in allowed_models or (
                self.spec.agent == TERMINUS_AGENT
                and (
                    is_tinker_terminus_model(self.spec.model)
                    or is_mimo_selfhosted_model(self.spec.model)
                    or is_breaker_selfhosted_model(self.spec.model)
                    or is_openrouter_model(self.spec.model)
                )
            )
            if not model_ok:
                raise ValueError(
                    "billable campaign model must be pinned to one of "
                    f"{sorted(allowed_models)}, a tinker/<base>[@checkpoint] "
                    "selector, a matched breaker-selfhosted route, the self-hosted "
                    "MiMo selector, or the OpenRouter selector for terminus-2"
                )
            if self.spec.est_cost_usd <= 0:
                raise ValueError("billable campaign specs require a positive cost estimate")
            if self.limits.max_cost_usd <= 0:
                raise ValueError("billable attempts require a positive cost ceiling")
            if self.spec.est_cost_usd > self.limits.max_cost_usd:
                raise ValueError("estimated cost exceeds the trial cost ceiling")
            if self.limits.max_requests < 1:
                raise ValueError("billable attempts require a provider request ceiling")
            if (
                min(
                    self.limits.max_input_tokens,
                    self.limits.max_output_tokens,
                    self.limits.max_total_tokens,
                )
                < 1
            ):
                raise ValueError("billable attempts require token ceilings")
        return self


class _CampaignAttemptBinding(_FrozenContract):
    ledger: CampaignLedger
    cell_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]+$")
    task_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]+$")
    attempt: int = Field(ge=1)


class CampaignAttempt(_FrozenContract):
    attempt_id: str = Field(pattern=r"^attempt-[0-9a-f]{24}$")
    identity: _CampaignAttemptBinding
    spec_id: str = Field(pattern=r"^campaign-[0-9a-f]{24}$")
    job_name: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]+$")
    spec_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    spec: ExperimentSpec
    limits: TrialLimits
    task_contract: CampaignTaskContract
    analysis_cell: CampaignAnalysisCell | None = None
    repeat_seed: int | str | None = None

    @model_validator(mode="after")
    def spec_is_bound_to_attempt(self) -> CampaignAttempt:
        if experiment_spec_digest(self.spec) != self.spec_digest:
            raise ValueError("campaign attempt spec digest mismatch")
        expected = (
            self.spec.spec_id == self.spec_id,
            self.spec.name == self.job_name,
            self.spec.campaign_ledger == self.identity.ledger,
            self.spec.campaign_cell_id == self.identity.cell_id,
            self.spec.campaign_attempt_id == self.attempt_id,
            self.spec.campaign_attempt_index == self.identity.attempt,
            self.spec.campaign_spec_digest == self.spec_digest,
            self.spec.task_id == self.identity.task_id,
            self.spec.campaign_evidence_store is not None,
        )
        if not all(expected):
            raise ValueError("campaign attempt identity disagrees with its ExperimentSpec")
        if self.task_contract is None:
            raise ValueError("campaign attempts require a frozen registered task contract")
        if self.task_contract is not None:
            contract = self.task_contract
            task_expected = (
                contract.task_id == self.identity.task_id,
                contract.task_ref == self.spec.task,
                contract.task_path == self.spec.executable_task_path,
                contract.task_family == self.spec.task_family,
                contract.task_version == self.spec.task_version,
                contract.verifier_digest == self.spec.verifier_digest,
                contract.package_digest == self.spec.task_package_digest,
            )
            if not all(task_expected):
                raise ValueError("campaign task contract disagrees with its ExperimentSpec")
        if self.analysis_cell is not None:
            analysis_expected = (
                self.analysis_cell.model == self.spec.model,
                self.analysis_cell.agent == self.spec.agent,
                self.analysis_cell.task_id == self.identity.task_id,
                self.repeat_seed is not None,
                self.repeat_seed == self.spec.generator_seed,
            )
            if not all(analysis_expected):
                raise ValueError("campaign analysis-cell identity disagrees with its spec")
        elif self.repeat_seed is not None:
            raise ValueError("campaign repeat seed requires an analysis-cell identity")
        return self


class CampaignManifest(_FrozenContract):
    schema_version: Literal["campaign-manifest/v3"] = _SCHEMA_MANIFEST
    ledger: CampaignLedger
    submitted_by: str = Field(min_length=1)
    limits: CampaignLimits
    matrix: CampaignMatrixContract
    attempts: tuple[CampaignAttempt, ...] = Field(min_length=1)
    evidence_store: str
    analysis_holds: tuple[str, ...] = ()
    manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @property
    def campaign_id(self) -> str:
        return self.ledger.ledger_id

    @property
    def benchmark(self) -> str:
        return self.ledger.family.value

    @property
    def billable(self) -> bool:
        return isinstance(self.ledger, CampaignMeasurementLedger)

    @model_validator(mode="after")
    def manifest_is_immutable_and_single_benchmark(self) -> CampaignManifest:
        _repo_relative(self.evidence_store)
        if any(item.identity.ledger != self.ledger for item in self.attempts):
            raise ValueError("cross-ledger campaign pooling is forbidden")
        for attribute in ("attempt_id", "spec_id", "job_name"):
            values = [getattr(item, attribute) for item in self.attempts]
            if len(values) != len(set(values)):
                raise ValueError(f"campaign {attribute} values must be unique")
        if any(
            item.spec.campaign_manifest_digest != self.manifest_digest
            or item.spec.campaign_evidence_store != self.evidence_store
            for item in self.attempts
        ):
            raise ValueError("attempt spec does not bind the campaign manifest contract")
        if (
            self.matrix.matrix_id != self.ledger.matrix_ref
            or self.matrix.benchmark_family != self.benchmark
        ):
            raise ValueError("campaign matrix identity disagrees with the canonical ledger")
        if any(
            item.task_contract.task_id != self.matrix.task_id
            or item.task_contract.task_path != self.matrix.task_path
            or item.task_contract.task_family != self.matrix.benchmark_family
            or item.task_contract.verifier_digest != self.matrix.verifier_digest
            or item.task_contract.package_digest != self.matrix.package_digest
            for item in self.attempts
        ):
            raise ValueError("campaign attempts disagree with the frozen matrix task identity")
        expected_holds = _analysis_cell_holds(self.attempts)
        if self.analysis_holds != expected_holds:
            raise ValueError("campaign analysis HOLD set does not match exact cells")
        if campaign_manifest_digest(self) != self.manifest_digest:
            raise ValueError("campaign manifest digest mismatch")
        return self


def campaign_manifest_digest(manifest: CampaignManifest | Mapping[str, Any]) -> str:
    payload = (
        manifest.model_dump(mode="json", exclude_none=True)
        if isinstance(manifest, CampaignManifest)
        else json.loads(json.dumps(manifest))
    )
    payload.pop("manifest_digest", None)
    for attempt in payload.get("attempts", []):
        spec = attempt.get("spec") if isinstance(attempt, dict) else None
        if isinstance(spec, dict):
            spec.pop("campaign_manifest_digest", None)
    return _digest(payload)


def _analysis_cell_holds(
    attempts: Sequence[CampaignDefinitionAttempt | CampaignAttempt],
) -> tuple[str, ...]:
    eligible = [item for item in attempts if item.spec.purpose in {"comparison", "elicitation"}]
    holds: list[str] = []
    by_cell_id: dict[str, set[str]] = {}
    by_identity: dict[str, list[CampaignDefinitionAttempt | CampaignAttempt]] = {}
    for item in eligible:
        cell_id = (
            item.cell_id if isinstance(item, CampaignDefinitionAttempt) else item.identity.cell_id
        )
        if item.analysis_cell is None or item.repeat_seed is None:
            holds.append(f"analysis_cell_incomplete:{cell_id}")
            continue
        identity = _canonical_json(item.analysis_cell.model_dump(mode="json"))
        by_cell_id.setdefault(cell_id, set()).add(identity)
        by_identity.setdefault(identity, []).append(item)
    for cell_id, identities in sorted(by_cell_id.items()):
        if len(identities) > 1:
            holds.append(f"analysis_cell_mixed:{cell_id}")
    for cell_attempts in by_identity.values():
        cell_ids = [
            item.cell_id if isinstance(item, CampaignDefinitionAttempt) else item.identity.cell_id
            for item in cell_attempts
        ]
        if any(len(by_cell_id[cell_id]) > 1 for cell_id in cell_ids):
            continue
        seeds = [item.repeat_seed for item in cell_attempts]
        if len(cell_attempts) < 2 or len(seeds) != len(set(seeds)):
            holds.append(f"analysis_cell_repeats_insufficient:{min(cell_ids)}")
    return tuple(sorted(set(holds)))














ArchiveHook = Callable[[CampaignManifest, CampaignAttempt, Path], Mapping[str, Any]]
BackfillHook = Callable[[CampaignManifest, CampaignAttempt, Path], Mapping[str, Any] | None]
DispatchHook = Callable[[Executor, Sequence[str]], int]
Clock = Callable[[], datetime]
CredentialProbe = Callable[[], frozenset[str]]






