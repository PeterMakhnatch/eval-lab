import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_BODY = 16 * 1024 * 1024
WEB_SEARCH_ID = "toolu_fable_websearch"
WEB_FETCH_ID = "toolu_fable_webfetch"
CANNED_CALLS = (
    (WEB_SEARCH_ID, "WebSearch", {"query": "fable canned web search"}),
    (WEB_FETCH_ID, "WebFetch", {"url": "https://example.invalid/fable", "prompt": "summarise"}),
)
TEXT_LIMIT = 400


class State:
    def __init__(self, mode: str, log: Path, replay: dict | None) -> None:
        assert mode in ("budget", "tools")
        assert mode != "budget" or replay is not None
        self.mode = mode
        self.log = log
        self.replay = replay
        self.t0 = time.monotonic()
        self.lock = threading.Lock()
        self.count = 0

    def write(self, row: dict) -> None:
        with self.lock:
            self.count += 1
            row = {"n": self.count, "t": round(time.monotonic() - self.t0, 3), **row}
            with self.log.open("a") as f:
                f.write(json.dumps(row) + "\n")


def tool_names(body: dict) -> list[str]:
    tools = body.get("tools") or []
    assert isinstance(tools, list)
    return [str(t.get("name") or t.get("type")) for t in tools[:256] if isinstance(t, dict)]


def tool_results(body: dict) -> list[dict]:
    messages = body.get("messages") or []
    assert isinstance(messages, list)
    if not messages or not isinstance(messages[-1], dict):
        return []
    content = messages[-1].get("content")
    if not isinstance(content, list):
        return []
    found = []
    for part in content[:64]:
        if not isinstance(part, dict) or part.get("type") != "tool_result":
            continue
        inner = part.get("content")
        if isinstance(inner, list):
            inner = " ".join(str(p.get("text", "")) for p in inner[:16] if isinstance(p, dict))
        found.append({"tool_use_id": part.get("tool_use_id"), "is_error": bool(part.get("is_error")),
                      "text": str(inner or "")[:TEXT_LIMIT]})
    return found


def answered_ids(body: dict) -> set[str]:
    messages = body.get("messages") or []
    assert isinstance(messages, list)
    found = set()
    for message in messages[-512:]:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        found.update(str(p.get("tool_use_id")) for p in content[:64]
                     if isinstance(p, dict) and p.get("type") == "tool_result")
    return found


def next_reply(body: dict) -> tuple[str, dict]:
    names = tool_names(body)
    if not names or any(n.startswith("web_search") for n in names):
        return "text", {"text": "ok"}
    answered = answered_ids(body)
    for tool_id, name, params in CANNED_CALLS:
        if tool_id not in answered:
            return "tool_use", {"id": tool_id, "name": name, "input": params}
    return "text", {"text": "done"}


def message_json(model: str, kind: str, block: dict) -> dict:
    content = [{"type": "text", "text": block["text"]}] if kind == "text" else [{"type": "tool_use", **block}]
    return {"id": "msg_fable", "type": "message", "role": "assistant", "model": model, "content": content,
            "stop_reason": "end_turn" if kind == "text" else "tool_use", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1}}


def sse_events(model: str, kind: str, block: dict) -> bytes:
    start = {"type": "message_start", "message": {**message_json(model, kind, block), "content": [],
                                                  "stop_reason": None}}
    if kind == "text":
        open_block = {"type": "text", "text": ""}
        delta = {"type": "text_delta", "text": block["text"]}
    else:
        open_block = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
        delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
    events = [
        ("message_start", start),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": open_block}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "usage": {"output_tokens": 1},
                           "delta": {"stop_reason": "end_turn" if kind == "text" else "tool_use",
                                     "stop_sequence": None}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


class Handler(BaseHTTPRequestHandler):
    state: State

    def log_message(self, format: str, *args: object) -> None:
        return

    def send(self, status: int, headers: dict, data: bytes) -> None:
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self.state.write({"method": "GET", "path": self.path})
        status = 200 if self.path == "/healthz" else 404
        self.send(status, {"content-type": "application/json"}, b'{"ok": true}')

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        assert 0 <= length <= MAX_BODY
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        row = {"method": "POST", "path": self.path, "model": body.get("model"), "stream": bool(body.get("stream")),
               "retry_count": self.headers.get("x-stainless-retry-count"), "tools": tool_names(body),
               "tool_results": tool_results(body), "max_tokens": body.get("max_tokens")}
        if self.path.split("?")[0].endswith("/count_tokens"):
            self.state.write({**row, "reply": "count_tokens"})
            self.send(200, {"content-type": "application/json"}, b'{"input_tokens": 1}')
            return
        if not self.path.split("?")[0].endswith("/v1/messages"):
            self.state.write({**row, "reply": 404})
            self.send(404, {"content-type": "application/json"}, b'{"error": "not found"}')
            return
        if self.state.mode == "budget":
            self.state.write({**row, "reply": self.state.replay["status"]})
            data = self.state.replay["body"].encode()
            self.send(int(self.state.replay["status"]), dict(self.state.replay["headers"]), data)
            return
        self.reply_tools(row, body)

    def reply_tools(self, row: dict, body: dict) -> None:
        kind, block = next_reply(body)
        model = str(body.get("model") or "fake")
        self.state.write({**row, "reply": kind, "tool": block.get("name")})
        if body.get("stream"):
            self.send(200, {"content-type": "text/event-stream", "request-id": "req_fable"},
                      sse_events(model, kind, block))
            return
        data = json.dumps(message_json(model, kind, block)).encode()
        self.send(200, {"content-type": "application/json", "request-id": "req_fable"}, data)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=("budget", "tools"))
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--replay", type=Path)
    args = parser.parse_args()
    assert 1024 < args.port < 65536
    replay = json.loads(args.replay.read_text()) if args.replay else None
    Handler.state = State(args.mode, args.log, replay)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
