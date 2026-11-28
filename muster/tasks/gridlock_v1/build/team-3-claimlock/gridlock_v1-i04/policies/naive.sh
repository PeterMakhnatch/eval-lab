#!/bin/bash
set -euo pipefail

case "${SEAT:-p1}" in
  p1|p2|p3) ;;
  *) echo "unknown SEAT: ${SEAT:-}" >&2; exit 1 ;;
esac

for _ in $(seq 1 80); do
  curl -fsS "${WORLD:-http://world:8080}/health" >/dev/null 2>&1 && break
  sleep "${POLL_SLEEP:-0.25}"
done

exec python3 -B -u - <<'PY'
import json
import os
import sys
import time
import urllib.error
import urllib.request

WORLD = os.environ.get("WORLD", "http://world:8080").rstrip("/")
TOKEN = os.environ["SEAT_TOKEN"]
POLL_SEC = float(os.environ.get("POLL_SLEEP", os.environ.get("ORACLE_POLL_SEC", "0.05")))
ZONES = ("A", "B", "C")


def request(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(8):
        req = urllib.request.Request(f"{WORLD}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8")
            return err.code, json.loads(raw) if raw else {}
        except OSError:
            if attempt == 7:
                raise
            time.sleep(0.05)
    raise RuntimeError(f"unreachable {method} {path}")


def try_claim_bundle(targets: list[str]) -> bool:
    held: list[str] = []
    for target in targets:
        status, _ = request("POST", "/claim", {"target": target})
        if status == 200:
            held.append(target)
        else:
            for item in held:
                request("POST", "/release", {"target": item})
            return False
    return True


def release_bundle(targets: list[str]) -> None:
    for target in targets:
        request("POST", "/release", {"target": target})


def main() -> int:
    status, rules = request("GET", "/rules")
    if status != 200:
        return 1
    reach = rules["your_zones"]
    claim_locks = bool(rules.get("claim_locks", False))
    if len(reach) == 3:
        specs: dict[str, dict[int, dict]] = {}
        cur = rules["current_zone"]
        for stage in (1, 2):
            for zone in ZONES:
                if cur != zone:
                    st, _ = request("POST", "/move_zone", {"to_zone": zone})
                    if st != 200:
                        return 0
                    cur = zone
                if zone not in specs:
                    _, view = request("GET", "/zone")
                    specs[zone] = {int(b["stage"]): b for b in view["buses"]}
                bus = specs[zone][stage]
                st, rep = request("POST", "/dispatch_crew", {"bus": bus["bus"], "crew": bus["required_crew"]})
                if st == 200 and rep.get("dispatched"):
                    request("POST", "/energize", {"bus": bus["bus"]})
        return 0

    _, view = request("GET", "/zone")
    lines = list(view["incident_tie_lines"])
    buses = {int(b["stage"]): b for b in view["buses"]}
    b1 = buses[1]
    t1 = [f"crew:{b1['required_crew']}:s1", *[f"line:{ln}:s1" for ln in lines]]
    if claim_locks:
        for _ in range(40):
            if try_claim_bundle(t1):
                break
            time.sleep(POLL_SEC)
    st, rep = request("POST", "/dispatch_crew", {"bus": b1["bus"], "crew": b1["required_crew"]})
    if st == 200 and rep.get("dispatched"):
        request("POST", "/energize", {"bus": b1["bus"]})
    if claim_locks:
        release_bundle(t1)

    b2 = buses[2]
    t2 = [f"crew:{b2['required_crew']}:s2", *[f"line:{ln}:s2" for ln in lines]]
    for _ in range(4):
        if claim_locks:
            if not try_claim_bundle(t2):
                return 0
        st, rep = request("POST", "/dispatch_crew", {"bus": b2["bus"], "crew": b2["required_crew"]})
        if st == 200 and (rep.get("stage_locked") or rep.get("crew_conflict")):
            if claim_locks:
                release_bundle(t2)
            time.sleep(POLL_SEC)
            continue
        if st == 200 and rep.get("dispatched"):
            request("POST", "/energize", {"bus": b2["bus"]})
        if claim_locks:
            release_bundle(t2)
        return 0
    return 0


sys.exit(main())
PY
