from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from . import _forensics_join as fj
from . import _forensics_rules as fr
from . import multiagent_adapter as ma
from . import world_events as we
from ._bespoke_adapter import BESPOKE_BOARD_PATHS, BESPOKE_PROXY_PATHS
from ._forensics_metrics import actor_read_file_after
from ._forensics_sweep import log_group
from ._multiagent_io import as_str, read_jsonl_records, safe_regular_file
from ._olam_muster_detail import verify_chain_jsonl_file

EdgeOutcome = Literal["influenced", "avoided_only", "triggered_only", "ignored", "neutral"]
_BOX_PAT = r"\bbox(?:es)?[\"']?\s*[:=#]?\s*(\d{1,4}(?:\s*(?:,|and|-|\.\.|to)\s*\d{1,4})*)"
_SUB_SPLIT = r",\s*(?=(?:and\s+)?[\"']?(?:i\b|i'|my\b|p[1-9]\d?\b|agent-\d+\b))"
_CLAUSE_RE = re.compile(rf"\.(?=\s|$)|[;!\n]+|{_SUB_SPLIT}", re.I)
_BOX_RE = re.compile(_BOX_PAT, re.I)
_SLIST_RE = re.compile(r"[\"']?(?:p[1-9]\d?|agent-\d+)[\"']?\s*:\s*\[([0-9,\s-]{1,64})\]", re.I)
_RANGE_RE = re.compile(r"^(\d{1,4})\s*(?:-|to|\.\.)\s*(\d{1,4})$", re.I)
_ROOM_RE = re.compile(r"\brooms?[\"']?\s*[:=#]?\s*(\d{1,4})\b", re.I)
_EMPTY_PAT = r"\broom[\"']?\s*[:=#]?\s*(\d{1,4})\b[^.,;!\n]{0,36}\b(?:empty|false|no +flag)\b"
_EMPTY_RE = re.compile(_EMPTY_PAT, re.I)
_SECTOR_RE = re.compile(r"\bsector\s*[:=#]?\s*([A-Za-z0-9_-]{1,32})\b", re.I)
_ZONE_RE = re.compile(r"\bzone\s*[:=#]?\s*([A-Za-z0-9_-]{1,16})\b", re.I)
_CHOICE_RE = re.compile(r"\b(alpha|beta|gamma|delta)\b", re.I)
_KV_INT_RE = re.compile(r"\b([a-z][a-z0-9_]{2,23})\s*(?:=|:|\s+)\s*(\d{1,6})\b", re.I)
_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z0-9_.-]+/[A-Za-z0-9_./-]*)")
_SEAT_PFX_RE = re.compile(r"^(?:http|p[1-9]\d?/)", re.I)
_NEG_PAT = r"\b(?:do\s+not|don't|dont|avoid|skip|already\s+(?:opened|answered|claimed))\b"
_NEG_RE = re.compile(_NEG_PAT, re.I)
_OWN_RE = re.compile(r"\b(?:i\s+will|i'll|i\s+am|i'm|claiming|claimed|opened|my)\b", re.I)
_FREE_RE = re.compile(r"\b(?:release|released|unclaimed|unopened)\b", re.I)
_STOP_KV = frozenset({"box", "boxes", "room", "rooms", "budget", "seq", "round", "step", "id"})
_SKIP_ACT = frozenset({"vote", "commit", "submit_clue", "demand", "propose", "pledge"})
_SHARED_KV = frozenset({"room", "item", "zone", "setting", "choice", "round"})
_MSG_SKIP = frozenset({"msg", "send", "post", "guard_unread_unblock", "guard_claim_released"})
_BOARD_ALL = (*BESPOKE_BOARD_PATHS, ("board-events.jsonl",))
_CHAIN_CANDS = (("verifier", "events.jsonl"), ("world", "events.jsonl"), ("events.jsonl",))
_CHAIN_ALL = (*_CHAIN_CANDS, *BESPOKE_PROXY_PATHS, ("proxy-usage.jsonl",))
_MAX_TEXT, _MAX_CLAUSES, _MAX_RANGE, _MAX_LINES = 8_000, 64, 64, 500_000


def _mk[T: BaseModel](cls: type[T], vals: Sequence[Any]) -> T:
    return cls(**dict(zip(cls.model_fields, vals, strict=True)))


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CausalInfluenceEdge(_StrictModel):
    sender: str
    msg_id: str
    post_seq: int
    recipient: str
    fetch_seq: int
    in_context_step: int | None = None
    outcome: EdgeOutcome
    downstream_action_seqs: list[int]
    avoided_targets: list[str]
    triggered_targets: list[str]
    duplicated_targets: list[str]


class MessageInfluenceRecord(_StrictModel):
    msg_id: str
    post_seq: int
    sender: str
    recipients_fetched: list[str]
    in_context_recipients: list[str]
    avoided_targets: list[str]
    triggered_targets: list[str]
    duplicated_targets: list[str]
    avoided_duplicate_targets: int
    triggered_actions: int
    contradicted_actions: int
    ignored: bool
    influence_score: float


class GuardImpactSummary(_StrictModel):
    claim_locks_granted: int
    claim_rejections_saved_dups: int
    unread_blocks_count: int
    post_block_fetch_compliance_rate: float | None = None
    hash_chain_checked_trials: int
    hash_chain_valid_trials: int
    hash_chain_tampered_trials: int


class _CdaRollup(_StrictModel):
    messages_posted: int
    messages_fetched: int
    avoided_duplicate_targets: int
    triggered_actions: int
    ignored_messages: int
    sender_causal_credit: dict[str, float]
    guard_impact: GuardImpactSummary


class TrialCdaReport(_CdaRollup):
    schema_version: str
    trial_name: str
    trial_id: str | None
    trial_dir: str
    task_name: str | None
    ecosystem: str
    roster: list[str]
    messages: list[MessageInfluenceRecord]
    edges: list[CausalInfluenceEdge]


class JobCdaReport(_CdaRollup):
    schema_version: str
    target_path: str
    job_id: str | None
    n_trials: int
    discovery_issues: list[str]
    trials: list[TrialCdaReport]


@dataclass(frozen=True, slots=True)
class _CdaCtx:
    roster: list[str]
    in_play: list[we.NormalizedWorldEvent]
    by_act: dict[str, list[we.NormalizedWorldEvent]]
    posts: list[we.NormalizedWorldEvent]
    maps: tuple[Any, ...]


def _ctgt(raw: str) -> str:
    t, p = raw.strip(), fr.normalize_repo_path(raw.strip().rstrip("/"))
    return (
        f"box:{int(t)}"
        if t.isdigit()
        else (f"file:{p}" if ("/" in t or "." in t) else f"target:{t.lower()}")
    )


def _expand_boxes(spec: str) -> set[str]:
    out: set[str] = set()
    for tok in (p.strip() for p in re.split(r"\s*(?:,|and)\s*", spec.strip(), flags=re.I)[:32]):
        if tok.isdigit():
            out.add(f"box:{int(tok)}")
        elif (m := _RANGE_RE.match(tok)) and 0 <= int(m[2]) - int(m[1]) <= _MAX_RANGE:
            out.update(f"box:{b}" for b in range(int(m[1]), int(m[2]) + 1))
    return out


def _event_targets(ev: we.NormalizedWorldEvent) -> tuple[set[str], set[str]]:
    excl, shared = set[str](), set[str]()
    excl.update({f"box:{b}"} if type(b := ev.detail.get("box")) is int else ())
    shared.update({f"room:{r}"} if type(r := ev.detail.get("room")) is int else ())
    if ct := as_str(ev.detail.get("claim_target")):
        excl.add(ct_tag := _ctgt(ct))
        shared.update({ct_tag} if ct_tag.startswith("file:") else ())
    fnorms = {f"file:{n}" for fp in ev.files if (n := fr.normalize_repo_path(fp.rstrip("/")))}
    excl, shared = excl | fnorms, shared | fnorms
    if ev.raw_kind == "answer":
        excl.add("action:answer")
        if isinstance(tots := ev.detail.get("answer_totals"), dict):
            shared.update(f"item:{k.lower()}" for k in tots if isinstance(k, str) and k)
    dup_ok = fr.is_duplicate_eligible_action(ev)
    if ev.target and ":" in ev.target:
        lhs, rhs = ev.target.split(":", 1)
        if lhs == "sector":
            excl.update({f"sector:{rhs.lower()}"} if dup_ok else ())
            shared.add(f"sector:{rhs.lower()}")
        elif lhs in ("submission", "choice", "round"):
            shared.add(f"{lhs}:{rhs.lower()}")
        elif "=" in rhs:
            for kv in (x for x in rhs.split(",")[:12] if "=" in x):
                kl, vl = (s.strip().lower() for s in kv.split("=", 1))
                if kl == "box" and vl.isdigit():
                    excl.add(f"box:{int(vl)}")
                elif kl in ("sector", "slot") and dup_ok:
                    excl.add(f"{kl}:{vl}")
                    shared.add(f"{kl}:{vl}")
                elif kl in _SHARED_KV:
                    shared.add(f"{kl}:{vl}")
                elif kl == "target" and vl:
                    excl.add(_ctgt(vl))
    fb = not excl and ev.accepted and dup_ok and ev.kind == "world_action"
    if fb and ev.raw_kind not in _SKIP_ACT:
        excl.add(f"act:{ev.raw_kind}:{ev.target or ev.raw_kind}")
    return excl, shared


def _message_targets(
    post: Any, s_excl: set[str], s_claims: set[str], peers: set[str], has_files: bool = True
) -> tuple[set[str], set[str], set[int]]:
    text, sender = (post.text or "")[:_MAX_TEXT], (post.actor or "").lower()
    msg_excl, msg_intro, empty_r, rel_tgts = set[str](), set[str](), set[int](), set[str]()
    ans_cues = ("already answered", "submitted the final", "submitted answer")
    for clause in (rc.strip() for rc in _CLAUSE_RE.split(text)[:_MAX_CLAUSES] if rc.strip()):
        low = clause.lower()
        c_excl = {b for m in _BOX_RE.finditer(clause) for b in _expand_boxes(m[1])}
        c_excl |= {b for m in _SLIST_RE.finditer(clause) for b in _expand_boxes(m[1])}
        secs = {f"sector:{m[1].lower()}" for m in _SECTOR_RE.finditer(clause)}
        p_ms = _PATH_RE.finditer(clause) if has_files else ()
        qs = (m[1].strip(".,;:()[]{}\"'").rstrip("/") for m in p_ms)
        p_tags = {
            f"file:{fr.normalize_repo_path(q)}" for q in qs if q and not _SEAT_PFX_RE.match(q)
        }
        c_excl |= secs | p_tags | ({"action:answer"} if any(p in low for p in ans_cues) else set())
        c_sh = secs | p_tags | {f"room:{int(m[1])}" for m in _ROOM_RE.finditer(clause)}
        c_sh |= {f"zone:{m[1].lower()}" for m in _ZONE_RE.finditer(clause)}
        c_sh |= {f"choice:{m[1].lower()}" for m in _CHOICE_RE.finditer(clause)}
        k_hits = (m[1].lower() for m in _KV_INT_RE.finditer(clause))
        c_sh |= {f"item:{k}" for k in k_hits if k not in _STOP_KV}
        c_sh |= {f"id:{t}" for t in fr.extract_id_tokens(low) if len(t) >= 4 and not t.isdigit()}
        empty_r.update(int(m[1]) for m in _EMPTY_RE.finditer(clause))
        is_rel = _FREE_RE.search(low) is not None
        has_s = bool(sender) and re.search(rf"\b{re.escape(sender)}\b", low) is not None
        has_p = any(re.search(rf"\b{re.escape(p.lower())}\b", low) for p in peers if p)
        own_ok = _OWN_RE.search(low) is not None and not (has_p and not has_s)
        cues = _NEG_RE.search(low) is not None or own_ok
        is_claim = not is_rel and (cues or (has_s and not has_p))
        rel_tgts.update(c_excl if is_rel else ())
        for cand in c_excl:
            use_ex = not is_rel and (is_claim or cand in s_excl or cand in s_claims)
            (msg_excl if use_ex else msg_intro).add(cand)
        msg_intro.update(c_sh)
    msg_excl.update(h for h in s_claims if h in msg_intro and h not in rel_tgts)
    return msg_excl, msg_intro - msg_excl, empty_r


def _normalize_credit(raw_by_seat: Mapping[str, float], roster: Sequence[str]) -> dict[str, float]:
    ordered = list(dict.fromkeys(roster))
    assert all(math.isfinite(v) and v >= 0.0 for v in raw_by_seat.values())
    total = math.fsum(raw_by_seat.get(s, 0.0) for s in ordered)
    if total <= 0.0 or not ordered:
        return dict.fromkeys(ordered, 0.0)
    shares = {s: round(raw_by_seat.get(s, 0.0) / total, 6) for s in ordered}
    top = max(ordered, key=lambda s: (raw_by_seat.get(s, 0.0), s))
    shares[top] = round(max(0.0, 1.0 - math.fsum(v for k, v in shares.items() if k != top)), 6)
    assert math.isclose(math.fsum(shares.values()), 1.0, abs_tol=1e-5)
    return shares


def _compute_guard_impact(
    tdir: Path | None, in_play: Sequence[we.NormalizedWorldEvent], wlog: Any, rset: set[str]
) -> GuardImpactSummary:
    locks = saved_dups = u_blocks = compliant = 0
    for idx, ev in enumerate(in_play):
        c_ok = ev.raw_kind == "claim" and ev.accepted and ev.detail.get("claim_granted") is True
        locks += int(ev.raw_kind == "guard_claim_granted" or c_ok)
        rsn, gb = (as_str(ev.detail.get("reason")) or "").lower(), ev.detail.get("guard_blocked")
        ac = gb == "already_claimed" or ev.raw_kind == "guard_claim_denied"
        saved_dups += int(ac or (ev.kind == "policy_refusal" and "claimed by" in rsn))
        ub = gb == "unread_messages" or ev.raw_kind == "guard_unread_block"
        if not ((ub or (ev.kind == "policy_refusal" and "unread" in rsn)) and ev.actor in rset):
            continue
        u_blocks += 1
        for nxt in (x for x in in_play[idx + 1 :] if x.actor == ev.actor):
            is_f = nxt.kind == "message_fetch" or nxt.raw_kind == "guard_unread_unblock"
            if nxt.accepted and (is_f or (nxt.kind == "lifecycle" and bool(nxt.fetched_msg_ids))):
                compliant += 1
                break
            ub2 = (
                nxt.detail.get("guard_blocked") == "unread_messages"
                or nxt.raw_kind == "guard_unread_block"
            )
            r_k = nxt.kind in ("world_action", "artifact_write", "policy_refusal")
            if ub2 or (r_k and nxt.detail.get("method") != "GET" and nxt.raw_kind not in _MSG_SKIP):
                break
    ev_f = [bool(v) for e in wlog.events if (v := e.detail.get("hash_chain_valid")) is not None]
    bad_keys = ("invalid_lines=", "non_dict_lines=", ":invalid_seq", ":broken_hash_chain")
    bad_e = any(k in w for w in wlog.parse_errors for k in bad_keys)
    c_fps = (safe_regular_file(tdir, *rel) for rel in _CHAIN_ALL) if tdir is not None else ()
    f_res = [r for fp in c_fps if fp and (r := verify_chain_jsonl_file(fp, _MAX_LINES)) is not None]
    ev_ok = (all(ev_f) and not bad_e) if ev_f else True
    cres = (ev_ok and all(f_res)) if (ev_f or f_res) else None
    chk, val, tamp = int(cres is not None), int(cres is True), int(cres is False)
    assert val + tamp == chk
    rate = round(compliant / u_blocks, 6) if u_blocks > 0 else None
    return _mk(GuardImpactSummary, (locks, saved_dups, u_blocks, rate, chk, val, tamp))


def _hit(cand: str, tset: set[str]) -> bool:
    if cand in tset or not cand.startswith("file:"):
        return cand in tset
    return any(
        t.startswith("file:") and (t.startswith(cand + "/") or cand.startswith(t + "/"))
        for t in tset
    )


def _merge_causal_events(
    in_play: Sequence[we.NormalizedWorldEvent], in_git: Sequence[we.NormalizedWorldEvent]
) -> list[we.NormalizedWorldEvent]:
    ps = sorted(in_play, key=lambda e: (e.seq, e.ts_epoch is None, e.ts_epoch or 0.0))
    gs = sorted(in_git, key=lambda e: (e.ts_epoch is None, e.ts_epoch or 0.0, e.seq))
    out, i, j = list[we.NormalizedWorldEvent](), 0, 0
    while i < len(ps) and j < len(gs):
        pt, gt = ps[i].ts_epoch, gs[j].ts_epoch
        take_p = pt is None or gt is None or pt < gt + 1.0
        out.append(ps[i] if take_p else gs[j])
        i, j = i + int(take_p), j + int(not take_p)
    return [*out, *ps[i:], *gs[j:]]


def _is_released(ev: we.NormalizedWorldEvent, ev_excl: set[str], s_rels: Any, s_acts: Any) -> bool:
    hb = fr.happened_before
    for rv, rt in s_rels:
        if hb(rv, ev, inclusive=True):
            for x in (x for x in ev_excl if _hit(x, rt)):
                if not any(hb(rv, cv) and hb(cv, ev) and _hit(x, ct) for cv, ct in s_acts):
                    return True
    return False


def _evaluate_edge(
    ctx: _CdaCtx, post: Any, recip: str, f_ev: Any, m_tgts: tuple[set[str], set[str], set[int]]
) -> tuple[CausalInfluenceEdge, set[tuple[str, str, int]], int]:
    assert post.actor is not None and post.msg_id is not None
    _, _, fwrites, freads, areads, _, in_ctx, rel_events, act_events = ctx.maps
    msg_excl, msg_intro, empty_rooms = m_tgts
    prior_excl, prior_refs, post_att = set[str](), set[str](), set[str]()
    trig_keys, trig_tgts, dup_tgts = set[tuple[str, str, int]](), set[str](), set[str]()
    contradictions, had_post = 0, False
    s_rels, s_acts = rel_events.get(post.actor, ()), act_events.get(post.actor, ())
    hb, rd_ok = fr.happened_before, actor_read_file_after
    skip_rk = ("release", "guard_claim_released", "guard_claim_denied")
    for ev in ctx.by_act[recip]:
        d, ok_w = ev.detail, ev.accepted and ev.kind in ("world_action", "artifact_write")
        is_act = ok_w and ev.raw_kind not in skip_rk
        gb = ev.raw_kind == "guard_claim_denied" or d.get("guard_blocked") == "already_claimed"
        g_ans = ev.kind == "policy_refusal" and d.get("already_answered") is True
        if not (is_act or gb or g_ans):
            continue
        ev_excl, ev_sh = _event_targets(ev)
        if not hb(f_ev, ev, inclusive=True):
            prior_excl.update(ev_excl if is_act else ())
            prior_refs.update((ev_excl | ev_sh) if is_act else ())
            continue
        had_post = True
        fps = [fr.normalize_repo_path(p.rstrip("/")) for p in ev.files if p] if is_act else []
        sw_p = [
            (p, s[-1])
            for p in fps
            if (s := [w for w in fwrites.get((post.actor, p), ()) if hb(w, ev)])
        ]
        handoff = bool(sw_p) and all(rd_ok(recip, p, sw, ev, freads, areads) for p, sw in sw_p)
        released = _is_released(ev, ev_excl, s_rels, s_acts)
        bad_rm = type(d.get("room")) is int and d["room"] in empty_rooms
        ow_hit = ev.overwrites_actor == post.actor and any(
            _hit(x, msg_excl | msg_intro) for x in ev_excl
        )
        contradictions += int(is_act and not handoff and not released and ow_hit) + int(
            is_act and bad_rm
        )
        if not handoff and not released:
            post_att.update(ev_excl)
            dup_tgts.update(x for x in ev_excl if _hit(x, msg_excl) and not _hit(x, prior_excl))
        if is_act:
            crefs = (ev_excl | ev_sh) if (handoff or released) else ((ev_excl - dup_tgts) | ev_sh)
            allow = msg_intro | msg_excl if (handoff or released) else msg_intro
            if hits := {r for r in crefs if _hit(r, allow) and not _hit(r, prior_refs)}:
                trig_keys.add((recip, ev.source_format, ev.seq))
                trig_tgts.update(hits)
    av_cand = {x for x in msg_excl if not _hit(x, prior_excl) and not _hit(x, post_att)}
    avoided = av_cand if had_post else set()
    ctot = contradictions + len(dup_tgts)
    outs: tuple[EdgeOutcome, ...] = ("neutral", "triggered_only", "avoided_only", "influenced")
    out: EdgeOutcome = "ignored" if ctot > 0 else outs[(bool(avoided) << 1) | bool(trig_tgts)]
    c_step = in_ctx.get((recip, log_group(post.source_format), post.msg_id))
    tseqs = sorted({s for _, _, s in trig_keys})
    v1 = (post.actor, post.msg_id, post.seq, recip, f_ev.seq, c_step, out)
    v2 = (tseqs, sorted(avoided), sorted(trig_tgts), sorted(dup_tgts))
    return _mk(CausalInfluenceEdge, (*v1, *v2)), trig_keys, ctot


def _walk_causal_state(comb: Any, rset: set[str], pfx_map: Any, rel_map: Any, init: bool) -> Any:
    by_act: dict[str, list[we.NormalizedWorldEvent]] = defaultdict(list)
    cur_excl, cur_claims = defaultdict[str, set[str]](set), defaultdict[str, set[str]](set)
    post_state: dict[tuple[str, int], tuple[set[str], set[str], bool]] = {}
    fwrites, freads = defaultdict(list), defaultdict(list)
    rel_events, act_events = defaultdict(list), defaultdict(list)
    seen_f = init
    for ev in comb:
        if ev.source_format.startswith("bespoke_board") and ev.seq in pfx_map:
            ev = ev.model_copy(update={"detail": {**ev.detail, "claim_target": pfx_map[ev.seq]}})
        if ev.actor not in rset or ev.actor in fr.SYSTEM_ACTORS:
            continue
        ac = ev.actor
        by_act[ac].append(ev)
        is_wa = ev.accepted and ev.kind in ("world_action", "artifact_write")
        if ev.kind == "message_post" and ev.accepted and ev.msg_id:
            post_state[(ev.source_format, ev.seq)] = (
                set(cur_excl[ac]),
                set(cur_claims[ac]),
                seen_f,
            )
        elif ev.raw_kind == "guard_claim_released" and ev.seq in rel_map:
            rt = rel_map[ev.seq]
            cur_claims[ac] -= rt
            cur_excl[ac] -= rt
            rel_events[ac].append((ev, rt))
        elif is_wa and ev.raw_kind != "guard_claim_denied":
            ex, _ = _event_targets(ev)
            if ev.raw_kind == "release":
                cur_claims[ac] -= ex
                cur_excl[ac] -= ex
                rel_events[ac].append((ev, ex))
            else:
                cur_excl[ac].update(ex)
                act_events[ac].append((ev, ex))
                c_ok = ev.raw_kind == "claim" and ev.detail.get("claim_granted") is True
                if ev.raw_kind == "guard_claim_granted" or c_ok:
                    cur_claims[ac].update(ex)
            for fp in ev.files:
                fwrites[(ac, fr.normalize_repo_path(fp.rstrip("/")))].append(ev)
                seen_f = True
            seen_f = seen_f or any(x.startswith("file:") for x in ex)
        elif ev.accepted and ev.kind == "world_read":
            for fp in ev.files:
                freads[(ac, fr.normalize_repo_path(fp.rstrip("/")))].append(ev)
                seen_f = True
    return by_act, post_state, fwrites, freads, rel_events, act_events


def _build_cda_ctx(
    trial: ma.NormalizedMultiAgentTrial, world_log: we.WorldEventLog, trial_dir: Path | None
) -> _CdaCtx:
    roster, rset = [m.member_id for m in trial.members], {m.member_id for m in trial.members}
    valid_m = (m for m in trial.members if m.trajectory_status == "present")
    obs = {m.member_id: s for m in valid_m if (s := trial.member_steps.get(m.member_id))}
    _, ev2step = fj.join_steps_and_events(obs, world_log.events, world_log.git_events, rset)
    in_play = [e for e in world_log.events if e.phase != "post_play" and not e.late]
    in_git = [e for e in world_log.git_events if e.phase != "post_play" and not e.late]
    rel_map, pfx_map = dict[int, set[str]](), dict[int, str]()
    bps = (p for rel in _BOARD_ALL if trial_dir and (p := safe_regular_file(trial_dir, *rel)))
    g_evs = ("guard_claim_granted", "guard_claim_released", "guard_claim_denied")
    for idx, rec in enumerate(read_jsonl_records(next(bps, None))[0], start=1):
        ra, pf, evn = as_str(rec.get("agent")), as_str(rec.get("prefix")), rec.get("event")
        if ra and pf and evn in g_evs:
            pfx_map[idx] = pf
            if evn == "guard_claim_released":
                rel_map.setdefault(idx, set()).add(_ctgt(pf))
    comb = _merge_causal_events(in_play, in_git)
    by_act, post_state, fwrites, freads, rel_events, act_events = _walk_causal_state(
        comb, rset, pfx_map, rel_map, trial.ecosystem == "bespoke_team"
    )
    areads: dict[tuple[str, str], list[float]] = defaultdict(list)
    for ts_ep, aid, fp in fj.extract_timed_atif_file_reads(obs) if obs else ():
        areads[(aid, fr.normalize_repo_path(fp.rstrip("/")))].append(ts_ep)
    mp = (e for e in comb if e.kind == "message_post" and e.accepted)
    posts = [e for e in mp if e.actor in rset and e.msg_id]
    p_by_id = {(log_group(e.source_format), e.msg_id): e for e in posts if e.msg_id}
    ffetch: dict[tuple[str, str, str], we.NormalizedWorldEvent] = {}
    for ev in comb:
        if ev.actor in rset and ev.accepted:
            grp = log_group(ev.source_format)
            for mid in ev.fetched_msg_ids:
                fk = (ev.actor, grp, mid)
                if fk not in ffetch or fr.happened_before(ev, ffetch[fk]):
                    ffetch[fk] = ev
    in_ctx = fj.find_in_context_steps(p_by_id, ffetch, obs, ev2step)
    maps = (rset, post_state, fwrites, freads, areads, ffetch, in_ctx, rel_events, act_events)
    return _CdaCtx(roster, comb, by_act, posts, maps)


_ScoreOut = tuple[MessageInfluenceRecord, list[CausalInfluenceEdge], set[tuple[str, str, int]]]


def _score_message(
    ctx: _CdaCtx, post: Any, seen_av: set[tuple[str, str]], seen_tr: set[tuple[str, str, int]]
) -> _ScoreOut:
    assert post.actor is not None and post.msg_id is not None
    rset, post_state, _, _, _, ffetch, in_ctx, _, _ = ctx.maps
    p_excl, p_claims, has_f = post_state.get((post.source_format, post.seq), (set(), set(), False))
    grp, peers, ok_r = log_group(post.source_format), rset - {post.actor}, post.recipients
    m_targets = _message_targets(post, p_excl, p_claims, peers, has_f)
    recips = [r for r in ctx.roster if r != post.actor and (not ok_r or r in ok_r)]
    fetched_r, ctx_r, edges = list[str](), list[str](), list[CausalInfluenceEdge]()
    av_all, tr_all, dp_all = set[str](), set[str](), set[str]()
    trig_keys: set[tuple[str, str, int]] = set()
    contra_n = 0
    for r in recips:
        if (f_ev := ffetch.get((r, grp, post.msg_id))) is None:
            continue
        fetched_r.append(r)
        if (r, grp, post.msg_id) in in_ctx:
            ctx_r.append(r)
        edge, r_tk, r_ct = _evaluate_edge(ctx, post, r, f_ev, m_targets)
        edges.append(edge)
        av_all.update(edge.avoided_targets)
        tr_all.update(edge.triggered_targets)
        dp_all.update(edge.duplicated_targets)
        trig_keys.update(r_tk)
        contra_n += r_ct
    av_c, ign = av_all - dp_all, bool(fetched_r) and contra_n > 0
    av_pairs = [(e.recipient, t) for e in edges for t in e.avoided_targets if t in av_c]
    nov_av = {t for rp, t in av_pairs if (rp, t) not in seen_av}
    seen_av.update(av_pairs)
    nov_tr, trig_n = trig_keys - seen_tr, len(trig_keys)
    seen_tr.update(trig_keys)
    score = round(max(0.0, float(len(nov_av) + len(nov_tr)) - 0.5 * float(contra_n)), 6)
    v1 = (post.msg_id, post.seq, post.actor, fetched_r, ctx_r, sorted(av_c), sorted(tr_all))
    v2 = (sorted(dp_all), len(av_c), trig_n, contra_n, ign, score)
    return _mk(MessageInfluenceRecord, (*v1, *v2)), edges, trig_keys


def analyze_normalized_trial_cda(
    trial: ma.NormalizedMultiAgentTrial, wlog: we.WorldEventLog, *, trial_dir: Path | None = None
) -> TrialCdaReport:
    ctx = _build_cda_ctx(trial, wlog, trial_dir)
    edges, msg_records = list[CausalInfluenceEdge](), list[MessageInfluenceRecord]()
    raw_credit: dict[str, float] = dict.fromkeys(ctx.roster, 0.0)
    trial_trig, trial_av, trial_dp = set[tuple[str, str, int]](), set[str](), set[str]()
    seen_av, seen_tr = set[tuple[str, str]](), set[tuple[str, str, int]]()
    for post in ctx.posts:
        rec, p_edges, p_tk = _score_message(ctx, post, seen_av, seen_tr)
        msg_records.append(rec)
        edges.extend(p_edges)
        trial_trig.update(p_tk)
        trial_av.update(rec.avoided_targets)
        trial_dp.update(rec.duplicated_targets)
        raw_credit[rec.sender] = raw_credit.get(rec.sender, 0.0) + rec.influence_score
    guard = _compute_guard_impact(trial_dir, ctx.in_play, wlog, ctx.maps[0])
    n_f = sum(bool(m.recipients_fetched) for m in msg_records)
    n_ig = sum(m.ignored for m in msg_records)
    n_av, n_tr = len(trial_av - trial_dp), len(trial_trig)
    base1 = (len(msg_records), n_f, n_av, n_tr, n_ig)
    base2 = (_normalize_credit(raw_credit, ctx.roster), guard)
    meta = ("evallab.seat_cda.trial/v1", trial.trial_name, trial.trial_id, trial.trial_dir)
    tail = (trial.task_name, trial.ecosystem, ctx.roster, msg_records, edges)
    return _mk(TrialCdaReport, (*base1, *base2, *meta, *tail))


def analyze_trial_cda(tdir: Path) -> TrialCdaReport:
    t = ma.read_multiagent_trial(tdir)
    r, als, fin = [m.member_id for m in t.members], t.member_aliases, t.agent_execution_finished_at
    wlog = we.load_world_events(tdir, roster=r, member_aliases=als, agent_execution_finished_at=fin)
    return analyze_normalized_trial_cda(t, wlog, trial_dir=tdir)


def analyze_cda(path: Path) -> TrialCdaReport | JobCdaReport:
    trial_dirs, job_id, issues = ma.discover_trial_dirs(path)
    if len(trial_dirs) == 1 and trial_dirs[0] == path.resolve():
        return analyze_trial_cda(trial_dirs[0])
    if not trial_dirs and job_id is None and not issues:
        raise ValueError(f"no Harbor trials found under {path}")
    unsorted = [analyze_trial_cda(t) for t in trial_dirs]
    reps = sorted(unsorted, key=lambda r: (r.trial_name, r.trial_id or ""))
    seats = list(dict.fromkeys(s for r in reps for s in r.roster))
    all_m = [m for r in reps for m in r.messages]
    raw_tot = {s: sum(m.influence_score for m in all_m if m.sender == s) for s in seats}
    gs = [r.guard_impact for r in reps]
    ub_r = [(g.unread_blocks_count, g.post_block_fetch_compliance_rate or 0.0) for g in gs]
    ub, csum = sum(n for n, _ in ub_r), sum(round(r * n) for n, r in ub_r)
    gk1 = ("claim_locks_granted", "claim_rejections_saved_dups", "hash_chain_checked_trials")
    gk2 = ("hash_chain_valid_trials", "hash_chain_tampered_trials")
    g_s = [sum(getattr(g, f) for g in gs) for f in (*gk1, *gk2)]
    cr = round(csum / ub, 6) if ub else None
    guard = _mk(GuardImpactSummary, (g_s[0], g_s[1], ub, cr, *g_s[2:]))
    rk = (*("messages_posted", "messages_fetched"), "avoided_duplicate_targets")
    r_s = [sum(getattr(r, f) for r in reps) for f in (*rk, "triggered_actions", "ignored_messages")]
    meta = ("evallab.seat_cda.job/v1", path.resolve().as_posix(), job_id, len(reps))
    cred = _normalize_credit(raw_tot, seats)
    return _mk(JobCdaReport, (*r_s, cred, guard, *meta, sorted(set(issues)), reps))


def format_cda_json(report: TrialCdaReport | JobCdaReport) -> str:
    return json.dumps(report.model_dump(mode="json"), allow_nan=False, sort_keys=True, indent=2)


def render_cda_text(report: TrialCdaReport | JobCdaReport) -> str:
    gi, rate = report.guard_impact, report.guard_impact.post_block_fetch_compliance_rate
    comp = f"{rate:.3f}" if rate is not None else "n/a"
    cred = ", ".join(f"{k}={v:.3f}" for k, v in report.sender_causal_credit.items()) or "none"
    if isinstance(report, JobCdaReport):
        hdr = f"Job CDA: {report.target_path} (trials={report.n_trials})"
    else:
        hdr = f"Trial CDA: {report.trial_name} ({report.ecosystem})"
    g_ln = f"Guard impact: locks={gi.claim_locks_granted} saved_dups={gi.claim_rejections_saved_dups} unread_blocks={gi.unread_blocks_count} compliance={comp} chain={gi.hash_chain_valid_trials}/{gi.hash_chain_checked_trials} valid (tampered={gi.hash_chain_tampered_trials})"
    m_ln = f"Messages: posted={report.messages_posted} fetched={report.messages_fetched} ignored={report.ignored_messages}"
    i_ln = f"Influence: avoided_duplicate_targets={report.avoided_duplicate_targets} triggered_actions={report.triggered_actions}"
    out = [hdr, m_ln, i_ln, f"Sender causal credit: {cred}", g_ln]
    for t in report.trials[:50] if isinstance(report, JobCdaReport) else ():
        if len(out) == 5:
            out.append("trial                          posted fetched avoided triggered ignored")
        av, tr, ig = t.avoided_duplicate_targets, t.triggered_actions, t.ignored_messages
        nm, mp, mf = t.trial_name[:30], t.messages_posted, t.messages_fetched
        out.append(f"{nm:<30} {mp:>6} {mf:>7} {av:>7} {tr:>9} {ig:>7}")
    return "\n".join(out)
