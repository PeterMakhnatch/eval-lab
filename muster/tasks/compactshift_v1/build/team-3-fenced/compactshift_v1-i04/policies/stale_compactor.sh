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


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def base_digest(task_id, base_constraints):
    return hashlib.sha256(f"{task_id}/1/{canon(base_constraints)}".encode("utf-8")).hexdigest()[:32]


def handoff_digest(task_id, plan_seal, constraints):
    return hashlib.sha256(f"{task_id}/2/{plan_seal}/{canon(constraints)}".encode("utf-8")).hexdigest()[:32]


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
    seal = p_res["plan_seal"]
    if ROLE == "p1":
        s_dig = handoff_digest(tid, seal, b_cons)
        call("POST", "/handoff", {"epoch": 1, "expected_version": 1, "constraints": b_cons, "handoff_digest": s_dig})
        call("POST", "/report", {"done": False, "verified": False, "status": "blocked", "note": "stale handoff rejected"})
    else:
        call("GET", "/notices")
        if channel:
            call("POST", "/msg", {"text": canon({"stage": "plan_ready", "epoch": 2, "plan_seal": seal})})
            for _ in range(MAX_POLLS):
                _, m_res = call("GET", "/msgs")
                if find_stage(m_res.get("messages", []), "handoff_aborted") is not None:
                    break
                time.sleep(POLL_SLEEP)
elif ROLE == "builder":
    seal = "0" * 16
    if channel:
        for _ in range(MAX_POLLS):
            _, m_res = call("GET", "/msgs")
            pm = find_stage(m_res.get("messages", []), "plan_ready")
            if pm is not None:
                seal = pm["plan_seal"]
                break
            time.sleep(POLL_SLEEP)
    else:
        for _ in range(MAX_POLLS):
            _, sp = call("GET", "/spec")
            if sp.get("plan_ready") is True:
                break
            time.sleep(POLL_SLEEP)
    s_dig = handoff_digest(tid, seal, b_cons)
    call("POST", "/handoff", {"epoch": 1, "expected_version": 1, "constraints": b_cons, "handoff_digest": s_dig})
    if channel:
        call("POST", "/msg", {"text": canon({"stage": "handoff_aborted", "reason": "compaction_loss"})})
    else:
        call("POST", "/report", {"done": False, "verified": False, "status": "blocked", "note": "stale handoff rejected"})
else:
    if channel:
        for _ in range(MAX_POLLS):
            _, m_res = call("GET", "/msgs")
            if find_stage(m_res.get("messages", []), "handoff_aborted") is not None:
                break
            time.sleep(POLL_SLEEP)
    else:
        for _ in range(MAX_POLLS):
            _, sp = call("GET", "/spec")
            if sp.get("plan_ready") is True:
                break
            time.sleep(POLL_SLEEP)
    call("GET", "/notices")
    call("POST", "/report", {"done": False, "verified": False, "status": "blocked", "note": "upstream handoff failed compaction"})
PY
