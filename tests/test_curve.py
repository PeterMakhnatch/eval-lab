from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from evallab import cli
from evallab.cohort import compare
from evallab.curve import build_curve, load_curve_spec
from evallab.report import build_eval_card
from evallab.schemas import CapabilityCurveSpec, CohortComparisonSpec

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / "tests/fixtures/curve/curve-spec.json"
PRODUCED_AT = datetime(2026, 8, 23, tzinfo=UTC)


def _spec_payload() -> dict[str, Any]:
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


def _build(payload: dict[str, Any] | None = None):
    spec = (
        CapabilityCurveSpec.model_validate(payload)
        if payload is not None
        else load_curve_spec(SPEC_PATH)
    )
    return build_curve(
        spec,
        repo_root=REPO_ROOT,
        produced_by="test-curve",
        produced_at=PRODUCED_AT,
    )


def _level(report, level: int):
    return next(item for item in report.levels if item.level == level)


def _add_trials(selector: dict[str, Any], *names: str) -> None:
    selector["trial_names"].extend(names)


def test_empirical_curve_composes_cohort_metrics_without_fit_or_score() -> None:
    spec = load_curve_spec(SPEC_PATH)
    report = _build()
    block_ids = _level(report, 1).exact_pair_set

    assert report.rankable is True
    assert report.refuse_to_rank_reasons == []
    assert len(block_ids) == 2
    assert all(value.startswith("sha256:") for value in block_ids)
    assert [_level(report, value).role for value in (1, 2, 3)] == [
        "reference",
        "descriptive",
        "primary",
    ]
    assert [_level(report, value).pass_any_first_k[0].rate for value in (1, 2, 3)] == [
        1.0,
        1.0,
        0.0,
    ]
    assert [_level(report, value).pass_all_first_k[0].rate for value in (1, 2, 3)] == [
        1.0,
        0.0,
        0.0,
    ]
    primary = _level(report, 3).contrasts[0]
    assert primary.n_pairs == 2
    assert primary.paired_delta == -1.0
    assert primary.paired_interval_95 == [-1.0, -1.0]
    assert (primary.wins, primary.ties, primary.losses) == (0, 0, 2)
    assert primary.pass_all_first_k_delta == -1.0
    assert primary.pass_all_first_k_interval_95 == [-1.0, -1.0]
    assert primary.rankable is True
    serialized = report.model_dump(mode="json")
    primary_level = next(level for level in serialized["levels"] if level["level"] == 3)
    assert "pass_any_first_k" in primary_level
    assert "pass_all_first_k" in primary_level
    assert "pass_at_k" not in primary_level
    assert "pass_power_k" not in primary_level

    cohort_report = compare(spec.comparisons[1].comparison_spec, repo_root=REPO_ROOT)  # type: ignore[arg-type]
    paired = cohort_report["paired"][0]
    assert primary.paired_delta == paired["mean_pass_any_first_k_delta"]
    assert primary.paired_interval_95 == paired["bootstrap_95"]
    assert (primary.wins, primary.ties, primary.losses) == (
        paired["wins"],
        paired["ties"],
        paired["losses"],
    )
    encoded = report.model_dump(mode="json")
    forbidden = {"score", "auc", "fit", "logistic", "irt", "mean_level", "max_level"}
    assert forbidden.isdisjoint(_all_keys(encoded))
    assert report.contract_note.endswith("not substantive generality evidence")


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for child in value.values() for key in _all_keys(child)}
    if isinstance(value, list):
        return {key for child in value for key in _all_keys(child)}
    return set()


def test_unpaired_poison_is_reported_and_refuses_curve_rank() -> None:
    payload = _spec_payload()
    depth_2 = payload["comparisons"][0]["comparison_spec"]
    _add_trials(depth_2["cohorts"][1], "poison__01", "poison__02")

    report = _build(payload)
    level = _level(report, 2)
    assert report.rankable is False
    assert len(level.unpaired_task_blocks) == 1
    assert level.unpaired_task_blocks[0].startswith("sha256:")
    assert any("unpaired eligible task block" in reason for reason in report.refuse_to_rank_reasons)


def test_authoritative_factor_coordinates_refuse_transposed_and_mixed_levels() -> None:
    transposed = _spec_payload()
    transposed_candidate = transposed["comparisons"][0]["comparison_spec"]["cohorts"][1]
    transposed_candidate["paths"] = ["tests/fixtures/curve/runs/depth-3"]
    report = _build(transposed)
    assert report.rankable is False
    assert any(
        "factor 'depth' is 3, expected 2" in reason for reason in report.refuse_to_rank_reasons
    )

    mixed = _spec_payload()
    mixed_candidate = mixed["comparisons"][0]["comparison_spec"]["cohorts"][1]
    mixed_candidate["paths"].append("tests/fixtures/curve/runs/depth-3")
    report = _build(mixed)
    assert report.rankable is False
    assert any(
        "mixes 2 authoritative factor coordinates" in reason
        for reason in report.refuse_to_rank_reasons
    )
    descriptive = _level(report, 2).contrasts[0]
    assert "descriptive level; not preregistered primary contrast" in descriptive.refusal_reasons
    assert any(
        "undeclared consequential" in reason or "mixes" in reason
        for reason in descriptive.refusal_reasons
    )


def test_factor_kind_requires_honest_execution_or_generator_provenance() -> None:
    missing_binding = _spec_payload()
    missing_binding["factor_kind"] = "execution"
    with pytest.raises(
        ValueError, match="execution capability curves require an explicit treatment_binding"
    ):
        CapabilityCurveSpec.model_validate(missing_binding)

    false_execution = _spec_payload()
    false_execution["factor_kind"] = "execution"
    false_execution["treatment_binding"] = "timeout_seconds"
    for source in false_execution["comparisons"]:
        source["comparison_spec"]["budget_exhaustion_is_failure"] = True
    report = _build(false_execution)
    assert report.rankable is False
    assert any(
        "execution factor is not bound to 'timeout_seconds'" in reason
        for reason in report.refuse_to_rank_reasons
    )


def test_identical_and_missing_authoritative_coordinates_refuse_globally(
    tmp_path: Path,
) -> None:
    def copied_payload(name: str) -> tuple[Path, dict[str, Any]]:
        fixture = tmp_path / name
        shutil.copytree(REPO_ROOT / "tests/fixtures/curve", fixture)
        payload = _spec_payload()
        for source in payload["comparisons"]:
            for selector in source["comparison_spec"]["cohorts"]:
                selector["paths"] = [
                    path.replace("tests/fixtures/curve", name) for path in selector["paths"]
                ]
        return fixture, payload

    fixture, identical = copied_payload("identical")
    metadata_path = fixture / "runs/depth-2/lab-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["experiment"]["factor_values"]["depth"] = 1
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    identical_report = build_curve(
        CapabilityCurveSpec.model_validate(identical),
        repo_root=tmp_path,
        produced_by="test",
        produced_at=PRODUCED_AT,
    )
    assert identical_report.rankable is False
    assert any(
        "factor 'depth' is 1, expected 2" in reason
        for reason in identical_report.refuse_to_rank_reasons
    )
    identical_descriptive = _level(identical_report, 2).contrasts[0]
    assert any(
        "declared variable 'factor_values_digest' does not differ" in reason
        for reason in identical_descriptive.refusal_reasons
    )

    fixture, missing = copied_payload("missing")
    metadata_path = fixture / "runs/depth-2/lab-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    del metadata["experiment"]["factor_values"]
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    missing_report = build_curve(
        CapabilityCurveSpec.model_validate(missing),
        repo_root=tmp_path,
        produced_by="test",
        produced_at=PRODUCED_AT,
    )
    assert missing_report.rankable is False
    assert any(
        "missing or invalid factor_values_json" in reason
        for reason in missing_report.refuse_to_rank_reasons
    )


def test_rewarded_non_budget_exception_remains_censored() -> None:
    payload = _spec_payload()["comparisons"][1]["comparison_spec"]
    payload["pass_k"] = [1]
    payload["cohorts"][0]["trial_names"] = ["infra__01"]
    payload["cohorts"][1]["trial_names"] = ["infra__01"]

    report = compare(CohortComparisonSpec.model_validate(payload), repo_root=REPO_ROOT)
    candidate = report["cohorts"][1]
    assert candidate["exception_count"] == 1
    assert candidate["capability_denominator"] == 0
    assert candidate["trial_pass_count"] == 0
    assert candidate["pass_any_first_k"][0]["n_tasks"] == 0
    assert report["paired"][0]["n_pairs"] == 0
    assert len(report["paired"][0]["unpaired_tasks"]) == 1


def test_timeout_budget_is_failure_only_for_declared_budget_treatment() -> None:
    payload = _spec_payload()["comparisons"][1]["comparison_spec"]
    payload["pass_k"] = [2]
    payload["cohorts"][0]["trial_names"] = ["timeout__01", "timeout__02"]
    payload["cohorts"][1]["trial_names"] = ["timeout__01", "timeout__02"]

    censored_spec = CohortComparisonSpec.model_validate(payload)
    censored = compare(censored_spec, repo_root=REPO_ROOT)
    assert censored["paired"][0]["n_pairs"] == 0
    assert len(censored["paired"][0]["unpaired_tasks"]) == 1
    assert censored["cohorts"][1]["exception_count"] == 2

    payload["budget_exhaustion_is_failure"] = True
    treatment_spec = CohortComparisonSpec.model_validate(payload)
    treatment = compare(treatment_spec, repo_root=REPO_ROOT)
    assert treatment["paired"][0]["n_pairs"] == 1
    assert treatment["cohorts"][1]["exception_count"] == 0
    assert treatment["cohorts"][1]["capability_denominator"] == 2
    assert treatment["cohorts"][1]["pass_any_first_k"][0]["rate"] == 0.0


def test_one_arm_infrastructure_failure_is_censored_and_drops_pair() -> None:
    payload = _spec_payload()
    depth_3 = payload["comparisons"][1]["comparison_spec"]
    _add_trials(depth_3["cohorts"][0], "infra__01", "infra__02")
    _add_trials(depth_3["cohorts"][1], "infra__01")

    report = _build(payload)
    level = _level(report, 3)
    assert report.rankable is False
    assert [item.trial_id for item in level.exception_trials] == ["m047-3-005"]
    assert level.exception_trials[0].exception_class == "DockerInfrastructureError"
    assert len(level.censored_task_blocks) == 1
    assert level.censored_task_blocks == level.unpaired_task_blocks
    assert len(level.exact_pair_set) == 2


def test_insufficient_k_refuses_without_attempt_level_inference() -> None:
    payload = _spec_payload()
    payload["primary_contrast"]["k"] = 3
    for source in payload["comparisons"]:
        source["comparison_spec"]["pass_k"] = [3]

    report = _build(payload)
    assert report.rankable is False
    primary = _level(report, 3)
    assert primary.pass_any_first_k[0].n_tasks == 0
    assert len(primary.pass_any_first_k[0].insufficient_task_blocks) == 2
    assert primary.contrasts[0].n_pairs == 0
    assert any("fewer than k" in reason for reason in report.refuse_to_rank_reasons)


def test_sparse_primary_and_zero_interval_refuse_rank() -> None:
    sparse = _spec_payload()
    only_one_block = ["block-a__01", "block-a__02"]
    for source in sparse["comparisons"]:
        for selector in source["comparison_spec"]["cohorts"]:
            selector["trial_names"] = only_one_block
    sparse_report = _build(sparse)
    assert sparse_report.rankable is False
    assert any("fewer than 2 pairs" in reason for reason in sparse_report.refuse_to_rank_reasons)

    zero = _spec_payload()
    zero["primary_contrast"]["level"] = 2
    zero_report = _build(zero)
    assert zero_report.rankable is False
    contrast = _level(zero_report, 2).contrasts[0]
    assert contrast.paired_interval_95 == [0.0, 0.0]
    assert any("includes zero" in reason for reason in contrast.refusal_reasons)
    assert _level(zero_report, 3).role == "descriptive"


def test_curve_provenance_and_input_digests_are_stable() -> None:
    first = _build()
    second = _build()
    assert first.common_controlled_fingerprint == second.common_controlled_fingerprint
    assert first.common_controlled_fingerprint is not None
    assert first.input_digests == second.input_digests
    assert first.produced_at == second.produced_at == PRODUCED_AT
    assert "$curve_spec" in first.input_digests
    assert all(value.startswith("sha256:") for value in first.input_digests.values())


def test_curve_cli_json_roundtrip_and_invalid_json_error(tmp_path: Path, capsys) -> None:
    assert (
        cli.run_cli(
            ["curve", "validate", "tests/fixtures/curve/curve-spec.json"],
            workspace=REPO_ROOT,
        )
        == 0
    )
    validated = json.loads(capsys.readouterr().out)
    assert validated["curve_id"] == "m047-depth-contract"
    assert validated["rankable"] is True

    output = tmp_path / "curve.json"
    assert (
        cli.run_cli(
            [
                "curve",
                "build",
                "tests/fixtures/curve/curve-spec.json",
                "--output",
                str(output),
                "--produced-by",
                "cli-golden",
            ],
            workspace=REPO_ROOT,
        )
        == 0
    )
    built = json.loads(capsys.readouterr().out)
    assert built["rankable"] is True
    assert output.is_file()

    assert cli.run_cli(["curve", "report", str(output)], workspace=REPO_ROOT) == 0
    frozen = json.loads(capsys.readouterr().out)
    assert frozen["produced_by"] == "cli-golden"

    refusal_payload = _spec_payload()
    refusal_payload["primary_contrast"]["level"] = 2
    refusal_spec = tmp_path / "scientific-refusal.json"
    refusal_spec.write_text(json.dumps(refusal_payload), encoding="utf-8")
    refusal_output = tmp_path / "scientific-refusal-report.json"
    assert (
        cli.run_cli(
            [
                "curve",
                "build",
                str(refusal_spec),
                "--output",
                str(refusal_output),
            ],
            workspace=REPO_ROOT,
        )
        == 0
    )
    refusal_status = json.loads(capsys.readouterr().out)
    assert refusal_status["rankable"] is False
    assert refusal_status["refuse_to_rank_reasons"]
    assert cli.run_cli(["curve", "report", str(refusal_output)], workspace=REPO_ROOT) == 0
    assert json.loads(capsys.readouterr().out)["rankable"] is False

    bad = tmp_path / "bad.json"
    bad.write_text('{"schema_version": 1}\n', encoding="utf-8")
    assert cli.run_cli(["curve", "validate", str(bad)], workspace=REPO_ROOT) == 2
    error = json.loads(capsys.readouterr().out)
    assert error["valid"] is False


def test_report_card_comparison_cannot_bypass_prereg(tmp_path: Path) -> None:
    spec = tmp_path / "comparison.json"
    spec.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "comparison-without-prereg",
                "hypothesis": "A comparison.",
                "purpose": "comparison",
                "task": "fixture",
                "agent": "fixture-agent",
                "submitted_by": "test",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="require a prereg block"):
        build_eval_card(spec, repo_root=tmp_path)
