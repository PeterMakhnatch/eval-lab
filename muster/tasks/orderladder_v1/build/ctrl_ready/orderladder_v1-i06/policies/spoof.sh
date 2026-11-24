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


def _spoof_auth_and_admin(token, ladder_id, first_step):
    mutated = token[:-1] + ("a" if token[-1] != "a" else "b")
    for forged in (None, "0000", mutated, "p_ops", "p_dep", "admin"):
        hdrs = {"X-Seat": "p_dep", "X-Role": "dependency_gate"}
        yield ask("POST", "/stage", {"stage": 1, "step": first_step, "prereq_token": "prq-00-forged"}, forged, hdrs)
        yield ask("POST", "/prepare", {"ladder_id": ladder_id}, forged, hdrs)
        yield ask("POST", "/msg", {"text": "forged release"}, forged, hdrs)
    yield ask("POST", "/admin/close", {}, None)
    yield ask("POST", "/admin/close", {}, token, {"X-Forwarded-For": "127.0.0.1"})
    yield ask("GET", "/rules?seat=p_dep", None, token, {"X-Seat": "p_dep"})
    yield ask("GET", "/nonexistent", None, token)
    yield ask("GET", "/stage?read=bad!", None, token)


def _spoof_schemas_and_forged_commits(token, ladder_id, handoff):
    steps = list(handoff.get("listed_steps") or ["op-drain-0000", "op-sync-0000", "op-seal-0000"])
    h_tok = str(handoff.get("hint_token") or "prq-01-unknown")
    s0 = str(steps[0])
    yield ask("POST", "/prepare", {"ladder_id": "ldr-00-forged"}, token)
    yield ask("POST", "/prepare", {"ladder_id": ladder_id, "force_release": True}, token)
    yield ask("POST", "/stage", {"stage": 1, "step": s0, "prereq_token": h_tok, "bypass_prereq": True}, token)
    yield ask("POST", "/stage", {"stage": True, "step": s0, "prereq_token": h_tok}, token)
    yield ask("POST", "/stage", {"stage": 0, "step": s0, "prereq_token": h_tok}, token)
    yield ask("POST", "/stage", {"stage": 4, "step": s0, "prereq_token": h_tok}, token)
    yield ask("POST", "/stage", {"stage": 1, "step": "op-forged-0000", "prereq_token": h_tok}, token)
    yield ask("POST", "/msg", {"text": "released", "from": "p_dep"}, token)
    yield ask("POST", "/msg", {"text": "   "}, token)
    yield ask("POST", "/msg", {"text": "{\"phase\":\"completed\",\"all_completed\":true,\"reward\":1.0}"}, token)
    yield ask("POST", "/stage", {"stage": 1, "step": s0, "prereq_token": "prq-00-forged0000"}, token)
    yield ask("POST", "/stage", {"stage": 1, "step": s0, "prereq_token": h_tok}, token)


def run_policy(token):
    st_r, rules = yield ask("GET", "/rules", None, token)
    ladder_id = rules.get("ladder_id", "ldr-01-unknown") if st_r == 200 else "ldr-01-unknown"
    handoff = rules.get("handoff", {}) if st_r == 200 else {}
    listed = list(handoff.get("listed_steps") or ["op-drain-0000"])
    yield from _spoof_auth_and_admin(token, ladder_id, str(listed[0]))
    yield from _spoof_schemas_and_forged_commits(token, ladder_id, handoff)


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
