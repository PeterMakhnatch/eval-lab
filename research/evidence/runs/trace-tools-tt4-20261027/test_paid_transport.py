"""Offline wire/funding tests; optional HTTP tests use only an in-memory transport."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

MOMENT = datetime(2026, 10, 28, 10, 0, tzinfo=UTC)


@pytest.fixture
def paid(monkeypatch):
    here = Path(__file__).resolve().parent
    monkeypatch.syspath_prepend(str(here))
    spec = importlib.util.spec_from_file_location("tt4_paid_transport_test", here / "paid.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


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
        "provider": "Google AI Studio",
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
            "cost": 0.000009,
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
                },
            }
        ],
    }


def admission(paid, key="not-a-real-key"):
    return {
        "schema": "evallab.openrouter_funding_admission/v1",
        "at": MOMENT.isoformat(),
        "expires_at": (MOMENT + timedelta(minutes=30)).isoformat(),
        "route": paid.ROUTE,
        "provider": paid.PROVIDER,
        "funding": "openrouter_credits",
        "applicable_google_byok": False,
        "api_key_sha256": hashlib.sha256(key.encode()).hexdigest(),
        "evidence_kind": "read-only-provider-integration-metadata",
        "evidence_reference": "temporary deterministic fixture; not production admission",
        "verified_by": "test fixture",
    }


def test_request_bound_is_context_plus_capped_output(paid):
    assert Decimal("0.106496") == paid.THEORETICAL_BOUND
    assert Decimal("0.11") == paid.REQUEST_BOUND
    assert paid.THEORETICAL_BOUND < paid.REQUEST_BOUND
    assert Decimal("14.85") == paid.REQUEST_BOUND * 135
    paid.validate_request(request_body(paid))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model", "openrouter/auto"),
        ("max_tokens", 4097),
        ("stream", True),
        ("reasoning", {"enabled": True}),
        ("models", ["other/model"]),
        ("plugins", [{"id": "web"}]),
        ("transforms", ["middle-out"]),
        ("tools", [{"type": "function"}]),
        ("service_tier", "priority"),
        ("max_completion_tokens", 8192),
        ("response_format", {"type": "json_object"}),
    ],
)
def test_unapproved_wire_options_never_reach_reservation(paid, field, value):
    body = request_body(paid)
    body[field] = value
    with pytest.raises(paid.TransportError):
        paid.validate_request(body)


@pytest.mark.parametrize(
    "change",
    [
        {"allow_fallbacks": True},
        {"only": ["google-vertex"]},
        {"data_collection": "allow"},
        {"max_price": {"prompt": 0.2, "completion": 0.4, "request": 0}},
    ],
)
def test_gateway_price_privacy_and_provider_policy_are_fixed(paid, change):
    body = request_body(paid)
    body["provider"].update(change)
    with pytest.raises(paid.TransportError):
        paid.validate_request(body)


def test_media_cannot_escape_the_text_only_cost_bound(paid):
    body = request_body(paid)
    body["messages"][0]["content"] = [{"type": "image_url", "image_url": {"url": "fixture"}}]
    with pytest.raises(paid.TransportError):
        paid.validate_request(body)


def test_response_has_exact_served_identity_native_usage_and_no_content_receipt(paid):
    receipt, text = paid.response_receipt(response_body(paid))
    assert receipt["served_model"] == paid.MODEL
    assert receipt["actual_usd"] == "0.000009"
    assert receipt["input_tokens"] == 10 and receipt["output_tokens"] == 20
    assert receipt["completion_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert "content" not in receipt and "reason" not in receipt


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("model",), "other/model"),
        (("provider",), "unapproved provider"),
        (("usage", "is_byok"), True),
        (("usage", "cost"), None),
        (("usage", "cost"), True),
        (("usage", "cost"), "NaN"),
        (("usage", "cost"), "0.106497"),
        (("usage", "prompt_tokens"), 1048577),
        (("usage", "completion_tokens"), 4097),
        (("usage", "completion_tokens"), True),
        (("usage", "total_tokens"), None),
        (("usage", "total_tokens"), True),
        (("usage", "total_tokens"), 31),
    ],
)
def test_response_identity_billing_and_bounds_fail_closed(paid, path, value):
    body = response_body(paid)
    target = body
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(paid.TransportError):
        paid.response_receipt(body)


def test_refusal_and_truncation_are_not_scores(paid):
    body = response_body(paid)
    body["choices"][0]["message"]["refusal"] = "refused"
    with pytest.raises(paid.TransportError):
        paid.response_receipt(body)
    body = response_body(paid)
    body["choices"][0]["finish_reason"] = "length"
    with pytest.raises(paid.TransportError):
        paid.response_receipt(body)


def test_missing_funding_evidence_blocks_before_any_backend(paid, tmp_path):
    with pytest.raises(paid.AdmissionError, match="no inference probe"):
        paid.funding_admission(tmp_path, "not-a-real-key", clock=MOMENT)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "change",
    [
        {"applicable_google_byok": True},
        {"funding": "Google BYOK"},
        {"route": "openrouter/other/model"},
        {"api_key_sha256": "wrong-key"},
        {"evidence_kind": "inference-response-is_byok"},
        {"evidence_reference": ""},
        {"verified_by": ""},
        {"expires_at": (MOMENT - timedelta(seconds=1)).isoformat()},
        {"at": (MOMENT + timedelta(seconds=3)).isoformat()},
    ],
)
def test_unproven_or_stale_credit_only_scope_is_refused(paid, tmp_path, change):
    receipt = admission(paid)
    receipt.update(change)
    (tmp_path / "funding-admission.json").write_text(json.dumps(receipt))
    with pytest.raises(paid.AdmissionError):
        paid.funding_admission(tmp_path, "not-a-real-key", clock=MOMENT)


def test_authoritative_key_scoped_receipt_admits_only_within_its_interval(paid, tmp_path):
    path = tmp_path / "funding-admission.json"
    path.write_text(json.dumps(admission(paid)))
    result = paid.funding_admission(tmp_path, "not-a-real-key", clock=MOMENT)
    assert result["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(paid.AdmissionError):
        paid.funding_admission(tmp_path, "different-key", clock=MOMENT)


def test_contract_append_is_idempotent_without_extra_schema_keys(paid, tmp_path):
    record = {
        "schema": "evallab.trace_tool_result/v1",
        "run_id": "r",
        "tool": "scout_metr",
        "detector": "reward_hacking_scanner",
        "detector_version": "v",
        "scan": "s",
        "value": None,
    }
    path = tmp_path / "records.jsonl"
    paid.append_once(path, [record, record])
    paid.append_once(path, [record])
    assert paid.jsonl(path) == [record]
    assert "batch_id" not in paid.jsonl(path)[0]


def test_guarded_native_http_attempt_is_reserved_once_and_duplicate_never_sent(
    paid, tmp_path, monkeypatch
):
    httpx2 = pytest.importorskip("httpx2")
    pytest.importorskip("metr_scanners")
    from openrouter_budget import Budget

    class Wire(httpx2.AsyncBaseTransport):
        calls = 0

        async def handle_async_request(self, request):
            self.calls += 1
            return httpx2.Response(200, json=response_body(paid), request=request)

    wire = Wire()
    budget = Budget(tmp_path / "budget", "tt4", clock=lambda: MOMENT)
    monkeypatch.setattr(paid, "funding_admission", lambda *a, **kw: {"sha256": "fixture"})
    monkeypatch.setattr(paid, "SPEND", tmp_path / "canonical.jsonl")
    monkeypatch.setattr(paid, "STATE", tmp_path / "state")
    transport = paid.guarded_transport(
        tmp_path / "campaign",
        "run-" + "a" * 24,
        "not-a-real-key",
        underlying=wire,
        budget=budget,
        cash_reader=lambda: {
            "available_usd": "100",
            "fetched_at": MOMENT.isoformat(),
            "source": "fixture",
        },
    )

    async def invoke():
        wrong = httpx2.Request(
            "POST",
            paid.BASE_URL + "/chat/completions",
            json=request_body(paid),
            headers={"Authorization": "Bearer other-fixture"},
        )
        with pytest.raises(paid.AdmissionError, match="credential differs"):
            await transport.handle_async_request(wrong)
        assert wire.calls == 0 and budget.snapshot()["arms"]["tt4"]["used_usd"] == "0"
        first = httpx2.Request(
            "POST",
            paid.BASE_URL + "/chat/completions",
            json=request_body(paid),
            headers={"Authorization": "Bearer not-a-real-key"},
        )
        response = await transport.handle_async_request(first)
        assert response.status_code == 200
        duplicate = httpx2.Request(
            "POST",
            paid.BASE_URL + "/chat/completions",
            json=request_body(paid),
            headers={"Authorization": "Bearer not-a-real-key"},
        )
        with pytest.raises(paid.AdmissionError, match="duplicate_attempt"):
            await transport.handle_async_request(duplicate)
        await transport.aclose()

    asyncio.run(invoke())
    assert wire.calls == 1
    assert budget.snapshot()["arms"]["tt4"]["used_usd"] == "0.000009000000"
    rows = paid.jsonl(tmp_path / "canonical.jsonl")
    assert len(rows) == 1 and rows[0]["usd"] == 0.000009
    assert (tmp_path / "campaign/receipts/production-call-smoke.json").exists()


def test_unknown_cost_keeps_full_reservation_and_stops_before_more_http(
    paid, tmp_path, monkeypatch
):
    httpx2 = pytest.importorskip("httpx2")
    pytest.importorskip("metr_scanners")
    from openrouter_budget import Budget

    class Wire(httpx2.AsyncBaseTransport):
        calls = 0

        async def handle_async_request(self, request):
            self.calls += 1
            body = response_body(paid)
            del body["usage"]["cost"]
            return httpx2.Response(200, json=body, request=request)

    wire = Wire()
    budget = Budget(tmp_path / "budget", "tt4", clock=lambda: MOMENT)
    monkeypatch.setattr(paid, "funding_admission", lambda *a, **kw: {"sha256": "fixture"})
    monkeypatch.setattr(paid, "SPEND", tmp_path / "canonical.jsonl")
    monkeypatch.setattr(paid, "STATE", tmp_path / "state")
    transport = paid.guarded_transport(
        tmp_path / "campaign",
        "run-" + "b" * 24,
        "not-a-real-key",
        underlying=wire,
        budget=budget,
        cash_reader=lambda: {
            "available_usd": "100",
            "fetched_at": MOMENT.isoformat(),
            "source": "fixture",
        },
    )

    async def invoke():
        request = httpx2.Request(
            "POST",
            paid.BASE_URL + "/chat/completions",
            json=request_body(paid),
            headers={"Authorization": "Bearer not-a-real-key"},
        )
        with pytest.raises(paid.TransportError):
            await transport.handle_async_request(request)
        with pytest.raises(paid.AdmissionError, match="halted"):
            await transport.handle_async_request(request)
        await transport.aclose()

    asyncio.run(invoke())
    assert wire.calls == 1
    assert budget.snapshot()["arms"]["tt4"]["held_usd"] == "0.11"
    row = paid.jsonl(tmp_path / "canonical.jsonl")[0]
    assert row["usd"] is None and row["input_tokens"] is None


def test_missing_funding_blocks_run_before_optional_model_or_account_calls(
    paid, tmp_path, monkeypatch
):
    monkeypatch.setattr(paid, "CAMPAIGN", tmp_path / "campaign")
    monkeypatch.setenv("OPENROUTER_API_KEY", "not-a-real-key")

    def forbidden(*args, **kwargs):
        raise AssertionError("funding refusal must precede any account/model request")

    monkeypatch.setattr(paid.urllib.request, "urlopen", forbidden)
    with pytest.raises(paid.AdmissionError, match="no inference probe"):
        paid.run([], None)
    assert not (tmp_path / "campaign").exists()


def test_native_sdk_roundtrip_enforces_wire_policy_and_preserves_usage(paid, tmp_path, monkeypatch):
    httpx2 = pytest.importorskip("httpx2")
    inspect_model = pytest.importorskip("inspect_ai.model")
    pytest.importorskip("metr_scanners")
    from inspect_ai.model._model_config import model_to_model_config
    from metr_scanners.reward_hacking import RewardHacking
    from openrouter_budget import Budget

    async def forbidden_network(*args, **kwargs):
        raise AssertionError("native-client unit tests must never send a network request")

    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", forbidden_network)

    class Wire(httpx2.AsyncBaseTransport):
        calls = 0

        async def handle_async_request(self, request):
            self.calls += 1
            paid.validate_request(json.loads(request.content))
            body = response_body(paid)
            body["id"] = "gen-test-" + str(self.calls)
            return httpx2.Response(200, json=body, request=request)

    wire = Wire()
    campaign = tmp_path / "campaign"
    budget = Budget(tmp_path / "budget", "tt4", clock=lambda: MOMENT)
    original_transport = paid.guarded_transport
    monkeypatch.setattr(paid, "CAMPAIGN", campaign)
    monkeypatch.setattr(paid, "funding_admission", lambda *a, **kw: {"sha256": "fixture"})
    monkeypatch.setattr(paid, "SPEND", tmp_path / "canonical.jsonl")
    monkeypatch.setattr(paid, "STATE", tmp_path / "state")
    monkeypatch.setattr(
        paid,
        "guarded_transport",
        lambda c, o, k: original_transport(
            c,
            o,
            k,
            underlying=wire,
            budget=budget,
            cash_reader=lambda: {
                "available_usd": "100",
                "fetched_at": MOMENT.isoformat(),
                "source": "fixture",
            },
        ),
    )
    paid.register_provider()
    config = inspect_model.GenerateConfig(
        max_tokens=4096,
        max_retries=0,
        max_connections=4,
        cache_prompt=False,
        fallback_models=[],
        extra_body={"plugins": [], "transforms": [], "usage": {"include": True}},
    )
    model = inspect_model.get_model(
        paid.INSPECT_MODEL,
        api_key="not-a-real-key",
        memoize=False,
        config=config,
        campaign=str(campaign),
        opaque_id="run-" + "c" * 24,
    )
    portable = model_to_model_config(model).model_dump_json()
    assert "not-a-real-key" not in portable and paid.INSPECT_MODEL in portable

    async def invoke():
        response_config = inspect_model.GenerateConfig(
            response_schema=inspect_model.ResponseSchema(
                name="RewardHacking", json_schema=RewardHacking.model_json_schema()
            )
        )
        async with model:
            first = await model.generate("fixture native prompt", config=response_config)
            await model.api.aclose()
            model.api.initialize()
            second = await model.generate("distinct fixture native prompt", config=response_config)
            assert RewardHacking.model_validate_json(second.completion).score == 5
            return first

    output = asyncio.run(invoke())
    assert output.model == paid.MODEL
    assert output.usage.input_tokens == 10
    assert output.usage.output_tokens == 20
    assert RewardHacking.model_validate_json(output.completion).score == 5
    assert wire.calls == 2
    assert budget.snapshot()["arms"]["tt4"]["used_usd"] == "0.000018000000"


def test_catalog_drift_cannot_invalidate_the_hard_request_bound(paid):
    endpoint = {
        "tag": paid.PROVIDER,
        "service_tier": None,
        "model_id": paid.MODEL,
        "provider_name": "Google AI Studio",
        "context_length": 1048576,
        "max_completion_tokens": 65535,
        "pricing": {"prompt": "0.0000001", "completion": "0.0000004"},
        "supported_parameters": [
            "structured_outputs",
            "response_format",
            "max_tokens",
            "reasoning",
        ],
    }
    assert (
        paid.validate_catalog({"data": {"endpoints": [endpoint]}})["request_bound_usd"]
        == "0.1064960"
    )
    for change in (
        {"context_length": 2097152},
        {"service_tier": "priority"},
        {"pricing": {"prompt": "0.0000002", "completion": "0.0000004"}},
        {"supported_parameters": ["max_tokens"]},
    ):
        altered = copy.deepcopy(endpoint)
        altered.update(change)
        with pytest.raises(paid.AdmissionError):
            paid.validate_catalog({"data": {"endpoints": [altered]}})


def test_native_metr_scan_preserves_original_step_references_without_network(
    paid, tmp_path, monkeypatch
):
    httpx2 = pytest.importorskip("httpx2")
    scout = pytest.importorskip("inspect_scout")
    pytest.importorskip("metr_scanners")
    from openrouter_budget import Budget

    from evallab.readers.scout_clouddb import _check_transcripts, to_transcript

    async def forbidden_network(*args, **kwargs):
        raise AssertionError("native-scanner unit tests must never send a network request")

    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", forbidden_network)

    opaque = "run-" + "d" * 24
    campaign, prior = tmp_path / "campaign", tmp_path / "prior"
    messages = [
        {
            "step_index": n,
            "role": role,
            "content": content,
            "reasoning": None,
            "tool_calls": [],
            "tool_call_id": None,
        }
        for n, role, content in ((7, "user", "fixture task"), (11, "assistant", "fixture response"))
    ]

    async def insert():
        async with scout.transcripts_db(str(prior / "transcripts")) as database:
            await database.insert([to_transcript(opaque, messages)])

    _check_transcripts(prior / "transcripts", claim=True)
    asyncio.run(insert())

    class Wire(httpx2.AsyncBaseTransport):
        calls = 0

        async def handle_async_request(self, request):
            self.calls += 1
            body = response_body(paid)
            body["choices"][0]["message"]["content"] = json.dumps(
                {
                    "quotes": ["[M1] fixture task"],
                    "reason": "deterministic test fixture only",
                    "score": 5,
                }
            )
            return httpx2.Response(200, json=body, request=request)

    wire = Wire()
    budget = Budget(tmp_path / "budget", "tt4", clock=lambda: MOMENT)
    original_transport = paid.guarded_transport
    monkeypatch.setattr(paid, "CAMPAIGN", campaign)
    monkeypatch.setattr(paid, "TRANSCRIPTS", prior / "transcripts")
    monkeypatch.setattr(paid, "SCANS", tmp_path / "scans")
    monkeypatch.setattr(paid, "funding_admission", lambda *a, **kw: {"sha256": "fixture"})
    monkeypatch.setattr(paid, "SPEND", tmp_path / "canonical.jsonl")
    monkeypatch.setattr(paid, "STATE", tmp_path / "state")
    monkeypatch.setattr(
        paid,
        "guarded_transport",
        lambda c, o, k: original_transport(
            c,
            o,
            k,
            underlying=wire,
            budget=budget,
            cash_reader=lambda: {
                "available_usd": "100",
                "fetched_at": MOMENT.isoformat(),
                "source": "fixture",
            },
        ),
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "not-a-real-key")
    paid.register_provider()
    status = paid.scan_one(opaque)
    assert status.complete and wire.calls == 1
    assert paid.locate_scan(opaque) == status.location
    assert paid.record_result(opaque, "fixture-trace", status.location) == 5
    native = [
        row
        for row in paid.jsonl(campaign / "receipts/results.jsonl")
        if row["detector"] == "reward_hacking_scanner"
    ]
    assert len(native) == 1 and native[0]["flag"] is None
    assert native[0]["evidence_steps"] and set(native[0]["evidence_steps"]) <= {7, 11}
    thresholds = {
        row["detector"]: row["flag"] for row in paid.jsonl(campaign / "receipts/results.jsonl")
    }
    assert thresholds["reward_hacking_scanner@ge5"] is True
    assert thresholds["reward_hacking_scanner@ge7"] is False


def test_stale_projection_is_refused_before_native_model_creation(paid, tmp_path, monkeypatch):
    from evallab.readers.scout_clouddb import StaleProjectionError

    stale = tmp_path / "unversioned"
    stale.mkdir()
    (stale / "historical.parquet").write_bytes(b"not-read")
    monkeypatch.setattr(paid, "TRANSCRIPTS", stale)
    with pytest.raises(StaleProjectionError, match="unversioned"):
        paid.scan_one("run-" + "e" * 24)


def test_inherited_credentials_and_tracing_sinks_are_not_activated(paid, monkeypatch):
    inherited = {
        "OPENROUTER_API_KEY": "fixture-inference-only",
        "OPENAI_API_KEY": "fixture-other",
        "GITHUB_TOKEN": "fixture-other",
        "LMNR_BASE_URL": "https://never.invalid",
        "OPENROUTER_MANAGEMENT_KEY": "fixture-management-not-inference",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "https://never.invalid",
        "HOME": "/fixture",
        "PATH": "/fixture/bin",
        "EVALLAB_DISABLE_LOCAL_DOCKER": "1",
    }
    monkeypatch.setattr(paid.os, "environ", inherited)
    paid.isolate_environment()
    assert inherited == {
        "OPENROUTER_API_KEY": "fixture-inference-only",
        "HOME": "/fixture",
        "PATH": "/fixture/bin",
        "EVALLAB_DISABLE_LOCAL_DOCKER": "1",
        "PYTHON_DOTENV_DISABLED": "1",
    }
