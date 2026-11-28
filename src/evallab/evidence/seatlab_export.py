from __future__ import annotations

import hashlib
import math
import os
import tempfile
import uuid
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from ._forensics_join import join_steps_and_events, step_observation_corpus
from ._forensics_sweep import log_group
from ._multiagent_io import MemberRecord, sanitize_json_value
from ._seatlab_scrub import (
    canonical_sha256 as _canonical_sha256,
)
from ._seatlab_scrub import (
    encode_bounded_record,
)
from ._seatlab_scrub import (
    sanitize_key as _sanitize_key,
)
from ._seatlab_scrub import (
    sanitize_role as _sanitize_role,
)
from ._seatlab_scrub import (
    scrub_text as _scrub_text,
)
from ._seatlab_scrub import (
    unique_key as _unique_key,
)
from .multiagent_adapter import (
    NormalizedMultiAgentTrial,
    discover_trial_dirs,
    read_multiagent_trial,
)
from .world_events import NormalizedWorldEvent, load_world_events

SEATLAB_SCHEMA = "seatlab.decision/v1"
_UUID_NAMESPACE = uuid.UUID("7c8b6f7a-4b4e-4d5f-a3be-8c4a12f019e4")


class SeatlabExportSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "evallab.seatlab-export-summary/v1"
    out_path: str
    n_trials: int
    n_decisions: int
    roles: list[str]
    sha256: str


def _canonical_trial_uuid(
    trial: NormalizedMultiAgentTrial,
    *,
    disambiguator: str | None = None,
) -> str:
    raw = (trial.trial_id or "").strip()
    if raw and disambiguator is None:
        try:
            return str(uuid.UUID(raw))
        except ValueError:
            pass
    parts = [trial.ecosystem, trial.task_name or "", trial.trial_name]
    if disambiguator:
        parts.append(disambiguator)
    elif raw:
        parts.append(raw)
    return str(uuid.uuid5(_UUID_NAMESPACE, ":".join(parts)))


def _strictly_before_for_export(a: NormalizedWorldEvent, b: NormalizedWorldEvent) -> bool:
    if log_group(a.source_format) == log_group(b.source_format):
        return a.seq < b.seq
    if a.ts_epoch is None or b.ts_epoch is None:
        return False
    if not (math.isfinite(a.ts_epoch) and math.isfinite(b.ts_epoch)):
        return False
    return a.ts_epoch < b.ts_epoch


def _visible_received_messages(
    decision_ev: NormalizedWorldEvent,
    actor_fetches: list[tuple[NormalizedWorldEvent, NormalizedWorldEvent, dict[str, Any]]],
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    rows = [
        row
        for post, fetch_ev, row in actor_fetches
        if _strictly_before_for_export(post, fetch_ev)
        and _strictly_before_for_export(fetch_ev, decision_ev)
        and _strictly_before_for_export(post, decision_ev)
    ]
    return rows[-limit:]


def _prior_self_history(
    decision_ev: NormalizedWorldEvent,
    actor_events: list[tuple[NormalizedWorldEvent, dict[str, Any]]],
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    rows = [row for ev, row in actor_events if _strictly_before_for_export(ev, decision_ev)]
    return rows[-limit:]


_ObsRow = tuple[int, float | None, float | None, float | None, dict[str, Any]]


def _prepare_atif_obs_index(
    trial: NormalizedMultiAgentTrial,
    *,
    end_of_play_epoch: float | None,
) -> dict[str, tuple[list[int], list[_ObsRow], list[float], list[_ObsRow]]]:
    indexed: dict[str, tuple[list[int], list[_ObsRow], list[float], list[_ObsRow]]] = {}
    for actor, steps in trial.member_steps.items():
        ordered = sorted(steps, key=lambda st: st.step_id)
        by_sid: list[_ObsRow] = []
        by_next_ts: list[tuple[float, _ObsRow]] = []
        next_ts_list: list[float | None] = [None] * len(ordered)
        future_ts: float | None = None
        for idx in range(len(ordered) - 1, -1, -1):
            next_ts_list[idx] = future_ts
            ts_val = ordered[idx].ts_epoch
            if ts_val is not None and math.isfinite(ts_val):
                future_ts = ts_val
        prev_ts: float | None = None
        for idx, st in enumerate(ordered):
            if st.ts_epoch is not None and math.isfinite(st.ts_epoch):
                prev_ts = st.ts_epoch
            next_ts = next_ts_list[idx]
            if end_of_play_epoch is not None and (
                (prev_ts is not None and prev_ts > end_of_play_epoch)
                or (next_ts is not None and next_ts > end_of_play_epoch)
            ):
                continue
            text = step_observation_corpus(st).strip()
            if not text:
                continue
            entry = {
                "step_id": st.step_id,
                "ts": st.timestamp,
                "content": _scrub_text(text, max_chars=800),
            }
            row: _ObsRow = (st.step_id, st.ts_epoch, prev_ts, next_ts, entry)
            by_sid.append(row)
            if next_ts is not None:
                by_next_ts.append((next_ts, row))
        by_next_ts.sort(key=lambda item: (item[0], item[1][0]))
        indexed[actor] = (
            [r[0] for r in by_sid],
            by_sid,
            [x[0] for x in by_next_ts],
            [x[1] for x in by_next_ts],
        )
    return indexed


def _prior_atif_observations(
    obs_index: dict[str, tuple[list[int], list[_ObsRow], list[float], list[_ObsRow]]],
    actor: str,
    linked_step_id: int | None,
    decision_epoch: float | None,
    *,
    limit: int = 12,
) -> list[dict[str, Any]]:
    entry = obs_index.get(actor)
    if entry is None:
        return []
    sids, sid_rows, next_tss, next_ts_rows = entry
    if linked_step_id is not None:
        pos = bisect_left(sids, linked_step_id)
        candidates = sid_rows[:pos]
        if decision_epoch is not None and math.isfinite(decision_epoch):
            candidates = [
                r
                for r in candidates
                if (
                    r[1] is None
                    and (r[2] is None or r[2] < decision_epoch)
                    and (r[3] is None or r[3] <= decision_epoch)
                )
                or (
                    r[1] is not None
                    and r[1] < decision_epoch
                    and r[3] is not None
                    and r[3] <= decision_epoch
                )
            ]
        return [r[4] for r in candidates[-limit:]]
    if decision_epoch is not None and math.isfinite(decision_epoch):
        pos = bisect_left(next_tss, decision_epoch)
        candidates = [r for r in next_ts_rows[:pos] if r[1] is not None and r[1] < decision_epoch]
        return [r[4] for r in candidates[-limit:]]
    return []


def _is_in_play_for_export(ev: NormalizedWorldEvent, end_of_play_epoch: float | None) -> bool:
    if ev.phase in ("post_play", "verification") or ev.late:
        return False
    return not (
        end_of_play_epoch is not None
        and ev.ts_epoch is not None
        and ev.ts_epoch > end_of_play_epoch
    )


def _build_actor_fetches(
    in_play: list[NormalizedWorldEvent],
    roster_set: set[str],
) -> dict[str, list[tuple[NormalizedWorldEvent, NormalizedWorldEvent, dict[str, Any]]]]:
    peer_posts_by_key = {
        (log_group(ev.source_format), ev.msg_id): ev
        for ev in in_play
        if ev.kind == "message_post" and ev.accepted and ev.actor in roster_set and ev.msg_id
    }
    scrubbed_post_text = {
        pkey: _scrub_text(post.text or "") for pkey, post in peer_posts_by_key.items()
    }
    first_fetch: dict[tuple[str, str, str], NormalizedWorldEvent] = {}
    for ev in in_play:
        if (
            ev.kind in ("message_fetch", "lifecycle")
            and ev.fetched_msg_ids
            and ev.accepted
            and ev.actor in roster_set
        ):
            grp = log_group(ev.source_format)
            for mid in ev.fetched_msg_ids:
                post = peer_posts_by_key.get((grp, mid))
                if (
                    post is None
                    or post.actor == ev.actor
                    or not _strictly_before_for_export(post, ev)
                ):
                    continue
                fkey = (ev.actor, grp, mid)
                cur = first_fetch.get(fkey)
                if cur is None or _strictly_before_for_export(ev, cur):
                    first_fetch[fkey] = ev

    fetches_by_actor: dict[
        str, list[tuple[NormalizedWorldEvent, NormalizedWorldEvent, dict[str, Any]]]
    ] = defaultdict(list)
    for (recip, grp, mid), fev in first_fetch.items():
        pkey = (grp, mid)
        post = peer_posts_by_key.get(pkey)
        if post is None or post.actor == recip:
            continue
        fetches_by_actor[recip].append(
            (
                post,
                fev,
                {
                    "msg_id": _scrub_text(str(mid), max_chars=256),
                    "sender": _scrub_text(
                        str(post.detail.get("facing_actor") or post.actor or ""), max_chars=256
                    ),
                    "post_seq": post.seq,
                    "fetch_seq": fev.seq,
                    "ts": post.ts,
                    "visibility": post.visibility,
                    "text": scrubbed_post_text[pkey],
                },
            )
        )
    for rows in fetches_by_actor.values():
        rows.sort(
            key=lambda item: (log_group(item[0].source_format), item[0].seq, item[0].msg_id or "")
        )
    return fetches_by_actor


def _build_actor_reads_and_acts(
    all_in_play: list[NormalizedWorldEvent],
    roster_set: set[str],
) -> tuple[
    dict[str, list[tuple[NormalizedWorldEvent, dict[str, Any]]]],
    dict[str, list[tuple[NormalizedWorldEvent, dict[str, Any]]]],
]:
    reads_by_actor: dict[str, list[tuple[NormalizedWorldEvent, dict[str, Any]]]] = defaultdict(list)
    acts_by_actor: dict[str, list[tuple[NormalizedWorldEvent, dict[str, Any]]]] = defaultdict(list)
    for ev in all_in_play:
        if not ev.accepted or ev.actor not in roster_set:
            continue
        entry = {
            "seq": ev.seq,
            "ts": ev.ts,
            "kind": _scrub_text(ev.raw_kind or ev.kind, max_chars=256),
            "target": _scrub_text(ev.target, max_chars=400) if ev.target is not None else None,
            "files": [_scrub_text(f, max_chars=256) for f in ev.files[:64]],
        }
        if ev.kind == "world_read":
            reads_by_actor[ev.actor].append((ev, entry))
        elif ev.kind in ("world_action", "artifact_write"):
            acts_by_actor[ev.actor].append((ev, entry))
    for rows in reads_by_actor.values():
        rows.sort(key=lambda item: (log_group(item[0].source_format), item[0].seq))
    for rows in acts_by_actor.values():
        rows.sort(key=lambda item: (log_group(item[0].source_format), item[0].seq))
    return reads_by_actor, acts_by_actor


def _build_single_decision(
    ev: NormalizedWorldEvent,
    trial: NormalizedMultiAgentTrial,
    trial_uuid: str,
    member_by_id: dict[str, MemberRecord],
    event_to_step: dict[tuple[str, int], int],
    fetches_by_actor: dict[
        str, list[tuple[NormalizedWorldEvent, NormalizedWorldEvent, dict[str, Any]]]
    ],
    reads_by_actor: dict[str, list[tuple[NormalizedWorldEvent, dict[str, Any]]]],
    acts_by_actor: dict[str, list[tuple[NormalizedWorldEvent, dict[str, Any]]]],
    obs_index: dict[str, tuple[list[int], list[_ObsRow], list[float], list[_ObsRow]]],
    seen_keys: set[str],
) -> dict[str, Any]:
    assert ev.actor is not None
    role = _sanitize_role(ev.actor)
    base_key = _sanitize_key(f"{trial.trial_name}:{role}:{ev.source_format}:seq{ev.seq}")
    key = _unique_key(base_key, seen_keys)
    linked_step = event_to_step.get((ev.source_format, ev.seq))
    mrec = member_by_id[ev.actor]
    view: dict[str, Any] = sanitize_json_value(
        {
            "actor": _scrub_text(mrec.display_name or ev.actor, max_chars=256),
            "canonical_seat": ev.actor,
            "ecosystem": trial.ecosystem,
            "task_name": trial.task_name,
            "decision_seq": ev.seq,
            "decision_ts": ev.ts,
            "received_messages": _visible_received_messages(ev, fetches_by_actor.get(ev.actor, [])),
            "prior_self_reads": _prior_self_history(ev, reads_by_actor.get(ev.actor, [])),
            "prior_self_actions": _prior_self_history(ev, acts_by_actor.get(ev.actor, [])),
            "prior_atif_observations": _prior_atif_observations(
                obs_index, ev.actor, linked_step, ev.ts_epoch
            ),
        }
    )
    action_val = _scrub_text(
        ev.action_signature or f"{ev.raw_kind or ev.kind}:{ev.target or ''}",
        max_chars=256,
    )
    return {
        "schema": SEATLAB_SCHEMA,
        "key": key,
        "role": role,
        "native_trial_id": trial_uuid,
        "basis": "reconstructed",
        "view": view,
        "view_sha256": _canonical_sha256(view),
        "observed": {"action": sanitize_json_value(action_val)},
        "answers": [],
    }


def build_seatlab_decisions_for_trial(
    trial_dir: Path,
    *,
    include_messages: bool = False,
    trial_disambiguator: str | None = None,
) -> list[dict[str, Any]]:
    trial = read_multiagent_trial(trial_dir)
    roster = [m.member_id for m in trial.members]
    roster_set = set(roster)
    member_by_id = {m.member_id: m for m in trial.members}
    wlog = load_world_events(
        trial_dir,
        roster=roster,
        member_aliases=trial.member_aliases,
        agent_execution_finished_at=trial.agent_execution_finished_at,
    )
    _, event_to_step = join_steps_and_events(
        trial.member_steps, wlog.events, wlog.git_events, roster_set
    )
    eop = wlog.end_of_play_ts_epoch
    in_play = [ev for ev in wlog.events if _is_in_play_for_export(ev, eop)]
    in_play_git = [ev for ev in wlog.git_events if _is_in_play_for_export(ev, eop)]
    all_in_play = sorted(
        (*in_play, *in_play_git),
        key=lambda ev: (
            ev.ts_epoch is None,
            ev.ts_epoch if ev.ts_epoch is not None else 0.0,
            log_group(ev.source_format),
            ev.seq,
        ),
    )
    fetches_by_actor = _build_actor_fetches(in_play, roster_set)
    reads_by_actor, acts_by_actor = _build_actor_reads_and_acts(all_in_play, roster_set)
    obs_index = _prepare_atif_obs_index(trial, end_of_play_epoch=eop)
    decision_kinds = (
        ("world_action", "artifact_write", "message_post")
        if include_messages
        else ("world_action", "artifact_write")
    )
    trial_uuid = _canonical_trial_uuid(trial, disambiguator=trial_disambiguator)
    seen_keys: set[str] = set()
    return [
        _build_single_decision(
            ev, trial, trial_uuid, member_by_id, event_to_step,
            fetches_by_actor, reads_by_actor, acts_by_actor, obs_index, seen_keys,
        )
        for ev in all_in_play
        if ev.kind in decision_kinds and ev.accepted and ev.actor in roster_set
    ]


def _atomic_write_tape(
    out_resolved: Path,
    records: list[tuple[str, str, str, str, bytes]],
) -> tuple[list[str], str]:
    out_resolved.parent.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()
    roles_set: set[str] = set()
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=out_resolved.parent,
            prefix=f".{out_resolved.name}.",
            suffix=".tmp",
            delete=False,
        ) as fh:
            tmp_path = Path(fh.name)
            for _, _, _, role, line in records:
                roles_set.add(role)
                fh.write(line)
                hasher.update(line)
        os.replace(tmp_path, out_resolved)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    return sorted(roles_set), hasher.hexdigest()


def export_seatlab_tape(
    path: Path,
    out_path: Path,
    *,
    include_messages: bool = False,
) -> SeatlabExportSummary:
    root = path.expanduser().resolve()
    trial_dirs, _, _ = discover_trial_dirs(root)
    if not trial_dirs:
        raise ValueError(f"no Harbor trials found under {path}")

    sorted_dirs = sorted(trial_dirs, key=lambda p: p.as_posix())
    records: list[tuple[str, str, str, str, bytes]] = []
    global_seen_keys: set[str] = set()
    seen_trial_uuids: set[str] = set()
    for td in sorted_dirs:
        rel_path = td.relative_to(root).as_posix() if td != root and root in td.parents else td.name
        trial_recs = build_seatlab_decisions_for_trial(td, include_messages=include_messages)
        if trial_recs:
            tid = str(trial_recs[0]["native_trial_id"])
            if tid in seen_trial_uuids:
                tid = str(uuid.uuid5(_UUID_NAMESPACE, f"{tid}:{rel_path}"))
                for rec in trial_recs:
                    rec["native_trial_id"] = tid
            seen_trial_uuids.add(tid)
        for rec in trial_recs:
            rec["key"] = _unique_key(str(rec["key"]), global_seen_keys)
            line = encode_bounded_record(rec)
            records.append(
                (
                    str(rec["native_trial_id"]),
                    str(rec["key"]),
                    str(rec["view_sha256"]),
                    str(rec["role"]),
                    line,
                )
            )
    records.sort(key=lambda item: (item[0], item[1], item[2]))
    roles, sha_hex = _atomic_write_tape(out_path.expanduser().resolve(), records)
    return SeatlabExportSummary(
        out_path=str(out_path),
        n_trials=len(sorted_dirs),
        n_decisions=len(records),
        roles=roles,
        sha256=sha_hex,
    )
