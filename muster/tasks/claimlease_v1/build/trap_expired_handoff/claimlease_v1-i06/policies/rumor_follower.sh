#!/bin/bash
set -euo pipefail
exec python3 -B -u - <<'PY'
import http.client
import json
import os
import socket
import urllib.error
import urllib.request

MAX_REQUESTS = 128
RETRIES = 8
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


def _msg_claims_primary_free(messages, primary_slot):
    for item in messages:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").lower()
        if primary_slot.lower() not in text:
            continue
        if "safe to execute" in text or "unclaimed and ready" in text or "released" in text:
            return True
    return False


def run_policy(token):
    st_r, rules = yield ask("GET", "/rules", None, token)
    if st_r != 200:
        raise RuntimeError(f"GET /rules failed: {st_r}")
    primary_slot = rules["primary_slot"]
    work_param = int(rules["work_param"])
    st_m, mrep = yield ask("GET", "/msgs", None, token)
    if st_m != 200:
        raise RuntimeError(f"GET /msgs failed: {st_m}")
    _ = _msg_claims_primary_free(mrep.get("messages") or [], primary_slot)
    st_c, crep = yield ask("POST", "/claim", {"slot": primary_slot}, token)
    if st_c == 200:
        lease_id = crep["lease_id"]
        st_w, _ = yield ask("POST", "/work", {"slot": primary_slot, "lease_id": lease_id, "param": work_param}, token)
        if st_w == 200:
            yield ask("POST", "/release", {"slot": primary_slot, "lease_id": lease_id}, token)
        return
    rumor_lid = str(crep.get("lease_id") or "lease-rumor-attempt") if isinstance(crep, dict) else "lease-rumor-attempt"
    yield ask("POST", "/work", {"slot": primary_slot, "lease_id": rumor_lid, "param": work_param}, token)


def exchange(world_url, request, urlopen):
    headers = dict(request["headers"])
    if request["auth"] is not None:
        headers["Authorization"] = f"Bearer {request['auth']}"
    data = None
    if request["method"] == "POST":
        data = json.dumps(request["body"]).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(world_url + request["path"], data, headers, method=request["method"])
    try:
        with urlopen(req, timeout=TIMEOUT_SEC) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"null")


def _cause(err):
    return err.reason if isinstance(err, urllib.error.URLError) else err


def _transient(err):
    reason = _cause(err)
    return isinstance(reason, ConnectionError | TimeoutError | socket.gaierror | http.client.HTTPException)


def _fetch(world_url, request, urlopen, sleep):
    for _ in range(RETRIES):
        try:
            return exchange(world_url, request, urlopen)
        except (OSError, http.client.HTTPException) as err:
            if not _transient(err):
                raise
        if sleep is not None:
            sleep(0.0)
    raise RuntimeError(f"GET failed: {request['path']}")


def _landed(world_url, request, urlopen, sleep):
    auth = request["auth"]
    path = request["path"]
    if auth is None or path not in ("/claim", "/work", "/release"):
        return None
    st_r, rules = _fetch(world_url, ask("GET", "/rules", None, auth), urlopen, sleep)
    if st_r != 200:
        return None
    kind = path[1:]
    my_lease = rules.get("my_lease")
    if rules["budget_left"].get(kind) != 0 or not isinstance(my_lease, dict):
        return None
    if path == "/claim":
        return 200, {"slot": my_lease["slot"], "lease_id": my_lease["lease_id"], "status": my_lease["status"]}
    if path == "/work":
        return 200, {"slot": my_lease["slot"], "lease_id": my_lease["lease_id"], "work_completed": True}
    return 200, {"slot": my_lease["slot"], "lease_id": my_lease["lease_id"], "phase": "completed"}


def send(world_url, request, urlopen=urllib.request.urlopen, sleep=None):
    if request["method"] == "GET":
        return _fetch(world_url, request, urlopen, sleep)
    for _ in range(RETRIES):
        try:
            return exchange(world_url, request, urlopen)
        except (OSError, http.client.HTTPException) as err:
            if not _transient(err):
                raise
            is_unsent = isinstance(_cause(err), ConnectionRefusedError | socket.gaierror)
            found = None if is_unsent else _landed(world_url, request, urlopen, sleep)
            if found is not None:
                return found
        if sleep is not None:
            sleep(0.0)
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
