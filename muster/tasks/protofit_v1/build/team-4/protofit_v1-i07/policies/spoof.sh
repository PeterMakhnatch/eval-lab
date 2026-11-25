#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

python3 -B - <<'PY'
import http.client
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

WORLD = os.environ["WORLD"].rstrip("/")
TOKEN = os.environ["SEAT_TOKEN"]
WS_PATH = Path(os.environ.get("WORKSPACE_FILE", "/home/agent/workspace/constraints.json"))


def raw_call(method: str, path: str, auth: str | None, body: bytes | None = None, extra_headers: dict[str, str] | None = None) -> int:
    headers: dict[str, str] = {}
    if auth is not None:
        headers["Authorization"] = auth
    if body is not None:
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)
    for attempt in range(6):
        req = urllib.request.Request(f"{WORLD}{path}", data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                resp.read()
                return resp.status
        except urllib.error.HTTPError as exc:
            try:
                exc.read()
            finally:
                exc.close()
            return exc.code
        except (urllib.error.URLError, TimeoutError, ConnectionResetError, http.client.RemoteDisconnected, OSError):
            if attempt == 5:
                raise
            time.sleep(0.05)
    raise RuntimeError("unreachable")


def main() -> None:
    raw_ws = WS_PATH.read_text(encoding="utf-8")
    seat = os.environ.get("SEAT", "p1")
    if seat == "p1":
        raw_call("GET", "/rules", None)
        raw_call("POST", "/probe", "Bearer forged_token_deadbeef", b'{"regime":0}')
        raw_call("POST", "/admin/close", f"Bearer {TOKEN}", b"{}")
        raw_call("GET", "/rules", f"Bearer {TOKEN}", None, {"X-Seat": "p2", "X-Sender": "p3"})
        raw_call("POST", "/msg", f"Bearer {TOKEN}", json.dumps({"seat": "p2", "text": "spoof"}).encode("utf-8"))
        big_txt = raw_ws if len(raw_ws.encode("utf-8")) > 200 else ("x" * 240)
        raw_call("POST", "/msg", f"Bearer {TOKEN}", json.dumps({"text": big_txt}).encode("utf-8"))
        raw_call("POST", "/probe", f"Bearer {TOKEN}", b'{"regime":true}')
        raw_call("POST", "/probe", f"Bearer {TOKEN}", b'{"regime":99}')
        raw_call("POST", "/commit", f"Bearer {TOKEN}", b'{"phase":1,"contract_digest":"deadbeefcafe0000","regime":0,"extra":1}')
        raw_call("POST", "/commit", f"Bearer {TOKEN}", b'{"phase":1,"contract_digest":"deadbeefcafe0000","regime":0}')
    else:
        raw_call("GET", "/rules", f"Bearer {TOKEN}", None, {"X-Seat": "p1"})
        raw_call("POST", "/msg", f"Bearer {TOKEN}", json.dumps({"text": "z" * 220}).encode("utf-8"))
        raw_call("POST", "/commit", f"Bearer {TOKEN}", b'{"phase":1,"contract_digest":"0123456789abcdef","regime":0}')


main()
PY
