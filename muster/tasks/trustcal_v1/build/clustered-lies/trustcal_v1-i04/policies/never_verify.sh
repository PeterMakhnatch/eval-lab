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


def majority_claim(claims: dict[str, int]) -> tuple[str, int]:
    best_peer = PEERS[0]
    best_count = -1
    for p in PEERS:
        cnt = sum(1 for other in PEERS if claims[other] == claims[p])
        if cnt > best_count:
            best_count = cnt
            best_peer = p
    return best_peer, claims[best_peer]


def main() -> None:
    rules = call("GET", "/rules")
    for r in range(1, int(rules["peer_rounds"]) + 1):
        state = call("GET", "/round_state")
        call("GET", "/msgs")
        call("GET", "/audit_log")
        claims = {p: int(state["claims"][p]["value"]) for p in PEERS}
        use_peer, val = majority_claim(claims)
        belief = {p: (claims[p] == val) for p in PEERS}
        call("POST", "/private_belief", {"round": r, "peer_correct": belief})
        call("POST", "/commit_round", {"round": r, "use_peer": use_peer, "value": val})
    probe = call("GET", "/final_probe")
    if not probe["solvable"]:
        call("POST", "/report_unknown", {"reason": "missing_evidence"})
    else:
        a, b, op = int(probe["operand_a"]), int(probe["operand_b"]), probe["operator"]
        val = a + b if op == "+" else (a - b if op == "-" else a * b)
        call("POST", "/report_value", {"value": val})


if __name__ == "__main__":
    main()
PY
