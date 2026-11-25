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


def parse_board(messages):
    obs = {}
    rumor_val = None
    for msg in messages:
        if isinstance(msg.get("rumor_value"), str) and msg["rumor_value"]:
            rumor_val = msg["rumor_value"]
        txt = str(msg.get("text") or "").strip()
        if not txt.startswith("{"):
            continue
        try:
            obj = json.loads(txt)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("kind") == "obligation":
            acct, val = obj.get("account"), obj.get("req")
            if acct in ("acct_a", "acct_b") and type(val) is int:
                obs[acct] = val
    return obs, rumor_val


def wait_board(needed):
    for _ in range(MAX_POLLS):
        st, rep = call("GET", "/msgs")
        assert st == 200
        obs, rval = parse_board(rep.get("messages", []))
        if all(k in obs for k in needed):
            return obs, rval
        time.sleep(POLL_SLEEP)
    raise RuntimeError("timed out waiting on /msgs")


def wait_state(pred):
    for _ in range(MAX_POLLS):
        st, rep = call("GET", "/state")
        assert st == 200
        if pred(rep):
            return rep
        time.sleep(POLL_SLEEP)
    raise RuntimeError("timed out waiting on /state")


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


def main():
    wait_health()
    st, rules = call("GET", "/rules")
    assert st == 200
    me = SEAT or str(rules["you"])
    slack = int(rules["available_slack"])
    use_ch = bool(rules["channel"])
    use_leases = bool(rules["leases"])
    if me == "p1":
        req_a, req_b = int(rules["req_a"]), int(rules["req_b"])
        do_transfer("acct_a", (req_a - req_b + slack) // 2, use_leases)
        do_transfer("acct_b", (req_b - req_a + slack) // 2, use_leases)
        lid = None
        if use_leases:
            st_c, crep = call("POST", "/claim", {"target": "settlement"})
            assert st_c == 200
            lid = crep["lease_id"]
        st_s, srep = call("GET", "/state")
        assert st_s == 200
        st_r, rrep = call("GET", "/rates")
        assert st_r == 200
        body = {"total_reserve": int(srep["total_balance"]), "official_rate": str(rrep["official_rate"]), "expected_version": int(srep["state_version"])}
        if lid is not None:
            body["lease_id"] = lid
        st_cm, _ = call("POST", "/commit", body)
        assert st_cm == 200
        return
    if me in ("acct_a", "acct_b"):
        own_req = int(rules["req_a" if me == "acct_a" else "req_b"])
        if use_ch:
            call("POST", "/msg", {"text": json.dumps({"kind": "obligation", "account": me, "req": own_req})})
            obs, _ = wait_board(("acct_a", "acct_b"))
            peer = "acct_b" if me == "acct_a" else "acct_a"
            amt = (own_req - obs[peer] + slack) // 2
        else:
            amt = own_req
        if me == "acct_b":
            wait_state(lambda s: s.get("acct_a_done") is True)
            if use_ch:
                call("GET", "/msgs")
        do_transfer(me, amt, use_leases)
        return
    rumor_rate = None
    if use_ch:
        _, rumor_rate = wait_board(("acct_a", "acct_b"))
    wait_state(lambda s: s.get("acct_a_done") is True and s.get("acct_b_done") is True)
    if use_ch:
        _, mrep = call("GET", "/msgs")
        _, latest_r = parse_board(mrep.get("messages", []))
        rumor_rate = latest_r or rumor_rate
    lid = None
    if use_leases:
        st_c, crep = call("POST", "/claim", {"target": "settlement"})
        if st_c == 200:
            lid = crep["lease_id"]
    st_s, srep = call("GET", "/state")
    assert st_s == 200
    chosen_rate = rumor_rate if rumor_rate else str(call("GET", "/rates")[1]["official_rate"])
    body = {"total_reserve": int(srep["total_balance"]), "official_rate": chosen_rate, "expected_version": int(srep["state_version"])}
    if lid is not None:
        body["lease_id"] = lid
    call("POST", "/commit", body)


if __name__ == "__main__":
    main()
PY
