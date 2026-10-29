"""Historical parser, sealed native transport and offline ledger boundaries."""

import builtins
import fcntl
import importlib.util
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
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace

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


def load_runner(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = (
        Path(__file__).resolve().parents[1]
        / "research/evidence/runs/trace-tools-tt4-20261027/run.py"
    )
    spec = importlib.util.spec_from_file_location("tt4_runner_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_seeded_round_robin_keeps_prefixes_balanced_without_exposing_strata(monkeypatch, tmp_path):
    runner = load_runner(monkeypatch, tmp_path)
    sample = [
        {"trace_id": f"{stratum}-{index}", "stratum": stratum}
        for stratum, count in zip(runner.STRATA, (8, 7, 5), strict=True)
        for index in range(count)
    ]
    ordered = runner.round_robin(sample)
    assert ordered == runner.round_robin(list(reversed(sample)))
    assert ordered != runner.round_robin(sample, seed=42)
    assert set(ordered) == {row["trace_id"] for row in sample}
    assert len(ordered) == len(sample)
    strata = {row["trace_id"]: row["stratum"] for row in sample}
    for offset in range(0, 15, 3):
        assert {strata[trace] for trace in ordered[offset:offset + 3]} == set(runner.STRATA)
    assert all(isinstance(trace, str) for trace in ordered)


def test_resumption_never_reuses_other_models_threshold_rows_or_missing_scores(monkeypatch, tmp_path):
    runner = load_runner(monkeypatch, tmp_path)
    base = {"tool": "scout_metr", "detector": runner.DETECTOR, "detector_version": runner.DETECTOR_VERSION, "flag": None}
    rows = [
        dict(base, run_id="zero", value=0),
        dict(base, run_id="ten", value=10),
        dict(base, run_id="missing", value=None),
        dict(base, run_id="bool", value=True),
        dict(base, run_id="nan", value=float("nan")),
        dict(base, run_id="outside", value=11),
        dict(base, run_id="grok", value=9, detector_version="omp-xai-oauth-grok-4.3"),
        dict(base, run_id="threshold-only", value=9, detector="reward_hacking_scanner@ge7"),
    ]
    assert runner.scores(rows) == {"zero": 0, "ten": 10}


def test_spend_recovery_is_idempotent_across_partial_dual_ledger_writes(monkeypatch, tmp_path):
    runner = load_runner(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "CAMPAIGN", tmp_path / "campaign")
    monkeypatch.setattr(runner, "RECEIPTS", tmp_path / "campaign/receipts")
    monkeypatch.setattr(runner, "STATE", tmp_path / "trace-tools")
    monkeypatch.setattr(runner, "SPEND", tmp_path / "canonical/spend.jsonl")
    opaque = "run-fixture"
    unrelated = {"tool": "other-lane", "basis": {"authorization": "separate"}}
    runner.append(runner.SPEND, [unrelated])
    runner.append(runner.CAMPAIGN / f"{opaque}-calls.jsonl", [
        {"ok": True, "input_tokens": 10, "cache_read_tokens": 20, "output_tokens": 3},
        {"ok": False, "error": "fixture"},
    ])
    runner.flush_spend(opaque)
    # Simulate interruption after the canonical append but before the other
    # ledger/cursor were durable. Replaying must not append a second charge.
    (runner.CAMPAIGN / f"{opaque}-spend-cursor.json").unlink()
    (runner.STATE / "spend.jsonl").unlink()
    runner.flush_spend(opaque)
    runner.flush_spend(opaque)
    assert len(runner.jsonl(runner.SPEND)) == 2
    assert runner.jsonl(runner.SPEND)[0] == unrelated
    assert len(runner.jsonl(runner.STATE / "spend.jsonl")) == 1
    assert len(runner.jsonl(runner.RECEIPTS / "spend.jsonl")) == 1
    record = runner.jsonl(runner.SPEND)[1]
    assert (record["input_tokens"], record["output_tokens"], record["usd"]) == (30, 3, 0)


@pytest.fixture
def kimi_quota_report():
    at = int(datetime.now(UTC).timestamp() * 1000)
    return {"generatedAt": at, "reports": [{"provider": "kimi-code", "fetchedAt": at - 100, "limits": [
        {
            "label": name, "status": "ok",
            "scope": {"provider": "kimi-code", "windowId": window_id},
            "amount": {"usedFraction": 0.2, "remainingFraction": 0.8},
            "window": {"id": window_id, "resetsAt": (index + 1) * 1000},
        }
        for index, (name, window_id) in enumerate((
            ("5h", "5h"), ("Monthly total", "limit_month_total"), ("Monthly code", "limit_month_code"),
        ))
    ]}]}


@pytest.mark.parametrize("window_index", [0, 1, 2])
@pytest.mark.parametrize("used,available", [(0.899999, True), (0.9, False), (1, False)])
def test_historical_quota_reserves_coding_headroom_in_every_window(
    monkeypatch, tmp_path, kimi_quota_report, sealed_route_guard, window_index, used, available,
):
    runner = load_runner(monkeypatch, tmp_path)
    limit = kimi_quota_report["reports"][0]["limits"][window_index]
    limit["amount"] = {"usedFraction": used, "remainingFraction": 1 - used}
    report_before = json.dumps(kimi_quota_report, sort_keys=True)
    before = filesystem_snapshot(tmp_path)
    with sealed_route_guard():
        result = runner.parse_quota_report(
            kimi_quota_report, observed_at_ms=kimi_quota_report["generatedAt"],
        )
    assert result["available"] is available
    assert result["stop_used_fraction"] == 0.9
    expected_resets = [] if available else [
        datetime.fromtimestamp(limit["window"]["resetsAt"] / 1000, UTC).isoformat(),
    ]
    assert result["reset_at"] == expected_resets
    assert result["freshness_ttl_ms"] == 60_000
    assert result["report"] == kimi_quota_report
    assert json.dumps(kimi_quota_report, sort_keys=True) == report_before
    assert filesystem_snapshot(tmp_path) == before


@pytest.mark.parametrize("field", ["usedFraction", "remainingFraction"])
@pytest.mark.parametrize("value", [None, True, "0.1", float("nan"), float("inf"), -0.1, 1.1])
def test_historical_quota_unknown_amount_is_fail_closed(
    monkeypatch, tmp_path, kimi_quota_report, sealed_route_guard, field, value,
):
    runner = load_runner(monkeypatch, tmp_path)
    kimi_quota_report["reports"][0]["limits"][0]["amount"][field] = value
    before = filesystem_snapshot(tmp_path)
    with (
        sealed_route_guard(),
        pytest.raises(RuntimeError, match="unknown subscription allowance"),
    ):
        runner.parse_quota_report(
            kimi_quota_report, observed_at_ms=kimi_quota_report["generatedAt"],
        )
    assert filesystem_snapshot(tmp_path) == before


@pytest.mark.parametrize("status", [None, "unknown"])
def test_historical_quota_unknown_status_is_fail_closed(
    monkeypatch, tmp_path, kimi_quota_report, sealed_route_guard, status,
):
    runner = load_runner(monkeypatch, tmp_path)
    kimi_quota_report["reports"][0]["limits"][0]["status"] = status
    before = filesystem_snapshot(tmp_path)
    with (
        sealed_route_guard(),
        pytest.raises(RuntimeError, match="unknown subscription allowance"),
    ):
        runner.parse_quota_report(
            kimi_quota_report, observed_at_ms=kimi_quota_report["generatedAt"],
        )
    assert filesystem_snapshot(tmp_path) == before


@pytest.mark.parametrize("mutation", [
    "wrong_provider", "scope_provider", "unknown_window", "mismatched_window",
    "duplicate_window", "missing_window", "stale_fetch", "stale_report",
    "future_fetch", "future_report", "boolean_fetch", "missing_fetch",
])
def test_historical_quota_refuses_stale_or_misidentified_machine_windows(
    monkeypatch, tmp_path, kimi_quota_report, sealed_route_guard, mutation,
):
    runner = load_runner(monkeypatch, tmp_path)
    observed = kimi_quota_report["generatedAt"]
    provider = kimi_quota_report["reports"][0]
    limit = provider["limits"][0]
    if mutation == "wrong_provider":
        provider["provider"] = "another-provider"
    elif mutation == "scope_provider":
        limit["scope"]["provider"] = "another-provider"
    elif mutation == "unknown_window":
        limit["scope"]["windowId"] = "unknown"
    elif mutation == "mismatched_window":
        limit["window"]["id"] = "unknown"
    elif mutation == "duplicate_window":
        provider["limits"][1] = limit.copy()
    elif mutation == "missing_window":
        provider["limits"].pop()
    elif mutation == "stale_fetch":
        provider["fetchedAt"] = observed - 60_001
    elif mutation == "stale_report":
        kimi_quota_report["generatedAt"] -= 60_001
        provider["fetchedAt"] -= 60_001
    elif mutation == "future_fetch":
        provider["fetchedAt"] = observed + 1
    elif mutation == "future_report":
        kimi_quota_report["generatedAt"] += 60_001
    elif mutation == "boolean_fetch":
        provider["fetchedAt"] = True
    else:
        provider.pop("fetchedAt")
    before = filesystem_snapshot(tmp_path)
    with (
        sealed_route_guard(),
        pytest.raises(RuntimeError, match="unknown subscription allowance"),
    ):
        runner.parse_quota_report(kimi_quota_report, observed_at_ms=observed)
    assert filesystem_snapshot(tmp_path) == before


def test_canonical_privacy_abstention_never_sends_or_becomes_a_negative(monkeypatch, tmp_path):
    runner = load_runner(monkeypatch, tmp_path)
    row = {"run_id": "private-fixture", "opaque_id": "run-" + "f" * 24}

    def forbidden(*args, **kwargs):
        raise AssertionError("privacy abstentions must not invoke a model or usage command")

    monkeypatch.setattr(subprocess, "run", forbidden)
    runner.source_abstention(row, "secret_unsafe")
    runner.source_abstention(row, "secret_unsafe")
    records = runner.jsonl(runner.STATE / "results-v1.jsonl")
    assert len(records) == 3
    assert {record["detector"] for record in records} == {
        "reward_hacking_scanner", "reward_hacking_scanner@ge5", "reward_hacking_scanner@ge7",
    }
    assert all(record["value"] is None and record["flag"] is None for record in records)
    assert all(record["evidence_steps"] == [] and record["scan"].startswith("abstain:") for record in records)
    assert runner.scores(records) == {}
    status = json.loads((runner.CAMPAIGN / f'{row["opaque_id"]}-status.json').read_text())
    assert status["terminal_abstention"] is True and status["native_scan_attempted"] is False
    assert not runner.SPEND.exists()


def test_missing_results_are_explicit_nulls_without_disabling_resume(monkeypatch, tmp_path):
    runner = load_runner(monkeypatch, tmp_path)
    keys = [
        {"run_id": name, "opaque_id": "run-" + character * 24}
        for name, character in (("done", "a"), ("privacy", "b"), ("unrun", "c"))
    ]
    runner.save(runner.RECEIPTS / "admission.json", {
        "input_projection": {"rejected": [{"opaque_id": keys[1]["opaque_id"], "category": "secret_unsafe"}]},
    })
    runner.append(runner.STATE / "results-v1.jsonl", [{
        "tool": "scout_metr", "detector": runner.DETECTOR,
        "detector_version": runner.DETECTOR_VERSION, "run_id": "done", "value": 8,
    }])
    unrun_status = runner.CAMPAIGN / f'{keys[2]["opaque_id"]}-status.json'
    runner.save(unrun_status, {"terminal_abstention": False, "location": "retained-native-scan"})
    original_status = unrun_status.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("finalizing missing results must not call a model or usage endpoint")

    monkeypatch.setattr(subprocess, "run", forbidden)
    runner.missing_abstentions(keys, {"done": 8})
    privacy_status = runner.CAMPAIGN / f'{keys[1]["opaque_id"]}-status.json'
    original_privacy_status = privacy_status.read_bytes()
    runner.missing_abstentions(keys, {"done": 8})
    records = runner.jsonl(runner.STATE / "results-v1.jsonl")
    assert len(records) == 7 and runner.scores(records) == {"done": 8}
    assert all(row["value"] is None and row["flag"] is None for row in records[1:])
    assert all(row["evidence_steps"] == [] for row in records[1:])
    assert unrun_status.read_bytes() == original_status
    assert privacy_status.read_bytes() == original_privacy_status
    assert json.loads(original_privacy_status)["terminal_abstention"] is True
    assert not runner.SPEND.exists()


def test_live_quota_and_run_deny_before_metadata_retry_or_generation(
    monkeypatch, tmp_path, kimi_quota_report, sealed_route_guard,
):
    runner = load_runner(monkeypatch, tmp_path)
    runner.append(runner.RECEIPTS / "quota.jsonl", [{"available": True, "report": kimi_quota_report}])
    for path in (
        runner.SPEND, runner.STATE / "spend.jsonl", runner.RECEIPTS / "quota-errors.jsonl",
    ):
        runner.append(path, [{"historical": True, "usd": 0}])
    before = filesystem_snapshot(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("sealed runner reached preparation, quota, collection or spend handling")

    live_quota = runner.quota
    for name in ("prepare", "quota", "collect", "flush_spend"):
        monkeypatch.setattr(runner, name, forbidden)
    with sealed_route_guard():
        for _ in range(3):
            with pytest.raises(omp_model.OmpTransportSealedError, match="sealed"):
                live_quota()
            with pytest.raises(omp_model.OmpTransportSealedError, match="sealed"):
                runner.run([], 200)
    assert filesystem_snapshot(tmp_path) == before


BUDGET_PATH = (
    Path(__file__).resolve().parents[1]
    / "research/evidence/runs/trace-tools-tt4-20261027/openrouter_budget.py"
)
BUDGET_NOW = datetime(2026, 10, 28, 16, 0, 0, tzinfo=UTC)
MISSING = object()
BUDGET_WORKER = """
import importlib.util, json, sys
from datetime import UTC, datetime
path, state, worker, count = sys.argv[1:5]
spec = importlib.util.spec_from_file_location("budget_worker", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
now = datetime(2026, 10, 28, 16, 0, 0, tzinfo=UTC)
cash = lambda: {"available_usd": "1000.00", "fetched_at": now.isoformat(), "source": "fixture-credits"}
budget = module.Budget(state, "tt4", clock=lambda: now)
codes = []
for index in range(int(count)):
    try:
        budget.reserve(f"proc-{worker}-{index}", "1.00", cash)
        codes.append("ok")
    except module.BudgetError as error:
        codes.append(error.code)
print(json.dumps(codes))
"""


@pytest.fixture
def budget_module():
    spec = importlib.util.spec_from_file_location("tt4_openrouter_budget_fixture", BUDGET_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def budget_dir(tmp_path):
    return tmp_path / "openrouter-budget"


@pytest.fixture
def make_budget(budget_module, budget_dir):
    def make(arm="tt4", now=BUDGET_NOW):
        return budget_module.Budget(budget_dir, arm, clock=lambda: now)

    return make


def cash_reading(**changes):
    reading = {
        "available_usd": "1000.00",
        "fetched_at": BUDGET_NOW.isoformat(),
        "source": "fixture-credits",
    }
    for key, value in changes.items():
        if value is MISSING:
            del reading[key]
        else:
            reading[key] = value
    return reading


def budget_cash(amount, *, age=0.0):
    def fetch():
        fetch.calls += 1
        return cash_reading(
            available_usd=amount, fetched_at=(BUDGET_NOW - timedelta(seconds=age)).isoformat()
        )

    fetch.calls = 0
    return fetch


def refused(module, code, call, *args, **kwargs):
    with pytest.raises(module.BudgetError) as caught:
        call(*args, **kwargs)
    assert caught.value.code == code


def usd(text):
    return Decimal(text)


def value_id(value):
    return f"{type(value).__name__}:{str(value)[:16]}"


def test_cash_is_fetched_under_the_state_lock_and_the_hold_is_durable_before_return(
    budget_module,
    make_budget,
    budget_dir,
):
    seen = []

    def fetch():
        descriptor = os.open(budget_dir / "state.lock", os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            seen.append("locked")
        else:
            seen.append("unlocked")
        finally:
            os.close(descriptor)
        return cash_reading()

    reservation = make_budget().reserve("durable-1", "0.11", fetch, {"stage": "smoke"})
    assert seen == ["locked"]
    assert reservation.bound_usd == usd("0.11")
    assert reservation.required_cash_usd == usd("35.11")  # bound + unspent TT7 earmark
    assert json.loads(json.dumps(reservation.to_dict()))["attempt_id"] == "durable-1"
    snapshot = make_budget().snapshot()  # a new process-equivalent view, no shared memory
    assert [(a["attempt_id"], a["status"], a["charge_usd"]) for a in snapshot["attempts"]] == [
        ("durable-1", "pending", "0.11"),
    ]
    assert set(snapshot["attempts"][0]) == {
        "attempt_id",
        "arm",
        "status",
        "bound_usd",
        "actual_usd",
        "charge_usd",
        "created_at",
        "updated_at",
    }
    assert "stage" not in json.dumps(snapshot, allow_nan=False)
    state = json.loads((budget_dir / "state.json").read_text())
    assert state["attempts"]["durable-1"]["metadata"] == {"stage": "smoke"}


def test_state_replacement_is_fsync_then_replace_then_directory_fsync(
    budget_module,
    make_budget,
    budget_dir,
    monkeypatch,
):
    calls = []
    real_fsync, real_replace = os.fsync, os.replace
    lock_path = budget_dir / "state.lock"
    markers_at_replace = []

    def fsync(descriptor):
        is_lock = os.fstat(descriptor).st_ino == lock_path.stat().st_ino
        calls.append("lock_fsync" if is_lock else "fsync")
        if is_lock:
            assert (budget_dir / "state.json").exists()
            assert lock_path.read_bytes()
        real_fsync(descriptor)

    def replace(source, destination):
        calls.append("replace")
        markers_at_replace.append(lock_path.read_bytes())
        real_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(budget_module.os, "fsync", fsync)
        patch.setattr(budget_module.os, "replace", replace)
        make_budget().reserve("order-1", "0.11", budget_cash("1000.00"))
        assert calls == ["fsync", "replace", "fsync", "lock_fsync"]
        marker = lock_path.read_bytes()
        assert markers_at_replace == [b""]  # no initialized marker before the first commit
        calls.clear()
        make_budget().reserve("order-2", "0.11", budget_cash("1000.00"))
        assert calls == ["fsync", "replace", "fsync", "lock_fsync"]
        assert markers_at_replace == [b"", marker]
        assert lock_path.read_bytes() == marker  # never append or replace the stable marker


def test_failed_state_write_returns_no_reservation_and_consumes_no_attempt_id(
    budget_module,
    make_budget,
    budget_dir,
    monkeypatch,
):
    def boom(*args):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(budget_module.os, "replace", boom)
        refused(
            budget_module,
            "state_write_failed",
            make_budget().reserve,
            "write-1",
            "0.11",
            budget_cash("1000.00"),
        )
    assert not (budget_dir / "state.json").exists()
    assert (budget_dir / "state.lock").read_bytes() == b""
    assert list(budget_dir.glob("*.tmp")) == []
    assert make_budget().reserve("write-1", "0.11", budget_cash("1000.00")).attempt_id == "write-1"


def test_initialized_lock_refuses_deleted_state_for_every_operation(
    budget_module,
    make_budget,
    budget_dir,
):
    cash = budget_cash("1000.00")
    make_budget().reserve("deleted-state-1", "0.11", cash)
    lock_path = budget_dir / "state.lock"
    lock_inode = lock_path.stat().st_ino
    marker = lock_path.read_bytes()
    assert marker
    (budget_dir / "state.json").unlink()
    for arm in ("tt4", "tt7"):
        budget = make_budget(arm)  # no instance-local memory of the original reservation
        for call, args in (
            (budget.snapshot, ()),
            (budget.reserve, ("deleted-state-2", "0.11", cash)),
            (budget.settle, ("deleted-state-1", "0.01", "generation:deleted-state")),
            (budget.fail, ("deleted-state-1", "unknown outcome")),
            (budget.close_arm, ("stop after missing state",)),
        ):
            refused(budget_module, "missing_state", call, *args)
            assert not (budget_dir / "state.json").exists()
    assert cash.calls == 1  # never fetch cash or make a new dispatch reservation
    assert lock_path.stat().st_ino == lock_inode
    assert lock_path.read_bytes() == marker


@pytest.mark.parametrize("preexisting_lock", [False, True])
def test_blank_lock_remains_uninitialized_until_the_first_successful_state_commit(
    budget_module,
    make_budget,
    budget_dir,
    preexisting_lock,
):
    lock_path = budget_dir / "state.lock"
    if preexisting_lock:
        budget_dir.mkdir()
        lock_path.write_bytes(b"")
    refused(
        budget_module,
        "cash_low",
        make_budget().reserve,
        "first-success",
        "0.11",
        budget_cash("35.10"),
    )
    lock_inode = lock_path.stat().st_ino
    assert lock_path.read_bytes() == b""
    assert not (budget_dir / "state.json").exists()
    for arm in ("tt4", "tt7"):
        snapshot = make_budget(arm).snapshot()
        assert snapshot["attempts"] == []
        assert usd(snapshot["arms"]["tt4"]["used_usd"]) == 0
        assert lock_path.read_bytes() == b""
        assert not (budget_dir / "state.json").exists()
    reservation = make_budget().reserve("first-success", "0.11", budget_cash("1000.00"))
    assert reservation.attempt_id == "first-success"
    assert lock_path.read_bytes()
    assert lock_path.stat().st_ino == lock_inode
    assert make_budget().snapshot()["attempts"][0]["status"] == "pending"


def test_threaded_reservations_never_exceed_the_arm_cap(budget_module, budget_dir):
    def reserve(index):
        budget = budget_module.Budget(budget_dir, "tt4", clock=lambda: BUDGET_NOW)
        try:
            budget.reserve(f"thread-{index}", "1.00", budget_cash("1000.00"))
        except budget_module.BudgetError as error:
            return error.code
        return "ok"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(reserve, range(24)))
    assert outcomes.count("ok") == 14
    assert outcomes.count("arm_cap") == 10
    arm = budget_module.Budget(budget_dir, "tt4", clock=lambda: BUDGET_NOW).snapshot()["arms"][
        "tt4"
    ]
    assert usd(arm["used_usd"]) == usd("14.00") and arm["attempts"]["pending"] == 14


def test_separate_processes_share_one_cap(budget_module, budget_dir):
    workers = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                BUDGET_WORKER,
                str(BUDGET_PATH),
                str(budget_dir),
                str(number),
                "5",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for number in range(4)
    ]
    codes = []
    for worker in workers:
        stdout, stderr = worker.communicate(timeout=120)
        assert worker.returncode == 0, stderr
        codes.extend(json.loads(stdout))
    assert codes.count("ok") == 14
    assert codes.count("arm_cap") == 6
    snapshot = budget_module.Budget(budget_dir, "tt4", clock=lambda: BUDGET_NOW).snapshot()
    assert usd(snapshot["arms"]["tt4"]["used_usd"]) == usd("14.00")
    assert len({attempt["attempt_id"] for attempt in snapshot["attempts"]}) == 14


def test_pending_and_failed_attempts_retain_their_whole_bound(budget_module, make_budget):
    budget = make_budget()
    cash = budget_cash("1000.00")
    budget.reserve("hold-pending", "0.11", cash)
    budget.reserve("hold-failed", "0.11", cash)
    failed = budget.fail("hold-failed", RuntimeError("HTTP 504 after the request was sent"))
    assert failed["status"] == "failed" and usd(failed["retained_usd"]) == usd("0.11")
    arm = budget.snapshot()["arms"]["tt4"]
    assert (
        usd(arm["held_usd"]) == usd("0.22")
        and usd(arm["settled_usd"]) == 0
        and usd(arm["used_usd"]) == usd("0.22")
    )
    assert arm["attempts"] == {"pending": 1, "settled": 0, "failed": 1}
    refused(
        budget_module, "bad_transition", budget.settle, "hold-failed", "0.01", "generation:gen-1"
    )
    refused(budget_module, "bad_transition", budget.fail, "hold-failed", "again")
    refused(budget_module, "duplicate_attempt", budget.reserve, "hold-failed", "0.11", cash)
    assert usd(make_budget().snapshot()["arms"]["tt4"]["used_usd"]) == usd("0.22")


def test_verified_actual_cost_releases_only_the_unused_remainder(budget_module, make_budget):
    budget = make_budget()
    cash = budget_cash("1000.00")
    budget.reserve("whole-cap", "14.85", cash)
    refused(budget_module, "arm_cap", budget.reserve, "no-room", "0.01", cash)
    settled = budget.settle(
        "whole-cap", "14.00", {"generation_id": "gen-1", "usage": {"cost": 14.0}}
    )
    assert usd(settled["released_usd"]) == usd("0.85")
    assert usd(settled["arm_used_usd"]) == usd("14.00") and usd(
        settled["arm_remaining_usd"]
    ) == usd("0.85")
    assert budget.reserve("room", "0.85", cash).arm_remaining_usd == 0
    refused(budget_module, "arm_cap", budget.reserve, "too-much", "0.01", cash)
    budget_free = make_budget()
    refused(budget_module, "arm_cap", budget_free.reserve, "still-full", "0.01", cash)


def test_zero_provider_cost_is_a_valid_settlement(make_budget):
    budget = make_budget()
    budget.reserve("free-1", "0.11", budget_cash("1000.00"))
    settled = budget.settle("free-1", 0, "generation:gen-free")
    assert usd(settled["released_usd"]) == usd("0.11") and usd(settled["arm_used_usd"]) == 0


@pytest.mark.parametrize(
    "proof", ["", "   ", {}, None, 0, ["generation"], b"generation"], ids=value_id
)
def test_settlement_requires_nonempty_provider_proof(budget_module, make_budget, proof):
    budget = make_budget()
    budget.reserve("proof-1", "0.11", budget_cash("1000.00"))
    refused(budget_module, "invalid_proof", budget.settle, "proof-1", "0.05", proof)
    assert budget.snapshot()["attempts"][0]["status"] == "pending"


def test_over_bound_cost_fails_closed_without_releasing_the_hold(
    budget_module, make_budget, budget_dir
):
    budget = make_budget()
    budget.reserve("over-1", "0.11", budget_cash("1000.00"))
    refused(budget_module, "over_bound", budget.settle, "over-1", "0.12", "generation:gen-2")
    snapshot = budget.snapshot()
    attempt = snapshot["attempts"][0]
    assert (attempt["status"], attempt["actual_usd"], usd(attempt["charge_usd"])) == (
        "pending",
        None,
        usd("0.12"),
    )
    assert usd(snapshot["arms"]["tt4"]["used_usd"]) == usd("0.12")
    refused(budget_module, "bad_transition", budget.settle, "over-1", "0.01", "generation:gen-2")
    failed = budget.fail("over-1", "billed above the reservation")
    assert usd(failed["retained_usd"]) == usd("0.12")
    assert usd(budget.snapshot()["arms"]["tt4"]["used_usd"]) == usd("0.12")
    events = json.loads((budget_dir / "state.json").read_text())["attempts"]["over-1"]["history"]
    assert [event["event"] for event in events] == [
        "reserved",
        "settle_rejected_over_bound",
        "failed",
    ]


def test_duplicate_attempts_are_always_refused_without_fetching_cash(budget_module, make_budget):
    budget = make_budget()
    cash = budget_cash("1000.00")
    budget.reserve("dup-pending", "0.11", cash)
    budget.reserve("dup-settled", "0.11", cash)
    budget.settle("dup-settled", "0.05", "generation:gen-3")
    budget.reserve("dup-failed", "0.11", cash)
    budget.fail("dup-failed", "timeout")
    assert cash.calls == 3
    for attempt in ("dup-pending", "dup-settled", "dup-failed"):
        refused(budget_module, "duplicate_attempt", budget.reserve, attempt, "0.11", cash)
        refused(budget_module, "duplicate_attempt", make_budget().reserve, attempt, "0.01", cash)
    assert cash.calls == 3
    refused(
        budget_module, "bad_transition", budget.settle, "dup-settled", "0.05", "generation:gen-3"
    )
    refused(budget_module, "bad_transition", budget.fail, "dup-settled", "late error")
    assert len(budget.snapshot()["attempts"]) == 3


def test_settle_and_fail_are_scoped_to_known_attempts_of_the_same_arm(budget_module, make_budget):
    budget = make_budget()
    budget.reserve("scoped-1", "0.11", budget_cash("1000.00"))
    refused(
        budget_module, "unknown_attempt", budget.settle, "never-reserved", "0.01", "generation:g"
    )
    refused(budget_module, "unknown_attempt", budget.fail, "never-reserved", "error")
    refused(
        budget_module, "wrong_arm", make_budget("tt7").settle, "scoped-1", "0.01", "generation:g"
    )
    refused(budget_module, "wrong_arm", make_budget("tt7").fail, "scoped-1", "error")
    assert budget.snapshot()["attempts"][0]["status"] == "pending"


@pytest.mark.parametrize(
    "age,code",
    [
        (0.0, None),
        (15.0, None),
        (-2.0, None),
        (15.001, "cash_stale"),
        (3600.0, "cash_stale"),
        (-2.001, "cash_future"),
        (-3600.0, "cash_future"),
    ],
)
def test_cash_must_be_at_most_15s_old_and_not_more_than_2s_in_the_future(
    budget_module,
    make_budget,
    budget_dir,
    age,
    code,
):
    budget = make_budget()
    cash = budget_cash("1000.00", age=age)
    if code is None:
        assert budget.reserve("age-1", "0.11", cash).attempt_id == "age-1"
    else:
        refused(budget_module, code, budget.reserve, "age-1", "0.11", cash)
        assert not (budget_dir / "state.json").exists()


@pytest.mark.parametrize(
    "reading",
    [
        "not-a-mapping",
        None,
        [],
        cash_reading(available_usd=1000.0),
        cash_reading(available_usd=Decimal("1000")),
        cash_reading(available_usd=True),
        cash_reading(available_usd=None),
        cash_reading(available_usd="-1"),
        cash_reading(available_usd="nan"),
        cash_reading(available_usd="inf"),
        cash_reading(available_usd="1e3"),
        cash_reading(available_usd=MISSING),
        cash_reading(fetched_at="2026-10-28T16:00:00"),
        cash_reading(fetched_at="2026-10-28T17:00:00+01:00"),
        cash_reading(fetched_at="yesterday"),
        cash_reading(fetched_at=1.0),
        cash_reading(fetched_at=MISSING),
        cash_reading(source=""),
        cash_reading(source="   "),
        cash_reading(source=None),
        cash_reading(source=MISSING),
    ],
    ids=lambda reading: str(reading)[:48],
)
def test_malformed_cash_readings_fail_closed(budget_module, make_budget, budget_dir, reading):
    refused(
        budget_module, "cash_invalid", make_budget().reserve, "bad-cash", "0.11", lambda: reading
    )
    assert not (budget_dir / "state.json").exists()


def test_cash_fetch_failure_reserves_nothing(budget_module, make_budget, budget_dir):
    def boom():
        raise TimeoutError("credits endpoint timed out")

    refused(budget_module, "cash_unavailable", make_budget().reserve, "boom-1", "0.11", boom)
    assert not (budget_dir / "state.json").exists()


def test_tt7_is_reserved_but_denied_and_nothing_can_approve_it(
    budget_module, make_budget, budget_dir
):
    cash = budget_cash("1000.00")
    refused(budget_module, "arm_not_approved", make_budget("tt7").reserve, "tt7-1", "1.00", cash)
    assert cash.calls == 0 and not (budget_dir / "state.json").exists()
    snapshot = make_budget("tt7").snapshot()
    assert snapshot["caps"] == {"tt4": "14.85", "tt7": "35.00"}
    assert snapshot["arms"]["tt4"]["dispatch_approved"] is True
    assert snapshot["arms"]["tt7"]["dispatch_approved"] is False
    assert snapshot["attempts"] == []
    assert {name for name in vars(budget_module.Budget) if not name.startswith("_")} == {
        "reserve",
        "settle",
        "fail",
        "snapshot",
        "close_arm",
        "bound_failed",
    }
    for constants in (budget_module.CAPS, budget_module.DISPATCH_APPROVED):
        with pytest.raises(TypeError):
            constants["tt7"] = "35.00"
    for arm in ("tt5", None, 4):
        refused(budget_module, "unknown_arm", budget_module.Budget, budget_dir, arm)


def edit_budget_state(budget_module, budget_dir, edit, reseal):
    path = budget_dir / "state.json"
    state = json.loads(path.read_text())
    edit(state)
    if reseal:
        state["integrity"] = {"sha256": budget_module.state_digest(state)}
    path.write_text(json.dumps(state))


def raise_both_caps(state):
    state["caps"]["tt4"] = "99.00"
    state["arms"]["tt4"]["cap_usd"] = "99.00"


def raise_arm_cap(state):
    state["arms"]["tt4"]["cap_usd"] = "99.00"


def approve_tt7(state):
    state["arms"]["tt7"]["dispatch_approved"] = True


def lower_bound(state):
    state["attempts"]["tamper-1"]["bound_usd"] = "0.01"


def drop_attempt(state):
    del state["attempts"]["tamper-1"]


def forge_settlement(state):
    state["attempts"]["tamper-1"].update(status="settled", actual_usd="0")


def drop_history(state):
    state["attempts"]["tamper-1"]["history"] = []


@pytest.mark.parametrize(
    "edit,reseal,code",
    [
        (raise_both_caps, True, "tampered_caps"),
        (raise_both_caps, False, "tampered_caps"),
        (raise_arm_cap, True, "tampered_caps"),
        (approve_tt7, True, "tampered_approval"),
        (lower_bound, False, "tampered_state"),
        (drop_attempt, False, "tampered_state"),
        (forge_settlement, True, "tampered_state"),
        (drop_history, True, "tampered_state"),
    ],
    ids=lambda value: getattr(value, "__name__", str(value)),
)
def test_tampered_state_fails_every_operation_closed_and_is_never_repaired(
    budget_module,
    make_budget,
    budget_dir,
    edit,
    reseal,
    code,
):
    budget = make_budget()
    cash = budget_cash("1000.00")
    budget.reserve("tamper-1", "0.11", cash)
    edit_budget_state(budget_module, budget_dir, edit, reseal)
    tampered = (budget_dir / "state.json").read_bytes()
    fetches = cash.calls
    for call, args in (
        (budget.snapshot, ()),
        (make_budget("tt7").snapshot, ()),
        (budget.reserve, ("tamper-2", "0.11", cash)),
        (budget.settle, ("tamper-1", "0.01", "generation:g")),
        (budget.fail, ("tamper-1", "error")),
        (budget.close_arm, ("stop",)),
    ):
        refused(budget_module, code, call, *args)
    assert cash.calls == fetches
    assert (budget_dir / "state.json").read_bytes() == tampered


@pytest.mark.parametrize("text", ["{not json", '{"seq": NaN}', "[]", ""])
def test_corrupt_state_files_fail_closed(budget_module, make_budget, budget_dir, text):
    budget_dir.mkdir()
    (budget_dir / "state.json").write_text(text)
    refused(budget_module, "tampered_state", make_budget().snapshot)
    refused(
        budget_module,
        "tampered_state",
        make_budget().reserve,
        "corrupt-1",
        "0.11",
        budget_cash("1000.00"),
    )
    assert (budget_dir / "state.json").read_text() == text


def test_low_cash_and_other_open_arm_earmark_block_reservations(
    budget_module, make_budget, budget_dir
):
    budget = make_budget()
    # TT4 must leave TT7's whole unspent 35.00 earmark untouched: 0.11 + 35.00.
    refused(budget_module, "cash_low", budget.reserve, "low-1", "0.11", budget_cash("35.10"))
    assert not (budget_dir / "state.json").exists()
    assert budget.reserve("low-1", "0.11", budget_cash("35.11")).required_cash_usd == usd("35.11")
    # A refusal never consumed the id; an existing pending hold is subtracted from cash too.
    refused(budget_module, "cash_low", budget.reserve, "low-2", "0.11", budget_cash("35.21"))
    assert budget.reserve("low-2", "0.11", budget_cash("35.22")).required_cash_usd == usd("35.22")
    budget.fail("low-2", "timeout")  # a failed hold keeps counting against cash
    refused(budget_module, "cash_low", budget.reserve, "low-3", "0.11", budget_cash("35.32"))
    assert budget.reserve("low-3", "0.11", budget_cash("35.33")).required_cash_usd == usd("35.33")


def test_settled_debits_stay_subtracted_without_reflection_inference(budget_module, make_budget):
    budget = make_budget()
    budget.reserve("shadow-1", "0.11", budget_cash("1000.00"))
    budget.settle("shadow-1", "0.10", "generation:gen-4")
    # Even if the balance already fell by 0.10, the debit is shadowed: 0.11 + 0.10 + 35.00.
    refused(budget_module, "cash_low", budget.reserve, "shadow-2", "0.11", budget_cash("35.20"))
    assert budget.reserve("shadow-2", "0.11", budget_cash("35.21")).required_cash_usd == usd(
        "35.21"
    )
    budget.settle("shadow-2", "0.11", "generation:gen-5")
    refused(budget_module, "cash_low", budget.reserve, "shadow-3", "0.11", budget_cash("35.31"))
    assert budget.reserve("shadow-3", "0.11", budget_cash("35.32")).required_cash_usd == usd(
        "35.32"
    )


def test_closing_tt7_releases_only_its_unused_earmark(budget_module, make_budget):
    budget = make_budget()
    budget.reserve("close-hold", "0.11", budget_cash("1000.00"))
    budget.reserve("close-set", "0.11", budget_cash("1000.00"))
    budget.settle("close-set", "0.05", "generation:gen-6")
    before = budget.snapshot()
    assert usd(before["global"]["open_earmark_usd"]) == usd("49.69")  # 14.85 - 0.16 + 35.00
    closed = make_budget("tt7").close_arm("TT7 was never dispatched")
    assert usd(closed["released_earmark_usd"]) == usd("35.00") and usd(closed["used_usd"]) == 0
    after = budget.snapshot()
    assert usd(after["global"]["open_earmark_usd"]) == usd("14.69")
    for key in ("settled_usd", "held_usd", "used_usd"):
        assert usd(after["arms"]["tt4"][key]) == usd(before["arms"]["tt4"][key])
    # Cash guard now keeps only own holds and settled shadow: 0.11 + 0.11 + 0.05.
    refused(budget_module, "cash_low", budget.reserve, "after-close", "0.11", budget_cash("0.26"))
    assert budget.reserve("after-close", "0.11", budget_cash("0.27")).required_cash_usd == usd(
        "0.27"
    )
    refused(
        budget_module,
        "arm_not_approved",
        make_budget("tt7").reserve,
        "tt7-late",
        "1.00",
        budget_cash("1000.00"),
    )


def test_closing_an_arm_stops_dispatch_but_keeps_every_charge(budget_module, make_budget):
    budget = make_budget()
    cash = budget_cash("1000.00")
    budget.reserve("c-pending", "0.11", cash)
    budget.reserve("c-failed", "0.11", cash)
    budget.fail("c-failed", "unknown outcome")
    budget.reserve("c-settled", "0.11", cash)
    budget.settle("c-settled", "0.04", "generation:gen-7")
    before = budget.snapshot()["arms"]["tt4"]
    closed = budget.close_arm("TT4 complete")
    assert closed["already_closed"] is False and usd(closed["released_earmark_usd"]) == usd("14.59")
    after = budget.snapshot()["arms"]["tt4"]
    assert after["open"] is False and usd(after["earmark_usd"]) == 0
    for key in ("settled_usd", "held_usd", "used_usd"):
        assert usd(after[key]) == usd(before[key])
    assert usd(after["held_usd"]) == usd("0.22") and usd(after["settled_usd"]) == usd("0.04")
    refused(budget_module, "arm_closed", budget.reserve, "c-new", "0.11", cash)
    refused(budget_module, "invalid_reason", budget.close_arm, "  ")
    # In-flight work can still resolve; only its own remainder is released.
    assert usd(budget.settle("c-pending", "0.02", "generation:gen-8")["released_usd"]) == usd(
        "0.09"
    )
    later = make_budget(now=BUDGET_NOW + timedelta(hours=1)).close_arm("different reason")
    assert later["already_closed"] is True
    assert (later["closed_reason"], later["closed_at"]) == ("TT4 complete", closed["closed_at"])


def test_attempt_records_are_append_only_across_later_transitions(make_budget, budget_dir):
    path = budget_dir / "state.json"
    budget = make_budget()
    cash = budget_cash("1000.00")
    budget.reserve("history-1", "0.11", cash, {"stage": "smoke"})
    original = json.loads(path.read_text())["attempts"]["history-1"]
    budget.reserve("history-2", "0.11", cash)
    budget.settle("history-1", "0.05", "generation:gen-9")
    budget.fail("history-2", "boom")
    attempts = json.loads(path.read_text())["attempts"]
    assert set(attempts) == {"history-1", "history-2"}
    settled = attempts["history-1"]
    for key in ("arm", "bound_usd", "cash", "metadata", "created_at", "seq"):
        assert settled[key] == original[key]
    assert settled["history"][:1] == original["history"]
    assert [event["event"] for event in settled["history"]] == ["reserved", "settled"]
    assert [event["event"] for event in attempts["history-2"]["history"]] == ["reserved", "failed"]
    sequence = [event["seq"] for attempt in attempts.values() for event in attempt["history"]]
    assert len(set(sequence)) == len(sequence) == 4


BAD_AMOUNTS = [
    True,
    False,
    None,
    "",
    " 1",
    "1 ",
    "nan",
    "NaN",
    "inf",
    "Infinity",
    "-0.01",
    "-0",
    "1e3",
    "1_0",
    "0x1",
    "abc",
    "0.0000000000001",
    "12345678901",
    [],
    {},
    SimpleNamespace(),
    float("nan"),
    float("inf"),
    float("-inf"),
    -1,
    -0.5,
    Decimal("NaN"),
    Decimal("Infinity"),
    Decimal("-1"),
    Decimal("-0"),
]


@pytest.mark.parametrize("value", BAD_AMOUNTS + ["0", 0, Decimal(0)], ids=value_id)
def test_reservation_bound_must_be_a_finite_positive_decimal(
    budget_module, make_budget, budget_dir, value
):
    cash = budget_cash("1000.00")
    refused(budget_module, "invalid_decimal", make_budget().reserve, "bad-bound", value, cash)
    assert cash.calls == 0 and not (budget_dir / "state.json").exists()


@pytest.mark.parametrize("value", BAD_AMOUNTS, ids=value_id)
def test_settled_actual_must_be_a_finite_nonnegative_decimal(budget_module, make_budget, value):
    budget = make_budget()
    budget.reserve("bad-actual", "0.11", budget_cash("1000.00"))
    refused(budget_module, "invalid_decimal", budget.settle, "bad-actual", value, "generation:g")
    assert budget.snapshot()["attempts"][0]["status"] == "pending"


@pytest.mark.parametrize("value", ["0.11", Decimal("0.11"), 0.11], ids=value_id)
def test_equivalent_decimal_inputs_reserve_the_same_bound(make_budget, value):
    assert make_budget().reserve("form-1", value, budget_cash("1000.00")).bound_usd == usd("0.11")


@pytest.mark.parametrize(
    "attempt_id",
    ["", " a", "a b", "../x", "a/b", "-lead", "x" * 129, None, 7, b"a"],
    ids=value_id,
)
def test_attempt_ids_must_be_safe_stable_strings(
    budget_module, make_budget, budget_dir, attempt_id
):
    budget = make_budget()
    cash = budget_cash("1000.00")
    refused(budget_module, "invalid_attempt_id", budget.reserve, attempt_id, "0.11", cash)
    refused(budget_module, "invalid_attempt_id", budget.settle, attempt_id, "0.11", "generation:g")
    refused(budget_module, "invalid_attempt_id", budget.fail, attempt_id, "error")
    assert cash.calls == 0 and not (budget_dir / "state.json").exists()


@pytest.mark.parametrize(
    "metadata",
    [
        "text",
        ["x"],
        {1: "x"},
        {"nan": float("nan")},
        {"big": "x" * 5000},
        {"object": SimpleNamespace()},
    ],
    ids=value_id,
)
def test_metadata_must_be_small_finite_json(budget_module, make_budget, metadata):
    refused(
        budget_module,
        "invalid_metadata",
        make_budget().reserve,
        "meta-1",
        "0.11",
        budget_cash("1000.00"),
        metadata,
    )
