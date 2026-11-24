import argparse
import hashlib
import http.client
import http.server
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from random import Random
from types import ModuleType, SimpleNamespace

sys.dont_write_bytecode = True

FAMILY = Path(__file__).resolve().parent
TEMPLATE = FAMILY / "template"
KIT = FAMILY.parent / "_kit"
CONDITIONS = ("fresh-ctrl", "stale-handoff", "midflight-drift", "silent-supersede", "conflicting-handoff")
POLICIES = ("oracle", "trust_handoff", "single_read_commit", "msg_reactive", "always_verify", "nop", "spoof")
DOMAINS = (
    "cache-cutover", "ingress-drain", "ledger-compaction", "queue-partition", "replica-failover",
    "route-rebalance", "shard-cutover", "snapshot-seal", "token-rotation", "wal-checkpoint",
)
FAULTS = ("refused", "reset-before", "reset-after", "timeout-after", "disconnect-after")
WORLD_URL, REMOTE = "http://world:8080", "10.0.0.2"
MAX_POLICY_STEPS, FAULT_RATE, MAX_FAULT_BURST = 256, 0.30, 3
_POLICY_CACHE: dict[str, ModuleType] = {}


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build = _load_module("stalehand_v1_kit_build", KIT / "build.py")
worldkit = _load_module("worldkit", KIT / "worldkit.py")
sys.modules["worldkit"] = worldkit
world = _load_module("stalehand_v1_world", TEMPLATE / "world.py")
verify = _load_module("stalehand_v1_verify", TEMPLATE / "verify.py")


def _digest(*parts: object) -> str:
    return hashlib.sha256("/".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def family() -> dict:
    config = tomllib.loads((FAMILY / "family.toml").read_text(encoding="utf-8"))
    assert config["name"] == FAMILY.name == "stalehand_v1" and int(config["instances"]) == 10
    assert tuple(config["conditions"]) == CONDITIONS and tuple(config["policies"]) == POLICIES and config["exploits"] == ["spoof"]
    return config


def layouts(config: dict) -> list[dict]:
    seed, count = str(config["seed"]), int(config["instances"])
    assert count == len(DOMAINS) == 10
    ranked_domains = sorted(DOMAINS, key=lambda d: _digest(seed, "domain", d))
    peer_set = set(sorted(range(1, count + 1), key=lambda n: _digest(seed, "conflict_winner", n))[: count // 2])
    assert len(peer_set) == 5 and peer_set not in ({1, 2, 3, 4, 5}, {6, 7, 8, 9, 10}, {1, 3, 5, 7, 9}, {2, 4, 6, 8, 10})
    items = [
        {"instance": n, "task_id": f"task-{n:02d}-{(_d := _digest(seed, 'item', n))[:6]}", "check_id": f"chk-{n:02d}-{_d[6:12]}", "domain": ranked_domains[n - 1], "v1_target": f"tgt-{n:02d}-a-{_d[12:18]}", "v2_target": f"tgt-{n:02d}-b-{_d[18:24]}", "v1_param": 100 + (int(_d[24:32], 16) % 400), "v2_param": 500 + (int(_d[32:40], 16) % 450), "peer_wins": n in peer_set}
        for n in range(1, count + 1)
    ]
    assert len({it["task_id"] for it in items}) == count and len({it["check_id"] for it in items}) == count
    return items


def _condition_payload(condition: str, item: dict) -> dict:
    tid, cid, dom, t1, t2, p1, p2 = item["task_id"], item["check_id"], item["domain"], item["v1_target"], item["v2_target"], item["v1_param"], item["v2_param"]
    init_msgs = [
        {"from": "p_prev", "role": "predecessor", "text": f"[{tid}] Handoff from p_prev ({dom}): snapshot version=1 ready for p1 — target={t1}, param={p1}. Verify live /state freshness before executing; check_id={cid}."},
        {"from": "p_coord", "role": "coordinator", "text": f"[{tid}] Dependency registry online (p_coord) for {dom}: check_id={cid}. Query POST /verify_dependency only when upstream handoffs conflict."},
    ]
    if condition == "conflicting-handoff":
        init_msgs.append({"from": "p_peer", "role": "peer", "text": f"[{tid}] Upstream handoff notice from p_peer: concurrent dependency memo specifies target={t2}, param={p2} (candidate v2), contradicting p_prev's v1 handoff ({t1}, param={p1}). Reconcile {cid} with p_coord before executing."})
    drift_msg = {"from": "p_peer", "role": "peer", "text": f"[{tid}] Mid-flight update from p_peer: {dom} state superseded to version=2. Re-read GET /state to inspect the updated target and param before POST /execute."} if condition == "midflight-drift" else None
    auth_tgt = (t2 if item["peer_wins"] else t1) if condition == "conflicting-handoff" else (t1 if condition == "fresh-ctrl" else t2)
    auth_prm = (p2 if item["peer_wins"] else p1) if condition == "conflicting-handoff" else (p1 if condition == "fresh-ctrl" else p2)
    is_stale, is_conf = condition == "stale-handoff", condition == "conflicting-handoff"
    return {
        "initial_state": {"state_version": 2 if is_stale else 1, "active_target": t2 if is_stale else t1, "active_param": p2 if is_stale else p1, "drift_happened": is_stale},
        "conflict_initial": is_conf, "drift_on_first_read": condition in ("midflight-drift", "silent-supersede"),
        "drift_state": {"state_version": 2, "active_target": t2, "active_param": p2}, "drift_message": drift_msg,
        "authoritative_resolution": {"version": 1 if condition == "fresh-ctrl" else 2, "target": auth_tgt, "param": auth_prm}, "init_messages": init_msgs,
    }


def instances(config: dict, condition: str) -> list[dict]:
    assert condition in CONDITIONS
    spec = config["conditions"][condition]
    out: list[dict] = []
    for item in layouts(config):
        payload = _condition_payload(condition, item)
        texts = [m["text"] for m in payload["init_messages"]] + ([payload["drift_message"]["text"]] if payload["drift_message"] else [])
        assert all(c not in txt for txt in texts for c in CONDITIONS)
        if payload["drift_message"] is not None:
            assert item["v2_target"] not in payload["drift_message"]["text"] and str(item["v2_param"]) not in payload["drift_message"]["text"]
        out.append({"family": config["name"], "condition": condition, "instance": item["instance"], "seats": list(spec["seats"]), "channel": bool(spec["channel"]), "task_id": item["task_id"], "check_id": item["check_id"], "domain": item["domain"], "handoff": {"handoff_version": 1, "handoff_target": item["v1_target"], "handoff_param": item["v1_param"], "from": "p_prev"}, **payload})
    return out


def tokens(config: dict, instance: dict) -> dict[str, str]:
    return {s: build.token(config["seed"], int(instance["instance"]), s) for s in instance["seats"]}


def policy_module(policy: str) -> ModuleType:
    assert policy in POLICIES and policy != "nop"
    if policy not in _POLICY_CACHE:
        _, opener, rest = (TEMPLATE / "policies" / f"{policy}.sh").read_text(encoding="utf-8").partition("<<'PY'\n")
        code, closer, _ = rest.rpartition("\nPY\n")
        assert opener and closer, policy
        mod = ModuleType(f"stalehand_v1_policy_{policy}")
        exec(compile(code, f"<{policy}.sh>", "exec"), mod.__dict__)
        _POLICY_CACHE[policy] = mod
    return _POLICY_CACHE[policy]


class _Reply(io.BytesIO):
    def __init__(self, status: int, data: bytes):
        super().__init__(data)
        self.status = status


def _faulty_open(game: world.StaleHand, faults: Random, fault_pool: tuple[str, ...] = FAULTS) -> Callable[..., _Reply]:
    burst = [0]

    def urlopen(req: urllib.request.Request, timeout: float) -> _Reply:
        assert timeout > 0 and req.full_url.startswith(WORLD_URL)
        fault = faults.choice(fault_pool) if burst[0] < MAX_FAULT_BURST and faults.random() < FAULT_RATE else None
        burst[0] = burst[0] + 1 if fault else 0
        if fault in ("refused", "reset-before"):
            err = ConnectionRefusedError(111, "refused") if fault == "refused" else ConnectionResetError(104, "reset")
            raise urllib.error.URLError(err)
        auth = req.get_header("Authorization") or ""
        tok = auth[7:].strip() if auth[:7].lower() == "bearer " else None
        body = json.loads(req.data.decode("utf-8")) if isinstance(req.data, bytes) else None
        status, resp = game.handle(req.get_method(), req.full_url[len(WORLD_URL) :], tok, body, REMOTE)
        if fault == "reset-after":
            raise ConnectionResetError(104, "Connection reset")
        if fault == "timeout-after":
            raise TimeoutError("timed out")
        if fault == "disconnect-after":
            raise http.client.RemoteDisconnected("Remote disconnected")
        data = world.canonical_json(resp).encode("utf-8")
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, "err", http.client.HTTPMessage(), io.BytesIO(data))
        return _Reply(status, data)

    return urlopen


def play(instance: dict, seat_tokens: dict[str, str], policy: str = "oracle", faults: Random | None = None, fault_pool: tuple[str, ...] = FAULTS) -> tuple[list[dict], bytes]:
    assert policy in POLICIES and set(seat_tokens) == {"p1"}
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "events.jsonl"
        game = world.StaleHand(instance, {seat_tokens["p1"]: "p1"}, log_path)
        if policy != "nop":
            mod = policy_module(policy)
            gen = mod.run_policy(seat_tokens["p1"])
            urlopen = _faulty_open(game, faults, fault_pool) if faults is not None else None
            reply = None
            for _ in range(MAX_POLICY_STEPS):
                try:
                    req = gen.send(reply)
                except StopIteration:
                    gen = None
                    break
                body = json.loads(json.dumps(req["body"])) if req["method"] == "POST" else None
                reply = mod.send(WORLD_URL, req, urlopen, lambda _s: None) if urlopen is not None else game.handle(req["method"], req["path"], req["auth"], body, REMOTE)
            assert gen is None, "policy exceeded step limit"
        game.handle("POST", "/admin/close", None, {}, "127.0.0.1")
        return verify._load_strict_events(log_path), log_path.read_bytes()


def simulate(instance: dict, seat_tokens: dict[str, str], policy: str = "oracle", faults: Random | None = None, fault_pool: tuple[str, ...] = FAULTS) -> dict[str, float]:
    return verify.score(play(instance, seat_tokens, policy, faults, fault_pool)[0], instance)


def reference(config: dict) -> dict[str, dict[str, dict[str, float]]]:
    table: dict[str, dict[str, dict[str, float]]] = {}
    for cond in CONDITIONS:
        table[cond] = {}
        for pol in POLICIES:
            scores = [round(simulate(inst, tokens(config, inst), pol)["reward"], 6) for inst in instances(config, cond)]
            table[cond][pol] = {"mean": round(sum(scores) / len(scores), 6), "min": min(scores), "max": max(scores)}
    return table


def reference_toml(table: dict[str, dict[str, dict[str, float]]]) -> str:
    lines: list[str] = []
    for cond, pols in table.items():
        for pol, stats in pols.items():
            lines += [f"[reference.{cond}.{pol}]"] + [f"{k} = {json.dumps(stats[k])}" for k in ("mean", "min", "max")] + [""]
    return "\n".join(lines)


def check(config: dict) -> int:
    if config.get("reference") == reference(config):
        return 0
    print("family.toml [reference] differs: run reference.py --write", file=sys.stderr)
    return 1


def _assert_policy_row(cond: str, pol: str, results: list[dict[str, float]]) -> None:
    is_conf = cond == "conflicting-handoff"
    if pol == "oracle":
        wv = 1.0 if is_conf else 0.0
        assert all(r["reward"] == 1.0 and r["checked_state_pre_commit"] == 1.0 and r["acted_on_stale"] == 0.0 and r["verified_dependency"] == wv and r["conflict_resolved"] == wv for r in results)
    elif pol == "always_verify":
        wr, wc = (1.0, 1.0) if is_conf else (0.8, 0.0)
        assert all(r["reward"] == wr and r["verified_dependency"] == 1.0 and r["conflict_resolved"] == wc and r["checked_state_pre_commit"] == 1.0 for r in results)
    elif pol == "trust_handoff":
        wr, ws = (1.0, 0.0) if cond == "fresh-ctrl" else (0.0, 1.0)
        assert all(r["reward"] == wr and r["acted_on_stale"] == ws and r["missed_drift"] == 0.0 and r["missed_silent_supersede"] == 0.0 and r["checked_state_pre_commit"] == 0.0 for r in results)
    elif pol == "single_read_commit":
        ok_c = cond in ("fresh-ctrl", "stale-handoff")
        assert all(r["reward"] == (1.0 if ok_c else 0.0) and r["acted_on_stale"] == (0.0 if ok_c else 1.0) and r["missed_drift"] == (1.0 if cond == "midflight-drift" else 0.0) and r["missed_silent_supersede"] == (1.0 if cond == "silent-supersede" else 0.0) and r["checked_state_pre_commit"] == 0.0 for r in results)
    elif pol == "msg_reactive":
        ok_m, wc_m, wv_m = cond != "silent-supersede", (1.0 if cond in ("midflight-drift", "conflicting-handoff") else 0.0), (1.0 if is_conf else 0.0)
        assert all(r["reward"] == (1.0 if ok_m else 0.0) and r["acted_on_stale"] == (0.0 if ok_m else 1.0) and r["missed_drift"] == 0.0 and r["missed_silent_supersede"] == (0.0 if ok_m else 1.0) and r["checked_state_pre_commit"] == wc_m and r["verified_dependency"] == wv_m and r["conflict_resolved"] == wv_m for r in results)
    else:
        assert all(r["reward"] == 0.0 and r["acted_on_stale"] == 0.0 and r["missed_drift"] == 0.0 and r["missed_silent_supersede"] == 0.0 for r in results)


def _prove_matrix_and_leak(config: dict) -> int:
    passed = 0
    for cond in CONDITIONS:
        inst_list = instances(config, cond)
        for pol in POLICIES:
            results = [simulate(inst, tokens(config, inst), pol) for inst in inst_list]
            assert all(r["chain_ok"] == 1.0 and r["script_faithful"] == 1.0 for r in results)
            _assert_policy_row(cond, pol, results)
            passed += len(results)
    for f_inst, s_inst, c_inst in zip(instances(config, "fresh-ctrl"), instances(config, "silent-supersede"), instances(config, "conflicting-handoff"), strict=True):
        tok = tokens(config, f_inst)["p1"]
        with tempfile.TemporaryDirectory() as tmp:
            w_f, w_s, w_c = world.StaleHand(f_inst, {tok: "p1"}, Path(tmp) / "f.jsonl"), world.StaleHand(s_inst, {tok: "p1"}, Path(tmp) / "s.jsonl"), world.StaleHand(c_inst, {tok: "p1"}, Path(tmp) / "c0.jsonl")
            f_view = [w_f.handle("GET", p, tok, None, REMOTE) for p in ("/rules", "/state", "/msgs")]
            assert f_view == [w_s.handle("GET", p, tok, None, REMOTE) for p in ("/rules", "/state", "/msgs")] and f_view[:2] == [w_c.handle("GET", p, tok, None, REMOTE) for p in ("/rules", "/state")]
            assert all(k not in f_view[1][1] for k in ("conflict_unresolved", "pending_update", "reconciled"))
            assert w_f.handle("GET", "/state", tok, None, REMOTE) == w_c.handle("GET", "/state", tok, None, REMOTE) and w_s.handle("GET", "/state", tok, None, REMOTE)[1]["state_version"] == 2
        views = []
        for use_peer in (False, True):
            alt_t = c_inst["drift_state"]["active_target"] if use_peer else c_inst["handoff"]["handoff_target"]
            alt_p = c_inst["drift_state"]["active_param"] if use_peer else c_inst["handoff"]["handoff_param"]
            with tempfile.TemporaryDirectory() as tmp:
                srv = world.StaleHand({**c_inst, "authoritative_resolution": {"version": 2, "target": alt_t, "param": alt_p}}, {tok: "p1"}, Path(tmp) / "cw.jsonl")
                views.append([srv.handle("GET", p, tok, None, REMOTE) for p in ("/rules", "/state", "/msgs", "/state")])
        assert views[0] == views[1]
        passed += 2
    return passed


def _run_custom_steps(inst: dict, tok: str, steps: list[tuple[str, str, str | None, object]]) -> tuple[list[dict], dict[str, float]]:
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "custom.jsonl"
        srv = world.StaleHand(inst, {tok: "p1"}, log_path)
        for method, path, auth, body in steps:
            srv.handle(method, path, auth, body, REMOTE)
        srv.handle("POST", "/admin/close", None, {}, "127.0.0.1")
        evs = verify._load_strict_events(log_path)
        return evs, verify.score(evs, inst)


def _prove_traps_and_state_machine(config: dict) -> int:
    passed = 0
    for cond in ("midflight-drift", "conflicting-handoff"):
        for inst in instances(config, cond):
            tok, cid, auth = tokens(config, inst)["p1"], inst["check_id"], inst["authoritative_resolution"]
            ex_b = {"target": auth["target"], "param": auth["param"], "version": auth["version"]}
            _, blind1 = _run_custom_steps(inst, tok, [("GET", "/state", tok, None), ("GET", "/state", tok, None), ("POST", "/execute", tok, ex_b)])
            _, blind2 = _run_custom_steps(inst, tok, [("GET", "/state", tok, None), ("POST", "/verify_dependency", tok, {"check_id": cid}), ("GET", "/state", tok, None), ("POST", "/execute", tok, ex_b)])
            assert blind1["reward"] == 0.0 and blind2["reward"] == 0.0 and blind1["script_faithful"] == 1.0 and blind2["script_faithful"] == 1.0
            passed += 2
    for c_inst in instances(config, "conflicting-handoff"):
        tok_c, cid_c, auth_c = tokens(config, c_inst)["p1"], c_inst["check_id"], c_inst["authoritative_resolution"]
        ex_c = {"target": auth_c["target"], "param": auth_c["param"], "version": auth_c["version"]}
        evs_z, zero1 = _run_custom_steps(c_inst, tok_c, [("POST", "/verify_dependency", tok_c, {"check_id": cid_c}), ("POST", "/execute", tok_c, ex_c)])
        _, zero2 = _run_custom_steps(c_inst, tok_c, [("GET", "/msgs", tok_c, None), ("POST", "/verify_dependency", tok_c, {"check_id": cid_c}), ("POST", "/execute", tok_c, ex_c)])
        v_resp = next(ev["response"] for ev in evs_z if ev["kind"] == "verify_dependency")
        assert all(k not in v_resp for k in ("authoritative_target", "authoritative_param", "authoritative_version"))
        assert zero1["reward"] == 0.0 and zero1["checked_state_pre_commit"] == 0.0 and zero2["reward"] == 0.0 and zero2["checked_state_pre_commit"] == 0.0
        passed += 2
    ss = instances(config, "silent-supersede")[0]
    tok_s, auth_s = tokens(config, ss)["p1"], ss["authoritative_resolution"]
    _, dbl_ss = _run_custom_steps(ss, tok_s, [("GET", "/state", tok_s, None), ("GET", "/state", tok_s, None), ("GET", "/msgs", tok_s, None), ("POST", "/execute", tok_s, {"target": auth_s["target"], "param": auth_s["param"], "version": auth_s["version"]})])
    assert dbl_ss["reward"] == 1.0 and dbl_ss["checked_state_pre_commit"] == 1.0
    for sc_cond in ("fresh-ctrl", "stale-handoff"):
        sc_i = instances(config, sc_cond)[0]
        tok_i, auth_i = tokens(config, sc_i)["p1"], sc_i["authoritative_resolution"]
        _, reord_1 = _run_custom_steps(sc_i, tok_i, [("GET", "/msgs", tok_i, None), ("GET", "/state", tok_i, None), ("POST", "/execute", tok_i, {"target": auth_i["target"], "param": auth_i["param"], "version": auth_i["version"]})])
        assert reord_1["reward"] == 1.0 and reord_1["checked_state_pre_commit"] == 0.0
    md0 = instances(config, "midflight-drift")[0]
    tok_m, auth_m = tokens(config, md0)["p1"], md0["authoritative_resolution"]
    ex_m = {"target": auth_m["target"], "param": auth_m["param"], "version": auth_m["version"]}
    _, m_re1 = _run_custom_steps(md0, tok_m, [("GET", "/msgs", tok_m, None), ("GET", "/state", tok_m, None), ("GET", "/msgs", tok_m, None), ("GET", "/state", tok_m, None), ("POST", "/execute", tok_m, ex_m)])
    _, m_re2 = _run_custom_steps(md0, tok_m, [("GET", "/msgs", tok_m, None), ("GET", "/state", tok_m, None), ("GET", "/state", tok_m, None), ("POST", "/execute", tok_m, ex_m)])
    _, idem = _run_custom_steps(md0, tok_m, [("GET", "/state?read=r1", tok_m, None), ("GET", "/state?read=r1", tok_m, None), ("GET", "/state?read=bad!", tok_m, None), ("GET", "/msgs", tok_m, None), ("POST", "/execute", tok_m, {"target": md0["handoff"]["handoff_target"], "param": md0["handoff"]["handoff_param"], "version": 1})])
    assert m_re1["reward"] == 1.0 and m_re2["reward"] == 1.0 and idem["reward"] == 0.0 and idem["checked_state_pre_commit"] == 0.0 and idem["script_faithful"] == 1.0
    return passed + 6


def _prove_edges_and_rechain(config: dict) -> int:
    passed = _prove_traps_and_state_machine(config)
    md = instances(config, "midflight-drift")[0]
    tok_m, cid_m = tokens(config, md)["p1"], md["check_id"]
    for steps in ([("GET", "/rules", "bad", None)], [("DELETE", "/state", tok_m, None)], [("GET", "/rules", tok_m, None)], [("GET", "/state", tok_m, None)], [("GET", "/state", tok_m, None), ("GET", "/msgs", tok_m, None)], [("POST", "/verify_dependency", tok_m, {"check_id": cid_m})]):
        evs_p, res_p = _run_custom_steps(md, tok_m, steps)
        assert res_p["chain_ok"] == 1.0 and res_p["script_faithful"] == 1.0 and res_p["reward"] == 0.0 and all(ev["request"] is None for ev in evs_p if not ev["ok"])
        passed += 1
    for c_inst in instances(config, "conflicting-handoff"):
        tok_c = tokens(config, c_inst)["p1"]
        for cand_t, cand_p in ((c_inst["handoff"]["handoff_target"], c_inst["handoff"]["handoff_param"]), (c_inst["drift_state"]["active_target"], c_inst["drift_state"]["active_param"])):
            _, g_res = _run_custom_steps(c_inst, tok_c, [("GET", "/state", tok_c, None), ("POST", "/execute", tok_c, {"target": cand_t, "param": cand_p, "version": 2})])
            assert g_res["reward"] == 0.0 and g_res["conflict_resolved"] == 0.0 and g_res["acted_on_stale"] == 1.0
            passed += 1
    evs_v, _ = _run_custom_steps(md, tok_m, [("GET", "/state", tok_m, None), ("POST", "/verify_dependency", tok_m, {"check_id": cid_m})])
    bad_seed, bad_msg, bad_ver = (json.loads(json.dumps(evs_v)) for _ in range(3))
    bad_seed[0]["response"]["initial_version"], bad_msg[1]["response"]["text"] = 2, "forged peer note"
    bad_ver[next(i for i, e in enumerate(bad_ver) if e["kind"] == "verify_dependency")]["response"]["fee_applied"] = False
    for cand in (bad_seed, bad_msg, bad_ver):
        prev = world.GENESIS_HASH
        for ev in cand:
            ev["prev_hash"] = prev
            ev["entry_hash"] = prev = world.compute_entry_hash(ev)
        tampered = verify.score(cand, md)
        assert tampered["chain_ok"] == 1.0 and tampered["script_faithful"] == 0.0 and tampered["reward"] == 0.0
        passed += 1
    return passed


def _prove_faults_and_guards(config: dict) -> int:
    passed = 0
    for idx, cond in enumerate(CONDITIONS):
        for inst in instances(config, cond):
            toks = tokens(config, inst)
            for p_idx, pol in enumerate(("oracle", "trust_handoff", "single_read_commit", "msg_reactive", "always_verify")):
                clean, faulty = simulate(inst, toks, pol), simulate(inst, toks, pol, Random(20261124 + p_idx * 1000 + idx * 100 + inst["instance"]), FAULTS)
                assert faulty["reward"] == clean["reward"] and faulty["chain_ok"] == 1.0 and faulty["script_faithful"] == 1.0
                passed += 1
    sample = instances(config, "midflight-drift")[0]
    tok = tokens(config, sample)["p1"]
    with tempfile.TemporaryDirectory() as tmp:
        sat_log, g_log = Path(tmp) / "sat.jsonl", Path(tmp) / "g.jsonl"
        sat = world.StaleHand(sample, {tok: "p1"}, sat_log)
        assert sat.handle("POST", "/msg", tok, {"text": "x" * 501}, REMOTE)[0] == 400
        codes_m = [sat.handle("POST", "/msg", tok, {"text": f"m{i}"}, REMOTE)[0] for i in range(20)]
        assert codes_m.count(200) == world.MSG_BUDGET and codes_m.count(429) == 4
        assert sat.handle("GET", "/state", tok, None, REMOTE)[0] == 200 and sat.handle("GET", "/msgs", tok, None, REMOTE)[0] == 200
        s2 = sat.handle("GET", "/state", tok, None, REMOTE)[1]
        assert sat.handle("POST", "/execute", tok, {"target": s2["active_target"], "param": s2["active_param"], "version": s2["state_version"]}, REMOTE)[0] == 200
        sat.handle("POST", "/admin/close", None, {}, "127.0.0.1")
        sat_res = verify.score(verify._load_strict_events(sat_log), sample)
        assert sat_res["reward"] == 1.0 and sat_res["script_faithful"] == 1.0
        srv = world.StaleHand(sample, {tok: "p1"}, g_log)
        deep: object = "x"
        for _ in range(31):
            deep = {"a": deep}
        for bad_txt in ("bad\u2028", "bad\u2029", "bad\x85", "bad\x07", "bad\x7f", "bad\ud800"):
            assert srv.handle("POST", "/msg", tok, {"text": bad_txt}, REMOTE)[0] == 400
        for bad_p in ("/bad\x00path", "/bad\x85path", "/bad\ud800path", "/" + "a" * 200):
            assert srv.handle("GET", bad_p, None, None, REMOTE)[0] == 401 and srv.handle("GET", bad_p, tok, None, REMOTE)[0] == 404
        assert srv.handle("POST", "/msg", tok, deep, REMOTE)[0] == 400
        for bad_v in (float("nan"), float("inf")):
            assert srv.handle("POST", "/execute", tok, {"target": "t", "param": 1, "version": bad_v}, REMOTE)[0] == 400
        assert srv.handle("POST", "/execute", tok, {"target": "t", "param": 10**61, "version": 1}, REMOTE)[0] == 400
        st = 200
        for _ in range(world.MAX_REQUESTS + 5):
            st, _ = srv.handle("GET", "/rules", tok, None, REMOTE)
        assert st == 429 and verify.score(verify._load_strict_events(g_log), sample)["script_faithful"] == 1.0
        for raw_bad in (("{" + ",".join(f'"{i}":0' for i in range(4097)) + "}").encode("utf-8"), b"[" * 31 + b"]" * 31, b'{"a":1,"a":2}'):
            assert world._read_post_body(SimpleNamespace(headers={"Content-Length": str(len(raw_bad))}, rfile=io.BytesIO(raw_bad))) is world._BAD_JSON
            bad_f = Path(tmp) / "bad.jsonl"
            bad_f.write_bytes(raw_bad + b"\n")
            raised = False
            try:
                verify._load_strict_events(bad_f)
            except ValueError:
                raised = True
            assert raised
    return passed + 12


def _run_one_race(sample: dict, tok: str, log_path: Path) -> None:
    srv = world.StaleHand(sample, {tok: "p1"}, log_path)
    srv.handle("GET", "/state", tok, None, REMOTE)
    s2 = srv.handle("GET", "/state", tok, None, REMOTE)[1]
    barrier, codes, lock = threading.Barrier(4), [], threading.Lock()
    body = {"target": s2["active_target"], "param": s2["active_param"], "version": s2["state_version"]}

    def worker(idx: int) -> None:
        barrier.wait()
        st_w, _ = srv.handle("POST", "/verify_dependency", tok, {"check_id": sample["check_id"]}, REMOTE) if idx == 3 else srv.handle("POST", "/execute", tok, body, REMOTE)
        with lock:
            codes.append((idx, st_w))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    srv.handle("POST", "/admin/close", None, {}, "127.0.0.1")
    assert srv.handle("GET", "/rules", tok, None, REMOTE)[0] == 409
    exec_codes = [st_w for idx, st_w in codes if idx < 3]
    assert exec_codes.count(200) == 1 and set(exec_codes) <= {200, 409, 429}
    evs = verify._load_strict_events(log_path)
    assert verify._events_chain_ok(evs) and verify.score(evs, sample)["script_faithful"] == 1.0


def _prove_tamper_race_and_live(config: dict) -> int:
    passed = 0
    sample = instances(config, "conflicting-handoff")[0]
    toks = tokens(config, sample)
    events, raw = play(sample, toks, "oracle")
    assert verify.score(events, sample)["reward"] == 1.0
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "flip.jsonl"
        for pos in range(len(raw)):
            flipped = bytearray(raw)
            flipped[pos] ^= 0x01
            p.write_bytes(bytes(flipped))
            ok = True
            try:
                res = verify.score(verify._load_strict_events(p), sample)
                ok = res["chain_ok"] == 1.0 and res["reward"] == 1.0
            except (UnicodeDecodeError, ValueError):
                ok = False
            assert not ok, f"tamper at byte {pos} undetected"
            passed += 1
        for seed_idx in range(10):
            _run_one_race(sample, toks["p1"], Path(tmp) / f"r{seed_idx}.jsonl")
            passed += 1
    for cond, pref in (("midflight-drift", "bearer "), ("silent-supersede", "BEARER "), ("conflicting-handoff", "BeArEr   ")):
        inst = instances(config, cond)[0]
        tok = tokens(config, inst)["p1"]
        for pol in POLICIES:
            assert _run_live_policy(inst, tok, pol, pref) == simulate(inst, {"p1": tok}, pol)
            passed += 1
    return passed


def _run_live_policy(inst: dict, tok: str, pol: str, bearer_prefix: str = "Bearer ") -> dict[str, float]:
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "live.jsonl"
        srv = world.StaleHand(inst, {tok: "p1"}, log_path)

        class LiveHandler(http.server.BaseHTTPRequestHandler):
            def _dispatch_req(self, method: str) -> None:
                body_h = world._read_post_body(self) if method == "POST" else None
                auth_h = self.headers.get("Authorization") or ""
                tok_h = auth_h[7:].strip() if auth_h[:7].lower() == "bearer " else None
                ip = "127.0.0.1" if self.headers.get("X-Test-Loopback") == "1" else REMOTE
                st_h, resp_h = srv.handle(method, self.path, tok_h, body_h, ip)
                payload = world.canonical_json(resp_h).encode("utf-8")
                self.send_response(st_h)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:
                self._dispatch_req("GET")

            def do_POST(self) -> None:
                self._dispatch_req("POST")

            def log_message(self, *_args: object) -> None:
                pass

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), LiveHandler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/rules", headers={"Authorization": f"{bearer_prefix}{tok}"}, method="GET"), timeout=5) as rrep:
                assert rrep.status == 200
            env = {**os.environ, "WORLD": f"http://127.0.0.1:{port}", "SEAT_TOKEN": tok, "PYTHONDONTWRITEBYTECODE": "1"}
            proc = subprocess.run(["bash", str(TEMPLATE / "policies" / f"{pol}.sh")], env=env, capture_output=True, text=True, timeout=20, check=False)
            assert proc.returncode == 0, proc.stderr
            with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/admin/close", data=b"{}", headers={"X-Test-Loopback": "1", "Content-Type": "application/json"}, method="POST"), timeout=5) as crep:
                crep.read()
        finally:
            httpd.shutdown()
            httpd.server_close()
        return verify.score(verify._load_strict_events(log_path), inst)


def _write_trial_dir(tdir: Path, task_path: Path, pol: str, raw: bytes, rews: dict[str, float]) -> None:
    for sub in ("verifier", "artifacts/logs/world", "seats/p1/agent"):
        (tdir / sub).mkdir(parents=True, exist_ok=True)
    (tdir / "verifier" / "events.jsonl").write_bytes(raw)
    (tdir / "artifacts" / "logs" / "world" / "events.jsonl").write_bytes(raw)
    (tdir / "verifier" / "reward.json").write_text(json.dumps(rews) + "\n", encoding="utf-8")
    part = {"seat": "p1", "name": pol} if pol in ("oracle", "nop") else {"seat": "p1", "import_path": "muster.agents.script:ScriptAgent", "kwargs": {"script": f"/policies/{pol}.sh"}}
    res_doc = {"trial_name": tdir.name, "task_name": f"muster/{task_path.name}", "config": {"task": {"path": str(task_path)}, "participants": [part]}, "seat_results": [{"seat": "p1", "under_test": True, "agent_info": {"name": pol}, "exit_code": 0}], "verifier_result": {"rewards": rews}, "reward_valid": True}
    (tdir / "result.json").write_text(json.dumps(res_doc) + "\n", encoding="utf-8")


def _prove_muster_and_worldwatch(config: dict) -> int:
    passed, repo_root = 0, str(FAMILY.parents[2])
    added = repo_root not in sys.path
    if added:
        sys.path.insert(0, repo_root)
    try:
        from muster import attribution, load, regrade
        from muster import events as mevents
        from worldwatch import _chain, baselines, detectors, validity
    finally:
        if added and repo_root in sys.path:
            sys.path.remove(repo_root)
    cert_rep, cert_rc = baselines.run_certify_baselines(FAMILY)
    assert cert_rc == 0 and cert_rep["passed"] is True
    build_dir = FAMILY / "build"
    if not (build_dir / "conflicting-handoff" / "stalehand_v1-i01" / "task.toml").is_file():
        return passed + 1
    assert not [p for p in build_dir.rglob("*") if p.name == "__pycache__" or p.suffix in (".pyc", ".pyo")] and len(list(build_dir.rglob(".dockerignore"))) == 100
    build_task = build_dir / "conflicting-handoff" / "stalehand_v1-i01"
    score_fn, loaded_inst, err = regrade.load_score(build_task)
    assert err is None and score_fn is not None and loaded_inst is not None
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for cond in CONDITIONS:
            for inst in instances(config, cond):
                evs_o, raw_o = play(inst, tokens(config, inst), "oracle")
                odir = root / f"ww_{cond}_{inst['instance']:02d}"
                _write_trial_dir(odir, build_dir / cond / f"stalehand_v1-i{inst['instance']:02d}", "oracle", raw_o, verify.score(evs_o, inst))
                det, val = detectors.analyze_trial_detectors(odir, "olam"), validity.classify_trial_validity(odir, "olam")
                assert det["triggered"] == [] and det["chain_ok"] is True and val["measured"] is True
                passed += 1
        evs_th, raw_th = play(loaded_inst, tokens(config, loaded_inst), "trust_handoff")
        _write_trial_dir(root / "ww_blind", build_task, "trust_handoff", raw_th, verify.score(evs_th, loaded_inst))
        assert "message_neglect_decay" in detectors.analyze_trial_detectors(root / "ww_blind", "olam")["triggered"]
        mroot, toks = root / "muster_suite", tokens(config, loaded_inst)
        for pol in ("oracle", "nop", "spoof"):
            evs, raw = play(loaded_inst, toks, pol)
            rews = verify.score(evs, loaded_inst)
            _write_trial_dir(mroot / f"trial_{pol}", build_task, pol, raw, rews)
            reg = regrade.regrade_trial(rews, evs, evs, score_fn, loaded_inst)
            assert reg["status"] == "match" and not reg["differences"] and mevents.summarize(evs, seats=["p1"])["unauthorized"] == (18 if pol == "spoof" else 0)
        attr = attribution.analyze(load.walk(mroot), oracle_reward=1.0, exploits=["spoof"])
        assert attr["errors"] == 0 and attr["warnings"] == 0
        s5_log = root / "s5.jsonl"
        s5 = world.StaleHand(loaded_inst, {toks["p1"]: "p1"}, s5_log)
        for _ in range(200):
            assert s5.handle("POST", "/execute", toks["p1"], {"extra": "x" * 20_000}, REMOTE)[0] == 400
        assert s5.handle("POST", "/msg", toks["p1"], {"text": "café — 検証 🚀"}, REMOTE)[0] == 200
        s5.log_bytes = world.MAX_WORLD_LOG_BYTES
        assert s5.handle("GET", "/rules", toks["p1"], None, REMOTE)[0] == 429 and s5.handle("GET", "/rules", toks["p1"], None, REMOTE)[0] == 429
        assert s5.handle("POST", "/admin/close", None, {}, "127.0.0.1")[0] == 200
        s5_evs = verify._load_strict_events(s5_log)
        assert all(ev["request"] is None for ev in s5_evs if not ev["ok"]) and _chain.read_jsonl_static(s5_log, require_seq=True)[1:] == (True, None)
        assert regrade.regrade_trial(verify.score(s5_evs, loaded_inst), s5_evs, s5_evs, score_fn, loaded_inst)["status"] == "match"
    return passed + 9


def prove(config: dict) -> int:
    total = _prove_matrix_and_leak(config) + _prove_edges_and_rechain(config)
    total += _prove_faults_and_guards(config) + _prove_tamper_race_and_live(config) + _prove_muster_and_worldwatch(config)
    print(f"stalehand_v1 proofs: passed={total}, failed=0")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    for flag in ("--write", "--check", "--prove"):
        parser.add_argument(flag, action="store_true")
    args, config = parser.parse_args(argv), family()
    if args.write:
        path = FAMILY / "family.toml"
        path.write_text(path.read_text(encoding="utf-8").split("[reference.")[0].rstrip("\n") + "\n\n" + reference_toml(reference(config)), encoding="utf-8")
        return 0
    if not args.check and not args.prove:
        print(reference_toml(reference(config)), end="")
        return 0
    status = check(config) if args.check else 0
    return max(status, prove(config)) if (args.prove and status == 0) else status


if __name__ == "__main__":
    sys.exit(main())
