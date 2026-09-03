"""Track B: deterministic capability-deficit mining from trial evidence.

Converts verifier/trajectory evidence for ONE trial into a versioned, digest-bound
:class:`CapabilityDeficitArtifact` describing *what the trial's outcome can and cannot
support* about an agent capability deficit.

Gate Zero constraints (orchestrator, 2026-09-03): this module never reads the
legacy corpus or classifies path-derived legacy authority. Positive attribution
re-verifies each cited source through the public artifact-authority boundary;
source paths remain provenance labels, never classification inputs.

Design constraints (overnight brief, track B; repair round per wH:p9/wH:p0 review
and wK:p7 adversarial findings 1-3):
- Strict typed input :class:`CapabilityDeficitInput` (``extra="forbid"``); unknown
  input keys are construction errors, never ignored; missing required keys are
  typed ValidationError, not KeyError.
- Deterministic and idempotent: same input -> byte-identical artifact.
- Environment non-evaluation is EXPLICIT only (``capture_status == "non_evaluated"``
  or ``tau2_evaluation is False``). A reward of 0 with a missing breakdown and no
  evaluator signal is evaluator-status-unknown (typed hold), never a non-evaluation
  and never a family.
- ``reward >= 1`` without a semantic evaluator signal (reward_breakdown present or
  ``tau2_evaluation is True``) is reward-only: family ``none``, gate
  ``unattributable``, hold ``reward_only_without_semantic_evaluator``. It never
  mints a refutation.
- ``deficit_supported`` and ``deficit_refuted`` require declared capture facts
  *and* a bytes-verified :class:`ArtifactAuthority` for every cited
  evidence/counterevidence source, plus a separately anchored authority for
  canonical ``TrialAdmissibilityV1`` bytes. Each source receipt must bind the
  exact digest, source artifact kind, and trial id, and every authority is
  re-read through :func:`reverify_authority`. Caller assertions, structural
  receipts, stale receipts, and missing or mismatched bindings fail closed to
  ``unattributable``.
  ``environment_integrity == "unknown"`` is absent provenance, not permission.
- Blind retry requires three or more consecutive calls with the same tool and
  canonical arguments, every one an error. A same-tool varied-argument
  all-error sequence is held as unclassified; it is neither blind retry nor
  adaptive success.
- Unknown mechanisms stay ``unclassified``; model prose cannot enter the
  mechanical core (provenance is a closed ``Literal["mechanical"]``); no causal or
  general claim is representable.
- No database, network, model, or subprocess: pure library over typed inputs.

TRACE-style measures (Track F constraint): :func:`trace_capability_measures`
computes Cov / ER- / ER+ / Delta over CERTIFIED artifact facts only, preserving
NA/PRESENT/LACKING statuses and explicit denominators. Delta is a priority signal
and NEVER a causal proof. No TRACE runtime, LLM labeler, MoE, or GRPO component is
implemented or imported.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from evallab.artifact_authority import (
    VERIFIER_IMPLEMENTATION_DIGEST,
    ArtifactAuthority,
    reverify_authority,
)
from evallab.benchmark_program_contracts import canonical_json, compute_sha256
from evallab.schemas import ContractModel, Digest, TrialAdmissibilityV1

SCHEMA_VERSION = Literal["capability-deficit-artifact/v1"]
SCHEMA_VERSION_VALUE: str = "capability-deficit-artifact/v1"
EXTRACTOR_ID = Literal["capability-deficit-miner"]
EXTRACTOR_ID_VALUE: str = "capability-deficit-miner"
EXTRACTOR_VERSION = Literal["1"]
EXTRACTOR_VERSION_VALUE: str = "1"
ALGORITHM_VERSION = Literal["deficit-classifier/v1"]
ALGORITHM_VERSION_VALUE: str = "deficit-classifier/v1"
DOMAIN = b"evallab.capability-deficit.v1\x00"


DeficitFamily = Literal[
    "none",
    "complete-but-reordered",
    "wrong-binding-or-addressing",
    "wrong-graph-traversal",
    "blind-retry",
    "malformed-output",
    "unclassified",
]

CaptureStatus = Literal[
    "captured",
    "partial_capture",
    "capture_loss",
    "non_evaluated",
    "unavailable",
]

EnvironmentIntegrity = Literal["declared", "agent_configured", "unknown"]

AttributionGate = Literal["deficit_supported", "deficit_refuted", "unattributable"]

ClaimScope = Literal["descriptive_single_trial"]

ProposedInterventionDimension = Literal[
    "instruction_ordering_scaffold",
    "retrieval_addressing",
    "graph_traversal_policy",
    "retry_policy",
    "output_format_contract",
    "none_available",
]

#: Fixed family -> intervention mapping. Mechanical, not inferred per trial.
INTERVENTION_DIMENSIONS: dict[str, tuple[ProposedInterventionDimension, ...]] = {
    "complete-but-reordered": ("instruction_ordering_scaffold",),
    "wrong-binding-or-addressing": ("retrieval_addressing", "output_format_contract"),
    "wrong-graph-traversal": ("graph_traversal_policy",),
    "blind-retry": ("retry_policy",),
    "malformed-output": ("output_format_contract",),
    "unclassified": ("none_available",),
    "none": ("none_available",),
}

#: Explicit classification precedence when several probe facts fire. Documented
#: per wK:p7 non-blocking note A1; highest entry wins.
FAMILY_PRECEDENCE: tuple[DeficitFamily, ...] = (
    "malformed-output",
    "wrong-binding-or-addressing",
    "wrong-graph-traversal",
    "complete-but-reordered",
    "blind-retry",
)

#: Evidence kinds per family that certify the probe condition was exercised.
FAMILY_PROBE_KINDS: dict[str, tuple[str, ...]] = {
    "complete-but-reordered": ("retrieval_completeness", "read_order_match"),
    "wrong-binding-or-addressing": (
        "function_name_match",
        "argument_semantic_match",
        "argument_type_match",
    ),
    "wrong-graph-traversal": ("graph_traversal_check",),
    "blind-retry": ("repeated_identical_failed_call",),
    "malformed-output": ("output_contract",),
}

_VERIFIER_PATH = "verifier/result.json"
_EVENTS_PATH = "artifacts/app/output/benchmark-events.jsonl"

_SOURCE_ARTIFACT_KINDS: dict[str, str] = {
    _VERIFIER_PATH: "verifier",
    _EVENTS_PATH: "interpretation",
    "verifier/test-stdout.txt": "verifier",
    "tau3_runtime_state.json": "final_state",
    "result.json": "outcome",
}


def _canonical_json_bytes(value: Any) -> bytes:
    """Canonical bytes, delegated to the spine implementation (wK:p6)."""
    return canonical_json(value).encode("utf-8")


def _domain_json_digest(domain: bytes, value: Any) -> str:
    """Domain-separated sha256 over canonical bytes, via spine compute_sha256."""
    return "sha256:" + compute_sha256(domain + _canonical_json_bytes(value))


class DeficitEvidence(ContractModel):
    """One mechanical fact supporting the family. Digest-bound, no prose."""

    evidence_id: str = Field(min_length=1, max_length=256)
    kind: Literal[
        "verifier_reason",
        "verifier_reward_breakdown",
        "retrieval_completeness",
        "read_order_match",
        "output_contract",
        "function_name_match",
        "argument_semantic_match",
        "argument_type_match",
        "repeated_identical_failed_call",
        "varied_argument_failed_calls",
        "runtime_termination",
        "graph_traversal_check",
    ]
    source_path: str = Field(min_length=1, max_length=512)
    source_artifact_digest: Digest
    detail: str = Field(min_length=1, max_length=512)
    provenance_kind: Literal["mechanical"] = "mechanical"


class Counterevidence(ContractModel):
    """A mechanical fact arguing against the family. Recorded, never dropped."""

    evidence_id: str = Field(min_length=1, max_length=256)
    kind: Literal[
        "adaptive_interleave_present",
        "order_match_present",
        "complete_retrieval_present",
        "distinct_retry_arguments",
        "verifier_pass",
        "function_name_match_present",
        "argument_semantic_match_present",
    ]
    source_path: str = Field(min_length=1, max_length=512)
    source_artifact_digest: Digest
    detail: str = Field(min_length=1, max_length=512)
    provenance_kind: Literal["mechanical"] = "mechanical"


class SourceBinding(ContractModel):
    """Immutable provenance. Substituting any digest changes the artifact digest."""

    job_id: str = Field(min_length=1)
    trial_id: str = Field(min_length=1)
    benchmark_family: str = Field(min_length=1)
    task_id: str
    task_digest: Digest
    verifier_result_digest: Digest
    events_digest: Digest
    test_stdout_digest: Digest
    cluster_key: str = Field(min_length=1)
    split: Literal["train", "held_out", "calibration", "unassigned"] = "unassigned"


class CaptureAccounting(ContractModel):
    #: ``environment_integrity``/``trial_admissible`` here are CALLER ASSERTIONS,
    #: valid only for offline fixtures until a Gate Zero verified-receipt type
    #: exists. Migration target (coordinated with wK:p6, shared module owned by
    #: wH:p0): positive authority must bind to authenticated
    #: evallab.evidence_store EvidenceArchive/EvidenceLocator anchors or an exact
    #: verified projection, with digest parity, produced by the extraction owner.
    #: "declared" + a boolean alone is NOT verified. The frozen literal below
    #: makes the self-attested status machine-visible in every artifact.
    verification_source: Literal["caller_assertion_offline_fixture"] = (
        "caller_assertion_offline_fixture"
    )
    capture_status: CaptureStatus
    environment_integrity: EnvironmentIntegrity
    trial_admissible: bool | None = None
    llm_calls_recorded: int = Field(ge=0)
    tool_calls_recorded: int = Field(ge=0)
    notes: str = Field(max_length=512, default="")


class VerifierFacts(ContractModel):
    status: str | None = None
    reward: float | None = None
    tau2_evaluation: bool | None = None
    reward_basis: tuple[str, ...] | None = None
    reward_breakdown: dict[str, float] | None = None
    termination_reason: str | None = None
    reason: str | None = None
    output_contract_ok: bool | None = None
    function_name_match: bool | None = None
    argument_semantic_match: bool | None = None
    argument_type_match: bool | None = None


class RetrievalFacts(ContractModel):
    required_reads: int | None = None
    observed_reads: int | None = None
    actual_read_order_matches_required: bool | None = None
    graph_traversal_violated: bool | None = None


class ToolCallSpec(ContractModel):
    tool_name: str | None = None
    arguments: Any = None
    is_error: bool = False


def _digest_of(digests: dict[str, Digest], path: str) -> str:
    if path not in digests:
        raise ValueError(f"artifact digest missing for source path: {path}")
    return digests[path]


class CapabilityDeficitInput(ContractModel):
    """Strict typed mining input. Unknown keys fail construction."""

    source_binding: SourceBinding
    artifact_digests: dict[str, Digest] = Field(default_factory=dict)
    verifier: VerifierFacts = Field(default_factory=VerifierFacts)
    retrieval: RetrievalFacts = Field(default_factory=RetrievalFacts)
    tool_call_sequence: tuple[ToolCallSpec, ...] = ()
    #: Authorities are untrusted input until the miner re-verifies them before
    #: opening a positive attribution gate.
    evidence_authorities: tuple[ArtifactAuthority, ...] = ()
    #: Separately verified canonical TrialAdmissibilityV1 record bytes. It is
    #: mandatory for positive attribution and is never caller assertion alone.
    admissibility_record_authority: ArtifactAuthority | None = None
    capture: CaptureAccounting = Field(
        default_factory=lambda: CaptureAccounting(
            capture_status="unavailable", environment_integrity="unknown"
        )
    )


class CapabilityDeficitArtifact(ContractModel):
    schema_version: SCHEMA_VERSION
    extractor_id: EXTRACTOR_ID
    extractor_version: EXTRACTOR_VERSION
    algorithm_version: ALGORITHM_VERSION
    source_binding: SourceBinding
    family: DeficitFamily
    attribution_gate: AttributionGate
    claim_scope: ClaimScope = "descriptive_single_trial"
    evidence: tuple[DeficitEvidence, ...] = ()
    counterevidence: tuple[Counterevidence, ...] = ()
    capture: CaptureAccounting
    #: Re-verified bytes authorities for every cited source on a positive claim.
    #: Unattributable artifacts deliberately carry no authority receipt.
    evidence_authorities: tuple[ArtifactAuthority, ...] = ()
    #: Separate archive authority for the canonical TrialAdmissibilityV1 bytes
    #: that bind every source receipt on a positive claim.
    admissibility_record_authority: ArtifactAuthority | None = None
    hold_reasons: tuple[str, ...] = ()
    proposed_intervention_dimensions: tuple[ProposedInterventionDimension, ...] = ()
    content_digest: Digest

    @model_validator(mode="after")
    def _enforce(self) -> CapabilityDeficitArtifact:
        if tuple(sorted(set(self.hold_reasons))) != self.hold_reasons:
            raise ValueError("capability deficit hold_reasons must be unique and sorted")
        ev_ids = tuple(e.evidence_id for e in self.evidence)
        if len(set(ev_ids)) != len(ev_ids):
            raise ValueError("duplicate deficit evidence_id")
        if ev_ids != tuple(sorted(ev_ids)):
            raise ValueError("deficit evidence must be sorted by evidence_id")
        ce_ids = tuple(c.evidence_id for c in self.counterevidence)
        if len(set(ce_ids)) != len(ce_ids):
            raise ValueError("duplicate counterevidence_id")
        if ce_ids != tuple(sorted(ce_ids)):
            raise ValueError("counterevidence must be sorted by evidence_id")
        overlap = {e.evidence_id for e in self.evidence} & {
            c.evidence_id for c in self.counterevidence
        }
        if overlap:
            raise ValueError(
                f"evidence id used as both evidence and counterevidence: {sorted(overlap)}"
            )
        def _check_parity(items: Any, label: str) -> None:
            for item in items:
                if item.source_path == _VERIFIER_PATH and (
                    item.source_artifact_digest
                    != self.source_binding.verifier_result_digest
                ):
                    raise ValueError(
                        f"{label} digest does not match source binding "
                        "verifier_result_digest"
                    )
                if item.source_path == _EVENTS_PATH and (
                    item.source_artifact_digest != self.source_binding.events_digest
                ):
                    raise ValueError(
                        f"{label} digest does not match source binding events_digest"
                    )
                if item.source_path == "verifier/test-stdout.txt" and (
                    item.source_artifact_digest != self.source_binding.test_stdout_digest
                ):
                    raise ValueError(
                        f"{label} digest does not match source binding test_stdout_digest"
                    )

        _check_parity(self.evidence, "evidence")
        _check_parity(self.counterevidence, "counterevidence")
        positive = (
            self.capture.environment_integrity == "declared"
            and self.capture.capture_status == "captured"
            and self.capture.trial_admissible is True
        )
        if self.attribution_gate in ("deficit_supported", "deficit_refuted"):
            if not positive:
                raise ValueError(
                    "deficit_supported/deficit_refuted require declared environment "
                    "integrity, captured status, and trial_admissible=True"
                )
            cited = (*self.evidence, *self.counterevidence)
            cited_paths = {item.source_path for item in cited}
            record_authority = self.admissibility_record_authority
            if (
                record_authority is None
                or record_authority.level != "bytes-verified"
                or record_authority.verifier_implementation_digest
                != VERIFIER_IMPLEMENTATION_DIGEST
                or record_authority.admissibility_binding is not None
                or record_authority.anchor is None
                or record_authority.artifact.ref != record_authority.anchor.inner_path
            ):
                raise ValueError(
                    "positive attribution requires an anchored, bytes-verified "
                    "admissibility record authority without a nested binding"
                )
            record_anchor = record_authority.anchor
            receipts = {authority.artifact.ref: authority for authority in self.evidence_authorities}
            if len(receipts) != len(self.evidence_authorities) or set(receipts) != cited_paths:
                raise ValueError(
                    "positive attribution requires exactly one authority receipt for every cited source"
                )
            for item in cited:
                path = item.source_path
                authority = receipts[path]
                expected_kind = _SOURCE_ARTIFACT_KINDS.get(path)
                binding = authority.admissibility_binding
                anchor = authority.anchor
                if (
                    expected_kind is None
                    or authority.level != "bytes-verified"
                    or authority.verifier_implementation_digest
                    != VERIFIER_IMPLEMENTATION_DIGEST
                    or authority.artifact.digest != item.source_artifact_digest
                    or binding is None
                    or binding.trial_id != self.source_binding.trial_id
                    or binding.artifact_kind != expected_kind
                    or anchor is None
                    or authority.artifact.ref != anchor.inner_path
                    or (
                        anchor.record_kind,
                        anchor.record_id,
                        anchor.expected_record_digest,
                        anchor.expected_content_digest,
                    )
                    != (
                        record_anchor.record_kind,
                        record_anchor.record_id,
                        record_anchor.expected_record_digest,
                        record_anchor.expected_content_digest,
                    )
                ):
                    raise ValueError(
                        "positive attribution authorities must share the "
                        "admissibility record archive coordinate"
                    )
        elif self.admissibility_record_authority is not None or self.evidence_authorities:
            raise ValueError("unattributable artifacts must not retain authority receipts")
        if self.attribution_gate == "deficit_supported":
            if not self.evidence:
                raise ValueError("deficit_supported requires at least one evidence item")
            if self.family in ("none", "unclassified"):
                raise ValueError("deficit_supported requires a concrete family")
        if self.attribution_gate == "deficit_refuted" and not self.counterevidence:
            raise ValueError("deficit_refuted requires counterevidence")
        if self.family == "unclassified" and self.attribution_gate == "deficit_supported":
            raise ValueError("unclassified family cannot support a deficit claim")
        expected_dims = (
            INTERVENTION_DIMENSIONS[self.family]
            if self.attribution_gate == "deficit_supported"
            else ("none_available",)
        )
        if self.proposed_intervention_dimensions != expected_dims:
            raise ValueError(
                "proposed_intervention_dimensions must equal the fixed family mapping "
                f"{expected_dims} for gate {self.attribution_gate}"
            )
        body = self.model_dump(mode="json", exclude={"content_digest"})
        if self.content_digest != _domain_json_digest(DOMAIN, body):
            raise ValueError("capability deficit artifact digest mismatch")
        return self


class CapabilityDeficitArtifactReceipt(ContractModel):
    """Externally archived authority over one exact generated artifact payload."""

    artifact: CapabilityDeficitArtifact
    artifact_authority: ArtifactAuthority

    @model_validator(mode="after")
    def _require_external_bytes_authority(self) -> CapabilityDeficitArtifactReceipt:
        authority = self.artifact_authority
        anchor = authority.anchor
        if (
            authority.level != "bytes-verified"
            or authority.verifier_implementation_digest
            != VERIFIER_IMPLEMENTATION_DIGEST
            or anchor is None
            or authority.admissibility_binding is not None
            or authority.artifact.ref != anchor.inner_path
        ):
            raise ValueError(
                "artifact receipt requires anchored, bytes-verified external "
                "authority without an admissibility binding"
            )
        return self


class _EvidenceBuilder:
    """Collects mechanical facts while classification walks the input."""

    def __init__(self, digests: dict[str, Digest]) -> None:
        self._digests = digests
        self._evidence: list[DeficitEvidence] = []
        self._counter: list[Counterevidence] = []

    def add(self, kind: str, path: str, detail: str) -> None:
        evidence_id = f"{path}@{_digest_of(self._digests, path)}:{kind}"
        self._evidence.append(
            DeficitEvidence(
                evidence_id=evidence_id,
                kind=kind,  # type: ignore[arg-type]
                source_path=path,
                source_artifact_digest=_digest_of(self._digests, path),
                detail=detail,
            )
        )

    def counter(self, kind: str, path: str, detail: str) -> None:
        evidence_id = f"{path}@{_digest_of(self._digests, path)}:counter:{kind}"
        self._counter.append(
            Counterevidence(
                evidence_id=evidence_id,
                kind=kind,  # type: ignore[arg-type]
                source_path=path,
                source_artifact_digest=_digest_of(self._digests, path),
                detail=detail,
            )
        )


def _reverified_authorities(
    binding: SourceBinding,
    evidence: tuple[DeficitEvidence, ...],
    counterevidence: tuple[Counterevidence, ...],
    authorities: tuple[ArtifactAuthority, ...],
    record_authority: ArtifactAuthority | None,
    *,
    repo_root: Path | str | None,
    store_root: Path | str | None,
) -> tuple[tuple[ArtifactAuthority, ...], ArtifactAuthority] | None:
    """Reopen canonical admissibility bytes before accepting source evidence."""

    if (
        record_authority is None
        or record_authority.level != "bytes-verified"
        or record_authority.verifier_implementation_digest
        != VERIFIER_IMPLEMENTATION_DIGEST
        or record_authority.admissibility_binding is not None
        or record_authority.anchor is None
        or record_authority.artifact.ref != record_authority.anchor.inner_path
    ):
        return None
    reopened_record = reverify_authority(
        record_authority,
        expected_verifier_digest=VERIFIER_IMPLEMENTATION_DIGEST,
        repo_root=repo_root,
        store_root=store_root,
    )
    if not isinstance(reopened_record, tuple):
        return None
    record_bytes, verified_record = reopened_record
    if verified_record != record_authority:
        return None
    try:
        admissibility = TrialAdmissibilityV1.model_validate_json(record_bytes)
    except ValueError:
        return None
    if record_bytes != _canonical_json_bytes(admissibility.model_dump(mode="json")) + b"\n":
        return None
    if (
        admissibility.trial_id != binding.trial_id
        or admissibility.decision != "admissible"
        or admissibility.analysis_eligibility != "causal-eligible"
        or admissibility.allowed_use != "causal"
        or admissibility.task_runtime_identity is None
        or admissibility.task_runtime_identity.registry_admission_state != "registered"
    ):
        return None
    if admissibility.source_paths is None:
        return None

    cited: dict[str, Digest] = {}
    for item in (*evidence, *counterevidence):
        existing = cited.setdefault(item.source_path, item.source_artifact_digest)
        if existing != item.source_artifact_digest:
            return None
    receipts = {authority.artifact.ref: authority for authority in authorities}
    if len(receipts) != len(authorities) or set(receipts) != set(cited):
        return None

    record_anchor = record_authority.anchor
    verified: list[ArtifactAuthority] = []
    for path, digest in cited.items():
        authority = receipts[path]
        receipt_binding = authority.admissibility_binding
        anchor = authority.anchor
        if (
            authority.level != "bytes-verified"
            or authority.verifier_implementation_digest != VERIFIER_IMPLEMENTATION_DIGEST
            or authority.artifact.digest != digest
            or authority.artifact.ref != path
            or receipt_binding is None
            or receipt_binding.trial_id != binding.trial_id
            or receipt_binding.admissibility_digest != admissibility.admissibility_digest
            or receipt_binding.artifact_kind != _SOURCE_ARTIFACT_KINDS.get(path)
            or anchor is None
            or authority.artifact.ref != anchor.inner_path
            or (
                anchor.record_kind,
                anchor.record_id,
                anchor.expected_record_digest,
                anchor.expected_content_digest,
            )
            != (
                record_anchor.record_kind,
                record_anchor.record_id,
                record_anchor.expected_record_digest,
                record_anchor.expected_content_digest,
            )
        ):
            return None
        admissibility_digests = admissibility.source_digests.model_dump(mode="json")
        admissibility_paths = admissibility.source_paths.model_dump(mode="json")
        if (
            admissibility_digests.get(receipt_binding.artifact_kind) != digest
            or path not in admissibility_paths.get(receipt_binding.artifact_kind, ())
        ):
            return None
        result = reverify_authority(
            authority,
            expected_verifier_digest=VERIFIER_IMPLEMENTATION_DIGEST,
            repo_root=repo_root,
            store_root=store_root,
        )
        if not isinstance(result, tuple):
            return None
        _bytes, reverified = result
        if reverified != authority:
            return None
        verified.append(reverified)
    return (
        tuple(sorted(verified, key=lambda authority: authority.artifact.ref)),
        verified_record,
    )


def _args_id(call: ToolCallSpec) -> str:
    return hashlib.sha256(_canonical_json_bytes(call.arguments)).hexdigest()


def reverify_capability_deficit_artifact(
    receipt_or_mapping: CapabilityDeficitArtifactReceipt | Mapping[str, Any],
    *,
    authority_repo_root: Path | str | None = None,
    authority_store_root: Path | str | None = None,
) -> bool:
    """Return whether an externally authenticated artifact retains live authority."""

    try:
        if isinstance(receipt_or_mapping, CapabilityDeficitArtifactReceipt):
            raw_receipt: Mapping[str, Any] = receipt_or_mapping.model_dump(mode="json")
        elif isinstance(receipt_or_mapping, Mapping):
            raw_receipt = receipt_or_mapping
        else:
            return False
        receipt = CapabilityDeficitArtifactReceipt.model_validate(raw_receipt)
        reopened_output = reverify_authority(
            receipt.artifact_authority,
            expected_verifier_digest=VERIFIER_IMPLEMENTATION_DIGEST,
            repo_root=authority_repo_root,
            store_root=authority_store_root,
        )
        if not isinstance(reopened_output, tuple):
            return False
        output_bytes, verified_output_authority = reopened_output
        if verified_output_authority != receipt.artifact_authority:
            return False
        expected_bytes = (
            _canonical_json_bytes(receipt.artifact.model_dump(mode="json")) + b"\n"
        )
        if output_bytes != expected_bytes:
            return False
        authenticated_artifact = CapabilityDeficitArtifact.model_validate_json(output_bytes)
        if authenticated_artifact != receipt.artifact:
            return False
        if authenticated_artifact.attribution_gate not in (
            "deficit_supported",
            "deficit_refuted",
        ):
            return False
        verified = _reverified_authorities(
            authenticated_artifact.source_binding,
            authenticated_artifact.evidence,
            authenticated_artifact.counterevidence,
            authenticated_artifact.evidence_authorities,
            authenticated_artifact.admissibility_record_authority,
            repo_root=authority_repo_root,
            store_root=authority_store_root,
        )
        if verified is None:
            return False
        authorities, record_authority = verified
        return (
            authorities == authenticated_artifact.evidence_authorities
            and record_authority
            == authenticated_artifact.admissibility_record_authority
        )
    except (TypeError, ValueError):
        return False


def _positive_verification(capture: CaptureAccounting) -> bool:


    """Positive verified integrity/admissibility. ``unknown`` is not permission."""

    return (
        capture.environment_integrity == "declared"
        and capture.capture_status == "captured"
        and capture.trial_admissible is True
    )


def mine_capability_deficit(
    spec: CapabilityDeficitInput | dict[str, Any],
    *,
    authority_repo_root: Path | str | None = None,
    authority_store_root: Path | str | None = None,
) -> CapabilityDeficitArtifact:
    """Classify one trial deterministically.

    Accepts a :class:`CapabilityDeficitInput` or a JSON-shaped dict, which is
    validated strictly first (unknown and missing keys are typed ValidationError,
    never KeyError). Classification reads declared fields only, in a fixed order.
    No field is inferred from names, paths, or defaults; source paths are
    provenance labels only and never influence classification. Environment
    non-evaluation is EXPLICIT only.
    """

    if not isinstance(spec, CapabilityDeficitInput):
        spec = CapabilityDeficitInput.model_validate(spec)

    binding = spec.source_binding
    verifier = spec.verifier
    retrieval = spec.retrieval
    calls = spec.tool_call_sequence
    capture = spec.capture

    ev = _EvidenceBuilder(spec.artifact_digests)
    hold: list[str] = []

    # --- axis 1: capture loss dominates everything ---------------------------
    if capture.capture_status in ("capture_loss", "unavailable"):
        return _artifact(
            binding, "unclassified", "unattributable", ev, capture,
            sorted({"unattributable_capture_loss"}),
        )

    # --- axis 2: EXPLICIT environment non-evaluation only --------------------
    if capture.capture_status == "non_evaluated" or verifier.tau2_evaluation is False:
        if verifier.termination_reason:
            ev.add(
                                "runtime_termination",
                "tau3_runtime_state.json",
                f"termination_reason={verifier.termination_reason}",
            )
        ev.add(
                        "verifier_reason",
            _VERIFIER_PATH,
            f"tau2_evaluation={verifier.tau2_evaluation!r} reward={verifier.reward!r} "
            + "breakdown=" + ("absent" if verifier.reward_breakdown is None else "present"),
        )
        return _artifact(
            binding, "unclassified", "unattributable", ev, capture,
            sorted({"environment_non_evaluation"}),
        )

    # --- axis 3: agent-configured environment --------------------------------
    if capture.environment_integrity == "agent_configured":
        ev.add(
                        "verifier_reason",
            _VERIFIER_PATH,
            "environment_integrity=agent_configured: outcome not attributable",
        )
        return _artifact(
            binding,
            "unclassified" if verifier.reward != 1.0 else "none",
            "unattributable",
            ev,
            capture,
            sorted({"environment_agent_configured"}),
        )

    # --- axis 4: reward without semantic evaluator evidence ------------------
    semantic = verifier.reward_breakdown is not None or verifier.tau2_evaluation is True
    if verifier.reward is not None and float(verifier.reward) >= 1.0 and not semantic:
        ev.add(
                        "verifier_reason",
            _VERIFIER_PATH,
            "reward>=1 without reward_breakdown and without tau2_evaluation: "
            "reward-only, unattributable",
        )
        return _artifact(
            binding,
            "none",
            "unattributable",
            ev,
            capture,
            sorted({"reward_only_without_semantic_evaluator"}),
        )

    # --- axis 5: verified pass (semantic evidence present) -------------------
    if verifier.reward is not None and float(verifier.reward) >= 1.0:
        ev.add(
                        "verifier_reward_breakdown",
            _VERIFIER_PATH,
            f"reward={verifier.reward!r} breakdown=present",
        )
        ev.counter(
                        "verifier_pass",
            _VERIFIER_PATH,
            "verified pass refutes any deficit family",
        )
        if not _positive_verification(capture):
            hold.append("positive_verification_missing")
            return _artifact(binding, "none", "unattributable", ev, capture, sorted(set(hold)))
        verified = _reverified_authorities(
            binding,
            tuple(ev._evidence),
            tuple(ev._counter),
            spec.evidence_authorities,
            spec.admissibility_record_authority,
            repo_root=authority_repo_root,
            store_root=authority_store_root,
        )
        if verified is None:
            hold.append("positive_authority_unverified")
            return _artifact(binding, "none", "unattributable", ev, capture, sorted(set(hold)))
        authorities, record_authority = verified
        return _artifact(
            binding,
            "none",
            "deficit_refuted",
            ev,
            capture,
            sorted(set(hold)),
            authorities,
            record_authority,
        )

    # --- axis 6: evaluator-status unknown on failure -------------------------
    if verifier.reward is not None and float(verifier.reward) < 1.0 and not semantic:
        hold.append("evaluator_status_unavailable")

    # --- axis 7: failure classification --------------------------------------
    if retrieval.required_reads is not None and retrieval.observed_reads is not None:
        ev.add(
                        "retrieval_completeness",
            _EVENTS_PATH,
            f"required={retrieval.required_reads} observed={retrieval.observed_reads}",
        )
        if retrieval.actual_read_order_matches_required is not None:
            ev.add(
                                "read_order_match",
                _EVENTS_PATH,
                "actual_read_order_matches_required="
                f"{retrieval.actual_read_order_matches_required!r}",
            )

    if verifier.reason:
        ev.add("verifier_reason", _VERIFIER_PATH, str(verifier.reason)[:512])

    candidates: set[str] = set()

    # complete-but-reordered: retrieved everything, failed on order.
    if (
        retrieval.required_reads is not None
        and retrieval.observed_reads is not None
        and retrieval.observed_reads == retrieval.required_reads
        and retrieval.actual_read_order_matches_required is False
    ):
        candidates.add("complete-but-reordered")

    if verifier.output_contract_ok is False:
        candidates.add("malformed-output")
        ev.add(
                        "output_contract",
            "result.json",
            "required output artifact missing or unparseable",
        )

    if verifier.function_name_match is False or (
        verifier.argument_semantic_match is False or verifier.argument_type_match is False
    ):
        candidates.add("wrong-binding-or-addressing")
    if verifier.function_name_match is True:
        ev.counter(
                        "order_match_present",
            "verifier/test-stdout.txt",
            "function name matched; any deficit is narrower than binding",
        )
    if verifier.argument_semantic_match is True:
        ev.counter(
                        "complete_retrieval_present",
            "verifier/test-stdout.txt",
            "argument semantics matched",
        )
    if verifier.argument_semantic_match is False:
        ev.add(
                        "argument_semantic_match",
            "verifier/test-stdout.txt",
            "argument value failed semantic match",
        )
    if verifier.argument_type_match is False:
        ev.add(
                        "argument_type_match",
            "verifier/test-stdout.txt",
            "argument shape failed type match",
        )

    if retrieval.graph_traversal_violated is True:
        ev.add(
                        "graph_traversal_check",
            _EVENTS_PATH,
            "declared traversal order violated",
        )
        candidates.add("wrong-graph-traversal")

    # blind retry: >=3 consecutive IDENTICAL (tool, arguments) calls, every one
    # errored. Varied-argument retries are UNSUCCESSFUL ADAPTATION, not blind
    # repetition (orchestrator ruling 2026-09-03, supersedes wK:p6): they stay
    # unclassified with a typed hold and full evidence, pending a separately
    # defined family.
    exact_runs: list[list[Any]] = []  # [tool, args_id, len, all_error]
    tool_runs: list[list[Any]] = []  # [tool, len, all_error, args_ids]
    exact_key: tuple[str, str] | None = None
    tool_key: str | None = None
    for c in calls:
        tool = c.tool_name or ""
        args_id = _args_id(c)
        key = (tool, args_id)
        if key == exact_key:
            run = exact_runs[-1]
            run[2] += 1
            run[3] = run[3] and c.is_error
        else:
            exact_runs.append([tool, args_id, 1, c.is_error])
            exact_key = key
        if tool == tool_key:
            tool_run = tool_runs[-1]
            tool_run[1] += 1
            tool_run[2] = tool_run[2] and c.is_error
            tool_run[3].append(args_id)
        else:
            tool_runs.append([tool, 1, c.is_error, [args_id]])
            tool_key = tool

    blind_tools: set[str] = set()
    for tool, _args_id_value, n, all_err in exact_runs:
        if n >= 3 and all_err:
            blind_tools.add(tool)
            candidates.add("blind-retry")
            ev.add(
                "repeated_identical_failed_call",
                _EVENTS_PATH,
                f"same tool '{tool}' called {n} times consecutively with identical "
                "canonical arguments; every attempt errored",
            )
    for tool, n, all_err, args_ids in tool_runs:
        if n >= 3 and all_err and len(set(args_ids)) > 1 and tool not in blind_tools:
            ev.add(
                "varied_argument_failed_calls",
                _EVENTS_PATH,
                f"same tool '{tool}' called {n} times consecutively with varied "
                "canonical arguments; every attempt errored",
            )
            hold.append("varied_argument_retry_unclassified")

    # adaptive interleave requires at least one SUCCESSFUL distinct call. When
    # every call errored the sequence is blind/unresolved, never "adaptive"
    # (wK:p6). A mixed blind-run + successful adaptive tail records BOTH.
    distinct_tools = len({c.tool_name or "" for c in calls}) > 1
    any_success = any(not c.is_error for c in calls)
    if calls and distinct_tools and any_success:
        ev.counter(
            "adaptive_interleave_present",
            _EVENTS_PATH,
            "retry sequence contains successful distinct calls: adaptive pattern present",
        )
        hold.append("retry_pattern_adaptive")

    # resolve candidates by documented precedence (wK:p7 note A1)
    family = "unclassified"
    if candidates:
        for f in FAMILY_PRECEDENCE:
            if f in candidates:
                family = f
                break

    if family == "unclassified":
        gate = "unattributable"
        hold.append("mechanism_not_represented")
    else:
        if not _positive_verification(capture):
            # Caller-declared capture/admissibility is necessary but never sufficient.
            hold.append("positive_verification_missing")
            hold.append("mechanism_not_represented")
            return _artifact(
                binding, "unclassified", "unattributable", ev, capture, sorted(set(hold))
            )
        verified = _reverified_authorities(
            binding,
            tuple(ev._evidence),
            tuple(ev._counter),
            spec.evidence_authorities,
            spec.admissibility_record_authority,
            repo_root=authority_repo_root,
            store_root=authority_store_root,
        )
        if verified is None:
            hold.append("positive_authority_unverified")
            hold.append("mechanism_not_represented")
            return _artifact(
                binding, "unclassified", "unattributable", ev, capture, sorted(set(hold))
            )
        authorities, record_authority = verified
        gate = "deficit_supported"
        return _artifact(
            binding,
            family,
            gate,
            ev,
            capture,
            sorted(set(hold)),
            authorities,
            record_authority,
        )

    return _artifact(binding, family, gate, ev, capture, sorted(set(hold)))


def _artifact(
    binding: SourceBinding,
    family: str,
    gate: str,
    ev: _EvidenceBuilder,
    capture: CaptureAccounting,
    hold_reasons: list[str],
    evidence_authorities: tuple[ArtifactAuthority, ...] = (),
    admissibility_record_authority: ArtifactAuthority | None = None,
) -> CapabilityDeficitArtifact:
    dims: tuple[ProposedInterventionDimension, ...] = (
        INTERVENTION_DIMENSIONS[family] if gate == "deficit_supported" else ("none_available",)
    )
    body = {
        "schema_version": SCHEMA_VERSION_VALUE,
        "extractor_id": EXTRACTOR_ID_VALUE,
        "extractor_version": EXTRACTOR_VERSION_VALUE,
        "algorithm_version": ALGORITHM_VERSION_VALUE,
        "source_binding": binding.model_dump(mode="json"),
        "family": family,
        "attribution_gate": gate,
        "claim_scope": "descriptive_single_trial",
        "evidence": [
            e.model_dump(mode="json")
            for e in sorted(ev._evidence, key=lambda x: x.evidence_id)
        ],
        "counterevidence": [
            c.model_dump(mode="json")
            for c in sorted(ev._counter, key=lambda x: x.evidence_id)
        ],
        "evidence_authorities": [
            authority.model_dump(mode="json")
            for authority in sorted(
                evidence_authorities, key=lambda authority: authority.artifact.ref
            )
        ],
        "admissibility_record_authority": (
            admissibility_record_authority.model_dump(mode="json")
            if admissibility_record_authority is not None
            else None
        ),
        "capture": capture.model_dump(mode="json"),
        "hold_reasons": list(hold_reasons),
        "proposed_intervention_dimensions": list(dims),
    }
    digest = _domain_json_digest(DOMAIN, body)
    return CapabilityDeficitArtifact(**body, content_digest=digest)


# ---------------------------------------------------------------------------
# TRACE-style capability measures (Track F). Cov/ER-/ER+/Delta over certified
# artifact facts only. Delta prioritizes; it never proves causality. No TRACE
# runtime, LLM labeler, MoE, or GRPO component exists here.
# ---------------------------------------------------------------------------

MeasureStatus = Literal["NA", "PRESENT", "LACKING"]


class TraceMeasure(ContractModel):
    status: MeasureStatus
    value: float | None = None
    denominator: int | None = Field(default=None, ge=0)
    numerator: int | None = Field(default=None, ge=0)


class TraceCapabilityMeasures(ContractModel):
    family: DeficitFamily
    cov: TraceMeasure
    er_minus: TraceMeasure
    er_plus: TraceMeasure
    delta: TraceMeasure
    #: Delta is a PRIORITY signal computed as ER+ - ER- over certified facts.
    #: It is never causal evidence and never a proof.
    delta_interpretation: Literal["priority_only_never_causal"] = (
        "priority_only_never_causal"
    )


def _certified(art: CapabilityDeficitArtifact) -> bool:
    return art.attribution_gate in ("deficit_supported", "deficit_refuted")


def _probe_exercised(art: CapabilityDeficitArtifact, family: str) -> bool:
    """The probe fact for ``family`` is PRESENT among certified mechanical facts."""

    kinds = {e.kind for e in art.evidence} | {c.kind for c in art.counterevidence}
    probe = FAMILY_PROBE_KINDS[family]
    return any(k in kinds for k in probe) or art.family == family


def trace_capability_measures(
    artifacts: list[CapabilityDeficitArtifact], family: str
) -> TraceCapabilityMeasures:
    """Compute Cov / ER- / ER+ / Delta for one family over certified artifacts.

    - Cov denominator: certified artifacts whose probe facts were recorded
      (evidence or counterevidence kinds for the family) — trials where the probe
      condition was actually evaluated. NA when zero.
    - ER+: deficit rate among probe-PRESENT trials. LACKING when the probe was
      never present. NA when the denominator is zero.
    - ER-: deficit rate among probe-LACKING trials within the same certified
      denominator. LACKING when no such trial exists. NA when zero.
    - Delta = ER+ - ER-. Priority ordering ONLY; never causal proof.
    """

    if family not in INTERVENTION_DIMENSIONS or family in ("none", "unclassified"):
        raise ValueError(f"unknown or non-probe family: {family!r}")

    certified = [a for a in artifacts if _certified(a)]
    probed = [a for a in certified if _probe_exercised(a, family)]
    unprobed = [a for a in certified if id(a) not in {id(b) for b in probed}]

    def _measure(count: int | None, denom: int | None, present: bool) -> TraceMeasure:
        if not present:
            return TraceMeasure(status="LACKING")
        if denom is None or denom == 0 or count is None:
            return TraceMeasure(status="NA")
        return TraceMeasure(status="PRESENT", value=count / denom, denominator=denom, numerator=count)

    cov = (
        _measure(len(probed), len(certified), present=True)
        if certified
        else TraceMeasure(status="NA")
    )
    plus_fail = sum(1 for a in probed if a.attribution_gate == "deficit_supported")
    minus_fail = sum(1 for a in unprobed if a.attribution_gate == "deficit_supported")
    er_plus = (
        _measure(plus_fail, len(probed), present=True)
        if probed
        else TraceMeasure(status="LACKING")
    )
    er_minus = (
        _measure(minus_fail, len(unprobed), present=True)
        if unprobed
        else TraceMeasure(status="LACKING")
    )
    if er_plus.status == "PRESENT" and er_minus.status == "PRESENT":
        delta = TraceMeasure(
            status="PRESENT",
            value=(er_plus.value or 0.0) - (er_minus.value or 0.0),
            denominator=len(certified),
            numerator=plus_fail,
        )
    else:
        delta = TraceMeasure(status="NA")

    return TraceCapabilityMeasures(
        family=family,  # type: ignore[arg-type]
        cov=cov,
        er_minus=er_minus,
        er_plus=er_plus,
        delta=delta,
    )


def trace_priority_order(
    measures_by_family: list[TraceCapabilityMeasures],
) -> list[tuple[str, float | None]]:
    """Rank families by Delta descending. Priority only, never causal proof.

    NA measures sort last and carry ``None``.
    """

    def key(m: TraceCapabilityMeasures) -> tuple[int, float]:
        if m.delta.status != "PRESENT" or m.delta.value is None:
            return (1, 0.0)
        return (0, -m.delta.value)

    ordered = sorted(measures_by_family, key=key)
    return [(m.family, m.delta.value if m.delta.status == "PRESENT" else None) for m in ordered]
