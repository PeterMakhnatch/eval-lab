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
    extract_from_payload,
    extract_from_sse,
    extract_ollama_ndjson,
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
        type(self).received.append({"path": self.path, "auth": self.headers.get("Authorization")})
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
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": "hel"}}],
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
                                "tool_calls": [{"index": 0, "function": {"arguments": 'd":"ls"}'}}]
                            },
                        }
                    ]
                },
                {
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                },
            ]
            chunks = [b"data: " + json.dumps(event).encode() + b"\n\n" for event in events] + [
                b"data: [DONE]\n\n"
            ]
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


def _post(
    port: int, path: str, payload: dict[str, Any], headers: dict[str, str] | None = None
) -> bytes:
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
    assert record["tool_calls"] == [{"id": "t1", "name": "bash", "arguments": '{"cmd":"ls"}'}]
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
        {
            "Authorization": "Bearer x",
            "X-Api-Key": "y",
            "X-Session-ID": "s",
            "Content-Type": "application/json",
        }
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
    assert classify_kind("/api/paas/v4/chat/completions") == "chat"
    assert classify_kind("/api/chat") == "ollama_chat"


def test_join_upstream_path_avoids_doubling() -> None:
    assert join_upstream_path("/v1", "/v1/chat/completions") == "/v1/chat/completions"
    assert join_upstream_path("/v1", "/chat/completions") == "/v1/chat/completions"
    assert join_upstream_path("", "/v1/chat/completions") == "/v1/chat/completions"
    assert join_upstream_path("/v1", "/v1") == "/v1"
    assert (
        join_upstream_path("/api/paas/v4", "/chat/completions") == "/api/paas/v4/chat/completions"
    )


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
    short = _trial(
        "trial-b", instruction="a much longer instruction text for trial b", window=window
    )
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
    attribution = attribute_calls([_call(1, [{"role": "user", "content": "same task"}])], twins)
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
    judgment = judge_trial(
        trial, [_call(1, [], texts=["hello", "never recorded"])], ambiguous=False
    )
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
    (job / "result.json").write_text(
        json.dumps({"id": f"job-{name}", "n_total_trials": len(trials)})
    )
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
                "started_at": spec.get("window", ("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"))[
                    0
                ],
                "finished_at": spec.get("window", ("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"))[
                    1
                ],
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
    assert (
        provenance["material_digest"]
        == f"sha256:{model_capture.sha256_file(tmp_path / 'cap' / 'calls.jsonl')}"
    )


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


def test_ollama_native_request_and_response() -> None:
    body = {"model": "qwen2.5:7b", "messages": [{"role": "user", "content": "do the thing"}]}
    assert request_message_pairs("ollama_chat", body) == [("user", "do the thing")]
    assert first_user_text("ollama_chat", body) == "do the thing"
    payload = {
        "model": "qwen2.5:7b",
        "message": {
            "role": "assistant",
            "content": "all done",
            "tool_calls": [{"function": {"name": "bash", "arguments": {"cmd": "ls"}}}],
        },
        "prompt_eval_count": 9,
        "eval_count": 3,
    }
    turns = extract_from_payload("ollama_chat", payload)
    assert turns.assistant_texts == ["all done"]
    assert turns.tool_calls == [{"id": None, "name": "bash", "arguments": '{"cmd": "ls"}'}]
    assert turns.usage == {"prompt_tokens": 9, "completion_tokens": 3}
    assert turns.model == "qwen2.5:7b"


def test_ollama_ndjson_stream_reassembly() -> None:
    raw = b"\n".join(
        [
            json.dumps(
                {"model": "qwen2.5:7b", "message": {"role": "assistant", "content": "hel"}}
            ).encode(),
            json.dumps({"message": {"role": "assistant", "content": "lo"}}).encode(),
            json.dumps({"done": True, "prompt_eval_count": 9, "eval_count": 3}).encode(),
        ]
    )
    turns = extract_ollama_ndjson(raw)
    assert turns.assistant_texts == ["hello"]
    assert turns.usage == {"prompt_tokens": 9, "completion_tokens": 3}
    assert turns.model == "qwen2.5:7b"


def test_zai_secret_proxy_chain_records_chat_and_scrubs_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.util
    import sys

    _StubUpstream.received = []
    stub = ThreadingHTTPServer(("127.0.0.1", 0), _StubUpstream)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    capture, recorder, _manifest = serve_capture(
        upstream=f"http://127.0.0.1:{stub.server_address[1]}",
        out_dir=tmp_path / "cap",
        bind="127.0.0.1",
        port=0,
    )
    capture_thread = threading.Thread(target=capture.serve_forever, daemon=True)
    capture_thread.start()
    provider_key = "zai-provider-key-sentinel-987654321"
    secret_file = tmp_path / "zai-key"
    secret_file.write_text(provider_key + "\n")
    secret_file.chmod(0o600)
    capability = "chain-capability-token"
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_SECRET_PATH", str(secret_file))
    monkeypatch.setenv(
        "EVALLAB_ZAI_OPENAPI_UPSTREAM", f"http://127.0.0.1:{capture.server_address[1]}"
    )
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_PROXY_CAPABILITY", capability)
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_ATTEMPT_ID", "trial-01")
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_MAX_REQUESTS", "5")
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_MAX_INPUT_TOKENS", "10000")
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_MAX_OUTPUT_TOKENS", "10000")
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_MAX_TOTAL_TOKENS", "20000")
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_MAX_COST_MICROS", "1000000")
    source = Path(__file__).resolve().parents[1] / "containers" / "zai_openapi_secret_proxy.py"
    spec = importlib.util.spec_from_file_location("chain_zai_openapi_proxy", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    proxy = module.serve(host="127.0.0.1", port=0)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{proxy.server_address[1]}/api/paas/v4/chat/completions",
            data=json.dumps(
                {"model": "zai/glm-5.3-flash", "messages": [{"role": "user", "content": "hi"}]}
            ).encode(),
            headers={"Content-Type": "application/json", "X-Evallab-Proxy-Capability": capability},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            assert response.status == 200
            assert json.loads(response.read())["choices"][0]["message"]["content"] == "done"
    finally:
        proxy.shutdown()
        capture.shutdown()
        stub.shutdown()
        recorder.close()
    assert _StubUpstream.received
    assert _StubUpstream.received[0]["auth"] == f"Bearer {provider_key}"
    records = [
        json.loads(line) for line in (tmp_path / "cap" / "calls.jsonl").read_text().splitlines()
    ]
    assert len(records) == 1
    record = records[0]
    assert record["request_body"]["model"] == "glm-5.3-flash"
    assert record["assistant_texts"] == ["done"]
    assert "authorization" not in {k.lower() for k in record["request_headers"]}
    assert provider_key not in json.dumps(record)


# ---------------------------------------------------------------------------
# HAR-126 G2: capture hop behind the self-hosted secret proxy.
#
# The round runs ``agent -> secret proxy -> capture serve -> Modal`` by
# pointing ``EVALLAB_MIMO_SELFHOSTED_UPSTREAM`` at the capture server. These
# tests prove at $0 (local fake upstream, no Modal) that the hop is a
# pass-through: request bytes unchanged, responses unchanged, bodies kept in
# ``calls.jsonl`` with per-job attribution via the runner-stamped route
# token.
# ---------------------------------------------------------------------------


class _RecordingStub(BaseHTTPRequestHandler):
    """Fake SGLang upstream keeping raw request bytes and path per call."""

    protocol_version = "HTTP/1.1"
    received: list[dict[str, Any]] = []
    lock = threading.Lock()

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode())
        except (ValueError, UnicodeDecodeError):
            payload = {}
        if self.path.endswith("/stream") or bool(payload.get("stream")):
            events = [
                {"model": "stub", "choices": [{"index": 0, "delta": {"content": "hel"}}]},
                {"choices": [{"index": 0, "delta": {"content": "lo"}}]},
                {
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                },
            ]
            body = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)
            body += b"data: [DONE]\n\n"
            content_type = "text/event-stream"
        else:
            body = json.dumps(
                {
                    "model": "stub",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "done"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                }
            ).encode()
            content_type = "application/json"
        with type(self).lock:
            type(self).received.append(
                {
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "host": self.headers.get("Host"),
                    "raw": raw,
                }
            )
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True


def _raw_post(port: int, path: str, raw: bytes, headers: dict[str, str]) -> bytes:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=raw, headers=headers, method="POST"
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def test_capture_passthrough_byte_identical_under_parallel_tokens(
    tmp_path: Path,
) -> None:
    """$0 pass-through proof: 20 same-task parallel trials through one capture.

    Every trial posts the *same* body (the G2 worst case: identical prompts
    defeat conversation-chaining attribution), each under its own
    ``/t/<token>/`` prefix. Asserts the fake upstream receives byte-identical
    bodies with and without the hop, clients get byte-identical responses,
    the upstream path is stripped, and ``calls.jsonl`` keeps both bodies
    with the right token while never recording auth headers.
    """
    import concurrent.futures

    _RecordingStub.received = []
    stub = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingStub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    server, recorder, _manifest = serve_capture(
        upstream=f"http://127.0.0.1:{stub.server_address[1]}",
        out_dir=tmp_path / "cap",
        bind="127.0.0.1",
        port=0,
    )
    capture_thread = threading.Thread(target=server.serve_forever, daemon=True)
    capture_thread.start()
    try:
        payload = {
            "model": "stub",
            "messages": [{"role": "user", "content": "do the thing"}],
            "temperature": 0.6,
            "top_p": 0.95,
            "top_k": 20,
            "chat_template_kwargs": {"enable_thinking": True},
        }
        raw = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json", "Authorization": "Bearer job-key"}
        control_response = _raw_post(stub.server_address[1], "/v1/chat/completions", raw, headers)
        assert len(_RecordingStub.received) == 1
        control_seen = _RecordingStub.received[0]["raw"]

        capture_port = server.server_address[1]
        tokens = [f"01ATTEMPT{i:02d}" for i in range(20)]

        def _one(token: str) -> bytes:
            return _raw_post(capture_port, f"/t/{token}/v1/chat/completions", raw, headers)

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            responses = list(pool.map(_one, tokens))
        assert responses and all(response == control_response for response in responses)
        # One streaming call: SSE bytes must also pass through untouched.
        stream_payload = {**payload, "stream": True}
        stream_raw = json.dumps(stream_payload).encode()
        control_stream = _raw_post(
            stub.server_address[1], "/v1/chat/completions", stream_raw, headers
        )
        via_stream = _raw_post(
            capture_port, "/t/01ATTEMPT00/v1/chat/completions", stream_raw, headers
        )
        assert via_stream == control_stream
        assert b"text/event-stream" not in via_stream
        assert b'"hel"' in via_stream and b"data: [DONE]" in via_stream
    finally:
        server.shutdown()
        stub.shutdown()
        recorder.close()
    # Upstream saw 1 control + 20 parallel + 2 streaming bodies, all identical.
    assert len(_RecordingStub.received) == 23
    assert all(
        entry["raw"] == control_seen or entry["raw"] == stream_raw
        for entry in _RecordingStub.received[1:]
    )
    assert all(entry["raw"] in (control_seen, stream_raw) for entry in _RecordingStub.received)
    # Stripped paths upstream; the provider key still reaches the upstream.
    # The capture drops the inbound Host, so the client sets it from the
    # upstream URL (Modal's edge routes on Host; 127.0.0.1:<capture-port>
    # must never leak through).
    stub_host = f"127.0.0.1:{stub.server_address[1]}"
    assert _RecordingStub.received[0]["path"] == "/v1/chat/completions"
    for entry in _RecordingStub.received[1:]:
        assert entry["path"] == "/v1/chat/completions"
        assert entry["auth"] == "Bearer job-key"
        assert entry["host"] == stub_host
    records = [
        json.loads(line)
        for line in (tmp_path / "cap" / "calls.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(records) == 21
    by_token: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        assert "authorization" not in {key.lower() for key in record["request_headers"]}
        assert "job-key" not in json.dumps(record)
        by_token.setdefault(record["route_token"], []).append(record)
    assert sorted(by_token) == sorted(tokens)
    assert all(len(calls) == 1 for token, calls in by_token.items() if token != "01ATTEMPT00")
    assert len(by_token["01ATTEMPT00"]) == 2
    plain = next(r for r in records if not r["response_sse"])
    assert json.loads(control_seen.decode()) == plain["request_body"]
    assert json.loads(control_response.decode()) == plain["response_body"]
    assert plain["assistant_texts"] == ["done"]
    streamed = next(r for r in records if r["response_sse"])
    assert streamed["assistant_texts"] == ["hello"]


def test_mimo_secret_proxy_stamps_token_and_stays_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Secret-proxy shaping is identical with and without the capture token.

    The proxy stamps ``/t/<token>/`` ahead of the pinned upstream path only
    when ``EVALLAB_CAPTURE_ROUTE_TOKEN`` is set; the shaped body (forced
    ``enable_thinking``/temperature/top_p/top_k) and the client response are
    byte-identical either way, and a malformed token fails open to the bare
    path instead of breaking the trial.
    """
    import importlib.util
    import sys

    _RecordingStub.received = []
    stub = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingStub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    provider_key = "mimo-provider-key-sentinel-135792468"
    secret_file = tmp_path / "mimo-key"
    secret_file.write_text(provider_key + "\n")
    secret_file.chmod(0o600)
    capability = "mimo-chain-capability"
    monkeypatch.setenv("EVALLAB_PROXY_PROVIDER", "mimo_selfhosted")
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_SECRET_PATH", str(secret_file))
    monkeypatch.setenv(
        "EVALLAB_MIMO_SELFHOSTED_UPSTREAM", f"http://127.0.0.1:{stub.server_address[1]}"
    )
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_PROXY_CAPABILITY", capability)
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_ATTEMPT_ID", "attempt-7")
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_MAX_REQUESTS", "10")
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_MAX_INPUT_TOKENS", "20000")
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_MAX_OUTPUT_TOKENS", "20000")
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_MAX_TOTAL_TOKENS", "40000")
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_MAX_COST_MICROS", "1000000")
    source = Path(__file__).resolve().parents[1] / "containers" / "zai_openapi_secret_proxy.py"
    spec = importlib.util.spec_from_file_location("mimo_capture_proxy", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    assert module._capture_prefixed_target("http://127.0.0.1:9/v1/chat/completions") == (
        "http://127.0.0.1:9/v1/chat/completions"
    )
    monkeypatch.setenv("EVALLAB_CAPTURE_ROUTE_TOKEN", "01ATTEMPT07")
    assert module._capture_prefixed_target("http://127.0.0.1:9/v1/chat/completions") == (
        "http://127.0.0.1:9/t/01ATTEMPT07/v1/chat/completions"
    )
    monkeypatch.setenv("EVALLAB_CAPTURE_ROUTE_TOKEN", "bad token!")
    assert module._capture_prefixed_target("http://127.0.0.1:9/v1/chat/completions") == (
        "http://127.0.0.1:9/v1/chat/completions"
    )
    proxy = module.serve(host="127.0.0.1", port=0)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    try:

        def _chat(token: str | None) -> bytes:
            if token is None:
                monkeypatch.delenv("EVALLAB_CAPTURE_ROUTE_TOKEN", raising=False)
            else:
                monkeypatch.setenv("EVALLAB_CAPTURE_ROUTE_TOKEN", token)
            body = json.dumps(
                {
                    "model": "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B",
                    "messages": [{"role": "user", "content": "hi"}],
                }
            ).encode()
            return _raw_post(
                proxy.server_address[1],
                "/v1/chat/completions",
                body,
                {"Content-Type": "application/json", "X-Evallab-Proxy-Capability": capability},
            )

        # NOTE: the per-trial capability is single-use per nonce-free POST
        # only when the proxy enforces nonces; plain capability POSTs repeat
        # fine, but the budget caps total requests (10 above).
        stamped_response = _chat("01ATTEMPT07")
        bare_response = _chat(None)
        assert stamped_response == bare_response
        assert json.loads(stamped_response)["choices"][0]["message"]["content"] == "done"
    finally:
        proxy.shutdown()
        stub.shutdown()
    assert len(_RecordingStub.received) == 2
    stamped, bare = _RecordingStub.received
    assert stamped["path"] == "/t/01ATTEMPT07/v1/chat/completions"
    assert bare["path"] == "/v1/chat/completions"
    # Same shaped body either way: the token rides the path, never the body.
    # (``max_tokens`` legitimately drops by the first call's reconciled
    # output usage: the proxy caps it at the remaining budget.)
    stamped_body = json.loads(stamped["raw"].decode())
    bare_body = json.loads(bare["raw"].decode())
    assert isinstance(stamped_body.pop("max_tokens"), int)
    assert isinstance(bare_body.pop("max_tokens"), int)
    assert stamped_body == bare_body
    assert stamped_body["model"] == "XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
    assert stamped_body["temperature"] == 0.6
    assert stamped_body["top_p"] == 0.95
    assert stamped_body["top_k"] == 20
    assert stamped_body["chat_template_kwargs"] == {"enable_thinking": True}
    assert stamped["auth"] == f"Bearer {provider_key}"


def test_link_attributes_calls_by_job_attempt_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Route tokens stamped by the runner resolve through job lab-metadata.

    The token is the job attempt id, which matches neither the trial name
    nor the trial id; ``link`` reads it from ``lab-metadata.json``
    ``provider_usage``. A second trial in the same job (shared attempt id)
    falls through to conversation chaining instead of going ambiguous.
    """
    job = _job_dir(
        tmp_path,
        "job-attempt",
        [
            {
                "trial_name": "trial-a",
                "window": ("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
                "trajectory": {
                    "session_id": "sess-a",
                    "steps": [
                        {"source": "user", "message": "do the thing alpha"},
                        {"source": "agent", "message": "all done"},
                    ],
                },
            }
        ],
    )
    (job / "lab-metadata.json").write_text(
        json.dumps({"provider_usage": {"attempt_id": "01ATTEMPTJOB"}})
    )
    capture = tmp_path / "cap"
    _write_calls(
        capture,
        [
            {
                "started_at": "2026-01-01T00:10:00Z",
                "ended_at": "2026-01-01T00:10:02Z",
                "method": "POST",
                "path": "/v1/chat/completions",
                "route_token": "01ATTEMPTJOB",
                "session_id": None,
                "request_body": {"messages": [{"role": "user", "content": "do the thing alpha"}]},
                "response_status": 200,
                "response_sse": False,
                "assistant_texts": ["all done"],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            }
        ],
    )
    receipt = link_capture(capture, job, derived_root=tmp_path / "derived")
    assert receipt["calls_total"] == 1
    assert receipt["calls_assigned"] == 1
    assert receipt["calls_unassigned"] == []
    (judgment,) = receipt["trials"]
    assert judgment["trial_name"] == "trial-a"
    assert judgment["verdict"] == "complete"


def test_runner_sets_capture_token_only_for_loopback_upstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner hands the proxy a route token iff capture opted in + loopback."""
    import evallab.runner as runner_module
    from evallab.execution_contracts import ProxyTrialLimits

    limits = ProxyTrialLimits(
        max_requests=50,
        max_input_tokens=100000,
        max_output_tokens=10000,
        max_total_tokens=110000,
        max_cost_micros=10000000,
    )
    kwargs: dict[str, Any] = {
        "provider": "mimo_selfhosted",
        "secret_path": tmp_path / "key",
        "capability": "capability-sentinel",
        "attempt_id": "01ATTEMPTJOB",
        "usage_path": tmp_path / "usage.json",
        "limits": limits,
        "timeout_seconds": 900.0,
        "mimo_native": "XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B",
    }
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_UPSTREAM", "http://127.0.0.1:8471")
    # No opt-in: loopback alone stamps nothing (dev fakes stay bare).
    monkeypatch.delenv("EVALLAB_MODEL_CAPTURE", raising=False)
    assert "EVALLAB_CAPTURE_ROUTE_TOKEN" not in runner_module._terminus_proxy_env(**kwargs)
    monkeypatch.setenv("EVALLAB_MODEL_CAPTURE", "1")
    env = runner_module._terminus_proxy_env(**kwargs)
    assert env["EVALLAB_CAPTURE_ROUTE_TOKEN"] == "01ATTEMPTJOB"
    assert env["EVALLAB_MIMO_SELFHOSTED_UPSTREAM"] == "http://127.0.0.1:8471"
    # A stray flag against the real Modal upstream still stamps nothing.
    monkeypatch.setenv(
        "EVALLAB_MIMO_SELFHOSTED_UPSTREAM",
        "https://p-makhnatch--evallab-mimo-v26-9b-mimoserver.us-east.modal.direct",
    )
    env = runner_module._terminus_proxy_env(**kwargs)
    assert "EVALLAB_CAPTURE_ROUTE_TOKEN" not in env
    monkeypatch.delenv("EVALLAB_MIMO_SELFHOSTED_UPSTREAM", raising=False)
    env = runner_module._terminus_proxy_env(**kwargs)
    assert "EVALLAB_CAPTURE_ROUTE_TOKEN" not in env


def _load_secret_proxy(monkeypatch: pytest.MonkeyPatch, name: str) -> Any:
    """Import the standalone secret-proxy container script under ``name``."""
    import importlib.util
    import sys

    source = Path(__file__).resolve().parents[1] / "containers" / "zai_openapi_secret_proxy.py"
    spec = importlib.util.spec_from_file_location(name, source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def _mimo_proxy_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, upstream_url: str) -> str:
    """Point a mimo_selfhosted secret proxy at ``upstream_url``; return the capability."""
    secret_file = tmp_path / "mimo-key"
    secret_file.write_text("mimo-provider-key-sentinel-135792468\n")
    secret_file.chmod(0o600)
    capability = "mimo-chain-capability"
    monkeypatch.setenv("EVALLAB_PROXY_PROVIDER", "mimo_selfhosted")
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_SECRET_PATH", str(secret_file))
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_UPSTREAM", upstream_url)
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_PROXY_CAPABILITY", capability)
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_ATTEMPT_ID", "attempt-7")
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_MAX_REQUESTS", "10")
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_MAX_INPUT_TOKENS", "20000")
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_MAX_OUTPUT_TOKENS", "20000")
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_MAX_TOTAL_TOKENS", "40000")
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_MAX_COST_MICROS", "1000000")
    return capability


def test_full_chain_proxy_capture_upstream_strips_token_and_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: real secret proxy -> capture -> fake upstream.

    The fake sees the canonical path, the upstream authority as Host (never
    the capture's loopback Host), and the shaped body; the client gets the
    same payload as a direct call; the record carries the route token.
    """
    _RecordingStub.received = []
    stub = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingStub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    capture, recorder, _manifest = serve_capture(
        upstream=f"http://127.0.0.1:{stub.server_address[1]}",
        out_dir=tmp_path / "cap",
        bind="127.0.0.1",
        port=0,
    )
    threading.Thread(target=capture.serve_forever, daemon=True).start()
    module = _load_secret_proxy(monkeypatch, "full_chain_proxy")
    capability = _mimo_proxy_env(
        monkeypatch, tmp_path, f"http://127.0.0.1:{capture.server_address[1]}"
    )
    monkeypatch.setenv("EVALLAB_CAPTURE_ROUTE_TOKEN", "01CHAINJOB")
    proxy = module.serve(host="127.0.0.1", port=0)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    try:
        body = json.dumps(
            {
                "model": "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B",
                "messages": [{"role": "user", "content": "hi"}],
            }
        ).encode()
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {capability}"}
        via_chain = _raw_post(proxy.server_address[1], "/v1/chat/completions", body, headers)
        direct = _raw_post(stub.server_address[1], "/v1/chat/completions", body, headers)
        # Same payload: the secret proxy re-serializes upstream JSON with
        # canonical separators, so whitespace differs by design; the capture
        # hop itself is byte-exact (proven by the passthrough test).
        assert json.loads(via_chain) == json.loads(direct)
        assert json.loads(via_chain)["choices"][0]["message"]["content"] == "done"
    finally:
        proxy.shutdown()
        capture.shutdown()
        stub.shutdown()
        recorder.close()
    assert len(_RecordingStub.received) == 2
    (seen,) = [entry for entry in _RecordingStub.received if b'"temperature"' in entry["raw"]]
    assert seen["path"] == "/v1/chat/completions"
    assert seen["host"] == f"127.0.0.1:{stub.server_address[1]}"
    shaped = json.loads(seen["raw"].decode())
    assert shaped["model"] == "XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
    assert shaped["temperature"] == 0.6
    assert shaped["chat_template_kwargs"] == {"enable_thinking": True}
    records = [
        json.loads(line)
        for line in (tmp_path / "cap" / "calls.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(records) == 1
    assert records[0]["route_token"] == "01CHAINJOB"
    assert records[0]["request_body"]["model"] == "XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"


def test_link_leaves_foreign_token_calls_unassigned(tmp_path: Path) -> None:
    """Two same-task jobs sharing one capture: no cross-job chaining.

    Job B links the shared capture; the call stamped with job A's attempt
    id stays unassigned even though the task text matches job B's trial.
    Job A links the same capture and claims it via route_token.
    """
    instruction = "do the shared thing " * 8
    window = ("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z")
    trajectory = {
        "session_id": "sess-x",
        "steps": [
            {"source": "user", "message": instruction},
            {"source": "agent", "message": "all done"},
        ],
    }
    job_a = _job_dir(
        tmp_path, "job-a", [{"trial_name": "trial-a", "window": window, "trajectory": trajectory}]
    )
    (job_a / "lab-metadata.json").write_text(
        json.dumps({"provider_usage": {"attempt_id": "01JOBAAAA"}})
    )
    job_b = _job_dir(
        tmp_path, "job-b", [{"trial_name": "trial-b", "window": window, "trajectory": trajectory}]
    )
    (job_b / "lab-metadata.json").write_text(
        json.dumps({"provider_usage": {"attempt_id": "01JOBBBBB"}})
    )
    capture = tmp_path / "cap"
    _write_calls(
        capture,
        [
            {
                "started_at": "2026-01-01T00:10:00Z",
                "ended_at": "2026-01-01T00:10:02Z",
                "method": "POST",
                "path": "/v1/chat/completions",
                "route_token": "01JOBAAAA",
                "session_id": None,
                "request_body": {"messages": [{"role": "user", "content": instruction}]},
                "response_status": 200,
                "response_sse": False,
                "assistant_texts": ["all done"],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            }
        ],
    )
    receipt_b = link_capture(capture, job_b, derived_root=tmp_path / "derived-b")
    assert receipt_b["calls_total"] == 1
    assert receipt_b["calls_assigned"] == 0
    assert receipt_b["calls_unassigned"] == [1]
    (judgment_b,) = receipt_b["trials"]
    assert judgment_b["captured_calls"] == 0
    receipt_a = link_capture(capture, job_a, derived_root=tmp_path / "derived-a")
    assert receipt_a["calls_assigned"] == 1
    (judgment_a,) = receipt_a["trials"]
    assert judgment_a["trial_name"] == "trial-a"
    assert judgment_a["verdict"] == "complete"


def test_capture_smoke_against_fake_upstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator smoke runs the real chain and reports shas, never secrets."""
    from evallab.model_capture import _forwarded_host, run_capture_smoke

    _RecordingStub.received = []
    stub = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingStub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("MIMO_SELFHOSTED_API_KEY", "smoke-fake-key-001")
        summary = run_capture_smoke(
            upstream=f"http://127.0.0.1:{stub.server_address[1]}",
            out_dir=tmp_path / "smoke",
        )
    finally:
        stub.shutdown()
    assert summary["status"] == 200
    assert summary["model"] == "stub"
    assert summary["calls"] == 1
    assert summary["forwarded_host"] == f"127.0.0.1:{stub.server_address[1]}"
    assert _forwarded_host("https://example.us-east.modal.direct") == "example.us-east.modal.direct"
    assert (
        _forwarded_host("https://example.us-east.modal.direct:443")
        == "example.us-east.modal.direct"
    )
    assert _forwarded_host("http://127.0.0.1:8471") == "127.0.0.1:8471"
    (seen,) = _RecordingStub.received
    assert seen["host"] == summary["forwarded_host"]
    assert seen["path"] == "/v1/chat/completions"
    assert seen["auth"] == "Bearer smoke-fake-key-001"
    assert summary["route_token"].startswith("smoke-")
    assert len(summary["request_sha256"]) == 64
    assert len(summary["response_sha256"]) == 64
    assert "smoke-fake-key-001" not in json.dumps(summary)
    record = json.loads((tmp_path / "smoke" / "calls.jsonl").read_text().splitlines()[0])
    assert record["route_token"] == summary["route_token"]
    assert "smoke-fake-key-001" not in json.dumps(record)
    assert (tmp_path / "smoke" / "smoke.json").is_file()


def test_capture_smoke_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing key and dead upstream both fail nonzero with no leak."""
    import pytest as _pytest

    from evallab.model_capture import SmokeError, run_capture_smoke

    monkeypatch.delenv("MIMO_SELFHOSTED_API_KEY", raising=False)
    with _pytest.raises(SmokeError, match="not set"):
        run_capture_smoke(upstream="http://127.0.0.1:9", out_dir=tmp_path / "s1")
    monkeypatch.setenv("MIMO_SELFHOSTED_API_KEY", "smoke-fake-key-001")
    with _pytest.raises(SmokeError):
        run_capture_smoke(upstream="http://127.0.0.1:9", out_dir=tmp_path / "s2")
