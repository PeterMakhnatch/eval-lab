"""``failure-library/v1`` contracts.

Task-side codes follow the taxonomy in ``research/hackbook/taxonomy.json``
(TS01-TS11 for what is wrong with a task, RS01-RS07 for what happened in a
run). The verification tier of a finding is a pure function of its evidence
kinds; the model refuses a stored tier that disagrees with that function.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import date
from typing import Literal, get_args

from pydantic import Field, model_validator

from evallab.schemas import ContractModel

SCHEMA_VERSION: Literal["failure-library/v1"] = "failure-library/v1"

DefectClass = Literal[
    "TS01",
    "TS02",
    "TS03",
    "TS04",
    "TS05",
    "TS06",
    "TS07",
    "TS08",
    "TS09",
    "TS10",
    "TS11",
    "unclassified",
]
RunCode = Literal["RS01", "RS02", "RS03", "RS04", "RS05", "RS06", "RS07"]
Direction = Literal["wrong_pass", "unfair_fail", "other"]
EvidenceKind = Literal[
    "executed",
    "upstream_fix",
    "human_label",
    "construction",
    "judge_label",
    "static_finding",
]
Tier = Literal["V3_executed", "V2_upstream_fixed", "V1_external_label", "V0_unverified"]
Split = Literal["DEV", "TEST", "heldout", "cross_test", "unassigned"]
Status = Literal["still_broken", "fixed_upstream", "unknown"]
Outcome = Literal["pass", "fail", "error", "unknown"]
LabelKind = Literal["executed", "human", "judge", "construction"]

TASK_SIDE_CODES: tuple[str, ...] = tuple(
    code for code in get_args(DefectClass) if code != "unclassified"
)
RUN_SIDE_CODES: tuple[str, ...] = get_args(RunCode)

#: Evidence kind -> the tier it can support on its own.
TIER_BY_EVIDENCE: dict[str, Tier] = {
    "executed": "V3_executed",
    "upstream_fix": "V2_upstream_fixed",
    "human_label": "V1_external_label",
    "construction": "V1_external_label",
    "judge_label": "V0_unverified",
    "static_finding": "V0_unverified",
}
#: Strongest first.
TIER_ORDER: tuple[Tier, ...] = (
    "V3_executed",
    "V2_upstream_fixed",
    "V1_external_label",
    "V0_unverified",
)

#: Splits that may feed the knowledge compiler or agent retrieval.
KNOWLEDGE_SPLITS: frozenset[str] = frozenset({"DEV"})

_SLUG = r"^[a-z0-9][a-z0-9._-]*$"


def derive_tier(kinds: Iterable[str]) -> Tier:
    """Return the strongest tier any of ``kinds`` supports."""
    tiers = {TIER_BY_EVIDENCE[kind] for kind in kinds}
    if not tiers:
        raise ValueError("a finding needs at least one piece of evidence")
    return next(tier for tier in TIER_ORDER if tier in tiers)


def make_finding_id(
    source_id: str, task_id: str, revision: str, defect_class: str, locator: str
) -> str:
    """Deterministic id: the same claim about the same task version maps to one id."""
    material = "\x1f".join((source_id, task_id, revision, defect_class, locator))
    return "fl-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


class TaskRef(ContractModel):
    """The exact task a claim is about, pinned as it was when observed."""

    source_id: str = Field(min_length=1, pattern=_SLUG)
    task_id: str = Field(min_length=1)
    upstream_repo: str = Field(min_length=1)
    upstream_revision: str | None = Field(default=None, min_length=1)
    task_path: str | None = None
    task_version_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


class Evidence(ContractModel):
    """One piece of support for a finding; its kind decides what tier it can carry."""

    kind: EvidenceKind
    url: str | None = None
    receipt_ref: str | None = None
    reproducer_ref: str | None = None
    fix_revision: str | None = None
    fix_diff_digest: str | None = None
    label_source: str | None = None
    note: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _kind_requirements(self) -> Evidence:
        if self.kind == "executed" and not (self.receipt_ref or self.reproducer_ref):
            raise ValueError("executed evidence needs a receipt_ref or reproducer_ref")
        if self.kind == "upstream_fix" and not self.fix_revision:
            raise ValueError("upstream_fix evidence needs the fix_revision")
        if self.kind in {"human_label", "judge_label"} and not self.label_source:
            raise ValueError(f"{self.kind} evidence needs a label_source")
        if self.kind in {"construction", "static_finding"} and not (self.note or self.url):
            raise ValueError(f"{self.kind} evidence needs a note or url saying how")
        return self


class Finding(ContractModel):
    """A claim that one pinned task version is broken."""

    schema_version: Literal["failure-library/v1"] = SCHEMA_VERSION
    finding_id: str = Field(pattern=r"^fl-[0-9a-f]{16}$")
    collection: str = Field(min_length=1, pattern=_SLUG)
    locator: str = Field(min_length=1)
    task: TaskRef
    defect_class: DefectClass
    direction: Direction
    summary: str = Field(min_length=1, max_length=1000)
    evidence: list[Evidence] = Field(min_length=1)
    tier: Tier
    finder: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    observed_at: date
    status_now: Status = "unknown"
    fixed_in_revision: str | None = None
    split: Split = "unassigned"
    license: str | None = None

    @model_validator(mode="after")
    def _provenance(self) -> Finding:
        revision = self.task.upstream_revision
        if not revision:
            raise ValueError("a finding must pin task.upstream_revision (tasks change upstream)")
        expected_tier = derive_tier(item.kind for item in self.evidence)
        if self.tier != expected_tier:
            raise ValueError(f"tier {self.tier} disagrees with evidence; expected {expected_tier}")
        if self.status_now == "fixed_upstream" and not self.fixed_in_revision:
            raise ValueError("status_now=fixed_upstream needs fixed_in_revision")
        expected_id = make_finding_id(
            self.task.source_id, self.task.task_id, revision, self.defect_class, self.locator
        )
        if self.finding_id != expected_id:
            raise ValueError(
                f"finding_id {self.finding_id} is not the deterministic id {expected_id}"
            )
        return self


class RunLabel(ContractModel):
    """A source-native label on a run, with who or what produced it."""

    label: str = Field(min_length=1)
    rs_code: RunCode | None = None
    kind: LabelKind
    source: str = Field(min_length=1)


class RunRecord(ContractModel):
    """One agent attempt worth learning from: hacked, failed, or a clean control."""

    schema_version: Literal["failure-library/v1"] = SCHEMA_VERSION
    run_id: str = Field(min_length=1)
    collection: str = Field(min_length=1, pattern=_SLUG)
    task: TaskRef
    model: str | None = None
    harness: str | None = None
    reward: float | None = None
    outcome: Outcome
    labels: list[RunLabel] = Field(default_factory=list)
    transcript_ref: str = Field(min_length=1)
    split: Split = "unassigned"
    license: str | None = None
    finding_ids: list[str] = Field(default_factory=list)
