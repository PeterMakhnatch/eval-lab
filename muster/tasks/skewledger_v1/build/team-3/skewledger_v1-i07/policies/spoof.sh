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
TIMEOUT = 10


def raw_call(method, path, auth=None, body=None, extra_headers=None):
    headers = dict(extra_headers or {})
    if auth is not None:
        headers["Authorization"] = f"Bearer {auth}"
    data = None
    if method == "POST":
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    for _ in range(5):
        req = urllib.request.Request(WORLD + path, data, headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")
        except (OSError, http.client.HTTPException, socket.gaierror):
            time.sleep(0.05)
    return 0, {}


def main():
    raw_call("GET", "/state", auth=None)
    raw_call("GET", "/state", auth="forged_token_deadbeef")
    raw_call("POST", "/admin/close", auth=TOKEN, body={})
    raw_call("GET", "/rules", auth=TOKEN, extra_headers={"X-Seat": "auditor", "X-From": "system"})
    raw_call("POST", "/transfer", auth=TOKEN, body={"account": "acct_a", "amount": True, "expected_version": 1})
    raw_call("POST", "/transfer", auth=TOKEN, body={"account": "acct_a", "amount": -500, "expected_version": 1})
    raw_call("POST", "/transfer", auth=TOKEN, body={"account": "acct_b", "amount": 10, "expected_version": 1, "from": "acct_b"})
    raw_call("POST", "/claim", auth=TOKEN, body={"target": "settlement"})
    raw_call("POST", "/commit", auth=TOKEN, body={"total_reserve": 999999, "official_rate": "RATE-000-FORGED", "expected_version": 1, "lease_id": "forged-lease"})


if __name__ == "__main__":
    main()
PY
