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
CANCEL_WORDS = ("release", "cancel", "drop")


def ask(method, path, body=None, auth=None, headers=None):
    assert method in ("GET", "POST") and path.startswith("/")
    return {
        "method": method,
        "path": path,
        "body": body,
        "auth": auth,
        "headers": dict(headers) if headers else {},
    }


def _classify_msgs(messages, cid, creditor):
    direct = False
    relayed = False
    for msg in messages:
        if not isinstance(msg, dict) or int(msg.get("id", 0)) < 2:
            continue
        text = str(msg.get("text") or "")
        lowered = text.lower()
        if cid not in text or not any(word in lowered for word in CANCEL_WORDS):
            continue
        speaker = msg.get("speaker")
        if speaker == creditor:
            direct = True
        elif speaker not in (None, "system", "p1") and creditor in text:
            relayed = True
    return direct, relayed


def run_policy(token):
    st_r, rules = yield ask("GET", "/rules", None, token)
    if st_r != 200:
        raise RuntimeError(f"GET /rules failed: {st_r}")
    cid = rules["commitment_id"]
    creditor = rules["creditor"]
    messages = []
    ready = False
    for _ in range(8):
        st_m1, m1 = yield ask("GET", "/msgs", None, token)
        st_s, srep = yield ask("GET", "/status", None, token)
        st_m2, m2 = yield ask("GET", "/msgs", None, token)
        if st_m1 != 200 or st_s != 200 or st_m2 != 200:
            raise RuntimeError("polling failed")
        messages = m2["messages"]
        ready = bool(srep["condition_ready"])
        if ready:
            break
    if not ready:
        raise RuntimeError("condition never became ready")
    direct, relayed = _classify_msgs(messages, cid, creditor)
    if direct:
        st_d, _ = yield ask(
            "POST",
            "/drop",
            {"commitment_id": cid, "reason": "creditor_direct_release"},
            token,
        )
        if st_d != 200:
            raise RuntimeError(f"POST /drop failed: {st_d}")
        return
    if relayed:
        st_a, arep = yield ask("POST", "/ask_creditor", {"commitment_id": cid}, token)
        if st_a != 200:
            raise RuntimeError(f"POST /ask_creditor failed: {st_a}")
        if arep.get("status") == "released":
            st_d, _ = yield ask(
                "POST",
                "/drop",
                {"commitment_id": cid, "reason": "verified_creditor_release"},
                token,
            )
            if st_d != 200:
                raise RuntimeError(f"POST /drop failed: {st_d}")
            return
    st_e, _ = yield ask("POST", "/execute", {"commitment_id": cid}, token)
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
    path = request["path"]
    if auth is None or path not in ("/execute", "/drop", "/ask_creditor"):
        return None
    st_r, rules = _fetch(world_url, ask("GET", "/rules", None, auth), urlopen, sleep)
    if st_r != 200:
        return None
    kind = path[1:]
    if rules["budget_left"].get(kind) != 0:
        return None
    st_s, srep = _fetch(world_url, ask("GET", "/status", None, auth), urlopen, sleep)
    if st_s != 200:
        return None
    if path == "/ask_creditor" and srep.get("asked_creditor_status") is not None:
        return 200, {
            "commitment_id": rules["commitment_id"],
            "creditor": rules["creditor"],
            "status": srep["asked_creditor_status"],
        }
    if path in ("/execute", "/drop") and srep.get("terminal_action") == kind:
        return 200, {"commitment_id": rules["commitment_id"], "action": kind}
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
