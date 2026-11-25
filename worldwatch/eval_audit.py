from __future__ import annotations

import os
import stat
import sys
import tempfile
import tomllib
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

from worldwatch._eval_audit_replay import (
    build_sys_paths,
    discover_family_modules,
    replay_single_episode,
    resolve_instances,
    resolve_seat_tokens,
    write_synthetic_trial,
)
from worldwatch._io import check_no_symlink_ancestors, read_regular_text, stat_regular_file
from worldwatch.detectors import analyze_trial_detectors
from worldwatch.model import DETECTOR_NAMES, InputError, as_list, as_map, finite_float, strict_int
from worldwatch.preflight import run_preflight

_EPS = 1e-6
_DEFAULT_MIN_SK_ORACLE = 1.0
_DEFAULT_MAX_S0K_ORACLE = 0.40
_DEFAULT_MAX_SK_NAIVE = 0.45


def _bad_input(target: Path, reason: str) -> tuple[dict[str, Any], int]:
    return (
        {
            "command": "certify-suite",
            "family_dir": str(target),
            "family": target.name,
            "status": "bad_input",
            "passed": False,
            "error": reason,
            "findings": [{"code": "bad-input", "severity": "error", "message": reason}],
        },
        2,
    )


def _pick_float(table: Mapping[str, Any], keys: tuple[str, ...], default: float) -> float:
    for key in keys:
        if key in table and (val := finite_float(table.get(key))) is not None:
            return val
    return default


def _pick_opt_float(table: Mapping[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key in table and (val := finite_float(table.get(key))) is not None:
            return val
    return None


def _parse_expected_detectors(
    raw_exp: Any,
    findings: list[dict[str, Any]],
) -> list[tuple[str | None, str, tuple[str, ...]]]:
    if raw_exp is None:
        return []
    if not isinstance(raw_exp, Mapping):
        findings.append({"code": "invalid-triad-metadata", "severity": "error", "message": "[triad.expected_detectors] must be a table"})
        return []
    valid_dets = frozenset(DETECTOR_NAMES)
    rules: list[tuple[str | None, str, tuple[str, ...]]] = []
    for k, val in sorted(raw_exp.items()):
        if isinstance(val, Mapping):
            for sub_pol, sub_val in sorted(val.items()):
                items = [sub_val] if isinstance(sub_val, str) else list(as_list(sub_val))
                names = tuple(str(x) for x in items if isinstance(x, str) and x)
                bad = [d for d in names if d not in valid_dets]
                if bad or not names:
                    findings.append({"code": "invalid-expected-detector", "severity": "error", "message": f"invalid expected detectors for {k}/{sub_pol}: {bad or sub_val!r}"})
                rules.append((str(k), str(sub_pol), tuple(d for d in names if d in valid_dets)))
            continue
        items = [val] if isinstance(val, str) else list(as_list(val))
        names = tuple(str(x) for x in items if isinstance(x, str) and x)
        bad = [d for d in names if d not in valid_dets]
        if bad or not names:
            findings.append({"code": "invalid-expected-detector", "severity": "error", "message": f"invalid expected detectors for {k!r}: {bad or val!r}"})
        if "/" in k or ":" in k:
            sep = "/" if "/" in k else ":"
            c_part, _, p_part = k.partition(sep)
            rules.append((c_part.strip() or None, p_part.strip(), tuple(d for d in names if d in valid_dets)))
        else:
            rules.append((None, str(k).strip(), tuple(d for d in names if d in valid_dets)))
    return rules


def _validate_triad_config(config: Mapping[str, Any], findings: list[dict[str, Any]]) -> dict[str, Any]:
    conds = as_map(config.get("conditions"))
    raw_triad = config.get("triad")
    if not isinstance(raw_triad, Mapping) or not raw_triad:
        findings.append({"code": "missing-triad-metadata", "severity": "error", "message": "family.toml must define non-empty [triad] metadata"})
        return {
            "valid": False, "s1_condition": None, "s0k_condition": None, "sk_condition": None,
            "sk_guard_condition": None, "min_coordination_gain": 0.20,
            "min_sk_oracle": _DEFAULT_MIN_SK_ORACLE, "max_s0k_oracle": _DEFAULT_MAX_S0K_ORACLE,
            "max_sk_naive": _DEFAULT_MAX_SK_NAIVE, "max_s1_oracle": None, "expected_detectors": [],
        }
    s1, s0k, sk, sk_g = raw_triad.get("s1_condition"), raw_triad.get("s0k_condition"), raw_triad.get("sk_condition"), raw_triad.get("sk_guard_condition")
    for key_nm, val in (("s1_condition", s1), ("s0k_condition", s0k), ("sk_condition", sk)):
        if not isinstance(val, str) or not val.strip() or val not in conds:
            findings.append({"code": "invalid-triad-metadata", "severity": "error", "message": f"[triad].{key_nm}={val!r} missing or not in [conditions]"})
    if sk_g is not None and sk_g != "" and (not isinstance(sk_g, str) or sk_g not in conds):
        findings.append({"code": "invalid-triad-metadata", "severity": "error", "message": f"[triad].sk_guard_condition={sk_g!r} not in [conditions]"})
    role_vals = [x for x in (s1, s0k, sk, sk_g if sk_g else None) if isinstance(x, str) and x]
    if len(set(role_vals)) != len(role_vals):
        findings.append({"code": "invalid-triad-metadata", "severity": "error", "message": "[triad] role conditions must be distinct"})
    raw_gain = finite_float(raw_triad.get("min_coordination_gain"))
    if raw_gain is None or not (0.0 <= raw_gain <= 1.0):
        findings.append({"code": "invalid-triad-metadata", "severity": "error", "message": "[triad].min_coordination_gain must be a finite float in [0.0, 1.0]"})
        min_gain = 0.20
    else:
        min_gain = raw_gain
    exp_rules = _parse_expected_detectors(raw_triad.get("expected_detectors"), findings)
    s1_s = s1 if isinstance(s1, str) and s1 in conds else None
    s0k_s = s0k if isinstance(s0k, str) and s0k in conds else None
    sk_s = sk if isinstance(sk, str) and sk in conds else None
    skg_s = sk_g if isinstance(sk_g, str) and sk_g in conds else None
    return {
        "valid": bool(s1_s and s0k_s and sk_s and raw_gain is not None),
        "s1_condition": s1_s, "s0k_condition": s0k_s, "sk_condition": sk_s, "sk_guard_condition": skg_s,
        "min_coordination_gain": min_gain,
        "min_sk_oracle": _pick_float(raw_triad, ("min_sk_oracle", "sk_oracle_min"), _DEFAULT_MIN_SK_ORACLE),
        "max_s0k_oracle": _pick_float(raw_triad, ("max_s0k_oracle", "s0k_oracle_max", "max_s0k", "s0k_max"), _DEFAULT_MAX_S0K_ORACLE),
        "max_sk_naive": _pick_float(raw_triad, ("max_sk_naive", "sk_naive_max", "max_naive_sk", "naive_max"), _DEFAULT_MAX_SK_NAIVE),
        "max_s1_oracle": _pick_opt_float(raw_triad, ("max_s1_oracle", "s1_oracle_max", "max_s1")),
        "expected_detectors": exp_rules,
    }


def _collect_policies(
    config: Mapping[str, Any],
    mods: Mapping[str, ModuleType | None],
    exp_rules: list[tuple[str | None, str, tuple[str, ...]]],
) -> tuple[list[str], dict[str, Any], list[str], list[str]]:
    pol_map: dict[str, Any] = {}
    ordered: list[str] = ["oracle", "nop"]
    for mod in (mods.get("policies"), mods.get("ref")):
        raw_p = getattr(mod, "POLICIES", None) if mod is not None else None
        if isinstance(raw_p, Mapping):
            for k, v in raw_p.items():
                if isinstance(k, str):
                    pol_map.setdefault(k, v)
                    if k not in ordered:
                        ordered.append(k)
        elif isinstance(raw_p, (list, tuple, set, frozenset)):
            for item in raw_p:
                if isinstance(item, str) and item not in ordered:
                    ordered.append(item)
    cfg_pols = config.get("policies")
    if isinstance(cfg_pols, Mapping):
        ordered.extend(str(k) for k in cfg_pols if isinstance(k, str) and str(k) not in ordered)
    elif isinstance(cfg_pols, list):
        ordered.extend(str(x) for x in cfg_pols if isinstance(x, str) and str(x) not in ordered)
    raw_triad = as_map(config.get("triad"))
    explicit_naive: list[str] = []
    if isinstance(raw_triad.get("naive_policy"), str) and raw_triad["naive_policy"]:
        explicit_naive.append(raw_triad["naive_policy"])
    for np in as_list(raw_triad.get("naive_policies")):
        if isinstance(np, str) and np and np not in explicit_naive:
            explicit_naive.append(np)
    exp_cfg = {str(x) for src in (config.get("exploits"), raw_triad.get("exploits")) for x in as_list(src) if isinstance(x, str)}
    for name in (*explicit_naive, *sorted(exp_cfg), *(r[1] for r in exp_rules)):
        if name and name not in ordered:
            ordered.append(name)
    for name in ordered:
        for mod in (mods.get("policies"), mods.get("ref")):
            if mod is not None and name not in pol_map and callable(getattr(mod, name, None)):
                pol_map[name] = getattr(mod, name)
    exploits = [
        p for p in ordered
        if p not in ("oracle", "nop") and (p in exp_cfg or p.lower() == "spoof" or p.lower().startswith(("exploit", "spoof_", "spoof-")))
    ]
    naive_pols = explicit_naive if explicit_naive else [p for p in ordered if p not in ("oracle", "nop", *exploits)]
    return ordered, pol_map, naive_pols, exploits


def _replay_cond_policy(
    family_dir: Path,
    family_name: str,
    config: Mapping[str, Any],
    mods: Mapping[str, ModuleType | None],
    cond: str,
    pol: str,
    inst_list: list[dict[str, Any]],
    pol_map: Mapping[str, Any],
    det_root: Path,
    sk_cond: str | None,
    skg_cond: str | None,
    need_det: bool,
    oracle_fps: list[dict[str, Any]],
    cp_hits: Counter[str],
    pol_hits: Counter[str],
    findings: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, int, int, bool]:
    rews: list[float] = []
    c_ok_all = l_ok_all = rt_ok_all = True
    ep_cnt = chained_cnt = 0
    used_disp = False
    for inst in inst_list:
        inum = strict_int(inst.get("instance")) or 1
        toks = resolve_seat_tokens(config, inst, mods.get("ref"))
        ep, ep_err = replay_single_episode(mods, inst, toks, pol, pol_map)
        if ep_err is not None or ep is None:
            findings.append({"code": "episode-replay-failed", "severity": "error", "message": f"{cond}/{pol}/i{inum:02d}: {ep_err}"})
            continue
        ep_cnt += 1
        used_disp = used_disp or bool(ep["used_dispatch"])
        rews.append(float(ep["reward"]))
        if not ep["log_intact"]:
            l_ok_all = False
            findings.append({"code": "world-log-not-intact", "severity": "error", "message": f"{cond}/{pol}/i{inum:02d}: world log_intact is False"})
        if ep["chain_ok"] is True:
            chained_cnt += 1
        elif ep["chain_ok"] is False:
            c_ok_all = False
            findings.append({"code": "tampered-hash-chain", "severity": "error", "message": f"{cond}/{pol}/i{inum:02d}: {ep['chain_error']}"})
        if not ep["roundtrip_ok"]:
            rt_ok_all = False
            findings.append({"code": "verify-roundtrip-mismatch", "severity": "error", "message": f"{cond}/{pol}/i{inum:02d}: {ep['roundtrip_error']}"})
        if (pol == "oracle" and cond in (sk_cond, skg_cond)) or need_det:
            t_dir = det_root / f"{family_name}__{cond}__i{inum:02d}__{pol}"
            b_task = family_dir / "build" / cond / f"{family_name}-i{inum:02d}"
            write_synthetic_trial(t_dir, family_name, inst, pol, ep["raw_bytes"], ep["score"], b_task, ep["atif"])
            trig = [str(x) for x in analyze_trial_detectors(t_dir, "olam")["triggered"]]
            if pol == "oracle" and cond == sk_cond and trig:
                oracle_fps.append({"condition": cond, "instance": inum, "triggered": trig})
                findings.append({"code": "oracle-detector-false-positive", "severity": "error", "message": f"clean Sk(oracle) {cond}/i{inum:02d} triggered detectors: {trig}"})
            if need_det:
                for dname in trig:
                    cp_hits[dname] += 1
                    pol_hits[dname] += 1
    row = (
        {
            "instances": len(rews), "mean": round(sum(rews) / len(rews), 6), "min": round(min(rews), 6),
            "max": round(max(rews), 6), "chain_ok": c_ok_all, "log_intact": l_ok_all, "roundtrip_ok": rt_ok_all,
        }
        if rews
        else None
    )
    return row, ep_cnt, chained_cnt, used_disp


def _replay_matrix_and_detectors(
    family_dir: Path,
    family_name: str,
    config: Mapping[str, Any],
    mods: Mapping[str, ModuleType | None],
    policies: list[str],
    pol_map: Mapping[str, Any],
    triad_info: Mapping[str, Any],
    naive_pols: list[str],
    findings: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, Any]]:
    conds = [str(k) for k in as_map(config.get("conditions"))]
    sk_cond, skg_cond = triad_info.get("sk_condition"), triad_info.get("sk_guard_condition")
    exp_rules: list[tuple[str | None, str, tuple[str, ...]]] = list(triad_info.get("expected_detectors") or [])
    need_det_pols = set(naive_pols) | {r[1] for r in exp_rules}
    matrix: dict[str, dict[str, dict[str, Any]]] = {}
    oracle_fps: list[dict[str, Any]] = []
    det_hits_by_cp: dict[tuple[str, str], Counter[str]] = {}
    det_hits_by_pol: dict[str, Counter[str]] = {}
    used_dispatch_any = False
    chained_episodes = total_episodes = 0
    with tempfile.TemporaryDirectory(prefix="ww-audit-trials-") as det_tmp:
        det_root = Path(det_tmp)
        for cond in conds:
            inst_list, ierr = resolve_instances(config, cond, family_dir, mods.get("ref"))
            if ierr is not None or not inst_list:
                findings.append({"code": "instance-resolution-failed", "severity": "error", "message": ierr or f"no instances for {cond}"})
                continue
            matrix[cond] = {}
            for pol in policies:
                row, ecnt, ccnt, udisp = _replay_cond_policy(
                    family_dir, family_name, config, mods, cond, pol, inst_list, pol_map, det_root,
                    sk_cond, skg_cond, (pol in need_det_pols), oracle_fps,
                    det_hits_by_cp.setdefault((cond, pol), Counter()), det_hits_by_pol.setdefault(pol, Counter()), findings,
                )
                total_episodes += ecnt
                chained_episodes += ccnt
                used_dispatch_any = used_dispatch_any or udisp
                if row is not None:
                    matrix[cond][pol] = row
    for cond_flt, pol_nm, exp_dets in exp_rules:
        counts = det_hits_by_cp.get((cond_flt, pol_nm), Counter()) if cond_flt else det_hits_by_pol.get(pol_nm, Counter())
        for dname in exp_dets:
            if counts.get(dname, 0) == 0:
                findings.append({"code": "missing-expected-detector", "severity": "error", "message": f"expected detector {dname!r} never fired on {f'{cond_flt}/{pol_nm}' if cond_flt else pol_nm}"})
    return matrix, {
        "episodes_replayed": total_episodes, "chained_episodes": chained_episodes,
        "engine": "kit_v2_dispatch" if used_dispatch_any else "world_handle",
        "sk_oracle_false_positives": oracle_fps,
        "naive_detector_hits": {pol: dict(sorted(cnt.items())) for pol, cnt in sorted(det_hits_by_pol.items()) if cnt},
    }


def _enforce_triad_and_baselines(
    matrix: Mapping[str, Mapping[str, Mapping[str, Any]]],
    triad_info: Mapping[str, Any],
    naive_pols: list[str],
    exploits: list[str],
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    for cond, pol_rows in sorted(matrix.items()):
        nop_row = pol_rows.get("nop")
        if nop_row is None:
            findings.append({"code": "missing-nop-score", "severity": "error", "message": f"nop not evaluated in {cond}"})
        elif float(nop_row["max"]) > _EPS or abs(float(nop_row["mean"])) > _EPS:
            findings.append({"code": "nonzero-nop-reward", "severity": "error", "message": f"nop scored mean={nop_row['mean']} max={nop_row['max']} in {cond} (must be 0.0)"})
        for epol in exploits:
            erow = pol_rows.get(epol)
            if erow is None:
                findings.append({"code": "missing-exploit-score", "severity": "error", "message": f"exploit {epol!r} not evaluated in {cond}"})
            elif float(erow["max"]) > _EPS or abs(float(erow["mean"])) > _EPS:
                findings.append({"code": "exploit-earns-reward", "severity": "error", "message": f"exploit {epol!r} scored max={erow['max']} in {cond} (must be 0.0)"})
    if not exploits:
        findings.append({"code": "missing-exploit-policy", "severity": "error", "message": "family defines no exploit/spoof policy"})
    if not naive_pols:
        findings.append({"code": "missing-naive-policy", "severity": "error", "message": "family defines no naive failure baseline policy"})
    s1_c, s0k_c, sk_c, skg_c = triad_info.get("s1_condition"), triad_info.get("s0k_condition"), triad_info.get("sk_condition"), triad_info.get("sk_guard_condition")
    s1_o = finite_float(as_map(as_map(matrix.get(str(s1_c))).get("oracle")).get("mean")) if s1_c else None
    s0k_o = finite_float(as_map(as_map(matrix.get(str(s0k_c))).get("oracle")).get("mean")) if s0k_c else None
    sk_o = finite_float(as_map(as_map(matrix.get(str(sk_c))).get("oracle")).get("mean")) if sk_c else None
    skg_o = finite_float(as_map(as_map(matrix.get(str(skg_c))).get("oracle")).get("mean")) if skg_c else None
    min_sk, max_s0k, max_naive, min_gain = float(triad_info["min_sk_oracle"]), float(triad_info["max_s0k_oracle"]), float(triad_info["max_sk_naive"]), float(triad_info["min_coordination_gain"])
    max_s1 = triad_info.get("max_s1_oracle")
    if sk_o is None:
        findings.append({"code": "missing-triad-oracle", "severity": "error", "message": f"missing Sk(oracle) score for {sk_c!r}"})
    elif sk_o + _EPS < min_sk:
        findings.append({"code": "triad-sk-oracle-low", "severity": "error", "message": f"Sk(oracle)={sk_o:.4f} < required {min_sk:.4f}"})
    if s0k_o is None:
        findings.append({"code": "missing-triad-oracle", "severity": "error", "message": f"missing S0k(oracle) score for {s0k_c!r}"})
    elif s0k_o > max_s0k + _EPS:
        findings.append({"code": "triad-s0k-oracle-high", "severity": "error", "message": f"S0k(oracle)={s0k_o:.4f} > allowed max {max_s0k:.4f}"})
    if s1_o is None and s1_c:
        findings.append({"code": "missing-triad-oracle", "severity": "error", "message": f"missing S1(oracle) score for {s1_c!r}"})
    elif s1_o is not None and isinstance(max_s1, float) and s1_o > max_s1 + _EPS:
        findings.append({"code": "triad-s1-oracle-high", "severity": "error", "message": f"S1(oracle)={s1_o:.4f} > allowed max {max_s1:.4f}"})
    if skg_c and (skg_o is None or skg_o + _EPS < min_sk):
        findings.append({"code": "triad-sk-guard-oracle-low", "severity": "error", "message": f"Sk_guard(oracle)={skg_o} < required {min_sk:.4f}"})
    sk_naive_scores: dict[str, float] = {}
    if sk_c and sk_c in matrix:
        for npol in naive_pols:
            if (nv := finite_float(as_map(matrix[sk_c].get(npol)).get("mean"))) is not None:
                sk_naive_scores[npol] = nv
                if nv > max_naive + _EPS:
                    findings.append({"code": "triad-sk-naive-high", "severity": "error", "message": f"Sk({npol})={nv:.4f} > allowed max {max_naive:.4f}"})
    coord_gain = round(sk_o - s0k_o, 6) if (sk_o is not None and s0k_o is not None) else None
    if coord_gain is not None and coord_gain + _EPS < min_gain:
        findings.append({"code": "insufficient-coordination-gain", "severity": "error", "message": f"Sk(oracle)-S0k(oracle)={coord_gain:.4f} < min_coordination_gain={min_gain:.4f}"})
    return {
        "s1_condition": s1_c, "s0k_condition": s0k_c, "sk_condition": sk_c, "sk_guard_condition": skg_c,
        "s1_oracle": s1_o, "s0k_oracle": s0k_o, "sk_oracle": sk_o, "sk_guard_oracle": skg_o,
        "sk_naive": sk_naive_scores, "headcount_delta": round(s0k_o - s1_o, 6) if (s0k_o is not None and s1_o is not None) else None,
        "coordination_gain": coord_gain, "guard_delta": round(skg_o - sk_o, 6) if (skg_o is not None and sk_o is not None) else None,
        "min_coordination_gain": min_gain, "max_s0k_oracle": max_s0k, "max_sk_naive": max_naive,
    }


def _run_build_preflight(family_dir: Path, findings: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        pf_rep, pf_rc = run_preflight([str(family_dir / "build")])
    except InputError as exc:
        findings.append({"code": "preflight-build-error", "severity": "error", "message": str(exc)})
        return {"passed": False, "tasks_checked": 0, "findings_count": 1, "error": str(exc)}
    if pf_rc != 0 or not pf_rep.get("passed", False) or int(pf_rep.get("findings_count", 0)) > 0:
        sample_codes = sorted({str(f.get("code")) for f in as_list(pf_rep.get("findings")) if isinstance(f, Mapping)})[:8]
        findings.append({"code": "preflight-failed", "severity": "error", "message": f"preflight found {pf_rep.get('findings_count', 0)} error(s) in build/: {sample_codes}"})
    return {
        "passed": bool(pf_rep.get("passed")) and pf_rc == 0, "tasks_checked": int(pf_rep.get("tasks_checked", 0)),
        "tasks_passed": int(pf_rep.get("tasks_passed", 0)), "findings_count": int(pf_rep.get("findings_count", 0)),
        "findings": list(as_list(pf_rep.get("findings"))[:25]),
    }


def _execute_in_sandbox(
    target: Path,
    family_name: str,
    config: Mapping[str, Any],
    triad_info: Mapping[str, Any],
    findings: list[dict[str, Any]],
) -> tuple[list[str], list[str], list[str], dict[str, Any], dict[str, Any], dict[str, Any]]:
    saved_path, saved_flag, saved_mods = list(sys.path), sys.dont_write_bytecode, dict(sys.modules)
    sys.dont_write_bytecode = True
    for extra_p in reversed(build_sys_paths(target)):
        if extra_p in sys.path:
            sys.path.remove(extra_p)
        sys.path.insert(0, extra_p)
    for stale_mod in ("codec", "worldkit", "build", "world", "verify", "policies"):
        sys.modules.pop(stale_mod, None)
    try:
        mods, mod_err = discover_family_modules(target)
        if mod_err is not None:
            findings.append({"code": "module-load-failed", "severity": "error", "message": mod_err})
            empty_det = {"episodes_replayed": 0, "chained_episodes": 0, "engine": "none", "sk_oracle_false_positives": [], "naive_detector_hits": {}}
            return [], [], [], {}, empty_det, {}
        policies, pol_map, naive_pols, exploits = _collect_policies(config, mods, triad_info["expected_detectors"])
        matrix, det_meta = _replay_matrix_and_detectors(
            target, family_name, config, mods, policies, pol_map, triad_info, naive_pols, findings
        )
        triad_eval = _enforce_triad_and_baselines(matrix, triad_info, naive_pols, exploits, findings)
        return policies, naive_pols, exploits, triad_eval, det_meta, matrix
    finally:
        keep_ww = {k: v for k, v in sys.modules.items() if k == "worldwatch" or k.startswith("worldwatch.")}
        sys.path[:], sys.dont_write_bytecode = saved_path, saved_flag
        sys.modules.clear()
        sys.modules.update(saved_mods)
        sys.modules.update(keep_ww)


def _certify_one_family_suite(target: Path) -> tuple[dict[str, Any], int]:
    toml_path = target / "family.toml"
    t_info, t_err = stat_regular_file(toml_path, root=target)
    if t_err is not None or t_info is None:
        return _bad_input(target, t_err or "missing family.toml")
    toml_txt, read_err = read_regular_text(toml_path, root=target)
    if read_err is not None or toml_txt is None:
        return _bad_input(target, read_err or "cannot read family.toml")
    try:
        config = tomllib.loads(toml_txt)
    except tomllib.TOMLDecodeError as exc:
        return _bad_input(target, f"invalid family.toml: {exc}")
    if not isinstance(config.get("conditions"), Mapping) or not config["conditions"]:
        return _bad_input(target, "family.toml has no [conditions] table")
    if not any(os.path.lexists(target / rel) for rel in ("reference.py", "world.py", "template/world.py", "verify.py", "template/verify.py")):
        return _bad_input(target, "missing reference.py / world.py / verify.py in family directory")
    family_name = str(config.get("name") or target.name)
    findings: list[dict[str, Any]] = []
    triad_info = _validate_triad_config(config, findings)
    policies, naive_pols, exploits, triad_eval, det_meta, matrix = _execute_in_sandbox(
        target, family_name, config, triad_info, findings
    )
    pf_summary = _run_build_preflight(target, findings)
    passed = len(findings) == 0
    return (
        {
            "command": "certify-suite", "family_dir": str(target), "family": family_name,
            "status": "passed" if passed else "failed", "passed": passed,
            "policies": policies, "naive_policies": naive_pols, "exploits": exploits,
            "triad": triad_eval, "replay": det_meta, "matrix": matrix,
            "preflight": pf_summary, "findings_count": len(findings), "findings": findings,
        },
        0 if passed else 1,
    )


def run_certify_suite(family_dir: Path) -> tuple[dict[str, Any], int]:
    target = Path(family_dir)
    if (anc_err := check_no_symlink_ancestors(target)) is not None:
        return _bad_input(target, f"unsafe path: {anc_err}")
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return _bad_input(target, f"path does not exist: {target}")
    except OSError as exc:
        return _bad_input(target, f"cannot stat {target}: {exc.strerror or exc}")
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return _bad_input(target, f"not a regular directory: {target}")
    if os.path.lexists(target / "family.toml"):
        return _certify_one_family_suite(target)
    try:
        children = sorted(
            Path(e.path) for e in os.scandir(target)
            if e.is_dir(follow_symlinks=False) and not e.name.startswith((".", "_")) and os.path.lexists(Path(e.path) / "family.toml")
        )
    except OSError:
        children = []
    if not children:
        return _bad_input(target, "missing family.toml")
    triad_children = [
        c for c in children
        if ( pair := read_regular_text(c / "family.toml", root=c) )[1] is None and pair[0] is not None and "[triad]" in pair[0]
    ]
    reports: list[dict[str, Any]] = []
    worst_rc = 0
    for child in (triad_children if triad_children else children):
        rep, rc = _certify_one_family_suite(child)
        reports.append(rep)
        worst_rc = max(worst_rc, rc)
    passed_cnt = sum(1 for r in reports if r.get("passed") is True)
    all_ok = worst_rc == 0 and passed_cnt == len(reports)
    return (
        {
            "command": "certify-suite", "root_dir": str(target),
            "status": "passed" if all_ok else ("bad_input" if worst_rc == 2 else "failed"),
            "passed": all_ok, "families_checked": len(reports), "families_passed": passed_cnt, "families": reports,
        },
        0 if all_ok else worst_rc,
    )
