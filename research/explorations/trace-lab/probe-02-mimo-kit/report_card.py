"""Per-task report card from metrics.jsonl (Trace Lab probe-02-mimo-kit).

Usage::

    uv run --no-project python report_card.py metrics.jsonl [--catalog CATALOG.csv] --out report_card.md

Also writes report_card.csv next to the .md. One row per task: attempts,
passes, pass rate, infra-error count, provisional verdict, median turns,
mean repetition, flooding rate, longest/shortest trace links.

Verdicts (always provisional unless a HAR-82 catalog joins its own
verdict columns): always-pass / always-fail / learnable (0<p<1) /
infra-only (no scored trials) / untested (no trials -- never emitted,
kept for the catalog join).

--catalog accepts a CSV with a task column; every other column is joined
as-is (prefixed ``catalog_``). Parquet catalogs are not read (stdlib
only): convert to CSV first; without a catalog all verdicts stay
provisional. HAR-82 had published no catalog path as of 2026-09-28.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path


def load_rows(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                print(f"warn: skip line {lineno} (bad JSON)", file=sys.stderr)
    return rows


def load_catalog(path: Path) -> dict[str, dict]:
    """Join a CSV catalog on its task column. Returns {task: {col: val}}."""
    with open(path, encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return {}
        names = [name.lower() for name in reader.fieldnames]
        task_col = next(
            (col for col, low in zip(reader.fieldnames, names, strict=False) if "task" in low),
            reader.fieldnames[0],
        )
        catalog = {}
        for record in reader:
            catalog[record[task_col]] = {
                f"catalog_{col}": val
                for col, val in record.items()
                if col != task_col
            }
    return catalog


def verdict_for(scored: list[float], n_infra: int) -> str:
    if not scored:
        return "infra-only"
    passes = sum(1 for reward in scored if reward >= 1.0)
    if passes == len(scored):
        return "always-pass"
    if passes == 0:
        return "always-fail"
    return "learnable"


def md_link(text: str, target: str) -> str:
    safe = target.replace(")", "%29")
    return f"[{text}]({safe})"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metrics", help="metrics.jsonl from metrics.py")
    parser.add_argument("--catalog", default=None, help="CSV catalog to join")
    parser.add_argument("--out", required=True, help="output report_card.md")
    args = parser.parse_args(argv)

    rows = load_rows(Path(args.metrics))
    catalog: dict[str, dict] = {}
    if args.catalog:
        catalog_path = Path(args.catalog)
        if catalog_path.suffix.lower() == ".csv" and catalog_path.is_file():
            catalog = load_catalog(catalog_path)
            print(f"joined catalog: {len(catalog)} tasks")
        else:
            print(
                "warn: catalog must be a CSV file (parquet is not read by "
                "this stdlib-only kit: convert to CSV first); "
                "verdicts stay 'provisional'",
                file=sys.stderr,
            )

    by_task: dict[str, list[dict]] = {}
    for row in rows:
        by_task.setdefault(row.get("task", "unknown"), []).append(row)

    cards: list[dict] = []
    for task in sorted(by_task):
        trials = by_task[task]
        scored = [t["reward"] for t in trials if t.get("scored")]
        n_infra = sum(1 for t in trials if t.get("infra_no_score"))
        passes = sum(1 for r in scored if r >= 1.0)
        turns = [t["turns"] for t in trials]
        reps = [t["repetition"]["mean_repetition"] for t in trials]
        floods = [t["repetition"]["flooding_rate"] for t in trials]
        by_turns = sorted(trials, key=lambda t: (t["turns"], t["trial"]))
        card = {
            "task": task,
            "attempts": len(trials),
            "passes": passes,
            "pass_rate": round(passes / len(scored), 3) if scored else "n/a",
            "infra_errors": n_infra,
            "verdict": verdict_for(scored, n_infra) + " (provisional)",
            "median_turns": statistics.median(turns) if turns else 0,
            "mean_repetition": round(sum(reps) / len(reps), 4) if reps else 0.0,
            "flooding_rate": round(sum(floods) / len(floods), 4)
            if floods
            else 0.0,
            "longest_trace": by_turns[-1]["trial_dir"] if by_turns else "",
            "longest_trace_trial": by_turns[-1]["trial"] if by_turns else "",
            "longest_trace_turns": by_turns[-1]["turns"] if by_turns else 0,
            "shortest_trace": by_turns[0]["trial_dir"] if by_turns else "",
            "shortest_trace_trial": by_turns[0]["trial"] if by_turns else "",
            "shortest_trace_turns": by_turns[0]["turns"] if by_turns else 0,
        }
        card.update(catalog.get(task, {}))
        cards.append(card)

    out = Path(args.out)
    lines = [
        "# Report card (per task)",
        "",
        f"_Source: {args.metrics}. Verdicts are provisional unless a catalog "
        "column says otherwise._",
        "",
        "| task | attempts | passes | pass rate | infra errors | verdict "
        "(provisional) | median turns | mean repetition | flooding rate | "
        "longest trace | shortest trace |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for card in cards:
        lines.append(
            f"| {card['task']} | {card['attempts']} | {card['passes']} | "
            f"{card['pass_rate']} | {card['infra_errors']} | {card['verdict']} | "
            f"{card['median_turns']} | {card['mean_repetition']} | "
            f"{card['flooding_rate']} | "
            f"{md_link(card['longest_trace_trial'] + ' (' + str(card['longest_trace_turns']) + ' turns)', card['longest_trace'])} | "
            f"{md_link(card['shortest_trace_trial'] + ' (' + str(card['shortest_trace_turns']) + ' turns)', card['shortest_trace'])} |"
        )
    if catalog:
        extra_cols = sorted(
            {key for card in cards for key in card if key.startswith("catalog_")}
        )
        if extra_cols:
            lines += ["", "## Catalog join", ""]
            lines.append("| task | " + " | ".join(extra_cols) + " |")
            lines.append("| --- | " + " | ".join("---" for _ in extra_cols) + " |")
            for card in cards:
                lines.append(
                    "| " + card["task"] + " | "
                    + " | ".join(str(card.get(col, "")) for col in extra_cols)
                    + " |"
                )
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    csv_path = out.with_suffix(".csv")
    fieldnames = [key for key in cards[0] if key] if cards else []
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(cards)
    print(f"wrote {len(cards)} task rows -> {out} + {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
