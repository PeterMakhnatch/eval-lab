from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from .schemas import ContractModel

#: ULID format per queue.new_ulid (Crockford base32, 26 chars, time-sortable).
ULID_PATTERN = r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$"
ULID_RE = re.compile(ULID_PATTERN)

#: Content digest format required by all contracts (T1 provenance).
SHA256_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
SHA256_RE = re.compile(SHA256_DIGEST_PATTERN)


def _validate_ulid(value: str) -> str:
    """Reject non-ULID identifiers at construction time."""
    if not ULID_RE.fullmatch(value):
        raise ValueError(f"identifier must be ULID, got {value!r}")
    return value


def _validate_sha256_digest(value: str) -> str:
    """Reject bare or malformed digests; every digest field must carry provenance."""
    if not SHA256_RE.fullmatch(value):
        raise ValueError(f"digest must be sha256: prefixed 64-hex, got {value!r}")
    return value


class EvidenceCitation(ContractModel):
    """Single evidence pointer inside an AnalysisRecord (path + optional step)."""

    path: str = Field(min_length=1, description="relative filesystem path to cited artifact")
    step: int | None = Field(
        default=None, ge=0, description="step index within trajectory if citation is step-specific"
    )


class ConfidenceClaim(ContractModel):
    """Automated claim with explicit uncertainty (T4). Never a bare float."""

    level: Literal["low", "medium", "high"] = Field(
        description="qualitative confidence label for the claim"
    )
    n: int | None = Field(
        default=None, ge=0, description="sample size backing statistical claim (None if N/A)"
    )
    interval: tuple[float, float] | None = Field(
        default=None,
        description="95% interval (low, high) when claim is numeric",
    )
    provenance_digest: str | None = Field(
        default=None,
        pattern=SHA256_DIGEST_PATTERN,
        description="sha256: of source data or rubric that produced this claim",
    )


class CriterionAgreement(ContractModel):
    """Per-criterion agreement score carrying n and rate (T4, not bare float)."""

    agreements: int = Field(ge=0, description="number of matching judgments on this criterion")
    total: int = Field(ge=0, description="total judgments rendered on this criterion")
    rate: float = Field(ge=0, le=1, description="agreements / total")


class Suite(ContractModel):
    """§2.1 Suite entity: named frozen collection of TaskVersion members.

    frozen_at set makes the instance reject all further mutation (enforced via
    __setattr__ guard + model validator). This is the contract that prevents
    post-freeze drift in parallel epic work.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    schema_version: Literal[1] = 1
    name: str = Field(
        min_length=1, max_length=80, description="human-readable suite identifier"
    )
    version: str = Field(
        min_length=1, max_length=40, description="suite version; increments on content change"
    )
    members: list[str] = Field(
        default_factory=list,
        description="TaskVersion references (task_ref@version strings) that constitute the suite",
    )
    frozen_at: datetime | None = Field(
        default=None,
        description="timestamp at which the suite was frozen; when set the instance is immutable",
    )

    _is_frozen: bool = False

    @model_validator(mode="after")
    def _mark_frozen(self) -> Suite:
        if self.frozen_at is not None:
            object.__setattr__(self, "_is_frozen", True)
        return self

    def __setattr__(self, name: str, value: Any) -> None:
        frozen = getattr(self, "_is_frozen", False)
        protected = {"_is_frozen", "_pydantic_fields_set"}
        if frozen and name not in protected:
            raise ValueError("frozen Suite is immutable per §2.1")
        super().__setattr__(name, value)


class AnalysisRecord(ContractModel):
    """§2.1 AnalysisRecord: model-assisted analysis output for one trial.

    analysis_id and trial_id are ULIDs. rubric_digest and all digests are
    validated sha256:. confidence carries explicit uncertainty (T4).
    """

    schema_version: Literal[1] = 1
    analysis_id: str = Field(description="ULID primary key for this analysis record")
    trial_id: str = Field(description="ULID of the source trial (join spine)")
    rubric_digest: str = Field(description="sha256: of the rubric applied")
    model: str = Field(min_length=1, description="judge model identifier")
    category: str = Field(
        min_length=1, description="analysis category (e.g. failure mode, capability)"
    )
    evidence: list[EvidenceCitation] = Field(
        default_factory=list, description="cited evidence locations (path, optional step)"
    )
    confidence: ConfidenceClaim = Field(
        description="claim confidence with n/interval/provenance per T4"
    )

    @field_validator("analysis_id", "trial_id")
    @classmethod
    def _ulid_ids(cls, v: str) -> str:
        return _validate_ulid(v)

    @field_validator("rubric_digest")
    @classmethod
    def _rubric_digest(cls, v: str) -> str:
        return _validate_sha256_digest(v)


class ObservationRecord(ContractModel):
    """§2.1 ObservationRecord: factual extraction per OBSERVATORY template.

    All factual fields taken verbatim from research/observations/TEMPLATE.md
    (template_version through evidence_files). trial_id is ULID. No invented
    taxonomy or capability fields.
    """

    schema_version: Literal[1] = 1
    template_version: Literal["observatory-1"] = Field(
        default="observatory-1", description="version of the observatory extraction template"
    )
    trial_id: str = Field(description="ULID of the observed trial (record key)")
    trial_name: str = Field(min_length=1, description="human-readable trial name")
    job: str = Field(min_length=1, description="job name that produced the trial")
    agent: str = Field(min_length=1, description="agent profile name")
    model: str | None = Field(default=None, description="model used (none for controls)")
    task: str = Field(min_length=1, description="task_ref@version or task path")
    reward: float | str = Field(description="verifier reward value (float or 'none')")
    steps_taken: int = Field(ge=0, description="number of steps executed")
    first_failure_step: int | Literal["none"] = Field(
        default="none", description="step of first failure or 'none'"
    )
    loop_detected: Literal["yes", "no"] = Field(
        default="no", description="whether a loop was detected"
    )
    loop_step: int | Literal["none"] = Field(
        default="none", description="step at which loop began or 'none'"
    )
    verified_before_done: Literal["yes", "no"] = Field(
        default="no", description="verifier passed before done signal"
    )
    tool_errors: int = Field(ge=0, description="count of tool invocation errors")
    summary: str = Field(description="one-sentence factual summary from trajectory/result.json")
    evidence_files: str = Field(
        default="", description="comma-separated list of evidence files examined"
    )

    @field_validator("trial_id")
    @classmethod
    def _trial_ulid(cls, v: str) -> str:
        return _validate_ulid(v)


class CalibrationRecord(ContractModel):
    """§2.1 CalibrationRecord: judge calibration result on a corpus.

    per_criterion_agreement uses explicit Agreement objects (T4) rather than
    bare floats. All digests validated.
    """

    schema_version: Literal[1] = 1
    calib_id: str = Field(description="ULID primary key for this calibration run")
    judge_model: str = Field(min_length=1, description="model that performed the judging")
    rubric_digest: str = Field(description="sha256: of rubric used for calibration")
    corpus_digest: str = Field(description="sha256: of the corpus of trials judged")
    per_criterion_agreement: dict[str, CriterionAgreement] = Field(
        min_length=1, description="agreement rate per rubric criterion (with n and rate)"
    )
    date: datetime = Field(description="calendar date of the calibration batch")

    @field_validator("calib_id")
    @classmethod
    def _calib_ulid(cls, v: str) -> str:
        return _validate_ulid(v)

    @field_validator("rubric_digest", "corpus_digest")
    @classmethod
    def _calib_digests(cls, v: str) -> str:
        return _validate_sha256_digest(v)


class Verdict(ContractModel):
    """§2.1 Verdict: human (or authorized) disposition on a discovery.

    status restricted to the literal set in §2.1. discovery_id is ULID.
    """

    schema_version: Literal[1] = 1
    discovery_id: str = Field(description="ULID of the discovery being verdicted (composite key)")
    status: Literal["accepted", "rejected", "needs_evidence", "pending"] = Field(
        description="disposition per §2.1"
    )
    by: str = Field(min_length=1, description="actor or session that issued the verdict")
    at: datetime = Field(description="timestamp of the verdict decision")
    note: str | None = Field(
        default=None, description="free-text rationale or pointer to evidence"
    )

    @field_validator("discovery_id")
    @classmethod
    def _discovery_ulid(cls, v: str) -> str:
        return _validate_ulid(v)
