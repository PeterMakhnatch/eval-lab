#!/usr/bin/env python3
"""Regenerate verifier runtime headroom and per-test duration tables.

Harbor wraps ``verifier.verify()`` in ``asyncio.wait_for(..., timeout=timeout_sec)``
(``.venv/.../harbor/trial/trial.py:859``), but the trial's ``verifier`` timing
block starts before the verifier environment is prepared (``single_step.py:93``),
so the recorded phase includes the verifier image build for
``environment_mode = "separate"`` packages.

The builder walks a published-results root (default ``~/Developer/eval-lab-results``,
layout ``<date>/<job>/<trial>/result.json``), keeps every trial whose
``task_name`` is one of the 12 library package names, and measures:

* ``phase_s``: ``verifier.finished_at - verifier.started_at`` from ``result.json``
* ``pytest_s``: the last ``=+ ... in <seconds>s`` summary line in
  ``verifier/test-stdout.txt`` (empty when the verifier prints no pytest summary)
* ``exception_type``: ``exception_info.exception_type``

``headroom.csv`` aggregates one row per package against that package's
``[verifier] timeout_sec`` on the checked-out revision. ``tests.csv`` records
each CTRF test and normalizes its recorded duration to seconds.

``--check`` rebuilds all three tables in memory and exits 1 on any drift from
the committed CSV files.

    uv run python research/experiments/library-verifier-headroom/build.py
    uv run python research/experiments/library-verifier-headroom/build.py --check
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
import tomllib
from collections import Counter
from collections.abc import Iterator, Sequence
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
DEFAULT_TASKS_ROOT = REPO_ROOT / "library" / "tasks"
TRIALS_NAME = "trials.csv"
HEADROOM_NAME = "headroom.csv"
TESTS_NAME = "tests.csv"
DEFAULT_ROOT = Path.home() / "Developer" / "eval-lab-results"
RESULTS_ROOT_ENV = "EVALLAB_RESULTS_ROOT"
#: Published results are laid out ``<date>/<job>/<trial>``; other root entries are not trials.
DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: Last pytest summary line, e.g. ``===== 6 passed in 12.34s =====``.
PYTEST_RE = re.compile(r"=+ .*? in ([\d.]+)s")
#: The 12 library package names. ``terminal-bench/break-filter-js-from-html`` is the
#: upstream task, not this package, and must not be matched.
PACKAGES = (
    "deepplanning-v1/travel-lisbon-002",
    "evallab/syn-funcdag-easy",
    "evallab/syn-funcdag-hard",
    "evallab/syn-funcdag-medium",
    "local-lab/event-summary",
    "peter/release-branch-rescue",
    "peter/sheet-recalc",
    "peter/sheet-recalc-cutoff",
    "petermakhnatch/transaction-reconciliation",
    "sierra-research/tau3-bench__tau3-retail-1",
    "terminal-bench/html-js-filter",
    "terminal-bench/query-optimize",
)
EXCLUDED_PACKAGES = ("terminal-bench/break-filter-js-from-html",)
TRIAL_COLUMNS = (
    "date",
    "job",
    "trial",
    "package",
    "reward",
    "phase_s",
    "pytest_s",
    "timeout_sec",
    "exception_type",
)
HEADROOM_COLUMNS = (
    "package",
    "modes_seen",
    "trials",
    "n",
    "phase_p50_s",
    "phase_p95_s",
    "phase_max_s",
    "pytest_max_s",
    "pytest_n",
    "max_pytest_ratio",
    "timeout_sec",
    "timeout_exceptions",
    "exception_types",
    "task_path",
)
TEST_COLUMNS = (
    "date",
    "job",
    "trial",
    "package",
    "test",
    "status",
    "duration_s",
    "duration_source",
)


class BuildError(Exception):
    """The inputs cannot produce the tables."""


def read_json(path: Path) -> dict:
    """Return the parsed object, or an empty object when absent or unreadable."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def number_text(value: float | None, places: int = 3) -> str:
    """Stable decimal text: ``60.0``, ``12.345``, ``1427.48``; empty for ``None``."""
    if value is None or not math.isfinite(value):
        return ""
    text = f"{value:.{places}f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


def finite(value: object) -> float | None:
    """Float value when it is a finite number, else ``None``."""
    try:
        number = float(str(value).strip())
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def parse_time(value: object) -> datetime | None:
    """Parse Harbor's UTC timestamps; ``None`` when absent or malformed."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def verifier_phase(result: dict) -> float | None:
    """Seconds from ``verifier.started_at`` to ``verifier.finished_at``."""
    verifier = result.get("verifier")
    if not isinstance(verifier, dict):
        return None
    started = parse_time(verifier.get("started_at"))
    finished = parse_time(verifier.get("finished_at"))
    if started is None or finished is None:
        return None
    return round((finished - started).total_seconds(), 3)


def pytest_seconds(trial_dir: Path) -> float | None:
    """Last pytest summary duration in the verifier stdout, or ``None``."""
    try:
        stdout = (trial_dir / "verifier" / "test-stdout.txt").read_text(errors="replace")
    except OSError:
        return None
    matches = PYTEST_RE.findall(stdout)
    return finite(matches[-1]) if matches else None


def recorded_reward(trial_dir: Path, result: dict) -> float | None:
    """Recorded reward from the trial result, else the verifier's reward file."""
    verifier = result.get("verifier_result")
    if isinstance(verifier, dict):
        rewards = verifier.get("rewards")
        if isinstance(rewards, dict) and rewards.get("reward") is not None:
            number = finite(rewards["reward"])
            if number is not None:
                return number
    try:
        return finite((trial_dir / "verifier" / "reward.txt").read_text(encoding="utf-8"))
    except OSError:
        return None


def exception_type(result: dict) -> str:
    """``exception_info.exception_type`` when recorded, else an empty string."""
    info = result.get("exception_info")
    if not isinstance(info, dict):
        return ""
    value = info.get("exception_type")
    return value if isinstance(value, str) else ""


def load_tasks(tasks_root: Path) -> dict[str, dict]:
    """Package name to ``{timeout_sec, path}`` from ``task.toml``.

    Raises ``BuildError`` unless every name in ``PACKAGES`` resolves exactly once.
    """
    if not tasks_root.is_dir():
        raise BuildError(f"task root not found: {tasks_root}")
    found: dict[str, dict] = {}
    duplicates: set[str] = set()
    for path in sorted(tasks_root.rglob("task.toml")):
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        task = data.get("task") or {}
        name = task.get("name")
        if name not in PACKAGES:
            continue
        if name in found:
            duplicates.add(name)
            continue
        verifier = data.get("verifier") or {}
        timeout = finite(verifier.get("timeout_sec"))
        if timeout is None:
            raise BuildError(f"{name}: [verifier] timeout_sec is missing in {path}")
        found[name] = {
            "timeout_sec": timeout,
            "path": path.relative_to(tasks_root).as_posix(),
        }
    missing = [name for name in PACKAGES if name not in found]
    if missing or duplicates:
        problems = []
        if missing:
            problems.append("missing " + ", ".join(missing))
        if duplicates:
            problems.append("duplicate " + ", ".join(sorted(duplicates)))
        raise BuildError(f"task metadata does not cover the 12 packages: {'; '.join(problems)}")
    return found


def trial_row(
    date: str, job: str, trial_dir: Path, result: dict, tasks: dict[str, dict]
) -> dict[str, str]:
    """One CSV row for a matched published trial.

    The internal ``_mode`` key is not a CSV column; ``headroom.csv`` aggregates it.
    """
    package = result["task_name"]
    mode = result.get("verifier_environment_mode")
    return {
        "date": date,
        "job": job,
        "trial": trial_dir.name,
        "package": package,
        "reward": number_text(recorded_reward(trial_dir, result)),
        "phase_s": number_text(verifier_phase(result)),
        "pytest_s": number_text(pytest_seconds(trial_dir)),
        "timeout_sec": number_text(tasks[package]["timeout_sec"]),
        "exception_type": exception_type(result),
        "_mode": mode if isinstance(mode, str) else "",
    }


def published_trials(
    root: Path, tasks: dict[str, dict]
) -> Iterator[tuple[str, str, Path, dict]]:
    """Yield matched published trials in deterministic directory order."""
    if not root.is_dir():
        raise BuildError(f"results root not found: {root}")
    dates = sorted(
        path for path in root.iterdir() if path.is_dir() and DATE_DIR_RE.match(path.name)
    )
    for date_dir in dates:
        jobs = sorted(path for path in date_dir.iterdir() if path.is_dir())
        for job_dir in jobs:
            trials = sorted(path for path in job_dir.iterdir() if path.is_dir())
            for trial_dir in trials:
                result_path = trial_dir / "result.json"
                if not result_path.is_file():
                    continue
                result = read_json(result_path)
                package = result.get("task_name")
                if not isinstance(package, str) or package not in tasks:
                    continue
                yield date_dir.name, job_dir.name, trial_dir, result


def trial_rows(root: Path, tasks: dict[str, dict]) -> list[dict[str, str]]:
    """Rows for every published trial of a library package, in walk order."""
    return [
        trial_row(date, job, trial_dir, result, tasks)
        for date, job, trial_dir, result in published_trials(root, tasks)
    ]


def ctrf_number(value: object) -> float | None:
    """Accept only finite JSON numbers, excluding booleans and numeric strings."""
    return finite(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def ctrf_rows(date: str, job: str, trial_dir: Path, result: dict) -> list[dict[str, str]]:
    """One row per CTRF test dictionary, with report-wide timestamp units."""
    results = read_json(trial_dir / "verifier" / "ctrf.json").get("results")
    if not isinstance(results, dict) or not isinstance(results.get("tests"), list):
        return []
    tests = [test for test in results["tests"] if isinstance(test, dict)]
    summary = results.get("summary")
    timestamps = [summary, *tests] if isinstance(summary, dict) else tests
    scale = 1.0
    if any(
        abs(number) > 1e11
        for item in timestamps
        for key in ("start", "stop")
        if (number := ctrf_number(item.get(key))) is not None
    ):
        scale = 1000.0
    rows = []
    for test in tests:
        start = ctrf_number(test.get("start"))
        stop = ctrf_number(test.get("stop"))
        duration = ctrf_number(test.get("duration"))
        source = ""
        if start is not None and stop is not None and stop >= start:
            duration = (stop - start) / scale
            source = "start_stop"
        elif duration is not None:
            duration /= scale
            source = "duration"
        rows.append(
            {
                "date": date,
                "job": job,
                "trial": trial_dir.name,
                "package": result["task_name"],
                "test": test.get("name") if isinstance(test.get("name"), str) else "",
                "status": test.get("status") if isinstance(test.get("status"), str) else "",
                "duration_s": number_text(duration, places=6),
                "duration_source": source,
            }
        )
    return rows


def percentile(values: Sequence[float], fraction: float) -> float | None:
    """Linear-interpolation percentile (the numpy default); ``None`` when empty."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _count_text(counts: Counter[str]) -> str:
    """Deterministic ``name:count`` list, highest count first then name."""
    return ";".join(
        f"{name}:{count}" for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )


def headroom_row(package: str, trials: Sequence[dict[str, str]], task: dict) -> dict[str, str]:
    """One aggregate row: phase and pytest runtime against ``timeout_sec``."""
    phases = [float(row["phase_s"]) for row in trials if row["phase_s"]]
    pytests = [float(row["pytest_s"]) for row in trials if row["pytest_s"]]
    counts = Counter(row["exception_type"] for row in trials if row["exception_type"])
    modes = Counter(row["_mode"] for row in trials if row["_mode"])
    timeout_sec = task["timeout_sec"]
    pytest_max = max(pytests) if pytests else None
    ratio = pytest_max / timeout_sec if pytest_max is not None and timeout_sec > 0 else None
    exceptions = _count_text(counts)
    return {
        "package": package,
        "modes_seen": _count_text(modes),
        "trials": str(len(trials)),
        "n": str(len(phases)),
        "phase_p50_s": number_text(percentile(phases, 0.50)),
        "phase_p95_s": number_text(percentile(phases, 0.95)),
        "phase_max_s": number_text(max(phases) if phases else None),
        "pytest_max_s": number_text(pytest_max),
        "pytest_n": str(len(pytests)),
        "max_pytest_ratio": number_text(ratio, places=4),
        "timeout_sec": number_text(timeout_sec),
        "timeout_exceptions": str(counts.get("VerifierTimeoutError", 0)),
        "exception_types": exceptions,
        "task_path": task["path"],
    }


def headroom_rows(rows: Sequence[dict[str, str]], tasks: dict[str, dict]) -> list[dict[str, str]]:
    """One row per package, in ``PACKAGES`` order."""
    grouped: dict[str, list[dict[str, str]]] = {name: [] for name in PACKAGES}
    for row in rows:
        grouped[row["package"]].append(row)
    return [headroom_row(name, grouped[name], tasks[name]) for name in PACKAGES]


def render(rows: Sequence[dict[str, str]], columns: Sequence[str]) -> str:
    """CSV text with a fixed column order and a trailing newline per row."""
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=list(columns), extrasaction="ignore", lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def check(targets: Sequence[tuple[Path, str]]) -> bool:
    """Compare each rebuilt CSV with the committed bytes; True when all match."""
    ok = True
    for target, rebuilt in targets:
        committed = target.read_bytes() if target.is_file() else b""
        if committed == rebuilt.encode("utf-8"):
            print(f"{target.name}: reproduces from the results root")
            continue
        ok = False
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
    return ok


def main(argv: Sequence[str] | None = None) -> int:
    """Build or check three tables. 0 = ok, 1 = drift, 2 = unusable inputs."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get(RESULTS_ROOT_ENV) or DEFAULT_ROOT),
        help=f"published results root (default: ${RESULTS_ROOT_ENV} or {DEFAULT_ROOT})",
    )
    parser.add_argument(
        "--tasks-root",
        type=Path,
        default=DEFAULT_TASKS_ROOT,
        help=f"task.toml tree for timeout_sec (default: {DEFAULT_TASKS_ROOT})",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=HERE,
        help=f"directory for all three CSVs (default: {HERE})",
    )
    parser.add_argument("--check", action="store_true", help="exit 1 if any CSV differs")
    args = parser.parse_args(argv)
    try:
        tasks = load_tasks(args.tasks_root)
        rows = []
        tests = []
        for date, job, trial_dir, result in published_trials(args.root, tasks):
            rows.append(trial_row(date, job, trial_dir, result, tasks))
            tests.extend(ctrf_rows(date, job, trial_dir, result))
        headroom = headroom_rows(rows, tasks)
    except BuildError as error:
        print(f"{'--check' if args.check else 'build'}: {error}", file=sys.stderr)
        return 2
    outputs = (
        (args.out_dir / TRIALS_NAME, render(rows, TRIAL_COLUMNS)),
        (args.out_dir / HEADROOM_NAME, render(headroom, HEADROOM_COLUMNS)),
        (args.out_dir / TESTS_NAME, render(tests, TEST_COLUMNS)),
    )
    if args.check:
        return 0 if check(outputs) else 1
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for target, text in outputs:
        target.write_text(text, encoding="utf-8")
    measured = sum(1 for row in rows if row["phase_s"])
    print(
        f"wrote {TRIALS_NAME}, {HEADROOM_NAME} and {TESTS_NAME}: "
        f"{len(rows)} trials, {measured} with a phase, "
        f"{len(headroom)} packages, {len(tests)} tests"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
