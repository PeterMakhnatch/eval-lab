from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path
from random import Random
from typing import Any

from worldwatch._sim_cases import SimRun
from worldwatch._sim_workflow_f1_f7 import exercise_workflow_redteam_f1_f7
from worldwatch.compare import run_compare
from worldwatch.model import parse_strict_json
from worldwatch.preflight import run_preflight
from worldwatch.sim_fixture import write_olam_trial


def _write_paired_trial(
    arm_dir: Path,
    *,
    family: str,
    condition: str,
    inst_idx: int,
    suffix: str,
    reward: float | None,
    reward_valid: bool = True,
    cost_per_seat: float = 0.03,
    stalled_seat: str | None = None,
    duration_s: float = 60.0,
    events: list[dict[str, Any]] | None = None,
    path_style: str = "double_underscore",
) -> Path:
    inst = f"i{inst_idx:02d}"
    stem = f"{family}__{condition}__{inst}"
    trial_name = f"{stem}__{suffix}"
    tdir = arm_dir / trial_name
    write_olam_trial(
        tdir,
        trial_name=trial_name,
        seats=("p1", "p2"),
        events=events,
        reward=reward,
        reward_valid=reward_valid,
        cost_per_seat=cost_per_seat,
        stalled_seat=stalled_seat,
        duration_s=duration_s,
    )
    if path_style == "nested":
        task_path = f"/sim/tasks/{family}/build/{condition}/{family}-{inst}"
    else:
        task_path = f"/sim/waves/{stem}"
    cfg_path = tdir / "config.json"
    cfg_doc = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg_doc["task"] = {"path": task_path, "name": f"muster/{family}-{inst}"}
    cfg_path.write_text(json.dumps(cfg_doc), encoding="utf-8")
    res_path = tdir / "result.json"
    res_doc = json.loads(res_path.read_text(encoding="utf-8"))
    res_doc["task_name"] = f"muster/{family}-{inst}"
    res_doc["config"] = cfg_doc
    res_path.write_text(json.dumps(res_doc), encoding="utf-8")
    return tdir


def _exercise_compare_exact_and_invariants(run: SimRun, root: Path, rng: Random) -> dict[str, Any]:
    arm_a = root / "cmp_a"
    arm_b = root / "cmp_b"
    arm_a_clone = root / "cmp_a_clone"
    arm_a_pad = root / "cmp_a_pad"
    arm_b_pad = root / "cmp_b_pad"
    for idx in range(1, 7):
        style = "nested" if idx % 2 == 0 else "double_underscore"
        _write_paired_trial(arm_a, family="probe_v1", condition="trap", inst_idx=idx, suffix="Aa11Bb2", reward=1.0, cost_per_seat=0.04, path_style=style)
        _write_paired_trial(arm_b, family="probe_v1", condition="trap", inst_idx=idx, suffix="Cc33Dd4", reward=0.0, cost_per_seat=0.02, path_style=style)
        _write_paired_trial(arm_a_clone, family="probe_v1", condition="trap", inst_idx=idx, suffix="Ee55Ff6", reward=1.0, cost_per_seat=0.04, path_style=style)
        _write_paired_trial(arm_a_pad, family="probe_v1", condition="trap", inst_idx=idx, suffix="Gg77Hh8", reward=1.0)
        _write_paired_trial(arm_b_pad, family="probe_v1", condition="trap", inst_idx=idx, suffix="Jj88Kk9", reward=0.0)
    _write_paired_trial(arm_a, family="probe_v1", condition="trap", inst_idx=1, suffix="Rr99Ss0", reward=1.0, cost_per_seat=0.04)

    pad_n = rng.randint(2, 4)
    for extra in range(1, pad_n + 1):
        tie_rew = 1.0 if extra % 2 == 0 else 0.5
        _write_paired_trial(arm_a_pad, family="probe_v1", condition="ctrl", inst_idx=extra, suffix="Pa12Bc3", reward=tie_rew)
        _write_paired_trial(arm_b_pad, family="probe_v1", condition="ctrl", inst_idx=extra, suffix="Pb45De6", reward=tie_rew)

    rep_ab, rc_ab = run_compare([f"arm_a={arm_a}", f"arm_b={arm_b}"], seed=run.seed)
    rep_ba, rc_ba = run_compare([f"arm_b={arm_b}", f"arm_a={arm_a}"], seed=run.seed)
    rep_pad, _ = run_compare([f"arm_a={arm_a_pad}", f"arm_b={arm_b_pad}"], seed=run.seed)
    rep_zero, rc_zero = run_compare([f"a1={arm_a}", f"a2={arm_a_clone}"], seed=run.seed)

    p_ab = rep_ab["pairs"][0]
    p_ba = rep_ba["pairs"][0]
    p_pad = rep_pad["pairs"][0]
    p_zero = rep_zero["pairs"][0]
    run.require("compare/rc", "clean-arms-exit-zero", rc_ab == 0 and rc_ba == 0 and rc_zero == 0)
    run.require("compare/dedup-keys", "duplicate-attempt-averaged-into-six-pairs", p_ab["n_paired"] == 6)
    run.require("compare/exact-signflip", "six-wins-p-is-0.03125", math.isclose(p_ab["sign_flip_p"], 0.03125, abs_tol=1e-8) and p_ab["sign_flip_exact"] is True)
    run.require("compare/exact-mcnemar", "six-discordant-p-is-0.03125", math.isclose(p_ab["mcnemar_p"], 0.03125, abs_tol=1e-8))
    run.require(
        "compare/symmetry",
        "swap-arms-preserves-p-negates-delta",
        math.isclose(p_ab["mean_delta"], -p_ba["mean_delta"], abs_tol=1e-9)
        and p_ab["sign_flip_p"] == p_ba["sign_flip_p"]
        and p_ab["mcnemar_p"] == p_ba["mcnemar_p"]
        and p_ab["attrition_fisher_p"] == p_ba["attrition_fisher_p"],
    )
    run.require(
        "compare/zero-diff-invariance",
        "adding-ties-preserves-signflip-and-mcnemar",
        p_pad["n_paired"] == 6 + pad_n
        and p_pad["sign_flip_p"] == p_ab["sign_flip_p"]
        and p_pad["mcnemar_p"] == p_ab["mcnemar_p"],
    )
    run.require(
        "compare/all-zero",
        "identical-rewards-give-p-one",
        p_zero["mean_delta"] == 0.0 and p_zero["sign_flip_p"] == 1.0 and p_zero["mcnemar_p"] == 1.0,
    )
    return {"sf_p": p_ab["sign_flip_p"], "pad_n": pad_n}


def _exercise_compare_mc_attrition_and_html(run: SimRun, root: Path, rng: Random) -> dict[str, Any]:
    mc_a = root / "mc_a"
    mc_b = root / "mc_b"
    for idx in range(1, 19):
        ra = round(0.55 + 0.40 * rng.random(), 4)
        rb = round(0.10 + 0.35 * rng.random(), 4)
        _write_paired_trial(mc_a, family="scale_v1", condition="k16", inst_idx=idx, suffix="Ma11Bb2", reward=ra)
        _write_paired_trial(mc_b, family="scale_v1", condition="k16", inst_idx=idx, suffix="Mb33Dd4", reward=rb)
    rep_mc1, _ = run_compare([f"ma={mc_a}", f"mb={mc_b}"], seed=run.seed)
    rep_mc2, _ = run_compare([f"ma={mc_a}", f"mb={mc_b}"], seed=run.seed)
    rep_mc_rev, _ = run_compare([f"mb={mc_b}", f"ma={mc_a}"], seed=run.seed)
    pm1, pm2, pmr = rep_mc1["pairs"][0], rep_mc2["pairs"][0], rep_mc_rev["pairs"][0]
    run.require(
        "compare/mc-seeded",
        "mc-signflip-seeded-and-symmetric",
        pm1["n_paired"] == 18
        and pm1["sign_flip_exact"] is False
        and 0.0 < pm1["sign_flip_p"] <= 1.0
        and pm1["sign_flip_p"] == pm2["sign_flip_p"] == pmr["sign_flip_p"],
    )

    attr_clean = root / "attr_clean"
    attr_bad = root / "attr_bad"
    dup_events = [
        {"t": 1_770_000_002.0, "seat": "p1", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 5}, "response": {"box": 5, "flag": True, "budget_left": 0}},
        {"t": 1_770_000_003.0, "seat": "p2", "kind": "open", "method": "POST", "status": 200, "ok": True, "request": {"box": 5}, "response": {"box": 5, "flag": True, "budget_left": 0}},
        {"t": 1_770_000_010.0, "seat": None, "kind": "admin.close", "method": "POST", "status": 200, "ok": True, "request": {}, "response": {"closed": True}},
    ]
    for idx in range(1, 9):
        _write_paired_trial(attr_clean, family="field", condition="indep-8", inst_idx=idx, suffix="Cl11Aa2", reward=0.5, path_style="nested")
        stalled = "p1" if idx <= 6 else None
        evs = dup_events if idx > 6 else None
        _write_paired_trial(attr_bad, family="field", condition="team-8", inst_idx=idx, suffix="Bd22Bb3", reward=1.0, stalled_seat=stalled, duration_s=450.0, events=evs, path_style="nested")

    html_file = root / "compare_xss.html"
    xss_label = 'bad_<script>alert(1)</script><!--"-->'
    rep_attr, rc_attr = run_compare([f"clean={attr_clean}", f"{xss_label}={attr_bad}"], html_path=html_file, alpha=0.05, seed=run.seed)
    pa = rep_attr["pairs"][0]
    run.require("compare/cond-pair", "single-condition-arms-pair-by-family-instance", pa["pairing_mode"] == "family_instance" and pa["n_paired"] == 2)
    run.require("compare/fisher-attrition", "differential-attrition-exits-one", rc_attr == 1 and pa["differential_attrition"] is True and pa["attrition_fisher_p"] < 0.05)
    run.require("compare/detector-delta", "triggered-detector-delta-recorded", "dup_unseen" in pa["detector_deltas"] and pa["detector_deltas"]["dup_unseen"] < 0.0)

    html_txt = html_file.read_text(encoding="utf-8")
    run.require("compare/html-csp", "strict-csp-and-offline", "default-src 'none'" in html_txt and "http://" not in html_txt and "https://" not in html_txt)
    run.require("compare/html-xss", "script-and-comment-escaped", "<script>alert(1)</script>" not in html_txt and "<!--" not in html_txt)
    marker = '<script id="worldwatch-compare" type="application/json">'
    _, _, after = html_txt.partition(marker)
    blob, _, _ = after.partition("</script>")
    parsed_blob = parse_strict_json(blob)
    run.require("compare/html-json", "json-blob-safe-and-valid", "<" not in blob and ">" not in blob and parsed_blob["command"] == "compare")
    return {"mc_p": pm1["sign_flip_p"], "fisher_p": pa["attrition_fisher_p"]}


def _write_synth_task(
    task_dir: Path,
    *,
    n_seats: int = 2,
    compose_bake: str | None = "false",
    internal_net: bool = True,
    host_tests_mount: bool = False,
) -> None:
    (task_dir / "environment" / "seat").mkdir(parents=True, exist_ok=True)
    (task_dir / "environment" / "world").mkdir(parents=True, exist_ok=True)
    (task_dir / "tests").mkdir(parents=True, exist_ok=True)
    (task_dir / "solution").mkdir(parents=True, exist_ok=True)
    seats = [f"p{i}" for i in range(1, n_seats + 1)]
    toml_lines = [
        'schema_version = "1.4"',
        'artifacts = ["/logs/world/events.jsonl"]',
        "[task]",
        'name = "muster/synth-i01"',
        "[metadata]",
        'family = "synth_v1"',
        'condition = "ctrl"',
        "instance = 1",
        "[verifier]",
        "timeout_sec = 120.0",
        "[agent]",
        "timeout_sec = 600.0",
        "[environment]",
        "build_timeout_sec = 600.0",
    ]
    if compose_bake is not None:
        toml_lines.extend(["[environment.env]", f'COMPOSE_BAKE = "{compose_bake}"'])
    for s in seats:
        toml_lines.append(f"[agents.{s}]")
    (task_dir / "task.toml").write_text("\n".join(toml_lines) + "\n", encoding="utf-8")
    (task_dir / "environment" / "Dockerfile").write_text("FROM python:3.13-slim\nCOPY world/ /world/\n", encoding="utf-8")
    (task_dir / "environment" / "seat" / "Dockerfile").write_text("FROM python:3.13-slim\n", encoding="utf-8")
    di_body = "**/__pycache__\n*.pyc\n.DS_Store\n"
    (task_dir / "environment" / ".dockerignore").write_text(di_body, encoding="utf-8")
    (task_dir / "environment" / "seat" / ".dockerignore").write_text(di_body, encoding="utf-8")
    (task_dir / "tests" / "verify.py").write_text("print('ok')\n", encoding="utf-8")
    (task_dir / "solution" / "solve.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    comp = [
        "services:",
        "  main:",
        '    command: ["python3", "/world/world.py"]',
        "    networks:",
        *[f"      {s}-world:\n        aliases: [world]" for s in seats],
        "    healthcheck:",
        '      test: ["CMD", "curl", "-fsS", "http://localhost:8080/health"]',
    ]
    for s in seats:
        comp.extend([f"  {s}:", "    build: ./seat", f"    networks: [{s}-world]"])
        if host_tests_mount and s == "p1":
            comp.extend(["    volumes:", "      - ./tests:/tests"])
    comp.append("networks:")
    flag = "true" if internal_net else "false"
    for s in seats:
        comp.extend([f"  {s}-world:", f"    internal: {flag}"])
    (task_dir / "environment" / "docker-compose.yaml").write_text("\n".join(comp) + "\n", encoding="utf-8")


def _exercise_preflight_suite(run: SimRun, root: Path) -> list[str]:
    valid_build = root / "pf_valid" / "build"
    _write_synth_task(valid_build / "ctrl" / "synth_v1-i01", n_seats=2, compose_bake="false")
    _write_synth_task(valid_build / "scale-26" / "synth_v1-i02", n_seats=26, compose_bake="false")
    ok_key = {"data": {"limit": 100.0, "limit_remaining": 15.0, "usage": 85.0}}
    ok_cred = {"data": {"total_credits": 130.0, "total_usage": 120.0}}
    rep_ok, rc_ok = run_preflight([str(valid_build)], key_snapshot=ok_key, credits_snapshot=ok_cred)
    run.require("preflight/valid", "clean-tasks-and-budget-pass", rc_ok == 0 and rep_ok["passed"] is True and rep_ok["tasks_checked"] == 2)

    f_di = root / "pf_no_di" / "t1"
    _write_synth_task(f_di)
    (f_di / "environment" / "seat" / ".dockerignore").unlink()
    rep_di, rc_di = run_preflight([str(f_di)])

    f_pyc = root / "pf_pyc" / "t1"
    _write_synth_task(f_pyc)
    pyc_dir = f_pyc / "environment" / "world" / "__pycache__"
    pyc_dir.mkdir(parents=True, exist_ok=True)
    (pyc_dir / "world.cpython-312.pyc").write_bytes(b"\x00\x01\x02\x03")
    rep_pyc, rc_pyc = run_preflight([str(f_pyc)])

    f_mnt = root / "pf_mount" / "t1"
    _write_synth_task(f_mnt, host_tests_mount=True)
    rep_mnt, rc_mnt = run_preflight([str(f_mnt)])

    f_net = root / "pf_net" / "t1"
    _write_synth_task(f_net, internal_net=False)
    rep_net, rc_net = run_preflight([str(f_net)])

    f_bake = root / "pf_bake" / "t1"
    _write_synth_task(f_bake, n_seats=25, compose_bake=None)
    rep_bake, rc_bake = run_preflight([str(f_bake)])

    trap_key = {"data": {"limit": 100.0, "limit_remaining": 0.0, "usage": 100.008843}}
    trap_cred = {"data": {"total_credits": 130.0, "total_usage": 123.10}}
    rep_key, rc_key = run_preflight([str(valid_build)], min_key_usd=1.0, min_account_usd=4.50, key_snapshot=trap_key, credits_snapshot=trap_cred)

    low_cred = {"data": {"total_credits": 130.0, "total_usage": 126.50}}
    rep_acct, rc_acct = run_preflight([str(valid_build)], min_key_usd=1.0, min_account_usd=4.50, key_snapshot=ok_key, credits_snapshot=low_cred)

    codes_di = {f["code"] for f in rep_di["findings"]}
    codes_pyc = {f["code"] for f in rep_pyc["findings"]}
    codes_mnt = {f["code"] for f in rep_mnt["findings"]}
    codes_net = {f["code"] for f in rep_net["findings"]}
    codes_bake = {f["code"] for f in rep_bake["findings"]}
    codes_key = {f["code"] for f in rep_key["findings"]}
    codes_acct = {f["code"] for f in rep_acct["findings"]}

    run.require("preflight/dockerignore", "missing-dockerignore-caught", rc_di == 1 and "missing-dockerignore" in codes_di)
    run.require("preflight/pyc", "stale-pyc-caught", rc_pyc == 1 and "bytecode-artifact" in codes_pyc)
    run.require("preflight/mount", "host-tests-mount-caught", rc_mnt == 1 and "forbidden-host-mount" in codes_mnt)
    run.require("preflight/internal-net", "non-internal-network-caught", rc_net == 1 and "compose-network-not-internal" in codes_net)
    run.require("preflight/compose-bake", "gt24-seats-without-bake-caught", rc_bake == 1 and "missing-compose-bake-false" in codes_bake)
    run.require(
        "preflight/key-cap-trap",
        "zero-key-limit-caught-despite-healthy-account",
        rc_key == 1 and "openrouter-key-limit-low" in codes_key and "openrouter-account-low" not in codes_key,
    )
    run.require("preflight/account-floor", "low-account-credits-caught", rc_acct == 1 and "openrouter-account-low" in codes_acct)
    return sorted(codes_di | codes_pyc | codes_mnt | codes_net | codes_bake | codes_key | codes_acct)


def _exercise_workflow_cli(
    run: SimRun,
    root: Path,
    run_cli: Callable[[list[str]], tuple[int, str, str, str]],
) -> None:
    rc_c_txt, out_c_txt, _, exc_c_txt = run_cli(["compare", f"a={root / 'c_exact' / 'cmp_a'}", f"b={root / 'c_exact' / 'cmp_b'}", "--seed", str(run.seed)])
    rc_c_json, out_c_json, _, exc_c_json = run_cli(["compare", f"a={root / 'c_exact' / 'cmp_a'}", f"b={root / 'c_exact' / 'cmp_b'}", "--seed", str(run.seed), "--json"])
    parsed_c = parse_strict_json(out_c_json)
    run.require(
        "workflow/cli-compare",
        "compare-cli-text-and-json",
        rc_c_txt == 0 and rc_c_json == 0 and exc_c_txt == "" and exc_c_json == "" and "compare: PASS" in out_c_txt and parsed_c["command"] == "compare",
    )
    rc_p_json, out_p_json, _, exc_p_json = run_cli(["preflight", str(root / "pf" / "pf_valid" / "build"), "--json"])
    rc_p_txt, out_p_txt, _, exc_p_txt = run_cli(["preflight", str(root / "pf" / "pf_no_di" / "t1")])
    parsed_p = parse_strict_json(out_p_json)
    run.require(
        "workflow/cli-preflight",
        "preflight-cli-text-and-json",
        rc_p_json == 0 and rc_p_txt == 1 and exc_p_json == "" and exc_p_txt == "" and parsed_p["passed"] is True and "missing-dockerignore" in out_p_txt,
    )


def exercise_compare_and_preflight(
    run: SimRun,
    root: Path,
    rng: Random,
    run_cli: Callable[[list[str]], tuple[int, str, str, str]] | None = None,
) -> None:
    run.scenarios += 1
    root.mkdir(parents=True, exist_ok=True)
    cmp_exact = _exercise_compare_exact_and_invariants(run, root / "c_exact", rng)
    cmp_mc = _exercise_compare_mc_attrition_and_html(run, root / "c_mc", rng)
    pf_codes = _exercise_preflight_suite(run, root / "pf")
    exercise_workflow_redteam_f1_f7(run, root / "f1_f7", run_cli)
    if run_cli is not None:
        _exercise_workflow_cli(run, root, run_cli)
    run.record("workflow", {"exact": cmp_exact, "mc": cmp_mc, "pf_codes": pf_codes})
