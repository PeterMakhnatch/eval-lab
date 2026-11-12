from __future__ import annotations

import pytest

from evallab.analysis_statistics import compute_wilson_interval, normal_quantile


def test_arbitrary_confidence_levels_and_normal_quantile() -> None:
    # Check Acklam normal quantile at standard points
    assert pytest.approx(normal_quantile(0.975), abs=1e-6) == 1.959963984540054
    assert pytest.approx(normal_quantile(0.95), abs=1e-5) == 1.644853
    assert pytest.approx(normal_quantile(0.995), abs=1e-5) == 2.575829

    with pytest.raises(ValueError):
        normal_quantile(0.0)
    with pytest.raises(ValueError):
        normal_quantile(1.0)

    # Test compute_wilson_interval helper
    cw = compute_wilson_interval(successes=10, denominator=20, confidence_level=0.95)
    assert cw is not None
    assert compute_wilson_interval(0, 0) is None

    # Test Wilson interval with different confidence levels
    w90 = compute_wilson_interval(successes=10, denominator=20, confidence_level=0.90)
    w95 = compute_wilson_interval(successes=10, denominator=20, confidence_level=0.95)
    w99 = compute_wilson_interval(successes=10, denominator=20, confidence_level=0.99)

    assert w90 is not None and w95 is not None and w99 is not None
    # Higher confidence level produces wider intervals
    assert (w90[1] - w90[0]) < (w95[1] - w95[0]) < (w99[1] - w99[0])
