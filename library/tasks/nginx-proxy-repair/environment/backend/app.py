#!/usr/bin/env python3
"""Acme status backend. Owned by the platform team; do not modify."""

import json
import os
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("BACKEND_PORT", "8081"))
NONCE = os.environ.get("BACKEND_NONCE") or secrets.token_hex(8)
ITEMS = [
    {"id": 1, "name": "alpha"},
    {"id": 2, "name": "beta"},
    {"id": 3, "name": "gamma"},
]


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/health":
            return self._json(200, {"status": "ok", "nonce": NONCE})
        if url.path == "/api/items":
            limit = parse_qs(url.query).get("limit", [""])[0]
            items = ITEMS[: int(limit)] if limit.isdigit() else ITEMS
            return self._json(200, items)
        return self._json(404, {"error": "not found"})


if __name__ == "__main__":
    print(f"status backend listening on 127.0.0.1:{PORT}", flush=True)
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
