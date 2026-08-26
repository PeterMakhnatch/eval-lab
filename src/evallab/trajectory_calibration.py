"""Fail-closed Platform serializers for frozen CalibrationReport versions.

V1 is the permanent bootstrap report embedded by PR #189. V1.1 is Track B's
three-rater successor contract; its current preregistration remains HOLD-only.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_serializer, model_validator

from evallab.schemas import ContractModel
from evallab.trajectory_judgment import (
    SHA256_PATTERN,
    TRAJECTORY_ONTOLOGY_V1_CLASSES,
)

BOOTSTRAP_CALIBRATION_SCHEMA = "calibration-report-v1"
HUMAN_CALIBRATION_SCHEMA = "calibration-report-v1.1"
Probability = Annotated[float, Field(ge=0, le=1)]
AgreementScore = Annotated[float, Field(ge=-1, le=1)]

_OPTIONAL_CLASS_FIELDS = frozenset(
    {
        "n_clusters",
        "rec_acc",
        "margin",
        "clustered_lower_one_sided_95",
        "cite_valid",
        "false_accept",
    }
)
_NONNULL_OPTIONAL_CLASS_FIELDS = frozenset({"n_clusters", "false_accept"})


def _reject_explicit_nulls(data: Any, fields: frozenset[str]) -> Any:
    if isinstance(data, Mapping):
        for field in fields:
            if field in data and data[field] is None:
                raise ValueError(f"{field} cannot be null")
    return data


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
    n_clusters: int | None = Field(default=None, ge=0)
    rec_acc: float | None = None
    margin: float | None = None
    clustered_lower_one_sided_95: float | None = None
    cite_valid: float | None = None
    false_accept: int | None = Field(default=None, ge=0)

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null_optional_fields(cls, data: Any) -> Any:
        return _reject_explicit_nulls(data, _NONNULL_OPTIONAL_CLASS_FIELDS)

    @model_serializer(mode="wrap")
    def omit_absent_optional_fields(self, serializer: Any) -> dict[str, Any]:
        payload = serializer(self)
        for field in _OPTIONAL_CLASS_FIELDS:
            if field not in self.model_fields_set:
                payload.pop(field, None)
        return payload

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: Any, handler: Any
    ) -> dict[str, Any]:
        json_schema = handler(core_schema)
        properties = json_schema["properties"]
        properties["n_clusters"] = {"type": "integer", "minimum": 0}
        properties["false_accept"] = {"type": "integer", "minimum": 0}
        # The frozen v1 schema explicitly permits null for these four metrics.
        return json_schema


class CalibrationReport(ContractModel):
    schema_name: Literal["calibration-report-v1"] = Field(
        validation_alias="schema", serialization_alias="schema"
    )
    calibration_version: str = Field(pattern=SHA256_PATTERN)
    acceptance_enabling_allowed: Literal[False]
    thresholds_digest: str = Field(pattern=SHA256_PATTERN)
    cluster_key: Literal["source_task_id"] | None = None
    n_items: int = Field(ge=0)
    n_proposed_accept: int = Field(ge=0)
    inter_rater: InterRaterReport
    global_metrics: GlobalCalibrationMetrics
    classes: dict[str, ClassCalibrationRow]
    hold_summary: list[str]

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null_cluster_key(cls, data: Any) -> Any:
        return _reject_explicit_nulls(data, frozenset({"cluster_key"}))

    @model_serializer(mode="wrap")
    def omit_absent_cluster_key(self, serializer: Any) -> dict[str, Any]:
        payload = serializer(self)
        if "cluster_key" not in self.model_fields_set:
            payload.pop("cluster_key", None)
        return payload

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: Any, handler: Any
    ) -> dict[str, Any]:
        json_schema = handler(core_schema)
        json_schema["properties"]["cluster_key"] = {"const": "source_task_id"}
        return json_schema


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
    required_independent_raters: int = Field(ge=3)
    n_independent_raters: int = Field(ge=0)
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
    abstention_rate: Probability | None
    citation_validity: Probability | None
    ece: Probability | None
    brier: Probability | None

    @model_validator(mode="after")
    def validate_abstention_rate(self) -> MachineCalibrationResults:
        if self.n_items == 0:
            if self.abstention_rate is not None:
                raise ValueError("abstention_rate must be null when n_items is 0")
        elif self.abstention_rate is None:
            raise ValueError("abstention_rate is required when n_items is at least 1")
        return self


class ClassCalibrationRowV1_1(ContractModel):
    acceptance_enabled: bool
    n_gold: int = Field(ge=0)
    n_accepted: int = Field(ge=0)
    false_accept_risk_ucb: Probability | None
    human_baseline_ppv: Probability | None
    noninferiority_margin: Probability | None
    noninferiority_pass: bool
    citation_validity: Probability | None
    hold_reasons: list[str]

    @field_validator("hold_reasons")
    @classmethod
    def validate_unique_hold_reasons(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("hold_reasons must be unique")
        return values


class CalibrationReportV1_1(ContractModel):
    schema_name: Literal["calibration-report-v1.1"] = Field(
        validation_alias="schema", serialization_alias="schema"
    )
    calibration_version: str = Field(pattern=SHA256_PATTERN)
    acceptance_enabling_allowed: bool
    ontology_digest: str = Field(pattern=SHA256_PATTERN)
    thresholds_digest: str = Field(pattern=SHA256_PATTERN)
    split_manifest_digest: str = Field(pattern=SHA256_PATTERN)
    item_manifest_digest: str = Field(pattern=SHA256_PATTERN)
    pack_manifest_digest: str = Field(pattern=SHA256_PATTERN)
    human_baseline: HumanBaselineReport
    machine_results: MachineCalibrationResults
    classes: dict[str, ClassCalibrationRowV1_1]
    hold_summary: list[str]

    @field_validator("hold_summary")
    @classmethod
    def validate_unique_hold_summary(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("hold_summary must be unique")
        return values


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
    return False
