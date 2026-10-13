"""Monetary admission boundaries; no tokenizer, provider, or model calls."""
from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest

from evallab.tracing import TraceError

_SCRIPT = Path(__file__).resolve().parents[1] / "research/experiments/auditor-sft-v1/train.py"
_SPEC = importlib.util.spec_from_file_location("auditor_sft_budget_under_test", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_RECIPE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_RECIPE)


def test_one_token_over_hard_cap_is_refused_before_cent_rounding() -> None:
    available = _RECIPE.HARD_CAP - _RECIPE.EVALUATION_RESERVE - _RECIPE.STORAGE_RESERVE
    last_admissible = int(available * 1_000_000 / (_RECIPE.EPOCHS * _RECIPE.TRAIN_PRICE))
    plan = _RECIPE.budget_for_tokens(last_admissible)
    assert Decimal(str(plan["whole_job_upper_bound_usd"])) <= _RECIPE.HARD_CAP
    with pytest.raises(TraceError):
        _RECIPE.budget_for_tokens(last_admissible + 1)


def test_training_cannot_consume_the_evaluation_and_storage_allocations() -> None:
    tokens_affordable_if_training_were_the_only_cost = int(
        _RECIPE.HARD_CAP * 1_000_000 / (_RECIPE.EPOCHS * _RECIPE.TRAIN_PRICE)
    )
    with pytest.raises(TraceError):
        _RECIPE.budget_for_tokens(tokens_affordable_if_training_were_the_only_cost)


@pytest.mark.parametrize("tokens", [0, -1])
def test_empty_or_negative_training_cannot_be_authorized(tokens: int) -> None:
    with pytest.raises(TraceError):
        _RECIPE.budget_for_tokens(tokens)
