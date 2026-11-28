from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ._crucible_world_parser import parse_crucible_rows
from ._multiagent_io import (
    as_dict,
    as_str,
    epoch_to_iso_utc,
    sanitize_json_value,
    sanitize_utf8_text,
)
from ._olam_muster_detail import (
    build_muster_event_detail,
    muster_target,
    resolve_muster_fetched_ids,
    verify_muster_hash_chain,
)

__all__ = ["parse_crucible_rows", "parse_muster_world_rows"]

_BROADCAST_TO = frozenset({"everyone", "all", "*"})


@dataclass
class _MusterRowKind:
    kind: str
    target: str | None
    msg_id: str | None = None
    recipients: list[str] = field(default_factory=list)
    visibility: str = "world_audit"
    accepted: bool = True
    phase: str = "play"
    authenticated: bool | None = None
    fetched: list[str] = field(default_factory=list)
    text: str | None = None
    action_sig: str | None = None
    just_closed: bool = False


def _muster_msg_routing(to: object) -> tuple[list[str], str, str]:
    if isinstance(to, str) and to.strip():
        clean_to = sanitize_utf8_text(to.strip())
        if clean_to.lower() in _BROADCAST_TO:
            return [], "public", "channel:public"
        return [clean_to], "direct", f"dm:{clean_to}"
    if isinstance(to, list):
        raw_to_l = [sanitize_utf8_text(x.strip()) for x in to if isinstance(x, str) and x.strip()]
        if any(x.lower() in _BROADCAST_TO for x in raw_to_l):
            return [], "public", "channel:public"
        visibility = "multi" if len(raw_to_l) > 1 else ("direct" if raw_to_l else "public")
        target = f"dm:{','.join(raw_to_l)}" if raw_to_l else "channel:public"
        return raw_to_l, visibility, target
    return [], "public", "channel:public"


def _classify_muster_row(
    seq: int,
    actor: str | None,
    raw_kind: str,
    method: str,
    status_code: int | None,
    *,
    ok: bool,
    closed: bool,
    late: bool,
    raw_request: object,
    request: dict[str, Any],
    response: dict[str, Any],
    posted_ids: list[tuple[int, str]],
) -> _MusterRowKind:
    res = _MusterRowKind(
        kind="world_read",
        target=muster_target(raw_kind, request),
        accepted=ok,
        phase="post_play" if (closed or late) else "play",
        authenticated=actor is not None,
    )
    if raw_kind == "admin.close":
        if ok:
            res.kind, res.visibility, res.phase, res.target = (
                "lifecycle",
                "lifecycle",
                "lifecycle",
                "admin.close",
            )
            res.just_closed = not closed
        else:
            res.kind = "auth_violation" if status_code == 401 else "policy_refusal"
            if status_code == 401:
                res.authenticated = False
    elif status_code == 401:
        res.kind, res.accepted, res.authenticated, res.target = (
            "auth_violation",
            False,
            False,
            raw_kind,
        )
    elif status_code == 404:
        res.kind, res.accepted, res.target = "probe", False, raw_kind
    elif not ok:
        res.kind, res.accepted = "policy_refusal", False
    elif method == "POST" and raw_kind == "msg":
        res.kind = "message_post"
        resp_id = response.get("id")
        res.msg_id = sanitize_utf8_text(str(resp_id)) if resp_id is not None else str(seq)
        mnum = resp_id if type(resp_id) is int and resp_id >= 1 else len(posted_ids) + 1
        posted_ids.append((mnum, res.msg_id))
        res.text = as_str(request.get("text")) or as_str(response.get("text"))
        res.recipients, res.visibility, res.target = _muster_msg_routing(request.get("to"))
    elif method == "GET" and raw_kind == "msgs":
        res.kind, res.target = "message_fetch", "msgs"
        res.fetched = resolve_muster_fetched_ids(response, posted_ids)
    elif method == "POST":
        res.kind = "world_action"
        res.action_sig = sanitize_utf8_text(
            f"{raw_kind} {json.dumps(sanitize_json_value(raw_request), sort_keys=True)}"
        )
    else:
        res.kind, res.target = "world_read", raw_kind
    return res


def _muster_event_dict(
    seq: int,
    ts_iso: str | None,
    ts_epoch: float | None,
    actor: str | None,
    raw_kind: str,
    status_code: int | None,
    late: bool,
    rec: dict[str, Any],
    request: dict[str, Any],
    row_kind: _MusterRowKind,
    detail: dict[str, Any],
) -> dict[str, Any]:
    return {
        "seq": seq,
        "ts": ts_iso,
        "ts_epoch": ts_epoch,
        "actor": actor,
        "kind": row_kind.kind,
        "target": row_kind.target,
        "msg_id": row_kind.msg_id,
        "recipients": row_kind.recipients,
        "visibility": row_kind.visibility,
        "accepted": row_kind.accepted,
        "phase": row_kind.phase,
        "source_format": "olam_muster_world",
        "raw_kind": raw_kind,
        "fetched_msg_ids": row_kind.fetched,
        "text": row_kind.text,
        "action_signature": row_kind.action_sig,
        "request_id": as_str(request.get("request_id"))
        or as_str(request.get("call_id"))
        or as_str(rec.get("request_id")),
        "authenticated": row_kind.authenticated,
        "claimed_actor": as_str(request.get("seat"))
        or as_str(request.get("from"))
        or as_str(request.get("author")),
        "status_code": status_code,
        "late": late,
        "detail": detail,
    }


def parse_muster_world_rows(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int | None, str | None, float | None]:
    events: list[dict[str, Any]] = []
    chain_flags = verify_muster_hash_chain(rows)
    posted_ids: list[tuple[int, str]] = []
    closed = False
    close_seq: int | None = None
    close_ts: str | None = None
    close_epoch: float | None = None
    last_seq = 0
    for idx, rec in enumerate(rows, start=1):
        raw_seq = rec.get("seq")
        seq = raw_seq if type(raw_seq) is int and raw_seq >= 1 else (last_seq + 1)
        last_seq = max(last_seq, seq)
        ts_iso, ts_epoch = epoch_to_iso_utc(
            rec.get("t") if rec.get("t") is not None else rec.get("ts")
        )
        actor = as_str(rec.get("seat"))
        raw_kind = as_str(rec.get("kind")) or ""
        method = (as_str(rec.get("method")) or "").upper()
        raw_status = rec.get("status")
        status_code = raw_status if type(raw_status) is int else None
        ok = rec.get("ok") is True and (status_code is None or status_code < 400)
        raw_request = rec.get("request")
        request = as_dict(raw_request)
        response = as_dict(rec.get("response"))
        late = rec.get("late") is True
        rk = _classify_muster_row(
            seq,
            actor,
            raw_kind,
            method,
            status_code,
            ok=ok,
            closed=closed,
            late=late,
            raw_request=raw_request,
            request=request,
            response=response,
            posted_ids=posted_ids,
        )
        if rk.just_closed:
            closed = True
            close_seq, close_ts, close_epoch = seq, ts_iso, ts_epoch
        detail = build_muster_event_detail(
            raw_kind=raw_kind,
            method=method,
            ok=ok,
            status_code=status_code,
            request=request,
            response=response,
        )
        if chain_flags is not None and idx - 1 < len(chain_flags):
            detail["hash_chain_valid"] = chain_flags[idx - 1]
        events.append(
            _muster_event_dict(
                seq, ts_iso, ts_epoch, actor, raw_kind, status_code, late, rec, request, rk, detail
            )
        )
    return events, close_seq, close_ts, close_epoch
