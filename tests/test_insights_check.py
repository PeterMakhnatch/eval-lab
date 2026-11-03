from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evallab.insights.check import check_card, check_extracted_claim, unsupported_numbers
from evallab.insights.models import (
    CheckResult,
    ClaimRecord,
    ExtractedClaim,
    InsightCard,
    Recipe,
    SourceRecord,
)
from evallab.insights.stats import compute


def _source(source_id: str, folder: str) -> SourceRecord:
    return SourceRecord(
        source_id=source_id, path=f"{folder}/report.md", family="experiment", kind="report",
        sha256="digest", size=1, mtime=datetime(2026, 11, 2, tzinfo=UTC), folder=folder,
    )


def _claim(**changes: object) -> ExtractedClaim:
    return ExtractedClaim.model_validate(
        {
            "local_id": "rate",
            "experiment": "E1",
            "statement": "The treatment passed 21 of 28 trials (75%).",
            "kind": "measurement",
            "k": 21,
            "n": 28,
            "value": 0.75,
            "quote": "The treatment passed 21 of 28 trials (75%).",
            "section_id": "section-a",
            **changes,
        }
    )


def _record(claim_id: str = "C1", **changes: object) -> ClaimRecord:
    return ClaimRecord.model_validate(
        {
            "claim_id": claim_id,
            "source_id": "source-a",
            "section_sha256": "section-digest",
            "fingerprint": "claim-digest",
            "pass_id": "extractor-a",
            "extracted_by": "synthetic",
            "created_at": datetime(2026, 11, 2, tzinfo=UTC),
            "claim": _claim(),
            "check": CheckResult(status="pass"),
            **changes,
        }
    )


def _card(**changes: object) -> InsightCard:
    return InsightCard.model_validate(
        {
            "local_id": "card-a",
            "kind": "anomaly",
            "title": "A measured difference",
            "statement": "The measured rate is 21 of 28 (75%), not a universal guarantee.",
            "evidence": [{"claim_id": "C1"}],
            "observations": ["The source reports 21 of 28 trials."],
            "interpretation": "Sampling uncertainty remains a possible explanation.",
            "why_not_obvious": "The source headline omits the sampling uncertainty.",
            "alternatives": ["The treatment may have a genuine effect."],
            "confidence": "medium",
            **changes,
        }
    )


@pytest.mark.parametrize("quote", ["21/28 passed (75%).", "21 of 28 passed (75%)."])
def test_claim_counts_fraction_and_of_forms(quote: str) -> None:
    claim = _claim(quote=quote)
    assert check_extracted_claim(claim, f"A header. {quote} A footer.").status == "pass"


def test_claim_normalizes_unicode_and_whitespace_in_quote() -> None:
    claim = _claim(quote="The ‘rate’ — 21 of 28 (75%).")
    section = "The 'rate' - 21\n of 28   (75%)."
    assert check_extracted_claim(claim, section).status == "pass"


def test_claim_accepts_comma_separated_counts() -> None:
    claim = _claim(k=1209, n=2000, value=0.6045, quote="1,209 of 2,000 passed (60.45%).")
    assert check_extracted_claim(claim, claim.quote).status == "pass"


def test_claim_fails_unverifiable_quote_or_missing_count() -> None:
    claim = _claim()
    assert check_extracted_claim(claim, "An unrelated source section.").status == "fail"
    missing = _claim(quote="The treatment passed 75% of trials.")
    result = check_extracted_claim(missing, missing.quote)
    assert result.status == "fail"
    assert any("k = 21" in note for note in result.notes)
    assert any("n = 28" in note for note in result.notes)
    # An identifier containing a count does not supply evidence for that count.
    identifier = _claim(quote="C21 passed 28 trials at 75%.")
    assert check_extracted_claim(identifier, identifier.quote).status == "fail"


def test_claim_fails_value_not_quoted_or_supported_by_counts() -> None:
    claim = _claim(value=0.37)
    result = check_extracted_claim(claim, claim.quote)
    assert result.status == "fail"
    assert any("not in the quote" in note for note in result.notes)
    # A unitless in-range value that is quoted but disagrees with k/n is ambiguous: no note.
    quoted = _check(value=0.37, quote="The treatment passed 21 of 28 trials; correlation 0.37.")
    assert quoted.status == "pass", quoted.notes
    qualitative = _claim(k=None, n=None, value=0.37, quote="No numerical rate was reported.")
    assert check_extracted_claim(qualitative, qualitative.quote).status == "fail"


def test_claim_accepts_printed_value_or_ratio_rounding() -> None:
    printed = _claim(k=None, n=None, value=0.293, quote="The measured rate was 29.3%.")
    assert check_extracted_claim(printed, printed.quote).status == "pass"
    decimal = _claim(k=None, n=None, value=0.293, quote="The measured rate was 0.293.")
    assert check_extracted_claim(decimal, decimal.quote).status == "pass"
    rounded = _claim(k=22, n=75, value=0.293, quote="22 of 75 trials passed.")
    assert check_extracted_claim(rounded, rounded.quote).status == "pass"


def test_claim_warns_when_reported_interval_differs_from_wilson() -> None:
    claim = _claim(ci_low=0.74, ci_high=0.76)
    result = check_extracted_claim(claim, claim.quote)
    assert result.status == "warn"
    assert any("method may differ" in note for note in result.notes)
    bounds = compute("wilson", {"k": 21, "n": 28})
    consistent = _claim(ci_low=bounds["low"], ci_high=bounds["high"])
    assert check_extracted_claim(consistent, consistent.quote).status == "pass"


def test_claim_mcnemar_warning_requires_absolute_and_relative_error() -> None:
    wrong = _claim(comparison={"test": "mcnemar_exact", "b": 6, "c": 4, "p_value": 0.2})
    assert check_extracted_claim(wrong, wrong.quote).status == "warn"
    small_absolute = _claim(
        comparison={"test": "mcnemar_exact", "b": 21, "c": 0, "p_value": 0.001}
    )
    assert check_extracted_claim(small_absolute, small_absolute.quote).status == "pass"
    small_relative = _claim(
        comparison={"test": "mcnemar_exact", "b": 6, "c": 4, "p_value": 0.75}
    )
    assert check_extracted_claim(small_relative, small_relative.quote).status == "pass"


def test_claim_warns_test_split_and_notes_in_sample() -> None:
    claim = _claim(split="test", in_sample=True)
    result = check_extracted_claim(claim, claim.quote)
    assert result.status == "warn"
    assert any("DEV-only" in note for note in result.notes)
    assert any("in-sample" in note for note in result.notes)


def test_card_passes_with_supported_evidence_and_numeric_forms() -> None:
    result = check_card(_card(), claims={"C1": _record()}, known_insights=set())
    assert result.status == "pass"


@pytest.mark.parametrize(
    "changes",
    [
        {"stale": True},
        {"check": {"status": "fail", "notes": ["The quote is absent."]}},
        {"recompute": {"status": "differs", "detail": "Receipt differs."}},
    ],
)
def test_card_fails_stale_or_failed_evidence(changes: dict[str, object]) -> None:
    result = check_card(_card(), claims={"C1": _record(**changes)}, known_insights=set())
    assert result.status == "fail"


def test_card_fails_unknown_evidence_and_challenge_targets() -> None:
    assert check_card(_card(), claims={}, known_insights=set()).status == "fail"
    card = _card(
        kind="challenge",
        challenge={
            "target_claim_ids": ["unknown"],
            "flaw": "small_n",
            "severity": "major",
            "check": "Collect more independent trials.",
            "decision_at_risk": "Treatment selection.",
        },
    )
    result = check_card(card, claims={"C1": _record()}, known_insights=set())
    assert result.status == "fail"
    assert any("unknown" in note for note in result.notes)
    stale_target = _record("unknown", stale=True)
    result = check_card(
        card, claims={"C1": _record(), "unknown": stale_target}, known_insights=set()
    )
    assert result.status == "fail"
    assert any("stale" in note for note in result.notes)


def test_card_warns_but_does_not_fail_on_a_partly_recomputed_claim() -> None:
    partial = {"status": "partial", "validated": ["k"], "unchecked": ["n"], "detail": "Partial."}
    result = check_card(_card(), claims={"C1": _record(recompute=partial)}, known_insights=set())
    assert result.status == "warn"
    assert any("k matches receipt; n not checked" in note for note in result.notes)


def test_connection_requires_distinct_source_folders_not_experiment_ids() -> None:
    card = _card(kind="connection", evidence=[{"claim_id": "C1"}, {"claim_id": "C2"}])
    claims = {"C1": _record(), "C2": _record("C2", source_id="source-b")}
    same_folder = {"source-a": _source("source-a", "/r/B3"), "source-b": _source("source-b", "/r/B3")}
    assert check_card(card, claims=claims, known_insights=set(), sources=same_folder).status == "fail"
    # Two experiment ids from one folder are one place, however the extractor labelled them.
    aliased = {"C1": _record(), "C2": _record("C2", source_id="source-b", claim=_claim(experiment="E2"))}
    result = check_card(card, claims=aliased, known_insights=set(), sources=same_folder)
    assert result.status == "fail" and any("source folders" in note for note in result.notes)
    folders = {"source-a": _source("source-a", "/r/B3"), "source-b": _source("source-b", "/r/F4")}
    assert check_card(card, claims=claims, known_insights=set(), sources=folders).status == "pass"
    # Without source records, distinct source ids stand in for distinct folders.
    assert check_card(card, claims=claims, known_insights=set()).status == "pass"


def test_computed_stat_rechecks_values_shape_and_boolean_flags() -> None:
    args = {"k": 21, "n": 28}
    result = compute("wilson", args)
    card = _card(computed=[{"op": "wilson", "args": args, "result": result}])
    claims = {"C1": _record()}
    assert check_card(card, claims=claims, known_insights=set()).status == "pass"
    for bad in ({**result, "p": 0.37}, {"p": 0.75}, {**result, "extra": 1}):
        card = _card(computed=[{"op": "wilson", "args": args, "result": bad}])
        assert check_card(card, claims=claims, known_insights=set()).status == "fail"
    card = _card(computed=[{"op": "wilson", "args": {"k": 5, "n": 1}, "result": {}}])
    assert check_card(card, claims=claims, known_insights=set()).status == "fail"
    card = _card(
        computed=[
            {
                "op": "bh",
                "args": {"p_values": [0.01]},
                "result": {"adjusted": [0.01], "rejected": [False]},
            }
        ]
    )
    assert check_card(card, claims=claims, known_insights=set()).status == "fail"


def test_card_fails_invented_title_percentage_and_warns_for_unknown_predecessor() -> None:
    card = _card(
        statement="The measured rate is 37%, which is not established by the source.",
        builds_on=["missing-insight"],
    )
    result = check_card(card, claims={"C1": _record()}, known_insights=set())
    assert result.status == "fail"
    assert any("37%" in note for note in result.notes)
    assert any("builds_on" in note for note in result.notes)


def test_unsupported_numbers_ignores_identifiers_times_years_and_list_markers() -> None:
    text = (
        "1. K7B and C1 use L0, v2 and gpt-6.1. PR #1734 landed in 2026 at 03:25.\n"
        "2) The invented rate is 37%."
    )
    assert unsupported_numbers(text, []) == ["37%"]
    assert unsupported_numbers("The value is 37.", []) == ["37"]


def test_unsupported_numbers_percent_decimal_fraction_and_scientific_forms() -> None:
    allowed = [22, 75, 0.293, "p = 1e-6"]
    assert unsupported_numbers("22/75 is 29.3%, also 0.293; p=1e-6.", allowed) == []
    assert unsupported_numbers("The rate is .293 and 29.7%.", [0.293]) == []
    assert unsupported_numbers("The rate is 30.0%.", [0.293]) == ["30.0%"]
    assert unsupported_numbers("1,209 of 2,000 trials.", [1209, 2000]) == []
    assert unsupported_numbers("Rate 37%; count 37.", [37]) == ["37%"]


def test_unsupported_numbers_reads_a_dash_after_a_percent_as_a_range() -> None:
    # An interval printed as "43.6%–97.0%" is two positive numbers, not a negative one.
    assert unsupported_numbers("Wilson 43.6%–97.0% and (2)-5 cases.", [0.4365, 0.9699, 2, 5]) == []
    assert unsupported_numbers("The change is -3 points.", [3]) == ["-3"]


def _check(**changes: object) -> CheckResult:
    claim = _claim(**changes)
    return check_extracted_claim(claim, claim.quote)


@pytest.mark.parametrize(
    "changes",
    [
        # Percent stored with a percent-bearing unit, LaTeX percent, ratio, seconds, USD.
        {"k": None, "n": None, "value": 60.0, "unit": "% per worker-hour",
         "quote": "Throughput rose 60.0% per worker-hour."},
        {"k": None, "n": None, "value": 16.7, "unit": "%", "quote": "The rate was 16.7\\%."},
        {"k": None, "n": None, "value": 3.2, "unit": "x", "quote": "The agent was 3.2x faster."},
        {"k": None, "n": None, "value": 93600, "unit": "s", "quote": "The run took 93,600s."},
        {"k": None, "n": None, "value": 0.04, "unit": "s", "quote": "Latency was 40ms."},
        {"k": None, "n": None, "value": 15, "unit": "USD", "quote": "The run cost $15."},
        # A count or currency value is not a proportion, so k/n comparisons do not apply.
        {"k": 1, "n": 3, "value": 1.0, "unit": "calls", "quote": "1.0 calls out of 3 trials."},
        {"k": 200, "n": 200, "value": 1.3, "unit": "USD", "quote": "200 of 200 trials cost $1.3."},
        # Percent unit with k/n: value is checked on the percent scale.
        {"value": 75.0, "unit": "%", "quote": "21 of 28 trials passed (75%)."},
    ],
)
def test_claim_value_is_unit_aware_with_no_false_alarms(changes: dict[str, object]) -> None:
    result = _check(**changes)
    assert result.status == "pass", result.notes


@pytest.mark.parametrize(
    "changes",
    [
        # The two real value-scale corruptions from the claim audit.
        {"k": None, "n": None, "value": 0.0024, "unit": "s",
         "quote": "The resume lag was .243 seconds."},
        {"k": None, "n": None, "value": 0.7253, "unit": "s", "quote": "The lag was 72.53s."},
        {"k": None, "n": None, "value": 5.0, "unit": "USD", "quote": "The run cost $15."},
    ],
)
def test_claim_fails_value_scale_corruption(changes: dict[str, object]) -> None:
    result = _check(**changes)
    assert result.status == "fail"
    assert any("not in the quote" in note for note in result.notes)


def test_claim_ci_on_the_wrong_scale_fails_but_declared_percent_passes() -> None:
    quote = "79 of 158 passed (50%), 95% interval 42.3-57.7%."
    wrong = _check(k=79, n=158, value=0.5, ci_low=42.3, ci_high=57.7, quote=quote)
    assert wrong.status == "fail"
    assert any("[0,1]" in note for note in wrong.notes)
    declared = _check(
        k=79, n=158, value=50.0, unit="%", ci_low=42.3, ci_high=57.7, quote=quote,
    )
    assert declared.status == "pass", declared.notes
    bounds = compute("wilson", {"k": 79, "n": 158})
    fraction = _check(k=79, n=158, value=0.5, ci_low=bounds["low"], ci_high=bounds["high"],
                      quote=quote)
    assert fraction.status == "pass"
    reversed_bounds = _check(k=79, n=158, value=0.5, ci_low=0.6, ci_high=0.4, quote=quote)
    assert reversed_bounds.status == "fail"
    # Without a value, a k/n interval is still a proportion interval.
    no_value = _check(k=79, n=158, value=None, ci_low=42.3, ci_high=57.7, quote=quote)
    assert no_value.status == "fail"


def test_percent_and_fraction_scales_must_agree_between_value_unit_and_interval() -> None:
    quote = "79 of 158 passed (50%), 95% interval 42.3-57.7%."
    # Percent unit, interval stored as fractions: a scale mix, not "a different method".
    mixed = _check(k=79, n=158, value=50.0, unit="%", ci_low=0.423, ci_high=0.577, quote=quote)
    assert mixed.status == "fail"
    assert any("fractions but the unit declares percent" in note for note in mixed.notes)
    # The same pair with a percent-scale value is declared consistently and passes.
    declared = _check(k=79, n=158, value=50.0, unit="%", ci_low=42.3, ci_high=57.7, quote=quote)
    assert declared.status == "pass", declared.notes
    # Fraction unit, percent-scale interval: out of range.
    fraction_unit = _check(
        k=79, n=158, value=0.5, unit="fraction", ci_low=42.3, ci_high=57.7, quote=quote,
    )
    assert fraction_unit.status == "fail" and any("[0,1]" in n for n in fraction_unit.notes)


def test_rate_intervals_without_counts_are_range_and_order_checked() -> None:
    quote = "Recall was 50% (95% interval 42.3-57.7%)."
    fields = {"k": None, "n": None, "metric": "recall", "quote": quote}
    bad_scale = _check(value=0.5, ci_low=42.3, ci_high=57.7, **fields)
    assert bad_scale.status == "fail" and any("[0,1]" in note for note in bad_scale.notes)
    above_one = _check(value=0.5, unit="fraction", ci_low=0.4, ci_high=1.2, **fields)
    assert above_one.status == "fail" and any("[0,1]" in note for note in above_one.notes)
    negative = _check(value=0.5, unit="fraction", ci_low=-0.1, ci_high=0.6, **fields)
    assert negative.status == "fail"
    swapped = _check(value=0.5, unit="fraction", ci_low=0.6, ci_high=0.4, **fields)
    assert swapped.status == "fail" and any("reversed" in note for note in swapped.notes)
    percent = _check(value=50.0, unit="%", ci_low=42.3, ci_high=57.7, **fields)
    assert percent.status == "pass", percent.notes
    fraction = _check(value=0.5, unit="fraction", ci_low=0.423, ci_high=0.577, **fields)
    assert fraction.status == "pass", fraction.notes
    mixed = _check(value=50.0, unit="%", ci_low=0.423, ci_high=0.577, **fields)
    assert mixed.status == "fail" and any("fractions but" in note for note in mixed.notes)
    # A non-proportion measurement keeps whatever interval its unit allows.
    seconds = _check(k=None, n=None, value=3.2, unit="s", ci_low=2.0, ci_high=4.0,
                     quote="took 3.2 s (95% interval 2.0-4.0 s)")
    assert seconds.status == "pass", seconds.notes


def test_claim_interval_checks_skip_non_binomial_intervals() -> None:
    bootstrap = _check(
        k=5, n=10, value=0.9, metric="auroc", ci_low=0.81, ci_high=0.96,
        quote="AUROC 0.9 (bootstrap interval 0.81-0.96) on 5 of 10 cases.",
    )
    assert bootstrap.status == "pass", bootstrap.notes
    percent_auroc = _check(
        k=5, n=10, value=90.0, unit="%", metric="auroc", ci_low=81.0, ci_high=96.0,
        quote="AUROC 90% (bootstrap interval 81%-96%) on 5 of 10 cases.",
    )
    assert percent_auroc.status == "pass", percent_auroc.notes


@pytest.mark.parametrize(
    ("changes", "needle"),
    [
        ({"k": 21, "n": 21, "value": 1.0, "quote": "All 21 trials passed (100%)."}, "distinct"),
        ({"k": 50, "n": 100, "value": 0.5, "quote": "50% of 100 trials passed."}, "k = 50"),
        ({"k": 2026, "n": 2026, "value": 1.0, "quote": "On 2026-11-02 every trial passed."},
         "k = 2026"),
    ],
)
def test_claim_counts_need_distinct_non_percent_non_date_tokens(
    changes: dict[str, object], needle: str,
) -> None:
    result = _check(**changes)
    assert result.status == "fail"
    assert any(needle in note for note in result.notes)
    assert _check(k=21, n=21, value=1.0, quote="21 of 21 trials passed (100%).").status == "pass"


def test_card_checks_every_free_text_field() -> None:
    claims = {"C1": _record()}
    for field, value in (
        ("title", "A 99% certainty on 40 runs"),
        ("statement", "The measured rate is 99% across the runs."),
    ):
        result = check_card(_card(**{field: value}), claims=claims, known_insights=set())
        assert result.status == "fail" and any(field in note for note in result.notes)
    warned = {
        "why_not_obvious": ("why_not_obvious", "The headline hides 7 excluded trials entirely."),
        "alternatives": ("alternative", ["A 63% baseline could explain the gap."]),
        "observations": ("observation", ["The source reports 17 of 28 trials."]),
        "interpretation": ("interpretation", "The gap of 64 points is large for this sample."),
    }
    for field, (label, value) in warned.items():
        result = check_card(_card(**{field: value}), claims=claims, known_insights=set())
        assert result.status == "warn" and any(label in note for note in result.notes), field
    challenge = _card(
        kind="challenge",
        challenge={
            "target_claim_ids": ["C1"], "flaw": "small_n", "severity": "major",
            "check": "Collect 400 more paired trials.", "decision_at_risk": "Pick the 1 best.",
        },
    )
    result = check_card(challenge, claims=claims, known_insights=set())
    assert result.status == "warn" and any("challenge check" in note for note in result.notes)
    prediction = _card(
        kind="mechanism",
        predictions=[{
            "statement": "The rate will exceed 91% next time.", "direction": "higher", "low": 0.91,
            "resolves_when": "A later run reports at least 40 trials.",
        }],
    )
    result = check_card(prediction, claims=claims, known_insights=set())
    assert result.status == "warn" and any("prediction" in note for note in result.notes)
    proposal = _card(
        kind="idea",
        predictions=[{
            "statement": "The lift appears in follow-up runs.", "direction": "higher",
            "low": 0.1, "resolves_when": "A later run reports the lift.",
        }],
        proposed_experiment={
            "hypothesis": "A 12% lift would show up across 300 cases.",
            "primary_variable": "prompt", "fixed_variables": ["model"], "arms": ["a", "b"],
            "cost_basis": "About 90 USD of replays", "needs_approval": True,
            "reuses_existing_data": True, "expected_if_true": "Lift",
            "expected_if_false": "No lift",
        },
    )
    result = check_card(proposal, claims=claims, known_insights=set())
    assert result.status == "warn" and any("proposal" in note for note in result.notes)


def test_card_numbers_are_supported_by_cited_claims_and_cited_section_text() -> None:
    claims = {"C1": _record()}
    card = _card(observations=["The report also lists 9 timeouts."])
    assert check_card(card, claims=claims, known_insights=set()).status == "warn"
    sections = {"section-a": "The treatment passed 21 of 28 trials. 9 timeouts were excluded."}
    supported = check_card(card, claims=claims, known_insights=set(), section_texts=sections)
    assert supported.status == "pass", supported.notes
    # Section text from a claim the card does not cite supports nothing.
    other = {"section-b": "9 timeouts were excluded."}
    assert check_card(
        card, claims=claims, known_insights=set(), section_texts=other,
    ).status == "warn"


def test_card_glued_unit_numbers_are_checked_not_masked() -> None:
    claims = {"C1": _record()}
    assert unsupported_numbers("a 36pp gap, 3x faster, 40ms, $15", []) == [
        "36pp", "3x", "40ms", "$15",
    ]
    assert unsupported_numbers("Latency 40ms over 3x.", ["0.04s", "3x"]) == []
    assert unsupported_numbers("A 36pp gap.", ["36%"]) == []
    # Identifiers with digits and zero-padded ids are still not numbers.
    assert unsupported_numbers("Rows R7B and v2 and gpt-6.1.", []) == []
    card = _card(title="Resolves 3x faster")
    assert check_card(card, claims=claims, known_insights=set()).status == "fail"


def test_computed_stat_inputs_must_come_from_cited_claims() -> None:
    claims = {"C1": _record()}
    invented = {"k": 85, "n": 100}
    result = compute("wilson", invented)
    card = _card(
        statement="Across 100 runs the rate is 85%, within 76.7-90.7%.",
        computed=[{"op": "wilson", "args": invented, "result": result}],
    )
    checked = check_card(card, claims=claims, known_insights=set())
    assert checked.status == "fail"
    assert any("inputs" in note for note in checked.notes)
    assert any("statement" in note and "85%" in note for note in checked.notes)
    real = {"k": 21, "n": 28}
    bounds = compute("wilson", real)
    grounded = _card(
        statement=f"The interval for 21 of 28 is {bounds['low']:.1%}-{bounds['high']:.1%}.",
        computed=[{"op": "wilson", "args": real, "result": bounds}],
    )
    assert check_card(grounded, claims=claims, known_insights=set()).status == "pass"
    # A 2x2 table may use complements of cited counts (the margins are cited).
    table = {"a": 21, "b": 7, "c": 7, "d": 21}
    two_by_two = _card(
        computed=[{"op": "fisher", "args": table, "result": compute("fisher", table)}],
    )
    paired = {"C1": _record(claim=_claim(quote="The treatment passed 21 of 28 trials; "
                                                 "controls passed 7 of 28 trials."))}
    assert check_card(two_by_two, claims=paired, known_insights=set()).status == "pass"


def test_card_recipes_are_rerun_and_their_values_become_supported(tmp_path: Path) -> None:
    (tmp_path / "summary.json").write_text(json.dumps({"caught": 9}), encoding="utf-8")
    recipe = Recipe(op="value", file="summary.json", pointer="/caught")
    card = _card(
        observations=["The receipt shows 9 timeouts."],
        recipes=[{"recipe": recipe, "folder": str(tmp_path), "value": 9}],
    )
    claims = {"C1": _record()}
    assert check_card(card, claims=claims, known_insights=set()).status == "pass"
    wrong = card.model_copy(update={"recipes": [
        card.recipes[0].model_copy(update={"value": 10}),
    ]})
    result = check_card(wrong, claims=claims, known_insights=set())
    assert result.status == "fail" and any("recorded value" in note for note in result.notes)
    missing = card.model_copy(update={"recipes": [card.recipes[0].model_copy(update={
        "recipe": Recipe(op="value", file="absent.json", pointer="/caught"),
    })]})
    assert check_card(missing, claims=claims, known_insights=set()).status == "fail"
