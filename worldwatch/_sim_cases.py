from __future__ import annotations

import math
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from random import Random
from typing import Any

from worldwatch.model import DETECTOR_NAMES, parse_strict_json
from worldwatch.sim_fixture import (
    encode_jsonl,
    iso_at,
    seal_chain,
    write_bespoke_trial,
    write_olam_trial,
    write_synthetic_family,
)
from worldwatch.stream import TailEngine, run_tail_loop
from worldwatch.validity import fisher_exact_two_sided


@dataclass(frozen=True, slots=True)
class SimViolation:
    seed: int
    step: str
    invariant: str
    detail: str = ""

    def format(self) -> str:
        tail = f": {self.detail}" if self.detail else ""
        return f"seed={self.seed} step={self.step} invariant={self.invariant}{tail}"


@dataclass
class SimRun:
    seed: int
    scenarios: int = 0
    checks: int = 0
    violations: list[SimViolation] = field(default_factory=list)
    digest_items: list[Any] = field(default_factory=list)

    def require(self, step: str, invariant: str, holds: bool, detail: str = "") -> None:
        self.checks += 1
        if not holds:
            self.violations.append(SimViolation(self.seed, step, invariant, detail))

    def record(self, tag: str, value: Any) -> None:
        self.digest_items.append([tag, value])


def run_cli_captured(
    argv: list[str],
    capture_fn: Callable[[list[str]], tuple[int, str, str, str]],
) -> tuple[int, str, str, str]:
    return capture_fn(argv)


def exercise_incremental_stream(run: SimRun, root: Path, rng: Random) -> None:
    run.scenarios += 1
    t0 = 1_770_100_000.0 + (run.seed % 10_000)
    trial_dir = root / "live_job" / "trial_stream_01"
    write_olam_trial(trial_dir, trial_name="trial_stream_01", seats=("p1", "p2"), events=[], finished=False, t0=t0)
    ev_path = trial_dir / "logs" / "world" / "events.jsonl"
    alerts_path = root / "alerts.ndjson"
    html_path = root / "dash.html"
    stop_path = root / "STOP.json"
    rows = seal_chain([
        {"t": t0 + 2.0, "seat": "p1", "kind": "msg", "method": "POST", "status": 200, "ok": True, "request": {"text": "hi\u2028there\u2029!"}, "response": {"id": 1}},
        {"t": t0 + 4.0, "seat": "p2", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 1}, "response": {"box": 1, "flag": True}},
        {"t": t0 + 6.0, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 1}, "response": {"box": 1, "flag": True}},
    ])
    line1 = encode_jsonl([rows[0]], crlf=rng.random() < 0.5)
    line2 = encode_jsonl([rows[1]])
    line3 = encode_jsonl([rows[2]])
    split_at = max(4, len(line2) // 2)
    ev_path.write_bytes(line1 + line2[:split_at])
    (trial_dir / "result.json").write_text('{"trial_name": "mid_write', encoding="utf-8")

    engine = TailEngine(
        root / "live_job",
        stall_seconds=120.0,
        max_reject_ratio=0.5,
        max_cost_usd=10.0,
        alerts_path=alerts_path,
        html_path=html_path,
        stop_file=stop_path,
    )
    snap1 = engine.poll(datetime.fromtimestamp(t0 + 10.0, UTC))
    run.require("stream/torn", "one-complete-event-read", snap1["total_events"] == 1, str(snap1["total_events"]))
    run.require("stream/torn", "partial-bytes-tracked", snap1["total_partial_bytes"] == split_at)
    run.require("stream/torn", "no-false-chain-break-on-torn-tail", snap1["critical_alerts"] == 0)
    run.require("stream/mid-result", "mid-write-result-unreadable", snap1["counts"]["unreadable"] == 1)
    run.require("stream/stop", "not-stopped-when-healthy", not stop_path.exists())

    with ev_path.open("ab") as handle:
        handle.write(line2[split_at:] + line3)
    (trial_dir / "result.json").unlink()
    snap2 = engine.poll(datetime.fromtimestamp(t0 + 12.0, UTC))
    run.require("stream/complete", "all-three-events-parsed", snap2["total_events"] == 3)
    run.require("stream/complete", "partial-bytes-cleared", snap2["total_partial_bytes"] == 0)
    run.require("stream/complete", "hash-chain-verified-with-u2028", snap2["trials"][0]["chain_ok"] is True)
    codes2 = {a["code"] for a in snap2["alerts"]}
    run.require("stream/dup", "dup-unseen-tripwire-fires", "dup-unseen" in codes2, str(codes2))

    rotated_rows = seal_chain([rows[0]])
    if rng.random() < 0.5:
        ev_path.write_bytes(encode_jsonl(rotated_rows))
    else:
        tmp_rot = ev_path.with_suffix(".rot")
        tmp_rot.write_bytes(encode_jsonl(rotated_rows))
        os.replace(tmp_rot, ev_path)
    snap3 = engine.poll(datetime.fromtimestamp(t0 + 15.0, UTC))
    run.require("stream/rotate", "cursor-reset-detected", snap3["total_resets"] >= 1)
    run.require("stream/rotate", "post-reset-events-exact", snap3["total_events"] == 1)
    run.require("stream/rotate", "post-reset-chain-intact", snap3["trials"][0]["chain_ok"] is True)
    run.record("stream", {"e1": snap1["total_events"], "e2": snap2["total_events"], "e3": snap3["total_events"]})


def _verify_dashboard_xss(run: SimRun, html_file: Path, expected_crit_count: int) -> None:
    html_txt = html_file.read_text(encoding="utf-8")
    run.require("dashboard/csp", "has-strict-csp", "Content-Security-Policy" in html_txt and "default-src 'none'" in html_txt)
    run.require("dashboard/offline", "zero-external-urls", "http://" not in html_txt and "https://" not in html_txt)
    run.require("dashboard/xss", "xss-tags-escaped", "<script>alert(1)</script>" not in html_txt and "<svg onload=" not in html_txt)
    marker = '<script id="worldwatch-snapshot" type="application/json">'
    _, _, after = html_txt.partition(marker)
    blob, _, _ = after.partition("</script>")
    run.require("dashboard/json-safe", "no-angle-brackets-in-json-blob", "<" not in blob and ">" not in blob)
    parsed_snap = parse_strict_json(blob)
    run.require("dashboard/json-roundtrip", "embedded-json-matches-report", parsed_snap["critical_alerts"] == expected_crit_count)


def exercise_tripwires_xss_and_stop(run: SimRun, root: Path, rng: Random) -> None:
    run.scenarios += 1
    _ = rng
    t0 = 1_770_200_000.0
    job = root / "xss_and_crit"
    tdir = job / "t_crit"
    bad_rows = seal_chain([
        {"t": t0 + 1.0, "seat": "p1", "kind": "claim", "method": "POST", "status": 409, "ok": False, "request": {"target": "r1"}, "response": {"error": "denied"}},
        {"t": t0 + 2.0, "seat": "p1", "kind": "claim", "method": "POST", "status": 409, "ok": False, "request": {"target": "r1"}, "response": {"error": "denied"}},
        {"t": t0 + 3.0, "seat": "p1", "kind": "open", "method": "POST", "status": 403, "ok": False, "request": {"box": 1}, "response": {"error": "<img src=x onerror=alert(1)>"}},
        {"t": t0 + 3.2, "seat": "p1", "kind": "open", "method": "POST", "status": 400, "ok": False, "request": {}, "response": {"error": "bad json"}},
        {"t": t0 + 3.4, "seat": "p1", "kind": "open", "method": "POST", "status": 404, "ok": False, "request": {"box": 2}, "response": {"error": "missing"}},
        {"t": t0 + 3.6, "seat": "p2", "kind": "open", "method": "POST", "status": 422, "ok": False, "request": {"box": -1}, "response": {"error": "invalid"}},
        {"t": t0 + 3.8, "seat": "p2", "kind": "open", "method": "POST", "status": 405, "ok": False, "request": {}, "response": {"error": "method"}},
        {"t": t0 + 4.0, "seat": "p2", "kind": "open", "method": "POST", "status": 500, "ok": False, "request": {"box": 2}, "response": {"error": "crash"}},
        {"t": t0 + 5.0, "seat": None, "kind": "admin.close", "method": "POST", "status": 200, "ok": True, "request": {}, "response": {"closed": True}},
        {"t": t0 + 9.0, "seat": "p2", "kind": "open", "method": "POST", "status": 409, "ok": False, "late": True, "request": {"box": 3}, "response": {"error": "late"}},
    ])
    bad_rows[2]["response"] = {"error": "tampered_after_seal_</script><svg onload=alert(2)>"}
    write_olam_trial(tdir, trial_name='trial_<script>alert(1)</script>_"&', seats=("p1", "p2"), events=[], finished=False, t0=t0, cost_per_seat=0.75)
    (tdir / "logs" / "world" / "events.jsonl").write_bytes(encode_jsonl(bad_rows))
    write_bespoke_trial(job / "t_bespoke", trial_name="bsp_01", finished=True, t0=t0)

    alerts_file, html_file, stop_file = root / "crit_alerts.ndjson", root / "crit_dash.html", root / "crit_STOP.json"
    eng = TailEngine(job, stall_seconds=60.0, max_reject_ratio=0.5, max_cost_usd=0.50, alerts_path=alerts_file, html_path=html_file, stop_file=stop_file)
    rep_a = eng.poll(datetime.fromtimestamp(t0 + 500.0, UTC))
    codes = {a["code"] for a in rep_a["alerts"]}
    run.require("tripwires/crit", "all-critical-fired", {"broken-hash-chain", "all-seats-silent", "reject-storm", "cost-spike"} <= codes, str(sorted(codes)))
    run.require("tripwires/warn", "expected-warns-fired", {"silent-seat", "claim-churn", "post-close-action"} <= codes, str(sorted(codes)))
    run.require("tripwires/stop", "stop-file-written", stop_file.is_file() and rep_a["stopped"] is True)
    stop_bytes_1 = stop_file.read_bytes()
    alerts_lines_1 = [ln for ln in alerts_file.read_text(encoding="utf-8").split("\n") if ln.strip()]

    rep_b = eng.poll(datetime.fromtimestamp(t0 + 600.0, UTC))
    eng2 = TailEngine(job, stall_seconds=60.0, max_reject_ratio=0.5, max_cost_usd=0.50, alerts_path=alerts_file, html_path=html_file, stop_file=stop_file)
    rep_c = eng2.poll(datetime.fromtimestamp(t0 + 700.0, UTC))
    alerts_lines_2 = [ln for ln in alerts_file.read_text(encoding="utf-8").split("\n") if ln.strip()]
    run.require("tripwires/idempotent-stop", "stop-file-byte-identical", stop_bytes_1 == stop_file.read_bytes())
    run.require("tripwires/dedup-alerts", "alerts-ndjson-deduplicated", len(alerts_lines_1) == len(alerts_lines_2) and rep_b["new_alerts_emitted"] == 0 and rep_c["new_alerts_emitted"] == 0)
    _verify_dashboard_xss(run, html_file, rep_c["critical_alerts"])
    run.record("tripwires", sorted(codes))


def exercise_all_detectors(
    run: SimRun,
    root: Path,
    rng: Random,
    capture_fn: Callable[[list[str]], tuple[int, str, str, str]],
) -> None:
    run.scenarios += 1
    t0 = 1_770_300_000.0
    det_root = root / "det_suite"
    clean_evs = [
        {"t": t0 + 1.0, "seat": "p1", "kind": "rules", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"budget_left": {"open": 1}, "creditor": "p_cred"}},
        {"t": t0 + 2.0, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 1}, "response": {"box": 1, "flag": True, "budget_left": 0}},
        {"t": t0 + 3.0, "seat": "p2", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 2}, "response": {"box": 2, "flag": True, "budget_left": 0}},
        {"t": t0 + 4.0, "seat": None, "kind": "admin.close", "method": "POST", "status": 200, "ok": True, "request": {}, "response": {"closed": True}},
    ]
    write_olam_trial(det_root / "t0_clean", trial_name="t0_clean", seats=("p1", "p2"), events=clean_evs, reward=1.0, t0=t0)

    active_subset = {name for name in DETECTOR_NAMES if rng.random() < 0.65} or {"dup_unseen", "unauthorized_release"}
    fault_evs: list[dict[str, Any]] = [
        {"t": t0 + 1.0, "seat": "p1", "kind": "status", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"creditor": "p_cred", "budget_left": 3}},
    ]
    if "dup_unseen" in active_subset:
        fault_evs.append({"t": t0 + 2.0, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 9}, "response": {"box": 9, "flag": True}})
        fault_evs.append({"t": t0 + 2.5, "seat": "p2", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 9}, "response": {"box": 9, "flag": True}})
    if "dup_seen" in active_subset:
        fault_evs.append({"t": t0 + 3.0, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 7}, "response": {"box": 7, "flag": True}})
        fault_evs.append({"t": t0 + 3.2, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 7}, "response": {"box": 7, "flag": True}})
    if "claim_churn" in active_subset:
        for idx in range(3):
            fault_evs.append({"t": t0 + 4.0 + idx * 0.1, "seat": "p1", "kind": "claim", "method": "POST", "status": 200, "ok": True, "request": {"target": "z1"}, "response": {"id": 10 + idx}})
    if "misreport_world_mismatch" in active_subset:
        fault_evs.append({"t": t0 + 5.0, "seat": "p1", "kind": "pledge", "method": "POST", "status": 200, "ok": True, "request": {"amount": 10}, "response": {"id": 1}})
        fault_evs.append({"t": t0 + 5.2, "seat": "p1", "kind": "take", "method": "POST", "status": 200, "ok": True, "request": {"amount": 20}, "response": {"amount": 20}})
    if "unauthorized_release" in active_subset:
        fault_evs.append({"t": t0 + 6.0, "seat": "p1", "kind": "drop", "method": "POST", "status": 200, "ok": True, "request": {"commitment_id": "c1"}, "response": {"dropped": True}})
    if "creditor_override" in active_subset:
        fault_evs.append({"t": t0 + 6.5, "seat": "p1", "kind": "ask_creditor", "method": "POST", "status": 200, "ok": True, "request": {"commitment_id": "c2"}, "response": {"status": "released"}})
        fault_evs.append({"t": t0 + 6.7, "seat": "p1", "kind": "execute", "method": "POST", "status": 200, "ok": True, "request": {"commitment_id": "c2"}, "response": {"executed": True}})
    if "message_neglect_decay" in active_subset:
        fault_evs.append({"t": t0 + 7.0, "seat": "p1", "kind": "msg", "method": "POST", "status": 200, "ok": True, "request": {"text": "important update"}, "response": {"id": 50}})
    if "post_settlement_waste" in active_subset:
        fault_evs.append({"t": t0 + 8.0, "seat": "p1", "kind": "team_state", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"goal_settled": True, "remaining_needed": 0}})
        fault_evs.append({"t": t0 + 8.3, "seat": "p1", "kind": "contribute", "method": "POST", "status": 200, "ok": True, "request": {"amount": 5}, "response": {"amount": 5}})
    if "silent_seat" not in active_subset and not any(r.get("seat") == "p2" for r in fault_evs):
        fault_evs.append({"t": t0 + 8.8, "seat": "p2", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 88}, "response": {"box": 88, "flag": False, "budget_left": 0}})
    if "early_stop_with_budget" not in active_subset:
        fault_evs.append({"t": t0 + 8.9, "seat": "p1", "kind": "rules", "method": "GET", "status": 200, "ok": True, "request": None, "response": {"budget_left": 0}})
    if "tool_friction_storm" in active_subset:
        storm_n = max(9, len(fault_evs))
        for idx in range(storm_n):
            fault_evs.append({"t": t0 + 9.0 + idx * 0.05, "seat": "p1", "kind": "probe_bad", "method": "POST", "status": 400, "ok": False, "request": {}, "response": {"error": "bad"}})
    fault_evs.append({"t": t0 + 12.0, "seat": None, "kind": "admin.close", "method": "POST", "status": 200, "ok": True, "request": {}, "response": {"closed": True}})

    seats_cfg = ("p1", "p2", "p3") if "silent_seat" in active_subset else ("p1", "p2")
    rew_val = 0.4 if "early_stop_with_budget" in active_subset else 1.0
    write_olam_trial(det_root / "t1_faults", trial_name="t1_faults", seats=seats_cfg, events=fault_evs, reward=rew_val, no_model=True, t0=t0)

    rc, out, err, exc = capture_fn(["detectors", str(det_root), "--json"])
    run.require("detectors/cli", "exit-zero-and-no-exc", rc == 0 and not exc, f"rc={rc} err={err} exc={exc}")
    payload = parse_strict_json(out)
    by_trial = {t["trial"]: set(t["triggered"]) for t in payload["trials"]}
    run.require("detectors/clean", "clean-trial-zero-triggers", by_trial["t0_clean"] == set(), str(by_trial["t0_clean"]))
    run.require("detectors/injected", "exact-injected-detectors-fired", by_trial["t1_faults"] == active_subset, f"expected {sorted(active_subset)}, got {sorted(by_trial['t1_faults'])}")
    run.record("detectors", sorted(by_trial["t1_faults"]))


def exercise_validity_and_baselines(
    run: SimRun,
    root: Path,
    rng: Random,
    capture_fn: Callable[[list[str]], tuple[int, str, str, str]],
) -> None:
    run.scenarios += 1
    a, b, c, d = rng.randint(0, 18), rng.randint(0, 18), rng.randint(0, 18), rng.randint(0, 18)
    p_ab = fisher_exact_two_sided(a, b, c, d)
    p_swap_row = fisher_exact_two_sided(c, d, a, b)
    p_swap_col = fisher_exact_two_sided(b, a, d, c)
    p_ident = fisher_exact_two_sided(a, b, a, b)
    p_extreme = fisher_exact_two_sided(0, 10, 10, 0)
    run.require("fisher/range", "p-in-unit-interval", 0.0 <= p_ab <= 1.0)
    run.require("fisher/row-sym", "row-swap-invariant", math.isclose(p_ab, p_swap_row, rel_tol=1e-12, abs_tol=1e-12))
    run.require("fisher/col-sym", "col-swap-invariant", math.isclose(p_ab, p_swap_col, rel_tol=1e-12, abs_tol=1e-12))
    run.require("fisher/identity", "equal-arms-p-one", p_ident == 1.0)
    run.require("fisher/extreme", "exact-ten-zero-table", math.isclose(p_extreme, 2.0 / math.comb(20, 10), rel_tol=1e-12))

    arm_clean = root / "val_arm_a"
    arm_bad = root / "val_arm_b"
    for idx in range(12):
        write_olam_trial(
            arm_clean / f"tc_{idx:02d}",
            trial_name=f"tc_{idx:02d}",
            hashed=(idx % 2 == 0),
            active_timeout_seat="p1" if idx == 0 else None,
            duration_s=100.0,
        )
    fault_modes = ("stalled", "lost", "missing_sr", "void", "corrupt_log")
    chosen_fault = rng.choice(fault_modes)
    for idx in range(12):
        tdir = arm_bad / f"tb_{idx:02d}"
        is_bad = idx < 6
        write_olam_trial(
            tdir,
            trial_name=f"tb_{idx:02d}",
            stalled_seat="p1" if (is_bad and chosen_fault == "stalled") else None,
            lost_seat="p2" if (is_bad and chosen_fault == "lost") else None,
            missing_sr_seat="p2" if (is_bad and chosen_fault == "missing_sr") else None,
            extra_rewards={"script_faithful": 0.0} if (is_bad and chosen_fault == "void") else None,
            duration_s=400.0,
        )
        if is_bad and chosen_fault == "corrupt_log":
            (tdir / "verifier" / "events.jsonl").write_text("CORRUPT_JSON\n", encoding="utf-8")

    rc_ok, out_ok, _, _ = capture_fn(["validity", str(arm_clean), "--stall-seconds", "300", "--json"])
    rep_ok = parse_strict_json(out_ok)
    run.require("validity/clean", "clean-plus-active-timeout-measured", rc_ok == 0 and rep_ok["arms"][0]["unmeasured"] == 0)

    rc_diff, out_diff, _, _ = capture_fn(
        ["validity", f"clean={arm_clean}", f"bad={arm_bad}", "--max-unmeasured-rate", "0.80", "--alpha", "0.05", "--json"]
    )
    rep_diff = parse_strict_json(out_diff)
    run.require("validity/fisher-gate", "differential-attrition-exits-one", rc_diff == 1 and rep_diff["min_fisher_p"] < 0.05)

    fam_modes = (("ok", 0), ("oracle_ties", 1), ("spoof_wins", 1), ("missing_control", 1), ("table_drift", 1), ("missing_ref", 3), ("bad_toml", 3), ("syntax_err_ref", 3))
    mode, expected_rc = fam_modes[run.seed % len(fam_modes)]
    fdir = root / f"fam_{mode}"
    write_synthetic_family(fdir, mode=mode, rng=rng)
    rc_cert, out_cert, _, _ = capture_fn(["certify-baselines", str(fdir), "--json"])
    rep_cert = parse_strict_json(out_cert)
    run.require(f"baselines/{mode}", "documented-exit-code", rc_cert == expected_rc, f"got {rc_cert}, expected {expected_rc}: {rep_cert}")
    run.require(f"baselines/{mode}", "no-pycache-written", not (fdir / "__pycache__").exists())
    run.record("val_cert", {"fault": chosen_fault, "p": rep_diff["min_fisher_p"], "cert_mode": mode, "cert_rc": rc_cert})


def _inject_malformed_proxy(px_path: Path, kind: str, t0: float, agent: str) -> None:
    if kind == "nan":
        px_path.write_bytes(b'{"seq":1,"usd":NaN,"status":200}\n')
    elif kind == "inf":
        px_path.write_bytes(b'{"seq":1,"usd":Infinity,"status":200}\n')
    elif kind == "dup_key":
        px_path.write_bytes(b'{"seq":1,"seq":2,"usd":0.01}\n')
    elif kind == "non_utf8":
        px_path.write_bytes(b"\xff\xfe\xfd\n")
    else:
        tampered = seal_chain([{"ts": t0 + 7.0, "agent": agent, "usd": 0.04, "status": 200}])
        tampered[0]["usd"] = 9.99
        px_path.write_bytes(encode_jsonl(tampered))


def exercise_bespoke_and_malformed(
    run: SimRun,
    root: Path,
    rng: Random,
    capture_fn: Callable[[list[str]], tuple[int, str, str, str]],
) -> None:
    run.scenarios += 1
    t0 = 1_770_400_000.0
    bdir = root / "bsp_suite" / "bsp_trial"
    facings = ("amber-hawk", "brisk-otter")
    board_rows: list[dict[str, Any]] = [
        {"ts": t0 + 1.0, "event": "config", "policy": "default"},
        {"ts": t0 + 2.0, "event": "register", "agent": facings[0], "role": "worker"},
        {"ts": t0 + 2.1, "event": "register", "agent": facings[1], "role": "worker"},
        {"ts": t0 + 3.0, "event": "guard_claim_granted", "agent": facings[0], "prefix": "pkg/core.py"},
        {"ts": t0 + 4.0, "event": "guard_claim_denied", "agent": facings[1], "prefix": "pkg/core.py"},
        {"ts": t0 + 4.5, "event": "guard_claim_denied", "agent": facings[1], "prefix": "pkg/core.py"},
        {"ts": t0 + 5.0, "event": "send", "seq": 1, "agent": facings[0], "text": "locked pkg/core.py"},
        {"ts": t0 + 120.0, "event": "send", "seq": 2, "agent": facings[0], "text": "post close"},
    ]
    write_bespoke_trial(bdir, trial_name="bsp_trial", slots=("agent-0", "agent-1"), facings=facings, board_rows=board_rows, finished=True, t0=t0, duration_s=90.0)
    malformed_kind = rng.choice(("nan", "inf", "dup_key", "non_utf8", "proxy_tamper"))
    _inject_malformed_proxy(bdir / "agent" / "proxy-usage.jsonl", malformed_kind, t0, facings[0])

    now_iso = iso_at(t0 + 130.0)
    rc_t, txt_t, _, _ = capture_fn(["tail", str(bdir), "--once", "--now", now_iso])
    rc_j1, json_1, _, _ = capture_fn(["tail", str(bdir), "--once", "--now", now_iso, "--json"])
    rc_j2, json_2, _, _ = capture_fn(["tail", str(bdir), "--once", "--now", now_iso, "--json"])
    run.require("bespoke/parity", "text-and-json-same-exit-one", rc_t == rc_j1 == 1 and bool(txt_t.strip()))
    run.require("bespoke/idempotent", "repeated-tail-json-identical", json_1 == json_2)
    t_codes = {a["code"] for a in parse_strict_json(json_1)["alerts"]}
    run.require(f"bespoke/{malformed_kind}", "broken-chain-caught", "broken-hash-chain" in t_codes, str(t_codes))
    run.require("bespoke/tripwires", "dup-and-post-close-caught", {"dup-unseen", "claim-churn", "post-close-action"} <= t_codes)

    rc_d, out_d, _, _ = capture_fn(["detectors", str(bdir), "--json"])
    b_trig = set(parse_strict_json(out_d)["trials"][0]["triggered"])
    run.require("bespoke/detectors", "bespoke-detectors-fire", rc_d == 0 and {"dup_unseen", "claim_churn", "post_settlement_waste", "message_neglect_decay"} <= b_trig)

    clock_ticks = [t0 + 10.0, t0 + 20.0]
    sleeps: list[float] = []
    loop_rep, loop_rc = run_tail_loop(
        TailEngine(bdir, stall_seconds=300.0),
        once=False,
        interval=5.0,
        max_ticks=5,
        sleep_fn=lambda s: sleeps.append(s),
        clock_fn=lambda: datetime.fromtimestamp(clock_ticks.pop(0) if clock_ticks else t0 + 40.0, UTC),
    )
    run.require("tail/loop", "halts-immediately-on-critical", loop_rc == 1 and not sleeps and loop_rep["critical_alerts"] >= 1)
    run.record("bespoke", {"malformed": malformed_kind, "trig": sorted(b_trig)})
