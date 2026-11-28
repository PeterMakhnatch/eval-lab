from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, ClassVar

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
WorldBase: Any = worldkit.World
Reject: Any = worldkit.Reject

INSTANCE = Path(os.environ.get("WORLD_INSTANCE", "/world/instance.json"))
SERVICES = ("service_a", "service_b", "service_c")
LOCAL_BUGS = ("a-local", "b-local", "c-local")
CROSS_BUGS = ("ab-chain", "bc-chain", "abc-chain")
TARGET_BUGS = (*LOCAL_BUGS, *CROSS_BUGS)
REQUIRED_SUBMISSION_KEYS = frozenset({"target_bug", "chain", "payload", "expected_breach"})
CLAIM_KEYS = frozenset({"target", "box", "slot", "ttl_ticks"})
RELEASE_KEYS = frozenset({"target", "box", "slot", "lease_id"})
MAX_STR_LEN = 512
MAX_PAYLOAD_BYTES = 512
MAX_PAYLOAD_KEYS = 16
MAX_INT_ABS = 10_000_000
MAX_SCAN_STEPS = 1_024
MAX_CLAIMS_PER_SEAT = 2
LEASE_EXPIRY_TICKS = 128

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


def _normalize_tokens(instance: dict[str, Any], tokens: dict[str, str] | None) -> dict[str, str] | None:
    if tokens is None:
        return None
    seat_set = {str(s) for s in instance["seats"]}
    keys = {str(k) for k in tokens}
    vals = {str(v) for v in tokens.values()}
    if vals == seat_set:
        return {str(k): str(v) for k, v in tokens.items()}
    if keys == seat_set:
        return {str(v): str(k) for k, v in tokens.items()}
    return {str(k): str(v) for k, v in tokens.items()}


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


def _validate_submission(body: dict[str, Any]) -> tuple[str, tuple[str, ...], dict[str, Any], str]:
    if not isinstance(body, dict) or set(body.keys()) != REQUIRED_SUBMISSION_KEYS:
        raise Reject(400, "submission must contain exact keys: target_bug, chain, payload, expected_breach")
    target_bug = body.get("target_bug")
    chain = body.get("chain")
    payload = body.get("payload")
    breach = body.get("expected_breach")
    if not isinstance(target_bug, str) or len(target_bug) > MAX_STR_LEN or target_bug not in TARGET_BUGS:
        raise Reject(400, f"target_bug must be one of {', '.join(TARGET_BUGS)}")
    if not isinstance(chain, list) or not 1 <= len(chain) <= 3:
        raise Reject(400, "chain must be a list of 1 to 3 service names")
    for item in chain:
        if not isinstance(item, str) or len(item) > MAX_STR_LEN or item not in SERVICES:
            raise Reject(400, "chain entries must be service_a, service_b, or service_c")
    clean_breach = codec.clean_str(breach, MAX_STR_LEN)
    if clean_breach is None or clean_breach != breach:
        raise Reject(400, f"expected_breach must be a non-empty string up to {MAX_STR_LEN} chars")
    if not isinstance(payload, dict) or len(payload) > MAX_PAYLOAD_KEYS:
        raise Reject(400, f"payload must be a JSON object of at most {MAX_PAYLOAD_BYTES} bytes")
    if len(codec.canonical_json(payload).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise Reject(400, f"payload must be a JSON object of at most {MAX_PAYLOAD_BYTES} bytes")
    for key, val in payload.items():
        if codec.clean_str(key, MAX_STR_LEN) is None:
            raise Reject(400, "payload keys must be bounded strings")
        if isinstance(val, str):
            if codec.clean_str(val, MAX_STR_LEN, allow_empty=True) is None:
                raise Reject(400, f"payload string values must be at most {MAX_STR_LEN} chars")
        elif not _is_strict_int(val):
            raise Reject(400, "payload values must be bounded integers or strings")
    return target_bug, tuple(chain), payload, clean_breach


class AuditWorld(WorldBase):
    budgets: ClassVar[dict[str, int]] = {"submit_finding": 4}
    lease_expiry_ticks = LEASE_EXPIRY_TICKS

    def __init__(
        self,
        instance: dict[str, Any],
        tokens: dict[str, str] | None = None,
        events: Path | None = None,
    ) -> None:
        assert isinstance(instance, dict) and instance["submit_budget"] > 0 and instance["global_submit_budget"] > 0
        norm_toks = _normalize_tokens(instance, tokens)
        seats = [str(s) for s in instance["seats"]]
        super().__init__(
            tokens=norm_toks,
            events=events,
            seats=seats if norm_toks is None else None,
            channel=bool(instance["channel"]),
            notices_enabled=False,
        )
        assert set(instance["services"]) == set(SERVICES)
        assert set(instance["workspace_services"]) == set(self.seats)
        self.instance = dict(instance)
        self.budgets = {"submit_finding": int(instance["submit_budget"])}
        self.global_submit_budget = int(instance["global_submit_budget"])
        self.claim_locks = bool(instance.get("claim_locks", False))
        self.max_leases_per_seat = MAX_CLAIMS_PER_SEAT if self.claim_locks else None
        self.services: dict[str, dict[str, Any]] = instance["services"]
        self.workspace_services: dict[str, list[str]] = instance["workspace_services"]
        self.submissions_used = 0
        self.found_bugs: dict[str, str] = {}
        self.seat_found_bugs: dict[str, set[str]] = {seat: set() for seat in self.seats}
        self._logged_spoofs = 0

    def valid_lease_target(self, target: str) -> bool:
        return target in TARGET_BUGS

    def _release_unused_claims(self, seat: str) -> None:
        stale = [
            target
            for target, held in self.active_leases.items()
            if held["holder"] == seat and held["expires_tick"] is not None
        ]
        for target in stale:
            del self.active_leases[target]

    def _execute_seat_request(
        self, seat: str, kind: str, method: str, body: dict[str, Any] | None
    ) -> tuple[int, dict[str, Any]]:
        if self.spoof_attempts > self._logged_spoofs:
            self._logged_spoofs = self.spoof_attempts
            req_log = body if method == "POST" else None
            return self._log(seat, kind, method, req_log, 403, {"error": "spoof_attempt_blocked"})
        prev_submissions = self.submissions_used
        status, reply = super()._execute_seat_request(seat, kind, method, body)
        if status == 507:
            self.submissions_used = prev_submissions
        elif self.claim_locks and (self.left(seat, "submit_finding") or 0) <= 0:
            self._release_unused_claims(seat)
        return status, reply

    def get_rules(self, seat: str) -> dict[str, Any]:
        rules = super().get_rules(seat)
        rules["claim_locks"] = self.claim_locks
        if self.claim_locks:
            rules["max_claims"] = MAX_CLAIMS_PER_SEAT
        rules["target_bugs"] = list(TARGET_BUGS)
        rules["workspace_services"] = list(self.workspace_services[seat])
        if self.channel:
            rules["global_submit_budget"] = self.global_submit_budget
            rules["global_budget_left"] = self.global_submit_budget - self.submissions_used
        return rules

    def get_leases(self, seat: str) -> dict[str, Any]:
        res = super().get_leases(seat)
        res["claims"] = res["leases"]
        return res

    get_claims = get_leases

    def post_claim(self, seat: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.claim_locks:
            raise Reject(404, "claims_disabled")
        if not body or set(body) - CLAIM_KEYS:
            raise Reject(400, "invalid_claim_schema")
        target = self._parse_lease_target(body)
        if target in self.found_bugs:
            raise Reject(409, "already_found", owner=self.found_bugs[target], target=target)
        if (self.left(seat, "submit_finding") or 0) <= 0 or (
            self.channel and self.submissions_used >= self.global_submit_budget
        ):
            raise Reject(409, "no_submit_budget_for_claim", budget_left=0)
        return super().post_claim(seat, body)

    def post_release(self, seat: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.claim_locks:
            raise Reject(404, "claims_disabled")
        if not body or set(body) - RELEASE_KEYS:
            raise Reject(400, "invalid_release_schema")
        target = self._parse_lease_target(body)
        if target in self.found_bugs:
            raise Reject(409, "already_found", owner=self.found_bugs[target], target=target)
        return super().post_release(seat, body)

    def _record_found_lease(self, seat: str, target_bug: str) -> None:
        self.lease_seq += 1
        self.active_leases[target_bug] = {
            "target": target_bug,
            "holder": seat,
            "lease_id": f"found-{self.lease_seq:04d}",
            "acquired_tick": self.logical_tick,
            "expires_tick": None,
            "expiry_ticks": None,
        }

    def post_submit_finding(self, seat: str, body: dict[str, Any]) -> dict[str, Any]:
        target_bug, chain, payload, breach = _validate_submission(body)
        if self.claim_locks:
            if target_bug in self.found_bugs:
                raise Reject(409, "already_found", owner=self.found_bugs[target_bug], target=target_bug)
            held = self.active_leases.get(target_bug)
            if held is not None and held["holder"] != seat:
                raise Reject(409, "already_claimed", target=target_bug, holder=held["holder"])
        if self.channel and self.submissions_used >= self.global_submit_budget:
            raise Reject(429, "global_submit_budget_exhausted", global_budget_left=0)
        chain_valid = chain == EXPECTED_CHAINS[target_bug] and breach == EXPECTED_BREACHES[target_bug]
        if not chain_valid:
            valid = False
        elif target_bug in LOCAL_BUGS:
            valid = _check_local_witness(target_bug, self.services, payload)
        else:
            valid = _check_cross_witness(target_bug, self.services, payload)
        self.submissions_used += 1
        if not self.channel:
            if not valid:
                return {"target_bug": target_bug, "valid": False, "accepted": False}
            own_dup = target_bug in self.seat_found_bugs[seat]
            self.seat_found_bugs[seat].add(target_bug)
            if target_bug not in self.found_bugs:
                self.found_bugs[target_bug] = seat
            return {
                "target_bug": target_bug,
                "valid": True,
                "accepted": not own_dup,
                "category": "local" if target_bug in LOCAL_BUGS else "cross",
            }
        global_left = self.global_submit_budget - self.submissions_used
        assert global_left >= 0
        if not valid:
            return {
                "target_bug": target_bug,
                "target": target_bug,
                "valid": False,
                "accepted": False,
                "duplicate": False,
                "global_budget_left": global_left,
            }
        duplicate = target_bug in self.found_bugs
        if not duplicate:
            self.found_bugs[target_bug] = seat
            if self.claim_locks:
                self._record_found_lease(seat, target_bug)
        return {
            "target_bug": target_bug,
            "target": target_bug,
            "valid": True,
            "accepted": not duplicate,
            "duplicate": duplicate,
            "category": "local" if target_bug in LOCAL_BUGS else "cross",
            "owner": self.found_bugs[target_bug],
            "global_budget_left": global_left,
        }


Audit = AuditWorld


def make_world(
    instance: dict[str, Any],
    tokens: dict[str, str] | None = None,
    events: Path | None = None,
) -> AuditWorld:
    return AuditWorld(instance, tokens=tokens, events=events)


def main() -> None:
    instance = json.loads(INSTANCE.read_text(encoding="utf-8"))
    worldkit.run_server(AuditWorld(instance))


if __name__ == "__main__":
    main()
