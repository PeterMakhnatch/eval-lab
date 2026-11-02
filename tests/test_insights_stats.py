from __future__ import annotations

import argparse
import json
import math
import time
from fractions import Fraction
from pathlib import Path

import pytest

from evallab.insights.commands.stat import _cmd, register
from evallab.insights.stats import (
    bh,
    cases_for_layer_value,
    compute,
    fisher,
    mcnemar_exact,
    min_n_mcnemar,
    rule_of_three,
    two_proportion,
    wilson,
)


def test_wilson_zero_events_and_empty_denominator() -> None:
    result = wilson(0, 48)
    assert result["p"] == 0
    assert result["low"] == pytest.approx(0, abs=1e-12)
    assert result["high"] == pytest.approx(0.0741001, abs=1e-6)
    assert wilson(0, 0) == {"p": None, "low": None, "high": None}
    assert wilson(48, 48)["low"] == pytest.approx(1 - 0.0741001, abs=1e-6)
    assert wilson(20, 40, confidence=0.99)["high"] > wilson(20, 40)["high"]


@pytest.mark.parametrize(
    ("b", "c", "p"),
    [(21, 0, 9.5367431640625e-7), (10, 0, 0.001953125), (6, 4, 0.75390625), (0, 0, 1)],
)
def test_exact_mcnemar_known_values(b: int, c: int, p: float) -> None:
    assert mcnemar_exact(b, c)["p_value"] == pytest.approx(p)
    assert mcnemar_exact(c, b)["p_value"] == pytest.approx(p)


def test_fisher_known_value_and_refusal() -> None:
    assert fisher(1, 9, 11, 3)["p_value"] == pytest.approx(0.0027594561852200836)
    assert fisher(0, 10, 0, 10)["p_value"] is None
    assert fisher(0, 0, 0, 0)["p_value"] is None


def test_two_proportion_newcombe_bounds_and_sign() -> None:
    forward = two_proportion(30, 50, 10, 50)
    reverse = two_proportion(10, 50, 30, 50)
    assert forward["diff"] == pytest.approx(0.4)
    assert forward["low"] == pytest.approx(0.21002, abs=1e-4)
    assert forward["high"] == pytest.approx(0.55173, abs=1e-4)
    assert reverse["diff"] == pytest.approx(-0.4)
    assert reverse["low"] == pytest.approx(-forward["high"])
    assert reverse["high"] == pytest.approx(-forward["low"])
    assert forward["p_value"] == reverse["p_value"]
    assert two_proportion(0, 0, 3, 10)["diff"] is None


def test_rule_of_three_reports_exact_and_approximate_bounds() -> None:
    result = rule_of_three(48)
    assert result["upper95"] == pytest.approx(1 - 0.05 ** (1 / 48))
    assert result["three_over_n"] == pytest.approx(3 / 48)
    assert rule_of_three(0)["upper95"] is None


def test_bh_step_up_is_monotone_and_preserves_input_order() -> None:
    values = [0.01, 0.04, 0.03, 0.002, 0.2]
    result = bh(values)
    assert result["adjusted"] == pytest.approx([0.025, 0.05, 0.05, 0.01, 0.2])
    assert result["rejected"] == [True, True, True, True, False]
    assert values == [0.01, 0.04, 0.03, 0.002, 0.2]
    assert bh([0.01, 0.01, 1.0])["adjusted"] == pytest.approx([0.015, 0.015, 1])
    assert bh([]) == {"adjusted": [], "rejected": []}


def test_minimum_discordance_uses_strict_alpha() -> None:
    assert min_n_mcnemar() == {"n": 6}
    assert min_n_mcnemar(0.03125) == {"n": 7}
    assert mcnemar_exact(5, 0)["p_value"] >= 0.05
    assert mcnemar_exact(6, 0)["p_value"] < 0.05


def test_layer_value_power_certain_rate_needs_minimum_discordance() -> None:
    minimum = min_n_mcnemar()["n"]
    assert cases_for_layer_value(1) == minimum
    assert cases_for_layer_value(1, max_n=minimum) == minimum
    assert cases_for_layer_value(1, max_n=minimum - 1) is None
    assert cases_for_layer_value(1, alpha=0.03125) == min_n_mcnemar(0.03125)["n"]


def test_layer_value_power_matches_brute_force_binomial() -> None:
    minimum = min_n_mcnemar()["n"]
    expected = next(
        n for n in range(minimum, 100)
        if sum(math.comb(n, k) for k in range(minimum, n + 1)) / 2**n >= 0.8
    )
    assert cases_for_layer_value(0.5) == expected
    assert cases_for_layer_value(0.5, max_n=expected) == expected
    assert cases_for_layer_value(0.5, max_n=expected - 1) is None


@pytest.mark.parametrize("rate", [0, -0.5, 1e-300])
def test_layer_value_power_unattainable_rate_returns_none(rate: float) -> None:
    assert cases_for_layer_value(rate) is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"unique_rate": True},
        {"unique_rate": "0.5"},
        {"unique_rate": float("nan")},
        {"unique_rate": float("inf")},
        {"unique_rate": 1.1},
        {"unique_rate": 0.5, "alpha": 0},
        {"unique_rate": 0.5, "alpha": 1},
        {"unique_rate": 0.5, "power": 0},
        {"unique_rate": 0.5, "power": 1},
        {"unique_rate": 0.5, "power": float("nan")},
        {"unique_rate": 0.5, "max_n": -1},
        {"unique_rate": 0.5, "max_n": True},
        {"unique_rate": 0.5, "max_n": 10.5},
    ],
)
def test_layer_value_power_refuses_invalid_parameters(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        cases_for_layer_value(**kwargs)


@pytest.mark.parametrize(
    ("operation", "args"),
    [
        ("wilson", {"k": 3, "n": 2}),
        ("wilson", {"k": True, "n": 2}),
        ("wilson", {"k": 1.0, "n": 2}),
        ("wilson", {"k": 1, "n": 2, "confidence": 1}),
        ("mcnemar", {"b": -1, "c": 0}),
        ("two_proportion", {"k1": 1, "n1": "2", "k2": 1, "n2": 2}),
        ("bh", {"p_values": [0.1, float("nan")]}),
        ("bh", {"p_values": [False]}),
        ("min_n_mcnemar", {"alpha": 0}),
        ("fisher", {"a": 1, "b": 2, "c": 3}),
        ("rule_of_three", {"n": 5, "extra": 1}),
        ("missing", {}),
    ],
)
def test_dispatcher_refuses_bad_arguments(operation: str, args: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        compute(operation, args)


def test_stat_command_prints_json_and_bad_input_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    parser = argparse.ArgumentParser()
    register(parser.add_subparsers())
    args = parser.parse_args(["stat", "mcnemar", "--args", '{"b": 10, "c": 0}'])
    assert _cmd(args, tmp_path) == 0
    assert json.loads(capsys.readouterr().out) == {"p_value": 0.001953125}
    for raw in ("{", "[]", '{"n": -1}'):
        assert _cmd(argparse.Namespace(op="rule_of_three", args=raw), tmp_path) == 2
        assert "Cannot compute statistic" in capsys.readouterr().err


def _exact_mcnemar(b: int, c: int) -> float:
    n, m = b + c, min(b, c)
    tail = sum(math.comb(n, i) for i in range(m + 1))
    return float(min(Fraction(1), Fraction(2 * tail, 2**n)))


@pytest.mark.parametrize(
    ("b", "c"),
    [(700, 900), (1200, 1150), (400, 700), (900, 700), (2000, 1900), (1001, 0)],
)
def test_mcnemar_log_space_matches_exact_integers_beyond_the_exact_cutoff(b: int, c: int) -> None:
    expected = _exact_mcnemar(b, c)
    assert mcnemar_exact(b, c)["p_value"] == pytest.approx(expected, rel=1e-9, abs=0)
    assert mcnemar_exact(c, b)["p_value"] == mcnemar_exact(b, c)["p_value"]


def test_mcnemar_is_fast_at_ten_thousand_discordant_pairs() -> None:
    started = time.perf_counter()
    equal = mcnemar_exact(10_000, 10_000)["p_value"]
    skewed = mcnemar_exact(10_000, 9_800)["p_value"]
    tiny = mcnemar_exact(20_000, 10_000)["p_value"]
    assert time.perf_counter() - started < 1.0
    assert equal == 1.0
    # Normal approximation: z = 200 / sqrt(19800) = 1.42, two-sided p about 0.157.
    assert skewed == pytest.approx(0.157, abs=0.002)
    assert tiny == 5e-324


def test_mcnemar_underflow_is_a_floor_not_zero() -> None:
    assert mcnemar_exact(5000, 0)["p_value"] == 5e-324
    p_value = mcnemar_exact(5000, 0)["p_value"]
    assert p_value is not None and p_value > 0


def test_bh_float_boundary_rejects_at_exactly_q() -> None:
    # 0.05 * 3 / 3 is one ulp above 0.05 in floats; step-up rejects all three at q = 0.05.
    result = bh([0.01, 0.01, 0.05])
    assert result["adjusted"] == [0.015, 0.015, 0.05]
    assert result["rejected"] == [True, True, True]
    assert bh([0.01, 0.01, 0.0501])["rejected"] == [True, True, False]
    assert bh([0.025, 0.025], q=0.05)["rejected"] == [True, True]
