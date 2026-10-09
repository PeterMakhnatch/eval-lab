from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from evallab.execution_contracts import MIMO_SELFHOSTED_CONTEXT_TOKENS
from evallab.mimoagent_worker import (
    SAMPLING,
    _pop_exec_recorder,
    _push_exec_recorder,
    _record_exec_result,
    _tool_exit_metadata,
)

ROOT = Path(__file__).resolve().parents[1]
NATIVE_PYTHON = ROOT / "tools/mimoagent-harbor/.venv/bin/python"
WORKER = ROOT / "src/evallab/mimoagent_worker.py"


def _answer(content="native answer", *, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": "local-completion",
        "object": "chat.completion",
        "created": 1,
        "model": "local-fixture",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }


def _run_worker(
    tmp_path,
    respond,
    *,
    budget=0.2,
    instruction="Repair /testbed/task.py",
    observer_fault=None,
    served_context_tokens=None,
    capture_tool_uploads=False,
    capture_tool_execs=False,
    antihack=False,
):
    if not NATIVE_PYTHON.is_file():
        pytest.skip("separate pinned Xiaomi interpreter is not installed")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            reply = respond(len(requests), request)
            if reply is None:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            status, payload, *body_delay = reply
            data = (json.dumps(payload) if not isinstance(payload, str) else payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if body_delay:
                time.sleep(body_delay[0])
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logs = tmp_path / "native"
    initial = {
        "instruction": instruction,
        "cwd": "/testbed",
        "model_name": "local-fixture",
        "proxy_url": f"http://127.0.0.1:{server.server_port}/v1",
        "capability": "local-only-test-capability",
        "config_path": str(ROOT / "tools/mimoagent-harbor/swe.yaml"),
        "global_config_dir": str(tmp_path / "global-config"),
        "native_logs_dir": str(logs),
        "native_trajectory_path": str(logs / "native-trajectory.json"),
        "antihack": antihack,
        "explicit_rules": False,
    }
    if served_context_tokens is not None:
        initial["served_context_tokens"] = served_context_tokens

    # Private fixture-only override, not a production config/environment knob.
    # SDK requests and parsing still run in the actual pinned interpreter.
    bootstrap = (
        "import runpy,sys; n=runpy.run_path(sys.argv[1]); "
        "g=n['main'].__globals__; g['COLD_START_BUDGET_S']=float(sys.argv[2]); "
        "g['_retry_delay']=lambda attempts: 0.005; "
    )
    if capture_tool_uploads:
        # Replace only the sandbox transport. The real SDK still parses
        # OpenAI tool_calls, runs WriteTool and writes its upload tempfile.
        bootstrap += (
            "from pathlib import Path; Rpc=g['SandboxRpc']; "
            "Rpc._request=lambda self,method,**kw: "
            "self.emit({'event':'fixture_upload','target':kw['target'],"
            "'file_text':Path(kw['source']).read_text()}) if method=='upload' "
            "else {'output':'File created successfully','exit_code':0}; "
        )
    if capture_tool_execs:
        # Replace only the sandbox transport. The real SDK still parses
        # OpenAI tool_calls and runs the native tool; the guard under test
        # sits between parsing and this transport.
        bootstrap += (
            "Rpc=g['SandboxRpc']; "
            "Rpc._request=lambda self,method,**kw: "
            "self.emit({'event':'fixture_exec','method':method,"
            "'command':kw.get('command')}) or "
            "{'output':'canned transport output','returncode':0}; "
        )
    if observer_fault is not None:
        bootstrap += (
            "fail=lambda *a,**k: (_ for _ in ()).throw("
            "OSError('secret observer failure at /controller/private')); "
        )
        if observer_fault == "message_file":
            bootstrap += (
                "from mimoagent.agents.base import BaseAgent; BaseAgent._append_msg_to_file=fail; "
            )
        else:
            bootstrap += "import mimoagent.run.utils.save as save; save.save_traj=fail; "
    bootstrap += "n['main']()"
    try:
        process = subprocess.run(
            [str(NATIVE_PYTHON), "-I", "-c", bootstrap, str(WORKER), str(budget)],
            input=json.dumps(initial) + "\n",
            text=True,
            capture_output=True,
            timeout=30,
            check=True,
            cwd=ROOT,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    events = [json.loads(line) for line in process.stdout.splitlines()]
    native_path = logs / "native-trajectory.json"
    native = json.loads(native_path.read_text()) if native_path.is_file() else None
    finished = next(event for event in events if event["event"] == "finished")
    calls = [event for event in events if event["event"] == "model_call"]
    return requests, events, calls, native, finished, process.stderr


def _assert_no_infrastructure_turn(native, events, finished, tmp_path):
    main = native["trajs"]["main"]["messages"]
    assert [message["role"] for message in main] == ["system", "user"]
    assert len([event for event in events if event["event"] == "message"]) == 2
    assert not any(event["event"] == "step_complete" for event in events)
    assert finished["stop_reason"] == "infra_error"
    assert native["info"]["infra_error"] == finished["infra_error"]
    exported = json.dumps({"events": events, "native": native})
    assert "Traceback (most recent call last)" not in exported
    assert str(tmp_path) not in exported
    assert str(ROOT) not in exported


def test_cold_endpoint_outlasts_native_five_attempt_limit_without_changing_prefix(tmp_path):
    requests, events, calls, native, finished, stderr = _run_worker(
        tmp_path,
        lambda attempt, request: (
            (503, {"error": {"message": "starting"}}) if attempt <= 6 else (200, _answer())
        ),
        budget=1,
    )
    assert [call["response_status"] for call in calls] == [503] * 6 + [200]
    assert requests == [requests[0]] * 7
    assert calls[-1]["sampling"] == SAMPLING
    assert calls[-1]["usage"] == _answer()["usage"]
    assert finished["exit_status"] == "Idle"
    assert finished["model_stats"]["api_calls"] == 1
    assert [message["role"] for message in native["trajs"]["main"]["messages"]] == [
        "system",
        "user",
        "assistant",
    ]
    assert finished["result"] == "native answer"
    assert "stop_reason" not in finished
    assert str(ROOT) not in stderr


@pytest.mark.parametrize(
    ("status", "body", "budget_reason"),
    [
        (400, {"error": {"message": "invalid request"}}, None),
        (401, {"error": {"message": "bad authentication"}}, None),
        (403, {"error": {"message": "denied"}}, None),
        (429, {"error": {"reason": "ceiling:requests"}}, "ceiling:requests"),
        (429, "trial budget exhausted", "trial budget exhausted"),
    ],
)
def test_permanent_and_budget_refusals_fail_fast_without_synthetic_user_turn(
    tmp_path, status, body, budget_reason
):
    requests, events, calls, native, finished, stderr = _run_worker(
        tmp_path, lambda attempt, request: (status, body)
    )
    assert len(requests) == 1
    assert [call["response_status"] for call in calls] == [status]
    assert calls[0]["proxy_budget_reason"] == budget_reason
    assert finished["infra_error"]["retry_rounds"] == 0
    assert finished["infra_error"]["last_response_status"] == status
    assert finished["infra_error"]["budget_refusal"] == budget_reason
    _assert_no_infrastructure_turn(native, events, finished, tmp_path)
    assert "native transport retry" not in stderr


@pytest.mark.parametrize("body", [_answer(""), "not valid JSON", {"choices": "malformed"}])
def test_http200_is_never_retried_even_if_native_cannot_accept_answer(tmp_path, body):
    requests, events, calls, native, finished, stderr = _run_worker(
        tmp_path, lambda attempt, request: (200, body)
    )
    assert len(requests) == 1
    assert [call["response_status"] for call in calls] == [200]
    assert finished["infra_error"]["retry_rounds"] == 0
    _assert_no_infrastructure_turn(native, events, finished, tmp_path)
    assert "native transport retry" not in stderr


def test_known_cold_retry_exhaustion_keeps_real_status_and_no_traceback(tmp_path):
    requests, events, calls, native, finished, stderr = _run_worker(
        tmp_path,
        lambda attempt, request: (503, {"error": {"message": f"host {ROOT}"}}),
        budget=0.08,
    )
    assert calls and all(call["response_status"] == 503 for call in calls)
    assert len(calls) == len(requests)
    assert finished["infra_error"]["retry_window_exhausted"] is True
    assert finished["infra_error"]["last_response_status"] == 503
    assert finished["model_stats"]["api_calls"] == 0
    _assert_no_infrastructure_turn(native, events, finished, tmp_path)
    assert str(ROOT) not in stderr


def test_connection_failure_attempt_is_recorded_without_inventing_status_or_usage(tmp_path):
    requests, events, calls, native, finished, stderr = _run_worker(
        tmp_path, lambda attempt, request: None if attempt == 1 else (200, _answer())
    )
    assert len(requests) == 2
    assert [call["response_status"] for call in calls] == [None, 200]
    assert calls[0]["usage"] is None
    assert calls[0]["transport_error_type"] == "APIConnectionError"
    assert finished["exit_status"] == "Idle"
    assert finished["model_stats"]["api_calls"] == 1


def test_known_cold_http200_body_retains_native_read_timeout(tmp_path):
    requests, events, calls, native, finished, stderr = _run_worker(
        tmp_path,
        lambda attempt, request: (
            (503, {"error": {"message": "starting"}}) if attempt == 1 else (200, _answer(), 0.35)
        ),
        budget=0.2,
    )
    assert [call["response_status"] for call in calls] == [503, 200]
    assert finished["exit_status"] == "Idle"
    assert finished["result"] == "native answer"


def test_initial_ordinary_generation_keeps_native_preheader_read_timeout(tmp_path):
    def respond(attempt, request):
        time.sleep(0.15)
        return 200, _answer()

    requests, events, calls, native, finished, stderr = _run_worker(tmp_path, respond, budget=0.05)
    assert [call["response_status"] for call in calls] == [200]
    assert finished["exit_status"] == "Idle"
    assert finished["result"] == "native answer"


def test_known_cold_retry_preheaders_cannot_wait_through_native_generation_timeout(tmp_path):
    def respond(attempt, request):
        if attempt == 1:
            return 503, {"error": {"message": "starting"}}
        time.sleep(0.25)
        return 503, {"error": {"message": "still starting"}}

    requests, events, calls, native, finished, stderr = _run_worker(tmp_path, respond, budget=0.06)
    assert [call["response_status"] for call in calls] == [503, None]
    assert calls[-1]["transport_error_type"] == "APITimeoutError"
    assert finished["infra_error"]["retry_window_exhausted"] is True
    _assert_no_infrastructure_turn(native, events, finished, tmp_path)


def test_provider_throttling_recovers_but_is_not_classified_as_budget_exhaustion(tmp_path):
    requests, events, calls, native, finished, stderr = _run_worker(
        tmp_path,
        lambda attempt, request: (
            (429, {"error": {"message": "provider rate limit exceeded"}})
            if attempt == 1
            else (200, _answer())
        ),
    )
    assert [call["response_status"] for call in calls] == [429, 200]
    assert all(call["proxy_budget_reason"] is None for call in calls)
    assert finished["exit_status"] == "Idle"


def test_child_log_identity_is_logical_before_parent_model_consumes_it(tmp_path):
    tool_call = {
        "id": "child-call",
        "type": "function",
        "function": {
            "name": "agent",
            "arguments": json.dumps(
                {"subagent_type": "explore", "prompt": "Inspect /testbed/task.py"}
            ),
        },
    }
    instruction = "Keep legitimate /testbed paths and the words ModelQueryError Traceback"
    requests, events, calls, native, finished, stderr = _run_worker(
        tmp_path,
        lambda attempt, request: (
            (200, _answer(None, tool_calls=[tool_call]))
            if attempt == 1
            else (200, _answer("child answer" if attempt == 2 else "root answer"))
        ),
        instruction=instruction,
    )
    parent_messages = native["trajs"]["main"]["messages"]
    result = next(message for message in parent_messages if message["role"] == "tool")
    assert result["tool_call_id"] == "child-call"
    assert "'log_file': 'childlog://explore_1'" in result["content"]
    assert requests[-1]["messages"][-1] == result
    assert "explore_1" in native["trajs"]
    assert instruction in parent_messages[1]["content"]
    assert (tmp_path / "native/agent_msgs/explore_1.log").is_file()
    assert finished["result"] == "root answer"
    assert str(ROOT) not in json.dumps({"events": events, "native": native})
    histories = {}
    boundaries = []
    for event in events:
        name = event.get("name")
        if event["event"] == "agent_start":
            histories[name] = []
        elif event["event"] == "message":
            histories[name].append(event["message"])
        elif event["event"] == "step_complete":
            messages = histories[name]
            assert event["message_count"] == len(messages)
            assert event["native_step"] == sum(m["role"] == "assistant" for m in messages)
            boundaries.append((name, event["native_step"], messages[-1]["role"]))
            if name == "main" and event["native_step"] == 1:
                assert messages[-1]["tool_call_id"] == "child-call"
    assert boundaries == [
        ("explore_1", 1, "assistant"),
        ("main", 1, "tool"),
        ("main", 2, "assistant"),
    ]


@pytest.mark.parametrize("suffix", ["", "\n"])
def test_openai_tool_calls_preserve_write_arguments_without_html_unescape(tmp_path, suffix):
    # qwen3_coder supplies ordinary OpenAI tool_calls. Entities and the
    # parser-supplied string boundary are not unescaped or repaired by us.
    file_text = "  <tag>&lt;literal&gt; &amp; &#10; &quot;雪&quot;</tag>" + suffix
    arguments = json.dumps({"path": "/testbed/output.txt", "file_text": file_text})
    tool_call = {
        "id": "write-call",
        "type": "function",
        "function": {"name": "write", "arguments": arguments},
    }
    requests, events, calls, native, finished, stderr = _run_worker(
        tmp_path,
        lambda attempt, request: (
            (200, _answer(None, tool_calls=[tool_call]))
            if attempt == 1
            else (200, _answer("write done"))
        ),
        capture_tool_uploads=True,
    )
    assert finished["exit_status"] == "Idle"
    assert finished["result"] == "write done"
    assert len(requests) == 2
    uploads = [event for event in events if event["event"] == "fixture_upload"]
    assert uploads == [
        {"event": "fixture_upload", "target": "/testbed/output.txt", "file_text": file_text}
    ]
    main = native["trajs"]["main"]["messages"]
    assistant = next(message for message in main if message["role"] == "assistant")
    assert assistant["tool_calls"] == [tool_call]
    assert requests[-1]["messages"][-2]["tool_calls"] == [tool_call]
    assert requests[-1]["messages"][-1]["tool_call_id"] == "write-call"


def test_child_query_failure_is_recorded_without_falsely_stopping_completed_root(tmp_path):
    tool_call = {
        "id": "failed-child-call",
        "type": "function",
        "function": {
            "name": "agent",
            "arguments": json.dumps(
                {"subagent_type": "explore", "prompt": "Inspect /testbed/task.py"}
            ),
        },
    }

    def respond(attempt, request):
        if attempt == 1:
            return 200, _answer(None, tool_calls=[tool_call])
        if attempt == 2:
            return 400, {"error": {"message": f"controller failure at {ROOT}"}}
        return 200, _answer("root recovered")

    requests, events, calls, native, finished, stderr = _run_worker(tmp_path, respond)
    assert finished["exit_status"] == "Idle"
    assert finished["result"] == "root recovered"
    assert "stop_reason" not in finished
    assert "infra_error" not in finished
    child_stop = finished["agent_stops"]["explore_1"]
    assert child_stop["exit_status"] == "ModelQueryError"
    assert child_stop["stop_reason"] == "infra_error"
    assert child_stop["infra_error"]["last_response_status"] == 400
    assert native["info"]["agent_stops"] == finished["agent_stops"]
    assert [message["role"] for message in native["trajs"]["explore_1"]["messages"]] == [
        "system",
        "user",
    ]
    parent_result = requests[-1]["messages"][-1]
    assert parent_result["tool_call_id"] == "failed-child-call"
    assert "'log_file': 'childlog://explore_1'" in parent_result["content"]
    exported = json.dumps({"events": events, "native": native, "requests": requests})
    assert str(ROOT) not in exported
    assert "Traceback (most recent call last)" not in exported
    assert [
        (event["name"], event["native_step"])
        for event in events
        if event["event"] == "step_complete"
    ] == [("main", 1), ("main", 2)]


def test_nested_empty_tool_does_not_steal_parent_exec_attribution():
    parent = _push_exec_recorder()
    child = _push_exec_recorder()
    try:
        _pop_exec_recorder(child)
        _record_exec_result({"returncode": 7})
        assert _tool_exit_metadata("bash", parent, None) == {
            "exit_codes": [7],
            "exit_code": 7,
        }
    finally:
        _pop_exec_recorder(child)
        _pop_exec_recorder(parent)


@pytest.mark.parametrize("observer_fault", ["message_file", "native_trajectory"])
def test_native_observer_file_failure_preserves_idle_history_and_usage(tmp_path, observer_fault):
    requests, events, calls, native, finished, stderr = _run_worker(
        tmp_path, lambda *_: (200, _answer("unchanged answer")), observer_fault=observer_fault
    )
    assert len(requests) == 1
    assert finished["exit_status"] == "Idle"
    assert finished["result"] == "unchanged answer"
    assert finished["model_stats"]["api_calls"] == 1
    assert finished["model_stats"]["input_tokens"] == 7
    assert finished["model_stats"]["output_tokens"] == 3
    messages = [event["message"] for event in events if event["event"] == "message"]
    assert [message["role"] for message in messages] == ["system", "user", "assistant"]
    assert messages[-1]["content"] == "unchanged answer"
    assert calls[0]["usage"]["prompt_tokens"] == 7
    assert f"native observer failure: {observer_fault} OSError" in stderr
    assert "secret observer failure" not in stderr
    assert "/controller/private" not in stderr
    if observer_fault == "message_file":
        assert native["trajs"]["main"]["messages"] == messages
    else:
        assert native is None


SGLANG_CONTEXT_BODY = {
    "object": "error",
    "message": "The input (262294 tokens) is longer than the model's context length (262144 tokens).",
    "type": "BadRequestError",
    "param": None,
    "code": 400,
}

SGLANG_TOTAL_OVERFLOW_BODY = {
    "object": "error",
    "message": (
        "Requested token count exceeds the model's maximum context length of 262144 tokens. "
        "You requested a total of 262608 tokens: 262294 tokens from the input messages and "
        "314 tokens for the completion. Please reduce the number of tokens in the input "
        "messages or the completion to fit within the limit."
    ),
    "type": "BadRequest",
    "param": None,
    "code": 400,
}


@pytest.mark.parametrize("body", [SGLANG_CONTEXT_BODY, SGLANG_TOTAL_OVERFLOW_BODY])
def test_sglang_context_400_ends_rollout_as_context_exhausted(tmp_path, body):
    requests, events, calls, native, finished, stderr = _run_worker(
        tmp_path, lambda attempt, request: (400, body)
    )
    assert len(requests) == 1
    assert [call["response_status"] for call in calls] == [400]
    assert finished["exit_status"] == "ContextExhausted"
    assert finished["stop_reason"] == "context_exhausted"
    assert "infra_error" not in finished
    exhaustion = finished["context_exhaustion"]
    assert exhaustion["error_type"] == "ModelQueryError"
    assert exhaustion["root_error_type"] == "BadRequestError"
    assert exhaustion["last_response_status"] == 400
    assert finished["result"] == "native context exhausted: ModelQueryError"
    # No synthetic user turn and no step boundary: history is untouched.
    main = native["trajs"]["main"]["messages"]
    assert [message["role"] for message in main] == ["system", "user"]
    assert len([event for event in events if event["event"] == "message"]) == 2
    assert not any(event["event"] == "step_complete" for event in events)
    assert native["info"]["stop_reason"] == "context_exhausted"
    assert native["info"]["context_exhaustion"] == exhaustion
    assert "infra_error" not in native["info"]
    exported = json.dumps({"events": events, "native": native})
    assert "Traceback (most recent call last)" not in exported
    assert "native transport retry" not in stderr


def test_non_context_400_stays_model_query_error(tmp_path):
    body = {
        "object": "error",
        "message": "Invalid schema for tool 'bash': property 'command' is required.",
        "type": "BadRequestError",
        "param": None,
        "code": 400,
    }
    requests, events, calls, native, finished, stderr = _run_worker(
        tmp_path, lambda attempt, request: (400, body)
    )
    assert finished["exit_status"] == "ModelQueryError"
    assert finished["stop_reason"] == "infra_error"
    assert "context_exhaustion" not in finished
    _assert_no_infrastructure_turn(native, events, finished, tmp_path)


@pytest.mark.parametrize(
    "last_prompt_tokens",
    [MIMO_SELFHOSTED_CONTEXT_TOKENS, MIMO_SELFHOSTED_CONTEXT_TOKENS - 1, 65_536],
)
def test_uninformative_400_uses_the_current_served_context_limit(tmp_path, last_prompt_tokens):
    tool_call = {
        "id": "child-call",
        "type": "function",
        "function": {
            "name": "agent",
            "arguments": json.dumps(
                {"subagent_type": "explore", "prompt": "Inspect /testbed/task.py"}
            ),
        },
    }
    first = _answer(None, tool_calls=[tool_call])
    first["usage"] = {
        "prompt_tokens": last_prompt_tokens,
        "completion_tokens": 12,
        "total_tokens": last_prompt_tokens + 12,
    }

    def respond(attempt, request):
        if attempt == 1:
            return 200, first
        if attempt == 2:
            return 200, _answer("child done")
        return 400, {"error": {"message": "request failed"}}

    requests, events, calls, native, finished, stderr = _run_worker(
        tmp_path, respond, served_context_tokens=MIMO_SELFHOSTED_CONTEXT_TOKENS
    )
    # After the child answers, the parent's next call fails with a vague
    # 400. Only a previous prefix filling the current 262k window qualifies;
    # neither the old 64k limit nor one token below the current one does.
    assert MIMO_SELFHOSTED_CONTEXT_TOKENS == 262_144
    if last_prompt_tokens == MIMO_SELFHOSTED_CONTEXT_TOKENS:
        assert finished["exit_status"] == "ContextExhausted"
        assert finished["stop_reason"] == "context_exhausted"
        assert finished["context_exhaustion"]["last_prompt_tokens"] == 262_144
        assert finished["context_exhaustion"]["served_context_tokens"] == 262_144
        assert finished["result"] == "native context exhausted: ModelQueryError"
    else:
        assert finished["exit_status"] == "ModelQueryError"
        assert finished["stop_reason"] == "infra_error"
        assert "context_exhaustion" not in finished


def test_small_prefix_uninformative_400_stays_model_query_error(tmp_path):
    requests, events, calls, native, finished, stderr = _run_worker(
        tmp_path,
        lambda attempt, request: (400, {"error": {"message": "request failed"}}),
        served_context_tokens=MIMO_SELFHOSTED_CONTEXT_TOKENS,
    )
    assert finished["exit_status"] == "ModelQueryError"
    assert finished["stop_reason"] == "infra_error"
    _assert_no_infrastructure_turn(native, events, finished, tmp_path)


def _git_spelunk_call():
    return {
        "id": "spelunk-call",
        "type": "function",
        "function": {
            "name": "bash",
            "arguments": json.dumps({"command": "git fsck --unreachable --no-reflogs"}),
        },
    }


def _respond_with_spelunk_then_answer(attempt, request):
    if attempt == 1:
        return 200, _answer(None, tool_calls=[_git_spelunk_call()])
    return 200, _answer("spelunk done")


def test_antihack_guard_blocks_object_spelunking_without_running_it(tmp_path):
    requests, events, calls, native, finished, stderr = _run_worker(
        tmp_path,
        _respond_with_spelunk_then_answer,
        capture_tool_execs=True,
        antihack=True,
    )
    assert finished["exit_status"] == "Idle"
    assert finished["result"] == "spelunk done"
    assert finished["antihack"] is True
    assert finished["antihack_blocks"] == 1
    # The blocked call never reached the sandbox transport.
    assert [event for event in events if event["event"] == "fixture_exec"] == []
    # The model saw the guard's dummy observation, not sandbox output.
    tool_message = next(
        message
        for message in native["trajs"]["main"]["messages"]
        if message.get("tool_call_id") == "spelunk-call"
    )
    assert "Permission denied" in tool_message["content"]
    assert "canned transport output" not in tool_message["content"]
    blocks = native["info"]["antihack_blocks"]
    assert [(block["tool"], block["field"]) for block in blocks] == [("bash", "command")]
    assert native["info"]["explicit_rules"] is False
    assert str(ROOT) not in stderr


def test_antihack_guard_off_by_default_runs_the_same_call(tmp_path):
    requests, events, calls, native, finished, stderr = _run_worker(
        tmp_path,
        _respond_with_spelunk_then_answer,
        capture_tool_execs=True,
    )
    assert finished["exit_status"] == "Idle"
    assert finished["antihack"] is False
    assert finished["antihack_blocks"] == 0
    execs = [event for event in events if event["event"] == "fixture_exec"]
    assert [event["command"] for event in execs] == ["git fsck --unreachable --no-reflogs"]
    tool_message = next(
        message
        for message in native["trajs"]["main"]["messages"]
        if message.get("tool_call_id") == "spelunk-call"
    )
    assert "canned transport output" in tool_message["content"]
    assert native["info"]["antihack_blocks"] == []


def test_antihack_guard_covers_sdk_spawned_children(tmp_path):
    spawn = {
        "id": "spawn-call",
        "type": "function",
        "function": {
            "name": "agent",
            "arguments": json.dumps(
                {"subagent_type": "explore", "prompt": "Inspect /testbed/task.py"}
            ),
        },
    }

    def respond(attempt, request):
        if attempt == 1:
            return 200, _answer(None, tool_calls=[spawn])
        if attempt == 2:
            return 200, _answer(None, tool_calls=[_git_spelunk_call()])
        if attempt == 3:
            return 200, _answer("child done")
        return 200, _answer("root done")

    requests, events, calls, native, finished, stderr = _run_worker(
        tmp_path,
        respond,
        capture_tool_execs=True,
        antihack=True,
    )
    assert finished["exit_status"] == "Idle"
    assert finished["result"] == "root done"
    assert finished["antihack_blocks"] == 1
    # The child's blocked call never reached the sandbox transport either.
    assert [event for event in events if event["event"] == "fixture_exec"] == []
    blocks = native["info"]["antihack_blocks"]
    assert [(block["tool"], block["field"]) for block in blocks] == [("bash", "command")]
    assert "explore_1" in native["trajs"]
