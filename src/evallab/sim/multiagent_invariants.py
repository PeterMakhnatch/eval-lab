from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from evallab.evidence._forensics_metrics import log_group
from evallab.evidence.bespoke_public_rollout import analyze_bespoke_public_package
from evallab.evidence.multiagent_adapter import (
    NormalizedMultiAgentTrial,
    parse_timestamp_epoch,
    read_multiagent_trial,
)
from evallab.evidence.seat_decompose import decompose_coordination
from evallab.evidence.seat_forensics import TrialForensicsReport, analyze_trial_forensics
from evallab.evidence.seat_html_report import extract_inline_js, render_seat_html_report
from evallab.evidence.seat_tables import export_forensics_tables
from evallab.evidence.seat_task_lint import BatchTaskLintReport, TaskLintReport, lint_task_path
from evallab.evidence.seatlab_export import SEATLAB_SCHEMA, export_seatlab_tape
from evallab.evidence.world_events import WorldEventLog, load_world_events
from evallab.seat_parser import _parse_labeled_path, _positive_int, register_parser
from evallab.sim.engine import Simulation

_FORBIDDEN_VIEW_KEYS = frozenset({"reward", "rewards", "verifier_result", "score"})
_CDN_TAG_RE = re.compile(r"<(?:script|link)\b[^>]+(?:src|href)\s*=\s*['\"]https?://", re.IGNORECASE)
_SEATLAB_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}")
_SEATLAB_ROLE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}")
_SEATLAB_FMT_RE = re.compile(r":([a-z0-9_]+):seq\d+(?:-\d+)?$")
_HTML_DATA_RE = re.compile(
    r'<script id="forensics-data" type="application/json">\s*(.*?)\s*</script>',
    re.S,
)


def _reject_nonfinite(token: str) -> Any:
    raise ValueError(f"non-finite JSON constant: {token}")


def _strict_json_roundtrip(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, allow_nan=False)
    json.loads(raw, parse_constant=_reject_nonfinite)
    return raw


def _contains_key(obj: Any, forbidden: frozenset[str]) -> tuple[bool, bool]:
    stack: list[Any] = [obj]
    steps = 0
    while stack and steps < 50_000:
        steps += 1
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if str(k).lower() in forbidden:
                    return True, False
                stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return False, bool(stack)


def _check_forensics_report_invariants(
    sim: Simulation, rep1: TrialForensicsReport, wlog: WorldEventLog
) -> None:
    f = rep1.funnel
    sim.require(
        f.messages_fetched + f.never_fetched == f.message_delivery_pairs,
        "funnel_fetched_plus_never_eq_pairs",
    )
    sim.require(0 <= f.messages_fetched <= f.message_delivery_pairs, "funnel_fetched_le_pairs")
    sim.require(0 <= f.messages_in_context <= f.messages_fetched, "funnel_in_ctx_le_fetched")
    sim.require(
        0 <= f.unique_messages_fetched <= f.messages_posted,
        "funnel_unique_fetched_le_posted",
    )
    for mid, mrep in rep1.members.items():
        mf = mrep.funnel
        sim.require(
            mf.messages_fetched + mf.never_fetched == mf.message_delivery_pairs,
            f"member_funnel_pairs_{mid}",
        )
        sim.require(0 <= mf.messages_in_context <= mf.messages_fetched, f"member_in_ctx_{mid}")
        if mf.context_observable:
            sim.require(
                0 <= mf.messages_acted_on <= mf.messages_in_context,
                f"member_acted_le_in_ctx_{mid}",
            )
    sim.require(
        rep1.collisions.stale_read_write_races
        == rep1.failure_mode_counts.get("stale_read_overwrite", 0),
        "stale_races_match_episodes",
    )
    sim.require(
        rep1.collisions.post_end_of_play_events
        == rep1.failure_mode_counts.get("post_end_of_play_mutation", 0),
        "post_eop_match_episodes",
    )
    h1 = rep1.herding
    sim.require(
        h1.unreported_duplicate_actions
        + h1.inflight_duplicate_actions
        + h1.informed_msg_duplicate_actions
        + h1.informed_state_duplicate_actions
        == rep1.collisions.duplicate_target_actions,
        "herding_dup_partition_exact",
    )
    sim.require(
        0 <= h1.post_then_read_actions <= h1.post_and_read_actions,
        "herding_protocol_monotonic",
    )
    ev_by_key = {(ev.source_format, ev.seq): ev for ev in (*wlog.events, *wlog.git_events)}
    for ep in rep1.failure_episodes:
        sim.require(bool(ep.episode_id and ep.summary), "episode_nonempty")
        if ep.kind == "post_end_of_play_mutation" and ep.source_format and ep.event_seq is not None:
            ev = ev_by_key.get((ep.source_format, ep.event_seq))
            if ev is not None:
                sim.require(
                    ev.kind in ("world_action", "artifact_write", "message_post") and ev.accepted,
                    "post_eop_only_accepted_mutation",
                )


def _build_seatlab_fetch_maps(
    wlog: WorldEventLog,
) -> tuple[
    set[tuple[str, int]],
    dict[tuple[str, str, str], int],
    dict[tuple[str, str, str], float | None],
]:
    post_play_keys: set[tuple[str, int]] = {
        (ev.source_format, ev.seq)
        for ev in (*wlog.events, *wlog.git_events)
        if ev.phase == "post_play" or ev.late
    }
    first_fetch_seq: dict[tuple[str, str, str], int] = {}
    first_fetch_ts: dict[tuple[str, str, str], float | None] = {}
    for ev in wlog.events:
        if ev.phase == "post_play" or ev.late:
            continue
        if ev.accepted and ev.actor is not None and ev.fetched_msg_ids:
            grp = log_group(ev.source_format)
            for mid in ev.fetched_msg_ids:
                fkey = (ev.actor, grp, mid)
                cur = first_fetch_seq.get(fkey)
                if cur is None or ev.seq < cur:
                    first_fetch_seq[fkey] = ev.seq
                if fkey not in first_fetch_ts:
                    first_fetch_ts[fkey] = ev.ts_epoch
                elif ev.ts_epoch is not None:
                    cur_ts = first_fetch_ts[fkey]
                    if cur_ts is None or ev.ts_epoch < cur_ts:
                        first_fetch_ts[fkey] = ev.ts_epoch
    return post_play_keys, first_fetch_seq, first_fetch_ts


def _check_seatlab_view_causality(
    sim: Simulation,
    trial: NormalizedMultiAgentTrial,
    view: dict[str, Any],
    dseq: int,
    dts: float | None,
    seat: str,
    dgrp: str,
    first_fetch_seq: dict[tuple[str, str, str], int],
    first_fetch_ts: dict[tuple[str, str, str], float | None],
) -> None:
    for msg in view.get("received_messages") or []:
        canon_sender = trial.member_aliases.get(str(msg["sender"]), str(msg["sender"]))
        sim.require(canon_sender != seat, "seatlab_no_self_msg")
        if trial.ecosystem == "olam_seats":
            sim.require(msg["post_seq"] < dseq, "seatlab_no_future_msg")
            sim.require(msg["fetch_seq"] < dseq, "seatlab_no_unfetched_msg")
        else:
            mts = parse_timestamp_epoch(msg.get("ts"))
            if dts is not None and mts is not None:
                sim.require(mts < dts, "seatlab_bespoke_no_future_msg")
            bf_key = (seat, "bespoke_board", str(msg["msg_id"]))
            sim.require(bf_key in first_fetch_ts, "seatlab_bespoke_msg_was_fetched")
            fts = first_fetch_ts.get(bf_key)
            if dts is not None and fts is not None:
                sim.require(fts < dts, "seatlab_bespoke_fetch_before_decision")
        fseq = first_fetch_seq.get((seat, dgrp, str(msg["msg_id"])))
        if trial.ecosystem == "olam_seats":
            sim.require(fseq is not None and fseq < dseq, "seatlab_fetch_before_decision")
    for pa in view.get("prior_self_actions") or []:
        if trial.ecosystem == "olam_seats":
            sim.require(pa["seq"] < dseq, "seatlab_no_future_action")
        elif dts is not None:
            ats = parse_timestamp_epoch(pa.get("ts"))
            if ats is not None:
                sim.require(ats < dts, "seatlab_bespoke_no_future_action")
    for pr in view.get("prior_self_reads") or []:
        if trial.ecosystem == "olam_seats":
            sim.require(pr["seq"] < dseq, "seatlab_no_future_read")
        elif dts is not None:
            rts = parse_timestamp_epoch(pr.get("ts"))
            if rts is not None:
                sim.require(rts < dts, "seatlab_bespoke_no_future_read")
    for obs in view.get("prior_atif_observations") or []:
        if dts is not None:
            ots = parse_timestamp_epoch(obs.get("ts"))
            if ots is not None:
                sim.require(ots < dts, "seatlab_no_future_atif_obs")


def _check_seatlab_tape_invariants(
    sim: Simulation,
    trial: NormalizedMultiAgentTrial,
    wlog: WorldEventLog,
    trial_dir: Path,
    scratch: Path,
    canary_secret: str,
) -> None:
    sl1 = scratch / f"{trial_dir.name}-sl1.jsonl"
    sl2 = scratch / f"{trial_dir.name}-sl2.jsonl"
    export_seatlab_tape(trial_dir, sl1, include_messages=False)
    export_seatlab_tape(trial_dir, sl2, include_messages=False)
    b1, b2 = sl1.read_bytes(), sl2.read_bytes()
    sim.require(b1 == b2, "seatlab_byte_identical")
    sim.require(canary_secret.encode() not in b1, "seatlab_symlink_isolation")
    post_play_keys, first_fetch_seq, first_fetch_ts = _build_seatlab_fetch_maps(wlog)
    sim.require(
        not any(c.subject == "feat a" and "feat-b" in c.branches for c in wlog.git_commits),
        "git_no_nonff_merge_branch_leak",
    )
    for line in b1.decode("utf-8").split("\n"):
        if not line.strip():
            continue
        rec = json.loads(line, parse_constant=_reject_nonfinite)
        sim.require(rec.get("schema") == SEATLAB_SCHEMA, "seatlab_schema")
        sim.require(
            isinstance(rec.get("key"), str) and _SEATLAB_KEY_RE.fullmatch(rec["key"]) is not None,
            "seatlab_key_wire_contract",
        )
        sim.require(
            isinstance(rec.get("role"), str)
            and _SEATLAB_ROLE_RE.fullmatch(rec["role"]) is not None,
            "seatlab_role_wire_contract",
        )
        sim.require("SECRET_REWARD_999" not in line, "seatlab_json_reward_scrubbed")
        view = rec["view"]
        expected_sha = hashlib.sha256(
            json.dumps(
                view, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
            ).encode("ascii")
        ).hexdigest()
        sim.require(rec.get("view_sha256") == expected_sha, "seatlab_view_sha256_wire_contract")
        has_forbidden, truncated = _contains_key(view, _FORBIDDEN_VIEW_KEYS)
        sim.require(not truncated, "seatlab_view_scan_complete")
        sim.require(not has_forbidden, "seatlab_no_reward_leak")
        dseq = view["decision_seq"]
        dts = parse_timestamp_epoch(view.get("decision_ts"))
        seat = view["canonical_seat"]
        fmt_m = _SEATLAB_FMT_RE.search(rec["key"])
        src_fmt = fmt_m.group(1) if fmt_m is not None else ""
        sim.require((src_fmt, dseq) not in post_play_keys, "seatlab_no_post_play_decision")
        dgrp = log_group(src_fmt) if src_fmt else ""
        _check_seatlab_view_causality(
            sim, trial, view, dseq, dts, seat, dgrp, first_fetch_seq, first_fetch_ts
        )


def _check_trial_html_invariants(
    sim: Simulation,
    rep1: TrialForensicsReport,
    trial_dir: Path,
    canary_secret: str,
) -> None:
    h1 = render_seat_html_report(trial_dir)
    h2 = render_seat_html_report(trial_dir)
    sim.require(h1 == h2, "html_byte_identical")
    sim.require(_CDN_TAG_RE.search(h1) is None, "html_zero_cdn")
    sim.require(canary_secret not in h1, "html_symlink_isolation")
    sim.require('<img src=x onerror="alert(1)">' not in h1, "html_xss_escaped")
    js = extract_inline_js(h1)
    sim.require(len(js) > 100 and "</script>" not in js.lower(), "html_inline_js_intact")
    hm = _HTML_DATA_RE.search(h1)
    sim.require(hm is not None, "html_payload_script_present")
    if hm is not None:
        hpayload = json.loads(hm.group(1), parse_constant=_reject_nonfinite)
        htrials = hpayload.get("trials") or []
        sim.require(len(htrials) == 1, "html_single_trial_embedded")
        if htrials:
            tl = htrials[0].get("timeline") or {}
            tl_evs = tl.get("events") or []
            for idx in range(1, len(tl_evs)):
                sim.require(
                    tl_evs[idx]["x_pos"] >= tl_evs[idx - 1]["x_pos"],
                    "html_timeline_xpos_monotonic",
                )
            traces = tl.get("message_traces") or []
            for tr in traces:
                if not tr["acted_on"]:
                    sim.require(tr["act_seq"] is None, "html_trace_no_act_seq_when_unacted")
            if rep1.funnel.message_delivery_pairs <= 120:
                sim.require(
                    len(traces) == rep1.funnel.message_delivery_pairs,
                    "html_trace_count_parity",
                )
                sim.require(
                    sum(1 for tr in traces if tr["acted_on"]) == rep1.funnel.messages_acted_on,
                    "html_trace_acted_parity",
                )
                sim.require(
                    sum(1 for tr in traces if tr["fetch_seq"] is not None)
                    == rep1.funnel.messages_fetched,
                    "html_trace_fetched_parity",
                )
                sim.require(
                    sum(1 for tr in traces if tr["in_context_step"] is not None)
                    == rep1.funnel.messages_in_context,
                    "html_trace_in_ctx_parity",
                )
        hcorp = (hpayload.get("corpora") or [{}])[0]
        if rep1.total_cost_usd is None and rep1.known_cost_usd is None:
            sim.require(hcorp.get("known_cost_usd") is None, "html_unknown_cost_is_null")


def _check_trial_tables_invariants(
    sim: Simulation,
    trial_dir: Path,
    scratch: Path,
    canary_secret: str,
    *,
    export_parquet: bool,
) -> None:
    t_out1 = scratch / f"{trial_dir.name}-tbl1"
    t_out2 = scratch / f"{trial_dir.name}-tbl2"
    fmt = "parquet" if export_parquet else "jsonl"
    s1 = export_forensics_tables(trial_dir, t_out1, fmt=fmt)
    s2 = export_forensics_tables(trial_dir, t_out2, fmt=fmt)
    sim.require(s1.row_counts == s2.row_counts, "tables_row_counts_identical")
    sim.require(s1.sha256_by_table == s2.sha256_by_table, "tables_sha256_identical")
    for name in ("trials", "seats", "events", "causal_links", "failure_episodes"):
        p1 = t_out1 / f"{name}.{fmt}"
        p2 = t_out2 / f"{name}.{fmt}"
        sim.require(p1.is_file() and p2.is_file(), f"table_exists_{name}")
        raw1, raw2 = p1.read_bytes(), p2.read_bytes()
        sim.require(raw1 == raw2, f"table_byte_identical_{name}")
        sim.require(canary_secret.encode() not in raw1, f"table_symlink_isolation_{name}")
        if export_parquet:
            tbl = pq.read_table(p1)
            sim.require(tbl.num_rows == s1.row_counts[name], f"parquet_rows_{name}")
            sim.require("trial_path" in tbl.column_names, f"parquet_trial_path_{name}")


def check_trial_invariants(
    sim: Simulation,
    trial_dir: Path,
    scratch: Path,
    canary_secret: str,
    *,
    export_parquet: bool = False,
) -> TrialForensicsReport:
    sim.begin("forensics_trial", name=trial_dir.name)
    trial = read_multiagent_trial(trial_dir)
    roster = [m.member_id for m in trial.members]
    if trial.ecosystem == "bespoke_team" and trial.member_aliases:
        roster_set = set(roster)
        for alias_name, canonical_id in trial.member_aliases.items():
            if alias_name != canonical_id:
                sim.require(
                    alias_name not in roster_set,
                    f"bespoke_alias_not_duplicated_{alias_name}",
                )
    wlog = load_world_events(
        trial_dir,
        roster=roster,
        member_aliases=trial.member_aliases,
        agent_execution_finished_at=trial.agent_execution_finished_at,
    )
    rep1 = analyze_trial_forensics(trial_dir)
    rep2 = analyze_trial_forensics(trial_dir)
    j1 = _strict_json_roundtrip(rep1.model_dump(mode="json"))
    j2 = _strict_json_roundtrip(rep2.model_dump(mode="json"))
    sim.require(j1 == j2, "forensics_byte_identical")
    sim.require(canary_secret not in j1, "forensics_symlink_isolation")
    _check_forensics_report_invariants(sim, rep1, wlog)
    _check_seatlab_tape_invariants(sim, trial, wlog, trial_dir, scratch, canary_secret)
    _check_trial_html_invariants(sim, rep1, trial_dir, canary_secret)
    _check_trial_tables_invariants(
        sim, trial_dir, scratch, canary_secret, export_parquet=export_parquet
    )
    return rep1


def check_decompose_invariants(
    sim: Simulation, solo_dir: Path, silent_dir: Path, team_dir: Path
) -> None:
    sim.begin("decompose_arms")
    d1 = decompose_coordination(solo_dir, silent_dir, team_dir)
    d2 = decompose_coordination(solo_dir, silent_dir, team_dir)
    j1 = _strict_json_roundtrip(d1.model_dump(mode="json"))
    j2 = _strict_json_roundtrip(d2.model_dump(mode="json"))
    sim.require(j1 == j2, "decompose_byte_identical")
    eff = d1.effects
    hc = eff.get("headcount")
    co = eff.get("coordination")
    tot = eff.get("total")
    if hc and co and tot:
        sim.require(
            abs((hc.difference + co.difference) - tot.difference) <= 1e-5,
            "decompose_headcount_plus_coord_eq_total",
        )
    for e in d1.effects.values():
        sim.require(math.isfinite(e.difference), f"decompose_finite_{e.effect}")
        if e.p_signflip is not None:
            sim.require(0.0 <= e.p_signflip <= 1.0, f"decompose_signflip_bounds_{e.effect}")
        if e.p_paired_t is not None:
            sim.require(0.0 <= e.p_paired_t <= 1.0, f"decompose_paired_t_bounds_{e.effect}")
        if e.ci_tasks is not None:
            lo, hi = e.ci_tasks
            sim.require(lo - 1e-6 <= e.difference <= hi + 1e-6, f"decompose_ci_contains_{e.effect}")


def check_task_lint_invariants(
    sim: Simulation,
    task_dir: Path,
    expected_errors: set[str] | None = None,
    canary_secret: str = "",
) -> None:
    sim.begin("lint_task", name=task_dir.name)
    r1 = lint_task_path(task_dir)
    r2 = lint_task_path(task_dir)
    j1 = _strict_json_roundtrip(r1.model_dump(mode="json"))
    j2 = _strict_json_roundtrip(r2.model_dump(mode="json"))
    sim.require(j1 == j2, "lint_byte_identical")
    if canary_secret:
        sim.require(canary_secret not in j1, "lint_symlink_isolation")
    if expected_errors is not None and isinstance(r1, TaskLintReport):
        actual_errors = {f.code for f in r1.findings if f.severity == "error"}
        sim.require(actual_errors == expected_errors, "lint_expected_hazards_exact")
        sim.require(r1.passed == (len(actual_errors) == 0), "lint_passed_matches_errors")
    elif isinstance(r1, BatchTaskLintReport):
        sim.require(
            r1.n_tasks == len(r1.tasks) == r1.n_passed + r1.n_failed,
            "lint_batch_counts_consistent",
        )
        sim.require(
            r1.n_failed == sum(1 for t in r1.tasks if not t.passed),
            "lint_batch_failed_matches",
        )


def check_bespoke_public_invariants(sim: Simulation, md_path: Path) -> None:
    sim.begin("bespoke_public")
    r1 = analyze_bespoke_public_package(md_path)
    r2 = analyze_bespoke_public_package(md_path)
    j1 = _strict_json_roundtrip(r1.model_dump(mode="json"))
    j2 = _strict_json_roundtrip(r2.model_dump(mode="json"))
    sim.require(j1 == j2, "bespoke_public_byte_identical")
    sim.require(r1.n_runs == 3 and not r1.parse_errors, "bespoke_public_parse_clean")
    for d in r1.decompositions:
        if (
            d.headcount_gain is not None
            and d.coordination_gain is not None
            and d.total_gain is not None
        ):
            sim.require(
                abs((d.headcount_gain + d.coordination_gain) - d.total_gain) <= 1e-5,
                "bespoke_public_identity",
            )


def check_executive_dashboard_invariants(
    sim: Simulation,
    primary_dir: Path,
    compare_dir: Path,
    solo_dir: Path,
    silent_dir: Path,
    team_dir: Path,
    bespoke_md: Path,
    canary_secret: str,
) -> None:
    sim.begin("executive_dashboard")
    xss_tag = '<img src=x onerror="alert(1)"></script><script>alert(2)</script>'
    h1 = render_seat_html_report(
        primary_dir,
        title=f"Executive {xss_tag}",
        label=f"Primary {xss_tag}",
        compare=[(f"Compare {xss_tag}", compare_dir)],
        decompositions=[(f"3-Arm {xss_tag}", solo_dir, silent_dir, team_dir)],
        bespoke_public=[bespoke_md],
        max_trials=20,
    )
    h2 = render_seat_html_report(
        primary_dir,
        title=f"Executive {xss_tag}",
        label=f"Primary {xss_tag}",
        compare=[(f"Compare {xss_tag}", compare_dir)],
        decompositions=[(f"3-Arm {xss_tag}", solo_dir, silent_dir, team_dir)],
        bespoke_public=[bespoke_md],
        max_trials=20,
    )
    sim.require(h1 == h2, "exec_html_byte_identical")
    sim.require(_CDN_TAG_RE.search(h1) is None, "exec_html_zero_cdn")
    sim.require(canary_secret not in h1, "exec_html_symlink_isolation")
    sim.require('<img src=x onerror="alert(1)">' not in h1, "exec_html_xss_escaped")
    sim.require("</script><script>alert(2)" not in h1.lower(), "exec_html_script_breakout_escaped")
    js = extract_inline_js(h1)
    sim.require(len(js) > 100 and "</script>" not in js.lower(), "exec_html_js_intact")
    m = _HTML_DATA_RE.search(h1)
    sim.require(m is not None, "exec_html_payload_script_present")
    if m is not None:
        payload = json.loads(m.group(1), parse_constant=_reject_nonfinite)
        sim.require(
            payload.get("schema_version") == "evallab.seat-html-report/v2", "exec_schema_v2"
        )
        corpora = payload.get("corpora") or []
        sim.require(len(corpora) == 3, "exec_three_corpora")
        sim.require(
            len({c.get("label") for c in corpora}) == len(corpora),
            "exec_unique_corpus_labels",
        )
        sim.require(len(payload.get("trials") or []) <= 20, "exec_respects_max_trials")
        ctx_obs = (payload.get("overview") or {}).get("funnel", {}).get("context_observable")
        sim.require(ctx_obs is None or isinstance(ctx_obs, bool), "exec_ctx_obs_is_bool")
        sim.require(
            payload.get("n_trials") == sum(int(c["n_trials"]) for c in corpora),
            "exec_total_trials_sum",
        )
        sim.require(len(payload.get("decompositions") or []) == 1, "exec_one_decomposition")
        sim.require(len(payload.get("bespoke_public") or []) == 1, "exec_one_bespoke_public")


def check_seat_cli_parser_invariants(sim: Simulation, root: Path, trial_dir: Path) -> None:
    import argparse

    sim.begin("seat_cli_parser")
    parser = argparse.ArgumentParser(prog="evallab")
    sub = parser.add_subparsers(dest="cmd", required=True)
    register_parser(sub)
    ns = parser.parse_args(
        [
            "seat",
            "report",
            str(trial_dir),
            "--html",
            str(root / "r.html"),
            "--compare",
            f"Olam / Runs={trial_dir}",
            "--max-trials",
            "5",
        ]
    )
    sim.require(ns.max_trials == 5 and ns.html == root / "r.html", "cli_report_bounds")
    lbl, pth = _parse_labeled_path(f"Olam / Runs={trial_dir}", root)
    sim.require(lbl == "Olam / Runs" and pth == trial_dir, "cli_labeled_path_slash")
    sim.require(_positive_int("42") == 42, "cli_positive_int")
