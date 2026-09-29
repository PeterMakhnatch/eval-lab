"""Reading sheet: pick traces for hand-labeling (Trace Lab probe-02-mimo-kit).

Usage::

    uv run --no-project python reading_sheet.py metrics.jsonl --out reading_sheet.md

Picks: 1 pass, 2 fails on the same task (if available), 1 infra error,
and the longest run. Writes a labeling table Peter fills in by hand with
columns: trial | what the agent tried | where it went wrong | failure
label (free text) | is the task at fault (y/n/unsure) | evidence line.

The evidence line is pre-filled from metrics (modes/exception/trace
path); every other labeling column starts blank for Peter.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_rows(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def evidence(row: dict) -> str:
    modes = row.get("failure_modes") or []
    if modes:
        steps = row["repetition"]
        return (
            f"modes={','.join(modes)} "
            f"(src={row.get('failure_class_source')}); "
            f"turns={row['turns']} calls={row['total_calls']} "
            f"across_repeat={steps['across_turn_repeat_rate']}; "
            f"trace={row.get('trajectory_path') or row['trial_dir']}"
        )
    if row.get("exception_class"):
        return (
            f"exception={row['exception_class']} "
            f"reward={row['reward']}; trace={row.get('trajectory_path') or row['trial_dir']}"
        )
    return (
        f"reward={row['reward']} turns={row['turns']} "
        f"calls={row['total_calls']} "
        f"across_repeat={row['repetition']['across_turn_repeat_rate']}; "
        f"trace={row.get('trajectory_path') or row['trial_dir']}"
    )


def pick(rows: list[dict]) -> list[tuple[str, dict]]:
    """Return (slot, row) picks; missing slots are reported, not faked."""
    scored = [row for row in rows if row.get("scored")]
    passes = [row for row in scored if (row["reward"] or 0) >= 1.0]
    fails = [row for row in scored if (row["reward"] or 0) < 1.0]
    infra = [row for row in rows if row.get("infra_no_score")]
    picks: list[tuple[str, dict]] = []
    if passes:
        picks.append(("pass", passes[0]))
    # Two fails on the same task when possible.
    by_task: dict[str, list[dict]] = {}
    for row in fails:
        by_task.setdefault(row.get("task", "?"), []).append(row)
    pair = next(
        (group for group in by_task.values() if len(group) >= 2), None
    )
    if pair:
        picks.append(("fail #1 (same task)", pair[0]))
        picks.append(("fail #2 (same task)", pair[1]))
    elif fails:
        picks.append(("fail #1", fails[0]))
        if len(fails) > 1:
            picks.append(("fail #2", fails[1]))
    if infra:
        picks.append(("infra error", infra[0]))
    if rows:
        longest = max(rows, key=lambda row: (row["turns"], row["total_calls"]))
        if all(row["trial"] != longest["trial"] for _, row in picks):
            picks.append(("longest run", longest))
        else:
            picks.append(("longest run (already listed)", longest))
    return picks


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metrics", help="metrics.jsonl from metrics.py")
    parser.add_argument("--out", required=True, help="output reading_sheet.md")
    args = parser.parse_args(argv)

    rows = load_rows(Path(args.metrics))
    picks = pick(rows)
    have = {slot.split(" (")[0] if slot.startswith("longest") else slot
            for slot, _ in picks}
    missing = sorted(
        short for short in ("pass", "fail #1", "fail #2", "infra error")
        if not any(short in slot for slot in have)
    )

    lines = [
        "# Reading sheet (hand labeling)",
        "",
        f"_Source: {args.metrics}. Open each trace, read it end to end, "
        "then fill one table row. Keep labels free text -- no fixed "
        "taxonomy yet._",
        "",
    ]
    if missing:
        lines.append(
            f"_Note: no trials available for: {', '.join(missing)} "
            "(slot skipped, not faked)._\n"
        )
    lines += [
        "## Traces to read",
        "",
    ]
    for slot, row in picks:
        lines += [
            f"### {slot}: `{row['trial']}`",
            "",
            f"- task: `{row.get('task')}`",
            f"- reward: {row['reward']} (scored={row.get('scored')})",
            f"- turns: {row['turns']}, calls: {row['total_calls']}, "
            f"tokens: {row.get('total_tokens')}",
            f"- trace: `{row.get('trajectory_path') or row['trial_dir']}`",
            "",
        ]
    lines += [
        "## Labeling table",
        "",
        "| trial | what the agent tried | where it went wrong | "
        "failure label (free text) | is the task at fault (y/n/unsure) | "
        "evidence line |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    seen_trials: set[str] = set()
    for _, row in picks:
        if row["trial"] in seen_trials:
            continue
        seen_trials.add(row["trial"])
        lines.append(
            f"| `{row['trial']}` |  |  |  |  | {evidence(row)} |"
        )
    lines += [
        "",
        "## After labeling",
        "",
        "- If 2+ rows blame the task (not the agent), flag the task for "
        "the HAR-82/HAR-83 env audit.",
        "- If rows show the same agent loop twice, note the loop shape "
        "(exact-repeat calls? silent outputs?) for the harness fix.",
        "",
    ]
    Path(args.out).write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {len(picks)} picks -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
