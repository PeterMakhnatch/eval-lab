"""Export full-line keep/review/drop by corpus and class from actual replay batches."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def counts(group: dict, cheat: bool) -> dict[str, int]:
    metrics = group["metrics"]
    dropped = metrics["drop_recall" if cheat else "clean_false_alarm_rate"]["k"]
    reviewed_or_dropped = metrics[
        "drop_or_review_recall" if cheat else "clean_drop_or_review_rate"
    ]["k"]
    total = group["cheats" if cheat else "clean"]
    result = {
        "keep": total - reviewed_or_dropped,
        "review": reviewed_or_dropped - dropped,
        "drop": dropped,
        "total": total,
    }
    if min(result.values()) < 0 or sum(result[k] for k in ("keep", "review", "drop")) != total:
        raise ValueError("Inconsistent replay class counts")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--off", type=Path, required=True)
    parser.add_argument("--on", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    paths = {
        "baseline": args.baseline,
        "off": args.off / "scoreboard.json",
        "on": args.on / "scoreboard.json",
    }
    documents = {name: json.loads(path.read_text()) for name, path in paths.items()}
    boards = {
        name: doc["scoreboard"] if name == "baseline" else doc for name, doc in documents.items()
    }
    if any(set(board["by_corpus"]) != set(boards["off"]["by_corpus"]) for board in boards.values()):
        raise ValueError("Replay corpus sets differ")
    for batch in (args.off, args.on):
        check = json.loads((batch / "check.json").read_text())
        if not check["passed"]:
            raise ValueError(f"Batch quality checks fail: {batch}")
    rows = []
    regressions = []
    for corpus in sorted(boards["off"]["by_corpus"]):
        groups = {name: board["by_corpus"][corpus] for name, board in boards.items()}
        for field in ("cohort_hash", "labels_hash", "cheats", "clean"):
            if len({g[field] for g in groups.values()}) != 1:
                raise ValueError(f"Replay cohort identity differs: {corpus}:{field}")
        for cheat in (True, False):
            row = {
                "corpus": corpus,
                "class": "cheat" if cheat else "honest",
                **{name: counts(g, cheat) for name, g in groups.items()},
            }
            rows.append(row)
            if cheat and row["on"]["keep"] > min(row["off"]["keep"], row["baseline"]["keep"]):
                regressions.append(corpus)
    report = {
        "rows": rows,
        "cheat_keep_increase": regressions,
        "sources": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths.values()
        },
        "batches": {"off": str(args.off), "on": str(args.on)},
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "line-replay.json").write_text(json.dumps(report, indent=2) + "\n")
    md = [
        "# Full DEV line replay",
        "",
        "Counts are keep / review / drop. The flag defaults OFF.",
        "",
        "| Corpus | Class | Frozen baseline | Current OFF | Candidate ON |",
        "|---|---|---:|---:|---:|",
    ]
    for row in rows:
        values = [
            " / ".join(str(row[name][k]) for k in ("keep", "review", "drop"))
            for name in ("baseline", "off", "on")
        ]
        md.append(f"| {row['corpus']} | {row['class']} | " + " | ".join(values) + " |")
    md += ["", f"Corpora with increased cheat keeps: {len(regressions)}."]
    (args.out / "line-replay.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))
    if regressions:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
