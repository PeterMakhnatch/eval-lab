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


def _stage_entry(stage_doc, idx):
    for item in stage_doc.get("stages") or []:
        if item.get("stage") == idx:
            return item
    raise RuntimeError(f"missing stage {idx}")


def run_policy(token):
    st_r, rules = yield ask("GET", "/rules", None, token)
    if st_r != 200:
        raise RuntimeError(f"GET /rules failed: {st_r}")
    ladder_id = rules["ladder_id"]
    st_m0, _ = yield ask("GET", "/msgs", None, token)
    st_s0, stage_doc = yield ask("GET", "/stage?read=init", None, token)
    if st_m0 != 200 or st_s0 != 200:
        raise RuntimeError("initial msgs/stage read failed")
    if stage_doc.get("peer_blocker"):
        st_p, _ = yield ask("POST", "/prepare", {"ladder_id": ladder_id}, token)
        st_mu, _ = yield ask("GET", "/msgs", None, token)
        st_su, stage_doc = yield ask("GET", "/stage?read=unblocked", None, token)
        if st_p != 200 or st_mu != 200 or st_su != 200 or stage_doc.get("peer_blocker"):
            raise RuntimeError("failed to unblock peer_blocker")
    st_q0, prereq_doc = yield ask("GET", "/prereq", None, token)
    if st_q0 != 200:
        raise RuntimeError(f"GET /prereq failed: {st_q0}")
    live_tok = prereq_doc["prereq_token"]
    if not stage_doc.get("prereq_ready"):
        st_sp, stage_doc = yield ask("GET", "/stage?read=prereq0", None, token)
        if st_sp != 200:
            raise RuntimeError(f"GET /stage after prereq failed: {st_sp}")
    for idx in (1, 2, 3):
        entry = _stage_entry(stage_doc, idx)
        if entry.get("status") != "ready":
            raise RuntimeError(f"stage {idx} not ready: {entry}")
        payload = {"stage": idx, "step": entry["step"], "prereq_token": live_tok}
        st_x, _ = yield ask("POST", "/stage", payload, token)
        if st_x != 200:
            raise RuntimeError(f"POST /stage {idx} failed: {st_x}")
        if idx < 3:
            st_mi, _ = yield ask("GET", "/msgs", None, token)
            st_si, stage_doc = yield ask("GET", f"/stage?read=after_{idx}", None, token)
            if st_mi != 200 or st_si != 200:
                raise RuntimeError(f"inter-stage read after {idx} failed")
            if stage_doc.get("token_revoked") or not stage_doc.get("prereq_ready"):
                st_qi, prereq_doc = yield ask("GET", "/prereq", None, token)
                if st_qi != 200:
                    raise RuntimeError(f"refresh GET /prereq failed: {st_qi}")
                live_tok = prereq_doc["prereq_token"]
                st_sr, stage_doc = yield ask("GET", f"/stage?read=refreshed_{idx}", None, token)
                if st_sr != 200:
                    raise RuntimeError(f"post-refresh GET /stage failed: {st_sr}")


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
    if auth is None or path not in ("/prepare", "/stage"):
        return None
    st_r, rules = _fetch(world_url, ask("GET", "/rules", None, auth), urlopen, sleep)
    if st_r != 200:
        return None
    if path == "/prepare":
        if rules.get("prepared") is True or rules["budget_left"].get("prepare") == 0:
            return 200, {"ladder_id": rules["ladder_id"], "unblocked": True, "peer_blocker": False}
        return None
    stage_num = int(request["body"]["stage"])
    if stage_num in (rules.get("completed_stages") or []):
        return 200, {
            "action": "stage",
            "stage": stage_num,
            "item_id": request["body"]["step"],
            "phase": "completed" if stage_num == 3 else "in_progress",
        }
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
