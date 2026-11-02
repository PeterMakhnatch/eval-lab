from __future__ import annotations

from datetime import UTC, datetime

import pytest

from evallab.insights.check import check_card, check_extracted_claim, unsupported_numbers
from evallab.insights.models import (
    CheckResult,
    ClaimRecord,
    ExtractedClaim,
    InsightCard,
)
from evallab.insights.stats import compute


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


def test_claim_warns_for_value_not_quoted_or_supported_by_counts() -> None:
    claim = _claim(value=0.37)
    result = check_extracted_claim(claim, claim.quote)
    assert result.status == "warn"
    assert any("not in the quote" in note for note in result.notes)
    assert any("differs from k/n" in note for note in result.notes)
    qualitative = _claim(k=None, n=None, value=0.37, quote="No numerical rate was reported.")
    assert check_extracted_claim(qualitative, qualitative.quote).status == "warn"


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


def test_connection_requires_distinct_experiments_not_distinct_claims() -> None:
    card = _card(kind="connection", evidence=[{"claim_id": "C1"}, {"claim_id": "C2"}])
    claims = {"C1": _record(), "C2": _record("C2")}
    assert check_card(card, claims=claims, known_insights=set()).status == "fail"
    claims["C2"] = _record("C2", claim=_claim(experiment="E2"))
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


def test_card_warns_for_unknown_predecessor_and_invented_percentage() -> None:
    card = _card(
        statement="The measured rate is 37%, which is not established by the source.",
        builds_on=["missing-insight"],
    )
    result = check_card(card, claims={"C1": _record()}, known_insights=set())
    assert result.status == "warn"
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
