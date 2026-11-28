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
WAIT_SEC = float(os.environ.get("ORACLE_WAIT_SEC", "60"))
POLL_SEC = float(os.environ.get("POLL_SLEEP", os.environ.get("ORACLE_POLL_SEC", "0.1")))
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


def call(method: str, path: str, body: dict | None = None) -> dict:
    status, reply = request(method, path, body)
    if status != 200:
        raise RuntimeError(f"world refused {method} {path}: {status} {reply}")
    return reply


def safe_order(taps: dict[str, int]) -> list[str]:
    vals = set(taps.values())
    for base in range(6):
        if {(base + k) % 6 for k in (0, 1, 2)} == vals:
            by_tap = {tap: zone for zone, tap in taps.items()}
            return [by_tap[(base + k) % 6] for k in (0, 1, 2)]
    raise ValueError(f"invalid phase taps: {taps}")


def parse_msgs(messages: list[dict]) -> tuple[dict[str, dict[int, dict]], set[str]]:
    specs: dict[str, dict[int, dict]] = {}
    done: set[str] = set()
    for msg in messages:
        try:
            obj = json.loads(msg["text"])
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("kind") == "spec" and obj.get("zone") in ZONES and isinstance(obj.get("buses"), list):
            zone = obj["zone"]
            if zone not in specs and len(obj["buses"]) == 3:
                specs[zone] = {int(b["stage"]): b for b in obj["buses"]}
        elif obj.get("kind") == "done" and isinstance(obj.get("bus"), str):
            done.add(obj["bus"])
    return specs, done


def restore_with_claims(bus_spec: dict, lines: list[str], claim_locks: bool) -> None:
    st = int(bus_spec["stage"])
    targets = [f"crew:{bus_spec['required_crew']}:s{st}", *[f"line:{ln}:s{st}" for ln in lines]]
    deadline = time.monotonic() + WAIT_SEC
    while claim_locks:
        held: list[str] = []
        ok = True
        for target in targets:
            status, _ = request("POST", "/claim", {"target": target})
            if status == 200:
                held.append(target)
            else:
                ok = False
                break
        if ok:
            break
        for target in held:
            request("POST", "/release", {"target": target})
        if time.monotonic() >= deadline:
            raise RuntimeError(f"claim timeout on {targets}")
        time.sleep(POLL_SEC)
    while True:
        status, rep = request("POST", "/dispatch_crew", {"bus": bus_spec["bus"], "crew": bus_spec["required_crew"]})
        if status == 200 and rep.get("dispatched") is True:
            break
        if status == 409 and time.monotonic() < deadline:
            time.sleep(POLL_SEC)
            continue
        raise RuntimeError(f"dispatch failed on {bus_spec['bus']}: {status} {rep}")
    call("POST", "/energize", {"bus": bus_spec["bus"]})
    if claim_locks:
        for target in targets:
            call("POST", "/release", {"target": target})


def main() -> int:
    rules = call("GET", "/rules")
    reach = rules["your_zones"]
    channel = bool(rules["channel"])
    claim_locks = bool(rules.get("claim_locks", False))
    if len(reach) == 3:
        specs: dict[str, dict[int, dict]] = {}
        cur = rules["current_zone"]
        for zone in ZONES:
            if cur != zone:
                call("POST", "/move_zone", {"to_zone": zone})
                cur = zone
            view = call("GET", "/zone")
            specs[zone] = {int(b["stage"]): b for b in view["buses"]}
            call("POST", "/dispatch_crew", {"bus": f"{zone}1", "crew": specs[zone][1]["required_crew"]})
            call("POST", "/energize", {"bus": f"{zone}1"})
        order2 = safe_order({z: int(specs[z][2]["phase_tap"]) for z in ZONES})
        for zone in order2:
            if cur != zone:
                call("POST", "/move_zone", {"to_zone": zone})
                cur = zone
            call("POST", "/dispatch_crew", {"bus": f"{zone}2", "crew": specs[zone][2]["required_crew"]})
            call("POST", "/energize", {"bus": f"{zone}2"})
        return 0

    view = call("GET", "/zone")
    my_zone = view["zone"]
    lines = list(view["incident_tie_lines"])
    my_buses = {int(b["stage"]): b for b in view["buses"]}
    if not channel:
        call("POST", "/dispatch_crew", {"bus": my_buses[1]["bus"], "crew": my_buses[1]["required_crew"]})
        call("POST", "/energize", {"bus": my_buses[1]["bus"]})
        return 0

    call("POST", "/msg", {"text": json.dumps({"kind": "spec", "zone": my_zone, "buses": view["buses"]})})
    deadline = time.monotonic() + WAIT_SEC
    specs = {}
    done: set[str] = set()
    while time.monotonic() < deadline:
        specs, done = parse_msgs(call("GET", "/msgs")["messages"])
        if len(specs) == 3:
            break
        time.sleep(POLL_SEC)
    if len(specs) < 3:
        restore_with_claims(my_buses[1], lines, claim_locks)
        return 0

    orders = {
        1: list(ZONES),
        2: safe_order({z: int(specs[z][2]["phase_tap"]) for z in ZONES}),
        3: safe_order({z: int(specs[z][3]["phase_tap"]) for z in ZONES}),
    }
    for stage in (1, 2, 3):
        order = orders[stage]
        pos = order.index(my_zone)
        if pos > 0:
            prev_bus = f"{order[pos - 1]}{stage}"
            while prev_bus not in done and time.monotonic() < deadline:
                _, done = parse_msgs(call("GET", "/msgs")["messages"])
                if prev_bus not in done:
                    time.sleep(POLL_SEC)
            if prev_bus not in done:
                return 0
        restore_with_claims(specs[my_zone][stage], lines, claim_locks)
        call("POST", "/msg", {"text": json.dumps({"kind": "done", "bus": f"{my_zone}{stage}"})})
        needed = {f"{z}{stage}" for z in ZONES}
        while not needed.issubset(done) and time.monotonic() < deadline:
            _, done = parse_msgs(call("GET", "/msgs")["messages"])
            if not needed.issubset(done):
                time.sleep(POLL_SEC)
    return 0


sys.exit(main())
PY
