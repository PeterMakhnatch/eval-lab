from __future__ import annotations

import math
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from worldwatch._compare_html import render_compare_html
from worldwatch._sim_cases import SimRun
from worldwatch.compare import (
    _key_from_task_path,
    _key_from_trial_name,
    _sign_flip_two_sided,
    run_compare,
)
from worldwatch.model import DETECTOR_NAMES
from worldwatch.preflight import run_preflight
from worldwatch.render import render_compare, render_preflight, to_json
from worldwatch.sim_fixture import write_olam_trial


def _write_minimal_task(
    tdir: Path,
    *,
    toml_text: str,
    compose_name: str = "docker-compose.yaml",
    compose_text: str | None = None,
    di_env: str | None = "**/__pycache__\n*.pyc\n",
    di_seat: str | None = "**/__pycache__\n*.pyc\n",
    with_seat_dir: bool = True,
) -> Path:
    (tdir / "environment").mkdir(parents=True, exist_ok=True)
    if with_seat_dir:
        (tdir / "environment" / "seat").mkdir(parents=True, exist_ok=True)
    (tdir / "task.toml").write_text(toml_text, encoding="utf-8")
    (tdir / "environment" / "Dockerfile").write_text("FROM python:3.13-slim\n", encoding="utf-8")
    if di_env is not None:
        (tdir / "environment" / ".dockerignore").write_text(di_env, encoding="utf-8")
    if with_seat_dir and di_seat is not None:
        (tdir / "environment" / "seat" / ".dockerignore").write_text(di_seat, encoding="utf-8")
    if compose_text is not None:
        (tdir / "environment" / compose_name).write_text(compose_text, encoding="utf-8")
    return tdir


def _exercise_f1_compose(run: SimRun, root: Path) -> Path:
    base_toml = (
        'schema_version = "1.4"\nartifacts = ["/logs/world/events.jsonl"]\n'
        '[task]\nname = "muster/t-i01"\ndescription = "world-scripted task"\n'
        '[metadata]\nfamily = "t_v1"\ncondition = "ctrl"\ninstance = 1\n'
        "[verifier]\ntimeout_sec = 60.0\n[agent]\ntimeout_sec = 120.0\n"
        '[environment]\nbuild_timeout_sec = 120.0\n[environment.env]\nCOMPOSE_BAKE = "false"\n[agents.p1]\n'
    )
    ok_yml = (
        "services:\n"
        "    main:\n"
        "        command:\n"
        "            - python3\n"
        "            - /world/world.py\n"
        "        networks:\n"
        "            p1-world:\n"
        "                aliases: [world]\n"
        "        healthcheck:\n"
        '            test: ["CMD", "curl", "-fsS", "http://localhost:8080/health"]\n'
        "    p1:\n"
        "        image: alpine\n"
        '        networks: ["p1-world"]\n'
        "networks:\n"
        "    p1-world:\n"
        '        "internal": "true"\n'
    )
    ok_dir = _write_minimal_task(
        root / "ok_4sp", toml_text=base_toml, compose_name="docker-compose.yml", compose_text=ok_yml, with_seat_dir=False
    )
    rep_ok, rc_ok = run_preflight([str(ok_dir)])
    run.require("f14/compose-4sp-multiline", "valid-4sp-multiline-cmd-passes", rc_ok == 0 and rep_ok["passed"] is True, str(rep_ok["findings"]))

    bad_4sp = (
        "services:\n    main:\n        command: [python3, /world/world.py]\n"
        "        healthcheck:\n            test: [CMD, curl, http://localhost:8080/health]\n"
        '    p1:\n        "privileged":\n            true\n        "network_mode":\n            "host"\n'
        '        "volumes":\n            - /:/host_root\n'
    )
    d_4sp = _write_minimal_task(root / "bad_4sp", toml_text=base_toml, compose_text=bad_4sp)
    rep_4sp, rc_4sp = run_preflight([str(d_4sp)])
    c_4sp = {f["code"] for f in rep_4sp["findings"]}
    run.require(
        "f14/compose-quoted-multiline",
        "quoted-and-multiline-bypasses-caught",
        rc_4sp == 1 and {"forbidden-host-mount", "compose-privileged-forbidden", "compose-host-network-forbidden"} <= c_4sp,
        str(c_4sp),
    )

    bad_flow = (
        "services:\n  main:\n    command: [python3, /world/world.py]\n"
        "    healthcheck:\n      test: [CMD, curl, http://localhost:8080/health]\n"
        '  p1: {image: alpine, privileged: true, network_mode: host, volumes: ["/:/host"]}\n'
    )
    d_flow = _write_minimal_task(root / "bad_flow", toml_text=base_toml, compose_text=bad_flow)
    rep_fl, rc_fl = run_preflight([str(d_flow)])
    c_fl = {f["code"] for f in rep_fl["findings"]}
    run.require(
        "f14/compose-flow",
        "inline-flow-mapping-caught",
        rc_fl == 1 and {"forbidden-host-mount", "compose-privileged-forbidden", "compose-host-network-forbidden"} <= c_fl,
        str(c_fl),
    )
    return ok_dir


def _exercise_f1_toml_and_di(run: SimRun, root: Path) -> None:
    bad_toml = (
        'schema_version = "1.4"\nartifacts = ["tests/verify.py"]\n'
        '[metadata]\nfamily = "t_v1"\ncondition = "c"\ninstance = 1\n'
        "[agent]\ntimeout_sec = 60.0\n"
        '[agents.p1]\nprivileged = true\nnetwork_mode = "host"\nvolumes = ["/:/host"]\n'
        '[environment.docker]\nsetup = "solution/solve.sh"\n'
    )
    d_tm = _write_minimal_task(root / "bad_toml", toml_text=bad_toml, di_env="# comment\n.DS_Store\n")
    (d_tm / "environment" / "stale.cpython-312.pyo").write_bytes(b"\x00\x01")
    rep_tm, rc_tm = run_preflight([str(d_tm)])
    c_tm = {f["code"] for f in rep_tm["findings"]}
    run.require(
        "f14/toml-and-dockerignore",
        "agents-subtable-relpath-comment-di-and-pyo-caught",
        rc_tm == 1
        and {
            "forbidden-host-mount",
            "compose-privileged-forbidden",
            "compose-host-network-forbidden",
            "missing-dockerignore",
            "bytecode-artifact",
        }
        <= c_tm,
        str(c_tm),
    )


def _exercise_f2_openrouter_security(
    run: SimRun,
    valid_task: Path,
    run_cli: Callable[[list[str]], tuple[int, str, str, str]] | None,
) -> None:
    canary = "sk-or-v1-CANARY-SECRET-REDACT-887766"
    malformed = f"“{canary}\r\n”"
    prev = os.environ.get("OPENROUTER_API_KEY")
    try:
        os.environ["OPENROUTER_API_KEY"] = malformed
        rep_or, rc_or = run_preflight([str(valid_task)], check_openrouter=True)
        blob = to_json(rep_or) + "\n" + render_preflight(rep_or)
        cli_ok = True
        if run_cli is not None:
            rc_c, out_c, err_c, exc_c = run_cli(["preflight", str(valid_task), "--check-openrouter"])
            blob += f"\n{out_c}\n{err_c}\n{exc_c}"
            cli_ok = rc_c == 1 and exc_c == ""
    finally:
        if prev is None:
            os.environ.pop("OPENROUTER_API_KEY", None)
        else:
            os.environ["OPENROUTER_API_KEY"] = prev
    or_codes = {f["code"] for f in rep_or["findings"]}
    run.require(
        "f14/openrouter-redaction",
        "malformed-key-rejected-and-never-leaked",
        rc_or == 1 and cli_ok and "openrouter-key-invalid" in or_codes and canary not in blob,
    )

    rep_neg, rc_neg = run_preflight(
        [str(valid_task)],
        key_snapshot={"data": {"limit": -5.0, "usage": -1.0}},
        credits_snapshot={"data": {"total_credits": 0.0, "total_usage": -10.0}},
    )
    neg_codes = {f["code"] for f in rep_neg["findings"]}
    run.require(
        "f14/openrouter-negative",
        "negative-credits-and-key-rejected",
        rc_neg == 1 and {"openrouter-key-invalid", "openrouter-account-invalid"} <= neg_codes,
        str(neg_codes),
    )

    rep_zero, rc_zero = run_preflight(
        [str(valid_task)],
        min_key_usd=0.0,
        min_account_usd=0.0,
        key_snapshot={"data": {"limit": 100.0, "limit_remaining": 0.0, "usage": 100.05}},
        credits_snapshot={"data": {"total_credits": 10.0, "total_usage": 10.0}},
    )
    z_codes = {f["code"] for f in rep_zero["findings"]}
    run.require(
        "f14/openrouter-exhausted",
        "zero-remaining-fails-even-at-zero-min",
        rc_zero == 1 and {"openrouter-key-limit-low", "openrouter-account-low"} <= z_codes,
        str(z_codes),
    )


def _exercise_f3_f4_stats_and_pairing(run: SimRun, root: Path) -> None:
    p_tiny, ex_tiny = _sign_flip_two_sided([5e-14] * 16, seed=0)
    p_mix, ex_mix = _sign_flip_two_sided([1.0, 1e-13], seed=0)
    run.require(
        "f14/signflip-tol",
        "tiny-effects-not-inflated-to-one",
        ex_tiny is True and ex_mix is True and math.isclose(p_tiny, 2.0 / 65536.0, rel_tol=1e-12) and math.isclose(p_mix, 0.5, rel_tol=1e-12),
        f"p_tiny={p_tiny} p_mix={p_mix}",
    )

    unp_a = root / "unp_a"
    unp_b = root / "unp_b"
    write_olam_trial(unp_a / "fa__c1__i01__Aa11Bb2", trial_name="fa__c1__i01__Aa11Bb2", reward=1.0)
    write_olam_trial(unp_a / "fa__c1__i02__Aa11Bb3", trial_name="fa__c1__i02__Aa11Bb3", reward=0.0, stalled_seat="p1", duration_s=500.0)
    write_olam_trial(unp_b / "fb__c2__i01__Cc33Dd4", trial_name="fb__c2__i01__Cc33Dd4", reward=1.0)
    rep_unp, rc_unp = run_compare([f"a={unp_a}", f"b={unp_b}"], seed=0)
    pu = rep_unp["pairs"][0]
    rendered_unp = render_compare(rep_unp)
    run.require(
        "f14/zero-paired",
        "zero-paired-returns-none-and-exits-one-with-measured-mean",
        rc_unp == 1
        and rep_unp["passed"] is False
        and pu["n_paired"] == 0
        and pu["zero_paired_keys"] is True
        and pu["sign_flip_p"] is None
        and pu["mcnemar_p"] is None
        and pu["only_in_a"] == 2
        and pu["only_in_b"] == 1
        and "1.0000" in rendered_unp
        and "0.5000" not in rendered_unp
        and "n/a" in rendered_unp,
    )

    mf_a = root / "mf_a"
    mf_b = root / "mf_b"
    for fam, ca, cb in (("boxes_v2", "default", "team-3"), ("field", "indep-8", "team-8")):
        for idx in (1, 2):
            ta = f"{fam}__{ca}__i0{idx}__Aa11Bb{idx}" if ca != "default" else f"{fam}__i0{idx}"
            tb = f"{fam}__{cb}__i0{idx}__Cc22Dd{idx}"
            write_olam_trial(mf_a / ta, trial_name=ta, reward=0.8)
            write_olam_trial(mf_b / tb, trial_name=tb, reward=0.4)
    write_olam_trial(mf_a / "field__indep-8__i03__Aa11Bb9", trial_name="field__indep-8__i03__Aa11Bb9", reward=0.8)
    rep_mf, rc_mf = run_compare([f"a={mf_a}", f"b={mf_b}"], seed=0)
    pmf = rep_mf["pairs"][0]
    run.require(
        "f14/per-family-fallback",
        "multi-family-and-default-pair-cleanly-with-subset",
        rc_mf == 0 and pmf["pairing_mode"] == "family_instance" and pmf["n_paired"] == 4 and pmf["only_in_a"] == 1 and pmf["only_in_b"] == 0,
        str(pmf),
    )


def _exercise_f4_keys_and_f6_html(run: SimRun, root: Path) -> None:
    k_low1 = _key_from_trial_name("trustcal_v1__no-history-anchor__uogdgw6", "muster/trustcal_v1-i01")
    k_low2 = _key_from_trial_name("trustcal_v1__no-history-anchor__8z4i6sm", "muster/trustcal_v1-i02")
    k_tr1 = _key_from_trial_name("crowdscale_v1__bystander-k16__i0__pwifg57", "muster/crowdscale_v1-i01")
    k_tr2 = _key_from_trial_name("crowdscale_v1__bystander-k16__i0__uogdgw6", "muster/crowdscale_v1-i02")
    k_p1 = _key_from_trial_name("probe__release", "probe")
    k_p2 = _key_from_trial_name("probe__execute", "probe")
    real_ext = root / "ext_real" / "t_dir"
    real_ext.mkdir(parents=True, exist_ok=True)
    (real_ext / "task.toml").write_text('[metadata]\nfamily = "secret_f"\ncondition = "secret_c"\ninstance = 9\n', encoding="utf-8")
    sym_anc = root / "sym_anc"
    os.symlink(root / "ext_real", sym_anc)
    k_sym = _key_from_task_path(str(sym_anc / "t_dir"))
    run.require(
        "f14/key-extraction",
        "lowercase-hash-truncation-2part-and-symlink-safe",
        k_low1 == ("trustcal_v1", "no-history-anchor", "i01")
        and k_low2 == ("trustcal_v1", "no-history-anchor", "i02")
        and k_tr1 == ("crowdscale_v1", "bystander-k16", "i01")
        and k_tr2 == ("crowdscale_v1", "bystander-k16", "i02")
        and k_p1 != k_p2
        and k_sym != ("secret_f", "secret_c", "i09"),
    )

    n_pairs = 1_300 if (run.seed in (0, 114) or run.seed % 8 == 0) else 24
    synth_rep: dict[str, Any] = {
        "command": "compare", "alpha": 0.05, "stall_seconds": 300.0, "seed": 0, "passed": True,
        "differential_pairs": [], "zero_paired_pairs": [],
        "arms": [
            {"arm": "clean_arm", "total_trials": 5, "measured": 5, "unmeasured_rate": 0.0, "mean_reward": 1.0, "mean_measured_reward": 1.0, "binary_pass_rate": 1.0, "mean_cost_usd": 0.01, "total_cost_usd": 0.05, "detector_rates": dict.fromkeys(DETECTOR_NAMES, 0.0), "detector_counts": dict.fromkeys(DETECTOR_NAMES, 0)},
            {"arm": "all_dets", "total_trials": 5, "measured": 5, "unmeasured_rate": 0.0, "mean_reward": 0.2, "mean_measured_reward": 0.2, "binary_pass_rate": 0.0, "mean_cost_usd": 0.01, "total_cost_usd": 0.05, "detector_rates": dict.fromkeys(DETECTOR_NAMES, 0.2), "detector_counts": dict.fromkeys(DETECTOR_NAMES, 1)},
        ],
        "pairs": [
            {"arm_a": f"a{i}", "arm_b": "b0", "n_paired": 5, "mean_delta": 0.1, "sign_flip_p": 0.25, "mcnemar_p": 0.5, "attrition_fisher_p": 1.0, "differential_attrition": False, "zero_paired_keys": False, "cost_delta_usd": 0.0, "detector_deltas": {}}
            for i in range(n_pairs)
        ],
    }
    html_big = render_compare_html(synth_rep)
    run.require(
        "f14/compare-html",
        "zero-dets-excluded-from-filter-11-dets-and-1300-pairs-rendered",
        'data-filter="clean_arm"' in html_big
        and all(d in html_big for d in DETECTOR_NAMES)
        and html_big.count("<tr data-filter=") == n_pairs + 2,
    )


def _exercise_f7_preflight_and_cli(
    run: SimRun,
    root: Path,
    valid_task: Path,
    run_cli: Callable[[list[str]], tuple[int, str, str, str]] | None,
) -> None:
    nested_root = root / "nested_tasks"
    for cond in ("cond_a", "cond_b"):
        td = nested_root / "fam_z" / "build" / cond / "fam_z-i01"
        _write_minimal_task(
            td,
            toml_text='[metadata]\nfamily = "fam_z"\ncondition = "c"\ninstance = 1\n[agent]\ntimeout_sec = 60.0\n',
            with_seat_dir=False,
        )
    rep_nest, rc_nest = run_preflight([str(nested_root)])
    rep_file, rc_file = run_preflight([str(valid_task / "task.toml")])
    lbls = {t["task"] for t in rep_nest["tasks"]}
    safe_pf_txt = render_preflight({"passed": True, "openrouter": {"checked": True, "passed": True}})
    run.require(
        "f14/preflight-depth-and-file",
        "depth4-cond-labels-and-task-toml-target-work",
        rc_nest == 0 and lbls == {"cond_a/fam_z-i01", "cond_b/fam_z-i01"} and rc_file == 0 and rep_file["tasks_checked"] == 1 and "min=n/a" in safe_pf_txt,
        str(lbls),
    )

    if run_cli is not None:
        bad_html_dir = root / "dir_as_html"
        bad_html_dir.mkdir(parents=True, exist_ok=True)
        a_dir = root / "cli_a"
        b_dir = root / "cli_b"
        write_olam_trial(a_dir / "f__c__i01__Aa11Bb2", trial_name="f__c__i01__Aa11Bb2", reward=1.0)
        write_olam_trial(b_dir / "f__c__i01__Cc22Dd3", trial_name="f__c__i01__Cc22Dd3", reward=0.5)
        rc_h, _, err_h, exc_h = run_cli(["compare", str(a_dir), str(b_dir), "--html", str(bad_html_dir)])
        rc_a0, _, _, _ = run_cli(["compare", str(a_dir), str(b_dir), "--alpha", "0.0"])
        rc_a1, _, _, _ = run_cli(["compare", str(a_dir), str(b_dir), "--alpha", "1.0"])
        rc_mk, _, _, _ = run_cli(["preflight", str(valid_task), "--min-key-usd", "0"])
        run.require(
            "f14/cli-errors",
            "bad-html-exits-3-and-alpha-bounds-exit-2",
            rc_h == 3 and exc_h == "" and "worldwatch:" in err_h and rc_a0 == 2 and rc_a1 == 2 and rc_mk == 2,
            f"rc_h={rc_h} exc={exc_h!r} a0={rc_a0} a1={rc_a1} mk={rc_mk}",
        )


def exercise_workflow_redteam_f1_f7(
    run: SimRun,
    root: Path,
    run_cli: Callable[[list[str]], tuple[int, str, str, str]] | None = None,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    valid_task = _exercise_f1_compose(run, root / "f1")
    _exercise_f1_toml_and_di(run, root / "f1_tm")
    _exercise_f2_openrouter_security(run, valid_task, run_cli)
    _exercise_f3_f4_stats_and_pairing(run, root / "f3_f4")
    _exercise_f4_keys_and_f6_html(run, root / "f4_f6")
    _exercise_f7_preflight_and_cli(run, root / "f7", valid_task, run_cli)
