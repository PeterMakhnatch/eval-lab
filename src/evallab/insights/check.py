"""Deterministic evidence, arithmetic and unsupported-number checks."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from evallab.insights.models import (
    CheckResult,
    ClaimRecord,
    ExtractedClaim,
    InsightCard,
    SourceRecord,
    normalize_text,
)
from evallab.insights.recompute import RecipeError, describe_recompute, run_recipe
from evallab.insights.stats import compute, mcnemar_exact, wilson
from evallab.insights.units import (
    Quantity,
    as_fraction,
    canonical_value,
    normalize_unit,
    parse_quantity,
    printed_tolerance,
    unit_kind,
)

_UNSIGNED = r"(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?"
_NUMBER_BODY = rf"[-+]?{_UNSIGNED}"
_SUFFIX = (
    r"(?:[ \t]*(?:\\?%|pp|milliseconds?|ms|microseconds?|us|µs|μs|nanoseconds?|ns|"
    r"seconds?|secs?|s|minutes?|mins?|hours?|hrs?|h|usd|USD|x|k|B))?"
)
# A sign right after a percent sign or closing bracket is a range dash ("43.6%-97.0%").
_NUMBER = re.compile(
    rf"(?<![\w.])(?:\$)?((?:(?<![%)\]])[-+])?{_UNSIGNED}){_SUFFIX}(?!\w|\.\d)"
)
_IDENTIFIER = re.compile(r"(?<![\w.])(?=[\w.-]*[A-Za-z])\w+(?:[.-]\w+)*(?!\w)")
_EXEMPT = re.compile(r"(?<!\w)#\d+\b|\b\d{1,2}:\d{2}(?::\d{2})?\b|(?m:^\s*\d+[.)]\s+)")
_RATE_METRIC = re.compile(
    r"recall|precision|(?:^|_)rate(?:\b|_)|capture|detection|survival|accuracy|agreement|asr"
)
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_NOT_BINOMIAL = re.compile(r"\b(?:bootstrap\w*|auroc|auc|roc)\b", re.IGNORECASE)


@dataclass(frozen=True)
class _Number:
    text: str
    value: float
    unit: str
    kind: str

    @property
    def percent(self) -> bool:
        return self.kind == "percent"

    @property
    def normalized(self) -> float:
        return canonical_value(self.value, self.unit)

    @property
    def fraction(self) -> float | None:
        return as_fraction(self.value, self.unit)


def _from_quantity(quantity: Quantity) -> _Number:
    return _Number(quantity.text, quantity.value, quantity.unit, quantity.kind)


def _tokens(text: str, *, ignore_context: bool = True) -> list[_Number]:
    text = "\n".join(normalize_text(line) for line in text.splitlines())
    if ignore_context:
        text = _EXEMPT.sub(lambda match: " " * len(match.group()), text)

    def mask_identifier(match: re.Match[str]) -> str:
        token = match.group()
        # Scientific notation and glued units are quantities, not identifiers such as K7B.
        quantity = parse_quantity(token)
        if re.fullmatch(_NUMBER_BODY, token) or (
            quantity is not None and quantity.kind != "other"
        ):
            return token
        return " " * len(token)

    text = _IDENTIFIER.sub(mask_identifier, text)
    result = []
    for match in _NUMBER.finditer(text):
        quantity = parse_quantity(match.group())
        if quantity is None:
            continue
        if (
            ignore_context
            and not quantity.unit
            and 2020 <= quantity.value <= 2030
            and quantity.value.is_integer()
        ):
            continue
        result.append(_from_quantity(quantity))
    return result


def _same_number(left: _Number, right: _Number) -> bool:
    left_fraction = left.fraction
    right_fraction = right.fraction
    if left_fraction is not None and right_fraction is not None:
        return abs(left_fraction - right_fraction) <= 0.0051 + 1e-12
    if left.percent or right.percent:
        return False
    if not left.unit and not right.unit and abs(left.value) <= 1 and abs(right.value) <= 1:
        return abs(left.value - right.value) <= 0.0051 + 1e-12
    if left.kind != right.kind and left.kind != "other" and right.kind != "other":
        return False
    return math.isclose(left.normalized, right.normalized, rel_tol=1e-9, abs_tol=1e-12)


def unsupported_numbers(text: str, allowed: Iterable[str | float | int]) -> list[str]:
    """Find unsupported numeric tokens, ignoring labels, PRs, times and years.

    A fraction supplies two numeric tokens. Percentages match their proportion form,
    and glued units retain their numeric prefix instead of being hidden as identifiers.
    """
    supported: list[_Number] = []
    for value in allowed:
        if isinstance(value, str):
            supported.extend(_tokens(value, ignore_context=False))
        elif not isinstance(value, bool) and math.isfinite(value):
            supported.append(_Number(str(value), float(value), "", "other"))
    result = []
    for token in _tokens(text):
        if token.text not in result and not any(
            _same_number(token, candidate) for candidate in supported
        ):
            result.append(token.text)
    return result


def _quoted_tolerance(value: float, unit: str | None, number: _Number) -> float | None:
    """Printed-rounding tolerance (canonical units) if ``number`` prints ``value``."""
    stated = canonical_value(value, unit)
    options = [(number.normalized, printed_tolerance(number.text))]
    if unit_kind(unit) == "percent" and not number.unit:
        # A bare number beside a percent unit is a percent: 64.3 for 64.3 (%).
        options.append((number.value / 100, printed_tolerance(number.text, unit="%")))
    elif not normalize_unit(unit) and number.percent and value > 1:
        # A percent stored without its unit: 75 for "75%".
        scale = canonical_value(1.0, "%")
        options.append((number.value, printed_tolerance(number.text) / scale))
    matches = [tol for candidate, tol in options if abs(candidate - stated) <= tol + 1e-12]
    return min(matches) if matches else None


def _value_quoted(value: float, unit: str | None, numbers: list[_Number]) -> bool:
    return any(_quoted_tolerance(value, unit, number) is not None for number in numbers)


def value_printed_tolerance(claim: ExtractedClaim) -> float:
    """Tolerance for a scalar: half a unit of its tightest printed form in the quote."""
    if claim.value is None:
        return 1e-9
    matches = [
        tolerance for number in _tokens(claim.quote, ignore_context=False)
        if (tolerance := _quoted_tolerance(claim.value, claim.unit, number)) is not None
    ]
    if matches:
        return min(matches)
    return printed_tolerance(str(claim.value), unit=claim.unit)


def _count_tokens(count: int, numbers: list[_Number]) -> list[int]:
    matches = []
    for index, number in enumerate(numbers):
        if number.kind in {"percent", "currency", "duration", "ratio", "fraction"}:
            continue
        if math.isclose(number.normalized, count, rel_tol=0, abs_tol=1e-12):
            matches.append(index)
    return matches


def is_proportion_claim(claim: ExtractedClaim) -> bool:
    """Whether the stored value is a binomial-style rate (same rule as signals)."""
    kind = unit_kind(claim.unit)
    metric = normalize_unit(claim.metric).replace(" ", "_").replace("-", "_")
    if metric in {"auroc", "auc"} or re.search(r"\bauroc\b", claim.quote, re.IGNORECASE):
        return False
    if kind in {"fraction", "percent"}:
        return True
    if claim.unit:  # a declared non-proportion unit wins over a rate-like label
        return False
    if metric == "false_alarm" or _RATE_METRIC.search(metric):
        return True
    if claim.k is None or not claim.n or claim.value is None:
        return False
    fraction = as_fraction(claim.value, claim.unit, quote=claim.quote)
    return fraction is not None and abs(fraction - claim.k / claim.n) <= 0.006 + 1e-12


def _stated_fraction(claim: ExtractedClaim) -> float | None:
    if claim.value is None:
        return claim.k / claim.n if claim.k is not None and claim.n else None
    return as_fraction(claim.value, claim.unit, quote=claim.quote)


def check_extracted_claim(claim: ExtractedClaim, section_text: str) -> CheckResult:
    failures: list[str] = []
    warnings: list[str] = []
    if normalize_text(claim.quote) not in normalize_text(section_text):
        failures.append("The quote is not present in the source section.")
    numbers = _tokens(_ISO_DATE.sub(" ", claim.quote), ignore_context=False)
    count_matches = {
        label: _count_tokens(count, numbers)
        for label, count in (("k", claim.k), ("n", claim.n))
        if count is not None
    }
    for label, matches in count_matches.items():
        if not matches:
            failures.append(f"The quote does not contain {label} = {getattr(claim, label)}.")
    if count_matches.get("k") and count_matches.get("n") and not any(
        left != right for left in count_matches["k"] for right in count_matches["n"]
    ):
        failures.append("The quote must use distinct, non-percent tokens for k and n.")
    proportion = claim.k / claim.n if claim.k is not None and claim.n else None
    is_proportion = is_proportion_claim(claim)
    stated_fraction = _stated_fraction(claim) if is_proportion else None
    if claim.value is not None:
        agrees = (
            stated_fraction is not None
            and proportion is not None
            and abs(stated_fraction - proportion) <= 0.006 + 1e-12
        )
        if not _value_quoted(claim.value, claim.unit, numbers) and not agrees:
            failures.append("The value is not in the quote and is not supported by k/n.")
        if is_proportion and proportion is not None and not agrees:
            failures.append("The stated proportion differs from k/n by more than 0.006.")
    if claim.ci_low is not None and claim.ci_high is not None and claim.ci_low > claim.ci_high:
        failures.append("The reported interval bounds are reversed.")
    # A k/n interval with no stated value is still a proportion interval unless it is a
    # bootstrap or AUROC interval, or carries a non-proportion unit.
    interval_is_proportion = is_proportion or (
        claim.value is None and unit_kind(claim.unit) == "other" and not claim.unit
        and not _NOT_BINOMIAL.search(claim.quote)
    )
    has_counts = claim.k is not None and bool(claim.n)
    if (
        claim.ci_low is not None
        and claim.ci_high is not None
        and claim.ci_low <= claim.ci_high
        and (interval_is_proportion if has_counts else is_proportion)
        and not _NOT_BINOMIAL.search(claim.quote)
    ):
        # Same scale rule as units.as_fraction: percent units divide by 100, anything else
        # is a fraction. An out-of-range bound is a data-contract error, not a method choice.
        percent = unit_kind(claim.unit) == "percent"
        scale = 100 if percent else 1
        low, high = claim.ci_low / scale, claim.ci_high / scale
        reference = stated_fraction if stated_fraction is not None else proportion
        if not (0 <= low <= 1 and 0 <= high <= 1):
            failures.append(
                "A proportion interval lies outside [0,1]; it is on the wrong scale "
                "(use fractions, or declare percent units)."
            )
        elif (
            percent
            and reference is not None
            and not low - 0.006 <= reference <= high + 0.006
            and claim.ci_low - 0.006 <= reference <= claim.ci_high + 0.006
        ):
            failures.append(
                "The interval bounds are fractions but the unit declares percent; "
                "value and interval must share one scale."
            )
        elif has_counts:
            interval = wilson(claim.k, claim.n)
            wilson_low, wilson_high = interval["low"], interval["high"]
            if (
                wilson_low is not None
                and wilson_high is not None
                and (abs(wilson_low - low) > 0.02 or abs(wilson_high - high) > 0.02)
            ):
                warnings.append("The reported interval differs from Wilson; the method may differ.")
    comparison = claim.comparison
    if (
        comparison is not None
        and comparison.test == "mcnemar_exact"
        and comparison.b is not None
        and comparison.c is not None
        and comparison.p_value is not None
    ):
        expected = mcnemar_exact(comparison.b, comparison.c)["p_value"]
        if expected is not None:
            error = abs(expected - comparison.p_value)
            if error > 0.002 and error > 0.15 * expected:
                warnings.append("The reported p-value differs from the exact McNemar test.")
    if claim.split == "test":
        warnings.append("This claim uses the test split; Eval Lab analyses are DEV-only.")
    if claim.in_sample:
        warnings.append("This result is in-sample, not independent evaluation.")
    return CheckResult(
        status="fail" if failures else "warn" if warnings else "pass",
        notes=failures + warnings,
    )


def _same_result(expected: object, reported: object) -> bool:
    if isinstance(expected, bool) or isinstance(reported, bool):
        return type(expected) is type(reported) and expected == reported
    if isinstance(expected, (int, float)) and isinstance(reported, (int, float)):
        return math.isfinite(reported) and math.isclose(expected, reported, rel_tol=1e-3)
    if isinstance(expected, dict) and isinstance(reported, dict):
        return expected.keys() == reported.keys() and all(
            _same_result(value, reported[key]) for key, value in expected.items()
        )
    if isinstance(expected, list) and isinstance(reported, list):
        return len(expected) == len(reported) and all(
            _same_result(left, right) for left, right in zip(expected, reported, strict=True)
        )
    return expected == reported


def _numeric_leaves(value: object) -> list[float | int]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [value]
    if isinstance(value, dict):
        return [number for item in value.values() for number in _numeric_leaves(item)]
    if isinstance(value, list):
        return [number for item in value for number in _numeric_leaves(item)]
    return []


_STAT_INPUTS: dict[str, tuple[str, ...]] = {
    "wilson": ("k", "n"),
    "fisher": ("a", "b", "c", "d"),
    "mcnemar": ("b", "c"),
    "two_proportion": ("k1", "n1", "k2", "n2"),
    "rule_of_three": ("n",),
    "bh": ("p_values",),
    "min_n_mcnemar": (),
}


def _allowed_values(value: float | int | None, unit: str | None) -> list[str | float | int]:
    """Forms in which a stored claim quantity supports numbers in later prose."""
    if value is None:
        return []
    normalized = normalize_unit(unit)
    kind = unit_kind(normalized)
    if kind == "percent":
        return [f"{value}%"]
    if kind == "duration" and " " not in normalized:
        return [value, f"{value}{normalized}"]
    return [value]


def _claim_numbers(claim: ExtractedClaim) -> list[str | float | int]:
    numbers: list[str | float | int] = [claim.quote]
    for count in (claim.k, claim.n):
        numbers.extend(_allowed_values(count, None))
    for value in (claim.value, claim.ci_low, claim.ci_high):
        numbers.extend(_allowed_values(value, claim.unit))
    if claim.comparison is not None:
        for value in (claim.comparison.p_value, claim.comparison.b, claim.comparison.c):
            numbers.extend(_allowed_values(value, None))
    return numbers


def _supported(number: float | int, allowed: list[str | float | int]) -> bool:
    return not unsupported_numbers(str(number), allowed)


def _stat_inputs_supported(
    op: str, args: dict[str, object], allowed: list[str | float | int],
) -> bool:
    """Every stat input must come from cited claims (a 2x2 table may use complements)."""
    if op == "fisher":
        cells = [args.get(name) for name in "abcd"]
        if not all(isinstance(cell, int) and not isinstance(cell, bool) for cell in cells):
            return False
        a, b, c, d = (int(str(cell)) for cell in cells)
        direct = all(_supported(cell, allowed) for cell in (a, b, c, d))
        margins = all(_supported(cell, allowed) for cell in (a, a + b, c, c + d))
        return direct or margins
    return all(
        _supported(number, allowed)
        for key in _STAT_INPUTS[op]
        for number in _numeric_leaves(args.get(key))
    )


def _card_prose(card: InsightCard) -> list[tuple[str, str]]:
    fields = [
        ("title", card.title),
        ("statement", card.statement),
        *(("observation", text) for text in card.observations),
        ("interpretation", card.interpretation),
        ("why_not_obvious", card.why_not_obvious),
        *(("alternative", text) for text in card.alternatives),
        *(("evidence note", evidence.note) for evidence in card.evidence if evidence.note),
    ]
    if card.challenge is not None:
        fields.extend([
            ("challenge check", card.challenge.check),
            ("challenge decision", card.challenge.decision_at_risk),
        ])
    for prediction in card.predictions:
        fields.extend([
            ("prediction", prediction.statement),
            ("prediction resolution", prediction.resolves_when),
        ])
        fields.extend(
            ("prediction metadata", text)
            for text in (
                prediction.metric, prediction.subject, prediction.dataset, prediction.condition,
            ) if text is not None
        )
    proposal = card.proposed_experiment
    if proposal is not None:
        fields.extend([
            ("proposal", proposal.hypothesis),
            ("proposal variable", proposal.primary_variable),
            *(("proposal fixed variable", text) for text in proposal.fixed_variables),
            *(("proposal arm", text) for text in proposal.arms),
            ("proposal cost basis", proposal.cost_basis),
            ("proposal expectation", proposal.expected_if_true),
            ("proposal expectation", proposal.expected_if_false),
            *(("proposal stop condition", text) for text in proposal.stop_conditions),
        ])
    return fields


def check_card(
    card: InsightCard,
    *,
    claims: Mapping[str, ClaimRecord],
    known_insights: set[str],
    sources: Mapping[str, SourceRecord] | None = None,
    section_texts: Mapping[str, str] | None = None,
) -> CheckResult:
    failures: list[str] = []
    warnings: list[str] = []
    evidence_ids = {evidence.claim_id for evidence in card.evidence}
    referenced = set(evidence_ids)
    if card.challenge is not None:
        referenced.update(card.challenge.target_claim_ids)
    claim_numbers: list[str | float | int] = []
    section_numbers: list[str | float | int] = []
    source_folders: set[str] = set()
    for claim_id in sorted(referenced):
        record = claims.get(claim_id)
        if record is None:
            failures.append(f"Unknown claim id: {claim_id}.")
            continue
        if record.stale:
            failures.append(f"Claim {claim_id} is stale.")
        if record.check.status == "fail":
            failures.append(f"Claim {claim_id} failed its evidence check.")
        if record.recompute is not None and record.recompute.status == "differs":
            failures.append(f"Claim {claim_id} differs from its receipt.")
        elif record.recompute is not None and record.recompute.status == "partial":
            warnings.append(
                f"Claim {claim_id} is only partly confirmed by its receipt: "
                f"{describe_recompute(record.recompute)}."
            )
        claim = record.claim
        claim_numbers.extend(_claim_numbers(claim))
        if section_texts is not None and claim.section_id in section_texts:
            section_numbers.append(section_texts[claim.section_id])
        if claim_id in evidence_ids:
            source = sources.get(record.source_id) if sources is not None else None
            folder = source.folder if source is not None else None
            # Claims from a log with no results folder (FINDINGS) count by their experiment.
            source_folders.add(folder or f"{record.source_id}:{claim.experiment}")
    if card.kind == "connection" and len(source_folders) < 2:
        failures.append("A connection needs evidence from at least two distinct source folders.")
    derived: list[str | float | int] = []
    for index, statistic in enumerate(card.computed, start=1):
        try:
            expected = compute(statistic.op, statistic.args)
        except ValueError as exc:
            failures.append(f"Computed statistic {index} has invalid arguments: {exc}")
            continue
        result_matches = _same_result(expected, statistic.result)
        if not result_matches:
            failures.append(f"Computed statistic {index} does not match its recomputed result.")
        inputs_supported = _stat_inputs_supported(statistic.op, statistic.args, claim_numbers)
        if not inputs_supported:
            failures.append(
                f"Computed statistic {index} ({statistic.op}) uses inputs that no cited claim states."
            )
        if result_matches and inputs_supported:
            derived.extend(_numeric_leaves(statistic.args))
            derived.extend(_numeric_leaves(expected))
    for index, checked in enumerate(card.recipes, start=1):
        try:
            expected_value = run_recipe(checked.recipe, Path(checked.folder))
        except (RecipeError, OSError, ValueError) as exc:
            failures.append(f"Recorded recipe {index} cannot be recomputed: {exc}")
            continue
        if not math.isclose(expected_value, checked.value, rel_tol=1e-9, abs_tol=1e-12):
            failures.append(f"Recorded recipe {index} does not match its recorded value.")
        else:
            derived.append(expected_value)
    for insight_id in card.builds_on:
        if insight_id not in known_insights:
            warnings.append(f"Unknown builds_on insight id: {insight_id}.")
    allowed = [*claim_numbers, *section_numbers, *derived]
    for field, prose in _card_prose(card):
        unsupported = unsupported_numbers(prose, allowed)
        if not unsupported:
            continue
        message = f"Numbers not supported by cited evidence in {field}: {', '.join(unsupported)}."
        (failures if field in {"title", "statement"} else warnings).append(message)
    return CheckResult(
        status="fail" if failures else "warn" if warnings else "pass",
        notes=failures + warnings,
    )

