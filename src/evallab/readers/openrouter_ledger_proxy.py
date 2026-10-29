"""Loopback OpenRouter proxy: one ledger row per paid call, served-model check, spend cap.

Some products (Phoenix PXI) call an OpenAI-compatible endpoint from inside their own
server and never report usage back. Point them at this proxy instead of OpenRouter: it
injects the real key (so the product never holds it), asks OpenRouter to include cost in
the response usage, denies providers that retain/train on prompts, forwards the request,
and appends one JSON line per call with the requested and served model, tokens and the
cost OpenRouter reported. Once the recorded cost reaches the cap it answers HTTP 402 and
forwards nothing.

Run: ``keys run -- python -m evallab.readers.openrouter_ledger_proxy --port 6047
--calls calls.jsonl --cap-usd 5``. Streaming and plain responses are both relayed
untouched; the ledger only reads the bytes it relays.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

UPSTREAM_HOST = "openrouter.ai"
UPSTREAM_PREFIX = "/api"
_HOP = {"connection", "keep-alive", "transfer-encoding", "content-length", "host", "authorization"}


def parse_response(body: bytes) -> dict[str, Any]:
    """Served model, id, usage and cost from a JSON or SSE response body (either API)."""
    found: dict[str, Any] = {"served": None, "id": None, "provider": None, "usage": None}
    text = body.decode("utf-8", "replace")
    documents: list[Any] = []
    if text.lstrip().startswith("{"):
        with contextlib.suppress(ValueError):
            documents.append(json.loads(text))
    else:
        for line in text.splitlines():
            if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]"):
                try:
                    documents.append(json.loads(line[5:]))
                except ValueError:
                    continue
    for document in documents:
        if not isinstance(document, Mapping):
            continue
        inner = document.get("response") if isinstance(document.get("response"), Mapping) else {}
        for source in (document, inner):
            found["served"] = source.get("model") or found["served"]
            found["id"] = source.get("id") or found["id"]
            found["provider"] = source.get("provider") or found["provider"]
            if isinstance(source.get("usage"), Mapping):
                found["usage"] = source["usage"]
    usage = found["usage"] or {}
    cost = usage.get("cost")
    return {
        "served": found["served"],
        "generation_id": found["id"],
        "provider": found["provider"],
        "input_tokens": usage.get("input_tokens", usage.get("prompt_tokens")),
        "output_tokens": usage.get("output_tokens", usage.get("completion_tokens")),
        "cost_usd": float(cost) if isinstance(cost, int | float) else None,
    }


class Ledger:
    def __init__(self, path: Path, cap_usd: float) -> None:
        self.path = path
        self.cap_usd = cap_usd
        self.lock = threading.Lock()
        self.usd = 0.0
        self.calls = 0

    def capped(self) -> bool:
        with self.lock:
            return self.usd >= self.cap_usd

    def append(self, row: Mapping[str, Any]) -> None:
        with self.lock:
            self.calls += 1
            self.usd += row.get("cost_usd") or 0.0
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, sort_keys=True) + "\n")


def make_handler(ledger: Ledger, api_key: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the ledger is the log
            return

        def _body(self) -> bytes:
            if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                chunks = []
                while (size := int(self.rfile.readline().strip() or b"0", 16)) > 0:
                    chunks.append(self.rfile.read(size))
                    self.rfile.readline()
                self.rfile.readline()
                return b"".join(chunks)
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def _reply(self, status: int, payload: Mapping[str, Any]) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            self._forward(b"")

        def do_POST(self) -> None:
            self._forward(self._body())

        def _forward(self, body: bytes) -> None:
            requested = None
            if body:
                try:
                    request = json.loads(body)
                except ValueError:
                    request = None
                if isinstance(request, dict):
                    requested = request.get("model")
                    request["usage"] = {"include": True}
                    request.setdefault("provider", {})["data_collection"] = "deny"
                    body = json.dumps(request).encode()
            if ledger.capped():
                self._reply(
                    402,
                    {"error": {"message": f"spend cap ${ledger.cap_usd:.2f} reached", "code": 402}},
                )
                return
            headers = {k: v for k, v in self.headers.items() if k.lower() not in _HOP}
            headers["Authorization"] = f"Bearer {api_key}"
            headers["Content-Length"] = str(len(body))
            connection = http.client.HTTPSConnection(UPSTREAM_HOST, timeout=600)
            path = (
                self.path if self.path.startswith(UPSTREAM_PREFIX) else UPSTREAM_PREFIX + self.path
            )
            connection.request(self.command, path, body=body or None, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in _HOP:
                    self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            seen = bytearray()
            while chunk := response.read(8192):
                seen += chunk
                self.wfile.write(chunk)
            self.close_connection = True
            if requested is not None:
                row = parse_response(bytes(seen))
                ledger.append(
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "path": self.path,
                        "status": response.status,
                        "requested": requested,
                        **row,
                        "served_matches_requested": bool(
                            row["served"]
                            and str(row["served"])
                            .split("/")[-1]
                            .startswith(str(requested).split("/")[-1])
                        ),
                    }
                )
            connection.close()

    return Handler


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--calls", type=Path, required=True)
    parser.add_argument("--cap-usd", type=float, required=True)
    args = parser.parse_args(argv)
    api_key = os.environ["OPENROUTER_API_KEY"]
    ledger = Ledger(args.calls, args.cap_usd)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(ledger, api_key))
    server.daemon_threads = True
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
