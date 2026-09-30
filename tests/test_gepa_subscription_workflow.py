"""Subscription resume/count boundaries remain separate from paid target approval."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.gepa_optimizer.budget import AggregateBudget, BudgetExhausted
from evallab.gepa_optimizer.proposer import JournaledReflectionLM, ProposalUnavailable
from evallab.gepa_optimizer.workflow import load_campaign


class SubscriptionTransport:
    def __init__(self) -> None:
        self.calls = 0

    def preflight(self) -> dict:
        return {"transport": "codex", "model": "codex/gpt-6.1-sol"}

    def request(self, prompt, *, directory: Path) -> dict:
        self.calls += 1
        return {
            "response": "Read the local requirements; implement and verify one scoped change.",
            "upstream_estimated_cost_usd": None,
            "usage": {"input_tokens": 20, "output_tokens": 10},
            "transport": self.preflight(),
        }


def test_subscription_resume_reuses_response_and_counts_unknown_cost_requests(tmp_path):
    budget = AggregateBudget(
        tmp_path / "budget",
        max_target_attempts=1,
        max_proposer_requests=2,
        max_proposer_cost_usd=None,
    )
    transport = SubscriptionTransport()

    def proposer():
        return JournaledReflectionLM(
            model="codex/gpt-6.1-sol",
            directory=tmp_path / "proposer",
            max_requests=2,
            budgets=(budget,),
            transport=transport,
        )

    first = proposer()
    text = first("First observed failure")
    resumed = proposer()
    assert resumed("First observed failure") == text
    assert resumed.replayed == 1
    resumed("Different observed failure")
    with pytest.raises(ProposalUnavailable, match="ceiling"):
        proposer()("Third observed failure")
    assert transport.calls == 2
    accounting = budget.summary()
    assert accounting["actual_billing_cost_usd"] is None
    assert accounting["proposer"]["completed"] == 2
    assert accounting["proposer"]["complete_estimated_cost_usd"] is None


def test_subscription_ambiguous_failure_blocks_changed_feedback(tmp_path):
    class FailingTransport(SubscriptionTransport):
        def request(self, prompt, *, directory):
            self.calls += 1
            raise TimeoutError("Remote outcome unknown")

    transport = FailingTransport()
    budget = AggregateBudget(
        tmp_path / "budget",
        max_target_attempts=1,
        max_proposer_requests=2,
        max_proposer_cost_usd=None,
    )
    proposer = JournaledReflectionLM(
        model="codex/gpt-6.1-sol",
        directory=tmp_path / "proposer",
        max_requests=2,
        budgets=(budget,),
        transport=transport,
    )
    with pytest.raises(ProposalUnavailable, match="retained outcome"):
        proposer("Observed failure")
    with pytest.raises(ProposalUnavailable, match="not usable"):
        proposer("Changed feedback is not a retry authorization")
    with pytest.raises(BudgetExhausted, match="unknown or failed"):
        budget.reserve("proposer", "different invocation")
    assert transport.calls == 1
    assert budget.summary()["proposer"]["unsettled"] == 1


def test_codex_selector_cannot_silently_fall_back_to_api(tmp_path):
    proposer = JournaledReflectionLM(
        model="codex/gpt-6.1-sol", directory=tmp_path / "proposer", max_requests=1
    )
    with pytest.raises(ProposalUnavailable, match="no API fallback"):
        proposer("Revise the instructions")
    assert proposer.new_requests == 0


@pytest.mark.parametrize(
    "override,remove,reason",
    [
        ({"proposer_model": "zai/glm-5.3"}, None, "pinned codex"),
        ({"proposer_model": "codex/--oss"}, None, "pinned codex"),
        ({"engine": "omni"}, None, "requires GEPA"),
        ({"max_proposer_cost_usd": 0.2}, None, "requires max_proposer_cost_usd=null"),
        ({}, "max_proposer_cost_usd", "requires max_proposer_cost_usd=null"),
        ({}, "max_proposer_requests", "explicit proposer and target"),
        ({}, "max_target_attempts", "explicit proposer and target"),
    ],
)
def test_subscription_campaign_refuses_unbounded_or_mislabelled_route(
    tmp_path, override, remove, reason
):
    config = {
        "name": "subscription",
        "engine": "gepa",
        "agent": "nop",
        "seed_candidate_path": "seed.txt",
        "output_dir": "runs/subscription",
        "examples": [
            {
                "task_id": "dev",
                "task_path": "tasks/dev",
                "task_package_digest": "sha256:" + "a" * 64,
                "split": "development",
            }
        ],
        "max_evals": 3,
        "max_target_attempts": 3,
        "max_proposer_requests": 1,
        "proposer_model": "codex/gpt-6.1-sol",
        "proposer_transport": "codex",
        "max_proposer_cost_usd": None,
    }
    config.update(override)
    if remove is not None:
        del config[remove]
    path = tmp_path / "campaign.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match=reason):
        load_campaign(path, tmp_path)
