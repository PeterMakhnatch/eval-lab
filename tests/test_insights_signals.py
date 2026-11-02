from __future__ import annotations

from datetime import UTC, datetime

import pytest

from evallab.insights.models import (
    CheckResult,
    ClaimRecord,
    ExtractedClaim,
    RecomputeResult,
    SignalRecord,
    claim_fingerprint,
)
from evallab.insights.signals import detect_signals
from evallab.insights.stats import wilson

NOW = datetime(2026, 11, 2, 18, tzinfo=UTC)


def _record(
    number: int = 1,
    *,
    stale: bool = False,
    failed: bool = False,
    recompute: RecomputeResult | None = None,
    **fields: object,
) -> ClaimRecord:
    values: dict[str, object] = {
        "local_id": str(number), "experiment": "E1", "kind": "measurement",
        "statement": "The detector found 21 of 28 cases.",
        "quote": "The detector found 21 of 28 cases.", "section_id": f"X-{number}",
        "metric": "recall", "subject": "detector", "dataset": "cases",
    }
    values.update(fields)
    claim = ExtractedClaim.model_validate(values)
    return ClaimRecord(
        claim_id=f"C-{number}", source_id=f"F-{number}", section_sha256="sha",
        fingerprint=claim_fingerprint(claim), pass_id="P-1", extracted_by="fixture",
        created_at=NOW, claim=claim, check=CheckResult(status="fail" if failed else "pass"),
        stale=stale, recompute=recompute,
    )


def _signals(*claims: ClaimRecord, kind: str) -> list[SignalRecord]:
    return [signal for signal in detect_signals(claims, now=NOW) if signal.kind == kind]


def test_value_mismatch_and_percentage_scaling() -> None:
    assert _signals(_record(k=21, n=28, value=0.9), kind="value_mismatch")
    assert not _signals(_record(k=21, n=28, value=0.75), kind="value_mismatch")
    assert not _signals(_record(k=21, n=28, value=75, unit="%"), kind="value_mismatch")
    assert not _signals(_record(value=0.9), kind="value_mismatch")


def test_ci_mismatch_and_rounding() -> None:
    assert _signals(_record(k=21, n=28, ci_low=0.74, ci_high=0.76), kind="ci_mismatch")
    interval = wilson(21, 28)
    assert not _signals(_record(k=21, n=28, ci_low=interval["low"], ci_high=interval["high"]),
                        kind="ci_mismatch")
    assert not _signals(_record(k=21, n=28), kind="ci_mismatch")


def test_p_mismatch_positive_and_negative() -> None:
    comparison = {"test": "mcnemar_exact", "b": 10, "c": 0, "p_value": 0.5}
    assert _signals(_record(kind="comparison", comparison=comparison), kind="p_mismatch")
    comparison["p_value"] = 2 / 2**10
    assert not _signals(_record(kind="comparison", comparison=comparison), kind="p_mismatch")
    comparison["test"] = "other"
    comparison["p_value"] = 0.5
    assert not _signals(_record(kind="comparison", comparison=comparison), kind="p_mismatch")


def test_recompute_differs_positive_and_negative() -> None:
    differs = RecomputeResult(status="differs", stated=0.75, value=0.5)
    reproduced = RecomputeResult(status="reproduced", stated=0.75, value=0.75)
    assert _signals(_record(recompute=differs), kind="recompute_differs")
    assert not _signals(_record(recompute=reproduced), kind="recompute_differs")
    assert not _signals(_record(), kind="recompute_differs")


@pytest.mark.parametrize("k", [0, 20])
def test_extreme_small_n_only_for_small_measurements(k: int) -> None:
    signals = _signals(_record(k=k, n=20, metric="false_alarm"), kind="extreme_small_n")
    assert len(signals) == 1 and "20" in signals[0].title
    if k == 0:
        assert "0 of 20 false alarms" in signals[0].title
    assert not _signals(_record(k=0, n=30), kind="extreme_small_n")
    assert not _signals(_record(k=10, n=20), kind="extreme_small_n")
    assert not _signals(_record(k=0, n=20, kind="method"), kind="extreme_small_n")
    assert not _signals(_record(k=0, n=0), kind="extreme_small_n")


def test_underpowered_null_requires_null_and_nonsignificant_p() -> None:
    fields: dict[str, object] = {
        "kind": "comparison", "statement": "There is no significant difference between detectors.",
        "comparison": {"test": "mcnemar_exact", "b": 3, "c": 2, "p_value": 1.0},
    }
    assert _signals(_record(**fields), kind="underpowered_null")
    fields["comparison"] = {"test": "mcnemar_exact", "b": 10, "c": 10, "p_value": 1.0}
    assert not _signals(_record(**fields), kind="underpowered_null")
    fields["comparison"] = {"test": "mcnemar_exact", "b": 5, "c": 0, "p_value": 0.01}
    assert not _signals(_record(**fields), kind="underpowered_null")
    fields["statement"] = "The detector has higher recall than the baseline."
    fields["comparison"] = {"test": "mcnemar_exact", "b": 3, "c": 2, "p_value": 1.0}
    assert not _signals(_record(**fields), kind="underpowered_null")


def test_underpowered_claim_exact_five_discordants_cannot_be_significant() -> None:
    fields: dict[str, object] = {
        "kind": "comparison", "statement": "The detector has significantly higher recall.",
        "comparison": {"test": "mcnemar_exact", "b": 5, "c": 0, "p_value": 0.0625},
    }
    result = _signals(_record(**fields), kind="underpowered_claim")
    assert result and result[0].detail["minimum_for_significance"] == 6
    fields["comparison"] = {"test": "mcnemar_exact", "b": 6, "c": 0, "p_value": 0.03125}
    assert not _signals(_record(**fields), kind="underpowered_claim")
    fields["comparison"] = {"test": "mcnemar_exact", "b": 5, "c": 0, "p_value": 0.0625}
    fields["statement"] = "There is no difference between the detectors."
    assert not _signals(_record(**fields), kind="underpowered_claim")


def test_drift_uses_fisher_and_requires_distinct_experiments() -> None:
    a = _record(1, k=31, n=46, experiment="E1", condition=" Test  Set ")
    b = _record(2, k=23, n=46, experiment="E2", condition="test set")
    assert not _signals(a, b, kind="drift")
    a = _record(1, k=40, n=46, experiment="E1", condition=" Test  Set ")
    b = _record(2, k=10, n=46, experiment="E2", condition="test set")
    signal = _signals(a, b, kind="drift")[0]
    assert signal.claim_ids == ["C-1", "C-2"]
    assert signal.detail["p_value"] < 0.01
    assert 0 < signal.score <= 5
    same = _record(3, k=10, n=46, experiment="E1", condition="test set")
    other_condition = _record(4, k=10, n=46, experiment="E2", condition="dev set")
    assert not _signals(a, same, kind="drift")
    assert not _signals(a, other_condition, kind="drift")


@pytest.mark.parametrize("relation", ["corrects", "refutes", "contradicts"])
def test_contradiction_links_target_experiment_claims(relation: str) -> None:
    original = _record(1, experiment="E1")
    followup = _record(2, experiment="E2", relations=[{
        "target_experiment": "E1", "relation": relation,
    }])
    signals = _signals(original, followup, kind="contradiction")
    assert len(signals) == 1 and signals[0].claim_ids == ["C-1", "C-2"]
    confirms = _record(3, experiment="E2", relations=[{
        "target_experiment": "E1", "relation": "confirms",
    }])
    assert not _signals(original, confirms, kind="contradiction")
    self_relation = _record(4, experiment="E1", relations=[{
        "target_experiment": "E1", "relation": relation,
    }])
    assert not _signals(original, self_relation, kind="contradiction")


@pytest.mark.parametrize("relation", ["extends", "confirms"])
def test_correction_cascade_links_dependents_and_correction(relation: str) -> None:
    original = _record(1, experiment="E1")
    dependent = _record(2, experiment="E2", relations=[{
        "target_experiment": "E1", "relation": relation,
    }])
    correction = _record(3, experiment="E3", relations=[{
        "target_experiment": "E1", "relation": "refutes",
    }])
    signals = _signals(original, dependent, correction, kind="correction_cascade")
    assert len(signals) == 1 and signals[0].claim_ids == ["C-1", "C-2", "C-3"]
    assert not _signals(original, dependent, kind="correction_cascade")
    merely_contradicts = _record(3, experiment="E3", relations=[{
        "target_experiment": "E1", "relation": "contradicts",
    }])
    assert not _signals(original, dependent, merely_contradicts, kind="correction_cascade")


def test_corrected_author_status_also_creates_cascade() -> None:
    corrected = _record(1, experiment="E1", status="corrected")
    dependent = _record(2, experiment="E2", relations=[{
        "target_experiment": "E1", "relation": "extends",
    }])
    assert _signals(corrected, dependent, kind="correction_cascade")


def test_coverage_gap_requires_both_axes_with_two_observations() -> None:
    cells = [("a", "x"), ("a", "y"), ("b", "x"), ("b", "z"), ("c", "y"), ("c", "z")]
    claims = [_record(index, subject=subject, dataset=dataset)
              for index, (subject, dataset) in enumerate(cells)]
    signals = _signals(*claims, kind="coverage_gap")
    assert {(s.detail["subject"], s.detail["dataset"]) for s in signals} == {
        ("a", "z"), ("b", "y"), ("c", "x"),
    }
    assert all(signal.claim_ids for signal in signals)
    sparse = [_record(1, subject="a", dataset="x"), _record(2, subject="b", dataset="y")]
    assert not _signals(*sparse, kind="coverage_gap")
    non_detector = [_record(index, subject=subject, dataset=dataset, metric="cost")
                    for index, (subject, dataset) in enumerate(cells)]
    assert not _signals(*non_detector, kind="coverage_gap")


def test_coverage_gap_is_capped_at_twenty() -> None:
    claims = []
    for subject in range(10):
        for offset in (0, 1):
            claims.append(_record(len(claims), subject=f"s{subject}",
                                  dataset=f"d{(subject + offset) % 10}"))
    assert len(_signals(*claims, kind="coverage_gap")) == 20


def test_multiplicity_bh_and_minimum_family_size() -> None:
    ps = [0.04, 0.2, 0.3, 0.6, 0.8]
    claims = [_record(index, kind="comparison", comparison={"test": "other", "p_value": p})
              for index, p in enumerate(ps)]
    signals = _signals(*claims, kind="multiplicity")
    assert len(signals) == 1
    assert signals[0].detail["lost_claim_ids"] == ["C-0"]
    assert not _signals(*claims[:4], kind="multiplicity")
    strong = [_record(index, comparison={"test": "other", "p_value": 0.001})
              for index in range(5)]
    assert not _signals(*strong, kind="multiplicity")
    nonsignificant = [_record(index, comparison={"test": "other", "p_value": 0.8})
                      for index in range(5)]
    assert not _signals(*nonsignificant, kind="multiplicity")


def test_stale_and_check_failed_claims_are_excluded_from_every_detector() -> None:
    assert not detect_signals([
        _record(1, k=0, n=10, value=0.9, stale=True),
        _record(2, k=0, n=10, value=0.9, failed=True),
    ], now=NOW)
    current = _record(3, k=40, n=46, experiment="E1")
    stale = _record(4, k=1, n=46, experiment="E2", stale=True)
    assert not _signals(current, stale, kind="drift")


def test_signal_ids_and_order_do_not_depend_on_input_order_or_clock() -> None:
    claims = [_record(1, k=0, n=10, value=0.9), _record(2, k=0, n=20, value=0.8)]
    first = detect_signals(claims, now=NOW)
    second = detect_signals(list(reversed(claims)), now=NOW.replace(hour=19))
    assert [signal.signal_id for signal in first] == [signal.signal_id for signal in second]
    assert first == sorted(first, key=lambda signal: (-signal.score, signal.signal_id))
