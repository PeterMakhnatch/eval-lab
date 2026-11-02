"""Shared parsing and normalization for quantities in claims and analyst prose.

One vocabulary for fraction vs percent vs count vs currency vs duration so that
claim checks, receipt recomputation and signals agree on what a stored number means.
Semantics match ``signals._fraction``: a percent unit divides by 100; a fraction unit
or no unit keeps values in [0, 1]; any other unit is not a proportion; and, only when
the source quote prints a percent sign, an out-of-range unitless value in (1, 100] is
read as a percent.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

UnitKind = Literal["fraction", "percent", "count", "currency", "duration", "ratio", "other"]

_PERCENT = {"%", "percent", "percentage", "pct", "pp"}
_FRACTION = {
    "1", "fraction", "proportion", "probability", "rate", "recall", "precision",
    "accuracy", "auroc",
}
_COUNT = {
    "b", "count", "counts", "item", "items", "case", "cases", "row", "rows", "call",
    "calls", "run", "runs", "trial", "trials", "task", "tasks", "package", "packages",
    "request", "requests", "execution", "executions", "review", "reviews", "drop", "drops",
    "k",
}
_CURRENCY = {"$", "usd", "us$", "dollar", "dollars"}
_DURATION = {
    "ns": 1e-9, "us": 1e-6, "µs": 1e-6, "μs": 1e-6, "ms": 1e-3,
    "s": 1.0, "sec": 1.0, "secs": 1.0, "second": 1.0, "seconds": 1.0,
    "m": 60.0, "min": 60.0, "mins": 60.0, "minute": 60.0, "minutes": 60.0,
    "h": 3600.0, "hr": 3600.0, "hrs": 3600.0, "hour": 3600.0, "hours": 3600.0,
}
_MULTIPLIER = {"k": 1e3, "b": 1e9}
_RATIO = {"x", "×", "times", "fold"}
_NUMBER = re.compile(
    r"^(?P<currency>[$])?\s*(?P<value>[-+]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)"
    r"(?:[eE][-+]?\d+)?)\s*(?P<unit>\\?%|pp|[A-Za-zµμ×]*)$"
)


def normalize_unit(unit: str | None) -> str:
    """Return a comparable unit spelling without changing its meaning."""
    if unit is None:
        return ""
    folded = unicodedata.normalize("NFKC", unit).strip().casefold()
    folded = folded.replace("\\%", "%").replace("percentage points", "pp")
    folded = folded.replace("percentage point", "pp")
    return re.sub(r"[\s_/-]+", " ", folded).strip()


def unit_kind(unit: str | None) -> UnitKind:
    normalized = normalize_unit(unit)
    if not normalized:
        return "other"
    if normalized in _PERCENT or normalized.startswith(("% ", "percent ", "percentage ")):
        return "percent"
    if normalized in _FRACTION:
        return "fraction"
    if normalized in _CURRENCY or normalized.startswith(("usd ", "$ ")):
        return "currency"
    if normalized in _DURATION:
        return "duration"
    if normalized in _RATIO:
        return "ratio"
    if normalized in _COUNT or normalized.endswith((" count", " counts", " cases", " rows")):
        return "count"
    return "other"


def as_fraction(
    value: float, unit: str | None, *, quote: str | None = None, interval: bool = False,
) -> float | None:
    """Convert a declared proportion to [0, 1]; ``None`` when it is not a proportion.

    ``quote`` enables the legacy fallback for an unlabeled percent: a value in (1, 100]
    is a percent when the quote prints ``%`` and the unit is absent (or, for an interval
    bound, a fraction unit that legacy extraction dropped from the whole pair).
    """
    if not math.isfinite(value):
        return None
    kind = unit_kind(unit)
    unitless = not normalize_unit(unit)
    if kind == "percent":
        fraction = value / 100
    elif (kind == "fraction" or unitless) and 0 <= value <= 1:
        fraction = value
    elif (
        (unitless or (interval and kind == "fraction"))
        and 1 < value <= 100
        and quote is not None
        and "%" in quote
    ):
        fraction = value / 100
    else:
        return None
    return fraction if 0 <= fraction <= 1 else None


def canonical_value(value: float, unit: str | None) -> float:
    """Convert to fractions, seconds, base counts, USD, or a unitless scalar."""
    normalized = normalize_unit(unit)
    kind = unit_kind(normalized)
    if kind == "percent":
        return value / 100
    if kind == "duration":
        return value * _DURATION[normalized]
    return value * _MULTIPLIER.get(normalized, 1.0)


@dataclass(frozen=True)
class Quantity:
    """A numeric token and the unit printed next to it."""

    text: str
    value: float
    unit: str
    kind: UnitKind

    @property
    def canonical(self) -> float:
        return canonical_value(self.value, self.unit)

    @property
    def fraction(self) -> float | None:
        return as_fraction(self.value, self.unit)


def parse_quantity(text: str) -> Quantity | None:
    """Parse forms such as ``36pp``, ``3x``, ``40ms``, ``$15`` and ``16.7\\%``."""
    match = _NUMBER.fullmatch(text.strip())
    if match is None:
        return None
    try:
        value = float(match.group("value").replace(",", ""))
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    normalized = normalize_unit(match.group("currency") or match.group("unit"))
    return Quantity(text.strip(), value, normalized, unit_kind(normalized))


def printed_tolerance(text: str, *, unit: str | None = None) -> float:
    """Half a unit in the last printed digit of ``text``, in canonical units.

    ``unit`` supplies the scale when ``text`` is a bare serialized number: ``29.3``
    with unit ``%`` is 0.0005 as a fraction; ``1.2e6`` is 50,000. Floors at 1e-9.
    """
    quantity = parse_quantity(text)
    match = _NUMBER.fullmatch(text.strip())
    if quantity is None or match is None:
        return 1e-9
    mantissa, _, exponent_text = match.group("value").replace(",", "").lower().partition("e")
    places = len(mantissa.split(".", 1)[1]) if "." in mantissa else 0
    half_unit = 0.5 * 10 ** (int(exponent_text or 0) - places)
    scale = canonical_value(1.0, quantity.unit or unit)
    return max(1e-9, half_unit * scale)
