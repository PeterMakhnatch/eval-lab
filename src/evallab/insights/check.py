"""Deterministic evidence, arithmetic and unsupported-number checks."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from evallab.insights.models import (
    CheckResult,
    ClaimRecord,
    ExtractedClaim,
    InsightCard,
    normalize_text,
)
from evallab.insights.stats import compute, mcnemar_exact, wilson

_UNSIGNED = r"(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?"
_NUMBER_BODY = rf"[-+]?{_UNSIGNED}"
# A sign right after a percent sign or closing bracket is a range dash ("43.6%-97.0%").
_NUMBER = re.compile(rf"(?<![\w.])((?:(?<![%)\]])[-+])?{_UNSIGNED})([ \t]*%)?(?!\w|\.\d)")
_IDENTIFIER = re.compile(r"(?<![\w.])(?=[\w.-]*[A-Za-z])\w+(?:[.-]\w+)*(?!\w)")
_EXEMPT = re.compile(r"(?<!\w)#\d+\b|\b\d{1,2}:\d{2}(?::\d{2})?\b|(?m:^\s*\d+[.)]\s+)")


@dataclass(frozen=True)
class _Number:
    text: str
    value: float
    percent: bool

    @property
    def normalized(self) -> float:
        return self.value / 100 if self.percent else self.value


def _tokens(text: str, *, ignore_context: bool = True) -> list[_Number]:
    text = "\n".join(normalize_text(line) for line in text.splitlines())
    if ignore_context:
        text = _EXEMPT.sub(lambda match: " " * len(match.group()), text)

    def mask_identifier(match: re.Match[str]) -> str:
        token = match.group()
        # Scientific notation is a number, not an identifier such as K7B.
        return token if re.fullmatch(_NUMBER_BODY, token) else " " * len(token)

    text = _IDENTIFIER.sub(mask_identifier, text)
    result = []
    for match in _NUMBER.finditer(text):
        raw = match.group(1)
        value = float(raw.replace(",", ""))
        percent = match.group(2) is not None
        if not math.isfinite(value):
            continue
        if ignore_context and not percent and 2020 <= value <= 2030 and value.is_integer():
            continue
        result.append(_Number(match.group().strip(), value, percent))
    return result


def _same_number(left: _Number, right: _Number) -> bool:
    if left.percent or right.percent or (abs(left.value) <= 1 and abs(right.value) <= 1):
        return abs(left.normalized - right.normalized) <= 0.0051 + 1e-12
    return math.isclose(left.value, right.value, rel_tol=1e-9, abs_tol=1e-12)


def unsupported_numbers(text: str, allowed: Iterable[str | float | int]) -> list[str]:
    """Find unsupported numeric tokens, ignoring labels, PRs, times and years.

    A fraction supplies two numeric tokens. Percentages match their proportion
    form, allowing half a percentage point of printed rounding.
    """
    supported: list[_Number] = []
    for value in allowed:
        if isinstance(value, str):
            supported.extend(_tokens(value, ignore_context=False))
        elif not isinstance(value, bool) and math.isfinite(value):
            supported.append(_Number(str(value), float(value), False))
    result = []
    for token in _tokens(text):
        if token.text not in result and not any(
            _same_number(token, candidate) for candidate in supported
        ):
            result.append(token.text)
    return result


def _value_quoted(value: float, numbers: list[_Number]) -> bool:
    for number in numbers:
        if number.percent:
            raw = number.text.rstrip("% ").replace(",", "")
            places = len(raw.split(".", 1)[1]) if "." in raw else 0
            if places <= 2 and math.isclose(round(value * 100, places), number.value, abs_tol=1e-9):
                return True
        elif math.isclose(value, number.value, rel_tol=1e-9, abs_tol=1e-12):
            return True
    return False


def check_extracted_claim(claim: ExtractedClaim, section_text: str) -> CheckResult:
    failures: list[str] = []
    warnings: list[str] = []
    if normalize_text(claim.quote) not in normalize_text(section_text):
        failures.append("The quote is not present in the source section.")
    numbers = _tokens(claim.quote, ignore_context=False)
    for label, count in (("k", claim.k), ("n", claim.n)):
        if count is not None and not any(number.value == count for number in numbers):
            failures.append(f"The quote does not contain {label} = {count}.")
    proportion = claim.k / claim.n if claim.k is not None and claim.n else None
    if claim.value is not None:
        agrees = proportion is not None and abs(claim.value - proportion) <= 0.006 + 1e-12
        if not _value_quoted(claim.value, numbers) and not agrees:
            warnings.append("The value is not in the quote and is not supported by k/n.")
        if proportion is not None and not agrees:
            warnings.append("The stated value differs from k/n by more than 0.006.")
    if claim.k is not None and claim.n and claim.ci_low is not None and claim.ci_high is not None:
        interval = wilson(claim.k, claim.n)
        low, high = interval["low"], interval["high"]
        if (
            low is not None
            and high is not None
            and (abs(low - claim.ci_low) > 0.02 or abs(high - claim.ci_high) > 0.02)
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


def check_card(
    card: InsightCard, *, claims: Mapping[str, ClaimRecord], known_insights: set[str]
) -> CheckResult:
    failures: list[str] = []
    warnings: list[str] = []
    evidence_ids = {evidence.claim_id for evidence in card.evidence}
    referenced = set(evidence_ids)
    if card.challenge is not None:
        referenced.update(card.challenge.target_claim_ids)
    allowed: list[str | float | int] = []
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
        claim = record.claim
        allowed.append(claim.quote)
        for value in (claim.k, claim.n, claim.value, claim.ci_low, claim.ci_high):
            if value is not None:
                allowed.append(value)
        if claim.comparison is not None:
            for value in (claim.comparison.p_value, claim.comparison.b, claim.comparison.c):
                if value is not None:
                    allowed.append(value)
    experiments = {claims[key].claim.experiment for key in evidence_ids if key in claims}
    if card.kind == "connection" and len(experiments) < 2:
        failures.append("A connection needs evidence from at least two distinct experiments.")
    for index, statistic in enumerate(card.computed, start=1):
        try:
            expected = compute(statistic.op, statistic.args)
        except ValueError as exc:
            failures.append(f"Computed statistic {index} has invalid arguments: {exc}")
            continue
        if not _same_result(expected, statistic.result):
            failures.append(f"Computed statistic {index} does not match its recomputed result.")
        allowed.extend(_numeric_leaves(expected))
    for insight_id in card.builds_on:
        if insight_id not in known_insights:
            warnings.append(f"Unknown builds_on insight id: {insight_id}.")
    prose = "\n".join([card.statement, *card.observations, card.interpretation])
    unsupported = unsupported_numbers(prose, allowed)
    if unsupported:
        warnings.append(f"Numbers not supported by cited evidence: {', '.join(unsupported)}.")
    return CheckResult(
        status="fail" if failures else "warn" if warnings else "pass",
        notes=failures + warnings,
    )
