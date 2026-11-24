from __future__ import annotations

import contextlib
import importlib.util
import inspect
import io
import os
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

from worldwatch._io import read_regular_text, stat_regular_file
from worldwatch.model import finite_float

_EPS = 1e-6
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
})
_DEGENERATE_PREFIXES = ("always_", "always-", "never_", "never-", "spoof_", "spoof-", "exploit")
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
_CONTROL_TOKENS = (
    "no-stimulus",
    "no_stimulus",
    "none",
    "solo",
    "no-history",
    "no_history",
    "always-honest",
    "always_honest",
    "control",
    "ctrl",
    "anchor",
)


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


def _import_reference_module(ref_path: Path, family_dir: Path) -> tuple[ModuleType | None, str | None]:
    saved_path = list(sys.path)
    saved_flag = sys.dont_write_bytecode
    saved_modules = set(sys.modules.keys())
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(family_dir))
    buf_out, buf_err = io.StringIO(), io.StringIO()
    mod_name = f"_ww_ref_{abs(hash(str(ref_path.resolve())))}"
    try:
        spec = importlib.util.spec_from_file_location(mod_name, ref_path)
        if spec is None or spec.loader is None:
            return None, "cannot create module spec for reference.py"
        module = importlib.util.module_from_spec(spec)
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            spec.loader.exec_module(module)
        return module, None
    except BaseException as exc:
        return None, f"reference.py import failed: {type(exc).__name__}: {exc}"
    finally:
        sys.path[:] = saved_path
        sys.dont_write_bytecode = saved_flag
        for added in list(sys.modules.keys()):
            if added not in saved_modules:
                sys.modules.pop(added, None)


def _extract_score(entry: Any) -> float | None:
    if isinstance(entry, Mapping):
        for key in ("mean", "reward", "score"):
            val = finite_float(entry.get(key))
            if val is not None:
                return val
        return None
    return finite_float(entry)


def _normalize_ref_table(source: Mapping[str, Any]) -> dict[str, dict[str, float]]:
    normalized: dict[str, dict[str, float]] = {}
    for key, val in source.items():
        if not isinstance(key, str) or not isinstance(val, Mapping):
            continue
        cond_field = val.get("condition")
        pol_field = val.get("policy")
        sc_direct = _extract_score(val)
        if isinstance(cond_field, str) and isinstance(pol_field, str) and sc_direct is not None:
            normalized.setdefault(cond_field, {})[pol_field] = sc_direct
            continue
        row: dict[str, float] = {}
        for pol, pval in val.items():
            sc = _extract_score(pval)
            if isinstance(pol, str) and sc is not None:
                row[pol] = sc
        if row:
            normalized[key] = row
    return normalized


def _evaluate_reference_table(
    module: ModuleType,
    config: dict[str, Any],
) -> tuple[dict[str, dict[str, float]] | None, list[dict[str, Any]], str | None]:
    findings: list[dict[str, Any]] = []
    saved_flag = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
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
        return None, findings, f"reference.py execution failed: {type(exc).__name__}: {exc}"
    finally:
        sys.dont_write_bytecode = saved_flag
    stored_raw = config.get("reference")
    if isinstance(stored_raw, Mapping) and isinstance(computed_raw, Mapping) and stored_raw != computed_raw:
        findings.append({
            "code": "reference-table-drift",
            "severity": "error",
            "message": "family.toml [reference] differs from reference.py computation.",
        })
    source = computed_raw if isinstance(computed_raw, Mapping) else stored_raw
    if not isinstance(source, Mapping) or not source:
        return None, findings, "no [reference] table produced by family.toml or reference.py"
    normalized = _normalize_ref_table(source)
    if not normalized:
        return None, findings, "reference table has no condition/policy numeric scores"
    return normalized, findings, None


def _classify_policies(
    table: Mapping[str, Mapping[str, float]],
    config: Mapping[str, Any],
    family_dir: Path,
) -> tuple[list[str], list[str]]:
    declared_pols: set[str] = set()
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
    in_table = {p for row in table.values() for p in row}
    degenerates: list[str] = []
    exploits: list[str] = []
    for name in sorted(in_table | exploits_cfg):
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
        if is_deg and name in in_table:
            degenerates.append(name)
        if is_exp:
            exploits.append(name)
    return degenerates, exploits


def _check_margins_and_exploits(
    table: Mapping[str, Mapping[str, float]],
    degenerates: list[str],
    exploits: list[str],
    module: ModuleType,
    config: dict[str, Any],
    findings: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    for cond, row in table.items():
        if "oracle" not in row:
            findings.append({"code": "missing-oracle", "severity": "error", "message": f"condition {cond!r} has no oracle score"})
    if not degenerates:
        findings.append({"code": "no-degenerate-baselines", "severity": "error", "message": "no degenerate baseline policies found in [reference]"})
    margins: dict[str, dict[str, Any]] = {}
    for pol in degenerates:
        shared = [c for c, r in table.items() if "oracle" in r and pol in r]
        if not shared:
            continue
        diffs = [table[c]["oracle"] - table[c][pol] for c in shared]
        mean_margin = sum(diffs) / len(diffs)
        min_margin = min(diffs)
        margins[pol] = {
            "conditions": len(shared),
            "oracle_mean": round(sum(table[c]["oracle"] for c in shared) / len(shared), 6),
            "policy_mean": round(sum(table[c][pol] for c in shared) / len(shared), 6),
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
    exploit_summary = _verify_exploits(table, exploits, module, config, findings)
    return margins, exploit_summary


def _verify_exploits(
    table: Mapping[str, Mapping[str, float]],
    exploits: list[str],
    module: ModuleType,
    config: dict[str, Any],
    findings: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    rewards_fn = getattr(module, "rewards", None)
    for exp in exploits:
        scores_by_cond: dict[str, float] = {}
        nop_by_cond: dict[str, float] = {}
        for cond, row in table.items():
            if exp in row:
                scores_by_cond[cond] = row[exp]
            elif callable(rewards_fn):
                with contextlib.suppress(BaseException):
                    vals = rewards_fn(config, cond, exp)
                    if isinstance(vals, list) and vals:
                        scores_by_cond[cond] = sum(float(x) for x in vals) / len(vals)
            nop_by_cond[cond] = row.get("nop", 0.0)
        if not scores_by_cond:
            continue
        max_sc = max(scores_by_cond.values())
        beaten_nop = [c for c, sc in scores_by_cond.items() if sc > max(0.0, nop_by_cond.get(c, 0.0)) + _EPS]
        out[exp] = {"conditions": len(scores_by_cond), "max_score": round(max_sc, 6), "exceeds_nop_in": beaten_nop}
        if beaten_nop:
            findings.append({
                "code": "exploit-earns-reward",
                "severity": "error",
                "message": f"exploit policy {exp!r} exceeds nop/zero baseline in {beaten_nop} (max={max_sc:.6f})",
            })
    return out


def _check_control_arms(config: Mapping[str, Any], findings: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    conds = config.get("conditions")
    cond_names = [str(k) for k in conds] if isinstance(conds, Mapping) else []
    trap_arms: list[str] = []
    control_arms: list[str] = []
    for cname in cond_names:
        low = cname.lower()
        spec = conds.get(cname) if isinstance(conds, Mapping) else {}
        spec_map = spec if isinstance(spec, Mapping) else {}
        is_ctrl = (
            any(tok in low for tok in _CONTROL_TOKENS)
            or low.endswith(("-c", "-n"))
            or spec_map.get("control") is True
            or str(spec_map.get("rule") or "").lower() == "solo"
        )
        is_trap = (
            any(tok in low for tok in _TRAP_TOKENS)
            or spec_map.get("trap") is True
        )
        if is_ctrl:
            control_arms.append(cname)
        if is_trap and not is_ctrl:
            trap_arms.append(cname)
    if trap_arms and not control_arms:
        findings.append({
            "code": "missing-control-arm",
            "severity": "error",
            "message": f"family defines behavioral trap condition(s) {trap_arms} without a no-stimulus/none/solo control arm",
        })
    return control_arms, trap_arms


def run_certify_baselines(family_dir: Path) -> tuple[dict[str, Any], int]:
    target = Path(family_dir)
    if not target.exists() or not target.is_dir():
        return _unusable(target, f"not a directory: {target}")
    toml_path = target / "family.toml"
    ref_path = target / "reference.py"
    t_info, t_err = stat_regular_file(toml_path)
    if t_err is not None or t_info is None:
        return _unusable(target, t_err or "missing family.toml")
    r_info, r_err = stat_regular_file(ref_path)
    if r_err is not None or r_info is None:
        return _unusable(target, r_err or "missing reference.py")
    toml_txt, read_err = read_regular_text(toml_path)
    if read_err is not None or toml_txt is None:
        return _unusable(target, read_err or "cannot read family.toml")
    try:
        config = tomllib.loads(toml_txt)
    except tomllib.TOMLDecodeError as exc:
        return _unusable(target, f"invalid family.toml: {exc}")
    if not isinstance(config.get("conditions"), Mapping) or not config["conditions"]:
        return _unusable(target, "family.toml has no [conditions] table")
    module, imp_err = _import_reference_module(ref_path, target)
    if imp_err is not None or module is None:
        return _unusable(target, imp_err or "cannot import reference.py")
    table, findings, eval_err = _evaluate_reference_table(module, config)
    if eval_err is not None or table is None:
        return _unusable(target, eval_err or "unusable reference table")
    degenerates, exploits = _classify_policies(table, config, target)
    margins, exploit_summary = _check_margins_and_exploits(
        table, degenerates, exploits, module, config, findings
    )
    control_arms, trap_arms = _check_control_arms(config, findings)
    passed = len(findings) == 0
    report: dict[str, Any] = {
        "command": "certify-baselines",
        "family_dir": str(target),
        "family": str(config.get("name") or target.name),
        "status": "passed" if passed else "failed",
        "passed": passed,
        "conditions_checked": len(table),
        "degenerate_policies": degenerates,
        "margins": margins,
        "exploits": exploit_summary,
        "control_arms": control_arms,
        "trap_arms": trap_arms,
        "findings": findings,
    }
    return report, (0 if passed else 1)
