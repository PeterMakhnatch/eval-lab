"""Loopback sub-LLM transport bridge (NO local hook, NO local accounting).

Speaks the released ``SubLLMProxy`` HTTP contract on environment loopback
so the worker's ``llm_query``/``llm_query_batched`` have an endpoint to
call. This bridge is a VERBATIM forwarder, not a replacement endpoint:
every request is written to ``subreq/sNNNNNN.json`` and the response is
whatever the host-side forwarder (inside ``ManagedReplBackend``) obtained
from the ACTUAL env-owned proxy at the ``proxy_url`` the environment
supplied. Hook invocation, rollout binding and accounting therefore happen
in the real env proxy and its state -- never here.

Usage: python3 bridge.py <work_dir> <port>
"""

from __future__ import annotations

import itertools
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

counter = itertools.count(1)


class Handler(BaseHTTPRequestHandler):
    server_version = "ManagedBridge/0"

    def log_message(self, *args: object) -> None:
        pass

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            body = {}
        return body if isinstance(body, dict) else {}

    def _send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        parts = self.path.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "rollout":
            self._send_json({"error": "unknown route"}, status=404)
            return
        rollout_id, route = parts[1], parts[2]
        if route not in ("llm_query", "llm_query_batched"):
            self._send_json({"error": "bad route"}, status=404)
            return
        server = self.server
        root: Path = server.work_dir  # type: ignore[attr-defined]
        subreq = root / "subreq"
        subresp = root / "subresp"
        subreq.mkdir(parents=True, exist_ok=True)
        subresp.mkdir(parents=True, exist_ok=True)
        name = f"s{next(counter):06d}"
        envelope = {
            "rollout_id": rollout_id,
            "route": route,
            "body": self._read_body(),
        }
        (subreq / f"{name}.json").write_text(json.dumps(envelope), encoding="utf-8")
        target = subresp / f"{name}.json"
        deadline = time.monotonic() + 110.0
        while not target.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not target.exists():
            self._send_json({"error": "sub-llm forward timeout"}, status=504)
            return
        self._send_json(json.loads(target.read_text(encoding="utf-8")))


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/opt/worker")
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8765
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.work_dir = root  # type: ignore[attr-defined]
    server.daemon_threads = True
    print(f"BRIDGE_READY port={port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
