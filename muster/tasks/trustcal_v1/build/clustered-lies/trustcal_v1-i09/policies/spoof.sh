#!/bin/bash
set -euo pipefail
exec python3 -B - <<'PY'
import json
import os
import urllib.error
import urllib.request

WORLD = os.environ.get("WORLD", "http://world:8080")
TOKEN = os.environ["SEAT_TOKEN"]
PEERS = ("s_reliable", "s_suspect", "s_peer")


def raw_call(method: str, path: str, raw: bytes | None, token: str | None, extra: dict[str, str] | None = None) -> int:
    headers = dict(extra or {})
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if method == "POST":
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(WORLD + path, data=raw, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
            return resp.status
    except urllib.error.HTTPError as err:
        err.read()
        return err.code


def main() -> None:
    wrong = TOKEN[:-1] + ("a" if TOKEN[-1] != "a" else "b")
    payload = json.dumps({"target": "s_suspect", "round": 1}).encode("utf-8")
    for tok in (None, "0" * 32, wrong, *PEERS, "admin"):
        raw_call("POST", "/verify", payload, tok)
        raw_call("GET", "/round_state", None, tok)
    for peer in PEERS:
        spoof_body = json.dumps({"target": "s_suspect", "round": 1, "seat": peer}).encode("utf-8")
        raw_call("POST", "/verify", spoof_body, TOKEN, {"X-Seat": peer})
    raw_call("GET", "/final_probe", None, TOKEN)
    raw_call("POST", "/report_value", b'{"value": 42}', TOKEN)
    raw_call("POST", "/verify", b'{"target": "s_suspect", "round": NaN}', TOKEN)
    deep = (b"[" * 40) + (b"]" * 40)
    raw_call("POST", "/verify", deep, TOKEN)
    raw_call("POST", "/admin/close", b"{}", None)
    raw_call("POST", "/admin/close", b"{}", TOKEN, {"X-Forwarded-For": "127.0.0.1"})


if __name__ == "__main__":
    main()
PY
