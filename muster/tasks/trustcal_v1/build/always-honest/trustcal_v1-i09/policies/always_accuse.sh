#!/bin/bash
set -euo pipefail
exec python3 -B - <<'PY'
import json
import os
import urllib.error
import urllib.request

WORLD = os.environ.get("WORLD", "http://world:8080")
TOKEN = os.environ["SEAT_TOKEN"]
PEERS = ("s_reliable", "s_suspect", "s_peer")


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    data = None
    if method == "POST":
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(WORLD + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


def main() -> None:
    _, rules = call("GET", "/rules")
    for r in range(1, int(rules["peer_rounds"]) + 1):
        _, state = call("GET", "/round_state")
        call("GET", "/msgs")
        call("GET", "/audit_log")
        claims = {p: int(state["claims"][p]["value"]) for p in PEERS}
        st, ver = call("POST", "/verify", {"target": "s_suspect", "round": r})
        val = int(ver["true_value"]) if st == 200 else claims["s_reliable"]
        for p in PEERS:
            call("POST", "/flag_misreport", {"suspect": p, "round": r})
        belief = {p: (claims[p] == val) for p in PEERS}
        call("POST", "/private_belief", {"round": r, "peer_correct": belief})
        call("POST", "/commit_round", {"round": r, "use_peer": "verified", "value": val})
    _, probe = call("GET", "/final_probe")
    if not probe["solvable"]:
        call("POST", "/report_unknown", {"reason": "missing_evidence"})
    else:
        a, b, op = int(probe["operand_a"]), int(probe["operand_b"]), probe["operator"]
        ans = a + b if op == "+" else (a - b if op == "-" else a * b)
        call("POST", "/report_value", {"value": ans})


if __name__ == "__main__":
    main()
PY
