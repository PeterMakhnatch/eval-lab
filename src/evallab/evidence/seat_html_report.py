from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ._forensics_join import extract_timed_atif_file_reads, find_in_context_steps
from ._forensics_metrics import (
    compute_duplicate_actions,
    happened_before,
    log_group,
    same_log,
)
from ._forensics_sweep import (
    build_action_suffix_index,
    compute_excluded_mut_steps,
    compute_last_mut_steps,
    has_downstream_action,
)
from ._multiagent_io import safe_finite_float, sanitize_json_value, sanitize_utf8_text
from ._seat_html_executive import (
    merge_corpus_overviews,
    summarize_bespoke_public_source,
    summarize_decomposition_spec,
    summarize_disk_corpus,
)
from ._seat_html_template import build_html_document
from .multiagent_adapter import NormalizedMultiAgentTrial
from .world_events import NormalizedWorldEvent, WorldEventLog

_SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.S)


def _short_label(ev: NormalizedWorldEvent) -> str:
    if ev.kind == "message_post":
        return f"post#{ev.msg_id or ev.seq}"
    if ev.kind == "message_fetch":
        cnt = len(ev.fetched_msg_ids)
        return f"fetch({cnt})" if cnt else "fetch"
    if ev.kind == "artifact_write":
        return f"write:{(ev.target or 'file')[:14]}"
    if ev.kind == "world_action":
        return f"{(ev.raw_kind or 'act')[:10]}:{(ev.target or '')[:10]}".rstrip(":")
    if ev.kind == "policy_refusal":
        return f"refused:{(ev.target or '')[:10]}"
    if ev.kind == "auth_violation":
        return f"auth!:{ev.status_code or ''}"
    return (ev.raw_kind or ev.kind)[:14]


def _find_matching_action(
    actions: list[NormalizedWorldEvent],
    fetch_ev: NormalizedWorldEvent,
    informed_dup_keys: set[tuple[str, int]],
    event_to_step: dict[tuple[str, int], int],
    *,
    min_step_gt: int | None,
    allow_same_step_same_log: bool,
) -> NormalizedWorldEvent | None:
    for act in actions:
        if (act.source_format, act.seq) in informed_dup_keys:
            continue
        if not happened_before(fetch_ev, act):
            continue
        act_st = event_to_step.get((act.source_format, act.seq))
        if min_step_gt is not None and act_st is not None:
            if act_st > min_step_gt or (
                allow_same_step_same_log and act_st == min_step_gt and same_log(fetch_ev, act)
            ):
                return act
            continue
        return act
    return None


def _map_epoch_x(ts_val: float | None, min_t: float, max_t: float, scaled: bool) -> float | None:
    t = safe_finite_float(ts_val)
    if t is None:
        return None
    if not scaled:
        diff = t - min_t
        return round(diff, 3) if math.isfinite(diff) and abs(diff) <= 1e12 else None
    h_span = math.ldexp(max_t, -1) - math.ldexp(min_t, -1)
    if h_span <= 0.0:
        return 0.0
    raw = ((math.ldexp(t, -1) - math.ldexp(min_t, -1)) / h_span) * 1000.0
    return round(raw, 3) if math.isfinite(raw) and abs(raw) <= 1e12 else None


def _render_timeline_nodes(
    wlog: WorldEventLog, event_to_step: dict[tuple[str, int], int], max_events: int
) -> tuple[list[dict[str, Any]], int, float | None]:
    vis = [
        ev
        for ev in (*wlog.events, *wlog.git_events)
        if ev.kind not in ("lifecycle", "world_internal")
    ]
    vis.sort(
        key=lambda ev: (
            safe_finite_float(ev.ts_epoch) is None,
            safe_finite_float(ev.ts_epoch) or 0.0,
            ev.source_format,
            ev.seq,
        )
    )
    sliced = vis[:max_events]
    timed = [t for ev in sliced if (t := safe_finite_float(ev.ts_epoch)) is not None]
    has_ep = bool(timed)
    min_t, max_t = (min(timed), max(timed)) if has_ep else (0.0, 0.0)
    raw_span = max_t - min_t if has_ep else 0.0
    scaled = has_ep and (not math.isfinite(raw_span) or raw_span > 1e9)
    max_x = (1000.0 if max_t > min_t else 0.0) if scaled else (raw_span if has_ep else 0.0)
    u_step = max(1.0, max_x * 0.05)
    out: list[dict[str, Any]] = []
    u_idx = 0
    for idx, ev in enumerate(sliced, start=1):
        if has_ep:
            mapped = _map_epoch_x(ev.ts_epoch, min_t, max_t, scaled)
            if mapped is not None:
                x_pos = mapped
            else:
                u_idx += 1
                x_pos = round(max_x + u_idx * u_step, 3)
        else:
            x_pos = float(idx)
        out.append(
            {
                "seq": ev.seq,
                "source_format": ev.source_format,
                "actor": ev.actor,
                "kind": ev.kind,
                "raw_kind": ev.raw_kind,
                "phase": ev.phase,
                "target": ev.target,
                "msg_id": ev.msg_id,
                "ts": ev.ts,
                "x_pos": x_pos,
                "step_id": event_to_step.get((ev.source_format, ev.seq)),
                "short_label": _short_label(ev),
                "text": (ev.text or "")[:200] if ev.text else None,
            }
        )
    eop_x = _map_epoch_x(wlog.end_of_play_ts_epoch, min_t, max_t, scaled) if has_ep else None
    return out, max(0, len(vis) - len(out)), eop_x


def _trace_recipient(
    recip: str,
    post: NormalizedWorldEvent,
    f_ev: NormalizedWorldEvent | None,
    ctx_step: int | None,
    has_atif: bool,
    same_idx: Any,
    cross_idx: Any,
    last_mut_step: dict[str, int],
    actions_by_member: dict[str, list[NormalizedWorldEvent]],
    informed_dup_keys: set[tuple[str, int]],
    event_to_step: dict[tuple[str, int], int],
) -> dict[str, Any]:
    acted_on, act_ev = False, None
    if f_ev is not None:
        if has_atif and ctx_step is not None:
            acted_on = (
                has_downstream_action(
                    recip,
                    f_ev,
                    same_idx,
                    cross_idx,
                    min_step_gt=ctx_step,
                    allow_same_step_same_log=True,
                )
                or last_mut_step.get(recip, -1) > ctx_step
            )
            if acted_on:
                act_ev = _find_matching_action(
                    actions_by_member.get(recip, []),
                    f_ev,
                    informed_dup_keys,
                    event_to_step,
                    min_step_gt=ctx_step,
                    allow_same_step_same_log=True,
                )
        elif not has_atif:
            acted_on = has_downstream_action(recip, f_ev, same_idx, cross_idx)
            if acted_on:
                act_ev = _find_matching_action(
                    actions_by_member.get(recip, []),
                    f_ev,
                    informed_dup_keys,
                    event_to_step,
                    min_step_gt=None,
                    allow_same_step_same_log=False,
                )
    return {
        "msg_id": post.msg_id,
        "sender": post.actor,
        "recipient": recip,
        "post_seq": post.seq,
        "post_ts": post.ts,
        "fetch_seq": f_ev.seq if f_ev is not None else None,
        "fetch_ts": f_ev.ts if f_ev is not None else None,
        "in_context_step": ctx_step,
        "acted_on": acted_on,
        "act_seq": act_ev.seq if acted_on and act_ev is not None else None,
        "snippet": (post.text or "")[:100],
    }


def _collect_trace_indices(
    wlog: WorldEventLog,
    roster_set: set[str],
    obs_steps: dict[str, Any],
    event_to_step: dict[tuple[str, int], int],
) -> tuple[Any, ...]:
    in_play = [ev for ev in wlog.events if ev.phase != "post_play" and not ev.late]
    in_git = [ev for ev in wlog.git_events if ev.phase != "post_play" and not ev.late]
    acts = [
        ev
        for ev in (*in_play, *in_git)
        if ev.kind in ("world_action", "artifact_write") and ev.accepted and ev.actor in roster_set
    ]
    agroups = {log_group(ev.source_format) for ev in acts}
    if len(agroups) == 1 and next(iter(agroups)) != "bespoke_git_bundle":
        acts.sort(key=lambda ev: (ev.seq, ev.source_format))
    elif acts:
        acts.sort(
            key=lambda ev: (
                ev.ts_epoch is None,
                ev.ts_epoch or 0.0,
                log_group(ev.source_format),
                ev.seq,
            )
        )
    posts = [
        ev
        for ev in in_play
        if ev.kind == "message_post"
        and ev.accepted
        and ev.actor in roster_set
        and ev.msg_id is not None
    ]
    p_by_id = {
        (log_group(ev.source_format), ev.msg_id): ev for ev in posts if ev.msg_id is not None
    }
    ffetch: dict[tuple[str, str, str], NormalizedWorldEvent] = {}
    for ev in in_play:
        if ev.actor in roster_set and ev.accepted and ev.fetched_msg_ids:
            grp = log_group(ev.source_format)
            for mid in ev.fetched_msg_ids:
                fk = (ev.actor, grp, mid)
                if fk not in ffetch or happened_before(ev, ffetch[fk]):
                    ffetch[fk] = ev
    in_ctx = find_in_context_steps(p_by_id, ffetch, obs_steps, event_to_step)
    need_fr = bool(in_git) or any(ev.kind == "artifact_write" for ev in in_play)
    t_reads = extract_timed_atif_file_reads(obs_steps) if need_fr else []
    _, _, dup_keys, _ = compute_duplicate_actions(
        acts, posts, ffetch, in_play_events=in_play, timed_reads=t_reads
    )
    excl_steps = compute_excluded_mut_steps(wlog.events, wlog.git_events, dup_keys, event_to_step)
    last_mut = compute_last_mut_steps(obs_steps, excl_steps)
    by_mem: dict[str, list[NormalizedWorldEvent]] = defaultdict(list)
    for ev in acts:
        if ev.actor is not None:
            by_mem[ev.actor].append(ev)
    same_idx, cross_idx = build_action_suffix_index(by_mem, dup_keys, event_to_step)
    return posts, ffetch, in_ctx, same_idx, cross_idx, last_mut, by_mem, dup_keys


def _build_trial_timeline(
    trial: NormalizedMultiAgentTrial,
    wlog: WorldEventLog,
    event_to_step: dict[tuple[str, int], int],
    max_events: int = 300,
    max_traces: int = 120,
) -> dict[str, Any]:
    if type(max_events) is not int or max_events < 1:
        raise ValueError(f"max_events must be >= 1, got {max_events!r}")
    if type(max_traces) is not int or max_traces < 0:
        raise ValueError(f"max_traces must be >= 0, got {max_traces!r}")
    roster = [m.member_id for m in trial.members]
    roster_set = set(roster)
    member_by_id = {m.member_id: m for m in trial.members}
    obs_steps = {
        m.member_id: trial.member_steps[m.member_id]
        for m in trial.members
        if m.trajectory_status == "present" and trial.member_steps.get(m.member_id)
    }
    timeline_events, trunc_cnt, eop_x = _render_timeline_nodes(wlog, event_to_step, max_events)
    posts, ffetch, in_ctx, same_idx, cross_idx, last_mut, by_mem, dup_keys = _collect_trace_indices(
        wlog, roster_set, obs_steps, event_to_step
    )
    traces: list[dict[str, Any]] = []
    for post in posts:
        if post.actor is None or post.msg_id is None:
            continue
        p_grp = log_group(post.source_format)
        recips = (
            sorted({r for r in post.recipients if r in roster_set and r != post.actor})
            if post.recipients
            else [r for r in roster if r != post.actor]
        )
        for recip in recips:
            if len(traces) >= max_traces:
                break
            has_atif = member_by_id[recip].trajectory_status == "present" and bool(
                obs_steps.get(recip)
            )
            traces.append(
                _trace_recipient(
                    recip,
                    post,
                    ffetch.get((recip, p_grp, post.msg_id)),
                    in_ctx.get((recip, p_grp, post.msg_id)),
                    has_atif,
                    same_idx,
                    cross_idx,
                    last_mut,
                    by_mem,
                    dup_keys,
                    event_to_step,
                )
            )
    return {
        "events": timeline_events,
        "truncated_events": trunc_cnt,
        "end_of_play_x": eop_x,
        "message_traces": traces,
    }


def _dedupe_label(raw: str, used: set[str]) -> str:
    candidate = raw
    suffix = 2
    while candidate in used:
        assert suffix <= 10_000
        candidate = f"{raw} #{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _allocate_quotas(total: int, n_sources: int) -> list[int]:
    if n_sources <= 0 or total <= 0:
        return [0] * max(0, n_sources)
    base, rem = divmod(total, n_sources)
    return [base + (1 if idx < rem else 0) for idx in range(n_sources)]


DecompSpec = tuple[str, Path, Path, Path] | tuple[str, Path, Path, Path, Path | None]


def _normalize_decomp_spec(spec: DecompSpec) -> tuple[str, Path, Path, Path, Path | None]:
    if len(spec) == 4:
        return (spec[0], spec[1], spec[2], spec[3], None)
    return spec


def _gather_corpora_and_trials(
    disk_specs: list[tuple[str | None, Path]],
    bp_list: list[Path],
    quotas: list[int],
    max_events_per_trial: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    corpora, bespoke_entries, trials_json = (
        list[dict[str, Any]](),
        list[dict[str, Any]](),
        list[dict[str, Any]](),
    )
    used_labels: set[str] = set()
    for idx, (c_label, c_path) in enumerate(disk_specs):
        raw_lbl = sanitize_utf8_text(c_label or c_path.resolve().name)
        uniq_label = _dedupe_label(raw_lbl, used_labels)
        summary, c_trials = summarize_disk_corpus(
            c_path,
            label=uniq_label,
            trial_quota=quotas[idx],
            max_events_per_trial=max_events_per_trial,
            build_timeline=_build_trial_timeline,
        )
        corpora.append(summary)
        trials_json.extend(c_trials)
    for bp_idx, bp_path in enumerate(bp_list):
        bp_resolved = bp_path.resolve()
        uniq_bp_label = _dedupe_label(
            sanitize_utf8_text(f"Bespoke Public ({bp_resolved.stem})"), used_labels
        )
        bp_corpus, bp_entry, bp_trials = summarize_bespoke_public_source(
            bp_resolved,
            label=uniq_bp_label,
            trial_quota=min(12, quotas[len(disk_specs) + bp_idx]),
            max_events_per_trial=max_events_per_trial,
            build_timeline=_build_trial_timeline,
        )
        corpora.append(bp_corpus)
        bespoke_entries.append(bp_entry)
        trials_json.extend(bp_trials)
    return corpora, bespoke_entries, trials_json


def build_html_payload(
    path: Path,
    *,
    title: str | None = None,
    label: str | None = None,
    compare: Sequence[tuple[str | None, Path]] | None = None,
    decompositions: Sequence[DecompSpec] | None = None,
    decompose_task_key: str | None = None,
    bespoke_public: Sequence[Path] | None = None,
    max_trials: int = 100,
    max_events_per_trial: int = 300,
) -> dict[str, Any]:
    if type(max_trials) is not int or max_trials < 1:
        raise ValueError(f"max_trials must be >= 1, got {max_trials!r}")
    if type(max_events_per_trial) is not int or max_events_per_trial < 1:
        raise ValueError(f"max_events_per_trial must be >= 1, got {max_events_per_trial!r}")
    resolved = path.resolve()
    disk_specs: list[tuple[str | None, Path]] = [(label, resolved), *(compare or ())]
    bp_list = list(bespoke_public or ())
    quotas = _allocate_quotas(max_trials, len(disk_specs) + len(bp_list))
    corpora, bespoke_entries, trials_json = _gather_corpora_and_trials(
        disk_specs, bp_list, quotas, max_events_per_trial
    )

    decomp_entries = [
        summarize_decomposition_spec(
            sanitize_utf8_text(d_label),
            solo,
            silent,
            team,
            equal_budget_solo=eb_solo,
            task_key_regex=decompose_task_key,
        )
        for d_label, solo, silent, team, eb_solo in (
            _normalize_decomp_spec(s) for s in (decompositions or ())
        )
    ]
    overview = merge_corpus_overviews(corpora)
    total_trials = sum(int(c["n_trials"]) for c in corpora)
    schema = (
        "evallab.seat-html-report/v2"
        if (compare or decompositions or bespoke_public)
        else "evallab.seat-html-report/v1"
    )
    clean_title = sanitize_utf8_text(title or f"Eval Lab Multi-Agent Forensics: {resolved.name}")
    return sanitize_json_value(
        {
            "schema_version": schema,
            "title": clean_title,
            "target_path": sanitize_utf8_text(resolved.as_posix()),
            "n_trials": total_trials,
            "overview": overview,
            "corpora": corpora,
            "decompositions": decomp_entries,
            "bespoke_public": bespoke_entries,
            "trials": trials_json,
        }
    )


def render_seat_html_report(
    path: Path,
    *,
    title: str | None = None,
    label: str | None = None,
    compare: Sequence[tuple[str | None, Path]] | None = None,
    decompositions: Sequence[DecompSpec] | None = None,
    decompose_task_key: str | None = None,
    bespoke_public: Sequence[Path] | None = None,
    max_trials: int = 100,
    max_events_per_trial: int = 300,
) -> str:
    payload = sanitize_json_value(
        build_html_payload(
            path,
            title=title,
            label=label,
            compare=compare,
            decompositions=decompositions,
            decompose_task_key=decompose_task_key,
            bespoke_public=bespoke_public,
            max_trials=max_trials,
            max_events_per_trial=max_events_per_trial,
        )
    )
    payload_json = json.dumps(payload, allow_nan=False, sort_keys=True, separators=(",", ":"))
    clean_title = sanitize_utf8_text(str(payload.get("title") or ""))
    return sanitize_utf8_text(build_html_document(clean_title, payload_json))


def write_seat_html_report(
    path: Path,
    out_html: Path,
    *,
    title: str | None = None,
    label: str | None = None,
    compare: Sequence[tuple[str | None, Path]] | None = None,
    decompositions: Sequence[DecompSpec] | None = None,
    decompose_task_key: str | None = None,
    bespoke_public: Sequence[Path] | None = None,
    max_trials: int = 100,
) -> Path:
    html = sanitize_utf8_text(
        render_seat_html_report(
            path,
            title=title,
            label=label,
            compare=compare,
            decompositions=decompositions,
            decompose_task_key=decompose_task_key,
            bespoke_public=bespoke_public,
            max_trials=max_trials,
        )
    )
    dest = out_html.expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(html, encoding="utf-8")
    return dest


def extract_inline_js(html: str) -> str:
    matches = _SCRIPT_RE.findall(html)
    if not matches:
        raise ValueError("HTML report contains no inline <script> block")
    return matches[-1]
