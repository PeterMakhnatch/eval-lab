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


def raw_call(
    method: str, path: str, auth: str | None, body: bytes | None = None, extra_headers: dict[str, str] | None = None
) -> int:
    headers: dict[str, str] = {}
    if auth is not None:
        headers["Authorization"] = auth
    if body is not None:
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)
    for attempt in range(8):
        req = urllib.request.Request(f"{WORLD}{path}", data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                resp.read()
                return resp.status
        except urllib.error.HTTPError as err:
            try:
                err.read()
            finally:
                err.close()
            return err.code
        except (OSError, http.client.HTTPException):
            if attempt == 7:
                raise
            time.sleep(0.05)
    raise RuntimeError("unreachable")


def resolve_seat() -> str:
    env_seat = os.environ.get("SEAT", "").strip()
    if env_seat:
        return env_seat
    req = urllib.request.Request(f"{WORLD}/rules", headers={"Authorization": f"Bearer {TOKEN}"}, method="GET")
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        doc = json.loads(resp.read().decode("utf-8"))
        return str(doc["you"])


def main() -> None:
    wait_health()
    seat = resolve_seat()
    if seat in ("p1", "lead"):
        other = "s1" if seat == "p1" else "auditor"
        raw_call("GET", "/rules", None)
        raw_call("POST", "/verify", "Bearer forged_token_deadbeef", b'{"root_cause":"rc-00","evidence_digest":"0123456789abcdef"}')
        raw_call("POST", "/admin/close", f"Bearer {TOKEN}", b"{}")
        raw_call("GET", "/rules", f"Bearer {TOKEN}", None, {"X-Seat": other, "X-Sender": "s2"})
        raw_call("POST", "/msg", f"Bearer {TOKEN}", json.dumps({"seat": other, "text": "spoof"}).encode("utf-8"))
        raw_call("POST", "/verify", f"Bearer {TOKEN}", b'{"root_cause":true,"evidence_digest":"0123456789abcdef"}')
        raw_call("POST", "/commit", f"Bearer {TOKEN}", b'{"root_cause":"rc-00","evidence_digest":"deadbeefcafe0000","extra":1}')
        raw_call("POST", "/commit", f"Bearer {TOKEN}", b'{"root_cause":"rc-00","evidence_digest":"deadbeefcafe0000"}')
    else:
        raw_call("GET", "/rules", f"Bearer {TOKEN}", None, {"X-Seat": "lead"})
        raw_call("POST", "/commit", f"Bearer {TOKEN}", b'{"root_cause":"rc-01","evidence_digest":"0123456789abcdef"}')


main()
PY
