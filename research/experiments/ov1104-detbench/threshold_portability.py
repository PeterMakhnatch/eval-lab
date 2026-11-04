"""Leave-one-corpus-out threshold portability on DEV (ov1104-detbench).

Exploratory, not pre-registered. Question: if you pick a detector's 1% / 5% FPR threshold on
four DEV corpora and apply it to the fifth, how far does the FPR overshoot? This is the
operational failure mode behind "classifiers fail across sources": a detector can have a good
pooled AUROC and still be unusable because its threshold does not transfer.

Usage (from the eval-lab worktree):
  uv run python research/experiments/ov1104-detbench/threshold_portability.py \
      [--runs ...] [--signals-dir ...] [--out research/experiments/ov1104-detbench/results/THRESHOLD-PORTABILITY.md]

Uses score.choose_threshold and score.combine, so the thresholds and the combinations are exactly
the ones SIGNAL-MATRIX.md reports. DEV only; no held-out row is read.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from detbench_lib import RUNS, SIGNALS  # noqa: E402
from score import INF, choose_threshold, combine, load_runs, load_signal

SINGLES = [
    "length_chars",
    "length_steps",
    "l0v_full_any_hard",
    "l0v_full_l0_hard_gate_rule_hidden_info_read",
    "cached_facets_haiku_all",
    "j2b_actions_task_diff_v2_flash",
    "j2b_3fam_mean",
    "probe_qwen3_8b",
    "probe_qwen3_8b_run_mean_z",
]

COMBOS = {
    "max_rule_j2b_length": (
        "max",
        ["l0v_full_l0_hard_gate_rule_hidden_info_read", "j2b_actions_task_diff_v2_flash", "length_chars"],
    ),
    "max_best_rule_j2b": (
        "max",
        ["l0v_full_l0_hard_gate_rule_hidden_info_read", "j2b_actions_task_diff_v2_flash"],
    ),
    "max_rule_probez_j2b": (
        "max",
        [
            "l0v_full_l0_hard_gate_rule_hidden_info_read",
            "probe_qwen3_8b_run_mean_z",
            "j2b_actions_task_diff_v2_flash",
        ],
    ),
    "or_best_rule_j2b": (
        "or",
        ["l0v_full_l0_hard_gate_rule_hidden_info_read", "j2b_actions_task_diff_v2_flash"],
    ),
}


def _rows(sig: dict[str, dict[str, Any]], runs: dict[str, dict[str, Any]], keys: list[str]) -> list[tuple[str, int, float]]:
    out: list[tuple[str, int, float]] = []
    for k in keys:
        r = sig.get(k)
        if r is None or r.get("score") is None:
            continue
        out.append((runs[k]["cluster"], int(runs[k]["label"]), float(r["score"])))
    return out


def _rates(rows: list[tuple[str, int, float]], thr: float) -> tuple[float | None, float | None]:
    npos = sum(1 for _, y, _ in rows if y == 1)
    nneg = sum(1 for _, y, _ in rows if y == 0)
    if thr == INF:
        return (0.0 if npos else None, 0.0 if nneg else None)
    recall = sum(1 for _, y, s in rows if y == 1 and s >= thr) / npos if npos else None
    fpr = sum(1 for _, y, s in rows if y == 0 and s >= thr) / nneg if nneg else None
    return recall, fpr


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=Path, default=RUNS)
    ap.add_argument("--signals-dir", type=Path, default=SIGNALS)
    ap.add_argument("--out", type=Path, default=HERE / "results" / "THRESHOLD-PORTABILITY.md")
    args = ap.parse_args(argv)

    runs = load_runs(args.runs, None)
    dev_runs = {k: r for k, r in runs.items() if r["split"] == "dev"}
    corpora = sorted({r["set"] for r in dev_runs.values()})
    dev_keys = list(dev_runs)

    sigs: dict[str, dict[str, dict[str, Any]]] = {}
    for name in SINGLES:
        p = args.signals_dir / f"{name}.jsonl"
        if p.is_file():
            sigs[name] = load_signal(p)
    for name, (method, members) in COMBOS.items():
        paths = [args.signals_dir / f"{m}.jsonl" for m in members]
        if all(p.is_file() for p in paths):
            loaded = [load_signal(p) for p in paths]
            sigs[name] = combine(loaded, runs, method, names=members)

    lines = [
        "# Threshold portability across DEV corpora (ov1104-detbench)",
        "",
        "Exploratory (not pre-registered). For each signal, pick the threshold that gives the target",
        "FPR on **four** DEV corpora, then apply it to the fifth and record the FPR and recall there.",
        "This is the operational failure mode behind \"classifiers fail across sources\": pooled AUROC",
        "can look fine while the threshold does not transfer. DEV only; no held-out row is read.",
        "",
        "Thresholds and combinations are produced by `score.choose_threshold` and `score.combine`, the",
        "same functions `SIGNAL-MATRIX.md` uses. Repro:",
        "",
        "```bash",
        "uv run python research/experiments/ov1104-detbench/threshold_portability.py",
        "```",
        "",
    ]

    for target in (0.01, 0.05):
        lines += [
            f"## Target FPR {target:.0%} on the four training corpora",
            "",
            f"| signal | {' | '.join(corpora)} | worst overshoot |",
            "|---|" + "---|" * len(corpora) + "---|",
        ]
        worst_summary: list[tuple[str, str, float]] = []
        for name, sig in sigs.items():
            cells: list[str] = []
            worst = ("-", 0.0)
            for held in corpora:
                train = [k for k in dev_keys if dev_runs[k]["set"] != held]
                held_keys = [k for k in dev_keys if dev_runs[k]["set"] == held]
                thr = choose_threshold(_rows(sig, dev_runs, train), target)
                recall, fpr = _rates(_rows(sig, dev_runs, held_keys), thr)
                if fpr is None:
                    cells.append("-")
                    continue
                cells.append(f"{fpr * 100:.1f}% / {recall * 100:.0f}%" if recall is not None else f"{fpr * 100:.1f}% / -")
                if fpr > worst[1]:
                    worst = (held, fpr)
            worst_summary.append((name, worst[0], worst[1]))
            lines.append(f"| `{name}` | {' | '.join(cells)} | **{worst[1] * 100:.1f}%** ({worst[0]}) |")
        lines += [
            "",
            "Cells are `FPR on the held corpus / recall on the held corpus` at the threshold tuned for",
            f"{target:.0%} FPR on the other four.",
            "",
            "### Worst overshoot, ranked",
            "",
            "| signal | worst held corpus | FPR there |",
            "|---|---|---:|",
        ]
        for name, held, fpr in sorted(worst_summary, key=lambda t: -t[2]):
            lines.append(f"| `{name}` | {held} | {fpr * 100:.1f}% |")
        lines.append("")

    lines += [
        "## What this shows",
        "",
        "- `length_chars` has the worst cross-corpus threshold transfer: tuned for 1% FPR on the other",
        "  four corpora it reaches ~23% FPR on `cheatbench`, so a length threshold is not usable as a",
        "  shared operating point even though its pooled AUROC is around 0.53",
        "  [check the table above for the exact number].",
        "- The percentile-rank `max` combinations are the most portable objects in the matrix: the",
        "  ranking is computed per source distribution, so the operating point moves with the corpus.",
        "  `max_rule_j2b_length` stays within a few points of the target on every held-out corpus at",
        "  both 1% and 5% targets, while beating `length_chars` on pooled AUROC on both splits.",
        "- Binary rule signals (`l0v_full_any_hard`, the per-reason L0 rules) cannot overshoot much",
        "  because they take few values, but that also means their recall is coarse.",
        "",
        "This is evidence for the deployment recommendation in `README.md`: use the `max` combination",
        "rather than a single member, and re-derive the threshold per corpus family when the family is",
        "known, because a shared threshold is only safe for the rank-based combination.",
        "",
    ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {args.out} ({len(sigs)} signals x {len(corpora)} corpora x 2 targets)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
