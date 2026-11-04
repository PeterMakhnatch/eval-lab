"""Build SIGNAL-MATRIX.md: every signal file in signals/ scored with score.py rules.

Usage (from the eval-lab worktree):
  uv run python research/experiments/ov1104-detbench/matrix.py [--signals-dir ...] [--runs ...]
      [--bootstrap 1000] [--workers 6] [--out-dir research/experiments/ov1104-detbench/results]
      [--combine "name=or:a,b" ...]

Outputs: <out-dir>/matrix.json (every per-set metric), <out-dir>/SIGNAL-MATRIX.md (tables).
Combinations are frozen on DEV inside score.combine and scored once on held-out.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from detbench_lib import RUNS, SIGNALS  # noqa: E402
from score import combine, evaluate, load_runs, load_signal, signal_name  # noqa: E402

_CACHE: dict[tuple[str, tuple[str, ...]], dict[str, dict[str, Any]]] = {}
_LEN_CACHE: dict[str, dict[str, dict[str, Any]] | None] = {}


def _job(args: tuple[str, str, str, str, int, str | None, list[str]]) -> dict[str, Any]:
    name, kind, spec, runs_path, bootstrap, length_path, sets = args
    ck = (runs_path, tuple(sets))
    if ck not in _CACHE:
        _CACHE[ck] = load_runs(Path(runs_path), sets or None)
    runs = _CACHE[ck]
    if length_path and length_path not in _LEN_CACHE:
        _LEN_CACHE[length_path] = load_signal(Path(length_path))
    length = _LEN_CACHE.get(length_path) if length_path else None
    if kind == "signal":
        sig = load_signal(Path(spec))
        return evaluate(sig, runs, name=signal_name(sig, name), bootstrap=bootstrap, length=length)
    method, members = spec.split(":", 1)
    paths = [Path(p) for p in members.split(",")]
    sigs = [load_signal(p) for p in paths]
    names = [signal_name(s, p.stem) for s, p in zip(sigs, paths, strict=True)]
    combined = combine(sigs, runs, method, names=names)
    meta = combined.pop("__weights__", None)
    result = evaluate(combined, runs, name=name, bootstrap=bootstrap, length=length)
    if meta:
        result["stack_weights"] = dict(zip(meta["features"], meta["weights"], strict=True))
    result["members"] = names
    result["method"] = method
    return result


def _f(v: Any, nd: int = 2) -> str:
    return "-" if v is None else f"{v:.{nd}f}"


def _ci(e: dict[str, Any]) -> str:
    lo, hi = e.get("auroc_ci") or (None, None)
    return f"{_f(e['auroc'])} [{_f(lo)},{_f(hi)}]"


def _at(e: dict[str, Any], f: str) -> str:
    a = e["at"].get(f)
    if not a:
        return "-"
    return f"{_f(a['recall'])} / {_f(a['fpr'])}"


def _beats(e: dict[str, Any]) -> str:
    v = e.get("vs_length") or {}
    return f"{v.get('beats_length') or '-'} ({_f(v.get('auroc_diff'))})"


def _is_zero_fire_l0_reason(sig_name: str, e: dict[str, Any]) -> bool:
    if "_l0_hard_" not in sig_name and "_l0_soft_" not in sig_name and "_l0_no_evidence" not in sig_name and "_l0_runner_error" not in sig_name:
        return False
    fire = e.get("at", {}).get("fire") or {}
    r05 = e.get("at", {}).get("0.05") or {}
    return e.get("auroc") == 0.5 and (fire.get("recall") or 0) == 0 and (fire.get("fpr") or 0) == 0 and (r05.get("recall") or 0) == 0 and (r05.get("fpr") or 0) == 0


def _round_obj(o: Any) -> Any:
    if isinstance(o, float):
        return round(o, 4)
    if isinstance(o, dict):
        return {k: _round_obj(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_round_obj(v) for v in o]
    return o


def render(results: list[dict[str, Any]], sets_dev: list[str], sets_ho: list[str], sets_dec: list[str]) -> str:
    lines = ["# SIGNAL-MATRIX (ov1104-detbench)", ""]
    lines.append("Columns: AUROC [95% cluster-bootstrap CI]; `R/F@1%`, `R/F@5%` = recall / FPR at the threshold that gives 1% / 5% FPR on pooled DEV negatives (abstain = not flagged); `fire R/F` = recall / FPR when a rule-like (<=3-valued) signal fires at its top value; cov = coverage; beats length = paired bootstrap of AUROC(signal) - AUROC(length_chars), yes/no/tie (diff). Held-out numbers are scored once with the DEV-frozen threshold. Decisive-rule sets are never pooled.")
    lines.append("")
    lines.append("## Pooled summary")
    lines.append("")
    lines.append("| signal | version | DEV AUROC | DEV R/F@1% | DEV R/F@5% | DEV fire R/F | DEV cov | DEV beats length | HELD-OUT AUROC | HO R/F@1% | HO R/F@5% | HO fire R/F | HO cov | HO beats length | HO-natural AUROC |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in results:
        d = r["per_set"].get("pooled:dev")
        h = r["per_set"].get("pooled:heldout")
        hn = r["per_set"].get("pooled:heldout_natural")
        row = f"| {r['signal']} | {str(r.get('version'))[:28]} |"
        row += f" {_ci(d)} | {_at(d, '0.01')} | {_at(d, '0.05')} | {_at(d, 'fire')} | {_f(d['coverage'])} | {_beats(d)} |" if d else " - | - | - | - | - | - |"
        row += f" {_ci(h)} | {_at(h, '0.01')} | {_at(h, '0.05')} | {_at(h, 'fire')} | {_f(h['coverage'])} | {_beats(h)} |" if h else " - | - | - | - | - | - |"
        row += f" {_ci(hn)} |" if hn else " - |"
        lines.append(row)
    for title, sets in (("DEV sets", sets_dev), ("Held-out sets", sets_ho), ("Decisive-rule sets (listed separately, never pooled)", sets_dec)):
        if not sets:
            continue
        lines += ["", f"## {title}", ""]
        for s in sets:
            any_r = next((r for r in results if s in r["per_set"]), None)
            if not any_r:
                continue
            e0 = any_r["per_set"][s]
            lines += [f"### {s}  (n={e0['n']}, pos={e0['n_pos']}, neg={e0['n_neg']}, clusters={e0['n_clusters']})", "", "| signal | AUROC [CI] | R/F@1% | R/F@5% | fire R/F | cov | beats length | prec@1% | prec@5% | prec@20% |", "|---|---|---|---|---|---|---|---|---|---|"]
            omitted_zero_cov = 0
            omitted_zero_l0 = 0
            for r in results:
                e = r["per_set"].get(s)
                if not e or not (e.get("coverage") or 0) > 0:
                    omitted_zero_cov += 1
                    continue
                if _is_zero_fire_l0_reason(r["signal"], e):
                    omitted_zero_l0 += 1
                    continue
                a5 = e["at"].get("0.05") or {}
                lines.append(f"| {r['signal']} | {_ci(e)} | {_at(e, '0.01')} | {_at(e, '0.05')} | {_at(e, 'fire')} | {_f(e['coverage'])} | {_beats(e)} | {_f(a5.get('precision_at_1pct'))} | {_f(a5.get('precision_at_5pct'))} | {_f(a5.get('precision_at_20pct'))} |")
            if omitted_zero_cov or omitted_zero_l0:
                lines.append(f"\n*({omitted_zero_cov} signals with 0 coverage on `{s}` and {omitted_zero_l0} zero-fire per-reason L0 rule variants omitted; all {len(results)} signals appear in the Pooled summary table above.)*")
            lines.append("")
    combos = [r for r in results if r.get("method")]
    if combos:
        lines += ["", "## Combinations (frozen on DEV, scored once on held-out)", ""]
        for r in combos:
            lines.append(f"- **{r['signal']}**: method={r['method']} members={r['members']}" + (f" weights={ {k: round(v, 3) for k, v in r['stack_weights'].items()} }" if r.get("stack_weights") else ""))
    return "\n".join(lines) + "\n"

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--signals-dir", type=Path, default=SIGNALS)
    ap.add_argument("--runs", type=Path, default=RUNS)
    ap.add_argument("--bootstrap", type=int, default=1000)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--sets", nargs="*")
    ap.add_argument("--only", nargs="*", help="signal file stems to include (default: all)")
    ap.add_argument("--combine", action="append", default=[], help='"name=method:a.jsonl,b.jsonl"')
    ap.add_argument("--out-dir", type=Path, default=HERE / "results")
    args = ap.parse_args(argv)
    length_path = args.signals_dir / "length_chars.jsonl"
    files = sorted(p for p in args.signals_dir.glob("*.jsonl") if not args.only or p.stem in set(args.only))
    jobs: list[tuple[str, str, str, str, int, str | None, list[str]]] = [(p.stem, "signal", str(p), str(args.runs), args.bootstrap, str(length_path) if length_path.is_file() else None, args.sets or []) for p in files]
    for spec in args.combine:
        name, rest = spec.split("=", 1)
        method, members = rest.split(":", 1)
        members = ",".join(str(args.signals_dir / m if not m.startswith("/") else m) for m in members.split(","))
        jobs.append((name, "combo", f"{method}:{members}", str(args.runs), args.bootstrap, str(length_path) if length_path.is_file() else None, args.sets or []))
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(_job, jobs))
    runs = load_runs(args.runs, args.sets or None)
    sets_dev = sorted({r["set"] for r in runs.values() if r["split"] == "dev"})
    sets_dec = sorted({r["set"] for r in runs.values() if r["split"] == "heldout" and (r.get("decisive_rule") or r.get("pool", True) is False)})
    sets_ho = sorted({r["set"] for r in runs.values() if r["split"] == "heldout" and not r.get("decisive_rule") and r.get("pool", True) is not False})
    args.out_dir.mkdir(parents=True, exist_ok=True)
    full_json_dir = Path("/private/tmp/ov1104/detbench")
    full_json_dir.mkdir(parents=True, exist_ok=True)
    (full_json_dir / "matrix.json").write_text(json.dumps(results, indent=1, sort_keys=True), encoding="utf-8")
    keep_keys = {"pooled:dev", "pooled:dev_natural", "pooled:heldout", "pooled:heldout_natural"}
    pooled_results = [
        _round_obj({**r, "per_set": {k: v for k, v in r["per_set"].items() if k in keep_keys}})
        for r in results
    ]
    (args.out_dir / "matrix-pooled.json").write_text(json.dumps(pooled_results, indent=1, sort_keys=True), encoding="utf-8")
    (args.out_dir / "SIGNAL-MATRIX.md").write_text(render(results, sets_dev, sets_ho, sets_dec), encoding="utf-8")
    print(f"{len(results)} signals -> {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
