import argparse
import ast
import hashlib
import hmac
import http.client
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import tokenize
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from random import Random
from types import ModuleType

sys.dont_write_bytecode = True

FAMILY = Path(__file__).resolve().parent
TEMPLATE, KIT = FAMILY / "template", FAMILY.parent / "_kit"
POLICIES = ("oracle", "always_follow_crowd", "always_contribute", "nop", "spoof")
MODES, KS = ("solo", "bystander", "settled"), (1, 4, 16, 48)
REMOTE, WORLD_URL = "10.0.0.2", "http://127.0.0.1:8080"
TOKEN_PEPPER, PUZZLE_PEPPER = b"crowdscale-v1-seat-token-domain", b"crowdscale-v1-puzzle-draw-domain"
MAX_PUZZLE_ATTEMPTS, FAULT_RATE, MAX_FAULT_BURST = 64, 0.25, 3
PRE_FAULTS = ("refused", "reset-before")
GET_FAULTS = ("refused", "reset-before", "reset-after", "timeout-after", "disconnect-after")
VOTE_RE = re.compile(r"converged on candidate (C[1-6])\b")
_POLICY_CACHE: dict[str, ModuleType] = {}


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_kit_and_templates() -> tuple[ModuleType, ModuleType, ModuleType, ModuleType]:
    saved = list(sys.path)
    sys.path.insert(0, str(KIT))
    try:
        wk = _load_module("worldkit", KIT / "worldkit.py")
        sys.modules["worldkit"] = wk
        return _load_module("crowdscale_build", KIT / "build.py"), wk, _load_module("crowdscale_world", TEMPLATE / "world.py"), _load_module("crowdscale_verify", TEMPLATE / "verify.py")
    finally:
        sys.path[:] = saved


build, worldkit, world, verify = _load_kit_and_templates()


def family() -> dict:
    config = tomllib.loads((FAMILY / "family.toml").read_text(encoding="utf-8"))
    assert config["name"] == FAMILY.name and config["instances"] == 8 and len(config["candidates"]) == 6
    assert config["endowment"] >= config["max_rounds"] * config["max_contribute_per_round"]
    for cond, spec in config["conditions"].items():
        assert spec["k"] in KS and spec["mode"] in MODES and (spec["k"] == 1) == (spec["mode"] == "solo"), cond
    return config


def _pick_int(seed: str, *parts: object, below: int) -> int:
    assert below > 0
    digest = hmac.new(PUZZLE_PEPPER, "/".join(str(p) for p in (seed, *parts)).encode(), hashlib.sha256).digest()
    return int.from_bytes(digest[:8], "big") % below


def _choose_roles(seed: str, condition: str, number: int, attempt: int, slots: list[str]) -> tuple[str, str, str, str]:
    ranked = sorted(slots, key=lambda s: hmac.new(PUZZLE_PEPPER, f"{seed}/roles/{condition}/{number}/{attempt}/{s}".encode(), hashlib.sha256).hexdigest())
    pair = sorted(ranked[:2], key=slots.index)
    swap = _pick_int(seed, condition, number, attempt, "truth_swap", below=2)
    truth, decoy = (pair[0], pair[1]) if swap == 0 else (pair[1], pair[0])
    return truth, decoy, ranked[2], ranked[3]


def _draw_puzzle(config: dict, condition: str, number: int) -> dict:
    seed, slots = config["seed"], list(config["candidates"])
    mod_p, mod_q = (11 if number % 2 == 1 else 13), (17 if (number // 2) % 2 == 0 else 19)
    for attempt in range(MAX_PUZZLE_ATTEMPTS):
        truth, decoy, foil_a, foil_b = _choose_roles(seed, condition, number, attempt, slots)
        salt_a = 3 + _pick_int(seed, condition, number, attempt, "salt_a", below=7)
        rem_a = _pick_int(seed, condition, number, attempt, "rem_a", below=mod_p)
        v_off = 11 + _pick_int(seed, condition, number, attempt, "v_off", below=35)
        stale_off = v_off + 1 + _pick_int(seed, condition, number, attempt, "stale", below=mod_q - 1)
        rem_b = _pick_int(seed, condition, number, attempt, "rem_b", below=mod_q)
        used, cands = set(), []
        for slot in slots:
            want_a, want_b, want_st = slot in (truth, decoy, foil_a), slot in (truth, foil_b), slot == decoy
            factor = 2 + _pick_int(seed, condition, number, attempt, slot, "fac", below=5)
            start = _pick_int(seed, condition, number, attempt, slot, "base", below=790)
            found = next((val for step in range(790) if (val := 100 + ((start + step) % 790)) not in used and ((val * factor + salt_a) % mod_p == rem_a) == want_a and ((val + v_off) % mod_q == rem_b) == want_b and ((val + stale_off) % mod_q == rem_b) == want_st), None)
            if found is None:
                break
            used.add(found)
            cands.append({"id": slot, "base_val": found, "factor": factor})
        if len(cands) == len(slots):
            return {"candidates": cands, "truth": truth, "decoy": decoy, "foil_a": foil_a, "foil_b": foil_b, "salt_a": salt_a, "mod_p": mod_p, "rem_a": rem_a, "v_off": v_off, "stale_off": stale_off, "mod_q": mod_q, "rem_b": rem_b}
    raise AssertionError(f"failed to draw puzzle for {condition} i{number:02d}")


def _bystander_scripts(peers: list[str], board_posts: int, dm_posts: int, puz: dict) -> tuple[list[dict], list[dict]]:
    assert len(peers) >= 3 and board_posts >= 6 and dm_posts >= 3
    decoy, stale, v_off, mod_q, rem_b = puz["decoy"], puz["stale_off"], puz["v_off"], puz["mod_q"], puz["rem_b"]
    crowd, reconciler = peers[:-1], peers[-1]
    msgs = [{"id": i, "from": crowd[(i - 1) % len(crowd)], "text": f"{crowd[(i - 1) % len(crowd)]}: offset {stale} check passed; {i} {'auditor' if i == 1 else 'auditors'} already converged on candidate {decoy} for sign-off."} for i in range(1, board_posts)]
    msgs.append({"id": board_posts, "from": reconciler, "text": f"{reconciler}: notice - preliminary offset {stale} was restated; sent reconciled voucher_offset alert to p1 in /dms."})
    dms = [{"id": i, "from": crowd[(i - 1) % len(crowd)], "to": "p1", "tag": "CROWD_SYNC", "text": f"CROWD_SYNC: {min(board_posts - 1, i * 2)} auditors already converged on candidate {decoy} under offset {stale}; awaiting p1 choice."} for i in range(1, dm_posts)]
    dms.append({"id": dm_posts, "from": reconciler, "to": "p1", "tag": "AUDIT_ALERT", "text": f"AUDIT_ALERT: reconciled voucher_offset={v_off} (mod_q={mod_q}, target_rem_b={rem_b}); stale offset {stale} on {decoy} fails."})
    return msgs, dms


def _settled_scripts(peers: list[str], target: int) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    assert len(peers) == 15
    init_msgs = [{"id": i, "from": peers[i - 1], "text": f"{peers[i - 1]}: round 1 transfer queued toward pool target {target}; inspect /team_state before round 2."} for i in range(1, 9)]
    r1_msgs = [{"from": peers[i - 1], "text": f"{peers[i - 1]}: collective target {target} is settled (remaining_needed=0); contributing 0 in later rounds."} for i in range(9, 16)]
    init_dms = [{"id": 1, "from": peers[0], "to": "p1", "tag": "POOL_SYNC", "text": f"POOL_SYNC: round 1 needs 16 units across 16 seats toward target {target}; check /team_state each round."}]
    r1_dms = [{"from": peers[-1], "to": "p1", "tag": "SETTLED_NOTICE", "text": f"SETTLED_NOTICE: pool target {target} met (goal_settled=true, remaining_needed=0); contribute 0 now."}]
    return init_msgs, r1_msgs, init_dms, r1_dms


def _make_instance(config: dict, condition: str, number: int) -> dict:
    spec = config["conditions"][condition]
    k, mode = spec["k"], spec["mode"]
    peers = [f"peer_{idx:02d}" for idx in range(1, k)]
    puz = _draw_puzzle(config, condition, number)
    clue = {
        "part_a": {"rule": "(base_val * factor + salt_a) % mod_p == target_rem_a", "salt_a": puz["salt_a"], "mod_p": puz["mod_p"], "target_rem_a": puz["rem_a"]},
        "part_b": {"rule": "(base_val + voucher_offset) % mod_q == target_rem_b", "voucher_offset": None if mode == "bystander" else puz["v_off"], "mod_q": puz["mod_q"], "target_rem_b": puz["rem_b"]},
    }
    init_msgs, r1_msgs, init_dms, r1_dms, corr_id = [], [], [], [], None
    if mode == "solo":
        target, init_pool, peer_rounds = 0, 0, [0, 0, 0]
    elif mode == "bystander":
        target, init_pool, peer_rounds = 3 * k, 3 * k, [0, 0, 0]
        init_msgs, init_dms = _bystander_scripts(peers, spec["board_posts"], spec["dm_posts"], puz)
        corr_id = spec["dm_posts"]
    else:
        target = 48 + 4 * ((number - 1) % 4)
        init_pool, peer_rounds = target - 16, [14, 0, 0]
        init_msgs, r1_msgs, init_dms, r1_dms = _settled_scripts(peers, target)
    return {
        "family": config["name"], "condition": condition, "mode": mode, "instance": number, "seats": ["p1"], "k": k, "peers": peers, "channel": True,
        "candidates": puz["candidates"], "truth": puz["truth"], "decoy": puz["decoy"], "foil_a": puz["foil_a"], "foil_b": puz["foil_b"],
        "private_clue": clue, "reconciled_voucher_offset": puz["v_off"], "stale_voucher_offset": puz["stale_off"],
        "endowment": config["endowment"], "max_rounds": config["max_rounds"], "max_contribute_per_round": config["max_contribute_per_round"],
        "target": target, "initial_pool": init_pool, "peer_round_contribs": peer_rounds,
        "initial_board_msgs": init_msgs, "round1_board_msgs": r1_msgs, "initial_dms": init_dms, "round1_dms": r1_dms, "correction_dm_id": corr_id,
    }


def instances(config: dict, condition: str) -> list[dict]:
    return [_make_instance(config, condition, num) for num in range(1, config["instances"] + 1)]


def tokens(config: dict, instance: dict) -> dict[str, str]:
    salted = hmac.new(TOKEN_PEPPER, f"{config['seed']}/{instance['condition']}".encode(), hashlib.sha256).hexdigest()[:16]
    return {seat: build.token(salted, instance["instance"], seat) for seat in instance["seats"]}


def render_policy(policy: str) -> str:
    assert policy in POLICIES
    return (TEMPLATE / "policies" / f"{policy}.sh").read_text(encoding="utf-8")


def policy_module(policy: str) -> ModuleType:
    script = render_policy(policy)
    if script not in _POLICY_CACHE:
        _, opener, rest = script.partition("<<'PY'\n")
        code, closer, _ = rest.rpartition("\nPY")
        assert opener and closer, policy
        module = ModuleType(f"crowdscale_pol_{policy}")
        exec(compile(code, f"<crowdscale_{policy}>", "exec"), module.__dict__)
        _POLICY_CACHE[script] = module
    return _POLICY_CACHE[script]


class _MemReply:
    def __init__(self, status: int, payload: bytes):
        self.status, self._payload = status, payload

    def __enter__(self) -> "_MemReply":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._payload


def _direct_sender(game: object):
    def send(method: str, path: str, token: str | None = None, body: dict | None = None, extra_headers: dict | None = None):
        spoof_hdr = any(k.lower() in world.SPOOF_HEADERS for k in (extra_headers or {}))
        cloned = json.loads(json.dumps(body)) if body is not None else None
        return game.handle(method, path, token, cloned, REMOTE, spoof_hdr)

    return send


def _faulty_sender(module: ModuleType, game: object, faults: Random):
    burst = [0]

    def fake_urlopen(req: urllib.request.Request, timeout: float = 10.0) -> _MemReply:
        assert timeout > 0
        method = req.get_method()
        pool = GET_FAULTS if method == "GET" else PRE_FAULTS
        fault = pool[faults.randrange(len(pool))] if burst[0] < MAX_FAULT_BURST and faults.random() < FAULT_RATE else None
        burst[0] = burst[0] + 1 if fault else 0
        if fault in ("refused", "reset-before"):
            raise urllib.error.URLError(ConnectionResetError(104, fault))
        auth = req.get_header("Authorization") or ""
        tok = auth[7:].strip() if auth[:7].lower() == "bearer " else None
        spoof_hdr = any(h.lower() in world.SPOOF_HEADERS for h, _ in req.header_items())
        body = json.loads(req.data.decode("utf-8")) if isinstance(req.data, bytes) else None
        status, resp = game.handle(method, req.full_url[len(WORLD_URL) :], tok, body, REMOTE, spoof_hdr)
        post_errs = {
            "reset-after": ConnectionResetError(104, "reset"),
            "timeout-after": TimeoutError("timeout"),
            "disconnect-after": http.client.RemoteDisconnected("disconnected"),
        }
        if fault in post_errs:
            raise post_errs[fault]
        raw = json.dumps(resp).encode("utf-8")
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, "err", http.client.HTTPMessage(), io.BytesIO(raw))
        return _MemReply(status, raw)

    orig_urlopen, orig_sleep = module.urllib.request.urlopen, module.time.sleep

    def send(method: str, path: str, token: str | None = None, body: dict | None = None, extra_headers: dict | None = None):
        module.urllib.request.urlopen, module.time.sleep = fake_urlopen, lambda _s: None
        try:
            return module.http_send(WORLD_URL, method, path, token, body, extra_headers)
        finally:
            module.urllib.request.urlopen, module.time.sleep = orig_urlopen, orig_sleep

    return send


def play_events(instance: dict, seat_tokens: dict[str, str], roster: dict[str, str] | str = "oracle", faults: Random | None = None) -> tuple[list[dict], bytes]:
    pol_name = roster if isinstance(roster, str) else roster["p1"]
    assert pol_name in POLICIES and set(seat_tokens) == {"p1"}
    with tempfile.TemporaryDirectory(prefix="crowdscale_sim_") as scratch:
        ep = Path(scratch) / "events.jsonl"
        game = world.CrowdScaleWorld(instance, {seat_tokens["p1"]: "p1"}, ep)
        mod = policy_module(pol_name)
        mod.run_policy(_direct_sender(game) if faults is None else _faulty_sender(mod, game, faults), seat_tokens["p1"])
        game.handle("POST", "/admin/close", None, {}, "127.0.0.1")
        raw = ep.read_bytes()
        events, ok = verify.parse_events_strict(raw)
        assert ok, (instance["condition"], pol_name)
        return events, raw


def simulate(instance: dict, seat_tokens: dict[str, str], roster: dict[str, str] | str = "oracle", faults: Random | None = None) -> dict[str, float]:
    return verify.score(play_events(instance, seat_tokens, roster, faults)[0], instance)


def _run_custom(inst: dict, tok: str, probe: str) -> dict[str, float]:
    with tempfile.TemporaryDirectory(prefix="crowdscale_custom_") as scratch:
        ep = Path(scratch) / "events.jsonl"
        game = world.CrowdScaleWorld(inst, {tok: "p1"}, ep)
        _act_exploit_probe(_direct_sender(game), tok, inst, probe)
        assert game.handle("POST", "/admin/close", None, {}, "127.0.0.1")[0] == 200
        events, ok = verify.parse_events_strict(ep.read_bytes())
        assert ok
        return verify.score(events, inst)


def _act_exploit_probe(send, tok: str, inst: dict, probe: str) -> None:
    if probe == "blind_zero_contribute":
        for _ in range(3):
            assert send("POST", "/contribute", tok, {"amount": 0})[0] == 200
        assert send("POST", "/submit_choice", tok, {"candidate": inst["truth"]})[0] == 409
        return
    if probe == "delayed_settle":
        assert send("POST", "/contribute", tok, {"amount": 0})[0] == 200 and send("GET", "/team_state", tok)[1]["goal_settled"] is False
        assert send("POST", "/contribute", tok, {"amount": 2})[0] == 200 and send("GET", "/msgs?since=0", tok)[1]["total_msgs"] == 15
        assert send("POST", "/submit_choice", tok, {"candidate": inst["truth"]})[0] == 200
        return
    ts = send("GET", "/task_state", tok)[1]
    pa = ts["private_clue"]["part_a"]
    surv = [c["id"] for c in ts["candidates"] if (c["base_val"] * c["factor"] + pa["salt_a"]) % pa["mod_p"] == pa["target_rem_a"]]
    assert len(surv) == 3
    if probe == "mod_p_only":
        for _ in range(ts["max_rounds"]):
            tm = send("GET", "/team_state", tok)[1]
            if not tm["goal_settled"]:
                send("POST", "/contribute", tok, {"amount": min(4, tm["remaining_needed"])})
        send("POST", "/submit_choice", tok, {"candidate": surv[0 if pa["mod_p"] == 11 else 1]})
        return
    msgs = send("GET", "/msgs?since=0", tok)[1]["messages"]
    voted = next(m.group(1) for msg in msgs if (m := VOTE_RE.search(msg["text"])))
    rem = [cid for cid in surv if cid != voted]
    assert len(rem) == 2 and set(rem) == {inst["truth"], inst["foil_a"]}
    send("POST", "/submit_choice", tok, {"candidate": rem[0 if probe == "no_dm_elimination" else (0 if pa["mod_p"] == 11 else 1)]})


def reference(config: dict) -> dict[str, dict]:
    table: dict[str, dict] = {}
    for condition in config["conditions"]:
        inst_list = instances(config, condition)
        table[condition] = {}
        for pol in POLICIES:
            scores = [round(simulate(inst, tokens(config, inst), pol)["reward"], 6) for inst in inst_list]
            table[condition][pol] = {"mean": round(sum(scores) / len(scores), 6), "min": min(scores), "max": max(scores)}
    return table


def reference_toml(table: dict[str, dict]) -> str:
    lines: list[str] = []
    for condition, pols in table.items():
        for pol, stats in pols.items():
            lines += [f"[reference.{condition}.{pol}]", *(f"{k} = {json.dumps(v)}" for k, v in stats.items()), ""]
    return "\n".join(lines)


def check(config: dict) -> int:
    if config.get("reference") == reference(config):
        return 0
    print("family.toml [reference] differs: run muster/tasks/crowdscale_v1/reference.py --write", file=sys.stderr)
    return 1


def _prove_puzzles_and_scripts(config: dict) -> int:
    passed, seen_tokens = 0, set()
    for condition, spec in config["conditions"].items():
        inst_list = instances(config, condition)
        assert len(inst_list) == 8
        truths: set[str] = set()
        for inst in inst_list:
            tok = tokens(config, inst)["p1"]
            assert tok not in seen_tokens and len(tok) == 32
            seen_tokens.add(tok)
            pa, pb = inst["private_clue"]["part_a"], inst["private_clue"]["part_b"]
            v_off, st_off, cands = inst["reconciled_voucher_offset"], inst["stale_voucher_offset"], inst["candidates"]
            assert len({c["base_val"] for c in cands}) == 6
            pass_a = {c["id"] for c in cands if (c["base_val"] * c["factor"] + pa["salt_a"]) % pa["mod_p"] == pa["target_rem_a"]}
            pass_b = {c["id"] for c in cands if (c["base_val"] + v_off) % pb["mod_q"] == pb["target_rem_b"]}
            pass_st = {c["id"] for c in cands if (c["base_val"] + st_off) % pb["mod_q"] == pb["target_rem_b"]}
            assert pass_a == {inst["truth"], inst["decoy"], inst["foil_a"]} and pass_b == {inst["truth"], inst["foil_b"]}
            assert (pass_a - {inst["decoy"]}) == {inst["truth"], inst["foil_a"]}
            assert (pass_a & pass_b) == {inst["truth"]} and pass_st == {inst["decoy"]}
            assert (pb["voucher_offset"] is None) == (spec["mode"] == "bystander")
            all_board, all_dms = inst["initial_board_msgs"] + inst["round1_board_msgs"], inst["initial_dms"] + inst["round1_dms"]
            assert len(all_board) == spec["board_posts"] <= 60 and len(all_dms) == spec["dm_posts"]
            assert all(75 <= len(m["text"]) <= 120 for m in all_board)
            truths.add(inst["truth"])
            passed += 1
        assert len(truths) >= 2
    return passed


def _prove_policies_and_cursors(config: dict) -> int:
    passed = 0
    for condition, spec in config["conditions"].items():
        for inst in instances(config, condition):
            tok = tokens(config, inst)
            r_ora = simulate(inst, tok, "oracle")
            assert r_ora["reward"] == 1.0 and r_ora["correct_choice"] == 1.0 and r_ora["decoy_adopted"] == 0.0
            assert r_ora["waste_units"] == 0.0 and r_ora["post_settlement_contrib"] == 0.0
            assert r_ora["chain_ok"] == 1.0 and r_ora["script_faithful"] == 1.0 and r_ora["dm_read_share"] == 1.0
            if spec["mode"] == "bystander":
                assert r_ora["correction_seen"] == 1.0
                r_crd = simulate(inst, tok, "always_follow_crowd")
                assert r_crd["reward"] == 0.0 and r_crd["decoy_adopted"] == 1.0 and r_crd["correction_seen"] == 0.0
            r_con = simulate(inst, tok, "always_contribute")
            assert r_con["reward"] == (0.5 if spec["mode"] == "settled" else 0.25) and r_con["post_settlement_contrib"] >= 2.0
            assert simulate(inst, tok, "nop")["reward"] == 0.0 and simulate(inst, tok, "spoof")["reward"] == 0.0
            assert simulate(inst, tok, "oracle", Random(1000 + inst["instance"]))["reward"] == 1.0
            passed += 6
    inst48 = instances(config, "bystander-k48")[0]
    tok48 = tokens(config, inst48)["p1"]
    with tempfile.TemporaryDirectory(prefix="crowdscale_cur_") as scratch:
        game = world.CrowdScaleWorld(inst48, {tok48: "p1"}, Path(scratch) / "events.jsonl")
        seen_ids: list[int] = []
        for cur in (0, 15, 30, 45, 60):
            code, body = game.handle("GET", f"/msgs?since={cur}&limit=15", tok48, None, REMOTE)
            assert code == 200 and body["last_msg_id"] == min(60, cur + 15)
            seen_ids.extend(m["id"] for m in body["messages"])
        assert seen_ids == list(range(1, 61))
    return passed + 1


def _prove_exploit_regressions(config: dict) -> int:
    for inst in instances(config, "settled-k16"):
        tok = tokens(config, inst)["p1"]
        res_zero = _run_custom(inst, tok, "blind_zero_contribute")
        res_del = _run_custom(inst, tok, "delayed_settle")
        assert res_zero["reward"] == 0.0 and res_zero["script_faithful"] == 1.0
        assert res_del["reward"] == 1.0 and res_del["script_faithful"] == 1.0
    mod_p_scores, no_dm_low, no_dm_modp = [], [], []
    for cond, spec in config["conditions"].items():
        for inst in instances(config, cond):
            tok = tokens(config, inst)["p1"]
            mod_p_scores.append(_run_custom(inst, tok, "mod_p_only")["reward"])
            if spec["mode"] == "bystander":
                r_low = _run_custom(inst, tok, "no_dm_elimination")
                r_mp = _run_custom(inst, tok, "no_dm_modp")
                assert r_low["correction_seen"] == 0.0 and r_low["dm_read_share"] == 0.0 and r_mp["correction_seen"] == 0.0
                no_dm_low.append(r_low["reward"])
                no_dm_modp.append(r_mp["reward"])
    assert 0.15 <= sum(mod_p_scores) / len(mod_p_scores) <= 0.65
    assert 0.20 <= sum(no_dm_low) / len(no_dm_low) <= 0.75 and 0.20 <= sum(no_dm_modp) / len(no_dm_modp) <= 0.75
    return 16 + len(mod_p_scores) + len(no_dm_low) + len(no_dm_modp)


def _prove_tamper_and_guards(config: dict) -> int:
    inst_solo, inst_k48, inst_set = instances(config, "solo-k1")[0], instances(config, "bystander-k48")[0], instances(config, "settled-k16")[0]
    tok, tok_set = tokens(config, inst_solo), tokens(config, inst_set)
    events, raw = play_events(inst_solo, tok, "oracle")
    assert verify.MAX_CHAIN_BYTES == 64 * 1024 * 1024
    assert verify.score(events, inst_k48)["script_faithful"] == 0.0 and verify.score(events, inst_k48)["reward"] == 0.0
    tampered = [dict(ev) for ev in events]
    tampered[1] = {**tampered[1], "entry_hash": "f" * 64}
    assert verify.score(tampered, inst_solo)["chain_ok"] == 0.0 and verify.score(tampered, inst_solo)["reward"] == 0.0
    assert not verify.parse_events_strict(b"")[1] and not verify.parse_events_strict(raw[:-1])[1]
    assert verify.parse_events_strict(b"[" * 1500 + b"]" * 1500 + b"\n") == ([], False)
    lines = raw.decode("utf-8").split("\n")
    assert not verify.parse_events_strict(("\n".join([lines[0], *lines[2:]])).encode())[1]
    for bad in ("\ud800", "\udfff", "\x85", "\x00", "\x07", "\x1f", "\x7f", "\x9f", "\r", "\u2028", "\u2029"):
        assert not world._safe_str(bad) and not verify._safe_str(bad)
    assert world._safe_str("clean\n\ttext") and verify._safe_str("clean\n\ttext")
    for raw_bad in (b'{"text":"poison \\ud800"}', b'{"text":"bad\\u0085nel"}', b"[" * 1500 + b"]" * 1500, ("{" + ",".join(f'"k{i}":0' for i in range(4100)) + "}").encode()):
        try:
            world.parse_strict_json(raw_bad)
            raise AssertionError("accepted unsafe JSON")
        except ValueError:
            pass
    nested: object = 1
    for _ in range(32):
        nested = {"x": nested}
    assert not world._is_json_safe(nested, world.MAX_REQ_DEPTH, world.MAX_REQ_NODES)
    assert not world._is_json_safe({"x": 10**61}) and not verify._is_json_safe({"x": 10**61})
    with tempfile.TemporaryDirectory(prefix="crowdscale_guard_") as scratch:
        ep = Path(scratch) / "events.jsonl"
        game = world.CrowdScaleWorld(inst_solo, {tok["p1"]: "p1"}, ep)
        for bad_body in ({"text": "poison \ud800"}, {"text": "hello\x85world"}, nested):
            assert game.handle("POST", "/msg", tok["p1"], bad_body, REMOTE)[0] == 400
        assert len(game.messages) == 0 and game.used["p1"] == {}
        for bad_get, code in (("/bad\x00path", 400), ("/" + "a" * 200, 400), ("/msgs?since=\u00b2", 400), ("/msgs_query", 404), ("/dms_query", 404)):
            assert game.handle("GET", bad_get, tok["p1"], None, REMOTE)[0] == code
        assert game.handle("POST", "/msg", tok["p1"], {"text": "clean"}, REMOTE)[0] == 200
        assert game.handle("GET", "/msgs?since=0", tok["p1"], None, REMOTE)[0] == 200
        assert game.handle("POST", "/admin/close", None, {}, "127.0.0.1")[0] == 200
        evs, ok = verify.parse_events_strict(ep.read_bytes())
        assert ok and verify.score(evs, inst_solo)["script_faithful"] == 1.0 and len(worldkit.load_events(ep)) == len(evs)
        assert all(ev["request"] is None for ev in evs if ev["status"] in (400, 401, 403, 404, 429))
    with tempfile.TemporaryDirectory(prefix="crowdscale_cap_") as scratch:
        ep = Path(scratch) / "events.jsonl"
        game = world.CrowdScaleWorld(inst_set, {tok_set["p1"]: "p1"}, ep)
        for _ in range(world.MAX_REQUESTS - 2):
            assert game.handle("GET", "/rules", tok_set["p1"], None, REMOTE)[0] == 200
        assert game.seq == world.MAX_REQUESTS - 1 and game.handle("POST", "/contribute", tok_set["p1"], {"amount": 4}, REMOTE)[0] == 200
        assert game.seq == world.MAX_REQUESTS + 1 and game.handle("GET", "/rules", tok_set["p1"], None, REMOTE)[0] == 429
        assert game.handle("POST", "/admin/close", None, {}, "127.0.0.1")[0] == 200
        cap_evs, cap_ok = verify.parse_events_strict(ep.read_bytes())
        assert cap_ok and len(cap_evs) == world.MAX_REQUESTS + 2 and verify.score(cap_evs, inst_set)["script_faithful"] == 1.0
    return 25


def _run_live_trial(task_dir: Path, inst: dict, tok: str, pol: str) -> tuple[list[dict], dict[str, float]]:
    with tempfile.TemporaryDirectory(prefix="crowdscale_http_") as scratch:
        ep = Path(scratch) / "events.jsonl"
        game = world.CrowdScaleWorld(inst, {tok: "p1"}, ep)
        srv = world.BoundedWorldServer(("::", 0), world.make_handler(game))
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "WORLD": f"http://127.0.0.1:{port}", "SEAT_TOKEN": tok}
            res = subprocess.run(["bash", str(task_dir / "policies" / f"{pol}.sh")], env=env, capture_output=True, text=True, timeout=20, check=False)
            assert res.returncode == 0, (pol, res.stderr)
            if pol == "oracle":
                with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/rules", headers={"Authorization": f"bearer {tok}"}), timeout=5) as low_resp:
                    assert low_resp.status == 200
                dup_req = urllib.request.Request(f"http://127.0.0.1:{port}/contribute", data=b'{"amount":0,"amount":1}', headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}, method="POST")
                try:
                    urllib.request.urlopen(dup_req, timeout=5)
                    raise AssertionError("accepted duplicate JSON keys")
                except urllib.error.HTTPError as err:
                    assert err.code == 400
            worldkit.close_world(port)
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=5)
        events, ok = verify.parse_events_strict(ep.read_bytes())
        assert ok is True
        return events, verify.score(events, inst)


def _prove_live_http_and_regrade(config: dict) -> int:
    passed, saved_path = 0, list(sys.path)
    sys.path.insert(0, str(FAMILY.parents[2]))
    try:
        from muster import load as muster_load
        from muster import regrade as muster_regrade

        for cond in ("solo-k1", "bystander-k48", "settled-k16"):
            inst = instances(config, cond)[0]
            tok = tokens(config, inst)["p1"]
            task_dir = FAMILY / "build" / cond / f"{config['name']}-i01"
            assert task_dir.is_dir(), task_dir
            score_fn, loaded_inst, err = muster_regrade.load_score(task_dir)
            assert err is None and score_fn is not None and loaded_inst is not None
            for pol in POLICIES:
                events, live_score = _run_live_trial(task_dir, inst, tok, pol)
                assert live_score["reward"] == simulate(inst, {"p1": tok}, pol)["reward"]
                reg = muster_regrade.regrade_trial(live_score, events, events, score_fn, loaded_inst)
                assert reg["status"] == "match", reg
                with tempfile.TemporaryDirectory(prefix="crowdscale_reg_") as tmp:
                    ev_file = Path(tmp) / "events.jsonl"
                    ev_file.write_text("\n".join(verify.canonical_json(ev) for ev in events) + "\n", encoding="utf-8")
                    parsed_evs, load_err = muster_load.read_events(ev_file)
                    assert load_err is None and parsed_evs == events
                passed += 1
    finally:
        sys.path[:] = saved_path
    return passed


def _prove_hygiene_and_tigerstyle() -> int:
    assert not [p for p in (FAMILY / "build").rglob("*") if p.name == "__pycache__" or p.suffix == ".pyc"]
    built = sorted((FAMILY / "build").glob("*/*"))
    assert len(built) == 40
    for task_dir in built:
        assert (task_dir / "environment" / ".dockerignore").is_file() and (task_dir / "environment" / "seat" / ".dockerignore").is_file()
    py_files = [FAMILY / "generate.py", FAMILY / "reference.py", TEMPLATE / "world.py", TEMPLATE / "verify.py"]
    for path in py_files:
        text = path.read_text(encoding="utf-8")
        assert len(text.split("\n")) - (1 if text.endswith("\n") else 0) <= 600, path
        with path.open("rb") as handle:
            assert all(tok.type != tokenize.COMMENT for tok in tokenize.tokenize(handle.readline)), path
        tree = ast.parse(text, filename=str(path))
        assert ast.get_docstring(tree) is None, path
        funcs: dict[str, set[str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                assert ast.get_docstring(node) is None, (path, node.name)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                assert node.end_lineno is not None and node.end_lineno - node.lineno + 1 <= 70, (path, node.name)
                funcs[node.name] = {s.func.id for s in ast.walk(node) if isinstance(s, ast.Call) and isinstance(s.func, ast.Name)}
        for name, direct in funcs.items():
            frontier, visited = list(direct), set()
            for _ in range(len(funcs) + 2):
                if not frontier:
                    break
                cur = frontier.pop()
                assert cur != name, (path, name)
                if cur not in visited and cur in funcs:
                    visited.add(cur)
                    frontier.extend(funcs[cur])
    return len(py_files)


def prove(config: dict) -> int:
    total = (
        _prove_puzzles_and_scripts(config) + _prove_policies_and_cursors(config)
        + _prove_exploit_regressions(config) + _prove_tamper_and_guards(config)
        + _prove_live_http_and_regrade(config) + _prove_hygiene_and_tigerstyle()
    )
    print(f"crowdscale_v1 proofs: passed={total}, failed=0")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compute and verify crowdscale_v1 reference scores and proofs.")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--prove", action="store_true")
    args = parser.parse_args(argv)
    config = family()
    status = 0
    if args.write:
        text = reference_toml(reference(config))
        path = FAMILY / "family.toml"
        head = path.read_text(encoding="utf-8").split("[reference.")[0].rstrip() + "\n\n"
        path.write_text(head + text, encoding="utf-8")
        config = family()
    if args.check:
        status = max(status, check(config))
    if args.prove:
        status = max(status, prove(config))
    if not (args.write or args.check or args.prove):
        print(reference_toml(reference(config)), end="")
    return status


if __name__ == "__main__":
    sys.exit(main())
