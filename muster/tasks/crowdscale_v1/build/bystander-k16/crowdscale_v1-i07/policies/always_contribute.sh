#!/bin/bash
set -euo pipefail
PYTHONDONTWRITEBYTECODE=1 exec python3 -B -u - <<'PY'
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request

OFFSET_RE = re.compile(r"voucher_offset=(\d+)")


def http_send(world_url, method, path, token=None, body=None, extra_headers=None):
    headers = dict(extra_headers or {})
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    for attempt in range(6):
        req = urllib.request.Request(f"{world_url}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8")
            return err.code, json.loads(raw) if raw else {}
        except (urllib.error.URLError, ConnectionError, TimeoutError, http.client.HTTPException):
            if attempt == 5:
                raise
            time.sleep(0.05 * (attempt + 1))
    raise RuntimeError("unreachable")


def solve_candidate(task_state, dms):
    part_a = task_state["private_clue"]["part_a"]
    part_b = task_state["private_clue"]["part_b"]
    offset = part_b["voucher_offset"]
    if offset is None:
        for dm in reversed(dms):
            if dm.get("tag") == "AUDIT_ALERT":
                match = OFFSET_RE.search(dm.get("text", ""))
                if match:
                    offset = int(match.group(1))
                    break
    assert isinstance(offset, int)
    for cand in task_state["candidates"]:
        ok_a = (cand["base_val"] * cand["factor"] + part_a["salt_a"]) % part_a["mod_p"] == part_a["target_rem_a"]
        ok_b = (cand["base_val"] + offset) % part_b["mod_q"] == part_b["target_rem_b"]
        if ok_a and ok_b:
            return cand["id"]
    raise AssertionError("no matching candidate")


def run_policy(send, token):
    status, task_state = send("GET", "/task_state", token=token)
    assert status == 200
    cap = task_state["max_contribute_per_round"]
    for _ in range(task_state["max_rounds"]):
        status, _ = send("POST", "/contribute", token=token, body={"amount": cap})
        assert status == 200
    status, dm_resp = send("GET", "/dms", token=token)
    assert status == 200
    winner = solve_candidate(task_state, dm_resp["dms"])
    status, _ = send("POST", "/submit_choice", token=token, body={"candidate": winner})
    assert status == 200


if __name__ == "__main__":
    world_url = os.environ["WORLD"]
    seat_token = os.environ["SEAT_TOKEN"]
    run_policy(
        lambda m, p, token=seat_token, body=None, extra_headers=None: http_send(
            world_url, m, p, token, body, extra_headers
        ),
        seat_token,
    )
PY
