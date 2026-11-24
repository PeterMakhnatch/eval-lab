from __future__ import annotations

import contextlib
import importlib.util
import inspect
import io
import os
import re
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

from worldwatch._io import read_regular_text, stat_regular_file
from worldwatch.model import finite_float

_EPS = 1e-6
_NOP_MAX_ALLOWED = 0.05
_SEG_SPLIT_RE = re.compile(r"[-_]+")
_DEGENERATE_EXACT = frozenset({
    "nop",
    "naive",
    "sequential",
    "random",
    "spoof",
    "eager",
    "silent",
    "greedy",
    "needy",
    "liar",
    "solo",
})
_DEGENERATE_PREFIXES = ("always_", "always-", "never_", "never-", "spoof_", "spoof-", "exploit", "blind_", "blind-")
_TRAP_TOKENS = (
    "bystander",
    "settled",
    "peer-release",
    "manager-release",
    "nospeaker",
    "relayed",
    "lies",
    "liar",
    "decoy",
    "trap",
    "rumor",
    "stale-t",
    "badlead-t",
    "wait-t",
    "deadclaim-t",
)
_EXACT_CONTROLS = frozenset({
    "none",
    "solo",
    "control",
    "ctrl",
    "anchor",
    "none-ctrl",
    "no-stimulus",
    "no_stimulus",
    "no-history",
    "no_history",
    "always-honest",
    "always_honest",
})


def _unusable(family_dir: Path, reason: str) -> tuple[dict[str, Any], int]:
    return (
        {
            "command": "certify-baselines",
            "family_dir": str(family_dir),
            "family": family_dir.name,
            "status": "unusable",
            "passed": False,
            "error": reason,
            "findings": [{"code": "unusable-family", "severity": "error", "message": reason}],
            "margins": {},
            "exploits": {},
            "control_arms": [],
            "trap_arms": [],
        },
        3,
    )


def _load_ref_in_place(ref_path: Path, family_dir: Path) -> tuple[ModuleType | None, str | None]:
    sys.path.insert(0, str(family_dir))
    buf_out, buf_err = io.StringIO(), io.StringIO()
    mod_name = f"_ww_ref_{abs(hash(str(ref_path.resolve())))}"
    try:
        spec = importlib.util.spec_from_file_location(mod_name, ref_path)
        if spec is None or spec.loader is None:
            return None, "cannot create module spec for reference.py"
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            spec.loader.exec_module(module)
        return module, None
    except BaseException as exc:
        return None, f"reference.py import failed: {type(exc).__name__}: {exc}"


def _extract_bounds(entry: Any) -> tuple[float, float, float] | None:
    if isinstance(entry, Mapping):
        mean_v: float | None = None
        for key in ("mean", "reward", "score"):
            val = finite_float(entry.get(key))
            if val is not None:
                mean_v = val
                break
        if mean_v is None:
            return None
        max_v = finite_float(entry.get("max"))
        min_v = finite_float(entry.get("min"))
        return mean_v, (max_v if max_v is not None else mean_v), (min_v if min_v is not None else mean_v)
    sc = finite_float(entry)
    return (sc, sc, sc) if sc is not None else None


def _normalize_ref_table(
    source: Mapping[str, Any],
    findings: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    means: dict[str, dict[str, float]] = {}
    maxes: dict[str, dict[str, float]] = {}
    for key, val in source.items():
        if not isinstance(key, str) or not isinstance(val, Mapping):
            continue
        cond_field = val.get("condition")
        pol_field = val.get("policy")
        b_direct = _extract_bounds(val)
        if isinstance(cond_field, str) and isinstance(pol_field, str) and b_direct is not None:
            mn, mx, lo = b_direct
            if lo > mn + _EPS or mn > mx + _EPS:
                findings.append({"code": "inconsistent-score-bounds", "severity": "error", "message": f"{cond_field}/{pol_field} bounds min={lo} mean={mn} max={mx}"})
            means.setdefault(cond_field, {})[pol_field] = mn
            maxes.setdefault(cond_field, {})[pol_field] = mx
            continue
        row_m: dict[str, float] = {}
        row_x: dict[str, float] = {}
        for pol, pval in val.items():
            b = _extract_bounds(pval)
            if isinstance(pol, str) and b is not None:
                mn, mx, lo = b
                if lo > mn + _EPS or mn > mx + _EPS:
                    findings.append({"code": "inconsistent-score-bounds", "severity": "error", "message": f"{key}/{pol} bounds min={lo} mean={mn} max={mx}"})
                row_m[pol] = mn
                row_x[pol] = mx
        if row_m:
            means[key] = row_m
            maxes[key] = row_x
    return means, maxes


def _evaluate_reference_table(
    module: ModuleType,
    config: dict[str, Any],
) -> tuple[dict[str, dict[str, float]] | None, dict[str, dict[str, float]] | None, list[dict[str, Any]], str | None]:
    findings: list[dict[str, Any]] = []
    buf_out, buf_err = io.StringIO(), io.StringIO()
    computed_raw: Any = None
    try:
        ref_fn = getattr(module, "reference", None)
        chk_fn = getattr(module, "check", None)
        if callable(ref_fn):
            sig = inspect.signature(ref_fn)
            params = [
                p for p in sig.parameters.values()
                if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                and p.default is inspect.Parameter.empty
            ]
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                computed_raw = ref_fn(config) if len(params) >= 1 else ref_fn()
        elif callable(chk_fn):
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = chk_fn(config)
            if rc not in (0, True, None):
                findings.append({"code": "reference-check-failed", "severity": "error", "message": "reference.check() failed"})
            computed_raw = config.get("reference")
        else:
            computed_raw = config.get("reference")
    except AssertionError as exc:
        findings.append({"code": "reference-assertion-failed", "severity": "error", "message": f"AssertionError: {exc}"})
        computed_raw = config.get("reference")
    except BaseException as exc:
        return None, None, findings, f"reference.py execution failed: {type(exc).__name__}: {exc}"
    stored_raw = config.get("reference")
    if isinstance(stored_raw, Mapping) and isinstance(computed_raw, Mapping) and stored_raw != computed_raw:
        findings.append({
            "code": "reference-table-drift",
            "severity": "error",
            "message": "family.toml [reference] differs from reference.py computation.",
        })
    source = computed_raw if isinstance(computed_raw, Mapping) else stored_raw
    if not isinstance(source, Mapping) or not source:
        return None, None, findings, "no [reference] table produced by family.toml or reference.py"
    means, maxes = _normalize_ref_table(source, findings)
    if not means:
        return None, None, findings, "reference table has no condition/policy numeric scores"
    return means, maxes, findings, None


def _module_policy_names(module: ModuleType) -> list[str]:
    raw = getattr(module, "POLICIES", None)
    if isinstance(raw, Mapping):
        return [str(k) for k in raw if isinstance(k, str)]
    if isinstance(raw, (list, tuple, set, frozenset)):
        return [str(x) for x in raw if isinstance(x, str)]
    return []


def _backfill_with_rewards(
    means: dict[str, dict[str, float]],
    maxes: dict[str, dict[str, float]],
    module: ModuleType,
    config: dict[str, Any],
    pol_candidates: list[str],
) -> None:
    rewards_fn = getattr(module, "rewards", None)
    if not callable(rewards_fn):
        return
    conds = [str(k) for k in (config.get("conditions") or {})]
    buf_out, buf_err = io.StringIO(), io.StringIO()
    for cond in conds:
        for pol in pol_candidates:
            if pol in means.get(cond, {}):
                continue
            with contextlib.suppress(BaseException), contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                vals = rewards_fn(config, cond, pol)
                nums = [v for x in vals if (v := finite_float(x)) is not None] if isinstance(vals, list) else []
                if nums:
                    means.setdefault(cond, {})[pol] = sum(nums) / len(nums)
                    maxes.setdefault(cond, {})[pol] = max(nums)


def _classify_policies(
    means: Mapping[str, Mapping[str, float]],
    config: Mapping[str, Any],
    family_dir: Path,
    mod_pols: list[str],
) -> tuple[list[str], list[str], set[str]]:
    declared_pols: set[str] = set(mod_pols)
    if isinstance(config.get("policies"), Mapping):
        declared_pols.update(str(k) for k in config["policies"])
    elif isinstance(config.get("policies"), list):
        declared_pols.update(str(x) for x in config["policies"] if isinstance(x, str))
    if isinstance(config.get("legal_strategies"), list):
        declared_pols.update(str(x) for x in config["legal_strategies"] if isinstance(x, str))
    exploits_cfg = {str(x) for x in (config.get("exploits") or []) if isinstance(x, str)}
    pol_dir = family_dir / "template" / "policies"
    if os.path.isdir(pol_dir) and not os.path.islink(pol_dir):
        with contextlib.suppress(OSError):
            for entry in os.scandir(pol_dir):
                if entry.is_file(follow_symlinks=False) and entry.name.endswith(".sh"):
                    stem = entry.name[:-3]
                    if stem != "seat":
                        declared_pols.add(stem)
    in_table = {p for row in means.values() for p in row}
    degenerates: list[str] = []
    exploits: list[str] = []
    for name in sorted(in_table | declared_pols | exploits_cfg):
        if name == "oracle":
            continue
        low = name.lower()
        is_exp = low == "spoof" or low.startswith("exploit") or name in exploits_cfg
        is_deg = (
            is_exp
            or low in _DEGENERATE_EXACT
            or low.startswith(_DEGENERATE_PREFIXES)
            or (name in declared_pols and name in in_table)
        )
        if is_deg and (name in in_table or name in mod_pols):
            degenerates.append(name)
        if is_exp:
            exploits.append(name)
    return degenerates, exploits, exploits_cfg


def _check_completeness_and_nop(
    means: Mapping[str, Mapping[str, float]],
    maxes: Mapping[str, Mapping[str, float]],
    config: Mapping[str, Any],
    mod_pols: list[str],
    degenerates: list[str],
    exploits: list[str],
    findings: list[dict[str, Any]],
) -> list[str]:
    cfg_conds = [str(k) for k in (config.get("conditions") or {})]
    unscored_conds = [c for c in cfg_conds if c not in means]
    if unscored_conds:
        findings.append({
            "code": "unscored-condition",
            "severity": "error",
            "message": f"conditions declared in family.toml missing from reference evaluation: {unscored_conds}",
        })
    scored_conds = [c for c in cfg_conds if c in means]
    in_table = {p for row in means.values() for p in row}
    known_pols = {"oracle", *mod_pols, *degenerates, *exploits}
    must_cover = sorted(set(mod_pols) | (in_table & known_pols))
    for pol in must_cover:
        missing_in = [c for c in scored_conds if pol not in means[c]]
        if missing_in:
            findings.append({
                "code": "unscored-policy",
                "severity": "error",
                "message": f"policy {pol!r} omitted from condition(s) {missing_in}",
            })
    bad_nop = [
        c for c in scored_conds
        if "nop" in means[c]
        and (means[c]["nop"] > _NOP_MAX_ALLOWED + _EPS or maxes[c].get("nop", 0.0) > _NOP_MAX_ALLOWED + _EPS)
    ]
    if bad_nop:
        findings.append({
            "code": "nonzero-nop-baseline",
            "severity": "error",
            "message": f"nop baseline exceeds {_NOP_MAX_ALLOWED} in condition(s) {bad_nop}",
        })
    return scored_conds


def _check_margins_and_exploits(
    means: Mapping[str, Mapping[str, float]],
    maxes: Mapping[str, Mapping[str, float]],
    degenerates: list[str],
    exploits: list[str],
    exploits_cfg: set[str],
    mod_pols: list[str],
    module: ModuleType,
    config: dict[str, Any],
    findings: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    scored_conds = _check_completeness_and_nop(
        means, maxes, config, mod_pols, degenerates, exploits, findings
    )
    for cond in scored_conds:
        if "oracle" not in means[cond]:
            findings.append({"code": "missing-oracle", "severity": "error", "message": f"condition {cond!r} has no oracle score"})
    if not degenerates:
        findings.append({"code": "no-degenerate-baselines", "severity": "error", "message": "no degenerate baseline policies found in [reference]"})
    margins: dict[str, dict[str, Any]] = {}
    for pol in degenerates:
        shared = [c for c in scored_conds if "oracle" in means[c] and pol in means[c]]
        if not shared:
            continue
        diffs = [means[c]["oracle"] - means[c][pol] for c in shared]
        mean_margin = sum(diffs) / len(diffs)
        min_margin = min(diffs)
        margins[pol] = {
            "conditions": len(shared),
            "oracle_mean": round(sum(means[c]["oracle"] for c in shared) / len(shared), 6),
            "policy_mean": round(sum(means[c][pol] for c in shared) / len(shared), 6),
            "mean_margin": round(mean_margin, 6),
            "min_margin": round(min_margin, 6),
        }
        if min_margin < -_EPS:
            findings.append({
                "code": "oracle-beaten-by-baseline",
                "severity": "error",
                "message": f"degenerate policy {pol!r} beats oracle in at least one condition (min_margin={min_margin:.6f})",
            })
        elif mean_margin <= _EPS:
            findings.append({
                "code": "non-positive-oracle-margin",
                "severity": "error",
                "message": f"oracle does not beat degenerate policy {pol!r} by a positive margin (mean_margin={mean_margin:.6f})",
            })
    exploit_summary = _verify_exploits(
        means, maxes, scored_conds, exploits, exploits_cfg, set(mod_pols), callable(getattr(module, "rewards", None)), findings
    )
    return margins, exploit_summary


def _verify_exploits(
    means: Mapping[str, Mapping[str, float]],
    maxes: Mapping[str, Mapping[str, float]],
    scored_conds: list[str],
    exploits: list[str],
    exploits_cfg: set[str],
    mod_pols_set: set[str],
    has_rewards_fn: bool,
    findings: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for exp in exploits:
        cond_peaks: dict[str, float] = {}
        for cond in scored_conds:
            if exp in means[cond]:
                cond_peaks[cond] = max(means[cond][exp], maxes[cond].get(exp, means[cond][exp]))
        if not cond_peaks:
            if exp in exploits_cfg or exp in mod_pols_set:
                findings.append({
                    "code": "unscored-exploit",
                    "severity": "error",
                    "message": f"declared exploit policy {exp!r} was not evaluated in any condition",
                })
            status = "unmodeled_in_reference" if has_rewards_fn else "unmodeled_no_rewards_fn"
            out[exp] = {"conditions": 0, "status": status, "max_score": None, "exceeds_nop_in": []}
            continue
        max_sc = max(cond_peaks.values())
        beaten = []
        for cond, peak in cond_peaks.items():
            nop_cap = min(_NOP_MAX_ALLOWED, max(0.0, maxes[cond].get("nop", means[cond].get("nop", 0.0))))
            if peak > nop_cap + _EPS:
                beaten.append(cond)
        out[exp] = {"conditions": len(cond_peaks), "max_score": round(max_sc, 6), "exceeds_nop_in": beaten}
        if beaten:
            findings.append({
                "code": "exploit-earns-reward",
                "severity": "error",
                "message": f"exploit policy {exp!r} exceeds zero/nop cap in {beaten} (max={max_sc:.6f})",
            })
    return out


def _check_control_arms(
    config: Mapping[str, Any],
    means: Mapping[str, Mapping[str, float]],
    findings: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    conds = config.get("conditions")
    cond_names = [str(k) for k in conds] if isinstance(conds, Mapping) else []
    trap_arms: list[str] = []
    control_arms: list[str] = []
    for cname in cond_names:
        low = cname.lower()
        segs = tuple(s for s in _SEG_SPLIT_RE.split(low) if s)
        spec = conds.get(cname) if isinstance(conds, Mapping) else {}
        spec_map = spec if isinstance(spec, Mapping) else {}
        has_trap_tok = any(tok in low for tok in _TRAP_TOKENS) or spec_map.get("trap") is True
        explicit_ctrl = spec_map.get("control") is True and spec_map.get("trap") is not True
        is_trap = has_trap_tok and not explicit_ctrl
        is_ctrl = not is_trap and (
            explicit_ctrl
            or str(spec_map.get("rule") or "").lower() == "solo"
            or low in _EXACT_CONTROLS
            or (bool(segs) and segs[0] == "solo")
            or any(tok in segs for tok in ("ctrl", "control", "anchor"))
            or segs[:2] in (("no", "stimulus"), ("no", "history"), ("always", "honest"))
        )
        if is_ctrl:
            control_arms.append(cname)
        if is_trap:
            trap_arms.append(cname)
    scored_controls = [c for c in control_arms if c in means and "oracle" in means[c]]
    if trap_arms and not scored_controls:
        findings.append({
            "code": "missing-control-arm",
            "severity": "error",
            "message": f"family defines behavioral trap condition(s) {trap_arms} without a scored control arm",
        })
    return control_arms, trap_arms


def _certify_one_family(target: Path) -> tuple[dict[str, Any], int]:
    toml_path = target / "family.toml"
    ref_path = target / "reference.py"
    t_info, t_err = stat_regular_file(toml_path, root=target)
    if t_err is not None or t_info is None:
        return _unusable(target, t_err or "missing family.toml")
    r_info, r_err = stat_regular_file(ref_path, root=target)
    if r_err is not None or r_info is None:
        return _unusable(target, r_err or "missing reference.py")
    toml_txt, read_err = read_regular_text(toml_path, root=target)
    if read_err is not None or toml_txt is None:
        return _unusable(target, read_err or "cannot read family.toml")
    try:
        config = tomllib.loads(toml_txt)
    except tomllib.TOMLDecodeError as exc:
        return _unusable(target, f"invalid family.toml: {exc}")
    if not isinstance(config.get("conditions"), Mapping) or not config["conditions"]:
        return _unusable(target, "family.toml has no [conditions] table")
    saved_path = list(sys.path)
    saved_flag = sys.dont_write_bytecode
    saved_modules = dict(sys.modules)
    sys.dont_write_bytecode = True
    try:
        module, imp_err = _load_ref_in_place(ref_path, target)
        if imp_err is not None or module is None:
            return _unusable(target, imp_err or "cannot import reference.py")
        means, maxes, findings, eval_err = _evaluate_reference_table(module, config)
        if eval_err is not None or means is None or maxes is None:
            return _unusable(target, eval_err or "unusable reference table")
        mod_pols = _module_policy_names(module)
        _, prelim_exp, exp_cfg = _classify_policies(means, config, target, mod_pols)
        in_tbl = sorted({p for r in means.values() for p in r})
        _backfill_with_rewards(means, maxes, module, config, list(dict.fromkeys(["oracle", *mod_pols, *in_tbl, *prelim_exp])))
        degenerates, exploits, exp_cfg = _classify_policies(means, config, target, mod_pols)
        margins, exploit_summary = _check_margins_and_exploits(
            means, maxes, degenerates, exploits, exp_cfg, mod_pols, module, config, findings
        )
        control_arms, trap_arms = _check_control_arms(config, means, findings)
    finally:
        sys.path[:] = saved_path
        sys.dont_write_bytecode = saved_flag
        sys.modules.clear()
        sys.modules.update(saved_modules)
    passed = len(findings) == 0
    return (
        {
            "command": "certify-baselines",
            "family_dir": str(target),
            "family": str(config.get("name") or target.name),
            "status": "passed" if passed else "failed",
            "passed": passed,
            "conditions_checked": len(means),
            "degenerate_policies": degenerates,
            "margins": margins,
            "exploits": exploit_summary,
            "control_arms": control_arms,
            "trap_arms": trap_arms,
            "findings": findings,
        },
        0 if passed else 1,
    )


def run_certify_baselines(family_dir: Path) -> tuple[dict[str, Any], int]:
    target = Path(family_dir)
    if not target.exists() or not target.is_dir():
        return _unusable(target, f"not a directory: {target}")
    if os.path.lexists(target / "family.toml") or os.path.lexists(target / "reference.py"):
        return _certify_one_family(target)
    try:
        children = sorted(
            Path(e.path)
            for e in os.scandir(target)
            if e.is_dir(follow_symlinks=False)
            and not e.name.startswith((".", "_"))
            and os.path.lexists(Path(e.path) / "family.toml")
        )
    except OSError:
        children = []
    if not children:
        return _unusable(target, "missing family.toml")
    certified: list[dict[str, Any]] = []
    skipped: list[str] = []
    worst_rc = 0
    for child in children:
        if not os.path.lexists(child / "reference.py"):
            skipped.append(child.name)
            continue
        rep, rc = _certify_one_family(child)
        certified.append(rep)
        if rc != 0:
            worst_rc = max(worst_rc, rc)
    if not certified:
        return _unusable(target, "no certifiable families with reference.py found")
    passed_cnt = sum(1 for r in certified if r["passed"])
    all_passed = worst_rc == 0 and passed_cnt == len(certified)
    return (
        {
            "command": "certify-baselines",
            "root_dir": str(target),
            "status": "passed" if all_passed else "failed",
            "passed": all_passed,
            "families_checked": len(certified),
            "families_passed": passed_cnt,
            "skipped_families": skipped,
            "families": certified,
        },
        0 if all_passed else worst_rc,
    )
