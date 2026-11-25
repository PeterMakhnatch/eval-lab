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


def request_world(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
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
            payload = exc.read().decode("utf-8")
            return exc.code, (json.loads(payload) if payload else {})
        except (urllib.error.URLError, TimeoutError, ConnectionResetError, http.client.RemoteDisconnected):
            if attempt == 5:
                raise
            time.sleep(0.05)
    raise RuntimeError("unreachable")


def solve_cfg(triples: list[list[int]], regime: int) -> int:
    for cfg_id in range(256):
        ok = True
        for bmask, bpar, rmask in triples:
            want = bpar ^ (((regime & rmask).bit_count()) & 1)
            if (((cfg_id & bmask).bit_count()) & 1) != want:
                ok = False
                break
        if ok:
            return cfg_id
    return 0


def probe_active_regime() -> tuple[int, str]:
    st, p0 = request_world("POST", "/probe", {"regime": 0})
    assert st == 200, p0
    if p0.get("matched") is True and isinstance(p0.get("regime_code"), str):
        return 0, p0["regime_code"]
    r_true = int(p0["observed_band"]) * 4 + int(p0["observed_offset"])
    st2, p1 = request_world("POST", "/probe", {"regime": r_true})
    assert st2 == 200 and p1.get("matched") is True, p1
    return r_true, str(p1["regime_code"])


def main() -> None:
    ws = json.loads(WS_PATH.read_text(encoding="utf-8"))
    task_id = str(ws["task_id"])
    local_triples = [
        [int(c["bit_mask"]), int(c["base_parity"]), int(c["regime_mask"])]
        for c in ws["local_constraints"]
    ]
    st_r, rules = request_world("GET", "/rules")
    assert st_r == 200, rules
    seat = str(rules["you"])
    seats = [str(s) for s in rules["seats"]]
    channel = bool(rules["channel"])
    barrier_on = bool(rules["barrier"])

    if not channel:
        r_true, r_code = probe_active_regime()
        cfg_id = solve_cfg(local_triples, r_true)
    else:
        msg_obj: dict[str, object] = {"k": "pf", "m": local_triples}
        if seat == seats[0]:
            r_loc, c_loc = probe_active_regime()
            msg_obj["r"] = r_loc
            msg_obj["c"] = c_loc
        msg_txt = json.dumps(msg_obj, separators=(",", ":"))
        assert len(msg_txt.encode("utf-8")) <= int(rules["max_msg_bytes"])
        st_m, _ = request_world("POST", "/msg", {"text": msg_txt})
        assert st_m == 200

        by_seat: dict[str, list[list[int]]] = {}
        r_found: int | None = None
        c_found: str | None = None
        for _ in range(120):
            st_g, m_resp = request_world("GET", "/msgs")
            assert st_g == 200
            for item in m_resp.get("messages", []):
                sender = str(item.get("from") or "")
                try:
                    parsed = json.loads(str(item.get("text") or ""))
                except ValueError:
                    continue
                if isinstance(parsed, dict) and parsed.get("k") == "pf":
                    by_seat[sender] = parsed["m"]
                    if isinstance(parsed.get("r"), int) and isinstance(parsed.get("c"), str):
                        r_found, c_found = int(parsed["r"]), str(parsed["c"])
            if len(by_seat) == len(seats) and r_found is not None and c_found is not None:
                break
            time.sleep(0.25)
        assert len(by_seat) == len(seats) and r_found is not None and c_found is not None
        r_true, r_code = r_found, c_found
        all_triples = [row for s_nm in sorted(by_seat) for row in by_seat[s_nm]]
        cfg_id = solve_cfg(all_triples, r_true)

    digest = hashlib.sha256(f"{task_id}:{r_true}:{r_code}:{cfg_id:03d}".encode("utf-8")).hexdigest()[:16]
    if barrier_on:
        st_b, b_resp = request_world("POST", "/barrier", {"phase": 1, "digest": digest})
        assert st_b == 200, b_resp
        if b_resp.get("satisfied") is not True:
            for _ in range(120):
                st_bg, bg_resp = request_world("GET", "/barrier?phase=1")
                assert st_bg == 200
                if bg_resp.get("satisfied") is True:
                    break
                time.sleep(0.25)
    if channel:
        request_world("GET", "/msgs")
    st_c, _ = request_world("POST", "/commit", {"phase": 1, "contract_digest": digest, "regime": r_true})
    if st_c == 409 and channel:
        request_world("GET", "/msgs")
        request_world("POST", "/commit", {"phase": 1, "contract_digest": digest, "regime": r_true})


main()
PY
