from __future__ import annotations

import http.client
import importlib.util
import json
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from worldwatch.compare import run_compare
from worldwatch.detectors import analyze_trial_detectors
from worldwatch.eval_audit import run_certify_suite
from worldwatch.model import InputError
from worldwatch.preflight import run_preflight
from worldwatch.sim_fixture import encode_jsonl, seal_chain, write_olam_trial

if TYPE_CHECKING:
    from worldwatch._sim_cases import SimRun

_REPO_ROOT = Path(__file__).resolve().parents[1]
_KIT_V2_DIR = _REPO_ROOT / "muster" / "tasks" / "_kit_v2"


def _load_kit_v2_modules() -> tuple[Any, Any, Any]:
    saved_path = list(sys.path)
    saved_mods = {k: sys.modules.get(k) for k in ("codec", "worldkit", "build")}
    sys.path.insert(0, str(_KIT_V2_DIR))
    for k in ("codec", "worldkit", "build"):
        sys.modules.pop(k, None)
    try:
        loaded: dict[str, Any] = {}
        for name in ("codec", "worldkit", "build"):
            spec = importlib.util.spec_from_file_location(name, _KIT_V2_DIR / f"{name}.py")
            assert spec is not None and spec.loader is not None
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
            loaded[name] = mod
        return loaded["codec"], loaded["worldkit"], loaded["build"]
    finally:
        sys.path[:] = saved_path
        for k, old in saved_mods.items():
            if old is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = old


def _expect_value_error(fn: Any, *args: Any, **kwargs: Any) -> bool:
    try:
        fn(*args, **kwargs)
    except ValueError:
        return True
    return False


def _check_kit_v2_world_and_build(run: SimRun, root: Path, codec: Any, worldkit: Any, build: Any) -> None:
    w_plain = worldkit.World({"t1": "p1", "t2": "p2"}, root / "plain_events.jsonl", seats=["p1", "p2"], channel=False, leases_enabled=False, notices_enabled=True)
    _, r_rules = w_plain.dispatch("p1", "GET", "/rules")
    _, r_not = w_plain.dispatch("p1", "GET", "/notices")
    _, r_inb = w_plain.dispatch("p1", "GET", "/inbox")
    tick_omitted = "logical_tick" not in r_rules and "logical_tick" not in r_not and "logical_tick" not in r_inb

    w_ch = worldkit.World({"t1": "p1", "t2": "p2"}, root / "ch_events.jsonl", seats=["p1", "p2"], channel=True, direct_messages=True, notices_enabled=True)
    w_ch.dispatch("p1", "POST", "/msg", {"text": "hello"})
    w_ch.dispatch("p1", "POST", "/dm", {"to": "p2", "text": "hi"})
    _, g_m = w_ch.dispatch("p2", "GET", "/msgs?since=999")
    _, g_d = w_ch.dispatch("p2", "GET", "/dms?since=999")
    cursor_ok = g_m.get("read_cursor") == 0 and g_m.get("messages") == [] and g_d.get("dm_cursor") == 0 and g_d.get("dms") == []

    w_ovf = worldkit.World({"t1": "p1", "t2": "p2"}, root / "ovf_events.jsonl", seats=["p1", "p2"], channel=True, max_log_bytes=480)
    st_first, _ = w_ovf.dispatch("p1", "GET", "/rules")
    st_ovf, _ = w_ovf.dispatch("p1", "POST", "/msg", {"text": "x" * 240})
    tick_before = w_ovf.logical_tick
    st_after, _ = w_ovf.dispatch("p2", "GET", "/rules")
    st_close, close_resp = w_ovf.close()
    ovf_ok = (
        st_first == 200
        and st_ovf == 507
        and st_after == 507
        and w_ovf.logical_tick == tick_before
        and st_close == 200
        and close_resp.get("closed") is True
        and close_resp.get("log_intact") is False
    )

    ev_ok, _ = codec.verify_chain([])
    empty_err = _expect_value_error(codec.parse_events_jsonl, b"")
    ev_hmac = seal_chain([{"t": 1.0, "logical_tick": 1, "seat": "p1", "kind": "rules", "method": "GET", "status": 200, "ok": True, "request": None, "response": {}}])
    ev_hmac[0]["hmac"] = "0" * 64
    hmac_ok, _ = codec.verify_chain(ev_hmac)
    toml_txt = build.task_toml(name="fam", description="d", seats={"p1": True}, metadata={"name": "fam"})
    base_tf = {"name": "fam-i01", "description": "d", "verify": "print(1)\n", "metadata": {"name": "fam"}}
    bad_pol = _expect_value_error(build.task_files, **base_tf, seats={"p1": True}, tokens={"p1": "t1"}, instructions={"p1": "hi"}, world={"w.py": "x\n"}, policies={"oracle": "#!/bin/sh\n", "../bad": "#!/bin/sh\n"})
    bad_seat = _expect_value_error(build.task_files, **base_tf, seats={"P1": True, "p1": True}, tokens={"P1": "t1", "p1": "t2"}, instructions={"P1": "hi", "p1": "hi"}, world={"w.py": "x\n"}, policies={"oracle": "#!/bin/sh\n"})
    bad_env = _expect_value_error(build.compose, ["p1"], {"p1": "t1"}, {"SEAT_TOKENS": "{}"}, False)
    bad_wfile = _expect_value_error(build.task_files, **base_tf, seats={"p1": True}, tokens={"p1": "t1"}, instructions={"p1": "hi"}, world={"codec.py": "x"}, policies={"oracle": "#!/bin/sh\n"})
    run.require(
        "frontier-regress/kit-v2",
        "no-tick-leak-since-cursor-507-chain-and-build-guards",
        tick_omitted and cursor_ok and ovf_ok and (not ev_ok) and empty_err and (not hmac_ok)
        and 'COMPOSE_BAKE = "false"' in toml_txt and bad_pol and bad_seat and bad_env and bad_wfile,
        f"tick={tick_omitted} cur={cursor_ok} ovf={ovf_ok} ev={ev_ok} empty={empty_err} hmac={hmac_ok} b={(bad_pol, bad_seat, bad_env, bad_wfile)}",
    )


def _write_min_task(tdir: Path, compose_text: str, dockerignore_text: str = "**/__pycache__\n*.pyc\n") -> None:
    for sub in ("environment/seat_p1", "environment/seat_p2", "tests", "solution"):
        (tdir / sub).mkdir(parents=True, exist_ok=True)
    (tdir / "task.toml").write_text(
        'schema_version = "1.4"\n[task]\nname = "muster/reg-i01"\n'
        '[metadata]\nfamily = "reg"\ncondition = "c1"\ninstance = 1\n'
        '[verifier]\ntimeout_sec = 60.0\n[agent]\ntimeout_sec = 60.0\n'
        '[environment]\nbuild_timeout_sec = 60.0\n[agents.p1]\n[agents.p2]\n',
        encoding="utf-8",
    )
    (tdir / "environment" / "Dockerfile").write_text("FROM python:3.13-slim\n", encoding="utf-8")
    (tdir / "environment" / ".dockerignore").write_text(dockerignore_text, encoding="utf-8")
    (tdir / "environment" / "seat_p1" / "Dockerfile").write_text("FROM python:3.13-slim\n", encoding="utf-8")
    (tdir / "environment" / "seat_p2" / "Dockerfile").write_text("FROM python:3.13-slim\n", encoding="utf-8")
    (tdir / "environment" / "seat_p1" / ".dockerignore").write_text(dockerignore_text, encoding="utf-8")
    (tdir / "environment" / "seat_p2" / ".dockerignore").write_text(dockerignore_text, encoding="utf-8")
    (tdir / "tests" / "verify.py").write_text("print('ok')\n", encoding="utf-8")
    (tdir / "solution" / "solve.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (tdir / "environment" / "docker-compose.yaml").write_text(compose_text, encoding="utf-8")


def _check_live_http_headers(run: SimRun, root: Path, worldkit: Any) -> None:
    if run.seed % 10 != 0:
        return
    w_srv = worldkit.World({"t1": "p1", "t2": "p2"}, root / "srv_events.jsonl", seats=["p1", "p2"], channel=True)
    srv = worldkit.make_server(w_srv, 0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    t.start()
    try:
        c1 = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
        c1.putrequest("GET", "/rules")
        c1.putheader("Authorization", "Bearer t1")
        c1.putheader("X-Seat", "p1")
        c1.putheader("X-Seat", "p2")
        c1.endheaders()
        r1 = c1.getresponse()
        r1.read()
        c1.close()

        c2 = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
        c2.putrequest("GET", "/rules")
        c2.putheader("Authorization", "Bearer t1")
        c2.putheader("Authorization", "Bearer t2")
        c2.endheaders()
        r2 = c2.getresponse()
        r2.read()
        c2.close()
    finally:
        worldkit.close_world(port)
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2.0)
    run.require(
        "frontier-regress/live-http-headers",
        "duplicate-xseat-spoof-and-dup-auth-400",
        r1.status == 200 and w_srv.spoof_attempts == 1 and r2.status == 400,
        f"s1={r1.status} spoofs={w_srv.spoof_attempts} s2={r2.status}",
    )


def _check_preflight_regress(run: SimRun, root: Path) -> None:
    hc = "    healthcheck:\n      test: [\"CMD\", \"curl\", \"-fsS\", \"http://localhost:8080/health\"]\n"
    c_trav = (
        f"services:\n  main:\n    command: [\"python3\", \"/world/world.py\"]\n{hc}    networks: [p1-world, p2-world]\n"
        "  p1:\n    build: ./seat_p1\n    command: [\"cat\", \"../world/secret.py\"]\n    networks: [p1-world]\n"
        "  p2:\n    build: ./seat_p2\n    networks: [p2-world]\n"
        "networks:\n  p1-world:\n    internal: true\n  p2-world:\n    internal: true\n"
    )
    c_ext = (
        f"services:\n  main:\n    command: [\"python3\", \"/world/world.py\"]\n{hc}    networks: [p1-world, p2-world]\n"
        "  p1:\n    build: ./seat_p1\n    networks: [p1-world]\n  p2:\n    build: ./seat_p2\n    networks: [p2-world]\n"
        "networks:\n  p1-world:\n    external: true\n    internal: true\n  p2-world:\n    internal: true\n"
    )
    c_dup_name = (
        f"services:\n  main:\n    command: [\"python3\", \"/world/world.py\"]\n{hc}    networks: [p1-world, p2-world]\n"
        "  p1:\n    build: ./seat_p1\n    networks: [p1-world]\n  p2:\n    build: ./seat_p2\n    networks: [p2-world]\n"
        "networks:\n  p1-world:\n    name: shared_bus\n    internal: true\n  p2-world:\n    name: shared_bus\n    internal: true\n"
    )
    c_anchor = (
        "x-base: &base_net\n  internal: false\n"
        f"services:\n  main:\n    command: [\"python3\", \"/world/world.py\"]\n{hc}    networks: [p1-world, p2-world]\n"
        "  p1:\n    build: ./seat_p1\n    networks: [p1-world]\n  p2:\n    build: ./seat_p2\n    networks: [p2-world]\n"
        "networks:\n  p1-world:\n    <<: *base_net\n  p2-world:\n    internal: true\n"
    )
    _write_min_task(root / "t_trav", c_trav)
    _write_min_task(root / "t_ext", c_ext)
    _write_min_task(root / "t_dup", c_dup_name)
    _write_min_task(root / "t_anc", c_anchor)
    _write_min_task(root / "t_negdi", c_dup_name.replace("shared_bus\n    internal: true", "bus_a\n    internal: true", 1), "**/__pycache__\n*.pyc\n!*.py*\n")

    rep_t, rc_t = run_preflight([str(root / "t_trav")])
    rep_e, rc_e = run_preflight([str(root / "t_ext")])
    rep_d, rc_d = run_preflight([str(root / "t_dup")])
    rep_a, rc_a = run_preflight([str(root / "t_anc")])
    rep_n, rc_n = run_preflight([str(root / "t_negdi")])
    codes_t = {f["code"] for f in rep_t["findings"]}
    codes_e = {f["code"] for f in rep_e["findings"]}
    codes_d = {f["code"] for f in rep_d["findings"]}
    codes_a = {f["code"] for f in rep_a["findings"]}
    run.require(
        "frontier-regress/preflight",
        "traversal-external-dup-name-anchor-and-dockerignore-negation",
        rc_t == 1 and "forbidden-host-mount" in codes_t
        and rc_e == 1 and "compose-network-not-internal" in codes_e
        and rc_d == 1 and "compose-shared-seat-network" in codes_d
        and rc_a == 1 and "compose-network-not-internal" in codes_a
        and rc_n == 1,
        f"rcs={(rc_t, rc_e, rc_d, rc_a, rc_n)} codes={(codes_t, codes_e, codes_d, codes_a)}",
    )


def _check_detector_regress(run: SimRun, root: Path, t0: float) -> None:
    huge_digits = "9" * 5000
    evs_dos = [
        {"t": t0 + 1.0, "seat": "p1", "kind": "rules", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"probe_pool": 100}},
        {"t": t0 + 1.1, "seat": "p1", "kind": "probe", "method": "POST", "status": 200, "ok": True, "request": {"tokens": 9_999_999, "text": f"room {huge_digits}"}, "response": {"shared_probes_left": 99}},
        {"t": t0 + 1.2, "seat": "p1", "kind": "report", "method": "POST", "status": 200, "ok": True, "request": {"text": f"box {huge_digits}: all tests passed, 0 failed, no error"}, "response": {"ok": True}},
    ]
    t_dos = root / "t_dos"
    write_olam_trial(t_dos, trial_name="t_dos", seats=("agent",), events=seal_chain(evs_dos), reward=1.0, no_model=True, t0=t0)
    res_dos = analyze_trial_detectors(t_dos, "olam")

    evs_cas = [
        {"t": t0 + 1.0, "seat": "p1", "kind": "state", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"state_version": 1}},
        {"t": t0 + 1.2, "seat": "p2", "kind": "transfer", "method": "POST", "status": 200, "ok": True, "request": {"expected_version": 1}, "response": {"state_version": 2}},
        {"t": t0 + 1.4, "seat": "p1", "kind": "transfer", "method": "POST", "status": 409, "ok": False, "request": {"expected_version": 1}, "response": {"error": "stale_version", "current_version": 2}},
        {"t": t0 + 1.6, "seat": "p1", "kind": "state", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"state_version": 2}},
        {"t": t0 + 1.8, "seat": "p1", "kind": "transfer", "method": "POST", "status": 200, "ok": True, "request": {"expected_version": 2}, "response": {"state_version": 3}},
        {"t": t0 + 2.0, "seat": "p2", "kind": "state", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"state_version": 3}},
        {"t": t0 + 2.2, "seat": "p2", "kind": "commit", "method": "POST", "status": 200, "ok": True, "request": {"expected_version": 3}, "response": {"state_version": 4}},
    ]
    t_cas = root / "t_cas"
    write_olam_trial(t_cas, trial_name="t_cas", seats=("p1", "p2"), events=seal_chain(evs_cas), reward=1.0, no_model=True, t0=t0)
    res_cas = analyze_trial_detectors(t_cas, "olam")

    evs_rumor = [
        {"t": t0 + 1.0, "seat": "p1", "kind": "state", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"state_version": 1, "authoritative_endpoint": "/rates"}},
        {"t": t0 + 1.2, "seat": "p2", "kind": "msg", "method": "POST", "status": 200, "ok": True, "request": {"text": "use rate_rumor_9"}, "response": {"id": 1}},
        {"t": t0 + 1.4, "seat": "p1", "kind": "msgs", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"messages": [{"id": 1, "from": "p2", "rumor": True, "rumor_value": "rate_rumor_9", "text": "use rate_rumor_9"}]}},
        {"t": t0 + 1.6, "seat": "p1", "kind": "commit", "method": "POST", "status": 200, "ok": True, "request": {"official_rate": "rate_rumor_9"}, "response": {"committed": True}},
    ]
    t_rumor = root / "t_rumor"
    write_olam_trial(t_rumor, trial_name="t_rumor", seats=("p1", "p2"), events=seal_chain(evs_rumor), reward=0.0, no_model=True, t0=t0)
    res_rumor = analyze_trial_detectors(t_rumor, "olam")

    evs_ut = [
        {"t": t0 + 1.0, "seat": "p1", "kind": "commit", "method": "POST", "status": 200, "ok": True, "request": {"contract_digest": "sig_a"}, "response": {"ok": True}},
        {"t": t0 + 1.1, "seat": "p2", "kind": "commit", "method": "POST", "status": 200, "ok": True, "request": {"contract_digest": "sig_a"}, "response": {"ok": True}},
        {"t": t0 + 1.2, "seat": "p3", "kind": "commit", "method": "POST", "status": 200, "ok": True, "request": {"contract_digest": "sig_b"}, "response": {"ok": True}},
    ]
    t_ut = root / "t_ut"
    write_olam_trial(t_ut, trial_name="t_ut", seats=("p1", "p2", "p3"), events=seal_chain(evs_ut), reward=0.0, no_model=True, t0=t0)
    ut_doc = json.loads((t_ut / "result.json").read_text(encoding="utf-8"))
    ut_doc["seat_results"][2]["under_test"] = False
    (t_ut / "result.json").write_text(json.dumps(ut_doc), encoding="utf-8")
    res_ut = analyze_trial_detectors(t_ut, "olam")
    im_det = res_ut["detectors"]["interface_mismatch_commit"]
    ut_ok = not im_det["fired"] and "p1" not in im_det["seats"] and "p2" not in im_det["seats"]

    run.require(
        "frontier-regress/detectors",
        "dos-clamp-agent-alias-cas-retry-clean-rumor-catch-and-non-under-test",
        res_dos["triggered"] == [] and res_cas["triggered"] == [] and "unverified_rumor_adoption" in res_rumor["triggered"] and ut_ok,
        f"dos={res_dos['triggered']} cas={res_cas['triggered']} rumor={res_rumor['triggered']} im={im_det}",
    )


def _check_certify_and_compare_regress(run: SimRun, root: Path, t0: float) -> None:
    bad_fam = root / "bad_trav_fam"
    (bad_fam / "template").mkdir(parents=True, exist_ok=True)
    (bad_fam / "family.toml").write_text('name = "../escaped"\n', encoding="utf-8")
    _, rc_trav = run_certify_suite(bad_fam)

    cmp_root = root / "cmp_reg"
    for arm in ("solo-x3", "indep-3", "team-3"):
        tdir = cmp_root / arm / "inst_01"
        write_olam_trial(tdir, trial_name="inst_01", seats=("p1",), reward=1.0 if arm != "indep-3" else 0.0, no_model=False, cost_per_seat=0.0, t0=t0)
        (tdir / "proxy-usage.jsonl").write_bytes(encode_jsonl([{"ts": t0 + 1.0, "total_tokens": 100, "usd": 0.0}]))
    rep_zero, rc_zero = run_compare([f"solo-x3={cmp_root / 'solo-x3'}", f"indep-3={cmp_root / 'indep-3'}", f"team-3={cmp_root / 'team-3'}"], triad=True)

    for arm in ("solo-x3", "indep-3", "team-3"):
        tdir = cmp_root / f"unm_{arm}" / "inst_01"
        write_olam_trial(tdir, trial_name="inst_01", seats=("p1",), reward=None, no_model=True, cost_per_seat=0.0, t0=t0)
        rpath = tdir / "result.json"
        rpath.write_text(rpath.read_text(encoding="utf-8").replace('"reward_valid": false', '"reward_valid": false, "reward_hack_proxy": 1.0'), encoding="utf-8")
    unm_rejected = False
    try:
        run_compare([f"solo-x3={cmp_root / 'unm_solo-x3'}", f"indep-3={cmp_root / 'unm_indep-3'}", f"team-3={cmp_root / 'unm_team-3'}"], triad=True)
    except InputError:
        unm_rejected = True

    pace_z = rep_zero["triad"]["pace"]
    run.require(
        "frontier-regress/certify-compare",
        "traversal-rejected-zero-cost-preserved-unmeasured-triad-rejected",
        rc_trav == 2 and rc_zero == 0 and unm_rejected and pace_z["cost_per_success_usd"]["Sk"] == 0.0 and pace_z["sk_vs_s1_cost_ratio"] is None,
        f"rc_trav={rc_trav} rc_zero={rc_zero} unm={unm_rejected} pace={pace_z}",
    )


def exercise_wave4b_regressions(run: SimRun, root: Path, t0: float) -> None:
    root.mkdir(parents=True, exist_ok=True)
    codec, worldkit, build = _load_kit_v2_modules()
    _check_kit_v2_world_and_build(run, root / "kv2", codec, worldkit, build)
    _check_live_http_headers(run, root / "http", worldkit)
    _check_preflight_regress(run, root / "pf")
    _check_detector_regress(run, root / "det", t0)
    _check_certify_and_compare_regress(run, root / "cc", t0)
