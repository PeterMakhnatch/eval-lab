"""Incremental contamination audit: audit only the passes added since the last run.

Steps (each cheap step re-runs over everything; each paid or slow step skips trials
it has already done, keyed by trial directory name):

1. ``inventory.py``: every recorded pass (published results home + trials census).
2. ``score_free.py``: rules + TA code rules, new trials only.
3. ``kev_chunks.py`` -> Modal Kev -> append: new traces only (``--no-kev`` skips;
   new passes then stay unflagged by Kev, so Sol still sees them only via the
   other detectors).
4. ``triage.py``: flags and the Sol to-do list.
5. ``sol_review.py``: GPT-6.1 Sol on flagged passes without a saved verdict.
6. ``report.py``: table, SFT lists and ``research/calibration/contamination-audit/labels.jsonl``
   (a label's ``audit_run``/``audited_at`` only change when its verdict changes).

Usage (worktree root; Kev on Modal costs about $0.4 per 1,000 windows):
  EVALLAB_DERIVED_ROOT=~/Developer/eval-lab/derived/parquet \
    uv run python research/experiments/contamination-audit/run.py [--dry-run] [--no-kev]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKTREE = HERE.parents[2]
CONTAM = WORKTREE / "derived/contam"
KEV = WORKTREE / "research/experiments/kev-hack-detector"


def step(*cmd: str) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=WORKTREE, check=True, env=os.environ)


def py(script: str, *args: str, extra: tuple[str, ...] = ()) -> None:
    step("uv", "run", *extra, "python", str(HERE / script), *args)


def count_lines(path: Path) -> int:
    if not path.is_file():
        return 0
    with path.open(encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())


def append(src: Path, dest: Path) -> None:
    with dest.open("a", encoding="utf-8") as out, src.open(encoding="utf-8") as fh:
        for line in fh:
            out.write(line)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="inventory + counts of new work only")
    ap.add_argument("--no-kev", action="store_true", help="skip the paid Modal Kev step")
    args = ap.parse_args()
    CONTAM.mkdir(parents=True, exist_ok=True)
    before = (
        {json.loads(x)["trial"] for x in (CONTAM / "free.jsonl").read_text().splitlines() if x}
        if (CONTAM / "free.jsonl").is_file()
        else set()
    )
    py("inventory.py")
    inv = [json.loads(x) for x in (CONTAM / "inventory.jsonl").read_text().splitlines() if x]
    new = [r for r in inv if not r["control"] and r["atif"] and r["trial"] not in before]
    print(f"{len(new)} agent passes with ATIF not audited before")
    if args.dry_run:
        for r in new:
            print(" ", r["family"], r["trial"])
        return
    py("score_free.py")
    if not args.no_kev:
        py("kev_chunks.py", extra=("--with", "transformers"))
        fresh = CONTAM / "kev_chunks.new.jsonl"
        n = count_lines(fresh)
        if n:
            scores = CONTAM / "kev.scores.new.jsonl"
            step(
                "modal",
                "run",
                f"{KEV / 'modal_kev.py'}::score",
                "--input",
                str(fresh),
                "--questions",
                str(KEV / "questions.json"),
                "--out",
                str(scores),
            )
            if count_lines(scores) != n:
                sys.exit(
                    f"Kev returned {count_lines(scores)} scores for {n} windows; nothing appended"
                )
            append(fresh, CONTAM / "kev_chunks.jsonl")
            append(scores, CONTAM / "kev.scores.jsonl")
            print(f"Kev: {n} new windows scored; record the Modal cost in spend.jsonl")
        fresh.unlink(missing_ok=True)
        (CONTAM / "kev.scores.new.jsonl").unlink(missing_ok=True)
    py("triage.py")
    py("sol_review.py", "--todo", str(CONTAM / "sol_todo.jsonl"))
    py("report.py")


if __name__ == "__main__":
    main()
