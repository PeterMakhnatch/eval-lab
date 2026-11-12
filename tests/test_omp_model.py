"""Historical parser and sealed native transport boundaries."""

import builtins
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

import pytest

from evallab.readers import omp_model


def end_event():
    return {
        "type": "agent_end",
        "messages": [{
            "role": "assistant",
            "provider": "kimi-code",
            "model": "k3.1-pro-highspeed",
            "stopReason": "stop",
            "responseId": "response-fixture",
            "content": [
                {"type": "thinking", "thinking": "private reasoning is not the answer"},
                {"type": "text", "text": '{"score":7,'},
                {"type": "text", "text": '"reason":"fixture","quotes":[]}'},
            ],
            "usage": {"input": 10, "output": 5, "cacheRead": 20, "cacheWrite": 3, "reasoningTokens": 2},
        }],
    }


def stream(event=None):
    return "diagnostic\n[]\n" + json.dumps(end_event() if event is None else event) + "\n"


def test_final_text_excludes_reasoning_and_preserves_native_usage():
    reply = omp_model.parse_omp_stream(stream())
    assert json.loads(reply.text)["score"] == 7
    assert "private reasoning" not in reply.text
    assert reply.served_model == omp_model.KIMI_MODEL
    assert reply.response_id == "response-fixture"
    assert (reply.input_tokens, reply.output_tokens, reply.cache_read_tokens) == (10, 5, 20)
    assert (reply.cache_write_tokens, reply.reasoning_tokens, reply.total_tokens) == (3, 2, 38)


@pytest.mark.parametrize("event", [
    {"type": "model_changed"},
    {"type": "model_fallback"},
    {"type": "FallbackAttempt"},
    {"type": "session", "resolvedModelIsFallback": True},
])
def test_any_fallback_event_rejects_even_matching_final_model(event):
    with pytest.raises(omp_model.OmpRouteError, match="fallback or change"):
        omp_model.parse_omp_stream(json.dumps(event) + "\n" + stream())


@pytest.mark.parametrize("field,value", [
    ("provider", "paid-provider"),
    ("model", "different-model"),
    ("model", None),
])
def test_identity_must_be_explicit_and_exact(field, value):
    event = end_event()
    event["messages"][0][field] = value
    with pytest.raises(omp_model.OmpRouteError, match="served-model mismatch"):
        omp_model.parse_omp_stream(stream(event))


@pytest.mark.parametrize("field,value", [
    ("stopReason", "error"),
    ("stopReason", "length"),
    ("stopReason", "aborted"),
    ("refusal", "policy"),
    ("errorMessage", "cyber_policy"),
    ("content", [{"type": "refusal", "refusal": "no"}]),
    ("content", [{"type": "toolCall", "name": "bash"}]),
    ("content", [{"type": "text", "text": "I cannot assist with that request."}]),
    ("content", [{"type": "text", "text": "I'm sorry, I can't help with that."}]),
    ("content", [{"type": "text", "text": "I’m sorry, but I can’t assist with that request."}]),
    ("content", [{"type": "text", "text": "Sorry, but I cannot provide that."}]),
    ("content", [{"type": "text", "text": ""}]),
    ("content", [{"type": "thinking", "thinking": "only reasoning"}]),
    ("content", [{"type": "text", "text": None}]),
])
def test_refusal_incomplete_and_nontext_responses_are_not_completions(field, value):
    event = end_event()
    event["messages"][0][field] = value
    with pytest.raises(omp_model.OmpRouteError):
        omp_model.parse_omp_stream(stream(event))


@pytest.mark.parametrize("stdout", ["", "not json\n", '{"type":"agent_end"}', stream() + stream()])
def test_truncated_or_ambiguous_stream_fails_closed(stdout):
    with pytest.raises(omp_model.OmpRouteError):
        omp_model.parse_omp_stream(stdout)


@pytest.mark.parametrize("messages", [[], [end_event()["messages"][0]] * 2])
def test_exactly_one_final_assistant_turn(messages):
    with pytest.raises(omp_model.OmpRouteError, match="exactly one"):
        omp_model.parse_omp_stream(stream({"type": "agent_end", "messages": messages}))


@pytest.mark.parametrize("usage", [None, {}, {"input": True, "output": 5}, {"input": 1, "output": -1}])
def test_usage_cannot_be_missing_or_invented(usage):
    event = end_event()
    event["messages"][0]["usage"] = usage
    with pytest.raises(omp_model.OmpRouteError, match="usage"):
        omp_model.parse_omp_stream(stream(event))


def test_environment_keeps_oauth_home_but_removes_inherited_credentials():
    env = {
        "HOME": "/fixture/home", "PATH": "/bin", "OMP_PROFILE": "fixture",
        "OPENAI_API_KEY": "not-a-real-secret", "API_KEY": "fixture",
        "ANTHROPIC_OAUTH_TOKEN": "fixture", "KIMI_AUTH_TOKEN": "fixture",
        "CURSOR_ACCESS_TOKEN": "fixture", "A_KEYWORD": "not a credential",
    }
    assert omp_model.subscription_env(env) == {
        "HOME": "/fixture/home", "PATH": "/bin", "OMP_PROFILE": "fixture",
        "A_KEYWORD": "not a credential",
    }
    assert "OPENAI_API_KEY" in env  # Never mutate the parent environment.


def test_render_single_user_preserves_full_text_and_separates_system():
    text = "a" * 200_000
    assert omp_model.render_prompt([
        {"role": "system", "content": "first"},
        {"role": "system", "content": [{"type": "text", "text": "second"}]},
        {"role": "user", "content": text},
    ]) == ("first\n\nsecond", text)


def test_render_multiturn_keeps_role_boundaries():
    assert omp_model.render_prompt([
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "followup"},
    ]) == ("", "[user]\nquestion\n\n[assistant]\nanswer\n\n[user]\nfollowup")


@pytest.mark.parametrize("messages", [
    [], [{"role": "tool", "content": "result"}],
    [{"role": "user", "content": [{"type": "image", "image": "fixture"}]}],
    [{"role": "user", "content": None}],
    [{"role": "assistant", "content": "", "tool_calls": [{"function": "bash"}]}],
    [{"role": "user", "content": "x"}, {"role": "system", "content": "late"}],
])
def test_render_rejects_inputs_that_cannot_be_preserved(messages):
    with pytest.raises(ValueError):
        omp_model.render_prompt(messages)


SEALED_ROUTES = (
    omp_model.KIMI_MODEL,
    "xai-oauth/grok-4.3",
    "other-provider/other-model",
    omp_model.INSPECT_MODEL,
    "ambiguous-model",
    "",
)


def filesystem_snapshot(root):
    return {
        path.relative_to(root).as_posix(): None if path.is_dir() else path.read_bytes()
        for path in root.rglob("*")
    }


@pytest.fixture
def sealed_route_guard(monkeypatch):
    """Fail on attempted execution, provider imports, environment access or file IO."""
    @contextmanager
    def guard():
        attempts = []

        def forbidden(operation):
            def reject(*args, **kwargs):
                attempts.append(operation)
                raise AssertionError(f"sealed native route attempted {operation}")
            return reject

        class UnreadableEnvironment(Mapping):
            def __getitem__(self, key):
                forbidden("environment access")()

            def __iter__(self):
                forbidden("environment access")()

            def __len__(self):
                forbidden("environment access")()

        original_import = builtins.__import__

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            if (
                globals is not None and globals.get("__name__") == omp_model.__name__
                or name.split(".", 1)[0] in {
                    "inspect_ai", "inspect_scout", "metr_scanners", "openai", "anthropic",
                }
            ):
                forbidden(f"import {name}")()
            return original_import(name, globals, locals, fromlist, level)

        with monkeypatch.context() as patch:
            for module, names in (
                (subprocess, ("run", "Popen")),
                (tempfile, ("TemporaryDirectory", "NamedTemporaryFile", "mkdtemp", "mkstemp")),
                (Path, ("exists", "stat", "mkdir", "touch", "unlink", "rename", "replace")),
                (os, ("open", "mkdir", "remove", "unlink", "rename", "replace")),
                (builtins, ("open",)),
                (io, ("open",)),
            ):
                for name in names:
                    patch.setattr(module, name, forbidden(f"{module.__name__}.{name}"))
            patch.setattr(os, "environ", UnreadableEnvironment())
            patch.setattr(omp_model, "subscription_env", forbidden("credential filtering"))
            patch.setattr(builtins, "__import__", guarded_import)
            patch.setattr(importlib, "import_module", forbidden("dynamic import"))
            yield
            assert attempts == []

    return guard


def test_denied_requests_leave_historical_parser_usable(sealed_route_guard):
    historical_stdout = stream()
    with sealed_route_guard():
        for request in (
            omp_model.require_native_omp_transport,
            lambda: omp_model.call_omp("system", "historical transcript"),
            omp_model.register_omp_provider,
        ):
            with pytest.raises(
                omp_model.OmpTransportSealedError, match="read-only isolation is not established",
            ) as caught:
                request()
            assert isinstance(caught.value, omp_model.OmpRouteError)
        reply = omp_model.parse_omp_stream(historical_stdout)
    assert json.loads(reply.text)["score"] == 7
    assert reply.served_model == omp_model.KIMI_MODEL
    assert reply.response_id == "response-fixture"
    assert reply.total_tokens == 38


@pytest.mark.parametrize("model", SEALED_ROUTES)
@pytest.mark.parametrize("artifacts_exist", [False, True])
def test_every_route_denies_before_receipt_halt_or_filesystem_access(
    tmp_path, sealed_route_guard, model, artifacts_exist,
):
    receipt = tmp_path / "native" / "calls.jsonl"
    halt = tmp_path / "native" / "halt.json"
    if artifacts_exist:
        receipt.parent.mkdir()
        receipt.write_bytes(b"immutable historical receipt\n")
        halt.write_bytes(b"immutable historical halt\n")
    before = filesystem_snapshot(tmp_path)
    with sealed_route_guard():
        for _ in range(3):
            with pytest.raises(omp_model.OmpTransportSealedError, match="Native OMP CLI transport is sealed"):
                omp_model.call_omp(
                    "private system", "private transcript", model=model, timeout=0,
                    receipt_path=receipt, halt_path=halt,
                )
    assert filesystem_snapshot(tmp_path) == before


def test_parallel_repeated_requests_never_start_transport_or_provider(tmp_path, sealed_route_guard):
    barrier = threading.Barrier(8)
    before = filesystem_snapshot(tmp_path)

    def request(index):
        barrier.wait(timeout=10)
        for _ in range(3):
            with pytest.raises(omp_model.OmpTransportSealedError, match="sealed"):
                omp_model.call_omp(
                    "system", "transcript", model=SEALED_ROUTES[index % len(SEALED_ROUTES)],
                    receipt_path=tmp_path / f"calls-{index}.jsonl", halt_path=tmp_path / "halt.json",
                )
            with pytest.raises(omp_model.OmpTransportSealedError, match="sealed"):
                omp_model.register_omp_provider()
            with pytest.raises(omp_model.OmpTransportSealedError, match="sealed"):
                omp_model.require_native_omp_transport()
        return index

    with ThreadPoolExecutor(max_workers=8) as pool, sealed_route_guard():
        futures = [pool.submit(request, index) for index in range(8)]
        assert [future.result(timeout=10) for future in futures] == list(range(8))
    assert filesystem_snapshot(tmp_path) == before


def test_generic_route_attestation_includes_provider_and_full_model_id():
    event = end_event()
    event["messages"][0].update(provider="xai-oauth", model="grok-4.3")
    reply = omp_model.parse_omp_stream(stream(event), "xai-oauth/grok-4.3")
    assert reply.served_model == "xai-oauth/grok-4.3"
    with pytest.raises(omp_model.OmpRouteError, match="served-model mismatch"):
        omp_model.parse_omp_stream(stream(event), "kimi-code/k3.1-pro-highspeed")



@pytest.mark.parametrize("preloaded", [False, True])
def test_registration_denies_before_optional_import_cached_factory_or_handler(
    monkeypatch, tmp_path, sealed_route_guard, preloaded,
):
    starts = []

    def forbidden(*args, **kwargs):
        starts.append("provider")
        raise AssertionError("sealed registration started a provider or handler")

    if preloaded:
        package = ModuleType("inspect_ai")
        model = ModuleType("inspect_ai.model")
        model.modelapi = forbidden
        model.get_model = forbidden
        package.model = model
        monkeypatch.setitem(sys.modules, "inspect_ai", package)
        monkeypatch.setitem(sys.modules, "inspect_ai.model", model)
        monkeypatch.setattr(omp_model, "_PROVIDER", forbidden, raising=False)
    else:
        monkeypatch.delitem(sys.modules, "inspect_ai", raising=False)
        monkeypatch.delitem(sys.modules, "inspect_ai.model", raising=False)
        monkeypatch.delattr(omp_model, "_PROVIDER", raising=False)
    before = filesystem_snapshot(tmp_path)
    with sealed_route_guard():
        for _ in range(3):
            with pytest.raises(omp_model.OmpTransportSealedError, match="Inspect provider registration"):
                omp_model.register_omp_provider()
    assert starts == []
    assert filesystem_snapshot(tmp_path) == before
