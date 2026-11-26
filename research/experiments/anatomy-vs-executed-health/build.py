#!/usr/bin/env python3
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

from evallab.package_anatomy import analyze_package

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
DEFAULT_CARD = REPO / "research/audits/corpus-health-tblite-verified-2026-11-15.json"
DEFAULT_CORPUS = Path("/tmp/tblv")
CORPUS_ENV = "TBLV_CORPUS_ROOT"
CARD_ENV = "TBLV_HEALTH_CARD"
CSV_NAME = "anatomy-health.csv"
COLUMNS = ("task", "executed_class", "hypothesis_codes", "hypothesis_count")
CARD_ROWS_MAX = 10_000
SCORE_MIN = 0
SCORE_MAX = 100


class BuildError(Exception):
    pass


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def read_card(path: Path) -> list[dict[str, str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BuildError(f"cannot read card {path}: {error}") from error
    rows = _object(payload).get("rows")
    if not isinstance(rows, list) or not rows:
        raise BuildError(f"card {path} has no nonempty rows list")
    if len(rows) > CARD_ROWS_MAX:
        raise BuildError(f"card {path} has {len(rows)} rows, above {CARD_ROWS_MAX}")
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
    assert len(card) == len(rows)
    return card


def hypothesis_code(hypothesis: dict[str, Any]) -> tuple[str, int]:
    identifier, score = hypothesis.get("id"), hypothesis.get("score")
    if not isinstance(identifier, str) or not identifier:
        raise BuildError(f"hypothesis has no string id: {hypothesis!r}")
    if not isinstance(score, int) or isinstance(score, bool) or not SCORE_MIN <= score <= SCORE_MAX:
        raise BuildError(f"hypothesis {identifier!r} has score {score!r} outside 0..100")
    code = identifier.split("#", 1)[0]
    assert code and "#" not in code
    return code, score


def anatomy_row(corpus: Path, task: str, executed_class: str) -> dict[str, str]:
    task_dir = corpus / task
    if not task_dir.is_dir() or not (task_dir / "task.toml").is_file():
        raise BuildError(
            f"card task {task!r} has no task package under {corpus}; "
            "the join key is the task directory name"
        )
    hypotheses = analyze_package(task_dir).get("vulnerability_hypotheses")
    if not isinstance(hypotheses, list):
        raise BuildError(f"anatomy of {task!r} has no vulnerability_hypotheses list")
    best: dict[str, int] = {}
    for hypothesis in hypotheses:
        code, score = hypothesis_code(_object(hypothesis))
        best[code] = max(score, best.get(code, SCORE_MIN))
    assert len(best) <= len(hypotheses)
    return {
        "task": task,
        "executed_class": executed_class,
        "hypothesis_codes": ";".join(f"{code}:{best[code]}" for code in sorted(best)),
        "hypothesis_count": str(len(hypotheses)),
    }


def build_rows(card_path: Path, corpus: Path) -> list[dict[str, str]]:
    if not corpus.is_dir():
        raise BuildError(f"corpus root {corpus} is not a directory")
    card = read_card(card_path)
    rows = [anatomy_row(corpus, row["task"], row["class"]) for row in card]
    assert len(rows) == len(card)
    return sorted(rows, key=lambda row: row["task"])


def render(rows: list[dict[str, str]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    writer.writerows([row[column] for column in COLUMNS] for row in rows)
    return buffer.getvalue()


def check(target: Path, rebuilt: str) -> bool:
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
    parser = argparse.ArgumentParser(
        description="Join package-anatomy hypotheses with the TBLite-verified health card. "
        "Exit codes: 0 built or current, 1 drift or missing CSV, 2 unusable inputs."
    )
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
