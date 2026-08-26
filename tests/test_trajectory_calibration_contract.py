from __future__ import annotations

import pytest
from pydantic import ValidationError

from evallab.trajectory_calibration import (
    CalibrationReport,
    CalibrationReportV1_1,
    UnsupportedCalibrationVersion,
    calibration_report_can_enable_acceptance,
    parse_calibration_report,
)
from evallab.trajectory_judgment import TRAJECTORY_ONTOLOGY_V1_CLASSES


def report_payload() -> dict:
    return {
        "schema": "calibration-report-v1",
        "calibration_version": "sha256:" + "1" * 64,
        "acceptance_enabling_allowed": False,
        "thresholds_digest": "sha256:" + "2" * 64,
        "n_items": 14,
        "n_proposed_accept": 0,
        "inter_rater": {
            "n_paired": 0,
            "cohen_kappa": None,
            "gwet_ac1": None,
            "observed_agreement": None,
            "kappa_min": 0.6,
            "alt_test_min": 0.6,
            "floor_pass": False,
        },
        "global_metrics": {
            "raw_judge_accuracy": None,
            "proposed_accept_precision": None,
            "coverage": 0.0,
            "selective_risk": None,
            "ece": None,
            "brier": None,
            "aurc": None,
            "risk_coverage": [],
            "abstention_precision": None,
            "abstention_justified_rate": None,
            "cite_valid_on_proposed_accept": None,
            "cross_judge_agreement": None,
            "cross_judge_is_not_gold": True,
        },
        "classes": {
            "infrastructure_failure": {
                "acceptance_enabled": False,
                "delta": 0.05,
                "n_gold": 0,
                "n_proposed_accept": 0,
                "prec_acc": None,
                "p_human": None,
                "wilson_lower_one_sided_95": None,
                "beta_lower_one_sided_95": None,
                "ci_width": None,
                "noninferiority_pass": False,
                "hold_reasons": ["acceptance_enabling_disabled"],
            }
        },
        "hold_summary": ["acceptance_enabling_disabled"],
    }


def successor_report_payload() -> dict:
    row = {
        "acceptance_enabled": False,
        "n_gold": 0,
        "n_accepted": 0,
        "false_accept_risk_ucb": None,
        "human_baseline_ppv": None,
        "noninferiority_margin": 0.05,
        "noninferiority_pass": False,
        "citation_validity": None,
        "hold_reasons": [
            "human_baseline_missing",
            "underpowered",
            "acceptance_enabling_disabled",
        ],
    }
    return {
        "schema": "calibration-report-v1.1",
        "calibration_version": "sha256:" + "3" * 64,
        "acceptance_enabling_allowed": False,
        "ontology_digest": "sha256:" + "4" * 64,
        "thresholds_digest": "sha256:" + "5" * 64,
        "split_manifest_digest": "sha256:" + "6" * 64,
        "item_manifest_digest": "sha256:" + "7" * 64,
        "pack_manifest_digest": "sha256:" + "8" * 64,
        "human_baseline": {
            "status": "hold",
            "required_independent_raters": 3,
            "n_independent_raters": 0,
            "blind_to_machine": True,
            "individual_labels_digest": None,
            "adjudication_digest": None,
            "pairwise_matrix": [],
            "krippendorff_alpha_nominal": None,
            "fleiss_kappa_multiclass": None,
            "gwet_ac1_multirater": None,
            "adjudication_counts": {
                "majority_without_adjudication": 0,
                "adjudicated": 0,
                "unresolved_hold": 0,
            },
            "per_class_precision": {},
        },
        "machine_results": {
            "judge_tuple_digest": "sha256:" + "9" * 64,
            "n_items": 0,
            "raw_accuracy": None,
            "coverage": 0.0,
            "accepted_precision": None,
            "abstention_rate": None,
            "citation_validity": None,
            "ece": None,
            "brier": None,
        },
        "classes": {
            class_id: dict(row) for class_id in TRAJECTORY_ONTOLOGY_V1_CLASSES
        },
        "hold_summary": [
            "human_labeling_not_started",
            "human_baseline_missing",
            "acceptance_enabling_disabled",
        ],
    }


def test_exact_disabled_calibration_report_roundtrip() -> None:
    report = CalibrationReport.model_validate(report_payload())
    assert report.acceptance_enabling_allowed is False
    assert all(not row.acceptance_enabled for row in report.classes.values())
    assert report.model_dump(mode="json", by_alias=True) == report_payload()


def test_report_and_class_cannot_enable_acceptance() -> None:
    report_enabled = report_payload()
    report_enabled["acceptance_enabling_allowed"] = True
    class_enabled = report_payload()
    class_enabled["classes"]["infrastructure_failure"][
        "acceptance_enabled"
    ] = True
    with pytest.raises(ValidationError):
        CalibrationReport.model_validate(report_enabled)
    with pytest.raises(ValidationError):
        CalibrationReport.model_validate(class_enabled)


def test_cross_judge_is_never_gold() -> None:
    payload = report_payload()
    payload["global_metrics"]["cross_judge_is_not_gold"] = False
    with pytest.raises(ValidationError):
        CalibrationReport.model_validate(payload)


def test_optional_nonnullable_fields_reject_explicit_null() -> None:
    for field in ("n_clusters", "false_accept"):
        payload = report_payload()
        payload["classes"]["infrastructure_failure"][field] = None
        with pytest.raises(ValidationError):
            CalibrationReport.model_validate(payload)
    payload = report_payload()
    payload["cluster_key"] = None
    with pytest.raises(ValidationError):
        CalibrationReport.model_validate(payload)


def test_nullable_v1_metrics_accept_explicit_null() -> None:
    payload = report_payload()
    row = payload["classes"]["infrastructure_failure"]
    for field in ("rec_acc", "margin", "clustered_lower_one_sided_95", "cite_valid"):
        row[field] = None
    report = CalibrationReport.model_validate(payload)
    parsed = report.classes["infrastructure_failure"]
    assert parsed.rec_acc is None
    assert parsed.margin is None
    assert parsed.clustered_lower_one_sided_95 is None
    assert parsed.cite_valid is None


def test_version_dispatch_keeps_v1_bootstrap_hold_only() -> None:
    payload = report_payload()
    row = payload["classes"]["infrastructure_failure"]
    payload["classes"] = {
        class_id: dict(row) for class_id in TRAJECTORY_ONTOLOGY_V1_CLASSES
    }
    report = parse_calibration_report(payload)
    assert calibration_report_can_enable_acceptance(report) is False
    assert report.inter_rater.cohen_kappa is None
    assert report.inter_rater.gwet_ac1 is None


def test_version_dispatch_parses_hold_only_three_rater_successor() -> None:
    report = parse_calibration_report(successor_report_payload())
    assert isinstance(report, CalibrationReportV1_1)
    assert report.human_baseline.status == "hold"
    assert report.human_baseline.required_independent_raters == 3
    assert report.human_baseline.n_independent_raters == 0
    assert report.human_baseline.individual_labels_digest is None
    assert report.machine_results.n_items == 0
    assert report.machine_results.abstention_rate is None
    assert calibration_report_can_enable_acceptance(report) is False


def test_version_dispatch_rejects_unknown_successor() -> None:
    payload = successor_report_payload()
    payload["schema"] = "calibration-report-v1.2"
    with pytest.raises(UnsupportedCalibrationVersion, match="unsupported"):
        parse_calibration_report(payload)


def test_dispatch_requires_exact_frozen_class_set() -> None:
    with pytest.raises(ValueError, match="class set"):
        parse_calibration_report(report_payload())


def test_successor_missing_required_independent_raters() -> None:
    payload = successor_report_payload()
    del payload["human_baseline"]["required_independent_raters"]
    with pytest.raises(ValidationError):
        CalibrationReportV1_1.model_validate(payload)


def test_successor_required_independent_raters_below_minimum() -> None:
    payload = successor_report_payload()
    payload["human_baseline"]["required_independent_raters"] = 2
    with pytest.raises(ValidationError):
        CalibrationReportV1_1.model_validate(payload)


def test_successor_n_independent_raters_below_zero() -> None:
    payload = successor_report_payload()
    payload["human_baseline"]["n_independent_raters"] = -1
    with pytest.raises(ValidationError):
        CalibrationReportV1_1.model_validate(payload)


def test_successor_zero_items_rejects_non_null_abstention_rate() -> None:
    payload = successor_report_payload()
    payload["machine_results"]["n_items"] = 0
    payload["machine_results"]["abstention_rate"] = 0.0
    with pytest.raises(ValidationError, match="abstention_rate"):
        CalibrationReportV1_1.model_validate(payload)


def test_successor_nonzero_items_require_abstention_rate() -> None:
    payload = successor_report_payload()
    payload["machine_results"]["n_items"] = 1
    payload["machine_results"]["abstention_rate"] = None
    with pytest.raises(ValidationError, match="abstention_rate"):
        CalibrationReportV1_1.model_validate(payload)


def test_successor_structural_enablement_flags_do_not_unlock() -> None:
    payload = successor_report_payload()
    payload["acceptance_enabling_allowed"] = True
    payload["classes"]["infrastructure_failure"]["acceptance_enabled"] = True
    report = CalibrationReportV1_1.model_validate(payload)
    assert report.acceptance_enabling_allowed is True
    assert report.classes["infrastructure_failure"].acceptance_enabled is True
    assert calibration_report_can_enable_acceptance(report) is False


def test_json_schema_matches_frozen_top_level_and_optional_types() -> None:
    schema = CalibrationReport.model_json_schema(by_alias=True)
    expected = {
        "schema",
        "calibration_version",
        "acceptance_enabling_allowed",
        "thresholds_digest",
        "cluster_key",
        "n_items",
        "n_proposed_accept",
        "inter_rater",
        "global_metrics",
        "classes",
        "hold_summary",
    }
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == expected
    assert set(schema["required"]) == expected - {"cluster_key"}
    assert schema["properties"]["cluster_key"]["const"] == "source_task_id"
    row_schema = schema["$defs"]["ClassCalibrationRow"]
    assert row_schema["properties"]["n_clusters"]["type"] == "integer"
    assert row_schema["properties"]["false_accept"]["type"] == "integer"


def test_successor_json_schema_matches_frozen_top_level() -> None:
    schema = CalibrationReportV1_1.model_json_schema(by_alias=True)
    expected = {
        "schema",
        "calibration_version",
        "acceptance_enabling_allowed",
        "ontology_digest",
        "thresholds_digest",
        "split_manifest_digest",
        "item_manifest_digest",
        "pack_manifest_digest",
        "human_baseline",
        "machine_results",
        "classes",
        "hold_summary",
    }
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == expected
    assert set(schema["required"]) == expected
    assert schema["properties"]["acceptance_enabling_allowed"]["type"] == "boolean"
    assert "const" not in schema["properties"]["acceptance_enabling_allowed"]
    human = schema["$defs"]["HumanBaselineReport"]
    assert "required_independent_raters" in human["properties"]
    assert "n_independent_raters" in human["properties"]
    assert "required_independent_raters" in human["required"]
    assert "n_independent_raters" in human["required"]
    row_schema = schema["$defs"]["ClassCalibrationRowV1_1"]
    assert row_schema["properties"]["acceptance_enabled"]["type"] == "boolean"
    assert "const" not in row_schema["properties"]["acceptance_enabled"]
