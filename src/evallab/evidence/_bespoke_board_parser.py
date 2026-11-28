from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any

from ._multiagent_io import (
    as_nonneg_int,
    as_str,
    epoch_to_iso_utc,
    safe_finite_float,
    sanitize_json_value,
    sanitize_utf8_text,
)

_BROADCAST_TO = frozenset({"everyone", "all", "*"})
_MAX_INFERRED_FETCH = 512
_MAX_TOTAL_INFERRED_SCANS = 200_000


@dataclass
class _BoardInboxState:
    posted: list[tuple[int, str, str | None, list[str], str | None]] = field(default_factory=list)
    posted_seqs: list[int] = field(default_factory=list)
    posted_monotonic: bool = True
    inbox_cursor: dict[str, int] = field(default_factory=dict)
    inbox_scan_idx: dict[str, int] = field(default_factory=dict)
    inbox_seen_ids: dict[str, set[str]] = field(default_factory=dict)
    inferred_scans: int = 0
    last_msg_seq: int = 0


@dataclass
class _BoardRowKind:
    actor: str | None
    authenticated: bool | None
    kind: str
    target: str | None
    msg_id: str | None = None
    recipients: list[str] = field(default_factory=list)
    visibility: str = "world_audit"
    accepted: bool = True
    phase: str = "play"
    fetched: list[str] = field(default_factory=list)
    text: str | None = None


def _canon(name: str | None, alias_map: dict[str, str]) -> str | None:
    return alias_map.get(name, name) if name is not None else None


def _handle_board_post(
    ev_name: str,
    rec: dict[str, Any],
    actor: str | None,
    authenticated: bool | None,
    channel: str | None,
    detail: dict[str, Any],
    alias_map: dict[str, str],
    state: _BoardInboxState,
) -> _BoardRowKind:
    if ev_name == "notify":
        detail["is_harness_notification"] = True
        if actor is None:
            actor, authenticated = "@harness", True
    raw_to = rec.get("to")
    recipients: list[str] = []
    if isinstance(raw_to, str) and raw_to.strip():
        clean_to = sanitize_utf8_text(raw_to.strip())
        if clean_to.lower() in _BROADCAST_TO:
            visibility = "public"
        else:
            recipients, visibility = [_canon(clean_to, alias_map) or clean_to], "direct"
    elif isinstance(raw_to, list):
        raw_items = [
            sanitize_utf8_text(x.strip()) for x in raw_to if isinstance(x, str) and x.strip()
        ]
        if any(x.lower() in _BROADCAST_TO for x in raw_items):
            visibility = "public"
        else:
            recipients = [_canon(x, alias_map) or x for x in raw_items]
            visibility = "multi" if len(recipients) > 1 else ("direct" if recipients else "public")
    else:
        visibility = "public"

    raw_mseq = rec.get("seq")
    if type(raw_mseq) is int and raw_mseq >= 1:
        mseq_int = raw_mseq
        state.last_msg_seq = max(state.last_msg_seq, mseq_int)
    else:
        state.last_msg_seq += 1
        mseq_int = state.last_msg_seq
    msg_id = str(mseq_int)
    target = (
        f"channel:{channel}"
        if channel
        else ("broadcast" if visibility == "public" else f"dm:{','.join(recipients)}")
    )
    if state.posted_seqs and mseq_int < state.posted_seqs[-1]:
        state.posted_monotonic = False
    state.posted.append((mseq_int, msg_id, actor, recipients, channel))
    state.posted_seqs.append(mseq_int)
    return _BoardRowKind(
        actor=actor,
        authenticated=authenticated,
        kind="message_post",
        target=target,
        msg_id=msg_id,
        recipients=recipients,
        visibility=visibility,
        text=as_str(rec.get("text")),
    )


def _infer_board_inbox_seqs(actor: str, returned: int, state: _BoardInboxState) -> list[str]:
    cur = state.inbox_cursor.get(actor, 0)
    seen = state.inbox_seen_ids.setdefault(actor, set())
    start_i = (
        max(bisect_right(state.posted_seqs, cur), state.inbox_scan_idx.get(actor, 0))
        if state.posted_monotonic
        else state.inbox_scan_idx.get(actor, 0)
    )
    matched: list[tuple[int, str]] = []
    last_scanned = start_i
    for idx_p in range(start_i, len(state.posted)):
        if state.inferred_scans >= _MAX_TOTAL_INFERRED_SCANS:
            break
        state.inferred_scans += 1
        last_scanned = idx_p + 1
        ms, mid, author, recips, _ = state.posted[idx_p]
        fresh = (ms > cur and mid not in seen) if state.posted_monotonic else (mid not in seen)
        if fresh and author != actor and (not recips or actor in recips):
            matched.append((ms, mid))
            if len(matched) >= returned:
                break
    state.inbox_scan_idx[actor] = max(state.inbox_scan_idx.get(actor, 0), last_scanned)
    fetched = [mid for _, mid in matched]
    seen.update(fetched)
    if matched:
        state.inbox_cursor[actor] = max(cur, max(ms for ms, _ in matched))
    return fetched


def _handle_board_inbox(
    rec: dict[str, Any], actor: str | None, detail: dict[str, Any], state: _BoardInboxState
) -> list[str]:
    if (ws := safe_finite_float(rec.get("wait_seconds"))) is not None:
        detail["wait_seconds"] = ws
    raw_seqs = rec.get("seqs")
    if isinstance(raw_seqs, list):
        fetched: list[str] = []
        int_seqs: list[int] = []
        seen_add: list[str] = []
        for s in raw_seqs:
            if type(s) is int:
                sid = str(s)
                fetched.append(sid)
                seen_add.append(sid)
                if s >= 0:
                    int_seqs.append(s)
            elif isinstance(s, str) and s.strip():
                clean_s = sanitize_utf8_text(s.strip())
                if clean_s.isascii() and clean_s.isdigit() and len(clean_s) <= 18:
                    iv = int(clean_s)
                    int_seqs.append(iv)
                    canon_s = str(iv)
                    fetched.append(canon_s)
                    seen_add.extend((clean_s, canon_s))
                else:
                    fetched.append(clean_s)
                    seen_add.append(clean_s)
        if actor:
            state.inbox_seen_ids.setdefault(actor, set()).update(seen_add)
            if int_seqs:
                state.inbox_cursor[actor] = max(state.inbox_cursor.get(actor, 0), max(int_seqs))
        return fetched
    returned = min(as_nonneg_int(rec.get("returned")) or 0, _MAX_INFERRED_FETCH)
    if actor and returned > 0 and state.inferred_scans < _MAX_TOTAL_INFERRED_SCANS:
        return _infer_board_inbox_seqs(actor, returned, state)
    return []


def _handle_board_history(
    rec: dict[str, Any], actor: str | None, channel: str | None, state: _BoardInboxState
) -> tuple[str, list[str]]:
    target = f"channel:{channel}" if channel else "history"
    raw_seqs = rec.get("seqs")
    if isinstance(raw_seqs, list):
        fetched = [
            sanitize_utf8_text(str(s)) for s in raw_seqs if type(s) is int or isinstance(s, str)
        ]
        return target, fetched
    after = as_nonneg_int(rec.get("after")) or 0
    raw_ret = as_nonneg_int(rec.get("returned"))
    cap = min(raw_ret, _MAX_INFERRED_FETCH) if raw_ret is not None else _MAX_INFERRED_FETCH
    matched_ids: list[str] = []
    if cap > 0 and state.inferred_scans < _MAX_TOTAL_INFERRED_SCANS:
        start_i = bisect_right(state.posted_seqs, after) if state.posted_monotonic else 0
        for idx_p in range(start_i, len(state.posted)):
            if state.inferred_scans >= _MAX_TOTAL_INFERRED_SCANS:
                break
            state.inferred_scans += 1
            ms, mid, author, recips, ch = state.posted[idx_p]
            if (
                ms > after
                and (not recips or (actor is not None and (actor in recips or author == actor)))
                and (channel is None or ch == channel)
            ):
                matched_ids.append(mid)
                if len(matched_ids) >= cap:
                    break
    return target, matched_ids


def _handle_board_lifecycle(
    ev_name: str,
    rec: dict[str, Any],
    actor: str | None,
    authenticated: bool | None,
    detail: dict[str, Any],
    alias_map: dict[str, str],
) -> _BoardRowKind:
    target = as_str(rec.get("prefix")) or ev_name
    accepted = True
    phase = "play"
    fetched: list[str] = []
    if ev_name == "config":
        if policy := as_str(rec.get("policy")):
            detail["policy"] = policy
        if isinstance(rec.get("plugins"), list):
            detail["plugins"] = sanitize_json_value(list(rec["plugins"]))
    if ev_name in ("config", "plugins", "register"):
        phase = "setup"
        authenticated = None if ev_name in ("config", "plugins") else True
    if ev_name == "subagent":
        op = as_str(rec.get("op")) or "subagent"
        target = _canon(as_str(rec.get("name")), alias_map) or op
        accepted = op != "slot_lost"
        if op == "wake" and isinstance(rec.get("seqs"), list):
            fetched = [
                sanitize_utf8_text(str(s))
                for s in rec["seqs"]
                if type(s) is int or isinstance(s, str)
            ]
    return _BoardRowKind(
        actor=actor,
        authenticated=authenticated,
        kind="lifecycle",
        target=target,
        visibility="lifecycle",
        accepted=accepted,
        phase=phase,
        fetched=fetched,
    )


def _classify_board_event(
    ev_name: str,
    rec: dict[str, Any],
    actor: str | None,
    channel: str | None,
    detail: dict[str, Any],
    alias_map: dict[str, str],
    state: _BoardInboxState,
) -> _BoardRowKind:
    authenticated: bool | None = actor is not None
    if ev_name in ("send", "post", "notify"):
        return _handle_board_post(
            ev_name, rec, actor, authenticated, channel, detail, alias_map, state
        )
    if ev_name == "inbox":
        return _BoardRowKind(
            actor=actor,
            authenticated=authenticated,
            kind="message_fetch",
            target="inbox",
            fetched=_handle_board_inbox(rec, actor, detail, state),
        )
    if ev_name in ("history", "read"):
        target, fetched = _handle_board_history(rec, actor, channel, state)
        return _BoardRowKind(
            actor=actor,
            authenticated=authenticated,
            kind="message_fetch",
            target=target,
            fetched=fetched,
        )
    if ev_name in ("refused", "rate_limited", "guard_unread_block", "guard_claim_denied"):
        if reason := as_str(rec.get("reason")):
            detail["reason"] = reason
        if action := as_str(rec.get("action")):
            detail["action"] = action
        target = (
            as_str(rec.get("action"))
            or as_str(rec.get("prefix"))
            or as_str(rec.get("target"))
            or ev_name
        )
        return _BoardRowKind(
            actor=actor,
            authenticated=authenticated,
            kind="policy_refusal",
            target=target,
            accepted=False,
        )
    if ev_name in ("config", "plugins", "register", "subagent", "guard_unread_unblock") or (
        ev_name == "guard_claim_released" and as_str(rec.get("reason")) == "expired"
    ):
        return _handle_board_lifecycle(ev_name, rec, actor, authenticated, detail, alias_map)
    return _BoardRowKind(
        actor=actor,
        authenticated=authenticated,
        kind="world_action",
        target=as_str(rec.get("prefix")) or ev_name,
    )


def parse_bespoke_board_event_dicts(
    rows: list[dict[str, Any]],
    *,
    member_aliases: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    state = _BoardInboxState()
    alias_map = member_aliases or {}
    for idx, rec in enumerate(rows, start=1):
        ev_name = as_str(rec.get("event")) or as_str(rec.get("type")) or ""
        src_fmt = "bespoke_board_legacy" if ev_name in ("post", "read") else "bespoke_board_v2"
        raw_ts = rec.get("ts") if rec.get("ts") is not None else rec.get("timestamp")
        ts_iso, ts_epoch = epoch_to_iso_utc(raw_ts)
        raw_actor = as_str(rec.get("agent"))
        if raw_actor is None and src_fmt == "bespoke_board_legacy":
            raw_actor = as_str(rec.get("from"))
        actor = _canon(raw_actor, alias_map)
        detail: dict[str, Any] = {}
        if raw_actor and actor and raw_actor != actor:
            detail["facing_actor"] = raw_actor
        rk = _classify_board_event(
            ev_name, rec, actor, as_str(rec.get("channel")), detail, alias_map, state
        )
        raw_claimed = as_str(rec.get("from")) or as_str(rec.get("author"))
        events.append(
            {
                "seq": idx,
                "ts": ts_iso,
                "ts_epoch": ts_epoch,
                "actor": rk.actor,
                "kind": rk.kind,
                "target": rk.target,
                "msg_id": rk.msg_id,
                "recipients": rk.recipients,
                "visibility": rk.visibility,
                "accepted": rk.accepted,
                "phase": rk.phase,
                "source_format": src_fmt,
                "raw_kind": ev_name,
                "fetched_msg_ids": rk.fetched,
                "text": rk.text,
                "request_id": as_str(rec.get("request_id")) or as_str(rec.get("call_id")),
                "authenticated": rk.authenticated,
                "claimed_actor": _canon(raw_claimed, alias_map),
                "detail": detail,
            }
        )
    return events
