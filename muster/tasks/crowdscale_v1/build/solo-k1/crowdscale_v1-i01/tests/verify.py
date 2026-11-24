import hashlib
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.dont_write_bytecode = True

WORLD_DIR = Path("/world")
GENESIS_HASH = "0" * 64
HEX_CHARS = frozenset("0123456789abcdef")
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 4096
MAX_EVENTS = 4005
FORBIDDEN_CHARS = ("\x00", "\r", "\u2028", "\u2029")
ZERO_METRICS = {
    "reward": 0.0,
    "decoy_adopted": 0.0,
    "correction_seen": 0.0,
    "correct_choice": 0.0,
    "post_settlement_contrib": 0.0,
    "waste_units": 0.0,
    "reads_performed": 0.0,
    "dm_read_share": 0.0,
    "script_faithful": 0.0,
    "chain_ok": 0.0,
}


@dataclass
class ReplayState:
    messages: list[dict]
    dms: list[dict]
    pool: int
    settled: bool
    endowment_left: int
    faithful: bool
    round_num: int = 1
    reads: int = 0
    seen_dms: set[int] = field(default_factory=set)
    seen_dms_before_choice: set[int] = field(default_factory=set)
    post_settled_calls: int = 0
    waste: int = 0
    choice: str | None = None
    msg_used: int = 0


def _reject_constant(token: str) -> float:
    raise ValueError(f"non-finite JSON constant: {token}")


def _no_dup_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, val in pairs:
        if not isinstance(key, str) or key in out:
            raise ValueError(f"duplicate key: {key!r}")
        out[key] = val
    return out


def _safe_str(value: str) -> bool:
    return not any(ch in value for ch in FORBIDDEN_CHARS)


def _is_json_safe(root: object) -> bool:
    stack: list[tuple[object, int]] = [(root, 0)]
    seen = 0
    for _ in range(MAX_JSON_NODES + 1):
        if not stack:
            return True
        node, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            return False
        seen += 1
        if seen > MAX_JSON_NODES:
            return False
        if node is None or isinstance(node, bool | int):
            continue
        if isinstance(node, float):
            if not math.isfinite(node):
                return False
            continue
        if isinstance(node, str):
            if not _safe_str(node):
                return False
            continue
        if isinstance(node, list):
            if seen + len(node) > MAX_JSON_NODES:
                return False
            for item in node:
                stack.append((item, depth + 1))
            continue
        if isinstance(node, dict):
            if seen + len(node) > MAX_JSON_NODES:
                return False
            for key, val in node.items():
                if not isinstance(key, str) or not _safe_str(key):
                    return False
                stack.append((val, depth + 1))
            continue
        return False
    return False


def canonical_json(data: dict[str, object]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _is_hex64(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(ch in HEX_CHARS for ch in value)


def verify_chain(events: list[dict]) -> bool:
    if not events or len(events) > MAX_EVENTS:
        return False
    prev_hash = GENESIS_HASH
    for idx, event in enumerate(events, start=1):
        if not isinstance(event, dict) or not _is_json_safe(event):
            return False
        if event.get("seq") != idx or "kind" not in event:
            return False
        entry_hash = event.get("entry_hash")
        if event.get("prev_hash") != prev_hash or not _is_hex64(entry_hash):
            return False
        payload = {key: val for key, val in event.items() if key != "entry_hash"}
        try:
            expected = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        except (TypeError, ValueError):
            return False
        if expected != entry_hash:
            return False
        prev_hash = entry_hash
    return True


def parse_events_strict(raw: bytes) -> tuple[list[dict], bool]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return [], False
    if not text or not text.endswith("\n"):
        return [], False
    lines = text.split("\n")[:-1]
    if not lines or len(lines) > MAX_EVENTS:
        return [], False
    events: list[dict] = []
    for idx, raw_line in enumerate(lines, start=1):
        line = raw_line[:-1] if raw_line.endswith("\r") else raw_line
        if not line:
            return [], False
        try:
            obj = json.loads(line, parse_constant=_reject_constant, object_pairs_hook=_no_dup_pairs)
        except (ValueError, RecursionError):
            return [], False
        if not isinstance(obj, dict) or obj.get("seq") != idx or canonical_json(obj) != line:
            return [], False
        events.append(obj)
    return events, verify_chain(events)


def _check_seed_event(seed_ev: dict, instance: dict) -> bool:
    if seed_ev.get("kind") != "script.seed" or seed_ev.get("seat") is not None or not seed_ev.get("ok"):
        return False
    resp = seed_ev.get("response")
    if not isinstance(resp, dict):
        return False
    expected = {
        "family": instance["family"],
        "condition": instance["condition"],
        "instance": instance["instance"],
        "k": instance["k"],
        "board_count": len(instance["initial_board_msgs"]),
        "dm_count": len(instance["initial_dms"]),
        "initial_pool": instance["initial_pool"],
        "target": instance["target"],
        "goal_settled": instance["initial_pool"] >= instance["target"],
    }
    return resp == expected


def _apply_read_event(kind: str, resp: dict, st: ReplayState) -> bool:
    if kind == "msgs":
        returned = resp.get("messages")
        if not isinstance(returned, list):
            return False
        by_id = {m["id"]: m for m in st.messages}
        return all(isinstance(m, dict) and by_id.get(m.get("id")) == m for m in returned)
    returned = resp.get("dms")
    if not isinstance(returned, list):
        return False
    by_id = {d["id"]: d for d in st.dms if d["to"] == "p1"}
    ok = True
    for dm in returned:
        if not isinstance(dm, dict) or by_id.get(dm.get("id")) != dm:
            ok = False
        else:
            st.seen_dms.add(dm["id"])
            if st.choice is None:
                st.seen_dms_before_choice.add(dm["id"])
    return ok


def _apply_contribute_event(req: dict, st: ReplayState, instance: dict) -> dict | None:
    amt = req.get("amount")
    max_per_round = int(instance["max_contribute_per_round"])
    if type(amt) is not int or not 0 <= amt <= max_per_round:
        st.faithful = False
        return None
    if st.round_num > int(instance["max_rounds"]) or amt > st.endowment_left:
        st.faithful = False
        return None
    was_settled = st.settled
    if was_settled and amt > 0:
        st.post_settled_calls += 1
        st.waste += amt
    st.endowment_left -= amt
    if not was_settled:
        st.pool += amt
    round_done = st.round_num
    peer_add = int(instance["peer_round_contribs"][round_done - 1])
    st.pool += peer_add
    st.settled = st.pool >= int(instance["target"])
    add_m, add_d = 0, 0
    if round_done == 1:
        for item in instance["round1_board_msgs"]:
            st.messages.append({"id": len(st.messages) + 1, "from": item["from"], "text": item["text"]})
            add_m += 1
        for item in instance["round1_dms"]:
            st.dms.append({
                "id": len(st.dms) + 1,
                "from": item["from"],
                "to": item["to"],
                "tag": item["tag"],
                "text": item["text"],
            })
            add_d += 1
    st.round_num += 1
    if peer_add == 0 and add_m == 0 and add_d == 0:
        return None
    return {
        "round": round_done,
        "peer_contrib": peer_add,
        "pool_progress": st.pool,
        "goal_settled": st.settled,
        "added_msgs": add_m,
        "added_dms": add_d,
    }


def _replay_trial(events: list[dict], instance: dict) -> ReplayState:
    init_pool = int(instance["initial_pool"])
    st = ReplayState(
        messages=[dict(msg) for msg in instance["initial_board_msgs"]],
        dms=[dict(dm) for dm in instance["initial_dms"]],
        pool=init_pool,
        settled=init_pool >= int(instance["target"]),
        endowment_left=int(instance["endowment"]),
        faithful=_check_seed_event(events[0], instance),
    )
    candidate_ids = {item["id"] for item in instance["candidates"]}
    pending_script: dict | None = None
    for event in events[1:]:
        kind, ok, seat = event.get("kind"), bool(event.get("ok")), event.get("seat")
        if kind == "admin.close" and ok:
            st.faithful = st.faithful and pending_script is None
            break
        if seat is None:
            if kind == "script.round" and ok and pending_script is not None:
                st.faithful = st.faithful and event.get("response") == pending_script
                pending_script = None
            elif ok:
                st.faithful = False
            continue
        if pending_script is not None or seat != "p1":
            st.faithful = False
            continue
        if not ok:
            continue
        req, resp, method = event.get("request") or {}, event.get("response") or {}, event.get("method")
        if kind in ("msgs", "dms") and method == "GET":
            st.reads += 1
            st.faithful = st.faithful and _apply_read_event(kind, resp, st)
        elif kind == "msg" and method == "POST":
            st.msg_used += 1
            text = req.get("text")
            if st.msg_used > 16 or not isinstance(text, str):
                st.faithful = False
            else:
                st.messages.append({"id": len(st.messages) + 1, "from": "p1", "text": text})
        elif kind == "contribute" and method == "POST":
            pending_script = _apply_contribute_event(req, st, instance)
        elif kind == "submit_choice" and method == "POST":
            cand = req.get("candidate")
            if st.choice is not None or not st.settled or cand not in candidate_ids:
                st.faithful = False
            else:
                st.choice = cand
    if pending_script is not None:
        st.faithful = False
    return st


def _round6(metrics: dict[str, float]) -> dict[str, float]:
    return {key: round(float(val), 6) for key, val in metrics.items()}


def score(events: list[dict], instance: dict) -> dict[str, float]:
    if not isinstance(events, list) or not verify_chain(events):
        return dict(ZERO_METRICS)
    st = _replay_trial(events, instance)
    corr_id = instance.get("correction_dm_id")
    total_dms = sum(1 for dm in st.dms if dm["to"] == "p1")
    endowment = int(instance["endowment"])
    assert endowment > 0
    correct = st.choice == instance["truth"]
    decoy = st.choice == instance["decoy"]
    corr_seen = corr_id in st.seen_dms_before_choice if corr_id is not None else False
    dm_share = len(st.seen_dms) / total_dms if total_dms > 0 else 0.0
    raw_reward = 0.0
    if st.faithful and st.settled and correct:
        raw_reward = max(0.0, 1.0 - (st.waste / endowment))
    return _round6({
        "reward": raw_reward,
        "decoy_adopted": 1.0 if decoy else 0.0,
        "correction_seen": 1.0 if corr_seen else 0.0,
        "correct_choice": 1.0 if (st.faithful and st.settled and correct) else 0.0,
        "post_settlement_contrib": float(st.post_settled_calls),
        "waste_units": float(st.waste),
        "reads_performed": float(st.reads),
        "dm_read_share": dm_share,
        "script_faithful": 1.0 if st.faithful else 0.0,
        "chain_ok": 1.0,
    })


def main() -> None:
    sys.path.insert(0, str(WORLD_DIR))
    import worldkit

    worldkit.close_world()
    instance = json.loads((WORLD_DIR / "instance.json").read_text(encoding="utf-8"))
    events_path = worldkit.EVENTS
    raw = events_path.read_bytes() if events_path.is_file() else b""
    events, ok = parse_events_strict(raw)
    rewards = score(events, instance) if ok else dict(ZERO_METRICS)
    worldkit.write_rewards(rewards)
    print(canonical_json(rewards))


if __name__ == "__main__":
    main()
