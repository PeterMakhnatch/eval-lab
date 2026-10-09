"""Offline tests for evallab.vcheck_client (no network, no models, no Docker)."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from typing import Any
from unittest.mock import patch

import pytest

from evallab.vcheck_client import (
    Budget,
    BudgetApprovalError,
    BudgetExceeded,
    ChainTamperError,
    ModelCallError,
    chain_append,
    chat_completion,
    cost_usd,
    run_tool_loop,
    verify_chain,
)


def _keys(**overrides: str | None):
    env = {"ZAI_API_KEY": "sub-key", "ZAI_OPENAPI_API_KEY": "metered-key"}
    env.update(overrides)
    return lambda name: env.get(name)


def _chat_body(content: Any = "hi", tool_calls: list | None = None) -> dict:
    return {
        "choices": [{"message": {"content": content, "tool_calls": tool_calls or []}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
    }


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._raw = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(url: str, status: int, body: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, status, "err", {}, io.BytesIO(body.encode()))


def _patch_urlopen(side_effects: list):
    calls: list[str] = []

    def fake(request, timeout=None):
        calls.append(request.full_url)
        effect = side_effects[min(len(calls) - 1, len(side_effects) - 1)]
        if isinstance(effect, Exception):
            raise effect
        return _FakeResponse(effect)

    return patch.object(urllib.request, "urlopen", fake), calls


def test_budget_cap_enforced_and_spend_jsonl_written(tmp_path):
    budget = Budget(cap_usd=0.01, approval="tester manual $0.01")
    budget.require_approval()
    budget.charge(
        0.004,
        model="glm-5.3",
        input_tokens=1000,
        output_tokens=500,
        run_dir=tmp_path,
        endpoint="subscription",
    )
    assert budget.spent_usd == pytest.approx(0.004)
    assert budget.remaining_usd == pytest.approx(0.006)
    with pytest.raises(BudgetExceeded):
        budget.charge(0.01, model="glm-5.3", run_dir=tmp_path)
    assert budget.spent_usd == pytest.approx(0.004)  # failed charge mutates nothing
    lines = (tmp_path / "spend.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry == {
        "model": "glm-5.3",
        "endpoint": "subscription",
        "input_tokens": 1000,
        "output_tokens": 500,
        "usd": pytest.approx(0.004),
        "at": entry["at"],
    }


def test_budget_requires_approval():
    with pytest.raises(BudgetApprovalError):
        Budget(cap_usd=1.0).require_approval()
    with pytest.raises(BudgetApprovalError):
        Budget(cap_usd=1.0, approval="  ").require_approval()
    Budget(cap_usd=1.0, approval="tester manual $1").require_approval()


def test_cost_math():
    assert cost_usd("glm-5.3", 1_000_000, 1_000_000) == pytest.approx(5.80)
    assert cost_usd("glm-5.3-flash", 1_000_000, 1_000_000) == pytest.approx(0.65)
    with pytest.raises(ValueError):
        cost_usd("nope-1", 10, 10)


def test_subscription_success_no_fallback():
    patcher, calls = _patch_urlopen([_chat_body("ok")])
    with patcher:
        out = chat_completion("glm-5.3", [{"role": "user", "content": "hi"}], key_provider=_keys())
    assert out["endpoint"] == "subscription"
    assert out["content"] == "ok"
    assert out["usage"] == {"input_tokens": 1000, "output_tokens": 500}
    assert len(calls) == 1
    assert calls[0].startswith("https://api.z.ai/api/coding/paas/v4/chat/completions")


def test_fallback_on_401_uses_metered():
    patcher, calls = _patch_urlopen(
        [_http_error("u", 401, "unauthorized"), _chat_body("via-metered")]
    )
    with patcher:
        out = chat_completion("glm-5.3", [{"role": "user", "content": "hi"}], key_provider=_keys())
    assert out["endpoint"] == "metered"
    assert out["content"] == "via-metered"
    assert calls[1].startswith("https://api.z.ai/api/paas/v4/chat/completions")


def test_fallback_on_expiry_429_but_not_plain_429():
    patcher, calls = _patch_urlopen(
        [_http_error("u", 429, "subscription expired, renew"), _chat_body("recovered")]
    )
    with patcher:
        out = chat_completion("glm-5.3", [{"role": "user", "content": "hi"}], key_provider=_keys())
    assert out["endpoint"] == "metered"
    assert len(calls) == 2

    patcher2, _ = _patch_urlopen([_http_error("u", 429, "rate limit, slow down")])
    with patcher2, pytest.raises(ModelCallError):
        chat_completion("glm-5.3", [{"role": "user", "content": "hi"}], key_provider=_keys())


def test_metered_only_when_no_subscription_key():
    patcher, calls = _patch_urlopen([_chat_body("m")])
    with patcher:
        out = chat_completion(
            "glm-5.3-flash",
            [{"role": "user", "content": "hi"}],
            key_provider=_keys(**{"ZAI_API_KEY": None}),
        )
    assert out["endpoint"] == "metered"
    assert calls[0].startswith("https://api.z.ai/api/paas/v4/chat/completions")


def test_chat_charges_budget_and_appends_records(tmp_path):
    budget = Budget(cap_usd=10.0, approval="tester manual $10")
    transcript: list[dict] = []
    patcher, _ = _patch_urlopen([_chat_body("done")])
    with patcher:
        out = chat_completion(
            "glm-5.3",
            [{"role": "user", "content": "hi"}],
            budget=budget,
            run_dir=tmp_path,
            key_provider=_keys(),
            records=transcript,
        )
    assert budget.spent_usd == pytest.approx(cost_usd("glm-5.3", 1000, 500))
    assert [r["kind"] for r in transcript] == ["request", "response"]
    verify_chain(transcript)
    assert out["records"] == transcript
    # Spend identity is attributed: the ledger records which route was billed.
    ledger = json.loads((tmp_path / "spend.jsonl").read_text().strip())
    assert ledger["endpoint"] == "subscription"
    # Keys never leak into trajectory records.
    blob = json.dumps(transcript)
    assert "sub-key" not in blob and "metered-key" not in blob


def test_hash_chain_tamper_detected():
    records: list[dict] = []
    chain_append(records, "request", {"q": 1})
    chain_append(records, "response", {"a": 2})
    verify_chain(records)
    records[0]["data"]["q"] = 999
    with pytest.raises(ChainTamperError):
        verify_chain(records)


def _tool_call_body(call_id: str = "call-1") -> dict:
    return _chat_body(
        None,
        [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "add", "arguments": json.dumps({"x": 2})},
            }
        ],
    )


def test_tool_loop_executes_and_returns_transcript():
    seen: list[dict] = []

    def add(args: dict) -> int:
        seen.append(args)
        return args["x"] + 1

    schema = {"name": "add", "description": "add one", "parameters": {"type": "object"}}
    patcher, _ = _patch_urlopen([_tool_call_body(), _chat_body("final: 3")])
    with patcher:
        transcript, results = run_tool_loop(
            "glm-5.3",
            [{"role": "user", "content": "add 2"}],
            tools={"add": (schema, add)},
            key_provider=_keys(),
            max_steps=5,
        )
    assert seen == [{"x": 2}]
    assert results == [{"name": "add", "arguments": {"x": 2}, "result": 3}]
    assert [r["kind"] for r in transcript] == ["request", "response", "note", "request", "response"]
    verify_chain(transcript)


def test_tool_loop_stops_at_max_steps():
    calls = {"n": 0}

    def add(args: dict) -> int:
        calls["n"] += 1
        return 1

    schema = {"name": "add", "description": "add", "parameters": {"type": "object"}}
    patcher, _ = _patch_urlopen([_tool_call_body(f"call-{i}") for i in range(10)])
    with patcher:
        transcript, results = run_tool_loop(
            "glm-5.3",
            [{"role": "user", "content": "loop"}],
            tools={"add": (schema, add)},
            key_provider=_keys(),
            max_steps=3,
        )
    assert calls["n"] == 3
    assert len(results) == 3
    assert len(transcript) == 3 * 3  # request + response + note per step
    verify_chain(transcript)


def test_tool_loop_unknown_tool_is_error_result_not_crash():
    body = _chat_body(
        None,
        [
            {
                "id": "c-9",
                "type": "function",
                "function": {"name": "ghost", "arguments": "{}"},
            }
        ],
    )
    patcher, _ = _patch_urlopen([body, _chat_body("recovered")])
    with patcher:
        _, results = run_tool_loop(
            "glm-5.3",
            [{"role": "user", "content": "hi"}],
            tools={},
            key_provider=_keys(),
            max_steps=5,
        )
    assert results[0]["name"] == "ghost"
    assert "error" in results[0]["result"]
