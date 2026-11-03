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
    source_id: str | None = None,
    recompute: RecomputeResult | None = None,
    **fields: object,
) -> ClaimRecord:
    values: dict[str, object] = {
        "local_id": str(number), "experiment": "E1", "kind": "measurement",
        "statement": "Recall was 21 of 28 (75%).",
        "quote": "Recall was 21 of 28 (75%).", "section_id": f"X-{number}",
        "metric": "recall", "subject": "detector", "dataset": "cases",
    }
    values.update(fields)
    claim = ExtractedClaim.model_validate(values)
    return ClaimRecord(
        claim_id=f"C-{number}", source_id=source_id or f"F-{number}", section_sha256="sha",
        fingerprint=claim_fingerprint(claim), pass_id="P-1", extracted_by="fixture",
        created_at=NOW, claim=claim, check=CheckResult(status="fail" if failed else "pass"),
        stale=stale, recompute=recompute,
    )


def _signals(*claims: ClaimRecord, kind: str) -> list[SignalRecord]:
    return [signal for signal in detect_signals(claims, now=NOW) if signal.kind == kind]


def test_value_mismatch_requires_rate_quantity_and_normalizes_percent() -> None:
    assert _signals(_record(k=21, n=28, value=0.9), kind="value_mismatch")
    assert not _signals(_record(k=21, n=28, value=0.75), kind="value_mismatch")
    assert not _signals(_record(k=21, n=28, value=75, unit="%"), kind="value_mismatch")
    assert not _signals(_record(k=21, n=28, value=75), kind="value_mismatch")  # scale is ambiguous
    assert not _signals(_record(metric="backend_calls", unit="calls", k=1, n=3, value=1),
                        kind="value_mismatch")
    assert not _signals(_record(metric="cost", unit="USD", k=200, n=200, value=1.3),
                        kind="value_mismatch")
    assert not _signals(_record(value=0.9), kind="value_mismatch")


def test_ci_mismatch_requires_wilson_rate_method_and_normalizes_legacy_percent() -> None:
    fields = {"k": 21, "n": 28, "quote": "Recall 21/28 (Wilson CI 74–76%)."}
    assert _signals(_record(**fields, ci_low=74, ci_high=76), kind="ci_mismatch")
    interval = wilson(21, 28)
    assert not _signals(_record(**fields, ci_low=interval["low"], ci_high=interval["high"]),
                        kind="ci_mismatch")
    assert not _signals(_record(metric="AUROC", quote="AUROC [1, 1] (bootstrap); recall 4/4",
                                k=4, n=4, ci_low=1, ci_high=1), kind="ci_mismatch")
    assert not _signals(_record(k=0, n=10, quote="Recall 0/10 (Clopper–Pearson 0–30.8%).",
                                ci_low=0, ci_high=30.8), kind="ci_mismatch")


def test_p_mismatch_positive_and_negative() -> None:
    comparison = {"test": "mcnemar_exact", "b": 10, "c": 0, "p_value": 0.5}
    assert _signals(_record(kind="comparison", comparison=comparison), kind="p_mismatch")
    comparison["p_value"] = 2 / 2**10
    assert not _signals(_record(kind="comparison", comparison=comparison), kind="p_mismatch")
    comparison["test"] = "other"
    comparison["p_value"] = 0.5
    assert not _signals(_record(kind="comparison", comparison=comparison), kind="p_mismatch")


def test_recompute_requires_same_typed_quantity_and_normalizes_units() -> None:
    ratio = {"op": "ratio", "num": {"op": "value", "file": "r.json", "pointer": "/recall_count"},
            "den": {"op": "value", "file": "r.json", "pointer": "/cases"}}
    assert _signals(_record(value=0.75, unit="fraction", recipe=ratio,
                            recompute=RecomputeResult(status="differs", stated=0.75, value=0.5)),
                    kind="recompute_differs")
    assert not _signals(_record(value=50, unit="%", recipe=ratio,
                                recompute=RecomputeResult(status="differs", stated=50, value=0.5)),
                        kind="recompute_differs")
    count_pointer = {"op": "value", "file": "r.json", "pointer": "/weak_count"}
    assert not _signals(_record(k=18, n=28, value=18, metric="weak-grader rate",
                                recipe=count_pointer,
                                recompute=RecomputeResult(status="differs", stated=18, value=18)),
                        kind="recompute_differs")


@pytest.mark.parametrize("k", [0, 20])
def test_extreme_small_n_excludes_administrative_and_deduplicates(k: int) -> None:
    signals = _signals(_record(k=k, n=20, metric="false_alarm"), kind="extreme_small_n")
    assert len(signals) == 1 and "20" in signals[0].title
    if k == 0:
        assert "0 of 20 false alarms" in signals[0].title
    assert not _signals(_record(metric="workflow score", unit="points", k=21, n=21),
                        kind="extreme_small_n")
    assert not _signals(_record(metric="tests passed", unit="tests", k=24, n=24),
                        kind="extreme_small_n")
    assert not _signals(_record(k=0, n=30), kind="extreme_small_n")
    assert not _signals(_record(k=10, n=20), kind="extreme_small_n")
    assert not detect_signals([_record(k=None, n=None, metric="recall")], now=NOW)
    duplicate = _record(2, experiment="E2", k=k, n=20, metric="false_alarm")
    grouped = _signals(_record(1, k=k, n=20, metric="false_alarm"), duplicate,
                       kind="extreme_small_n")
    assert len(grouped) == 1 and grouped[0].claim_ids == ["C-1", "C-2"]


def test_small_n_is_capped_and_prefers_decision_relevant_issues() -> None:
    claims = [_record(i, k=20, n=20, metric="capture", dataset=f"cohort-{i}",
                      statement=f"Gate decision uses capture 20/20 in cohort {i}.",
                      quote="Capture 20/20.") for i in range(5)]
    signals = _signals(*claims, kind="extreme_small_n")
    assert len(signals) == 3 and all(s.detail["decision_relevant"] for s in signals)


def test_underpowered_null_requires_an_equivalence_conclusion() -> None:
    fields: dict[str, object] = {
        "kind": "comparison", "statement": "The detectors are equivalent; there is no effect.",
        "comparison": {"test": "mcnemar_exact", "b": 3, "c": 2, "p_value": 1.0},
    }
    assert _signals(_record(**fields), kind="underpowered_null")
    fields["statement"] = "The comparison was not significant; rerun in progress."
    assert not _signals(_record(**fields), kind="underpowered_null")
    for cautious in ["The detectors did not differ.", "The detector was not significantly better.",
                     "The comparison was n.s."]:
        fields["statement"] = cautious
        assert not _signals(_record(**fields), kind="underpowered_claim")
        assert not _signals(_record(**fields), kind="underpowered_null")
    fields["comparison"] = {"test": "mcnemar_exact", "b": 10, "c": 10, "p_value": 1.0}
    fields["statement"] = "The detectors are equivalent."
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


def test_drift_requires_same_quantity_cohort_and_condition() -> None:
    a = _record(1, k=40, n=46, experiment="E1", condition="test")
    b = _record(2, k=10, n=46, experiment="E2", condition="test")
    assert _signals(a, b, kind="drift")
    other_condition = _record(4, k=10, n=46, experiment="E2", condition="dev")
    assert not _signals(a, other_condition, kind="drift")
    count = _record(5, k=10, n=46, experiment="E2", condition="test", unit="count")
    assert not _signals(a, count, kind="drift")


@pytest.mark.parametrize("relation", ["corrects", "refutes", "contradicts"])
def test_contradiction_identity_survives_support_growth(relation: str) -> None:
    original = _record(1, experiment="E1")
    followup = _record(2, experiment="E2", relations=[{"target_experiment": "E1", "relation": relation}])
    first = _signals(original, followup, kind="contradiction")[0]
    second = _signals(original, followup, _record(3, experiment="E1"), kind="contradiction")[0]
    assert first.signal_id == second.signal_id and first.claim_ids != second.claim_ids


def test_design_contrast_is_not_contradiction() -> None:
    original = _record(1, experiment="E1")
    contrast = _record(2, experiment="E2", relations=[{
        "target_experiment": "E1", "relation": "contradicts",
        "note": "This is a design contrast, not a refutation of E1 results.",
    }])
    assert not _signals(original, contrast, kind="contradiction")


def test_cascade_requires_property_scoped_reliance_not_rerun() -> None:
    original = _record(1, experiment="E1", metric="delivery_semantics", status="refuted")
    dependent = _record(2, experiment="E2", metric="delivery_semantics",
                        statement="The conclusion relies on the earlier delivery semantics result.",
                        relations=[{"target_experiment": "E1", "relation": "extends"}])
    assert _signals(original, dependent, kind="correction_cascade")
    unrelated = _record(3, experiment="E2", metric="runtime",
                        statement="The conclusion relies on the earlier delivery semantics result.",
                        relations=[{"target_experiment": "E1", "relation": "extends"}])
    assert not _signals(original, unrelated, kind="correction_cascade")
    rerun = _record(4, experiment="E2", metric="delivery_semantics",
                    statement="Independent rerun of delivery semantics, relying on no earlier result.",
                    relations=[{"target_experiment": "E1", "relation": "extends"}])
    assert not _signals(original, rerun, kind="correction_cascade")


def _coverage_claims() -> list[ClaimRecord]:
    cells = [("a", "x"), ("a", "y"), ("b", "x"), ("b", "z"), ("c", "y"), ("c", "z")]
    return [_record(index, subject=subject, dataset=dataset, k=10, n=20)
            for index, (subject, dataset) in enumerate(cells)]


def test_coverage_gap_suppresses_measured_aliases_and_preserves_cohorts() -> None:
    claims = _coverage_claims()
    claims.append(_record(99, subject="length baseline", dataset="z", k=5, n=20))
    assert not _signals(*claims, kind="coverage_gap")
    other = _record(100, subject="a", dataset="z", condition="other cohort", k=5, n=20)
    absent = _record(101, subject="a", dataset="z", k=0, n=20,
                     statement="Recall for a on z is not yet measured.")
    assert not _signals(*claims, other, absent, kind="coverage_gap")


def test_coverage_gap_requires_explicit_absence() -> None:
    claims = _coverage_claims()
    missing = _record(99, subject="a", dataset="z", metric="recall", kind="qualitative",
                      statement="Recall for a on z is not yet measured.")
    signal = _signals(*claims, missing, kind="coverage_gap")
    assert len(signal) == 1 and signal[0].detail["absence"] == "explicit source statement"


def _family_claims() -> list[ClaimRecord]:
    return [_record(index, kind="comparison", metric="capture", dataset="cohort",
                    condition="reader family", comparison={
                        "test": "other", "baseline": "reference", "b": index, "c": 0, "p_value": p})
            for index, p in enumerate([0.04, 0.2, 0.3, 0.6, 0.8])]


def test_multiplicity_is_family_scoped_and_deduplicated() -> None:
    claims = _family_claims()
    signals = _signals(*claims, kind="multiplicity")
    assert len(signals) == 1 and signals[0].detail["lost_claim_ids"] == ["C-0"]
    duplicate = _record(99, kind="comparison", metric="capture", dataset="cohort",
                        condition="reader family", comparison={
                            "test": "other", "baseline": "reference", "b": 0, "c": 0, "p_value": 0.04})
    deduplicated = _signals(*claims, duplicate, kind="multiplicity")[0]
    assert deduplicated.detail["tests"] == 5
    unrelated = _record(100, kind="comparison", metric="capture", dataset="cohort",
                        condition="defense family", comparison={
                            "test": "other", "baseline": "reference", "b": 20, "c": 0,
                            "p_value": 0.04})
    # The small reader family stays unchanged rather than absorbing defense tests.
    separate = _signals(*claims[:4], unrelated, _record(
        101, kind="comparison", metric="capture", dataset="cohort", condition="defense family",
        comparison={"test": "other", "baseline": "reference", "b": 21, "c": 0,
                    "p_value": 0.8}), kind="multiplicity")
    defense = [signal for signal in separate if signal.detail["family"][5] == "defense comparisons"]
    assert len(defense) == 1 and defense[0].detail["tests"] == 2


def test_stale_and_check_failed_claims_are_excluded_from_every_detector() -> None:
    assert not detect_signals([_record(stale=True), _record(failed=True)], now=NOW)


def test_signal_ids_and_order_do_not_depend_on_input_order_or_clock() -> None:
    claims = [_record(1, k=0, n=10, value=0.9), _record(2, k=0, n=20, value=0.8)]
    first = detect_signals(claims, now=NOW)
    second = detect_signals(list(reversed(claims)), now=NOW.replace(hour=19))
    assert [signal.signal_id for signal in first] == [signal.signal_id for signal in second]
    assert first == sorted(first, key=lambda signal: (-signal.score, signal.signal_id))


def _reader_test(number: int, reader: str, *, b: int, c: int, p: float,
                 condition: str | None = None) -> ClaimRecord:
    return _record(number, kind="comparison", metric="paired recall", dataset="cohort",
                   subject=None, models=[reader, "reference"], condition=condition,
                   comparison={"test": "mcnemar_exact", "baseline": "reference",
                               "b": b, "c": c, "p_value": p})


def test_multiplicity_keeps_distinct_readers_with_identical_counts() -> None:
    claims = [
        _reader_test(1, "reader-a", b=0, c=6, p=0.031), _reader_test(2, "reader-b", b=0, c=6, p=0.031),
        _reader_test(3, "reader-c", b=2, c=2, p=1.0), _reader_test(4, "reader-d", b=2, c=2, p=1.0),
        _reader_test(5, "reader-e", b=4, c=1, p=0.375),
        _reader_test(6, "reader-a", b=0, c=6, p=0.031, condition="restated in prose"),
    ]
    signal = _signals(*claims, kind="multiplicity")[0]
    assert signal.detail["tests"] == 5  # readers a-e; the restatement of reader-a is not a test
    assert signal.detail["lost_claim_ids"] == ["C-1", "C-2"]


def _relation(number: int, target: str, **fields: object) -> ClaimRecord:
    return _record(number, experiment="D1", relations=[{
        "target_experiment": target, "relation": "corrects"}], **fields)


def test_relation_target_prefers_the_longer_id_named_in_the_claim() -> None:
    hack_drop = _record(1, experiment="C1")
    folder = _record(2, experiment="ov-c1-c5-live-rollouts-20261101")
    correction = _relation(3, "C1", statement="The earlier C1-C5 report counted two as attempts.")
    signal = _signals(hack_drop, folder, correction, kind="contradiction")[0]
    assert signal.detail["target_experiment"] == "ov-c1-c5-live-rollouts-20261101"
    assert "C-1" not in signal.claim_ids and "C-2" in signal.claim_ids
    exact = _relation(4, "C1", statement="The earlier hack-drop numbers were wrong.")
    assert _signals(hack_drop, folder, exact, kind="contradiction")[0].detail[
        "target_experiment"] == "C1"


def test_ambiguous_relation_target_is_refused() -> None:
    claims = [_record(1, experiment="C1"), _record(2, experiment="ov-c1-c5-a-20261101"),
              _record(3, experiment="ov-c1-c5-b-20261101"),
              _relation(4, "C1", statement="The earlier C1-C5 report counted two as attempts.")]
    assert not _signals(*claims, kind="contradiction")
    unresolved = [_record(1, experiment="C1"),
                  _relation(2, "C1", statement="The earlier C1-C5 report counted two as attempts.")]
    assert not _signals(*unresolved, kind="contradiction")


def test_one_contradiction_per_retraction_not_per_restating_claim() -> None:
    first = _relation(1, "E9", metric="recall", statement="Retracts 9/10 recall at 0/10 FA; now 7/10.")
    second = _relation(2, "E9", metric="recall and FA", statement="Corrected 9/10 recall, 0/10 FA.")
    other = _relation(3, "E9", metric="latency", statement="Latency was 3 s, not 5 s.")
    signals = _signals(first, second, other, kind="contradiction")
    assert sorted(s.detail["correcting_claim_ids"] for s in signals) == [["C-1", "C-2"], ["C-3"]]


def test_small_n_groups_columns_of_one_table_row_and_skips_bounded_rows() -> None:
    row = {"section_id": "X-row", "metric": "ASR", "n": 4}
    columns = [_record(i, k=k, condition=name, source_id="F-table", **row) for i, (name, k) in
               enumerate([("H1", 0), ("HS", 0), ("L0", 4)], start=1)]
    signals = _signals(*columns, kind="extreme_small_n")
    assert len(signals) == 1 and signals[0].claim_ids == ["C-1", "C-2", "C-3"]
    bounded = _record(9, k=2, n=2, metric="precision",
                      quote="Precision 2/2 = 100% $[34.2\\%, 100.0\\%]$ in the table.")
    assert not _signals(bounded, kind="extreme_small_n")
    gate = _record(10, k=4, n=4, subject="Gate 1", dataset="TraceMonitor", condition="sol_astra",
                   statement="The gate decision rests on 4/4 recall.")
    restated = _record(11, k=4, n=4, subject=None, dataset="sol_astra", condition=None)
    assert [s.claim_ids for s in _signals(gate, restated, kind="extreme_small_n")] == [["C-10", "C-11"]]


def _design_claim(number: int = 1, statement: str | None = None) -> ClaimRecord:
    return _record(number, kind="method", metric=None, subject=None, dataset=None, n=324,
                   statement=statement or "Phase 2 ran 18 cheats x 6 variants x 3 repeats = 324 trials.",
                   quote="18 cheats x 6 variants x 3 repeats = 324 trials")


def _trial_test(number: int, *, n: int, p: float, experiment: str = "E1") -> ClaimRecord:
    return _record(number, experiment=experiment, kind="comparison", metric="flip rate",
                   subject=None, dataset=None, k=3, n=n,
                   comparison={"test": "mcnemar_exact", "b": 35, "c": 10, "p_value": p})


def test_pseudo_replication_needs_explicit_cases_and_trials() -> None:
    design, tested = _design_claim(), _trial_test(2, n=324, p=0.000247)
    signal = _signals(design, tested, kind="pseudo_replication")[0]
    assert signal.detail["cases"] == 18 and signal.detail["trials"] == 324
    assert signal.claim_ids == ["C-1", "C-2"]
    sub_family = _trial_test(3, n=54, p=0.01)  # 18 cases x 3 repeats of one variant
    assert _signals(design, tested, sub_family, kind="pseudo_replication")[0].detail["tests"][1][
        "n"] == 54
    assert not _signals(design, _trial_test(2, n=324, p=0.2), kind="pseudo_replication")
    assert not _signals(design, _trial_test(2, n=300, p=0.001), kind="pseudo_replication")
    assert not _signals(design, _trial_test(2, n=18, p=0.001), kind="pseudo_replication")
    other = _trial_test(2, n=324, p=0.001, experiment="E2")
    assert not _signals(design, other, kind="pseudo_replication")
    wrong_total = _design_claim(statement="Phase 2 ran 18 cheats x 6 variants x 3 repeats = 300 trials.")
    assert not _signals(wrong_total, tested, kind="pseudo_replication")
    assert not _signals(_design_claim(statement="Ran 18 cheats x 6 files x 3 repeats = 324 trials."),
                        tested, kind="pseudo_replication")
