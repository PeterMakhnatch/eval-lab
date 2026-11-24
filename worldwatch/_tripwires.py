from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from worldwatch._io import check_no_symlink_ancestors
from worldwatch._trial import TrialBundle
from worldwatch.model import (
    CRITICAL_TRIPWIRES,
    Alert,
    SeatLive,
    as_list,
    as_map,
    finite_float,
    make_alert_id,
    parse_iso_utc,
    stamp_utc,
    strict_int,
)

_CHANNEL_KINDS = frozenset({
    "msg",
    "msgs",
    "new",
    "dms",
    "notice",
    "notices",
    "pledge",
    "pledges",
    "claim",
    "claims",
    "rules",
    "status",
    "health",
    "admin.close",
    "ask_creditor",
    "private_belief",
    "game",
    "task_state",
    "team_state",
    "round_state",
    "audit_log",
    "final_probe",
    "script.seed",
    "script.round",
})

_SHARED_TARGET_KEYS = (
    "box",
    "record",
    "bus",
    "crew",
    "bug_id",
    "target_id",
    "item_id",
    "task_id",
    "file",
    "path",
    "prefix",
    "slot",
    "zone",
)

_SINGLE_ANSWER_KINDS = frozenset({"answer", "submit_answer", "finalize"})
_FRICTION_HTTP_CODES = frozenset({400, 401, 403, 404, 405, 422})


def _event_seat(row: Mapping[str, Any], ecosystem: str, facing_map: Mapping[str, str]) -> str | None:
    if ecosystem == "bespoke":
        raw = row.get("agent") or row.get("from")
        if isinstance(raw, str) and raw:
            return facing_map.get(raw, raw)
        return None
    raw = row.get("seat")
    return raw if isinstance(raw, str) and raw else None


def _event_time(row: Mapping[str, Any]) -> datetime | None:
    for key in ("t", "ts", "timestamp"):
        if key in row:
            parsed = parse_iso_utc(row.get(key))
            if parsed is not None:
                return parsed
    return None


def is_counted_world_request(row: Mapping[str, Any], ecosystem: str) -> bool:
    if ecosystem == "bespoke":
        return str(row.get("event") or "") not in ("config", "register", "subagent")
    kind = str(row.get("kind") or "")
    if kind in ("script.seed", "script.round"):
        return False
    return not (kind == "admin.close" and row.get("status") == 200)


def is_world_friction_rejection(row: Mapping[str, Any], ecosystem: str) -> bool:
    if ecosystem == "bespoke":
        ev_kind = str(row.get("event") or "")
        if ev_kind in ("config", "register", "subagent", "guard_claim_denied", "guard_unread_block"):
            return False
        if ev_kind == "refused" or row.get("ok") is False:
            return True
        st = strict_int(row.get("status"))
        return st is not None and (st in _FRICTION_HTTP_CODES or st >= 500)
    st = strict_int(row.get("status"))
    if st == 409:
        return False
    resp = as_map(row.get("response"))
    err_txt = str(resp.get("error") or "").lower()
    if st == 429:
        return not (
            resp.get("budget_left") == 0
            or "budget" in err_txt
            or "calls left" in err_txt
            or "quota" in err_txt
        )
    if st is not None:
        return st in _FRICTION_HTTP_CODES or st >= 500
    return row.get("ok") is False


def extract_seat_event_stats(
    events: list[dict[str, Any]],
    ecosystem: str,
    facing_map: Mapping[str, str],
) -> tuple[dict[str, int], dict[str, datetime], int, int]:
    counts: dict[str, int] = {}
    latest: dict[str, datetime] = {}
    total_req = 0
    rejected = 0
    for row in events:
        seat = _event_seat(row, ecosystem, facing_map)
        ts = _event_time(row)
        if seat is not None:
            counts[seat] = counts.get(seat, 0) + 1
            if ts is not None and (seat not in latest or ts > latest[seat]):
                latest[seat] = ts
        if not is_counted_world_request(row, ecosystem):
            continue
        total_req += 1
        if is_world_friction_rejection(row, ecosystem):
            rejected += 1
    return counts, latest, total_req, rejected


def build_seat_liveness(
    bundle: TrialBundle,
    seat_counts: Mapping[str, int],
    seat_latest: Mapping[str, datetime],
    now: datetime,
    stall_seconds: float,
    config_mtime: datetime | None,
) -> tuple[SeatLive, ...]:
    assert stall_seconds > 0
    finished_trial = bundle.result_state == "ready"
    out: list[SeatLive] = []
    for info in bundle.seats:
        ev_cnt = seat_counts.get(info.seat, 0)
        steps = info.atif.agent_steps if info.atif and info.atif.problem is None else 0
        cands = [
            dt
            for dt in (
                info.atif.last_step_at if info.atif and info.atif.problem is None else None,
                seat_latest.get(info.seat),
            )
            if dt is not None
        ]
        last_act = max(cands) if cands else None
        anchor = last_act or info.started_at or bundle.agent_started_at or bundle.started_at or config_mtime
        stalled = False
        silent_s: float | None = None
        if finished_trial:
            end_dt = info.finished_at or bundle.agent_finished_at or bundle.finished_at or now
            if anchor is not None and end_dt is not None:
                silent_s = round(max(0.0, (end_dt - anchor).total_seconds()), 1)
            if info.exception == "AgentTimeoutError":
                if info.is_model and (info.atif is None or info.atif.problem is not None or last_act is None):
                    stalled = True
                elif silent_s is not None and silent_s >= stall_seconds:
                    stalled = True
            state = "stalled" if stalled else ("error" if (info.exception or info.exit_error or info.exit_code not in (None, 0)) else "done")
        else:
            if anchor is not None:
                silent_s = round(max(0.0, (now - anchor).total_seconds()), 1)
            if info.exit_code is not None or info.exit_error is not None:
                state = "exited"
            elif info.atif is not None and info.atif.marked_complete:
                state = "done"
            else:
                state = "playing"
                if silent_s is not None and silent_s >= stall_seconds:
                    stalled = True
        out.append(
            SeatLive(
                seat=info.seat,
                state=state,
                model=info.model_name or info.agent_name,
                agent_steps=steps,
                world_events=ev_cnt,
                last_activity_at=stamp_utc(last_act),
                silent_s=silent_s,
                exit_code=info.exit_code,
                stalled=stalled,
            )
        )
    return tuple(out)


def shared_action_target(row: Mapping[str, Any]) -> str | None:
    kind = str(row.get("kind") or "")
    if not kind or kind in _CHANNEL_KINDS:
        return None
    if kind in _SINGLE_ANSWER_KINDS:
        return f"{kind}:team_singleton"
    for container in (as_map(row.get("request")), as_map(row.get("response"))):
        for key in _SHARED_TARGET_KEYS:
            val = container.get(key)
            if isinstance(val, (str, int)) and not isinstance(val, bool):
                return f"{kind}:{key}={val}"
    return None


def detect_dup_unseen_events(
    events: list[dict[str, Any]],
    ecosystem: str,
    facing_map: Mapping[str, str],
) -> tuple[int, list[str]]:
    if ecosystem == "bespoke":
        return _detect_bespoke_dup_unseen(events, facing_map)
    seen_msgs: dict[str, set[int]] = {}
    posts_by_seat: dict[str, list[tuple[int, int]]] = {}
    first_by_target: dict[str, tuple[str, int]] = {}
    dup_seats: list[str] = []
    hits = 0
    for idx, row in enumerate(events, start=1):
        if row.get("kind") == "admin.close" and row.get("ok"):
            break
        seat = _event_seat(row, "olam", facing_map)
        if seat is None or not row.get("ok"):
            continue
        raw_seq = strict_int(row.get("seq"))
        seq = raw_seq if raw_seq is not None else idx
        kind = str(row.get("kind") or "")
        method = str(row.get("method") or "")
        resp = as_map(row.get("response"))
        if method == "POST" and kind in ("msg", "notice", "claim"):
            mid = strict_int(resp.get("id"))
            if mid is not None:
                posts_by_seat.setdefault(seat, []).append((seq, mid))
            continue
        if method == "GET" and kind in ("msgs", "new", "dms", "notices", "claims"):
            bucket = seen_msgs.setdefault(seat, set())
            for list_key in ("messages", "notices", "claims", "dms"):
                for msg in as_list(resp.get(list_key)):
                    mid = strict_int(as_map(msg).get("id"))
                    if mid is not None:
                        bucket.add(mid)
            continue
        if method != "POST":
            continue
        target = shared_action_target(row)
        if target is None:
            continue
        prior = first_by_target.get(target)
        if prior is None:
            first_by_target[target] = (seat, seq)
            continue
        first_seat, first_seq = prior
        if first_seat == seat:
            continue
        read_ids = seen_msgs.get(seat, set())
        informed = any(p_seq > first_seq and mid in read_ids for p_seq, mid in posts_by_seat.get(first_seat, ()))
        if not informed:
            hits += 1
            if seat not in dup_seats:
                dup_seats.append(seat)
    return hits, dup_seats


def _detect_bespoke_dup_unseen(
    events: list[dict[str, Any]],
    facing_map: Mapping[str, str],
) -> tuple[int, list[str]]:
    seen_seqs: dict[str, set[int]] = {}
    posts_by_seat: dict[str, list[int]] = {}
    owner_by_prefix: dict[str, tuple[str, int]] = {}
    hits = 0
    dup_seats: list[str] = []
    for idx, row in enumerate(events, start=1):
        ev = str(row.get("event") or "")
        seat = _event_seat(row, "bespoke", facing_map)
        if seat is None:
            continue
        b_seq = strict_int(row.get("seq"))
        if ev in ("send", "post") and b_seq is not None:
            posts_by_seat.setdefault(seat, []).append(b_seq)
        elif ev in ("inbox", "read"):
            bucket = seen_seqs.setdefault(seat, set())
            for s in as_list(row.get("seqs")):
                si = strict_int(s)
                if si is not None:
                    bucket.add(si)
        elif ev in ("guard_claim_granted", "edit", "write") and isinstance(row.get("prefix") or row.get("path"), str):
            pref = str(row.get("prefix") or row.get("path"))
            owner_by_prefix.setdefault(pref, (seat, idx))
        elif ev == "guard_claim_denied" and isinstance(row.get("prefix"), str):
            pref = str(row["prefix"])
            prior = owner_by_prefix.get(pref)
            if prior is not None and prior[0] != seat:
                read_s = seen_seqs.get(seat, set())
                if not any(s in read_s for s in posts_by_seat.get(prior[0], ())):
                    hits += 1
                    if seat not in dup_seats:
                        dup_seats.append(seat)
    return hits, dup_seats


def detect_claim_churn_events(
    events: list[dict[str, Any]],
    ecosystem: str,
    facing_map: Mapping[str, str],
) -> tuple[int, list[str]]:
    target_touches: dict[str, int] = {}
    denied = 0
    seats_hit: list[str] = []
    for row in events:
        seat = _event_seat(row, ecosystem, facing_map)
        if ecosystem == "bespoke":
            ev = str(row.get("event") or "")
            pref = str(row.get("prefix") or "")
            if ev in ("guard_claim_granted", "guard_claim_released", "guard_claim_denied"):
                if pref:
                    target_touches[pref] = target_touches.get(pref, 0) + 1
                if ev == "guard_claim_denied":
                    denied += 1
                if seat and seat not in seats_hit and (denied >= 2 or target_touches.get(pref, 0) >= 3):
                    seats_hit.append(seat)
        else:
            kind = str(row.get("kind") or "")
            req = as_map(row.get("request"))
            if kind in ("claim", "release"):
                tgt = str(req.get("target") or req.get("resource") or req.get("box") or req.get("item") or kind)
                target_touches[tgt] = target_touches.get(tgt, 0) + 1
                if not row.get("ok"):
                    denied += 1
                if seat and seat not in seats_hit and (denied >= 2 or target_touches[tgt] >= 3):
                    seats_hit.append(seat)
            elif kind == "msg" and row.get("ok") and isinstance(req.get("text"), str):
                txt = req["text"].strip()
                if txt.startswith("{") and "claim" in txt:
                    try:
                        body = json.loads(txt)
                    except ValueError:
                        body = None
                    if isinstance(body, Mapping) and "claim" in body:
                        key = f"msg_claim:{body['claim']}"
                        target_touches[key] = target_touches.get(key, 0) + 1
                        if seat and target_touches[key] >= 2 and seat not in seats_hit:
                            seats_hit.append(seat)
    churn_targets = sum(1 for c in target_touches.values() if c >= 3) + sum(
        1 for k, c in target_touches.items() if k.startswith("msg_claim:") and c >= 2
    )
    score = churn_targets + (denied if denied >= 2 else 0)
    return score, seats_hit


def detect_post_close_events(
    events: list[dict[str, Any]],
    bundle: TrialBundle,
) -> tuple[int, list[str]]:
    hits = 0
    seats: list[str] = []
    if bundle.ecosystem == "bespoke":
        end_dt = bundle.agent_finished_at
        closed = False
        for row in events:
            ev = str(row.get("event") or "")
            if ev == "close":
                closed = True
                continue
            if ev in ("config", "register"):
                continue
            ts = _event_time(row)
            is_after = closed or (end_dt is not None and ts is not None and (ts - end_dt).total_seconds() > 0.001)
            if is_after:
                hits += 1
                seat = _event_seat(row, "bespoke", bundle.facing_to_seat)
                if seat and seat not in seats:
                    seats.append(seat)
        return hits, seats
    closed = False
    for row in events:
        if row.get("kind") == "admin.close" and row.get("ok"):
            closed = True
            continue
        if row.get("late") is True or (closed and row.get("seat") is not None):
            hits += 1
            seat = _event_seat(row, "olam", bundle.facing_to_seat)
            if seat and seat not in seats:
                seats.append(seat)
    return hits, seats


def make_alert(
    code: str,
    now_iso: str,
    trial: str | None,
    seat: str | None,
    message: str,
    evidence: dict[str, Any],
    discriminator: str = "",
) -> Alert:
    severity = "critical" if code in CRITICAL_TRIPWIRES else "warn"
    aid = make_alert_id(code, trial, seat, discriminator)
    return Alert(
        alert_id=aid,
        timestamp=now_iso,
        code=code,
        severity=severity,
        trial=trial,
        seat=seat,
        message=message,
        evidence=evidence,
    )


def _has_olam_world_context(bundle: TrialBundle, events_count: int) -> bool:
    if events_count > 0:
        return True
    tdir = bundle.path
    if os.path.isdir(tdir / "seats") and not os.path.islink(tdir / "seats"):
        return True
    for rel in ("verifier/events.jsonl", "logs/world/events.jsonl", "artifacts/logs/world/events.jsonl"):
        cand = tdir / rel
        if check_no_symlink_ancestors(cand, root=tdir) is None and os.path.lexists(cand):
            return True
    return False


def _health_tripwires(
    bundle: TrialBundle,
    seats_live: tuple[SeatLive, ...],
    total_req: int,
    rejected_req: int,
    chain_error: str | None,
    now_iso: str,
    max_reject_ratio: float,
    events_count: int = 0,
) -> list[Alert]:
    alerts: list[Alert] = []
    tname = bundle.trial_name
    if chain_error is not None:
        msg = f"Trial {tname}: event/usage log integrity check failed ({chain_error})."
        alerts.append(make_alert("broken-hash-chain", now_iso, tname, None, msg, {"error": chain_error}))
    stalled_seats = [s for s in seats_live if s.stalled]
    for s in stalled_seats:
        msg = f"Trial {tname} seat {s.seat} silent for {s.silent_s}s."
        alerts.append(make_alert("silent-seat", now_iso, tname, s.seat, msg, {"silent_s": s.silent_s, "state": s.state}))
    active_candidates = [s for s in seats_live if s.state in ("playing", "stalled")]
    if (
        seats_live
        and active_candidates
        and len(stalled_seats) == len(active_candidates)
        and (bundle.result_state != "ready" or len(stalled_seats) == len(seats_live))
    ):
        msg = f"Trial {tname}: all {len(stalled_seats)} active seat(s) are silent above threshold."
        alerts.append(make_alert("all-seats-silent", now_iso, tname, None, msg, {"seats": [s.seat for s in stalled_seats]}))
    ratio = (rejected_req / total_req) if total_req > 0 else 0.0
    n_seats = max(1, len(seats_live))
    min_rej = 1 if max_reject_ratio <= 0.0 else max(6, 3 * n_seats)
    if total_req >= min_rej and rejected_req >= min_rej and ratio > max_reject_ratio:
        msg = f"Trial {tname}: rejected {rejected_req}/{total_req} ({ratio:.1%}) exceeds {max_reject_ratio:.1%}."
        ev = {"rejected": rejected_req, "total": total_req, "ratio": round(ratio, 6)}
        alerts.append(make_alert("reject-storm", now_iso, tname, None, msg, ev))
    if bundle.ecosystem == "olam" and _has_olam_world_context(bundle, events_count):
        model_seats = {info.seat for info in bundle.seats if info.under_test and info.is_model}
        for s in seats_live:
            if s.seat in model_seats and s.agent_steps >= 5 and s.world_events == 0:
                msg = f"Trial {tname} seat {s.seat} took {s.agent_steps} steps with 0 world requests."
                ev = {"agent_steps": s.agent_steps, "world_events": 0, "state": s.state}
                alerts.append(make_alert("zero-world-activity", now_iso, tname, s.seat, msg, ev))
    return alerts


def evaluate_trial_tripwires(
    bundle: TrialBundle,
    seats_live: tuple[SeatLive, ...],
    events: list[dict[str, Any]],
    total_req: int,
    rejected_req: int,
    chain_error: str | None,
    now_iso: str,
    max_reject_ratio: float,
) -> list[Alert]:
    alerts = _health_tripwires(
        bundle,
        seats_live,
        total_req,
        rejected_req,
        chain_error,
        now_iso,
        max_reject_ratio,
        events_count=len(events),
    )
    tname = bundle.trial_name
    churn_score, churn_seats = detect_claim_churn_events(events, bundle.ecosystem, bundle.facing_to_seat)
    if churn_score > 0:
        alerts.append(
            make_alert(
                "claim-churn",
                now_iso,
                tname,
                churn_seats[0] if len(churn_seats) == 1 else None,
                f"Trial {tname}: claim churn detected (score={churn_score}).",
                {"score": churn_score, "seats": churn_seats},
            )
        )
    dup_hits, dup_seats = detect_dup_unseen_events(events, bundle.ecosystem, bundle.facing_to_seat)
    if dup_hits > 0:
        alerts.append(
            make_alert(
                "dup-unseen",
                now_iso,
                tname,
                dup_seats[0] if len(dup_seats) == 1 else None,
                f"Trial {tname}: {dup_hits} duplicate action(s) with unseen peer updates.",
                {"count": dup_hits, "seats": dup_seats},
            )
        )
    late_hits, late_seats = detect_post_close_events(events, bundle)
    if late_hits > 0:
        alerts.append(
            make_alert(
                "post-close-action",
                now_iso,
                tname,
                late_seats[0] if len(late_seats) == 1 else None,
                f"Trial {tname}: {late_hits} post-close action(s) recorded.",
                {"count": late_hits, "seats": late_seats},
            )
        )
    return alerts


def evaluate_cost_tripwire(
    total_cost_usd: float | None,
    max_cost_usd: float | None,
    now_iso: str,
) -> Alert | None:
    if max_cost_usd is None or total_cost_usd is None:
        return None
    val = finite_float(total_cost_usd)
    if val is None or val <= max_cost_usd:
        return None
    return make_alert(
        "cost-spike",
        now_iso,
        None,
        None,
        f"Total observed cost ${val:.4f} exceeds cap ${max_cost_usd:.4f}.",
        {"total_cost_usd": round(val, 6), "max_cost_usd": round(max_cost_usd, 6)},
    )
