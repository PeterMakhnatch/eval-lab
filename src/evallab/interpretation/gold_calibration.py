"""Three-rater gold-set calibration contracts, readiness, and precision helpers.

This module feeds ``HumanBaselineReport`` (calibration-report-v1.1). It does not
compute or replace that report. Label population is blocked pending real
qualified human raters plus Peter's explicit approval. Machine outputs are not
raters. Acceptance thresholds are declared but unset; unset never evaluates as
a pass.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from evallab.interpretation.trajectory_judgment import (
    TRAJECTORY_ONTOLOGY_V1_CLASSES,
    Digest,
    canonical_json_digest,
)
from evallab.schemas import ContractModel

ONTOLOGY_VERSION = "traj.judge.ontology.v1"
RATING_RECORD_SCHEMA_VERSION = "gold-rating-record-v1"
DECLARED_CATEGORY_UNIVERSE = 12
REQUIRED_INDEPENDENT_RATERS = 3
LABEL_POPULATION_STATUS = "BLOCKED_PENDING_REAL_RATERS_AND_APPROVAL"
MIN_CLUSTERS_FLOOR = 20
LABEL_POPULATION_BLOCK_REASON = (
    "label population blocked pending real qualified human raters and Peter's explicit approval"
)
AUTHORIZATION_BOUNDARY = (
    "AUTHORIZATION BOUNDARY: Label population is BLOCKED pending real qualified "
    "human raters plus Peter's explicit approval. This template carries zero labels. "
    "Machine or model output is not a rater. Do not populate class_id without authorization."
)

if len(TRAJECTORY_ONTOLOGY_V1_CLASSES) != DECLARED_CATEGORY_UNIVERSE:
    raise RuntimeError("declared Gwet category universe must match the frozen ontology size")


class GoldItemRef(ContractModel):
    """Immutable reference to one gold item. Item selection itself is Analyst-owned."""

    item_id: str
    source_trial_id: str
    step_index: int
    source_sha256: Digest
    selection_stratum: str
    sampling_weight: float = Field(gt=0)
    evidence_start_step: int
    evidence_end_step: int
    redaction_digest: Digest


class GoldCorpusLock(ContractModel):
    corpus_id: str
    corpus_version: str
    ontology_version: Literal["traj.judge.ontology.v1"] = ONTOLOGY_VERSION
    rubric_digest: Digest
    item_count: int
    strata_counts: dict[str, int]
    item_digests: list[Digest]
    corpus_digest: Digest
    frozen_at: datetime

    @model_validator(mode="after")
    def validate_lock_identity(self) -> GoldCorpusLock:
        if self.item_count != len(self.item_digests):
            raise ValueError("item_count must equal len(item_digests)")
        expected = canonical_json_digest([self.item_digests, self.rubric_digest])
        if self.corpus_digest != expected:
            raise ValueError(
                "corpus_digest must equal canonical_json_digest([ordered item_digests, rubric_digest])"
            )
        return self


class RaterQualification(ContractModel):
    rater_id: str
    is_machine: Literal[False] = False
    qualification_status: Literal["qualified", "provisional", "revoked"]
    quiz_digest: Digest
    quiz_items_attempted: int = Field(ge=0)
    quiz_items_correct: int = Field(ge=0)
    qualified_at: datetime | None = None
    independence_group: str
    declared_conflicts: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_qualification_contract(self) -> RaterQualification:
        if self.quiz_items_correct > self.quiz_items_attempted:
            raise ValueError("quiz_items_correct cannot exceed quiz_items_attempted")
        if self.qualification_status == "qualified" and self.qualified_at is None:
            raise ValueError("qualified_at is required when qualification_status is qualified")
        if self.qualification_status != "qualified" and self.qualified_at is not None:
            raise ValueError("qualified_at must be null unless qualification_status is qualified")
        return self


class RatingRecord(ContractModel):
    record_schema_version: Literal["gold-rating-record-v1"] = RATING_RECORD_SCHEMA_VERSION
    corpus_digest: Digest
    rubric_digest: Digest
    item_id: str
    rater_id: str
    blinded: Literal[True] = True
    class_id: str | None = None
    missing_reason: (
        Literal[
            "insufficient_evidence",
            "out_of_scope",
            "rater_unavailable",
            "abstained",
        ]
        | None
    ) = None
    confidence: Literal["low", "medium", "high"] | None = None
    evidence_step_ids: list[int] = Field(default_factory=list)
    rated_at: datetime

    @model_validator(mode="after")
    def validate_exactly_one_label(self) -> RatingRecord:
        class_set = self.class_id is not None
        missing_set = self.missing_reason is not None
        if class_set == missing_set:
            raise ValueError("exactly one of class_id and missing_reason must be non-null")
        if self.class_id is not None and self.class_id not in TRAJECTORY_ONTOLOGY_V1_CLASSES:
            raise ValueError("class_id is not in the frozen trajectory ontology v1")
        return self


class ItemReadiness(ContractModel):
    item_id: str
    distinct_qualified_rater_ids: list[str]
    n_qualified_independent_raters: int
    duplicate_rater_ids: list[str]
    missing_rating_rater_ids: list[str]
    status: Literal["READY", "NOT_READY"]
    not_ready_reasons: list[str]


class PrecisionPlan(ContractModel):
    primary_statistic: Literal["gwet_ac1_multirater"] = "gwet_ac1_multirater"
    declared_category_universe: Literal[12] = DECLARED_CATEGORY_UNIVERSE
    target_ci_half_width: float = Field(gt=0)
    bootstrap_unit: Literal["source_trial_cluster"] = "source_trial_cluster"
    bootstrap_resamples: int = Field(ge=1)
    measured_within_trial_icc: float | None = None
    n_items_required: int | None = None
    n_clusters_available: int | None = None
    n_clusters_effective: float | None = Field(default=None, gt=0)
    min_clusters_floor: int = Field(default=MIN_CLUSTERS_FLOOR, ge=1)
    feasibility: Literal[
        "FEASIBLE",
        "INFEASIBLE",
        "INFEASIBLE_INSUFFICIENT_CLUSTERS",
        "UNDETERMINED_PENDING_ICC",
    ]
    acceptance_threshold: None = None

    @field_validator("acceptance_threshold")
    @classmethod
    def reject_set_acceptance_threshold(cls, value: object) -> None:
        if value is not None:
            raise ValueError(
                "acceptance_threshold is declared but unset; unset never evaluates as pass"
            )
        return None

    @model_validator(mode="after")
    def validate_icc_feasibility(self) -> PrecisionPlan:
        breaches_floor = (
            self.n_clusters_effective is not None
            and self.n_clusters_effective < self.min_clusters_floor
        )
        if self.feasibility == "INFEASIBLE_INSUFFICIENT_CLUSTERS":
            if self.n_clusters_effective is None:
                raise ValueError(
                    "INFEASIBLE_INSUFFICIENT_CLUSTERS requires a measured n_clusters_effective"
                )
            return self
        if (
            self.measured_within_trial_icc is None
            and not breaches_floor
            and self.feasibility != "UNDETERMINED_PENDING_ICC"
        ):
            raise ValueError(
                "feasibility must be UNDETERMINED_PENDING_ICC when measured_within_trial_icc is None"
            )
        return self


class GoldSetReadinessReport(ContractModel):
    corpus_digest: Digest
    rubric_digest: Digest
    required_independent_raters: int = Field(default=REQUIRED_INDEPENDENT_RATERS, ge=3)
    per_item: list[ItemReadiness]
    n_items_ready: int
    n_items_not_ready: int
    precision_plan: PrecisionPlan
    status: Literal["READY", "NOT_READY"]
    label_population_status: Literal["BLOCKED_PENDING_REAL_RATERS_AND_APPROVAL"] = (
        LABEL_POPULATION_STATUS
    )
    blocked_reasons: list[str]


class AdjudicationRecord(ContractModel):
    item_id: str
    adjudicator_rater_id: str | None
    original_rater_ids: list[str]
    resolved_class_id: str | None
    outcome: Literal["majority_no_adjudication", "adjudicated", "unresolved_hold"]
    adjudicated_at: datetime

    @model_validator(mode="after")
    def validate_adjudicator_not_original(self) -> AdjudicationRecord:
        if (
            self.adjudicator_rater_id is not None
            and self.adjudicator_rater_id in self.original_rater_ids
        ):
            raise ValueError("adjudicator_rater_id must not be in original_rater_ids")
        if (
            self.resolved_class_id is not None
            and self.resolved_class_id not in TRAJECTORY_ONTOLOGY_V1_CLASSES
        ):
            raise ValueError("resolved_class_id is not in the frozen trajectory ontology v1")
        return self


def freeze_corpus(
    items: Sequence[GoldItemRef],
    rubric_digest: Digest,
    corpus_id: str,
    corpus_version: str,
    frozen_at: datetime,
) -> GoldCorpusLock:
    """Lock a gold corpus. Identical items in any order yield an identical digest."""
    ordered = sorted(items, key=lambda item: item.item_id)
    item_ids = [item.item_id for item in ordered]
    if len(item_ids) != len(set(item_ids)):
        raise ValueError("gold items must have unique item_id values")
    item_digests = [canonical_json_digest(item) for item in ordered]
    return GoldCorpusLock(
        corpus_id=corpus_id,
        corpus_version=corpus_version,
        ontology_version=ONTOLOGY_VERSION,
        rubric_digest=rubric_digest,
        item_count=len(item_digests),
        strata_counts=dict(
            sorted(Counter(item.selection_stratum for item in ordered).items())
        ),
        item_digests=item_digests,
        corpus_digest=canonical_json_digest([item_digests, rubric_digest]),
        frozen_at=frozen_at,
    )


def evaluate_item_readiness(
    item_id: str,
    ratings: Sequence[RatingRecord],
    qualifications: Sequence[RaterQualification],
) -> ItemReadiness:
    """Apply the three-rater readiness rule to one item."""
    item_ratings = [rating for rating in ratings if rating.item_id == item_id]
    rater_counts = Counter(rating.rater_id for rating in item_ratings)
    duplicate_rater_ids = sorted(rater_id for rater_id, n in rater_counts.items() if n > 1)
    qualifications_by_id = {qual.rater_id: qual for qual in qualifications}
    missing_rating_rater_ids = sorted(
        {rating.rater_id for rating in item_ratings if rating.missing_reason is not None}
    )

    counted_ids: list[str] = []
    counted_groups: set[str] = set()
    seen: set[str] = set()
    for rating in item_ratings:
        if rating.rater_id in seen:
            continue
        seen.add(rating.rater_id)
        if rating.class_id is None:
            continue
        qualification = qualifications_by_id.get(rating.rater_id)
        if qualification is None or qualification.qualification_status != "qualified":
            continue
        counted_ids.append(rating.rater_id)
        counted_groups.add(qualification.independence_group)

    distinct_qualified_rater_ids = sorted(counted_ids)
    reasons: list[str] = []
    if duplicate_rater_ids:
        reasons.append("duplicate (item_id, rater_id) pairs are present")
    if len(distinct_qualified_rater_ids) < REQUIRED_INDEPENDENT_RATERS:
        reasons.append(
            "fewer than 3 distinct qualified human raters with a non-null class_id"
        )
    if len(counted_groups) < REQUIRED_INDEPENDENT_RATERS:
        reasons.append(
            "qualified raters span fewer than 3 distinct independence_group values"
        )
    return ItemReadiness(
        item_id=item_id,
        distinct_qualified_rater_ids=distinct_qualified_rater_ids,
        n_qualified_independent_raters=len(distinct_qualified_rater_ids),
        duplicate_rater_ids=duplicate_rater_ids,
        missing_rating_rater_ids=missing_rating_rater_ids,
        status="READY" if not reasons else "NOT_READY",
        not_ready_reasons=reasons,
    )


def evaluate_gold_set_readiness(
    lock: GoldCorpusLock,
    ratings: Sequence[RatingRecord],
    qualifications: Sequence[RaterQualification],
    precision_plan: PrecisionPlan,
) -> GoldSetReadinessReport:
    """Corpus-level readiness. Label population stays blocked in this PR."""
    matched = [rating for rating in ratings if rating.corpus_digest == lock.corpus_digest]
    item_ids = sorted({rating.item_id for rating in matched})
    per_item = [
        evaluate_item_readiness(item_id, matched, qualifications) for item_id in item_ids
    ]
    n_items_ready = sum(1 for item in per_item if item.status == "READY")
    n_items_not_ready = len(per_item) - n_items_ready
    items_ready = (
        lock.item_count > 0
        and n_items_not_ready == 0
        and n_items_ready == lock.item_count
        and all(item.status == "READY" for item in per_item)
    )
    status: Literal["READY", "NOT_READY"] = "READY" if items_ready else "NOT_READY"
    blocked_reasons = [LABEL_POPULATION_BLOCK_REASON]
    extras: list[str] = []
    if len(per_item) != lock.item_count:
        extras.append("rated item count does not match frozen corpus item_count")
    extras.extend(
        f"{item.item_id}: {reason}"
        for item in per_item
        for reason in item.not_ready_reasons
    )
    if status == "NOT_READY":
        blocked_reasons.extend(sorted(extras))
    return GoldSetReadinessReport(
        corpus_digest=lock.corpus_digest,
        rubric_digest=lock.rubric_digest,
        required_independent_raters=REQUIRED_INDEPENDENT_RATERS,
        per_item=per_item,
        n_items_ready=n_items_ready,
        n_items_not_ready=n_items_not_ready,
        precision_plan=precision_plan,
        status=status,
        label_population_status=LABEL_POPULATION_STATUS,
        blocked_reasons=blocked_reasons,
    )


def adjudicate_item(
    item_id: str,
    ratings: Sequence[RatingRecord],
    adjudicator_qualification: RaterQualification | None = None,
) -> AdjudicationRecord:
    """Majority, eligible-adjudicator slot, or unresolved hold. Never invents a class."""
    item_ratings = [rating for rating in ratings if rating.item_id == item_id]
    original_rater_ids = sorted({rating.rater_id for rating in item_ratings})
    if (
        adjudicator_qualification is not None
        and adjudicator_qualification.rater_id in original_rater_ids
    ):
        raise ValueError("adjudicator_rater_id must not be in original_rater_ids")

    votes: dict[str, str] = {}
    for rating in item_ratings:
        if rating.class_id is not None:
            votes[rating.rater_id] = rating.class_id
    tallies = Counter(votes.values())
    n_labeled = sum(tallies.values())
    majority_class: str | None = None
    if n_labeled:
        _winner, win_count = max(tallies.items(), key=lambda item: (item[1], item[0]))
        if win_count > n_labeled / 2:
            majority_class = _winner

    adjudicated_at = datetime.now(UTC)
    if majority_class is not None:
        return AdjudicationRecord(
            item_id=item_id,
            adjudicator_rater_id=None,
            original_rater_ids=original_rater_ids,
            resolved_class_id=majority_class,
            outcome="majority_no_adjudication",
            adjudicated_at=adjudicated_at,
        )

    eligible = (
        adjudicator_qualification is not None
        and adjudicator_qualification.qualification_status == "qualified"
    )
    if eligible and adjudicator_qualification is not None:
        return AdjudicationRecord(
            item_id=item_id,
            adjudicator_rater_id=adjudicator_qualification.rater_id,
            original_rater_ids=original_rater_ids,
            resolved_class_id=None,
            outcome="adjudicated",
            adjudicated_at=adjudicated_at,
        )
    return AdjudicationRecord(
        item_id=item_id,
        adjudicator_rater_id=None,
        original_rater_ids=original_rater_ids,
        resolved_class_id=None,
        outcome="unresolved_hold",
        adjudicated_at=adjudicated_at,
    )


def required_n_eff_ceiling(n_clusters: int, icc: float) -> float:
    """Effective-sample-size ceiling under cluster sampling: ``n_eff <= K / ICC``."""
    if icc <= 0:
        raise ValueError("icc must be positive")
    return n_clusters / icc


def effective_clusters_kish(cluster_sizes: Sequence[int]) -> float:
    """Kish effective cluster count: ``(sum m)^2 / sum(m^2)``.

    Equal cluster sizes return the nominal count; concentration lowers it. This is
    the single canonical implementation so unequal-cluster inflation is never
    recomputed inline.
    """
    if not cluster_sizes:
        raise ValueError("cluster_sizes must be non-empty")
    if any(size <= 0 for size in cluster_sizes):
        raise ValueError("cluster sizes must be positive")
    total = sum(cluster_sizes)
    return (total * total) / sum(size * size for size in cluster_sizes)


def assess_feasibility(plan: PrecisionPlan) -> PrecisionPlan:
    """Copy ``plan`` with feasibility set by strict precedence.

    Order is the contract. A measured effective-cluster floor breach is
    ICC-independent, so it outranks the pending-ICC branch: no ICC pilot can
    rescue a corpus that has too few independent clusters to bootstrap over.
    """
    if (
        plan.n_clusters_effective is not None
        and plan.n_clusters_effective < plan.min_clusters_floor
    ):
        return plan.model_copy(
            update={"feasibility": "INFEASIBLE_INSUFFICIENT_CLUSTERS"}
        )
    if plan.measured_within_trial_icc is None:
        return plan.model_copy(update={"feasibility": "UNDETERMINED_PENDING_ICC"})
    if plan.n_clusters_available is None or plan.n_items_required is None:
        raise ValueError(
            "n_clusters_available and n_items_required are required when ICC is measured"
        )
    ceiling = required_n_eff_ceiling(
        plan.n_clusters_available, plan.measured_within_trial_icc
    )
    feasibility = "FEASIBLE" if ceiling >= plan.n_items_required else "INFEASIBLE"
    return plan.model_copy(update={"feasibility": feasibility})


def gwet_ac1_chance_agreement(class_proportions: Sequence[float], q: int) -> float:
    """Gwet AC1 chance-agreement with declared universe ``q``.

    ``p_e = (1 / (q - 1)) * sum_k pi_k (1 - pi_k)``. The denominator is ``q - 1``,
    never ``q``. ``q`` is the declared category universe, not the count of observed
    labels.
    """
    if q < 2:
        raise ValueError("declared category universe q must be at least 2")
    return (1.0 / (q - 1)) * sum(pi * (1.0 - pi) for pi in class_proportions)


def empty_rating_record_template() -> dict[str, object]:
    """Keys-only rating template. Every class_id is null; zero labels."""
    return {
        "record_schema_version": RATING_RECORD_SCHEMA_VERSION,
        "corpus_digest": "<CORPUS_DIGEST>",
        "rubric_digest": "<RUBRIC_DIGEST>",
        "item_id": "<ITEM_ID>",
        "rater_id": "<RATER_ID>",
        "blinded": True,
        "class_id": None,
        "missing_reason": None,
        "confidence": None,
        "evidence_step_ids": [],
        "rated_at": None,
    }


def empty_rater_qualification_template() -> dict[str, object]:
    """Keys-only qualification template. Machine raters are structurally excluded."""
    return {
        "rater_id": "<RATER_ID>",
        "is_machine": False,
        "qualification_status": "provisional",
        "quiz_digest": "<QUIZ_DIGEST>",
        "quiz_items_attempted": 0,
        "quiz_items_correct": 0,
        "qualified_at": None,
        "independence_group": "<INDEPENDENCE_GROUP>",
        "declared_conflicts": [],
    }


def render_rating_record_template() -> str:
    payload = json.dumps(empty_rating_record_template(), sort_keys=True)
    return f"# {AUTHORIZATION_BOUNDARY}\n{payload}\n"


def render_rater_qualification_template() -> str:
    payload = json.dumps(empty_rater_qualification_template(), indent=2, sort_keys=True)
    return f"# {AUTHORIZATION_BOUNDARY}\n{payload}\n"
