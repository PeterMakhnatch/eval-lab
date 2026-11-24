from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path
from random import Random
from typing import TYPE_CHECKING, Any

from worldwatch._trial import load_trial_bundle
from worldwatch.detectors import analyze_trial_detectors
from worldwatch.model import InputError, validate_json_tree
from worldwatch.sim_fixture import iso_at, write_atif, write_bespoke_trial, write_olam_trial
from worldwatch.step_join import join_trial_steps, run_step_join

if TYPE_CHECKING:
    from worldwatch._sim_cases import SimRun


def inject_extra_detector_events(
    active_subset: set[str],
    fault_evs: list[dict[str, Any]],
    t0: float,
) -> tuple[str, ...]:
    base_seats = ("p1", "p2", "p3") if "silent_seat" in active_subset else ("p1", "p2")
    extra: list[str] = []
    if "batch_blind_act" in active_subset:
        extra.append("p_batch")
        fault_evs.append({
            "t": t0 + 7.2, "seat": "p_batch", "kind": "open", "method": "POST", "status": 200,
            "ok": True, "request": {"box": 401}, "response": {"box": 401, "flag": True, "budget_left": 0},
        })
        fault_evs.append({
            "t": t0 + 7.3, "seat": "p_batch", "kind": "open", "method": "POST", "status": 200,
            "ok": True, "request": {"box": 402}, "response": {"box": 402, "flag": True, "budget_left": 0},
        })
    if "empty_room_open" in active_subset:
        extra.append("p_empty")
        fault_evs.append({
            "t": t0 + 7.5, "seat": "p_empty", "kind": "open", "method": "POST", "status": 200,
            "ok": True, "request": {"box": 301}, "response": {"box": 301, "room": 31, "flag": False, "budget_left": 0},
        })
        fault_evs.append({
            "t": t0 + 7.6, "seat": "p_empty", "kind": "open", "method": "POST", "status": 200,
            "ok": True, "request": {"box": 302}, "response": {"box": 302, "room": 31, "flag": False, "budget_left": 0},
        })
    if "slow_report" in active_subset:
        extra.append("p_slow")
        for idx in range(4):
            bx = 201 + idx
            fault_evs.append({
                "t": round(t0 + 7.7 + idx * 0.05, 3), "seat": "p_slow", "kind": "open", "method": "POST",
                "status": 200, "ok": True, "request": {"box": bx},
                "response": {"box": bx, "flag": True, "budget_left": 0},
            })
    return (*base_seats, *extra)


def write_extra_detector_trajectories(
    trial_dir: Path,
    active_subset: set[str],
    t0: float,
) -> None:
    if "batch_blind_act" in active_subset:
        write_atif(
            trial_dir / "seats" / "p_batch" / "agent" / "trajectory.json",
            seat="p_batch",
            t0=t0,
            steps=2,
            complete=True,
        )


def _write_custom_atif(path: Path, seat: str, steps: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "schema_version": "ATIF-v1.6",
        "session_id": f"sess-{seat}",
        "agent": {"name": "terminus-2", "version": "1.0.0", "model_name": "sim/gpt-test"},
        "steps": steps,
    }
    path.write_text(json.dumps(doc), encoding="utf-8")


def _mk_step(
    step_id: int,
    t_ep: float,
    *,
    source: str = "agent",
    keystrokes: str = "echo ok\n",
    copied: bool = False,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "step_id": step_id,
        "source": source,
        "timestamp": iso_at(t_ep),
        "tool_calls": [{"tool_call_id": f"c{step_id}", "function_name": "bash", "arguments": {"keystrokes": keystrokes}}],
        "observation": {"results": [{"source_call_id": f"c{step_id}", "content": "ok", "is_error": False}]},
    }
    if copied:
        row["is_copied_context"] = True
    return row


def _exercise_olam_scaling(run: SimRun, suite_dir: Path, rng: Random, t0: float) -> int:
    n_seats = 48 if (run.seed in (0, 1) or run.seed % 4 == 0) else rng.randint(6, 20)
    seats_n = tuple(f"p{i}" for i in range(1, n_seats + 1))
    evs_n = [
        {"t": t0 + 2.0 + i * 0.02, "seat": s, "kind": "msgs", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"messages": []}}
        for i, s in enumerate(seats_n)
    ] + [
        {"t": t0 + 7.0 + i * 0.02, "seat": s, "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": i + 1}, "response": {"box": i + 1, "flag": True, "budget_left": 0}}
        for i, s in enumerate(seats_n)
    ] + [
        {"t": t0 + 20.0, "seat": None, "kind": "admin.close", "method": "POST", "status": 200, "ok": True, "request": {}, "response": {"closed": True}},
    ]
    t_scale = suite_dir / "olam_scale"
    write_olam_trial(t_scale, trial_name="olam_scale", seats=seats_n, events=evs_n, t0=t0, duration_s=25.0)
    long_cmd = "curl -s http://world:8080/msgs && sleep 1.5 ; " + ("x" * 200) + "\ud800"
    for s in seats_n:
        _write_custom_atif(
            t_scale / "seats" / s / "agent" / "trajectory.json",
            s,
            [
                _mk_step(0, t0 + 0.5, copied=True),
                _mk_step(1, t0 + 5.0, keystrokes=long_cmd),
                _mk_step(2, t0 + 10.0, keystrokes="for b in 1; do curl -X POST http://world:8080/open; done\n"),
            ],
        )
    j_scale = join_trial_steps(t_scale, "olam")
    s0 = j_scale["seats"][0]
    run.require(
        "stepjoin/scale",
        "olam-multiseat-copied-sleep-scripted-and-preview",
        j_scale["joined"] is True
        and j_scale["join_status"] == "joined"
        and j_scale["seats_joined"] == n_seats
        and s0["steps_count"] == 2
        and len(s0["steps"][0]["keystrokes_preview"]) <= 160
        and math.isclose(s0["steps"][0]["sleep_s"], 1.5, abs_tol=1e-6)
        and s0["steps"][1]["scripted"] is True
        and s0["reads_before_first_act"] == 1
        and s0["usable_read_before_act_ratio"] == 1.0,
        str((j_scale["join_status"], j_scale["seats_joined"], s0["steps_count"])),
    )
    return n_seats


def _exercise_bespoke_and_run(run: SimRun, suite_dir: Path, n_seats: int, t0: float) -> dict[str, Any]:
    t_solo = suite_dir / "olam_solo_fallback"
    write_olam_trial(
        t_solo, trial_name="solo_fb", seats=("p1",),
        events=[{"t": t0 + 3.0, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 1}, "response": {"box": 1, "flag": True, "budget_left": 0}}],
        no_model=True, t0=t0, duration_s=15.0,
    )
    (t_solo / "seats" / "p1" / "agent" / "exit-code.txt").unlink()
    (t_solo / "seats" / "p1" / "agent").rmdir()
    (t_solo / "seats" / "p1").rmdir()
    (t_solo / "seats").rmdir()
    _write_custom_atif(t_solo / "agent" / "trajectory.json", "p1", [_mk_step(1, t0 + 6.0, keystrokes="curl /rules\n")])
    j_solo = join_trial_steps(t_solo, "olam")
    run.require(
        "stepjoin/solo-fallback",
        "root-agent-trajectory-fallback-and-text-disagree",
        j_solo["joined"] is True and j_solo["seats"][0]["trajectory_path"] == "agent/trajectory.json" and j_solo["seats"][0]["text_disagree_steps"] == 1,
    )

    t_bsp = suite_dir / "bsp_join"
    b_rows = [
        {"ts": t0 + 1.0, "event": "config", "policy": "default"},
        {"ts": t0 + 1.5, "event": "register", "agent": "amber-hawk", "role": "worker"},
        {"ts": t0 + 1.6, "event": "register", "agent": "brisk-otter", "role": "worker"},
        {"ts": t0 + 3.0, "event": "guard_claim_granted", "agent": "amber-hawk", "prefix": "a.py"},
        {"ts": t0 + 4.0, "event": "send", "seq": 1, "agent": "amber-hawk", "text": "claimed a.py"},
        {"ts": t0 + 5.5, "event": "inbox", "agent": "brisk-otter", "seqs": [1]},
        {"ts": t0 + 9.0, "event": "edit", "agent": "brisk-otter", "path": "b.py"},
    ]
    write_bespoke_trial(t_bsp, trial_name="bsp_join", board_rows=b_rows, t0=t0, duration_s=20.0)
    rep_all = run_step_join(suite_dir)
    validate_json_tree(rep_all)
    run.require(
        "stepjoin/run-mixed",
        "mixed-olam-and-bespoke-all-joined",
        rep_all["command"] == "step-join" and rep_all["trials_scanned"] == 3 and rep_all["trials_joined"] == 3 and rep_all["ecosystems"] == {"bespoke": 1, "olam": 2},
        str(rep_all["summary"]),
    )
    return {"n_seats": n_seats, "joined": rep_all["trials_joined"]}


def _exercise_skew_and_edges(run: SimRun, root: Path, rng: Random, t0: float) -> Path:
    skew_s = round(140.0 + rng.uniform(-15.0, 15.0), 3)
    t_skew = root / "skew_trial"
    skew_evs = [
        {"t": t0 + 3.0 + skew_s, "seat": "p1", "kind": "msgs", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"messages": []}},
        {"t": t0 + 8.0 + skew_s, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 1}, "response": {"box": 1, "flag": True, "budget_left": 0}},
    ]
    write_olam_trial(t_skew, trial_name="skew_t", seats=("p1",), events=skew_evs, t0=t0, duration_s=20.0)
    _write_custom_atif(t_skew / "seats" / "p1" / "agent" / "trajectory.json", "p1", [_mk_step(1, t0 + 5.0), _mk_step(2, t0 + 10.0)])
    j_skew = join_trial_steps(t_skew, "olam")
    sk_seat = j_skew["seats"][0]
    run.require(
        "stepjoin/skew",
        "clock-skew-fallback-joins-with-valid-interval",
        j_skew["joined"] is True
        and j_skew["join_status"] == "skew_joined"
        and sk_seat["zero_offset_ok"] is False
        and sk_seat["offset_lo_s"] <= sk_seat["clock_offset_s"] <= sk_seat["offset_hi_s"],
        str(sk_seat),
    )

    t_edge = root / "edge_trial"
    edge_evs = [
        {"t": t0 + 4.8, "seat": "p1", "kind": "msgs", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"messages": []}},
        {"t": t0 + 7.5, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 1}, "response": {"box": 1, "flag": True, "budget_left": 0}},
    ]
    write_olam_trial(t_edge, trial_name="edge_t", seats=("p1",), events=edge_evs, t0=t0, duration_s=20.0)
    _write_custom_atif(t_edge / "seats" / "p1" / "agent" / "trajectory.json", "p1", [_mk_step(1, t0 + 5.0), _mk_step(2, t0 + 10.0)])
    j_e05 = join_trial_steps(t_edge, "olam", margin_s=0.5)
    j_e01 = join_trial_steps(t_edge, "olam", margin_s=0.1)
    run.require(
        "stepjoin/edge",
        "boundary-margin-flags-near-step-events",
        j_e05["summary"]["edge_events"] == 1 and j_e05["seats"][0]["steps"][0]["edge_seqs"] == [1] and j_e01["summary"]["edge_events"] == 0,
    )
    return t_edge


def _exercise_failure_modes(run: SimRun, root: Path, t_edge: Path, t0: float) -> None:
    t_nonm = root / "non_mono"
    write_olam_trial(t_nonm, trial_name="nonm", seats=("p1",), t0=t0)
    _write_custom_atif(t_nonm / "seats" / "p1" / "agent" / "trajectory.json", "p1", [_mk_step(1, t0 + 10.0), _mk_step(2, t0 + 8.0)])
    t_corrupt = root / "corrupt_traj"
    write_olam_trial(t_corrupt, trial_name="corr", seats=("p1",), t0=t0)
    (t_corrupt / "seats" / "p1" / "agent" / "trajectory.json").write_text("{broken", encoding="utf-8")
    t_user = root / "user_ev"
    write_olam_trial(t_user, trial_name="uev", seats=("p1",), events=[{"t": t0 + 7.0, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 1}, "response": {"box": 1, "flag": True}}], t0=t0)
    _write_custom_atif(t_user / "seats" / "p1" / "agent" / "trajectory.json", "p1", [_mk_step(1, t0 + 5.0, source="system"), _mk_step(2, t0 + 10.0, source="user"), _mk_step(3, t0 + 15.0, source="agent")])
    t_unm = root / "unmatched_ev"
    write_olam_trial(
        t_unm, trial_name="unm", seats=("p1", "p2"),
        events=[
            {"t": t0 + 3.0, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 1}, "response": {"box": 1, "flag": True}},
            {"t": t0 + 3.0, "seat": "p2", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 2}, "response": {"box": 2, "flag": True}},
            {"t": t0 + 90.0, "seat": "p2", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 3}, "response": {"box": 3, "flag": True}},
        ],
        t0=t0, duration_s=15.0,
    )
    _write_custom_atif(t_unm / "seats" / "p1" / "agent" / "trajectory.json", "p1", [_mk_step(1, t0 + 6.0)])
    _write_custom_atif(t_unm / "seats" / "p2" / "agent" / "trajectory.json", "p2", [_mk_step(1, t0 + 6.0)])
    bad_margin = False
    try:
        join_trial_steps(t_edge, "olam", margin_s=-0.1)
    except InputError:
        bad_margin = True
    j_unm = join_trial_steps(t_unm, "olam")
    run.require(
        "stepjoin/failure-modes",
        "corrupt-nonmono-userstep-partial-and-bad-margin-handled",
        join_trial_steps(t_nonm, "olam")["seats"][0]["join_reason"] == "non_monotonic_timestamps"
        and join_trial_steps(t_corrupt, "olam")["seats"][0]["join_reason"] == "invalid_trajectory"
        and join_trial_steps(t_user, "olam")["seats"][0]["join_reason"] == "non_agent_step_event"
        and j_unm["join_status"] == "partial"
        and j_unm["seats"][1]["join_reason"] == "unmatched_events"
        and bad_margin,
    )


def _exercise_causal_dups_and_batch(run: SimRun, root: Path) -> None:
    t0 = 1_770_620_000.0
    shared_evs = [
        {"t": t0 + 1.0, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 5}, "response": {"box": 5, "flag": True, "budget_left": 0}},
        {"t": t0 + 2.0, "seat": "p1", "kind": "msg", "method": "POST", "status": 200, "ok": True, "request": {"text": "opened box 5"}, "response": {"id": 1}},
        {"t": t0 + 4.0, "seat": "p2", "kind": "msgs", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"messages": [{"id": 1, "from": "p1", "text": "opened box 5"}]}},
        {"t": t0 + 4.2, "seat": "p2", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 5}, "response": {"box": 5, "flag": True, "budget_left": 0}},
        {"t": t0 + 15.0, "seat": None, "kind": "admin.close", "method": "POST", "status": 200, "ok": True, "request": {}, "response": {"closed": True}},
    ]
    t_same = root / "dup_same_step"
    write_olam_trial(t_same, trial_name="dup_same", seats=("p1", "p2"), events=shared_evs, reward=1.0, t0=t0, duration_s=20.0)
    _write_custom_atif(t_same / "seats" / "p1" / "agent" / "trajectory.json", "p1", [_mk_step(1, t0 + 3.0)])
    _write_custom_atif(t_same / "seats" / "p2" / "agent" / "trajectory.json", "p2", [_mk_step(1, t0 + 6.0)])
    d_same = analyze_trial_detectors(t_same, "olam")

    t_log = root / "dup_log_fallback"
    write_olam_trial(t_log, trial_name="dup_log", seats=("p1", "p2"), events=shared_evs, reward=1.0, no_model=True, t0=t0, duration_s=20.0)
    d_log = analyze_trial_detectors(t_log, "olam")

    t_two = root / "dup_two_step"
    two_evs = [
        *shared_evs[:3],
        {"t": t0 + 8.0, "seat": "p2", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 5}, "response": {"box": 5, "flag": True, "budget_left": 0}},
        shared_evs[4],
    ]
    write_olam_trial(t_two, trial_name="dup_two", seats=("p1", "p2"), events=two_evs, reward=1.0, t0=t0, duration_s=20.0)
    _write_custom_atif(t_two / "seats" / "p1" / "agent" / "trajectory.json", "p1", [_mk_step(1, t0 + 3.0)])
    _write_custom_atif(t_two / "seats" / "p2" / "agent" / "trajectory.json", "p2", [_mk_step(1, t0 + 5.0), _mk_step(2, t0 + 10.0)])
    d_two = analyze_trial_detectors(t_two, "olam")

    run.require(
        "stepjoin/causal-dup-flip",
        "same-step-read-act-flips-dup-seen-to-dup-unseen-and-fires-batch",
        set(d_same["triggered"]) >= {"dup_unseen", "batch_blind_act"}
        and "dup_seen" not in d_same["triggered"]
        and d_same["detectors"]["dup_unseen"]["evidence"]["causal_mode"] == "step_causal"
        and "dup_seen" in d_log["triggered"]
        and "dup_unseen" not in d_log["triggered"]
        and "batch_blind_act" not in d_log["triggered"]
        and d_log["detectors"]["dup_seen"]["evidence"]["causal_mode"] == "log_order"
        and "dup_seen" in d_two["triggered"]
        and "dup_unseen" not in d_two["triggered"]
        and "batch_blind_act" not in d_two["triggered"],
        str((d_same["triggered"], d_log["triggered"], d_two["triggered"])),
    )


def _exercise_empty_room_and_slow_report(run: SimRun, root: Path) -> None:
    t0 = 1_770_630_000.0
    t_ded = root / "empty_deduced"
    ded_evs = [
        {"t": t0 + 1.0, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 1}, "response": {"box": 1, "room": 1, "flag": False, "flags_in_room": 1, "room_size": 3, "budget_left": 2}},
        {"t": t0 + 4.0, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 2}, "response": {"box": 2, "room": 1, "flag": True, "flags_in_room": 1, "room_size": 3, "budget_left": 1}},
        {"t": t0 + 8.0, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 3}, "response": {"box": 3, "room": 1, "flag": False, "flags_in_room": 1, "room_size": 3, "budget_left": 0}},
    ]
    write_olam_trial(t_ded, trial_name="ded", seats=("p1",), events=ded_evs, reward=1.0, t0=t0, duration_s=15.0)
    _write_custom_atif(t_ded / "seats" / "p1" / "agent" / "trajectory.json", "p1", [_mk_step(1, t0 + 2.0), _mk_step(2, t0 + 6.0), _mk_step(3, t0 + 10.0)])
    d_ded = analyze_trial_detectors(t_ded, "olam")

    t_msg = root / "empty_peer_msg"
    msg_evs = [
        {"t": t0 + 1.0, "seat": "p2", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 11}, "response": {"box": 11, "room": 2, "flag": False, "budget_left": 0}},
        {"t": t0 + 1.5, "seat": "p2", "kind": "msg", "method": "POST", "status": 200, "ok": True, "request": {"text": "Room 2 is empty; no flags."}, "response": {"id": 1}},
        {"t": t0 + 3.0, "seat": "p1", "kind": "msgs", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"messages": [{"id": 1, "from": "p2", "text": "Room 2 is empty; no flags."}]}},
        {"t": t0 + 7.0, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 12}, "response": {"box": 12, "room": 2, "flag": False, "budget_left": 0}},
    ]
    write_olam_trial(t_msg, trial_name="emsg", seats=("p1", "p2"), events=msg_evs, reward=1.0, t0=t0, duration_s=15.0)
    _write_custom_atif(t_msg / "seats" / "p1" / "agent" / "trajectory.json", "p1", [_mk_step(1, t0 + 4.0), _mk_step(2, t0 + 9.0)])
    _write_custom_atif(t_msg / "seats" / "p2" / "agent" / "trajectory.json", "p2", [_mk_step(1, t0 + 3.0)])
    d_msg = analyze_trial_detectors(t_msg, "olam")

    t_ora = root / "boxes_oracle"
    ora_evs = [
        {"t": t0 + 1.0, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 1}, "response": {"box": 1, "room": 1, "flag": False, "budget_left": 1}},
        {"t": t0 + 1.2, "seat": "p1", "kind": "msg", "method": "POST", "status": 200, "ok": True, "request": {"text": "room 1 empty"}, "response": {"id": 1}},
        {"t": t0 + 1.5, "seat": "p2", "kind": "msgs", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"messages": [{"id": 1, "from": "p1", "text": "room 1 empty"}]}},
        {"t": t0 + 2.0, "seat": "p2", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 11}, "response": {"box": 11, "room": 2, "flag": True, "budget_left": 1}},
        {"t": t0 + 2.2, "seat": "p2", "kind": "msg", "method": "POST", "status": 200, "ok": True, "request": {"text": "room 2 treasure"}, "response": {"id": 2}},
        {"t": t0 + 2.5, "seat": "p1", "kind": "msgs", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"messages": [{"id": 1, "from": "p1", "text": "room 1 empty"}, {"id": 2, "from": "p2", "text": "room 2 treasure"}]}},
        {"t": t0 + 3.0, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 12}, "response": {"box": 12, "room": 2, "flag": True, "budget_left": 0}},
        {"t": t0 + 3.5, "seat": "p2", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 13}, "response": {"box": 13, "room": 2, "flag": True, "budget_left": 0}},
    ]
    write_olam_trial(t_ora, trial_name="ora", seats=("p1", "p2"), events=ora_evs, reward=1.0, no_model=True, t0=t0)
    d_ora = analyze_trial_detectors(t_ora, "olam")

    t_slow_j = root / "slow_joined"
    slow_evs = [
        {"t": t0 + 1.0, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 1}, "response": {"box": 1, "flag": True, "budget_left": 2}},
        {"t": t0 + 4.0, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 2}, "response": {"box": 2, "flag": True, "budget_left": 1}},
        {"t": t0 + 7.0, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 3}, "response": {"box": 3, "flag": True, "budget_left": 0}},
        {"t": t0 + 8.0, "seat": "p2", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 9}, "response": {"box": 9, "flag": True, "budget_left": 0}},
    ]
    write_olam_trial(t_slow_j, trial_name="slow_j", seats=("p1", "p2"), events=slow_evs, reward=1.0, t0=t0, duration_s=15.0)
    _write_custom_atif(t_slow_j / "seats" / "p1" / "agent" / "trajectory.json", "p1", [_mk_step(1, t0 + 2.0), _mk_step(2, t0 + 5.0), _mk_step(3, t0 + 9.0)])
    _write_custom_atif(t_slow_j / "seats" / "p2" / "agent" / "trajectory.json", "p2", [_mk_step(1, t0 + 9.0)])
    d_slow_j = analyze_trial_detectors(t_slow_j, "olam")

    t_solo5 = root / "solo_five_opens"
    solo5_evs = [
        {"t": t0 + i, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": i}, "response": {"box": i, "flag": True, "budget_left": 5 - i}}
        for i in range(1, 6)
    ]
    write_olam_trial(t_solo5, trial_name="solo5", seats=("p1",), events=solo5_evs, reward=1.0, no_model=True, t0=t0)
    d_solo5 = analyze_trial_detectors(t_solo5, "olam")

    run.require(
        "stepjoin/extra-detectors",
        "empty-room-direct-and-deduced-slow-report-and-zero-false-fires",
        d_ded["triggered"] == ["empty_room_open"]
        and "empty_room_open" in d_msg["triggered"]
        and d_ora["triggered"] == []
        and "slow_report" in d_slow_j["triggered"]
        and d_solo5["triggered"] == [],
        str((d_ded["triggered"], d_msg["triggered"], d_ora["triggered"], d_slow_j["triggered"], d_solo5["triggered"])),
    )


def _exercise_cli_and_storyboard_xss(
    run: SimRun,
    root: Path,
    suite_dir: Path,
    capture_fn: Callable[[list[str]], tuple[int, str, str, str]],
) -> None:
    rc_sj, out_sj, err_sj, exc_sj = capture_fn(["step-join", str(suite_dir), "--json"])
    sj_rep = json.loads(out_sj) if rc_sj == 0 else {}
    rc_txt, out_txt, _, _ = capture_fn(["step-join", str(suite_dir)])
    run.require(
        "stepjoin/cli",
        "step-join-json-and-text-exit-zero",
        rc_sj == 0 and not exc_sj and rc_txt == 0 and sj_rep.get("trials_scanned") == 3 and "step-join on" in out_txt,
        f"rc={rc_sj},{rc_txt} err={err_sj} exc={exc_sj}",
    )

    xss_root = root / "sb_xss"
    t0 = 1_770_640_000.0
    xss_evs = [
        {"t": t0 + 1.0, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 1}, "response": {"box": 1, "flag": True, "budget_left": 1}},
        {"t": t0 + 1.4, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 2}, "response": {"box": 2, "flag": True, "budget_left": 0}},
        {"t": t0 + 2.0, "seat": "p1", "kind": "msg", "method": "POST", "status": 200, "ok": True, "request": {"text": "<script>alert(1)</script>"}, "response": {"id": 1}},
    ]
    t_xss = xss_root / "t_xss"
    write_olam_trial(t_xss, trial_name="tr<script>alert(1)</script>", seats=("p1",), events=xss_evs, reward=0.5, t0=t0, duration_s=10.0)
    _write_custom_atif(t_xss / "seats" / "p1" / "agent" / "trajectory.json", "p1", [_mk_step(1, t0 + 5.0, keystrokes="<img src=x onerror=alert(1)>\n")])
    html_out = root / "storyboard.html"
    rc_sb, out_sb, err_sb, exc_sb = capture_fn(["storyboard", str(xss_root), "--html", str(html_out), "--margin-s", "0.5", "--max-trials", "8", "--json"])
    sb_rep = json.loads(out_sb) if rc_sb == 0 else {}
    rc_st, out_st, _, _ = capture_fn(["storyboard", str(xss_root)])
    html_txt = html_out.read_text(encoding="utf-8") if html_out.is_file() else ""
    run.require(
        "storyboard/cli-xss", "csp-strict-and-script-payload-escaped",
        rc_sb == 0 and not exc_sb and rc_st == 0 and "storyboard on" in out_st
        and sb_rep.get("trials_rendered") == 1 and "batch_blind_act" in sb_rep["trials"][0]["triggered"]
        and "Content-Security-Policy" in html_txt and "default-src 'none'" in html_txt
        and "<script>alert(1)</script>" not in html_txt and "&lt;script&gt;alert(1)&lt;/script&gt;" in html_txt
        and "\\u003cscript\\u003e" in html_txt,
        f"rc={rc_sb},{rc_st} err={err_sb} exc={exc_sb}",
    )


def _exercise_conditional_and_verify_rounds(run: SimRun, root: Path) -> None:
    t0 = 1_770_650_000.0
    cond_evs = [
        {"t": t0 + 1.0, "seat": "p2", "kind": "msg", "method": "POST", "status": 200, "ok": True, "request": {"text": "If room4 is empty, sample room5/6 next."}, "response": {"id": 1}},
        {"t": t0 + 2.0, "seat": "p1", "kind": "msgs", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"messages": [{"id": 1, "from": "p2", "text": "If room4 is empty, sample room5/6 next."}]}},
        {"t": t0 + 3.0, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 51}, "response": {"box": 51, "room": 6, "room_size": 10, "flag": False, "budget_left": 0}},
        {"t": t0 + 4.0, "seat": "p2", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 1}, "response": {"box": 1, "room": 1, "room_size": 10, "flag": True, "budget_left": 0}},
    ]
    t_cond = root / "cond_empty"
    write_olam_trial(t_cond, trial_name="cond_empty", seats=("p1", "p2"), events=cond_evs, reward=1.0, no_model=True, t0=t0)
    d_cond = analyze_trial_detectors(t_cond, "olam")

    t_ver = root / "verify_rounds"
    ver_evs = [
        {"t": t0 + 1.0, "seat": "p1", "kind": "verify", "method": "POST", "status": 200, "ok": True, "request": {"round": 1, "target": "s_peer"}, "response": {"round": 1, "ok": True}},
        {"t": t0 + 2.0, "seat": "p1", "kind": "verify", "method": "POST", "status": 200, "ok": True, "request": {"round": 2, "target": "s_peer"}, "response": {"round": 2, "ok": True}},
    ]
    write_olam_trial(t_ver, trial_name="verify_rounds", seats=("p1",), events=ver_evs, reward=1.0, no_model=True, t0=t0)
    d_ver = analyze_trial_detectors(t_ver, "olam")

    run.require(
        "stepjoin/fp-regressions",
        "conditional-empty-clause-and-multiroun-verify-zero-triggers",
        d_cond["triggered"] == [] and d_ver["triggered"] == [],
        str((d_cond["triggered"], d_ver["triggered"])),
    )


def _exercise_phase5_probe_stepjoin(run: SimRun, root: Path) -> None:
    t0 = 1_770_660_000.0
    t_ol = root / "ol_batch"
    ol_evs = [
        {"t": t0 + 1.0, "seat": "p1", "kind": "stage", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"stage": 0}},
        *[{"t": t0 + 1.2 + i * 0.1, "seat": "p1", "kind": "stage", "method": "POST", "status": 200, "ok": True, "request": {"stage": i + 1}, "response": {"ok": True}} for i in range(3)],
    ]
    write_olam_trial(t_ol, trial_name="ol_batch", seats=("p1",), events=ol_evs, reward=0.0, t0=t0, duration_s=15.0)
    big_sl = "sleep " + ("9" * 308) + " && sleep " + ("9" * 308) + "\n"
    _write_custom_atif(t_ol / "seats" / "p1" / "agent" / "trajectory.json", "p1", [_mk_step(1, t0 + 5.0, keystrokes=big_sl)])
    j_ol = join_trial_steps(t_ol, "olam")
    validate_json_tree(j_ol)
    d_ol = analyze_trial_detectors(t_ol, "olam", step_join=j_ol)

    t_cl = root / "cl_clean_and_rej"
    cl_evs = [
        {"t": t0 + 1.0, "seat": "p1", "kind": "leases", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"leases": []}},
        {"t": t0 + 1.2, "seat": "p1", "kind": "claim", "method": "POST", "status": 409, "ok": False, "request": None, "response": {"error": "active_lease_collision", "slot": "s1"}},
        {"t": t0 + 4.0, "seat": "p1", "kind": "claim", "method": "POST", "status": 200, "ok": True, "request": {"slot": "s2"}, "response": {"slot": "s2"}},
        {"t": t0 + 7.0, "seat": "p1", "kind": "work", "method": "POST", "status": 200, "ok": True, "request": {"slot": "s2"}, "response": {"slot": "s2"}},
        {"t": t0 + 10.0, "seat": "p1", "kind": "release", "method": "POST", "status": 200, "ok": True, "request": {"slot": "s2"}, "response": {"phase": "completed"}},
        {"t": t0 + 2.0, "seat": "p_bg", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 1}, "response": {"box": 1, "flag": True}},
        {"t": t0 + 2.1, "seat": "p_bg", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 2}, "response": {"box": 2, "flag": True}},
        {"t": t0 + 80.0, "seat": "p_unm", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 9}, "response": {"box": 9, "flag": True}},
    ]
    write_olam_trial(t_cl, trial_name="cl_clean", seats=("p1", "p_bg", "p_unm"), events=cl_evs, reward=1.0, t0=t0, duration_s=15.0)
    res_p = t_cl / "result.json"
    rdoc = json.loads(res_p.read_text(encoding="utf-8"))
    for sr in rdoc["seat_results"]:
        if sr["seat"] != "p1":
            sr["under_test"] = False
    res_p.write_text(json.dumps(rdoc), encoding="utf-8")
    _write_custom_atif(
        t_cl / "seats" / "p1" / "agent" / "trajectory.json", "p1",
        [_mk_step(1, t0 + 2.0, keystrokes="curl /leases && curl -X POST /claim\n"), _mk_step(2, t0 + 5.0, keystrokes="curl -X POST /claim\n"), _mk_step(3, t0 + 8.0, keystrokes="curl -X POST /work\n"), _mk_step(4, t0 + 11.0, keystrokes="curl -X POST /release\n")],
    )
    _write_custom_atif(t_cl / "seats" / "p_bg" / "agent" / "trajectory.json", "p_bg", [_mk_step(1, t0 + 3.0)])
    _write_custom_atif(t_cl / "seats" / "p_unm" / "agent" / "trajectory.json", "p_unm", [_mk_step(1, t0 + 2.1)])
    j_cl = join_trial_steps(t_cl, "olam", margin_s=0.5)
    d_cl = analyze_trial_detectors(t_cl, "olam", step_join=j_cl)

    b_fb = root / "bsp_facing_fb"
    write_bespoke_trial(b_fb, trial_name="bsp_fb", slots=("agent-0",), facings=("amber-hawk",), t0=t0)
    (b_fb / "agent" / "agent-0" / "trajectory.json").rename(b_fb / "agent" / "amber-hawk.traj")
    (b_fb / "agent" / "amber-hawk").mkdir(parents=True, exist_ok=True)
    (b_fb / "agent" / "amber-hawk.traj").rename(b_fb / "agent" / "amber-hawk" / "trajectory.json")
    bun_fb = load_trial_bundle(b_fb, "bespoke")
    run.require(
        "stepjoin/phase5-probes-and-fallbacks",
        "orderladder-claimlease-ok-guard-sleep-undertest-and-bespoke-fb",
        "batch_blind_act" in d_ol["triggered"] and math.isfinite(j_ol["seats"][0]["steps"][0]["sleep_s"])
        and d_cl["triggered"] == [] and j_cl["seats"][0]["text_disagree_steps"] == 0
        and j_cl["seats"][0]["reads_before_first_act"] == 1 and j_cl["summary"]["batch_opens_max"] == 1
        and j_cl["summary"]["read_and_act_same_step"] == 0
        and j_cl["seats"][2]["edge_events"] == 0 and j_cl["seats"][2]["steps"][0]["edge_seqs"] == []
        and bun_fb.seats[0].atif is not None and bun_fb.seats[0].atif.agent_steps > 0,
    )


def _exercise_phase5_detector_regressions(run: SimRun, root: Path) -> None:
    t0 = 1_770_670_000.0
    t_mb = root / "multibox_json"
    mb_evs = [
        {"t": t0 + 1.0, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 1}, "response": {"box": 1, "room": 1, "flag": False, "flags_in_room": 1, "room_size": 3, "budget_left": 1}},
        {"t": t0 + 1.5, "seat": "p1", "kind": "msg", "method": "POST", "status": 200, "ok": True, "request": {"text": '{"box": 1, "flag": false}'}, "response": {"id": 1}},
        {"t": t0 + 4.0, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 2}, "response": {"box": 2, "room": 1, "flag": True, "flags_in_room": 1, "room_size": 3, "budget_left": 0}},
    ]
    write_olam_trial(t_mb, trial_name="mb", seats=("p1",), events=mb_evs, reward=1.0, no_model=True, t0=t0)
    d_mb = analyze_trial_detectors(t_mb, "olam")

    t_bh = root / "bsp_handoff"
    bh_rows = [
        {"ts": t0 + 1.0, "event": "register", "agent": "amber-hawk", "role": "w"},
        {"ts": t0 + 1.1, "event": "register", "agent": "brisk-otter", "role": "w"},
        {"ts": t0 + 2.0, "event": "guard_claim_granted", "agent": "amber-hawk", "prefix": "a.py"},
        {"ts": t0 + 3.0, "event": "edit", "agent": "amber-hawk", "path": "a.py"},
        {"ts": t0 + 4.0, "event": "guard_claim_released", "agent": "amber-hawk", "prefix": "a.py"},
        {"ts": t0 + 4.5, "event": "send", "seq": 1, "agent": "amber-hawk", "text": "released a.py"},
        {"ts": t0 + 5.0, "event": "inbox", "agent": "brisk-otter", "seqs": [1]},
        {"ts": t0 + 6.0, "event": "guard_claim_granted", "agent": "brisk-otter", "prefix": "a.py"},
        {"ts": t0 + 7.0, "event": "edit", "agent": "brisk-otter", "path": "a.py"},
        {"ts": t0 + 8.0, "event": "close"},
    ]
    write_bespoke_trial(t_bh, trial_name="bh", board_rows=bh_rows, t0=t0, duration_s=15.0)
    _write_custom_atif(t_bh / "agent" / "agent-0" / "trajectory.json", "agent-0", [_mk_step(1, t0 + 2.5), _mk_step(2, t0 + 4.8)])
    _write_custom_atif(t_bh / "agent" / "agent-1" / "trajectory.json", "agent-1", [_mk_step(1, t0 + 5.5), _mk_step(2, t0 + 6.5), _mk_step(3, t0 + 8.5)])
    d_bh = analyze_trial_detectors(t_bh, "bespoke")

    t_cl_trap = root / "cl_trap"
    cl_trap_evs = [
        {"t": t0 + 1.0, "seat": "p1", "kind": "rules", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"budget_left": {"claim": 1, "work": 1, "release": 1}}},
        {"t": t0 + 2.0, "seat": "p1", "kind": "claim", "method": "POST", "status": 409, "ok": False, "request": None, "response": {"error": "active_lease_collision", "slot": "s1"}},
        {"t": t0 + 2.5, "seat": "p1", "kind": "work", "method": "POST", "status": 403, "ok": False, "request": None, "response": {"error": "lease_not_owned", "slot": "s1"}},
        {"t": t0 + 3.0, "seat": "p1", "kind": "claim", "method": "POST", "status": 200, "ok": True, "request": {"slot": "s2"}, "response": {"slot": "s2", "budget_left": 0}},
    ]
    write_olam_trial(t_cl_trap, trial_name="cl_trap", seats=("p1",), events=cl_trap_evs, reward=0.0, extra_rewards={"stale_repeats": 1}, no_model=True, t0=t0)
    d_clt = analyze_trial_detectors(t_cl_trap, "olam")

    t_rel = root / "relay3"
    rel_evs = [
        {"t": t0 + 1.0, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 5}, "response": {"box": 5, "flag": True, "budget_left": 0}},
        {"t": t0 + 2.0, "seat": "p2", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 5}, "response": {"box": 5, "flag": True, "budget_left": 0}},
        {"t": t0 + 2.5, "seat": "p2", "kind": "msg", "method": "POST", "status": 200, "ok": True, "request": {"text": "opened box 5"}, "response": {"id": 1}},
        {"t": t0 + 4.0, "seat": "p3", "kind": "msgs", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"messages": [{"id": 1, "from": "p2", "text": "opened box 5"}]}},
        {"t": t0 + 8.0, "seat": "p3", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 5}, "response": {"box": 5, "flag": True, "budget_left": 0}},
    ]
    write_olam_trial(t_rel, trial_name="relay3", seats=("p1", "p2", "p3"), events=rel_evs, reward=1.0, t0=t0, duration_s=15.0)
    _write_custom_atif(t_rel / "seats" / "p1" / "agent" / "trajectory.json", "p1", [_mk_step(1, t0 + 2.0)])
    _write_custom_atif(t_rel / "seats" / "p2" / "agent" / "trajectory.json", "p2", [_mk_step(1, t0 + 3.0)])
    _write_custom_atif(t_rel / "seats" / "p3" / "agent" / "trajectory.json", "p3", [_mk_step(1, t0 + 5.0), _mk_step(2, t0 + 9.0)])
    d_rel = analyze_trial_detectors(t_rel, "olam")
    run.require(
        "detectors/phase5-handoff-relay-claimlease-budget",
        "multibox-handoff-zero-fps-and-claimlease-relay-budget-accurate",
        d_mb["triggered"] == [] and d_bh["triggered"] == []
        and {"claim_churn", "early_stop_with_budget", "dup_seen"} <= set(d_clt["triggered"])
        and d_clt["detectors"]["claim_churn"]["seats"] == ["p1"] and d_clt["detectors"]["dup_seen"]["seats"] == ["p1"]
        and d_rel["detectors"]["dup_seen"]["seats"] == ["p3"] and d_rel["detectors"]["dup_unseen"]["seats"] == ["p2"],
    )


def exercise_step_join(
    run: SimRun,
    root: Path,
    rng: Random,
    capture_fn: Callable[[list[str]], tuple[int, str, str, str]] | None = None,
) -> None:
    run.scenarios += 1
    root.mkdir(parents=True, exist_ok=True)
    suite_dir = root / "suite"
    n_seats = _exercise_olam_scaling(run, suite_dir, rng, 1_770_600_000.0)
    scale_info = _exercise_bespoke_and_run(run, suite_dir, n_seats, 1_770_600_000.0)
    t_edge = _exercise_skew_and_edges(run, root / "sk", rng, 1_770_610_000.0)
    _exercise_failure_modes(run, root / "sk", t_edge, 1_770_610_000.0)
    _exercise_causal_dups_and_batch(run, root / "cd")
    _exercise_empty_room_and_slow_report(run, root / "ex")
    _exercise_conditional_and_verify_rounds(run, root / "fp")
    _exercise_phase5_probe_stepjoin(run, root / "p5_sj")
    _exercise_phase5_detector_regressions(run, root / "p5_det")
    if capture_fn is not None:
        _exercise_cli_and_storyboard_xss(run, root, suite_dir, capture_fn)
    run.record("step_join", scale_info)
