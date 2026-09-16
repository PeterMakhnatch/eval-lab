"""Behavioural checks for the RLM policy catalog and the synthetic suite."""

from __future__ import annotations

import re

import pytest

from evallab.rlm.bench import generate_suite, normalize_answer, score
from evallab.rlm.bench.generators import MERCHANT_CATEGORY, MONTH_NAMES
from evallab.rlm.policies import (
    ENVIRONMENT_BRIDGE_ADDENDUM,
    POLICIES,
    RlmPolicy,
    policy_from_json,
    resolve_policy,
)


def test_stock_policy_matches_harbor_defaults_and_is_content_addressed() -> None:
    stock = resolve_policy("stock")
    assert (stock.max_iters, stock.max_llm_calls, stock.max_output_chars) == (20, 50, 10_000)
    assert stock.instruction_addendum == "" and stock.separate_sub_lm is False
    assert stock.digest() == policy_from_json(stock.to_json()).digest()
    assert stock.digest() != stock.derive("x", "one field changed", max_iters=21).digest()
    assert len({policy.digest() for policy in POLICIES.values()}) == len(POLICIES)


def test_unknown_policy_names_known_ids() -> None:
    with pytest.raises(ValueError, match="stock"):
        resolve_policy("does-not-exist")


def test_bridge_policies_only_carry_environment_guidance() -> None:
    assert resolve_policy("bridge").environment_addendum == ENVIRONMENT_BRIDGE_ADDENDUM
    assert resolve_policy("orchestrator").environment_addendum == ""
    assert resolve_policy("tools-bridge").container_python_tool is True
    assert isinstance(RlmPolicy(policy_id="p", description="d", source="s"), RlmPolicy)


def test_suite_is_deterministic_per_seed_and_sized() -> None:
    first = generate_suite(3, 2, 50_000)
    second = generate_suite(3, 2, 50_000)
    assert [(t.task_id, t.context, t.query, t.answer) for t in first] == [
        (t.task_id, t.context, t.query, t.answer) for t in second
    ]
    assert generate_suite(4, 2, 50_000)[0].context != first[0].context
    assert [t.family for t in first] == ["ledger-agg"] * 2 + ["chain-lookup"] * 2 + [
        "state-tracking"
    ] * 2
    for task in first:
        assert 0.8 * 50_000 <= len(task.context) <= 1.2 * 50_000, (task.task_id, len(task.context))
        assert "ground_truth" in task.meta and "difficulty" in task.meta


def _replay_state(context: str) -> dict[str, int]:
    state: dict[str, int] = {}
    snapshot: dict[str, int] | None = None
    for line in context.splitlines():
        parts = line.split()
        if not parts or line.startswith(("#", "[")):
            continue
        op = parts[0]
        if op == "BEGIN":
            snapshot = dict(state)
        elif op == "COMMIT":
            snapshot = None
        elif op == "ROLLBACK":
            assert snapshot is not None
            state, snapshot = dict(snapshot), None
        elif op == "SET":
            state[parts[1]] = int(parts[2])
        elif op == "INC":
            state[parts[1]] = state.get(parts[1], 0) + int(parts[2])
        elif op == "DEL":
            state.pop(parts[1], None)
        elif op == "RENAME" and parts[1] in state:
            state[parts[2]] = state.pop(parts[1])
    return state


def test_answers_are_reproducible_from_the_rendered_context_and_ground_truth() -> None:
    for task in generate_suite(5, 4, 60_000):
        truth = task.meta["ground_truth"]
        if task.family == "state-tracking":
            state = _replay_state(task.context)
            assert state == truth["final_state"]
            if task.meta["kind"] == "final-value":
                key = re.search(r"key (sku-\S+)\?", task.query).group(1)
                assert score(task, str(state[key])) == 1.0
            else:
                threshold = int(re.search(r"greater than (\d+)", task.query).group(1))
                assert score(task, str(sum(1 for v in state.values() if v > threshold))) == 1.0
        elif task.family == "ledger-agg":
            records = truth["records"]
            if task.meta["kind"] == "account-category-total":
                account = re.search(r"account (ACC-\d+)", task.query).group(1)
                total = sum(
                    r["amount"]
                    for r in records
                    if r["account"] == account
                    and r["amount"] > 0
                    and MERCHANT_CATEGORY[r["merchant"]] == task.meta["category"]
                )
                assert score(task, f"${total:,.2f}") == 1.0
            elif task.meta["kind"] == "account-month-total":
                account = re.search(r"account (ACC-\d+)", task.query).group(1)
                month_name = re.search(r"in (\w+ 2026)", task.query).group(1)
                month = next(m for m, n in MONTH_NAMES.items() if n == month_name)
                total = sum(
                    r["amount"]
                    for r in records
                    if r["account"] == account and r["month"] == month and r["amount"] > 0
                )
                assert score(task, f"{total:.2f}") == 1.0
        else:
            people = {p["emp_id"]: p for p in truth["people"]}
            invoices = {i["invoice"]: i for i in truth["invoices"]}
            invoice = re.search(r"invoice (INV-\d+)", task.query).group(1)
            manager = people[people[invoices[invoice]["approver"]]["manager"]]
            if task.meta["kind"] == "manager-id":
                assert score(task, manager["emp_id"]) == 1.0
            elif task.meta["kind"] == "office-of-manager":
                moves = truth["relocations"].get(manager["emp_id"])
                office = max(moves)[1] if moves else manager["office"]
                assert score(task, f"The {office} office.") == 1.0
            else:
                assert score(task, people[manager["manager"]]["dept"].upper()) == 1.0


@pytest.mark.parametrize(
    ("answer", "prediction", "expected"),
    [
        ("1234.50", "```\n$1,234.50\n```", 1.0),
        ("1234.50", "1234.5 USD", 1.0),
        ("1234.50", "1240.00", 1.0),  # within 0.5 %
        ("1234.50", "1250.00", 0.0),
        ("0", "0.004", 1.0),  # absolute tolerance floor
        ("ACC-5021", "acc-5021.", 1.0),
        ("Berlin", "Berlin office", 1.0),
        ("Berlin", "Lisbon", 0.0),
        ("14", "", 0.0),
    ],
)
def test_scoring_normalises_formats_without_accepting_wrong_values(
    answer: str, prediction: str, expected: float
) -> None:
    task = generate_suite(1, 1, 20_000)[0]
    task = type(task)(task.task_id, task.family, task.context, task.query, answer, task.meta)
    assert score(task, prediction) == expected
    assert normalize_answer("  'X'  ") == "x"
