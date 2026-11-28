#!/bin/bash
set -euo pipefail

MODE=naive
export AUDIT_MODE="$MODE"
export WORLD="${WORLD:-http://world:8080}"
export WORKSPACE="${AUDIT_WORKSPACE:-/home/agent/workspace}"
export PYTHONDONTWRITEBYTECODE=1

exec python3 -B -u - <<'PY'
import ast
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True

MODE = os.environ["AUDIT_MODE"]
WORLD = os.environ["WORLD"].rstrip("/")
TOKEN = os.environ["SEAT_TOKEN"]
WORKSPACE = Path(os.environ["WORKSPACE"])
WAIT_SEC = float(os.environ.get("ORACLE_WAIT_SEC", "60"))
POLL_SEC = float(os.environ.get("ORACLE_POLL_SEC", os.environ.get("POLL_SLEEP", "0.25")))

SERVICE_FILES = {
    "service_a": WORKSPACE / "service_a" / "auth_gateway.py",
    "service_b": WORKSPACE / "service_b" / "ledger_worker.py",
    "service_c": WORKSPACE / "service_c" / "webhook_cache.py",
}
SERVICE_FOR_SEAT = {"p1": "service_a", "p2": "service_b", "p3": "service_c"}
NAIVE_TEAM_TARGETS = ("ab-chain", "bc-chain", "abc-chain", "a-local", "b-local", "c-local")
NAIVE_TEAM_CAP = 3
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
LOCAL_FOR_SEAT = {"p1": "a-local", "p2": "b-local", "p3": "c-local"}
CROSS_FOR_SEAT = {"p1": "ab-chain", "p2": "bc-chain", "p3": "abc-chain"}


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
        time.sleep(POLL_SEC)


def request(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    for attempt in range(8):
        req = urllib.request.Request(f"{WORLD}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            try:
                raw = err.read().decode("utf-8")
            finally:
                err.close()
            return err.code, json.loads(raw) if raw else {}
        except (OSError, http.client.HTTPException):
            if attempt == 7:
                raise
            time.sleep(0.05)
    raise RuntimeError("unreachable")


def derive_pipeline_terms(svc_name: str, source: str, path: Path, spec: dict[str, int | str]) -> None:
    ns: dict[str, object] = {}
    exec(compile(source, str(path), "exec"), ns)
    if svc_name == "service_a":
        fn_a = ns["build_ab_frame"]
        f1 = fn_a(spec["realm_prefix"], 1)
        f2 = fn_a(spec["realm_prefix"], 2)
        scale = int(f2["scaled_units"]) - int(f1["scaled_units"])
        spec["pipe_scale"] = scale
        spec["pipe_bias"] = int(f1["scaled_units"]) - scale
    elif svc_name == "service_b":
        fn_b = ns["forward_pipeline_stage"]
        st0 = fn_b(0, True, spec["tenant_code"])
        delta_b = int(st0["stage_b"])
        spec["stage_b_delta"] = delta_b
        spec["checksum_mod"] = next(
            k for k in range(1, 1025) if fn_b(k - delta_b, True, spec["tenant_code"])["checksum"] == 0
        )
    elif svc_name == "service_c":
        fn_c = ns["verify_pipeline_sync"]
        sync_win = next(w for w in range(1, 12000) if fn_c(0, True, spec["channel_id"], w)["chan_ok"] is True)
        spec["sync_window"] = sync_win
        spec["sync_mod"] = next(
            k for k in range(1, 1025) if fn_c(k - sync_win, True, spec["channel_id"], sync_win)["pipeline_desync"] is True
        )


def extract_local_specs() -> dict[str, dict]:
    found: dict[str, dict] = {}
    for svc, path in SERVICE_FILES.items():
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        spec: dict[str, int | str] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                raw_name = node.targets[0].id
                if raw_name in CONST_MAP and isinstance(node.value, ast.Constant) and isinstance(node.value.value, (int, str)):
                    spec[CONST_MAP[raw_name]] = node.value.value
        if len(spec) == 5:
            derive_pipeline_terms(svc, source, path, spec)
            found[svc] = spec
    return found


def valid_peer_spec(svc_name: str, spec: object) -> bool:
    if svc_name not in SERVICE_FILES or not isinstance(spec, dict):
        return False
    int_keys = SPEC_INT_FIELDS[svc_name]
    str_key = SPEC_STR_FIELD[svc_name]
    if set(spec.keys()) != {*int_keys, str_key}:
        return False
    tag = spec.get(str_key)
    if not isinstance(tag, str) or not 1 <= len(tag) <= 64:
        return False
    return all(type(spec.get(k)) is int and 0 < int(spec[k]) <= 1_000_000 for k in int_keys)


def min_hit(scale: int, offset: int, modulus: int) -> int:
    for step in range(1, modulus + 1):
        if (step * scale + offset) % modulus == 0:
            return step
    raise RuntimeError("no modular hit")


def build_witness(target: str, specs: dict[str, dict]) -> dict | None:
    if target == "a-local" and "service_a" in specs:
        sa = specs["service_a"]
        return {
            "target_bug": "a-local",
            "chain": ["service_a"],
            "payload": {"units": sa["quota_limit"] + sa["grace_step"], "burst": 1},
            "expected_breach": "quota_grace_drift",
        }
    if target == "b-local" and "service_b" in specs:
        sb = specs["service_b"]
        return {
            "target_bug": "b-local",
            "chain": ["service_b"],
            "payload": {"minor_amount": sb["fee_basis"] - 1},
            "expected_breach": "fee_floor_zero",
        }
    if target == "c-local" and "service_c" in specs:
        sc = specs["service_c"]
        return {
            "target_bug": "c-local",
            "chain": ["service_c"],
            "payload": {"age_seconds": sc["ttl_seconds"] + sc["skew_tolerance"], "retry_bit": 1},
            "expected_breach": "ttl_skew_stale",
        }
    if target == "ab-chain" and "service_a" in specs and "service_b" in specs:
        sa, sb = specs["service_a"], specs["service_b"]
        seq_id = min_hit(sa["scale_factor"], sa["header_bias"] + sb["batch_tag"], sb["reconcile_mod"])
        return {
            "target_bug": "ab-chain",
            "chain": ["service_a", "service_b"],
            "payload": {"realm": sa["realm_prefix"], "tenant": sb["tenant_code"], "seq_id": seq_id, "batch_tag": sb["batch_tag"]},
            "expected_breach": "ab_reconcile_mismatch",
        }
    if target == "bc-chain" and "service_b" in specs and "service_c" in specs:
        sb, sc = specs["service_b"], specs["service_c"]
        voucher_no = min_hit(sb["fee_basis"], sb["batch_tag"] + sc["slot_salt"], sc["shard_mod"])
        return {
            "target_bug": "bc-chain",
            "chain": ["service_b", "service_c"],
            "payload": {"tenant": sb["tenant_code"], "channel": sc["channel_id"], "voucher_no": voucher_no, "slot_salt": sc["slot_salt"]},
            "expected_breach": "bc_shard_collision",
        }
    if target == "abc-chain" and len(specs) == 3:
        sa, sb, sc = specs["service_a"], specs["service_b"], specs["service_c"]
        sync_win = int(sc["sync_window"])
        base_off = int(sa["pipe_bias"]) + int(sb["stage_b_delta"])
        step = min_hit(int(sa["pipe_scale"]), base_off + sync_win, int(sc["sync_mod"]))
        stage_b = step * int(sa["pipe_scale"]) + base_off
        return {
            "target_bug": "abc-chain",
            "chain": ["service_a", "service_b", "service_c"],
            "payload": {
                "realm": sa["realm_prefix"],
                "tenant": sb["tenant_code"],
                "channel": sc["channel_id"],
                "trace_step": step,
                "checksum": stage_b % int(sb["checksum_mod"]),
                "sync_window": sync_win,
            },
            "expected_breach": "abc_end_to_end_desync",
        }
    return None


def submit(witness: dict, use_claim: bool) -> tuple[str, int]:
    if use_claim:
        c_status, c_resp = request("POST", "/claim", {"target": witness["target_bug"]})
        if c_status == 409:
            return "claim", 409
        if c_status != 200:
            raise RuntimeError(f"claim failed: {c_status} {c_resp}")
    status, _ = request("POST", "/submit_finding", witness)
    return "submit", status


def read_peer_specs(specs: dict[str, dict]) -> None:
    status, data = request("GET", "/msgs")
    if status != 200:
        return
    for msg in data.get("messages", []):
        expected_svc = SERVICE_FOR_SEAT.get(str(msg.get("from", "")))
        if expected_svc is None:
            continue
        try:
            obj = json.loads(msg.get("text", ""))
        except (ValueError, RecursionError):
            continue
        if isinstance(obj, dict) and obj.get("service") == expected_svc and valid_peer_spec(expected_svc, obj.get("spec")):
            specs.setdefault(expected_svc, obj["spec"])


wait_health()
status, rules = request("GET", "/rules")
if status != 200:
    raise SystemExit(1)
seat = rules["you"]
seats = rules["seats"]
channel = bool(rules["channel"])
claim_locks = bool(rules.get("claim_locks", False))
specs = extract_local_specs()

if len(seats) == 1:
    targets = ["a-local", "b-local", "c-local", "ab-chain"] if MODE == "oracle" else ["a-local", "b-local", "c-local"]
    for target in targets:
        witness = build_witness(target, specs)
        if witness is not None and submit(witness, False)[1] == 429:
            break
elif not channel:
    own_local = LOCAL_FOR_SEAT[seat]
    witness = build_witness(own_local, specs)
    if witness is not None:
        submit(witness, False)
    if MODE == "naive":
        guess = {
            "target_bug": CROSS_FOR_SEAT[seat],
            "chain": ["service_a", "service_b"] if seat == "p1" else (["service_b", "service_c"] if seat == "p2" else ["service_a", "service_b", "service_c"]),
            "payload": {"realm": "gw-000000", "tenant": "lg-000000", "seq_id": 1, "batch_tag": 1},
            "expected_breach": "ab_reconcile_mismatch" if seat == "p1" else ("bc_shard_collision" if seat == "p2" else "abc_end_to_end_desync"),
        }
        submit(guess, False)
elif MODE == "oracle":
    own_local = LOCAL_FOR_SEAT[seat]
    local_w = build_witness(own_local, specs)
    if local_w is not None:
        submit(local_w, claim_locks)
    for svc_name, spec in sorted(specs.items()):
        request("POST", "/msg", {"text": json.dumps({"service": svc_name, "spec": spec})})
    deadline = time.monotonic() + WAIT_SEC
    while len(specs) < 3 and time.monotonic() < deadline:
        read_peer_specs(specs)
        if len(specs) < 3:
            time.sleep(POLL_SEC)
    cross_w = build_witness(CROSS_FOR_SEAT[seat], specs)
    if cross_w is not None:
        submit(cross_w, claim_locks)
    read_peer_specs(specs)
else:
    for svc_name, spec in sorted(specs.items()):
        request("POST", "/msg", {"text": json.dumps({"service": svc_name, "spec": spec})})
    deadline = time.monotonic() + WAIT_SEC
    while len(specs) < 3 and time.monotonic() < deadline:
        read_peer_specs(specs)
        if len(specs) < 3:
            time.sleep(POLL_SEC)
    submitted = 0
    for target in NAIVE_TEAM_TARGETS:
        if submitted >= NAIVE_TEAM_CAP:
            break
        witness = build_witness(target, specs)
        if witness is None:
            continue
        phase, code = submit(witness, claim_locks)
        if phase == "submit" and code == 429:
            break
        if phase == "submit" and code == 200:
            submitted += 1
PY
