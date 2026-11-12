"""Paired harness-gate planning in src/evallab/power.py.

Validation anchors:
- exp04-check's recorded ``gate_table``
  (research-context/reef/experiments/work/04-check/summary.json), computed by
  ``04_gate_aa.py``'s exact ``publish_probability`` at the tutorial grader
  pass rates 32/60, 34/60, 39/60 for [sieve], [fib], [csv];
- HAR-72's DeepSeek A/A at ceiling pass rates (0 publishes in 30 trials),
  which must come out of the calculator as a near-zero false-publish rate.
"""

from __future__ import annotations

import pytest

from evallab.power import (
    sign_test_p_value,
)

#: 04-check's per-task pass rates: [sieve] 32/60, [fib] 34/60, [csv] 39/60.
EXP04_CHECK_RATES = [32 / 60, 34 / 60, 39 / 60]

#: 04-check summary.json gate_table rows: (candidate label, rule, repeats, p_publish).
EXP04_CHECK_GATE_TABLE = [
    ("null (A/A)", "wins>losses", 1, 0.34),
    ("null (A/A)", "wins>losses", 5, 0.426),
    ("null (A/A)", "sign test p<0.05", 5, 0.019),
    ("null (A/A)", "sign test p<0.05", 10, 0.03),
    ("+0.2 per task", "wins>losses", 1, 0.543),
    ("+0.2 per task", "wins>losses", 5, 0.845),
    ("+0.2 per task", "sign test p<0.05", 5, 0.169),
    ("+0.2 per task", "sign test p<0.05", 10, 0.404),
    ("+0.4 per task", "wins>losses", 1, 0.761),
    ("+0.4 per task", "wins>losses", 5, 0.996),
    ("+0.4 per task", "sign test p<0.05", 5, 0.681),
    ("+0.4 per task", "sign test p<0.05", 10, 0.977),
]

#: HAR-72's DeepSeek A/A pooled pass rates (5 repeats x 3 tasks, 0/30 publishes).
HAR72_CEILING_RATES = [1.0, 0.98, 1.0]


def test_sign_test_p_value_matches_exact_binomial_tail() -> None:
    """The p-value is the exact upper binomial tail with ties dropped."""
    assert sign_test_p_value(0, 0) == 1.0
    assert sign_test_p_value(5, 0) == pytest.approx(1 / 32)
    assert sign_test_p_value(6, 0) == pytest.approx(1 / 64)
    assert sign_test_p_value(6, 4) == pytest.approx(386 / 1024)
    assert sign_test_p_value(3, 3) == pytest.approx(42 / 64)


def test_sign_test_p_value_rejects_invalid_counts() -> None:
    """Negative, boolean, and non-integer counts are refused."""
    with pytest.raises(ValueError):
        sign_test_p_value(-1, 0)
    with pytest.raises(ValueError):
        sign_test_p_value(0, 2.5)
    with pytest.raises(ValueError):
        sign_test_p_value(True, 0)


