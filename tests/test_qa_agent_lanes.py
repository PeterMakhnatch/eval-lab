"""Behavior of the Task QA model lanes: one conversation, honest accounting, no leaks.

The OMP lane runs against a deterministic stand-in executable that prints the
documented ``--mode json`` events and writes a session file; the OpenAI lane runs
against a scripted transport behind the real driver, ledger and budget.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evallab.gepa_optimizer.codex_transport import CodexTransportError
from evallab.interpretation.monitor_agent import InvestigatorError, ModelCompletion
from evallab.qa import agent_lanes as lanes
from evallab.qa.agent_lanes import DriverError, OpenAIDriver

SESSION_ID = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0001"
OMP_MODEL = "anthropic/claude-sonnet-5-5"
GPT_MODEL = "reviewer-model-1"
KEY = "sk-test-secret-0123456789abcdef"

FAKE_OMP = r'''
import json, os, pathlib, sys

here = pathlib.Path(__file__).resolve().parent
argv = sys.argv[1:]
if argv == ["--version"]:
    print("omp 0.0.0-fixture")
    sys.exit(0)
scenario = json.loads((here / "scenario.json").read_text())
log = here / "calls.jsonl"


def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None


prompt = argv[argv.index("--") + 1] if "--" in argv else sys.stdin.read()
with log.open("a") as handle:
    handle.write(json.dumps({"argv": argv, "prompt": prompt, "env": sorted(os.environ)}) + "\n")
call = sum(1 for _ in log.open())
turn = scenario["turns"][call - 1]
session_dir = pathlib.Path(opt("--session-dir"))
resume = opt("--resume")
header_id = scenario["session_id"] if resume is None else resume
if turn.get("new_session_on_resume") and resume is not None:
    header_id = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0bad"
files = sorted(session_dir.glob("*_" + header_id + ".jsonl"))
if files:
    path = files[0]
else:
    path = session_dir / ("2026-10-21T00-00-00_" + header_id + ".jsonl")
    with path.open("w") as handle:
        handle.write(json.dumps({"type": "title", "title": ""}).ljust(255) + "\n")
        handle.write(json.dumps({"type": "session", "version": 3, "id": header_id}) + "\n")
        if turn.get("native_digest_only"):
            handle.write(json.dumps({"type": "custom", "customType": "system-prompt-digest",
                                     "data": "c621c58185caa6c4"}) + "\n")
        else:
            init = {"type": "session_init", "systemPrompt": [turn.get("system_prompt", "Reviewer.")]}
            handle.write(json.dumps(init) + "\n")
assistant = {
    "role": "assistant", "provider": "anthropic", "model": "claude-sonnet-5-5",
    "stopReason": "stop", "content": [{"type": "text", "text": turn["text"]}],
    "usage": {"input": 10, "output": 5, "cacheRead": 100, "cacheWrite": 0,
              "totalTokens": 115, "cost": {"total": 0.01}},
    "requestControls": {"tools": {"declared": [], "active": [], "deferred": []}},
}
assistant.update(turn.get("assistant", {}))
echo = turn.get("user_echo", prompt)
events = [
    {"type": "session", "version": 3, "id": header_id},
    {"type": "agent_start"},
    {"type": "message_end", "message": {"role": "user", "content": [{"type": "text", "text": echo}]}},
    *turn.get("extra_events", []),
    {"type": "message_end", "message": assistant},
    {"type": "agent_end", "messages": [assistant], "isTerminal": True},
]
with path.open("a") as handle:
    for message in ({"role": "user", "content": prompt}, assistant):
        handle.write(json.dumps({"type": "message", "message": message}) + "\n")
for event in events:
    print(json.dumps(event))
if turn.get("stderr"):
    print(turn["stderr"], file=sys.stderr)
sys.exit(turn.get("exit", 0))
'''


@pytest.fixture
def fake_omp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "omp"
    script.write_text(f"#!{sys.executable}\n{FAKE_OMP}")
    script.chmod(0o755)
    monkeypatch.setattr(lanes, "OMP_COMMAND", (str(script),))
    monkeypatch.setattr(
        lanes, "_read_subscription",
        lambda timeout: {"availability": "observed", "weekly_used_percent": 1.0, "fetched_at": None},
    )

    def configure(*turns: dict[str, Any]) -> None:
        (bin_dir / "scenario.json").write_text(json.dumps({"session_id": SESSION_ID, "turns": turns}))

    def calls() -> list[dict[str, Any]]:
        log = bin_dir / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return SimpleNamespace(configure=configure, calls=calls)


def omp_driver(tmp_path: Path) -> Any:
    return lanes.make_driver(
        lane="omp", model=OMP_MODEL, directory=tmp_path / "agent" / "omp",
        repo_root=tmp_path, timeout_seconds=60,
    )


def saved_user_prompts(driver: Any) -> list[str]:
    prompts = []
    for line in Path(driver.facts["session_file"]).read_text().splitlines():
        entry = json.loads(line.strip() or "{}")
        message = entry.get("message") or {}
        if entry.get("type") == "message" and message.get("role") == "user":
            prompts.append(message["content"])
    return prompts


# -- omp -------------------------------------------------------------------------------


def test_omp_resumes_one_saved_conversation_and_reports_native_usage(tmp_path, fake_omp):
    fake_omp.configure({"text": "first"}, {"text": "second"})
    driver = omp_driver(tmp_path)

    first = driver.turn("first prompt", timeout_seconds=30)
    second = driver.turn("second prompt", timeout_seconds=30)

    assert first["thread_id"] == second["thread_id"] == driver.thread_id == SESSION_ID
    calls = fake_omp.calls()
    assert "--resume" not in calls[0]["argv"]
    assert calls[1]["argv"][calls[1]["argv"].index("--resume") + 1] == SESSION_ID
    assert saved_user_prompts(driver) == ["first prompt", "second prompt"]
    assert first["usage"] == {
        "input_tokens": 110, "cached_input_tokens": 100,
        "cache_write_input_tokens": 0, "output_tokens": 5,
    }
    assert second["cumulative_usage"]["input_tokens"] == 220
    assert second["actual_model"] == OMP_MODEL
    assert second["response"] == "second"
    assert driver.facts["thread_id_kind"] == "native_omp_session"


def test_omp_prompt_larger_than_argv_limit_arrives_intact(tmp_path, fake_omp):
    big = "x" * (lanes.OMP_ARGV_PROMPT_BYTES + 5000)
    fake_omp.configure({"text": "ok"})
    driver = omp_driver(tmp_path)

    reply = driver.turn(big, timeout_seconds=30)

    (call,) = fake_omp.calls()
    assert "--" not in call["argv"] and call["prompt"] == big
    assert reply["response"] == "ok"


def test_omp_refuses_a_different_served_model(tmp_path, fake_omp):
    fake_omp.configure({"text": "hi", "assistant": {"model": "claude-opus-5-5"}})
    driver = omp_driver(tmp_path)

    with pytest.raises(DriverError, match="claude-opus-5-5"):
        driver.turn("p", timeout_seconds=30)

    assert driver.thread_id == SESSION_ID
    assert (driver.directory / "turn-001" / "events.jsonl").read_text()
    assert driver.accounting()["usage_complete"] is False


@pytest.mark.parametrize(
    "turn",
    [
        {"text": "x", "extra_events": [{"type": "tool_execution_start", "toolName": "bash"}]},
        {"text": "x", "assistant": {"content": [
            {"type": "text", "text": "x"}, {"type": "toolCall", "name": "bash"}]}},
        {"text": "x", "assistant": {"stopReason": "toolUse"}},
        {"text": "x", "extra_events": [{"type": "auto_compaction_start"}]},
    ],
)
def test_omp_turn_with_native_tool_activity_fails_closed(tmp_path, fake_omp, turn):
    fake_omp.configure(turn)
    driver = omp_driver(tmp_path)

    with pytest.raises(DriverError):
        driver.turn("p", timeout_seconds=30)

@pytest.mark.parametrize("field", ["declared", "active", "deferred"])
def test_omp_refuses_ambient_capabilities_even_without_a_tool_call(tmp_path, fake_omp, field):
    tools = {"declared": [], "active": [], "deferred": []}
    tools[field] = ["mcp__outside_task"]
    fake_omp.configure({"text": "apparently ordinary", "assistant": {
        "requestControls": {"tools": tools},
    }})
    driver = omp_driver(tmp_path)
    with pytest.raises(DriverError, match="zero-tool"):
        driver.turn("ordinary source review", timeout_seconds=30)
    assert driver.accounting()["usage_complete"] is False


def test_omp_native_digest_does_not_masquerade_as_an_effective_prompt_snapshot(tmp_path, fake_omp):
    fake_omp.configure({"text": "answer", "native_digest_only": True})
    driver = omp_driver(tmp_path)
    driver.turn("ordinary source review", timeout_seconds=30)
    audit = driver.facts["prompt_audit"]
    assert audit["effective_text_observed"] is False
    assert audit["system_prompt_chars"] is None
    assert audit["system_prompt_sha256"] is None
    assert audit["native_base_prompt_digests"] == ["c621c58185caa6c4"]



@pytest.mark.parametrize(
    "usage",
    [
        None,
        {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        {"input": 10, "output": 5},
    ],
)
def test_omp_unreported_usage_stays_unknown(tmp_path, fake_omp, usage):
    fake_omp.configure({"text": "answer", "assistant": {"usage": usage}})
    driver = omp_driver(tmp_path)

    reply = driver.turn("p", timeout_seconds=30)

    assert reply["response"] == "answer"
    assert reply["usage_known"] is False
    assert set(reply["usage"].values()) == {None}
    assert driver.cumulative == {}
    assert driver.accounting()["usage_complete"] is False
    assert reply["api_equivalent_usd"] is None


def test_omp_refuses_a_resume_that_silently_starts_another_session(tmp_path, fake_omp):
    fake_omp.configure({"text": "one"}, {"text": "two", "new_session_on_resume": True})
    driver = omp_driver(tmp_path)
    driver.turn("p1", timeout_seconds=30)

    with pytest.raises(DriverError, match="exactly one"):
        driver.turn("p2", timeout_seconds=30)

    assert driver.thread_id == SESSION_ID
    assert driver.invocations == 2


def test_omp_unverified_subscription_refuses_before_any_run(tmp_path, fake_omp, monkeypatch):
    fake_omp.configure({"text": "never"})
    driver = omp_driver(tmp_path)

    def unverified(timeout):
        raise DriverError("OMP Anthropic subscription could not be verified")

    monkeypatch.setattr(lanes, "_read_subscription", unverified)

    with pytest.raises(DriverError, match="subscription"):
        driver.turn("p", timeout_seconds=30)

    assert fake_omp.calls() == []
    assert driver.invocations == 0


def test_omp_failed_exit_preserves_the_raw_evidence(tmp_path, fake_omp):
    fake_omp.configure({"text": "partial", "exit": 3, "stderr": "boom happened"})
    driver = omp_driver(tmp_path)

    with pytest.raises(DriverError, match="exited 3"):
        driver.turn("p", timeout_seconds=30)

    turn_dir = driver.directory / "turn-001"
    assert "boom happened" in (turn_dir / "client.log").read_text()
    assert (turn_dir / "events.jsonl").read_text()


def test_omp_child_never_receives_api_keys(tmp_path, fake_omp, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-must-not-reach-child")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-reach-child")
    fake_omp.configure({"text": "ok"})
    driver = omp_driver(tmp_path)

    driver.turn("p", timeout_seconds=30)

    (call,) = fake_omp.calls()
    assert "PATH" in call["env"]
    assert not {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"} & set(call["env"])


def test_omp_refuses_a_session_whose_prompt_carries_memory(tmp_path, fake_omp):
    fake_omp.configure({"text": "ok", "system_prompt": "Base.\n<memories>\n- private note\n</memories>"})
    driver = omp_driver(tmp_path)

    with pytest.raises(DriverError, match="not isolated"):
        driver.turn("p", timeout_seconds=30)


def test_omp_refuses_a_prompt_echo_that_differs(tmp_path, fake_omp):
    fake_omp.configure({"text": "ok", "user_echo": "something else"})
    driver = omp_driver(tmp_path)

    with pytest.raises(DriverError, match="verbatim"):
        driver.turn("the real prompt", timeout_seconds=30)


# -- openai-compatible -----------------------------------------------------------------


class ScriptedTransport:
    """Deterministic provider: returns or raises the next scripted outcome."""

    def __init__(self, *outcomes: ModelCompletion | Exception) -> None:
        self.model = GPT_MODEL
        self.timeout_seconds = 30.0
        self.outcomes = list(outcomes)
        self.seen: list[list[dict[str, str]]] = []

    def complete(self, messages, *, max_output_tokens, schema):
        self.seen.append([dict(m) for m in messages])
        outcome = self.outcomes.pop(0) if self.outcomes else ModelCompletion(content="{}")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def reply(text="{}", *, tokens=(100, 20), model=GPT_MODEL):
    return ModelCompletion(
        content=text, input_tokens=tokens[0], output_tokens=tokens[1],
        returned_model=model, request_id="req-1",
    )


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("LANE_TEST_KEY", KEY)


def openai_driver(tmp_path, transport, *, max_usd=5.0) -> OpenAIDriver:
    return OpenAIDriver(
        model=GPT_MODEL, directory=tmp_path / "agent" / "openai", timeout_seconds=30,
        max_usd=max_usd, endpoint="https://api.example.test/v1", api_key_env="LANE_TEST_KEY",
        input_usd_per_million=3.0, output_usd_per_million=15.0, transport=transport,
    )


def all_text(root: Path) -> str:
    return "\n".join(p.read_text(errors="replace") for p in root.rglob("*") if p.is_file())


def test_openai_resends_one_persisted_conversation(tmp_path, api_key):
    transport = ScriptedTransport(reply('{"a": 1}'), reply('{"b": 2}'))
    driver = openai_driver(tmp_path, transport)

    first = driver.turn("first", timeout_seconds=30)
    second = driver.turn("second", timeout_seconds=30)

    assert transport.seen[1] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": '{"a": 1}'},
        {"role": "user", "content": "second"},
    ]
    assert first["thread_id"] == second["thread_id"] == driver.thread_id
    assert driver.thread_id.startswith("local-conversation:")
    assert driver.facts["thread_id_kind"] == "local_conversation"
    assert second["cumulative_usage"] == {"input_tokens": 200, "output_tokens": 40}
    assert second["metered_usd"] == pytest.approx((100 * 3 + 20 * 15) / 1e6)


def test_openai_refuses_a_request_whose_reservation_exceeds_the_cap(tmp_path, api_key):
    transport = ScriptedTransport(reply())
    driver = openai_driver(tmp_path, transport, max_usd=0.01)

    with pytest.raises(DriverError, match="no request sent"):
        driver.turn("p", timeout_seconds=30)

    assert transport.seen == []
    assert driver.accounting()["requests_sent"] == 0


def test_openai_unreported_usage_cannot_authorize_unlimited_calls(tmp_path, api_key):
    cap = 0.25
    transport = ScriptedTransport(*[reply(tokens=(None, None)) for _ in range(20)])
    driver = openai_driver(tmp_path, transport, max_usd=cap)

    with pytest.raises(DriverError, match="no request sent"):
        for _ in range(20):
            result = driver.turn("p", timeout_seconds=30)
            assert result["usage_known"] is False and result["cost_known"] is False
            assert result["metered_usd"] is None

    accounting = driver.accounting()
    assert 0 < accounting["requests_sent"] < 20
    assert accounting["usage_complete"] is False and accounting["metered_usd"] is None
    assert 0 < accounting["metered_usd_upper_bound"] <= cap


@pytest.mark.parametrize("tokens", [(100, None), (None, 20), (0, 0)])
def test_openai_partial_or_zero_usage_is_unknown_not_zero(tmp_path, api_key, tokens):
    driver = openai_driver(tmp_path, ScriptedTransport(reply(tokens=tokens)))

    result = driver.turn("p", timeout_seconds=30)

    assert result["usage"] == {"input_tokens": None, "output_tokens": None}
    assert result["cost_known"] is False
    assert driver.cumulative == {}


def test_openai_tool_call_attempt_fails_and_counts_observed_billing(tmp_path, api_key):
    failure = InvestigatorError(
        "provider did not finish a plain assistant response",
        observed_usage=(120, 30), finish_reason="tool_calls",
    )
    driver = openai_driver(tmp_path, ScriptedTransport(failure))

    with pytest.raises(DriverError, match="tool call"):
        driver.turn("p", timeout_seconds=30)

    assert driver.cumulative == {"input_tokens": 120, "output_tokens": 30}
    assert driver.accounting()["metered_usd"] == pytest.approx((120 * 3 + 30 * 15) / 1e6)
    assert (driver.directory / "turn-001" / "error.json").exists()


def test_openai_provider_error_with_unknown_usage_stays_unknown_and_redacts_key(tmp_path, api_key):
    failure = InvestigatorError(
        "provider HTTP request rejected", http_status=429, provider_detail=f"invalid key {KEY}",
    )
    driver = openai_driver(tmp_path, ScriptedTransport(failure))

    with pytest.raises(DriverError, match="HTTP 429") as raised:
        driver.turn("p", timeout_seconds=30)

    assert KEY not in str(raised.value)
    assert KEY not in all_text(driver.directory)
    assert KEY not in json.dumps(driver.facts)
    assert driver.accounting()["usage_complete"] is False
    assert driver.accounting()["metered_usd"] is None


def test_openai_reply_from_a_different_model_is_discarded(tmp_path, api_key):
    transport = ScriptedTransport(reply(model="cheaper-fallback"), reply('{"ok": 1}'))
    driver = openai_driver(tmp_path, transport)

    with pytest.raises(DriverError, match="fallback is forbidden"):
        driver.turn("first", timeout_seconds=30)
    driver.turn("second", timeout_seconds=30)

    assert transport.seen[1] == [{"role": "user", "content": "second"}]
    assert driver.cumulative["input_tokens"] == 200  # the billed mismatched call still counts


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"endpoint": "https://user:pw@api.example.test/v1"}, "credentials"),
        ({"api_key_env": "sk-abc-123"}, "NAME"),
        ({"api_key_env": "LANE_TEST_MISSING"}, "LANE_TEST_MISSING is not set"),
        ({"max_usd": 0}, "positive metered"),
        ({"input_usd_per_million": None}, "prices"),
        ({"output_usd_per_million": 0}, "prices"),
        ({"endpoint": None}, "endpoint"),
    ],
)
def test_openai_requires_explicit_safe_metered_configuration(tmp_path, api_key, kwargs, message):
    settings: dict[str, Any] = {
        "lane": "openai", "model": GPT_MODEL, "directory": tmp_path / "d", "repo_root": tmp_path,
        "timeout_seconds": 30, "max_usd": 1.0, "endpoint": "https://api.example.test/v1",
        "api_key_env": "LANE_TEST_KEY", "input_usd_per_million": 3.0,
        "output_usd_per_million": 15.0, **kwargs,
    }

    with pytest.raises(DriverError, match=message):
        lanes.make_driver(**settings)

    assert not (tmp_path / "d").exists()


def test_openai_facts_name_the_key_variable_but_never_its_value(tmp_path, api_key):
    driver = lanes.make_driver(
        lane="openai", model=GPT_MODEL, directory=tmp_path / "d", repo_root=tmp_path,
        timeout_seconds=30, max_usd=1.0, endpoint="https://api.example.test/v1",
        api_key_env="LANE_TEST_KEY", input_usd_per_million=3.0, output_usd_per_million=15.0,
    )

    assert driver.facts["api_key_env"] == "LANE_TEST_KEY"
    assert driver.facts["endpoint"].endswith("/chat/completions")
    assert KEY not in json.dumps(driver.facts) + all_text(driver.directory)


# -- factory and codex ---------------------------------------------------------------


@pytest.mark.parametrize("lane", ["codex", "omp"])
def test_subscription_lanes_refuse_metered_settings(tmp_path, lane):
    with pytest.raises(DriverError, match="subscription-only"):
        lanes.make_driver(
            lane=lane, model=OMP_MODEL, directory=tmp_path / "d", repo_root=tmp_path,
            timeout_seconds=30, max_usd=1.0,
        )
    with pytest.raises(DriverError, match="subscription-only"):
        lanes.make_driver(
            lane=lane, model=OMP_MODEL, directory=tmp_path / "e", repo_root=tmp_path,
            timeout_seconds=30, endpoint="https://api.example.test/v1",
        )


def test_unknown_lane_and_unlisted_omp_model_are_refused(tmp_path):
    with pytest.raises(DriverError, match="unknown lane"):
        lanes.make_driver(
            lane="glm", model="m", directory=tmp_path / "d", repo_root=tmp_path,
            timeout_seconds=30,
        )
    with pytest.raises(DriverError, match="requires one of"):
        lanes.make_driver(
            lane="omp", model="anthropic/opus", directory=tmp_path / "e", repo_root=tmp_path,
            timeout_seconds=30,
        )


def test_codex_lane_maps_transport_failures_and_keeps_native_evidence(tmp_path, monkeypatch):
    driver = lanes.make_driver(
        lane="codex", model="codex/gpt-5.5", directory=tmp_path / "codex", repo_root=tmp_path,
        timeout_seconds=30,
    )
    native = {
        "response": "r", "invocation": 1, "thread_id": "t", "directory": "d",
        "usage": {"input_tokens": 5, "output_tokens": 2}, "cumulative_usage": {},
        "text_selection": None,
    }
    monkeypatch.setattr(driver._session, "turn", lambda prompt, timeout_seconds: native)

    ok = driver.turn("p", timeout_seconds=30)

    assert ok["response"] == "r" and ok["usage"] == native["usage"]
    assert ok["metered_usd"] == 0.0 and ok["cost_basis"] == "chatgpt_subscription"
    assert driver.accounting()["usage_complete"] is True

    def fail(prompt, timeout_seconds):
        driver._session.invocations = 2
        raise CodexTransportError("Codex output limit reached")

    monkeypatch.setattr(driver._session, "turn", fail)
    with pytest.raises(DriverError, match="output limit"):
        driver.turn("p", timeout_seconds=30)

    assert driver.invocations == 2
    assert driver.accounting()["usage_complete"] is False
