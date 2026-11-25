from __future__ import annotations

import argparse
import hashlib
import importlib.util
import itertools
import json
import os
import subprocess
import sys
import tempfile
import threading
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

sys.dont_write_bytecode = True

FAMILY = Path(__file__).resolve().parent
TEMPLATE = FAMILY / "template"
KIT = FAMILY.parent / "_kit_v2"
CONDITIONS = ("solo-x3", "indep-3", "team-3", "team-3-fenced")
POLICIES = ("oracle", "nop", "stale_snapshot", "rumor_auditor", "spoof")
DOMAINS = (
    "clearinghouse-margin",
    "correspondent-nostro",
    "custody-collateral",
    "escrow-disbursement",
    "fx-netting-pool",
    "interbank-liquidity",
    "omnibus-reserve",
    "prime-broker-sweep",
    "repo-triparty",
    "treasury-drawdown",
)
REQ_LEVELS = (92, 96, 100, 104, 108, 112, 116, 120)
SLACK_LEVELS = (144, 152, 160, 168, 176)


def _load_mod(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bootstrap_modules() -> tuple[ModuleType, ModuleType, ModuleType, ModuleType, ModuleType, ModuleType]:
    saved = list(sys.path)
    sys.path.insert(0, str(KIT))
    try:
        c_mod = _load_mod("codec", KIT / "codec.py")
        sys.modules["codec"] = c_mod
        wk_mod = _load_mod("worldkit", KIT / "worldkit.py")
        sys.modules["worldkit"] = wk_mod
        b_mod = _load_mod("skewledger_build", KIT / "build.py")
        w_mod = _load_mod("skewledger_world", TEMPLATE / "world.py")
        v_mod = _load_mod("skewledger_verify", TEMPLATE / "verify.py")
        p_mod = _load_mod("skewledger_policies", FAMILY / "policies.py")
        return c_mod, wk_mod, b_mod, w_mod, v_mod, p_mod
    finally:
        sys.path[:] = saved


codec, worldkit, build, world, verify, policies_mod = _bootstrap_modules()


def _digest(*parts: object) -> str:
    return hashlib.sha256("/".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def family() -> dict[str, Any]:
    cfg = tomllib.loads((FAMILY / "family.toml").read_text(encoding="utf-8"))
    assert cfg["name"] == FAMILY.name == "skewledger_v1" and int(cfg["instances"]) == 10
    assert tuple(cfg["conditions"]) == CONDITIONS and tuple(cfg["policies"]) == POLICIES
    return cfg


def _draw_rates(seed: str, idx: int) -> tuple[str, str, str]:
    h = _digest(seed, "rates", idx)
    b1 = 110 + (int(h[0:6], 16) % 250)
    b2 = 380 + (int(h[6:12], 16) % 250)
    b3 = 660 + (int(h[12:18], 16) % 300)
    r_init, r_post, r_rumor = f"RATE-{b1}-BPS", f"RATE-{b2}-BPS", f"RATE-{b3}-BPS"
    assert len({r_init, r_post, r_rumor}) == 3
    return r_init, r_post, r_rumor


def layouts(config: dict[str, Any]) -> list[dict[str, Any]]:
    seed = str(config["seed"])
    count = int(config["instances"])
    assert count == len(DOMAINS) == 10
    ranked_domains = sorted(DOMAINS, key=lambda d: _digest(seed, "domain", d))
    items: list[dict[str, Any]] = []
    for n in range(1, count + 1):
        d = _digest(seed, "item", n)
        bal_a = 240 + 4 * (int(d[6:10], 16) % 25)
        bal_b = 240 + 4 * (int(d[10:14], 16) % 25)
        slack = SLACK_LEVELS[int(d[14:18], 16) % len(SLACK_LEVELS)]
        idx_a = (n - 1 + (int(d[18:22], 16) % 3)) % len(REQ_LEVELS)
        idx_b = (idx_a + 1 + (int(d[22:26], 16) % (len(REQ_LEVELS) - 1))) % len(REQ_LEVELS)
        req_a, req_b = REQ_LEVELS[idx_a], REQ_LEVELS[idx_b]
        floor = bal_a + bal_b - slack
        assert req_a + req_b > slack and (req_a - req_b + slack) % 2 == 0
        r_init, r_post, r_rumor = _draw_rates(seed, n)
        items.append({
            "instance": n,
            "task_id": f"skew-{n:02d}-{d[:6]}",
            "domain": ranked_domains[n - 1],
            "init_bal_a": bal_a,
            "init_bal_b": bal_b,
            "reserve_floor": floor,
            "available_slack": slack,
            "req_a": req_a,
            "req_b": req_b,
            "initial_rate": r_init,
            "post_transfer_rate": r_post,
            "rumor_rate": r_rumor,
        })
    assert len({it["task_id"] for it in items}) == count
    assert len({it["req_a"] - it["req_b"] for it in items}) >= 5
    return items


def instances(config: dict[str, Any], condition: str) -> list[dict[str, Any]]:
    assert condition in CONDITIONS
    spec = config["conditions"][condition]
    out: list[dict[str, Any]] = []
    for item in layouts(config):
        out.append({
            "family": str(config["name"]),
            "condition": condition,
            "instance": int(item["instance"]),
            "seats": [str(s) for s in spec["seats"]],
            "channel": bool(spec["channel"]),
            "notices_enabled": bool(spec.get("notices_enabled", True)),
            "leases_enabled": bool(spec.get("leases_enabled", False)),
            "unread_guard_kinds": [str(x) for x in spec.get("unread_guard_kinds", [])],
            "fenced": bool(spec.get("fenced", False)),
            **{k: item[k] for k in (
                "task_id", "domain", "init_bal_a", "init_bal_b", "reserve_floor",
                "available_slack", "req_a", "req_b", "initial_rate", "post_transfer_rate", "rumor_rate",
            )},
        })
    return out


def tokens(config: dict[str, Any], instance: dict[str, Any]) -> dict[str, str]:
    salt = f"{config['seed']}/{instance['condition']}"
    return {str(s): build.token(salt, int(instance["instance"]), str(s)) for s in instance["seats"]}


def replay(
    instance: dict[str, Any],
    seat_tokens: dict[str, str],
    policy: str = "oracle",
    *,
    seat_order: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    assert policy in POLICIES
    tok_to_seat = {tok: seat for seat, tok in seat_tokens.items()}
    with tempfile.TemporaryDirectory(prefix="skewledger-ep-") as tmp:
        ev_path = Path(tmp) / "events.jsonl"
        game = world.SkewLedgerWorld(instance, tok_to_seat, ev_path)
        fn = policies_mod.POLICIES[policy]
        if seat_order is not None and policy in ("oracle", "stale_snapshot", "rumor_auditor"):
            fn(game, instance, seat_order)
        else:
            fn(game, instance)
        game.close()
        raw_bytes = ev_path.read_bytes()
        evs = codec.parse_events_jsonl(raw_bytes)
        return {"events": evs, "raw_bytes": raw_bytes, "world": game}


def simulate(
    instance: dict[str, Any],
    seat_tokens: dict[str, str],
    policy: str = "oracle",
    *,
    seat_order: tuple[str, ...] | None = None,
) -> dict[str, float]:
    ep = replay(instance, seat_tokens, policy, seat_order=seat_order)
    return verify.score(ep["events"], instance)


def reference(config: dict[str, Any]) -> dict[str, dict[str, dict[str, float]]]:
    table: dict[str, dict[str, dict[str, float]]] = {}
    for cond in CONDITIONS:
        table[cond] = {}
        inst_list = instances(config, cond)
        for pol in POLICIES:
            rews = [round(simulate(inst, tokens(config, inst), pol)["reward"], 6) for inst in inst_list]
            table[cond][pol] = {
                "mean": round(sum(rews) / len(rews), 6),
                "min": min(rews),
                "max": max(rews),
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


def _assert_matrix(config: dict[str, Any]) -> int:
    expected = {
        ("solo-x3", "oracle"): 1.0,
        ("indep-3", "oracle"): 0.0,
        ("team-3", "oracle"): 1.0,
        ("team-3-fenced", "oracle"): 1.0,
        ("solo-x3", "rumor_auditor"): 1.0,
        ("indep-3", "rumor_auditor"): 0.0,
        ("team-3", "rumor_auditor"): 0.0,
        ("team-3-fenced", "rumor_auditor"): 0.0,
    }
    count = 0
    for cond in CONDITIONS:
        for inst in instances(config, cond):
            toks = tokens(config, inst)
            for pol in POLICIES:
                res = simulate(inst, toks, pol)
                want = expected.get((cond, pol), 0.0)
                assert res["reward"] == want and res["chain_ok"] == 1.0 and res["script_faithful"] == 1.0
                if cond == "indep-3" and pol == "oracle":
                    assert res["write_skew_breach"] == 1.0 and res["reserve_preserved"] == 0.0
                if cond in ("team-3", "team-3-fenced") and pol == "stale_snapshot":
                    assert res["stale_commits"] >= 1.0
                if cond in ("team-3", "team-3-fenced") and pol == "rumor_auditor":
                    assert res["rumor_adopted"] == 1.0
                if pol == "spoof":
                    assert res["spoof_attempts"] >= 3.0
                count += 1
    return count


def _prove_permutations_and_tamper(config: dict[str, Any]) -> int:
    count = 0
    for cond in ("team-3", "team-3-fenced"):
        for inst in instances(config, cond):
            toks = tokens(config, inst)
            for perm in itertools.permutations(inst["seats"]):
                res = simulate(inst, toks, "oracle", seat_order=tuple(perm))
                assert res["reward"] == 1.0 and res["stale_commits"] == 0.0
                count += 1
    sample = instances(config, "team-3-fenced")[0]
    ep = replay(sample, tokens(config, sample), "oracle")
    raw = ep["raw_bytes"]
    for pos in range(0, len(raw), 19):
        flipped = bytearray(raw)
        flipped[pos] ^= 0x01
        ok = True
        try:
            evs_f = codec.parse_events_jsonl(bytes(flipped))
            sc_f = verify.score(evs_f, sample)
            ok = sc_f["chain_ok"] == 1.0 and sc_f["reward"] == 1.0
        except (UnicodeDecodeError, ValueError):
            ok = False
        assert not ok
        count += 1
    forged = json.loads(json.dumps(ep["events"]))
    for ev in forged:
        if ev.get("kind") == "transfer" and ev.get("ok") is True:
            ev["response"]["bal_a"] = int(ev["response"]["bal_a"]) + 4
            break
    prev = codec.GENESIS_HASH
    for ev in forged:
        ev["prev_hash"] = prev
        ev["entry_hash"] = prev = codec.compute_entry_hash(prev, ev)
    f_res = verify.score(forged, sample)
    assert f_res["chain_ok"] == 1.0 and f_res["script_faithful"] == 0.0 and f_res["reward"] == 0.0
    indep_items = instances(config, "indep-3")
    for ga in REQ_LEVELS:
        hits = sum(
            1 for it in indep_items
            if (int(it["req_a"]) - int(it["req_b"]) + int(it["available_slack"])) // 2 == ga
        )
        assert hits <= 3
    return count + 2


def _bind_live_client_gate(srv: Any) -> list[bool]:
    allow_loopback = [False]
    orig_handle = srv.handle

    def _gated_handle(
        method: Any, path: Any, token: str | None, body: Any, client: str, **kw: Any
    ) -> tuple[int, dict[str, Any]]:
        eff = client if (allow_loopback[0] and token is None) else worldkit.REMOTE_CLIENT
        return orig_handle(method, path, token, body, eff, **kw)

    srv.handle = _gated_handle
    return allow_loopback


def _run_live_shell_episode(inst: dict[str, Any], seat_tokens: dict[str, str], pol: str) -> dict[str, float]:
    tok_to_seat = {tok: seat for seat, tok in seat_tokens.items()}
    with tempfile.TemporaryDirectory(prefix="skewledger-live-") as tmp:
        ev_path = Path(tmp) / "live.jsonl"
        srv = world.SkewLedgerWorld(inst, tok_to_seat, ev_path)
        allow_loopback = _bind_live_client_gate(srv)
        httpd = worldkit.make_server(srv, 0)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
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
                _, stderr_txt = p.communicate(timeout=20)
                assert p.returncode == 0, stderr_txt
            allow_loopback[0] = True
            close_doc = worldkit.close_world(port)
            assert close_doc.get("closed") is True and close_doc.get("log_intact") is True
        finally:
            httpd.shutdown()
            httpd.server_close()
        if pol == "spoof":
            assert srv.spoof_attempts >= 3
        return verify.score(verify.load_strict_events(ev_path), inst)


def _prove_live_shells(config: dict[str, Any]) -> int:
    count = 0
    for cond in CONDITIONS:
        inst = instances(config, cond)[0]
        toks = tokens(config, inst)
        for pol in POLICIES:
            live_res = _run_live_shell_episode(inst, toks, pol)
            mem_res = simulate(inst, toks, pol)
            assert live_res["reward"] == mem_res["reward"] and live_res["chain_ok"] == 1.0 and live_res["script_faithful"] == 1.0
            if pol == "spoof":
                assert live_res["spoof_attempts"] >= 3.0
            if cond in ("team-3", "team-3-fenced") and pol == "stale_snapshot":
                assert live_res["stale_commits"] >= 1.0
            if cond in ("team-3", "team-3-fenced") and pol == "rumor_auditor":
                assert live_res["rumor_adopted"] == 1.0
            count += 1
    return count


def prove(config: dict[str, Any]) -> int:
    total = _assert_matrix(config) + _prove_permutations_and_tamper(config) + _prove_live_shells(config)
    print(f"skewledger_v1 proofs: passed={total}, failed=0")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    for flag in ("--write", "--check", "--prove"):
        parser.add_argument(flag, action="store_true")
    args, cfg = parser.parse_args(argv), family()
    if args.write:
        path = FAMILY / "family.toml"
        head = path.read_text(encoding="utf-8").split("[reference.")[0].rstrip("\n")
        path.write_text(head + "\n\n" + reference_toml(reference(cfg)), encoding="utf-8")
        return 0
    if not args.check and not args.prove:
        print(reference_toml(reference(cfg)), end="")
        return 0
    status = check(cfg) if args.check else 0
    return max(status, prove(cfg)) if (args.prove and status == 0) else status


if __name__ == "__main__":
    sys.exit(main())
