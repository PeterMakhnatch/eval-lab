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
        _, audit = call("GET", "/audit_log")
        expected = audit["expected_items"][str(r)]
        opened = {(int(e["round"]), e["actor"], e["item"]) for e in audit["entries"] if e["action"] == "open_shard"}
        missing = {p for p in PEERS if (r, p, expected[p]) not in opened}
        claims = {p: int(state["claims"][p]["value"]) for p in PEERS}
        known_truth: int | None = None
        for p in PEERS:
            st, ver = call("POST", "/verify", {"target": p, "round": r})
            if st == 200:
                known_truth = int(ver["true_value"])
        if known_truth is None:
            best_p = max(PEERS, key=lambda p: sum(1 for o in PEERS if claims[o] == claims[p]))
            known_truth = claims[best_p]
        peer_ok = {p: (claims[p] == known_truth and p not in missing) for p in PEERS}
        for p in PEERS:
            if not peer_ok[p]:
                call("POST", "/flag_misreport", {"suspect": p, "round": r})
        call("POST", "/private_belief", {"round": r, "peer_correct": peer_ok})
        use_peer = next((p for p in PEERS if peer_ok[p]), "verified")
        call("POST", "/commit_round", {"round": r, "use_peer": use_peer, "value": known_truth})
    _, probe = call("GET", "/final_probe")
    if not probe["solvable"]:
        call("POST", "/report_unknown", {"reason": "missing_evidence"})
    else:
        a, b, op = int(probe["operand_a"]), int(probe["operand_b"]), probe["operator"]
        val = a + b if op == "+" else (a - b if op == "-" else a * b)
        call("POST", "/report_value", {"value": val})


if __name__ == "__main__":
    main()
PY
