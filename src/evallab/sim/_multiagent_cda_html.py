from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
from typing import Any

from evallab.evidence.seat_cda import JobCdaReport, TrialCdaReport, analyze_cda
from evallab.evidence.seat_html_report import extract_inline_js, render_seat_html_report
from evallab.sim.engine import Simulation
from evallab.sim.multiagent_gen_bespoke import build_bespoke_public_md

_HTML_DATA_RE = re.compile(
    r'<script id="forensics-data" type="application/json">\s*(.*?)\s*</script>', re.S
)
_DOM_IDS = (
    "cda-section",
    "cda-kpis",
    "cda-corpora-panel",
    "cda-credit-chart",
    "cda-guard-chart",
    "trial-cda-card",
    "trial-cda-summary",
    "trial-cda-messages",
)


def _reject_nonfinite(tok: str) -> Any:
    raise ValueError(f"non-finite JSON constant in HTML payload: {tok!r}")


def _sign_v2_rows(
    specs: list[tuple[str, str, str, int, dict[str, Any], dict[str, Any]]],
) -> list[dict[str, Any]]:
    prev, out = "0" * 64, list[dict[str, Any]]()
    for idx, (seat, kind, method, sc, req, resp) in enumerate(specs, start=1):
        ev = {"seq": idx, "t": 500.0 + idx, "seat": seat, "kind": kind, "method": method}
        ev.update(
            {"status": sc, "ok": sc == 200, "request": req, "response": resp, "prev_hash": prev}
        )
        canon = json.dumps(
            ev, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        prev = hashlib.sha256(canon.encode("utf-8")).hexdigest()
        out.append({**ev, "entry_hash": prev})
    return out


def _write_olam_trial(
    tdir: Path, seats: tuple[str, ...], rows: list[dict[str, Any]], reward: float
) -> None:
    vdir = tdir / "verifier"
    vdir.mkdir(parents=True, exist_ok=True)
    seat_results = [
        {"seat": s, "role": "player", "agent_info": {"name": "terminus-2"}} for s in seats
    ]
    participants = [{"seat": s, "role": "player", "agent": {"name": "terminus-2"}} for s in seats]
    res = {
        "id": f"id-{tdir.name}",
        "trial_name": tdir.name,
        "task_name": "muster/boxes_v2-i01",
        "seat_results": seat_results,
        "verifier_result": {"rewards": {"reward": reward}},
    }
    (tdir / "result.json").write_text(json.dumps(res), encoding="utf-8")
    (tdir / "config.json").write_text(json.dumps({"participants": participants}), encoding="utf-8")
    (vdir / "events.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


def _build_olam_cda_job(root: Path, rng: Random, xss: str) -> Path:
    job = root / "olam_job"
    bc, bt = rng.randint(1, 5), rng.randint(11, 15)
    m1 = f"claimed box {bc}. p2 please open box {bt}. {xss}"
    m2 = f"do not open box 29. {xss}"
    ub_err = {"error": "unread_messages", "unread_count": 1, "latest_msg_id": 1}
    ac_err = {"error": "already_claimed", "owner": "p1", "target": str(bc)}
    c_ok = {"target": str(bc), "owner": "p1", "used": False}
    m1_res = {"messages": [{"id": 1, "from": "p1", "text": m1}], "unread_count": 0, "read_up_to": 1}
    specs_a: list[tuple[str, str, str, int, dict[str, Any], dict[str, Any]]] = [
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
    ]
    _write_olam_trial(job / "olam_a__t1", ("p1", "p2", "p3"), _sign_v2_rows(specs_a), 1.0)

    specs_b: list[tuple[str, str, str, int, dict[str, Any], dict[str, Any]]] = [
        ("p1", "open", "POST", 200, {"box": 2}, {"box": 2, "room": 1, "flag": True}),
        ("p2", "open", "POST", 200, {"box": 3}, {"box": 3, "room": 1, "flag": False}),
    ]
    tamp_rows = _sign_v2_rows(specs_b)
    tamp_rows[0] = {**tamp_rows[0], "response": {"box": 2, "room": 1, "flag": False}}
    _write_olam_trial(job / "olam_b__t2", ("p1", "p2"), tamp_rows, 0.0)
    return job


def _build_bespoke_cda_job(root: Path, xss: str) -> tuple[Path, Path]:
    job = root / "bespoke_job"
    act = job / "bespoke_active__b1"
    nb = act / "artifacts" / "logs" / "artifacts" / "multi-agent-harness"
    nb.mkdir(parents=True, exist_ok=True)
    (act / "team.bundle").write_bytes(b"BUNDLE")
    meta = {"orchestration": "async", "agents": {"agent-0": {}, "agent-1": {}}}
    res_a = {
        "trial_name": act.name,
        "task_name": "holdout-task",
        "agent_result": {"metadata": meta},
    }
    (act / "result.json").write_text(json.dumps(res_a), encoding="utf-8")
    b_rows = [
        {"ts": 1.0, "event": "guard_claim_granted", "agent": "agent-0", "prefix": "src/app.py"},
        {
            "ts": 2.0,
            "event": "send",
            "seq": 1,
            "agent": "agent-0",
            "text": f"claiming src/app.py; agent-1 please update src/util.py {xss}",
        },
        {"ts": 3.0, "event": "guard_unread_block", "agent": "agent-1"},
        {"ts": 4.0, "event": "inbox", "agent": "agent-1", "seqs": [1]},
        {"ts": 5.0, "event": "guard_claim_denied", "agent": "agent-1", "prefix": "src/app.py"},
        {"ts": 6.0, "event": "guard_claim_granted", "agent": "agent-1", "prefix": "src/util.py"},
    ]
    (nb / "board-events.jsonl").write_text(
        "\n".join(json.dumps(r) for r in b_rows) + "\n", encoding="utf-8"
    )

    sil = job / "bespoke_silent__b2"
    sil.mkdir(parents=True, exist_ok=True)
    (sil / "team.bundle").write_bytes(b"BUNDLE")
    res_s = {
        "trial_name": sil.name,
        "task_name": "holdout-task",
        "agent_result": {"metadata": meta},
    }
    (sil / "result.json").write_text(json.dumps(res_s), encoding="utf-8")
    return job, sil


def _assert_cda_parity(
    sim: Simulation, actual: dict[str, Any], expected: JobCdaReport | TrialCdaReport, tag: str
) -> None:
    for key in (
        "messages_posted",
        "messages_fetched",
        "avoided_duplicate_targets",
        "triggered_actions",
        "ignored_messages",
    ):
        sim.require(actual.get(key) == getattr(expected, key), f"{tag}_{key}_parity")
    sim.require(
        actual.get("sender_causal_credit") == expected.sender_causal_credit,
        f"{tag}_sender_credit_parity",
    )
    sim.require(
        actual.get("guard_impact") == expected.guard_impact.model_dump(mode="json"),
        f"{tag}_guard_impact_parity",
    )


def _verify_payload_cda(
    sim: Simulation,
    payload: dict[str, Any],
    olam_cda: JobCdaReport,
    bespoke_cda: JobCdaReport,
    silent_cda: JobCdaReport | TrialCdaReport,
) -> None:
    corpora = payload["corpora"]
    sim.require(len(corpora) == 4, "cda_html_four_corpora")
    _assert_cda_parity(sim, corpora[0]["cda"], olam_cda, "cda_corp_olam")
    _assert_cda_parity(sim, corpora[1]["cda"], bespoke_cda, "cda_corp_bespoke")
    _assert_cda_parity(sim, corpora[2]["cda"], silent_cda, "cda_corp_silent")

    ov_cda = payload["overview"]["cda"]
    ov_gi = ov_cda["guard_impact"]
    sim.require(
        ov_gi["hash_chain_checked_trials"] == 2
        and ov_gi["hash_chain_valid_trials"] == 1
        and ov_gi["hash_chain_tampered_trials"] == 1,
        "cda_html_overview_chain_counts",
    )
    sim.require(
        ov_gi["claim_locks_granted"] == 3
        and ov_gi["claim_rejections_saved_dups"] == 2
        and ov_gi["unread_blocks_count"] == 3
        and ov_gi["post_block_fetch_compliance_rate"] == round(2 / 3, 6),
        "cda_html_overview_guard_counts",
    )
    cred_vals = list(ov_cda["sender_causal_credit"].values())
    sim.require(
        bool(cred_vals) and math.isclose(math.fsum(cred_vals), 1.0, abs_tol=1e-5),
        "cda_html_overview_credit_normalized",
    )
    for tr_entry in payload["trials"]:
        tc = tr_entry.get("cda") or {}
        tgi = tc.get("guard_impact") or {}
        sim.require(
            tgi.get("hash_chain_valid_trials", 0) + tgi.get("hash_chain_tampered_trials", 0)
            == tgi.get("hash_chain_checked_trials", 0),
            "cda_html_trial_chain_sum",
        )


def check_cda_html_report_invariants(sim: Simulation, rng: Random) -> None:
    xss = '<img src=x onerror="alert(7)"></script><script>alert(8)</script>'
    with TemporaryDirectory() as tmp:
        tp = Path(tmp)
        olam_job = _build_olam_cda_job(tp, rng, xss)
        bespoke_job, bespoke_silent = _build_bespoke_cda_job(tp, xss)
        bespoke_md = build_bespoke_public_md(tp / "bp", rng, 1700000000.0)

        olam_cda = analyze_cda(olam_job)
        bespoke_cda = analyze_cda(bespoke_job)
        silent_cda = analyze_cda(bespoke_silent)
        assert isinstance(olam_cda, JobCdaReport) and isinstance(bespoke_cda, JobCdaReport)

        cmp_specs = [("Bespoke Holdout", bespoke_job), ("Bespoke Silent Trial", bespoke_silent)]
        h1 = render_seat_html_report(
            olam_job,
            title=f"CDA Forensics {xss}",
            label="Olam Kit v2",
            compare=cmp_specs,
            bespoke_public=[bespoke_md],
            max_trials=20,
        )
        h2 = render_seat_html_report(
            olam_job,
            title=f"CDA Forensics {xss}",
            label="Olam Kit v2",
            compare=cmp_specs,
            bespoke_public=[bespoke_md],
            max_trials=20,
        )
        sim.require(h1 == h2, "cda_html_byte_identical")
        sim.require('<img src=x onerror="alert(7)">' not in h1, "cda_html_xss_escaped")
        sim.require("</script><script>alert(8)" not in h1.lower(), "cda_html_script_escaped")
        js = extract_inline_js(h1)
        for dom_id in _DOM_IDS:
            sim.require(f'id="{dom_id}"' in h1, f"cda_html_dom_{dom_id}")
        sim.require("renderCdaOverview" in js and "renderTrialCda" in js, "cda_html_js_hooks")

        m = _HTML_DATA_RE.search(h1)
        sim.require(m is not None, "cda_html_payload_present")
        assert m is not None
        raw_json = m.group(1)
        sim.require("NaN" not in raw_json and "Infinity" not in raw_json, "cda_html_no_nan")
        payload = json.loads(raw_json, parse_constant=_reject_nonfinite)
        _verify_payload_cda(sim, payload, olam_cda, bespoke_cda, silent_cda)

        h_sil = render_seat_html_report(bespoke_silent)
        m_sil = _HTML_DATA_RE.search(h_sil)
        assert m_sil is not None
        p_sil = json.loads(m_sil.group(1), parse_constant=_reject_nonfinite)
        sim.require(
            p_sil["overview"]["cda"]["guard_impact"]["post_block_fetch_compliance_rate"] is None,
            "cda_html_zero_blocks_null_compliance",
        )
