#!/usr/bin/env python3
"""Join task-lint findings with the executed TBLite-verified health card (HAR-454).

The health card records one executed class per task. This builder runs the
read-only static task lint on the same task package in a corpus clone and
writes one row per card task: the class, the sorted lint codes, and the number
of findings. The join key is the card task name; it must equal a task
directory name in the clone, and that directory must hold `task.toml`.

Lint findings are descriptive, not keep/fix/discard predictions
(`src/evallab/dataset_audit_checks.py`).

Reproduce from the repository worktree:
    git clone https://github.com/huggingface/terminal-bench-lite-verified /tmp/tblv
    git -C /tmp/tblv checkout f8f4fc8
    uv run python research/experiments/task-lint-vs-executed-health/build.py
    uv run python research/experiments/task-lint-vs-executed-health/build.py --check

TBLV_CORPUS_ROOT overrides the corpus root; TBLV_HEALTH_CARD overrides the card.
`--out-dir` writes elsewhere. `--check` compares bytes and never writes.
Exit codes: 0 = built or identical, 1 = CSV drift/missing, 2 = unusable inputs.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

from evallab.task_lint import lint_task

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
DEFAULT_CARD = REPO / "research/audits/corpus-health-tblite-verified-2026-11-15.json"
DEFAULT_CORPUS = Path("/tmp/tblv")
CORPUS_ENV = "TBLV_CORPUS_ROOT"
CARD_ENV = "TBLV_HEALTH_CARD"
CSV_NAME = "task-lint-health.csv"
COLUMNS = ("task", "executed_class", "lint_codes", "finding_count")


class BuildError(Exception):
    """The inputs cannot produce the joined table."""


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def read_card(path: Path) -> list[dict[str, str]]:
    """Return card task/class pairs, or raise BuildError for unusable JSON."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BuildError(f"cannot read card {path}: {error}") from error
    rows = _object(payload).get("rows")
    if not isinstance(rows, list) or not rows:
        raise BuildError(f"card {path} has no nonempty rows list")
    card: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, value in enumerate(rows):
        row = _object(value)
        task, executed_class = row.get("task"), row.get("class")
        if (
            not isinstance(task, str)
            or not task
            or not isinstance(executed_class, str)
            or not executed_class
        ):
            raise BuildError(f"card row {index} needs a nonempty string task and class")
        if task in seen:
            raise BuildError(f"card repeats task {task!r}")
        seen.add(task)
        card.append({"task": task, "class": executed_class})
    return card


def lint_row(corpus: Path, task: str, executed_class: str) -> dict[str, str]:
    """Lint one joined task package and return its CSV row."""
    task_dir = corpus / task
    if not task_dir.is_dir() or not (task_dir / "task.toml").is_file():
        raise BuildError(
            f"card task {task!r} has no task package under {corpus}; "
            "the join key is the task directory name"
        )
    findings = lint_task(task_dir)
    codes = sorted({finding.rule for finding in findings})
    return {
        "task": task,
        "executed_class": executed_class,
        "lint_codes": ";".join(codes),
        "finding_count": str(len(findings)),
    }


def build_rows(card_path: Path, corpus: Path) -> list[dict[str, str]]:
    """Return one row per card task in task-name order."""
    if not corpus.is_dir():
        raise BuildError(f"corpus root {corpus} is not a directory")
    rows = [lint_row(corpus, row["task"], row["class"]) for row in read_card(card_path)]
    return sorted(rows, key=lambda row: row["task"])


def render(rows: list[dict[str, str]]) -> str:
    """Render exact columns with a header and LF line endings."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    writer.writerows([row[column] for column in COLUMNS] for row in rows)
    return buffer.getvalue()


def check(target: Path, rebuilt: str) -> bool:
    """Compare exact CSV bytes without writing; emit a unified diff on drift."""
    try:
        current = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"{target} is missing; run without --check", file=sys.stderr)
        return False
    except (OSError, UnicodeError) as error:
        raise BuildError(f"cannot read {target}: {error}") from error
    if current == rebuilt:
        return True
    diff = difflib.unified_diff(
        current.splitlines(keepends=True),
        rebuilt.splitlines(keepends=True),
        fromfile=str(target),
        tofile="rebuilt",
    )
    sys.stderr.writelines(diff)
    return False


def main(argv: list[str] | None = None) -> int:
    """Build or check the CSV: 0 = success, 1 = drift, 2 = unusable inputs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--card", type=Path, default=Path(os.environ.get(CARD_ENV, DEFAULT_CARD)))
    parser.add_argument(
        "--corpus", type=Path, default=Path(os.environ.get(CORPUS_ENV, DEFAULT_CORPUS))
    )
    parser.add_argument("--out-dir", type=Path, default=HERE)
    parser.add_argument("--check", action="store_true", help="compare bytes; write nothing")
    args = parser.parse_args(argv)
    try:
        text = render(build_rows(args.card, args.corpus))
        target = args.out_dir / CSV_NAME
        if args.check:
            if check(target, text):
                print(f"{target} is current")
                return 0
            return 1
        args.out_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="")
    except BuildError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"{len(text.splitlines()) - 1} rows written to {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
