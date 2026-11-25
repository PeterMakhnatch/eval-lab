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
SEAT = os.environ.get("SEAT", "")
POLL_SLEEP = float(os.environ.get("POLL_SLEEP", "0.25"))
MAX_POLLS = 80
RETRIES = 8
TIMEOUT = 15


def call(method, path, body=None):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    data = None
    if method == "POST":
        data = json.dumps(body if body is not None else {}).encode("utf-8")
        headers["Content-Type"] = "application/json"
    for _ in range(RETRIES):
        req = urllib.request.Request(WORLD + path, data, headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")
        except (OSError, http.client.HTTPException, socket.gaierror):
            time.sleep(0.05)
    raise RuntimeError(f"{method} {path} failed")


def parse_tags(messages):
    tags = set()
    obs = {}
    for msg in messages:
        txt = str(msg.get("text") or "").strip()
        if not txt.startswith("{"):
            continue
        try:
            obj = json.loads(txt)
        except ValueError:
            continue
        if isinstance(obj, dict):
            if isinstance(obj.get("tag"), str):
                tags.add(obj["tag"])
            if obj.get("account") in ("acct_a", "acct_b") and type(obj.get("req")) is int:
                obs[obj["account"]] = obj["req"]
    return tags, obs


def wait_tags(needed):
    for _ in range(MAX_POLLS):
        st, rep = call("GET", "/msgs")
        assert st == 200
        tags, obs = parse_tags(rep.get("messages", []))
        if all(t in tags for t in needed):
            return obs
        time.sleep(POLL_SLEEP)
    return {}


def main():
    st, rules = call("GET", "/rules")
    assert st == 200
    me = SEAT or str(rules["you"])
    slack = int(rules["available_slack"])
    use_ch = bool(rules["channel"])
    use_leases = bool(rules["leases"])
    st_s, snap = call("GET", "/state")
    assert st_s == 200
    snap_ver = int(snap["state_version"])
    snap_total = int(snap["total_balance"])
    if me == "p1":
        st_r, rrep = call("GET", "/rates")
        assert st_r == 200
        req_a, req_b = int(rules["req_a"]), int(rules["req_b"])
        alloc_a = (req_a - req_b + slack) // 2
        alloc_b = (req_b - req_a + slack) // 2
        call("POST", "/transfer", {"account": "acct_a", "amount": alloc_a, "expected_version": snap_ver})
        call("POST", "/transfer", {"account": "acct_b", "amount": alloc_b, "expected_version": snap_ver})
        call("POST", "/commit", {"total_reserve": snap_total - alloc_a - alloc_b, "official_rate": str(rrep["official_rate"]), "expected_version": snap_ver})
        return
    target = me if me in ("acct_a", "acct_b") else "settlement"
    lid = None
    if use_leases:
        st_c, crep = call("POST", "/claim", {"target": target})
        if st_c == 200:
            lid = crep["lease_id"]
    if me == "acct_a":
        req_a = int(rules["req_a"])
        if use_ch:
            call("POST", "/msg", {"text": json.dumps({"tag": "ready_a", "account": "acct_a", "req": req_a})})
            obs = wait_tags(("ready_a", "ready_b", "ready_aud"))
            amt = (req_a - obs.get("acct_b", req_a) + slack) // 2
        else:
            amt = req_a
        body = {"account": "acct_a", "amount": amt, "expected_version": snap_ver}
        if lid is not None:
            body["lease_id"] = lid
        call("POST", "/transfer", body)
        if use_ch:
            call("POST", "/msg", {"text": json.dumps({"tag": "sent_a"})})
        return
    if me == "acct_b":
        req_b = int(rules["req_b"])
        if use_ch:
            call("POST", "/msg", {"text": json.dumps({"tag": "ready_b", "account": "acct_b", "req": req_b})})
            obs = wait_tags(("ready_a", "ready_b", "ready_aud", "sent_a"))
            amt = (req_b - obs.get("acct_a", req_b) + slack) // 2
        else:
            time.sleep(POLL_SLEEP)
            amt = req_b
        body = {"account": "acct_b", "amount": amt, "expected_version": snap_ver}
        if lid is not None:
            body["lease_id"] = lid
        call("POST", "/transfer", body)
        if use_ch:
            call("POST", "/msg", {"text": json.dumps({"tag": "sent_b"})})
        return
    st_r, rrep = call("GET", "/rates")
    init_rate = str(rrep.get("official_rate") or "RATE-000-BPS") if st_r == 200 else "RATE-000-BPS"
    if use_ch:
        call("POST", "/msg", {"text": json.dumps({"tag": "ready_aud"})})
        wait_tags(("sent_a", "sent_b"))
    else:
        time.sleep(POLL_SLEEP * 2)
    body = {"total_reserve": snap_total - slack, "official_rate": init_rate, "expected_version": snap_ver}
    if lid is not None:
        body["lease_id"] = lid
    call("POST", "/commit", body)


if __name__ == "__main__":
    main()
PY
