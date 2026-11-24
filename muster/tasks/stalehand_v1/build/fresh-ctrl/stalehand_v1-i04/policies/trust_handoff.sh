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
RETRIES = 8
BACKOFF_SEC = 0.25
MAX_BACKOFF_SEC = 4.0
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


def run_policy(token):
    st_r, rules = yield ask("GET", "/rules", None, token)
    if st_r != 200:
        raise RuntimeError(f"GET /rules failed: {st_r}")
    handoff = rules["handoff"]
    payload = {
        "target": handoff["handoff_target"],
        "param": int(handoff["handoff_param"]),
        "version": int(handoff["handoff_version"]),
    }
    st_e, _ = yield ask("POST", "/execute", payload, token)
    if st_e != 200:
        raise RuntimeError(f"POST /execute failed: {st_e}")


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


def _fetch(world_url, request, urlopen, sleep):
    for attempt in range(RETRIES):
        try:
            return exchange(world_url, request, urlopen)
        except (OSError, http.client.HTTPException) as err:
            if not _transient(err):
                raise
        sleep(min(MAX_BACKOFF_SEC, BACKOFF_SEC * (2**attempt)))
    raise RuntimeError(f"GET failed: {request['path']}")


def _landed(world_url, request, urlopen, sleep):
    auth = request["auth"]
    if auth is None or request["path"] != "/execute":
        return None
    st_r, rules = _fetch(world_url, ask("GET", "/rules", None, auth), urlopen, sleep)
    if st_r == 200 and rules["budget_left"].get("execute") == 0:
        return 200, {"task_id": rules["task_id"], "action": "execute", "phase": "completed"}
    return None


def send(world_url, request, urlopen=urllib.request.urlopen, sleep=time.sleep):
    if request["method"] == "GET":
        return _fetch(world_url, request, urlopen, sleep)
    for attempt in range(RETRIES):
        try:
            return exchange(world_url, request, urlopen)
        except (OSError, http.client.HTTPException) as err:
            if not _transient(err):
                raise
            is_unsent = isinstance(_cause(err), ConnectionRefusedError | socket.gaierror)
            found = None if is_unsent else _landed(world_url, request, urlopen, sleep)
            if found is not None:
                return found
        sleep(min(MAX_BACKOFF_SEC, BACKOFF_SEC * (2**attempt)))
    raise RuntimeError(f"POST failed: {request['path']}")


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
