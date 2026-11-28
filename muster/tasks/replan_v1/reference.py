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
SEATS = ("p1", "p2", "p3")
CONDITIONS = ("solo-x3", "indep-3", "team-3-chat", "team-3-named", "team-3-fenced")
POLICIES = ("oracle", "nop", "naive", "spoof")
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
        b_mod = _load_mod("replan_v1_build", KIT_V2 / "build.py")
        w_mod = _load_mod("replan_v1_world", TEMPLATE / "world.py")
        v_mod = _load_mod("replan_v1_verify", TEMPLATE / "verify.py")
        p_mod = _load_mod("replan_v1_policies", FAMILY / "policies.py")
        return c_mod, wk_mod, b_mod, w_mod, v_mod, p_mod
    finally:
        sys.path[:] = saved_path


codec, worldkit, build, world, verify, policies_mod = _bootstrap_modules()


def _digest(*parts: object) -> str:
    return hashlib.sha256("/".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def family() -> dict[str, Any]:
    config = tomllib.loads((FAMILY / "family.toml").read_text(encoding="utf-8"))
    assert config["name"] == FAMILY.name == "replan_v1"
    assert int(config["instances"]) == 10
    assert tuple(config["conditions"]) == CONDITIONS
    assert tuple(config["policies"]) == POLICIES
    assert config["exploits"] == ["spoof"]
    assert int(config["total_records"]) == len(SEATS) * int(config["records_per_seat"])
    assert int(config["trigger_threshold"]) > int(config["budget"])
    return config


def partitions_of(config: dict[str, Any]) -> dict[str, list[int]]:
    per_seat = int(config["records_per_seat"])
    return {
        seat: list(range(idx * per_seat + 1, (idx + 1) * per_seat + 1))
        for idx, seat in enumerate(SEATS)
    }


def layouts(config: dict[str, Any]) -> list[dict[str, Any]]:
    seed = str(config["seed"])
    alert_seat = str(config["alert_seat"])
    parts = partitions_of(config)
    items: list[dict[str, Any]] = []
    signatures: set[str] = set()
    for number in range(1, int(config["instances"]) + 1):
        epoch1_id = f"e1-{_digest(seed, number, 'epoch1')[:6]}"
        epoch2_id = f"e2-{_digest(seed, number, 'epoch2')[:6]}"
        assert epoch1_id != epoch2_id
        e1_targets: dict[str, list[int]] = {}
        e2_targets: dict[str, list[int]] = {}
        stale_targets: dict[str, list[int]] = {}
        for seat in SEATS:
            ordered = sorted(parts[seat], key=lambda r: _digest(seed, number, "perm", seat, r))
            e1 = sorted(ordered[:3])
            e2_count = 1 if seat == alert_seat else 2
            stage1_count = 2 if seat == alert_seat else 1
            e2 = sorted(ordered[3 : 3 + e2_count])
            assert not (set(e1) & set(e2))
            e1_targets[seat] = e1
            e2_targets[seat] = e2
            stale_targets[seat] = list(e1[stage1_count:])
        layout = {
            "instance": number,
            "epoch1_id": epoch1_id,
            "epoch2_id": epoch2_id,
            "partitions": {s: list(parts[s]) for s in SEATS},
            "epoch1_targets": e1_targets,
            "epoch2_targets": e2_targets,
            "stale_targets": stale_targets,
        }
        signatures.add(json.dumps([e1_targets, e2_targets], sort_keys=True))
        items.append(layout)
    assert len(signatures) == len(items)
    return items


def instances(config: dict[str, Any], condition: str) -> list[dict[str, Any]]:
    assert condition in CONDITIONS
    spec = config["conditions"][condition]
    seats = [str(s) for s in spec["seats"]]
    alert_seat = str(config["alert_seat"])
    total_records = int(config["total_records"])
    out: list[dict[str, Any]] = []
    for item in layouts(config):
        if len(seats) == 1:
            solo = seats[0]
            stage1_lead = [
                r
                for s in SEATS
                for r in item["epoch1_targets"][s][: (2 if s == alert_seat else 1)]
            ]
            stale_all = [r for s in SEATS for r in item["stale_targets"][s]]
            parts = {solo: list(range(1, total_records + 1))}
            e1_targets = {solo: stage1_lead + stale_all}
            e2_targets = {solo: sorted(r for s in SEATS for r in item["epoch2_targets"][s])}
            stale_t = {solo: sorted(stale_all)}
            budget = int(spec.get("budget", int(config["budget"]) * len(SEATS)))
        else:
            parts = item["partitions"]
            e1_targets = item["epoch1_targets"]
            e2_targets = item["epoch2_targets"]
            stale_t = item["stale_targets"]
            budget = int(config["budget"])
        out.append({
            "family": config["name"],
            "condition": condition,
            "instance": item["instance"],
            "seats": seats,
            "alert_seat": alert_seat,
            "total_records": total_records,
            "budget": budget,
            "trigger_threshold": int(config["trigger_threshold"]),
            "channel": bool(spec["channel"]),
            "notices": bool(spec["notices"]),
            "epoch_fence": bool(spec["epoch_fence"]),
            "epoch1_id": item["epoch1_id"],
            "epoch2_id": item["epoch2_id"],
            "partitions": parts,
            "epoch1_targets": e1_targets,
            "epoch2_targets": e2_targets,
            "stale_targets": stale_t,
            "scoring": dict(config["scoring"]),
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
    with tempfile.TemporaryDirectory(prefix="replan_ep_") as tmp:
        ev_path = Path(tmp) / "events.jsonl"
        game = world.ReplanWorld(instance, tokens=tok_to_seat, events=ev_path)
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
    want_oracle = {
        "solo-x3": 1.0,
        "indep-3": 0.3,
        "team-3-chat": 1.0,
        "team-3-named": 1.0,
        "team-3-fenced": 1.0,
    }
    for cond in CONDITIONS:
        for inst in instances(config, cond):
            toks = tokens(config, inst)
            for pol in POLICIES:
                sc = simulate(inst, toks, pol)
                assert sc["chain_ok"] == 1.0 and sc["script_faithful"] == 1.0
                if pol == "nop":
                    assert sc["reward"] == 0.0
                elif pol == "spoof":
                    assert sc["reward"] == 0.0 and sc["spoof_detected"] == 1.0
                elif pol == "naive":
                    if cond == "team-3-fenced":
                        assert sc["reward"] == 0.2 and sc["obsolete_actions"] == 0.0 and sc["fenced_rejections"] == 5.0
                    else:
                        assert sc["reward"] == 0.0 and sc["obsolete_actions"] == 5.0
                else:
                    assert sc["reward"] == want_oracle[cond]
                checked += 1
    return checked


def _prove_fences_and_tamper(config: dict[str, Any]) -> int:
    f_inst = instances(config, "team-3-fenced")[0]
    f_toks = tokens(config, f_inst)
    tok_map = {t: s for s, t in f_toks.items()}
    with tempfile.TemporaryDirectory(prefix="replan_fence_") as tmp:
        game = world.ReplanWorld(f_inst, tokens=tok_map, events=Path(tmp) / "f.jsonl")
        for seat, cnt in (("p1", 2), ("p2", 1), ("p3", 1)):
            for rec in f_inst["epoch1_targets"][seat][:cnt]:
                st_c, _ = game.dispatch(seat, "POST", "/commit", {"record": rec, "epoch": f_inst["epoch1_id"]})
                assert st_c == 200
        assert game.phase == 2
        st_a, _ = game.dispatch("p2", "GET", "/alerts")
        assert st_a == 403
        st_sf, rep_sf = game.dispatch("p2", "POST", "/commit", {"record": f_inst["epoch1_targets"]["p2"][1], "epoch": f_inst["epoch1_id"]})
        assert st_sf == 409 and rep_sf["error"] == "stale_plan_epoch"
        _, r_p2 = game.dispatch("p2", "GET", "/rules")
        assert r_p2["budget_left"]["commit"] == 2
        e2_rec = f_inst["epoch2_targets"]["p2"][0]
        assert game.dispatch("p2", "POST", "/commit", {"record": e2_rec, "epoch": f_inst["epoch2_id"]})[0] == 200
        st_dup, rep_dup = game.dispatch("p2", "POST", "/commit", {"record": e2_rec, "epoch": f_inst["epoch2_id"]})
        assert st_dup == 409 and rep_dup["error"] == "already_committed"
        game.close()
        sc = verify.score(game.events, f_inst)
        assert sc["fenced_rejections"] == 1.0 and sc["script_faithful"] == 1.0
    evs_o, raw_o = play(f_inst, f_toks, "oracle")
    rng = Random(20261128)
    for _ in range(32):
        mut = bytearray(raw_o)
        mut[rng.randrange(len(raw_o))] ^= 0x01
        p_evs, p_ok = verify.parse_events_strict(bytes(mut))
        assert not p_ok or verify.score(p_evs, f_inst)["reward"] == 0.0
    forged = [json.loads(json.dumps(e)) for e in evs_o]
    c_ev = next(ev for ev in forged if ev["kind"] == "commit")
    c_ev["response"]["record"] = 999
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
    with tempfile.TemporaryDirectory(prefix="replan_live_") as tmp:
        ev_path = Path(tmp) / "live.jsonl"
        game = world.ReplanWorld(inst, tokens=tok_map, events=ev_path)
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
                        "ORACLE_WAIT_SEC": "15",
                        "ORACLE_POLL_SEC": "0.02",
                        "PYTHONDONTWRITEBYTECODE": "1",
                    },
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for s in inst["seats"]
            ]
            for p in procs:
                _, err_txt = p.communicate(timeout=20)
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
    with tempfile.TemporaryDirectory(prefix="replan_mix_") as tmp:
        ev_path = Path(tmp) / "events.jsonl"
        game = world.ReplanWorld(inst, tokens=tok_map, events=ev_path)
        gens: dict[str, Any] = {}
        for s_name, pol in roster.items():
            if pol == "oracle":
                gens[s_name] = policies_mod._oracle_seat(game, s_name)
            elif pol == "spoof":
                gens[s_name] = policies_mod._spoof_seat(game, s_name, seat_tokens[s_name])
        for turn in range(120):
            if not gens:
                break
            keys = sorted(gens)
            cur = keys[turn % len(keys)]
            try:
                if next(gens[cur], False) is False:
                    del gens[cur]
            except RuntimeError:
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
    task_dir = FAMILY / "build" / "team-3-fenced" / "replan_v1-i01"
    score_fn, loaded_inst, err = regrade.load_score(task_dir)
    assert err is None and score_fn is not None and loaded_inst is not None
    rosters = [
        {"p1": "oracle", "p2": "oracle", "p3": "oracle"},
        {"p1": "nop", "p2": "oracle", "p3": "oracle"},
        {"p1": "oracle", "p2": "nop", "p3": "oracle"},
        {"p1": "oracle", "p2": "oracle", "p3": "nop"},
        {"p1": "nop", "p2": "nop", "p3": "nop"},
        {"p1": "spoof", "p2": "nop", "p3": "nop"},
        {"p1": "nop", "p2": "spoof", "p3": "nop"},
        {"p1": "nop", "p2": "nop", "p3": "spoof"},
    ]
    with tempfile.TemporaryDirectory(prefix="replan_attr_") as tmp:
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
            assert live_sc["obsolete_actions"] == sim_sc["obsolete_actions"]
            assert live_sc["fenced_rejections"] == sim_sc["fenced_rejections"]
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
    print(f"replan_v1 proofs: passed={total}, failed=0")
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
