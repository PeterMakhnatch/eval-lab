from __future__ import annotations

import bisect
import math
import os
import re
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from worldwatch._chain import find_bespoke_board_log, find_olam_event_log, read_jsonl_static
from worldwatch._io import discover_trials, read_tolerant_json_object, stat_regular_file
from worldwatch._trial import SeatInfo, TrialBundle, load_trial_bundle, with_olam_seat_alias
from worldwatch._tripwires import shared_action_target
from worldwatch.model import (
    SEAT_NAME_RE,
    InputError,
    as_list,
    as_map,
    finite_float,
    parse_iso_utc,
    sanitize_surrogates,
    strict_int,
)

DEFAULT_STEP_MARGIN_S = 0.5
_MAX_STEPS = 100_000
_MAX_SKEW_S = 86_400.0
_MAX_SLEEP_S = 1e12
_NUDGE_S = 1e-6
_PREVIEW_LIMIT = 160

_SCRIPTED_RE = re.compile(r"\b(?:for|while|until|do|xargs|seq|python3?|node|bash|sh)\b|\$\(")
_SLEEP_RE = re.compile(r"\bsleep\s+([0-9]+(?:\.[0-9]+)?)\b")
_TEXT_REQ_RE = re.compile(
    r"/(verify_dependency|private_belief|report_unknown|flag_misreport|submit_choice|submit_answer|"
    r"ask_creditor|report_value|commit_round|final_probe|round_state|task_state|team_state|"
    r"authority|audit_log|contribute|transfer|finalize|contract|notices|pledges|prepare|release|"
    r"reveals|handoff|barrier|execute|claims|leases|prereq|status|verify|notice|pledge|answer|"
    r"commit|report|rates|shift|check|stage|inbox|rules|state|claim|probe|epoch|ledger|msgs|open|"
    r"mine|take|work|drop|game|plan|spec|scan|new|dms|msg|dm|ack)\b"
)

_OLAM_READ_KINDS = frozenset({
    "msgs", "new", "dms", "notices", "inbox", "claims", "barrier", "epoch",
    "contract", "rates", "reveals", "handoff", "probe", "report", "check",
    "stage", "prereq", "leases", "state", "status", "spec", "plan", "authority",
    "task_state", "team_state", "round_state", "audit_log",
    "final_probe", "mine", "game", "pledges", "ledger",
})
_OLAM_MSG_KINDS = frozenset({"msg", "notice", "dm", "ack", "report"})
_OLAM_ACT_KINDS = frozenset({
    "open", "probe", "scan", "execute", "claim", "take", "pledge", "drop",
    "release", "contribute", "answer", "report_value", "submit_choice", "submit_answer", "finalize",
    "stage", "prepare", "work", "verify_dependency", "report_unknown", "verify",
    "flag_misreport", "commit_round", "commit", "barrier", "epoch", "ledger",
    "contract", "handoff", "shift", "check", "transfer", "plan",
})
_OLAM_META_KINDS = frozenset({
    "rules", "health", "root", "admin.close", "script.seed", "script.round",
    "ask_creditor", "private_belief", "report",
})
_BESPOKE_SKIP_KINDS = frozenset({"config", "register", "subagent", "close"})
_BESPOKE_READ_KINDS = frozenset({"inbox", "read"})
_BESPOKE_MSG_KINDS = frozenset({"send", "post"})
_BESPOKE_ACT_KINDS = frozenset({"guard_claim_granted", "edit", "write"})


def _valid_seat(name: str) -> bool:
    return bool(SEAT_NAME_RE.match(name)) and name not in (".", "..")


def _validate_margin(margin_s: float) -> float:
    val = finite_float(margin_s)
    if val is None or val < 0.0:
        raise InputError(f"margin_s must be a non-negative finite float, got {margin_s!r}")
    return val


def _candidate_traj_paths(
    trial_dir: Path, ecosystem: str, seat_info: SeatInfo, seats: tuple[SeatInfo, ...]
) -> list[Path]:
    cands: list[Path] = []
    if ecosystem == "bespoke":
        if _valid_seat(seat_info.seat):
            cands.append(trial_dir / "agent" / seat_info.seat / "trajectory.json")
        if seat_info.facing != seat_info.seat and _valid_seat(seat_info.facing):
            cands.append(trial_dir / "agent" / seat_info.facing / "trajectory.json")
        if len(seats) == 1:
            cands.append(trial_dir / "agent" / "trajectory.json")
        return cands
    if _valid_seat(seat_info.seat):
        cands.append(trial_dir / "seats" / seat_info.seat / "agent" / "trajectory.json")
    n_under = sum(1 for s in seats if s.under_test)
    if len(seats) == 1 or (seat_info.under_test and n_under == 1 and not os.path.isdir(trial_dir / "seats")):
        fallback = trial_dir / "agent" / "trajectory.json"
        if fallback not in cands:
            cands.append(fallback)
    return cands


def _extract_step_cmd(step: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for call in as_list(step.get("tool_calls"))[:256]:
        cmap = as_map(call)
        args = as_map(cmap.get("arguments"))
        found = next((args[k] for k in ("keystrokes", "cmd", "command") if isinstance(args.get(k), str) and args[k]), None)
        if isinstance(found, str):
            parts.append(found)
        else:
            fn = cmap.get("function_name")
            if isinstance(fn, str) and fn and fn not in ("bash", "mark_task_complete", "task_complete"):
                parts.append(fn)
    return "\n".join(parts)


def _make_preview(raw_cmd: str) -> str:
    clean = " ".join(sanitize_surrogates(raw_cmd).split())
    return clean if len(clean) <= _PREVIEW_LIMIT else clean[: _PREVIEW_LIMIT - 3] + "..."


def _load_seat_steps(
    trial_dir: Path, ecosystem: str, seat_info: SeatInfo, seats: tuple[SeatInfo, ...]
) -> tuple[str | None, list[dict[str, Any]] | None, str | None]:
    chosen = next((c for c in _candidate_traj_paths(trial_dir, ecosystem, seat_info, seats) if os.path.lexists(c)), None)
    if chosen is None:
        return None, None, "no_trajectory"
    rel_path = chosen.relative_to(trial_dir).as_posix() if chosen.is_relative_to(trial_dir) else str(chosen)
    info, serr = stat_regular_file(chosen, root=trial_dir)
    if serr is not None or info is None:
        return rel_path, None, "invalid_trajectory"
    doc, jerr = read_tolerant_json_object(chosen, root=trial_dir)
    if jerr is not None or doc is None or not isinstance(doc.get("schema_version"), str):
        return rel_path, None, "invalid_trajectory"
    raw_steps = doc.get("steps")
    if not isinstance(raw_steps, list):
        return rel_path, None, "invalid_trajectory"
    parsed: list[dict[str, Any]] = []
    raw_epochs: list[float] = []
    for item in raw_steps[:_MAX_STEPS]:
        if not isinstance(item, Mapping):
            return rel_path, None, "invalid_trajectory"
        if item.get("is_copied_context") is True:
            continue
        ts_raw = item.get("timestamp")
        ts_dt = parse_iso_utc(ts_raw)
        if ts_dt is None:
            return rel_path, None, "invalid_trajectory"
        idx = len(parsed)
        sid_raw = item.get("step_id")
        sid = sid_raw if isinstance(sid_raw, (int, str)) and not isinstance(sid_raw, bool) else idx + 1
        cmd = _extract_step_cmd(item)
        sleep_s = 0.0
        for raw_sl in _SLEEP_RE.findall(cmd)[:1024]:
            sl_v = finite_float(float(raw_sl[:400])) if len(raw_sl) <= 400 else _MAX_SLEEP_S
            if sl_v is not None and sl_v > 0.0:
                sleep_s = min(_MAX_SLEEP_S, sleep_s + min(sl_v, _MAX_SLEEP_S))
        assert math.isfinite(sleep_s)
        ep = ts_dt.timestamp()
        raw_epochs.append(ep)
        parsed.append({
            "step_index": idx,
            "step_id": sanitize_surrogates(sid) if isinstance(sid, str) else sid,
            "source": sanitize_surrogates(str(item.get("source") or "unknown")),
            "timestamp": sanitize_surrogates(str(ts_raw)),
            "t_epoch": round(ep, 6),
            "keystrokes_preview": _make_preview(cmd),
            "sleep_s": round(sleep_s, 6),
            "scripted": bool(_SCRIPTED_RE.search(cmd)),
            "_text_reqs": _TEXT_REQ_RE.findall(cmd),
            "_win_t": ep,
        })
    if not parsed:
        return rel_path, None, "invalid_trajectory"
    for idx in range(1, len(parsed)):
        if raw_epochs[idx] < raw_epochs[idx - 1]:
            return rel_path, None, "non_monotonic_timestamps"
        if parsed[idx]["_win_t"] <= parsed[idx - 1]["_win_t"]:
            parsed[idx]["_win_t"] = parsed[idx - 1]["_win_t"] + _NUDGE_S
    return rel_path, parsed, None


def _event_epoch(row: Mapping[str, Any]) -> float | None:
    for key in ("t", "ts", "timestamp"):
        if key in row and (dt := parse_iso_utc(row.get(key))) is not None:
            return dt.timestamp()
    return None


def _olam_target_label(row: Mapping[str, Any]) -> str | None:
    shared = shared_action_target(row)
    if shared is not None:
        return sanitize_surrogates(shared)
    req = as_map(row.get("request")) or as_map(row.get("req"))
    resp = as_map(row.get("response")) or as_map(row.get("res"))
    for container in (req, resp):
        for key in ("room", "target", "resource", "item", "commitment_id", "slot", "stage", "key", "shard", "phase", "epoch"):
            val = container.get(key)
            if isinstance(val, (str, int)) and not isinstance(val, bool):
                return sanitize_surrogates(f"{key}={val}")
    return None


def _classify_olam_role(method: str, kind: str, ok_val: bool) -> str:
    if not ok_val or not kind:
        return "other"
    if method == "GET" and kind in _OLAM_READ_KINDS:
        return "read"
    if method == "POST" and kind in _OLAM_MSG_KINDS:
        return "msg"
    if method == "POST" and kind in _OLAM_ACT_KINDS:
        return "act"
    if kind in _OLAM_META_KINDS:
        return "other"
    if method == "GET":
        return "read"
    if method == "POST":
        return "act"
    return "other"


def _extract_seat_events(
    events: list[dict[str, Any]], ecosystem: str, seat: str, facing_map: Mapping[str, str]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if ecosystem == "bespoke":
        for idx, row in enumerate(events, start=1):
            ev_kind = str(row.get("event") or "")
            if ev_kind == "close":
                break
            if ev_kind in _BESPOKE_SKIP_KINDS:
                continue
            raw_ag = row.get("agent") or row.get("from")
            mapped = facing_map.get(raw_ag, raw_ag) if isinstance(raw_ag, str) and raw_ag else None
            if mapped != seat:
                continue
            ok_val = row.get("ok") is not False and ev_kind not in ("refused", "guard_claim_denied", "guard_unread_block")
            role = (
                "read" if (ev_kind in _BESPOKE_READ_KINDS and ok_val)
                else ("msg" if (ev_kind in _BESPOKE_MSG_KINDS and ok_val) else ("act" if (ev_kind in _BESPOKE_ACT_KINDS and ok_val) else "other"))
            )
            tgt_raw = row.get("prefix") or row.get("path")
            out.append({
                "seq": idx, "kind": sanitize_surrogates(ev_kind), "method": "GET" if ev_kind in _BESPOKE_READ_KINDS else "POST",
                "ok": ok_val, "t": _event_epoch(row), "role": role,
                "target": sanitize_surrogates(str(tgt_raw)) if isinstance(tgt_raw, str) and tgt_raw else None,
            })
        return out
    for idx, row in enumerate(events, start=1):
        raw_k = str(row.get("kind") or "").strip()
        raw_m = str(row.get("method") or "").strip().upper()
        pref_m = ""
        if raw_k.startswith(("GET ", "POST ")):
            pref_m, _, raw_k = raw_k.partition(" ")
        elif not raw_m and raw_k.startswith(("get_", "post_")):
            ph, _, raw_k = raw_k.partition("_")
            pref_m = ph.upper()
        kind = raw_k.strip("/").replace("/", ".")
        st_code = strict_int(row.get("status"))
        ok_val = row["ok"] is True if "ok" in row else (st_code is not None and 200 <= st_code < 300)
        if kind == "admin.close" and ok_val:
            break
        raw_s = row.get("seat")
        mapped_s = facing_map.get(raw_s, raw_s) if isinstance(raw_s, str) and raw_s else None
        if kind in ("script.seed", "script.round") or mapped_s != seat:
            continue
        seq = strict_int(row.get("seq")) or idx
        has_b = row.get("request") is not None or row.get("req") is not None
        method = (
            raw_m if raw_m in ("GET", "POST")
            else ("POST" if raw_m in ("PUT", "PATCH", "DELETE") else (pref_m if pref_m in ("GET", "POST") else ("GET" if (kind in _OLAM_READ_KINDS and not has_b) else ("POST" if has_b else "GET"))))
        )
        role = _classify_olam_role(method, kind, ok_val)
        out.append({
            "seq": seq, "kind": sanitize_surrogates(kind), "method": sanitize_surrogates(method),
            "ok": ok_val, "t": _event_epoch(row), "role": role, "target": _olam_target_label(row),
        })
    return out


def _build_step_bounds(
    steps: list[dict[str, Any]], seat_info: SeatInfo, bundle: TrialBundle
) -> tuple[list[tuple[float, float]], list[float], int]:
    first_agent = steps[0]["source"] == "agent"
    start_p = 0 if first_agent else 1
    w0 = float(steps[0]["_win_t"])
    started_cands = [
        dt.timestamp() - 1.0
        for dt in (seat_info.started_at, bundle.agent_started_at, bundle.started_at)
        if dt is not None
    ]
    base_lo = min(*started_cands, w0 - 10.0) if started_cands else (w0 - 3600.0)
    lo0 = base_lo if first_agent else w0
    bounds = [(lo0, w0)]
    for idx in range(1, len(steps)):
        bounds.append((float(steps[idx - 1]["_win_t"]), float(steps[idx]["_win_t"])))
    return bounds, [b[1] for b in bounds], start_p


def _try_assign_offset(
    steps: list[dict[str, Any]], bounds: list[tuple[float, float]], uppers: list[float],
    start_p: int, seat_evs: list[dict[str, Any]], offset: float,
) -> tuple[list[int | None], int, bool, float, float]:
    placements: list[int | None] = []
    agent_matched = 0
    outside = False
    lo, hi = -math.inf, math.inf
    n_steps = len(steps)
    for ev in seat_evs:
        ev_t = ev["t"]
        if ev_t is None or start_p >= n_steps:
            placements.append(None)
            outside = True
            continue
        shifted = ev_t - offset
        pos = bisect.bisect_left(uppers, shifted, lo=start_p)
        if pos >= n_steps or not (bounds[pos][0] < shifted <= bounds[pos][1]):
            placements.append(None)
            outside = True
            continue
        if steps[pos]["source"] != "agent":
            placements.append(None)
            continue
        placements.append(pos)
        agent_matched += 1
        lo = max(lo, ev_t - bounds[pos][1])
        hi = min(hi, ev_t - bounds[pos][0])
    return placements, agent_matched, outside, lo, hi


def _find_skew_assignment(
    steps: list[dict[str, Any]], bounds: list[tuple[float, float]], uppers: list[float],
    start_p: int, seat_evs: list[dict[str, Any]], *, out0: bool = True, ecosystem: str = "olam",
) -> tuple[list[int | None], float, float, float] | None:
    total_evs = len(seat_evs)
    valid_ts = [float(ev["t"]) for ev in seat_evs if ev["t"] is not None]
    agent_idxs = [
        i for i in range(start_p, len(steps))
        if steps[i]["source"] == "agent"
        and (
            total_evs >= 2
            or out0
            or ecosystem != "olam"
            or steps[i]["scripted"]
            or steps[i]["_text_reqs"] == [seat_evs[0]["kind"]]
            or (valid_ts and bounds[i][1] <= valid_ts[0])
        )
    ]
    if not agent_idxs or not valid_ts:
        return None
    by_width = sorted(agent_idxs, key=lambda i: bounds[i][1] - bounds[i][0])
    step_sample = list(dict.fromkeys([*by_width[:16], *agent_idxs[:16], *agent_idxs[-16:]]))[:48]
    ev_sample = list(dict.fromkeys([*valid_ts[:12], *valid_ts[-12:]]))[:24]
    seen_buckets: set[int] = set()
    checked = 0
    for ev_t in ev_sample:
        for s_idx in step_sample:
            b_lo, b_hi = bounds[s_idx]
            mid = 0.5 * (max(b_lo, b_hi - 60.0) + b_hi) if s_idx == 0 else 0.5 * (b_lo + b_hi)
            cand = ev_t - mid
            bucket = round(cand * 1000.0)
            if abs(cand) > _MAX_SKEW_S or bucket in seen_buckets:
                continue
            seen_buckets.add(bucket)
            checked += 1
            plc, mc, _, loc, hic = _try_assign_offset(steps, bounds, uppers, start_p, seat_evs, cand)
            if mc == total_evs and math.isfinite(loc) and math.isfinite(hic) and loc <= hic:
                mid_off = 0.5 * (loc + hic)
                plm, mm, _, lom, him = _try_assign_offset(steps, bounds, uppers, start_p, seat_evs, mid_off)
                if mm == total_evs:
                    return plm, round(mid_off, 6), lom, him
                return plc, round(cand, 6), loc, hic
            if checked >= 512:
                return None
    return None


def _populate_step_events(
    steps: list[dict[str, Any]], bounds: list[tuple[float, float]],
    seat_evs: list[dict[str, Any]], placements: list[int | None], offset: float, margin_s: float,
) -> int:
    for st in steps:
        st["event_seqs"] = []
        st["events_brief"] = []
        st["read_count"] = st["act_count"] = st["msg_count"] = 0
        st["same_step_read_act"] = False
        st["edge_seqs"] = []
    edge_total = 0
    for ev, pos in zip(seat_evs, placements, strict=True):
        if pos is None or ev["t"] is None:
            continue
        shifted = float(ev["t"]) - offset
        b_lo, b_hi = bounds[pos]
        is_edge = bool((b_hi - shifted) < margin_s or (pos >= 1 and (shifted - b_lo) < margin_s))
        st = steps[pos]
        seq = int(ev["seq"])
        st["event_seqs"].append(seq)
        st["events_brief"].append({
            "seq": seq, "kind": ev["kind"], "method": ev["method"], "ok": ev["ok"],
            "t": round(float(ev["t"]), 6), "edge": is_edge, "target": ev["target"],
        })
        if ev["role"] in ("read", "act", "msg"):
            st[f"{ev['role']}_count"] += 1
        if is_edge:
            st["edge_seqs"].append(seq)
            edge_total += 1
    return edge_total


def _compute_step_rollups(
    steps: list[dict[str, Any]], seat_evs: list[dict[str, Any]],
    placements: list[int | None], ecosystem: str,
) -> tuple[int, int, int | None, float | None, int]:
    ev_by_step: list[list[dict[str, Any]]] = [[] for _ in steps]
    for ev, pos in zip(seat_evs, placements, strict=True):
        if pos is not None:
            ev_by_step[pos].append(ev)
    same_cnt = batch_max = disagree_cnt = 0
    act_pos: list[int] = []
    read_pos: list[int] = []
    for idx, st in enumerate(steps):
        if st["source"] != "agent":
            continue
        batch_max = max(batch_max, int(st["act_count"]))
        step_evs = ev_by_step[idx]
        r_seqs = [int(e["seq"]) for e in step_evs if e["role"] == "read"]
        a_seqs = [int(e["seq"]) for e in step_evs if e["role"] == "act"]
        read_pos.extend(idx for _ in r_seqs)
        act_pos.extend(idx for _ in a_seqs)
        if r_seqs and any(a > min(r_seqs) for a in a_seqs):
            st["same_step_read_act"] = True
            same_cnt += 1
        if ecosystem == "olam" and not st["scripted"] and [e["kind"] for e in step_evs] != st["_text_reqs"]:
            disagree_cnt += 1
    for st in steps:
        st.pop("_win_t", None)
        st.pop("_text_reqs", None)
    if not act_pos:
        return batch_max, same_cnt, None, None, disagree_cnt
    reads_before = sum(1 for rn in read_pos if rn < act_pos[0])
    usable = sum(1 for k, an in enumerate(act_pos) if any((act_pos[k - 1] if k else -1) < rn < an for rn in read_pos))
    return batch_max, same_cnt, reads_before, round(usable / len(act_pos), 6), disagree_cnt


def _make_seat_dict(
    seat_info: SeatInfo, traj_rel: str | None, joined: bool, reason: str,
    offset: float, lo: float | None, hi: float | None, zero_ok: bool, edges: int,
    steps: list[dict[str, Any]], matched: int, unmatched: int,
    b_max: int = 0, same_ra: int = 0, r_before: int | None = None, u_ratio: float | None = None, txt_dis: int = 0,
) -> dict[str, Any]:
    for st in steps:
        st.pop("_win_t", None)
        st.pop("_text_reqs", None)
        st.setdefault("event_seqs", [])
        st.setdefault("events_brief", [])
        st.setdefault("read_count", 0)
        st.setdefault("act_count", 0)
        st.setdefault("msg_count", 0)
        st.setdefault("same_step_read_act", False)
        st.setdefault("edge_seqs", [])
    return {
        "seat": seat_info.seat, "facing": seat_info.facing, "under_test": seat_info.under_test,
        "trajectory_path": traj_rel, "joined": joined, "join_reason": reason,
        "clock_offset_s": offset, "offset_lo_s": lo, "offset_hi_s": hi, "zero_offset_ok": zero_ok,
        "edge_events": edges, "steps_count": len(steps), "events_matched": matched, "events_unmatched": unmatched,
        "batch_opens_max": b_max, "read_and_act_same_step": same_ra,
        "reads_before_first_act": r_before, "usable_read_before_act_ratio": u_ratio,
        "text_disagree_steps": txt_dis, "steps": steps,
    }


def _join_single_seat(
    trial_dir: Path, ecosystem: str, margin_s: float,
    seat_info: SeatInfo, bundle: TrialBundle, events: list[dict[str, Any]],
) -> dict[str, Any]:
    seat_evs = _extract_seat_events(events, ecosystem, seat_info.seat, bundle.facing_to_seat)
    traj_rel, steps, load_err = _load_seat_steps(trial_dir, ecosystem, seat_info, bundle.seats)
    if load_err is not None or steps is None:
        return _make_seat_dict(seat_info, traj_rel, False, load_err or "invalid_trajectory", 0.0, None, None, False, 0, [], 0, len(seat_evs))
    if not seat_evs:
        return _make_seat_dict(seat_info, traj_rel, True, "joined", 0.0, 0.0, 0.0, True, 0, steps, 0, 0)
    bounds, uppers, start_p = _build_step_bounds(steps, seat_info, bundle)
    pl0, m0, out0, lo0, hi0 = _try_assign_offset(steps, bounds, uppers, start_p, seat_evs, 0.0)
    total_evs = len(seat_evs)
    zero_ok = m0 == total_evs
    chosen_pl, chosen_off, chosen_lo, chosen_hi = pl0, 0.0, lo0, hi0
    joined = zero_ok
    if not zero_ok:
        skew_res = _find_skew_assignment(steps, bounds, uppers, start_p, seat_evs, out0=out0, ecosystem=ecosystem)
        if skew_res is not None:
            chosen_pl, chosen_off, chosen_lo, chosen_hi = skew_res
            joined = True
    if not joined:
        fail_reason = "unmatched_events" if out0 else "non_agent_step_event"
        return _make_seat_dict(seat_info, traj_rel, False, fail_reason, 0.0, None, None, False, 0, steps, m0, total_evs - m0)
    edges = _populate_step_events(steps, bounds, seat_evs, chosen_pl, chosen_off, margin_s)
    b_max, same_ra, r_before, u_ratio, txt_dis = _compute_step_rollups(steps, seat_evs, chosen_pl, ecosystem)
    reason = "joined" if zero_ok else "skew_joined"
    return _make_seat_dict(
        seat_info, traj_rel, True, reason, chosen_off, round(chosen_lo, 6), round(chosen_hi, 6),
        zero_ok, edges, steps, total_evs, 0, b_max, same_ra, r_before, u_ratio, txt_dis,
    )


def _classify_trial_join(seat_reports: list[dict[str, Any]]) -> tuple[bool, str, bool]:
    targets = [s for s in seat_reports if s["under_test"]] or seat_reports
    if not targets:
        return False, "no_trajectory", False
    n_joined = sum(1 for s in targets if s["joined"])
    if n_joined == len(targets):
        all_zero = all(s["zero_offset_ok"] for s in targets)
        return True, ("joined" if all_zero else "skew_joined"), all_zero
    if n_joined > 0:
        return False, "partial", False
    return False, ("no_trajectory" if all(s["join_reason"] == "no_trajectory" for s in targets) else "unjoined"), False


def join_trial_steps(
    trial_dir: Path,
    ecosystem: str,
    *,
    margin_s: float = DEFAULT_STEP_MARGIN_S,
    bundle: TrialBundle | None = None,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    assert ecosystem in ("olam", "bespoke")
    clean_margin = _validate_margin(margin_s)
    tdir = Path(trial_dir)
    bun = bundle if bundle is not None else load_trial_bundle(tdir, ecosystem)
    if events is None:
        ev_path = find_bespoke_board_log(tdir) if ecosystem == "bespoke" else find_olam_event_log(tdir)
        evs, _, _ = read_jsonl_static(ev_path, require_seq=(ecosystem == "olam"), root=tdir) if ev_path is not None else ([], None, None)
    else:
        evs = events
    bun = with_olam_seat_alias(bun, evs)
    seat_reports = [_join_single_seat(tdir, ecosystem, clean_margin, s_info, bun, evs) for s_info in bun.seats]
    joined, status, zero_all = _classify_trial_join(seat_reports)
    under_reports = [s for s in seat_reports if s["under_test"]] or seat_reports
    summary = {
        "batch_opens_max": max((int(s["batch_opens_max"]) for s in under_reports), default=0),
        "read_and_act_same_step": sum(int(s["read_and_act_same_step"]) for s in under_reports),
        "edge_events": sum(int(s["edge_events"]) for s in seat_reports),
        "zero_offset_ok_all": zero_all,
        "events_matched": sum(int(s["events_matched"]) for s in seat_reports),
        "events_unmatched": sum(int(s["events_unmatched"]) for s in seat_reports),
    }
    return {
        "trial": sanitize_surrogates(bun.trial_name), "path": sanitize_surrogates(str(tdir)),
        "ecosystem": ecosystem, "margin_s": clean_margin, "events_count": len(evs),
        "joined": joined, "join_status": status,
        "seats_joined": sum(1 for s in seat_reports if s["joined"]), "seats_total": len(seat_reports),
        "seats": seat_reports, "summary": summary,
    }


def run_step_join(root: Path, *, margin_s: float = DEFAULT_STEP_MARGIN_S) -> dict[str, Any]:
    clean_margin = _validate_margin(margin_s)
    target = Path(root)
    if not target.exists():
        raise InputError(f"path does not exist: {target}")
    if not target.is_dir():
        raise InputError(f"not a directory: {target}")
    discovered = discover_trials(target)
    if not discovered:
        raise InputError(f"no trials found under: {target}")
    trials = [join_trial_steps(tdir, eco, margin_s=clean_margin) for tdir, eco in discovered]
    summary = {
        "status_counts": dict(sorted(Counter(t["join_status"] for t in trials).items())),
        "seats_joined": sum(int(t["seats_joined"]) for t in trials),
        "seats_total": sum(int(t["seats_total"]) for t in trials),
        "events_matched": sum(int(t["summary"]["events_matched"]) for t in trials),
        "events_unmatched": sum(int(t["summary"]["events_unmatched"]) for t in trials),
        "edge_events": sum(int(t["summary"]["edge_events"]) for t in trials),
        "batch_opens_trials": sum(1 for t in trials if int(t["summary"]["batch_opens_max"]) >= 2),
        "same_step_read_act_trials": sum(1 for t in trials if int(t["summary"]["read_and_act_same_step"]) >= 1),
        "batch_opens_max": max((int(t["summary"]["batch_opens_max"]) for t in trials), default=0),
        "read_and_act_same_step": sum(int(t["summary"]["read_and_act_same_step"]) for t in trials),
    }
    return {
        "command": "step-join", "path": sanitize_surrogates(str(target)), "margin_s": clean_margin,
        "trials_scanned": len(trials), "trials_joined": sum(1 for t in trials if t["joined"]),
        "ecosystems": dict(sorted(Counter(t["ecosystem"] for t in trials).items())),
        "summary": summary, "trials": trials,
    }
