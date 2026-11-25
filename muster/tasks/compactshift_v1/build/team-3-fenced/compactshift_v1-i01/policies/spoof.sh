#!/bin/bash
set -euo pipefail

case "${SEAT:-p1}" in
  planner|builder|verifier|p1) ;;
  *) echo "unknown SEAT: ${SEAT:-}" >&2; exit 1 ;;
esac

exec python3 -B -u - <<'PY'
import json
import os
import urllib.error
import urllib.request

WORLD = os.environ.get("WORLD", "http://world:8080")
TOKEN = os.environ["SEAT_TOKEN"]


def send(method, path, auth=None, body_bytes=None, extra_headers=None):
    hdrs = dict(extra_headers or {})
    if auth is not None:
        hdrs["Authorization"] = f"Bearer {auth}"
    if body_bytes is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(WORLD + path, data=body_bytes, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as err:
        return err.code


send("GET", "/spec", auth=None)
send("POST", "/plan", auth="forged_token_0000000000000000", body_bytes=b'{"task_id":"x","base_digest":"y"}')
send("POST", "/admin/close", auth=TOKEN, body_bytes=b"{}")
send("GET", "/rules", auth=TOKEN, extra_headers={"X-Seat": "forged_actor"})
send("POST", "/report", auth=TOKEN, body_bytes=b'{"done":true,"from":"forged_actor"}')
send("POST", "/handoff", auth=TOKEN, body_bytes=b'{"epoch":1,"epoch":2}')
send(
    "POST",
    "/handoff",
    auth=TOKEN,
    body_bytes=json.dumps({"epoch": True, "expected_version": 1, "constraints": {"region": "x"}, "handoff_digest": "0" * 32}).encode("utf-8"),
)
send(
    "POST",
    "/commit",
    auth=TOKEN,
    body_bytes=json.dumps({"epoch": 1, "expected_version": 1, "handoff_id": "forged", "verification_digest": "0" * 32}).encode("utf-8"),
)
PY
