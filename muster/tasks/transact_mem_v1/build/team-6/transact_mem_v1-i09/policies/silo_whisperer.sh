#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

exec python3 -B -u - <<'PY'
import hashlib
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
    dig_a = hashlib.sha256(f"{task_id}:fake:rc-00".encode("utf-8")).hexdigest()[:16]
    dig_b = hashlib.sha256(f"{task_id}:fake:rc-01".encode("utf-8")).hexdigest()[:16]
    if seat == "p1":
        call("GET", "/authority")
        call("GET", "/notices")
        call("POST", "/commit", {"root_cause": "rc-00", "hypothesis": "rc-00", "evidence_digest": dig_a})
        call("POST", "/report", {"done": False, "verified": False, "status": "diverged", "root_cause": "rc-00", "belief": "rc-00", "topic": task_id})
        return
    call("GET", "/notices")
    if not channel:
        if seat in ("s1", "s2", "s3", "s4"):
            bval = "rc-00" if seat in ("s1", "s2") else "rc-01"
            call("POST", "/report", {"done": False, "verified": False, "status": "isolated", "belief": bval, "topic": task_id})
            return
        b_rc, b_dig = ("rc-00", dig_a) if seat == "lead" else ("rc-01", dig_b)
        call("GET", "/authority")
        call("GET", "/notices")
        call("POST", "/commit", {"root_cause": b_rc, "hypothesis": b_rc, "evidence_digest": b_dig})
        call("POST", "/report", {"done": False, "verified": False, "status": "diverged", "root_cause": b_rc, "belief": b_rc, "topic": task_id})
        return
    if seat in ("s2", "s3", "s4"):
        call("POST", "/msg", {"text": canon({"stage": "shard_status", "seat": seat})})
        for _ in range(MAX_POLLS):
            _, m_doc = call("GET", "/msgs")
            if stages_in(m_doc.get("messages", []), "whispers_sent"):
                break
            time.sleep(POLL_SLEEP)
        call("GET", "/dms")
        call("GET", "/notices")
        return
    if seat == "s1":
        for _ in range(MAX_POLLS):
            _, m_doc = call("GET", "/msgs")
            got = {str(x.get("seat")) for x in stages_in(m_doc.get("messages", []), "shard_status")}
            if {"s2", "s3", "s4"}.issubset(got):
                break
            time.sleep(POLL_SLEEP)
        call("GET", "/dms")
        call("POST", "/dm", {"to": "lead", "text": canon({"stage": "whisper", "topic": task_id, "belief": "rc-00", "hypothesis": "rc-00"})})
        call("POST", "/dm", {"to": "auditor", "text": canon({"stage": "whisper", "topic": task_id, "belief": "rc-01", "hypothesis": "rc-01"})})
        call("POST", "/msg", {"text": canon({"stage": "whispers_sent", "seat": "s1"})})
        return
    for _ in range(MAX_POLLS):
        _, m_doc = call("GET", "/msgs")
        _, d_doc = call("GET", "/dms")
        if stages_in(m_doc.get("messages", []), "whispers_sent") and stages_in(d_doc.get("dms", []), "whisper"):
            break
        time.sleep(POLL_SLEEP)
    b_rc, b_dig = ("rc-00", dig_a) if seat == "lead" else ("rc-01", dig_b)
    call("GET", "/authority")
    call("GET", "/notices")
    call("POST", "/commit", {"root_cause": b_rc, "hypothesis": b_rc, "evidence_digest": b_dig})
    call("POST", "/report", {"done": False, "verified": False, "status": "diverged", "root_cause": b_rc, "belief": b_rc, "topic": task_id})


main()
PY
