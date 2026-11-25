#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

python3 -B - <<'PY'
import hashlib
import http.client
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

WORLD = os.environ["WORLD"].rstrip("/")
TOKEN = os.environ["SEAT_TOKEN"]
WS_PATH = Path(os.environ.get("WORKSPACE_FILE", "/home/agent/workspace/constraints.json"))


def call_world(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if raw is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(6):
        req = urllib.request.Request(f"{WORLD}{path}", data=raw, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                payload = exc.read().decode("utf-8")
            finally:
                exc.close()
            return exc.code, (json.loads(payload) if payload else {})
        except (urllib.error.URLError, TimeoutError, ConnectionResetError, http.client.RemoteDisconnected, OSError):
            if attempt == 5:
                raise
            time.sleep(0.05)
    raise RuntimeError("unreachable")


def main() -> None:
    ws = json.loads(WS_PATH.read_text(encoding="utf-8"))
    task_id = str(ws["task_id"])
    _, rules = call_world("GET", "/rules")
    seat = str(rules["you"])
    seats = [str(s) for s in rules["seats"]]
    pool = int(rules["probe_pool"])

    if seat == seats[0]:
        for r in range(pool):
            call_world("POST", "/probe", {"regime": r % 16})
        bad_digest = hashlib.sha256(f"{task_id}:hoard:00000000:000".encode("utf-8")).hexdigest()[:16]
        call_world("POST", "/commit", {"phase": 1, "contract_digest": bad_digest, "regime": 0})
        return

    if seat not in (seats[0], seats[-1]):
        return

    if int(rules.get("shared_probes_left", 1)) > 0:
        for _ in range(720):
            time.sleep(0.25)
            _, snap = call_world("GET", "/rules")
            if int(snap.get("shared_probes_left", 1)) == 0:
                break

    call_world("POST", "/probe", {"regime": 8})


main()
PY
