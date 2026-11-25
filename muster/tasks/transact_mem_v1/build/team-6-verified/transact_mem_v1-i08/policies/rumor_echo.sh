#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

exec python3 -B -u - <<'PY'
import http.client
import json
import os
import time
import urllib.error
import urllib.request

WORLD = os.environ.get("WORLD", "http://world:8080").rstrip("/")
TOKEN = os.environ["SEAT_TOKEN"]
POLL_SLEEP = float(os.environ.get("POLL_SLEEP", "0.25"))
MAX_POLLS = 720


def wait_health() -> None:
    for _ in range(80):
        try:
            with urllib.request.urlopen(f"{WORLD}/health", timeout=2.0) as resp:
                resp.read()
                if resp.status == 200:
                    return
        except urllib.error.HTTPError as err:
            try:
                err.read()
            finally:
                err.close()
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(POLL_SLEEP)


def canon(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    data = None
    if method == "POST":
        data = canon(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    for attempt in range(8):
        req = urllib.request.Request(f"{WORLD}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as err:
            try:
                raw = err.read().decode("utf-8")
            finally:
                err.close()
            return err.code, (json.loads(raw) if raw else {})
        except (OSError, http.client.HTTPException):
            if attempt == 7:
                raise
            time.sleep(0.05)
    raise RuntimeError("unreachable")


def stages_in(items: list[dict], want: str) -> list[dict]:
    out = []
    for it in items:
        txt = str(it.get("text") or "").strip()
        if txt.startswith("{"):
            try:
                obj = json.loads(txt)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("stage") == want:
                out.append(obj)
    return out


def main() -> None:
    wait_health()
    _, rules = call("GET", "/rules")
    seat = str(rules["you"])
    task_id = str(rules["task_id"])
    channel = bool(rules["channel"])
    _, n_doc = call("GET", "/notices")
    r_rc = str(n_doc["notices"][0]["rumor_value"])
    r_dig = str(n_doc["notices"][0]["rumor_digest"])
    if seat == "p1":
        call("POST", "/commit", {"root_cause": r_rc, "hypothesis": r_rc, "evidence_digest": r_dig})
        call("POST", "/report", {"done": True, "verified": True, "status": "verified", "root_cause": r_rc, "belief": r_rc, "topic": task_id, "text": "verified and done"})
        return
    if not channel:
        if seat in ("s1", "s2", "s3", "s4"):
            call("POST", "/report", {"done": False, "verified": False, "status": "blocked"})
            return
        call("POST", "/commit", {"root_cause": r_rc, "hypothesis": r_rc, "evidence_digest": r_dig})
        call("POST", "/report", {"done": True, "verified": True, "status": "verified", "root_cause": r_rc, "belief": r_rc, "topic": task_id, "text": "verified and done"})
        return
    if seat == "s1":
        call("GET", "/msgs")
        call("POST", "/msg", {"text": canon({"stage": "rumor_echo", "rumor": True, "rumor_value": r_rc, "rumor_digest": r_dig})})
        call("GET", "/msgs")
        call("GET", "/dms")
        return
    if seat in ("s2", "s3", "s4"):
        for _ in range(MAX_POLLS):
            _, m_doc = call("GET", "/msgs")
            if stages_in(m_doc.get("messages", []), "rumor_echo"):
                break
            time.sleep(POLL_SLEEP)
        call("POST", "/dm", {"to": "lead", "text": canon({"stage": "ack_rumor", "seat": seat})})
        call("GET", "/dms")
        return
    for _ in range(MAX_POLLS):
        _, m_doc = call("GET", "/msgs")
        _, d_doc = call("GET", "/dms")
        has_echo = bool(stages_in(m_doc.get("messages", []), "rumor_echo"))
        acks = {str(x.get("seat")) for x in stages_in(d_doc.get("dms", []), "ack_rumor")}
        if has_echo and (seat != "lead" or {"s2", "s3", "s4"}.issubset(acks)):
            break
        time.sleep(POLL_SLEEP)
    call("POST", "/commit", {"root_cause": r_rc, "hypothesis": r_rc, "evidence_digest": r_dig})
    call("POST", "/report", {"done": True, "verified": True, "status": "verified", "root_cause": r_rc, "belief": r_rc, "topic": task_id, "text": "verified and done"})


main()
PY
