"""Strict Codex transcript parsing and subscription-route wiring.

Every test here is deterministic and hermetic: the real CLI and the host
Keychain are never touched. CLI-boundary probes ``_RUN``, ``_POPEN`` and
``_SEED_POPEN`` are stubbed per test.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from evallab.gepa_optimizer import codex_transport
from evallab.gepa_optimizer.codex_transport import (
    CodexTransport,
    CodexTransportError,
    _poll_events,
    parse_transcript,
)


def _event(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _agent_completed(text: str, item_id: str = "msg-1") -> dict:
    return {
        "type": "item.completed",
        "item": {"id": item_id, "type": "agent_message", "text": text},
    }


def _usage(**overrides: int) -> dict:
    base = {
        "input_tokens": 120,
        "cached_input_tokens": 40,
        "cache_write_input_tokens": 0,
        "output_tokens": 60,
        "reasoning_output_tokens": 10,
    }
    base.update(overrides)
    return base


def _valid_lines(text: str = "revised instructions") -> list[str]:
    return [
        _event({"type": "thread.started", "thread_id": "thread-1"}),
        _event({"type": "turn.started"}),
        _event({"type": "item.started", "item": {"id": "msg-1", "type": "agent_message"}}),
        _event(_agent_completed(text)),
        _event({"type": "turn.completed", "usage": _usage()}),
    ]


def _write(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_valid_single_turn_returns_text_and_honest_usage(tmp_path):
    path = _write(tmp_path / "events.jsonl", _valid_lines())
    response, usage = parse_transcript(path)
    assert response == "revised instructions"
    assert usage == _usage()


def test_reconnected_single_turn_retains_final_text_and_usage(tmp_path):
    lines = _valid_lines()
    lines.insert(
        2,
        _event(
            {
                "type": "error",
                "message": "Reconnecting... 2/5 (unexpected status 503 Service Unavailable)",
            }
        ),
    )
    path = _write(tmp_path / "events.jsonl", lines)
    offset, pending, started = _poll_events(path, 0, b"")
    assert (offset, pending, started) == (path.stat().st_size, b"", True)
    response, usage = parse_transcript(path)
    assert response == "revised instructions"
    assert usage == _usage()


def test_reconnect_without_completion_cannot_yield_partial_text(tmp_path):
    lines = _valid_lines()[:-1]
    lines.insert(
        2,
        _event({"type": "error", "message": "Reconnecting... 2/5 (stream disconnected)"}),
    )
    path = _write(tmp_path / "events.jsonl", lines)
    with pytest.raises(CodexTransportError, match="complete single turn"):
        parse_transcript(path)


@pytest.mark.parametrize(
    "error",
    [
        {"type": "turn.failed", "error": {"message": "subscription refused"}},
        {"type": "error", "message": "fatal provider failure"},
        {"type": "error", "message": "Reconnecting... 2/5 (model rerouted: fallback)"},
    ],
)
def test_reconnect_does_not_hide_fatal_failure_or_model_reroute(tmp_path, error):
    lines = _valid_lines()
    lines.insert(
        2,
        _event({"type": "error", "message": "Reconnecting... 1/5 (stream disconnected)"}),
    )
    lines.insert(3, _event(error))
    path = _write(tmp_path / "events.jsonl", lines)
    with pytest.raises(CodexTransportError):
        _poll_events(path, 0, b"")
    with pytest.raises(CodexTransportError):
        parse_transcript(path)


def test_error_event_never_yields_text(tmp_path):
    path = _write(
        tmp_path / "events.jsonl",
        [_event({"type": "error", "message": "boom"})],
    )
    with pytest.raises(CodexTransportError):
        parse_transcript(path)


def test_failed_turn_with_provider_refusal_never_yields_text(tmp_path):
    path = _write(
        tmp_path / "events.jsonl",
        [
            _event({"type": "thread.started", "thread_id": "t"}),
            _event({"type": "turn.started"}),
            _event(
                {
                    "type": "turn.failed",
                    "error": {"message": "not entitled: subscription required"},
                }
            ),
        ],
    )
    with pytest.raises(CodexTransportError, match="subscription required"):
        parse_transcript(path)


def test_incomplete_or_empty_output_is_not_a_proposal(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text("", encoding="utf-8")
    with pytest.raises(CodexTransportError):
        parse_transcript(path)
    _write(path, _valid_lines()[:-1])  # no turn.completed
    with pytest.raises(CodexTransportError):
        parse_transcript(path)


def test_empty_agent_text_is_not_a_proposal(tmp_path):
    lines = _valid_lines("   ")
    path = _write(tmp_path / "events.jsonl", lines)
    with pytest.raises(CodexTransportError):
        parse_transcript(path)


@pytest.mark.parametrize(
    "item",
    [
        {"id": "x", "type": "command_execution"},
        {"id": "x", "type": "file_change"},
        {"id": "x", "type": "mcp_tool_call"},
        {"id": "x", "type": "collab_tool_call"},
        {"id": "x", "type": "web_search"},
        {"id": "x", "type": "todo_list"},
        {"id": "x", "type": "error", "message": "bad"},
    ],
)
def test_any_tool_item_rejects_the_turn(tmp_path, item):
    lines = [
        _event({"type": "thread.started", "thread_id": "t"}),
        _event({"type": "turn.started"}),
        _event({"type": "item.started", "item": item}),
        _event(_agent_completed("revised instructions")),
        _event({"type": "turn.completed", "usage": _usage()}),
    ]
    path = _write(tmp_path / "events.jsonl", lines)
    with pytest.raises(CodexTransportError, match="tool"):
        parse_transcript(path)


def _catalog_probe(tool: str, *, text: str, status: str = "completed") -> dict:
    return {
        "id": tool,
        "type": "mcp_tool_call",
        "server": "codex",
        "tool": tool,
        "arguments": {},
        "error": None,
        "status": status,
        "result": None
        if status != "completed"
        else {"content": [{"type": "text", "text": text}], "structured_content": None},
    }


@pytest.mark.parametrize("allow_preamble", [False, True])
@pytest.mark.parametrize(
    ("tool", "text", "status"),
    [
        ("list_mcp_resources", '{"resources":[]}', "in_progress"),
        ("list_mcp_resources", '{"resources":[]}', "completed"),
        ("list_mcp_resource_templates", '{"resourceTemplates":[]}', "completed"),
    ],
)
def test_empty_catalog_lookups_are_still_tool_calls(tmp_path, allow_preamble, tool, text, status):
    lines = _valid_lines()
    kind = "item.started" if status == "in_progress" else "item.completed"
    lines.insert(2, _event({"type": kind, "item": _catalog_probe(tool, text=text, status=status)}))
    path = _write(tmp_path / "events.jsonl", lines)
    with pytest.raises(CodexTransportError):
        parse_transcript(path, allow_preamble=allow_preamble)
    with pytest.raises(CodexTransportError):
        _poll_events(path, 0, b"")


def test_a_catalog_probe_that_returns_resources_still_rejects(tmp_path):
    lines = _valid_lines()
    lines.insert(
        2,
        _event(
            {
                "type": "item.completed",
                "item": _catalog_probe(
                    "list_mcp_resources", text='{"resources":[{"uri":"file://x"}]}'
                ),
            }
        ),
    )
    path = _write(tmp_path / "events.jsonl", lines)
    with pytest.raises(CodexTransportError, match="tool"):
        parse_transcript(path)


def test_more_than_one_completion_is_ambiguous(tmp_path):
    lines = _valid_lines() + [
        _event(_agent_completed("second", item_id="msg-2")),
        _event({"type": "turn.completed", "usage": _usage()}),
    ]
    path = _write(tmp_path / "events.jsonl", lines)
    with pytest.raises(CodexTransportError):
        parse_transcript(path)


def test_two_agent_messages_with_one_turn_is_ambiguous(tmp_path):
    lines = _valid_lines()
    lines.insert(3, _event(_agent_completed("extra", item_id="msg-2")))
    path = _write(tmp_path / "events.jsonl", lines)
    with pytest.raises(CodexTransportError):
        parse_transcript(path)


def test_preamble_mode_accepts_final_text_without_relaxing_turn_completion(tmp_path):
    lines = _valid_lines("final proposal")
    lines.insert(2, _event(_agent_completed("I will check the supplied context.", item_id="intro")))
    path = _write(tmp_path / "events.jsonl", lines)
    text, usage = parse_transcript(path, allow_preamble=True)
    assert text == "final proposal"
    assert usage["input_tokens"] == _usage()["input_tokens"]
    _write(path, lines[:-1])
    with pytest.raises(CodexTransportError):
        parse_transcript(path, allow_preamble=True)


@pytest.mark.parametrize(
    "bad_event",
    [
        {"type": "turn.failed", "error": {"message": "failure"}},
        {"type": "error", "message": "provider error"},
        {"type": "turn.started"},
        {"type": "item.started", "item": {"id": "tool", "type": "command_execution"}},
    ],
)
def test_preamble_mode_rejects_failed_or_tool_bearing_turns(tmp_path, bad_event):
    lines = _valid_lines("final proposal")
    lines.insert(2, _event(_agent_completed("preamble", item_id="intro")))
    lines.insert(3, _event(bad_event))
    path = _write(tmp_path / "events.jsonl", lines)
    with pytest.raises(CodexTransportError):
        parse_transcript(path, allow_preamble=True)


def test_unknown_event_or_item_shapes_fail_closed(tmp_path):
    path = _write(
        tmp_path / "events.jsonl",
        [_event({"type": "thread.started", "thread_id": "t"}), _event({"type": "frobnicate"})],
    )
    with pytest.raises(CodexTransportError, match="Unknown"):
        parse_transcript(path)
    lines = _valid_lines()
    lines.insert(3, _event({"type": "item.completed", "item": {"id": "z", "type": "teleport"}}))
    _write(path, lines)
    with pytest.raises(CodexTransportError, match="Unknown"):
        parse_transcript(path)


def test_oversize_transcript_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_transport, "MAX_TRANSCRIPT_BYTES", 16)
    path = _write(tmp_path / "events.jsonl", _valid_lines())
    assert path.stat().st_size > 16
    with pytest.raises(CodexTransportError, match="bound"):
        parse_transcript(path)


def test_invalid_or_missing_usage_is_rejected(tmp_path):
    lines = _valid_lines()
    lines[-1] = _event({"type": "turn.completed", "usage": {"input_tokens": -1}})
    path = _write(tmp_path / "events.jsonl", lines)
    with pytest.raises(CodexTransportError, match="usage"):
        parse_transcript(path)
    lines[-1] = _event({"type": "turn.completed"})
    _write(path, lines)
    with pytest.raises(CodexTransportError, match="usage"):
        parse_transcript(path)


def test_live_monitor_preserves_partial_utf8_and_fails_fast_on_tools(tmp_path):
    path = tmp_path / "events.jsonl"
    event = _event(_agent_completed("café")).encode()
    split = event.index("é".encode()) + 1
    path.write_bytes(event[:split])
    offset, pending, started = _poll_events(path, 0, b"")
    with path.open("ab") as stream:
        stream.write(event[split:] + b"\n")
    offset, pending, started = _poll_events(path, offset, pending, started)
    assert pending == b""
    with path.open("ab") as stream:
        stream.write(
            _event(
                {
                    "type": "item.started",
                    "item": {"id": "sh", "type": "command_execution"},
                }
            ).encode()
            + b"\n"
        )
    with pytest.raises(CodexTransportError, match="tool"):
        _poll_events(path, offset, pending, started)


class _FakeRun:
    """Stub for ``codex --version`` and ``codex login status`` probes."""

    def __init__(
        self,
        *,
        version_stdout="codex-cli 0.162.1\n",
        login_stdout="Logged in using ChatGPT\n",
        login_code=0,
    ):
        self.version_stdout = version_stdout
        self.login_stdout = login_stdout
        self.login_code = login_code
        self.argv_seen: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.argv_seen.append(list(argv))
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, self.version_stdout.encode(), b"")
        return subprocess.CompletedProcess(argv, self.login_code, self.login_stdout.encode(), b"")


@pytest.fixture
def cli_probes(tmp_path, monkeypatch):
    exe = tmp_path / "codex"
    exe.write_bytes(b'#!/bin/sh\nexec codex "$@"\n')
    monkeypatch.setattr(codex_transport, "CODEX_EXECUTABLE", exe)
    run = _FakeRun()
    monkeypatch.setattr(codex_transport, "_RUN", run)
    monkeypatch.setattr(
        codex_transport, "_SEED_POPEN", lambda *a, **k: pytest.fail("unstubbed native seed server")
    )
    return exe, run


def test_preflight_refuses_api_key_or_logged_out_state(cli_probes, monkeypatch):
    _, run = cli_probes
    transport = CodexTransport(repo_root=Path("/tmp"), model="codex/gpt-6.1-sol")
    run.login_stdout = "Logged in using API key\n"
    with pytest.raises(CodexTransportError, match="ChatGPT"):
        transport.preflight()
    run.login_stdout = ""
    run.login_code = 1
    with pytest.raises(CodexTransportError, match="ChatGPT"):
        transport.preflight()


def test_preflight_requires_version_probe_and_executable(cli_probes, monkeypatch):
    _, run = cli_probes
    transport = CodexTransport(repo_root=Path("/tmp"), model="codex/gpt-6.1-sol")
    run.version_stdout = "something-else\n"
    with pytest.raises(CodexTransportError, match="requires codex-cli"):
        transport.preflight()
    monkeypatch.setattr(codex_transport, "CODEX_EXECUTABLE", Path("/nonexistent-codex-probe"))
    with pytest.raises(CodexTransportError, match="unavailable"):
        transport.preflight()
    with pytest.raises(ValueError):
        CodexTransport(repo_root=Path("/tmp"), model="  ")


class _FakeChild:
    """Stub child that replays one canned transcript through the real files."""

    def __init__(self, argv, **kwargs):
        self.argv = list(argv)
        self.kwargs = kwargs
        self.env = dict(kwargs.get("env", {}))
        self.returncode: int | None = None
        self._exit_code = kwargs.get("_exit_code", 0)
        self.pid = 999_999_999  # never signalled: killpg raises, _stop suppresses
        stdout = kwargs.get("stdout")
        lines = kwargs.get("_canned_lines", _valid_lines())
        if stdout is not None:
            stdout.write(("\n".join(lines) + "\n").encode())
            stdout.flush()
        last = next((argv[i + 1] for i, token in enumerate(argv) if token == "-o"), None)
        if last is not None:
            Path(last).write_text(
                kwargs.get("_last_message", "revised instructions"), encoding="utf-8"
            )

    def poll(self):
        if self.returncode is None:
            self.returncode = self._exit_code
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


def _stub_request_child(monkeypatch, **overrides):
    seen: dict = {}

    def _factory(argv, **kwargs):
        child = _FakeChild(argv, **{**kwargs, **overrides})
        seen["argv"] = child.argv
        seen["env"] = child.env
        seen["stdin"] = kwargs.get("stdin")
        seen["cwd"] = kwargs.get("cwd")
        seen["child"] = child
        return child

    monkeypatch.setattr(codex_transport, "_POPEN", _factory)
    return seen


def test_request_refuses_failed_or_tool_using_children(tmp_path, monkeypatch, cli_probes):
    transport = CodexTransport(repo_root=tmp_path, model="codex/gpt-6.1-sol")

    def _run_case(name, lines=None, returncode=0):
        _stub_request_child(monkeypatch, _canned_lines=lines or [], _exit_code=returncode)
        with pytest.raises(CodexTransportError):
            transport.request("revise", directory=tmp_path / name)

    _run_case(
        "failed-turn",
        _valid_lines()[:-1] + [_event({"type": "turn.failed", "error": {"message": "nope"}})],
    )
    tool_lines = _valid_lines()
    tool_lines.insert(
        2,
        _event({"type": "item.completed", "item": {"id": "sh", "type": "command_execution"}}),
    )
    _run_case("tool-turn", tool_lines)
    _run_case("nonzero-exit", _valid_lines(), returncode=3)


def test_request_timeout_kills_group_without_retry(tmp_path, monkeypatch, cli_probes):
    killed: list = []

    class _HangingChild:
        pid = 999_999_999
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return None

    monkeypatch.setattr(codex_transport, "_POPEN", lambda *a, **k: _HangingChild())
    monkeypatch.setattr(os, "killpg", lambda *a, **k: killed.append(a))
    clock = iter((0.0, 1.0))
    monkeypatch.setattr(codex_transport.time, "monotonic", lambda: next(clock))
    transport = CodexTransport(repo_root=tmp_path, model="codex/gpt-6.1-sol", timeout_seconds=0.2)
    with pytest.raises(CodexTransportError, match="timed out"):
        transport.request("revise", directory=tmp_path / "hang")
    assert killed, "timed-out child process group must be signalled"
    # Retained journal shows the attempt without usable text.
    assert (tmp_path / "hang" / "events.jsonl").is_file()


def test_subscription_child_cannot_receive_api_keys_or_provider_overrides(
    tmp_path, monkeypatch, cli_probes
):
    monkeypatch.setenv("OPENAI_API_KEY", "unused-test-value")
    monkeypatch.setenv("ZAI_OPENAPI_API_KEY", "unused-test-value")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid")
    seen = _stub_request_child(monkeypatch)
    transport = CodexTransport(repo_root=tmp_path, model="codex/gpt-6.1-sol")
    transport.request("Revise the instructions", directory=tmp_path / "request")
    assert "OPENAI_API_KEY" not in seen["env"]
    assert "ZAI_OPENAPI_API_KEY" not in seen["env"]
    assert "OPENAI_BASE_URL" not in seen["env"]


def test_unreported_token_fields_remain_absent(tmp_path):
    lines = _valid_lines()
    lines[-1] = _event(
        {"type": "turn.completed", "usage": {"input_tokens": 120, "output_tokens": 60}}
    )
    _, usage = parse_transcript(_write(tmp_path / "events.jsonl", lines))
    assert "cached_input_tokens" not in usage
    assert "reasoning_output_tokens" not in usage


def test_second_started_or_unfinished_turn_is_not_a_proposal(tmp_path):
    lines = _valid_lines()
    lines.insert(2, _event({"type": "turn.started"}))
    with pytest.raises(CodexTransportError, match="more than one turn"):
        parse_transcript(_write(tmp_path / "events.jsonl", lines))


def test_startup_warning_is_retained_without_discarding_completed_response(tmp_path):
    lines = _valid_lines()
    lines.insert(
        1,
        _event(
            {
                "type": "item.completed",
                "item": {"type": "error", "message": "Configuration warning"},
            }
        ),
    )
    path = _write(tmp_path / "events.jsonl", lines)
    response, _ = parse_transcript(path)
    assert response == "revised instructions"
    offset, pending, started = _poll_events(path, 0, b"")
    assert started
    assert pending == b""


@pytest.mark.parametrize("during_turn", [False, True])
def test_model_reroute_or_active_turn_error_is_not_a_proposal(tmp_path, during_turn):
    lines = _valid_lines()
    message = "Turn error" if during_turn else "model rerouted: requested -> replacement"
    lines.insert(
        2 if during_turn else 1,
        _event({"type": "item.completed", "item": {"type": "error", "message": message}}),
    )
    path = _write(tmp_path / "events.jsonl", lines)
    with pytest.raises(CodexTransportError):
        parse_transcript(path)
    with pytest.raises(CodexTransportError):
        _poll_events(path, 0, b"")


@pytest.fixture
def empty_seed_cli(tmp_path, monkeypatch, cli_probes):
    home = tmp_path / "native-home"
    home.mkdir()
    (home / ".sandbox_migration").touch()
    rollout = home / "sessions" / "seed.jsonl"
    state = SimpleNamespace(
        thread_id=str(uuid4()), rollout=rollout, home=home, requests=[], extra_records=[],
        metadata_patch={}, thread_patch={},
        config={
            "mcp_servers": {"private_server": {"env": {"SECRET": "never-retain-this"}}},
            "features": {feature: False for feature in codex_transport.DISABLED_FEATURES},
            "notify": [], "project_doc_max_bytes": 0, "model_provider": "openai",
        },
    )

    class SeedServer:
        returncode = 0

        def __init__(self, argv, **kwargs):
            reader, writer = os.pipe()
            self.stdout = os.fdopen(reader, "rb", buffering=0)
            self.thread = None
            self.params = None

            class Input:
                closed = False

                def write(inner, payload):
                    request = json.loads(payload)
                    method, params = request["method"], request["params"]
                    state.requests.append(request)
                    if method == "initialize":
                        result = {"codexHome": str(home)}
                    elif method == "config/read":
                        result = {"config": state.config}
                    elif method == "thread/start":
                        assert params["config"]["mcp_servers"] == {
                            name: {"enabled": False}
                            for name in state.config.get("mcp_servers", {})
                        }
                        self.params = params
                        self.thread = {
                            "id": state.thread_id, "sessionId": state.thread_id,
                            "path": str(rollout), "turns": [], "preview": "",
                            "ephemeral": False, "historyMode": "legacy",
                        }
                        self.thread.update(state.thread_patch)
                        result = {"thread": self.thread}
                    elif method == "thread/section/move":
                        rollout.parent.mkdir(parents=True)
                        meta = {
                            "id": state.thread_id, "session_id": state.thread_id,
                            "cwd": self.params["cwd"],
                            "runtime_workspace_roots": [self.params["cwd"]],
                            "model_provider": "openai", "cli_version": "0.162.1",
                            "history_mode": "legacy",
                            "base_instructions": {
                                "text": "Native model-only instructions",
                                "provenance": {"type": "model", "model": self.params["model"]},
                            },
                        }
                        meta.update(state.metadata_patch)
                        records = [
                            {"timestamp": "2026-10-19T00:00:00Z", "type": "session_meta", "payload": meta},
                            *state.extra_records,
                        ]
                        rollout.write_text("".join(json.dumps(record) + "\n" for record in records))
                        result = {}
                    elif method == "thread/read":
                        result = {"thread": self.thread}
                    else:
                        pytest.fail(f"seed server received nonempty activity: {method}")
                    os.write(writer, (json.dumps({"id": request["id"], "result": result}) + "\n").encode())
                    return len(payload)

                def flush(inner):
                    pass

                def close(inner):
                    inner.closed = True
                    os.close(writer)

            self.stdin = Input()

        def poll(self):
            return self.returncode

        def wait(self, **kwargs):
            return self.returncode

    monkeypatch.setattr(codex_transport, "_SEED_POPEN", SeedServer)
    return state


@pytest.mark.parametrize("shared_prefill", [False, True])
def test_requests_keep_independent_inputs_and_immutable_evidence(
    tmp_path, monkeypatch, empty_seed_cli, shared_prefill
):
    launches = []
    answers = iter(("private vote one", "independent vote two"))

    def launch(argv, **kwargs):
        cwd = Path(kwargs["cwd"])
        launches.append((cwd, kwargs["stdin"].read(), tuple(cwd.iterdir())))
        answer = next(answers)
        lines = _valid_lines(answer)
        lines[0] = _event({"type": "thread.started", "thread_id": str(uuid4())})
        return _FakeChild(argv, **kwargs, _canned_lines=lines, _last_message=answer)

    monkeypatch.setattr(codex_transport, "_POPEN", launch)
    prefill = tmp_path / "prefill" if shared_prefill else None
    transport = CodexTransport(
        repo_root=tmp_path, model="codex/gpt-6.1-sol", prefill_cwd=prefill
    )
    first = tmp_path / "vote-one"
    second = tmp_path / "vote-two"
    prompt = "Independently review the same task."
    assert transport.request(prompt, directory=first)["response"] == "private vote one"
    retained = {p.name: p.read_bytes() for p in first.iterdir() if p.is_file()}
    assert transport.request(prompt, directory=second)["response"] == "independent vote two"
    assert {p.name: p.read_bytes() for p in first.iterdir() if p.is_file()} == retained
    assert retained.keys() == {"prompt.txt", "events.jsonl", "client.log", "last_message.txt"}
    assert (first / "last_message.txt").read_text() == "private vote one"
    assert (second / "last_message.txt").read_text() == "independent vote two"
    assert [stdin for _, stdin, _ in launches] == [prompt.encode(), prompt.encode()]
    assert all(not contents for _, _, contents in launches)
    assert (launches[0][0] == launches[1][0]) is shared_prefill
    for directory in (first, second):
        for name in retained:
            assert (directory / name).stat().st_mode & 0o777 == 0o600
    if shared_prefill:
        assert prefill is not None and not tuple(prefill.iterdir())
        assert not (first / "cwd").exists()
        assert not (second / "cwd").exists()
        assert [request["method"] for request in empty_seed_cli.requests] == [
            "initialize", "config/read", "thread/start", "thread/section/move", "thread/read",
        ]
        seed = transport._prefill_seed
        assert seed is not None
        assert seed.rollout.read_bytes() == seed.snapshot.read_bytes() == seed.content
        bootstrap = (first / "prefill-seed" / "events.jsonl").read_text()
        assert "never-retain-this" not in bootstrap
        assert prompt not in bootstrap and "private vote one" not in bootstrap
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in seed.snapshot.parent.iterdir())
    else:
        assert not empty_seed_cli.requests
    with pytest.raises(FileExistsError):
        transport.request(prompt, directory=first)
    assert len(launches) == 2
    assert {p.name: p.read_bytes() for p in first.iterdir() if p.is_file()} == retained


@pytest.mark.parametrize("overlap", ["same", "cwd-in-evidence", "evidence-in-cwd"])
def test_shared_prefill_cannot_contain_or_be_contained_by_evidence(
    tmp_path, monkeypatch, cli_probes, overlap
):
    evidence = tmp_path / "evidence"
    prefill = evidence
    if overlap == "cwd-in-evidence":
        prefill = evidence / "prefill"
    elif overlap == "evidence-in-cwd":
        prefill = tmp_path / "prefill"
        evidence = prefill / "evidence"
    monkeypatch.setattr(
        codex_transport, "_POPEN", lambda *a, **k: pytest.fail("overlapping context launched")
    )
    transport = CodexTransport(
        repo_root=tmp_path, model="codex/gpt-6.1-sol", prefill_cwd=prefill
    )
    with pytest.raises(ValueError, match="disjoint"):
        transport.request("review", directory=evidence)
    assert not evidence.exists()
    assert not prefill.exists()


@pytest.mark.parametrize("linked", ["cwd", "cwd-parent", "evidence-parent"])
def test_shared_prefill_refuses_symlink_escape(
    tmp_path, monkeypatch, cli_probes, linked
):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    prefill = tmp_path / "prefill"
    evidence = tmp_path / "evidence"
    if linked == "cwd":
        prefill = link
    elif linked == "cwd-parent":
        prefill = link / "prefill"
    else:
        evidence = link / "evidence"
    monkeypatch.setattr(
        codex_transport, "_POPEN", lambda *a, **k: pytest.fail("symlink context launched")
    )
    transport = CodexTransport(
        repo_root=tmp_path, model="codex/gpt-6.1-sol", prefill_cwd=prefill
    )
    with pytest.raises(ValueError, match="symlinks"):
        transport.request("review", directory=evidence)
    assert not tuple(outside.iterdir())


@pytest.mark.parametrize("entry", ["prior-vote.txt", ".codex/history.jsonl", "AGENTS.md"])
def test_shared_prefill_refuses_existing_context_without_deleting_it(
    tmp_path, monkeypatch, cli_probes, entry
):
    prefill = tmp_path / "prefill"
    context = prefill / entry
    context.parent.mkdir(parents=True)
    context.write_text("another vote or instruction must never be visible")
    monkeypatch.setattr(
        codex_transport, "_POPEN", lambda *a, **k: pytest.fail("nonempty context launched")
    )
    transport = CodexTransport(
        repo_root=tmp_path, model="codex/gpt-6.1-sol", prefill_cwd=prefill
    )
    with pytest.raises(ValueError, match="empty"):
        transport.request("review", directory=tmp_path / "evidence")
    assert context.read_text() == "another vote or instruction must never be visible"


def test_contaminated_prefill_rejects_response_and_preserves_failed_evidence(
    tmp_path, monkeypatch, empty_seed_cli
):
    prefill = tmp_path / "prefill"
    launches = []

    def contaminate(argv, **kwargs):
        launches.append(Path(kwargs["cwd"]))
        (Path(kwargs["cwd"]) / "prior-vote.txt").write_text("must not reach another vote")
        return _FakeChild(argv, **kwargs)

    monkeypatch.setattr(codex_transport, "_POPEN", contaminate)
    transport = CodexTransport(
        repo_root=tmp_path, model="codex/gpt-6.1-sol", prefill_cwd=prefill
    )
    first = tmp_path / "failed-vote"
    with pytest.raises(ValueError, match="empty"):
        transport.request("review", directory=first)
    retained = {p.name: p.read_bytes() for p in first.iterdir() if p.is_file()}
    assert retained["prompt.txt"] == b"review"
    assert retained["events.jsonl"]
    assert retained["last_message.txt"] == b"revised instructions"
    with pytest.raises(ValueError, match="empty"):
        transport.request("review independently", directory=tmp_path / "blocked-vote")
    assert launches == [prefill]
    assert {p.name: p.read_bytes() for p in first.iterdir() if p.is_file()} == retained
    assert (prefill / "prior-vote.txt").read_text() == "must not reach another vote"


@pytest.mark.parametrize("contamination", ["user-prompt", "assistant-output", "ancestry", "custom-base", "live-turn"])
def test_seed_with_any_inherited_context_never_launches_a_vote(
    tmp_path, monkeypatch, empty_seed_cli, contamination
):
    if contamination in {"user-prompt", "assistant-output"}:
        empty_seed_cli.extra_records.append({
            "timestamp": "2026-10-19T00:00:00Z", "type": "response_item",
            "payload": {
                "type": "message", "role": "user" if contamination == "user-prompt" else "assistant",
                "content": [{"type": "input_text", "text": "a previous private vote"}],
            },
        })
    elif contamination == "ancestry":
        empty_seed_cli.metadata_patch["history_base"] = {"thread_id": str(uuid4()), "ordinal": 1}
    elif contamination == "custom-base":
        empty_seed_cli.metadata_patch["base_instructions"] = {
            "text": "a previous private vote", "provenance": {"type": "custom"},
        }
    else:
        empty_seed_cli.thread_patch["turns"] = [{"id": "prior-turn", "items": []}]
    monkeypatch.setattr(
        codex_transport, "_POPEN", lambda *a, **k: pytest.fail("contaminated seed launched a vote")
    )
    transport = CodexTransport(
        repo_root=tmp_path, model="codex/gpt-6.1-sol", prefill_cwd=tmp_path / "prefill"
    )
    failed = tmp_path / "failed"
    with pytest.raises(CodexTransportError, match="seed"):
        transport.request("new independent rubric prompt", directory=failed)
    assert (failed / "prompt.txt").read_text() == "new independent rubric prompt"
    assert (failed / "prefill-seed" / "events.jsonl").read_bytes()
    assert empty_seed_cli.rollout.exists()
    retained = empty_seed_cli.rollout.read_bytes()
    with pytest.raises(CodexTransportError, match="already failed"):
        transport.request("do not silently recreate the seed", directory=tmp_path / "again")
    assert empty_seed_cli.rollout.read_bytes() == retained
    assert sum(request["method"] == "thread/start" for request in empty_seed_cli.requests) == 1


@pytest.mark.parametrize("setting", [
    "instructions", "base_instructions", "user_instructions", "developer_instructions",
    "model_instructions_file", "model_catalog_json", "model_provider", "model_providers",
])
def test_seed_refuses_custom_config_before_thread_creation_and_redacts_values(
    tmp_path, monkeypatch, empty_seed_cli, setting
):
    empty_seed_cli.config[setting] = (
        {"openai": {"base_url": "https://unapproved.invalid/private-route"}}
        if setting == "model_providers" else "private-inherited-instructions"
    )
    monkeypatch.setattr(
        codex_transport, "_POPEN", lambda *a, **k: pytest.fail("custom seed launched a vote")
    )
    transport = CodexTransport(
        repo_root=tmp_path, model="codex/gpt-6.1-sol", prefill_cwd=tmp_path / "prefill"
    )
    evidence = tmp_path / "failed"
    with pytest.raises(CodexTransportError, match="native instructions and provider"):
        transport.request("review independently", directory=evidence)
    assert [request["method"] for request in empty_seed_cli.requests] == ["initialize", "config/read"]
    retained = b"".join(p.read_bytes() for p in (evidence / "prefill-seed").iterdir())
    assert b"never-retain-this" not in retained
    assert b"private-inherited-instructions" not in retained
    assert b"unapproved.invalid" not in retained
    assert not empty_seed_cli.rollout.exists()


@pytest.mark.parametrize("mutation", ["history", "native-instructions", "snapshot", "symlink"])
def test_mutated_or_replaced_seed_blocks_the_next_independent_vote(
    tmp_path, monkeypatch, empty_seed_cli, mutation
):
    launches = []

    def launch(argv, **kwargs):
        launches.append(kwargs["stdin"].read())
        lines = _valid_lines("first private vote")
        lines[0] = _event({"type": "thread.started", "thread_id": str(uuid4())})
        return _FakeChild(argv, **kwargs, _canned_lines=lines, _last_message="first private vote")

    monkeypatch.setattr(codex_transport, "_POPEN", launch)
    transport = CodexTransport(
        repo_root=tmp_path, model="codex/gpt-6.1-sol", prefill_cwd=tmp_path / "prefill"
    )
    first = tmp_path / "first"
    transport.request("independent rubric", directory=first)
    retained = {p.name: p.read_bytes() for p in first.iterdir() if p.is_file()}
    seed = transport._prefill_seed
    assert seed is not None
    if mutation == "history":
        with seed.rollout.open("ab") as stream:
            stream.write(b'{"type":"response_item","payload":{"text":"prior vote"}}\n')
    elif mutation == "native-instructions":
        record = json.loads(seed.content)
        record["payload"]["base_instructions"]["text"] = "a prior vote disguised as native instructions"
        seed.rollout.write_text(json.dumps(record) + "\n")
    elif mutation == "snapshot":
        seed.snapshot.write_bytes(b"changed retained seed")
    else:
        original = seed.rollout.with_name("original-seed.jsonl")
        seed.rollout.rename(original)
        seed.rollout.symlink_to(original)
    with pytest.raises((CodexTransportError, ValueError), match="history|changed|symlinks"):
        transport.request("new independent rubric", directory=tmp_path / "second")
    assert launches == [b"independent rubric"]
    assert {p.name: p.read_bytes() for p in first.iterdir() if p.is_file()} == retained
    assert sum(request["method"] == "thread/start" for request in empty_seed_cli.requests) == 1


@pytest.mark.parametrize("reuse", ["seed", "previous-vote"])
def test_fork_cannot_reuse_seed_or_prior_vote_lifecycle(
    tmp_path, monkeypatch, empty_seed_cli, reuse
):
    prior_id = str(uuid4())
    ids = iter((empty_seed_cli.thread_id,)) if reuse == "seed" else iter((prior_id, prior_id))

    def launch(argv, **kwargs):
        lines = _valid_lines("not accepted without a fresh lifecycle")
        lines[0] = _event({"type": "thread.started", "thread_id": next(ids)})
        return _FakeChild(argv, **kwargs, _canned_lines=lines)

    monkeypatch.setattr(codex_transport, "_POPEN", launch)
    transport = CodexTransport(
        repo_root=tmp_path, model="codex/gpt-6.1-sol", prefill_cwd=tmp_path / "prefill"
    )
    if reuse == "previous-vote":
        first = transport.request("first independent rubric", directory=tmp_path / "first")
        assert first["transport"]["prefill_cache"]["thread_id"] == prior_id
        assert first["transport"]["prefill_cache"]["seed_thread_id"] != prior_id
    failed = tmp_path / "failed"
    with pytest.raises(CodexTransportError, match="reused"):
        transport.request("new independent rubric", directory=failed)
    assert (failed / "events.jsonl").read_bytes()
    assert (failed / "prompt.txt").read_text() == "new independent rubric"
    assert transport._prefill_seed.rollout.read_bytes() == transport._prefill_seed.content


def test_seed_contamination_during_a_fork_rejects_its_text_and_blocks_later_votes(
    tmp_path, monkeypatch, empty_seed_cli
):
    launches = []

    def launch(argv, **kwargs):
        launches.append(kwargs["stdin"].read())
        with empty_seed_cli.rollout.open("ab") as stream:
            stream.write(b'{"type":"response_item","payload":{"text":"another private vote"}}\n')
        lines = _valid_lines("must not be accepted")
        lines[0] = _event({"type": "thread.started", "thread_id": str(uuid4())})
        return _FakeChild(argv, **kwargs, _canned_lines=lines, _last_message="must not be accepted")

    monkeypatch.setattr(codex_transport, "_POPEN", launch)
    transport = CodexTransport(
        repo_root=tmp_path, model="codex/gpt-6.1-sol", prefill_cwd=tmp_path / "prefill"
    )
    failed = tmp_path / "failed"
    with pytest.raises(CodexTransportError, match="history"):
        transport.request("independent rubric", directory=failed)
    retained = {p.name: p.read_bytes() for p in failed.iterdir() if p.is_file()}
    assert retained["last_message.txt"] == b"must not be accepted"
    assert retained["events.jsonl"]
    with pytest.raises(CodexTransportError, match="history"):
        transport.request("new independent rubric", directory=tmp_path / "blocked")
    assert launches == [b"independent rubric"]
    assert {p.name: p.read_bytes() for p in failed.iterdir() if p.is_file()} == retained


@pytest.mark.parametrize("unsafe", ["hooks", "plugins", "legacy-notify", "project-instructions"])
def test_unknown_effective_hook_or_instruction_isolation_never_creates_a_seed(
    tmp_path, monkeypatch, empty_seed_cli, unsafe
):
    if unsafe in {"hooks", "plugins"}:
        empty_seed_cli.config["features"][unsafe] = True
    elif unsafe == "legacy-notify":
        empty_seed_cli.config["notify"] = ["private-hook-command", "private-hook-argument"]
    else:
        empty_seed_cli.config["project_doc_max_bytes"] = 1024
    monkeypatch.setattr(
        codex_transport, "_POPEN", lambda *a, **k: pytest.fail("unsafe seed launched a vote")
    )
    transport = CodexTransport(
        repo_root=tmp_path, model="codex/gpt-6.1-sol", prefill_cwd=tmp_path / "prefill"
    )
    evidence = tmp_path / "failed"
    with pytest.raises(CodexTransportError, match="isolation is unknown"):
        transport.request("independent rubric", directory=evidence)
    assert [request["method"] for request in empty_seed_cli.requests] == ["initialize", "config/read"]
    assert not empty_seed_cli.rollout.exists()
    retained = (evidence / "prefill-seed" / "events.jsonl").read_text()
    assert "private-hook-command" not in retained
    assert "private-hook-argument" not in retained
    assert "never-retain-this" not in retained


@pytest.mark.parametrize("marker_kind", ["missing", "symlink", "directory"])
def test_seed_never_triggers_native_policy_migration(
    tmp_path, monkeypatch, empty_seed_cli, marker_kind
):
    marker = empty_seed_cli.home / ".sandbox_migration"
    marker.unlink()
    if marker_kind == "symlink":
        target = tmp_path / "other-marker"
        target.write_bytes(b"v1\n")
        marker.symlink_to(target)
    elif marker_kind == "directory":
        marker.mkdir()
    monkeypatch.setattr(
        codex_transport, "_POPEN", lambda *a, **k: pytest.fail("unmigrated seed launched a vote")
    )
    transport = CodexTransport(
        repo_root=tmp_path, model="codex/gpt-6.1-sol", prefill_cwd=tmp_path / "prefill"
    )
    with pytest.raises(CodexTransportError, match="refusing policy migration"):
        transport.request("independent rubric", directory=tmp_path / "failed")
    assert [request["method"] for request in empty_seed_cli.requests] == ["initialize", "config/read"]
    assert not empty_seed_cli.rollout.exists()
    if marker_kind == "missing":
        assert not marker.exists()
    elif marker_kind == "symlink":
        assert marker.is_symlink() and target.read_bytes() == b"v1\n"
    else:
        assert marker.is_dir() and not tuple(marker.iterdir())


def test_metadata_seed_does_not_inherit_or_modify_native_rules_or_hook_config(
    tmp_path, monkeypatch, empty_seed_cli
):
    rules = empty_seed_cli.home / "rules"
    rules.mkdir()
    policy = rules / "default.rules"
    policy.write_text("private local execution policy")
    hooks = empty_seed_cli.home / "hooks.json"
    hooks.write_text("private hook configuration")
    empty_seed_cli.config["hooks"] = {
        "SessionStart": [{"hooks": [{"type": "command", "command": "private-hook-command"}]}],
    }
    retained = {path: path.read_bytes() for path in (
        policy, hooks, empty_seed_cli.home / ".sandbox_migration",
    )}

    def launch(argv, **kwargs):
        lines = _valid_lines("independent vote")
        lines[0] = _event({"type": "thread.started", "thread_id": str(uuid4())})
        return _FakeChild(argv, **kwargs, _canned_lines=lines, _last_message="independent vote")

    monkeypatch.setattr(codex_transport, "_POPEN", launch)
    transport = CodexTransport(
        repo_root=tmp_path, model="codex/gpt-6.1-sol", prefill_cwd=tmp_path / "prefill"
    )
    evidence = tmp_path / "vote"
    assert transport.request("independent rubric", directory=evidence)["response"] == "independent vote"
    assert {path: path.read_bytes() for path in retained} == retained
    seed = transport._prefill_seed
    assert seed is not None
    assert b"private local execution policy" not in seed.content
    assert b"private hook configuration" not in seed.content
    assert b"private-hook-command" not in seed.content
    bootstrap = (evidence / "prefill-seed" / "events.jsonl").read_text()
    assert "private-hook-command" not in bootstrap
    assert "never-retain-this" not in bootstrap
