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
MAX_POLLS = 100
RETRIES = 8
TIMEOUT = 15


def wait_health():
    for _ in range(60):
        try:
            with urllib.request.urlopen(WORLD + "/health", timeout=2) as resp:
                if resp.status == 200:
                    return
        except (OSError, http.client.HTTPException, socket.gaierror):
            pass
        time.sleep(0.05)


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
            try:
                raw = err.read()
            finally:
                err.close()
            return err.code, json.loads(raw or b"{}")
        except (OSError, http.client.HTTPException, socket.gaierror):
            time.sleep(0.05)
    raise RuntimeError(f"{method} {path} failed")


def parse_obligations(messages):
    found = {}
    for msg in messages:
        txt = str(msg.get("text") or "").strip()
        if not txt.startswith("{"):
            continue
        try:
            obj = json.loads(txt)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("kind") == "obligation":
            acct = obj.get("account")
            val = obj.get("req")
            if acct in ("acct_a", "acct_b") and type(val) is int:
                found[acct] = val
    return found


def wait_obligations(needed):
    for _ in range(MAX_POLLS):
        st, rep = call("GET", "/msgs")
        assert st == 200
        obs = parse_obligations(rep.get("messages", []))
        if all(k in obs for k in needed):
            return obs
        time.sleep(POLL_SLEEP)
    raise RuntimeError(f"timed out waiting for obligations {needed}")


def wait_state(pred):
    for _ in range(MAX_POLLS):
        st, rep = call("GET", "/state")
        assert st == 200
        if pred(rep):
            return rep
        time.sleep(POLL_SLEEP)
    raise RuntimeError("timed out waiting for /state condition")


def do_transfer(account, amount, use_leases):
    lid = None
    if use_leases:
        st_c, crep = call("POST", "/claim", {"target": account})
        assert st_c == 200
        lid = crep["lease_id"]
    st_s, srep = call("GET", "/state")
    assert st_s == 200
    payload = {"account": account, "amount": amount, "expected_version": int(srep["state_version"])}
    if lid is not None:
        payload["lease_id"] = lid
    st_t, _ = call("POST", "/transfer", payload)
    assert st_t == 200
    if lid is not None:
        st_r, _ = call("POST", "/release", {"target": account, "lease_id": lid})
        assert st_r == 200


def do_commit(use_leases, use_notices, use_channel):
    if use_channel:
        st_m, _ = call("GET", "/msgs")
        assert st_m == 200
    if use_notices:
        st_n, _ = call("GET", "/notices")
        assert st_n == 200
    lid = None
    if use_leases:
        st_c, crep = call("POST", "/claim", {"target": "settlement"})
        assert st_c == 200
        lid = crep["lease_id"]
    st_s, srep = call("GET", "/state")
    assert st_s == 200
    st_r, rrep = call("GET", "/rates")
    assert st_r == 200
    payload = {
        "total_reserve": int(srep["total_balance"]),
        "official_rate": str(rrep["official_rate"]),
        "expected_version": int(srep["state_version"]),
    }
    if lid is not None:
        payload["lease_id"] = lid
    st_cm, _ = call("POST", "/commit", payload)
    assert st_cm == 200


def main():
    wait_health()
    st, rules = call("GET", "/rules")
    assert st == 200
    me = SEAT or str(rules["you"])
    slack = int(rules["available_slack"])
    use_ch = bool(rules["channel"])
    use_leases = bool(rules["leases"])
    use_notices = bool(rules["notices"])
    if me == "p1":
        req_a, req_b = int(rules["req_a"]), int(rules["req_b"])
        alloc_a = (req_a - req_b + slack) // 2
        alloc_b = (req_b - req_a + slack) // 2
        do_transfer("acct_a", alloc_a, use_leases)
        do_transfer("acct_b", alloc_b, use_leases)
        do_commit(use_leases, use_notices, use_ch)
        return
    if me == "acct_a":
        req_a = int(rules["req_a"])
        if use_ch:
            call("POST", "/msg", {"text": json.dumps({"kind": "obligation", "account": "acct_a", "req": req_a})})
            obs = wait_obligations(("acct_a", "acct_b"))
            amt = (req_a - obs["acct_b"] + slack) // 2
        else:
            amt = req_a
        do_transfer("acct_a", amt, use_leases)
        return
    if me == "acct_b":
        req_b = int(rules["req_b"])
        if use_ch:
            call("POST", "/msg", {"text": json.dumps({"kind": "obligation", "account": "acct_b", "req": req_b})})
            obs = wait_obligations(("acct_a", "acct_b"))
            amt = (req_b - obs["acct_a"] + slack) // 2
        else:
            amt = req_b
        wait_state(lambda s: s.get("acct_a_done") is True)
        if use_ch:
            call("GET", "/msgs")
        do_transfer("acct_b", amt, use_leases)
        return
    if use_ch:
        wait_obligations(("acct_a", "acct_b"))
    wait_state(lambda s: s.get("acct_a_done") is True and s.get("acct_b_done") is True)
    do_commit(use_leases, use_notices, use_ch)


if __name__ == "__main__":
    main()
PY
