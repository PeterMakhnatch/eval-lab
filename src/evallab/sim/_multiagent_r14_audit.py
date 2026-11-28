from __future__ import annotations

import math
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from evallab.evidence._decompose_models import (
    ArmDecompositionSummary,
    PaceResourceVector,
    compute_best_of_k_solo,
)
from evallab.evidence._decompose_stats import compare_paired_arms, exact_sign_flip_p
from evallab.evidence._forensics_shell import is_mutating_shell_cmd
from evallab.evidence._forensics_shell_lex import split_shell_tokens
from evallab.evidence._multiagent_io import MemberRecord, MemberUsage
from evallab.evidence._olam_world_parsers import parse_muster_world_rows
from evallab.evidence._seat_html_cda import merge_corpus_cda
from evallab.evidence.multiagent_adapter import NormalizedMultiAgentTrial
from evallab.evidence.seat_cda import analyze_normalized_trial_cda
from evallab.evidence.seat_html_report import _build_trial_timeline, write_seat_html_report
from evallab.evidence.world_events import NormalizedWorldEvent, WorldEventLog
from evallab.sim._multiagent_cda_html import _sign_v2_rows, _write_olam_trial
from evallab.sim.engine import Simulation


def _check_r14_decompose(sim: Simulation) -> None:
    base_o = {f"t{i}": [1e15] for i in range(6)}
    cand_o = {f"t{i}": [1e15 + 10.0] for i in range(6)}
    cmp = compare_paired_arms(base_o, cand_o)
    sim.require(
        cmp.p_signflip == 0.03125 and cmp.p_perm_rerun == 0.03125 and cmp.perm_rerun_exact,
        "r14_perm_centered_huge_offset",
    )
    sf_p, sf_ex = exact_sign_flip_p([0.5, -0.5 + 2**-19, 0.5, -0.5 + 2**-48])
    sim.require(sf_ex and sf_p == 0.75, "r14_sign_flip_no_unit_floor")

    pace = PaceResourceVector(mean_cost_usd=0.01)
    solo_a = ArmDecompositionSummary(
        role="solo",
        path="s",
        seats_min=1,
        seats_max=1,
        n_tasks_total=4,
        n_tasks_shared=4,
        n_trials_shared=7,
        score_macro=0.3375,
        score_micro=0.3857,
        pace=pace,
    )
    team_a = solo_a.model_copy(update={"role": "team", "seats_min": 2, "seats_max": 2})
    solo_out = {"t0": [0.4, 0.5], "t1": [0.4, 0.4], "t2": [0.45, 0.45], "t3": [0.0]}
    team_out = {k: [0.5] for k in solo_out}
    team_tr: dict[str, list[NormalizedMultiAgentTrial]] = {k: [] for k in solo_out}
    bok = compute_best_of_k_solo(
        ["t0", "t1", "t2", "t3"], solo_out, team_out, team_tr, solo_a, team_a, 2
    )
    assert bok is not None and bok.unbiased_score_macro is not None
    sim.require(
        bok.unbiased_score_macro <= bok.empirical_max_macro + 1e-12
        and abs(bok.empirical_max_macro - 0.45) < 1e-12,
        "r14_bok_subset_aligned",
    )
    bok_none = compute_best_of_k_solo(
        ["t0", "t1", "t2", "t3"], solo_out, team_out, team_tr, solo_a, team_a, 5
    )
    assert bok_none is not None
    sim.require(
        bok_none.unbiased_score_macro is None and math.isfinite(bok_none.empirical_max_macro),
        "r14_bok_sparse_fallback",
    )


def _check_r14_cda(sim: Simulation) -> None:
    zu = MemberUsage(cost_status="zero_no_model")
    ms = [MemberRecord(member_id=s, trajectory_status="missing", usage=zu) for s in ("p1", "p2")]
    tr2 = NormalizedMultiAgentTrial(
        trial_dir="", trial_name="r14_cda", ecosystem="olam_seats", members=ms
    )
    m_tr = "opened box 1 = FLAG (room 1 treasure), room 2: empty"
    m_un = "boxes 33-36 are unopened"
    c_ok = {"target": "4", "owner": "p1", "used": False}
    specs: list[tuple[str, str, str, int, dict[str, Any], dict[str, Any]]] = [
        ("p1", "open", "POST", 200, {"box": 1}, {"box": 1, "room": 1, "flag": True}),
        ("p1", "claim", "POST", 200, {"box": 4}, c_ok),
        ("p1", "release", "POST", 200, {"box": 4}, {"released": "4"}),
        (
            "p1",
            "msg",
            "POST",
            200,
            {"text": "box 4 open"},
            {"id": 1, "from": "p1", "text": "box 4 open"},
        ),
        ("p1", "msg", "POST", 200, {"text": m_tr}, {"id": 2, "from": "p1", "text": m_tr}),
        ("p1", "msg", "POST", 200, {"text": m_tr}, {"id": 3, "from": "p1", "text": m_tr}),
        ("p1", "msg", "POST", 200, {"text": m_un}, {"id": 4, "from": "p1", "text": m_un}),
        ("p1", "msg", "POST", 200, {"text": m_un}, {"id": 5, "from": "p1", "text": m_un}),
        ("p2", "msgs", "GET", 200, {}, {"read_up_to": 5, "unread_count": 0}),
        ("p2", "open", "POST", 200, {"box": 4}, {"box": 4, "room": 1, "flag": True}),
        ("p2", "open", "POST", 200, {"box": 33}, {"box": 33, "room": 5, "flag": False}),
        ("p1", "open", "POST", 200, {"box": 34}, {"box": 34, "room": 5, "flag": False}),
    ]
    evs = [
        NormalizedWorldEvent.model_validate(r)
        for r in parse_muster_world_rows(_sign_v2_rows(specs))[0]
    ]
    evs[8] = evs[8].model_copy(update={"ts_epoch": 150.0})
    rep = analyze_normalized_trial_cda(tr2, WorldEventLog(event_log_present=True, events=evs))
    sim.require(rep.ignored_messages == 0, "r14_cda_no_false_ignored")
    sim.require(rep.triggered_actions == 2, "r14_cda_dedup_triggered")
    sim.require(
        sum(m.avoided_duplicate_targets for m in rep.messages) == 2
        and rep.avoided_duplicate_targets == 1,
        "r14_cda_dedup_avoided",
    )


def _check_r14_shell_and_html(sim: Simulation) -> None:
    toks = split_shell_tokens("echo $'\\cß' $'\\cﬁ' $'\\c\\\\'")
    sim.require(len(toks) == 4 and all(len(x) == 1 for x in toks[1:]), "r14_lex_nonascii_ctrl")
    for cmd in (
        "echo ok # c \\\nrm -f f",
        "echo ok \\\r\nrm -f f",
        'sort $"-o" f /etc/hosts',
        "sort $'-o\\x00junk' f /etc/hosts",
    ):
        sim.require(is_mutating_shell_cmd(cmd), "r14_lex_mutating_guards")

    tr = NormalizedMultiAgentTrial(trial_dir="", trial_name="t", ecosystem="olam_seats", members=[])
    evs = [
        NormalizedWorldEvent(
            seq=i,
            actor="p1",
            kind="world_action",
            visibility="public",
            source_format="olam_muster_world",
            ts_epoch=v,
        )
        for i, v in ((1, -1e308), (2, 1e308))
    ]
    tl = _build_trial_timeline(
        tr, WorldEventLog(event_log_present=True, events=evs, end_of_play_ts_epoch=0.0), {}
    )
    sim.require(
        all(math.isfinite(e["x_pos"]) for e in tl["events"])
        and tl["events"][0]["x_pos"] < tl["events"][1]["x_pos"]
        and tl["end_of_play_x"] == 500.0,
        "r14_html_extreme_ts_finite",
    )
    mc = merge_corpus_cda(
        [
            {
                "cda": {
                    "guard_impact": {
                        "unread_blocks_count": 4,
                        "post_block_fetch_compliance_rate": math.nan,
                    }
                }
            },
            {
                "cda": {
                    "guard_impact": {
                        "unread_blocks_count": 4,
                        "post_block_fetch_compliance_rate": 2.5,
                    }
                }
            },
            {"cda": {"compliant_unread_blocks": 99, "guard_impact": {"unread_blocks_count": 2}}},
        ]
    )
    rate = mc["guard_impact"]["post_block_fetch_compliance_rate"]
    sim.require(rate is not None and 0.0 <= rate <= 1.0, "r14_merge_cda_rate_clamped")
    with TemporaryDirectory() as tmp:
        tp = Path(tmp)
        rows = _sign_v2_rows([("p1", "open", "POST", 200, {"box": 1}, {"box": 1, "room": 1})])
        _write_olam_trial(tp / "job" / "t1__a", ("p1",), rows, 1.0)
        out_h = write_seat_html_report(
            tp / "job",
            tp / "out.html",
            title=f"Bad{chr(0xD800)}Title",
            label=f"Bad{chr(0xDFFF)}Label",
        )
        txt = out_h.read_text(encoding="utf-8")
        no_surr = not any(0xD800 <= ord(ch) <= 0xDFFF for ch in txt)
        sim.require(
            "Bad?Title" in txt and "Bad?Label" in txt and no_surr, "r14_html_surrogate_safe"
        )


def check_r14_audit_regressions(sim: Simulation) -> None:
    _check_r14_decompose(sim)
    _check_r14_cda(sim)
    _check_r14_shell_and_html(sim)
