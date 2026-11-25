#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

python3 -B - <<'PY'
import hashlib
import json
import os
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
    req = urllib.request.Request(f"{WORLD}{path}", data=raw, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8")
        return exc.code, (json.loads(payload) if payload else {})


def main() -> None:
    ws = json.loads(WS_PATH.read_text(encoding="utf-8"))
    task_id = str(ws["task_id"])
    triples = [
        [int(c["bit_mask"]), int(c["base_parity"]), int(c["regime_mask"])]
        for c in ws["local_constraints"]
    ]
    _, rules = call_world("GET", "/rules")
    seat = str(rules["you"])
    seats = [str(s) for s in rules["seats"]]
    local_cfg = 0
    for cfg_id in range(256):
        if all((((cfg_id & bm).bit_count()) & 1) == bp for bm, bp, _ in triples):
            local_cfg = cfg_id
            break
    local_digest = hashlib.sha256(f"{task_id}:{seat}:00000000:{local_cfg:03d}".encode("utf-8")).hexdigest()[:16]
    if bool(rules.get("barrier")) and bool(rules.get("fenced")) and len(seats) > 1:
        call_world("POST", "/barrier", {"phase": 1, "digest": local_digest})
    call_world("POST", "/commit", {"phase": 1, "contract_digest": local_digest, "regime": 0})


main()
PY
