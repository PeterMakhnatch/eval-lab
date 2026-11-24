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

VOTE_RE = re.compile(r"converged on candidate (C[1-6])\b")


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


def pick_from_crowd_or_part_a(task_state, messages):
    votes = {}
    for msg in messages:
        match = VOTE_RE.search(msg.get("text", ""))
        if match:
            slot = match.group(1)
            votes[slot] = votes.get(slot, 0) + 1
    if votes:
        return max(sorted(votes), key=lambda key: votes[key])
    part_a = task_state["private_clue"]["part_a"]
    for cand in task_state["candidates"]:
        if (cand["base_val"] * cand["factor"] + part_a["salt_a"]) % part_a["mod_p"] == part_a["target_rem_a"]:
            return cand["id"]
    return task_state["candidates"][0]["id"]


def run_policy(send, token):
    status, task_state = send("GET", "/task_state", token=token)
    assert status == 200
    cap = task_state["max_contribute_per_round"]
    for _ in range(task_state["max_rounds"]):
        status, team = send("GET", "/team_state", token=token)
        assert status == 200
        if team["goal_settled"]:
            break
        amt = min(cap, team["remaining_needed"])
        status, _ = send("POST", "/contribute", token=token, body={"amount": amt})
        assert status == 200
    status, msg_resp = send("GET", "/msgs", token=token)
    assert status == 200
    chosen = pick_from_crowd_or_part_a(task_state, msg_resp["messages"])
    status, _ = send("POST", "/submit_choice", token=token, body={"candidate": chosen})
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
