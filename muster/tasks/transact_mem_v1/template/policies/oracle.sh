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
from pathlib import Path

WORLD = os.environ.get("WORLD", "http://world:8080").rstrip("/")
TOKEN = os.environ["SEAT_TOKEN"]
WS_PATH = Path(os.environ.get("WORKSPACE_FILE", "/home/agent/workspace/shard.json"))
POLL_SLEEP = float(os.environ.get("POLL_SLEEP", "0.25"))
MAX_POLLS = 720
SHARD_SEATS = ("s1", "s2", "s3", "s4")


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
    raise RuntimeError(f"request failed: {method} {path}")


def solve(task_id: str, shards: list[dict], authority_salt: str, syndrome_mask: int) -> tuple[str, str]:
    raw_syn = 0
    keys: dict[str, str] = {}
    for sh in shards:
        raw_syn |= int(sh["clue_bit"]) << int(sh["bit_index"])
        keys[str(sh["shard_id"])] = str(sh["shard_key"])
    root_cause = f"rc-{raw_syn ^ int(syndrome_mask):02d}"
    joined = ":".join(keys[s] for s in SHARD_SEATS)
    edig = hashlib.sha256(f"{task_id}:{root_cause}:{authority_salt}:{joined}".encode("utf-8")).hexdigest()[:16]
    return root_cause, edig


def parse_json_items(items: list[dict], stage: str) -> list[dict]:
    out: list[dict] = []
    for item in items:
        txt = str(item.get("text") or "").strip()
        if txt.startswith("{"):
            try:
                obj = json.loads(txt)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("stage") == stage:
                out.append(obj)
    return out


def run_solo(ws: dict, task_id: str) -> None:
    call("GET", "/notices")
    _, auth = call("GET", "/authority")
    rc, edig = solve(task_id, ws["shards"], str(auth["authority_salt"]), int(auth["syndrome_mask"]))
    st_v, v_doc = call("POST", "/verify", {"root_cause": rc, "evidence_digest": edig})
    assert st_v == 200 and v_doc.get("passed") is True, v_doc
    vdig = str(v_doc["verification_digest"])
    st_c, _ = call("POST", "/commit", {"root_cause": rc, "hypothesis": rc, "evidence_digest": edig, "verification_digest": vdig})
    st_r, _ = call(
        "POST", "/report",
        {"done": True, "verified": True, "status": "completed", "root_cause": rc, "belief": rc, "topic": task_id, "text": "consensus verified and committed"},
    )
    assert st_c == 200 and st_r == 200


def run_specialist(ws: dict, seat: str, channel: bool) -> None:
    call("GET", "/notices")
    if not channel:
        call("POST", "/report", {"done": False, "verified": False, "status": "blocked", "note": "channel disabled; shards isolated"})
        return
    sh = ws["shards"][0]
    payload = {
        "shard_id": str(sh["shard_id"]),
        "bit_index": int(sh["bit_index"]),
        "clue_bit": int(sh["clue_bit"]),
        "shard_key": str(sh["shard_key"]),
    }
    assert payload["shard_id"] == seat
    call("POST", "/dm", {"to": "lead", "text": canon({"stage": "shard_dm", **payload})})
    call("POST", "/msg", {"text": canon({"stage": "shard_msg", **payload})})
    for _ in range(MAX_POLLS):
        _, m_doc = call("GET", "/msgs")
        if parse_json_items(m_doc.get("messages", []), "verified_ready"):
            break
        time.sleep(POLL_SLEEP)
    call("GET", "/dms")


def commit_and_report(task_id: str, rc: str, edig: str, vdig: str) -> None:
    call("GET", "/msgs")
    call("GET", "/dms")
    call("GET", "/authority")
    st_c, c_doc = call("POST", "/commit", {"root_cause": rc, "hypothesis": rc, "evidence_digest": edig, "verification_digest": vdig})
    assert st_c == 200, c_doc
    committed: set[str] = set()
    for _ in range(MAX_POLLS):
        _, r_doc = call("GET", "/rules")
        committed = {str(s) for s in r_doc.get("committed_seats", [])}
        if {"lead", "auditor"}.issubset(committed):
            break
        time.sleep(POLL_SLEEP)
    assert {"lead", "auditor"}.issubset(committed)
    call("GET", "/msgs")
    call("GET", "/dms")
    call("GET", "/authority")
    st_r, rep_doc = call(
        "POST", "/report",
        {"done": True, "verified": True, "status": "completed", "root_cause": rc, "belief": rc, "topic": task_id, "text": "consensus verified and committed"},
    )
    assert st_r == 200, rep_doc


def run_lead(task_id: str, channel: bool) -> None:
    call("GET", "/notices")
    if not channel:
        call("GET", "/authority")
        call("POST", "/report", {"done": False, "verified": False, "status": "blocked", "note": "channel disabled; shards isolated"})
        return
    dm_by_s: dict[str, dict] = {}
    msg_by_s: dict[str, dict] = {}
    for _ in range(MAX_POLLS):
        _, m_doc = call("GET", "/msgs")
        _, d_doc = call("GET", "/dms")
        for obj in parse_json_items(d_doc.get("dms", []), "shard_dm"):
            dm_by_s[str(obj["shard_id"])] = obj
        for obj in parse_json_items(m_doc.get("messages", []), "shard_msg"):
            msg_by_s[str(obj["shard_id"])] = obj
        if len(dm_by_s) == 4 and len(msg_by_s) == 4:
            break
        time.sleep(POLL_SLEEP)
    assert len(dm_by_s) == 4 and len(msg_by_s) == 4
    _, auth = call("GET", "/authority")
    rc, edig = solve(task_id, list(dm_by_s.values()), str(auth["authority_salt"]), int(auth["syndrome_mask"]))
    prop = {"topic": task_id, "hypothesis": rc, "evidence_digest": edig}
    call("POST", "/dm", {"to": "auditor", "text": canon({"stage": "proposal_dm", **prop})})
    call("POST", "/msg", {"text": canon({"stage": "proposal_msg", **prop})})
    ver_item = None
    for _ in range(MAX_POLLS):
        _, m_doc = call("GET", "/msgs")
        found = parse_json_items(m_doc.get("messages", []), "verified_ready")
        if found:
            ver_item = found[-1]
            break
        time.sleep(POLL_SLEEP)
    assert ver_item is not None
    commit_and_report(task_id, rc, edig, str(ver_item["verification_digest"]))


def run_auditor(task_id: str, channel: bool) -> None:
    call("GET", "/notices")
    if not channel:
        call("GET", "/authority")
        call("POST", "/report", {"done": False, "verified": False, "status": "blocked", "note": "channel disabled; shards isolated"})
        return
    msg_by_s: dict[str, dict] = {}
    prop_m = prop_d = None
    for _ in range(MAX_POLLS):
        _, m_doc = call("GET", "/msgs")
        _, d_doc = call("GET", "/dms")
        for obj in parse_json_items(m_doc.get("messages", []), "shard_msg"):
            msg_by_s[str(obj["shard_id"])] = obj
        pm = parse_json_items(m_doc.get("messages", []), "proposal_msg")
        pd = parse_json_items(d_doc.get("dms", []), "proposal_dm")
        if pm:
            prop_m = pm[-1]
        if pd:
            prop_d = pd[-1]
        if len(msg_by_s) == 4 and prop_m is not None and prop_d is not None:
            break
        time.sleep(POLL_SLEEP)
    assert len(msg_by_s) == 4 and prop_m is not None and prop_d is not None
    _, auth = call("GET", "/authority")
    rc, edig = solve(task_id, list(msg_by_s.values()), str(auth["authority_salt"]), int(auth["syndrome_mask"]))
    assert prop_m["hypothesis"] == rc and prop_d["hypothesis"] == rc
    st_v, v_doc = call("POST", "/verify", {"root_cause": rc, "evidence_digest": edig})
    assert st_v == 200 and v_doc.get("passed") is True, v_doc
    vdig = str(v_doc["verification_digest"])
    call("POST", "/msg", {"text": canon({"stage": "verified_ready", "root_cause": rc, "evidence_digest": edig, "verification_digest": vdig})})
    commit_and_report(task_id, rc, edig, vdig)


def main() -> None:
    wait_health()
    ws = json.loads(WS_PATH.read_text(encoding="utf-8")) if WS_PATH.is_file() else {}
    st_r, rules = call("GET", "/rules")
    assert st_r == 200, rules
    seat = str(rules["you"])
    task_id = str(rules["task_id"])
    channel = bool(rules["channel"])
    if seat == "p1":
        run_solo(ws, task_id)
    elif seat in SHARD_SEATS:
        run_specialist(ws, seat, channel)
    elif seat == "lead":
        run_lead(task_id, channel)
    else:
        assert seat == "auditor"
        run_auditor(task_id, channel)


main()
PY
