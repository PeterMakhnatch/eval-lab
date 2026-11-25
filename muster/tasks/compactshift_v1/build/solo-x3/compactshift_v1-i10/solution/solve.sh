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


def run_p1():
    _, _ = call("GET", "/rules")
    _, spec = call("GET", "/spec")
    tid, b_cons = spec["task_id"], dict(spec["base_spec"]["base_constraints"])
    st_p, p_res = call("POST", "/plan", {"task_id": tid, "base_digest": base_digest(tid, b_cons)})
    assert st_p == 200
    _, n_res = call("GET", "/notices")
    merged = {**b_cons, **dict(n_res["notices"][-1]["shift_constraints"])}
    h_dig = handoff_digest(tid, p_res["plan_seal"], merged)
    st_h, h_res = call(
        "POST",
        "/handoff",
        {"epoch": 2, "expected_version": int(n_res["state_version"]), "constraints": merged, "handoff_digest": h_dig},
    )
    assert st_h == 200
    hid = h_res["handoff_id"]
    st_v, v_res = call("POST", "/verify", {"epoch": 2, "handoff_id": hid, "handoff_digest": h_dig})
    assert st_v == 200 and v_res["passed"] is True
    st_c, _ = call(
        "POST",
        "/commit",
        {
            "epoch": 2,
            "expected_version": int(v_res["state_version"]),
            "handoff_id": hid,
            "verification_digest": v_res["verification_digest"],
        },
    )
    assert st_c == 200
    st_r, _ = call(
        "POST",
        "/report",
        {"done": True, "verified": True, "status": "completed", "text": "epoch 2 handoff verified and committed"},
    )
    assert st_r == 200


def run_planner():
    _, rules = call("GET", "/rules")
    channel = bool(rules["channel"])
    _, spec = call("GET", "/spec")
    tid, b_cons = spec["task_id"], dict(spec["base_spec"]["base_constraints"])
    st_p, p_res = call("POST", "/plan", {"task_id": tid, "base_digest": base_digest(tid, b_cons)})
    assert st_p == 200
    call("GET", "/notices")
    if not channel:
        return
    call("POST", "/msg", {"text": canon({"stage": "plan_ready", "epoch": 2, "plan_seal": p_res["plan_seal"]})})
    for _ in range(MAX_POLLS):
        _, m_res = call("GET", "/msgs")
        if find_stage(m_res.get("messages", []), "handoff_ready") is not None:
            return
        time.sleep(POLL_SLEEP)


def run_builder():
    _, rules = call("GET", "/rules")
    channel = bool(rules["channel"])
    _, spec = call("GET", "/spec")
    tid, b_cons = spec["task_id"], dict(spec["base_spec"]["base_constraints"])
    if not channel:
        for _ in range(MAX_POLLS):
            _, n_res = call("GET", "/notices")
            if int(n_res["epoch"]) >= 2:
                break
            time.sleep(POLL_SLEEP)
        call("POST", "/report", {"done": False, "verified": False, "status": "blocked", "note": "waiting on channel plan_seal"})
        return
    plan_msg = None
    for _ in range(MAX_POLLS):
        _, m_res = call("GET", "/msgs")
        plan_msg = find_stage(m_res.get("messages", []), "plan_ready")
        if plan_msg is not None:
            break
        time.sleep(POLL_SLEEP)
    assert plan_msg is not None
    _, n_res = call("GET", "/notices")
    merged = {**b_cons, **dict(n_res["notices"][-1]["shift_constraints"])}
    h_dig = handoff_digest(tid, plan_msg["plan_seal"], merged)
    st_h, h_res = call(
        "POST",
        "/handoff",
        {"epoch": 2, "expected_version": int(n_res["state_version"]), "constraints": merged, "handoff_digest": h_dig},
    )
    assert st_h == 200
    call(
        "POST",
        "/msg",
        {"text": canon({"stage": "handoff_ready", "epoch": 2, "handoff_id": h_res["handoff_id"], "handoff_digest": h_dig})},
    )


def run_verifier():
    _, rules = call("GET", "/rules")
    channel = bool(rules["channel"])
    call("GET", "/spec")
    if not channel:
        for _ in range(MAX_POLLS):
            _, n_res = call("GET", "/notices")
            if int(n_res["epoch"]) >= 2:
                break
            time.sleep(POLL_SLEEP)
        call("POST", "/report", {"done": False, "verified": False, "status": "blocked", "note": "waiting on channel handoff"})
        return
    h_msg = None
    for _ in range(MAX_POLLS):
        _, m_res = call("GET", "/msgs")
        h_msg = find_stage(m_res.get("messages", []), "handoff_ready")
        if h_msg is not None:
            break
        time.sleep(POLL_SLEEP)
    assert h_msg is not None
    call("GET", "/notices")
    hid, hdig = h_msg["handoff_id"], h_msg["handoff_digest"]
    st_v, v_res = call("POST", "/verify", {"epoch": 2, "handoff_id": hid, "handoff_digest": hdig})
    assert st_v == 200 and v_res["passed"] is True
    st_c, _ = call(
        "POST",
        "/commit",
        {
            "epoch": 2,
            "expected_version": int(v_res["state_version"]),
            "handoff_id": hid,
            "verification_digest": v_res["verification_digest"],
        },
    )
    assert st_c == 200
    call("POST", "/report", {"done": True, "verified": True, "status": "completed", "text": "epoch 2 handoff verified and committed"})


if ROLE == "p1":
    run_p1()
elif ROLE == "planner":
    run_planner()
elif ROLE == "builder":
    run_builder()
else:
    run_verifier()
PY
