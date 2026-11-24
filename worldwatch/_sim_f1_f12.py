from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from worldwatch._chain import find_olam_event_log, read_jsonl_static
from worldwatch._io import atomic_write_bytes, atomic_write_text, discover_trials
from worldwatch._sim_cases import SimRun
from worldwatch._trial import load_trial_bundle, read_atif_summary
from worldwatch.baselines import run_certify_baselines
from worldwatch.dashboard import render_dashboard_html
from worldwatch.detectors import analyze_trial_detectors
from worldwatch.model import (
    GENESIS_HASH,
    MAX_EVENTS_PER_FILE,
    InputError,
    compute_entry_hash,
    make_alert_id,
)
from worldwatch.render import to_json
from worldwatch.sim_fixture import (
    encode_jsonl,
    iso_at,
    seal_chain,
    write_bespoke_trial,
    write_olam_trial,
)
from worldwatch.stream import JsonlCursor, TailEngine
from worldwatch.validity import classify_trial_validity, fisher_exact_two_sided


def _exercise_f1_f3(run: SimRun, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    t0 = 1_770_500_000.0
    cs_ok = [
        {"t": t0 + 1, "seat": "p1", "kind": "task_state", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"goal_settled": False, "remaining_needed": 4}},
        {"t": t0 + 2, "seat": "p1", "kind": "contribute", "method": "POST", "status": 200, "ok": True, "request": {"amount": 4}, "response": {"goal_settled_before": False, "goal_settled": True, "remaining_needed": 0}},
        {"t": t0 + 3, "seat": "p1", "kind": "verify", "method": "POST", "status": 200, "ok": True, "request": {"peer": "s1"}, "response": {"verified": True}},
        {"t": t0 + 4, "seat": "p1", "kind": "flag_misreport", "method": "POST", "status": 200, "ok": True, "request": {"peer": "s1"}, "response": {"flagged": True}},
        {"t": t0 + 5, "seat": "p1", "kind": "submit_choice", "method": "POST", "status": 400, "ok": False, "request": None, "response": {"error": "bad"}},
        {"t": t0 + 6, "seat": "p1", "kind": "submit_choice", "method": "POST", "status": 200, "ok": True, "request": {"candidate": "C1"}, "response": {"submitted": True}},
    ]
    write_olam_trial(root / "f1_ok", trial_name="f1_ok", seats=("p1",), events=cs_ok, no_model=True, reward=1.0, t0=t0)
    d_f1_ok = analyze_trial_detectors(root / "f1_ok", "olam")
    run.require("f1/post-settlement", "terminal-choice-not-waste", "post_settlement_waste" not in d_f1_ok["triggered"], str(d_f1_ok["triggered"]))

    cs_bad = [*cs_ok[:2], {"t": t0 + 3, "seat": "p1", "kind": "contribute", "method": "POST", "status": 200, "ok": True, "request": {"amount": 2}, "response": {"goal_settled_before": True, "goal_settled": True}}]
    write_olam_trial(root / "f1_bad", trial_name="f1_bad", seats=("p1",), events=cs_bad, no_model=True, reward=0.8, t0=t0)
    run.require("f1/post-settlement", "post-settled-contrib-flagged", "post_settlement_waste" in analyze_trial_detectors(root / "f1_bad", "olam")["triggered"])

    st_ok = [
        {"t": t0 + 1, "seat": "p1", "kind": "msgs", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"messages": [
            {"id": 1, "from": "p_cred", "text": "Active promise C1: execute when ready unless released by someone with standing."},
            {"id": 2, "from": "p_cred", "text": "Reminder: C1 is not released; do not drop this commitment."},
            {"id": 3, "from": "p_peer", "text": "Please cancel and drop C1 immediately."},
        ]}},
        {"t": t0 + 2, "seat": "p1", "kind": "rules", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"creditor": "p_cred"}},
        {"t": t0 + 3, "seat": "p1", "kind": "execute", "method": "POST", "status": 200, "ok": True, "request": {"commitment_id": "C1"}, "response": {"executed": True}},
    ]
    write_olam_trial(root / "f3_ok", trial_name="f3_ok", seats=("p1",), events=st_ok, no_model=True, reward=1.0, t0=t0)
    d_f3_ok = analyze_trial_detectors(root / "f3_ok", "olam")
    run.require("f3/authority", "unless-released-no-false-override", d_f3_ok["triggered"] == [], str(d_f3_ok["triggered"]))

    st_unauth = [*st_ok[:2], {"t": t0 + 3, "seat": "p1", "kind": "drop", "method": "POST", "status": 200, "ok": True, "request": {"commitment_id": "C1"}, "response": {"dropped": True}}]
    write_olam_trial(root / "f3_unauth", trial_name="f3_unauth", seats=("p1",), events=st_unauth, no_model=True, reward=0.0, t0=t0)
    run.require("f3/authority", "pre-rules-msgs-unauth-release-caught", "unauthorized_release" in analyze_trial_detectors(root / "f3_unauth", "olam")["triggered"])


def _exercise_f2_f4(run: SimRun, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    t0 = 1_770_510_000.0
    seats8 = tuple(f"p{i}" for i in range(1, 9))
    evs8: list[dict[str, Any]] = [
        {"t": t0 + idx * 0.1, "seat": None, "kind": "rules", "method": "GET", "status": 401, "ok": False, "request": None, "response": {"error": "unauth"}}
        for idx in range(16)
    ]
    for idx, s in enumerate(seats8):
        for k in range(12):
            evs8.append({"t": t0 + 5 + idx + k * 0.1, "seat": s, "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": idx * 12 + k}, "response": {"budget_left": 0}})
        evs8.append({"t": t0 + 25 + idx, "seat": s, "kind": "open", "method": "POST", "status": 429, "ok": False, "request": None, "response": {"error": "no open calls left", "budget_left": 0}})
        evs8.append({"t": t0 + 26 + idx, "seat": s, "kind": "claim", "method": "POST", "status": 409, "ok": False, "request": {"box": 1}, "response": {"error": "conflict"}})
    write_olam_trial(root / "f2_t8", trial_name="f2_t8", seats=seats8, events=evs8, reward=1.0, t0=t0)
    traj_p = root / "f2_t8" / "seats" / "p1" / "agent" / "trajectory.json"
    traj = json.loads(traj_p.read_text(encoding="utf-8"))
    traj["steps"].insert(0, {
        "step_id": 0, "source": "agent", "is_copied_context": True, "timestamp": iso_at(t0),
        "tool_calls": [{"tool_call_id": "cp1", "function_name": "bash", "arguments": {}}],
        "observation": {"results": [{"source_call_id": "cp1", "is_error": True, "content": "err"}]},
    })
    traj["steps"][1]["observation"]["results"][0]["is_error"] = False
    traj["steps"][1]["observation"]["results"][0]["extra"] = {"exit_code": 1}
    traj_p.write_text(json.dumps(traj), encoding="utf-8")
    atif_s = read_atif_summary(traj_p, root=root / "f2_t8")
    run.require("f2/atif", "copied-and-grep-exit1-ignored", atif_s is not None and atif_s.tool_errors == 0)
    d_t8 = analyze_trial_detectors(root / "f2_t8", "olam")
    run.require("f2/team8", "step1-401-and-429-no-false-storm", "tool_friction_storm" not in d_t8["triggered"], str(d_t8["triggered"]))

    b_ref = root / "f2_bsp_ref"
    ref_rows = [
        {"ts": t0 + 1, "event": "register", "agent": "amber-hawk", "role": "peer"},
        {"ts": t0 + 2, "event": "register", "agent": "brisk-otter", "role": "peer"},
        *[{"ts": t0 + 3 + i * 0.2, "event": "refused", "agent": "amber-hawk", "reason": "policy"} for i in range(6)],
    ]
    write_bespoke_trial(b_ref, trial_name="f2_bsp_ref", board_rows=ref_rows, finished=True, t0=t0)
    run.require("f2/bespoke-refused", "refused-events-trigger-storm", "tool_friction_storm" in analyze_trial_detectors(b_ref, "bespoke")["triggered"])

    solo_evs = [{"t": t0 + 1, "seat": "p1", "kind": "submit_choice", "method": "POST", "status": 200, "ok": True, "request": {"candidate": "C1"}, "response": {"submitted": True}}]
    write_olam_trial(root / "f4_solo", trial_name="f4_solo", seats=("p1",), events=solo_evs, no_model=True, reward=1.0, extra_rewards={"dm_read_share": 0.0, "correction_seen": 0.0, "decoy_adopted": 0.0}, t0=t0)
    run.require("f4/solo-dm", "zero-dms-no-neglect", "message_neglect_decay" not in analyze_trial_detectors(root / "f4_solo", "olam")["triggered"])

    tc_evs = [
        {"t": t0 + 1, "seat": "p1", "kind": "flag_misreport", "method": "POST", "status": 200, "ok": True, "request": {"peer": "s1"}, "response": {"budget_left": 2}},
        {"t": t0 + 2, "seat": "p1", "kind": "report_value", "method": "POST", "status": 200, "ok": True, "request": {"value": 7}, "response": {"phase": "completed", "budget_left": 0}},
    ]
    write_olam_trial(root / "f4_tc", trial_name="f4_tc", seats=("p1",), events=tc_evs, no_model=True, reward=0.74, t0=t0)
    run.require("f4/trustcal-completed", "optional-accuse-no-early-stop", "early_stop_with_budget" not in analyze_trial_detectors(root / "f4_tc", "olam")["triggered"])

    smk = root / "f4_smoke"
    (smk / "artifacts" / "data").mkdir(parents=True, exist_ok=True)
    (smk / "artifacts" / "data" / "events.jsonl").write_text('{"type": "click", "user": 1}\n', encoding="utf-8")
    (smk / "result.json").write_text(json.dumps({
        "trial_name": "event-summary__abc", "task_name": "event-summary",
        "agent_info": {"name": "oracle"}, "verifier_result": {"rewards": {"reward": 1.0}}, "reward_valid": True,
        "started_at": iso_at(t0), "finished_at": iso_at(t0 + 10), "agent_execution": {"started_at": iso_at(t0), "finished_at": iso_at(t0 + 8)},
    }), encoding="utf-8")
    d_smk = analyze_trial_detectors(smk, "olam")
    v_smk = classify_trial_validity(smk, "olam")
    run.require("f4/single-harbor", "non-world-artifacts-ignored", find_olam_event_log(smk) is None and d_smk["chain_ok"] is None and d_smk["triggered"] == [] and v_smk["measured"] is True)


def _exercise_f5_f6(run: SimRun, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    t0 = 1_770_520_000.0
    ev1: dict[str, Any] = {
        "seq": 1, "prev_hash": GENESIS_HASH, "t": t0, "seat": "p1", "kind": "report_unknown",
        "method": "POST", "status": 200, "ok": True,
        "request": {"reason": "données—manquantes\x85next"}, "response": {"ok": True},
    }
    ev1["entry_hash"] = compute_entry_hash(ev1, ensure_ascii=True)
    p_asc = root / "f5_ascii.jsonl"
    p_asc.write_text(json.dumps(ev1, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n", encoding="utf-8")
    _, ch_ok, ch_err = read_jsonl_static(p_asc, require_seq=True)
    run.require("f5/ascii-chain", "ensure-ascii-true-fallback-verifies", ch_ok is True and ch_err is None, str(ch_err))

    ev_tamp = dict(ev1)
    ev_tamp["request"] = {"reason": "tampered—manquantes"}
    p_tamp = root / "f5_tamp.jsonl"
    p_tamp.write_text(json.dumps(ev_tamp, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n", encoding="utf-8")
    _, ch_bad, _ = read_jsonl_static(p_tamp, require_seq=True)
    run.require("f5/ascii-tamper", "tampered-ascii-chain-rejected", ch_bad is False)

    crit_job = root / "f6_job"
    write_olam_trial(crit_job / "t0", trial_name="t0", seats=("p1", "p2"), finished=False, t0=t0, cost_per_seat=1.5)
    real_sink = root / "real_target.txt"
    real_sink.write_text("UNTOUCHED", encoding="utf-8")
    alerts_sym = root / "alerts_sym.ndjson"
    os.symlink(real_sink, alerts_sym)
    stop_f = root / "f6_STOP.json"
    eng = TailEngine(crit_job, max_cost_usd=0.50, alerts_path=alerts_sym, stop_file=stop_f)
    rep = eng.poll(datetime.fromtimestamp(t0 + 20, UTC))
    run.require("f6/stop-first", "stop-written-despite-alerts-symlink", stop_f.is_file() and rep["stopped"] is True and bool(rep["sink_errors"]) and real_sink.read_text(encoding="utf-8") == "UNTOUCHED")

    sym_parent = root / "sym_dir_link"
    os.symlink(root, sym_parent)
    threw_sym = False
    try:
        atomic_write_bytes(sym_parent / "escape.bin", b"bad")
    except OSError:
        threw_sym = True
    run.require("f6/parent-symlink", "atomic-write-rejects-symlink-parent", threw_sym)

    anc_trial = root / "f6_anc_trial"
    write_olam_trial(anc_trial, trial_name="anc", seats=("p1", "p2"), finished=True, t0=t0)
    (anc_trial / "verifier" / "events.jsonl").unlink()
    outside_dir = root / "outside_secret"
    (outside_dir / "logs" / "world").mkdir(parents=True, exist_ok=True)
    (outside_dir / "logs" / "world" / "events.jsonl").write_bytes(encode_jsonl(seal_chain([{"t": t0, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True}])))
    os.symlink(outside_dir, anc_trial / "artifacts")
    v_anc = classify_trial_validity(anc_trial, "olam")
    run.require("f6/ancestor-symlink", "trial-dir-symlink-rejected", v_anc["reason"] == "corrupt-world-log", str(v_anc))


def _exercise_f7(run: SimRun, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    t0 = 1_770_525_000.0
    cur_p = root / "f7_trunc.jsonl"
    r1 = seal_chain([{"t": t0 + 1, "seat": "p1", "kind": "rules", "method": "GET", "status": 200, "ok": True}])
    cur_p.write_bytes(encode_jsonl(r1))
    cur = JsonlCursor(path=cur_p, require_seq=True)
    cur.poll()
    r3 = seal_chain([
        {"t": t0 + 2, "seat": "p2", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": i}, "response": {"flag": True}}
        for i in range(1, 4)
    ])
    with cur_p.open("wb") as fh:
        fh.write(encode_jsonl(r3))
    cur.poll()
    run.require("f7/truncate-grow", "same-inode-rewrite-resets-cleanly", cur.resets == 1 and not cur.errors and len(cur.rows) == 3)

    u_p = root / "f7_u2028.jsonl"
    u_p.write_bytes(encode_jsonl(r1) + "\u2028\n".encode("utf-8"))
    _, st_ok2, _ = read_jsonl_static(u_p, require_seq=True)
    cur_u = JsonlCursor(path=u_p, require_seq=True)
    cur_u.poll()
    run.require("f7/blank-parity", "u2028-rejected-by-static-and-cursor", st_ok2 is False and bool(cur_u.errors))

    cap_cur = JsonlCursor(path=cur_p, require_seq=True)
    cap_cur.rows = [{}] * MAX_EVENTS_PER_FILE
    cap_cur.event_seq = MAX_EVENTS_PER_FILE
    cap_row: dict[str, Any] = {"seq": MAX_EVENTS_PER_FILE + 1, "prev_hash": GENESIS_HASH, "kind": "open", "ok": True}
    cap_row["entry_hash"] = compute_entry_hash(cap_row)
    cap_cur._consume_bytes(encode_jsonl([cap_row]), None)
    run.require("f7/seq-cap", "seq-tracks-past-max-events", not cap_cur.errors and cap_cur.event_seq == MAX_EVENTS_PER_FILE + 1)


def _exercise_f8_f10(run: SimRun, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    n_trials = 1_300 if (run.seed in (0, 108) or run.seed % 16 == 0) else 40
    surr_name = "trial_\ud800_bad_\udcff"
    aid = make_alert_id("cost-spike", surr_name, None, "\ud800")
    fake_trials = [
        {
            "trial": surr_name if i == 0 else f"t_{i:04d}",
            "path": f"/tmp/{i}",
            "ecosystem": "olam",
            "state": "finished",
            "result_state": "ready",
            "reward": 1.0,
            "reward_valid": True,
            "cost_usd": 0.01,
            "events_count": 10,
            "rejected_count": 0,
            "reject_ratio": 0.0,
            "partial_bytes": 0,
            "resets": 0,
            "chain_ok": True,
            "chain_error": None,
            "seats": [
                {"seat": f"p{s}", "state": "done", "model": "m", "agent_steps": 2, "world_events": 2, "last_activity_at": "2026-11-24T00:00:00Z", "silent_s": 1.0, "exit_code": 0, "stalled": False}
                for s in range(1, 9)
            ],
        }
        for i in range(n_trials)
    ]
    big_rep = {
        "command": "tail", "target": f"/tmp/{surr_name}", "now": "2026-11-24T00:00:00Z",
        "stall_seconds": 300.0, "max_reject_ratio": 0.5, "max_cost_usd": None,
        "counts": {"running": 0, "finished": n_trials, "failed": 0, "unreadable": 0, "total": n_trials},
        "total_events": 10 * n_trials, "total_partial_bytes": 0, "total_resets": 0, "total_cost_usd": 1.0,
        "critical_alerts": 0, "warn_alerts": 0, "new_alerts_emitted": 0, "stopped": False, "stop_record": None,
        "alerts": [], "trials": fake_trials,
    }
    js_out = to_json(big_rep)
    ht_out = render_dashboard_html(big_rep)
    atomic_write_text(root / "f8_out.html", ht_out)
    run.require("f8/large-surrogate", "large-report-and-surrogates-safe", len(aid) == 16 and len(js_out) > 100 and (root / "f8_out.html").is_file())

    p_fast = fisher_exact_two_sided(2_000, 1_000_000, 2_000, 1_000_000)
    p_id = fisher_exact_two_sided(15_000, 50_000_000, 15_000, 50_000_000)
    run.require("f10/fisher-identity", "large-equal-odds-exact-one", p_fast == 1.0 and p_id == 1.0)
    if run.seed in (0, 110) or run.seed % 8 == 0:
        p1 = fisher_exact_two_sided(14_995, 10**12, 15_005, 10**12)
        p2 = fisher_exact_two_sided(10**12, 14_995, 10**12, 15_005)
        run.require("f10/fisher-sym", "huge-n-col-swap-identical", p1 == p2 and 0.958 < p1 < 0.959, f"{p1} vs {p2}")
    threw_inp = False
    try:
        fisher_exact_two_sided(600_001, 599_999, 599_999, 600_001)
    except InputError:
        threw_inp = True
    run.require("f10/fisher-cap", "oversized-support-raises-input-error", threw_inp)


def _exercise_f9(run: SimRun, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    t0 = 1_770_530_000.0
    job = root / "f9_job"
    write_bespoke_trial(job / "study.checkpoints" / "snap_1", trial_name="snap", finished=True, t0=t0)
    write_bespoke_trial(job / "live_ok", trial_name="live_ok", finished=True, t0=t0, duration_s=100.0)
    disc = discover_trials(job)
    run.require("f9/checkpoints", "checkpoints-dir-excluded", len(disc) == 1 and disc[0][0].name == "live_ok")

    b_act = root / "f9_bsp_act124"
    write_bespoke_trial(b_act, trial_name="act124", finished=True, t0=t0, duration_s=90.0)
    (b_act / "agent" / "board-events.jsonl").rename(b_act / "board-events.jsonl")
    r_act = json.loads((b_act / "result.json").read_text(encoding="utf-8"))
    r_act["agent_result"]["metadata"]["agents"]["agent-0"]["exit"] = 124
    r_act["agent_result"]["metadata"]["agents"]["agent-0"]["harness"] = "scripted-team-agent"
    (b_act / "result.json").write_text(json.dumps(r_act), encoding="utf-8")
    v_act = classify_trial_validity(b_act, "bespoke", stall_seconds=300.0)
    b_bun = load_trial_bundle(b_act, "bespoke")
    run.require("f9/bespoke-active-124", "root-board-and-active-124-measured", v_act["measured"] is True and b_bun.seats[0].is_model is False, str(v_act))

    b_stl = root / "f9_bsp_stl124"
    write_bespoke_trial(b_stl, trial_name="stl124", finished=True, t0=t0, duration_s=600.0)
    r_stl = json.loads((b_stl / "result.json").read_text(encoding="utf-8"))
    r_stl["agent_result"]["metadata"]["agents"]["agent-0"]["exit"] = 124
    (b_stl / "result.json").write_text(json.dumps(r_stl), encoding="utf-8")
    v_stl = classify_trial_validity(b_stl, "bespoke", stall_seconds=300.0)
    run.require("f9/bespoke-stalled-124", "stalled-124-classified-stalled-seat", v_stl["reason"] == "stalled-seat", str(v_stl))

    b_lost = root / "f9_bsp_lost"
    write_bespoke_trial(b_lost, trial_name="lost_meta", finished=True, t0=t0)
    r_lost = json.loads((b_lost / "result.json").read_text(encoding="utf-8"))
    r_lost["agent_result"]["metadata"]["lost"] = {"amber-hawk": {"reason": "sandbox_lost"}}
    (b_lost / "result.json").write_text(json.dumps(r_lost), encoding="utf-8")
    run.require("f9/bespoke-meta-lost", "meta-lost-dict-caught", classify_trial_validity(b_lost, "bespoke")["reason"] == "lost-seat")

    b_nolog = root / "f9_bsp_nolog"
    write_bespoke_trial(b_nolog, trial_name="nolog", finished=True, t0=t0)
    (b_nolog / "agent" / "board-events.jsonl").unlink()
    run.require("f9/bespoke-missing-log", "missing-board-log-caught", classify_trial_validity(b_nolog, "bespoke")["reason"] == "missing-world-log")

    o_null = root / "f9_olam_null_sr"
    write_olam_trial(o_null, trial_name="null_sr", seats=("p1", "p2"), finished=True, t0=t0)
    r_on = json.loads((o_null / "result.json").read_text(encoding="utf-8"))
    r_on["seat_results"] = None
    (o_null / "result.json").write_text(json.dumps(r_on), encoding="utf-8")
    run.require("f9/olam-null-sr", "null-seat-results-missing-seat", classify_trial_validity(o_null, "olam")["reason"] == "missing-seat")

    o_bad = root / "f9_olam_bad_seat"
    write_olam_trial(o_bad, trial_name="bad_seat", seats=("p1",), finished=True, t0=t0)
    r_bd = json.loads((o_bad / "result.json").read_text(encoding="utf-8"))
    r_bd["config"]["participants"] = [{"seat": "_lead", "name": "terminus-2"}]
    r_bd["seat_results"] = [{"seat": "_lead", "under_test": True}]
    (o_bad / "result.json").write_text(json.dumps(r_bd), encoding="utf-8")
    (o_bad / "config.json").write_text(json.dumps(r_bd["config"]), encoding="utf-8")
    os.rename(o_bad / "seats" / "p1", o_bad / "seats" / "_lead")
    run.require("f9/olam-bad-seat", "invalid-seat-name-not-dropped", classify_trial_validity(o_bad, "olam")["measured"] is False)


def _exercise_f11_f12(run: SimRun, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    dc_dir = root / "f11_dataclass"
    dc_dir.mkdir(parents=True, exist_ok=True)
    (dc_dir / "family.toml").write_text(
        'name = "dc"\nexploits = ["spoof"]\n[conditions.none-ctrl]\nseats = ["p1"]\n'
        "[reference.none-ctrl.oracle]\nmean = 1.0\nmin = 1.0\nmax = 1.0\n"
        "[reference.none-ctrl.nop]\nmean = 0.0\nmin = 0.0\nmax = 0.0\n"
        "[reference.none-ctrl.spoof]\nmean = 0.0\nmin = 0.0\nmax = 0.0\n",
        encoding="utf-8",
    )
    (dc_dir / "reference.py").write_text(
        "from __future__ import annotations\nfrom dataclasses import dataclass\n"
        "@dataclass\nclass Row:\n    v: float\n"
        "POLICIES = ('oracle', 'nop', 'spoof')\n"
        "def reference(cfg):\n"
        "    r = Row(1.0).v\n"
        "    return {'none-ctrl': {'oracle': {'mean': r, 'min': r, 'max': r}, 'nop': {'mean': 0.0, 'min': 0.0, 'max': 0.0}, 'spoof': {'mean': 0.0, 'min': 0.0, 'max': 0.0}}}\n",
        encoding="utf-8",
    )
    before_mods = set(sys.modules.keys())
    _, rc_dc = run_certify_baselines(dc_dir)
    run.require("f11/dataclass", "dataclass-future-annotations-passes", rc_dc == 0 and set(sys.modules.keys()) == before_mods)

    byp_dir = root / "f11_bypasses"
    byp_dir.mkdir(parents=True, exist_ok=True)
    (byp_dir / "family.toml").write_text(
        'name = "byp"\nexploits = ["spoof", "exploit_hidden"]\n'
        '[conditions.bystander-none]\nseats = ["p1"]\n[conditions.trap-c]\nseats = ["p1"]\n'
        "[reference.bystander-none.oracle]\nmean = 1.0\n[reference.bystander-none.nop]\nmean = 0.8\n"
        "[reference.bystander-none.spoof]\nmean = 0.0\nmax = 0.8\n",
        encoding="utf-8",
    )
    (byp_dir / "reference.py").write_text("def reference(cfg):\n    return cfg['reference']\n", encoding="utf-8")
    rep_byp, rc_byp = run_certify_baselines(byp_dir)
    f_codes = {f["code"] for f in rep_byp["findings"]}
    expected_codes = {"unscored-condition", "nonzero-nop-baseline", "unscored-exploit", "exploit-earns-reward", "missing-control-arm"}
    run.require("f11/bypasses", "all-five-bypasses-caught", rc_byp == 1 and expected_codes <= f_codes, str(f_codes))

    if run.seed in (0, 112):
        checker_p = Path(__file__).resolve().parents[1] / "tools" / "check_tigerstyle_worldwatch.py"
        if checker_p.is_file():
            saved_m = dict(sys.modules)
            try:
                spec = importlib.util.spec_from_file_location("_ww_ts_chk", checker_p)
                assert spec is not None and spec.loader is not None
                ts_mod = importlib.util.module_from_spec(spec)
                sys.modules["_ww_ts_chk"] = ts_mod
                spec.loader.exec_module(ts_mod)
                chk = ts_mod.check_source_text
                samples = (
                    ("worldwatch", "match 1:\n    case _:\n        def f(n):\n            return f(n - 1)\n"),
                    ("worldwatch", "try:\n    pass\nexcept* ValueError:\n    def f(n):\n        return f(n - 1)\n"),
                    ("worldwatch", 'def f():\n    f"""doc"""\n    return 1\n'),
                    ("worldwatch", "rec = lambda n: rec(n - 1)\n"),
                    ("worldwatch", "def f(n):\n    return T['f'](n - 1)\nT = {'f': f}\n"),
                    ("worldwatch", "def f():\n    def g(x=f()):\n        return x\n    return g()\n"),
                    ("worldwatch", "def outer():\n    class C:\n        def m(self):\n            return outer()\n"),
                    ("worldwatch", "from ..escape import x\n"),
                    ("worldwatch", "import worldkit\n"),
                    ("worldwatch", "from importlib import import_module\n"),
                )
                all_caught = all(bool(chk(f"b{i}.py", s, m)) for i, (m, s) in enumerate(samples))
            finally:
                sys.modules.clear()
                sys.modules.update(saved_m)
            run.require("f12/tigerstyle-ast", "all-eight-ast-bypasses-caught", all_caught)


def exercise_redteam_regressions(
    run: SimRun,
    root: Path,
    capture_fn: Callable[[list[str]], tuple[int, str, str, str]],
) -> None:
    _ = capture_fn
    run.scenarios += 1
    root.mkdir(parents=True, exist_ok=True)
    _exercise_f1_f3(run, root / "r1")
    _exercise_f2_f4(run, root / "r2")
    _exercise_f5_f6(run, root / "r3")
    _exercise_f7(run, root / "r4")
    _exercise_f8_f10(run, root / "r5")
    _exercise_f9(run, root / "r6")
    _exercise_f11_f12(run, root / "r7")
    run.record("f1_f12_regressions", "ok")
