"""Render the aggregate baseline tables (markdown) from ``baselines.json``; aggregates only, no per-case rows.

    python tools/render_tables.py --results baselines.json --out BASELINES.md
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SCOPES = ("cheatbench", "goodfire", "terminal_wrench", "deep_swe", "c1c5", "sol_astra", "pooled")
LABELS = {"cheatbench": "CheatBench", "goodfire": "Goodfire", "terminal_wrench": "Terminal Wrench",
          "deep_swe": "deep_swe", "c1c5": "C1-C5", "sol_astra": "Sol/Astra", "pooled": "pooled"}


def cell(rate: dict[str, Any] | None) -> str:
    if not rate or not rate.get("n"):
        return "n/a"
    lo, hi = rate["wilson95"]
    return f"{rate['k']}/{rate['n']} ({100 * lo:.0f}-{100 * hi:.0f})"


def auc(entry: dict[str, Any]) -> str:
    if entry["value"] is None:
        return "n/a"
    return f"{entry['value']:.3f} [{entry['ci95'][0]:.2f}, {entry['ci95'][1]:.2f}]" if entry["ci95"] else f"{entry['value']:.3f}"


def table(header: list[str], rows: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|",
            *("| " + " | ".join(row) + " |" for row in rows), ""]


def render(results: dict[str, Any]) -> str:
    readers = results["readers"]
    names = list(readers)
    out = ["# Baselines (stored readings, no model call)", "",
           "Every number is on the `report` half. Thresholds come from `tune` only. `k/n (lo-hi)` is a count with its 95% "
           "Wilson interval in percent. `n/a` = NOT MEASURED (the reader has no reading for the family, or no tune "
           "negatives).", "", "## Coverage (cases with a stored reading / scored cases)", ""]
    first = readers[names[0]]["coverage"]
    out += table(["reader", *(LABELS[f] for f in first)],
                 [[n, *(f"{readers[n]['coverage'][f]['covered']}/{readers[n]['coverage'][f]['scored_cases']}"
                        for f in first)] for n in names])
    out += ["## AUROC on `report` (95% bootstrap interval)", ""]
    out += table(["reader", *(LABELS[s] for s in SCOPES)],
                 [[n, *(auc(readers[n]["auroc"][s]) if s in readers[n]["auroc"] else "n/a" for s in SCOPES)]
                  for n in names])
    for alpha in ("0", "1", "2", "5"):
        out += [f"## Recall and false alarms at a {alpha}% false-alarm budget", ""]
        rows = []
        for n in names:
            row = [n]
            for s in SCOPES:
                point = readers[n]["alpha"][alpha].get(s)
                row.append("n/a" if not point else f"R {cell(point.get('recall'))}<br>FA {cell(point.get('false_alarm'))}")
            rows.append(row)
        out += table(["reader", *(LABELS[s] for s in SCOPES)], rows)
    matched = results["length_matched"]
    out += ["## Length-matched negatives (each positive paired with the nearest-length negative, per family and split)", ""]
    out += table(["reader", *(f"{LABELS[s]} AUROC" for s in SCOPES)],
                 [[n, *(auc(matched[n]["auroc"][s]) if s in matched[n]["auroc"] else "n/a" for s in SCOPES)]
                  for n in names])
    rows = []
    for n in names:
        row = [n]
        for s in SCOPES:
            point = matched[n]["alpha"]["2"].get(s)
            row.append("n/a" if not point else f"R {cell(point.get('recall'))}<br>FA {cell(point.get('false_alarm'))}")
        rows.append(row)
    out += ["Recall and false alarms at a 2% budget, length-matched:", ""]
    out += table(["reader", *(LABELS[s] for s in SCOPES)], rows)
    out += ["## Paired comparisons (exact McNemar on the shared covered `report` cases)", "",
            "`b` = cases only reader A alerts on; `c` = cases only reader B alerts on. Each reader uses its own tune "
            "threshold at the stated budget. Positives test recall; negatives test false alarms.", ""]
    rows = []
    for key, entry in results["compare"].items():
        a, rest = key.split("__vs__")
        b, alpha = rest.rsplit("__a", 1)[0], entry["alpha"]
        for scope in ("pooled", "cheatbench", "goodfire", "terminal_wrench", "deep_swe"):
            per = entry["scopes"].get(scope)
            if not per or "recall" not in per:
                continue
            rec, fa = per["recall"], per["false_alarm"]
            rows.append([a, b, f"{alpha:g}%", LABELS[scope], f"{rec['b']}/{rec['c']} p={rec['p']:.3f}",
                         f"{fa['b']}/{fa['c']} p={fa['p']:.3f}", str(rec["n"]), str(fa["n"])])
    out += table(["A", "B", "budget", "scope", "recall b/c", "false alarms b/c", "n pos", "n neg"], rows)
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.write_text(render(json.loads(args.results.read_text())))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
