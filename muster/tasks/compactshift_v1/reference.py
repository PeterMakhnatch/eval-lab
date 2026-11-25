from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.server
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import tomllib
from pathlib import Path
from random import Random
from types import ModuleType
from typing import Any

sys.dont_write_bytecode = True

FAMILY = Path(__file__).resolve().parent
TEMPLATE = FAMILY / "template"
KIT_V2 = FAMILY.parent / "_kit_v2"
CONDITIONS = ("solo-x3", "indep-3", "team-3", "team-3-fenced")
POLICIES = ("oracle", "nop", "stale_compactor", "blind_reporter", "spoof")
DOMAINS = (
    "cache-cutover",
    "checkpoint-rollover",
    "config-compaction",
    "ingress-drain",
    "journal-epoch",
    "kv-rebalance",
    "lease-migration",
    "quorum-reseed",
    "schema-promote",
    "wal-rotation",
)
REGIONS = ("us-east-1", "us-west-2", "eu-west-1", "ap-south-1")
SHIFT_PAIRS = (
    ("drain_mode", "strict-quiesce"),
    ("quorum_fence", "epoch-fenced"),
    ("compaction_tier", "tier-sealed"),
    ("wal_sync", "fsync-barrier"),
)
REMOTE_CLIENT = "10.0.0.2"


def _load_mod(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _bootstrap_modules() -> tuple[ModuleType, ModuleType, ModuleType, ModuleType, ModuleType, ModuleType]:
    saved_path = list(sys.path)
    sys.path.insert(0, str(KIT_V2))
    try:
        c_mod = _load_mod("codec", KIT_V2 / "codec.py")
        wk_mod = _load_mod("worldkit", KIT_V2 / "worldkit.py")
        b_mod = _load_mod("compactshift_v1_build", KIT_V2 / "build.py")
        w_mod = _load_mod("compactshift_v1_world", TEMPLATE / "world.py")
        v_mod = _load_mod("compactshift_v1_verify", TEMPLATE / "verify.py")
        p_mod = _load_mod("compactshift_v1_policies", FAMILY / "policies.py")
        return c_mod, wk_mod, b_mod, w_mod, v_mod, p_mod
    finally:
        sys.path[:] = saved_path


codec, worldkit, build, world, verify, policies_mod = _bootstrap_modules()


def _digest(*parts: object) -> str:
    return hashlib.sha256("/".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def family() -> dict[str, Any]:
    config = tomllib.loads((FAMILY / "family.toml").read_text(encoding="utf-8"))
    assert config["name"] == FAMILY.name == "compactshift_v1"
    assert int(config["instances"]) == 10
    assert tuple(config["conditions"]) == CONDITIONS
    assert tuple(config["policies"]) == POLICIES
    assert config["exploits"] == ["spoof"]
    return config


def layouts(config: dict[str, Any]) -> list[dict[str, Any]]:
    seed = str(config["seed"])
    count = int(config["instances"])
    assert count == len(DOMAINS) == 10
    ranked_domains = sorted(DOMAINS, key=lambda d: _digest(seed, "domain", d))
    items: list[dict[str, Any]] = []
    for n in range(1, count + 1):
        d = _digest(seed, "instance", n)
        tid = f"cshift-{n:02d}-{d[:6]}"
        dom = ranked_domains[n - 1]
        region = REGIONS[int(d[6:8], 16) % len(REGIONS)]
        batch_cap = 64 * (1 + (int(d[8:10], 16) % 8))
        s_key, s_val = SHIFT_PAIRS[int(d[10:12], 16) % len(SHIFT_PAIRS)]
        inv_tok = f"inv-{n:02d}-{d[12:22]}"
        plan_seal = f"ps-{n:02d}-{d[22:34]}"
        handoff_id = f"ho-{n:02d}-{d[34:44]}"
        b_cons = {"region": region, "batch_cap": batch_cap}
        exp_cons = {**b_cons, s_key: s_val, "invariant_token": inv_tok}
        h_dig = world.compute_handoff_digest(tid, plan_seal, exp_cons)
        v_dig = world.compute_verification_digest(tid, h_dig, inv_tok)
        items.append({
            "instance": n,
            "task_id": tid,
            "domain": dom,
            "base_spec": {"schema": "v1", "service": dom, "base_constraints": b_cons},
            "shift_constraints": [
                {"key": s_key, "value": s_val},
                {"key": "invariant_token", "value": inv_tok},
            ],
            "invariant_token": inv_tok,
            "required_constraints": ["region", "batch_cap", s_key, "invariant_token"],
            "plan_seal": plan_seal,
            "handoff_id": handoff_id,
            "handoff_digest": h_dig,
            "verification_digest": v_dig,
        })
    assert len({it["task_id"] for it in items}) == count
    assert len({it["plan_seal"] for it in items}) == count
    return items


def instances(config: dict[str, Any], condition: str) -> list[dict[str, Any]]:
    assert condition in CONDITIONS
    spec = config["conditions"][condition]
    out: list[dict[str, Any]] = []
    for item in layouts(config):
        out.append({
            "family": config["name"],
            "condition": condition,
            "instance": item["instance"],
            "seats": [str(s) for s in spec["seats"]],
            "channel": bool(spec["channel"]),
            "notices_enabled": bool(spec["notices_enabled"]),
            "unread_guard_kinds": [str(k) for k in spec["unread_guard_kinds"]],
            "fenced": bool(spec["fenced"]),
            **{
                k: item[k]
                for k in (
                    "task_id",
                    "domain",
                    "base_spec",
                    "shift_constraints",
                    "invariant_token",
                    "required_constraints",
                    "plan_seal",
                    "handoff_id",
                    "handoff_digest",
                    "verification_digest",
                )
            },
        })
    return out


def tokens(config: dict[str, Any], instance: dict[str, Any]) -> dict[str, str]:
    salted = f"{config['seed']}/{instance['condition']}"
    inum = int(instance["instance"])
    return {str(s): build.token(salted, inum, str(s)) for s in instance["seats"]}


def replay(
    instance: dict[str, Any],
    seat_tokens: dict[str, str],
    policy: str = "oracle",
) -> dict[str, Any]:
    assert policy in POLICIES
    tok_to_seat = {tok: seat for seat, tok in seat_tokens.items()}
    with tempfile.TemporaryDirectory(prefix="cshift_ep_") as tmp:
        ev_path = Path(tmp) / "events.jsonl"
        game = world.CompactShiftWorld(instance, tokens=tok_to_seat, events=ev_path)
        policies_mod.POLICIES[policy](game, instance, seat_tokens)
        game.close()
        raw_bytes = ev_path.read_bytes()
        parsed, ok = verify.parse_events_strict(raw_bytes)
        assert ok and game.log_intact
        return {"events": parsed, "raw_bytes": raw_bytes, "world": game}


def play(
    instance: dict[str, Any],
    seat_tokens: dict[str, str],
    policy: str = "oracle",
) -> tuple[list[dict[str, Any]], bytes]:
    ep = replay(instance, seat_tokens, policy)
    return ep["events"], ep["raw_bytes"]


def simulate(
    instance: dict[str, Any],
    seat_tokens: dict[str, str],
    policy: str = "oracle",
) -> dict[str, float]:
    evs, _ = play(instance, seat_tokens, policy)
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
            for k in ("mean", "min", "max"):
                lines.append(f"{k} = {json.dumps(stats[k])}")
            lines.append("")
    return "\n".join(lines)


def check(config: dict[str, Any]) -> int:
    if config.get("reference") == reference(config):
        return 0
    print("family.toml [reference] differs: run reference.py --write", file=sys.stderr)
    return 1


def _assert_expected_matrix(config: dict[str, Any]) -> int:
    checked = 0
    want = {
        ("solo-x3", "oracle"): 1.0,
        ("indep-3", "oracle"): 0.25,
        ("team-3", "oracle"): 1.0,
        ("team-3-fenced", "oracle"): 1.0,
    }
    for cond in CONDITIONS:
        for inst in instances(config, cond):
            toks = tokens(config, inst)
            for pol in POLICIES:
                sc = simulate(inst, toks, pol)
                assert sc["chain_ok"] == 1.0 and sc["script_faithful"] == 1.0
                if pol in ("nop", "spoof"):
                    assert sc["reward"] == 0.0
                elif pol == "stale_compactor":
                    assert sc["reward"] == 0.15 and sc["compaction_loss"] == 1.0
                elif pol == "blind_reporter":
                    assert sc["reward"] == 0.10 and sc["phantom_success"] == 1.0
                else:
                    assert sc["reward"] == want[(cond, "oracle")]
                checked += 1
    return checked


def _prove_fences_and_tamper(config: dict[str, Any]) -> int:
    f_inst = instances(config, "team-3-fenced")[0]
    f_toks = tokens(config, f_inst)
    tok_map = {t: s for s, t in f_toks.items()}
    with tempfile.TemporaryDirectory(prefix="cshift_fence_") as tmp:
        game = world.CompactShiftWorld(f_inst, tokens=tok_map, events=Path(tmp) / "f.jsonl")
        b_cons = dict(f_inst["base_spec"]["base_constraints"])
        tid = str(f_inst["task_id"])
        st_p, p_res = game.dispatch("planner", "POST", "/plan", {"task_id": tid, "base_digest": world.compute_base_digest(tid, b_cons)})
        assert st_p == 200
        game.dispatch("planner", "POST", "/msg", {"text": json.dumps({"plan_seal": p_res["plan_seal"]})})
        s_map = {str(x["key"]): x["value"] for x in f_inst["shift_constraints"]}
        merged = {**b_cons, **s_map}
        h_dig = world.compute_handoff_digest(tid, str(p_res["plan_seal"]), merged)
        st_unr, unr_res = game.dispatch("builder", "POST", "/handoff", {"epoch": 2, "expected_version": 2, "constraints": merged, "handoff_digest": h_dig})
        assert st_unr == 409 and unr_res["error"] == "unread_messages"
        game.dispatch("builder", "GET", "/msgs")
        st_unack, unack_res = game.dispatch("builder", "POST", "/handoff", {"epoch": 2, "expected_version": 2, "constraints": merged, "handoff_digest": h_dig})
        assert st_unack == 409 and unack_res["error"] == "unacked_epoch"
        game.dispatch("builder", "GET", "/notices")
        st_stale, stale_res = game.dispatch("builder", "POST", "/handoff", {"epoch": 2, "expected_version": 1, "constraints": merged, "handoff_digest": h_dig})
        assert st_stale == 409 and stale_res["error"] == "stale_version"
        st_ok, _ = game.dispatch("builder", "POST", "/handoff", {"epoch": 2, "expected_version": 2, "constraints": merged, "handoff_digest": h_dig})
        assert st_ok == 200
        game.close()
        sc = verify.score(game.events, f_inst)
        assert sc["unread_guard_blocks"] == 1.0 and sc["stale_version_rejects"] == 1.0 and sc["script_faithful"] == 1.0
    evs_o, raw_o = play(f_inst, f_toks, "oracle")
    rng = Random(20261125)
    for _ in range(32):
        pos = rng.randrange(len(raw_o))
        mut = bytearray(raw_o)
        mut[pos] ^= 0x01
        p_evs, p_ok = verify.parse_events_strict(bytes(mut))
        assert not p_ok or verify.score(p_evs, f_inst)["reward"] == 0.0
    forged = [json.loads(json.dumps(e)) for e in evs_o]
    plan_ev = next(ev for ev in forged if ev["kind"] == "plan")
    plan_ev["response"]["plan_seal"] = "ps-forged"
    prev = codec.GENESIS_HASH
    for ev in forged:
        ev["prev_hash"] = prev
        prev = codec.compute_entry_hash(prev, ev)
        ev["entry_hash"] = prev
    t_sc = verify.score(forged, f_inst)
    assert t_sc["chain_ok"] == 1.0 and t_sc["script_faithful"] == 0.0 and t_sc["reward"] == 0.0
    return 40


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
            is_close = (
                self.path.split("?", 1)[0].strip("/") == "admin/close"
                and self.headers.get("X-Test-Loopback") == "1"
            )
            body, b_err = None, None
            if method == "POST":
                try:
                    body, b_err = codec.read_framed_json_body(self.rfile, raw_clen, allow_empty_object=is_close)
                except OSError:
                    self.close_connection = True
                    return
            client_ip = "127.0.0.1" if is_close else REMOTE_CLIENT
            st_c, resp = game.handle(method, self.path, tok, body, client_ip, headers=hdrs, body_error=b_err)
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
    tok_map = {t: s for s, t in seat_tokens.items()}
    with tempfile.TemporaryDirectory(prefix="cshift_live_") as tmp:
        ev_path = Path(tmp) / "live.jsonl"
        game = world.CompactShiftWorld(inst, tokens=tok_map, events=ev_path)
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _make_live_handler(game))
        port = httpd.server_address[1]
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        try:
            script = str(TEMPLATE / "policies" / f"{pol}.sh")
            procs = [
                subprocess.Popen(
                    ["bash", script],
                    env={
                        **os.environ,
                        "WORLD": f"http://127.0.0.1:{port}",
                        "SEAT": s,
                        "SEAT_TOKEN": seat_tokens[s],
                        "POLL_SLEEP": "0.02",
                        "PYTHONDONTWRITEBYTECODE": "1",
                    },
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for s in inst["seats"]
            ]
            for p in procs:
                _, err_txt = p.communicate(timeout=15)
                assert p.returncode == 0, err_txt
            game.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            th.join(timeout=5)
        evs, ok = verify.parse_events_strict(ev_path.read_bytes())
        assert ok
        return verify.score(evs, inst)


def _run_mixed_roster(inst: dict[str, Any], seat_tokens: dict[str, str], roster: dict[str, str]) -> tuple[bytes, dict[str, float]]:
    tok_map = {t: s for s, t in seat_tokens.items()}
    with tempfile.TemporaryDirectory(prefix="cshift_mix_") as tmp:
        ev_path = Path(tmp) / "events.jsonl"
        game = world.CompactShiftWorld(inst, tokens=tok_map, events=ev_path)
        gens: dict[str, Any] = {}
        for s_name, pol in roster.items():
            if pol == "oracle":
                fn = (
                    policies_mod._oracle_planner
                    if s_name == "planner"
                    else (policies_mod._oracle_builder if s_name == "builder" else policies_mod._oracle_verifier)
                )
                gens[s_name] = fn(game, s_name)
            elif pol == "spoof":
                gens[s_name] = policies_mod._spoof_seat(game, s_name, seat_tokens[s_name])
        for turn in range(60):
            if not gens:
                break
            keys = sorted(gens)
            cur = keys[turn % len(keys)]
            if next(gens[cur], False) is False:
                del gens[cur]
        game.close()
        raw = ev_path.read_bytes()
        evs, ok = verify.parse_events_strict(raw)
        assert ok
        return raw, verify.score(evs, inst)


def _prove_muster_attribution(config: dict[str, Any]) -> int:
    repo_root = str(FAMILY.parents[2])
    added = repo_root not in sys.path
    if added:
        sys.path.insert(0, repo_root)
    try:
        from muster import attribution, load, regrade
    finally:
        if added and repo_root in sys.path:
            sys.path.remove(repo_root)
    inst = instances(config, "team-3-fenced")[0]
    toks = tokens(config, inst)
    task_dir = FAMILY / "build" / "team-3-fenced" / "compactshift_v1-i01"
    score_fn, loaded_inst, err = regrade.load_score(task_dir)
    assert err is None and score_fn is not None and loaded_inst is not None
    rosters = [
        {"planner": "oracle", "builder": "oracle", "verifier": "oracle"},
        {"planner": "nop", "builder": "oracle", "verifier": "oracle"},
        {"planner": "oracle", "builder": "nop", "verifier": "oracle"},
        {"planner": "oracle", "builder": "oracle", "verifier": "nop"},
        {"planner": "nop", "builder": "nop", "verifier": "nop"},
        {"planner": "spoof", "builder": "nop", "verifier": "nop"},
        {"planner": "nop", "builder": "spoof", "verifier": "nop"},
        {"planner": "nop", "builder": "nop", "verifier": "spoof"},
    ]
    with tempfile.TemporaryDirectory(prefix="cshift_attr_") as tmp:
        root = Path(tmp)
        for idx, rost in enumerate(rosters):
            raw, rews = _run_mixed_roster(inst, toks, rost)
            evs, _ = verify.parse_events_strict(raw)
            assert regrade.regrade_trial(rews, evs, evs, score_fn, loaded_inst)["status"] == "match"
            tdir = root / f"trial_{idx:02d}"
            (tdir / "verifier").mkdir(parents=True, exist_ok=True)
            (tdir / "verifier" / "events.jsonl").write_bytes(raw)
            (tdir / "verifier" / "reward.json").write_text(json.dumps(rews) + "\n", encoding="utf-8")
            parts = [
                {"seat": s, "name": p} if p in ("oracle", "nop") else {"seat": s, "import_path": "muster.agents.script:ScriptAgent", "kwargs": {"script": f"/policies/{p}.sh"}}
                for s, p in rost.items()
            ]
            s_res = [{"seat": s, "under_test": True, "agent_info": {"name": p}, "exit_code": 0} for s, p in rost.items()]
            doc = {"trial_name": tdir.name, "task_name": f"muster/{task_dir.name}", "config": {"task": {"path": str(task_dir)}, "participants": parts}, "seat_results": s_res, "verifier_result": {"rewards": rews}, "reward_valid": True}
            (tdir / "result.json").write_text(json.dumps(doc) + "\n", encoding="utf-8")
        rep = attribution.analyze(load.walk(root), oracle_reward=1.0, exploits=["spoof"])
        assert rep["errors"] == 0 and rep["warnings"] == 0, rep["findings"]
    return len(rosters)


def _prove_live_policies(config: dict[str, Any]) -> int:
    passed = 0
    for cond in CONDITIONS:
        inst = instances(config, cond)[0]
        toks = tokens(config, inst)
        for pol in POLICIES:
            live_sc = _run_live_http(inst, toks, pol)
            sim_sc = simulate(inst, toks, pol)
            assert live_sc["reward"] == sim_sc["reward"]
            assert live_sc["compaction_loss"] == sim_sc["compaction_loss"]
            assert live_sc["phantom_success"] == sim_sc["phantom_success"]
            assert live_sc["spoof_detected"] == sim_sc["spoof_detected"]
            passed += 1
    return passed


def prove(config: dict[str, Any]) -> int:
    total = (
        _assert_expected_matrix(config)
        + _prove_fences_and_tamper(config)
        + _prove_live_policies(config)
        + _prove_muster_attribution(config)
    )
    print(f"compactshift_v1 proofs: passed={total}, failed=0")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
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
