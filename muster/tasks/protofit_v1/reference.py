from __future__ import annotations

import argparse
import hashlib
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
from typing import Any

sys.dont_write_bytecode = True

FAMILY = Path(__file__).resolve().parent
TEMPLATE = FAMILY / "template"
KIT = FAMILY.parent / "_kit_v2"
CONDITIONS = ("solo-x4", "indep-4", "team-4", "team-4-barrier")
POLICY_NAMES = ("oracle", "nop", "premature_committer", "budget_hoarder", "spoof")
ALL_SEATS = ("p1", "p2", "p3", "p4")
DOMAINS = (
    "bgp-route-session",
    "consensus-wal-envelope",
    "cxl-cache-coherency",
    "grpc-stream-multiplex",
    "kafka-partition-epoch",
    "nvme-fabric-queue",
    "quic-transport-framing",
    "raft-snapshot-wire",
    "rdma-credit-handshake",
    "tls-record-schedule",
)
LANE_SPECS = (
    {"bit": 0, "subsystem": "framing", "name": "crc_width_toggle", "note": "Selects 32-bit vs 64-bit frame trailer checksum envelope."},
    {"bit": 1, "subsystem": "framing", "name": "header_varint_mode", "note": "Controls compact varint vs aligned fixed-slot header packing."},
    {"bit": 2, "subsystem": "flow", "name": "credit_window_scale", "note": "Switches coarse vs fine receiver credit grant quantizer."},
    {"bit": 3, "subsystem": "flow", "name": "backpressure_watermark", "note": "Selects early vs deferred congestion pause threshold."},
    {"bit": 4, "subsystem": "recovery", "name": "selective_nack_bitmap", "note": "Enables sparse range NACK blocks vs cumulative replay."},
    {"bit": 5, "subsystem": "recovery", "name": "epoch_fence_strict", "note": "Requires monotonic generation tag on retransmit frames."},
    {"bit": 6, "subsystem": "security", "name": "key_phase_ratchet", "note": "Couples per-window AEAD nonce rotation to frame counter."},
    {"bit": 7, "subsystem": "security", "name": "Zero_copy_trailer_mac", "note": "Aligns detached MAC tag boundary with DMA scatter-gather."},
)


def _load_mod(name: str, path: Path, *, register: str | None = None) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    if register is not None:
        sys.modules[register] = mod
    spec.loader.exec_module(mod)
    return mod


codec: Any = _load_mod("codec", KIT / "codec.py", register="codec")
worldkit: Any = _load_mod("worldkit", KIT / "worldkit.py", register="worldkit")
build: Any = _load_mod("protofit_v1_build", KIT / "build.py")
world: Any = _load_mod("protofit_v1_world", TEMPLATE / "world.py")
verify: Any = _load_mod("protofit_v1_verify", TEMPLATE / "verify.py")
policies_mod: Any = _load_mod("protofit_v1_policies", FAMILY / "policies.py")
POLICIES: dict[str, Any] = dict(policies_mod.POLICIES)


def _digest(*parts: object) -> str:
    return hashlib.sha256("/".join(str(p) for p in parts).encode()).hexdigest()


def _rank_f2(rows: list[int]) -> int:
    basis: list[int] = []
    for val in rows:
        cur = int(val)
        for piv in basis:
            high = 1 << (piv.bit_length() - 1)
            if cur & high:
                cur ^= piv
        if cur:
            basis.append(cur)
            basis.sort(reverse=True)
    return len(basis)


def _sample_nonsingular_masks(rng: Random) -> list[int]:
    pool = [m for m in range(1, 256) if m.bit_count() >= 3]
    for _ in range(4096):
        cand = rng.sample(pool, 8)
        if _rank_f2(cand) == 8:
            return cand
    raise RuntimeError("failed to sample nonsingular F2 matrix")


def _local_valid_cfgs(constraints: list[dict[str, Any]], regime: int) -> list[int]:
    out: list[int] = []
    for cfg_id in range(256):
        if all(
            (((cfg_id & int(c["bit_mask"])).bit_count()) & 1)
            == (int(c["base_parity"]) ^ (((regime & int(c["regime_mask"])).bit_count()) & 1))
            for c in constraints
        ):
            out.append(cfg_id)
    return out


def _build_item(seed: str, n: int, domain: str) -> dict[str, Any]:
    h = _digest(seed, "layout", n)
    rng = Random(int(h[:16], 16))
    task_id = f"pf-{n:02d}-{h[16:22]}"
    active_regime = ((n * 3) + int(h[22:24], 16)) % 16
    regime_code = h[24:32]
    for _ in range(4096):
        bmasks = _sample_nonsingular_masks(rng)
        rmasks = [rng.randint(1, 15) for _ in range(8)]
        true_cfg = rng.randint(24, 240)
        constraints: list[dict[str, Any]] = []
        for idx in range(8):
            owner = ALL_SEATS[idx // 2]
            rm = rmasks[idx]
            bm = bmasks[idx]
            req_p = (true_cfg & bm).bit_count() & 1
            r_mod = (active_regime & rm).bit_count() & 1
            base_p = req_p ^ r_mod
            constraints.append({
                "constraint_id": f"c{idx + 1:02d}",
                "owner_seat": owner,
                "coupled_subsystems": f"{LANE_SPECS[idx]['subsystem']}->{LANE_SPECS[(idx + 3) % 8]['subsystem']}",
                "bit_mask": bm,
                "base_parity": base_p,
                "regime_mask": rm,
            })
        joint_valid = _local_valid_cfgs(constraints, active_regime)
        assert joint_valid == [true_cfg]
        by_s = {s: _local_valid_cfgs(constraints[2 * i : 2 * i + 2], active_regime) for i, s in enumerate(ALL_SEATS)}
        mins = {s: vals[0] for s, vals in by_s.items()}
        maxs = {s: vals[-1] for s, vals in by_s.items()}
        if (
            all(len(v) == 64 for v in by_s.values())
            and all(mins[s] != true_cfg and maxs[s] != true_cfg for s in ALL_SEATS)
            and len(set(mins.values())) >= 3
        ):
            target_digest = hashlib.sha256(
                f"{task_id}:{active_regime}:{regime_code}:{true_cfg:03d}".encode()
            ).hexdigest()[:16]
            return {
                "instance": n,
                "task_id": task_id,
                "domain": domain,
                "active_regime": active_regime,
                "regime_code": regime_code,
                "true_cfg": true_cfg,
                "target_digest": target_digest,
                "all_constraints": constraints,
            }
    raise RuntimeError("failed to construct coupled protocol instance")


def family() -> dict[str, Any]:
    cfg = tomllib.loads((FAMILY / "family.toml").read_text(encoding="utf-8"))
    assert cfg["name"] == "protofit_v1" and int(cfg["instances"]) == 10
    assert tuple(cfg["conditions"]) == CONDITIONS and tuple(cfg["policies"]) == POLICY_NAMES
    return cfg


def layouts(config: dict[str, Any]) -> list[dict[str, Any]]:
    seed, count = str(config["seed"]), int(config["instances"])
    assert count == len(DOMAINS) == 10
    ranked_domains = sorted(DOMAINS, key=lambda d: _digest(seed, "domain", d))
    items = [_build_item(seed, n, ranked_domains[n - 1]) for n in range(1, count + 1)]
    assert len({it["task_id"] for it in items}) == count
    assert len({it["active_regime"] for it in items}) >= 6
    return items


def _workspace_doc(task_id: str, domain: str, seat: str, local_c: list[dict[str, Any]]) -> dict[str, Any]:
    clean_c = [
        {
            "constraint_id": c["constraint_id"],
            "coupled_subsystems": c["coupled_subsystems"],
            "bit_mask": c["bit_mask"],
            "base_parity": c["base_parity"],
            "regime_mask": c["regime_mask"],
        }
        for c in local_c
    ]
    doc = {
        "task_id": task_id,
        "domain": domain,
        "seat": seat,
        "num_regimes": 16,
        "num_configs": 256,
        "parity_rule": "((cfg_id & bit_mask).bit_count() & 1) == (base_parity ^ ((regime & regime_mask).bit_count() & 1))",
        "digest_formula": "sha256(f'{task_id}:{regime}:{regime_code}:{cfg_id:03d}').hexdigest()[:16]",
        "lanes": [dict(spec) for spec in LANE_SPECS],
        "local_constraints": clean_c,
    }
    raw = json.dumps(doc, indent=2)
    assert len(raw.encode()) > 600
    return doc


def instances(config: dict[str, Any], condition: str) -> list[dict[str, Any]]:
    assert condition in CONDITIONS
    spec = config["conditions"][condition]
    seats = [str(s) for s in spec["seats"]]
    out: list[dict[str, Any]] = []
    for item in layouts(config):
        all_c = item["all_constraints"]
        if len(seats) == 1:
            seat_docs = {seats[0]: _workspace_doc(item["task_id"], item["domain"], seats[0], all_c)}
        else:
            seat_docs = {
                s: _workspace_doc(item["task_id"], item["domain"], s, [c for c in all_c if c["owner_seat"] == s])
                for s in seats
            }
            assert all(len(seat_docs[s]["local_constraints"]) == 2 for s in seats)
        out.append({
            "family": config["name"],
            "condition": condition,
            "instance": item["instance"],
            "seats": seats,
            "channel": bool(spec["channel"]),
            "barrier_enabled": bool(spec["barrier_enabled"]),
            "barrier_quorum": int(spec["barrier_quorum"]),
            "probe_pool": int(spec["probe_pool"]),
            "max_msg_bytes": int(spec["max_msg_bytes"]),
            "unread_guard_kinds": [str(x) for x in spec["unread_guard_kinds"]],
            "fenced": bool(spec["fenced"]),
            "task_id": item["task_id"],
            "domain": item["domain"],
            "active_regime": item["active_regime"],
            "regime_code": item["regime_code"],
            "true_cfg": item["true_cfg"],
            "target_digest": item["target_digest"],
            "all_constraints": [dict(c) for c in all_c],
            "seat_constraints": seat_docs,
        })
    return out


def tokens(config: dict[str, Any], instance: dict[str, Any]) -> dict[str, str]:
    return {str(s): str(build.token(str(config["seed"]), int(instance["instance"]), str(s))) for s in instance["seats"]}


def play(instance: dict[str, Any], seat_tokens: dict[str, str], policy: str = "oracle") -> tuple[list[dict[str, Any]], bytes, Any]:
    assert policy in POLICIES
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "events.jsonl"
        tok_to_seat = {tok: seat for seat, tok in seat_tokens.items()}
        game = world.ProtoFitWorld(instance, tok_to_seat, log_path)
        POLICIES[policy](game, instance, seat_tokens)
        game.close()
        raw_bytes = log_path.read_bytes()
        evs = verify._load_strict_events(log_path)
        return evs, raw_bytes, game


def simulate(instance: dict[str, Any], seat_tokens: dict[str, str], policy: str = "oracle") -> dict[str, float]:
    evs, _, _ = play(instance, seat_tokens, policy)
    return verify.score(evs, instance)


def reference(config: dict[str, Any]) -> dict[str, dict[str, dict[str, float]]]:
    table: dict[str, dict[str, dict[str, float]]] = {}
    for cond in CONDITIONS:
        table[cond] = {}
        inst_list = instances(config, cond)
        for pol in POLICY_NAMES:
            scores = [round(simulate(inst, tokens(config, inst), pol)["reward"], 6) for inst in inst_list]
            table[cond][pol] = {"mean": round(sum(scores) / len(scores), 6), "min": min(scores), "max": max(scores)}
    return table


def reference_toml(table: dict[str, dict[str, dict[str, float]]]) -> str:
    lines: list[str] = []
    for cond, pols in table.items():
        for pol, stats in pols.items():
            lines += [f"[reference.{cond}.{pol}]"] + [f"{k} = {json.dumps(stats[k])}" for k in ("mean", "min", "max")] + [""]
    return "\n".join(lines)


def check(config: dict[str, Any]) -> int:
    if config.get("reference") == reference(config):
        return 0
    print("family.toml [reference] differs: run reference.py --write", file=sys.stderr)
    return 1


def _assert_policy_row(cond: str, pol: str, results: list[dict[str, float]]) -> None:
    if pol == "oracle":
        exp = 1.0 if cond in ("solo-x4", "team-4", "team-4-barrier") else 0.0
        assert all(
            r["reward"] == exp and r["probed_regime"] == 1.0 and r["pool_starvation"] == 0.0
            and r["premature_commits"] == 0.0 and r["spoof_attempts"] == 0.0
            for r in results
        )
        if cond in ("team-4", "team-4-barrier"):
            assert all(r["probes_used"] <= 2.0 and r["barrier_satisfied"] == 1.0 and r["interface_mismatch"] == 0.0 for r in results)
    elif pol == "nop":
        assert all(r["reward"] == 0.0 and r["probes_used"] == 0.0 for r in results)
    elif pol == "premature_committer":
        assert all(r["reward"] == 0.0 for r in results)
        if cond in ("team-4", "team-4-barrier"):
            assert all(r["premature_commits"] >= 1.0 for r in results)
    elif pol == "budget_hoarder":
        assert all(r["reward"] == 0.0 and r["probes_used"] == 8.0 for r in results)
        if cond != "solo-x4":
            assert all(r["pool_starvation"] == 1.0 for r in results)
    else:
        assert pol == "spoof"
        assert all(r["reward"] == 0.0 and r["spoof_attempts"] >= 3.0 for r in results)


def _prove_matrix_and_isolation(config: dict[str, Any]) -> int:
    passed = 0
    for cond in CONDITIONS:
        inst_list = instances(config, cond)
        for inst in inst_list:
            for seat, ws_doc in inst["seat_constraints"].items():
                raw_ws = json.dumps(ws_doc)
                assert inst["regime_code"] not in raw_ws and inst["target_digest"] not in raw_ws
                assert all(c_nm not in raw_ws for c_nm in CONDITIONS)
                if len(inst["seats"]) > 1:
                    own_ids = {c["constraint_id"] for c in inst["all_constraints"] if c["owner_seat"] == seat}
                    got_ids = {c["constraint_id"] for c in ws_doc["local_constraints"]}
                    assert got_ids == own_ids and len(got_ids) == 2
            passed += 1
        for pol in POLICY_NAMES:
            results = [simulate(inst, tokens(config, inst), pol) for inst in inst_list]
            assert all(r["chain_ok"] == 1.0 and r["script_faithful"] == 1.0 for r in results)
            _assert_policy_row(cond, pol, results)
            passed += len(results)
    return passed


def _prove_tamper_and_fences(config: dict[str, Any]) -> int:
    passed = 0
    sample = instances(config, "team-4-barrier")[0]
    toks = tokens(config, sample)
    evs_o, raw_o, _ = play(sample, toks, "oracle")
    assert verify.score(evs_o, sample)["reward"] == 1.0
    with tempfile.TemporaryDirectory() as tmp:
        flip_p = Path(tmp) / "flip.jsonl"
        for pos in range(0, len(raw_o), 13):
            mut = bytearray(raw_o)
            mut[pos] ^= 0x01
            flip_p.write_bytes(bytes(mut))
            ok = True
            try:
                res = verify.score(verify._load_strict_events(flip_p), sample)
                ok = res["chain_ok"] == 1.0 and res["reward"] == 1.0
            except (UnicodeDecodeError, ValueError):
                ok = False
            assert not ok
            passed += 1
        tampered = json.loads(json.dumps(evs_o))
        tampered[0]["response"]["probe_pool"] = 99
        prev = codec.GENESIS_HASH
        for ev in tampered:
            ev["prev_hash"] = prev
            ev["entry_hash"] = prev = codec.compute_entry_hash(prev, ev)
        t_res = verify.score(tampered, sample)
        assert t_res["chain_ok"] == 1.0 and t_res["script_faithful"] == 0.0 and t_res["reward"] == 0.0
        srv = world.ProtoFitWorld(sample, {v: k for k, v in toks.items()}, Path(tmp) / "fence.jsonl")
        st_m, _ = srv.dispatch("p1", "POST", "/msg", {"text": "hello"})
        st_u, r_u = srv.dispatch("p2", "POST", "/commit", {"phase": 1, "contract_digest": sample["target_digest"], "regime": sample["active_regime"]})
        srv.dispatch("p2", "GET", "/msgs")
        st_q, r_q = srv.dispatch("p2", "POST", "/commit", {"phase": 1, "contract_digest": sample["target_digest"], "regime": sample["active_regime"]})
        assert st_m == 200 and st_u == 409 and r_u["error"] == "unread_messages"
        assert st_q == 409 and r_q["error"] == "quorum_not_ready"
    return passed + 3


def _run_live_http_episode(inst: dict[str, Any], seat_tokens: dict[str, str], pol: str) -> dict[str, float]:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        log_path = tmp_p / "live.jsonl"
        srv = world.ProtoFitWorld(inst, {v: k for k, v in seat_tokens.items()}, log_path)
        httpd = worldkit.make_server(srv, 0)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            procs: list[subprocess.Popen[str]] = []
            for seat in inst["seats"]:
                ws_file = tmp_p / f"ws_{seat}.json"
                ws_file.write_text(json.dumps(inst["seat_constraints"][seat], indent=2) + "\n", encoding="utf-8")
                env = {
                    **os.environ,
                    "WORLD": f"http://127.0.0.1:{port}",
                    "SEAT": seat,
                    "SEAT_TOKEN": seat_tokens[seat],
                    "WORKSPACE_FILE": str(ws_file),
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
                procs.append(subprocess.Popen(
                    ["bash", str(TEMPLATE / "policies" / f"{pol}.sh")],
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                ))
            for proc in procs:
                _, stderr = proc.communicate(timeout=25)
                assert proc.returncode == 0, stderr
            worldkit.close_world(port)
        finally:
            httpd.shutdown()
            httpd.server_close()
        return verify.score(verify._load_strict_events(log_path), inst)


def _prove_live_and_audit(config: dict[str, Any]) -> int:
    passed = 0
    for cond in CONDITIONS:
        inst = instances(config, cond)[0]
        toks = tokens(config, inst)
        for pol in POLICY_NAMES:
            live_res = _run_live_http_episode(inst, toks, pol)
            sim_res = simulate(inst, toks, pol)
            assert live_res["reward"] == sim_res["reward"] and live_res["chain_ok"] == 1.0 and live_res["script_faithful"] == 1.0
            passed += 1
    repo_root = str(FAMILY.parents[2])
    added = repo_root not in sys.path
    if added:
        sys.path.insert(0, repo_root)
    try:
        from muster import regrade
        from worldwatch import eval_audit
    finally:
        if added and repo_root in sys.path:
            sys.path.remove(repo_root)
    if (FAMILY / "build" / "team-4" / "protofit_v1-i01" / "task.toml").is_file():
        rep, rc = eval_audit.run_certify_suite(FAMILY)
        assert rc == 0 and rep["passed"] is True and rep["replay"]["sk_oracle_false_positives"] == []
        btask = FAMILY / "build" / "team-4-barrier" / "protofit_v1-i01"
        score_fn, b_inst, err = regrade.load_score(btask)
        assert err is None and score_fn is not None and b_inst is not None
        evs_b, _, _ = play(b_inst, tokens(config, b_inst), "oracle")
        rews_b = verify.score(evs_b, b_inst)
        reg = regrade.regrade_trial(rews_b, evs_b, evs_b, score_fn, b_inst)
        assert reg["status"] == "match" and not reg["differences"]
        passed += 2
    return passed


def prove(config: dict[str, Any]) -> int:
    total = _prove_matrix_and_isolation(config) + _prove_tamper_and_fences(config) + _prove_live_and_audit(config)
    print(f"protofit_v1 proofs: passed={total}, failed=0")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    for flag in ("--write", "--check", "--prove"):
        parser.add_argument(flag, action="store_true")
    args, config = parser.parse_args(argv), family()
    if args.write:
        path = FAMILY / "family.toml"
        head = path.read_text(encoding="utf-8").split("[reference.")[0].rstrip("\n")
        path.write_text(head + "\n\n" + reference_toml(reference(config)), encoding="utf-8")
        return 0
    if not args.check and not args.prove:
        print(reference_toml(reference(config)), end="")
        return 0
    status = check(config) if args.check else 0
    return max(status, prove(config)) if (args.prove and status == 0) else status


if __name__ == "__main__":
    sys.exit(main())
