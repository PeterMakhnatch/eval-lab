from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from worldwatch._io import read_tolerant_json_object  # noqa: E402
from worldwatch.detectors import DETECTOR_NAMES, analyze_trial_detectors  # noqa: E402
from worldwatch.model import finite_float  # noqa: E402
from worldwatch.validity import (  # noqa: E402
    DEFAULT_STALL_SECONDS,
    classify_trial_validity,
    fisher_exact_two_sided,
    run_validity,
)

DEFAULT_ROOT = Path("/Users/petermakhnatch/Developer/eval-lab-results/worldwatch-probes-2026-11-24")
MAX_ANALYZE_TRIALS = 5_000
MAX_RESULT_BYTES = 2 * 1024 * 1024


def _parse_task_identity(result: dict[str, Any]) -> tuple[str, str, str]:
    cfg = result.get("config")
    task_cfg = cfg.get("task") if isinstance(cfg, dict) else None
    tpath = str(task_cfg.get("path") or "") if isinstance(task_cfg, dict) else ""
    parts = Path(tpath).name.split("__") if tpath else []
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    task_name = str(result.get("task_name") or "unknown")
    return task_name, "unknown", "i00"


def _extract_agent_metrics(result: dict[str, Any]) -> dict[str, Any]:
    seats = result.get("seat_results")
    raw_ar: Any = (
        seats[0].get("agent_result")
        if isinstance(seats, list) and seats and isinstance(seats[0], dict)
        else result.get("agent_result")
    )
    ar: dict[str, Any] = raw_ar if isinstance(raw_ar, dict) else {}
    raw_meta = ar.get("metadata")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    exc = result.get("exception_info")
    exc_type = str(exc.get("exception_type") or "") if isinstance(exc, dict) else None
    return {
        "cost_usd": finite_float(ar.get("cost_usd")),
        "n_input_tokens": int(ar.get("n_input_tokens") or 0),
        "n_cache_tokens": int(ar.get("n_cache_tokens") or 0),
        "n_output_tokens": int(ar.get("n_output_tokens") or 0),
        "n_episodes": int(meta.get("n_episodes") or 0),
        "exception_type": exc_type,
    }


def _discover_wave_trials(wave_dir: Path, wave_name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not wave_dir.is_dir():
        return rows
    for model_dir in sorted(p for p in wave_dir.iterdir() if p.is_dir()):
        model = model_dir.name
        for dirpath, dirnames, filenames in os.walk(model_dir):
            dirnames.sort()
            if "result.json" not in filenames:
                continue
            data, err = read_tolerant_json_object(Path(dirpath) / "result.json", max_bytes=MAX_RESULT_BYTES)
            if err is not None or data is None or "trial_name" not in data:
                continue
            dirnames[:] = []
            assert len(rows) < MAX_ANALYZE_TRIALS
            family, condition, instance = _parse_task_identity(data)
            vr = data.get("verifier_result")
            raw_rew = vr.get("rewards") if isinstance(vr, dict) else {}
            rewards = (
                {str(k): float(v) for k, v in raw_rew.items() if finite_float(v) is not None}
                if isinstance(raw_rew, dict)
                else {}
            )
            det = analyze_trial_detectors(Path(dirpath), "olam")
            val = classify_trial_validity(Path(dirpath), "olam", DEFAULT_STALL_SECONDS)
            agent = _extract_agent_metrics(data)
            rows.append({
                "wave": wave_name,
                "model": model,
                "trial": str(data.get("trial_name") or Path(dirpath).name),
                "path": dirpath,
                "family": family,
                "condition": condition,
                "instance": instance,
                "reward": finite_float(rewards.get("reward")),
                "reward_valid": bool(data.get("reward_valid") is True),
                "measured": bool(val.get("measured")),
                "validity_reason": str(val.get("reason") or "unknown"),
                "rewards": rewards,
                "detectors_triggered": list(det.get("triggered") or []),
                "detectors": det.get("detectors") or {},
                "chain_ok_ww": bool(det.get("chain_ok")),
                "chain_error_ww": det.get("chain_error"),
                "events_count": int(det.get("events_count") or 0),
                **agent,
            })
    return rows


def _mean(vals: list[float]) -> float | None:
    return round(sum(vals) / len(vals), 6) if vals else None


def _summarize_group(items: list[dict[str, Any]]) -> dict[str, Any]:
    rews = [float(x["reward"]) for x in items if x["reward"] is not None]
    m_rews = [float(x["reward"]) for x in items if x["measured"] and x["reward"] is not None]
    costs = [float(x["cost_usd"]) for x in items if x["cost_usd"] is not None]
    eps = [float(x["n_episodes"]) for x in items if x["n_episodes"] > 0]
    subkeys = sorted({k for x in items for k in x["rewards"] if k != "reward"})
    submeans = {k: _mean([float(x["rewards"][k]) for x in items if k in x["rewards"]]) for k in subkeys}
    trig_counts: Counter[str] = Counter()
    for x in items:
        for d in x["detectors_triggered"]:
            trig_counts[d] += 1
    return {
        "n": len(items),
        "measured": sum(1 for x in items if x["measured"]),
        "n_valid_reward": len(rews),
        "mean_reward": _mean(rews),
        "mean_measured_reward": _mean(m_rews),
        "total_cost_usd": round(sum(costs), 6),
        "mean_cost_usd": _mean(costs),
        "mean_episodes": _mean(eps),
        "chain_ok_count": sum(1 for x in items if x["chain_ok_ww"]),
        "script_faithful_count": sum(1 for x in items if x["rewards"].get("script_faithful") == 1.0),
        "submetrics": submeans,
        "detectors": {name: int(trig_counts.get(name, 0)) for name in DETECTOR_NAMES},
    }


def _run_regrade(targets: list[Path]) -> dict[str, Any]:
    existing = [p for p in targets if p.is_dir()]
    if not existing:
        return {"ran": False, "exit_codes": [], "total": 0, "matched": 0, "mismatched": 0}
    total = matched = mismatched = 0
    codes: list[int] = []
    for path in existing:
        proc = subprocess.run(
            ["uv", "run", "--no-sync", "python", "-m", "muster", "regrade", "--json", str(path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        codes.append(proc.returncode)
        if not proc.stdout.strip():
            continue
        try:
            obj = json.loads(proc.stdout)
        except ValueError:
            continue
        trials = obj.get("rows") or obj.get("trials") if isinstance(obj, dict) else None
        if isinstance(trials, list):
            for t in trials:
                if isinstance(t, dict):
                    total += 1
                    st = t.get("status")
                    if st == "match":
                        matched += 1
                    elif st == "mismatch":
                        mismatched += 1
    return {
        "ran": True,
        "exit_codes": codes,
        "total": total,
        "matched": matched,
        "mismatched": mismatched,
    }


def _read_spend_ledger(ledger_path: Path) -> dict[str, Any]:
    if not ledger_path.is_file():
        return {"entries": [], "openrouter_delta_usd": None}
    entries: list[dict[str, Any]] = []
    for raw in ledger_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line:
            entries.append(json.loads(line))
    usages = [finite_float(e.get("total_usage_usd")) for e in entries if e.get("total_usage_usd") is not None]
    delta = (
        round(usages[-1] - usages[0], 6)
        if len(usages) >= 2 and usages[0] is not None and usages[-1] is not None
        else None
    )
    return {
        "entries": entries,
        "first_usage_usd": usages[0] if usages else None,
        "last_usage_usd": usages[-1] if usages else None,
        "openrouter_delta_usd": delta,
    }


def _aggregate_validity(by_model: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    arms: list[dict[str, Any]] = []
    for m, items in sorted(by_model.items()):
        meas = sum(1 for x in items if x["measured"])
        unm = len(items) - meas
        reasons = dict(sorted(Counter(x["validity_reason"] for x in items if not x["measured"]).items()))
        arms.append({
            "arm": m,
            "total": len(items),
            "measured": meas,
            "unmeasured": unm,
            "unmeasured_rate": round(unm / max(len(items), 1), 6),
            "attrition_by_reason": reasons,
        })
    pairs: list[dict[str, Any]] = []
    for i in range(len(arms)):
        for j in range(i + 1, len(arms)):
            a, b = arms[i], arms[j]
            pval = fisher_exact_two_sided(a["unmeasured"], a["measured"], b["unmeasured"], b["measured"])
            pairs.append({
                "arm_a": a["arm"],
                "arm_b": b["arm"],
                "fisher_p": round(pval, 8),
                "differential_attrition": pval < 0.05,
            })
    return {
        "arms": arms,
        "pairs": pairs,
        "min_fisher_p": min((p["fisher_p"] for p in pairs), default=None),
        "passed": all(a["unmeasured_rate"] <= 0.10 for a in arms) and not any(p["differential_attrition"] for p in pairs),
    }


def _build_wave_report(root: Path, wave: str, trials: list[dict[str, Any]], regrade_dirs: list[Path], single_dir: Path | None) -> dict[str, Any]:
    if not trials:
        return {"wave": wave, "present": False, "trials_count": 0}
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_model_fam: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    by_cell: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in trials:
        by_model[row["model"]].append(row)
        by_model_fam[(row["model"], row["family"])].append(row)
        by_cell[(row["model"], row["family"], row["condition"])].append(row)
    val_report = _aggregate_validity(by_model)
    val_rc = 0 if val_report["passed"] else 1
    if single_dir is not None:
        cli_rep, cli_rc = run_validity([f"{m}={single_dir / m}" for m in sorted(by_model)])
        val_report["cli_passed"] = cli_rep["passed"]
        val_rc = cli_rc
    return {
        "wave": wave,
        "present": True,
        "trials_count": len(trials),
        "validity_rc": val_rc,
        "validity": val_report,
        "regrade": _run_regrade(regrade_dirs),
        "overall": _summarize_group(trials),
        "by_model": {m: _summarize_group(by_model[m]) for m in sorted(by_model)},
        "by_model_family": {f"{m}|{f}": _summarize_group(by_model_fam[(m, f)]) for m, f in sorted(by_model_fam)},
        "by_cell": {f"{m}|{f}|{c}": _summarize_group(by_cell[(m, f, c)]) for m, f, c in sorted(by_cell)},
        "trials": trials,
    }


def _fmt(val: float | None, digits: int = 3) -> str:
    return f"{val:.{digits}f}" if val is not None else "n/a"


def _render_wave_section(wave_name: str, w: dict[str, Any]) -> list[str]:
    rg = w["regrade"]
    lines = [
        f"## Scope `{wave_name}` ({w['trials_count']} trials)",
        "",
        f"- Validity passed: `{w['validity']['passed']}` (min Fisher p: `{w['validity'].get('min_fisher_p')}`)",
        f"- Regrade: `{rg.get('matched')}/{rg.get('total')}` match, `{rg.get('mismatched')}` mismatch (codes: `{rg.get('exit_codes')}`)",
        "",
        "| Model | Family | N | Measured | Mean Reward | Cost USD | Mean Ep | Behavioral Readout |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for key, stats in w["by_model_family"].items():
        model, fam = key.split("|", 1)
        sub = stats["submetrics"]
        extra = (
            f"unauth={_fmt(sub.get('unauthorized_release'))} ask={_fmt(sub.get('asked_creditor'))}"
            if fam == "standing_v1"
            else f"gap={_fmt(sub.get('private_public_gap'))} caught={_fmt(sub.get('false_done_caught'))}"
            if fam == "trustcal_v1"
            else f"choice={_fmt(sub.get('correct_choice'))} dm={_fmt(sub.get('dm_read_share'))} waste={_fmt(sub.get('waste_units'))}"
        )
        lines.append(
            f"| `{model}` | `{fam}` | {stats['n']} | {stats['measured']} | {_fmt(stats['mean_reward'])} | "
            f"${stats['total_cost_usd']:.4f} | {_fmt(stats['mean_episodes'], 1)} | {extra} |"
        )
    lines.extend(["", "| Model | Family | Condition | N | Reward | Readout | Detectors |", "|---|---|---|---:|---:|---|---|"])
    for key, stats in w["by_cell"].items():
        model, fam, cond = key.split("|", 2)
        sub = stats["submetrics"]
        trig = ",".join(f"{k}:{v}" for k, v in stats["detectors"].items() if v > 0) or "-"
        readout = (
            f"exec={_fmt(sub.get('executed'), 2)} rel={_fmt(sub.get('released'), 2)} ask={_fmt(sub.get('asked_creditor'), 2)}"
            if fam == "standing_v1"
            else f"caught={_fmt(sub.get('false_done_caught'), 1)} vacc={_fmt(sub.get('true_accusations'), 1)} gap={_fmt(sub.get('private_public_gap'), 2)}"
            if fam == "trustcal_v1"
            else f"choice={_fmt(sub.get('correct_choice'), 2)} decoy={_fmt(sub.get('decoy_adopted'), 2)} dm={_fmt(sub.get('dm_read_share'), 2)} waste={_fmt(sub.get('waste_units'), 1)}"
        )
        lines.append(f"| `{model}` | `{fam}` | `{cond}` | {stats['n']} | {_fmt(stats['mean_reward'])} | {readout} | {trig} |")
    lines.append("")
    return lines


def _render_markdown(summary: dict[str, Any]) -> str:
    lines: list[str] = [
        "# WorldWatch and Behavioral Probes Live Calibration Report",
        "",
        f"- Root: `{summary['root']}`",
        f"- OpenRouter ledger account delta: `{summary['ledger'].get('openrouter_delta_usd')}` USD",
        f"- Total recorded trial cost across all waves: `${summary['total_recorded_cost_usd']:.6f}`",
        "",
    ]
    for scope in ("scored", "wave0", "wave0b", "wave1"):
        w = summary["waves"].get(scope) or {}
        if w.get("present"):
            lines.extend(_render_wave_section(scope, w))
    return "\n".join(lines) + "\n"


def run_analysis(root: Path) -> dict[str, Any]:
    wave_names = ("wave0", "wave0b", "wave1")
    per_wave = {w: _discover_wave_trials(root / "runs" / w, w) for w in wave_names}
    waves = {
        w: _build_wave_report(root, w, per_wave[w], [root / "runs" / w], root / "runs" / w)
        for w in wave_names
    }
    scored_trials = list(per_wave["wave1"])
    scored_dirs = [root / "runs" / w for w in ("wave1",) if (root / "runs" / w).is_dir()]
    waves["scored"] = _build_wave_report(root, "scored", scored_trials, scored_dirs, None)
    total_cost = round(
        sum(float(x["cost_usd"]) for w in wave_names for x in per_wave[w] if x["cost_usd"] is not None),
        6,
    )
    ledger = _read_spend_ledger(root / "spend-ledger.jsonl")
    summary = {"root": str(root), "total_recorded_cost_usd": total_cost, "ledger": ledger, "waves": waves}
    (root / "SUMMARY.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (root / "REPORT.md").write_text(_render_markdown(summary), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze live behavioral probe waves")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    summary = run_analysis(args.root)
    print(f"Analyzed root={args.root} total_recorded_cost_usd={summary['total_recorded_cost_usd']:.6f}")
    for wname in ("wave0", "wave0b", "wave1", "scored"):
        w = summary["waves"].get(wname) or {}
        if w.get("present"):
            print(
                f"  scope={wname} trials={w['trials_count']} mean_reward={w['overall']['mean_reward']} "
                f"validity_passed={w['validity']['passed']} regrade={w['regrade']['matched']}/{w['regrade']['total']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
