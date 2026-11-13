#!/usr/bin/env python3
"""Regenerate ``ratios.csv``: the query-optimize runtime-check margin.

``test_compare_golden_vs_solution_runtime`` in
``library/tasks/query-optimize/tests/test_outputs.py`` fails when
``median(solution) > 1.05 * median(golden)``. The oracle submits the golden
query, so its ratio measures timing noise, not speed.

The builder walks a published-results root (default ``~/Developer/eval-lab-results``,
layout ``<date>/<job>/<trial>``), keeps every trial of the ``query-optimize``
task package, and parses the timing dict the verifier prints::

    {'iterations': 5, 'golden': {'median_s': ...}, 'solution': {'median_s': ...}, ...}

Trials without that line keep an empty ``golden_median_s``, ``solution_median_s``
and ``ratio``: a missing measurement is not a zero. ``--check`` rebuilds the
table in memory and exits 1 on any drift from the committed CSV.

    uv run python research/experiments/query-optimize-runtime-margin/build.py
    uv run python research/experiments/query-optimize-runtime-margin/build.py --check
"""

from __future__ import annotations

import argparse
import csv
import difflib
import io
import json
import math
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

HERE = Path(__file__).resolve().parent
CSV_NAME = "ratios.csv"
DEFAULT_ROOT = Path.home() / "Developer" / "eval-lab-results"
RESULTS_ROOT_ENV = "EVALLAB_RESULTS_ROOT"
TASK = "query-optimize"
#: The verifier prints ``{'iterations': 5, 'golden': {'median_s': ...}, ...}``.
GOLDEN_RE = re.compile(r"'golden':\s*\{\s*'median_s':\s*([^,}\s]+)")
SOLUTION_RE = re.compile(r"'solution':\s*\{\s*'median_s':\s*([^,}\s]+)")
#: Env var the cheat ladder sets; its presence marks a ladder cell, not a control.
LADDER_ENV = "EVALLAB_CHEAT_LADDER_VERSION="
COLUMNS = (
    "job",
    "trial",
    "role",
    "reward",
    "golden_median_s",
    "solution_median_s",
    "ratio",
)


class BuildError(Exception):
    """The results root cannot produce the table."""


def slug(value: str) -> str:
    """Lowercase ``value`` with every non-alphanumeric run folded to one dash."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", value.lower()))


def read_json(path: Path) -> dict:
    """Return the parsed object, or an empty object when absent or unreadable."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def task_names(job_meta: dict, trial_result: dict, trial_config: dict) -> list[str]:
    """Names and paths that identify the trial's task package.

    The job name is excluded on purpose: ladder cells stage the task under a
    name that does not mention ``query-optimize``.
    """
    staging = job_meta.get("task_staging") or {}
    names: list[str] = [
        staging.get("declared_task_name"),
        staging.get("source_task_basename"),
        trial_result.get("task_name"),
    ]
    task_path = (trial_result.get("task_id") or {}).get("path") or (
        trial_config.get("task") or {}
    ).get("path")
    if isinstance(task_path, str):
        names.append(Path(task_path).name)
    return [name for name in names if isinstance(name, str) and name]


def is_query_optimize(names: Sequence[str]) -> bool:
    return any(TASK in slug(name) for name in names)


def agent_name(trial_config: dict, trial_result: dict) -> str:
    agent = trial_config.get("agent")
    if isinstance(agent, dict) and isinstance(agent.get("name"), str):
        return agent["name"]
    agent_info = trial_result.get("agent_info")
    if isinstance(agent_info, dict) and isinstance(agent_info.get("name"), str):
        return agent_info["name"]
    return ""


def role(job_name: str, trial_config: dict, trial_result: dict, command: Sequence) -> str:
    """Cell role: an explicit job-name token, else ladder, else the agent name.

    Jobs with no role token in their name (floor ladder, hardening cells) carry
    the ladder version in their recorded command; that is the next best label.
    """
    lowered = job_name.lower()
    for token in ("oracle", "nop", "cheat", "ladder"):
        if token in lowered:
            return token
    if any(LADDER_ENV in str(arg) for arg in command):
        return "ladder"
    folded = slug(agent_name(trial_config, trial_result))
    for token in ("cheat", "oracle", "nop", "codex"):
        if token in folded:
            return token
    return folded


def finite(value: object) -> float | None:
    """Float value when it is a finite number, else ``None``."""
    text = str(value).strip()
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def reward(trial_dir: Path, trial_result: dict) -> str:
    """Recorded reward from the trial result, else the verifier's reward file."""
    verifier = trial_result.get("verifier_result")
    if isinstance(verifier, dict):
        rewards = verifier.get("rewards")
        if isinstance(rewards, dict) and rewards.get("reward") is not None:
            number = finite(rewards["reward"])
            if number is not None:
                return str(number)
    path = trial_dir / "verifier" / "reward.txt"
    try:
        number = finite(path.read_text(encoding="utf-8"))
    except OSError:
        return ""
    return str(number) if number is not None else ""


def medians(stdout: str) -> tuple[float | None, float | None]:
    """Golden and solution medians from the printed timing dict, if present."""
    golden_match = GOLDEN_RE.search(stdout)
    solution_match = SOLUTION_RE.search(stdout)
    golden = finite(golden_match.group(1)) if golden_match else None
    solution = finite(solution_match.group(1)) if solution_match else None
    return golden, solution


def trial_row(job_name: str, trial_dir: Path, job_meta: dict) -> dict[str, str] | None:
    """One CSV row for a query-optimize trial, or ``None`` for another task."""
    trial_config = read_json(trial_dir / "config.json")
    trial_result = read_json(trial_dir / "result.json")
    if not is_query_optimize(task_names(job_meta, trial_result, trial_config)):
        return None
    command = job_meta.get("command")
    golden: float | None = None
    solution: float | None = None
    try:
        stdout = (trial_dir / "verifier" / "test-stdout.txt").read_text(errors="replace")
    except OSError:
        stdout = ""
    golden, solution = medians(stdout)
    ratio = solution / golden if golden and solution is not None else None
    return {
        "job": job_name,
        "trial": trial_dir.name,
        "role": role(
            job_name,
            trial_config,
            trial_result,
            command if isinstance(command, list) else (),
        ),
        "reward": reward(trial_dir, trial_result),
        "golden_median_s": "" if golden is None else str(golden),
        "solution_median_s": "" if solution is None else str(solution),
        "ratio": "" if ratio is None else str(ratio),
    }


def build(root: Path) -> list[dict[str, str]]:
    """Rows for every query-optimize trial under ``root``, in walk order."""
    if not root.is_dir():
        raise BuildError(f"results root not found: {root}")
    rows: list[dict[str, str]] = []
    dates = sorted(path for path in root.iterdir() if path.is_dir())
    for date_dir in dates:
        jobs = sorted(path for path in date_dir.iterdir() if path.is_dir())
        for job_dir in jobs:
            job_meta = read_json(job_dir / "lab-metadata.json")
            trials = sorted(path for path in job_dir.iterdir() if path.is_dir())
            for trial_dir in trials:
                if not (trial_dir / "config.json").exists() and not (
                    trial_dir / "result.json"
                ).exists():
                    continue
                row = trial_row(job_dir.name, trial_dir, job_meta)
                if row is not None:
                    rows.append(row)
    return rows


def render(rows: Sequence[dict[str, str]]) -> str:
    """CSV text with a fixed column order and a trailing newline per row."""
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=list(COLUMNS), extrasaction="ignore", lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def check(target: Path, rebuilt: str) -> bool:
    """Compare ``rebuilt`` with the committed bytes; True when identical."""
    committed = target.read_bytes() if target.is_file() else b""
    if committed == rebuilt.encode("utf-8"):
        print(f"{target.name}: reproduces from the results root")
        return True
    print(f"{target.name}: stale; rerun without --check to regenerate", file=sys.stderr)
    diff = difflib.unified_diff(
        committed.decode("utf-8", "replace").splitlines(),
        rebuilt.splitlines(),
        fromfile=f"{target.name} (committed)",
        tofile=f"{target.name} (rebuilt)",
        lineterm="",
    )
    for line in list(diff)[:40]:
        print(line, file=sys.stderr)
    return False


def main(argv: Sequence[str] | None = None) -> int:
    """Build or check ``ratios.csv``. 0 = ok, 1 = drift, 2 = unusable root."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get(RESULTS_ROOT_ENV) or DEFAULT_ROOT),
        help=f"published results root (default: ${RESULTS_ROOT_ENV} or {DEFAULT_ROOT})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=HERE / CSV_NAME,
        help=f"CSV to write or check (default: {HERE / CSV_NAME})",
    )
    parser.add_argument("--check", action="store_true", help="exit 1 if the CSV differs")
    args = parser.parse_args(argv)
    try:
        rows = build(args.root)
    except BuildError as error:
        print(f"{'--check' if args.check else 'build'}: {error}", file=sys.stderr)
        return 2
    text = render(rows)
    if args.check:
        return 0 if check(args.out, text) else 1
    args.out.write_text(text, encoding="utf-8")
    measured = sum(1 for row in rows if row["ratio"])
    print(f"wrote {args.out}: {len(rows)} trials, {measured} with a runtime ratio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
