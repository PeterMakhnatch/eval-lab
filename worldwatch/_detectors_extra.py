from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from worldwatch._detectors_rules import _hit, _row_seat, joined_event_steps
from worldwatch._trial import TrialBundle
from worldwatch.model import DetectorHit, as_list, as_map, nonneg_int, strict_int

_CLAUSE_RE = re.compile(r"[.;!\n]+")
_ROOM_LIST_RE = re.compile(r"\brooms?\"?\s*[#=:]?\s*(\d+(?:\s*(?:-|,|/|and|&)\s*\d+)*)", re.I)
_BOX_SINGLE_RE = re.compile(r"\bbox(?:es)?\"?\s*[#=:]?\s*(\d+)\b", re.I)
_EMPTY_RE = re.compile(
    r"\bempty\b|flag\"?\s*[=:]?\s*false|\bfalse\b|no\s*flags?|noflag|"
    r"0\s+flags?|flags?\"?\s*[=:]?\s*0\b|has\s+0\b|not\s+(?:a\s+)?treasure",
    re.I,
)
_CONDITIONAL_EMPTY_RE = re.compile(
    r"\b(?:if|whether|unless|suppose|assuming|except)\b|\?",
    re.I,
)
_FLAG_POS_RE = re.compile(r"flag\"?\s*[=:]?\s*true|\btrue\b|found\s+(?:a\s+)?flag|\btreasure\b", re.I)
_DISCOVERY_KINDS = frozenset({"open", "probe", "scan"})
_REPORT_KINDS = frozenset({"msg", "notice"})


def rule_batch_blind_act(
    events: list[dict[str, Any]],
    bundle: TrialBundle,
    step_join: Mapping[str, Any],
) -> DetectorHit:
    _ = (events, bundle)
    batch_steps = same_ra_steps = max_acts = total_hits = 0
    hit_seats: list[str] = []
    for s_raw in as_list(step_join.get("seats")):
        s_map = as_map(s_raw)
        if s_map.get("joined") is not True or s_map.get("under_test", True) is False:
            continue
        seat = str(s_map.get("seat") or "")
        seat_fired = False
        for st_raw in as_list(s_map.get("steps")):
            st = as_map(st_raw)
            if st.get("source") != "agent":
                continue
            acts = nonneg_int(st.get("act_count")) or 0
            same_ra = st.get("same_step_read_act") is True
            max_acts = max(max_acts, acts)
            if acts >= 2 or same_ra:
                total_hits += 1
                batch_steps += int(acts >= 2)
                same_ra_steps += int(same_ra)
                seat_fired = True
        if seat_fired and seat:
            hit_seats.append(seat)
    return _hit(
        "batch_blind_act",
        total_hits,
        hit_seats,
        {
            "batch_act_steps": batch_steps,
            "same_step_read_act_steps": same_ra_steps,
            "max_acts_in_step": max_acts,
        },
    )


def _rooms_in_clause(clause: str) -> set[int]:
    rooms: set[int] = set()
    for match in _ROOM_LIST_RE.finditer(clause):
        for tok in re.split(r"\s*(?:,|/|and|&)\s*", match.group(1)):
            nums = [int(n) for n in re.findall(r"\d+", tok)[:4]]
            if len(nums) == 2 and 0 < nums[0] < nums[1] and nums[1] - nums[0] <= 32:
                rooms.update(range(nums[0], nums[1] + 1))
            else:
                rooms.update(n for n in nums if n > 0)
    return rooms


def _parse_msg_room_signals(
    text: str,
    room_size: int | None,
) -> tuple[set[int], list[tuple[int, int]]]:
    empty_rooms: set[int] = set()
    pos_flags: list[tuple[int, int]] = []
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            jmap = as_map(json.loads(stripped))
        except ValueError:
            jmap = {}
        j_room = strict_int(jmap.get("room"))
        j_box = strict_int(jmap.get("box"))
        if j_room is None and j_box is not None and j_box >= 1 and room_size and room_size > 0:
            j_room = (j_box - 1) // room_size + 1
        if j_room is not None and j_room >= 1:
            if jmap.get("empty") is True or jmap.get("flags") == 0 or jmap.get("flags_in_room") == 0:
                empty_rooms.add(j_room)
            elif jmap.get("flag") is False and "flags_in_room" not in jmap:
                empty_rooms.add(j_room)
            elif jmap.get("flag") is True and j_box is not None:
                pos_flags.append((j_room, j_box))
    for clause in _CLAUSE_RE.split(text)[:64]:
        c_rooms = _rooms_in_clause(clause)
        c_boxes = [int(m.group(1)) for m in _BOX_SINGLE_RE.finditer(clause)]
        if room_size and room_size > 0:
            c_rooms |= {(b - 1) // room_size + 1 for b in c_boxes if b >= 1}
        if _EMPTY_RE.search(clause) and not _CONDITIONAL_EMPTY_RE.search(clause):
            empty_rooms |= c_rooms
        elif _FLAG_POS_RE.search(clause) and c_boxes:
            for bx in c_boxes[:8]:
                rm = (bx - 1) // room_size + 1 if (room_size and room_size > 0) else (next(iter(c_rooms)) if len(c_rooms) == 1 else None)
                if rm is not None and rm >= 1:
                    pos_flags.append((rm, bx))
    return empty_rooms, pos_flags


def _scan_room_world_meta(events: list[dict[str, Any]]) -> tuple[bool, int | None, int | None, set[int]]:
    room_size: int | None = None
    flags_per_room: int | None = None
    flag_rooms: set[int] = set()
    has_rooms = False
    for row in events:
        if row.get("kind") == "admin.close" and row.get("ok"):
            break
        if not row.get("ok"):
            continue
        req = as_map(row.get("request"))
        resp = as_map(row.get("response"))
        rs = nonneg_int(resp.get("room_size"))
        if rs is not None and rs > 0:
            room_size = rs
            has_rooms = True
        fpr = nonneg_int(resp.get("flags_per_room"))
        if fpr is None:
            fpr = nonneg_int(resp.get("room_flags"))
        if fpr is not None and fpr > 0:
            flags_per_room = fpr
        if row.get("kind") == "open":
            bx = strict_int(resp.get("box")) or strict_int(req.get("box"))
            rm = strict_int(resp.get("room")) or strict_int(req.get("room"))
            if rm is None and room_size and bx is not None and bx >= 1:
                rm = (bx - 1) // room_size + 1
            if rm is not None and rm >= 1:
                has_rooms = True
                if resp.get("flag") is True:
                    flag_rooms.add(rm)
    return has_rooms, room_size, flags_per_room, flag_rooms


def _mark_room_empty(
    empty_at: dict[tuple[str, int], tuple[int, int | None]],
    seat: str,
    room: int,
    seq: int,
    step: int | None,
) -> None:
    cur = empty_at.get((seat, room))
    if cur is None or seq < cur[0]:
        empty_at[(seat, room)] = (seq, step)


def _record_room_box_obs(
    seat: str,
    room: int,
    box: int | None,
    flag: bool | None,
    seq: int,
    step: int | None,
    cap: int | None,
    rsize: int | None,
    known_flags: dict[tuple[str, int], dict[int, tuple[int, int | None]]],
    known_opens: dict[tuple[str, int], dict[int, tuple[int, int | None]]],
    empty_at: dict[tuple[str, int], tuple[int, int | None]],
) -> None:
    if box is not None:
        op_map = known_opens.setdefault((seat, room), {})
        op_map.setdefault(box, (seq, step))
        if rsize is not None and rsize > 0 and len(op_map) >= rsize:
            _mark_room_empty(empty_at, seat, room, seq, step)
        if flag is True:
            fl_map = known_flags.setdefault((seat, room), {})
            fl_map.setdefault(box, (seq, step))
            if cap is not None and cap > 0 and len(fl_map) >= cap:
                _mark_room_empty(empty_at, seat, room, seq, step)


def _apply_text_room_signals(
    text: str,
    seat: str,
    seq: int,
    st_idx: int | None,
    include_pos: bool,
    global_rsize: int | None,
    global_cap: int | None,
    true_flag_rooms: set[int],
    room_caps: Mapping[int, int],
    room_sizes: Mapping[int, int],
    known_flags: dict[tuple[str, int], dict[int, tuple[int, int | None]]],
    known_opens: dict[tuple[str, int], dict[int, tuple[int, int | None]]],
    empty_at: dict[tuple[str, int], tuple[int, int | None]],
) -> None:
    emp_set, pos_list = _parse_msg_room_signals(text, global_rsize)
    for rm in emp_set:
        if global_cap is not None or room_caps.get(rm) is not None or rm not in true_flag_rooms:
            _mark_room_empty(empty_at, seat, rm, seq, st_idx)
    if include_pos:
        for rm, bx in pos_list:
            _record_room_box_obs(
                seat, rm, bx, True, seq, st_idx, room_caps.get(rm, global_cap),
                room_sizes.get(rm, global_rsize), known_flags, known_opens, empty_at,
            )


def rule_empty_room_open(
    events: list[dict[str, Any]],
    bundle: TrialBundle,
    *,
    step_join: Mapping[str, Any] | None = None,
) -> DetectorHit:
    if bundle.ecosystem != "olam":
        return _hit("empty_room_open", 0, [], {})
    has_rooms, global_rsize, global_cap, true_flag_rooms = _scan_room_world_meta(events)
    if not has_rooms:
        return _hit("empty_room_open", 0, [], {})
    seat_steps = joined_event_steps(step_join)
    empty_at: dict[tuple[str, int], tuple[int, int | None]] = {}
    known_flags: dict[tuple[str, int], dict[int, tuple[int, int | None]]] = {}
    known_opens: dict[tuple[str, int], dict[int, tuple[int, int | None]]] = {}
    room_caps: dict[int, int] = {}
    room_sizes: dict[int, int] = {}
    hits = 0
    hit_seats: list[str] = []
    for idx, row in enumerate(events, start=1):
        if row.get("kind") == "admin.close" and row.get("ok"):
            break
        seat = _row_seat(row, bundle)
        if seat is None or not row.get("ok"):
            continue
        seq = strict_int(row.get("seq")) or idx
        st_idx = seat_steps.get(seat, {}).get(seq)
        kind, method = str(row.get("kind") or ""), str(row.get("method") or "")
        req, resp = as_map(row.get("request")), as_map(row.get("response"))
        if method == "GET" and kind in ("msgs", "new", "dms", "notices"):
            for lkey in ("messages", "dms", "notices"):
                for item in as_list(resp.get(lkey)):
                    _apply_text_room_signals(str(as_map(item).get("text") or ""), seat, seq, st_idx, True, global_rsize, global_cap, true_flag_rooms, room_caps, room_sizes, known_flags, known_opens, empty_at)
        elif method == "POST" and kind in ("msg", "notice"):
            _apply_text_room_signals(str(req.get("text") or resp.get("text") or ""), seat, seq, st_idx, False, global_rsize, global_cap, true_flag_rooms, room_caps, room_sizes, known_flags, known_opens, empty_at)
        elif method == "POST" and kind == "open":
            bx = strict_int(resp.get("box")) or strict_int(req.get("box"))
            rm = strict_int(resp.get("room")) or strict_int(req.get("room"))
            r_sz = nonneg_int(resp.get("room_size")) or global_rsize
            rm = ((bx - 1) // r_sz + 1) if (rm is None and r_sz and bx is not None and bx >= 1) else rm
            if rm is None or rm < 1:
                continue
            if r_sz and r_sz > 0:
                room_sizes[rm] = r_sz
            if (fir := nonneg_int(resp.get("flags_in_room"))) is not None:
                room_caps[rm] = fir
            prior = empty_at.get((seat, rm))
            if prior is not None and ((prior[1] < st_idx) if (st_idx is not None and prior[1] is not None) else (prior[0] < seq)):
                hits += 1
                hit_seats.append(seat)
            cap = room_caps.get(rm, global_cap)
            flg = resp.get("flag") if isinstance(resp.get("flag"), bool) else None
            if fir == 0 or resp.get("remaining_flags") == 0 or resp.get("empty") is True or resp.get("room_empty") is True or (flg is False and cap is None):
                _mark_room_empty(empty_at, seat, rm, seq, st_idx)
            _record_room_box_obs(seat, rm, bx, flg, seq, st_idx, cap, room_sizes.get(rm, global_rsize), known_flags, known_opens, empty_at)
    return _hit("empty_room_open", hits, hit_seats, {"empty_room_opens": hits})


def rule_slow_report(
    events: list[dict[str, Any]],
    bundle: TrialBundle,
    step_join: Mapping[str, Any] | None = None,
) -> DetectorHit:
    if bundle.ecosystem != "olam":
        return _hit("slow_report", 0, [], {})
    all_seats = {s.seat for s in bundle.seats if s.seat}
    channel_disabled = False
    for row in events:
        if row.get("kind") == "admin.close" and row.get("ok"):
            break
        s = _row_seat(row, bundle)
        if s:
            all_seats.add(s)
        if row.get("ok") and as_map(row.get("response")).get("channel") is False:
            channel_disabled = True
    if len(all_seats) < 2 or channel_disabled:
        return _hit("slow_report", 0, [], {})
    under_test = {s.seat for s in bundle.seats if s.under_test} or all_seats
    seat_steps = joined_event_steps(step_join)
    streaks: dict[str, tuple[int | None, int, set[int]]] = {}
    hits = 0
    hit_seats: list[str] = []
    for idx, row in enumerate(events, start=1):
        if row.get("kind") == "admin.close" and row.get("ok"):
            break
        seat = _row_seat(row, bundle)
        if seat is None or seat not in under_test or not row.get("ok"):
            continue
        if row.get("method") != "POST":
            continue
        seq = strict_int(row.get("seq")) or idx
        kind = str(row.get("kind") or "")
        st_idx = seat_steps.get(seat, {}).get(seq)
        if kind in _DISCOVERY_KINDS:
            cur = streaks.get(seat)
            if cur is None:
                streaks[seat] = (st_idx, 0, set())
            else:
                first_st, f_acts, f_steps = cur
                if first_st is not None and st_idx is not None and st_idx > first_st:
                    f_steps.add(st_idx)
                streaks[seat] = (first_st, f_acts + 1, f_steps)
        elif kind in _REPORT_KINDS:
            cur = streaks.pop(seat, None)
            if cur is not None and (cur[1] >= 3 or len(cur[2]) >= 2):
                hits += 1
                hit_seats.append(seat)
    for seat, cur in streaks.items():
        if cur[1] >= 3 or len(cur[2]) >= 2:
            hits += 1
            hit_seats.append(seat)
    return _hit("slow_report", hits, hit_seats, {"slow_streaks": hits})
