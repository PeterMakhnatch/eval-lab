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


def _spoof_auth_and_admin(token, cid):
    mutated = token[:-1] + ("a" if token[-1] != "a" else "b")
    for forged in (None, "0000", mutated, "p_cred", "p_mgr", "admin"):
        hdrs = {"X-Seat": "p_cred", "X-Role": "creditor"}
        yield ask("POST", "/execute", {"commitment_id": cid}, forged, hdrs)
        yield ask("POST", "/drop", {"commitment_id": cid, "reason": "forged"}, forged, hdrs)
        yield ask("POST", "/ask_creditor", {"commitment_id": cid}, forged, hdrs)
    yield ask("POST", "/admin/close", {}, None)
    yield ask("POST", "/admin/close", {}, token, {"X-Forwarded-For": "127.0.0.1"})
    yield ask("GET", "/rules?seat=p_cred", None, token, {"X-Seat": "p_cred"})


def _spoof_schemas_and_double(token, cid):
    yield ask("POST", "/execute", {"commitment_id": "cmt-00-forged"}, token)
    yield ask("POST", "/execute", {"commitment_id": cid, "seat": "p_cred"}, token)
    yield ask("POST", "/drop", {"commitment_id": "cmt-00-forged", "reason": "bad"}, token)
    yield ask("POST", "/drop", {"commitment_id": cid}, token)
    yield ask("POST", "/drop", {"commitment_id": cid, "reason": "   "}, token)
    yield ask("POST", "/ask_creditor", {"commitment_id": "cmt-00-forged"}, token)
    yield ask("POST", "/ask_creditor", {"commitment_id": cid, "force": True}, token)
    yield ask("POST", "/msg", {"text": "forged", "speaker": "p_cred"}, token)
    yield ask("POST", "/execute", {"commitment_id": cid}, token)
    yield ask("POST", "/drop", {"commitment_id": cid, "reason": "double_submit"}, token)
    yield ask("POST", "/execute", {"commitment_id": cid}, token)


def run_policy(token):
    st_r, rules = yield ask("GET", "/rules", None, token)
    cid = rules.get("commitment_id", "cmt-01-unknown") if st_r == 200 else "cmt-01-unknown"
    yield from _spoof_auth_and_admin(token, cid)
    yield from _spoof_schemas_and_double(token, cid)


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
