from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
from typing import Any

from evallab.evidence._bespoke_adapter import BESPOKE_BUNDLE_PATHS
from evallab.evidence._decompose_models import _u_stat_best_of_k_task
from evallab.evidence._decompose_perm import exact_mcnemar_p, stratified_permutation_p
from evallab.evidence._decompose_stats import (
    _safe_fmean,
    _t_central_p,
    exact_sign_flip_p,
    t_quantile,
    t_two_sided_p,
)
from evallab.evidence._forensics_models import (
    CollisionMetrics,
    JobForensicsReport,
    MessageFunnelMetrics,
    render_forensics_summary,
)
from evallab.evidence._forensics_rules import extract_id_tokens, normalize_repo_path
from evallab.evidence._forensics_shell import (
    extract_bash_read_paths,
    is_mutating_shell_cmd,
    strip_heredocs,
)
from evallab.evidence._forensics_sweep import sweep_unread_for_actor
from evallab.evidence._herding_models import aggregate_herding_summary, compute_mixed_motive_metrics
from evallab.evidence._multiagent_io import MemberRecord, MemberUsage, usage_mismatches
from evallab.evidence._olam_muster_detail import verify_chain_jsonl_file, verify_muster_hash_chain
from evallab.evidence._olam_world_parsers import parse_crucible_rows, parse_muster_world_rows
from evallab.evidence._seatlab_scrub import scrub_text
from evallab.evidence._task_lint_compose import _is_host_bind_mount, lint_compose_services
from evallab.evidence._task_lint_contracts import _check_main_seat_tokens_map
from evallab.evidence.harbor_seat_trajectories import _mismatches
from evallab.evidence.multiagent_adapter import NormalizedMultiAgentTrial, read_multiagent_trial
from evallab.evidence.seat_cda import (
    analyze_normalized_trial_cda,
    analyze_trial_cda,
    format_cda_json,
    render_cda_text,
)
from evallab.evidence.seat_forensics import analyze_normalized_trial
from evallab.evidence.world_events import (
    NormalizedWorldEvent,
    WorldEventLog,
    _parse_bespoke_board_rows,
    load_world_events,
)
from evallab.sim._multiagent_cda_html import check_cda_html_report_invariants
from evallab.sim._multiagent_r14_audit import check_r14_audit_regressions
from evallab.sim._multiagent_r15_hardening import check_r15_hardening
from evallab.sim.engine import Simulation


def _wev(
    seq: int, actor: str, kind: Any, fmt: Any = "bespoke_board_v2", **kw: Any
) -> NormalizedWorldEvent:
    return NormalizedWorldEvent(
        seq=seq, actor=actor, kind=kind, source_format=fmt, visibility="public", **kw
    )


def _c_row(
    seq: int, actor: str, et: str, vis: str, pay: dict[str, Any], rec: list[str] | None = None
) -> dict[str, Any]:
    d: dict[str, Any] = {"seq": seq, "actor": actor, "event_type": et, "visibility": vis}
    d["payload"] = pay
    return {**d, "recipients": rec} if rec is not None else d


_CRUCIBLE_FIXTURE_ROWS: list[dict[str, Any]] = [
    _c_row(2, "a", "chat_public", "public", {"message_seq": 1}),
    _c_row(3, "b", "board_post", "public", {"message_seq": 1}),
    _c_row(4, "a", "chat_dm", "dm:a,b", {"message_seq": 1}, ["b"]),
    _c_row(5, "c", "chat_dm", "dm:c,d", {"message_seq": 1}, ["d"]),
    _c_row(6, "b", "message_read", "world_audit", {"channel": "public", "msg_seqs": [1]}),
    _c_row(7, "b", "message_read", "world_audit", {"channel": "dm", "msg_seqs": [1]}),
    _c_row(8, "eve", "message_read", "world_audit", {"channel": "dm", "msg_seqs": [1]}),
    _c_row(9, "b", "message_read", "world_audit", {"channel": "board", "msg_seqs": [99]}),
    _c_row(10, "eve", "message_read", "world_audit", {"channel": "dm:a,b", "msg_seqs": [1]}),
    _c_row(11, "b", "message_read", "dm:b,a", {"msg_seqs": [1]}),
    _c_row(12, "a", "chat_dm", "public", {"to": ["b"], "message_seq": 2}),
]


def _check_scrub_and_stats(sim: Simulation, rng: Random) -> None:
    depth = rng.randint(1, 4)
    tok = f"SECRET_VAL_{rng.randint(1000, 9999)}"
    payload: Any = {"reward": tok, "ok": True}
    for lvl in range(depth):
        payload = {f"w{lvl}": json.dumps(payload)}
    scrubbed = scrub_text(f"LOG: {json.dumps(payload)}")
    sim.require(tok not in scrubbed and "[redacted]" in scrubbed, "scrub_nested_json_no_leak")
    for line in (
        f'LOG: {{"reward[0]": "{tok}", "ok": true}}',
        f"harbor.verifier result: {tok}",
        f'log: {{"reward\'s": "{tok}", "ok": 1}}',
        f'trial.rewards[0].value = "{tok}"',
        f'if (reward === "{tok}") pass',
        json.dumps({"wrap": json.dumps({"yaml": f"reward:\n  {tok}\nnext: ok"})}),
        json.dumps({"wrap": json.dumps(f'reward=\\\\"{tok}A\\"{tok}B\\\\" ok=1')}),
        f'LOG: {{"reward\\nkey": "{tok}", "ok": true}}',
        json.dumps({"yaml": f"reward:\r\n  {tok}\r\nnext: ok"}),
        f'LOG: {{"a": "{{reward=", "reward": "{tok}"}}',
    ):
        out = scrub_text(line)
        sim.require(tok not in out and "[redacted]" in out, "scrub_variants_no_leak")
    sim.require(scrub_text("a[b]" * 500, max_chars=2500) == "a[b]" * 500, "scrub_subscript_linear")

    p_lo = rng.choice([1e-17, 1e-20, 1e-30])
    df_val = rng.choice([1.0, 2.0, 10.0, 100.0, 1e7])
    q_lo = t_quantile(p_lo, df_val)
    sim.require(math.isfinite(q_lo) and q_lo < 0.0, "t_quantile_lower_tail_finite")
    p_rt = t_two_sided_p(q_lo, df_val)
    sim.require(abs(p_rt - 2.0 * p_lo) / (2.0 * p_lo) < 1e-11, "t_quantile_lower_tail_roundtrip")
    sim.require(t_two_sided_p(1e-161, 5e-324) == 1.0, "t_two_sided_subnormal_df")
    sim.require(math.isinf(t_quantile(0.55, 1e-4)), "t_quantile_tiny_df_inf")
    c_tiny = _t_central_p(1e-170, 1.0)
    sim.require(abs(c_tiny - (2.0 / math.pi) * 1e-170) < 1e-182, "t_central_tiny_asymptotic")
    sim.require(_safe_fmean([1e16 + 2.0, -1e16, -2.0]) == 0.0, "safe_fmean_exact_cancel")
    p_mid = 0.5 + rng.choice([1e-8, 1e-12, 1e-15])
    q_mid = t_quantile(p_mid, df_val)
    exp_c = 2.0 * (p_mid - 0.5)
    sim.require(abs(_t_central_p(q_mid, df_val) - exp_c) / exp_c < 1e-11, "t_quantile_central_rt")
    sim.require(exact_sign_flip_p([1e13, 1.0]) == (0.5, True), "sign_flip_wide_range_exact")
    sim.require(
        exact_sign_flip_p([0.1, 0.2, -0.3, 0.5]) == (10 / 16, True), "sign_flip_decimal_tie"
    )
    sim.require(
        exact_sign_flip_p([1.0, 0.1, 0.2, -0.3, 1e-7]) == (0.5, True), "sign_flip_r9_decimal_tie"
    )
    sim.require(
        exact_sign_flip_p([5e5, -5e5, 1.0, -0.999999999, 1.0, -0.999999999]) == (0.875, True),
        "sign_flip_r9_cancel_tie",
    )


def _check_shell_invariants(sim: Simulation) -> None:
    for mut_cmd in (
        "/usr/bin/env rm -rf /",
        "env -u PWD rm -rf /",
        "curl -H 'a>' -X 'POST' http://world/submit",
        r"echo $'it\'s'; rm -rf /",
        "echo ->out.txt",
        "cat <(rm -rf /)",
        "echo $((rm -rf /); echo ok)",
        "curl -sD headers.txt http://world/status",
        "curl -X POST http://world/submit http://world/done",
        "sort -o data.txt data.txt",
        "uniq in.txt out.txt",
        "git diff --output=patch.diff",
        'echo "${x:-"\'"}" ; rm -rf /',
        'echo pwned >&1"evil.txt"',
        "env -uCC ./exploit.sh",
        'sed -n "1,5w out.txt" in.txt',
        "curl -K post.cfg http://world/status",
        "git grep -O sh foo",
        "echo \"${x:-'$(rm -rf /)'}\"",
        "cat ${x:-<(rm -rf /)}",
        r"sed $'\x2d\x69' 's/a/b/' f.txt",
        "< echo rm -rf /",
        "sed -n '1{1w pwned\n}' f.txt",
        "curl -G -F 'k=v' http://world/upload",
        "git -c diff.external=sh diff",
        "env -P /bin:/tmp/echo rm -rf /",
        "rg --pre sh pat f.txt",
        "less -o pwned f.txt",
    ):
        sim.require(is_mutating_shell_cmd(mut_cmd), "shell_mutating_detected")
    for ro_cmd in (
        "curl -s -o /dev/null -w '%{http_code}' http://world/status",
        "sed -ne's/foo/bar/ip' app.py",
        r"echo foo \> bar",
        "echo $(( (1 + (2)) << 1 ))",
        "echo $(( 5 > 3 ))",
        "curl -o - http://world/status",
        "curl -G -d q=foo http://world/search",
        "echo hi >| /dev/null",
        "uniq -cf 1 input.txt",
        "git -c color.ui=never status",
    ):
        sim.require(not is_mutating_shell_cmd(ro_cmd), "shell_readonly_preserved")
    sim.require(strip_heredocs("$((" * 200) == "$((" * 200, "heredoc_unclosed_arith_linear")
    sim.require(
        extract_bash_read_paths("cat << 'EOF' > r.sh\ncat fake.py\nEOF\ncat real.py")
        == ["real.py"],
        "shell_heredoc_quoted_stripped",
    )
    sim.require(
        extract_bash_read_paths("grep 'a > src/app.py' src/app.py") == ["src/app.py"],
        "shell_quoted_redir_preserved",
    )
    sim.require(
        extract_bash_read_paths("cat <<A; cat <<B\ncat f1.py\nA\ncat f2.py\nB\ncat <real.py")
        == ["real.py"],
        "shell_multi_heredoc_and_inredir",
    )


def _check_crucible_and_board_invariants(sim: Simulation) -> None:
    c_rows = parse_crucible_rows(_CRUCIBLE_FIXTURE_ROWS)
    sim.require(c_rows[4]["fetched_msg_ids"] == ["2"], "crucible_public_no_board_clobber")
    sim.require(c_rows[5]["fetched_msg_ids"] == ["4"], "crucible_dm_actor_isolated")
    sim.require(c_rows[6]["fetched_msg_ids"] == ["dm:1"], "crucible_dm_outsider_blocked")
    sim.require(c_rows[7]["fetched_msg_ids"] == ["board:99"], "crucible_miss_namespaced")
    sim.require(c_rows[8]["fetched_msg_ids"] == ["dm:a,b:1"], "crucible_dm_explicit_blocked")
    sim.require(c_rows[9]["fetched_msg_ids"] == ["4"], "crucible_dm_reversed_resolved")
    sim.require(
        c_rows[10]["visibility"] == "direct" and c_rows[10]["recipients"] == ["b"],
        "crucible_dm_list_to_private",
    )
    b_events = _parse_bespoke_board_rows(
        [
            {"event": "post", "agent": "b", "seq": 5, "text": "hi"},
            {"event": "inbox", "agent": "a", "seqs": ["²", "9" * 5000, " 5 "]},
            {"event": "inbox", "agent": "a", "returned": 1},
        ]
    )
    sim.require(
        len(b_events) == 3
        and b_events[1].fetched_msg_ids == ["²", "9" * 5000, "5"]
        and b_events[2].fetched_msg_ids == [],
        "bespoke_inbox_nonascii_and_padded_seqs",
    )


def _check_usage_and_compose(sim: Simulation) -> None:
    sim.require(not _is_host_bind_mount(r"a:C:\data"), "compose_win_named_vol_ok")
    sim.require(not _is_host_bind_mount(r"C:\data"), "compose_win_anon_vol_ok")
    sim.require(_is_host_bind_mount(r"C:\host:${APP_DIR}"), "compose_win_interp_bind")
    codes: list[str] = []
    lint_compose_services(
        {
            "p1": {"image": "alpine", "network_mode": "service:main"},
            "Main": {"image": "alpine", "networks": ["net_Main"]},
        },
        ["p1", "Main"],
        "environment/docker-compose.yaml",
        lambda code, _sev, _cat, _msg, _path: codes.append(code),
    )
    sim.require("shared_inter_seat_network" not in codes, "compose_service_main_no_alias")

    big, um_i = 1 << 53, ["usage_mismatch:n_input_tokens"]
    for exp_dict, obs_dict, expected in (
        ({"n_input_tokens": big + 1}, {"total_prompt_tokens": float(big)}, um_i),
        ({"n_input_tokens": 10**400}, {"total_prompt_tokens": 1.0}, um_i),
        ({}, {"total_prompt_tokens": 10**400}, []),
        ({"n_input_tokens": 99999.5}, {"total_prompt_tokens": 10}, um_i),
        ({"n_input_tokens": True}, {"total_prompt_tokens": 1}, um_i),
        ({"cost_usd": 10**400}, {"total_cost_usd": 1.0}, ["usage_mismatch:cost_usd"]),
    ):
        sim.require(
            _mismatches(exp_dict, {"final_metrics": obs_dict}) == expected
            and usage_mismatches(exp_dict, obs_dict) == expected,
            "usage_mismatch_parity",
        )


def _check_forensics_r9(sim: Simulation) -> None:
    sim.require("42" in extract_id_tokens("id=seq=42"), "id_tokens_overlapping_prefix")
    sim.require(normalize_repo_path("./" * 500 + "a.py") == "a.py", "norm_repo_path_linear")
    post_ev = _wev(1, "a", "message_post", msg_id="m1", ts_epoch=10.0)
    fetch_ev = _wev(2, "b", "message_fetch", fetched_msg_ids=["m1"], ts_epoch=16.1)
    act_ev0 = _wev(2, "b", "world_action", "bespoke_git_bundle", ts_epoch=14.0)
    act_ev = _wev(3, "b", "world_action", "bespoke_git_bundle", ts_epoch=15.1)
    unread_map = sweep_unread_for_actor(
        "b",
        [act_ev0, act_ev],
        [(0, post_ev)],
        {("b", "bespoke_board", "m1"): fetch_ev},
    )
    sim.require(list(unread_map) == [id(act_ev0)], "sweep_ieee754_grace_boundary")
    empty_batch = JobForensicsReport(
        target_path="/tmp/t",
        n_trials=0,
        funnel=MessageFunnelMetrics(),
        collisions=CollisionMetrics(),
    )
    sim.require("logs_complete=0/0" in render_forensics_summary(empty_batch), "render_summary_ok")


def _check_r10_perm_herding_and_contracts(sim: Simulation) -> None:
    mc_p = exact_mcnemar_p([(0.0, 1.0)] * 6 + [(0.0, 0.0)] * 4)
    sim.require(mc_p is not None and abs(mc_p - 0.03125) < 1e-12, "mcnemar_exact_6_0")
    sim.require(exact_mcnemar_p([(0.5, 1.0)]) is None, "mcnemar_nonbinary_none")
    perm_p, perm_ex = stratified_permutation_p([([0.0], [1.0])] * 11 + [([0.0], [0.0])])
    sim.require(perm_ex and perm_p is not None and abs(perm_p - 1 / 1024) < 1e-12, "perm_exact_12")
    bok = _u_stat_best_of_k_task([0.0, 0.5, 1.0], 2)
    sim.require(abs(bok - (0.5 + 2.0) / 3.0) < 1e-12, "u_stat_bok_exact")

    r3_resp = {"box": 4, "room": 1, "flag": True, "team_opens": [{"seat": "p2", "box": 3}]}
    m_in = [
        (1, 100.0, "game", "GET", {}, {"pool": 30, "seats": 3, "your_need": 12}),
        (2, 101.0, "claim", "POST", {"need": 1}, {"id": 1, "from": "p1", "need": 1}),
        (3, 102.0, "open", "POST", {"box": 4}, r3_resp),
    ]
    mk = ("seq", "t", "kind", "method", "request", "response")
    m_rows = parse_muster_world_rows(
        [{**dict(zip(mk, x, strict=True)), "seat": "p1", "status": 200, "ok": True} for x in m_in]
    )[0]
    sim.require(m_rows[0]["detail"].get("private_need") == 12, "muster_detail_private_need")
    sim.require(m_rows[1]["detail"].get("claimed_need") == 1, "muster_detail_claimed_need")
    sim.require(m_rows[2]["detail"].get("team_open_boxes") == [3], "muster_detail_team_opens")

    tok_codes: list[str] = []
    _check_main_seat_tokens_map(
        '{"t1": "p1"}',
        ["p1", "p2"],
        {"p1": {"SEAT_TOKEN": "t1"}, "p2": {"SEAT_TOKEN": "t2"}},
        "environment/docker-compose.yaml",
        lambda code, _sev, _cat, _msg, _path: tok_codes.append(code),
    )
    sim.require("seat_tokens_map_mismatch" in tok_codes, "contract_seat_tokens_mismatch")


def _check_r10_redteam_stats(sim: Simulation) -> None:
    s1 = ([-0.95e308], [-0.95e308, 0.95e308])
    s2 = ([0.95e308], [0.95e308, -0.95e308])
    s3 = ([0.0], [1e307])
    p_ex, is_ex = stratified_permutation_p([s1, s2, s3])
    p_mc, is_mc = stratified_permutation_p([s1, s2] * 6 + [s3], seed=7, max_draws=64)
    sim.require(is_ex and p_ex is not None and 0.0 < p_ex <= 1.0, "perm_huge_scale_exact")
    sim.require((not is_mc) and p_mc is not None and 0.0 < p_mc <= 1.0, "perm_huge_scale_mc")
    sim.require(abs(_u_stat_best_of_k_task([1e6] * 7, 3) - 1e6) < 1e-6, "u_stat_large_scale")
    mm1 = compute_mixed_motive_metrics(["p1", "p2"], {"p1", "p2"}, [], {"p1": 1e200, "p2": 1e200})
    mm2 = compute_mixed_motive_metrics(["p1", "p2"], {"p1", "p2"}, [], {"p1": -1e308, "p2": 1e308})
    sim.require(mm1["seat_reward_evenness_jain"] == 1.0, "jain_huge_equal_rewards")
    sim.require(mm2["seat_reward_spread"] is None, "spread_overflow_none")


def _check_r10_redteam_automaton(sim: Simulation) -> None:
    b_evs = [
        _wev(1, "p1", "world_action", raw_kind="act", ts_epoch=100.0),
        _wev(2, "p1", "message_post", raw_kind="send", msg_id="1", ts_epoch=100.0),
        _wev(10**400, "p1", "message_fetch", raw_kind="inbox", fetched_msg_ids=["9"]),
    ]
    g_evs = [
        _wev(1, "p2", "artifact_write", "bespoke_git_bundle", raw_kind="git_commit", ts_epoch=200.0)
    ]
    zu = MemberUsage(cost_status="zero_no_model")
    ms = [MemberRecord(member_id=s, trajectory_status="missing", usage=zu) for s in ("p1", "p2")]
    tr = NormalizedMultiAgentTrial(
        trial_dir="", trial_name="rt", ecosystem="bespoke_team", members=ms, cost_complete=True
    )
    rep = analyze_normalized_trial(
        tr, WorldEventLog(event_log_present=True, events=b_evs, git_events=g_evs)
    )
    sim.require(rep.herding.post_then_read_actions == 0, "automaton_same_ts_seq_order")
    sim.require(aggregate_herding_summary([rep]).post_then_read_actions == 0, "herding_agg_clean")
    bl_q = {"seq": 1, "seat": "p1", "kind": "open", "ok": True, "request": {"box": 1, "room": 1}}
    bl_r = [{**bl_q, "response": {"budget_left": {"reward": 99, "opens": 8}}}]
    sim.require(
        parse_muster_world_rows(bl_r)[0][0]["detail"].get("budget_left") == {"opens": 8},
        "budget_left_scrubbed",
    )
    empty_tok: list[str] = []
    _check_main_seat_tokens_map(
        "{}", [], {}, "dc.yaml", lambda c, _s, _k, _m, _p: empty_tok.append(c)
    )
    sim.require("seat_tokens_map_mismatch" in empty_tok, "empty_seat_tokens_rejected")


def _sign_v2_rows(
    specs: list[tuple[str, str, str, int, dict[str, Any], dict[str, Any]]],
) -> list[dict[str, Any]]:
    prev, out = "0" * 64, list[dict[str, Any]]()
    for idx, (seat, kind, method, sc, req, resp) in enumerate(specs, start=1):
        ev = {"seq": idx, "t": 200.0 + idx, "seat": seat, "kind": kind, "method": method}
        ev.update(
            {"status": sc, "ok": sc == 200, "request": req, "response": resp, "prev_hash": prev}
        )
        canon = json.dumps(
            ev, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        prev = hashlib.sha256(canon.encode("utf-8")).hexdigest()
        out.append({**ev, "entry_hash": prev})
    return out


def _check_r11_kit_v2_and_cda(sim: Simulation, rng: Random) -> None:
    bc, bt = rng.randint(1, 5), rng.randint(11, 15)
    m1, m2 = f"claimed box {bc}. p2 please open box {bt}.", "do not open box 29."
    ub_err = {"error": "unread_messages", "unread_count": 1, "latest_msg_id": 1}
    ac_err = {"error": "already_claimed", "owner": "p1", "target": str(bc)}
    c_ok = {"target": str(bc), "owner": "p1", "used": False}
    m1_res = {"messages": [{"id": 1, "from": "p1", "text": m1}], "unread_count": 0, "read_up_to": 1}
    r1_pay = {"round": 1, "reveals": {"p1": {"reward": 99}}}
    rev_res = {"round": 2, "committed": [], "rounds": [r1_pay]}
    specs: list[tuple[str, str, str, int, dict[str, Any], dict[str, Any]]] = [
        ("p1", "claim", "POST", 200, {"box": bc}, c_ok),
        ("p1", "msg", "POST", 200, {"text": m1}, {"id": 1, "from": "p1", "text": m1}),
        ("p2", "open", "POST", 409, {"box": bt}, ub_err),
        ("p2", "msgs", "GET", 200, {}, m1_res),
        ("p2", "open", "POST", 200, {"box": bt}, {"box": bt, "room": 2, "flag": True}),
        ("p3", "open", "POST", 409, {"box": 21}, ub_err),
        ("p3", "claim", "POST", 409, {"box": bc}, ac_err),
        ("p2", "msg", "POST", 200, {"text": m2}, {"id": 2, "from": "p2", "text": m2}),
        ("p3", "msgs", "GET", 200, {}, {"read_up_to": 2, "unread_count": 0}),
        ("p3", "open", "POST", 200, {"box": 29}, {"box": 29, "room": 3, "flag": False}),
        ("p1", "commit", "POST", 200, {"payload": {"reward": 99}}, {"round": 1, "revealed": True}),
        ("p1", "reveals", "GET", 200, {}, rev_res),
        ("p1", "release", "POST", 200, {"box": bc}, {"released": str(bc)}),
    ]
    raw_v2 = _sign_v2_rows(specs)
    pr = parse_muster_world_rows(raw_v2)[0]
    sim.require(all(r["detail"].get("hash_chain_valid") is True for r in pr), "v2_chain_valid")
    sim.require(pr[0]["detail"].get("claim_granted") is True, "v2_claim_granted")
    d2, d6 = pr[2]["detail"], pr[6]["detail"]
    ub_ok = d2.get("guard_blocked") == "unread_messages" and d2.get("unread_count") == 1
    cur_ok = pr[3]["detail"].get("read_up_to") == 1 and pr[8]["fetched_msg_ids"] == ["1", "2"]
    ac_ok = d6.get("guard_blocked") == "already_claimed" and d6.get("claim_owner") == "p1"
    cr_ok = (
        pr[10]["detail"].get("revealed") is True and pr[11]["detail"].get("completed_rounds") == 1
    )
    sim.require(ub_ok and cur_ok and ac_ok and cr_ok, "v2_guard_cursor_reveal")
    sim.require(pr[12]["detail"].get("claim_released") is True, "v2_claim_released")
    zu = MemberUsage(cost_status="zero_no_model")
    ms = [
        MemberRecord(member_id=s, trajectory_status="missing", usage=zu) for s in ("p1", "p2", "p3")
    ]
    tr3 = NormalizedMultiAgentTrial(
        trial_dir="", trial_name="v2", ecosystem="olam_seats", members=ms
    )
    w_evs = [NormalizedWorldEvent.model_validate(r) for r in pr]
    cda = analyze_normalized_trial_cda(tr3, WorldEventLog(event_log_present=True, events=w_evs))
    inf_t = (cda.avoided_duplicate_targets, cda.triggered_actions, cda.ignored_messages)
    sim.require(inf_t == (1, 1, 1), "cda_influence_counts")
    sim.require(cda.sender_causal_credit == {"p1": 1.0, "p2": 0.0, "p3": 0.0}, "cda_credit_norm")
    gi = cda.guard_impact
    g_t = (gi.claim_locks_granted, gi.claim_rejections_saved_dups, gi.unread_blocks_count)
    c_t = (gi.hash_chain_checked_trials, gi.hash_chain_valid_trials, gi.hash_chain_tampered_trials)
    gc_ok = g_t == (1, 1, 2) and gi.post_block_fetch_compliance_rate == 0.5 and c_t == (1, 1, 0)
    sim.require(gc_ok, "cda_guard_and_chain")
    j_ok = '"schema_version": "evallab.seat_cda.trial/v1"' in format_cda_json(cda)
    sim.require(j_ok and "compliance=0.500" in render_cda_text(cda), "cda_render_outputs")
    tamp_raw = [dict(r, response=dict(r["response"])) for r in raw_v2]
    tamp_raw[4]["response"]["flag"] = False
    tr_t = parse_muster_world_rows(tamp_raw)[0]
    tail_bad = all(r["detail"]["hash_chain_valid"] is False for r in tr_t[4:])
    sim.require(tr_t[3]["detail"]["hash_chain_valid"] is True and tail_bad, "v2_tamper_pinpoint")
    t_evs = [NormalizedWorldEvent.model_validate(r) for r in tr_t]
    tgi = analyze_normalized_trial_cda(
        tr3, WorldEventLog(event_log_present=True, events=t_evs)
    ).guard_impact
    sim.require(
        (tgi.hash_chain_checked_trials, tgi.hash_chain_valid_trials, tgi.hash_chain_tampered_trials)
        == (1, 0, 1),
        "cda_tampered_trial_detected",
    )


def _check_r12_chain_and_bespoke_paths(sim: Simulation) -> None:
    sim.require(("team.bundle",) in BESPOKE_BUNDLE_PATHS, "bespoke_root_bundle_path")
    r3 = _sign_v2_rows([("p1", "open", "POST", 200, {"box": i}, {"box": i}) for i in (1, 2, 3)])
    up_r = {**r3[0], "entry_hash": str(r3[0]["entry_hash"]).upper()}
    hm_r = {**r3[0], "hmac": "Z" * 64}
    sim.require(verify_muster_hash_chain([up_r]) == [False], "chain_rejects_upper_hex")
    sim.require(verify_muster_hash_chain([hm_r]) == [False], "chain_rejects_bad_hmac")
    with TemporaryDirectory() as tmp:
        tp = Path(tmp)
        fp3, fp9 = tp / "c3.jsonl", tp / "c9.jsonl"
        fp3.write_text("\n".join(json.dumps(r) for r in r3) + "\n", encoding="utf-8")
        sim.require(verify_chain_jsonl_file(fp3, max_lines=2) is False, "chain_cap_fail_closed")
        sim.require(verify_chain_jsonl_file(fp3, max_lines=5) is True, "chain_within_cap_ok")
        s8 = [{"seq": i, "seat": "p1", "kind": "open", "ok": True} for i in range(1, 9)]
        fp9.write_text("\n".join(json.dumps(r) for r in [*s8, r3[0]]) + "\n", encoding="utf-8")
        sim.require(verify_chain_jsonl_file(fp9) is False, "chain_scans_past_line_8")
        td = tp / "btrial"
        nb = td / "artifacts" / "logs" / "artifacts" / "multi-agent-harness"
        nb.mkdir(parents=True)
        (td / "team.bundle").write_bytes(b"BUNDLE")
        meta = {"orchestration": "async", "agents": {"agent-0": {}, "agent-1": {}}}
        res = {"trial_name": "btrial", "task_name": "t", "agent_result": {"metadata": meta}}
        (td / "result.json").write_text(json.dumps(res), encoding="utf-8")
        b_rows = [
            {"ts": 1.0, "event": "guard_claim_granted", "agent": "agent-0", "prefix": "src/m.py"},
            {"ts": 2.0, "event": "send", "seq": 1, "agent": "agent-0", "text": "claiming src/m.py"},
            {"ts": 3.0, "event": "guard_claim_released", "agent": "agent-0", "prefix": "src/m.py"},
            {"ts": 4.0, "event": "send", "seq": 2, "agent": "agent-0", "text": "released src/m.py"},
            {"ts": 5.0, "event": "inbox", "agent": "agent-1", "seqs": [1, 2]},
            {"ts": 6.0, "event": "guard_claim_granted", "agent": "agent-1", "prefix": "src/m.py"},
        ]
        (nb / "board-events.jsonl").write_text(
            "\n".join(json.dumps(x) for x in b_rows) + "\n", encoding="utf-8"
        )
        bonly = tp / "bonly"
        bonly.mkdir()
        (bonly / "team.bundle").write_bytes(b"BUNDLE")
        b_det = read_multiagent_trial(bonly).ecosystem == "bespoke_team"
        sim.require(b_det and load_world_events(bonly).git_bundle_present, "bespoke_root_bundle")
        brep = analyze_trial_cda(td)
        b_ok = brep.guard_impact.claim_locks_granted == 2 and brep.ignored_messages == 0
        sim.require(b_ok and brep.triggered_actions == 1, "bespoke_nested_board_cda")


def _check_r12_cda_guard_and_nop(sim: Simulation) -> None:
    zu = MemberUsage(cost_status="zero_no_model")
    ms = [MemberRecord(member_id=s, trajectory_status="missing", usage=zu) for s in ("p1", "p2")]
    tr2 = NormalizedMultiAgentTrial(
        trial_dir="", trial_name="r12", ecosystem="bespoke_team", members=ms
    )
    evs_g = [
        _wev(
            1, "p2", "policy_refusal", raw_kind="guard_unread_block", accepted=False, ts_epoch=10.0
        ),
        _wev(2, "p2", "message_fetch", raw_kind="inbox", fetched_msg_ids=["1"], ts_epoch=12.0),
    ]
    git_g = [
        _wev(
            1,
            "p2",
            "artifact_write",
            "bespoke_git_bundle",
            raw_kind="git_commit",
            files=["a.py"],
            ts_epoch=11.0,
        )
    ]
    g_rep = analyze_normalized_trial_cda(
        tr2, WorldEventLog(event_log_present=True, events=evs_g, git_events=git_g)
    )
    sim.require(
        g_rep.guard_impact.post_block_fetch_compliance_rate == 0.0,
        "cda_intervening_git_noncompliant",
    )
    m7 = {"id": 1, "from": "p1", "text": "claimed box 7"}
    nop_specs: list[tuple[str, str, str, int, dict[str, Any], dict[str, Any]]] = [
        ("p1", "claim", "POST", 200, {"box": 7}, {"target": "7", "owner": "p1", "used": False}),
        ("p1", "msg", "POST", 200, {"text": "claimed box 7"}, m7),
        ("p2", "msgs", "GET", 200, {}, {"read_up_to": 1, "unread_count": 0}),
    ]
    nop_rows = parse_muster_world_rows(_sign_v2_rows(nop_specs))[0]
    nop_evs = [NormalizedWorldEvent.model_validate(r) for r in nop_rows]
    nop_rep = analyze_normalized_trial_cda(
        tr2, WorldEventLog(event_log_present=True, events=nop_evs)
    )
    nop_ok = nop_rep.avoided_duplicate_targets == 0 and nop_rep.edges[0].outcome == "neutral"
    sim.require(
        nop_ok and nop_rep.sender_causal_credit["p1"] == 0.0, "cda_idle_nop_no_avoided_credit"
    )


def _check_r13_eight_seat_cda(sim: Simulation) -> None:
    zu, ids = MemberUsage(cost_status="zero_no_model"), [f"p{i}" for i in range(1, 9)]
    ms = [MemberRecord(member_id=s, trajectory_status="missing", usage=zu) for s in ids]
    tr8 = NormalizedMultiAgentTrial(
        trial_dir="", trial_name="r", ecosystem="olam_seats", members=ms
    )
    m1 = "I claimed/opened box 1, p2 open boxes 101-160, p8 open box 51 (p7/p8 check)"
    m2 = '{"room":6,"box":51,"flag":false,"note":"claimed/opened"}'
    specs8: list[tuple[str, str, str, int, dict[str, Any], dict[str, Any]]] = [
        ("p1", "open", "POST", 200, {"box": 1}, {"box": 1, "room": 1, "flag": True}),
        ("p1", "msg", "POST", 200, {"text": m1}, {"id": 1, "from": "p1", "text": m1}),
        ("p2", "msgs", "GET", 200, {}, {"read_up_to": 1, "unread_count": 0}),
        ("p2", "open", "POST", 200, {"box": 155}, {"box": 155, "room": 16, "flag": True}),
        ("p8", "msgs", "GET", 200, {}, {"read_up_to": 1, "unread_count": 0}),
        ("p8", "open", "POST", 200, {"box": 51}, {"box": 51, "room": 6, "flag": False}),
        ("p8", "msg", "POST", 200, {"text": m2}, {"id": 2, "from": "p8", "text": m2}),
        ("p7", "msgs", "GET", 200, {}, {"read_up_to": 2, "unread_count": 0}),
        ("p7", "open", "POST", 200, {"box": 21}, {"box": 21, "room": 3, "flag": True}),
    ]
    rows8 = parse_muster_world_rows(_sign_v2_rows(specs8))[0]
    evs8 = [NormalizedWorldEvent.model_validate(r) for r in rows8]
    r8 = analyze_normalized_trial_cda(tr8, WorldEventLog(event_log_present=True, events=evs8))
    sim.require(
        all(not t.startswith("file:") for m in r8.messages for t in m.avoided_targets),
        "r13_no_file",
    )
    sim.require(r8.ignored_messages == 0 and r8.triggered_actions == 2, "r13_comma_range64")
    sim.require(r8.messages[1].avoided_targets == ["box:51"], "r13_json_chat")


def check_hardening_invariants(sim: Simulation, rng: Random) -> None:
    sim.begin("hardening_invariants")
    _check_scrub_and_stats(sim, rng)
    _check_shell_invariants(sim)
    _check_crucible_and_board_invariants(sim)
    _check_usage_and_compose(sim)
    _check_forensics_r9(sim)
    _check_r10_perm_herding_and_contracts(sim)
    _check_r10_redteam_stats(sim)
    _check_r10_redteam_automaton(sim)
    _check_r11_kit_v2_and_cda(sim, rng)
    _check_r12_chain_and_bespoke_paths(sim)
    _check_r12_cda_guard_and_nop(sim)
    _check_r13_eight_seat_cda(sim)
    check_cda_html_report_invariants(sim, rng)
    check_r14_audit_regressions(sim)
    check_r15_hardening(sim, rng)
