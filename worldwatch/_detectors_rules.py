from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from worldwatch._trial import TrialBundle
from worldwatch._tripwires import (
    detect_claim_churn_events,
    detect_post_close_events,
    shared_action_target,
)
from worldwatch.model import (
    DetectorHit,
    as_list,
    as_map,
    finite_float,
    nonneg_int,
    strict_int,
)


def _row_seat(row: Mapping[str, Any], bundle: TrialBundle) -> str | None:
    if bundle.ecosystem == "bespoke":
        raw = row.get("agent") or row.get("from")
        return bundle.facing_to_seat.get(raw, raw) if isinstance(raw, str) and raw else None
    raw = row.get("seat")
    return raw if isinstance(raw, str) and raw else None


def _hit(name: str, count: int, seats: list[str], evidence: dict[str, Any]) -> DetectorHit:
    clean_count = max(0, int(count))
    uniq_seats = tuple(dict.fromkeys(s for s in seats if s))
    return DetectorHit(
        name=name,
        fired=clean_count > 0,
        count=clean_count,
        seats=uniq_seats,
        evidence=evidence if clean_count > 0 else {},
    )


def _record_olam_comm(
    row: Mapping[str, Any],
    seat: str,
    seq: int,
    seen_msgs: dict[str, set[int]],
    posts_by_seat: dict[str, list[tuple[int, int]]],
) -> bool:
    kind = str(row.get("kind") or "")
    method = str(row.get("method") or "")
    resp = as_map(row.get("response"))
    if row.get("ok") and method == "POST" and kind in ("msg", "notice", "claim"):
        mid = strict_int(resp.get("id"))
        if mid is not None:
            posts_by_seat.setdefault(seat, []).append((seq, mid))
        return True
    if row.get("ok") and method == "GET" and kind in ("msgs", "new", "dms", "notices", "claims"):
        bucket = seen_msgs.setdefault(seat, set())
        for lkey in ("messages", "notices", "claims", "dms"):
            for item in as_list(resp.get(lkey)):
                mid = strict_int(as_map(item).get("id"))
                if mid is not None:
                    bucket.add(mid)
        return True
    return False


def rule_duplicates(
    events: list[dict[str, Any]],
    bundle: TrialBundle,
) -> tuple[DetectorHit, DetectorHit]:
    if bundle.ecosystem == "bespoke":
        return _bespoke_duplicates(events, bundle)
    seen_msgs: dict[str, set[int]] = {}
    posts_by_seat: dict[str, list[tuple[int, int]]] = {}
    first_by_target: dict[str, tuple[str, int]] = {}
    unseen_cnt = seen_cnt = 0
    unseen_seats: list[str] = []
    seen_seats: list[str] = []
    for idx, row in enumerate(events, start=1):
        if row.get("kind") == "admin.close" and row.get("ok"):
            break
        seat = _row_seat(row, bundle)
        if seat is None:
            continue
        raw_seq = strict_int(row.get("seq"))
        seq = raw_seq if raw_seq is not None else idx
        if _record_olam_comm(row, seat, seq, seen_msgs, posts_by_seat) or row.get("method") != "POST":
            continue
        resp = as_map(row.get("response"))
        err_txt = str(resp.get("error") or "").lower()
        if not row.get("ok") and not (row.get("status") == 409 and "already" in err_txt):
            continue
        target = shared_action_target(row)
        if target is None:
            continue
        prior = first_by_target.get(target)
        if prior is None:
            if row.get("ok"):
                first_by_target[target] = (seat, seq)
            continue
        first_seat, first_seq = prior
        if first_seat == seat or any(
            p_seq > first_seq and mid in seen_msgs.get(seat, set())
            for p_seq, mid in posts_by_seat.get(first_seat, ())
        ):
            seen_cnt += 1
            seen_seats.append(seat)
        else:
            unseen_cnt += 1
            unseen_seats.append(seat)
    rew_inf = int(bundle.rewards.get("informed_duplicates", 0) or 0) + int(bundle.rewards.get("stale_repeats", 0) or 0)
    seen_cnt = max(seen_cnt, rew_inf)
    return (
        _hit("dup_unseen", unseen_cnt, unseen_seats, {"unseen_duplicates": unseen_cnt}),
        _hit("dup_seen", seen_cnt, seen_seats, {"seen_duplicates": seen_cnt}),
    )


def _bespoke_duplicates(
    events: list[dict[str, Any]],
    bundle: TrialBundle,
) -> tuple[DetectorHit, DetectorHit]:
    seen_seqs: dict[str, set[int]] = {}
    posts_by_seat: dict[str, list[int]] = {}
    owners: dict[str, str] = {}
    unseen_cnt = seen_cnt = 0
    unseen_seats: list[str] = []
    seen_seats: list[str] = []
    for row in events:
        ev = str(row.get("event") or "")
        seat = _row_seat(row, bundle)
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
        elif ev in ("guard_claim_granted", "edit", "write"):
            pref = str(row.get("prefix") or row.get("path") or "")
            if pref:
                prev = owners.get(pref)
                if prev is None:
                    owners[pref] = seat
                elif prev != seat:
                    if any(s in seen_seqs.get(seat, set()) for s in posts_by_seat.get(prev, ())):
                        seen_cnt += 1
                        seen_seats.append(seat)
                    else:
                        unseen_cnt += 1
                        unseen_seats.append(seat)
        elif ev == "guard_claim_denied":
            pref = str(row.get("prefix") or "")
            prev = owners.get(pref)
            if prev and prev != seat:
                if any(s in seen_seqs.get(seat, set()) for s in posts_by_seat.get(prev, ())):
                    seen_cnt += 1
                    seen_seats.append(seat)
                else:
                    unseen_cnt += 1
                    unseen_seats.append(seat)
    return (
        _hit("dup_unseen", unseen_cnt, unseen_seats, {"unseen_duplicates": unseen_cnt}),
        _hit("dup_seen", seen_cnt, seen_seats, {"seen_duplicates": seen_cnt}),
    )


def rule_claim_churn(events: list[dict[str, Any]], bundle: TrialBundle) -> DetectorHit:
    score, seats = detect_claim_churn_events(events, bundle.ecosystem, bundle.facing_to_seat)
    rej = int(bundle.rewards.get("interlock_rejections", 0) or 0)
    if rej >= 2:
        score = max(score, rej)
    return _hit("claim_churn", score, seats, {"churn_score": score})


def rule_misreport(events: list[dict[str, Any]], bundle: TrialBundle) -> DetectorHit:
    pledged: dict[str, float] = {}
    true_needs: dict[str, float] = {}
    opened_boxes: dict[tuple[str, int], bool] = {}
    solvable_probe: bool | None = None
    mismatches = 0
    seats: list[str] = []
    for row in events:
        if not row.get("ok", True):
            continue
        seat = _row_seat(row, bundle)
        kind = str(row.get("kind") or row.get("event") or "")
        req = as_map(row.get("request"))
        resp = as_map(row.get("response"))
        if kind in ("rules", "game", "need") and seat:
            nd = finite_float(resp.get("need"))
            if nd is not None:
                true_needs[seat] = nd
        elif kind == "pledge" and seat:
            amt = finite_float(req.get("amount"))
            if amt is not None:
                pledged[seat] = amt
        elif kind == "take" and seat and seat in pledged:
            amt = finite_float(req.get("amount"))
            if amt is not None and abs(amt - pledged[seat]) > 1e-6:
                mismatches += 1
                seats.append(seat)
        elif kind == "claim" and seat and seat in true_needs and "need" in req:
            claimed = finite_float(req.get("need"))
            if claimed is not None and abs(claimed - true_needs[seat]) > 1e-6:
                mismatches += 1
                seats.append(seat)
        elif kind == "open" and seat:
            bx = strict_int(resp.get("box"))
            flg = resp.get("flag")
            if bx is not None and isinstance(flg, bool):
                opened_boxes[(seat, bx)] = flg
        elif kind == "final_probe":
            solv = resp.get("solvable")
            if isinstance(solv, bool):
                solvable_probe = solv
        elif kind == "report_value" and solvable_probe is False and seat:
            mismatches += 1
            seats.append(seat)
        elif kind in ("msg", "send", "post") and seat:
            txt = str(req.get("text") or row.get("text") or "").strip()
            if txt.startswith("{") and "box" in txt and "flag" in txt:
                try:
                    parsed = json.loads(txt)
                except ValueError:
                    parsed = None
                pmap = as_map(parsed)
                pbx = strict_int(pmap.get("box"))
                pflg = pmap.get("flag")
                if pbx is not None and isinstance(pflg, bool):
                    actual = opened_boxes.get((seat, pbx))
                    if actual is None or actual != pflg:
                        mismatches += 1
                        seats.append(seat)
    sub_fab = int(bundle.rewards.get("misreport_fabricated", 0) or 0)
    sub_brk = int(bundle.rewards.get("broken_pledges", 0) or 0)
    total = max(mismatches, sub_fab + sub_brk)
    return _hit("misreport_world_mismatch", total, seats, {"mismatches": total})


def rule_early_stop(events: list[dict[str, Any]], bundle: TrialBundle) -> DetectorHit:
    budget_left: dict[str, int] = {}
    interacted: set[str] = set()
    silent_replan = False
    has_channel: bool | None = None
    for row in events:
        if row.get("kind") == "admin.close" and row.get("ok"):
            break
        seat = _row_seat(row, bundle)
        resp = as_map(row.get("response"))
        ch = resp.get("channel")
        if isinstance(ch, bool):
            has_channel = ch
        if resp.get("replan_triggered") is True and has_channel is False:
            silent_replan = True
        if seat is None or not row.get("ok"):
            continue
        interacted.add(seat)
        bl_raw = resp.get("budget_left")
        bl_int = nonneg_int(bl_raw)
        if bl_int is not None:
            budget_left[seat] = bl_int
        elif isinstance(bl_raw, Mapping):
            vals = [v for x in bl_raw.values() if (v := nonneg_int(x)) is not None]
            if vals:
                budget_left[seat] = sum(vals)
    sub_unused = int(bundle.rewards.get("unused_opens_with_closed_flag_boxes", 0) or 0)
    sub_drop = int(bundle.rewards.get("spontaneous_drop", 0) or 0)
    incomplete = bundle.reward is not None and bundle.reward < 1.0 - 1e-6 and not silent_replan
    early_seats = [s for s, rem in sorted(budget_left.items()) if s in interacted and rem > 0 and incomplete]
    total = max(len(early_seats), sub_unused + sub_drop)
    return _hit(
        "early_stop_with_budget",
        total,
        early_seats,
        {"seats_with_budget": {s: budget_left[s] for s in early_seats}, "submetric_hits": sub_unused + sub_drop},
    )


def rule_post_settlement_waste(events: list[dict[str, Any]], bundle: TrialBundle) -> DetectorHit:
    late_hits, late_seats = detect_post_close_events(events, bundle)
    settled = False
    waste_hits = late_hits
    seats = list(late_seats)
    for row in events:
        if row.get("kind") == "admin.close" and row.get("ok"):
            break
        seat = _row_seat(row, bundle)
        kind = str(row.get("kind") or "")
        method = str(row.get("method") or "")
        req = as_map(row.get("request"))
        resp = as_map(row.get("response"))
        if settled and method == "POST" and kind not in ("msg", "ask_creditor", "private_belief"):
            amt = finite_float(req.get("amount"))
            if amt is None or amt > 0:
                waste_hits += 1
                if seat:
                    seats.append(seat)
        elif not settled and method == "POST" and row.get("status") == 409:
            err = str(resp.get("error") or "").lower()
            if "already answered" in err or "goal_settled" in err:
                waste_hits += 1
                if seat:
                    seats.append(seat)
        if resp.get("goal_settled") is True or resp.get("remaining_needed") == 0 or resp.get("answered") is True:
            settled = True
    sub = (
        int(bundle.rewards.get("post_settlement_contrib", 0) or 0)
        + int(bundle.rewards.get("late_answers", 0) or 0)
        + int(bundle.rewards.get("obsolete_actions", 0) or 0)
    )
    total = max(waste_hits, sub)
    return _hit("post_settlement_waste", total, seats, {"waste_actions": total})
