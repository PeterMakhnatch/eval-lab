from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ._multiagent_io import (
    as_dict,
    as_str,
    epoch_to_iso_utc,
    sanitize_json_value,
    sanitize_utf8_text,
)

_CRUCIBLE_PRIMARY_CHANNELS: dict[str, str] = {
    "chat_public": "public",
    "message_public": "public",
    "chat_post": "public",
    "chat_message": "public",
    "board_post": "board",
    "scout_report": "round_reports",
    "round_report": "round_reports",
    "plan_post": "plan",
    "chat_dm": "dm",
    "message_dm": "dm",
}
_CRUCIBLE_MESSAGE_POST_TYPES = frozenset(_CRUCIBLE_PRIMARY_CHANNELS)
_CRUCIBLE_ALIAS_CHANNELS: dict[str, tuple[str, ...]] = {
    "scout_report": ("reports",),
    "round_report": ("reports",),
}
_CRUCIBLE_ARTIFACT_WRITE_TYPES = frozenset({"workspace_commit", "repo_commit", "plan_write"})
_CRUCIBLE_WORLD_READ_TYPES = frozenset(
    {
        "workspace_read",
        "workspace_list",
        "plan_read",
        "submission_read",
        "dossier_read",
        "test_read",
        "observation_read",
        "scan",
    }
)
_CRUCIBLE_LIFECYCLE_TYPES = frozenset({"world_started", "world_ready", "done", "seat_done"})
_CRUCIBLE_INTERNAL_TYPES = frozenset(
    {
        "observation_truth",
        "dossier_truth",
        "fault_injected",
        "script_scan",
        "mechanism_detection",
        "camouflage_healthcheck",
        "server_error",
        "service_deploy_requested",
    }
)
_CRUCIBLE_REFUSAL_TYPES = frozenset({"request_rejected", "strict_gate_reject"})
_FILE_HEADER_RE = re.compile(r"^#\s*file:\s*(\S+)", re.IGNORECASE)
_DM_SPLIT_RE = re.compile(r"[,:]")
_SYSTEM_ACTORS = frozenset({"world", "system", "@harness", "harbor"})


@dataclass
class _CrucibleMsgIndex:
    posted_ch_by_seq: dict[str, set[str]] = field(default_factory=dict)
    posted_by_ch_mseq: dict[tuple[str, str], str] = field(default_factory=dict)
    posted_by_alias_mseq: dict[tuple[str, str], str] = field(default_factory=dict)
    posted_dm_by_actor_mseq: dict[tuple[str, str], str] = field(default_factory=dict)
    dm_participants_by_seq: dict[str, set[str]] = field(default_factory=dict)
    posted_by_mseq: dict[str, str] = field(default_factory=dict)


@dataclass
class _CrucibleRowKind:
    kind: str
    target: str | None
    visibility: str
    accepted: bool = True
    phase: str = "play"
    msg_id: str | None = None
    fetched: list[str] = field(default_factory=list)
    text: str | None = None
    files: list[str] = field(default_factory=list)
    commit_sha: str | None = None
    overwrites_actor: str | None = None
    status_code: int | None = None
    action_sig: str | None = None


def _crucible_files(raw_kind: str, payload: dict[str, Any]) -> list[str]:
    if raw_kind in ("plan_write", "plan_read"):
        return [as_str(payload.get("plan_file")) or as_str(payload.get("path")) or "plan.md"]
    raw_files = payload.get("files")
    if isinstance(raw_files, (list, dict)):
        found = sorted(
            {sanitize_utf8_text(p.strip()) for p in raw_files if isinstance(p, str) and p.strip()}
        )
        if found:
            return found
    if path := as_str(payload.get("path")):
        return [path]
    content = as_str(payload.get("content"))
    if content and (match := _FILE_HEADER_RE.match(content.splitlines()[0].strip())):
        return [match.group(1)]
    return []


def _crucible_message_text(payload: dict[str, Any]) -> str | None:
    if text := (as_str(payload.get("text")) or as_str(payload.get("note"))):
        return text
    if isinstance(payload.get("claim"), dict):
        return sanitize_utf8_text(json.dumps(sanitize_json_value(payload["claim"]), sort_keys=True))
    return None


def _canon_dm_channel(raw: str) -> str:
    if not raw.startswith("dm:"):
        return raw
    parts = sorted({p.strip() for p in _DM_SPLIT_RE.split(raw[3:]) if p.strip()})
    return f"dm:{','.join(parts)}" if parts else raw


def _dm_vis_ok(actor: str | None, cand: str, dm_participants_by_seq: dict[str, set[str]]) -> bool:
    return (
        actor is None or cand not in dm_participants_by_seq or actor in dm_participants_by_seq[cand]
    )


def _resolve_crucible_mseq_hit(
    sid: str,
    actor: str | None,
    ch_candidates: tuple[str, ...],
    channel_unscoped: bool,
    index: _CrucibleMsgIndex,
) -> str | None:
    for table in (index.posted_by_ch_mseq, index.posted_by_alias_mseq):
        for ch in ch_candidates:
            if ch == "dm":
                if actor is not None and (
                    dm_hit := index.posted_dm_by_actor_mseq.get((actor, sid))
                ):
                    return dm_hit
                cand = table.get(("dm", sid))
                if cand is not None and _dm_vis_ok(actor, cand, index.dm_participants_by_seq):
                    return cand
            elif (cand := table.get((ch, sid))) is not None and _dm_vis_ok(
                actor, cand, index.dm_participants_by_seq
            ):
                return cand
    if channel_unscoped:
        cand = index.posted_by_mseq.get(sid)
        if cand is not None and _dm_vis_ok(actor, cand, index.dm_participants_by_seq):
            return cand
        if actor is not None:
            return index.posted_dm_by_actor_mseq.get((actor, sid))
    return None


def _extract_crucible_recipients(
    rec: dict[str, Any], raw_kind: str, payload: dict[str, Any], *, is_dm_payload_ch: bool
) -> tuple[list[str], str, str]:
    raw_recips = rec.get("recipients")
    if isinstance(raw_recips, list):
        recipients = [
            sanitize_utf8_text(r.strip()) for r in raw_recips if isinstance(r, str) and r.strip()
        ]
    elif isinstance(raw_recips, str) and raw_recips.strip():
        recipients = [sanitize_utf8_text(raw_recips.strip())]
    else:
        recipients = []
    if not recipients and (raw_kind in ("chat_dm", "message_dm") or is_dm_payload_ch):
        raw_to = payload.get("to")
        if isinstance(raw_to, list):
            recipients = [
                sanitize_utf8_text(r.strip()) for r in raw_to if isinstance(r, str) and r.strip()
            ]
        elif to := as_str(raw_to):
            recipients = [to]
    raw_vis = as_str(rec.get("visibility")) or "world_audit"
    if raw_vis in ("public", "world_private"):
        visibility = raw_vis
    elif raw_vis.startswith("dm:"):
        visibility = "multi" if len(recipients) > 1 else "direct"
    else:
        visibility = "world_audit"
    return recipients, raw_vis, visibility


def _index_crucible_post(
    msg_id: str,
    raw_kind: str,
    visibility: str,
    explicit_post_ch: str | None,
    primary_chs: set[str],
    dm_parts: set[str],
    *,
    is_dm_post: bool,
    payload: dict[str, Any],
    index: _CrucibleMsgIndex,
) -> None:
    alias_chs: set[str] = set()
    if explicit_post_ch is None:
        alias_chs.update(_CRUCIBLE_ALIAS_CHANNELS.get(raw_kind, ()))
        if not is_dm_post and visibility == "public" and "public" not in primary_chs:
            alias_chs.add("public")
    if is_dm_post and "dm" not in primary_chs:
        alias_chs.add("dm")
    alias_chs -= primary_chs
    index.posted_ch_by_seq[msg_id] = primary_chs | alias_chs
    raw_mseq = payload.get("message_seq")
    if isinstance(raw_mseq, (int, str)) and not isinstance(raw_mseq, bool):
        mseq_s = sanitize_utf8_text(str(raw_mseq))
        for p_ch in primary_chs:
            index.posted_by_ch_mseq[(p_ch, mseq_s)] = msg_id
        for a_ch in alias_chs:
            index.posted_by_alias_mseq[(a_ch, mseq_s)] = msg_id
        for p in dm_parts:
            index.posted_dm_by_actor_mseq[(p, mseq_s)] = msg_id
        if not is_dm_post:
            index.posted_by_mseq[mseq_s] = msg_id


def _handle_crucible_post(
    seq: int,
    actor: str | None,
    raw_kind: str,
    raw_vis: str,
    visibility: str,
    recipients: list[str],
    explicit_post_ch: str | None,
    *,
    is_dm_payload_ch: bool,
    payload: dict[str, Any],
    index: _CrucibleMsgIndex,
) -> _CrucibleRowKind:
    msg_id = str(seq)
    text = _crucible_message_text(payload)
    is_dm_post = (
        raw_vis.startswith("dm:") or raw_kind in ("chat_dm", "message_dm") or is_dm_payload_ch
    )
    dm_parts: set[str] = set()
    if is_dm_post:
        if actor is not None:
            dm_parts.add(actor)
        dm_parts.update(recipients)
        for dm_src in (raw_vis, explicit_post_ch or ""):
            if dm_src.startswith("dm:"):
                dm_parts.update(p.strip() for p in _DM_SPLIT_RE.split(dm_src[3:]) if p.strip())
        if (
            recipients
            or raw_vis.startswith("dm:")
            or (explicit_post_ch is not None and explicit_post_ch.startswith("dm:"))
        ):
            index.dm_participants_by_seq[msg_id] = dm_parts
        visibility = "multi" if (len(recipients) > 1 or len(dm_parts) > 2) else "direct"
        ch_name = explicit_post_ch or (raw_vis if raw_vis.startswith("dm:") else "dm")
        primary_chs: set[str] = set()
        if raw_vis.startswith("dm:"):
            primary_chs.add(_canon_dm_channel(raw_vis))
        if explicit_post_ch is not None and explicit_post_ch != "dm":
            primary_chs.add(_canon_dm_channel(explicit_post_ch))
        if len(dm_parts) >= 2:
            primary_chs.add(f"dm:{','.join(sorted(dm_parts))}")
        if not primary_chs:
            primary_chs.add(explicit_post_ch or "dm")
    elif explicit_post_ch is not None:
        ch_name, primary_chs = explicit_post_ch, {explicit_post_ch}
    else:
        ch_name = _CRUCIBLE_PRIMARY_CHANNELS.get(raw_kind, visibility)
        primary_chs = {ch_name}
    _index_crucible_post(
        msg_id,
        raw_kind,
        visibility,
        explicit_post_ch,
        primary_chs,
        dm_parts,
        is_dm_post=is_dm_post,
        payload=payload,
        index=index,
    )
    return _CrucibleRowKind(
        kind="message_post",
        target=f"channel:{ch_name}",
        visibility=visibility,
        msg_id=msg_id,
        text=text,
    )


def _handle_crucible_read(
    actor: str | None,
    raw_vis: str,
    visibility: str,
    explicit_ch: str | None,
    payload: dict[str, Any],
    index: _CrucibleMsgIndex,
) -> _CrucibleRowKind:
    ch_name = explicit_ch or (raw_vis if raw_vis.startswith("dm:") else "messages")
    channel_unscoped = explicit_ch is None and not raw_vis.startswith("dm:")
    using_msg_seqs = payload.get("msg_seqs") is not None
    raw_seqs = payload.get("msg_seqs") if using_msg_seqs else payload.get("message_ids")
    c_vis = _canon_dm_channel(raw_vis) if raw_vis.startswith("dm:") else raw_vis
    c_exp = (
        _canon_dm_channel(explicit_ch)
        if explicit_ch is not None and explicit_ch.startswith("dm:")
        else explicit_ch
    )
    if raw_vis.startswith("dm:"):
        ch_candidates: tuple[str, ...] = (
            (c_vis, c_exp) if c_exp is not None and c_exp != c_vis and c_exp != "dm" else (c_vis,)
        )
    elif c_exp is not None:
        ch_candidates = (c_exp,)
    else:
        ch_candidates = (c_vis, ch_name, visibility)
    fetched: list[str] = []
    if isinstance(raw_seqs, list):
        for raw_id in raw_seqs:
            if not isinstance(raw_id, (int, str)) or isinstance(raw_id, bool):
                continue
            sid = sanitize_utf8_text(str(raw_id))
            mseq_hit = _resolve_crucible_mseq_hit(
                sid, actor, ch_candidates, channel_unscoped, index
            )
            seq_chs = index.posted_ch_by_seq.get(sid)
            seq_ok = (
                seq_chs is not None
                and _dm_vis_ok(actor, sid, index.dm_participants_by_seq)
                and (channel_unscoped or any(c in seq_chs for c in ch_candidates))
            )
            seq_hit = sid if seq_ok else None
            hit = (mseq_hit or seq_hit) if using_msg_seqs else (seq_hit or mseq_hit)
            if hit is not None:
                fetched.append(hit)
            elif channel_unscoped and sid not in index.posted_ch_by_seq:
                fetched.append(sid)
            else:
                fetched.append(f"{ch_name}:{sid}")
    return _CrucibleRowKind(
        kind="message_fetch", target=f"channel:{ch_name}", visibility=visibility, fetched=fetched
    )


def _classify_crucible_world_action(
    raw_kind: str, payload: dict[str, Any]
) -> tuple[bool, str, str]:
    accepted = payload.get("accepted") is not False
    target = raw_kind
    if raw_kind == "mine" and (sector := as_str(payload.get("sector"))):
        target = f"sector:{sector}"
    elif raw_kind == "service_deploy":
        target = f"service:{as_str(payload.get('entrypoint')) or 'default'}"
    elif raw_kind in ("decision", "round_decide") and payload.get("round") is not None:
        target = f"round:{sanitize_utf8_text(str(payload.get('round')))}"
    elif raw_kind in ("submit", "review_approve", "review_reject", "review_commit"):
        raw_sub = payload.get("submission_id")
        if raw_sub is None:
            raw_sub = payload.get("revision")
        sub = sanitize_utf8_text(str(raw_sub)) if raw_sub is not None else "default"
        target = f"submission:{sub}"
    elif raw_kind in ("transfer", "refine"):
        to_s = sanitize_utf8_text(str(payload.get("to") or ""))
        mat_s = sanitize_utf8_text(str(payload.get("material") or ""))
        target = f"{raw_kind}:{to_s}:{mat_s}"
    elif raw_kind == "accuse":
        target = f"suspect:{as_str(payload.get('suspect')) or 'unknown'}"
    elif raw_kind == "permission_revocation":
        target = f"seat:{as_str(payload.get('target_seat')) or 'unknown'}"
    elif raw_kind == "vote":
        target = f"choice:{as_str(payload.get('choice')) or 'unknown'}"
    return accepted, target, f"{raw_kind}:{target}"


def _classify_crucible_non_msg(
    raw_kind: str, visibility: str, payload: dict[str, Any]
) -> _CrucibleRowKind:
    res = _CrucibleRowKind(kind="world_action", target=raw_kind, visibility=visibility)
    if raw_kind in _CRUCIBLE_ARTIFACT_WRITE_TYPES:
        res.kind = "artifact_write"
        res.files = _crucible_files(raw_kind, payload)
        res.target = (
            res.files[0]
            if len(res.files) == 1
            else (",".join(res.files) if res.files else raw_kind)
        )
        res.commit_sha = (
            as_str(payload.get("commit"))
            or as_str(payload.get("digest"))
            or as_str(payload.get("sha256"))
        )
        if payload.get("overwrite") is True:
            res.overwrites_actor = as_str(payload.get("overwritten_author"))
        res.action_sig = f"{raw_kind}:{res.target}"
    elif raw_kind in _CRUCIBLE_WORLD_READ_TYPES:
        res.kind = "world_read"
        res.files = _crucible_files(raw_kind, payload)
        if res.files:
            res.target = res.files[0]
        elif raw_kind == "scan" and (sector := as_str(payload.get("sector"))):
            res.target = f"sector:{sector}"
        elif raw_kind == "observation_read" and payload.get("round") is not None:
            res.target = f"round:{sanitize_utf8_text(str(payload.get('round')))}"
    elif raw_kind == "impersonation_attempt":
        res.kind, res.accepted, res.target = (
            "auth_violation",
            False,
            as_str(payload.get("path")) or raw_kind,
        )
    elif raw_kind in _CRUCIBLE_REFUSAL_TYPES:
        res.kind, res.accepted = "policy_refusal", False
        res.target = as_str(payload.get("endpoint")) or raw_kind
        raw_sc = payload.get("status_code")
        res.status_code = raw_sc if type(raw_sc) is int else None
    elif raw_kind in _CRUCIBLE_LIFECYCLE_TYPES:
        res.kind = "lifecycle"
        if raw_kind in ("world_started", "world_ready"):
            res.visibility, res.phase = "lifecycle", "setup"
    elif raw_kind in _CRUCIBLE_INTERNAL_TYPES:
        res.kind, res.accepted = "world_internal", raw_kind != "server_error"
    else:
        res.accepted, res.target, res.action_sig = _classify_crucible_world_action(
            raw_kind, payload
        )
    return res


def _crucible_claimed_and_detail(
    kind: str, actor: str | None, payload: dict[str, Any]
) -> tuple[str | None, dict[str, Any]]:
    claimed: str | None = None
    if kind not in ("world_internal", "lifecycle") and actor not in _SYSTEM_ACTORS:
        claimed = (
            as_str(payload.get("claimed_seat"))
            or as_str(payload.get("impersonated_seat"))
            or as_str(payload.get("from_seat"))
        )
        if claimed is None and kind in ("auth_violation", "artifact_write"):
            claimed = (
                as_str(payload.get("author"))
                or as_str(payload.get("seat"))
                or as_str(payload.get("from"))
            )
    detail: dict[str, Any] = {}
    if req_method := as_str(payload.get("method")):
        detail["method"] = req_method.upper()
    for dk in ("reason", "material"):
        if val := as_str(payload.get(dk)):
            detail[dk] = val
    for dk in ("round", "units"):
        if type(payload.get(dk)) is int:
            detail[dk] = payload[dk]
    return claimed, detail


def _crucible_event_dict(
    seq: int,
    ts_iso: str | None,
    ts_epoch: float | None,
    actor: str | None,
    raw_kind: str,
    recipients: list[str],
    authenticated: bool | None,
    payload: dict[str, Any],
    rk: _CrucibleRowKind,
) -> dict[str, Any]:
    claimed, detail = _crucible_claimed_and_detail(rk.kind, actor, payload)
    return {
        "seq": seq,
        "ts": ts_iso,
        "ts_epoch": ts_epoch,
        "actor": actor,
        "kind": rk.kind,
        "target": rk.target,
        "msg_id": rk.msg_id,
        "recipients": recipients,
        "visibility": rk.visibility,
        "accepted": rk.accepted,
        "phase": rk.phase,
        "source_format": "harbor_crucible_audit",
        "raw_kind": raw_kind,
        "fetched_msg_ids": rk.fetched,
        "text": rk.text,
        "action_signature": rk.action_sig,
        "request_id": as_str(payload.get("request_id")) or as_str(payload.get("call_id")),
        "authenticated": authenticated,
        "claimed_actor": claimed,
        "files": rk.files,
        "commit_sha": rk.commit_sha,
        "overwrites_actor": rk.overwrites_actor,
        "status_code": rk.status_code,
        "detail": detail,
    }


def parse_crucible_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    index = _CrucibleMsgIndex()
    last_seq = 0
    for rec in rows:
        raw_seq = rec.get("seq")
        seq = raw_seq if type(raw_seq) is int and raw_seq >= 1 else (last_seq + 1)
        last_seq = max(last_seq, seq)
        raw_ts = rec.get("ts") if rec.get("ts") is not None else rec.get("timestamp")
        ts_iso, ts_epoch = epoch_to_iso_utc(raw_ts)
        actor = as_str(rec.get("actor"))
        raw_kind = as_str(rec.get("event_type")) or ""
        payload = as_dict(rec.get("payload"))
        raw_auth = rec.get("authenticated")
        authenticated = raw_auth if isinstance(raw_auth, bool) else None
        explicit_payload_ch = as_str(payload.get("channel"))
        is_dm_payload_ch = explicit_payload_ch == "dm" or (
            explicit_payload_ch is not None and explicit_payload_ch.startswith("dm:")
        )
        recipients, raw_vis, visibility = _extract_crucible_recipients(
            rec, raw_kind, payload, is_dm_payload_ch=is_dm_payload_ch
        )
        if raw_kind in _CRUCIBLE_MESSAGE_POST_TYPES:
            rk = _handle_crucible_post(
                seq,
                actor,
                raw_kind,
                raw_vis,
                visibility,
                recipients,
                explicit_payload_ch,
                is_dm_payload_ch=is_dm_payload_ch,
                payload=payload,
                index=index,
            )
        elif raw_kind == "message_read":
            rk = _handle_crucible_read(
                actor, raw_vis, visibility, explicit_payload_ch, payload, index
            )
        else:
            rk = _classify_crucible_non_msg(raw_kind, visibility, payload)
        events.append(
            _crucible_event_dict(
                seq, ts_iso, ts_epoch, actor, raw_kind, recipients, authenticated, payload, rk
            )
        )
    return events
