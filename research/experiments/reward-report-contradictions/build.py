#!/usr/bin/env python3
"""Rebuild reward/report contradictions from published Harbor trials (HAR-437).

HAR-434 identified a trial with reward 1 despite 19 failed CTRF tests. This
read-only, $0 scan makes that discrepancy reproducible across the published
results root. It runs no trials, Docker commands, network requests, or model calls.

Walk only sorted <YYYY-MM-DD>/<job>/<trial> directories with result.json.
Prefer its finite numeric verifier_result.rewards.reward; otherwise use the
finite verifier/reward.txt value. Always read reward.txt to expose disagreement.
Reject booleans, malformed values, and non-finite rewards. Invalid JSON is {}.

For a non-empty results.tests list in verifier/ctrf.json, count test dictionaries:
passed is "passed", skipped is "skipped" or "pending", and every other status
is failed. Otherwise use numeric summary tests/passed/failed and infer skipped
as max(0, tests - passed - failed). Only tests > 0 is comparable.
A comparable trial contradicts its report exactly when reward >= 1 and failed > 0,
or reward > 0 and passed == 0. Missing rewards never contradict a report.

Classes describe the report, not an independent verdict on agent behavior:
* forged: passed == 0 and failed > 0; no test-derived score can be positive.
* partial-credit: passed > 0 and failed > 0; scoring choices can explain it.
* unknown: all other contradictions, including all-skipped reports.

Reasons prefer "full reward with {failed} failed tests" (singular "test" for 1); otherwise use
"positive reward with no passed test ({failed} failed, {skipped} skipped)".
Append "; reward.txt says {reward_txt}" when rewards differ by more than 1e-9.
CSV rows follow walk order, use LF endings, and format numbers to 6 decimal
places with trailing zeros removed. The header is present even without rows.

The same walk also writes reward-file-mismatches.csv: every published trial whose
captured reward differs from its retained verifier/reward.txt by more than 1e-9.
The captured reward is the finite numeric result.json value. A trial is a mismatch
only when both values are usable, so a missing reward.txt or a missing captured
reward never produces a row. Columns are the directory names, the exact task_name,
config.agent.name, config.agent.model_name (empty when absent or not a string), and
both reward values. Mismatch values use the shortest round-trip decimal text, so a
difference just above 1e-9 stays visible. --check compares both CSVs and never
writes either.

Reproduce from the repository worktree:
    python3 research/experiments/reward-report-contradictions/build.py
    python3 research/experiments/reward-report-contradictions/build.py --check
    python3 research/experiments/reward-report-contradictions/build.py \
        --root /path/to/eval-lab-results --out-dir /tmp/reward-contradictions

EVALLAB_RESULTS_ROOT overrides the default ~/Developer/eval-lab-results root.
Exit codes: 0 = built or identical, 1 = CSV drift/missing, 2 = unusable inputs.
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
from collections import Counter
from collections.abc import Iterator, Sequence
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = Path.home() / "Developer" / "eval-lab-results"
RESULTS_ROOT_ENV = "EVALLAB_RESULTS_ROOT"
CSV_NAME = "contradictions.csv"
MISMATCH_CSV_NAME = "reward-file-mismatches.csv"
DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MISMATCH_COLUMNS = (
    "date",
    "job",
    "trial",
    "task",
    "agent",
    "model",
    "reward",
    "reward_txt",
)
COLUMNS = (
    "date",
    "job",
    "trial",
    "task",
    "reward",
    "reward_source",
    "reward_txt",
    "tests",
    "passed",
    "failed",
    "skipped",
    "reason",
    "classification",
)
CLASSES = ("forged", "partial-credit", "unknown")


class BuildError(Exception):
    """The inputs cannot produce the contradiction table."""


def read_json(path: Path) -> dict:
    """Return a JSON object, or {} for unreadable, malformed, or non-object JSON."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def finite(value: object) -> float | None:
    """Return a finite number or numeric text value, excluding booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def number_text(value: float | None) -> str:
    """Use up to 6 decimal places, no trailing zeros, and no negative zero."""
    if value is None or not math.isfinite(value):
        return ""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def exact_number_text(value: float | None) -> str:
    """Use the shortest round-trip text so a listed difference stays visible."""
    if value is None or not math.isfinite(value):
        return ""
    text = repr(float(value))
    return text[:-2] if text.endswith(".0") else text


def _json_number(value: object) -> float | None:
    """Accept finite JSON numbers, not numeric strings or booleans."""
    return finite(value) if isinstance(value, (int, float)) else None


def ctrf_counts(results: object) -> tuple[int, int, int, int] | None:
    """Return (tests, passed, failed, skipped) from CTRF results, or None."""
    if not isinstance(results, dict):
        return None
    tests = results.get("tests")
    if isinstance(tests, list) and tests:
        total = passed = failed = skipped = 0
        for test in tests:
            if not isinstance(test, dict):
                continue
            total += 1
            status = test.get("status")
            if status == "passed":
                passed += 1
            elif status in ("skipped", "pending"):
                skipped += 1
            else:
                failed += 1
        return total, passed, failed, skipped
    summary = results.get("summary")
    if not isinstance(summary, dict):
        return None
    total = _json_number(summary.get("tests"))
    passed = _json_number(summary.get("passed"))
    failed = _json_number(summary.get("failed"))
    if total is None or passed is None or failed is None:
        return None
    total, passed, failed = int(total), int(passed), int(failed)
    return total, passed, failed, max(0, total - passed - failed)


def reward_values(trial_dir: Path, result: dict) -> tuple[float | None, str, float | None]:
    """Return (effective reward, source, reward.txt), preferring result.json."""
    try:
        reward_txt = finite((trial_dir / "verifier" / "reward.txt").read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        reward_txt = None
    verifier = result.get("verifier_result")
    if isinstance(verifier, dict):
        rewards = verifier.get("rewards")
        if isinstance(rewards, dict):
            reward = _json_number(rewards.get("reward"))
            if reward is not None:
                return reward, "result", reward_txt
    if reward_txt is not None:
        return reward_txt, "reward_txt", reward_txt
    return None, "", None


def agent_identity(result: dict) -> tuple[str, str]:
    """Return (config.agent.name, config.agent.model_name) strings, else empty."""
    config = result.get("config")
    agent = config.get("agent") if isinstance(config, dict) else None
    if not isinstance(agent, dict):
        return "", ""
    name = agent.get("name")
    model = agent.get("model_name")
    return name if isinstance(name, str) else "", model if isinstance(model, str) else ""


def contradiction_reason(reward: float, counts: tuple[int, int, int, int]) -> str:
    """Return the exact contradiction reason, or empty for a consistent trial."""
    tests, passed, failed, skipped = counts
    if tests <= 0:
        return ""
    if reward >= 1 and failed > 0:
        noun = "test" if failed == 1 else "tests"
        return f"full reward with {failed} failed {noun}"
    if reward > 0 and passed == 0:
        return f"positive reward with no passed test ({failed} failed, {skipped} skipped)"
    return ""


def classify(reward: float, counts: tuple[int, int, int, int]) -> str:
    """Classify a contradiction; return empty when there is no contradiction."""
    if not contradiction_reason(reward, counts):
        return ""
    _, passed, failed, _ = counts
    if passed == 0 and failed > 0:
        return "forged"
    if passed > 0 and failed > 0:
        return "partial-credit"
    return "unknown"


def _directories(root: Path) -> list[Path]:
    """List directory children in name order without recursive discovery."""
    try:
        with os.scandir(root) as entries:
            return sorted(
                (Path(entry.path) for entry in entries if entry.is_dir()),
                key=lambda path: path.name,
            )
    except OSError as error:
        raise BuildError(f"cannot scan {root}: {error}") from error


def published_trials(root: Path) -> Iterator[tuple[str, str, Path, dict]]:
    """Yield every published trial at exactly 3 directory levels in walk order."""
    if not root.is_dir():
        raise BuildError(f"results root is missing or not a directory: {root}")
    for date_dir in _directories(root):
        if not DATE_DIR_RE.fullmatch(date_dir.name):
            continue
        for job_dir in _directories(date_dir):
            for trial_dir in _directories(job_dir):
                result_path = trial_dir / "result.json"
                if result_path.is_file():
                    yield date_dir.name, job_dir.name, trial_dir, read_json(result_path)


def _contradiction_row(
    date: str,
    job: str,
    trial_dir: Path,
    result: dict,
    reward: float | None,
    source: str,
    reward_txt: float | None,
    counts: tuple[int, int, int, int] | None,
) -> dict[str, str] | None:
    """Build a row from evidence already read by the caller."""
    if reward is None or counts is None:
        return None
    reason = contradiction_reason(reward, counts)
    if not reason:
        return None
    if reward_txt is not None and abs(reward_txt - reward) > 1e-9:
        reason += f"; reward.txt says {number_text(reward_txt)}"
    tests, passed, failed, skipped = counts
    task = result.get("task_name")
    return {
        "date": date,
        "job": job,
        "trial": trial_dir.name,
        "task": task if isinstance(task, str) else "",
        "reward": number_text(reward),
        "reward_source": source,
        "reward_txt": number_text(reward_txt),
        "tests": str(tests),
        "passed": str(passed),
        "failed": str(failed),
        "skipped": str(skipped),
        "reason": reason,
        "classification": classify(reward, counts),
    }


def trial_row(date: str, job: str, trial_dir: Path, result: dict) -> dict[str, str] | None:
    """Read one trial and return its contradiction row, or None."""
    reward, source, reward_txt = reward_values(trial_dir, result)
    counts = ctrf_counts(read_json(trial_dir / "verifier" / "ctrf.json").get("results"))
    return _contradiction_row(date, job, trial_dir, result, reward, source, reward_txt, counts)


def _mismatch_row(
    date: str,
    job: str,
    trial_dir: Path,
    result: dict,
    reward: float | None,
    source: str,
    reward_txt: float | None,
) -> dict[str, str] | None:
    """Build a reward-file mismatch row from evidence already read by the caller."""
    if reward is None or source != "result" or reward_txt is None:
        return None
    if abs(reward_txt - reward) <= 1e-9:
        return None
    task = result.get("task_name")
    agent, model = agent_identity(result)
    return {
        "date": date,
        "job": job,
        "trial": trial_dir.name,
        "task": task if isinstance(task, str) else "",
        "agent": agent,
        "model": model,
        "reward": exact_number_text(reward),
        "reward_txt": exact_number_text(reward_txt),
    }


def _scan(root: Path) -> tuple[list[dict[str, str]], list[dict[str, str]], Counter[str]]:
    """Read each trial once; retain rows, mismatch rows, and coverage counters."""
    rows = []
    mismatches = []
    counters: Counter[str] = Counter()
    for date, job, trial_dir, result in published_trials(root):
        counters["scanned"] += 1
        reward, source, reward_txt = reward_values(trial_dir, result)
        counts = ctrf_counts(read_json(trial_dir / "verifier" / "ctrf.json").get("results"))
        counters["with_reward"] += reward is not None
        counters["comparable"] += counts is not None and counts[0] > 0
        row = _contradiction_row(date, job, trial_dir, result, reward, source, reward_txt, counts)
        if row is not None:
            rows.append(row)
            counters["contradictions"] += 1
            counters[row["classification"]] += 1
        mismatch = _mismatch_row(date, job, trial_dir, result, reward, source, reward_txt)
        if mismatch is not None:
            mismatches.append(mismatch)
            counters["mismatches"] += 1
    return rows, mismatches, counters


def trial_rows(root: Path) -> list[dict[str, str]]:
    """Return every contradiction row in deterministic date/job/trial order."""
    return _scan(root)[0]


def mismatch_rows(root: Path) -> list[dict[str, str]]:
    """Return every reward-file mismatch row in deterministic date/job/trial order."""
    return _scan(root)[1]


def _render(rows: Sequence[dict[str, str]], columns: Sequence[str]) -> str:
    """Render exact columns with a header and LF line endings."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def render(rows: Sequence[dict[str, str]]) -> str:
    """Render the exact contradiction CSV columns with a header and LF endings."""
    return _render(rows, COLUMNS)


def render_mismatches(rows: Sequence[dict[str, str]]) -> str:
    """Render the exact mismatch CSV columns with a header and LF endings."""
    return _render(rows, MISMATCH_COLUMNS)


def check(target: Path, rebuilt: str) -> bool:
    """Compare exact CSV bytes without writing; emit a unified diff on drift."""
    try:
        committed = target.read_bytes()
    except FileNotFoundError:
        committed = b""
    if committed == rebuilt.encode("utf-8"):
        print(f"{target.name}: reproduces from the results root")
        return True
    print(f"{target.name}: missing or stale; rerun without --check to regenerate", file=sys.stderr)
    diff = difflib.unified_diff(
        committed.decode("utf-8", "replace").splitlines(keepends=True),
        rebuilt.splitlines(keepends=True),
        fromfile=f"{target.name} (committed)",
        tofile=f"{target.name} (rebuilt)",
    )
    for line in diff:
        sys.stderr.write(line)
        if not line.endswith("\n"):
            sys.stderr.write("\n\\ No newline at end of file\n")
    return False


def main(argv: Sequence[str] | None = None) -> int:
    """Build or check the CSV: 0 = success, 1 = drift, 2 = unusable inputs."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get(RESULTS_ROOT_ENV, str(DEFAULT_ROOT))),
        help=f"published results root (default: ${RESULTS_ROOT_ENV} or {DEFAULT_ROOT})",
    )
    parser.add_argument(
        "--out-dir", type=Path, default=HERE, help="directory for the two CSVs"
    )
    parser.add_argument("--check", action="store_true", help="exit 1 for CSV drift; never write")
    args = parser.parse_args(argv)
    try:
        rows, mismatches, counters = _scan(args.root)
        fields = ("scanned", "comparable", "with_reward", "contradictions", *CLASSES, "mismatches")
        print(" ".join(f"{name}={counters[name]}" for name in fields))
        outputs = (
            (CSV_NAME, render(rows)),
            (MISMATCH_CSV_NAME, render_mismatches(mismatches)),
        )
        if args.check:
            results = [check(args.out_dir / name, text) for name, text in outputs]
            return 0 if all(results) else 1
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for name, text in outputs:
            (args.out_dir / name).write_text(text, encoding="utf-8", newline="")
            print(f"wrote {name}")
    except (BuildError, OSError) as error:
        print(f"{'--check' if args.check else 'build'}: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
