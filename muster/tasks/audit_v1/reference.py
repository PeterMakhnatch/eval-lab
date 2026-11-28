from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import http.server
import importlib.util
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import tomllib
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

sys.dont_write_bytecode = True

FAMILY = Path(__file__).resolve().parent
TEMPLATE = FAMILY / "template"
KIT_V2 = FAMILY.parent / "_kit_v2"
CONDITIONS = ("solo-x3", "indep-3", "team-3", "team-3-claimlock")
POLICIES = ("oracle", "nop", "naive", "spoof")
SERVICES = ("service_a", "service_b", "service_c")
SPEC_PEPPER = "3b7e91d40a58c2f619e47b83d2056fa984c1e72b5d903a6fc8142e79b05d3a8c"
TOKEN_PEPPER = "9f4c2e81b7d046a3e5128c9b0fd73a64c18e5b29a47d60f31e8c4b927a6d05e1"
PRIMES_RECONCILE = (313, 331, 353, 379, 401, 433, 461, 491, 523, 557, 593, 619, 653, 691, 733, 769, 811, 857, 907, 953)
PRIMES_SHARD = (311, 337, 359, 383, 409, 439, 463, 499, 541, 571, 601, 631, 661, 701, 739, 773, 821, 863, 911, 967)
REMOTE_CLIENT = "10.0.0.2"


def _load_mod(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _bootstrap_modules() -> tuple[Any, Any, Any, Any, Any, Any]:
    saved_path = list(sys.path)
    sys.path.insert(0, str(KIT_V2))
    try:
        c_mod = _load_mod("codec", KIT_V2 / "codec.py")
        wk_mod = _load_mod("worldkit", KIT_V2 / "worldkit.py")
        b_mod = _load_mod("audit_v1_build", KIT_V2 / "build.py")
        w_mod = _load_mod("audit_v1_world", TEMPLATE / "world.py")
        v_mod = _load_mod("audit_v1_verify", TEMPLATE / "verify.py")
        p_mod = _load_mod("audit_v1_policies", FAMILY / "policies.py")
        return c_mod, wk_mod, b_mod, w_mod, v_mod, p_mod
    finally:
        sys.path[:] = saved_path


codec, worldkit, build, world, verify, policies_mod = _bootstrap_modules()


def family() -> dict[str, Any]:
    config = tomllib.loads((FAMILY / "family.toml").read_text(encoding="utf-8"))
    assert config["name"] == FAMILY.name == "audit_v1"
    assert int(config["instances"]) == 10
    assert tuple(config["conditions"]) == CONDITIONS
    assert tuple(config["policies"]) == POLICIES
    assert config["exploits"] == ["spoof"]
    return config


def _num(seed: str, instance: int, label: str, low: int, high: int) -> int:
    assert low <= high
    digest = hmac.new(bytes.fromhex(SPEC_PEPPER), f"{seed}/{instance}/num/{label}".encode(), hashlib.sha256).hexdigest()
    return low + int(digest[:12], 16) % (high - low + 1)


def _tag(seed: str, instance: int, prefix: str) -> str:
    digest = hmac.new(bytes.fromhex(SPEC_PEPPER), f"{seed}/{instance}/tag/{prefix}".encode(), hashlib.sha256).hexdigest()
    return f"{prefix}-{digest[:6]}"


def service_specs(seed: str, number: int) -> dict[str, dict[str, int | str]]:
    assert number >= 1
    p_rec = PRIMES_RECONCILE[(number * 3 + _num(seed, number, "rec_idx", 0, 19)) % len(PRIMES_RECONCILE)]
    p_shd = PRIMES_SHARD[(number * 5 + _num(seed, number, "shd_idx", 0, 19)) % len(PRIMES_SHARD)]
    fee_basis = _num(seed, number, "fee_basis", 130, 300)
    min_settle = _num(seed, number, "min_settle", 40, 110)
    assert min_settle < fee_basis < p_shd
    return {
        "service_a": {
            "quota_limit": _num(seed, number, "quota_limit", 1200, 4800),
            "grace_step": _num(seed, number, "grace_step", 17, 89),
            "scale_factor": _num(seed, number, "scale_factor", 11, 29),
            "header_bias": _num(seed, number, "header_bias", 101, 899),
            "realm_prefix": _tag(seed, number, "gw"),
        },
        "service_b": {
            "fee_basis": fee_basis,
            "min_settle": min_settle,
            "reconcile_mod": p_rec,
            "batch_tag": _num(seed, number, "batch_tag", 1000, 8999),
            "tenant_code": _tag(seed, number, "lg"),
        },
        "service_c": {
            "ttl_seconds": _num(seed, number, "ttl_seconds", 300, 1800),
            "skew_tolerance": _num(seed, number, "skew_tolerance", 13, 67),
            "shard_mod": p_shd,
            "slot_salt": _num(seed, number, "slot_salt", 1001, 8999),
            "channel_id": _tag(seed, number, "wh"),
        },
    }


def workspace_map(seats: list[str]) -> dict[str, list[str]]:
    if seats == ["p1"]:
        return {"p1": list(SERVICES)}
    assert seats == ["p1", "p2", "p3"]
    return {"p1": ["service_a"], "p2": ["service_b"], "p3": ["service_c"]}


def instances(config: dict[str, Any], condition: str) -> list[dict[str, Any]]:
    assert condition in CONDITIONS
    spec = config["conditions"][condition]
    seats = [str(s) for s in spec["seats"]]
    out = [
        {
            "family": config["name"],
            "condition": condition,
            "instance": number,
            "seats": seats,
            "submit_budget": int(spec["submit_budget"]),
            "global_submit_budget": int(spec["global_submit_budget"]),
            "channel": bool(spec["channel"]),
            "claim_locks": bool(spec.get("claim_locks", False)),
            "workspace_services": workspace_map(seats),
            "services": service_specs(str(config["seed"]), number),
        }
        for number in range(1, int(config["instances"]) + 1)
    ]
    assert len(out) == int(config["instances"])
    return out


def tokens(config: dict[str, Any], instance: dict[str, Any]) -> dict[str, str]:
    cond, num, key = instance["condition"], instance["instance"], bytes.fromhex(TOKEN_PEPPER)
    return {
        str(seat): hmac.new(key, f"{config['seed']}/{cond}/{num}/{seat}".encode(), hashlib.sha256).hexdigest()[:32]
        for seat in instance["seats"]
    }


def render_service_a(sa: dict[str, int | str]) -> str:
    lines = [
        f"GATEWAY_QUOTA_LIMIT = {sa['quota_limit']}",
        f"GATEWAY_GRACE_STEP = {sa['grace_step']}",
        f"GATEWAY_SCALE_FACTOR = {sa['scale_factor']}",
        f"GATEWAY_HEADER_BIAS = {sa['header_bias']}",
        f"GATEWAY_REALM_PREFIX = {json.dumps(sa['realm_prefix'])}",
        "",
        "",
        "def normalize_realm(raw_realm: str) -> str:",
        "    assert isinstance(raw_realm, str)",
        "    return raw_realm.strip().lower()",
        "",
        "",
        "def check_quota_window(requested_units: int, burst_flag: int) -> dict[str, int | bool]:",
        "    assert type(requested_units) is int and type(burst_flag) is int",
        "    cap = GATEWAY_QUOTA_LIMIT + (GATEWAY_GRACE_STEP if burst_flag == 1 else 0)",
        "    allowed = 1 <= requested_units <= cap",
        '    return {"tier_ok": allowed, "over_cap": requested_units - GATEWAY_QUOTA_LIMIT}',
        "",
        "",
        "def build_ab_frame(realm: str, seq_id: int) -> dict[str, int | str | bool]:",
        "    assert type(seq_id) is int",
        "    norm = normalize_realm(realm)",
        "    realm_ok = norm == GATEWAY_REALM_PREFIX and 1 <= seq_id <= GATEWAY_QUOTA_LIMIT",
        "    scaled = seq_id * GATEWAY_SCALE_FACTOR + GATEWAY_HEADER_BIAS",
        '    return {"realm_ok": realm_ok, "scaled_units": scaled, "realm": norm}',
    ]
    return "\n".join(lines) + "\n"


def render_service_b(sb: dict[str, int | str]) -> str:
    lines = [
        f"LEDGER_FEE_BASIS = {sb['fee_basis']}",
        f"LEDGER_MIN_SETTLE = {sb['min_settle']}",
        f"LEDGER_RECONCILE_MOD = {sb['reconcile_mod']}",
        f"LEDGER_BATCH_TAG = {sb['batch_tag']}",
        f"LEDGER_TENANT_CODE = {json.dumps(sb['tenant_code'])}",
        "",
        "",
        "def compute_settlement_fee(minor_amount: int) -> dict[str, int | bool]:",
        "    assert type(minor_amount) is int",
        "    accepted = minor_amount >= LEDGER_MIN_SETTLE",
        "    fee = minor_amount // LEDGER_FEE_BASIS if accepted else -1",
        '    return {"accepted": accepted, "fee": fee}',
        "",
        "",
        "def verify_ab_frame(scaled_units: int, realm_ok: bool, tenant_code: str, batch_tag: int) -> dict[str, int | bool]:",
        "    assert type(scaled_units) is int and type(batch_tag) is int",
        "    tenant_ok = tenant_code.strip().lower() == LEDGER_TENANT_CODE and batch_tag == LEDGER_BATCH_TAG",
        "    skipped = bool(realm_ok and tenant_ok and (scaled_units + LEDGER_BATCH_TAG) % LEDGER_RECONCILE_MOD == 0)",
        '    return {"tenant_ok": tenant_ok, "reconcile_skipped": skipped}',
        "",
        "",
        "def emit_webhook_digest(tenant_code: str, voucher_no: int) -> dict[str, int | bool]:",
        "    assert type(voucher_no) is int",
        "    tenant_ok = tenant_code.strip().lower() == LEDGER_TENANT_CODE and voucher_no >= 1",
        "    digest_val = voucher_no * LEDGER_FEE_BASIS + LEDGER_BATCH_TAG",
        '    return {"tenant_ok": tenant_ok, "digest_val": digest_val}',
        "",
        "",
        "def forward_pipeline_stage(scaled_units: int, realm_ok: bool, tenant_code: str) -> dict[str, int | bool]:",
        "    assert type(scaled_units) is int",
        "    tenant_ok = tenant_code.strip().lower() == LEDGER_TENANT_CODE",
        "    upstream_ok = bool(realm_ok and tenant_ok)",
        "    stage_b = scaled_units + LEDGER_BATCH_TAG + LEDGER_MIN_SETTLE",
        "    checksum = stage_b % LEDGER_RECONCILE_MOD",
        '    return {"upstream_ok": upstream_ok, "stage_b": stage_b, "checksum": checksum}',
    ]
    return "\n".join(lines) + "\n"


def render_service_c(sc: dict[str, int | str]) -> str:
    lines = [
        f"CACHE_TTL_SECONDS = {sc['ttl_seconds']}",
        f"CACHE_SKEW_TOLERANCE = {sc['skew_tolerance']}",
        f"CACHE_SHARD_MOD = {sc['shard_mod']}",
        f"CACHE_SLOT_SALT = {sc['slot_salt']}",
        f"CACHE_CHANNEL_ID = {json.dumps(sc['channel_id'])}",
        "",
        "",
        "def check_freshness(age_seconds: int, retry_bit: int) -> dict[str, int | bool]:",
        "    assert type(age_seconds) is int and type(retry_bit) is int",
        "    limit = CACHE_TTL_SECONDS + CACHE_SKEW_TOLERANCE if retry_bit == 1 else CACHE_TTL_SECONDS - 1",
        "    accepted = 0 <= age_seconds <= limit",
        '    return {"accepted": accepted, "stale_excess": age_seconds - CACHE_TTL_SECONDS}',
        "",
        "",
        "def route_webhook_shard(digest_val: int, tenant_ok: bool, channel_id: str, slot_salt: int) -> dict[str, int | bool]:",
        "    assert type(digest_val) is int and type(slot_salt) is int",
        "    chan_ok = channel_id.strip().lower() == CACHE_CHANNEL_ID and slot_salt == CACHE_SLOT_SALT",
        "    slot = (digest_val + CACHE_SLOT_SALT) % CACHE_SHARD_MOD",
        '    return {"chan_ok": chan_ok, "slot": slot, "reserved_shard_hit": bool(tenant_ok and chan_ok and slot == 0)}',
        "",
        "",
        "def verify_pipeline_sync(stage_b: int, upstream_ok: bool, channel_id: str, sync_window: int) -> dict[str, int | bool]:",
        "    assert type(stage_b) is int and type(sync_window) is int",
        "    expected_win = CACHE_TTL_SECONDS + CACHE_SLOT_SALT",
        "    chan_ok = channel_id.strip().lower() == CACHE_CHANNEL_ID and sync_window == expected_win",
        "    desync = bool(upstream_ok and chan_ok and (stage_b + sync_window) % CACHE_SHARD_MOD == 0)",
        '    return {"chan_ok": chan_ok, "pipeline_desync": desync}',
    ]
    return "\n".join(lines) + "\n"


def render_all_services(instance: dict[str, Any]) -> dict[str, tuple[str, str]]:
    svcs = instance["services"]
    return {
        "service_a": ("service_a/auth_gateway.py", render_service_a(svcs["service_a"])),
        "service_b": ("service_b/ledger_worker.py", render_service_b(svcs["service_b"])),
        "service_c": ("service_c/webhook_cache.py", render_service_c(svcs["service_c"])),
    }


def seat_workspace_files(instance: dict[str, Any], seat: str) -> dict[str, str]:
    rendered = render_all_services(instance)
    return {rendered[svc][0]: rendered[svc][1] for svc in instance["workspace_services"][seat]}


def replay(
    instance: dict[str, Any],
    seat_tokens: dict[str, str],
    roster: dict[str, str] | str = "oracle",
    pick: Callable[[list[str], int], str] | None = None,
    patience: int = policies_mod.PATIENCE,
) -> dict[str, Any]:
    r_map = dict.fromkeys(instance["seats"], roster) if isinstance(roster, str) else dict(roster)
    tok_to_seat = {tok: seat for seat, tok in seat_tokens.items()}
    with tempfile.TemporaryDirectory(prefix="audit_ep_") as tmp:
        ev_path = Path(tmp) / "events.jsonl"
        game = world.AuditWorld(instance, tokens=tok_to_seat, events=ev_path)
        plays = {
            seat: policies_mod.POLICIES[pol](
                policies_mod.Seat(game, seat, seat_tokens[seat], seat_workspace_files(instance, seat), patience)
            )
            for seat, pol in r_map.items()
            if pol != "nop"
        }
        for turn in range(100_000):
            if not plays:
                break
            seats = sorted(plays)
            chosen = pick(seats, turn) if pick else seats[turn % len(seats)]
            if next(plays[chosen], False) is False:
                del plays[chosen]
        assert not plays, "a policy did not finish"
        game.close()
        raw_bytes = ev_path.read_bytes()
        parsed, ok = verify.parse_events_bytes(raw_bytes)
        assert ok and game.log_intact and worldkit.chain_ok(parsed)
        return {"events": parsed, "raw_bytes": raw_bytes, "world": game}


def play(
    instance: dict[str, Any],
    seat_tokens: dict[str, str],
    roster: dict[str, str] | str = "oracle",
    pick: Callable[[list[str], int], str] | None = None,
    patience: int = policies_mod.PATIENCE,
) -> tuple[list[dict[str, Any]], bytes]:
    ep = replay(instance, seat_tokens, roster, pick=pick, patience=patience)
    return ep["events"], ep["raw_bytes"]


def simulate(
    instance: dict[str, Any],
    seat_tokens: dict[str, str],
    roster: dict[str, str] | str = "oracle",
    pick: Callable[[list[str], int], str] | None = None,
    patience: int = policies_mod.PATIENCE,
) -> dict[str, float]:
    evs, _ = play(instance, seat_tokens, roster, pick=pick, patience=patience)
    return verify.score(evs, instance)


def reference(config: dict[str, Any]) -> dict[str, dict[str, dict[str, float]]]:
    table: dict[str, dict[str, dict[str, float]]] = {}
    for cond in CONDITIONS:
        table[cond] = {}
        inst_list = instances(config, cond)
        for pol in POLICIES:
            scores = [round(simulate(inst, tokens(config, inst), pol)["reward"], 6) for inst in inst_list]
            table[cond][pol] = {
                "mean": round(sum(scores) / len(scores), 6),
                "min": min(scores),
                "max": max(scores),
            }
    return table


def reference_toml(table: dict[str, dict[str, dict[str, float]]]) -> str:
    lines: list[str] = []
    for cond, pols in table.items():
        for pol, stats in pols.items():
            lines.append(f"[reference.{cond}.{pol}]")
            for key in ("mean", "min", "max"):
                lines.append(f"{key} = {json.dumps(stats[key])}")
            lines.append("")
    return "\n".join(lines)


def check(config: dict[str, Any]) -> int:
    if config.get("reference") == reference(config):
        return 0
    print("family.toml [reference] differs: run reference.py --write", file=sys.stderr)
    return 1


def _seeded_pick(rng: random.Random) -> Callable[[list[str], int], str]:
    def _pick(seats: list[str], _turn: int) -> str:
        return rng.choice(seats)

    return _pick


def _assert_expected_matrix(config: dict[str, Any]) -> int:
    checked = 0
    want_ora = {"solo-x3": 0.666667, "indep-3": 0.5, "team-3": 1.0, "team-3-claimlock": 1.0}
    want_nai = {"solo-x3": 0.5, "indep-3": 0.5, "team-3": 0.5, "team-3-claimlock": 1.0}
    for cond in CONDITIONS:
        for inst in instances(config, cond):
            toks = tokens(config, inst)
            for pol in POLICIES:
                sc = simulate(inst, toks, pol)
                assert sc["chain_ok"] == 1.0 and sc["script_faithful"] == 1.0
                if pol in ("nop", "spoof"):
                    assert sc["reward"] == 0.0
                elif pol == "oracle":
                    assert sc["reward"] == want_ora[cond] and sc["invalid_submissions"] == 0.0 and sc["duplicate_submissions"] == 0.0
                else:
                    assert sc["reward"] == want_nai[cond]
                checked += 1
            pick_fn = _seeded_pick(random.Random(inst["instance"] + 2026))
            assert simulate(inst, toks, "oracle", pick=pick_fn)["reward"] == want_ora[cond]
            assert simulate(inst, toks, "naive", pick=pick_fn)["reward"] == want_nai[cond]
    return checked


def _prove_workspaces_and_fences(config: dict[str, Any]) -> int:
    checked = 0
    build_dir = FAMILY / "build"
    for cond in CONDITIONS:
        for inst in instances(config, cond):
            task_dir = build_dir / cond / f"audit_v1-i{int(inst['instance']):02d}"
            for seat in inst["seats"]:
                ws = task_dir / "environment" / f"seat_{seat}" / "workspace"
                actual = sorted(p.relative_to(ws).as_posix() for p in ws.rglob("*") if p.is_file())
                want = sorted(seat_workspace_files(inst, seat))
                assert actual == want
                checked += 1
    cl_inst = instances(config, "team-3-claimlock")[0]
    cl_toks = tokens(config, cl_inst)
    with tempfile.TemporaryDirectory(prefix="audit_fence_") as tmp:
        game = world.AuditWorld(cl_inst, tokens={t: s for s, t in cl_toks.items()}, events=Path(tmp) / "f.jsonl")
        assert game.dispatch("p1", "POST", "/claim", {"target": "a-local"})[0] == 200
        assert game.dispatch("p1", "POST", "/claim", {"target": "b-local"})[0] == 200
        st3, r3 = game.dispatch("p1", "POST", "/claim", {"target": "c-local"})
        assert st3 == 409 and r3["error"] == "lease_limit"
        bad_w = policies_mod.finding("a-local", ["service_a"], {"units": 1, "burst": 1}, "quota_grace_drift")
        for _ in range(4):
            assert game.dispatch("p1", "POST", "/submit_finding", bad_w)[0] == 200
        assert game.dispatch("p2", "GET", "/claims")[1]["claims"] == []
        assert game.dispatch("p2", "POST", "/claim", {"target": "b-local"})[0] == 200
        game.close()
    return checked


def _make_live_handler(game: Any) -> type[http.server.BaseHTTPRequestHandler]:
    class LiveHandler(http.server.BaseHTTPRequestHandler):
        def _serve(self, method: str) -> None:
            tok, raw_clen, hdrs, hdr_err = codec.extract_http_headers(self.headers)
            if hdr_err is not None:
                self.close_connection = True
                raw_err = codec.canonical_json({"error": hdr_err}).encode("utf-8")
                with contextlib.suppress(OSError):
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw_err)))
                    self.end_headers()
                    self.wfile.write(raw_err)
                return
            body, b_err = (None, None)
            if method == "POST":
                body, b_err = codec.read_framed_json_body(self.rfile, raw_clen, allow_empty_object=False)
            st_c, resp = game.handle(method, self.path, tok, body, REMOTE_CLIENT, headers=hdrs, body_error=b_err)
            raw = codec.canonical_json(resp).encode("utf-8")
            with contextlib.suppress(OSError):
                self.send_response(st_c)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        def do_GET(self) -> None:
            self._serve(self.command)

        do_POST = do_PUT = do_DELETE = do_PATCH = do_GET

        def log_message(self, format: str, *args: Any) -> None:
            _ = (format, args)

    return LiveHandler


def _run_live_http(inst: dict[str, Any], seat_tokens: dict[str, str], pol: str) -> dict[str, float]:
    task_dir = FAMILY / "build" / inst["condition"] / f"audit_v1-i{int(inst['instance']):02d}"
    with tempfile.TemporaryDirectory(prefix="audit_live_") as tmp:
        ev_path = Path(tmp) / "live.jsonl"
        game = world.AuditWorld(inst, tokens={t: s for s, t in seat_tokens.items()}, events=ev_path)
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _make_live_handler(game))
        port = httpd.server_address[1]
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        try:
            script = str(task_dir / "policies" / f"{pol}.sh")
            procs = [
                subprocess.Popen(
                    ["bash", script],
                    env={
                        **os.environ,
                        "WORLD": f"http://127.0.0.1:{port}",
                        "SEAT": s,
                        "SEAT_TOKEN": seat_tokens[s],
                        "AUDIT_WORKSPACE": str(task_dir / "environment" / f"seat_{s}" / "workspace"),
                        "ORACLE_POLL_SEC": "0.02",
                        "ORACLE_WAIT_SEC": "10",
                        "PYTHONDONTWRITEBYTECODE": "1",
                    },
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for s in inst["seats"]
            ]
            for proc in procs:
                _, err_txt = proc.communicate(timeout=15)
                assert proc.returncode == 0, err_txt
            game.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            th.join(timeout=5)
        evs, ok = verify.parse_events_bytes(ev_path.read_bytes())
        assert ok
        return verify.score(evs, inst)


def _prove_live_and_attribution(config: dict[str, Any]) -> int:
    passed = 0
    for cond in CONDITIONS:
        inst = instances(config, cond)[0]
        toks = tokens(config, inst)
        for pol in POLICIES:
            live_sc = _run_live_http(inst, toks, pol)
            sim_sc = simulate(inst, toks, pol)
            assert live_sc["reward"] == sim_sc["reward"] and live_sc["spoof_detected"] == sim_sc["spoof_detected"]
            passed += 1
    repo_root = str(FAMILY.parents[2])
    added = repo_root not in sys.path
    if added:
        sys.path.insert(0, repo_root)
    try:
        from muster import attribution, load, regrade
    finally:
        if added and repo_root in sys.path:
            sys.path.remove(repo_root)
    inst = instances(config, "team-3-claimlock")[0]
    toks = tokens(config, inst)
    task_dir = FAMILY / "build" / "team-3-claimlock" / "audit_v1-i01"
    score_fn, loaded_inst, err = regrade.load_score(task_dir)
    assert err is None and score_fn is not None and loaded_inst == inst
    rosters = [
        {"p1": "oracle", "p2": "oracle", "p3": "oracle"},
        {"p1": "nop", "p2": "oracle", "p3": "oracle"},
        {"p1": "oracle", "p2": "nop", "p3": "oracle"},
        {"p1": "oracle", "p2": "oracle", "p3": "nop"},
        {"p1": "nop", "p2": "nop", "p3": "nop"},
        {"p1": "spoof", "p2": "oracle", "p3": "oracle"},
    ]
    with tempfile.TemporaryDirectory(prefix="audit_attr_") as tmp:
        root = Path(tmp)
        for idx, rost in enumerate(rosters):
            evs, raw = play(inst, toks, rost)
            rews = verify.score(evs, inst)
            assert regrade.regrade_trial(rews, evs, evs[:-1], score_fn, loaded_inst)["status"] == "match"
            tdir = root / f"trial_{idx:02d}"
            (tdir / "verifier").mkdir(parents=True, exist_ok=True)
            (tdir / "verifier" / "events.jsonl").write_bytes(raw)
            (tdir / "verifier" / "reward.json").write_text(json.dumps(rews) + "\n", encoding="utf-8")
            parts = [{"seat": s, "name": p} for s, p in rost.items()]
            s_res = [{"seat": s, "under_test": True, "agent_info": {"name": p}, "exit_code": 0} for s, p in rost.items()]
            doc = {"trial_name": tdir.name, "task_name": "muster/audit_v1-i01", "config": {"task": {"path": str(task_dir)}, "participants": parts}, "seat_results": s_res, "verifier_result": {"rewards": rews}, "reward_valid": True}
            (tdir / "result.json").write_text(json.dumps(doc) + "\n", encoding="utf-8")
        rep = attribution.analyze(load.walk(root), oracle_reward=1.0, exploits=["spoof"])
        assert rep["errors"] == 0 and rep["warnings"] == 0, rep["findings"]
    return passed + len(rosters)


def prove(config: dict[str, Any]) -> int:
    total = _assert_expected_matrix(config) + _prove_workspaces_and_fences(config) + _prove_live_and_attribution(config)
    print(f"audit_v1 proofs: passed={total}, failed=0")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage [reference] tables and proofs for audit_v1.")
    for flag in ("--write", "--check", "--prove"):
        parser.add_argument(flag, action="store_true")
    args = parser.parse_args(argv)
    config = family()
    if args.write:
        path = FAMILY / "family.toml"
        head = path.read_text(encoding="utf-8").split("[reference.")[0].rstrip("\n")
        path.write_text(head + "\n\n" + reference_toml(reference(config)), encoding="utf-8")
        return 0
    if not (args.check or args.prove):
        print(reference_toml(reference(config)), end="")
        return 0
    status = check(config) if args.check else 0
    if args.prove and status == 0:
        status = max(status, prove(config))
    return status


if __name__ == "__main__":
    sys.exit(main())
