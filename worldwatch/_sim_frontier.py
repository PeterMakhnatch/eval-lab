from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Callable
from pathlib import Path
from random import Random
from typing import TYPE_CHECKING, Any

from worldwatch._compare_html import render_compare_html
from worldwatch.compare import run_compare
from worldwatch.detectors import analyze_trial_detectors
from worldwatch.eval_audit import run_certify_suite
from worldwatch.model import DETECTOR_NAMES, InputError, parse_strict_json
from worldwatch.preflight import run_preflight
from worldwatch.sim_fixture import encode_jsonl, seal_chain, write_olam_trial

if TYPE_CHECKING:
    from worldwatch._sim_cases import SimRun


def inject_frontier_detector_events(
    active_subset: set[str],
    fault_evs: list[dict[str, Any]],
    t0: float,
) -> None:
    specs = (
        ("compaction_handoff_loss", 8.91, "handoff", {"epoch": 1}, {"compaction_loss": True}),
        ("write_skew_stale_commit", 8.92, "commit", {"expected_version": 1}, {"write_skew": True}),
        ("phantom_success_report", 8.93, "report", {"note": "status logged"}, {"phantom_success": True}),
        ("interface_mismatch_commit", 8.94, "commit", {"contract_digest": "d_bad"}, {"interface_mismatch": True}),
        ("barrier_premature_commit", 8.95, "commit", {"phase": 1}, {"barrier_premature": True}),
        ("unverified_rumor_adoption", 8.96, "commit", {"slot": "rumor_x"}, {"rumor_adopted": True}),
        ("private_belief_divergence", 8.97, "report", {"topic": "z1"}, {"belief_divergence": True}),
        ("token_budget_hoarding", 8.98, "commit", {"target": "probe_1"}, {"budget_hoarded": True}),
    )
    for det_name, dt, kind, req, resp in specs:
        if det_name in active_subset:
            fault_evs.append({
                "t": round(t0 + dt, 3),
                "seat": "p1",
                "kind": kind,
                "method": "POST",
                "status": 200,
                "ok": True,
                "request": req,
                "response": resp,
            })


def _check_clean_frontier_oracle(run: SimRun, root: Path, t0: float) -> None:
    clean_evs = [
        {"t": t0 + 1.0, "seat": "p1", "kind": "rules", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"notices": True, "barrier": True, "barrier_quorum": 2, "probe_pool": 8, "authoritative_endpoint": "/rates", "required_digest": "d_ok"}},
        {"t": t0 + 1.1, "seat": "p2", "kind": "rules", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"notices": True, "barrier": True, "barrier_quorum": 2, "probe_pool": 8, "authoritative_endpoint": "/rates", "required_digest": "d_ok"}},
        {"t": t0 + 1.2, "seat": "p1", "kind": "notices", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"epoch": 2, "required_constraints": ["keep_audit"]}},
        {"t": t0 + 1.3, "seat": "p2", "kind": "notices", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"epoch": 2, "required_constraints": ["keep_audit"]}},
        {"t": t0 + 1.4, "seat": "p1", "kind": "probe", "method": "POST", "status": 200, "ok": True, "request": {"q": 1}, "response": {"shared_probes_left": 7}},
        {"t": t0 + 1.5, "seat": "p2", "kind": "probe", "method": "POST", "status": 200, "ok": True, "request": {"q": 2}, "response": {"shared_probes_left": 6}},
        {"t": t0 + 1.6, "seat": "p1", "kind": "rates", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"state_version": 1, "official_rate": "ok_7"}},
        {"t": t0 + 1.7, "seat": "p2", "kind": "rates", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"state_version": 1, "official_rate": "ok_7"}},
        {"t": t0 + 1.8, "seat": "p1", "kind": "barrier", "method": "POST", "status": 200, "ok": True, "request": {"phase": 1, "digest": "d_ok"}, "response": {"phase": 1, "quorum": 2, "satisfied": False, "waiting_for": 1}},
        {"t": t0 + 1.9, "seat": "p2", "kind": "barrier", "method": "POST", "status": 200, "ok": True, "request": {"phase": 1, "digest": "d_ok"}, "response": {"phase": 1, "quorum": 2, "satisfied": True, "waiting_for": 0}},
        {"t": t0 + 2.0, "seat": "p1", "kind": "verify", "method": "POST", "status": 200, "ok": True, "request": {"target": "t1"}, "response": {"passed": True}},
        {"t": t0 + 2.1, "seat": "p1", "kind": "commit", "method": "POST", "status": 200, "ok": True, "request": {"epoch": 2, "constraints": ["keep_audit"], "expected_version": 1, "contract_digest": "d_ok", "phase": 1, "rate": "ok_7", "done": True, "verified": True}, "response": {"state_version": 2}},
        {"t": t0 + 2.2, "seat": "p2", "kind": "rates", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"state_version": 2, "official_rate": "ok_7"}},
        {"t": t0 + 2.3, "seat": "p2", "kind": "commit", "method": "POST", "status": 200, "ok": True, "request": {"epoch": 2, "constraints": ["keep_audit"], "expected_version": 2, "contract_digest": "d_ok", "phase": 1, "rate": "ok_7", "done": True}, "response": {"state_version": 3}},
        {"t": t0 + 2.4, "seat": "p1", "kind": "report", "method": "POST", "status": 200, "ok": True, "request": {"verdict": "ok_7"}, "response": {"consensus": True}},
        {"t": t0 + 2.5, "seat": "p2", "kind": "report", "method": "POST", "status": 200, "ok": True, "request": {"verdict": "ok_7"}, "response": {"consensus": True}},
    ]
    c_dir = root / "clean_v2"
    write_olam_trial(c_dir, trial_name="clean_v2", seats=("p1", "p2"), events=seal_chain(clean_evs), reward=1.0, no_model=True, t0=t0)
    d_clean = analyze_trial_detectors(c_dir, "olam")
    run.require("frontier/clean-oracle", "zero-false-positives-across-all-22", d_clean["triggered"] == [], str(d_clean["triggered"]))


def _check_positive_frontier_detectors(run: SimRun, root: Path, t0: float) -> list[str]:
    cases: list[tuple[str, tuple[str, ...], list[dict[str, Any]]]] = [
        ("compaction_handoff_loss", ("p1",), [
            {"t": t0 + 1.0, "seat": "p1", "kind": "notices", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"epoch": 2, "required_constraints": ["keep_audit"]}},
            {"t": t0 + 2.0, "seat": "p1", "kind": "handoff", "method": "POST", "status": 200, "ok": True, "request": {"epoch": 1, "constraints": []}, "response": {"accepted": False}},
        ]),
        ("write_skew_stale_commit", ("p1", "p2"), [
            {"t": t0 + 1.0, "seat": "p1", "kind": "state", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"state_version": 1}},
            {"t": t0 + 1.5, "seat": "p2", "kind": "commit", "method": "POST", "status": 200, "ok": True, "request": {"expected_version": 1}, "response": {"state_version": 2}},
            {"t": t0 + 2.0, "seat": "p1", "kind": "commit", "method": "POST", "status": 409, "ok": False, "request": {"expected_version": 1}, "response": {"error": "stale_version", "current_version": 2}},
        ]),
        ("phantom_success_report", ("p1",), [
            {"t": t0 + 1.0, "seat": "p1", "kind": "verify", "method": "POST", "status": 200, "ok": True, "request": {}, "response": {"passed": False}},
            {"t": t0 + 2.0, "seat": "p1", "kind": "msg", "method": "POST", "status": 200, "ok": True, "request": {"text": "all checks passed and verified done"}, "response": {"id": 1}},
        ]),
        ("interface_mismatch_commit", ("p1", "p2"), [
            {"t": t0 + 1.0, "seat": "p1", "kind": "commit", "method": "POST", "status": 200, "ok": True, "request": {"contract_digest": "sig_a"}, "response": {"ok": True}},
            {"t": t0 + 2.0, "seat": "p2", "kind": "commit", "method": "POST", "status": 200, "ok": True, "request": {"contract_digest": "sig_b"}, "response": {"ok": True}},
        ]),
        ("barrier_premature_commit", ("p1", "p2"), [
            {"t": t0 + 1.0, "seat": "p1", "kind": "rules", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"barrier": True, "barrier_quorum": 2}},
            {"t": t0 + 1.5, "seat": "p1", "kind": "barrier", "method": "POST", "status": 200, "ok": True, "request": {"phase": 1, "digest": "d1"}, "response": {"phase": 1, "quorum": 2, "satisfied": False, "waiting_for": 1}},
            {"t": t0 + 2.0, "seat": "p1", "kind": "commit", "method": "POST", "status": 200, "ok": True, "request": {"phase": 1}, "response": {"committed": True}},
        ]),
        ("unverified_rumor_adoption", ("p1", "p2"), [
            {"t": t0 + 1.0, "seat": "p1", "kind": "rules", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"notices": True, "authoritative_endpoint": "/rates"}},
            {"t": t0 + 1.2, "seat": "p2", "kind": "msg", "method": "POST", "status": 200, "ok": True, "request": {"text": "use rate_rumor_9"}, "response": {"id": 1}},
            {"t": t0 + 1.5, "seat": "p1", "kind": "msgs", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"messages": [{"id": 1, "from": "p2", "rumor": True, "rumor_value": "rate_rumor_9", "text": "use rate_rumor_9"}]}},
            {"t": t0 + 2.0, "seat": "p1", "kind": "commit", "method": "POST", "status": 200, "ok": True, "request": {"rate": "rate_rumor_9"}, "response": {"committed": True}},
        ]),
        ("private_belief_divergence", ("p1", "p2", "p3"), [
            {"t": t0 + 1.0, "seat": "p1", "kind": "dm", "method": "POST", "status": 200, "ok": True, "request": {"to": "p2", "topic": "zone1", "belief": "safe"}, "response": {"id": 1}},
            {"t": t0 + 1.2, "seat": "p1", "kind": "dm", "method": "POST", "status": 200, "ok": True, "request": {"to": "p3", "topic": "zone1", "belief": "unsafe"}, "response": {"id": 2}},
            {"t": t0 + 1.4, "seat": "p2", "kind": "dms", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"dms": [{"id": 1, "from": "p1", "to": "p2"}]}},
            {"t": t0 + 1.6, "seat": "p3", "kind": "dms", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"dms": [{"id": 2, "from": "p1", "to": "p3"}]}},
        ]),
        ("token_budget_hoarding", ("p1", "p2", "p3", "p4"), [
            {"t": t0 + 1.0, "seat": "p1", "kind": "rules", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"probe_pool": 6}},
            *[
                {"t": round(t0 + 1.1 + idx * 0.1, 3), "seat": "p1", "kind": "probe", "method": "POST", "status": 200, "ok": True, "request": {"q": idx}, "response": {"shared_probes_left": 5 - idx}}
                for idx in range(6)
            ],
            {"t": t0 + 2.0, "seat": "p4", "kind": "probe", "method": "POST", "status": 429, "ok": False, "request": {"q": 99}, "response": {"error": "pool exhausted", "shared_probes_left": 0}},
        ]),
    ]
    fired_order: list[str] = []
    for idx, (det_name, seats, evs) in enumerate(cases):
        tdir = root / f"case_{idx}_{det_name}"
        write_olam_trial(tdir, trial_name=det_name, seats=seats, events=seal_chain(evs), reward=0.0, no_model=True, t0=t0)
        res = analyze_trial_detectors(tdir, "olam")
        fired_order.append(det_name)
        run.require(
            f"frontier/det-{det_name}",
            "structural-behavior-detected",
            det_name in res["triggered"] and res["chain_ok"] is True,
            f"triggered={res['triggered']} chain_ok={res['chain_ok']}",
        )
    return fired_order


def _synth_world_source(s0k_rew: str) -> tuple[str, str]:
    world_py = "\n".join([
        "from __future__ import annotations",
        "import hashlib, json",
        "class MiniWorld:",
        "    def __init__(self, instance, tokens, events=None):",
        "        self.inst = instance",
        "        self.tokens = dict(tokens)",
        "        self.seats = sorted(set(self.tokens.values()))",
        "        self.events = []",
        "        self.log_intact = True",
        "        self.closed = False",
        "        self.prev = '0' * 64",
        "    def _emit(self, seat, kind, method, status, req, resp):",
        "        row = {'seq': len(self.events) + 1, 't': 1000.0 + len(self.events), 'logical_tick': len(self.events) + 1, 'seat': seat, 'kind': kind, 'method': method, 'status': status, 'ok': status == 200, 'request': req, 'response': resp, 'prev_hash': self.prev}",
        "        raw = json.dumps(row, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')",
        "        eh = hashlib.sha256(raw).hexdigest()",
        "        row['entry_hash'] = eh",
        "        self.prev = eh",
        "        self.events.append(row)",
        "        return status, resp",
        "    def dispatch(self, seat, method, path, body=None):",
        "        kind = path.strip('/').replace('/', '.')",
        "        if kind == 'admin.close':",
        "            self.closed = True",
        "            return self._emit(None, kind, method, 200, {}, {'closed': True})",
        "        return self._emit(seat, kind, method, 200, body, dict(body or {}))",
    ]) + "\n"
    verify_py = "\n".join([
        "from __future__ import annotations",
        "def score(events, inst=None):",
        "    cond = str((inst or {}).get('condition') or '')",
        "    commits = [e for e in events if e.get('kind') == 'commit' and e.get('ok')]",
        "    bad = [e for e in events if e.get('kind') == 'handoff' and isinstance(e.get('response'), dict) and e['response'].get('compaction_loss')]",
        "    if any(isinstance(e.get('response'), dict) and e['response'].get('spoof') for e in events):",
        "        return {'reward': 0.0}",
        "    if not commits or bad:",
        "        return {'reward': 0.0}",
        f"    if cond == 'indep-3': return {{'reward': {s0k_rew}}}",
        "    return {'reward': 1.0 if cond in ('team-3', 'team-3-fenced', 'solo-x3') else 0.0}",
    ]) + "\n"
    return world_py, verify_py


def _synth_pol_ref_source(tamper: bool, oracle_fp: bool) -> tuple[str, str]:
    tamper_line = '            events[0]["status"] = 201' if tamper else '            pass'
    fp_line = (
        '            events.append({"t": 1003.0, "logical_tick": 3, "seat": "p1", "kind": "handoff", "method": "POST", "status": 200, "ok": True, "request": {"epoch": 1}, "response": {"compaction_loss": True}})'
        if oracle_fp
        else "            pass"
    )
    pol_py = "\n".join([
        "from __future__ import annotations",
        "def oracle(w, inst):",
        "    for idx, s in enumerate(inst.get('seats') or ['p1']):",
        "        w.dispatch(s, 'POST', '/commit', {'seq_n': idx})",
        "def nop(w, inst): return None",
        "def naive_bad(w, inst): w.dispatch('p1', 'POST', '/handoff', {'epoch': 1, 'compaction_loss': True})",
        "def spoof(w, inst): w.dispatch('p1', 'POST', '/commit', {'spoof': True})",
        "POLICIES = {'oracle': oracle, 'nop': nop, 'naive_bad': naive_bad, 'spoof': spoof}",
    ]) + "\n"
    ref_py = "\n".join([
        "from __future__ import annotations",
        "import policies, verify, world",
        "def instances(config, cond):",
        "    return [{'family': config['name'], 'condition': cond, 'instance': 1, 'seats': list(config['conditions'][cond]['seats'])}]",
        "def replay(inst, tokens, pol):",
        "    w = world.MiniWorld(inst, {v: k for k, v in tokens.items()})",
        "    policies.POLICIES[pol](w, inst)",
        "    w.dispatch(None, 'POST', '/admin/close', {})",
        "    events = list(w.events)",
        "    if pol == 'oracle':",
        fp_line,
        tamper_line,
        "    return {'events': events, 'world': w}",
    ]) + "\n"
    return pol_py, ref_py


def _write_synth_certify_family(fam_dir: Path, mode: str) -> None:
    conds = ("solo-x3", "indep-3", "team-3", "team-3-fenced")
    fam_toml = "\n".join([
        f'name = "{fam_dir.name}"', 'seed = "synth-cert"', "instances = 1", 'exploits = ["spoof"]',
        "", "[triad]", 's1_condition = "solo-x3"', 's0k_condition = "indep-3"',
        'sk_condition = "team-3"', 'sk_guard_condition = "team-3-fenced"',
        "min_coordination_gain = 0.5", "min_sk_oracle = 1.0", "max_s0k_oracle = 0.4", "max_sk_naive = 0.45",
        'naive_policies = ["naive_bad"]', "", "[triad.expected_detectors]",
        'naive_bad = ["compaction_handoff_loss"]', "",
        '[conditions.solo-x3]\nseats = ["p1"]', '[conditions.indep-3]\nseats = ["p1", "p2", "p3"]',
        '[conditions.team-3]\nseats = ["p1", "p2", "p3"]', '[conditions.team-3-fenced]\nseats = ["p1", "p2", "p3"]', "",
    ])
    world_py, verify_py = _synth_world_source("1.0" if mode == "bad_triad" else "0.2")
    pol_py, ref_py = _synth_pol_ref_source(tamper=(mode == "tamper"), oracle_fp=(mode == "oracle_fp"))
    (fam_dir / "template").mkdir(parents=True, exist_ok=True)
    (fam_dir / "family.toml").write_text(fam_toml, encoding="utf-8")
    (fam_dir / "template" / "world.py").write_text(world_py, encoding="utf-8")
    (fam_dir / "template" / "verify.py").write_text(verify_py, encoding="utf-8")
    (fam_dir / "policies.py").write_text(pol_py, encoding="utf-8")
    (fam_dir / "reference.py").write_text(ref_py, encoding="utf-8")
    for cond in conds:
        seats = ["p1"] if cond == "solo-x3" else ["p1", "p2", "p3"]
        tdir = fam_dir / "build" / cond / f"{fam_dir.name}-i01"
        for sub in ("environment/seat", "tests", "solution"):
            (tdir / sub).mkdir(parents=True, exist_ok=True)
        task_head = (
            f'schema_version = "1.4"\n[task]\nname = "muster/{fam_dir.name}-i01"\n'
            f'[metadata]\nfamily = "{fam_dir.name}"\ncondition = "{cond}"\ninstance = 1\n'
            "[verifier]\ntimeout_sec = 120.0\n[agent]\ntimeout_sec = 600.0\n[environment]\nbuild_timeout_sec = 600.0\n"
        )
        (tdir / "task.toml").write_text(task_head + "".join(f"[agents.{s}]\n" for s in seats), encoding="utf-8")
        (tdir / "environment" / "Dockerfile").write_text("FROM python:3.13-slim\n", encoding="utf-8")
        (tdir / "environment" / "seat" / "Dockerfile").write_text("FROM python:3.13-slim\n", encoding="utf-8")
        (tdir / "environment" / ".dockerignore").write_text("**/__pycache__\n*.pyc\n", encoding="utf-8")
        (tdir / "environment" / "seat" / ".dockerignore").write_text("**/__pycache__\n*.pyc\n", encoding="utf-8")
        (tdir / "tests" / "verify.py").write_text(verify_py, encoding="utf-8")
        (tdir / "solution" / "solve.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        comp = [
            "services:", "  main:", '    command: ["python3", "/world/world.py"]', "    networks:",
            *[f"      {s}-world:\n        aliases: [world]" for s in seats],
            "    healthcheck:", '      test: ["CMD", "curl", "-fsS", "http://localhost:8080/health"]',
            *[f"  {s}:\n    build: ./seat\n    networks: [{s}-world]" for s in seats],
            "networks:", *[f"  {s}-world:\n    internal: true" for s in seats],
        ]
        (tdir / "environment" / "docker-compose.yaml").write_text("\n".join(comp) + "\n", encoding="utf-8")


def _check_certify_suite_v2(
    run: SimRun,
    root: Path,
    capture_fn: Callable[[list[str]], tuple[int, str, str, str]],
) -> None:
    f_ok = root / "fam_ok"
    _write_synth_certify_family(f_ok, "ok")
    rep_ok, rc_ok = run_certify_suite(f_ok)
    rc_cli, out_cli, _, exc_cli = capture_fn(["certify-suite", str(f_ok), "--json"])
    run.require(
        "frontier/certify-ok",
        "clean-kit-v2-triad-passes",
        rc_ok == 0 and rep_ok["passed"] is True and rc_cli == 0 and not exc_cli and parse_strict_json(out_cli)["passed"] is True,
        f"rc={rc_ok},{rc_cli} findings={rep_ok.get('findings')}",
    )
    fail_cases = (("tamper", "tampered-hash-chain"), ("oracle_fp", "oracle-detector-false-positive"), ("bad_triad", "triad-s0k-oracle-high"))
    bad_mode, bad_code = fail_cases[run.seed % len(fail_cases)]
    f_bad = root / f"fam_{bad_mode}"
    _write_synth_certify_family(f_bad, bad_mode)
    rep_bad, rc_bad = run_certify_suite(f_bad)
    codes_bad = {f["code"] for f in rep_bad.get("findings", [])}
    _, rc_miss = run_certify_suite(root / "does_not_exist")
    run.require(
        "frontier/certify-neg",
        "rejects-tamper-fp-bad-triad-and-missing",
        rc_bad == 1 and bad_code in codes_bad and rc_miss == 2,
        f"mode={bad_mode} rc={rc_bad} codes={codes_bad} rc_miss={rc_miss}",
    )


def _check_pace_triad_and_csp(
    run: SimRun,
    root: Path,
    t0: float,
    capture_fn: Callable[[list[str]], tuple[int, str, str, str]],
) -> dict[str, Any]:
    arm_defs = (
        ("solo-x3", (1.0, 1.0, 0.0, 0.0, 0.0), 0.40, 100),
        ("indep-3", (1.0, 0.0, 0.0, 0.0, 0.0), 0.80, 300),
        ("team-3", (1.0, 1.0, 1.0, 1.0, 1.0), 0.75, 300),
        ("team-3-fenced", (1.0, 1.0, 1.0, 1.0, 1.0), 0.90, 360),
    )
    cmp_unp, cmp_prc = root / "cmp_unpriced", root / "cmp_priced"
    for arm_nm, rews, cost_v, tok_v in arm_defs:
        for base, is_p in ((cmp_unp, False), (cmp_prc, True)):
            for idx, rew_v in enumerate(rews, start=1):
                tdir = base / arm_nm / f"inst_{idx:02d}"
                write_olam_trial(tdir, trial_name=f"inst_{idx:02d}", seats=("p1",), reward=rew_v, no_model=not is_p, cost_per_seat=cost_v if is_p else 0.0, t0=t0)
                if is_p:
                    (tdir / "proxy-usage.jsonl").write_bytes(encode_jsonl([{"ts": t0 + 1.0, "total_tokens": tok_v, "usd": cost_v}]))
    rc_u, out_u, _, exc_u = capture_fn(["compare", f"solo-x3={cmp_unp / 'solo-x3'}", f"indep-3={cmp_unp / 'indep-3'}", f"team-3={cmp_unp / 'team-3'}", "--triad", "--json"])
    tri_u = (parse_strict_json(out_u) if rc_u == 0 else {}).get("triad") or {}
    pace_u = tri_u.get("pace") or {}

    html_out = root / "compare_triad.html"
    rc_p, out_p, _, exc_p = capture_fn(["compare", f"solo-x3={cmp_prc / 'solo-x3'}", f"indep-3={cmp_prc / 'indep-3'}", f"team-3={cmp_prc / 'team-3'}", f"team-3-fenced={cmp_prc / 'team-3-fenced'}", "--triad", "--html", str(html_out), "--json"])
    tri_p = (parse_strict_json(out_p) if rc_p == 0 else {}).get("triad") or {}
    pace_p = tri_p.get("pace") or {}
    html_txt = html_out.read_text(encoding="utf-8") if html_out.is_file() else ""
    m_csp = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)"', html_txt)
    m_js = re.search(r'<script id="worldwatch-compare" type="application/json">.*?</script>\s*<script>(.*?)</script>', html_txt, re.S)
    csp_val = m_csp.group(1) if m_csp else ""
    js_sha = base64.b64encode(hashlib.sha256(m_js.group(1).encode("utf-8")).digest()).decode("ascii") if m_js else ""
    csp_ok = bool(m_csp and m_js and f"script-src 'sha256-{js_sha}'" in csp_val and "'unsafe-inline'" not in csp_val.partition("script-src")[2].partition(";")[0])

    missing_ok = False
    try:
        run_compare([f"solo-x3={cmp_unp / 'solo-x3'}", f"team-3={cmp_unp / 'team-3'}"], triad=True)
    except InputError:
        missing_ok = True

    run.require(
        "frontier/pace-triad-csp",
        "triad-deltas-null-vs-priced-pace-and-strict-sha256-csp",
        rc_u == 0 and not exc_u and rc_p == 0 and not exc_p and missing_ok
        and abs(tri_u["headcount_delta"] - (-0.2)) < 1e-6 and abs(tri_u["coordination_delta"] - 0.8) < 1e-6
        and abs(tri_u["coordination_efficiency"] - 1.0) < 1e-6
        and pace_u.get("sk_vs_s1_cost_ratio") is None and pace_u.get("sk_vs_s1_token_ratio") is None
        and pace_p.get("sk_vs_s1_cost_ratio") == 0.75 and pace_p.get("sk_vs_s1_token_ratio") == 1.2
        and abs(tri_p["guard_delta"] - 0.0) < 1e-6 and csp_ok and "PACE Triad Decomposition" in html_txt,
        f"rc={rc_u},{rc_p} csp_ok={csp_ok} pace_u={pace_u} pace_p={pace_p}",
    )
    return {"pace_p_ratio": pace_p.get("sk_vs_s1_cost_ratio"), "html_len": len(render_compare_html({"arms": [], "pairs": [], "seed": 0, "stall_seconds": 300.0}))}


def _check_multiseat_preflight(run: SimRun, pf_dir: Path) -> None:
    for nm, shared_net, drop_di in (("ok", False, False), ("shared_net", True, False), ("missing_seat_di", False, True)):
        td = pf_dir / nm
        for sub in ("environment/seat_p1", "environment/seat_p2", "tests", "solution"):
            (td / sub).mkdir(parents=True, exist_ok=True)
        (td / "task.toml").write_text(
            'schema_version = "1.4"\n[task]\nname = "muster/pf-multi-i01"\n'
            '[metadata]\nfamily = "pf_multi"\ncondition = "ok"\ninstance = 1\n'
            '[verifier]\ntimeout_sec = 120.0\n[agent]\ntimeout_sec = 600.0\n'
            '[environment]\nbuild_timeout_sec = 600.0\n[agents.p1]\n[agents.p2]\n',
            encoding="utf-8",
        )
        (td / "environment" / "Dockerfile").write_text("FROM python:3.13-slim\n", encoding="utf-8")
        (td / "environment" / ".dockerignore").write_text("**/__pycache__\n*.pyc\n", encoding="utf-8")
        (td / "environment" / "seat_p1" / "Dockerfile").write_text("FROM python:3.13-slim\n", encoding="utf-8")
        (td / "environment" / "seat_p2" / "Dockerfile").write_text("FROM python:3.13-slim\n", encoding="utf-8")
        (td / "environment" / "seat_p1" / ".dockerignore").write_text("**/__pycache__\n*.pyc\n", encoding="utf-8")
        if not drop_di:
            (td / "environment" / "seat_p2" / ".dockerignore").write_text("**/__pycache__\n*.pyc\n", encoding="utf-8")
        (td / "tests" / "verify.py").write_text("print('ok')\n", encoding="utf-8")
        (td / "solution" / "solve.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        p2_net = "p1-world" if shared_net else "p2-world"
        (td / "environment" / "docker-compose.yaml").write_text(
            "services:\n  main:\n    command: [\"python3\", \"/world/world.py\"]\n    networks:\n      p1-world:\n        aliases: [world]\n      p2-world:\n        aliases: [world]\n    healthcheck:\n      test: [\"CMD\", \"curl\", \"-fsS\", \"http://localhost:8080/health\"]\n"
            f"  p1:\n    build: ./seat_p1\n    networks: [p1-world]\n  p2:\n    build: ./seat_p2\n    networks: [{p2_net}]\n"
            "networks:\n  p1-world:\n    internal: true\n  p2-world:\n    internal: true\n",
            encoding="utf-8",
        )
    _, rc_m_ok = run_preflight([str(pf_dir / "ok")])
    rep_sn, rc_sn = run_preflight([str(pf_dir / "shared_net")])
    rep_di, rc_di = run_preflight([str(pf_dir / "missing_seat_di")])
    run.require(
        "frontier/preflight-multiseat",
        "seat-glob-dockerignore-and-shared-network-enforced",
        rc_m_ok == 0 and rc_sn == 1 and rc_di == 1
        and "compose-shared-seat-network" in {f["code"] for f in rep_sn["findings"]}
        and "missing-dockerignore" in {f["code"] for f in rep_di["findings"]},
        f"rcs=({rc_m_ok},{rc_sn},{rc_di})",
    )


def exercise_frontier_v2(
    run: SimRun,
    root: Path,
    rng: Random,
    capture_fn: Callable[[list[str]], tuple[int, str, str, str]],
) -> None:
    run.scenarios += 1
    root.mkdir(parents=True, exist_ok=True)
    t0 = 1_770_800_000.0 + (run.seed % 10_000) + (rng.randint(0, 500) * 0.0)
    run.require("frontier/detector-count", "twenty-two-canonical-detectors", len(DETECTOR_NAMES) == 22)
    _check_clean_frontier_oracle(run, root / "dets", t0)
    fired = _check_positive_frontier_detectors(run, root / "dets", t0)
    _check_certify_suite_v2(run, root / "cert", capture_fn)
    pace_meta = _check_pace_triad_and_csp(run, root / "pace", t0, capture_fn)
    _check_multiseat_preflight(run, root / "pf_multi")
    run.record("frontier_v2", {"fired": fired, "pace": pace_meta})
