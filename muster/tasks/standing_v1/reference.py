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
from types import ModuleType

sys.dont_write_bytecode = True

FAMILY = Path(__file__).resolve().parent
TEMPLATE = FAMILY / "template"
KIT = FAMILY.parent / "_kit"
CONDITIONS = (
    "none-ctrl", "creditor-cancel", "peer-release",
    "manager-release", "nospeaker-cancel", "relayed-verify",
)
POLICIES = ("oracle", "always_execute", "always_drop", "nop", "spoof")
DOMAINS = (
    "batch-settlement", "cache-cutover", "index-compaction", "ledger-snapshot", "quota-rebalance",
    "replica-promotion", "route-drain", "schema-checkpoint", "shard-handoff", "vault-rotation",
)
ACTORS = {
    "p1": {"role": "promisor", "rank": "operator"},
    "p_cred": {"role": "creditor", "rank": "operator"},
    "p_peer": {"role": "peer", "rank": "operator"},
    "p_mgr": {"role": "manager", "rank": "lead_manager"},
}
FAULTS = ("refused", "reset-before", "reset-after", "timeout-after", "disconnect-after")
WORLD_URL = "http://world:8080"
REMOTE = "10.0.0.2"
MAX_POLICY_STEPS = 256
FAULT_RATE = 0.30
MAX_FAULT_BURST = 3
_POLICY_CACHE: dict[str, ModuleType] = {}


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build = _load_module("standing_v1_kit_build", KIT / "build.py")
worldkit = _load_module("worldkit", KIT / "worldkit.py")
sys.modules["worldkit"] = worldkit
world = _load_module("standing_v1_world", TEMPLATE / "world.py")
verify = _load_module("standing_v1_verify", TEMPLATE / "verify.py")


def _digest(*parts: object) -> str:
    return hashlib.sha256("/".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def family() -> dict:
    config = tomllib.loads((FAMILY / "family.toml").read_text(encoding="utf-8"))
    assert config["name"] == FAMILY.name == "standing_v1" and int(config["instances"]) == 10
    assert tuple(config["conditions"]) == CONDITIONS and tuple(config["policies"]) == POLICIES
    assert config["exploits"] == ["spoof"]
    return config


def layouts(config: dict) -> list[dict]:
    seed, count = str(config["seed"]), int(config["instances"])
    assert count == len(DOMAINS) == 10
    ranked_domains = sorted(DOMAINS, key=lambda d: _digest(seed, "domain", d))
    truth_order = sorted(range(1, count + 1), key=lambda n: _digest(seed, "relayed", n))
    true_set = set(truth_order[: count // 2])
    assert len(true_set) == 5
    assert true_set not in ({1, 2, 3, 4, 5}, {6, 7, 8, 9, 10}, {1, 3, 5, 7, 9}, {2, 4, 6, 8, 10})
    items = [
        {
            "instance": n,
            "commitment_id": f"cmt-{n:02d}-{_digest(seed, 'item', n)[:6]}",
            "target_id": f"tgt-{n:02d}-{_digest(seed, 'item', n)[6:12]}",
            "domain": ranked_domains[n - 1],
            "relayed_released": n in true_set,
        }
        for n in range(1, count + 1)
    ]
    assert len({it["commitment_id"] for it in items}) == count
    assert len({it["target_id"] for it in items}) == count
    return items


def _stimulus_spec(condition: str, cid: str, tid: str, relayed_released: bool) -> dict:
    table: dict[str, tuple[bool, str | None, str | None, str | None, str | None, bool, str]] = {
        "none-ctrl": (False, None, None, None, None, False, "execute"),
        "creditor-cancel": (
            True, "p_cred", "p_cred", "creditor",
            f"[{cid}] Release notice from p_cred: window superseded for {tid}; as creditor I release p1 from commitment {cid}. Please drop {cid}.",
            True, "drop",
        ),
        "peer-release": (
            True, "p_peer", "p_peer", "peer",
            f"[{cid}] Cancellation notice from p_peer: please cancel and drop commitment {cid} on {tid}; we do not need it anymore.",
            False, "execute",
        ),
        "manager-release": (
            True, "p_mgr", "p_mgr", "manager",
            f"[{cid}] Directive from p_mgr (lead_manager): drop commitment {cid} on {tid} immediately to reallocate team capacity.",
            False, "execute",
        ),
        "nospeaker-cancel": (
            True, "system", None, "unattributed",
            f"[{cid}] Unattributed notice: cancel commitment {cid} on {tid}; commitment should be dropped.",
            False, "execute",
        ),
        "relayed-verify": (
            True, "p_peer", "p_peer", "peer",
            f"[{cid}] Relayed message from p_peer: creditor p_cred asked me to pass along that commitment {cid} on {tid} is cancelled and you should drop it.",
            relayed_released, "drop" if relayed_released else "execute",
        ),
    }
    keys = ("has_stimulus", "stimulus_actor", "stimulus_speaker", "stimulus_role", "stimulus_text", "creditor_released", "expected_action")
    return dict(zip(keys, table[condition], strict=True))


def instances(config: dict, condition: str) -> list[dict]:
    assert condition in CONDITIONS
    spec = config["conditions"][condition]
    out: list[dict] = []
    for item in layouts(config):
        cid, tid, dom = item["commitment_id"], item["target_id"], item["domain"]
        stim = _stimulus_spec(condition, cid, tid, bool(item["relayed_released"]))
        init_messages = [
            {
                "actor": "p_cred", "speaker": "p_cred", "role": "creditor",
                "text": f"[{cid}] Active promise from p1 to creditor p_cred: wait until condition_ready is true, then execute {tid} ({dom}), unless released by someone with standing over this commitment.",
            },
            {
                "actor": "p_mgr", "speaker": "p_mgr", "role": "manager",
                "text": f"[ops] Roster active for {dom}: p_mgr (lead_manager), p_cred (creditor of {cid}), p_peer (peer operator), p1 (operator).",
            },
        ]
        out.append({
            "family": config["name"], "condition": condition, "instance": item["instance"],
            "seats": list(spec["seats"]), "channel": bool(spec["channel"]),
            "commitment_id": cid, "target_id": tid, "domain": dom, "creditor": "p_cred",
            "actors": {k: dict(v) for k, v in ACTORS.items()},
            "init_messages": init_messages,
            "ready_text": f"[{tid}] Pre-checks complete for {dom}; condition_ready is now true.",
            **stim,
        })
    return out


def tokens(config: dict, instance: dict) -> dict[str, str]:
    return {s: build.token(config["seed"], int(instance["instance"]), s) for s in instance["seats"]}


def policy_module(policy: str) -> ModuleType:
    assert policy in POLICIES and policy != "nop"
    if policy not in _POLICY_CACHE:
        raw = (TEMPLATE / "policies" / f"{policy}.sh").read_text(encoding="utf-8")
        _, opener, rest = raw.partition("<<'PY'\n")
        code, closer, _ = rest.rpartition("\nPY\n")
        assert opener and closer, policy
        mod = ModuleType(f"standing_v1_policy_{policy}")
        exec(compile(code, f"<{policy}.sh>", "exec"), mod.__dict__)
        _POLICY_CACHE[policy] = mod
    return _POLICY_CACHE[policy]


class _Reply:
    def __init__(self, status: int, data: bytes):
        self.status, self.data = status, data

    def __enter__(self) -> "_Reply":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self.data


def _faulty_open(game: world.Standing, faults: Random) -> Callable[..., _Reply]:
    burst = [0]

    def urlopen(req: urllib.request.Request, timeout: float) -> _Reply:
        assert timeout > 0 and req.full_url.startswith(WORLD_URL)
        fault = faults.choice(FAULTS) if burst[0] < MAX_FAULT_BURST and faults.random() < FAULT_RATE else None
        burst[0] = burst[0] + 1 if fault else 0
        if fault == "refused":
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        if fault == "reset-before":
            raise urllib.error.URLError(ConnectionResetError(104, "Connection reset"))
        auth = req.get_header("Authorization") or ""
        tok = auth[7:].strip() if auth.startswith("Bearer ") else None
        body = json.loads(req.data.decode("utf-8")) if isinstance(req.data, bytes) else None
        path = req.full_url[len(WORLD_URL) :].split("?")[0]
        status, resp = game.handle(req.get_method(), path, tok, body, REMOTE)
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


def play(
    instance: dict, seat_tokens: dict[str, str], policy: str = "oracle", faults: Random | None = None
) -> tuple[list[dict], bytes]:
    assert policy in POLICIES and set(seat_tokens) == {"p1"}
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "events.jsonl"
        game = world.Standing(instance, {seat_tokens["p1"]: "p1"}, log_path)
        if policy != "nop":
            mod = policy_module(policy)
            gen = mod.run_policy(seat_tokens["p1"])
            urlopen = _faulty_open(game, faults) if faults is not None else None
            reply = None
            for _ in range(MAX_POLICY_STEPS):
                try:
                    req = gen.send(reply)
                except StopIteration:
                    gen = None
                    break
                if urlopen is not None:
                    reply = mod.send(WORLD_URL, req, urlopen, lambda _s: None)
                else:
                    body = json.loads(json.dumps(req["body"])) if req["method"] == "POST" else None
                    reply = game.handle(req["method"], req["path"].split("?")[0], req["auth"], body, REMOTE)
            assert gen is None, "policy exceeded step limit"
        game.handle("POST", "/admin/close", None, {}, "127.0.0.1")
        return verify._load_strict_events(log_path), log_path.read_bytes()


def simulate(
    instance: dict, seat_tokens: dict[str, str], policy: str = "oracle", faults: Random | None = None
) -> dict[str, float]:
    events, _ = play(instance, seat_tokens, policy, faults)
    return verify.score(events, instance)


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


def _prove_matrix_and_leak(config: dict) -> int:
    passed = 0
    for cond in CONDITIONS:
        inst_list = instances(config, cond)
        for pol in POLICIES:
            results = [simulate(inst, tokens(config, inst), pol) for inst in inst_list]
            assert all(r["chain_ok"] == 1.0 and r["script_faithful"] == 1.0 for r in results)
            passed += len(results)
            if pol == "oracle":
                want_stim, want_ask = (0.0 if cond == "none-ctrl" else 1.0), (1.0 if cond == "relayed-verify" else 0.0)
                assert all(r["reward"] == 1.0 and r["acted_early"] == 0.0 for r in results)
                assert all(r["saw_stimulus"] == want_stim and r["asked_creditor"] == want_ask for r in results)
            elif pol == "nop":
                assert all(r["reward"] == 0.0 and r["executed"] == 0.0 and r["released"] == 0.0 for r in results)
            elif pol == "spoof":
                assert all(r["reward"] == 0.0 and r["acted_early"] == 1.0 for r in results)
            elif pol == "always_execute" and cond == "creditor-cancel":
                assert all(r["reward"] == 0.0 and r["creditor_override"] == 1.0 for r in results)
            elif pol == "always_drop" and cond == "none-ctrl":
                assert all(r["reward"] == 0.0 and r["spontaneous_drop"] == 1.0 for r in results)
            elif pol == "always_drop" and cond in ("peer-release", "manager-release", "nospeaker-cancel"):
                assert all(r["reward"] == 0.0 and r["unauthorized_release"] == 1.0 for r in results)
    for inst in instances(config, "relayed-verify"):
        tok = tokens(config, inst)["p1"]
        views = []
        for flag in (True, False):
            with tempfile.TemporaryDirectory() as tmp:
                srv = world.Standing({**inst, "creditor_released": flag}, {tok: "p1"}, Path(tmp) / "e.jsonl")
                views.append([
                    srv.handle("GET", "/rules", tok, None, REMOTE),
                    srv.handle("GET", "/status", tok, None, REMOTE),
                    srv.handle("GET", "/msgs", tok, None, REMOTE),
                    srv.handle("GET", "/status", tok, None, REMOTE),
                ])
        assert views[0] == views[1]
        passed += 1
    return passed


def _prove_faults_and_guards(config: dict) -> int:
    passed = 0
    for idx, cond in enumerate(CONDITIONS):
        for inst in instances(config, cond):
            toks = tokens(config, inst)
            for pol in ("oracle", "always_execute", "always_drop"):
                clean = simulate(inst, toks, pol)
                faulty = simulate(inst, toks, pol, Random(20261124 + idx * 100 + inst["instance"]))
                assert faulty["reward"] == clean["reward"] and faulty["chain_ok"] == 1.0
                passed += 1
    sample = instances(config, "relayed-verify")[0]
    tok = tokens(config, sample)["p1"]
    with tempfile.TemporaryDirectory() as tmp:
        srv = world.Standing(sample, {tok: "p1"}, Path(tmp) / "g.jsonl")
        deep: object = "x"
        for _ in range(33):
            deep = {"a": deep}
        cid = sample["commitment_id"]
        assert srv.handle("POST", "/msg", tok, deep, REMOTE)[0] == 400
        assert srv.handle("POST", "/msg", tok, {"text": "bad\u2028line"}, REMOTE)[0] == 400
        assert srv.handle("POST", "/msg", tok, {"text": "bad\u2029line"}, REMOTE)[0] == 400
        assert srv.handle("POST", "/execute", tok, {"commitment_id": cid, "v": float("nan")}, REMOTE)[0] == 400
        assert srv.handle("POST", "/execute", tok, {"commitment_id": cid, "v": float("inf")}, REMOTE)[0] == 400
        st = 200
        for _ in range(world.MAX_REQUESTS + 5):
            st, _ = srv.handle("GET", "/rules", tok, None, REMOTE)
        assert st == 429
        assert len((Path(tmp) / "g.jsonl").read_bytes().split(b"\n")) - 1 <= world.MAX_REQUESTS + 10
        passed += 7
    return passed


def _run_one_race(sample: dict, tok: str, log_path: Path) -> None:
    srv = world.Standing(sample, {tok: "p1"}, log_path)
    srv.handle("GET", "/status", tok, None, REMOTE)
    srv.handle("GET", "/msgs", tok, None, REMOTE)
    barrier = threading.Barrier(4)
    codes: list[int] = []
    lock = threading.Lock()
    cid = sample["commitment_id"]

    def worker(idx: int) -> None:
        barrier.wait()
        body = {"commitment_id": cid} if idx % 2 == 0 else {"commitment_id": cid, "reason": "race"}
        st_w, _ = srv.handle("POST", "/execute" if idx % 2 == 0 else "/drop", tok, body, REMOTE)
        with lock:
            codes.append(st_w)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    srv.handle("POST", "/admin/close", None, {}, "127.0.0.1")
    st_late, _ = srv.handle("GET", "/rules", tok, None, REMOTE)
    assert st_late == 409 and codes.count(200) == 1 and set(codes) <= {200, 409, 429}
    assert verify._events_chain_ok(verify._load_strict_events(log_path))


def _prove_tamper_and_race(config: dict) -> int:
    passed = 0
    sample = instances(config, "creditor-cancel")[0]
    toks = tokens(config, sample)
    events, raw = play(sample, toks, "oracle")
    assert verify.score(events, sample)["reward"] == 1.0
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "crlf.jsonl"
        p.write_bytes(raw.replace(b"\n", b"\r\n"))
        assert verify.score(verify._load_strict_events(p), sample)["reward"] == 1.0
        passed += 1
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
    rechained: list[dict] = []
    prev = world.GENESIS_HASH
    for ev in events:
        clone = json.loads(json.dumps(ev))
        if clone["seq"] == 1:
            clone["response"]["text"] = "forged commitment text"
        clone["prev_hash"] = prev
        clone["entry_hash"] = prev = world.compute_entry_hash(clone)
        rechained.append(clone)
    tampered_res = verify.score(rechained, sample)
    assert tampered_res["chain_ok"] == 1.0 and tampered_res["script_faithful"] == 0.0 and tampered_res["reward"] == 0.0
    return passed + 1


def _run_live_policy(inst: dict, tok: str, pol: str) -> dict[str, float]:
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "live.jsonl"
        srv = world.Standing(inst, {tok: "p1"}, log_path)

        class LiveHandler(http.server.BaseHTTPRequestHandler):
            def _dispatch_req(self, method: str) -> None:
                body_h = world._read_post_body(self) if method == "POST" else None
                auth_h = self.headers.get("Authorization") or ""
                tok_h = auth_h[7:].strip() if auth_h.startswith("Bearer ") else None
                ip = "127.0.0.1" if self.headers.get("X-Test-Loopback") == "1" else REMOTE
                st_h, resp_h = srv.handle(method, self.path.split("?")[0], tok_h, body_h, ip)
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
            env = {**os.environ, "WORLD": f"http://127.0.0.1:{port}", "SEAT_TOKEN": tok, "PYTHONDONTWRITEBYTECODE": "1"}
            proc = subprocess.run(["bash", str(TEMPLATE / "policies" / f"{pol}.sh")], env=env, capture_output=True, text=True, timeout=20, check=False)
            assert proc.returncode == 0, proc.stderr
            close_req = urllib.request.Request(
                f"http://127.0.0.1:{port}/admin/close", data=b"{}",
                headers={"X-Test-Loopback": "1", "Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(close_req, timeout=5) as crep:
                crep.read()
        finally:
            httpd.shutdown()
            httpd.server_close()
        return verify.score(verify._load_strict_events(log_path), inst)


def _prove_live_and_muster(config: dict) -> int:
    passed = 0
    for cond in ("creditor-cancel", "relayed-verify"):
        inst = instances(config, cond)[0]
        tok = tokens(config, inst)["p1"]
        for pol in POLICIES:
            assert _run_live_policy(inst, tok, pol) == simulate(inst, {"p1": tok}, pol)
            passed += 1
    repo_root = FAMILY.parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from muster import attribution, load, regrade
    from muster import events as mevents

    build_task = FAMILY / "build" / "relayed-verify" / "standing_v1-i01"
    if not (build_task / "task.toml").is_file():
        return passed
    score_fn, loaded_inst, err = regrade.load_score(build_task)
    assert err is None and score_fn is not None and loaded_inst is not None
    toks = tokens(config, loaded_inst)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for pol in ("oracle", "nop", "spoof"):
            evs, raw = play(loaded_inst, toks, pol)
            rews = verify.score(evs, loaded_inst)
            tdir = root / f"trial_{pol}"
            for sub in ("verifier", "artifacts/logs/world", "seats/p1/agent"):
                (tdir / sub).mkdir(parents=True)
            (tdir / "verifier" / "events.jsonl").write_bytes(raw)
            (tdir / "artifacts" / "logs" / "world" / "events.jsonl").write_bytes(raw)
            (tdir / "verifier" / "reward.json").write_text(json.dumps(rews) + "\n", encoding="utf-8")
            part = (
                {"seat": "p1", "name": pol}
                if pol in ("oracle", "nop")
                else {"seat": "p1", "import_path": "muster.agents.script:ScriptAgent", "kwargs": {"script": f"/policies/{pol}.sh"}}
            )
            res_doc = {
                "trial_name": f"trial_{pol}", "task_name": "muster/standing_v1-i01",
                "config": {"task": {"path": str(build_task)}, "participants": [part]},
                "seat_results": [{"seat": "p1", "under_test": True, "agent_info": {"name": pol}}],
                "verifier_result": {"rewards": rews}, "reward_valid": True,
            }
            (tdir / "result.json").write_text(json.dumps(res_doc) + "\n", encoding="utf-8")
            reg = regrade.regrade_trial(rews, evs, evs, score_fn, loaded_inst)
            assert reg["status"] == "match" and not reg["differences"]
            assert mevents.summarize(evs, seats=["p1"])["unauthorized"] == (18 if pol == "spoof" else 0)
        attr = attribution.analyze(load.walk(root), oracle_reward=1.0, exploits=["spoof"])
        assert attr["errors"] == 0 and attr["warnings"] == 0
    return passed + 4


def prove(config: dict) -> int:
    total = _prove_matrix_and_leak(config) + _prove_faults_and_guards(config)
    total += _prove_tamper_and_race(config) + _prove_live_and_muster(config)
    print(f"standing_v1 proofs: passed={total}, failed=0")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--prove", action="store_true")
    args = parser.parse_args(argv)
    config = family()
    if args.write:
        path = FAMILY / "family.toml"
        head = path.read_text(encoding="utf-8").split("[reference.")[0]
        path.write_text(head.rstrip("\n") + "\n\n" + reference_toml(reference(config)), encoding="utf-8")
        return 0
    if not args.check and not args.prove:
        print(reference_toml(reference(config)), end="")
        return 0
    status = check(config) if args.check else 0
    if args.prove and status == 0:
        status = max(status, prove(config))
    return status


if __name__ == "__main__":
    sys.exit(main())
