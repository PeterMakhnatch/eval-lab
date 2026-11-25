from __future__ import annotations

import math
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
    is_safe_ident,
    replay_single_episode,
    resolve_instances,
    resolve_seat_tokens,
    scan_family_symlinks,
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
_TRANSIENT_MODS = ("codec", "worldkit", "build", "world", "verify", "policies", "reference")


def _bad_input(target: Path, reason: str) -> tuple[dict[str, Any], int]:
    return ({"command": "certify-suite", "family_dir": str(target), "family": target.name or str(target), "status": "bad_input", "passed": False, "error": reason, "findings": [{"code": "bad-input", "severity": "error", "message": reason}]}, 2)


def _bounded_float(table: Mapping[str, Any], keys: tuple[str, ...], default: float | None, lo: float, hi: float, label: str, findings: list[dict[str, Any]]) -> float:
    for key in keys:
        if key in table:
            val = finite_float(table.get(key))
            if val is None or not (lo - 1e-12 <= val <= hi + 1e-12):
                findings.append({"code": "invalid-triad-metadata", "severity": "error", "message": f"[triad].{label}={table.get(key)!r} must be in [{lo}, {hi}]"})
                return max(lo, min(hi, val)) if val is not None else (default if default is not None else lo)
            return val
    if default is None:
        findings.append({"code": "invalid-triad-metadata", "severity": "error", "message": f"[triad].{label} is required in [{lo}, {hi}]"})
        return lo
    return default


def _opt_bounded_float(table: Mapping[str, Any], keys: tuple[str, ...], lo: float, hi: float, label: str, findings: list[dict[str, Any]]) -> float | None:
    for key in keys:
        if key in table and table.get(key) is not None:
            val = finite_float(table.get(key))
            if val is None or not (lo - 1e-12 <= val <= hi + 1e-12):
                findings.append({"code": "invalid-triad-metadata", "severity": "error", "message": f"[triad].{label}={table.get(key)!r} must be in [{lo}, {hi}]"})
                return None
            return val
    return None


def _parse_expected_detectors(
    raw_exp: Any, conds: Mapping[str, Any], findings: list[dict[str, Any]],
) -> list[tuple[str | None, str, tuple[str, ...]]]:
    if raw_exp is None:
        findings.append({"code": "missing-expected-detectors", "severity": "error", "message": "family.toml must define non-empty [triad.expected_detectors]"})
        return []
    if not isinstance(raw_exp, Mapping) or not raw_exp:
        findings.append({"code": "invalid-triad-metadata", "severity": "error", "message": "[triad.expected_detectors] must be a non-empty table"})
        return []
    valid_dets = frozenset(DETECTOR_NAMES)
    rules: list[tuple[str | None, str, tuple[str, ...]]] = []
    for k, val in sorted(raw_exp.items()):
        entries: list[tuple[str | None, str, Any]] = []
        if isinstance(val, Mapping):
            entries.extend((str(k), str(sub_pol), sub_val) for sub_pol, sub_val in sorted(val.items()))
        elif "/" in k or ":" in k:
            sep = "/" if "/" in k else ":"
            c_part, _, p_part = k.partition(sep)
            entries.append((c_part.strip() or None, p_part.strip(), val))
        else:
            entries.append((None, str(k).strip(), val))
        for c_flt, p_nm, raw_v in entries:
            items = [raw_v] if isinstance(raw_v, str) else list(as_list(raw_v))
            names = tuple(str(x) for x in items if isinstance(x, str) and x)
            bad = [d for d in names if d not in valid_dets]
            if c_flt is not None and c_flt not in conds:
                findings.append({"code": "invalid-expected-detector", "severity": "error", "message": f"unknown condition {c_flt!r} in [triad.expected_detectors]"})
            if not is_safe_ident(p_nm) or bad or not names:
                findings.append({"code": "invalid-expected-detector", "severity": "error", "message": f"invalid expected detectors for {c_flt or '*'}/{p_nm}: {bad or raw_v!r}"})
            rules.append((c_flt, p_nm, tuple(d for d in names if d in valid_dets)))
    if not any(r[2] for r in rules):
        findings.append({"code": "missing-expected-detectors", "severity": "error", "message": "[triad.expected_detectors] defines no valid detector rules"})
    return rules


def _check_triad_seat_counts(
    seat_counts: Mapping[str, int], s1_s: str | None, s0k_s: str | None, sk_s: str | None, skg_s: str | None, findings: list[dict[str, Any]]
) -> None:
    if any(f.get("code") == "invalid-triad-seats" for f in findings):
        return
    if s1_s in seat_counts and s0k_s in seat_counts and sk_s in seat_counts:
        n_s1, n_s0k, n_sk = seat_counts[str(s1_s)], seat_counts[str(s0k_s)], seat_counts[str(sk_s)]
        if not (1 <= n_s1 < n_s0k == n_sk):
            findings.append({"code": "invalid-triad-seats", "severity": "error", "message": f"triad seats must satisfy 1 <= len(S1)={n_s1} < len(S0k)={n_s0k} == len(Sk)={n_sk}"})
        elif skg_s and skg_s in seat_counts and seat_counts[skg_s] != n_sk:
            findings.append({"code": "invalid-triad-seats", "severity": "error", "message": f"Sk_guard seat count {seat_counts[skg_s]} != Sk seat count {n_sk}"})


def _validate_triad_config(config: Mapping[str, Any], findings: list[dict[str, Any]]) -> dict[str, Any]:
    conds = as_map(config.get("conditions"))
    raw_triad = config.get("triad")
    if not isinstance(raw_triad, Mapping) or not raw_triad:
        findings.append({"code": "missing-triad-metadata", "severity": "error", "message": "family.toml must define non-empty [triad] metadata"})
        return {
            "valid": False, "s1_condition": None, "s0k_condition": None, "sk_condition": None, "sk_guard_condition": None,
            "min_coordination_gain": 0.20, "min_sk_oracle": _DEFAULT_MIN_SK_ORACLE, "max_s0k_oracle": _DEFAULT_MAX_S0K_ORACLE,
            "max_sk_naive": _DEFAULT_MAX_SK_NAIVE, "min_s1_oracle": None, "max_s1_oracle": None, "expected_detectors": [],
        }
    s1, s0k, sk, sk_g = raw_triad.get("s1_condition"), raw_triad.get("s0k_condition"), raw_triad.get("sk_condition"), raw_triad.get("sk_guard_condition")
    for key_nm, val in (("s1_condition", s1), ("s0k_condition", s0k), ("sk_condition", sk)):
        if not is_safe_ident(val) or val not in conds:
            findings.append({"code": "invalid-triad-metadata", "severity": "error", "message": f"[triad].{key_nm}={val!r} missing, unsafe, or not in [conditions]"})
    if sk_g is not None and sk_g != "" and (not is_safe_ident(sk_g) or sk_g not in conds):
        findings.append({"code": "invalid-triad-metadata", "severity": "error", "message": f"[triad].sk_guard_condition={sk_g!r} unsafe or not in [conditions]"})
    role_vals = [x for x in (s1, s0k, sk, sk_g if sk_g else None) if isinstance(x, str) and x]
    if len(set(role_vals)) != len(role_vals):
        findings.append({"code": "invalid-triad-metadata", "severity": "error", "message": "[triad] role conditions must be distinct"})
    min_gain = _bounded_float(raw_triad, ("min_coordination_gain",), None, 0.20, 1.0, "min_coordination_gain", findings)
    min_sk = _bounded_float(raw_triad, ("min_sk_oracle", "sk_oracle_min"), _DEFAULT_MIN_SK_ORACLE, 0.50, 1.0, "min_sk_oracle", findings)
    max_s0k = _bounded_float(raw_triad, ("max_s0k_oracle", "s0k_oracle_max", "max_s0k", "s0k_max"), _DEFAULT_MAX_S0K_ORACLE, 0.0, 0.50, "max_s0k_oracle", findings)
    max_naive = _bounded_float(raw_triad, ("max_sk_naive", "sk_naive_max", "max_naive_sk", "naive_max"), _DEFAULT_MAX_SK_NAIVE, 0.0, 0.50, "max_sk_naive", findings)
    min_s1 = _opt_bounded_float(raw_triad, ("min_s1_oracle", "s1_oracle_min", "min_s1"), 0.0, 1.0, "min_s1_oracle", findings)
    max_s1 = _opt_bounded_float(raw_triad, ("max_s1_oracle", "s1_oracle_max", "max_s1"), 0.0, 1.0, "max_s1_oracle", findings)
    exp_rules = _parse_expected_detectors(raw_triad.get("expected_detectors"), conds, findings)
    s1_s = s1 if is_safe_ident(s1) and s1 in conds else None
    s0k_s = s0k if is_safe_ident(s0k) and s0k in conds else None
    sk_s = sk if is_safe_ident(sk) and sk in conds else None
    skg_s = sk_g if is_safe_ident(sk_g) and sk_g in conds else None
    cfg_seat_counts = {str(ck): len(as_list(as_map(cv).get("seats"))) for ck, cv in conds.items() if isinstance(as_map(cv).get("seats"), list)}
    _check_triad_seat_counts(cfg_seat_counts, s1_s, s0k_s, sk_s, skg_s, findings)
    return {
        "valid": bool(s1_s and s0k_s and sk_s), "s1_condition": s1_s, "s0k_condition": s0k_s, "sk_condition": sk_s,
        "sk_guard_condition": skg_s, "min_coordination_gain": min_gain, "min_sk_oracle": min_sk,
        "max_s0k_oracle": max_s0k, "max_sk_naive": max_naive, "min_s1_oracle": min_s1, "max_s1_oracle": max_s1,
        "expected_detectors": exp_rules,
    }


def _collect_policies(
    config: Mapping[str, Any], mods: Mapping[str, ModuleType | None], exp_rules: list[tuple[str | None, str, tuple[str, ...]]], findings: list[dict[str, Any]],
) -> tuple[list[str], dict[str, Any], list[str], list[str]]:
    pol_map: dict[str, Any] = {}
    raw_ordered: list[str] = ["oracle", "nop"]
    for mod in (mods.get("policies"), mods.get("ref")):
        raw_p = getattr(mod, "POLICIES", None) if mod is not None else None
        if isinstance(raw_p, Mapping):
            for k, v in raw_p.items():
                if isinstance(k, str):
                    pol_map.setdefault(k, v)
                    if k not in raw_ordered:
                        raw_ordered.append(k)
        elif isinstance(raw_p, (list, tuple, set, frozenset)):
            raw_ordered.extend(item for item in raw_p if isinstance(item, str) and item not in raw_ordered)
    cfg_pols = config.get("policies")
    if isinstance(cfg_pols, Mapping):
        raw_ordered.extend(str(k) for k in cfg_pols if isinstance(k, str) and str(k) not in raw_ordered)
    elif isinstance(cfg_pols, list):
        raw_ordered.extend(str(x) for x in cfg_pols if isinstance(x, str) and str(x) not in raw_ordered)
    raw_triad = as_map(config.get("triad"))
    explicit_naive: list[str] = []
    if isinstance(raw_triad.get("naive_policy"), str) and raw_triad["naive_policy"]:
        explicit_naive.append(raw_triad["naive_policy"])
    for np in as_list(raw_triad.get("naive_policies")):
        if isinstance(np, str) and np and np not in explicit_naive:
            explicit_naive.append(np)
    exp_cfg = {str(x) for src in (config.get("exploits"), raw_triad.get("exploits")) for x in as_list(src) if isinstance(x, str)}
    for name in (*explicit_naive, *sorted(exp_cfg), *(r[1] for r in exp_rules)):
        if name and name not in raw_ordered:
            raw_ordered.append(name)
    ordered: list[str] = []
    for name in raw_ordered:
        if not is_safe_ident(name):
            findings.append({"code": "invalid-policy-name", "severity": "error", "message": f"unsafe policy name: {name!r}"})
            continue
        ordered.append(name)
        for mod in (mods.get("policies"), mods.get("ref")):
            if mod is not None and name not in pol_map and callable(getattr(mod, name, None)):
                pol_map[name] = getattr(mod, name)
    exploits = [
        p for p in ordered
        if p not in ("oracle", "nop") and (p in exp_cfg or p.lower() == "spoof" or p.lower().startswith(("exploit", "spoof_", "spoof-")))
    ]
    reserved = frozenset(("oracle", "nop", *exploits))
    bad_naive = [p for p in explicit_naive if p in reserved or not is_safe_ident(p)]
    if bad_naive:
        findings.append({"code": "invalid-naive-policy", "severity": "error", "message": f"naive_policies cannot include oracle, nop, exploit, or unsafe names: {bad_naive}"})
    naive_src = explicit_naive if explicit_naive else ordered
    naive_pols = [p for p in naive_src if p in ordered and p not in reserved]
    return ordered, pol_map, naive_pols, exploits


def _replay_cond_policy(
    family_dir: Path, family_name: str, config: Mapping[str, Any], mods: Mapping[str, ModuleType | None],
    cond: str, pol: str, inst_list: list[dict[str, Any]], pol_map: Mapping[str, Any], det_root: Path,
    clean_oracle_conds: frozenset[str], need_det: bool, oracle_fps: list[dict[str, Any]],
    cp_hits: Counter[str], pol_hits: Counter[str], findings: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, int, int, bool]:
    rews: list[float] = []
    c_ok_all = l_ok_all = rt_ok_all = True
    ep_cnt = chained_cnt = 0
    used_disp = False
    real_det_root = det_root.resolve()
    for inst in inst_list:
        inum = strict_int(inst.get("instance")) or 1
        toks = resolve_seat_tokens(config, inst, mods.get("ref"))
        ep, ep_err = replay_single_episode(mods, inst, toks, pol, pol_map)
        if ep_err is not None or ep is None:
            findings.append({"code": "episode-replay-failed", "severity": "error", "message": f"{cond}/{pol}/i{inum:02d}: {ep_err}"})
            continue
        ep_cnt += 1
        used_disp = used_disp or bool(ep["used_dispatch"])
        rew_f = float(ep["reward"])
        rews.append(rew_f)
        if not (-_EPS <= rew_f <= 1.0 + _EPS):
            findings.append({"code": "out-of-range-reward", "severity": "error", "message": f"{cond}/{pol}/i{inum:02d}: reward {rew_f} outside [0.0, 1.0]"})
        if not ep["log_intact"]:
            l_ok_all = False
            findings.append({"code": "world-log-not-intact", "severity": "error", "message": f"{cond}/{pol}/i{inum:02d}: world log_intact is False"})
        if ep["chain_ok"] is True:
            chained_cnt += 1
        else:
            c_ok_all = False
            c_code = "unhashed-event-log" if ep["chain_ok"] is None else "tampered-hash-chain"
            findings.append({"code": c_code, "severity": "error", "message": f"{cond}/{pol}/i{inum:02d}: {ep['chain_error'] or 'missing hash chain'}"})
        if not ep["roundtrip_ok"]:
            rt_ok_all = False
            findings.append({"code": "verify-roundtrip-mismatch", "severity": "error", "message": f"{cond}/{pol}/i{inum:02d}: {ep['roundtrip_error']}"})
        check_ora_fp = pol == "oracle" and cond in clean_oracle_conds
        if check_ora_fp or need_det:
            t_dir = det_root / f"{family_name}__{cond}__i{inum:02d}__{pol}"
            if check_no_symlink_ancestors(t_dir, root=det_root) is not None or not t_dir.resolve().is_relative_to(real_det_root):
                findings.append({"code": "unsafe-trial-path", "severity": "error", "message": f"unsafe synthetic trial path for {cond}/{pol}/i{inum:02d}"})
                continue
            b_task = family_dir / "build" / cond / f"{family_name}-i{inum:02d}"
            write_synthetic_trial(t_dir, family_name, inst, pol, ep["raw_bytes"], ep["score"], b_task, ep["atif"], root=det_root)
            trig = [str(x) for x in analyze_trial_detectors(t_dir, "olam")["triggered"]]
            if check_ora_fp and trig:
                oracle_fps.append({"condition": cond, "instance": inum, "triggered": trig})
                findings.append({"code": "oracle-detector-false-positive", "severity": "error", "message": f"clean oracle {cond}/i{inum:02d} triggered detectors: {trig}"})
            if need_det:
                for dname in trig:
                    cp_hits[dname] += 1
                    pol_hits[dname] += 1
    row = (
        {
            "instances": len(rews), "mean": round(math.fsum(rews) / len(rews), 6), "min": round(min(rews), 6),
            "max": round(max(rews), 6), "chain_ok": c_ok_all, "log_intact": l_ok_all, "roundtrip_ok": rt_ok_all,
        }
        if rews
        else None
    )
    return row, ep_cnt, chained_cnt, used_disp


def _replay_matrix_and_detectors(
    family_dir: Path, family_name: str, config: Mapping[str, Any], mods: Mapping[str, ModuleType | None],
    policies: list[str], pol_map: Mapping[str, Any], triad_info: Mapping[str, Any], naive_pols: list[str], findings: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, Any]]:
    raw_conds = [str(k) for k in as_map(config.get("conditions"))]
    s1_cond, s0k_cond = triad_info.get("s1_condition"), triad_info.get("s0k_condition")
    sk_cond, skg_cond = triad_info.get("sk_condition"), triad_info.get("sk_guard_condition")
    clean_oracle_conds = frozenset(str(c) for c in (s1_cond, sk_cond, skg_cond) if isinstance(c, str) and c)
    exp_rules: list[tuple[str | None, str, tuple[str, ...]]] = list(triad_info.get("expected_detectors") or [])
    need_det_pols = set(naive_pols) | {r[1] for r in exp_rules}
    matrix: dict[str, dict[str, dict[str, Any]]] = {}
    oracle_fps: list[dict[str, Any]] = []
    det_hits_by_cp: dict[tuple[str, str], Counter[str]] = {}
    det_hits_by_pol: dict[str, Counter[str]] = {}
    inst_seat_counts: dict[str, int] = {}
    used_dispatch_any = False
    chained_episodes = total_episodes = 0
    with tempfile.TemporaryDirectory(prefix="ww-audit-trials-") as det_tmp:
        det_root = Path(os.path.abspath(det_tmp))
        for cond in raw_conds:
            if not is_safe_ident(cond):
                findings.append({"code": "invalid-condition-name", "severity": "error", "message": f"unsafe condition name: {cond!r}"})
                continue
            inst_list, ierr = resolve_instances(config, cond, family_dir, mods.get("ref"))
            if ierr is not None or not inst_list:
                findings.append({"code": "instance-resolution-failed", "severity": "error", "message": ierr or f"no instances for {cond}"})
                continue
            seat_lens = {len(it["seats"]) for it in inst_list}
            if len(seat_lens) != 1:
                findings.append({"code": "invalid-triad-seats", "severity": "error", "message": f"inconsistent seat counts in {cond!r}: {sorted(seat_lens)}"})
            inst_seat_counts[cond] = next(iter(seat_lens))
            matrix[cond] = {}
            for pol in policies:
                row, ecnt, ccnt, udisp = _replay_cond_policy(
                    family_dir, family_name, config, mods, cond, pol, inst_list, pol_map, det_root,
                    clean_oracle_conds, (pol in need_det_pols), oracle_fps,
                    det_hits_by_cp.setdefault((cond, pol), Counter()), det_hits_by_pol.setdefault(pol, Counter()), findings,
                )
                total_episodes += ecnt
                chained_episodes += ccnt
                used_dispatch_any = used_dispatch_any or udisp
                if row is not None:
                    matrix[cond][pol] = row
    _check_triad_seat_counts(inst_seat_counts, s1_cond, s0k_cond, sk_cond, skg_cond, findings)
    for cond_flt, pol_nm, exp_dets in exp_rules:
        req_cond = cond_flt if cond_flt is not None else (str(sk_cond) if sk_cond else None)
        counts = det_hits_by_cp.get((req_cond, pol_nm), Counter()) if req_cond else Counter()
        for dname in exp_dets:
            if counts.get(dname, 0) == 0:
                scope = f"{cond_flt}/{pol_nm}" if cond_flt else f"{sk_cond or 'Sk'}/{pol_nm}"
                findings.append({"code": "missing-expected-detector", "severity": "error", "message": f"expected detector {dname!r} never fired on {scope}"})
    return matrix, {
        "episodes_replayed": total_episodes, "chained_episodes": chained_episodes,
        "engine": "kit_v2_dispatch" if used_dispatch_any else "world_handle",
        "sk_oracle_false_positives": oracle_fps,
        "naive_detector_hits": {pol: dict(sorted(cnt.items())) for pol, cnt in sorted(det_hits_by_pol.items()) if cnt},
    }


def _enforce_triad_and_baselines(
    matrix: Mapping[str, Mapping[str, Mapping[str, Any]]], triad_info: Mapping[str, Any],
    naive_pols: list[str], exploits: list[str], findings: list[dict[str, Any]],
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
    min_s1_cfg, max_s1_cfg = triad_info.get("min_s1_oracle"), triad_info.get("max_s1_oracle")
    if sk_c:
        if sk_o is None:
            findings.append({"code": "missing-triad-oracle", "severity": "error", "message": f"missing Sk(oracle) score for {sk_c!r}"})
        elif sk_o + _EPS < min_sk:
            findings.append({"code": "triad-sk-oracle-low", "severity": "error", "message": f"Sk(oracle)={sk_o:.4f} < required {min_sk:.4f}"})
    if s0k_c:
        if s0k_o is None:
            findings.append({"code": "missing-triad-oracle", "severity": "error", "message": f"missing S0k(oracle) score for {s0k_c!r}"})
        elif s0k_o > max_s0k + _EPS:
            findings.append({"code": "triad-s0k-oracle-high", "severity": "error", "message": f"S0k(oracle)={s0k_o:.4f} > allowed max {max_s0k:.4f}"})
    if s1_c:
        if s1_o is None:
            findings.append({"code": "missing-triad-oracle", "severity": "error", "message": f"missing S1(oracle) score for {s1_c!r}"})
        else:
            req_s1_min = float(min_s1_cfg) if isinstance(min_s1_cfg, float) else (min(0.5, sk_o) if sk_o is not None else 0.5)
            if s1_o + _EPS < req_s1_min:
                findings.append({"code": "triad-s1-oracle-low", "severity": "error", "message": f"S1(oracle)={s1_o:.4f} < required {req_s1_min:.4f}"})
            if isinstance(max_s1_cfg, float) and s1_o > max_s1_cfg + _EPS:
                findings.append({"code": "triad-s1-oracle-high", "severity": "error", "message": f"S1(oracle)={s1_o:.4f} > allowed max {max_s1_cfg:.4f}"})
            if s0k_o is not None and s1_o + _EPS < s0k_o:
                findings.append({"code": "triad-s1-below-s0k", "severity": "error", "message": f"S1(oracle)={s1_o:.4f} < S0k(oracle)={s0k_o:.4f}"})
    if skg_c and (skg_o is None or skg_o + _EPS < min_sk):
        findings.append({"code": "triad-sk-guard-oracle-low", "severity": "error", "message": f"Sk_guard(oracle)={skg_o} < required {min_sk:.4f}"})
    sk_naive_scores: dict[str, float] = {}
    if sk_c and sk_c in matrix:
        for npol in naive_pols:
            nv = finite_float(as_map(matrix[sk_c].get(npol)).get("mean"))
            if nv is None:
                findings.append({"code": "missing-naive-score", "severity": "error", "message": f"naive policy {npol!r} not evaluated in {sk_c!r}"})
            else:
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


def _evict_sandbox_modules(
    saved_mods: dict[str, ModuleType], saved_transient: dict[str, ModuleType], sandbox_roots: tuple[Path, ...], family_name: str
) -> None:
    fam_prefix = f"{family_name}_"
    for name, mod in list(sys.modules.items()):
        if name in _TRANSIENT_MODS or name.startswith("_ww_audit_") or (family_name and name.startswith(fam_prefix)):
            if name not in saved_mods or sys.modules.get(name) is not saved_mods.get(name):
                sys.modules.pop(name, None)
            continue
        if name in saved_mods and sys.modules.get(name) is saved_mods.get(name):
            continue
        mod_file = getattr(mod, "__file__", None)
        if isinstance(mod_file, str) and mod_file:
            try:
                resolved = Path(mod_file).resolve()
            except OSError:
                continue
            if any(resolved.is_relative_to(sr) for sr in sandbox_roots):
                sys.modules.pop(name, None)
                if name in saved_mods:
                    sys.modules[name] = saved_mods[name]
    for tname in _TRANSIENT_MODS:
        sys.modules.pop(tname, None)
        if tname in saved_transient:
            sys.modules[tname] = saved_transient[tname]


def _execute_in_sandbox(
    target: Path, family_name: str, config: Mapping[str, Any], triad_info: Mapping[str, Any], findings: list[dict[str, Any]],
) -> tuple[list[str], list[str], list[str], dict[str, Any], dict[str, Any], dict[str, Any]]:
    empty_det = {"episodes_replayed": 0, "chained_episodes": 0, "engine": "none", "sk_oracle_false_positives": [], "naive_detector_hits": {}}
    if (sym_err := scan_family_symlinks(target)) is not None:
        findings.append({"code": "unsafe-symlink", "severity": "error", "message": f"unsafe symlink in family directory: {sym_err}"})
        return [], [], [], {}, empty_det, {}
    saved_path, saved_flag, saved_mods = list(sys.path), sys.dont_write_bytecode, dict(sys.modules)
    saved_transient = {k: sys.modules[k] for k in _TRANSIENT_MODS if k in sys.modules}
    extra_paths = build_sys_paths(target)
    sandbox_roots = tuple({Path(p).resolve() for p in (str(target), *extra_paths)})
    sys.dont_write_bytecode = True
    for extra_p in reversed(extra_paths):
        if extra_p in sys.path:
            sys.path.remove(extra_p)
        sys.path.insert(0, extra_p)
    for stale_mod in _TRANSIENT_MODS:
        sys.modules.pop(stale_mod, None)
    try:
        mods, mod_err = discover_family_modules(target)
        if mod_err is not None:
            findings.append({"code": "module-load-failed", "severity": "error", "message": mod_err})
            return [], [], [], {}, empty_det, {}
        policies, pol_map, naive_pols, exploits = _collect_policies(config, mods, triad_info["expected_detectors"], findings)
        matrix, det_meta = _replay_matrix_and_detectors(target, family_name, config, mods, policies, pol_map, triad_info, naive_pols, findings)
        triad_eval = _enforce_triad_and_baselines(matrix, triad_info, naive_pols, exploits, findings)
        return policies, naive_pols, exploits, triad_eval, det_meta, matrix
    finally:
        sys.path[:], sys.dont_write_bytecode = saved_path, saved_flag
        _evict_sandbox_modules(saved_mods, saved_transient, sandbox_roots, family_name)


def _certify_one_family_suite(raw_target: Path) -> tuple[dict[str, Any], int]:
    if ".." in Path(raw_target).parts:
        return _bad_input(Path(raw_target), "unsafe path: contains '..'")
    target = Path(os.path.abspath(raw_target))
    if (anc_err := check_no_symlink_ancestors(target)) is not None:
        return _bad_input(target, f"unsafe path: {anc_err}")
    try:
        st = os.lstat(target)
    except OSError as exc:
        return _bad_input(target, f"cannot stat {target}: {exc.strerror or exc}")
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return _bad_input(target, f"not a regular directory: {target}")
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
    has_code = any(os.path.lexists(target / rel) for rel in ("reference.py", "world.py", "template/world.py", "verify.py", "template/verify.py"))
    if not has_code and not os.path.isdir(target / "build"):
        return _bad_input(target, "missing reference.py / world.py / verify.py in family directory")
    family_name = str(config.get("name") or target.name)
    if not is_safe_ident(family_name):
        return _bad_input(target, f"unsafe family name: {family_name!r}")
    findings: list[dict[str, Any]] = []
    triad_info = _validate_triad_config(config, findings)
    policies, naive_pols, exploits, triad_eval, det_meta, matrix = _execute_in_sandbox(target, family_name, config, triad_info, findings)
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


def _select_root_children(target: Path) -> list[Path]:
    triad_children: list[Path] = []
    nontriad_children: list[Path] = []
    broken_children: list[Path] = []
    try:
        entries = sorted(os.scandir(target), key=lambda e: e.name)
    except OSError:
        return []
    for ent in entries:
        if ent.name.startswith((".", "_")):
            continue
        c_path = Path(ent.path)
        if ent.is_symlink():
            broken_children.append(c_path)
            continue
        if not ent.is_dir(follow_symlinks=False) or not os.path.lexists(c_path / "family.toml"):
            continue
        toml_txt, rerr = read_regular_text(c_path / "family.toml", root=c_path)
        if rerr is not None or toml_txt is None:
            broken_children.append(c_path)
            continue
        try:
            doc = tomllib.loads(toml_txt)
        except tomllib.TOMLDecodeError:
            broken_children.append(c_path)
            continue
        if "triad" in doc:
            triad_children.append(c_path)
        else:
            nontriad_children.append(c_path)
    if triad_children:
        return sorted([*triad_children, *broken_children])
    return sorted([*nontriad_children, *broken_children])


def run_certify_suite(family_dir: Path) -> tuple[dict[str, Any], int]:
    raw_in = Path(family_dir)
    if ".." in raw_in.parts:
        return _bad_input(raw_in, "unsafe path: contains '..'")
    target = Path(os.path.abspath(raw_in))
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
    selected = _select_root_children(target)
    if not selected:
        return _bad_input(target, "missing family.toml")
    reports: list[dict[str, Any]] = []
    worst_rc = 0
    for child in selected:
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
