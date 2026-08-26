"""Fail-closed Platform serializers for frozen CalibrationReport versions.

V1 is the permanent bootstrap report embedded by PR #189. V1.1 is Track B's
three-rater successor contract; its current preregistration remains HOLD-only.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import Field
from pydantic.experimental.missing_sentinel import MISSING

from evallab.schemas import ContractModel
from evallab.trajectory_judgment import (
    SHA256_PATTERN,
    TRAJECTORY_ONTOLOGY_V1_CLASSES,
)

BOOTSTRAP_CALIBRATION_SCHEMA = "calibration-report-v1"
HUMAN_CALIBRATION_SCHEMA = "calibration-report-v1.1"
Probability = Annotated[float, Field(ge=0, le=1)]
AgreementScore = Annotated[float, Field(ge=-1, le=1)]


class UnsupportedCalibrationVersion(ValueError):
    """Raised for reports outside the two reviewed frozen schemas."""


class InterRaterReport(ContractModel):
    """Compatibility metrics only; never sufficient to unlock acceptance."""
    n_paired: int = Field(ge=0)
    cohen_kappa: float | None
    gwet_ac1: float | None
    observed_agreement: float | None
    kappa_min: float
    alt_test_min: float
    floor_pass: bool


class RiskCoveragePoint(ContractModel):
    coverage: float
    risk: float | None
    n: int


class GlobalCalibrationMetrics(ContractModel):
    raw_judge_accuracy: float | None
    proposed_accept_precision: float | None
    coverage: float
    selective_risk: float | None
    ece: float | None
    brier: float | None
    aurc: float | None
    risk_coverage: list[RiskCoveragePoint]
    abstention_precision: float | None
    abstention_justified_rate: float | None
    cite_valid_on_proposed_accept: float | None
    cross_judge_agreement: float | None
    cross_judge_is_not_gold: Literal[True]


class ClassCalibrationRow(ContractModel):
    acceptance_enabled: Literal[False]
    delta: float
    n_gold: int = Field(ge=0)
    n_proposed_accept: int = Field(ge=0)
    prec_acc: float | None
    p_human: float | None
    wilson_lower_one_sided_95: float | None
    beta_lower_one_sided_95: float | None
    ci_width: float | None
    noninferiority_pass: bool
    hold_reasons: list[str] = Field(min_length=1)
    n_clusters: int | MISSING = Field(default=MISSING, ge=0)
    rec_acc: float | None | MISSING = MISSING
    margin: float | None | MISSING = MISSING
    clustered_lower_one_sided_95: float | None | MISSING = MISSING
    cite_valid: float | None | MISSING = MISSING
    false_accept: int | MISSING = Field(default=MISSING, ge=0)


class CalibrationReport(ContractModel):
    schema_name: Literal["calibration-report-v1"] = Field(
        validation_alias="schema", serialization_alias="schema"
    )
    calibration_version: str = Field(pattern=SHA256_PATTERN)
    acceptance_enabling_allowed: Literal[False]
    thresholds_digest: str = Field(pattern=SHA256_PATTERN)
    cluster_key: Literal["source_task_id"] | MISSING = MISSING
    n_items: int = Field(ge=0)
    n_proposed_accept: int = Field(ge=0)
    inter_rater: InterRaterReport
    global_metrics: GlobalCalibrationMetrics
    classes: dict[str, ClassCalibrationRow]
    hold_summary: list[str]


class PairwiseAgreement(ContractModel):
    rater_a: str
    rater_b: str
    n_items: int = Field(ge=0)
    observed_agreement: Probability | None
    cohen_kappa: AgreementScore | None


class AdjudicationCounts(ContractModel):
    majority_without_adjudication: int = Field(ge=0)
    adjudicated: int = Field(ge=0)
    unresolved_hold: int = Field(ge=0)


class HumanClassPrecision(ContractModel):
    n_predictions: int = Field(ge=0)
    true_positive: int = Field(ge=0)
    ppv: Probability | None
    ci_lower: Probability | None
    ci_upper: Probability | None


class HumanBaselineReport(ContractModel):
    status: Literal["complete", "hold"]
    n_independent_raters: int = Field(ge=3)
    blind_to_machine: Literal[True]
    individual_labels_digest: str | None = Field(pattern=SHA256_PATTERN)
    adjudication_digest: str | None = Field(pattern=SHA256_PATTERN)
    pairwise_matrix: list[PairwiseAgreement]
    krippendorff_alpha_nominal: AgreementScore | None
    fleiss_kappa_multiclass: AgreementScore | None
    gwet_ac1_multirater: AgreementScore | None
    adjudication_counts: AdjudicationCounts
    per_class_precision: dict[str, HumanClassPrecision]


class MachineCalibrationResults(ContractModel):
    judge_tuple_digest: str = Field(pattern=SHA256_PATTERN)
    n_items: int = Field(ge=0)
    raw_accuracy: Probability | None
    coverage: Probability
    accepted_precision: Probability | None
    # The frozen HOLD proposal uses null for the undefined zero-item rate.
    # Track B's schema says number; the conflict is paged on PR #189.
    abstention_rate: Probability | None
    citation_validity: Probability | None
    ece: Probability | None
    brier: Probability | None


class ClassCalibrationRowV1_1(ContractModel):
    acceptance_enabled: Literal[False]
    n_gold: int = Field(ge=0)
    n_accepted: int = Field(ge=0)
    false_accept_risk_ucb: Probability | None
    human_baseline_ppv: Probability | None
    noninferiority_margin: Probability | None
    noninferiority_pass: bool
    citation_validity: Probability | None
    hold_reasons: list[str]


class CalibrationReportV1_1(ContractModel):
    schema_name: Literal["calibration-report-v1.1"] = Field(
        validation_alias="schema", serialization_alias="schema"
    )
    calibration_version: str = Field(pattern=SHA256_PATTERN)
    acceptance_enabling_allowed: Literal[False]
    ontology_digest: str = Field(pattern=SHA256_PATTERN)
    thresholds_digest: str = Field(pattern=SHA256_PATTERN)
    split_manifest_digest: str = Field(pattern=SHA256_PATTERN)
    item_manifest_digest: str = Field(pattern=SHA256_PATTERN)
    pack_manifest_digest: str = Field(pattern=SHA256_PATTERN)
    human_baseline: HumanBaselineReport
    machine_results: MachineCalibrationResults
    classes: dict[str, ClassCalibrationRowV1_1]
    hold_summary: list[str]


CalibrationReportVersion = CalibrationReport | CalibrationReportV1_1

def _require_frozen_class_set(report: CalibrationReportVersion) -> None:
    class_ids = set(report.classes)
    if class_ids != TRAJECTORY_ONTOLOGY_V1_CLASSES:
        missing = sorted(TRAJECTORY_ONTOLOGY_V1_CLASSES - class_ids)
        unexpected = sorted(class_ids - TRAJECTORY_ONTOLOGY_V1_CLASSES)
        raise ValueError(
            f"calibration class set does not match frozen ontology; "
            f"missing={missing}, unexpected={unexpected}"
        )


def parse_calibration_report(payload: Mapping[str, Any]) -> CalibrationReportVersion:
    """Version-dispatch a frozen report without inferring calibration evidence."""
    schema = payload.get("schema")
    if schema == BOOTSTRAP_CALIBRATION_SCHEMA:
        report: CalibrationReportVersion = CalibrationReport.model_validate(dict(payload))
    elif schema == HUMAN_CALIBRATION_SCHEMA:
        report = CalibrationReportV1_1.model_validate(dict(payload))
    else:
        raise UnsupportedCalibrationVersion(
            f"unsupported calibration report schema: {schema!r}"
        )
    _require_frozen_class_set(report)
    return report


def calibration_report_can_enable_acceptance(
    report: CalibrationReportVersion,
) -> Literal[False]:
    """Neither frozen version may unlock acceptance."""
    if report.acceptance_enabling_allowed is not False:
        raise AssertionError("CalibrationReport enablement invariant violated")
    return False
