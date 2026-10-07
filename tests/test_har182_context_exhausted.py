"""HAR-182: context overflow is agent-side, never infra.

A served-context refusal ends the rollout as ContextExhausted with stop
reason ``context_exhausted`` so Harbor grades the final state. These tests
lock the downstream contract: counted (not excluded) verdicts, no watch
infra rules, no campaign replacement, and no infra-spike fence input.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from evallab.counts import classify_counts
from evallab.execution_contracts import MIMO_SELFHOSTED_CONTEXT_TOKENS
from evallab.mimoagent_trajectory import native_to_atif
from evallab.mimoagent_worker import _is_context_length_error
from evallab.step_layers import limit_hit_summary, stop_category


def _error(status, text):
    return SimpleNamespace(
        status_code=status,
        response=SimpleNamespace(text=text),
        body=None,
        message=f"Error code: {status} - {text}",
    )


def test_sglang_exact_bodies_are_context_exhaustion():
    prompt_overflow = (
        "The input (262294 tokens) is longer than the model's context length (262144 tokens)."
    )
    assert _is_context_length_error(_error(400, prompt_overflow)) is True
    total_overflow = (
        "Requested token count exceeds the model's maximum context length of "
        "262144 tokens. You requested a total of 262608 tokens."
    )
    assert _is_context_length_error(_error(400, total_overflow)) is True


def test_equivalent_vendor_wordings_are_context_exhaustion():
    assert (
        _is_context_length_error(
            _error(
                400,
                "This model's maximum context length is 262144 tokens. However, "
                "you requested 262294 tokens.",
            )
        )
        is True
    )
    assert (
        _is_context_length_error(_error(400, "code: string_above_max_length; input exceeds limit"))
        is True
    )


def test_non_overflow_400_is_still_an_error():
    assert (
        _is_context_length_error(
            _error(400, "Invalid schema for tool 'bash': property 'command' is required.")
        )
        is False
    )
    assert _is_context_length_error(_error(400, "bad authentication")) is False
    assert _is_context_length_error(_error(401, "bad authentication")) is False


def test_non_400_failures_are_never_context_exhaustion():
    assert (
        _is_context_length_error(
            _error(500, "The input (262294 tokens) is longer than the model's context length")
        )
        is False
    )
    assert _is_context_length_error(_error(429, "rate limit exceeded")) is False
    assert _is_context_length_error(_error(400, "trial budget exhausted")) is False


def test_uninformative_400_needs_a_full_prefix_to_count():
    vague = _error(400, "request failed")
    assert MIMO_SELFHOSTED_CONTEXT_TOKENS == 262_144
    assert (
        _is_context_length_error(
            vague,
            last_prompt_tokens=MIMO_SELFHOSTED_CONTEXT_TOKENS,
            served_context_tokens=MIMO_SELFHOSTED_CONTEXT_TOKENS,
        )
        is True
    )
    assert (
        _is_context_length_error(
            vague,
            last_prompt_tokens=MIMO_SELFHOSTED_CONTEXT_TOKENS - 1,
            served_context_tokens=MIMO_SELFHOSTED_CONTEXT_TOKENS,
        )
        is False
    )
    assert (
        _is_context_length_error(
            vague,
            last_prompt_tokens=65_536,
            served_context_tokens=MIMO_SELFHOSTED_CONTEXT_TOKENS,
        )
        is False
    )
    assert _is_context_length_error(vague) is False


def test_graded_context_trial_is_counted_never_excluded():
    for reward, verdict in ((0.0, "counted_fail"), (1.0, "counted_pass")):
        counts = classify_counts(reward=reward, scored=True, exception=None)
        assert counts["verdict"] == verdict
        assert "infra" not in counts["reasons"]


def test_context_stop_is_agent_side_not_our_limit():
    assert stop_category("context_exhausted") == "context_exhausted"
    assert stop_category("context_exhausted") != "our_limit"
    assert stop_category("context_exhausted") != "error"
    summary = limit_hit_summary(["context_exhausted"] * 10)
    assert summary["limit_hit_trials"] == 0
    assert summary["setup_limited"] is False


def test_context_trial_is_not_infra_excluded_for_replacement():
    from evallab.campaign_approval import trial_is_infra_excluded

    trial_result: dict = {}
    assert trial_is_infra_excluded(trial_result, {"reward": 0.0}) is False


def test_context_trial_fires_no_watch_infra_rules(tmp_path: Path):
    from evallab.live_watch import (
        WatchThresholds,
        evaluate_alerts,
        evaluate_fleet_alerts,
        trial_signals,
    )

    trial_dir = tmp_path / "job" / "task__a1"
    agent = trial_dir / "agent"
    agent.mkdir(parents=True)
    (agent / "trajectory.json").write_text(
        json.dumps({"steps": [{"step_id": 1, "source": "agent", "message": "m"}]})
    )
    (trial_dir / "result.json").write_text(
        json.dumps(
            {
                "finished_at": "2026-10-06T19:41:20.206679Z",
                "verifier_result": {"rewards": {"reward": 0.0}},
                "agent_result": {
                    "metadata": {
                        "stop_reason": "context_exhausted",
                        "native_exit_status": "ContextExhausted",
                    }
                },
            }
        )
    )
    import time

    status = trial_signals(
        trial_dir.parent,
        trial_dir,
        thresholds=WatchThresholds(),
        from_config=False,
        now=time.time(),
    )
    assert status["state"] == "finished"
    assert status["exception_type"] is None
    rules = {alert["rule"] for alert in evaluate_alerts(status, thresholds=WatchThresholds())}
    assert "infra_error" not in rules
    fleet = evaluate_fleet_alerts([status], thresholds=WatchThresholds(), now=time.time())
    assert [alert["rule"] for alert in fleet if "spike" in alert["rule"]] == []


def test_no_spike_rule_names_context_exhaustion():
    from evallab.dispatch_guards import SPIKE_RULES

    assert frozenset({"proxy_error_spike", "infra_spike"}) == SPIKE_RULES


def test_atif_carries_context_exhaustion_without_infra_error():
    native = {
        "info": {
            "exit_status": "ContextExhausted",
            "stop_reason": "context_exhausted",
            "context_exhaustion": {
                "error_type": "ModelQueryError",
                "root_error_type": "BadRequestError",
                "last_response_status": 400,
            },
        },
        "trajs": {
            "main": {
                "messages": [
                    {"role": "user", "content": "Fix the bug."},
                    {"role": "assistant", "content": "Inspecting."},
                ],
                "tools": [],
            }
        },
    }
    atif = native_to_atif(native, [], trajectory_id="t", model_name="m")
    assert atif["extra"]["stop_reason"] == "context_exhausted"
    assert atif["extra"]["context_exhaustion"]["last_response_status"] == 400
    assert "infra_error" not in atif["extra"]
