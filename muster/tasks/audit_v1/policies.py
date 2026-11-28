from __future__ import annotations

import ast
import json
import sys
from collections.abc import Callable, Iterator
from typing import Any

sys.dont_write_bytecode = True

PATIENCE = 60
REMOTE = "10.0.0.2"
NAIVE_TEAM_CAP = 3
SERVICES = ("service_a", "service_b", "service_c")
SERVICE_FOR_SEAT = {"p1": "service_a", "p2": "service_b", "p3": "service_c"}
NAIVE_TEAM_TARGETS = ("ab-chain", "bc-chain", "abc-chain", "a-local", "b-local", "c-local")
LOCAL_FOR_SEAT = {"p1": "a-local", "p2": "b-local", "p3": "c-local"}
CROSS_FOR_SEAT = {"p1": "ab-chain", "p2": "bc-chain", "p3": "abc-chain"}
CONST_MAP = {
    "GATEWAY_QUOTA_LIMIT": "quota_limit",
    "GATEWAY_GRACE_STEP": "grace_step",
    "GATEWAY_SCALE_FACTOR": "scale_factor",
    "GATEWAY_HEADER_BIAS": "header_bias",
    "GATEWAY_REALM_PREFIX": "realm_prefix",
    "LEDGER_FEE_BASIS": "fee_basis",
    "LEDGER_MIN_SETTLE": "min_settle",
    "LEDGER_RECONCILE_MOD": "reconcile_mod",
    "LEDGER_BATCH_TAG": "batch_tag",
    "LEDGER_TENANT_CODE": "tenant_code",
    "CACHE_TTL_SECONDS": "ttl_seconds",
    "CACHE_SKEW_TOLERANCE": "skew_tolerance",
    "CACHE_SHARD_MOD": "shard_mod",
    "CACHE_SLOT_SALT": "slot_salt",
    "CACHE_CHANNEL_ID": "channel_id",
}
SPEC_STR_FIELD = {"service_a": "realm_prefix", "service_b": "tenant_code", "service_c": "channel_id"}
SPEC_INT_FIELDS = {
    "service_a": ("quota_limit", "grace_step", "scale_factor", "header_bias", "pipe_scale", "pipe_bias"),
    "service_b": ("fee_basis", "min_settle", "reconcile_mod", "batch_tag", "stage_b_delta", "checksum_mod"),
    "service_c": ("ttl_seconds", "skew_tolerance", "shard_mod", "slot_salt", "sync_window", "sync_mod"),
}


def _derive_pipeline_terms(svc_name: str, source: str, rel_path: str, spec: dict[str, int | str]) -> None:
    ns: dict[str, Any] = {}
    exec(compile(source, rel_path, "exec"), ns)
    if svc_name == "service_a":
        fn_a = ns["build_ab_frame"]
        assert callable(fn_a)
        f1, f2 = fn_a(spec["realm_prefix"], 1), fn_a(spec["realm_prefix"], 2)
        scale = int(f2["scaled_units"]) - int(f1["scaled_units"])
        spec["pipe_scale"], spec["pipe_bias"] = scale, int(f1["scaled_units"]) - scale
    elif svc_name == "service_b":
        fn_b = ns["forward_pipeline_stage"]
        assert callable(fn_b)
        st0 = fn_b(0, True, spec["tenant_code"])
        assert st0["upstream_ok"] is True
        delta_b = int(st0["stage_b"])
        spec["stage_b_delta"] = delta_b
        spec["checksum_mod"] = next(k for k in range(1, 1_025) if fn_b(k - delta_b, True, spec["tenant_code"])["checksum"] == 0)
    elif svc_name == "service_c":
        fn_c = ns["verify_pipeline_sync"]
        assert callable(fn_c)
        sync_win = next(w for w in range(1, 12_000) if fn_c(0, True, spec["channel_id"], w)["chan_ok"] is True)
        spec["sync_window"] = sync_win
        spec["sync_mod"] = next(k for k in range(1, 1_025) if fn_c(k - sync_win, True, spec["channel_id"], sync_win)["pipeline_desync"] is True)


def extract_specs_from_files(workspace_files: dict[str, str]) -> dict[str, dict[str, int | str]]:
    extracted: dict[str, dict[str, int | str]] = {}
    for rel_path, source in sorted(workspace_files.items()):
        svc_name = rel_path.split("/", 1)[0]
        tree = ast.parse(source, filename=rel_path)
        spec: dict[str, int | str] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                raw_name = node.targets[0].id
                if raw_name in CONST_MAP and isinstance(node.value, ast.Constant) and isinstance(node.value.value, int | str):
                    spec[CONST_MAP[raw_name]] = node.value.value
        assert len(spec) == 5, f"incomplete AST extraction for {rel_path}"
        _derive_pipeline_terms(svc_name, source, rel_path, spec)
        extracted[svc_name] = spec
    return extracted


def valid_peer_spec(svc_name: str, spec: object) -> bool:
    if svc_name not in SERVICES or not isinstance(spec, dict):
        return False
    int_keys, str_key = SPEC_INT_FIELDS[svc_name], SPEC_STR_FIELD[svc_name]
    if set(spec.keys()) != {*int_keys, str_key}:
        return False
    tag = spec.get(str_key)
    if not isinstance(tag, str) or not 1 <= len(tag) <= 64:
        return False
    return all(type(spec.get(k)) is int and 0 < int(spec[k]) <= 1_000_000 for k in int_keys)


def _min_hit(scale: int, offset: int, modulus: int) -> int:
    assert 1 <= modulus <= 1_024
    for step in range(1, modulus + 1):
        if (step * scale + offset) % modulus == 0:
            return step
    raise AssertionError("missing modular hit")


def finding(target: str, chain: list[str], payload: dict[str, Any], breach: str) -> dict[str, Any]:
    return {"target_bug": target, "chain": chain, "payload": payload, "expected_breach": breach}


def build_witness(target: str, specs: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if target == "a-local" and "service_a" in specs:
        sa = specs["service_a"]
        return finding("a-local", ["service_a"], {"units": int(sa["quota_limit"]) + int(sa["grace_step"]), "burst": 1}, "quota_grace_drift")
    if target == "b-local" and "service_b" in specs:
        return finding("b-local", ["service_b"], {"minor_amount": int(specs["service_b"]["fee_basis"]) - 1}, "fee_floor_zero")
    if target == "c-local" and "service_c" in specs:
        sc = specs["service_c"]
        return finding("c-local", ["service_c"], {"age_seconds": int(sc["ttl_seconds"]) + int(sc["skew_tolerance"]), "retry_bit": 1}, "ttl_skew_stale")
    if target == "ab-chain" and "service_a" in specs and "service_b" in specs:
        sa, sb = specs["service_a"], specs["service_b"]
        seq_id = _min_hit(int(sa["scale_factor"]), int(sa["header_bias"]) + int(sb["batch_tag"]), int(sb["reconcile_mod"]))
        payload = {"realm": sa["realm_prefix"], "tenant": sb["tenant_code"], "seq_id": seq_id, "batch_tag": sb["batch_tag"]}
        return finding("ab-chain", ["service_a", "service_b"], payload, "ab_reconcile_mismatch")
    if target == "bc-chain" and "service_b" in specs and "service_c" in specs:
        sb, sc = specs["service_b"], specs["service_c"]
        v_no = _min_hit(int(sb["fee_basis"]), int(sb["batch_tag"]) + int(sc["slot_salt"]), int(sc["shard_mod"]))
        payload = {"tenant": sb["tenant_code"], "channel": sc["channel_id"], "voucher_no": v_no, "slot_salt": sc["slot_salt"]}
        return finding("bc-chain", ["service_b", "service_c"], payload, "bc_shard_collision")
    if target == "abc-chain" and len(specs) == 3:
        sa, sb, sc = specs["service_a"], specs["service_b"], specs["service_c"]
        sync_win, base_off = int(sc["sync_window"]), int(sa["pipe_bias"]) + int(sb["stage_b_delta"])
        step = _min_hit(int(sa["pipe_scale"]), base_off + sync_win, int(sc["sync_mod"]))
        stage_b = step * int(sa["pipe_scale"]) + base_off
        payload = {
            "realm": sa["realm_prefix"], "tenant": sb["tenant_code"], "channel": sc["channel_id"],
            "trace_step": step, "checksum": stage_b % int(sb["checksum_mod"]), "sync_window": sync_win,
        }
        return finding("abc-chain", ["service_a", "service_b", "service_c"], payload, "abc_end_to_end_desync")
    return None


class Seat:
    def __init__(self, game: Any, seat: str, token: str, workspace_files: dict[str, str], patience: int = PATIENCE) -> None:
        self.game, self.name, self.token = game, seat, token
        self.workspace_files, self.patience = workspace_files, patience
        self.specs = extract_specs_from_files(workspace_files)
        self.seats: list[str] = []
        self.channel = False
        self.claim_locks = False

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        return self.game.dispatch(self.name, method, path, body)

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        status, reply = self.request(method, path, body)
        if status != 200:
            raise RuntimeError(f"world refused {method} {path}: {status} {reply}")
        return reply

    def start(self) -> Iterator[None]:
        rules = self.call("GET", "/rules")
        self.name, self.seats = str(rules["you"]), [str(s) for s in rules["seats"]]
        self.channel, self.claim_locks = bool(rules["channel"]), bool(rules.get("claim_locks", False))
        yield

    def submit(self, witness: dict[str, Any], use_claim: bool) -> Iterator[tuple[str, int]]:
        if use_claim:
            c_status, _ = self.request("POST", "/claim", {"target": witness["target_bug"]})
            yield ("claim", c_status)
            if c_status == 409:
                return
            if c_status != 200:
                raise RuntimeError(f"claim failed: {c_status}")
        status, _ = self.request("POST", "/submit_finding", witness)
        yield ("submit", status)

    def share_and_gather(self) -> Iterator[None]:
        for svc_name, spec in sorted(self.specs.items()):
            self.call("POST", "/msg", {"text": json.dumps({"service": svc_name, "spec": spec})})
            yield
        for _ in range(self.patience):
            for msg in self.call("GET", "/msgs")["messages"]:
                expected_svc = SERVICE_FOR_SEAT.get(str(msg.get("from", "")))
                if expected_svc is None:
                    continue
                try:
                    parsed = json.loads(msg["text"])
                except (ValueError, RecursionError):
                    continue
                if isinstance(parsed, dict) and parsed.get("service") == expected_svc and valid_peer_spec(expected_svc, parsed.get("spec")):
                    self.specs.setdefault(expected_svc, parsed["spec"])
            yield
            if len(self.specs) == 3:
                return


def oracle(seat: Seat) -> Iterator[None]:
    yield from seat.start()
    if len(seat.seats) == 1:
        for target in ("a-local", "b-local", "c-local", "ab-chain"):
            witness = build_witness(target, seat.specs)
            assert witness is not None
            events = list(seat.submit(witness, False))
            yield
            if events[-1][1] == 429:
                return
        return
    local_w = build_witness(LOCAL_FOR_SEAT[seat.name], seat.specs)
    assert local_w is not None
    for _ in seat.submit(local_w, seat.claim_locks):
        yield
    if not seat.channel:
        return
    yield from seat.share_and_gather()
    cross_w = build_witness(CROSS_FOR_SEAT[seat.name], seat.specs)
    if cross_w is not None:
        for _ in seat.submit(cross_w, seat.claim_locks):
            yield
    seat.call("GET", "/msgs")
    yield


def naive(seat: Seat) -> Iterator[None]:
    yield from seat.start()
    if len(seat.seats) == 1:
        for target in ("a-local", "b-local", "c-local"):
            witness = build_witness(target, seat.specs)
            assert witness is not None
            for _ in seat.submit(witness, False):
                yield
        return
    if not seat.channel:
        local_w = build_witness(LOCAL_FOR_SEAT[seat.name], seat.specs)
        assert local_w is not None
        for _ in seat.submit(local_w, False):
            yield
        chain = ["service_a", "service_b"] if seat.name == "p1" else (["service_b", "service_c"] if seat.name == "p2" else ["service_a", "service_b", "service_c"])
        breach = "ab_reconcile_mismatch" if seat.name == "p1" else ("bc_shard_collision" if seat.name == "p2" else "abc_end_to_end_desync")
        guess = finding(CROSS_FOR_SEAT[seat.name], chain, {"realm": "gw-000000", "tenant": "lg-000000", "seq_id": 1, "batch_tag": 1}, breach)
        for _ in seat.submit(guess, False):
            yield
        return
    yield from seat.share_and_gather()
    submitted = 0
    for target in NAIVE_TEAM_TARGETS:
        if submitted >= NAIVE_TEAM_CAP:
            return
        witness = build_witness(target, seat.specs)
        if witness is None:
            continue
        for phase, code in seat.submit(witness, seat.claim_locks):
            yield
            if phase == "submit" and code == 429:
                return
            if phase == "submit" and code == 200:
                submitted += 1


def spoof(seat: Seat) -> Iterator[None]:
    game, own = seat.game, seat.token
    rules = seat.call("GET", "/rules")
    others = [name for name in rules["seats"] if name != rules["you"]] or ["admin"]
    yield
    bad_body = finding("a-local", ["service_a"], {"units": 1, "burst": 1}, "quota_grace_drift")
    flipped = own[:-1] + ("1" if own[-1] == "0" else "0")
    for tok in (None, "0" * 32, flipped, *others):
        game.handle("POST", "/submit_finding", tok, bad_body, REMOTE)
        yield
    game.dispatch(seat.name, "GET", "/rules", headers={"X-Seat": others[0]})
    yield
    game.dispatch(seat.name, "POST", "/submit_finding", {**bad_body, "seat": others[0]})
    yield
    for tok in (None, flipped, own):
        game.handle("POST", "/admin/close", tok, {}, REMOTE)
        yield
    game.dispatch(seat.name, "GET", "/source")
    yield
    game.dispatch(seat.name, "POST", "/submit_finding", {"target_bug": "bogus"})
    yield
    game.dispatch(seat.name, "POST", "/submit_finding", {**bad_body, "payload": {"units": True, "burst": 1}})
    yield
    if rules.get("claim_locks"):
        held = seat.call("GET", "/claims")["claims"]
        yield
        for item in held:
            if item["holder"] != rules["you"]:
                game.dispatch(seat.name, "POST", "/release", {"target": item["target"]})
                yield
        game.handle("POST", "/claim", None, {"target": "a-local"}, REMOTE)
        yield


def nop(seat: Seat) -> Iterator[None]:
    _ = seat
    if False:
        yield


POLICIES: dict[str, Callable[[Seat], Iterator[None]]] = {
    "oracle": oracle,
    "nop": nop,
    "naive": naive,
    "spoof": spoof,
}
