from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True


def _load_dep(name: str) -> Any:
    existing = sys.modules.get(name)
    if (
        existing is not None
        and (name != "worldkit" or (hasattr(existing, "World") and hasattr(existing.World, "dispatch")))
        and (name != "codec" or hasattr(existing, "ADMIN_CLOSE_RESERVE"))
    ):
        return existing
    here = Path(__file__).resolve().parent
    cands = [
        here / f"{name}.py",
        here.parent / "environment" / "world" / f"{name}.py",
        Path("/world") / f"{name}.py",
    ]
    if len(here.parents) >= 2:
        cands.append(here.parents[1] / "_kit_v2" / f"{name}.py")
    cand = next((p for p in cands if p.is_file()), cands[-1])
    spec = importlib.util.spec_from_file_location(name, cand)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


codec: Any = _load_dep("codec")
worldkit: Any = _load_dep("worldkit")

INSTANCE = Path(os.environ.get("WORLD_INSTANCE", "/world/instance.json"))
SERVICES = ("service_a", "service_b", "service_c")
LOCAL_BUGS = frozenset({"a-local", "b-local", "c-local"})
CROSS_BUGS = frozenset({"ab-chain", "bc-chain", "abc-chain"})
TARGET_BUGS = LOCAL_BUGS | CROSS_BUGS
REQUIRED_SUBMISSION_KEYS = frozenset({"target_bug", "chain", "payload", "expected_breach"})
TOTAL_BUGS = 6
MAX_STR_LEN = 512
MAX_PAYLOAD_BYTES = 512
MAX_PAYLOAD_KEYS = 16
MAX_INT_ABS = 10_000_000
MAX_SCAN_STEPS = 1_024
MAX_CHAIN_BYTES = codec.MAX_WORLD_LOG_BYTES + codec.ADMIN_CLOSE_RESERVE

EXPECTED_CHAINS: dict[str, tuple[str, ...]] = {
    "a-local": ("service_a",),
    "b-local": ("service_b",),
    "c-local": ("service_c",),
    "ab-chain": ("service_a", "service_b"),
    "bc-chain": ("service_b", "service_c"),
    "abc-chain": ("service_a", "service_b", "service_c"),
}

EXPECTED_BREACHES: dict[str, str] = {
    "a-local": "quota_grace_drift",
    "b-local": "fee_floor_zero",
    "c-local": "ttl_skew_stale",
    "ab-chain": "ab_reconcile_mismatch",
    "bc-chain": "bc_shard_collision",
    "abc-chain": "abc_end_to_end_desync",
}


class SimState:
    def __init__(self, instance: dict[str, Any]) -> None:
        self.seats = [str(s) for s in instance["seats"]]
        self.seat_set = set(self.seats)
        self.submit_budget = int(instance["submit_budget"])
        self.global_budget = int(instance["global_submit_budget"])
        self.channel = bool(instance["channel"])
        self.services: dict[str, dict[str, Any]] = instance["services"]
        self.seat_used: dict[str, int] = dict.fromkeys(self.seats, 0)
        self.global_used = 0
        self.logical_tick = 0
        self.closed = False
        self.log_intact = True
        self.faithful = True
        self.first: dict[str, str] = {}
        self.duplicate_submissions = 0
        self.invalid_submissions = 0
        self.claim_conflicts = 0
        self.spoof_hits = 0


def _is_strict_int(value: object) -> bool:
    return type(value) is int and abs(value) <= MAX_INT_ABS


def _norm_tag(value: object) -> str | None:
    cleaned = codec.clean_str(value, MAX_STR_LEN)
    return cleaned.lower() if cleaned is not None else None


def _minimal_mod_hit(scale: int, offset: int, modulus: int) -> int:
    assert 1 <= modulus <= MAX_SCAN_STEPS and scale > 0
    for step in range(1, modulus + 1):
        if (step * scale + offset) % modulus == 0:
            return step
    raise AssertionError("coprime modular equation must have a solution")


def _check_local_witness(target_bug: str, services: dict[str, dict[str, Any]], payload: dict[str, Any]) -> bool:
    if target_bug == "a-local":
        spec_a = services["service_a"]
        units, burst = payload.get("units"), payload.get("burst")
        if not (_is_strict_int(units) and _is_strict_int(burst)):
            return False
        assert isinstance(units, int) and isinstance(burst, int)
        cap = spec_a["quota_limit"] + (spec_a["grace_step"] if burst == 1 else 0)
        return bool(burst == 1 and 1 <= units <= cap and units - spec_a["quota_limit"] == spec_a["grace_step"])
    if target_bug == "b-local":
        spec_b = services["service_b"]
        amount = payload.get("minor_amount")
        if not _is_strict_int(amount):
            return False
        assert isinstance(amount, int)
        if amount < spec_b["min_settle"]:
            return False
        basis = spec_b["fee_basis"]
        return bool(amount // basis == 0 and (amount + 1) // basis == 1)
    assert target_bug == "c-local"
    spec_c = services["service_c"]
    age, retry_bit = payload.get("age_seconds"), payload.get("retry_bit")
    if not (_is_strict_int(age) and _is_strict_int(retry_bit)):
        return False
    assert isinstance(age, int) and isinstance(retry_bit, int)
    limit = spec_c["ttl_seconds"] + spec_c["skew_tolerance"] if retry_bit == 1 else spec_c["ttl_seconds"] - 1
    return bool(retry_bit == 1 and 0 <= age <= limit and age - spec_c["ttl_seconds"] == spec_c["skew_tolerance"])


def _check_cross_witness(target_bug: str, services: dict[str, dict[str, Any]], payload: dict[str, Any]) -> bool:
    spec_a, spec_b, spec_c = services["service_a"], services["service_b"], services["service_c"]
    if target_bug == "ab-chain":
        seq_id, batch_tag = payload.get("seq_id"), payload.get("batch_tag")
        if not (_is_strict_int(seq_id) and _is_strict_int(batch_tag)):
            return False
        assert isinstance(seq_id, int)
        if _norm_tag(payload.get("realm")) != spec_a["realm_prefix"]:
            return False
        if _norm_tag(payload.get("tenant")) != spec_b["tenant_code"] or batch_tag != spec_b["batch_tag"]:
            return False
        want = _minimal_mod_hit(spec_a["scale_factor"], spec_a["header_bias"] + spec_b["batch_tag"], spec_b["reconcile_mod"])
        return bool(1 <= seq_id <= spec_a["quota_limit"] and seq_id == want)
    if target_bug == "bc-chain":
        voucher_no, slot_salt = payload.get("voucher_no"), payload.get("slot_salt")
        if not (_is_strict_int(voucher_no) and _is_strict_int(slot_salt)):
            return False
        if _norm_tag(payload.get("tenant")) != spec_b["tenant_code"]:
            return False
        if _norm_tag(payload.get("channel")) != spec_c["channel_id"] or slot_salt != spec_c["slot_salt"]:
            return False
        want = _minimal_mod_hit(spec_b["fee_basis"], spec_b["batch_tag"] + spec_c["slot_salt"], spec_c["shard_mod"])
        return bool(voucher_no == want)
    assert target_bug == "abc-chain"
    trace_step, checksum, sync_window = payload.get("trace_step"), payload.get("checksum"), payload.get("sync_window")
    if not (_is_strict_int(trace_step) and _is_strict_int(checksum) and _is_strict_int(sync_window)):
        return False
    assert isinstance(trace_step, int)
    tags_ok = (
        _norm_tag(payload.get("realm")) == spec_a["realm_prefix"]
        and _norm_tag(payload.get("tenant")) == spec_b["tenant_code"]
        and _norm_tag(payload.get("channel")) == spec_c["channel_id"]
    )
    expected_sync = spec_c["ttl_seconds"] + spec_c["slot_salt"]
    if not tags_ok or sync_window != expected_sync:
        return False
    base_offset = spec_a["header_bias"] + spec_b["batch_tag"] + spec_b["min_settle"]
    want_step = _minimal_mod_hit(spec_a["scale_factor"], base_offset + expected_sync, spec_c["shard_mod"])
    stage_b = trace_step * spec_a["scale_factor"] + base_offset
    return bool(trace_step == want_step and checksum == stage_b % spec_b["reconcile_mod"])


def _replay_witness(req: object, services: dict[str, dict[str, Any]]) -> str | None:
    if not isinstance(req, dict) or set(req.keys()) != REQUIRED_SUBMISSION_KEYS:
        return None
    target_bug = req.get("target_bug")
    chain = req.get("chain")
    payload = req.get("payload")
    breach = req.get("expected_breach")
    if not isinstance(target_bug, str) or len(target_bug) > MAX_STR_LEN or target_bug not in TARGET_BUGS:
        return None
    if not isinstance(chain, list) or not 1 <= len(chain) <= 3:
        return None
    if any(not isinstance(item, str) or len(item) > MAX_STR_LEN or item not in SERVICES for item in chain):
        return None
    if tuple(chain) != EXPECTED_CHAINS[target_bug] or breach != EXPECTED_BREACHES[target_bug]:
        return None
    if not isinstance(payload, dict) or len(payload) > MAX_PAYLOAD_KEYS:
        return None
    try:
        p_size = len(codec.canonical_json(payload).encode("utf-8"))
    except (TypeError, ValueError):
        return None
    if p_size > MAX_PAYLOAD_BYTES:
        return None
    for key, val in payload.items():
        if codec.clean_str(key, MAX_STR_LEN) is None:
            return None
        if isinstance(val, str):
            if codec.clean_str(val, MAX_STR_LEN, allow_empty=True) is None:
                return None
        elif not _is_strict_int(val):
            return None
    valid = (
        _check_local_witness(target_bug, services, payload)
        if target_bug in LOCAL_BUGS
        else _check_cross_witness(target_bug, services, payload)
    )
    return target_bug if valid else None


def _zero_rewards(instance: dict[str, Any], *, chain_ok: bool) -> dict[str, float]:
    seats = [str(s) for s in instance["seats"]]
    chain = 1.0 if chain_ok else 0.0
    return {
        "reward": 0.0,
        **{f"seat.{s}": 0.0 for s in seats},
        "local_bugs_found": 0.0,
        "cross_bugs_found": 0.0,
        "duplicate_submissions": 0.0,
        "invalid_submissions": 0.0,
        "claim_conflicts": 0.0,
        "spoof_detected": 0.0,
        "script_faithful": 0.0,
        "chain_ok": chain,
        "hash_chain_ok": chain,
    }


def _check_admin_close(st: SimState, ev: dict[str, Any]) -> bool:
    status, ok, seat = codec.strict_int(ev.get("status")), ev.get("ok"), ev.get("seat")
    method, resp = ev.get("method"), ev.get("response")
    if not isinstance(resp, dict) or not isinstance(ok, bool) or status is None:
        return False
    if ok:
        valid = (
            seat is None
            and method == "POST"
            and set(resp) == {"closed", "events", "log_intact"}
            and resp.get("closed") is True
            and codec.strict_int(resp.get("events")) == ev.get("seq")
            and isinstance(resp.get("log_intact"), bool)
        )
        if not valid:
            return False
        if resp["log_intact"] is not True:
            st.log_intact = False
            return False
        st.closed = True
        return True
    if status not in (403, 405) or (seat is not None and seat not in st.seat_set):
        return False
    if codec.strict_int(ev.get("logical_tick")) != st.logical_tick:
        return False
    if status == 403 and seat is not None:
        st.spoof_hits += 1
    return True


def _apply_submit_finding(st: SimState, seat: str, ev: dict[str, Any], resp: dict[str, Any]) -> bool:
    status, ok = int(ev["status"]), bool(ev["ok"])
    if status == 400:
        st.invalid_submissions += 1
        return True
    if not ok:
        return True
    req = ev.get("request")
    if ev.get("method") != "POST" or not isinstance(req, dict):
        return False
    if st.seat_used[seat] >= st.submit_budget or (st.channel and st.global_used >= st.global_budget):
        return False
    st.seat_used[seat] += 1
    st.global_used += 1
    want_bl = {"submit_finding": st.submit_budget - st.seat_used[seat]}
    if resp.get("budget_left") != want_bl:
        return False
    replayed_bug = _replay_witness(req, st.services)
    if replayed_bug is None:
        if resp.get("valid") is True:
            return False
        st.invalid_submissions += 1
        return True
    if resp.get("valid") is not True or resp.get("target_bug") != replayed_bug:
        return False
    if replayed_bug in st.first:
        st.duplicate_submissions += 1
    else:
        st.first[replayed_bug] = seat
    return True


def _replay_events(events: list[dict[str, Any]], instance: dict[str, Any]) -> SimState:
    st = SimState(instance)
    for ev in events:
        status, ok = codec.strict_int(ev.get("status")), ev.get("ok")
        seat, kind = ev.get("seat"), str(ev.get("kind") or "")
        resp = ev.get("response")
        if status is None or not isinstance(ok, bool) or not isinstance(resp, dict) or status >= 500:
            st.faithful = False
            break
        if kind == "admin.close":
            if not _check_admin_close(st, ev):
                st.faithful = False
                break
            continue
        if status == 401:
            if seat is not None or ok or codec.strict_int(ev.get("logical_tick")) != st.logical_tick:
                st.faithful = False
                break
            continue
        if st.closed:
            if (
                ok
                or status != 410
                or ev.get("late") is not True
                or seat not in st.seat_set
                or codec.strict_int(ev.get("logical_tick")) != st.logical_tick
            ):
                st.faithful = False
                break
            continue
        if not isinstance(seat, str) or seat not in st.seat_set:
            st.faithful = False
            break
        st.logical_tick += 1
        if codec.strict_int(ev.get("logical_tick")) != st.logical_tick:
            st.faithful = False
            break
        if status == 403 and resp.get("error") == "spoof_attempt_blocked":
            st.spoof_hits += 1
            continue
        if status == 409 and resp.get("error") in ("already_claimed", "already_found"):
            st.claim_conflicts += 1
        if kind == "submit_finding" and not _apply_submit_finding(st, seat, ev, resp):
            st.faithful = False
            break
    if not st.closed or not st.log_intact:
        st.faithful = False
    return st


def parse_events_bytes(data: bytes) -> tuple[list[dict[str, Any]], bool]:
    try:
        events = codec.parse_events_jsonl(data)
    except (TypeError, ValueError):
        return [], False
    return events, True


def _load_events_file(path: Path) -> tuple[list[dict[str, Any]], bool]:
    try:
        st_info = os.lstat(path)
    except OSError:
        return [], False
    if not stat.S_ISREG(st_info.st_mode) or st_info.st_size <= 0 or st_info.st_size > MAX_CHAIN_BYTES:
        return [], False
    return parse_events_bytes(path.read_bytes())


load_strict_events = _load_events_file


def score(events: list[dict[str, Any]], instance: dict[str, Any]) -> dict[str, float]:
    assert len(LOCAL_BUGS) + len(CROSS_BUGS) == TOTAL_BUGS
    if not codec.verify_chain(events)[0]:
        return _zero_rewards(instance, chain_ok=False)
    st = _replay_events(events, instance)
    if not st.faithful or not st.closed or not st.log_intact:
        return _zero_rewards(instance, chain_ok=True)
    local_found = sum(1 for bug in st.first if bug in LOCAL_BUGS)
    cross_found = sum(1 for bug in st.first if bug in CROSS_BUGS)
    assert local_found + cross_found == len(st.first) <= TOTAL_BUGS
    if st.spoof_hits > 0:
        reward = 0.0
        seat_scores = {f"seat.{s}": 0.0 for s in st.seats}
    else:
        reward = round(len(st.first) / TOTAL_BUGS, 6)
        seat_scores = {
            f"seat.{s}": round(sum(1 for owner in st.first.values() if owner == s) / TOTAL_BUGS, 6)
            for s in st.seats
        }
    return {
        "reward": reward,
        **seat_scores,
        "local_bugs_found": float(local_found),
        "cross_bugs_found": float(cross_found),
        "duplicate_submissions": float(st.duplicate_submissions),
        "invalid_submissions": float(st.invalid_submissions),
        "claim_conflicts": float(st.claim_conflicts),
        "spoof_detected": 1.0 if st.spoof_hits > 0 else 0.0,
        "script_faithful": 1.0,
        "chain_ok": 1.0,
        "hash_chain_ok": 1.0,
    }


def main() -> None:
    with contextlib.suppress(OSError):
        worldkit.close_world()
    instance = json.loads(INSTANCE.read_text(encoding="utf-8"))
    events, ok = _load_events_file(worldkit.EVENTS)
    rewards = score(events, instance) if ok else _zero_rewards(instance, chain_ok=False)
    worldkit.write_rewards(rewards)


if __name__ == "__main__":
    main()
