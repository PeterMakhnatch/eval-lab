"""Deterministic Wilson intervals for the retained TB4 audit scorer."""

from __future__ import annotations

import math


def normal_quantile(p: float) -> float:
    """Compute the inverse standard normal cumulative distribution function.

    Uses Peter J. Acklam's rational approximation algorithm (precision ~1.15e-9 or better).
    """
    if not 0.0 < p < 1.0:
        raise ValueError(f"probability p must be strictly between 0 and 1, got {p}")

    a = (
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    )
    b = (
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    )
    c = (
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    )
    d = (
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    )

    p_low = 0.02425
    p_high = 1.0 - p_low

    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (
            (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
            * q
            / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
        )
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(
        (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5])
        / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    )


def compute_wilson_interval(
    successes: int,
    denominator: int,
    confidence_level: float = 0.95,
) -> tuple[float, float] | None:
    if not 0.0 < confidence_level < 1.0:
        raise ValueError(f"confidence_level must be between 0 and 1, got {confidence_level}")
    if denominator <= 0:
        return None
    z = normal_quantile(1.0 - (1.0 - confidence_level) / 2.0)
    proportion = successes / denominator
    z_squared = z * z
    scale = 1.0 + z_squared / denominator
    center = (proportion + z_squared / (2.0 * denominator)) / scale
    half_width = (
        z
        * math.sqrt(
            proportion * (1.0 - proportion) / denominator
            + z_squared / (4.0 * denominator * denominator)
        )
        / scale
    )
    lower = max(0.0, center - half_width)
    upper = min(1.0, center + half_width)
    return lower, upper
