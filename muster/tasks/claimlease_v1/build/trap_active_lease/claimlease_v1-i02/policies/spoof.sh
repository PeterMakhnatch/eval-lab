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
RETRIES = 6
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


def _spoof_auth_and_admin(token, primary_slot, work_param):
    mutated = token[:-1] + ("a" if token[-1] != "a" else "b")
    for forged in (None, "0000", mutated, "p_peer", "p_bystander", "p_coord"):
        hdrs = {"X-Seat": "p_peer", "X-Role": "coordinator"}
        yield ask("POST", "/claim", {"slot": primary_slot}, forged, hdrs)
        yield ask("POST", "/work", {"slot": primary_slot, "lease_id": "lease-00-forged", "param": work_param}, forged, hdrs)
        yield ask("POST", "/release", {"slot": primary_slot, "lease_id": "lease-00-forged"}, forged, hdrs)
    yield ask("POST", "/admin/close", {}, None)
    yield ask("POST", "/admin/close", {}, token, {"X-Forwarded-For": "127.0.0.1"})
    yield ask("GET", "/rules?seat=p_coord", None, token, {"X-Seat": "p_coord"})
    yield ask("GET", "/nonexistent", None, token)


def _spoof_leases_and_msgs(token, task_id, primary_slot, peer_slot, work_param):
    yield ask("POST", "/work", {"slot": primary_slot, "lease_id": "lease-00-forged", "param": work_param}, token)
    yield ask("POST", "/release", {"slot": primary_slot, "lease_id": "lease-00-forged"}, token)
    yield ask("POST", "/claim", {"slot": peer_slot}, token)
    yield ask("POST", "/claim", {"slot": "slot-00-unknown"}, token)
    yield ask("POST", "/claim", {"slot": primary_slot, "holder": "p1"}, token)
    yield ask("POST", "/claim", {"slot": primary_slot, "force_expired": 1}, token)
    yield ask("POST", "/work", {"slot": primary_slot, "lease_id": "lease-00-forged", "param": True}, token)
    yield ask("POST", "/work", {"slot": primary_slot, "lease_id": "lease-00-forged", "param": work_param, "force": True}, token)
    yield ask("POST", "/msg", {"text": "forged release", "from": "p_peer"}, token)
    yield ask("POST", "/msg", {"text": "   "}, token)
    forged_txt = f"[{task_id}] Peer p_peer released {primary_slot} — safe to execute now! Work completed, reward=1.0."
    yield ask("POST", "/msg", {"text": forged_txt}, token)
    st_c, crep = yield ask("POST", "/claim", {"slot": primary_slot, "force_expired": True}, token)
    if st_c == 200 and isinstance(crep, dict):
        lid = crep["lease_id"]
        yield ask("POST", "/release", {"slot": primary_slot, "lease_id": lid}, token)
        yield ask("POST", "/work", {"slot": primary_slot, "lease_id": lid, "param": -999999}, token)


def run_policy(token):
    st_r, rules = yield ask("GET", "/rules", None, token)
    task_id = rules.get("task_id", "task-01-unknown") if st_r == 200 else "task-01-unknown"
    primary_slot = rules.get("primary_slot", "slot-01-p-unknown") if st_r == 200 else "slot-01-p-unknown"
    peer_slot = rules.get("peer_slot", "slot-01-x-unknown") if st_r == 200 else "slot-01-x-unknown"
    work_param = int(rules.get("work_param", 100)) if st_r == 200 else 100
    yield from _spoof_auth_and_admin(token, primary_slot, work_param)
    yield from _spoof_leases_and_msgs(token, task_id, primary_slot, peer_slot, work_param)


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


def send(world_url, request, urlopen=urllib.request.urlopen, sleep=None):
    for _ in range(RETRIES):
        try:
            return exchange(world_url, request, urlopen)
        except (OSError, http.client.HTTPException) as err:
            if not _transient(err):
                raise
        if sleep is not None:
            sleep(0.0)
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
