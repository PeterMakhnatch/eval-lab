"""Subscription transport identity, data-loss and concurrency boundaries (offline)."""

import asyncio
import importlib.util
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

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


def test_transport_uses_isolated_cm9_flags_and_writes_only_attestation(monkeypatch, tmp_path):
    def run(argv, **kwargs):
        assert argv[:5] == ["omp", "-p", "--mode", "json", "--no-session"]
        for flag in ("--no-tools", "--no-lsp", "--no-extensions", "--no-skills", "--no-rules", "--no-title", "--no-pty"):
            assert flag in argv
        assert argv[argv.index("--model") + 1] == omp_model.KIMI_MODEL
        assert argv[argv.index("--system-prompt") + 1] == "scanner system"
        overlay = Path(argv[argv.index("--config") + 1])
        assert overlay.read_text() == omp_model.NO_FALLBACK_OVERLAY
        assert overlay.parent == Path(kwargs["cwd"])
        assert kwargs["env"] == {"HOME": "/fixture/home"}
        assert kwargs["input"] == "private transcript"
        return SimpleNamespace(returncode=0, stdout=stream())

    monkeypatch.setattr(omp_model.os, "environ", {"HOME": "/fixture/home", "ZAI_API_KEY": "fixture"})
    monkeypatch.setattr(omp_model.subprocess, "run", run)
    path = tmp_path / "calls.jsonl"
    reply = omp_model.call_omp("scanner system", "private transcript", receipt_path=path)
    receipt = json.loads(path.read_text())
    assert receipt["ok"] and receipt["served_model"] == reply.served_model
    assert receipt["usd"] == 0 and receipt["fallback"] is False
    assert "private transcript" not in path.read_text()
    assert "scanner system" not in path.read_text()


def test_failed_child_does_not_parse_success_shaped_stdout(monkeypatch, tmp_path):
    monkeypatch.setattr(omp_model.os, "environ", {})
    monkeypatch.setattr(omp_model.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=stream()))
    path = tmp_path / "calls.jsonl"
    with pytest.raises(omp_model.OmpRouteError, match="status 1"):
        omp_model.call_omp("system", "user", receipt_path=path)
    assert json.loads(path.read_text())["ok"] is False


def test_process_bound_is_shared_across_concurrent_calls(monkeypatch):
    lock = threading.Lock()
    four_started = threading.Event()
    release = threading.Event()
    active = maximum = 0

    def run(*args, **kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            if active == 4:
                four_started.set()
        assert release.wait(10)
        with lock:
            active -= 1
        return SimpleNamespace(returncode=0, stdout=stream())

    monkeypatch.setattr(omp_model.os, "environ", {})
    monkeypatch.setattr(omp_model.subprocess, "run", run)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(omp_model.call_omp, "system", "user") for _ in range(8)]
        try:
            assert four_started.wait(10)
            assert maximum == 4
        finally:
            release.set()
        assert len([future.result() for future in futures]) == 8
    assert maximum == 4


def test_unqualified_routes_are_rejected_before_execution():
    with pytest.raises(ValueError, match="explicit provider/model"):
        omp_model.call_omp("system", "user", model="ambiguous-model")

@pytest.mark.parametrize("halt_while_queued", [False, True])
def test_quota_halt_prevents_even_previously_queued_subprocesses(monkeypatch, tmp_path, halt_while_queued):
    halt = tmp_path / "halt.json"

    class Slot:
        def __enter__(self):
            if halt_while_queued:
                halt.write_text("{}")

        def __exit__(self, *args):
            return False

    if not halt_while_queued:
        halt.write_text("{}")
    monkeypatch.setattr(omp_model, "_PROCESS_SLOTS", Slot())
    monkeypatch.setattr(omp_model.subprocess, "run", lambda *args, **kwargs: pytest.fail("halted subprocess started"))
    receipt = tmp_path / "calls.jsonl"
    with pytest.raises(omp_model.OmpRouteError, match="halted before subprocess"):
        omp_model.call_omp("system", "user", halt_path=halt, receipt_path=receipt)
    assert not receipt.exists()  # No hosted call was made.


def test_generic_route_attestation_includes_provider_and_full_model_id():
    event = end_event()
    event["messages"][0].update(provider="xai-oauth", model="grok-4.3")
    reply = omp_model.parse_omp_stream(stream(event), "xai-oauth/grok-4.3")
    assert reply.served_model == "xai-oauth/grok-4.3"
    with pytest.raises(omp_model.OmpRouteError, match="served-model mismatch"):
        omp_model.parse_omp_stream(stream(event), "kimi-code/k3.1-pro-highspeed")



def test_inspect_provider_schema_and_usage_without_model_call(monkeypatch):
    inspect_model = pytest.importorskip("inspect_ai.model")
    captured = {}

    def call(system, prompt, **kwargs):
        captured.update(system=system, prompt=prompt, **kwargs)
        return omp_model.parse_omp_stream(stream())

    monkeypatch.setattr(omp_model, "call_omp", call)
    assert omp_model.register_omp_provider() is omp_model.register_omp_provider()
    model = inspect_model.get_model(omp_model.INSPECT_MODEL)
    result = asyncio.run(model.api.generate(
        [inspect_model.ChatMessageUser(content="unchanged scanner prompt")], [], "none",
        inspect_model.GenerateConfig(response_schema=inspect_model.ResponseSchema(
            name="RewardHacking", json_schema={"type": "object", "properties": {"score": {"type": "number"}}},
        )),
    ))
    assert captured["prompt"] == "unchanged scanner prompt"
    assert json.loads(captured["system"].split("schema:\n", 1)[1])["type"] == "object"
    assert result.completion == omp_model.parse_omp_stream(stream()).text
    assert result.usage.total_tokens == 38
    assert result.usage.total_cost == 0
    assert result.metadata["served_model"] == omp_model.KIMI_MODEL


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
