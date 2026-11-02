from __future__ import annotations

import pytest

from evallab.insights.units import (
    as_fraction,
    canonical_value,
    normalize_unit,
    parse_quantity,
    printed_tolerance,
    unit_kind,
)


@pytest.mark.parametrize(
    ("unit", "kind"),
    [
        ("%", "percent"), ("percent", "percent"), ("\\%", "percent"), ("pp", "percent"),
        ("percentage points", "percent"), ("% per worker-hour", "percent"),
        ("fraction", "fraction"), ("proportion", "fraction"), ("rate", "fraction"),
        ("USD", "currency"), ("$", "currency"), ("USD per run", "currency"),
        ("s", "duration"), ("ms", "duration"), ("Seconds", "duration"),
        ("x", "ratio"), ("calls", "count"), ("cases", "count"), ("k", "count"),
        ("tokens per case", "other"), (None, "other"), ("", "other"),
    ],
)
def test_unit_kind(unit: str | None, kind: str) -> None:
    assert unit_kind(unit) == kind


@pytest.mark.parametrize(
    ("text", "value", "unit", "kind"),
    [
        ("36pp", 36.0, "pp", "percent"),
        ("3x", 3.0, "x", "ratio"),
        ("40ms", 40.0, "ms", "duration"),
        ("$15", 15.0, "$", "currency"),
        ("16.7\\%", 16.7, "%", "percent"),
        ("93,600s", 93600.0, "s", "duration"),
        ("29.3%", 29.3, "%", "percent"),
        ("10k", 10.0, "k", "count"),
        ("1e-6", 1e-6, "", "other"),
        (".243", 0.243, "", "other"),
    ],
)
def test_parse_quantity_splits_numbers_from_glued_units(
    text: str, value: float, unit: str, kind: str,
) -> None:
    quantity = parse_quantity(text)
    assert quantity is not None
    assert (quantity.value, quantity.unit, quantity.kind) == (value, unit, kind)


@pytest.mark.parametrize("text", ["K7B", "gpt-6.1", "abc", "", "3 4", "$"])
def test_parse_quantity_refuses_identifiers_and_non_numbers(text: str) -> None:
    assert parse_quantity(text) is None


def test_canonical_values_use_fractions_seconds_and_base_counts() -> None:
    assert canonical_value(64.3, "%") == pytest.approx(0.643)
    assert canonical_value(40, "ms") == pytest.approx(0.04)
    assert canonical_value(2, "min") == 120
    assert canonical_value(10, "k") == 10_000
    assert canonical_value(15, "USD") == 15
    assert canonical_value(0.75, None) == 0.75
    quantity = parse_quantity("40ms")
    assert quantity is not None and quantity.canonical == pytest.approx(0.04)


def test_as_fraction_matches_the_signal_rules() -> None:
    assert as_fraction(60.0, "%") == pytest.approx(0.6)
    assert as_fraction(0.6, "fraction") == 0.6
    assert as_fraction(0.6, None) == 0.6
    assert as_fraction(1.0, "calls") is None  # a declared non-proportion unit
    assert as_fraction(1.3, "USD") is None
    assert as_fraction(150, "%") is None  # out of range after conversion
    assert as_fraction(42.3, None) is None  # a unitless percent needs evidence
    assert as_fraction(42.3, None, quote="CI 42.3-57.7%") == pytest.approx(0.423)
    assert as_fraction(42.3, "fraction", quote="CI 42.3-57.7%") is None
    assert as_fraction(42.3, "fraction", quote="CI 42.3-57.7%", interval=True) == (
        pytest.approx(0.423)
    )
    assert as_fraction(float("nan"), "%") is None


@pytest.mark.parametrize(
    ("text", "unit", "tolerance"),
    [
        ("29.3%", None, 0.0005),
        ("29%", None, 0.005),
        ("29.3", "%", 0.0005),
        ("0.293", None, 0.0005),
        ("3.2", "s", 0.05),
        ("40ms", None, 0.0005),
        ("1,234,567", None, 0.5),
        ("1.2e6", None, 50_000),
        ("$15", None, 0.5),
        ("0.30000000000000004", None, 1e-9),
    ],
)
def test_printed_tolerance_is_half_a_unit_of_the_last_digit(
    text: str, unit: str | None, tolerance: float,
) -> None:
    assert printed_tolerance(text, unit=unit) == pytest.approx(tolerance)


def test_normalize_unit_is_stable() -> None:
    assert normalize_unit("  Percentage Points ") == "pp"
    assert normalize_unit("\\%") == "%"
    assert normalize_unit("% per worker-hour") == "% per worker hour"
    assert normalize_unit(None) == ""
