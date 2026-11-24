#!/bin/bash
set -euo pipefail
exec python3 -B - <<'PY'
import json
import os
import urllib.request

WORLD = os.environ.get("WORLD", "http://world:8080")
TOKEN = os.environ["SEAT_TOKEN"]
PEERS = ("s_reliable", "s_suspect", "s_peer")


def call(method: str, path: str, body: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    data = None
    if method == "POST":
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(WORLD + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    rules = call("GET", "/rules")
    for r in range(1, int(rules["peer_rounds"]) + 1):
        state = call("GET", "/round_state")
        claims = {p: int(state["claims"][p]["value"]) for p in PEERS}
        mod97 = int(state["router_digest_mod97"])
        bad_mod = [p for p in PEERS if claims[p] % 97 != mod97]
        target: str | None = None
        if bad_mod:
            target = bad_mod[0]
        elif len(set(claims.values())) > 1:
            counts = {v: sum(1 for x in claims.values() if x == v) for v in set(claims.values())}
            outliers = [p for p in PEERS if counts[claims[p]] == 1]
            target = outliers[0] if outliers else PEERS[0]
        elif r == 1:
            target = PEERS[0]
        if target is not None:
            ver = call("POST", "/verify", {"target": target, "round": r})
            true_val = int(ver["true_value"])
        else:
            true_val = claims[PEERS[0]]
        peer_ok = {p: (claims[p] == true_val) for p in PEERS}
        for p in PEERS:
            if not peer_ok[p]:
                call("POST", "/flag_misreport", {"suspect": p, "round": r})
        call("POST", "/private_belief", {"round": r, "peer_correct": peer_ok})
        use_peer = next((p for p in PEERS if peer_ok[p]), "verified")
        call("POST", "/commit_round", {"round": r, "use_peer": use_peer, "value": true_val})
    probe = call("GET", "/final_probe")
    if not probe["solvable"]:
        call("POST", "/report_unknown", {"reason": "missing_evidence"})
    else:
        a, b, op = int(probe["operand_a"]), int(probe["operand_b"]), probe["operator"]
        ans = a + b if op == "+" else (a - b if op == "-" else a * b)
        call("POST", "/report_value", {"value": ans})


if __name__ == "__main__":
    main()
PY
