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

from evallab.mimoagent_worker import SAMPLING

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


def _run_worker(tmp_path, respond, *, budget=0.2, instruction="Repair /testbed/task.py"):
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
    }
    # Private fixture-only override, not a production config/environment knob.
    # SDK requests and parsing still run in the actual pinned interpreter.
    bootstrap = (
        "import runpy,sys; n=runpy.run_path(sys.argv[1]); "
        "g=n['main'].__globals__; g['COLD_START_BUDGET_S']=float(sys.argv[2]); "
        "g['_retry_delay']=lambda attempts: 0.005; n['main']()"
    )
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
    native = json.loads((logs / "native-trajectory.json").read_text())
    finished = next(event for event in events if event["event"] == "finished")
    calls = [event for event in events if event["event"] == "model_call"]
    return requests, events, calls, native, finished, process.stderr


def _assert_no_infrastructure_turn(native, events, finished, tmp_path):
    main = native["trajs"]["main"]["messages"]
    assert [message["role"] for message in main] == ["system", "user"]
    assert len([event for event in events if event["event"] == "message"]) == 2
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
            (503, {"error": {"message": "starting"}})
            if attempt <= 6 else (200, _answer())
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
        "system", "user", "assistant"
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
            (503, {"error": {"message": "starting"}})
            if attempt == 1 else (200, _answer(), 0.35)
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

    requests, events, calls, native, finished, stderr = _run_worker(
        tmp_path, respond, budget=0.05
    )
    assert [call["response_status"] for call in calls] == [200]
    assert finished["exit_status"] == "Idle"
    assert finished["result"] == "native answer"


def test_known_cold_retry_preheaders_cannot_wait_through_native_generation_timeout(tmp_path):
    def respond(attempt, request):
        if attempt == 1:
            return 503, {"error": {"message": "starting"}}
        time.sleep(0.25)
        return 503, {"error": {"message": "still starting"}}

    requests, events, calls, native, finished, stderr = _run_worker(
        tmp_path, respond, budget=0.06
    )
    assert [call["response_status"] for call in calls] == [503, None]
    assert calls[-1]["transport_error_type"] == "APITimeoutError"
    assert finished["infra_error"]["retry_window_exhausted"] is True
    _assert_no_infrastructure_turn(native, events, finished, tmp_path)


def test_provider_throttling_recovers_but_is_not_classified_as_budget_exhaustion(tmp_path):
    requests, events, calls, native, finished, stderr = _run_worker(
        tmp_path,
        lambda attempt, request: (
            (429, {"error": {"message": "provider rate limit exceeded"}})
            if attempt == 1 else (200, _answer())
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
            "arguments": json.dumps({"subagent_type": "explore", "prompt": "Inspect /testbed/task.py"}),
        },
    }
    instruction = "Keep legitimate /testbed paths and the words ModelQueryError Traceback"
    requests, events, calls, native, finished, stderr = _run_worker(
        tmp_path,
        lambda attempt, request: (
            (200, _answer(None, tool_calls=[tool_call]))
            if attempt == 1 else (200, _answer("child answer" if attempt == 2 else "root answer"))
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


def test_child_query_failure_is_recorded_without_falsely_stopping_completed_root(tmp_path):
    tool_call = {
        "id": "failed-child-call",
        "type": "function",
        "function": {
            "name": "agent",
            "arguments": json.dumps({"subagent_type": "explore", "prompt": "Inspect /testbed/task.py"}),
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
        "system", "user"
    ]
    parent_result = requests[-1]["messages"][-1]
    assert parent_result["tool_call_id"] == "failed-child-call"
    assert "'log_file': 'childlog://explore_1'" in parent_result["content"]
    exported = json.dumps({"events": events, "native": native, "requests": requests})
    assert str(ROOT) not in exported
    assert "Traceback (most recent call last)" not in exported
