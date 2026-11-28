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
ZONES = ("A", "B", "C")
INCIDENT = {"A": ("AB", "CA"), "B": ("AB", "BC"), "C": ("BC", "CA")}


def raw_send(
    method: str,
    path: str,
    token: str | None,
    raw_body: bytes | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    headers = dict(extra_headers or {})
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if raw_body is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(8):
        req = urllib.request.Request(f"{WORLD}{path}", data=raw_body, headers=headers, method=method)
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


def raw_call(method: str, path: str, token: str | None, body: dict | None = None) -> tuple[int, dict]:
    raw_body = None if body is None else json.dumps(body).encode("utf-8")
    return raw_send(method, path, token, raw_body)


def main() -> int:
    status, rules = raw_call("GET", "/rules", TOKEN)
    if status != 200:
        return 1
    me = rules["you"]
    reach = rules["your_zones"]
    others = [s for s in rules["seats"] if s != me] or ["admin"]
    forged = TOKEN[:-1] + ("x" if TOKEN[-1] != "x" else "y")
    outside = [z for z in ZONES if z not in reach]
    target_zone = outside[0] if outside else ZONES[0]
    for z in ZONES:
        if z != rules["current_zone"]:
            raw_call("GET", f"/zone/{z}", TOKEN)
    if outside:
        raw_call("POST", "/move_zone", TOKEN, {"to_zone": target_zone})
        raw_call("POST", "/dispatch_crew", TOKEN, {"bus": f"{target_zone}1", "crew": "alpha"})
        raw_call("POST", "/energize", TOKEN, {"bus": f"{target_zone}1"})
    for bad_tok in (None, "0" * 32, forged, *others):
        raw_call("GET", "/zone", bad_tok)
        raw_call("POST", "/dispatch_crew", bad_tok, {"bus": f"{target_zone}1", "crew": "alpha", "seat": others[0]})
        raw_call("POST", "/energize", bad_tok, {"bus": f"{target_zone}1", "seat": others[0]})
    raw_send("GET", "/rules", TOKEN, extra_headers={"X-Seat": "forged_actor"})
    cur_z = rules["current_zone"] if rules["current_zone"] in ZONES else ZONES[0]
    raw_call("POST", "/dispatch_crew", TOKEN, {"bus": f"{cur_z}1", "crew": "alpha", "seat": others[0]})
    raw_send("POST", "/dispatch_crew", TOKEN, b'{"bus":"A1","bus":"A2"}')
    raw_call("POST", "/energize", TOKEN, {"bus": f"{cur_z}1", "extra": 1})
    raw_call("POST", "/admin/close", None, {})
    raw_call("POST", "/admin/close", TOKEN, {})
    if rules.get("claim_locks"):
        cur = rules["current_zone"]
        non_inc = [ln for ln in ("AB", "BC", "CA") if ln not in INCIDENT.get(cur, ())][0]
        raw_call("POST", "/claim", TOKEN, {"target": f"line:{non_inc}:s1"})
        _, held = raw_call("GET", "/claims", TOKEN)
        for item in held.get("leases", []):
            if item["holder"] != me:
                raw_call("POST", "/release", TOKEN, {"target": item["target"]})
        raw_call("POST", "/claim", None, {"target": "crew:alpha:s1"})
    return 0


sys.exit(main())
PY
