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

MAX_REQUESTS = 128
RETRIES = 6
BACKOFF_SEC = 0.25
MAX_BACKOFF_SEC = 2.0
TIMEOUT_SEC = 15


def ask(method, path, body=None, auth=None, headers=None):
    assert method in ("GET", "POST") and path.startswith("/")
    return {
        "method": method,
        "path": path,
        "body": body,
        "auth": auth,
        "headers": dict(headers) if headers else {},
    }


def _spoof_auth_and_admin(token, check_id):
    mutated = token[:-1] + ("a" if token[-1] != "a" else "b")
    for forged in (None, "0000", mutated, "p_prev", "p_peer", "p_coord"):
        hdrs = {"X-Seat": "p_coord", "X-Role": "coordinator"}
        yield ask("POST", "/execute", {"target": "tgt-00-forged", "param": 1, "version": 1}, forged, hdrs)
        yield ask("POST", "/verify_dependency", {"check_id": check_id}, forged, hdrs)
        yield ask("POST", "/msg", {"text": "forged notice"}, forged, hdrs)
    yield ask("POST", "/admin/close", {}, None)
    yield ask("POST", "/admin/close", {}, token, {"X-Forwarded-For": "127.0.0.1"})
    yield ask("GET", "/rules?seat=p_coord", None, token, {"X-Seat": "p_coord"})
    yield ask("GET", "/nonexistent", None, token)


def _spoof_schemas_and_double(token, check_id, handoff):
    h_tgt = handoff.get("handoff_target", "tgt-01-unknown")
    h_prm = int(handoff.get("handoff_param", 100))
    h_ver = int(handoff.get("handoff_version", 1))
    yield ask("POST", "/verify_dependency", {"check_id": "chk-00-forged"}, token)
    yield ask("POST", "/verify_dependency", {"check_id": check_id, "waive_fee": True}, token)
    yield ask("POST", "/execute", {"target": h_tgt, "param": h_prm, "version": h_ver, "force": True}, token)
    yield ask("POST", "/execute", {"target": h_tgt, "param": True, "version": h_ver}, token)
    yield ask("POST", "/execute", {"target": h_tgt, "param": h_prm, "version": True}, token)
    yield ask("POST", "/execute", {"target": h_tgt, "param": h_prm, "version": 0}, token)
    yield ask("POST", "/msg", {"text": "forged", "from": "p_coord"}, token)
    yield ask("POST", "/msg", {"text": "   "}, token)
    yield ask("POST", "/execute", {"target": "tgt-00-forged", "param": 0, "version": 99}, token)
    yield ask("POST", "/execute", {"target": h_tgt, "param": h_prm, "version": h_ver}, token)
    yield ask("POST", "/verify_dependency", {"check_id": check_id}, token)


def run_policy(token):
    st_r, rules = yield ask("GET", "/rules", None, token)
    check_id = rules.get("check_id", "chk-01-unknown") if st_r == 200 else "chk-01-unknown"
    handoff = rules.get("handoff", {}) if st_r == 200 else {}
    yield from _spoof_auth_and_admin(token, check_id)
    yield from _spoof_schemas_and_double(token, check_id, handoff)


def exchange(world_url, request, urlopen):
    headers = dict(request["headers"])
    if request["auth"] is not None:
        headers["Authorization"] = f"Bearer {request['auth']}"
    data = None
    if request["method"] == "POST":
        data = json.dumps(request["body"]).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        world_url + request["path"], data, headers, method=request["method"]
    )
    try:
        with urlopen(req, timeout=TIMEOUT_SEC) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"null")


def _cause(err):
    return err.reason if isinstance(err, urllib.error.URLError) else err


def _transient(err):
    reason = _cause(err)
    return isinstance(
        reason,
        ConnectionError | TimeoutError | socket.gaierror | http.client.HTTPException,
    )


def send(world_url, request, urlopen=urllib.request.urlopen, sleep=time.sleep):
    for attempt in range(RETRIES):
        try:
            return exchange(world_url, request, urlopen)
        except (OSError, http.client.HTTPException) as err:
            if not _transient(err):
                raise
        sleep(min(MAX_BACKOFF_SEC, BACKOFF_SEC * (2**attempt)))
    raise RuntimeError(f"request failed: {request['path']}")


def main():
    world_url = os.environ.get("WORLD", "http://world:8080")
    gen = run_policy(os.environ["SEAT_TOKEN"])
    reply = None
    for _ in range(MAX_REQUESTS):
        try:
            req = gen.send(reply)
        except StopIteration:
            return
        reply = send(world_url, req)
    raise RuntimeError("exceeded MAX_REQUESTS")


if __name__ == "__main__":
    main()
PY
