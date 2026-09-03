"""Track C fixture-only curriculum-candidate descriptors.

Repair grafts (p7 BLOCK repair + wH:p1 coordinated-cutover, 2026-09-03):
executed leak-scan RESULTS recorded per candidate with a leak_scan_failed
typed refusal, and explicit trace_acknowledgment (provided vs neutral_default)
so missing TRACE priorities are never silently NA. All other contract
surface — trusted_parent_outputs, receipt rehydration, live B re-verification,
contrast-pair handoff, scope literals, semantic digests — is unchanged from
the integrated variant to preserve Track H consumers.

This module is deliberately a semantic, offline contract.  It neither
materializes tasks nor provides registration, CAS, dispatch, or training paths.
"""

from __future__ import annotations

import hashlib
import math
import re as _re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator

from evallab.artifact_authority import ArtifactAuthority
from evallab.benchmark_program_contracts import canonical_json, compute_sha256
from evallab.interpretation.capability_deficits import (
    CapabilityDeficitArtifact,
    CapabilityDeficitArtifactReceipt,
    CapabilityDeficitOutputExpectation,
    Counterevidence,
    DeficitEvidence,
    DeficitFamily,
    ProposedInterventionDimension,
    TraceCapabilityMeasures,
    reverify_capability_deficit_artifact,
)
from evallab.schemas import ContractModel, Digest

SCHEMA_VERSION_VALUE = "curriculum-candidate-artifact/v1"
SCHEMA_VERSION = Literal["curriculum-candidate-artifact/v1"]
GENERATOR_ID_VALUE = "curriculum-candidate-synthesizer"
GENERATOR_ID = Literal["curriculum-candidate-synthesizer"]
GENERATOR_VERSION_VALUE = "1"
GENERATOR_VERSION = Literal["1"]
ALGORITHM_VERSION_VALUE = "curriculum-candidates/v1"
ALGORITHM_VERSION = Literal["curriculum-candidates/v1"]
DOMAIN = b"evallab.curriculum-candidates.v1\x00"

TransformId = Literal["funcdag_cross_source_conflict", "funcdag_addressing_permutation"]
CandidateArm = Literal["base", "variant"]
RefusalCode = Literal[
    "invalid_parent_artifact",
    "duplicate_parent_artifact",
    "uncertified_attribution_gate",
    "parent_authority_unverified",
    "prohibited_benchmark_family",
    "held_out_nonleakage",
    "calibration_nonleakage",
    "leak_scan_failed",
    "no_transform_for_family",
    "budget_exceeded",
]

TRANSFORM_ELIGIBILITY: dict[TransformId, tuple[tuple[DeficitFamily, ...], tuple[ProposedInterventionDimension, ...]]] = {
    "funcdag_cross_source_conflict": (
        ("wrong-binding-or-addressing", "wrong-graph-traversal"),
        ("retrieval_addressing", "graph_traversal_policy"),
    ),
    "funcdag_addressing_permutation": (
        ("complete-but-reordered", "wrong-binding-or-addressing"),
        ("instruction_ordering_scaffold", "retrieval_addressing"),
    ),
}
PROHIBITED_BENCHMARK_FAMILIES = frozenset({"syn-funcdag-easy"})


def _domain_digest(label: str, value: Any) -> str:
    """Hash a canonical semantic payload using the repository digest spine."""
    payload = {"label": label, "value": value}
    return "sha256:" + compute_sha256(DOMAIN + canonical_json(payload).encode("utf-8"))


def _generator_implementation_digest() -> str:
    """Bind descriptors to the actual checked-in generator implementation."""
    return "sha256:" + compute_sha256(Path(__file__).read_bytes())


def _drbg_stream(seed_material: bytes, count: int) -> list[int]:
    values: list[int] = []
    counter = 0
    while len(values) < count:
        block = hashlib.sha256(DOMAIN + seed_material + counter.to_bytes(4, "big")).digest()
        values.extend(int.from_bytes(block[index : index + 8], "big") for index in range(0, 32, 8))
        counter += 1
    return values[:count]


def _validated_trace(trace: TraceCapabilityMeasures) -> TraceCapabilityMeasures:
    """Reject impossible TRACE states while retaining Track B's exact type."""
    for name, measure in (
        ("cov", trace.cov),
        ("er_minus", trace.er_minus),
        ("er_plus", trace.er_plus),
        ("delta", trace.delta),
    ):
        if measure.status == "PRESENT":
            lower, upper = (-1.0, 1.0) if name == "delta" else (0.0, 1.0)
            if measure.value is None or not math.isfinite(measure.value) or not lower <= measure.value <= upper:
                raise ValueError(f"PRESENT TRACE {name} requires a finite value in [{lower}, {upper}]")
            if measure.denominator is None or measure.denominator <= 0:
                raise ValueError("PRESENT TRACE measures require a positive denominator")
            if measure.numerator is None or measure.numerator > measure.denominator:
                raise ValueError("PRESENT TRACE numerator must be within denominator")
        elif measure.value is not None or measure.denominator is not None or measure.numerator is not None:
            raise ValueError("NA/LACKING TRACE measures must not carry numeric values")
    return trace

_FORBIDDEN_SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    ("no_hex_secrets", r"\b[0-9a-f]{64}\b"),
    ("no_digest_markers", r"sha256:"),
    ("no_answer_markers", r"\banswer\b|\bsolution\b"),
    ("no_verifier_keywords", r"\bverifier\b|\bexpected_answer\b"),
)


class LeakScanResult(ContractModel):
    """Executed deterministic leak-scan outcome for one candidate spec."""

    check: str = Field(min_length=1)
    result: Literal["pass", "fail"]


def _leak_scan_results(spec_payload: dict[str, Any]) -> tuple[LeakScanResult, ...]:
    """Execute leak scans over the serialized spec payload (repair graft F-8)."""
    text = canonical_json(spec_payload)
    results = []
    for name, pattern in _FORBIDDEN_SECRET_PATTERNS:
        hit = _re.search(pattern, text, _re.IGNORECASE) is not None
        results.append(LeakScanResult(check=name, result="fail" if hit else "pass"))
    return tuple(results)



class CrossSourceConflictSpec(ContractModel):
    entity_count: int = Field(ge=1, le=16)
    source_count: int = Field(ge=2, le=8)
    authoritative_source_index: int = Field(ge=0)
    conflict_axis: Literal["value", "unit", "currency", "timestamp"]
    distractor_fields: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _source_index_exists(self) -> CrossSourceConflictSpec:
        if self.authoritative_source_index >= self.source_count:
            raise ValueError("authoritative source index must be within source_count")
        return self


class AddressingPermutationSpec(ContractModel):
    address_axes: tuple[str, ...] = Field(min_length=2)
    permutation: tuple[int, ...] = Field(min_length=2)
    distractor_density: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _valid_permutation(self) -> AddressingPermutationSpec:
        if len(self.address_axes) != len(self.permutation) or sorted(self.permutation) != list(range(len(self.address_axes))):
            raise ValueError("permutation must reorder every address axis exactly once")
        return self

    @property
    def permutation_identity(self) -> bool:
        return self.permutation == tuple(range(len(self.address_axes)))


class TwinBinding(ContractModel):
    twin_pair_id: Digest
    twin_id: Digest
    arm: CandidateArm
    one_variable_delta: Literal["authoritative_source_index", "permutation"]


class CandidateProvenance(ContractModel):
    parent_deficit_digest: Digest
    parent_artifact: CapabilityDeficitArtifact
    parent_artifact_authority: ArtifactAuthority
    parent_output_expectation: CapabilityDeficitOutputExpectation
    parent_evidence: tuple[DeficitEvidence, ...]
    parent_counterevidence: tuple[Counterevidence, ...]
    parent_authority_not_transferred: Literal[True] = True

    @model_validator(mode="after")
    def _exact_parent_lineage(self) -> CandidateProvenance:
        if self.parent_deficit_digest != self.parent_artifact.content_digest:
            raise ValueError("parent deficit digest must equal the rehydrated Track B artifact digest")
        if self.parent_output_expectation.artifact_content_digest != self.parent_artifact.content_digest:
            raise ValueError("parent output expectation must match the Track B artifact digest")
        if self.parent_output_expectation.artifact_bytes_digest != self.parent_artifact_authority.artifact.digest:
            raise ValueError("parent output expectation must match the Track B authority bytes digest")
        if self.parent_output_expectation.anchor != self.parent_artifact_authority.anchor:
            raise ValueError("parent output expectation must match the Track B authority anchor")
        if self.parent_evidence != self.parent_artifact.evidence:
            raise ValueError("candidate must preserve exact parent evidence lineage")
        if self.parent_counterevidence != self.parent_artifact.counterevidence:
            raise ValueError("candidate must preserve exact parent counterevidence lineage")
        CapabilityDeficitArtifactReceipt(
            artifact=self.parent_artifact,
            artifact_authority=self.parent_artifact_authority,
        )
        return self


class NonleakageBinding(ContractModel):
    parent_split: Literal["train", "unassigned"]
    candidate_split: Literal["train", "unassigned"]
    cluster_key: str = Field(min_length=1)
    policy: Literal["cluster_key_fail_closed"] = "cluster_key_fail_closed"

    @model_validator(mode="after")
    def _pinned_split(self) -> NonleakageBinding:
        if self.candidate_split != self.parent_split:
            raise ValueError("candidate split must be pinned to the parent split")
        return self

class ValidationPlan(ContractModel):
    hidden_verifier_plan: tuple[str, ...] = Field(min_length=1)
    leak_scan: tuple[str, ...] = Field(min_length=1)
    solvability_controls: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _required_controls(self) -> ValidationPlan:
        required = {"oracle_pass_x3", "nop_fail", "plausible_mutant_rejection"}
        if not required.issubset(self.solvability_controls):
            raise ValueError("validation plan omits required solvability controls")
        return self


_PLANS: dict[TransformId, ValidationPlan] = {
    "funcdag_cross_source_conflict": ValidationPlan(
        hidden_verifier_plan=("authoritative_source_resolution_checked", "wrong_source_selection_fails"),
        leak_scan=("no_verifier_secrets_in_spec", "answer_values_absent_from_instructions"),
        solvability_controls=("oracle_pass_x3", "nop_fail", "plausible_mutant_rejection"),
    ),
    "funcdag_addressing_permutation": ValidationPlan(
        hidden_verifier_plan=("addressed_entity_exact_match", "wrong_address_fails"),
        leak_scan=("no_verifier_secrets_in_spec", "permutation_leak_absent"),
        solvability_controls=("oracle_pass_x3", "nop_fail", "plausible_mutant_rejection"),
    ),
}


class SyntheticTaskCandidate(ContractModel):
    schema_version: Literal["curriculum-candidate/v1"] = "curriculum-candidate/v1"
    descriptor_only: Literal[True] = True
    fixture_only: Literal[True] = True
    status: Literal["quarantined"] = "quarantined"
    training_eligible: Literal[False] = False
    authority_scope: Literal["priority_only_never_general"] = "priority_only_never_general"
    candidate_id: Digest
    generator_id: GENERATOR_ID
    generator_version: GENERATOR_VERSION
    generator_implementation_digest: Digest
    algorithm_version: ALGORITHM_VERSION
    transform_id: TransformId
    expected_capability: ProposedInterventionDimension
    deficit_family: DeficitFamily
    seed: int = Field(ge=0)
    spec: CrossSourceConflictSpec | AddressingPermutationSpec
    twin: TwinBinding
    nonleakage: NonleakageBinding
    validation_plan: ValidationPlan
    provenance: CandidateProvenance
    rank: int = Field(ge=1)
    rank_basis: Literal["priority_only_non_causal"] = "priority_only_non_causal"
    trace_priority: TraceCapabilityMeasures
    leak_scan_results: tuple[LeakScanResult, ...] = Field(min_length=1)
    trace_acknowledgment: Literal["provided", "neutral_default"]

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"candidate_id"})

    @model_validator(mode="after")
    def _semantic_identity(self) -> SyntheticTaskCandidate:
        _validated_trace(self.trace_priority)
        parent = self.provenance.parent_artifact
        eligible_families, eligible_capabilities = TRANSFORM_ELIGIBILITY[self.transform_id]
        if self.deficit_family != parent.family:
            raise ValueError("candidate deficit family must match the Track B parent")
        if self.deficit_family not in eligible_families:
            raise ValueError("parent deficit family is not eligible for this transform")
        if self.expected_capability not in eligible_capabilities:
            raise ValueError("expected capability is not eligible for this transform")
        if self.expected_capability not in parent.proposed_intervention_dimensions:
            raise ValueError("expected capability must be proposed by the Track B parent")
        if self.transform_id == "funcdag_cross_source_conflict":
            if type(self.spec) is not CrossSourceConflictSpec:
                raise ValueError("transform requires a CrossSourceConflictSpec")
            expected_delta = "authoritative_source_index"
        else:
            if type(self.spec) is not AddressingPermutationSpec:
                raise ValueError("transform requires an AddressingPermutationSpec")
            expected_delta = "permutation"
        if self.twin.one_variable_delta != expected_delta:
            raise ValueError("twin delta must match its transform")
        if self.validation_plan != _PLANS[self.transform_id]:
            raise ValueError("validation plan must match the checked-in transform plan")
        if self.trace_priority.family != self.deficit_family:
            raise ValueError("TRACE family must match the parent deficit family")
        if self.nonleakage.parent_split != parent.source_binding.split:
            raise ValueError("nonleakage parent split must match the Track B parent")
        if self.nonleakage.cluster_key != parent.source_binding.cluster_key:
            raise ValueError("nonleakage cluster key must match the Track B parent")
        if self.generator_implementation_digest != _generator_implementation_digest():
            raise ValueError("candidate is not bound to this generator implementation")
        expected_pair = _domain_digest(
            "twin-pair",
            {
                "parent_deficit_digest": self.provenance.parent_deficit_digest,
                "transform_id": self.transform_id,
                "seed": self.seed,
            },
        )
        if self.twin.twin_pair_id != expected_pair:
            raise ValueError("twin-pair identifier does not match its semantic payload")
        expected_twin = _domain_digest(
            "twin",
            {
                "twin_pair_id": self.twin.twin_pair_id,
                "arm": self.twin.arm,
                "one_variable_delta": self.twin.one_variable_delta,
                "spec": self.spec.model_dump(mode="json"),
            },
        )
        if self.twin.twin_id != expected_twin:
            raise ValueError("twin identifier does not match its semantic payload")
        if self.candidate_id != _domain_digest("candidate", self.semantic_payload()):
            raise ValueError("candidate identifier does not match its semantic payload")
        if any(item.result == "fail" for item in self.leak_scan_results):
            raise ValueError("candidate spec failed its executed leak scan")
        return self


class ContrastPair(ContractModel):
    contrast_pair_id: Digest
    twin_pair_id: Digest
    candidate_ids: tuple[Digest, Digest]
    one_variable_delta: Literal["authoritative_source_index", "permutation"]

    @model_validator(mode="after")
    def _semantic_identity(self) -> ContrastPair:
        payload = {
            "twin_pair_id": self.twin_pair_id,
            "candidate_ids": list(self.candidate_ids),
            "one_variable_delta": self.one_variable_delta,
        }
        if self.contrast_pair_id != _domain_digest("contrast-pair", payload):
            raise ValueError("contrast pair identifier does not match its semantic payload")
        return self


class DeficitRefusal(ContractModel):
    reason_code: RefusalCode
    input_digest: Digest
    detail: str = Field(min_length=1, max_length=512)
    rank: int | None = Field(default=None, ge=1)


class SynthesisResult(ContractModel):
    schema_version: SCHEMA_VERSION = SCHEMA_VERSION_VALUE
    descriptor_only: Literal[True] = True
    fixture_only: Literal[True] = True
    status: Literal["quarantined"] = "quarantined"
    training_eligible: Literal[False] = False
    authority_scope: Literal["priority_only_never_general"] = "priority_only_never_general"
    generator_implementation_digest: Digest
    candidates: tuple[SyntheticTaskCandidate, ...] = ()
    contrast_pairs: tuple[ContrastPair, ...] = ()
    refusals: tuple[DeficitRefusal, ...] = ()
    content_digest: Digest

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"content_digest"})

    @model_validator(mode="after")
    def _semantic_graph(self) -> SynthesisResult:
        current_implementation = _generator_implementation_digest()
        if self.generator_implementation_digest != current_implementation:
            raise ValueError("curriculum artifact is not bound to this generator implementation")
        if any(candidate.generator_implementation_digest != self.generator_implementation_digest for candidate in self.candidates):
            raise ValueError("candidate implementation digests must match the curriculum artifact")
        candidate_by_id = {candidate.candidate_id: candidate for candidate in self.candidates}
        if len(candidate_by_id) != len(self.candidates):
            raise ValueError("candidate references must be unique")
        if len(self.contrast_pairs) * 2 != len(self.candidates):
            raise ValueError("every candidate must belong to exactly one contrast pair")
        referenced: set[str] = set()
        pair_ids: set[str] = set()
        pair_ranks: list[int] = []
        canonical_candidate_ids: list[str] = []
        for pair in self.contrast_pairs:
            if pair.twin_pair_id in pair_ids:
                raise ValueError("twin-pair references must be unique")
            pair_ids.add(pair.twin_pair_id)
            if pair.candidate_ids[0] == pair.candidate_ids[1] or any(
                identifier not in candidate_by_id for identifier in pair.candidate_ids
            ):
                raise ValueError("contrast pair references must resolve uniquely")
            if any(identifier in referenced for identifier in pair.candidate_ids):
                raise ValueError("candidate belongs to more than one contrast pair")
            referenced.update(pair.candidate_ids)
            base, variant = (candidate_by_id[identifier] for identifier in pair.candidate_ids)
            if base.twin.arm != "base" or variant.twin.arm != "variant":
                raise ValueError("contrast pair candidate_ids must be ordered base then variant")
            if base.twin.twin_pair_id != pair.twin_pair_id or variant.twin.twin_pair_id != pair.twin_pair_id:
                raise ValueError("contrast pair twin reference mismatch")
            if base.twin.one_variable_delta != pair.one_variable_delta or variant.twin.one_variable_delta != pair.one_variable_delta:
                raise ValueError("contrast pair delta reference mismatch")
            if base.provenance != variant.provenance:
                raise ValueError("contrast pair candidates must preserve identical parent provenance")
            base_spec = base.spec.model_dump(mode="json")
            variant_spec = variant.spec.model_dump(mode="json")
            differences = {key for key in base_spec if base_spec[key] != variant_spec[key]}
            if differences != {pair.one_variable_delta}:
                raise ValueError("contrast pair must differ in exactly its declared variable")
            pair_ranks.append(base.rank)
            canonical_candidate_ids.extend(pair.candidate_ids)
        if referenced != set(candidate_by_id):
            raise ValueError("every candidate must resolve through one contrast pair")
        if pair_ranks != sorted(pair_ranks) or len(set(pair_ranks)) != len(pair_ranks):
            raise ValueError("contrast pairs must be in canonical unique rank order")
        if tuple(canonical_candidate_ids) != tuple(candidate.candidate_id for candidate in self.candidates):
            raise ValueError("candidates must use canonical contrast-pair ordering")
        if self.content_digest != _domain_digest("artifact", self.semantic_payload()):
            raise ValueError("curriculum artifact digest does not match its semantic payload")
        return self

def _input_digest(value: object) -> str:
    if isinstance(value, CapabilityDeficitArtifactReceipt):
        raw: Any = value.model_dump(mode="json")
    elif isinstance(value, Mapping):
        raw = dict(value)
    else:
        raw = {"malformed_input_type": f"{type(value).__module__}.{type(value).__qualname__}"}
    return _domain_digest("input", raw)


def _rehydrate_receipt(
    value: Mapping[str, Any] | CapabilityDeficitArtifactReceipt,
) -> CapabilityDeficitArtifactReceipt:
    if isinstance(value, CapabilityDeficitArtifactReceipt):
        raw: Mapping[str, Any] = value.model_dump(mode="json")
    elif isinstance(value, Mapping):
        raw = dict(value)
    else:
        raise ValueError("Track B input must be a capability-deficit artifact receipt")
    return CapabilityDeficitArtifactReceipt.model_validate(raw)


def _rehydrate_output_expectation(
    value: CapabilityDeficitOutputExpectation | Mapping[str, Any],
) -> CapabilityDeficitOutputExpectation:
    if isinstance(value, CapabilityDeficitOutputExpectation):
        raw: Mapping[str, Any] = value.model_dump(mode="json")
    elif isinstance(value, Mapping):
        raw = dict(value)
    else:
        raise ValueError("trusted parent output must be an output expectation")
    return CapabilityDeficitOutputExpectation.model_validate(raw)


def rehydrate_curriculum_artifact(value: Mapping[str, Any] | SynthesisResult) -> SynthesisResult:
    """Parse and recompute every semantic identifier in an offline descriptor."""
    raw = value.model_dump(mode="json") if isinstance(value, SynthesisResult) else dict(value)
    return SynthesisResult.model_validate(raw)


def _specs(transform_id: TransformId, stream: list[int]) -> tuple[CrossSourceConflictSpec | AddressingPermutationSpec, CrossSourceConflictSpec | AddressingPermutationSpec]:
    if transform_id == "funcdag_cross_source_conflict":
        source_count = 2 + stream[0] % 3
        base = stream[1] % source_count
        variant = (base + 1 + stream[2] % (source_count - 1)) % source_count
        common = {
            "entity_count": 1 + stream[3] % 4,
            "source_count": source_count,
            "conflict_axis": ("value", "unit", "currency", "timestamp")[stream[4] % 4],
            "distractor_fields": ("region",) if stream[5] % 2 else (),
        }
        return (CrossSourceConflictSpec(authoritative_source_index=base, **common), CrossSourceConflictSpec(authoritative_source_index=variant, **common))
    count = 2 + stream[0] % 2
    axes = ("primary_key", "secondary_key", "tertiary_key")[:count]
    identity = tuple(range(count))
    variant = list(identity)
    left, right = stream[1] % count, (stream[1] + 1 + stream[2] % (count - 1)) % count
    variant[left], variant[right] = variant[right], variant[left]
    common = {"address_axes": axes, "distractor_density": round((stream[3] % 40) / 100, 2)}
    return (AddressingPermutationSpec(permutation=identity, **common), AddressingPermutationSpec(permutation=tuple(variant), **common))


def _candidate(receipt: CapabilityDeficitArtifactReceipt, expected_output: CapabilityDeficitOutputExpectation, transform_id: TransformId, expected: ProposedInterventionDimension, seed: int, rank: int, arm: CandidateArm, spec: CrossSourceConflictSpec | AddressingPermutationSpec, pair_id: str, delta: Literal["authoritative_source_index", "permutation"], trace: TraceCapabilityMeasures, trace_provided: bool) -> SyntheticTaskCandidate:
    parent = receipt.artifact
    twin_id = _domain_digest("twin", {"twin_pair_id": pair_id, "arm": arm, "one_variable_delta": delta, "spec": spec.model_dump(mode="json")})
    body: dict[str, Any] = {
        "candidate_id": "sha256:" + "0" * 64,
        "generator_id": GENERATOR_ID_VALUE,
        "generator_version": GENERATOR_VERSION_VALUE,
        "generator_implementation_digest": _generator_implementation_digest(),
        "algorithm_version": ALGORITHM_VERSION_VALUE,
        "transform_id": transform_id,
        "expected_capability": expected,
        "deficit_family": parent.family,
        "seed": seed,
        "spec": spec.model_dump(mode="json"),
        "twin": {"twin_pair_id": pair_id, "twin_id": twin_id, "arm": arm, "one_variable_delta": delta},
        "nonleakage": {"parent_split": parent.source_binding.split, "candidate_split": parent.source_binding.split, "cluster_key": parent.source_binding.cluster_key, "policy": "cluster_key_fail_closed"},
        "validation_plan": _PLANS[transform_id].model_dump(mode="json"),
        "provenance": {"parent_deficit_digest": parent.content_digest, "parent_artifact": parent.model_dump(mode="json"), "parent_artifact_authority": receipt.artifact_authority.model_dump(mode="json"), "parent_output_expectation": expected_output.model_dump(mode="json"), "parent_evidence": [item.model_dump(mode="json") for item in parent.evidence], "parent_counterevidence": [item.model_dump(mode="json") for item in parent.counterevidence], "parent_authority_not_transferred": True},
        "rank": rank,
        "rank_basis": "priority_only_non_causal",
        "trace_priority": trace.model_dump(mode="json"),
        "leak_scan_results": [item.model_dump(mode="json") for item in _leak_scan_results(spec.model_dump(mode="json"))],
        "trace_acknowledgment": "provided" if trace_provided else "neutral_default",
    }
    body["candidate_id"] = _domain_digest("candidate", {key: value for key, value in body.items() if key != "candidate_id"} | {"schema_version": "curriculum-candidate/v1", "descriptor_only": True, "fixture_only": True, "status": "quarantined", "training_eligible": False, "authority_scope": "priority_only_never_general"})
    return SyntheticTaskCandidate.model_validate(body)


def _refusal(reason_code: RefusalCode, value: object, detail: str, rank: int | None = None) -> DeficitRefusal:
    return DeficitRefusal(reason_code=reason_code, input_digest=_input_digest(value), detail=detail, rank=rank)


def synthesize_curriculum_candidates(
    deficit_artifacts: Sequence[Mapping[str, Any] | CapabilityDeficitArtifactReceipt],
    trace_priorities: Mapping[str, TraceCapabilityMeasures] | None = None,
    *,
    trusted_parent_outputs: Mapping[
        Digest, CapabilityDeficitOutputExpectation | Mapping[str, Any]
    ],
    seed: int = 0,
    budget: int | None = None,
    authority_repo_root: Path | str | None = None,
    authority_store_root: Path | str | None = None,
) -> SynthesisResult:
    """Produce deterministic, fixture-only priority descriptors from Track B receipts."""
    if budget is not None and budget < 0:
        raise ValueError("budget must be non-negative")
    trace_priorities = trace_priorities or {}
    eligible: list[tuple[CapabilityDeficitArtifactReceipt, CapabilityDeficitOutputExpectation, TraceCapabilityMeasures, TransformId]] = []
    refusals: list[DeficitRefusal] = []
    seen_parent_digests: set[str] = set()
    for value in deficit_artifacts:
        try:
            receipt = _rehydrate_receipt(value)
        except (TypeError, ValidationError, ValueError):
            refusals.append(_refusal("invalid_parent_artifact", value, "Track B artifact receipt failed typed rehydration"))
            continue
        parent = receipt.artifact
        if parent.content_digest in seen_parent_digests:
            refusals.append(_refusal("duplicate_parent_artifact", value, "duplicate Track B parent artifact"))
            continue
        seen_parent_digests.add(parent.content_digest)
        if parent.attribution_gate != "deficit_supported":
            refusals.append(_refusal("uncertified_attribution_gate", value, "only deficit_supported Track B artifacts can seed descriptors"))
            continue
        try:
            expected_output = _rehydrate_output_expectation(
                trusted_parent_outputs[parent.content_digest]
            )
            if expected_output.artifact_content_digest != parent.content_digest:
                raise ValueError("trusted parent output key mismatch")
        except (KeyError, TypeError, ValidationError, ValueError):
            refusals.append(
                _refusal(
                    "parent_authority_unverified",
                    value,
                    "trusted Track B parent output expectation is missing or invalid",
                )
            )
            continue
        if not reverify_capability_deficit_artifact(
            receipt,
            expected_output=expected_output,
            authority_repo_root=authority_repo_root,
            authority_store_root=authority_store_root,
        ):
            refusals.append(
                _refusal(
                    "parent_authority_unverified",
                    value,
                    "Track B parent failed live authority re-verification",
                )
            )
            continue
        if parent.source_binding.benchmark_family in PROHIBITED_BENCHMARK_FAMILIES:
            refusals.append(_refusal("prohibited_benchmark_family", value, "prohibited benchmark family"))
            continue
        if parent.source_binding.split == "held_out":
            refusals.append(_refusal("held_out_nonleakage", value, "held_out evidence cannot seed a descriptor"))
            continue
        if parent.source_binding.split == "calibration":
            refusals.append(_refusal("calibration_nonleakage", value, "calibration evidence cannot seed a descriptor"))
            continue
        provided = parent.content_digest in trace_priorities
        trace = trace_priorities.get(parent.content_digest)
        if trace is None:
            trace = TraceCapabilityMeasures.model_validate({"family": parent.family, "cov": {"status": "NA"}, "er_minus": {"status": "NA"}, "er_plus": {"status": "NA"}, "delta": {"status": "NA"}})
        _validated_trace(trace)
        matched = False
        for transform_id, (families, dimensions) in sorted(TRANSFORM_ELIGIBILITY.items()):
            if parent.family in families and (set(dimensions) & set(parent.proposed_intervention_dimensions)):
                eligible.append((receipt, expected_output, trace, transform_id, provided))
                matched = True
        if not matched:
            refusals.append(_refusal("no_transform_for_family", value, "no eligible fixture-only transform for Track B family"))
    eligible.sort(key=lambda item: (-(item[2].er_minus.value or 0.0), -(item[2].delta.value or 0.0), item[0].artifact.content_digest, item[3]))
    candidates: list[SyntheticTaskCandidate] = []
    pairs: list[ContrastPair] = []
    for rank, (receipt, expected_output, trace, transform_id, provided) in enumerate(eligible, start=1):
        parent = receipt.artifact
        if budget is not None and len(pairs) >= budget:
            refusals.append(_refusal("budget_exceeded", receipt, f"rank={rank} cut by budget={budget}", rank))
            continue
        dimensions = TRANSFORM_ELIGIBILITY[transform_id][1]
        expected = next(dimension for dimension in dimensions if dimension in parent.proposed_intervention_dimensions)
        pair_id = _domain_digest("twin-pair", {"parent_deficit_digest": parent.content_digest, "transform_id": transform_id, "seed": seed})
        delta: Literal["authoritative_source_index", "permutation"] = "authoritative_source_index" if transform_id == "funcdag_cross_source_conflict" else "permutation"
        stream = _drbg_stream(parent.content_digest.encode("utf-8") + b"\x00" + transform_id.encode("utf-8") + b"\x00" + str(seed).encode("ascii"), 8)
        base_spec, variant_spec = _specs(transform_id, stream)
        try:
            base = _candidate(receipt, expected_output, transform_id, expected, seed, rank, "base", base_spec, pair_id, delta, trace, provided)
            variant = _candidate(receipt, expected_output, transform_id, expected, seed, rank, "variant", variant_spec, pair_id, delta, trace, provided)
        except ValueError as exc:
            refusals.append(_refusal("leak_scan_failed", receipt, str(exc)[:512], rank))
            continue
        candidate_ids = (base.candidate_id, variant.candidate_id)
        pairs.append(ContrastPair(contrast_pair_id=_domain_digest("contrast-pair", {"twin_pair_id": pair_id, "candidate_ids": list(candidate_ids), "one_variable_delta": delta}), twin_pair_id=pair_id, candidate_ids=candidate_ids, one_variable_delta=delta))
        candidates.extend((base, variant))
    body: dict[str, Any] = {"generator_implementation_digest": _generator_implementation_digest(), "candidates": [candidate.model_dump(mode="json") for candidate in candidates], "contrast_pairs": [pair.model_dump(mode="json") for pair in pairs], "refusals": [refusal.model_dump(mode="json") for refusal in refusals]}
    body["content_digest"] = _domain_digest("artifact", body | {"schema_version": SCHEMA_VERSION_VALUE, "descriptor_only": True, "fixture_only": True, "status": "quarantined", "training_eligible": False, "authority_scope": "priority_only_never_general"})
    return SynthesisResult.model_validate(body)
