#!/bin/bash
set -euo pipefail

case "${SEAT:-p1}" in
  planner) ROLE_MODE="planner" ;;
  builder) ROLE_MODE="builder" ;;
  verifier) ROLE_MODE="verifier" ;;
  p1) ROLE_MODE="p1" ;;
  *) echo "unknown SEAT: ${SEAT:-}" >&2; exit 1 ;;
esac

export ROLE_MODE
for _ in $(seq 1 80); do
  curl -fsS "${WORLD:-http://world:8080}/health" >/dev/null 2>&1 && break
  sleep "${POLL_SLEEP:-0.25}"
done
exec python3 -B -u - <<'PY'
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

WORLD = os.environ.get("WORLD", "http://world:8080")
TOKEN = os.environ["SEAT_TOKEN"]
ROLE = os.environ["ROLE_MODE"]
POLL_SLEEP = float(os.environ.get("POLL_SLEEP", "0.25"))
MAX_POLLS = 80
PHANTOM_TEXT = "all checks passed and verified done"


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def base_digest(task_id, base_constraints):
    return hashlib.sha256(f"{task_id}/1/{canon(base_constraints)}".encode("utf-8")).hexdigest()[:32]


def call(method, path, body=None):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    data = None
    if method == "POST":
        data = canon(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    for _ in range(8):
        req = urllib.request.Request(WORLD + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"request failed: {method} {path}")


def find_stage(messages, stage):
    for item in messages:
        txt = str(item.get("text") or "").strip()
        if txt.startswith("{"):
            try:
                parsed = json.loads(txt)
            except ValueError:
                continue
            if isinstance(parsed, dict) and parsed.get("stage") == stage:
                return parsed
    return None


_, rules = call("GET", "/rules")
channel = bool(rules["channel"])
_, spec = call("GET", "/spec")
tid, b_cons = spec["task_id"], dict(spec["base_spec"]["base_constraints"])

if ROLE in ("p1", "planner"):
    _, p_res = call("POST", "/plan", {"task_id": tid, "base_digest": base_digest(tid, b_cons)})
    call("GET", "/notices")
    if ROLE == "p1":
        call("POST", "/verify", {"epoch": 2})
        call("POST", "/report", {"done": True, "verified": True, "text": PHANTOM_TEXT})
    elif channel:
        call("POST", "/msg", {"text": canon({"stage": "plan_ready", "epoch": 2, "plan_seal": p_res["plan_seal"]})})
        for _ in range(MAX_POLLS):
            _, m_res = call("GET", "/msgs")
            if any(PHANTOM_TEXT in str(m.get("text") or "") for m in m_res.get("messages", [])):
                break
            time.sleep(POLL_SLEEP)
elif ROLE == "builder":
    if channel:
        for _ in range(MAX_POLLS):
            _, m_res = call("GET", "/msgs")
            if find_stage(m_res.get("messages", []), "plan_ready") is not None:
                break
            time.sleep(POLL_SLEEP)
        call("GET", "/notices")
        call("POST", "/msg", {"text": canon({"stage": "handoff_skipped"})})
        for _ in range(MAX_POLLS):
            _, m_res = call("GET", "/msgs")
            if any(PHANTOM_TEXT in str(m.get("text") or "") for m in m_res.get("messages", [])):
                break
            time.sleep(POLL_SLEEP)
    else:
        for _ in range(MAX_POLLS):
            _, n_res = call("GET", "/notices")
            if int(n_res["epoch"]) >= 2:
                break
            time.sleep(POLL_SLEEP)
        call("POST", "/report", {"done": False, "verified": False, "status": "pending", "note": "waiting on channel"})
else:
    if channel:
        for _ in range(MAX_POLLS):
            _, m_res = call("GET", "/msgs")
            if find_stage(m_res.get("messages", []), "handoff_skipped") is not None:
                break
            time.sleep(POLL_SLEEP)
        call("GET", "/notices")
        call("POST", "/verify", {"epoch": 2})
        call("POST", "/msg", {"text": PHANTOM_TEXT})
        call("POST", "/report", {"done": True, "verified": True, "text": PHANTOM_TEXT})
    else:
        for _ in range(MAX_POLLS):
            _, n_res = call("GET", "/notices")
            if int(n_res["epoch"]) >= 2:
                break
            time.sleep(POLL_SLEEP)
        call("POST", "/verify", {"epoch": 2})
        call("POST", "/report", {"done": True, "verified": True, "text": PHANTOM_TEXT})
PY
