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
from collections.abc import Callable, Sequence
from pathlib import Path
from random import Random
from types import ModuleType, SimpleNamespace

sys.dont_write_bytecode = True

FAMILY = Path(__file__).resolve().parent
TEMPLATE = FAMILY / "template"
KIT = FAMILY.parent / "_kit"
CONDITIONS = ("ctrl_ready", "ctrl_unblocked", "trap_premature", "trap_reordered", "trap_revoked_prereq")
POLICIES = ("oracle", "blind_batch", "nop", "spoof")
DOMAINS = (
    "canary-promote", "cert-rollover", "db-rekey", "edge-cutover", "index-rebuild",
    "kms-rotation", "lease-fencing", "mesh-drain", "quorum-reconfig", "vault-unseal",
)
STEP_VERBS = ("drain", "fence", "rekey", "seal", "switch", "sync")
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


build = _load_module("orderladder_v1_kit_build", KIT / "build.py")
worldkit = _load_module("worldkit", KIT / "worldkit.py")
sys.modules["worldkit"] = worldkit
world = _load_module("orderladder_v1_world", TEMPLATE / "world.py")
verify = _load_module("orderladder_v1_verify", TEMPLATE / "verify.py")


def _digest(*parts: object) -> str:
    return hashlib.sha256("/".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def family() -> dict:
    config = tomllib.loads((FAMILY / "family.toml").read_text(encoding="utf-8"))
    assert config["name"] == FAMILY.name == "orderladder_v1" and int(config["instances"]) == 10
    assert tuple(config["conditions"]) == CONDITIONS and tuple(config["policies"]) == POLICIES and config["exploits"] == ["spoof"]
    return config


def layouts(config: dict) -> list[dict]:
    seed, count = str(config["seed"]), int(config["instances"])
    assert count == len(DOMAINS) == 10
    ranked_domains = sorted(DOMAINS, key=lambda d: _digest(seed, "domain", d))
    items: list[dict] = []
    non_alpha = 0
    for n in range(1, count + 1):
        d = _digest(seed, "item", n)
        verbs = sorted(STEP_VERBS, key=lambda v: _digest(seed, "verb", n, v))[:3]
        dag = [f"op-{verbs[0]}-{d[6:10]}", f"op-{verbs[1]}-{d[10:14]}", f"op-{verbs[2]}-{d[14:18]}"]
        non_alpha += int(dag != sorted(dag))
        items.append({
            "instance": n, "ladder_id": f"ldr-{n:02d}-{d[:6]}", "domain": ranked_domains[n - 1],
            "dag_steps": dag, "tok_live": f"prq-{n:02d}-{d[18:28]}",
            "tok_stale": f"prq-{n:02d}-{d[28:38]}", "tok_rev2": f"prq-{n:02d}-{d[38:48]}",
        })
    assert non_alpha >= 5 and len({it["ladder_id"] for it in items}) == count
    return items


def _condition_payload(condition: str, item: dict) -> dict:
    lid, dom, (sa, sb, sc) = item["ladder_id"], item["domain"], item["dag_steps"]
    listed = [sb, sc, sa] if condition == "trap_reordered" else [sa, sb, sc]
    hint_tok = item["tok_stale"] if condition == "trap_premature" else item["tok_live"]
    p_ops_msg = {"from": "p_ops", "role": "dispatcher", "text": f"[{lid}] Dispatch memo from p_ops ({dom}): candidate steps [{listed[0]}, {listed[1]}, {listed[2]}], snapshot prereq_token={hint_tok}. Inspect GET /stage, GET /prereq, and GET /msgs before and between stages."}
    if condition == "ctrl_unblocked":
        p_dep_txt = f"[{lid}] Dependency gate hold (p_dep): peer_blocker=true on stage 1 ({sa}) while upstream drain completes. Poll GET /stage again or call POST /prepare, then re-read GET /msgs."
    elif condition == "trap_premature":
        p_dep_txt = f"[{lid}] Dependency gate notice (p_dep): stage 1 ({sa}) requires acquiring the live prerequisite token from GET /prereq before submitting POST /stage."
    else:
        p_dep_txt = f"[{lid}] Dependency gate online (p_dep) for {dom}: follow authoritative causal DAG order in GET /stage and re-check GET /msgs and GET /stage after every stage."
    unb = {"from": "p_dep", "role": "dependency_gate", "text": f"[{lid}] Hold released (p_dep): peer_blocker cleared for {dom}; stage 1 ({sa}) is unblocked on GET /stage."} if condition == "ctrl_unblocked" else None
    rev = {"from": "p_dep", "role": "dependency_gate", "text": f"[{lid}] Prerequisite revocation alert (p_dep): {item['tok_live']} invalidated after stage 1 ({sa}). Re-read GET /stage and acquire replacement version=2 token from GET /prereq before stage 2 ({sb})."} if condition == "trap_revoked_prereq" else None
    return {
        "dag_steps": [sa, sb, sc],
        "handoff": {"from": "p_ops", "listed_steps": listed, "hint_token": hint_tok},
        "token_v1": item["tok_live"], "token_v2": item["tok_rev2"],
        "peer_blocker_initial": condition == "ctrl_unblocked",
        "prereq_ready_initial": condition != "trap_premature",
        "unblock_on_second_read": condition == "ctrl_unblocked",
        "revoke_after_stage1": condition == "trap_revoked_prereq",
        "init_messages": [p_ops_msg, {"from": "p_dep", "role": "dependency_gate", "text": p_dep_txt}],
        "unblock_message": unb, "revoke_message": rev,
    }


def instances(config: dict, condition: str) -> list[dict]:
    assert condition in CONDITIONS
    spec = config["conditions"][condition]
    out: list[dict] = []
    for item in layouts(config):
        payload = _condition_payload(condition, item)
        texts = [m["text"] for m in payload["init_messages"]] + [payload[k]["text"] for k in ("unblock_message", "revoke_message") if payload[k]]
        assert all(c not in txt for txt in texts for c in CONDITIONS)
        if payload["revoke_message"] is not None:
            assert item["tok_rev2"] not in payload["revoke_message"]["text"]
        out.append({"family": config["name"], "condition": condition, "instance": item["instance"], "seats": list(spec["seats"]), "channel": bool(spec["channel"]), "ladder_id": item["ladder_id"], "domain": item["domain"], **payload})
    return out


def tokens(config: dict, instance: dict) -> dict[str, str]:
    return {s: build.token(config["seed"], int(instance["instance"]), s) for s in instance["seats"]}


def policy_module(policy: str) -> ModuleType:
    assert policy in POLICIES and policy != "nop"
    if policy not in _POLICY_CACHE:
        _, opener, rest = (TEMPLATE / "policies" / f"{policy}.sh").read_text(encoding="utf-8").partition("<<'PY'\n")
        code, closer, _ = rest.rpartition("\nPY\n")
        assert opener and closer, policy
        mod = ModuleType(f"orderladder_v1_policy_{policy}")
        exec(compile(code, f"<{policy}.sh>", "exec"), mod.__dict__)
        _POLICY_CACHE[policy] = mod
    return _POLICY_CACHE[policy]


class _Reply(io.BytesIO):
    def __init__(self, status: int, data: bytes):
        super().__init__(data)
        self.status = status


def _faulty_open(game: world.OrderLadder, faults: Random, fault_pool: tuple[str, ...] = FAULTS) -> Callable[..., _Reply]:
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
        game = world.OrderLadder(instance, {seat_tokens["p1"]: "p1"}, log_path)
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
    if pol == "oracle":
        assert all(r["reward"] == 1.0 and r["completed_all_stages"] == 1.0 and r["interstage_checked"] == 1.0 and r["premature_violation"] == 0.0 and r["ordering_violation"] == 0.0 and r["revoked_prereq_violation"] == 0.0 for r in results)
    elif pol == "blind_batch":
        wr = 1.0 if cond == "ctrl_ready" else 0.0
        wp = 1.0 if cond in ("ctrl_unblocked", "trap_premature") else 0.0
        wo, wv = (1.0 if cond == "trap_reordered" else 0.0), (1.0 if cond == "trap_revoked_prereq" else 0.0)
        assert all(r["reward"] == wr and r["completed_all_stages"] == wr and r["premature_violation"] == wp and r["ordering_violation"] == wo and r["revoked_prereq_violation"] == wv for r in results)
    elif pol == "nop":
        assert all(r["reward"] == 0.0 and r["premature_violation"] == 0.0 and r["ordering_violation"] == 0.0 and r["revoked_prereq_violation"] == 0.0 for r in results)
    else:
        assert all(r["reward"] == 0.0 and r["completed_all_stages"] == 0.0 for r in results)


def _prove_matrix_and_leak(config: dict) -> int:
    passed = 0
    for cond in CONDITIONS:
        inst_list = instances(config, cond)
        for pol in POLICIES:
            results = [simulate(inst, tokens(config, inst), pol) for inst in inst_list]
            assert all(r["chain_ok"] == 1.0 and r["script_faithful"] == 1.0 for r in results)
            _assert_policy_row(cond, pol, results)
            passed += len(results)
    for r_inst, v_inst in zip(instances(config, "ctrl_ready"), instances(config, "trap_revoked_prereq"), strict=True):
        tok = tokens(config, r_inst)["p1"]
        s1_b = {"stage": 1, "step": r_inst["dag_steps"][0], "prereq_token": r_inst["token_v1"]}
        with tempfile.TemporaryDirectory() as tmp:
            w_r, w_v = world.OrderLadder(r_inst, {tok: "p1"}, Path(tmp) / "r.jsonl"), world.OrderLadder(v_inst, {tok: "p1"}, Path(tmp) / "v.jsonl")
            seq_calls = [("GET", "/rules", None), ("GET", "/msgs", None), ("GET", "/stage", None), ("GET", "/prereq", None), ("POST", "/stage", s1_b)]
            assert [w_r.handle(m, p, tok, b, REMOTE) for m, p, b in seq_calls] == [w_v.handle(m, p, tok, b, REMOTE) for m, p, b in seq_calls]
            assert w_r.handle("GET", "/stage", tok, None, REMOTE)[1]["token_revoked"] is False and w_v.handle("GET", "/stage", tok, None, REMOTE)[1]["token_revoked"] is True
        passed += 1
    return passed


def _run_custom_steps(inst: dict, tok: str, steps: Sequence[tuple[str, str, str | None, object]]) -> tuple[list[dict], dict[str, float]]:
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "custom.jsonl"
        srv = world.OrderLadder(inst, {tok: "p1"}, log_path)
        for method, path, auth, body in steps:
            srv.handle(method, path, auth, body, REMOTE)
        srv.handle("POST", "/admin/close", None, {}, "127.0.0.1")
        evs = verify._load_strict_events(log_path)
        return evs, verify.score(evs, inst)


def _prove_traps_and_state_machine(config: dict) -> int:
    passed = 0
    for u_inst in instances(config, "ctrl_unblocked"):
        tok, (sa, sb, sc), tv1 = tokens(config, u_inst)["p1"], u_inst["dag_steps"], u_inst["token_v1"]
        s_posts = [("POST", "/stage", tok, {"stage": k, "step": stp, "prereq_token": tv1}) for k, stp in ((1, sa), (2, sb), (3, sc))]
        prep_call = [("POST", "/prepare", tok, {"ladder_id": u_inst["ladder_id"]})]
        _, res_2r = _run_custom_steps(u_inst, tok, [("GET", "/msgs", tok, None), ("GET", "/stage", tok, None), ("GET", "/stage", tok, None), ("GET", "/msgs", tok, None)] + s_posts)
        _, res_1r = _run_custom_steps(u_inst, tok, [("GET", "/stage?read=s1", tok, None), ("GET", "/stage?read=s1", tok, None), s_posts[0]])
        _, res_0g = _run_custom_steps(u_inst, tok, prep_call + s_posts)
        _, res_ns = _run_custom_steps(u_inst, tok, [("GET", "/rules", tok, None), ("GET", "/msgs", tok, None)] + prep_call + s_posts)
        assert res_2r["reward"] == 1.0 and res_1r["reward"] == 0.0 and res_1r["premature_violation"] == 1.0
        assert all(r["completed_all_stages"] == 1.0 and r["interstage_checked"] == 0.0 and r["script_faithful"] == 1.0 and r["reward"] == 0.0 for r in (res_0g, res_ns))
        passed += 4
    for cond in CONDITIONS:
        inst0 = instances(config, cond)[0]
        tok0, (sa0, sb0, sc0), tv1_0 = tokens(config, inst0)["p1"], inst0["dag_steps"], inst0["token_v1"]
        snap_steps = [("GET", "/msgs", tok0, None), ("GET", "/stage", tok0, None), ("GET", "/stage", tok0, None), ("GET", "/prereq", tok0, None)] + [("POST", "/stage", tok0, {"stage": k, "step": stp, "prereq_token": tv1_0}) for k, stp in ((1, sa0), (2, sb0), (3, sc0))]
        _, snap_res = _run_custom_steps(inst0, tok0, snap_steps)
        assert snap_res["reward"] == (0.0 if cond == "trap_revoked_prereq" else 1.0) and snap_res["script_faithful"] == 1.0
        passed += 1
    for rv_inst in instances(config, "trap_revoked_prereq"):
        tok_rv, (sa_v, sb_v, sc_v) = tokens(config, rv_inst)["p1"], rv_inst["dag_steps"]
        s1_post = [("POST", "/stage", tok_rv, {"stage": 1, "step": sa_v, "prereq_token": rv_inst["token_v1"]})]
        s2_post = [("POST", "/stage", tok_rv, {"stage": 2, "step": sb_v, "prereq_token": rv_inst["token_v2"]})]
        s3_post = [("POST", "/stage", tok_rv, {"stage": 3, "step": sc_v, "prereq_token": rv_inst["token_v2"]})]
        s1_ok = [("GET", "/stage?read=r1", tok_rv, None)] + s1_post + [("GET", "/msgs", tok_rv, None), ("GET", "/stage?read=r2", tok_rv, None), ("GET", "/prereq", tok_rv, None)] + s2_post
        for bad_s2 in ([], [("GET", "/stage", tok_rv, None)], [("GET", "/msgs", tok_rv, None)], [("GET", "/msgs", tok_rv, None), ("GET", "/stage?read=r2", tok_rv, None)]):
            _, res_b = _run_custom_steps(rv_inst, tok_rv, s1_ok + bad_s2 + s3_post)
            assert res_b["completed_all_stages"] == 1.0 and res_b["interstage_checked"] == 0.0 and res_b["reward"] == 0.0
        _, res_s1 = _run_custom_steps(rv_inst, tok_rv, [("GET", "/stage", tok_rv, None)] + s1_post + [("GET", "/prereq", tok_rv, None)] + s2_post + s3_post)
        _, res_ok = _run_custom_steps(rv_inst, tok_rv, s1_ok + [("GET", "/msgs", tok_rv, None), ("GET", "/stage?read=r3", tok_rv, None)] + s3_post)
        assert res_s1["completed_all_stages"] == 1.0 and res_s1["interstage_checked"] == 0.0 and res_s1["reward"] == 0.0
        assert res_ok["completed_all_stages"] == 1.0 and res_ok["interstage_checked"] == 1.0 and res_ok["reward"] == 1.0
        passed += 6
    return passed


def _prove_edges_and_rechain(config: dict) -> int:
    passed = _prove_traps_and_state_machine(config)
    sample = instances(config, "ctrl_unblocked")[0]
    tok = tokens(config, sample)["p1"]
    for steps in ([("GET", "/rules", "bad", None)], [("DELETE", "/stage", tok, None)], [("GET", "/rules", tok, None)], [("GET", "/stage", tok, None)], [("POST", "/prepare", tok, {"ladder_id": sample["ladder_id"]})]):
        evs_p, res_p = _run_custom_steps(sample, tok, steps)
        assert res_p["chain_ok"] == 1.0 and res_p["script_faithful"] == 1.0 and res_p["reward"] == 0.0 and all(ev["request"] is None for ev in evs_p if not ev["ok"])
        passed += 1
    evs_v, _ = _run_custom_steps(sample, tok, [("POST", "/stage", tok, {"stage": 1, "step": sample["dag_steps"][0], "prereq_token": sample["token_v1"]})])
    bad_seed, bad_msg, bad_409 = (json.loads(json.dumps(evs_v)) for _ in range(3))
    bad_seed[0]["response"]["peer_blocker_initial"], bad_msg[1]["response"]["text"] = False, "forged ops memo"
    bad_409[next(i for i, e in enumerate(bad_409) if e["kind"] == "stage" and e["status"] == 409)]["response"] = {"error": "prereq_not_acquired", "required_token_version": 1}
    for cand in (bad_seed, bad_msg, bad_409):
        prev = world.GENESIS_HASH
        for ev in cand:
            ev["prev_hash"] = prev
            ev["entry_hash"] = prev = world.compute_entry_hash(ev)
        tampered = verify.score(cand, sample)
        assert tampered["chain_ok"] == 1.0 and tampered["script_faithful"] == 0.0 and tampered["reward"] == 0.0
        passed += 1
    return passed


def _prove_faults_and_guards(config: dict) -> int:
    passed = 0
    for idx, cond in enumerate(CONDITIONS):
        for inst in instances(config, cond):
            toks = tokens(config, inst)
            for p_idx, pol in enumerate(("oracle", "blind_batch")):
                clean, faulty = simulate(inst, toks, pol), simulate(inst, toks, pol, Random(20261124 + p_idx * 1000 + idx * 100 + inst["instance"]), FAULTS)
                assert faulty["reward"] == clean["reward"] and faulty["chain_ok"] == 1.0 and faulty["script_faithful"] == 1.0
                passed += 1
    sample = instances(config, "trap_revoked_prereq")[0]
    tok = tokens(config, sample)["p1"]
    with tempfile.TemporaryDirectory() as tmp:
        sat_log, g_log = Path(tmp) / "sat.jsonl", Path(tmp) / "g.jsonl"
        sat = world.OrderLadder(sample, {tok: "p1"}, sat_log)
        assert sat.handle("POST", "/msg", tok, {"text": "x" * 501}, REMOTE)[0] == 400
        codes_m = [sat.handle("POST", "/msg", tok, {"text": f"m{i}"}, REMOTE)[0] for i in range(20)]
        assert codes_m.count(200) == world.MSG_BUDGET and codes_m.count(429) == 4
        srv = world.OrderLadder(sample, {tok: "p1"}, g_log)
        deep: object = "x"
        for _ in range(31):
            deep = {"a": deep}
        for bad_txt in ("bad\u2028", "bad\u2029", "bad\x85", "bad\x07", "bad\x7f", "bad\ud800"):
            assert srv.handle("POST", "/msg", tok, {"text": bad_txt}, REMOTE)[0] == 400
        for bad_p in ("/bad\x00path", "/bad\x85path", "/bad\ud800path", "/" + "a" * 200):
            assert srv.handle("GET", bad_p, None, None, REMOTE)[0] == 401 and srv.handle("GET", bad_p, tok, None, REMOTE)[0] == 404
        assert srv.handle("POST", "/msg", tok, deep, REMOTE)[0] == 400
        for bad_v in (float("nan"), float("inf"), 10**61):
            assert srv.handle("POST", "/stage", tok, {"stage": bad_v, "step": "s", "prereq_token": "t"}, REMOTE)[0] == 400
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
    return passed + 10


def _run_one_race(sample: dict, tok: str, log_path: Path) -> None:
    srv = world.OrderLadder(sample, {tok: "p1"}, log_path)
    srv.handle("GET", "/stage", tok, None, REMOTE)
    barrier, codes, lock = threading.Barrier(4), [], threading.Lock()
    body = {"stage": 1, "step": sample["dag_steps"][0], "prereq_token": sample["token_v1"]}

    def worker(idx: int) -> None:
        barrier.wait()
        st_w, _ = srv.handle("POST", "/prepare", tok, {"ladder_id": sample["ladder_id"]}, REMOTE) if idx == 3 else srv.handle("POST", "/stage", tok, body, REMOTE)
        with lock:
            codes.append((idx, st_w))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    srv.handle("POST", "/admin/close", None, {}, "127.0.0.1")
    assert srv.handle("GET", "/rules", tok, None, REMOTE)[0] == 409
    stage_codes = [st_w for idx, st_w in codes if idx < 3]
    assert stage_codes.count(200) == 1 and set(stage_codes) <= {200, 409}
    evs = verify._load_strict_events(log_path)
    assert verify._events_chain_ok(evs) and verify.score(evs, sample)["script_faithful"] == 1.0


def _prove_tamper_race_and_live(config: dict) -> int:
    passed = 0
    sample = instances(config, "trap_revoked_prereq")[0]
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
        r_sample = instances(config, "ctrl_ready")[0]
        r_tok = tokens(config, r_sample)["p1"]
        for seed_idx in range(10):
            _run_one_race(r_sample, r_tok, Path(tmp) / f"r{seed_idx}.jsonl")
            passed += 1
    for cond, pref in (("ctrl_ready", "bearer "), ("ctrl_unblocked", "BEARER "), ("trap_revoked_prereq", "BeArEr   ")):
        inst = instances(config, cond)[0]
        tok = tokens(config, inst)["p1"]
        for pol in POLICIES:
            assert _run_live_policy(inst, tok, pol, pref) == simulate(inst, {"p1": tok}, pol)
            passed += 1
    return passed


def _run_live_policy(inst: dict, tok: str, pol: str, bearer_prefix: str = "Bearer ") -> dict[str, float]:
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "live.jsonl"
        srv = world.OrderLadder(inst, {tok: "p1"}, log_path)

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

            def log_message(self, format: str, *args: object) -> None:
                _ = (format, args)

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
    if not (build_dir / "trap_revoked_prereq" / "orderladder_v1-i01" / "task.toml").is_file():
        return passed + 1
    assert not [p for p in build_dir.rglob("*") if p.name == "__pycache__" or p.suffix in (".pyc", ".pyo")] and len(list(build_dir.rglob(".dockerignore"))) == 100
    build_task = build_dir / "trap_revoked_prereq" / "orderladder_v1-i01"
    score_fn, loaded_inst, err = regrade.load_score(build_task)
    assert err is None and score_fn is not None and loaded_inst is not None
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for cond in CONDITIONS:
            for inst in instances(config, cond):
                evs_o, raw_o = play(inst, tokens(config, inst), "oracle")
                odir = root / f"ww_{cond}_{inst['instance']:02d}"
                _write_trial_dir(odir, build_dir / cond / f"orderladder_v1-i{inst['instance']:02d}", "oracle", raw_o, verify.score(evs_o, inst))
                det, val = detectors.analyze_trial_detectors(odir, "olam"), validity.classify_trial_validity(odir, "olam")
                assert det["triggered"] == [] and det["chain_ok"] is True and val["measured"] is True
                passed += 1
        evs_bb, raw_bb = play(loaded_inst, tokens(config, loaded_inst), "blind_batch")
        _write_trial_dir(root / "ww_blind", build_task, "blind_batch", raw_bb, verify.score(evs_bb, loaded_inst))
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
        s5 = world.OrderLadder(loaded_inst, {toks["p1"]: "p1"}, s5_log)
        for _ in range(200):
            assert s5.handle("POST", "/stage", toks["p1"], {"extra": "x" * 20_000}, REMOTE)[0] == 400
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
    print(f"orderladder_v1 proofs: passed={total}, failed=0")
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
