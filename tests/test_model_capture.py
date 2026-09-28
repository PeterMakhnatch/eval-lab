"""Focused behavior tests for independent model-call capture.

Covers the proxy record path (forwarding, streaming SSE reassembly, header
and key scrubbing), attribution precedence (route token > session >
conversation chaining), every completeness verdict, link Parquet output, and
the run-report surface.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import UTC
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from evallab import model_capture
from evallab.model_capture import (
    CaptureRecorder,
    TrialEvidence,
    attribute_calls,
    classify_kind,
    extract_from_sse,
    find_trial_capture,
    first_user_text,
    join_upstream_path,
    judge_trial,
    link_capture,
    parse_sse_payloads,
    redact_key_text,
    request_message_pairs,
    selected_headers,
    serve_capture,
    split_route_token,
    write_provenance,
)

SECRET = "upstream-key-sentinel-abcdef123456"


class _StubUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    received: list[dict[str, Any]] = []

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _reply(self, status: int, content_type: str, body: bytes) -> None:
        type(self).received.append(
            {"path": self.path, "auth": self.headers.get("Authorization")}
        )
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode())
        except (ValueError, UnicodeDecodeError):
            payload = {}
        if self.path.endswith("/stream") or bool(payload.get("stream")):
            events: list[Any] = [
                {
                    "id": "c1",
                    "model": "stub",
                    "choices": [
                        {"index": 0, "delta": {"role": "assistant", "content": "hel"}}
                    ],
                },
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "content": "lo",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "t1",
                                        "function": {"name": "bash", "arguments": '{"cm'},
                                    }
                                ],
                            },
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "function": {"arguments": 'd":"ls"}'}}
                                ]
                            },
                        }
                    ]
                },
                {
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                },
            ]
            chunks = [
                b"data: " + json.dumps(event).encode() + b"\n\n" for event in events
            ] + [b"data: [DONE]\n\n"]
            self._reply(200, "text/event-stream", b"".join(chunks))
        else:
            stream = bool(payload.get("stream"))
            if stream:
                self._reply(200, "text/event-stream", b"data: [DONE]\n\n")
                return
            body = json.dumps(
                {
                    "id": "c1",
                    "model": "stub",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "done",
                                "tool_calls": [
                                    {
                                        "id": "t1",
                                        "type": "function",
                                        "function": {"name": "bash", "arguments": '{"cmd":"ls"}'},
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                }
            ).encode()
            self._reply(200, "application/json", body)


@pytest.fixture()
def upstream() -> Any:
    _StubUpstream.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubUpstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join()


def _proxy(tmp_path: Path, upstream: Any, **kwargs: Any) -> Any:
    server, recorder, _manifest = serve_capture(
        upstream=f"http://127.0.0.1:{upstream.server_address[1]}",
        out_dir=tmp_path / "cap",
        bind="127.0.0.1",
        port=0,
        **kwargs,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, recorder, thread


def _post(port: int, path: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> bytes:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read()


def _records(tmp_path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (tmp_path / "cap" / "calls.jsonl").read_text().splitlines()
        if line.strip()
    ]


def test_proxy_forwards_and_records_full_bodies(tmp_path: Path, upstream: Any) -> None:
    server, recorder, thread = _proxy(tmp_path, upstream)
    try:
        body = _post(
            server.server_address[1],
            "/v1/chat/completions",
            {"model": "stub", "messages": [{"role": "user", "content": "hi"}]},
            {"Authorization": "Bearer client-secret-token", "X-Session-ID": "sess-1"},
        )
        payload = json.loads(body)
        assert payload["choices"][0]["message"]["content"] == "done"
    finally:
        server.shutdown()
        thread.join()
        recorder.close()
    (record,) = _records(tmp_path)
    assert record["method"] == "POST"
    assert record["path"] == "/v1/chat/completions"
    assert record["route_token"] is None
    assert record["response_status"] == 200
    assert record["request_body"]["messages"][0]["content"] == "hi"
    assert record["response_body"]["choices"][0]["message"]["content"] == "done"
    assert record["assistant_texts"] == ["done"]
    assert record["tool_calls"] == [
        {"id": "t1", "name": "bash", "arguments": '{"cmd":"ls"}'}
    ]
    assert record["usage"] == {"prompt_tokens": 11, "completion_tokens": 7}
    assert record["model"] == "stub"
    assert record["session_id"] == "sess-1"
    assert record["error"] is None
    assert isinstance(record["upstream_latency_s"], float)
    # Auth is never recorded, even though it was forwarded in transparent mode.
    assert "authorization" not in record["request_headers"]
    assert "client-secret-token" not in json.dumps(record)


def test_proxy_reassembles_streaming_tool_calls(tmp_path: Path, upstream: Any) -> None:
    server, recorder, thread = _proxy(tmp_path, upstream)
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions",
            data=json.dumps({"model": "stub", "stream": True, "messages": []}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
        assert b"data: [DONE]" in raw
    finally:
        server.shutdown()
        thread.join()
        recorder.close()
    (record,) = _records(tmp_path)
    assert record["response_sse"] is True
    assert "data: [DONE]" in record["response_body"]["sse_raw"]
    assert record["assistant_texts"] == ["hello"]
    assert record["tool_calls"] == [{"id": "t1", "name": "bash", "arguments": '{"cmd":"ls"}'}]
    assert record["usage"] == {"prompt_tokens": 11, "completion_tokens": 7}


def test_proxy_injects_upstream_key_and_never_records_it(tmp_path: Path, upstream: Any) -> None:
    server, recorder, thread = _proxy(tmp_path, upstream, upstream_key=SECRET)
    try:
        _post(
            server.server_address[1],
            "/t/trial-9/v1/chat/completions",
            {"model": "stub", "messages": [{"role": "user", "content": "hi"}]},
            {"Authorization": "Bearer client-placeholder"},
        )
    finally:
        server.shutdown()
        thread.join()
        recorder.close()
    assert _StubUpstream.received[-1]["auth"] == f"Bearer {SECRET}"
    assert _StubUpstream.received[-1]["path"] == "/v1/chat/completions"
    (record,) = _records(tmp_path)
    assert record["route_token"] == "trial-9"
    assert SECRET not in json.dumps(record)


def test_proxy_records_upstream_outage(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubUpstream)
    port = server.server_address[1]
    server.server_close()
    proxy, recorder, manifest = serve_capture(
        upstream=f"http://127.0.0.1:{port}", out_dir=tmp_path / "cap", bind="127.0.0.1", port=0
    )
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(urllib.error.URLError):
            _post(proxy.server_address[1], "/v1/chat/completions", {"model": "stub"})
    finally:
        proxy.shutdown()
        thread.join()
        recorder.close()
    assert manifest["upstream"] == f"http://127.0.0.1:{port}"
    (record,) = _records(tmp_path)
    assert record["response_status"] is None
    assert record["error"]["kind"] == "upstream_unreachable"


def test_sse_reassembly_unit() -> None:
    raw = (
        b": comment\n"
        b'data: {"choices":[{"index":0,"delta":{"content":"a"}}]}\n\n'
        b'data: {"choices":[{"index":0,"delta":{"content":"b"}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    assert [p["choices"][0]["delta"] for p in parse_sse_payloads(raw)] == [
        {"content": "a"},
        {"content": "b"},
    ]
    assert extract_from_sse("chat", raw).assistant_texts == ["ab"]


def test_header_selection_and_key_redaction() -> None:
    headers = selected_headers(
        {"Authorization": "Bearer x", "X-Api-Key": "y", "X-Session-ID": "s", "Content-Type": "application/json"}
    )
    assert headers == {"content-type": "application/json", "x-session-id": "s"}
    assert redact_key_text(f"leaked {SECRET} here", SECRET) == "leaked <redacted> here"
    assert redact_key_text("short", "abc") == "short"
    assert split_route_token("/t/trial_a-1/v1/chat/completions") == (
        "trial_a-1",
        "/v1/chat/completions",
    )
    assert split_route_token("/v1/chat/completions") == (None, "/v1/chat/completions")
    assert split_route_token("/t/bad token!/v1/chat/completions")[0] is None
    assert classify_kind("/v1/chat/completions") == "chat"
    assert classify_kind("/chat/completions") == "chat"
    assert classify_kind("/v1/responses") == "responses"
    assert classify_kind("/v1/messages") == "anthropic_messages"
    assert classify_kind("/v1/models") == "unknown"


def test_join_upstream_path_avoids_doubling() -> None:
    assert join_upstream_path("/v1", "/v1/chat/completions") == "/v1/chat/completions"
    assert join_upstream_path("/v1", "/chat/completions") == "/v1/chat/completions"
    assert join_upstream_path("", "/v1/chat/completions") == "/v1/chat/completions"
    assert join_upstream_path("/v1", "/v1") == "/v1"
    assert join_upstream_path("/api/paas/v4", "/chat/completions") == "/api/paas/v4/chat/completions"


def test_request_pairs_and_first_user() -> None:
    body = {
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": [{"type": "text", "text": "do it"}]},
        ]
    }
    assert request_message_pairs("chat", body) == [("system", "sys"), ("user", "do it")]
    assert first_user_text("chat", body) == "do it"
    assert first_user_text("legacy_completions", {"prompt": "run"}) == "run"
    assert first_user_text("responses", {"input": "go"}) == "go"
    assert first_user_text("chat", {}) == ""


def _trial(
    name: str,
    *,
    session: str | None = None,
    agent_steps: int = 0,
    agent_texts: list[str] | None = None,
    user_texts: list[str] | None = None,
    instruction: str | None = None,
    window: tuple[str, str] | None = None,
) -> TrialEvidence:
    from datetime import datetime

    start = end = None
    if window is not None:
        start = datetime.fromisoformat(window[0].replace("Z", "+00:00"))
        end = datetime.fromisoformat(window[1].replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
    return TrialEvidence(
        name=name,
        trial_id=f"id-{name}",
        directory=Path(f"/tmp/{name}"),
        agent="mini-swe-agent",
        window_start=start,
        window_end=end,
        atif_session=session,
        atif_user_texts=user_texts or [],
        atif_agent_texts=agent_texts or [],
        atif_agent_steps=agent_steps,
        atif_present=agent_steps > 0 or bool(agent_texts),
        instruction=instruction,
    )


def _call(
    seq: int,
    messages: list[dict[str, str]],
    *,
    token: str | None = None,
    session: str | None = None,
    texts: list[str] | None = None,
    started: str = "2026-01-01T00:00:10Z",
    ended: str = "2026-01-01T00:00:12Z",
 ) -> dict[str, Any]:
    return {
        "seq": seq,
        "started_at": started,
        "ended_at": ended,
        "method": "POST",
        "path": "/v1/chat/completions",
        "route_token": token,
        "session_id": session,
        "request_body": {"messages": messages},
        "response_status": 200,
        "assistant_texts": texts or [],
        "usage": None,
    }


def test_attribution_prefers_route_token_over_session() -> None:
    trials = [
        _trial("trial-a", session="sess-shared"),
        _trial("trial-b", session="sess-shared"),
    ]
    calls = [_call(1, [{"role": "user", "content": "hi"}], token="trial-b", session="sess-shared")]
    attribution = attribute_calls(calls, trials)
    assert attribution.assigned == {1: "trial-b"}
    assert attribution.method == {1: "route_token"}
    assert attribution.ambiguous_trials == set()


def test_attribution_session_match_and_conflict() -> None:
    trials = [_trial("trial-a", session="sess-a"), _trial("trial-b", session="sess-b")]
    calls = [_call(1, [{"role": "user", "content": "hi"}], session="sess-a")]
    attribution = attribute_calls(calls, trials)
    assert attribution.assigned == {1: "trial-a"}
    assert attribution.method == {1: "session"}
    conflict = [_call(2, [{"role": "user", "content": "hi"}], session="sess-shared")]
    shared = [_trial("x", session="sess-shared"), _trial("y", session="sess-shared")]
    attribution = attribute_calls(conflict, shared)
    assert attribution.assigned == {}
    assert attribution.unassigned == [2]
    assert attribution.ambiguous_trials == {"x", "y"}


def test_attribution_conversation_chaining_by_instruction_and_window() -> None:
    window = ("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z")
    trials = [
        _trial("trial-a", instruction="do the thing", window=window),
        _trial("trial-b", instruction="do the other", window=window),
    ]
    calls = [
        _call(1, [{"role": "user", "content": "do the thing"}]),
        _call(
            2,
            [
                {"role": "user", "content": "do the thing"},
                {"role": "assistant", "content": "working"},
                {"role": "user", "content": "continue"},
            ],
        ),
    ]
    attribution = attribute_calls(calls, trials)
    assert attribution.assigned == {1: "trial-a", 2: "trial-a"}
    assert attribution.method == {1: "conversation", 2: "conversation"}


def test_attribution_anchors_when_agent_embeds_instruction() -> None:
    window = ("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z")
    instruction = "repair the workflow engine flags " * 4
    trial = _trial("trial-a", instruction=instruction, window=window)
    wrapped = "You are an AI assistant. Task: " + instruction + " Reply with JSON."
    calls = [_call(1, [{"role": "user", "content": wrapped}])]
    attribution = attribute_calls(calls, [trial])
    assert attribution.assigned == {1: "trial-a"}
    assert attribution.method == {1: "conversation"}
    short = _trial("trial-b", instruction="a much longer instruction text for trial b", window=window)
    attribution = attribute_calls([_call(1, [{"role": "user", "content": "hi"}])], [short])
    assert attribution.unassigned == [1]


def test_attribution_rejects_out_of_window_and_reports_ambiguity() -> None:
    window = ("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z")
    trials = [_trial("trial-a", instruction="same task", window=window)]
    late = _call(
        1,
        [{"role": "user", "content": "same task"}],
        started="2026-01-01T05:00:00Z",
        ended="2026-01-01T05:00:01Z",
    )
    assert attribute_calls([late], trials).unassigned == [1]
    twins = [
        _trial("trial-a", instruction="same task", window=window),
        _trial("trial-b", instruction="same task", window=window),
    ]
    attribution = attribute_calls(
        [_call(1, [{"role": "user", "content": "same task"}])], twins
    )
    assert attribution.unassigned == [1]
    assert attribution.ambiguous_trials == {"trial-a", "trial-b"}


def test_verdict_complete() -> None:
    trial = _trial("t", agent_steps=2, agent_texts=["hello world", "second turn"])
    calls = [_call(1, [], texts=["hello world"]), _call(2, [], texts=["second turn"])]
    judgment = judge_trial(trial, calls, ambiguous=False)
    assert judgment["verdict"] == "complete"
    assert judgment["first_divergence"] is None
    assert judgment["captured_assistant_turns"] == 2


def test_verdict_complete_despite_harness_rerendering() -> None:
    captured = '```json\n{"analysis": "The current state shows we are in /app directory.", "commands": ["ls -la"]}\n```'
    rendered = "Analysis: The current state shows we are in /app directory."
    trial = _trial("t", agent_steps=1, agent_texts=[rendered])
    judgment = judge_trial(trial, [_call(1, [], texts=[captured])], ambiguous=False)
    assert judgment["verdict"] == "complete"


def test_verdict_trajectory_missing() -> None:
    trial = _trial("t")
    judgment = judge_trial(trial, [_call(1, [], texts=["hello"])], ambiguous=False)
    assert judgment["verdict"] == "trajectory_missing"


def test_verdict_trajectory_truncated_by_count_and_by_text() -> None:
    trial = _trial("t", agent_steps=1, agent_texts=["hello"])
    calls = [_call(1, [], texts=["hello"]), _call(2, [], texts=["extra turn"])]
    judgment = judge_trial(trial, calls, ambiguous=False)
    assert judgment["verdict"] == "trajectory_truncated"
    assert judgment["first_divergence"]["kind"] == "count_mismatch"
    trial = _trial("t", agent_steps=2, agent_texts=["hello", "something else"])
    judgment = judge_trial(trial, [_call(1, [], texts=["hello", "never recorded"])], ambiguous=False)
    assert judgment["verdict"] == "trajectory_truncated"
    assert judgment["first_divergence"]["kind"] == "missing_turn"
    assert judgment["first_divergence"]["turn_index"] == 1


def test_verdict_capture_missing_and_ambiguous() -> None:
    trial = _trial("t", agent_steps=2, agent_texts=["hello", "bye"])
    assert judge_trial(trial, [], ambiguous=False)["verdict"] == "capture_missing"
    assert judge_trial(trial, [], ambiguous=True)["verdict"] == "ambiguous"
    empty = _trial("t")
    assert judge_trial(empty, [], ambiguous=False)["verdict"] == "capture_missing"


def _job_dir(
    tmp_path: Path,
    name: str,
    trials: list[dict[str, Any]],
    *,
    with_task: str | None = None,
) -> Path:
    job = tmp_path / name
    (job).mkdir()
    (job / "result.json").write_text(json.dumps({"id": f"job-{name}", "n_total_trials": len(trials)}))
    for spec in trials:
        trial_dir = job / spec["trial_name"]
        trial_dir.mkdir()
        result = {
            "id": spec.get("trial_id", f"id-{spec['trial_name']}"),
            "trial_name": spec["trial_name"],
            "config": {
                "agent": {"name": spec.get("agent", "mini-swe-agent")},
                **({"task": {"path": with_task}} if with_task else {}),
            },
            "agent_execution": {
                "started_at": spec.get("window", ("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"))[0],
                "finished_at": spec.get("window", ("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"))[1],
            },
        }
        (trial_dir / "result.json").write_text(json.dumps(result))
        if spec.get("trajectory") is not None:
            agent_dir = trial_dir / "agent"
            agent_dir.mkdir()
            (agent_dir / "trajectory.json").write_text(json.dumps(spec["trajectory"]))
    if with_task:
        task_dir = tmp_path / with_task
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "instruction.md").write_text("do the thing\n")
    return job


def _write_calls(capture: Path, calls: list[dict[str, Any]]) -> None:
    capture.mkdir(parents=True, exist_ok=True)
    with (capture / "calls.jsonl").open("w", encoding="utf-8") as handle:
        for index, call in enumerate(calls, start=1):
            handle.write(json.dumps({"seq": index, **call}) + "\n")


def test_link_end_to_end_and_report_lookup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    capture = tmp_path / "cap"
    _write_calls(
        capture,
        [
            {
                "started_at": "2026-01-01T00:10:00Z",
                "ended_at": "2026-01-01T00:10:02Z",
                "method": "POST",
                "path": "/v1/chat/completions",
                "route_token": "trial-a",
                "session_id": None,
                "request_body": {"messages": [{"role": "user", "content": "do the thing"}]},
                "response_status": 200,
                "response_sse": False,
                "assistant_texts": ["all done"],
                "tool_calls": [],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                "model": "stub",
                "upstream_latency_s": 0.01,
                "error": None,
            }
        ],
    )
    job = _job_dir(
        tmp_path,
        "job1",
        [
            {
                "trial_name": "trial-a",
                "trajectory": {
                    "schema_version": "ATIF-v1.7",
                    "session_id": "sess-a",
                    "steps": [
                        {"step_id": 1, "source": "user", "message": "do the thing"},
                        {"step_id": 2, "source": "agent", "message": "all done"},
                    ],
                },
            },
            {"trial_name": "trial-b", "trajectory": None},
        ],
    )
    derived = tmp_path / "derived"
    receipt = link_capture(capture, job, derived_root=derived)
    assert receipt["calls_total"] == 1
    assert receipt["calls_assigned"] == 1
    by_name = {t["trial_name"]: t for t in receipt["trials"]}
    assert by_name["trial-a"]["verdict"] == "complete"
    assert by_name["trial-b"]["verdict"] == "capture_missing"
    assert (derived / "job_id=job-job1" / "model_calls.parquet").is_file()
    assert (derived / "job_id=job-job1" / "trial_capture.parquet").is_file()
    assert (derived / "job_id=job-job1" / "capture_link.json").is_file()
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived / "parquet"))
    assert find_trial_capture(job / "trial-a") is None
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived))
    found = find_trial_capture(job / "trial-a")
    assert found is not None and found["verdict"] == "complete"
    assert find_trial_capture(job / "trial-b")["verdict"] == "capture_missing"
    assert find_trial_capture(job / "missing") is None


def test_lookup_anchors_at_containing_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EVALLAB_DERIVED_ROOT", raising=False)
    checkout = tmp_path / "lab"
    (checkout / ".git").mkdir(parents=True)
    (checkout / "runs" / "r").mkdir(parents=True)
    capture = checkout / "cap"
    job = _job_dir(checkout / "runs" / "r", "job9", [{"trial_name": "trial-a", "trajectory": None}])
    _write_calls(capture, [])
    link_capture(capture, job, repo_root=checkout)
    leaf = "job_id=job-job9"
    assert (checkout / "derived" / "parquet" / leaf / "trial_capture.parquet").is_file()
    found = find_trial_capture(job / "trial-a")
    assert found is not None and found["verdict"] == "capture_missing"


def test_link_conversation_match_without_token(tmp_path: Path) -> None:
    capture = tmp_path / "cap"
    _write_calls(
        capture,
        [
            {
                "started_at": "2026-01-01T00:10:00Z",
                "ended_at": "2026-01-01T00:10:02Z",
                "method": "POST",
                "path": "/v1/chat/completions",
                "route_token": None,
                "session_id": None,
                "request_body": {"messages": [{"role": "user", "content": "do the thing"}]},
                "response_status": 200,
                "response_sse": False,
                "assistant_texts": ["working"],
                "tool_calls": [],
                "usage": None,
                "model": "stub",
                "upstream_latency_s": 0.01,
                "error": None,
            }
        ],
    )
    job = _job_dir(
        tmp_path,
        "job2",
        [
            {
                "trial_name": "trial-a",
                "trajectory": {
                    "schema_version": "ATIF-v1.7",
                    "session_id": "other",
                    "steps": [
                        {"step_id": 1, "source": "user", "message": "do the thing"},
                        {"step_id": 2, "source": "agent", "message": "working"},
                    ],
                },
            }
        ],
        with_task="task",
    )
    receipt = link_capture(capture, job, derived_root=tmp_path / "derived")
    (judgment,) = receipt["trials"]
    assert judgment["verdict"] == "complete"


def test_provenance_validates_against_schema(tmp_path: Path) -> None:
    recorder = CaptureRecorder(tmp_path / "cap")
    recorder.append({"hello": "world"})
    recorder.close()
    provenance = write_provenance(tmp_path / "cap", upstream="http://127.0.0.1:9")
    from evallab.schemas import ProvenanceMetadata

    parsed = ProvenanceMetadata(**{k: v for k, v in provenance.items() if k != "call_count"})
    assert parsed.zone == "02-local-evidence"
    assert parsed.material_digest.startswith("sha256:")


def test_clean_shutdown_closes_with_matching_digest(tmp_path: Path, upstream: Any) -> None:
    server, recorder, thread = _proxy(tmp_path, upstream)
    _post(server.server_address[1], "/v1/chat/completions", {"model": "stub", "messages": []})
    # shutdown() must run off the serving thread (as the SIGTERM handler does).
    closer = threading.Thread(target=server.shutdown, daemon=True)
    closer.start()
    thread.join(timeout=15)
    closer.join(timeout=15)
    recorder.close()
    provenance = write_provenance(tmp_path / "cap", upstream="http://127.0.0.1:9")
    assert not thread.is_alive()
    assert provenance["call_count"] == 1
    assert provenance["material_digest"] == f"sha256:{model_capture.sha256_file(tmp_path / 'cap' / 'calls.jsonl')}"

def test_run_report_capture_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from evallab.interpretation.run_report import (
        build_run_report,
        render_run_report_markdown,
    )

    job = _job_dir(tmp_path, "job3", [{"trial_name": "trial-a", "trajectory": None}])
    trial = job / "trial-a"
    report = build_run_report(trial)
    assert report["capture"] is None
    assert "## Independent capture" not in render_run_report_markdown(report)
    capture = tmp_path / "cap"
    _write_calls(capture, [])
    derived = tmp_path / "derived"
    link_capture(capture, job, derived_root=derived)
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived))
    report = build_run_report(trial)
    assert report["capture"] is not None
    assert report["capture"]["verdict"] == "capture_missing"
    assert report["schema"] == "evallab.run_report/v1"
    rendered = render_run_report_markdown(report)
    assert "## Independent capture" in rendered
    assert "capture missing" in rendered
