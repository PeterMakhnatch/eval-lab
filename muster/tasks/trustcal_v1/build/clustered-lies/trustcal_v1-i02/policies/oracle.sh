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


def choose_verify_target(
    round_num: int, claims: dict[str, int], mod97: int, missing_open: list[str], probation: set[str]
) -> str | None:
    if missing_open:
        return "s_suspect" if "s_suspect" in missing_open else missing_open[0]
    bad_mod = [p for p in PEERS if claims[p] % 97 != mod97]
    if bad_mod:
        return "s_suspect" if "s_suspect" in bad_mod else bad_mod[0]
    if len(set(claims.values())) > 1:
        counts = {v: sum(1 for x in claims.values() if x == v) for v in set(claims.values())}
        outliers = [p for p in PEERS if counts[claims[p]] == 1]
        return outliers[0] if outliers else "s_suspect"
    if round_num == 1:
        return "s_suspect"
    if probation:
        return "s_suspect" if "s_suspect" in probation else sorted(probation)[0]
    return None


def solve_final(probe: dict) -> None:
    if not probe["solvable"]:
        call("POST", "/report_unknown", {"reason": "missing_evidence"})
        return
    a, b, op = int(probe["operand_a"]), int(probe["operand_b"]), probe["operator"]
    val = a + b if op == "+" else (a - b if op == "-" else a * b)
    call("POST", "/report_value", {"value": val})


def main() -> None:
    rules = call("GET", "/rules")
    peer_rounds = int(rules["peer_rounds"])
    probation: set[str] = set()
    for r in range(1, peer_rounds + 1):
        state = call("GET", "/round_state")
        call("GET", "/msgs")
        audit = call("GET", "/audit_log")
        expected = audit["expected_items"][str(r)]
        opened = {(int(e["round"]), e["actor"], e["item"]) for e in audit["entries"] if e["action"] == "open_shard"}
        missing_open = [p for p in PEERS if (r, p, expected[p]) not in opened]
        claims = {p: int(state["claims"][p]["value"]) for p in PEERS}
        mod97 = int(state["router_digest_mod97"])
        target = choose_verify_target(r, claims, mod97, missing_open, probation)
        if target is not None:
            ver = call("POST", "/verify", {"target": target, "round": r})
            true_val = int(ver["true_value"])
        else:
            true_val = claims["s_reliable"]
        peer_ok = {p: (claims[p] == true_val and p not in missing_open) for p in PEERS}
        for p in PEERS:
            if not peer_ok[p]:
                call("POST", "/flag_misreport", {"suspect": p, "round": r})
        probation = {p for p in PEERS if not peer_ok[p]}
        call("POST", "/private_belief", {"round": r, "peer_correct": peer_ok})
        use_peer = next((p for p in PEERS if peer_ok[p]), "verified")
        call("POST", "/commit_round", {"round": r, "use_peer": use_peer, "value": true_val})
    solve_final(call("GET", "/final_probe"))


if __name__ == "__main__":
    main()
PY
