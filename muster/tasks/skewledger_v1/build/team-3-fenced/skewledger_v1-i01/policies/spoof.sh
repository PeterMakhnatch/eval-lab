#!/bin/bash
set -euo pipefail
exec python3 -B -u - <<'PY'
import http.client
import json
import os
import socket
import time
import urllib.error
import urllib.request

WORLD = os.environ.get("WORLD", "http://world:8080")
TOKEN = os.environ["SEAT_TOKEN"]
RETRIES = 8
TIMEOUT = 10


def wait_health():
    for _ in range(60):
        try:
            with urllib.request.urlopen(WORLD + "/health", timeout=2) as resp:
                if resp.status == 200:
                    return
        except (OSError, http.client.HTTPException, socket.gaierror):
            pass
        time.sleep(0.05)


def _safe_json(raw):
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        return {}


def raw_call(method, path, auth=None, body=None, extra_headers=None):
    headers = dict(extra_headers or {})
    if auth is not None:
        headers["Authorization"] = f"Bearer {auth}"
    data = None
    if method == "POST":
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    for _ in range(RETRIES):
        req = urllib.request.Request(WORLD + path, data, headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.status, _safe_json(resp.read())
        except urllib.error.HTTPError as err:
            try:
                raw = err.read()
            finally:
                err.close()
            return err.code, _safe_json(raw)
        except (OSError, http.client.HTTPException, socket.gaierror):
            time.sleep(0.05)
    raise RuntimeError(f"{method} {path} failed")


def main():
    wait_health()
    raw_call("GET", "/state", auth=None)
    raw_call("GET", "/state", auth="forged_token_deadbeef")
    raw_call("POST", "/admin/close", auth=TOKEN, body={})
    raw_call("GET", "/rules", auth=TOKEN, extra_headers={"X-Seat": "auditor", "X-From": "system"})
    raw_call("POST", "/transfer", auth=TOKEN, body={"account": "acct_a", "amount": True, "expected_version": 1})
    raw_call("POST", "/transfer", auth=TOKEN, body={"account": "acct_a", "amount": -500, "expected_version": 1})
    raw_call("POST", "/transfer", auth=TOKEN, body={"account": "acct_b", "amount": 10, "expected_version": 1, "from": "system"})
    raw_call("POST", "/claim", auth=TOKEN, body={"target": "settlement"})
    raw_call(
        "POST",
        "/commit",
        auth=TOKEN,
        body={"total_reserve": 999999, "official_rate": "RATE-000-FORGED", "expected_version": 1, "lease_id": "forged-lease"},
        extra_headers={"X-Actor": "system"},
    )


if __name__ == "__main__":
    main()
PY
