"""Offline tests for the TT-4b Kimi arm: tool-free wire, fixed price/provider bounds, billing."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def load_arm(monkeypatch, arm, name):
    monkeypatch.syspath_prepend(str(HERE))
    monkeypatch.setenv("TT4_ARM", arm)
    spec = importlib.util.spec_from_file_location(name, HERE / "paid.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def kimi(monkeypatch):
    return load_arm(monkeypatch, "kimi-k3", "tt4_kimi_arm_test")


def request_body(paid):
    return {
        "model": paid.MODEL,
        "max_tokens": 4096,
        "stream": False,
        "reasoning": {"enabled": False},
        "provider": copy.deepcopy(paid.PROVIDER_POLICY),
        "plugins": [],
        "transforms": [],
        "messages": [{"role": "user", "content": "native prompt"}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "RewardHacking", "schema": {}},
        },
    }


def response_body(paid):
    return {
        "id": "gen-test-123",
        "model": paid.MODEL,
        "provider": "InferenceNet",
        "usage": {
            "prompt_tokens": 243,
            "completion_tokens": 38,
            "total_tokens": 281,
            "cost": 0.000468477,
            "is_byok": False,
            "completion_tokens_details": {"reasoning_tokens": 0},
        },
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": '{"quotes":[],"reason":"fixture only","score":5}',
                    "refusal": None,
                    "reasoning": None,
                },
            }
        ],
    }


def test_arm_is_pinned_third_party_kimi_not_the_byok_blocked_route(kimi):
    assert kimi.ARM == "kimi-k3" and kimi.MODEL == "moonshotai/kimi-k3"
    assert kimi.PROVIDER == "inference-net/fp4" and kimi.PROVIDER_NAME == "InferenceNet"
    assert "gemini" not in kimi.ROUTE and "moonshot" not in kimi.PROVIDER
    assert kimi.PROVIDER_POLICY["only"] == kimi.PROVIDER_POLICY["order"] == [kimi.PROVIDER]
    assert kimi.PROVIDER_POLICY["allow_fallbacks"] is False
    assert kimi.PROVIDER_POLICY["data_collection"] == "deny"
    assert kimi.INSPECT_MODEL == "tt4openrouter/moonshotai/kimi-k3"
    assert "kimi-k3" in kimi.CAMPAIGN.name and "gemini" not in kimi.CAMPAIGN.name


def test_request_bound_follows_the_body_byte_cap_and_fits_the_arm_cap(kimi):
    assert kimi.MAX_INPUT_TOKENS == kimi.MAX_BODY_BYTES == 700_000
    assert (
        Decimal("700000") * kimi.INPUT_PRICE + Decimal("4096") * kimi.OUTPUT_PRICE
        == kimi.THEORETICAL_BOUND
    )
    assert kimi.THEORETICAL_BOUND < kimi.REQUEST_BOUND
    assert Decimal("14.85") >= kimi.REQUEST_BOUND * 67  # 67 failed attempts still fit the cap


def test_default_arm_keeps_the_original_gemini_bounds(monkeypatch):
    gemini = load_arm(monkeypatch, "gemini", "tt4_gemini_arm_test")
    assert gemini.MAX_BODY_BYTES is None and Decimal("0.106496") == gemini.THEORETICAL_BOUND
    assert Decimal("0.11") == gemini.REQUEST_BOUND


def test_wire_body_needs_size_proof_and_never_any_tool_parameter(kimi):
    body = request_body(kimi)
    kimi.validate_request(body, 1000)
    with pytest.raises(kimi.TransportError, match="input-token bound"):
        kimi.validate_request(body)  # byte length missing
    with pytest.raises(kimi.TransportError, match="input-token bound"):
        kimi.validate_request(body, kimi.MAX_BODY_BYTES + 1)
    for field, value in (
        ("tools", []),
        ("tool_choice", "none"),
        ("tool_choice", "auto"),
        ("functions", []),
        ("function_call", "none"),
        ("parallel_tool_calls", False),
    ):
        changed = request_body(kimi)
        changed[field] = value
        with pytest.raises(kimi.TransportError):
            kimi.validate_request(changed, 1000)
    changed = request_body(kimi)
    changed["messages"].append({"role": "assistant", "content": "x", "tool_calls": [{"id": "1"}]})
    with pytest.raises(kimi.TransportError):
        kimi.validate_request(changed, 1000)


def test_provider_policy_cannot_drift(kimi):
    for change in (
        {"only": ["moonshotai"]},
        {"allow_fallbacks": True},
        {"data_collection": "allow"},
        {"max_price": {"prompt": 1.0, "completion": 11.0, "request": 0}},
    ):
        body = request_body(kimi)
        body["provider"].update(change)
        with pytest.raises(kimi.TransportError):
            kimi.validate_request(body, 1000)


def test_response_receipt_attests_served_model_provider_and_credit_billing(kimi):
    receipt, text = kimi.response_receipt(response_body(kimi))
    assert receipt["served_model"] == "moonshotai/kimi-k3"
    assert receipt["served_provider"] == "InferenceNet" and receipt["is_byok"] is False
    assert receipt["actual_usd"] == "0.000468477"
    assert "fixture only" not in json.dumps(receipt) and json.loads(text)["score"] == 5
    for path, value in (
        (("model",), "moonshotai/kimi-k3.1-pro"),
        (("provider",), "Moonshot AI"),
        (("usage", "is_byok"), True),
        (("usage", "completion_tokens_details", "reasoning_tokens"), 5),
    ):
        body = response_body(kimi)
        target = body
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value
        with pytest.raises(kimi.TransportError):
            kimi.response_receipt(body)
    body = response_body(kimi)
    body["choices"][0]["message"]["tool_calls"] = [{"id": "call_1"}]
    with pytest.raises(kimi.TransportError):
        kimi.response_receipt(body)


def test_catalog_must_match_the_pinned_endpoint_price_and_context(kimi):
    endpoint = {
        "tag": kimi.PROVIDER,
        "service_tier": None,
        "model_id": kimi.MODEL,
        "provider_name": "InferenceNet",
        "context_length": 1048576,
        "max_completion_tokens": 943718,
        "pricing": {"prompt": "0.000000239", "completion": "0.0000108"},
        "supported_parameters": [
            "structured_outputs",
            "response_format",
            "max_tokens",
            "reasoning",
        ],
    }
    kimi.validate_catalog({"data": {"endpoints": [endpoint]}})
    for change in (
        {"pricing": {"prompt": "0.0000005", "completion": "0.0000108"}},
        {"pricing": {"prompt": "0.000000239", "completion": "0.00002"}},
        {"provider_name": "Moonshot AI"},
        {"context_length": 262144},
    ):
        altered = copy.deepcopy(endpoint)
        altered.update(change)
        with pytest.raises(kimi.AdmissionError):
            kimi.validate_catalog({"data": {"endpoints": [altered]}})


def test_funding_gate_is_the_pinned_provider_policy_not_a_missing_google_receipt(kimi, tmp_path):
    admitted = kimi.funding_admission(tmp_path, "not-a-real-key")
    assert admitted["evidence_kind"].startswith("pinned-third-party-provider")
    assert not list(tmp_path.iterdir())


def test_shard_argument_parses_k_of_n_only(kimi):
    assert kimi.parse_shard("2/4") == (2, 4)
    for bad in ("4/4", "x/4", "1", "-1/4"):
        with pytest.raises(Exception, match="--shard"):
            kimi.parse_shard(bad)


MOMENT = datetime(2026, 10, 28, 10, 0, tzinfo=UTC)
OPAQUE = "run-" + "a" * 24
CASH = {"available_usd": "100", "fetched_at": MOMENT.isoformat(), "source": "fixture"}


def isolate_state(paid, monkeypatch, tmp_path):
    monkeypatch.setattr(paid, "CAMPAIGN", tmp_path / "campaign")
    monkeypatch.setattr(paid, "SPEND", tmp_path / "canonical.jsonl")
    monkeypatch.setattr(paid, "STATE", tmp_path / "state")


def test_rate_limit_rejection_is_bounded_not_terminal_and_retries_under_a_new_attempt_id(
    kimi, tmp_path, monkeypatch
):
    httpx2 = pytest.importorskip("httpx2")
    pytest.importorskip("metr_scanners")
    from openrouter_budget import Budget

    isolate_state(kimi, monkeypatch, tmp_path)
    answers = [429, 200]

    class Wire(httpx2.AsyncBaseTransport):
        calls = 0

        async def handle_async_request(self, request):
            self.calls += 1
            if answers.pop(0) == 429:
                return httpx2.Response(429, json={"error": {"code": 429}}, request=request)
            return httpx2.Response(200, json=response_body(kimi), request=request)

    wire = Wire()
    budget = Budget(tmp_path / "budget", "tt4", clock=lambda: MOMENT)
    transport = kimi.guarded_transport(
        kimi.CAMPAIGN,
        OPAQUE,
        "not-a-real-key",
        underlying=wire,
        budget=budget,
        cash_reader=lambda: CASH,
    )

    def send():
        request = httpx2.Request(
            "POST",
            kimi.BASE_URL + "/chat/completions",
            json=request_body(kimi),
            headers={"Authorization": "Bearer not-a-real-key"},
        )
        return transport.handle_async_request(request)

    async def invoke():
        with pytest.raises(kimi.TransportError, match="HTTP-status-429"):
            await send()
        assert not (kimi.CAMPAIGN / "terminal").exists()
        assert not (kimi.CAMPAIGN / "halt.json").exists()
        assert (await send()).status_code == 200
        with pytest.raises(kimi.AdmissionError, match="duplicate_attempt"):
            await send()  # an attested body is never sent twice
        await transport.aclose()

    asyncio.run(invoke())
    assert wire.calls == 2
    receipts = {path.name: kimi.load(path) for path in (kimi.CAMPAIGN / "calls").glob("*.json")}
    limited = [r for r in receipts.values() if r["status"] == "rate_limited"]
    attested = [r for r in receipts.values() if r["status"] == "attested"]
    assert len(limited) == 1 and len(attested) == 1 and attested[0]["attempt_id"].endswith(":r1")
    bound = kimi.rate_limit_bound(limited[0]["request_bytes"])
    assert limited[0]["actual_usd"] == str(bound) and bound > 0
    used = budget.snapshot()["arms"]["tt4"]
    assert used["attempts"] == {"failed": 0, "pending": 0, "settled": 2}
    assert Decimal(used["used_usd"]) == bound + Decimal("0.000468477")


def test_rate_limit_retries_are_capped_per_request(kimi, tmp_path, monkeypatch):
    isolate_state(kimi, monkeypatch, tmp_path)
    calls = kimi.CAMPAIGN / "calls"
    for number in range(kimi.RATE_LIMIT_RETRIES):
        stem = f"tt4:{OPAQUE}:{'b' * 64}"
        name = stem if number == 0 else f"{stem}:r{number}"
        kimi.save(calls / f"{name}.json", {"status": "rate_limited"})
    with pytest.raises(kimi.AdmissionError, match="exhausted"):
        kimi.next_attempt_id(kimi.CAMPAIGN, OPAQUE, "b" * 64)
    kimi.save(calls / f"tt4:{OPAQUE}:{'c' * 64}.json", {"status": "attested"})
    assert kimi.next_attempt_id(kimi.CAMPAIGN, OPAQUE, "c" * 64) == f"tt4:{OPAQUE}:{'c' * 64}"


def test_bound_failed_releases_the_hold_down_to_a_proven_bound(tmp_path):
    from openrouter_budget import Budget, BudgetError

    budget = Budget(tmp_path, "tt4", clock=lambda: MOMENT)
    budget.reserve("attempt-1", Decimal("0.22"), lambda: CASH)
    with pytest.raises(BudgetError, match="not failed"):
        budget.bound_failed("attempt-1", Decimal("0.01"), {"why": "pending"})
    budget.fail("attempt-1", "TransportError")
    assert Decimal(budget.snapshot()["arms"]["tt4"]["held_usd"]) == Decimal("0.22")
    with pytest.raises(BudgetError, match="exceeds the original"):
        budget.bound_failed("attempt-1", Decimal("0.23"), {"why": "too big"})
    result = budget.bound_failed("attempt-1", Decimal("0.003"), {"why": "429 body"})
    assert Decimal(result["released_usd"]) == Decimal("0.217")
    arm = budget.snapshot()["arms"]["tt4"]
    assert Decimal(arm["held_usd"]) == 0 and Decimal(arm["settled_usd"]) == Decimal("0.003")
    with pytest.raises(BudgetError, match="not failed"):
        budget.bound_failed("attempt-1", Decimal("0.001"), {"why": "twice"})


def test_recover_bounds_old_429_holds_and_supersedes_only_rate_limited_traces(
    kimi, tmp_path, monkeypatch
):
    import openrouter_budget
    from openrouter_budget import Budget

    isolate_state(kimi, monkeypatch, tmp_path)
    monkeypatch.setattr(openrouter_budget, "DEFAULT_STATE_DIR", tmp_path / "budget")
    budget = Budget(tmp_path / "budget", "tt4", clock=lambda: MOMENT)
    fingerprint = "d" * 64
    attempt_id = f"tt4:{OPAQUE}:{fingerprint}"
    budget.reserve(attempt_id, kimi.REQUEST_BOUND, lambda: fetch_cash_fixture())
    budget.fail(attempt_id, "TransportError")
    receipt = {
        "at": MOMENT.isoformat(),
        "attempt_id": attempt_id,
        "opaque_id": OPAQUE,
        "status": "error",
        "http_status": 429,
        "http_attempted": True,
        "request_bytes": 47680,
        "actual_usd": None,
        "input_tokens": None,
        "output_tokens": None,
    }
    kimi.save(kimi.CAMPAIGN / "calls" / f"{attempt_id}.json", receipt)
    other = "run-" + "e" * 24
    kimi.save(
        kimi.CAMPAIGN / "calls" / "tt4:other.json",
        dict(receipt, opaque_id=other, attempt_id="x", http_status=500),
    )
    for opaque, scored in ((OPAQUE, False), (other, False)):
        kimi.save(
            kimi.CAMPAIGN / "traces" / f"{opaque}.json",
            {"phase": "terminal", "scored": scored, "native_scan_attempted": True},
        )
        kimi.save(kimi.CAMPAIGN / "terminal" / f"{opaque}.json", {"reason": "TransportError"})
    result = kimi.recover()
    assert result["bounded_429_attempts"] == 1 and result["superseded_traces"] == 1
    amount = kimi.rate_limit_bound(47680)
    fixed = kimi.load(kimi.CAMPAIGN / "calls" / f"{attempt_id}.json")
    assert fixed["status"] == "rate_limited" and fixed["actual_usd"] == str(amount)
    assert (
        kimi.load(kimi.CAMPAIGN / "receipts/original-429-calls" / f"{attempt_id}.json")["status"]
        == "error"
    )
    assert not (kimi.CAMPAIGN / "traces" / f"{OPAQUE}.json").exists()
    assert (kimi.CAMPAIGN / "traces" / f"{other}.json").exists()  # not a 429-only failure
    assert (
        kimi.next_attempt_id(kimi.CAMPAIGN, OPAQUE, fingerprint) == f"tt4:{OPAQUE}:e1:{fingerprint}"
    )
    arm = budget.snapshot()["arms"]["tt4"]
    assert Decimal(arm["held_usd"]) == 0 and Decimal(arm["settled_usd"]) == amount
    assert kimi.recover()["bounded_429_attempts"] == 0  # idempotent
    assert kimi.recover(retry_failed=True)["superseded_traces"] == 1
    assert not (kimi.CAMPAIGN / "traces" / f"{other}.json").exists()


def fetch_cash_fixture():
    return CASH


def test_offline_proof_native_scan_sends_no_tools_and_executes_nothing(monkeypatch):
    pytest.importorskip("httpx2")
    pytest.importorskip("inspect_scout")
    pytest.importorskip("metr_scanners")
    monkeypatch.syspath_prepend(str(HERE))
    import kimi_transport_proof

    result = kimi_transport_proof.prove()
    assert result["verdicts"]["wire_requests"] > 1 and result["proved"], result["verdicts"]
    assert not result["scan"]["sentinel_hits"]
