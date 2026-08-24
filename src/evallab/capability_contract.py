"""Typed, evidence-bound P/R/U/C/Y capability admission.

The report is an independent vector, never an aggregate score. Missing evidence
is descriptive insufficiency; stale, mismatched, contaminated, or revised
evidence fails closed.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from evallab.registry import (
    certification_envelope_from_packet,
    verify_certification_packet,
    verify_control_evidence,
)
from evallab.schemas import (
    CapabilityCurveReport,
    ContractModel,
    TaskContamination,
    TaskRegistryRecord,
)

_SHA = r"^sha256:[0-9a-f]{64}$"


class ClaimKind(StrEnum):
    P = "P"
    R = "R"
    U = "U"
    C = "C"
    Y = "Y"


ArtifactKind = Literal[
    "curve_report", "power_analysis", "cohort_report", "workbench_certificate",
    "task_registry_record", "state_event_facts", "upstream_imports", "protocol_spec",
    "surface_oracle", "nop_control", "equivalence_preregistration", "adaptation_report",
    "longitudinal_phase", "longitudinal_report", "production_report", "harness_policy",
    "freeze_record", "novelty_certificate", "integration_ledger",
]


class _EquivalencePreregistration(ContractModel):
    schema_version: Literal[1] = 1
    claim_kind: Literal["P"] = "P"
    metric: Literal["paired_delta"] = "paired_delta"
    direction: Literal["equivalence"] = "equivalence"
    primary_k: int = Field(ge=1)
    equivalence_margin: float = Field(gt=0)


class _LongitudinalPhaseRecord(ContractModel):
    schema_version: Literal[1] = 1
    phase_identity: str = Field(min_length=1)
    phase_index: int = Field(ge=0)
    observed_at: datetime


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _digest(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _identity(path: str, digest: str, kind: str) -> str:
    return _digest(_canonical({"kind": kind, "path": path, "sha256": digest}))


def _relative(value: str, label: str) -> str:
    parsed = PurePosixPath(value)
    if (not value or parsed.is_absolute() or value != parsed.as_posix()
            or any(part in {"", ".", ".."} for part in parsed.parts)):
        raise ValueError(f"{label} must be a normalized repository-relative POSIX path")
    return value


class BoundArtifactRef(ContractModel):
    """Reference bound to current bytes and their repository location and kind."""

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_SHA)
    kind: ArtifactKind
    identity: str = Field(pattern=_SHA)

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        return _relative(value, "artifact path")

    @model_validator(mode="after")
    def exact_identity(self) -> BoundArtifactRef:
        if self.identity != _identity(self.path, self.sha256, self.kind):
            raise ValueError("artifact identity does not bind path, sha256, and kind")
        return self

    @classmethod
    def bind(cls, *, repo_root: Path, path: str, kind: ArtifactKind) -> BoundArtifactRef:
        normalized = _relative(path, "artifact path")
        digest = _digest(_read_file(repo_root, normalized))
        return cls(path=normalized, sha256=digest, kind=kind,
                   identity=_identity(normalized, digest, kind))


class HarnessPolicySnapshot(ContractModel):
    artifact: BoundArtifactRef
    label: str = Field(min_length=1)
    protocol_identity: str = Field(min_length=1)
    harness_identity: str = Field(min_length=1)
    retries: int = Field(ge=0)
    schema_guard: bool
    tool_shortlisting: list[str]
    termination: str = Field(min_length=1)
    step_budget: int = Field(ge=1)
    token_budget: int = Field(ge=1)
    wall_budget_seconds: int = Field(ge=1)
    compaction_model: str = Field(min_length=1)
    compaction_settings: dict[str, Any]
    compaction_seed: int
    model_identity: str = Field(min_length=1)
    preamble_identity: str = Field(min_length=1)
    adapter_identity: str = Field(min_length=1)
    truncation: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_snapshot(self) -> HarnessPolicySnapshot:
        if self.artifact.kind != "harness_policy":
            raise ValueError("harness snapshot artifact must have kind harness_policy")
        if len(self.tool_shortlisting) != len(set(self.tool_shortlisting)):
            raise ValueError("tool_shortlisting must not contain duplicates")
        return self

    def controls(self, *, excluding: str | None = None) -> dict[str, Any]:
        values = self.model_dump(mode="json", exclude={"artifact", "label"})
        if excluding == "protocol":
            values.pop("protocol_identity")
        elif excluding == "harness":
            values.pop("harness_identity")
        return values

    def budgets(self) -> tuple[int, int, int, int]:
        return self.retries, self.step_budget, self.token_budget, self.wall_budget_seconds


class FreezeRecord(ContractModel):
    artifact: BoundArtifactRef
    frozen_at: datetime
    first_trace_at: datetime
    frozen_artifacts: list[BoundArtifactRef] = Field(min_length=1)
    post_trace_revisions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_freeze(self) -> FreezeRecord:
        if self.artifact.kind != "freeze_record":
            raise ValueError("freeze artifact must have kind freeze_record")
        ids = [item.identity for item in self.frozen_artifacts]
        if len(ids) != len(set(ids)):
            raise ValueError("frozen_artifacts must be unique")
        return self


class NoveltyCertificate(ContractModel):
    artifact: BoundArtifactRef
    issued_at: datetime
    first_trace_at: datetime
    task_identity: str = Field(min_length=1)
    registry_record: BoundArtifactRef
    contamination: TaskContamination
    heldout_allowed_use: bool
    reference_prompt_borrowing: bool
    recoverable_in_world_knowledge: bool
    post_trace_contamination: bool = False

    @model_validator(mode="after")
    def validate_kinds(self) -> NoveltyCertificate:
        if self.artifact.kind != "novelty_certificate":
            raise ValueError("novelty artifact must have kind novelty_certificate")
        if self.registry_record.kind != "task_registry_record":
            raise ValueError("registry_record must have kind task_registry_record")
        return self


class IntegrationCostLedger(ContractModel):
    """Raw measured costs only; deliberately no normalized or aggregate score.

    ``added_loc`` counts nonblank, noncomment physical lines in M052-owned new
    production modules. ``modified_loc`` counts the same line unit only inside
    explicit M052 integration blocks in pre-existing production modules.
    ``revisions`` counts one current sha256-bound source snapshot per measured
    production path; it is not a commit count or an edit-round estimate.
    """

    artifact: BoundArtifactRef
    raw_dependencies: list[str]
    added_loc: int = Field(ge=0)
    modified_loc: int = Field(ge=0)
    environment_specific_symbols: list[str]
    prompt_tokens: int = Field(ge=0)
    revisions: int = Field(ge=0)
    post_trace_fixes: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_kind(self) -> IntegrationCostLedger:
        if self.artifact.kind != "integration_ledger":
            raise ValueError("ledger artifact must have kind integration_ledger")
        return self


class CapabilityClaimSpec(ContractModel):
    kind: ClaimKind
    availability: Literal["available", "unavailable"]
    statement: str = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list)
    evidence: list[BoundArtifactRef] = Field(default_factory=list)
    harness_policies: list[HarnessPolicySnapshot] = Field(default_factory=list)
    freeze: FreezeRecord | None = None
    declared_factor: Literal["protocol", "harness"] | None = None
    preregistered_equivalence_margin: float | None = Field(default=None, gt=0)
    equivalence_interval_95: tuple[float, float] | None = None
    frozen_domains: list[str] = Field(default_factory=list)
    frozen_environments: list[str] = Field(default_factory=list)
    inferential_outcome: (
        Literal["supports_claim", "inconclusive", "contradicts_claim"] | None
    ) = None
    novelty: NoveltyCertificate | None = None
    longitudinal_phases: list[BoundArtifactRef] = Field(default_factory=list)
    integration_cost: IntegrationCostLedger | None = None

    @field_validator("equivalence_interval_95")
    @classmethod
    def ordered_interval(cls, value: tuple[float, float] | None) -> tuple[float, float] | None:
        if value is not None and value[0] > value[1]:
            raise ValueError("equivalence_interval_95 must be ordered")
        return value

    @model_validator(mode="after")
    def explicit_unavailability(self) -> CapabilityClaimSpec:
        if self.availability == "unavailable" and (self.evidence or self.harness_policies
                or self.freeze is not None or self.novelty is not None
                or self.integration_cost is not None or self.longitudinal_phases):
            raise ValueError("unavailable claims cannot carry evidence or execution claims")
        return self


class CapabilityContractSpec(ContractModel):
    schema_version: Literal[1] = 1
    experiment_id: str = Field(min_length=1)
    claims: list[CapabilityClaimSpec] = Field(default_factory=list)
    authoring_identity_inputs: list[str] = Field(default_factory=list)
    tuning_identity_inputs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_claims(self) -> CapabilityContractSpec:
        kinds = [claim.kind for claim in self.claims]
        if len(kinds) != len(set(kinds)):
            raise ValueError("a capability contract may contain at most one claim per kind")
        return self


class CapabilityClaimResult(ContractModel):
    kind: ClaimKind
    status: Literal["satisfied", "insufficient", "invalid", "unavailable"]
    reasons: list[str]
    evidence: list[BoundArtifactRef]


class CapabilityContractReport(ContractModel):
    schema_version: Literal[1] = 1
    experiment_id: str
    status: Literal["valid_insufficient", "invalid", "eligible_for_analysis"]
    refuse_substantive_generality: bool
    claims: list[CapabilityClaimResult]
    evidence_digest: str = Field(pattern=_SHA)

    @model_validator(mode="after")
    def coherent_vector(self) -> CapabilityContractReport:
        if [item.kind for item in self.claims] != list(ClaimKind):
            raise ValueError("report must preserve the complete P/R/U/C/Y vector")
        states = {item.status for item in self.claims}
        expected = ("invalid" if "invalid" in states else "eligible_for_analysis"
                    if states == {"satisfied"} else "valid_insufficient")
        if self.status != expected:
            raise ValueError("global status contradicts per-claim results")
        if self.refuse_substantive_generality != (self.status != "eligible_for_analysis"):
            raise ValueError("generality refusal must follow typed eligibility")
        return self


def _read_file(repo_root: Path, relative: str) -> bytes:
    if repo_root.is_symlink():
        raise ValueError("repository root cannot be a symlink")
    root = repo_root.resolve(strict=True)
    candidate = root
    for part in PurePosixPath(relative).parts:
        candidate = candidate / part
        try:
            mode = os.lstat(candidate).st_mode
        except OSError as exc:
            raise ValueError(f"artifact is missing: {relative}") from exc
        if stat.S_ISLNK(mode):
            raise ValueError(f"artifact path traverses a symlink: {relative}")
    try:
        candidate.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError(f"artifact escapes repository: {relative}") from exc
    if not stat.S_ISREG(os.lstat(candidate).st_mode):
        raise ValueError(f"artifact is not a regular file: {relative}")
    return candidate.read_bytes()


def _verify(root: Path, ref: BoundArtifactRef) -> tuple[bytes | None, str | None]:
    try:
        raw = _read_file(root, ref.path)
    except ValueError as exc:
        return None, str(exc)
    current = _digest(raw)
    if current != ref.sha256:
        return raw, f"artifact bytes changed after binding: {ref.path}"
    if _identity(ref.path, current, ref.kind) != ref.identity:
        return raw, f"artifact replay/identity mismatch: {ref.path}"
    return raw, None


def _refs(claim: CapabilityClaimSpec) -> list[BoundArtifactRef]:
    refs = list(claim.evidence) + [policy.artifact for policy in claim.harness_policies]
    if claim.freeze:
        refs += [claim.freeze.artifact, *claim.freeze.frozen_artifacts]
    if claim.novelty:
        refs += [claim.novelty.artifact, claim.novelty.registry_record]
    if claim.integration_cost:
        refs.append(claim.integration_cost.artifact)
    refs += claim.longitudinal_phases
    return list({ref.identity: ref for ref in refs}.values())


def _verify_refs(claim: CapabilityClaimSpec, root: Path,
                 observed: dict[str, str]) -> tuple[dict[str, bytes], list[str]]:
    raws: dict[str, bytes] = {}
    errors: list[str] = []
    for ref in _refs(claim):
        raw, error = _verify(root, ref)
        observed[ref.identity] = _digest(raw) if raw is not None else "missing"
        if error:
            errors.append(error)
        elif raw is not None:
            raws[ref.identity] = raw
    return raws, errors


def _common(claim: CapabilityClaimSpec) -> tuple[list[str], list[str]]:
    insufficient: list[str] = []
    invalid: list[str] = []
    if not claim.harness_policies:
        insufficient.append("complete harness policy snapshot is unavailable")
    freeze = claim.freeze
    if freeze is None:
        insufficient.append("pre-trace freeze record is unavailable")
        return insufficient, invalid
    if freeze.frozen_at >= freeze.first_trace_at:
        invalid.append("freeze must precede the first trace")
    if freeze.post_trace_revisions:
        invalid.append("post-trace revision invalidates frozen evidence")
    pretrace_kinds = {
        "workbench_certificate",
        "task_registry_record",
        "protocol_spec",
        "surface_oracle",
        "nop_control",
        "equivalence_preregistration",
        "power_analysis",
    }
    required = {
        item.identity for item in claim.evidence if item.kind in pretrace_kinds
    }
    required.update(policy.artifact.identity for policy in claim.harness_policies)
    if claim.novelty:
        required.update((claim.novelty.artifact.identity, claim.novelty.registry_record.identity))
    required.update(item.identity for item in claim.longitudinal_phases)
    if missing := required - {item.identity for item in freeze.frozen_artifacts}:
        invalid.append(f"freeze omits {len(missing)} declared pre-trace input(s)")
    if claim.kind != ClaimKind.P and len(claim.harness_policies) > 1:
        reference = claim.harness_policies[0].controls()
        if any(policy.controls() != reference for policy in claim.harness_policies[1:]):
            invalid.append("harness policy snapshots differ within the frozen claim")
    return insufficient, invalid


def _kinds(claim: CapabilityClaimSpec) -> set[str]:
    return {item.kind for item in claim.evidence}


def _protocol(
    claim: CapabilityClaimSpec,
    raws: dict[str, bytes],
) -> tuple[list[str], list[str]]:
    insufficient, invalid = _common(claim)
    needed = {"workbench_certificate", "surface_oracle", "nop_control"}
    if missing := sorted(needed - _kinds(claim)):
        insufficient.append(f"P evidence missing artifact kinds: {', '.join(missing)}")
    report, curve_insufficient, curve_invalid = _curve(claim, raws)
    prereg, prereg_insufficient, prereg_invalid = _equivalence_preregistration(
        claim, raws
    )
    insufficient.extend(curve_insufficient)
    insufficient.extend(prereg_insufficient)
    invalid.extend(curve_invalid)
    invalid.extend(prereg_invalid)
    if claim.declared_factor is None:
        insufficient.append("protocol or harness treatment is not an explicit factor")
    if len(claim.harness_policies) != 2:
        insufficient.append("P requires exactly two matched harness policy snapshots")
    elif claim.declared_factor:
        left, right = claim.harness_policies
        if left.budgets() != right.budgets():
            invalid.append("hidden retry/step/token/wall budget difference")
        if left.controls(excluding=claim.declared_factor) != right.controls(
            excluding=claim.declared_factor
        ):
            invalid.append("hidden harness policy difference outside the declared factor")
        if getattr(left, f"{claim.declared_factor}_identity") == getattr(
            right, f"{claim.declared_factor}_identity"
        ):
            invalid.append("declared P factor has no distinct treatment identities")

    if claim.preregistered_equivalence_margin is None:
        insufficient.append("declared equivalence margin is unavailable")
    elif (
        prereg is not None
        and claim.preregistered_equivalence_margin != prereg.equivalence_margin
    ):
        invalid.append("declared equivalence margin contradicts preregistration bytes")

    derived_interval: tuple[float, float] | None = None
    if report is not None and prereg is not None:
        if report.primary_contrast.k != prereg.primary_k:
            invalid.append("curve primary contrast k contradicts preregistration bytes")
        primary = [level for level in report.levels if level.role == "primary"]
        if len(primary) != 1:
            invalid.append("curve report must contain exactly one primary paired level")
        else:
            level = primary[0]
            if level.level != report.primary_contrast.level:
                invalid.append("curve primary level contradicts its declared contrast")
            contrasts = [
                contrast
                for contrast in level.contrasts
                if contrast.k == report.primary_contrast.k
            ]
            if len(contrasts) != 1:
                invalid.append(
                    "curve report must contain exactly one primary-k paired contrast"
                )
            elif contrasts[0].paired_interval_95 is None:
                insufficient.append("curve primary paired interval is unavailable")
            else:
                interval = contrasts[0].paired_interval_95
                if len(interval) != 2:
                    invalid.append(
                        "curve primary paired interval must contain exactly two bounds"
                    )
                else:
                    derived_interval = (float(interval[0]), float(interval[1]))

    if claim.equivalence_interval_95 is None:
        insufficient.append("declared paired equivalence interval is unavailable")
    elif (
        derived_interval is not None
        and claim.equivalence_interval_95 != derived_interval
    ):
        invalid.append("declared paired interval contradicts current curve bytes")
    if (
        derived_interval is not None
        and prereg is not None
        and (
            derived_interval[0] < -prereg.equivalence_margin
            or derived_interval[1] > prereg.equivalence_margin
        )
    ):
        insufficient.append(
            "artifact-derived interval is outside the preregistered margin"
        )
    return insufficient, invalid


def _curve(
    claim: CapabilityClaimSpec,
    raws: dict[str, bytes],
) -> tuple[CapabilityCurveReport | None, list[str], list[str]]:
    refs = [ref for ref in claim.evidence if ref.kind == "curve_report"]
    if not refs:
        return None, ["curve_report is unavailable"], []
    if len(refs) != 1:
        return None, [], ["exactly one curve_report is required"]
    raw = raws.get(refs[0].identity)
    if raw is None:
        return None, [], []
    try:
        return CapabilityCurveReport.model_validate_json(raw), [], []
    except ValueError:
        return None, [], ["curve_report does not satisfy the frozen curve contract"]


def _equivalence_preregistration(
    claim: CapabilityClaimSpec,
    raws: dict[str, bytes],
) -> tuple[_EquivalencePreregistration | None, list[str], list[str]]:
    refs = [
        ref for ref in claim.evidence if ref.kind == "equivalence_preregistration"
    ]
    if not refs:
        return None, ["equivalence preregistration is unavailable"], []
    if len(refs) != 1:
        return None, [], ["exactly one equivalence preregistration is required"]
    raw = raws.get(refs[0].identity)
    if raw is None:
        return None, [], []
    try:
        return _EquivalencePreregistration.model_validate_json(raw), [], []
    except ValueError:
        return None, [], ["equivalence preregistration bytes are invalid"]


def _reliability(
    claim: CapabilityClaimSpec,
    raws: dict[str, bytes],
) -> tuple[list[str], list[str]]:
    insufficient, invalid = _common(claim)
    if not claim.frozen_domains:
        insufficient.append("R requires frozen heldout domains")
    if not claim.frozen_environments:
        insufficient.append("R requires frozen heldout environments")
    if missing := sorted({"curve_report", "power_analysis"} - _kinds(claim)):
        insufficient.append(f"R evidence missing artifact kinds: {', '.join(missing)}")
    report, curve_insufficient, curve_invalid = _curve(claim, raws)
    insufficient.extend(curve_insufficient)
    invalid.extend(curve_invalid)
    if report is not None:
        primary_levels = [level for level in report.levels if level.role == "primary"]
        k = report.primary_contrast.k
        if len(primary_levels) != 1:
            invalid.append("curve report must contain exactly one primary paired level")
        else:
            primary = primary_levels[0]
            if (
                not any(metric.k == k for metric in primary.pass_at_k)
                or not any(metric.k == k for metric in primary.pass_power_k)
            ):
                insufficient.append("paired evidence must report both pass@k and pass^k")
        if not report.rankable:
            insufficient.append("curve report is inconclusive or refuses paired inference")
    if claim.inferential_outcome is None:
        insufficient.append("R inferential outcome is unavailable")
    elif claim.inferential_outcome == "inconclusive":
        insufficient.append("CI overlap or non-significance is inconclusive")
    elif claim.inferential_outcome == "contradicts_claim":
        invalid.append("paired reliability evidence contradicts the claim")
    return insufficient, invalid


def _novelty_controls(claim: CapabilityClaimSpec) -> tuple[list[str], list[str]]:
    insufficient, invalid = _common(claim)
    novelty = claim.novelty
    if novelty is None:
        return [*insufficient, "pre-trace novelty certificate is unavailable"], invalid
    if novelty.issued_at >= novelty.first_trace_at:
        invalid.append("novelty certificate must precede the first trace")
    if claim.freeze and novelty.first_trace_at != claim.freeze.first_trace_at:
        invalid.append("novelty and freeze records disagree on first trace")
    if not novelty.heldout_allowed_use:
        invalid.append("task registry does not allow heldout use")
    if not novelty.contamination.basis.strip():
        invalid.append("TaskContamination requires an evidentiary basis")
    if novelty.contamination.in_pretrain != "n":
        invalid.append("unfamiliarity requires TaskContamination.in_pretrain='n'")
    if novelty.reference_prompt_borrowing:
        invalid.append("reference-prompt borrowing is not admissible novelty evidence")
    if not novelty.recoverable_in_world_knowledge:
        invalid.append("required knowledge is not recoverable in-world")
    if novelty.post_trace_contamination:
        invalid.append("post-trace contamination invalidates novelty evidence")
    return insufficient, invalid


def _continual_controls(
    claim: CapabilityClaimSpec,
    raws: dict[str, bytes],
) -> tuple[list[str], list[str]]:
    insufficient, invalid = _common(claim)
    phases = claim.longitudinal_phases
    if len(phases) < 2:
        insufficient.append("C requires at least two frozen longitudinal phases")
    if any(item.kind != "longitudinal_phase" for item in phases):
        invalid.append("C longitudinal phase references have the wrong artifact kind")
    digests = [item.sha256 for item in phases]
    if len(digests) != len(set(digests)):
        invalid.append("C longitudinal phases must bind distinct bytes")

    records: list[_LongitudinalPhaseRecord] = []
    for phase in phases:
        raw = raws.get(phase.identity)
        if raw is None:
            continue
        try:
            records.append(_LongitudinalPhaseRecord.model_validate_json(raw))
        except ValueError:
            invalid.append(
                f"C longitudinal phase bytes are invalid: {phase.path}"
            )
    if len(records) == len(phases):
        identities = [record.phase_identity for record in records]
        if len(identities) != len(set(identities)):
            invalid.append("C longitudinal phase identities must be distinct")
        for index in range(1, len(records)):
            previous = records[index - 1]
            current = records[index]
            if (
                current.phase_index <= previous.phase_index
                or current.observed_at <= previous.observed_at
            ):
                invalid.append(
                    "C longitudinal phases must be strictly ordered by index and timestamp"
                )
                break
    if "longitudinal_report" not in _kinds(claim):
        insufficient.append("C requires longitudinal evidence across frozen phases")
    return insufficient, invalid


def _reasons(
    kind: ClaimKind,
    claim: CapabilityClaimSpec,
    raws: dict[str, bytes],
) -> tuple[list[str], list[str]]:
    if kind == ClaimKind.P:
        return _protocol(claim, raws)
    if kind == ClaimKind.R:
        return _reliability(claim, raws)
    if kind == ClaimKind.U:
        insufficient, invalid = _novelty_controls(claim)
        needed = {"workbench_certificate", "task_registry_record", "adaptation_report"}
        if missing := sorted(needed - _kinds(claim)):
            insufficient.append(f"U evidence missing artifact kinds: {', '.join(missing)}")
        return insufficient, invalid
    if kind == ClaimKind.C:
        return _continual_controls(claim, raws)
    insufficient, invalid = _common(claim)
    if "production_report" not in _kinds(claim):
        insufficient.append("Y requires production reliability evidence")
    if claim.integration_cost is None:
        insufficient.append("Y requires an IntegrationCostLedger")
    elif claim.integration_cost.post_trace_fixes:
        invalid.append("post-trace fixes invalidate the frozen production claim")
    return insufficient, invalid

def _freeze_artifact_reasons(claim: CapabilityClaimSpec,
                             raws: dict[str, bytes]) -> list[str]:
    freeze = claim.freeze
    if freeze is None:
        return []
    raw = raws.get(freeze.artifact.identity)
    if raw is None:
        return []
    try:
        body = json.loads(raw)
        if isinstance(body, dict) and isinstance(body.get("freeze"), dict):
            body = body["freeze"]
        expected = freeze.model_dump(mode="json", exclude={"artifact"})
        if not isinstance(body, dict) or any(
            body.get(key) != value for key, value in expected.items()
        ):
            return [
                "freeze record bytes do not match the declared timestamps and artifact set"
            ]
    except Exception:
        return ["freeze record artifact is not valid JSON"]
    return []


def _harness_artifact_reasons(claim: CapabilityClaimSpec,
                              raws: dict[str, bytes]) -> list[str]:
    errors: list[str] = []
    for policy in claim.harness_policies:
        raw = raws.get(policy.artifact.identity)
        if raw is None:
            continue
        try:
            body = json.loads(raw)
            if isinstance(body, dict) and isinstance(body.get("snapshot"), dict):
                body = body["snapshot"]
            expected = policy.controls()
            if not isinstance(body, dict) or any(
                body.get(key) != value for key, value in expected.items()
            ):
                errors.append(
                    f"harness snapshot differs from current bytes: {policy.artifact.path}"
                )
        except Exception:
            errors.append(
                f"harness snapshot artifact is not valid JSON: {policy.artifact.path}"
            )
    return errors


def _typed_artifact_reasons(claim: CapabilityClaimSpec,
                            raws: dict[str, bytes]) -> list[str]:
    checks: list[tuple[str, BoundArtifactRef, dict[str, Any]]] = []
    if claim.novelty:
        checks.append((
            "novelty certificate",
            claim.novelty.artifact,
            claim.novelty.model_dump(mode="json", exclude={"artifact"}),
        ))
    if claim.integration_cost:
        checks.append((
            "integration ledger",
            claim.integration_cost.artifact,
            claim.integration_cost.model_dump(mode="json", exclude={"artifact"}),
        ))
    errors: list[str] = []
    for label, ref, expected in checks:
        raw = raws.get(ref.identity)
        if raw is None:
            continue
        try:
            body = json.loads(raw)
            nested_key = "novelty" if label.startswith("novelty") else "ledger"
            if isinstance(body, dict) and isinstance(body.get(nested_key), dict):
                body = body[nested_key]
            if not isinstance(body, dict) or any(
                body.get(key) != value for key, value in expected.items()
            ):
                errors.append(f"{label} bytes do not match the declared typed record")
        except Exception:
            errors.append(f"{label} artifact is not valid JSON")
    return errors


def _workbench_packet_reasons(claim: CapabilityClaimSpec, root: Path,
                              raws: dict[str, bytes]) -> list[str]:
    errors: list[str] = []
    for ref in (item for item in claim.evidence if item.kind == "workbench_certificate"):
        raw = raws.get(ref.identity)
        if raw is None:
            continue
        try:
            body = json.loads(raw)
            binding = body["task_binding"]
            certification_envelope_from_packet(
                root,
                ref.path,
                task_id=binding["task_id"],
                task_version=binding["task_version"],
                task_path=binding["task_path"],
                package_digest=binding["package_digest"],
            )
        except Exception:
            errors.append(
                f"workbench_certificate is not a current M049 packet: {ref.path}"
            )
    return errors


def _novelty_registry_reasons(claim: CapabilityClaimSpec, root: Path,
                              raws: dict[str, bytes]) -> list[str]:
    novelty = claim.novelty
    if novelty is None:
        return []
    raw = raws.get(novelty.registry_record.identity)
    if raw is None:
        return []
    try:
        record = TaskRegistryRecord.model_validate_json(raw)
    except Exception:
        return ["novelty registry bytes do not satisfy TaskRegistryRecord"]
    errors: list[str] = []
    if novelty.task_identity != f"{record.task_id}@{record.version}":
        errors.append("novelty task identity does not match current registry bytes")
    if record.state != "registered":
        errors.append("novelty task is not registered")
    if "heldout" not in record.allowed_uses or not novelty.heldout_allowed_use:
        errors.append("current registry bytes do not allow heldout use")
    if record.contamination != novelty.contamination:
        errors.append("novelty contamination assertion differs from current registry bytes")
    if record.certification.state != "bound":
        errors.append("novelty task lacks a byte-bound M049 certificate")
    else:
        packet_refs = {
            item.path for item in claim.evidence if item.kind == "workbench_certificate"
        }
        if record.certification.packet_path not in packet_refs:
            errors.append("novelty registry certificate is not among claim evidence")
    if not errors:
        try:
            verify_control_evidence(root, record)
            verify_certification_packet(root, record)
        except Exception:
            errors.append("current registry control or certificate evidence failed verification")
    return errors



def _result_evidence(claim: CapabilityClaimSpec) -> list[BoundArtifactRef]:
    """Exclude model_copy-forged refs that cannot cross the strict report boundary."""
    valid: list[BoundArtifactRef] = []
    for ref in claim.evidence:
        try:
            valid.append(BoundArtifactRef.model_validate(ref.model_dump(mode="python")))
        except ValidationError:
            continue
    return valid


def evaluate_capability_contract(
    spec: CapabilityContractSpec,
    *,
    repo_root: Path,
) -> CapabilityContractReport:
    """Re-read current evidence and evaluate every claim independently."""
    if repo_root.is_symlink():
        raise ValueError("repository root cannot be a symlink")
    root = repo_root.resolve(strict=True)
    by_kind = {claim.kind: claim for claim in spec.claims}
    results: list[CapabilityClaimResult] = []
    observed: dict[str, str] = {}
    for kind in ClaimKind:
        claim = by_kind.get(kind)
        if claim is None:
            results.append(CapabilityClaimResult(kind=kind, status="unavailable",
                reasons=[f"{kind.value} claim was not supplied"], evidence=[]))
            continue
        if claim.availability == "unavailable":
            results.append(CapabilityClaimResult(kind=kind, status="unavailable",
                reasons=[*claim.limitations, f"{kind.value} evidence is explicitly unavailable"],
                evidence=[]))
            continue
        raws, binding_errors = _verify_refs(claim, root, observed)
        insufficient, invalid = _reasons(kind, claim, raws)
        invalid.extend(binding_errors)
        invalid.extend(_freeze_artifact_reasons(claim, raws))
        invalid.extend(_harness_artifact_reasons(claim, raws))
        invalid.extend(_typed_artifact_reasons(claim, raws))
        invalid.extend(_workbench_packet_reasons(claim, root, raws))
        invalid.extend(_novelty_registry_reasons(claim, root, raws))
        if claim.novelty:
            identity = claim.novelty.task_identity
            if identity in spec.authoring_identity_inputs:
                invalid.append("heldout identity entered authoring inputs")
            if identity in spec.tuning_identity_inputs:
                invalid.append("heldout identity entered tuning inputs")
        status: Literal["satisfied", "insufficient", "invalid", "unavailable"] = (
            "invalid" if invalid else "insufficient" if insufficient else "satisfied")
        results.append(CapabilityClaimResult(
            kind=kind,
            status=status,
            reasons=[*invalid, *insufficient, *claim.limitations],
            evidence=_result_evidence(claim),
        ))
    states = {item.status for item in results}
    global_status: Literal["valid_insufficient", "invalid", "eligible_for_analysis"] = (
        "invalid" if "invalid" in states else "eligible_for_analysis"
        if states == {"satisfied"} else "valid_insufficient")
    payload = {"spec": spec.model_dump(mode="json"), "observed": dict(sorted(observed.items())),
               "results": [item.model_dump(mode="json") for item in results]}
    return CapabilityContractReport(experiment_id=spec.experiment_id, status=global_status,
        refuse_substantive_generality=global_status != "eligible_for_analysis",
        claims=results, evidence_digest=_digest(_canonical(payload)))
