"""In-memory contract tests for gold-set calibration. No labels are written to disk."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from evallab.interpretation.gold_calibration import (
    DECLARED_CATEGORY_UNIVERSE,
    LABEL_POPULATION_STATUS,
    GoldCorpusLock,
    GoldItemRef,
    PrecisionPlan,
    RaterQualification,
    RatingRecord,
    adjudicate_item,
    assess_feasibility,
    cluster_sizes_by_logical_trial,
    detect_duplicate_items,
    effective_clusters_kish,
    evaluate_gold_set_readiness,
    evaluate_item_readiness,
    freeze_corpus,
    gwet_ac1_chance_agreement,
    intake_ratings,
    n_logical_trials,
    required_n_eff_ceiling,
)
from evallab.interpretation.trajectory_judgment import canonical_json_digest

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
NOW = datetime(2026, 8, 28, tzinfo=UTC)
CLASS_A = "appropriate_action"
CLASS_B = "infrastructure_failure"


def _item(
    item_id: str,
    *,
    stratum: str = "featured",
    trial: str = "trial-1",
    logical_trial_id: str | None = None,
    step_index: int | None = None,
    source_sha256: str = DIGEST_A,
) -> GoldItemRef:
    if step_index is None:
        step_index = 4 + sum(bytearray(item_id.encode("utf-8")))
    return GoldItemRef(
        item_id=item_id,
        source_trial_id=trial,
        logical_trial_id=trial if logical_trial_id is None else logical_trial_id,
        step_index=step_index,
        source_sha256=source_sha256,
        selection_stratum=stratum,
        sampling_weight=1.0,
        evidence_start_step=1,
        evidence_end_step=8,
        redaction_digest=DIGEST_B,
    )


def _qual(
    rater_id: str,
    *,
    group: str,
    status: str = "qualified",
    qualified_at: datetime | None = NOW,
) -> RaterQualification:
    if status != "qualified":
        qualified_at = None
    return RaterQualification(
        rater_id=rater_id,
        is_machine=False,
        qualification_status=status,
        quiz_digest=DIGEST_C,
        quiz_items_attempted=10,
        quiz_items_correct=9,
        qualified_at=qualified_at,
        independence_group=group,
        declared_conflicts=[],
    )


def _rating(
    item_id: str,
    rater_id: str,
    *,
    corpus_digest: str,
    class_id: str | None = CLASS_A,
    missing_reason: str | None = None,
) -> RatingRecord:
    return RatingRecord(
        record_schema_version="gold-rating-record-v1",
        corpus_digest=corpus_digest,
        rubric_digest=DIGEST_B,
        item_id=item_id,
        rater_id=rater_id,
        blinded=True,
        class_id=class_id,
        missing_reason=missing_reason,
        confidence="high" if class_id is not None else None,
        evidence_step_ids=[1, 2, 3],
        rated_at=NOW,
    )


def _plan(**overrides: object) -> PrecisionPlan:
    payload: dict[str, object] = {
        "primary_statistic": "gwet_ac1_multirater",
        "declared_category_universe": 12,
        "target_ci_half_width": 0.05,
        "bootstrap_unit": "source_trial_cluster",
        "bootstrap_resamples": 1000,
        "measured_within_trial_icc": None,
        "n_items_required": 96,
        "n_clusters_available": 23,
        "n_clusters_effective": None,
        "min_clusters_floor": 20,
        "feasibility": "UNDETERMINED_PENDING_ICC",
        "acceptance_threshold": None,
    }
    payload.update(overrides)
    return PrecisionPlan.model_validate(payload)


def _three_groups() -> list[RaterQualification]:
    return [
        _qual("r1", group="lab-a"),
        _qual("r2", group="lab-b"),
        _qual("r3", group="lab-c"),
    ]


def test_two_qualified_independent_raters_not_ready() -> None:
    lock = freeze_corpus([_item("item-1")], DIGEST_B, "corpus", "v1", NOW)
    ratings = [
        _rating("item-1", "r1", corpus_digest=lock.corpus_digest),
        _rating("item-1", "r2", corpus_digest=lock.corpus_digest),
    ]
    quals = [_qual("r1", group="lab-a"), _qual("r2", group="lab-b")]
    readiness = evaluate_item_readiness("item-1", ratings, quals)
    assert readiness.status == "NOT_READY"
    assert readiness.n_qualified_independent_raters == 2


def test_exactly_three_qualified_independent_raters_ready() -> None:
    lock = freeze_corpus([_item("item-1")], DIGEST_B, "corpus", "v1", NOW)
    quals = _three_groups()
    ratings = [
        _rating("item-1", rater_id, corpus_digest=lock.corpus_digest)
        for rater_id in ("r1", "r2", "r3")
    ]
    readiness = evaluate_item_readiness("item-1", ratings, quals)
    assert readiness.status == "READY"
    assert readiness.distinct_qualified_rater_ids == ["r1", "r2", "r3"]
    report = evaluate_gold_set_readiness(lock, ratings, quals, _plan())
    assert report.status == "READY"
    assert report.n_items_ready == 1
    assert report.n_items_not_ready == 0


def test_duplicate_rater_id_not_ready_and_recorded() -> None:
    lock = freeze_corpus([_item("item-1")], DIGEST_B, "corpus", "v1", NOW)
    quals = _three_groups()
    ratings = [
        _rating("item-1", "r1", corpus_digest=lock.corpus_digest),
        _rating("item-1", "r1", corpus_digest=lock.corpus_digest),
        _rating("item-1", "r2", corpus_digest=lock.corpus_digest),
    ]
    readiness = evaluate_item_readiness("item-1", ratings, quals)
    assert readiness.status == "NOT_READY"
    assert readiness.duplicate_rater_ids == ["r1"]
    assert "r1" in readiness.distinct_qualified_rater_ids
    assert "r2" in readiness.distinct_qualified_rater_ids


def test_shared_independence_group_not_ready() -> None:
    lock = freeze_corpus([_item("item-1")], DIGEST_B, "corpus", "v1", NOW)
    quals = [
        _qual("r1", group="same-lab"),
        _qual("r2", group="same-lab"),
        _qual("r3", group="same-lab"),
    ]
    ratings = [
        _rating("item-1", rater_id, corpus_digest=lock.corpus_digest)
        for rater_id in ("r1", "r2", "r3")
    ]
    readiness = evaluate_item_readiness("item-1", ratings, quals)
    assert readiness.status == "NOT_READY"
    assert readiness.distinct_qualified_rater_ids == ["r1", "r2", "r3"]
    assert any("independence_group" in reason for reason in readiness.not_ready_reasons)


def test_missing_reason_does_not_count() -> None:
    lock = freeze_corpus([_item("item-1")], DIGEST_B, "corpus", "v1", NOW)
    quals = _three_groups()
    ratings = [
        _rating("item-1", "r1", corpus_digest=lock.corpus_digest),
        _rating("item-1", "r2", corpus_digest=lock.corpus_digest),
        _rating(
            "item-1",
            "r3",
            corpus_digest=lock.corpus_digest,
            class_id=None,
            missing_reason="insufficient_evidence",
        ),
    ]
    readiness = evaluate_item_readiness("item-1", ratings, quals)
    assert readiness.status == "NOT_READY"
    assert readiness.missing_rating_rater_ids == ["r3"]
    assert "r3" not in readiness.distinct_qualified_rater_ids


def test_non_qualified_rater_does_not_count() -> None:
    lock = freeze_corpus([_item("item-1")], DIGEST_B, "corpus", "v1", NOW)
    quals = [
        _qual("r1", group="lab-a"),
        _qual("r2", group="lab-b"),
        _qual("r3", group="lab-c", status="provisional"),
    ]
    ratings = [
        _rating("item-1", rater_id, corpus_digest=lock.corpus_digest)
        for rater_id in ("r1", "r2", "r3")
    ]
    readiness = evaluate_item_readiness("item-1", ratings, quals)
    assert readiness.status == "NOT_READY"
    assert readiness.distinct_qualified_rater_ids == ["r1", "r2"]


def test_is_machine_true_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        RaterQualification(
            rater_id="model-1",
            is_machine=True,
            qualification_status="qualified",
            quiz_digest=DIGEST_C,
            quiz_items_attempted=10,
            quiz_items_correct=10,
            qualified_at=NOW,
            independence_group="machines",
            declared_conflicts=[],
        )


def test_class_id_outside_ontology_raises() -> None:
    with pytest.raises(ValidationError):
        RatingRecord(
            corpus_digest=DIGEST_A,
            rubric_digest=DIGEST_B,
            item_id="item-1",
            rater_id="r1",
            class_id="not_a_frozen_ontology_class",
            missing_reason=None,
            rated_at=NOW,
        )


def test_class_id_and_missing_reason_xor() -> None:
    with pytest.raises(ValidationError):
        RatingRecord(
            corpus_digest=DIGEST_A,
            rubric_digest=DIGEST_B,
            item_id="item-1",
            rater_id="r1",
            class_id=None,
            missing_reason=None,
            rated_at=NOW,
        )
    with pytest.raises(ValidationError):
        RatingRecord(
            corpus_digest=DIGEST_A,
            rubric_digest=DIGEST_B,
            item_id="item-1",
            rater_id="r1",
            class_id=CLASS_A,
            missing_reason="abstained",
            rated_at=NOW,
        )


def test_freeze_corpus_deterministic_and_order_insensitive() -> None:
    items = [_item("item-b", stratum="s2"), _item("item-a", stratum="s1")]
    lock_forward = freeze_corpus(items, DIGEST_B, "corpus", "v1", NOW)
    lock_reversed = freeze_corpus(list(reversed(items)), DIGEST_B, "corpus", "v1", NOW)
    assert lock_forward.corpus_digest == lock_reversed.corpus_digest
    assert lock_forward.item_digests == lock_reversed.item_digests
    assert lock_forward.item_digests == [
        canonical_json_digest(_item("item-a", stratum="s1")),
        canonical_json_digest(_item("item-b", stratum="s2")),
    ]
    assert lock_forward.item_count == 2
    assert lock_forward.corpus_digest == canonical_json_digest(
        [lock_forward.item_digests, DIGEST_B]
    )
    assert lock_forward.strata_counts == {"s1": 1, "s2": 1}


def test_adjudication_tie_without_eligible_adjudicator_is_unresolved_hold() -> None:
    lock = freeze_corpus([_item("item-1")], DIGEST_B, "corpus", "v1", NOW)
    ratings = [
        _rating("item-1", "r1", corpus_digest=lock.corpus_digest, class_id=CLASS_A),
        _rating("item-1", "r2", corpus_digest=lock.corpus_digest, class_id=CLASS_B),
    ]
    record = adjudicate_item("item-1", ratings)
    assert record.outcome == "unresolved_hold"
    assert record.resolved_class_id is None


def test_adjudicator_from_original_rater_ids_raises() -> None:
    lock = freeze_corpus([_item("item-1")], DIGEST_B, "corpus", "v1", NOW)
    ratings = [
        _rating("item-1", "r1", corpus_digest=lock.corpus_digest, class_id=CLASS_A),
        _rating("item-1", "r2", corpus_digest=lock.corpus_digest, class_id=CLASS_B),
    ]
    with pytest.raises(ValueError, match="original_rater_ids"):
        adjudicate_item("item-1", ratings, adjudicator_qualification=_qual("r1", group="lab-a"))


def test_label_population_status_blocked_when_items_ready() -> None:
    lock = freeze_corpus([_item("item-1")], DIGEST_B, "corpus", "v1", NOW)
    quals = _three_groups()
    ratings = [
        _rating("item-1", rater_id, corpus_digest=lock.corpus_digest)
        for rater_id in ("r1", "r2", "r3")
    ]
    report = evaluate_gold_set_readiness(lock, ratings, quals, _plan())
    assert report.status == "READY"
    assert report.label_population_status == LABEL_POPULATION_STATUS
    assert report.label_population_status == "BLOCKED_PENDING_REAL_RATERS_AND_APPROVAL"


def test_precision_plan_none_icc_forces_undetermined() -> None:
    plan = _plan(measured_within_trial_icc=None, feasibility="UNDETERMINED_PENDING_ICC")
    assert plan.feasibility == "UNDETERMINED_PENDING_ICC"
    assessed = assess_feasibility(plan)
    assert assessed.feasibility == "UNDETERMINED_PENDING_ICC"
    with pytest.raises(ValidationError):
        _plan(measured_within_trial_icc=None, feasibility="FEASIBLE")


def test_non_none_acceptance_threshold_raises() -> None:
    with pytest.raises(ValidationError):
        _plan(acceptance_threshold=0.8)


def test_gwet_ac1_chance_agreement_divides_by_q_minus_one() -> None:
    assert gwet_ac1_chance_agreement((0.5, 0.5), 2) == 0.5
    proportions = (0.5, 0.5) + (0.0,) * 10
    assert gwet_ac1_chance_agreement(proportions, DECLARED_CATEGORY_UNIVERSE) == 0.5 / 11
    with pytest.raises(ValueError, match="at least 2"):
        gwet_ac1_chance_agreement((1.0,), 1)


def test_cluster_ceiling_arithmetic() -> None:
    assert required_n_eff_ceiling(44, 0.3) == pytest.approx(44 / 0.3)
    assert required_n_eff_ceiling(44, 0.5) == pytest.approx(88.0)
    feasible = assess_feasibility(
        _plan(
            measured_within_trial_icc=0.3,
            n_clusters_available=44,
            feasibility="UNDETERMINED_PENDING_ICC",
        )
    )
    assert feasible.feasibility == "FEASIBLE"
    infeasible = assess_feasibility(
        _plan(
            measured_within_trial_icc=0.5,
            n_clusters_available=44,
            feasibility="UNDETERMINED_PENDING_ICC",
        )
    )
    assert infeasible.feasibility == "INFEASIBLE"
    with pytest.raises(ValueError, match="positive"):
        required_n_eff_ceiling(44, 0.0)


def test_effective_clusters_kish_equal_clusters_equal_k() -> None:
    assert effective_clusters_kish([4, 4, 4, 4]) == 4.0


def test_effective_clusters_kish_unequal_clusters() -> None:
    assert effective_clusters_kish([10, 1, 1, 1]) == pytest.approx(169 / 103)


def test_effective_clusters_kish_rejects_empty_and_non_positive() -> None:
    with pytest.raises(ValueError):
        effective_clusters_kish([])
    with pytest.raises(ValueError):
        effective_clusters_kish([4, 0])
    with pytest.raises(ValueError):
        effective_clusters_kish([3, -1])


def test_kish_floor_breach_beats_undetermined_icc() -> None:
    """Measured Kish-floor breach wins even when ICC is unset. Do not reorder."""
    assessed = assess_feasibility(
        _plan(
            n_clusters_effective=19.4,
            min_clusters_floor=20,
            measured_within_trial_icc=None,
            feasibility="UNDETERMINED_PENDING_ICC",
        )
    )
    assert assessed.feasibility == "INFEASIBLE_INSUFFICIENT_CLUSTERS"
    assert assessed.feasibility != "UNDETERMINED_PENDING_ICC"


def test_kish_above_floor_with_none_icc_is_undetermined() -> None:
    assessed = assess_feasibility(
        _plan(
            n_clusters_effective=25.0,
            min_clusters_floor=20,
            measured_within_trial_icc=None,
            feasibility="UNDETERMINED_PENDING_ICC",
        )
    )
    assert assessed.feasibility == "UNDETERMINED_PENDING_ICC"


def test_measured_corpus_kish_below_floor() -> None:
    assert effective_clusters_kish([31] + [9] * 22) < 20

def test_rating_record_rater_qualification_and_lock_are_frozen() -> None:
    lock = freeze_corpus([_item("item-1")], DIGEST_B, "corpus", "v1", NOW)
    assert isinstance(lock, GoldCorpusLock)
    record = _rating("item-1", "r1", corpus_digest=lock.corpus_digest)
    qual = _qual("r1", group="lab-a")
    with pytest.raises(ValidationError):
        record.item_id = "mutated"
    with pytest.raises(ValidationError):
        qual.rater_id = "mutated"
    with pytest.raises(ValidationError):
        lock.corpus_id = "mutated"


def test_cluster_sizes_group_by_logical_trial_not_source_trial() -> None:
    items = [
        _item("item-1", trial="run-a", logical_trial_id="lt-shared", step_index=1),
        _item("item-2", trial="run-b", logical_trial_id="lt-shared", step_index=2),
    ]
    sizes = cluster_sizes_by_logical_trial(items)
    assert sizes == [2]
    assert n_logical_trials(items) == 1
    assert {item.source_trial_id for item in items} == {"run-a", "run-b"}
    assert effective_clusters_kish(sizes) == 1.0


def test_detect_duplicate_items_and_freeze_rejects_named_key() -> None:
    items = [
        _item("item-1", trial="run-a", logical_trial_id="lt-1", step_index=7),
        _item("item-2", trial="run-b", logical_trial_id="lt-1", step_index=7),
    ]
    key = ("lt-1", 7, DIGEST_A)
    assert detect_duplicate_items(items) == [key]
    with pytest.raises(ValueError) as exc:
        freeze_corpus(items, DIGEST_B, "corpus", "v1", NOW)
    message = str(exc.value)
    assert "lt-1" in message
    assert "7" in message
    assert DIGEST_A in message


def test_freeze_corpus_succeeds_and_digest_stable_when_duplicate_free() -> None:
    items = [_item("item-b", stratum="s2"), _item("item-a", stratum="s1")]
    lock_forward = freeze_corpus(items, DIGEST_B, "corpus", "v1", NOW)
    lock_reversed = freeze_corpus(list(reversed(items)), DIGEST_B, "corpus", "v1", NOW)
    assert lock_forward.corpus_digest == lock_reversed.corpus_digest
    assert lock_forward.item_ids == ["item-a", "item-b"]
    assert detect_duplicate_items(items) == []


def test_cannot_judge_does_not_count_and_is_missing() -> None:
    lock = freeze_corpus([_item("item-1")], DIGEST_B, "corpus", "v1", NOW)
    quals = _three_groups()
    ratings = [
        _rating("item-1", "r1", corpus_digest=lock.corpus_digest),
        _rating("item-1", "r2", corpus_digest=lock.corpus_digest),
        _rating(
            "item-1",
            "r3",
            corpus_digest=lock.corpus_digest,
            class_id=None,
            missing_reason="cannot_judge",
        ),
    ]
    readiness = evaluate_item_readiness("item-1", ratings, quals)
    assert readiness.status == "NOT_READY"
    assert readiness.missing_rating_rater_ids == ["r3"]
    assert "r3" not in readiness.distinct_qualified_rater_ids
    assert readiness.n_qualified_independent_raters == 2


def test_intake_ratings_rejects_mismatched_corpus_digest() -> None:
    lock = freeze_corpus([_item("item-1")], DIGEST_B, "corpus", "v1", NOW)
    raw = [_rating("item-1", "r1", corpus_digest=DIGEST_A).model_dump(mode="json")]
    with pytest.raises(ValueError, match="index 0") as exc:
        intake_ratings(raw, lock)
    assert "corpus_digest" in str(exc.value)


def test_intake_ratings_rejects_item_id_absent_from_lock() -> None:
    lock = freeze_corpus([_item("item-1")], DIGEST_B, "corpus", "v1", NOW)
    raw = [_rating("item-missing", "r1", corpus_digest=lock.corpus_digest).model_dump(mode="json")]
    with pytest.raises(ValueError, match="index 0") as exc:
        intake_ratings(raw, lock)
    assert "item-missing" in str(exc.value)


def test_intake_ratings_rejects_repeated_item_rater_pair() -> None:
    lock = freeze_corpus([_item("item-1")], DIGEST_B, "corpus", "v1", NOW)
    row = _rating("item-1", "r1", corpus_digest=lock.corpus_digest).model_dump(mode="json")
    with pytest.raises(ValueError, match="index 1") as exc:
        intake_ratings([row, row], lock)
    message = str(exc.value)
    assert "item-1" in message
    assert "r1" in message


def test_intake_ratings_rejects_class_id_outside_ontology() -> None:
    lock = freeze_corpus([_item("item-1")], DIGEST_B, "corpus", "v1", NOW)
    row = _rating("item-1", "r1", corpus_digest=lock.corpus_digest).model_dump(mode="json")
    row["class_id"] = "not_a_frozen_ontology_class"
    with pytest.raises(ValueError, match="index 0") as exc:
        intake_ratings([row], lock)
    assert "not_a_frozen_ontology_class" in str(exc.value) or "ontology" in str(exc.value)


def test_intake_ratings_returns_typed_records_for_clean_batch() -> None:
    lock = freeze_corpus([_item("item-1")], DIGEST_B, "corpus", "v1", NOW)
    raw = [
        _rating("item-1", rater_id, corpus_digest=lock.corpus_digest).model_dump(mode="json")
        for rater_id in ("r1", "r2", "r3")
    ]
    records = intake_ratings(raw, lock)
    assert [record.rater_id for record in records] == ["r1", "r2", "r3"]
    assert all(isinstance(record, RatingRecord) for record in records)
    assert all(record.class_id == CLASS_A for record in records)


def test_assess_feasibility_model_copy_works_on_frozen_plan() -> None:
    plan = _plan(
        measured_within_trial_icc=0.3,
        n_clusters_available=44,
        feasibility="UNDETERMINED_PENDING_ICC",
    )
    assessed = assess_feasibility(plan)
    assert assessed.feasibility == "FEASIBLE"
    assert plan.feasibility == "UNDETERMINED_PENDING_ICC"
    assert assessed is not plan
    with pytest.raises(ValidationError):
        assessed.feasibility = "INFEASIBLE"
