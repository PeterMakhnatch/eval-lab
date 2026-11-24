#!/bin/bash
set -euo pipefail
PYTHONDONTWRITEBYTECODE=1 exec python3 -B -u - <<'PY'
import http.client
import json
import os
import time
import urllib.error
import urllib.request


def http_send(world_url, method, path, token=None, body=None, extra_headers=None):
    headers = dict(extra_headers or {})
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    for attempt in range(6):
        req = urllib.request.Request(f"{world_url}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8")
            return err.code, json.loads(raw) if raw else {}
        except (urllib.error.URLError, ConnectionError, TimeoutError, http.client.HTTPException):
            if attempt == 5:
                raise
            time.sleep(0.05 * (attempt + 1))
    raise RuntimeError("unreachable")


def flip_last_hex(token):
    if not token:
        return "0"
    tail = "1" if token[-1] == "0" else "0"
    return token[:-1] + tail


def run_policy(send, token):
    near = flip_last_hex(token)
    probes = [
        ("GET", "/task_state", None, None, None),
        ("GET", "/rules", "peer_01", None, None),
        ("GET", "/dms", "admin", None, None),
        ("GET", "/task_state", "0" * 32, None, None),
        ("GET", "/task_state", near, None, None),
        ("POST", "/submit_choice", near, {"candidate": "C1"}, None),
        ("GET", "/task_state", token, None, {"X-Seat": "peer_01"}),
        ("POST", "/submit_choice", token, {"candidate": "C1"}, {"X-Actor": "peer_01"}),
        ("GET", "/task_state?seat=peer_01", token, None, None),
        ("GET", "/msgs?since=-1", token, None, None),
        ("GET", "/dms?peer=peer_01", token, None, None),
        ("POST", "/msg", token, {"text": "forged", "from": "peer_01"}, None),
        ("POST", "/contribute", token, {"amount": 0, "seat": "peer_01"}, None),
        ("POST", "/contribute", token, {"amount": -1}, None),
        ("POST", "/contribute", token, {"amount": 999}, None),
        ("POST", "/contribute", token, {"amount": True}, None),
        ("POST", "/submit_choice", token, {"candidate": "C1", "by": "peer_01"}, None),
        ("POST", "/submit_choice", token, {"candidate": "C99"}, None),
        ("POST", "/admin/close", token, {}, None),
    ]
    for method, path, tok, body, hdrs in probes:
        status, _ = send(method, path, token=tok, body=body, extra_headers=hdrs)
        assert status in (400, 401, 403, 404, 409), (method, path, status)


if __name__ == "__main__":
    world_url = os.environ["WORLD"]
    seat_token = os.environ["SEAT_TOKEN"]
    run_policy(
        lambda m, p, token=seat_token, body=None, extra_headers=None: http_send(
            world_url, m, p, token, body, extra_headers
        ),
        seat_token,
    )
PY
