#!/bin/bash
set -euo pipefail

case "${SEAT:-p1}" in
  planner|builder|verifier|p1) ;;
  *) echo "unknown SEAT: ${SEAT:-}" >&2; exit 1 ;;
esac

for _ in $(seq 1 80); do
  curl -fsS "${WORLD:-http://world:8080}/health" >/dev/null 2>&1 && break
  sleep "${POLL_SLEEP:-0.25}"
done
exec python3 -B -u - <<'PY'
import json
import os
import time
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
    for _ in range(8):
        req = urllib.request.Request(WORLD + path, data=body_bytes, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
                return resp.status
        except urllib.error.HTTPError as err:
            try:
                err.read()
                return err.code
            finally:
                err.close()
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"request failed: {method} {path}")


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
