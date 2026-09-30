"""Strict Codex transcript parsing and subscription-route wiring.

Every test here is deterministic and hermetic: the real CLI and the host
Keychain are never touched. ``codex_transport._RUN``/``_POPEN`` are the
only CLI-boundary probes and are stubbed per test.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

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


def test_unknown_event_or_item_shapes_fail_closed(tmp_path):
    path = _write(
        tmp_path / "events.jsonl",
        [_event({"type": "thread.started", "thread_id": "t"}), _event({"type": "frobnicate"})],
    )
    with pytest.raises(CodexTransportError, match="Unknown"):
        parse_transcript(path)
    lines = _valid_lines()
    lines.insert(
        3, _event({"type": "item.completed", "item": {"id": "z", "type": "teleport"}})
    )
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

    def __init__(self, *, version_stdout="codex-cli 0.154.0\n", login_stdout="Logged in using ChatGPT\n", login_code=0):
        self.version_stdout = version_stdout
        self.login_stdout = login_stdout
        self.login_code = login_code
        self.argv_seen: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.argv_seen.append(list(argv))
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, self.version_stdout.encode(), b"")
        return subprocess.CompletedProcess(
            argv, self.login_code, self.login_stdout.encode(), b""
        )


@pytest.fixture
def cli_probes(tmp_path, monkeypatch):
    exe = tmp_path / "codex"
    exe.write_bytes(b"#!/bin/sh\nexec codex \"$@\"\n")
    monkeypatch.setattr(codex_transport, "CODEX_EXECUTABLE", exe)
    run = _FakeRun()
    monkeypatch.setattr(codex_transport, "_RUN", run)
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
    monkeypatch.setattr(
        codex_transport, "CODEX_EXECUTABLE", Path("/nonexistent-codex-probe")
    )
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
        last = next(
            (argv[i + 1] for i, token in enumerate(argv) if token == "-o"), None
        )
        if last is not None:
            Path(last).write_text("revised instructions", encoding="utf-8")

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


def test_request_refuses_failed_or_tool_using_children(
    tmp_path, monkeypatch, cli_probes
):
    transport = CodexTransport(repo_root=tmp_path, model="codex/gpt-6.1-sol")

    def _run_case(name, lines=None, returncode=0):
        _stub_request_child(
            monkeypatch, _canned_lines=lines or [], _exit_code=returncode
        )
        with pytest.raises(CodexTransportError):
            transport.request("revise", directory=tmp_path / name)

    _run_case(
        "failed-turn",
        _valid_lines()[:-1]
        + [_event({"type": "turn.failed", "error": {"message": "nope"}})],
    )
    tool_lines = _valid_lines()
    tool_lines.insert(
        2,
        _event(
            {"type": "item.completed", "item": {"id": "sh", "type": "command_execution"}}
        ),
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
    transport = CodexTransport(
        repo_root=tmp_path, model="codex/gpt-6.1-sol", timeout_seconds=0.2
    )
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
    lines[-1] = _event({"type": "turn.completed", "usage": {"input_tokens": 120, "output_tokens": 60}})
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
        _event({"type": "item.completed", "item": {"type": "error", "message": "Configuration warning"}}),
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
