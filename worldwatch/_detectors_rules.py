from __future__ import annotations

import json
import re
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

_META_POST_KINDS = frozenset({"msg", "notice", "ask_creditor", "private_belief", "script.seed", "script.round"})
_POOL_EXEMPT_KINDS = frozenset({"submit_choice", "report_value", "report_unknown", "flag_misreport", "verify"})
_TERMINAL_ANSWER_KINDS = frozenset({"submit_choice", "report_value", "report_unknown", "answer", "submit_answer", "finalize"})
_EXTRA_DUP_SKIP = frozenset({
    "msg", "msgs", "new", "dms", "notice", "notices", "pledge", "pledges",
    "claim", "claims", "release", "rules", "status", "health", "admin.close", "ask_creditor",
    "private_belief", "game", "task_state", "team_state", "round_state", "audit_log",
    "final_probe", "script.seed", "script.round", "mine",
})
_EXTRA_TARGET_KEYS = ("room", "target", "resource", "item", "node", "cell")


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


def joined_event_steps(step_join: Mapping[str, Any] | None) -> dict[str, dict[int, int]]:
    if step_join is None:
        return {}
    out: dict[str, dict[int, int]] = {}
    for s_raw in as_list(step_join.get("seats")):
        s_map = as_map(s_raw)
        seat = s_map.get("seat")
        if not isinstance(seat, str) or not seat or s_map.get("joined") is not True:
            continue
        seq_map: dict[int, int] = {}
        for st_raw in as_list(s_map.get("steps")):
            st_map = as_map(st_raw)
            s_idx = nonneg_int(st_map.get("step_index"))
            if s_idx is None:
                continue
            for seq_val in as_list(st_map.get("event_seqs")):
                sq = strict_int(seq_val)
                if sq is not None:
                    seq_map[sq] = s_idx
        out[seat] = seq_map
    return out


def _dup_action_target(row: Mapping[str, Any]) -> str | None:
    base = shared_action_target(row)
    if base is not None:
        return base
    kind = str(row.get("kind") or "")
    if not kind or kind in _EXTRA_DUP_SKIP:
        return None
    req, resp = as_map(row.get("request")), as_map(row.get("response"))
    rnd = strict_int(req.get("round")) if kind == "verify" else None
    if rnd is None and kind == "verify":
        rnd = strict_int(resp.get("round"))
    prefix = f"{kind}@r{rnd}" if rnd is not None else kind
    for container in (req, resp):
        for key in _EXTRA_TARGET_KEYS:
            val = container.get(key)
            if isinstance(val, (str, int)) and not isinstance(val, bool):
                return f"{prefix}:{key}={val}"
    return None


def _target_mentioned_in_post(target: str, text: str) -> bool:
    if not text or ":" not in target or "=" not in target:
        return False
    _, _, kv = target.partition(":")
    key, _, val = kv.partition("=")
    if not val:
        return False
    esc_val = re.escape(val)
    pat = rf"\b{re.escape(key)}(?:es|s)?\"?\s*[#=:]?\s*{esc_val}(?!\d)"
    return re.search(pat, text, re.I) is not None


def _record_olam_comm(
    row: Mapping[str, Any],
    seat: str,
    seq: int,
    seat_steps: Mapping[str, Mapping[int, int]],
    seen_msgs: dict[str, set[int]],
    seen_msg_step: dict[str, dict[int, int]],
    posts_by_seat: dict[str, list[tuple[int, int, str]]],
) -> bool:
    kind = str(row.get("kind") or "")
    method = str(row.get("method") or "")
    resp = as_map(row.get("response"))
    req = as_map(row.get("request"))
    if row.get("ok") and method == "POST" and kind in ("msg", "notice", "claim"):
        mid = strict_int(resp.get("id"))
        if mid is not None:
            txt = str(req.get("text") or resp.get("text") or "")
            posts_by_seat.setdefault(seat, []).append((seq, mid, txt))
        return True
    if row.get("ok") and method == "GET" and kind in ("msgs", "new", "dms", "notices", "claims"):
        bucket = seen_msgs.setdefault(seat, set())
        r_step = seat_steps.get(seat, {}).get(seq)
        step_bucket = seen_msg_step.setdefault(seat, {})
        for lkey in ("messages", "notices", "claims", "dms"):
            for item in as_list(resp.get(lkey)):
                mid = strict_int(as_map(item).get("id"))
                if mid is not None:
                    bucket.add(mid)
                    if r_step is not None:
                        prev_st = step_bucket.get(mid)
                        step_bucket[mid] = r_step if prev_st is None else min(prev_st, r_step)
        return True
    return False


def _classify_olam_dup(
    seat: str,
    seq: int,
    priors: list[tuple[str, int]],
    target: str,
    seat_steps: Mapping[str, Mapping[int, int]],
    seen_msgs: Mapping[str, set[int]],
    seen_msg_step: Mapping[str, Mapping[int, int]],
    posts_by_seat: Mapping[str, list[tuple[int, int, str]]],
) -> tuple[bool, bool]:
    s_map = seat_steps.get(seat)
    act_step = s_map.get(seq) if s_map is not None else None
    used_causal = act_step is not None
    if any(a_seat == seat for a_seat, _ in priors):
        return True, used_causal
    qual_mids = [
        mid
        for a_seat, a_seq in priors
        for p_seq, mid, txt in posts_by_seat.get(a_seat, ())
        if p_seq < seq and (p_seq > a_seq or _target_mentioned_in_post(target, txt))
    ]
    if act_step is not None:
        st_reads = seen_msg_step.get(seat, {})
        return any(mid in st_reads and st_reads[mid] < act_step for mid in qual_mids), True
    read_ids = seen_msgs.get(seat, set())
    return any(mid in read_ids for mid in qual_mids), False


def rule_duplicates(
    events: list[dict[str, Any]],
    bundle: TrialBundle,
    *,
    step_join: Mapping[str, Any] | None = None,
) -> tuple[DetectorHit, DetectorHit]:
    seat_steps = joined_event_steps(step_join)
    if bundle.ecosystem == "bespoke":
        return _bespoke_duplicates(events, bundle, seat_steps)
    seen_msgs: dict[str, set[int]] = {}
    seen_msg_step: dict[str, dict[int, int]] = {}
    posts_by_seat: dict[str, list[tuple[int, int, str]]] = {}
    prior_actors_by_target: dict[str, list[tuple[str, int]]] = {}
    unseen_cnt = seen_cnt = 0
    unseen_causal = seen_causal = False
    unseen_seats: list[str] = []
    seen_seats: list[str] = []
    for idx, row in enumerate(events, start=1):
        if row.get("kind") == "admin.close" and row.get("ok"):
            break
        seat = _row_seat(row, bundle)
        if seat is None:
            continue
        seq = strict_int(row.get("seq")) or idx
        if _record_olam_comm(row, seat, seq, seat_steps, seen_msgs, seen_msg_step, posts_by_seat) or row.get("method") != "POST":
            continue
        resp = as_map(row.get("response"))
        err_txt = str(resp.get("error") or "").lower()
        if not row.get("ok") and not (row.get("status") == 409 and "already" in err_txt):
            continue
        target = _dup_action_target(row)
        if target is None:
            continue
        priors = prior_actors_by_target.get(target)
        if not priors:
            if row.get("ok"):
                prior_actors_by_target[target] = [(seat, seq)]
            continue
        is_seen, causal = _classify_olam_dup(
            seat, seq, priors, target, seat_steps, seen_msgs, seen_msg_step, posts_by_seat,
        )
        if all(a_seat != seat for a_seat, _ in priors):
            priors.append((seat, seq))
        if is_seen:
            seen_cnt += 1
            seen_causal = seen_causal or causal
            seen_seats.append(seat)
        else:
            unseen_cnt += 1
            unseen_causal = unseen_causal or causal
            unseen_seats.append(seat)
    rew_inf = int(bundle.rewards.get("informed_duplicates", 0) or 0) + int(bundle.rewards.get("stale_repeats", 0) or 0)
    seen_cnt = max(seen_cnt, rew_inf)
    if seen_cnt > 0 and not seen_seats and len(bundle.seats) == 1:
        seen_seats = [bundle.seats[0].seat]
    u_mode = "step_causal" if unseen_causal else "log_order"
    s_mode = "step_causal" if seen_causal else "log_order"
    return (
        _hit("dup_unseen", unseen_cnt, unseen_seats, {"unseen_duplicates": unseen_cnt, "causal_mode": u_mode}),
        _hit("dup_seen", seen_cnt, seen_seats, {"seen_duplicates": seen_cnt, "causal_mode": s_mode}),
    )


def _bespoke_duplicates(
    events: list[dict[str, Any]],
    bundle: TrialBundle,
    seat_steps: Mapping[str, Mapping[int, int]],
) -> tuple[DetectorHit, DetectorHit]:
    seen_seqs: dict[str, set[int]] = {}
    seen_seq_step: dict[str, dict[int, int]] = {}
    posts_by_seat: dict[str, list[int]] = {}
    owners: dict[str, list[str]] = {}
    unseen_cnt = seen_cnt = 0
    unseen_causal = seen_causal = False
    unseen_seats: list[str] = []
    seen_seats: list[str] = []
    for idx, row in enumerate(events, start=1):
        ev = str(row.get("event") or "")
        if ev == "close":
            break
        seat = _row_seat(row, bundle)
        if seat is None:
            continue
        b_seq = strict_int(row.get("seq"))
        pref = str(row.get("prefix") or row.get("path") or "")
        if ev in ("send", "post") and b_seq is not None:
            posts_by_seat.setdefault(seat, []).append(b_seq)
        elif ev in ("inbox", "read"):
            bucket = seen_seqs.setdefault(seat, set())
            r_step = seat_steps.get(seat, {}).get(idx)
            st_bucket = seen_seq_step.setdefault(seat, {})
            for s in as_list(row.get("seqs")):
                si = strict_int(s)
                if si is not None:
                    bucket.add(si)
                    if r_step is not None:
                        prev_st = st_bucket.get(si)
                        st_bucket[si] = r_step if prev_st is None else min(prev_st, r_step)
        elif ev == "guard_claim_released" and pref:
            owners.pop(pref, None)
        elif ev in ("guard_claim_granted", "edit", "write", "guard_claim_denied") and pref:
            holders = owners.get(pref)
            if not holders:
                if ev != "guard_claim_denied":
                    owners[pref] = [seat]
                continue
            if seat in holders:
                continue
            act_step = seat_steps.get(seat, {}).get(idx)
            if act_step is not None:
                st_reads = seen_seq_step.get(seat, {})
                is_seen = any(s in st_reads and st_reads[s] < act_step for h in holders for s in posts_by_seat.get(h, ()))
                causal = True
            else:
                is_seen = any(s in seen_seqs.get(seat, set()) for h in holders for s in posts_by_seat.get(h, ()))
                causal = False
            holders.append(seat)
            if is_seen:
                seen_cnt += 1
                seen_causal = seen_causal or causal
                seen_seats.append(seat)
            else:
                unseen_cnt += 1
                unseen_causal = unseen_causal or causal
                unseen_seats.append(seat)
    u_mode = "step_causal" if unseen_causal else "log_order"
    s_mode = "step_causal" if seen_causal else "log_order"
    return (
        _hit("dup_unseen", unseen_cnt, unseen_seats, {"unseen_duplicates": unseen_cnt, "causal_mode": u_mode}),
        _hit("dup_seen", seen_cnt, seen_seats, {"seen_duplicates": seen_cnt, "causal_mode": s_mode}),
    )


def rule_claim_churn(events: list[dict[str, Any]], bundle: TrialBundle) -> DetectorHit:
    score, seats = detect_claim_churn_events(events, bundle.ecosystem, bundle.facing_to_seat)
    rej = int(bundle.rewards.get("interlock_rejections", 0) or 0)
    if rej >= 2:
        score = max(score, rej)
    if score > 0 and not seats and len(bundle.seats) == 1:
        seats = [bundle.seats[0].seat]
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
    if total > 0 and not seats and len(bundle.seats) == 1:
        seats = [bundle.seats[0].seat]
    return _hit("misreport_world_mismatch", total, seats, {"mismatches": total})


def rule_early_stop(events: list[dict[str, Any]], bundle: TrialBundle) -> DetectorHit:
    budget_left: dict[str, int] = {}
    ep_budgets: dict[str, dict[str, int]] = {}
    interacted: set[str] = set()
    completed_seats: set[str] = set()
    silent_replan = False
    has_channel: bool | None = None
    for row in events:
        if row.get("kind") == "admin.close" and row.get("ok"):
            break
        seat = _row_seat(row, bundle)
        kind = str(row.get("kind") or "")
        resp = as_map(row.get("response"))
        ch = resp.get("channel")
        if isinstance(ch, bool):
            has_channel = ch
        if resp.get("replan_triggered") is True and has_channel is False:
            silent_replan = True
        if seat is None or not row.get("ok"):
            continue
        interacted.add(seat)
        if resp.get("phase") == "completed" or resp.get("answered") is True or kind in _TERMINAL_ANSWER_KINDS:
            completed_seats.add(seat)
            ep_budgets.pop(seat, None)
            budget_left[seat] = 0
            continue
        if seat in completed_seats or kind == "flag_misreport":
            continue
        bl_raw = resp.get("budget_left")
        bl_int = nonneg_int(bl_raw)
        if bl_int is not None:
            if seat in ep_budgets and kind in ep_budgets[seat]:
                ep_budgets[seat][kind] = bl_int
                budget_left[seat] = sum(ep_budgets[seat].values())
            elif seat in ep_budgets and kind == "drop" and bl_int == 0 and "execute" in ep_budgets[seat]:
                ep_budgets[seat]["execute"] = 0
                budget_left[seat] = sum(ep_budgets[seat].values())
            elif seat not in ep_budgets:
                budget_left[seat] = bl_int
        elif isinstance(bl_raw, Mapping):
            ep_map = {
                str(k): v for k, x in bl_raw.items()
                if str(k) not in (
                    "flag_misreport", "accuse", "msg", "ask_creditor", "verify_dependency", "prepare", "drop",
                )
                and (v := nonneg_int(x)) is not None
            }
            if ep_map:
                ep_budgets[seat] = ep_map
                budget_left[seat] = sum(ep_map.values())
    sub_unused = int(bundle.rewards.get("unused_opens_with_closed_flag_boxes", 0) or 0)
    sub_drop = int(bundle.rewards.get("spontaneous_drop", 0) or 0)
    incomplete = bundle.reward is not None and bundle.reward < 1.0 - 1e-6 and not silent_replan
    early_seats = [
        s for s, rem in sorted(budget_left.items())
        if s in interacted and s not in completed_seats and rem > 0 and incomplete
    ]
    total = max(len(early_seats), sub_unused + sub_drop)
    if total > 0 and not early_seats and len(bundle.seats) == 1:
        early_seats = [bundle.seats[0].seat]
    return _hit(
        "early_stop_with_budget",
        total,
        early_seats,
        {"seats_with_budget": {s: budget_left.get(s, 0) for s in early_seats}, "submetric_hits": sub_unused + sub_drop},
    )


def rule_post_settlement_waste(events: list[dict[str, Any]], bundle: TrialBundle) -> DetectorHit:
    late_hits, late_seats = detect_post_close_events(events, bundle)
    pool_settled = False
    answered = False
    waste_hits = late_hits
    seats = list(late_seats)
    for row in events:
        if row.get("kind") == "admin.close" and row.get("ok"):
            break
        seat = _row_seat(row, bundle)
        kind = str(row.get("kind") or "")
        method = str(row.get("method") or "")
        ok = row.get("ok") is True
        status = strict_int(row.get("status"))
        req = as_map(row.get("request"))
        resp = as_map(row.get("response"))
        if method == "POST" and kind not in _META_POST_KINDS:
            err = str(resp.get("error") or "").lower()
            is_dup_409 = status == 409 and (
                "already answered" in err
                or "already submitted" in err
                or "goal_settled" in err
                or "already settled" in err
            )
            amt = finite_float(req.get("amount"))
            is_waste = (
                is_dup_409
                or (ok and resp.get("post_settlement_waste") is True)
                or (answered and ok)
                or (
                    (pool_settled or resp.get("goal_settled_before") is True)
                    and ok
                    and kind not in _POOL_EXEMPT_KINDS
                    and amt is not None
                    and amt > 0
                )
            )
            if is_waste:
                waste_hits += 1
                if seat:
                    seats.append(seat)
        if ok:
            if resp.get("goal_settled") is True or resp.get("remaining_needed") == 0:
                pool_settled = True
            if resp.get("answered") is True or kind in _TERMINAL_ANSWER_KINDS:
                answered = True
    sub = (
        int(bundle.rewards.get("post_settlement_contrib", 0) or 0)
        + int(bundle.rewards.get("late_answers", 0) or 0)
        + int(bundle.rewards.get("obsolete_actions", 0) or 0)
    )
    total = max(waste_hits, sub)
    if total > 0 and not seats and len(bundle.seats) == 1:
        seats = [bundle.seats[0].seat]
    return _hit("post_settlement_waste", total, seats, {"waste_actions": total})
