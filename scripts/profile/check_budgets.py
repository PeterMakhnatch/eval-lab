"""Fail if a profile report exceeds committed median budgets by more than X%.

A breach is not final on its first measurement. With `--reprofile` (what
`.github/workflows/perf.yml` passes), the breached paths -- and only those -- are
re-profiled exactly once, with the same corpus pin, warmup and reps as the report
under test, and the run fails only when that second median also exceeds the
ceiling. A single shared-runner spike therefore costs a notice, not a red main,
while a real regression breaches twice. Both medians are written into
`profile-report.json` before the process exits either way.

There is no retry loop: `REMEASURE_PASSES` is 1 and every re-measurement is
recorded, so a run can never quietly re-roll a budget until it passes.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

REQUIRED_PATHS = (
    "ingest",
    "projection",
    "facts",
    "digest",
    "queue-tick-100",
    "fleet-status",
)

# One re-measurement per breached path per run. Raising this turns the gate into
# a loop that eventually draws a fast sample and hides the regression.
REMEASURE_PASSES = 1

# A re-measure that hangs must not eat the job's 15-minute budget: it is killed
# and the first breach stands.
REMEASURE_TIMEOUT_S = 600.0

HARNESS_PATH = Path(__file__).resolve().parent / "harness.py"


def load_budgets(path: Path) -> dict:
    payload = json.loads(path.read_text())
    if "tolerance_pct" not in payload or "paths" not in payload:
        raise ValueError(f"invalid budgets file: {path}")
    missing = [name for name in REQUIRED_PATHS if name not in payload["paths"]]
    if missing:
        raise ValueError(f"budgets missing paths: {missing}")
    corpus = payload.get("corpus")
    if not isinstance(corpus, dict):
        raise ValueError(
            f"budgets file {path} declares no 'corpus' block. Budgets are only "
            "meaningful against the corpus they were measured on; declare "
            "roots/jobs/result_json for the pinned corpus."
        )
    for key in ("roots", "jobs", "result_json"):
        if key not in corpus:
            raise ValueError(f"budgets corpus block missing {key!r}: {path}")
    return payload


def load_report(path: Path) -> dict:
    payload = json.loads(path.read_text())
    names = [item["path"] for item in payload.get("paths", [])]
    missing = [name for name in REQUIRED_PATHS if name not in names]
    if missing:
        raise ValueError(f"report missing paths: {missing}")
    return payload


def load_remeasure_report(path: Path, names: list[str]) -> dict:
    """Load the one re-measurement report and require every breached path in it."""
    payload = json.loads(path.read_text())
    seen = {item.get("path") for item in payload.get("paths", [])}
    missing = [name for name in names if name not in seen]
    if missing:
        raise ValueError(f"re-measure report {path} has no measurement for {missing}")
    return payload


@dataclass(frozen=True)
class Breach:
    """One path whose measured median exceeded its ceiling."""

    path: str
    budget_ms: float
    ceiling_ms: float
    median_ms: float

    def describe(self, tolerance_pct: float) -> str:
        return (
            f"{self.path}: median {self.median_ms:.3f} ms exceeds budget "
            f"{self.budget_ms:.3f} ms + {tolerance_pct:g}% "
            f"(ceiling {self.ceiling_ms:.3f} ms)"
        )


@dataclass(frozen=True)
class Verdict:
    """The gate's decision, its printed lines, and the re-measurement record."""

    exit_code: int
    lines: list[str] = field(default_factory=list)
    remeasurement: dict | None = None


def ceilings(budgets: dict) -> list[tuple[str, float, float]]:
    """(path, budget_ms, ceiling_ms) for each committed budget, in file order."""
    tolerance = float(budgets["tolerance_pct"])
    return [
        (name, float(budget_ms), float(budget_ms) * (1.0 + tolerance / 100.0))
        for name, budget_ms in budgets["paths"].items()
    ]


def median_by_path(report: dict) -> dict[str, float]:
    return {item["path"]: float(item["median_ms"]) for item in report.get("paths", [])}


def find_breaches(report: dict, budgets: dict) -> list[Breach]:
    medians = median_by_path(report)
    return [
        Breach(path=name, budget_ms=budget_ms, ceiling_ms=ceiling_ms, median_ms=medians[name])
        for name, budget_ms, ceiling_ms in ceilings(budgets)
        if medians[name] > ceiling_ms
    ]


def assert_corpus_shape(report: dict, budgets: dict) -> list[str]:
    """Fail when a report was measured against a corpus the budgets do not describe.

    A perf number only means something relative to the corpus it was measured
    on. If the profiled corpus changes, every budget silently changes meaning
    and the run is not a usable re-baseline sample either. Say both.
    """
    expected = budgets["corpus"]
    actual_roots = list(report.get("corpus_roots", []))
    expected_roots = list(expected["roots"])
    mismatches: list[str] = []
    if actual_roots != expected_roots:
        mismatches.append(f"corpus_roots {actual_roots} != declared {expected_roots}")
    for key, field_name in (("jobs", "corpus_jobs"), ("result_json", "corpus_result_json")):
        actual = report.get(field_name)
        if actual != expected[key]:
            mismatches.append(f"{field_name} {actual} != declared {expected[key]}")
    if not mismatches:
        return []
    return [
        "profiled corpus does not match the shape the budgets were measured on: "
        + "; ".join(mismatches)
        + ". The six budgets describe the pinned corpus only, so this report "
        "neither passes nor fails them meaningfully, and it MUST NOT be cited "
        "as a re-baseline sample. Either profile the pinned default (drop "
        "--corpus), or, if the pin changed deliberately, re-baseline and update "
        "the 'corpus' block in budgets.json in the same commit."
    ]


def maybe_rebaseline_notice(report: dict, budgets: dict) -> list[str]:
    notices: list[str] = []
    medians = median_by_path(report)
    for name, budget_ms in budgets["paths"].items():
        median = medians[name]
        if median < 0.5 * float(budget_ms):
            notices.append(
                f"{name}: median {median:.3f} ms is under 50% of budget "
                f"{budget_ms:.3f} ms — consider re-baselining"
            )
    return notices


def remeasurement_block(
    *,
    first: list[Breach],
    second_report: dict | None,
    tolerance_pct: float,
    problem: str | None = None,
    corpus: dict | None = None,
) -> dict:
    """Record both medians per breached path, whatever the outcome was."""
    second_medians = {} if second_report is None else median_by_path(second_report)
    rows = []
    for item in first:
        again = second_medians.get(item.path)
        if again is None or problem is not None:
            outcome = "not-usable"
        elif again > item.ceiling_ms:
            outcome = "still-exceeds"
        else:
            outcome = "cleared"
        rows.append(
            {
                "path": item.path,
                "budget_ms": item.budget_ms,
                "ceiling_ms": round(item.ceiling_ms, 3),
                "tolerance_pct": tolerance_pct,
                "first_median_ms": item.median_ms,
                "remeasure_median_ms": again,
                "outcome": outcome,
            }
        )
    return {
        "attempts": REMEASURE_PASSES,
        "problem": problem,
        "warmup": None if second_report is None else second_report.get("warmup"),
        "reps": None if second_report is None else second_report.get("reps"),
        "generated_at": None if second_report is None else second_report.get("generated_at"),
        "corpus": corpus,
        "paths": rows,
    }


def judge(
    report: dict,
    budgets: dict,
    remeasure: Callable[[list[str]], dict | None] | None = None,
) -> Verdict:
    """Decide the run: a breach fails only if a second measurement also breaches.

    `remeasure` is the one-shot seam that re-profiles exactly the named paths,
    with the same corpus pin, warmup and reps. It returns the second report, or
    `None` when no usable measurement was produced. Passing `None` keeps the
    strict first-measurement behaviour (a breach fails immediately).
    """
    tolerance = float(budgets["tolerance_pct"])
    lines = [f"notice: {item}" for item in maybe_rebaseline_notice(report, budgets)]
    first = find_breaches(report, budgets)
    if not first:
        return Verdict(0, lines + ["perf budgets ok"])
    if remeasure is None:
        return Verdict(
            1,
            lines
            + ["perf budget exceeded:"]
            + [f"  {item.describe(tolerance)}" for item in first],
        )

    names = [item.path for item in first]
    second_report = remeasure(names)
    if second_report is None:
        block = remeasurement_block(first=first, second_report=None, tolerance_pct=tolerance)
        return Verdict(
            1,
            lines
            + ["perf re-measure unusable; the first breach stands:"]
            + [f"  {item.describe(tolerance)}" for item in first],
            block,
        )

    problems = assert_corpus_shape(second_report, budgets)
    if (second_report.get("warmup"), second_report.get("reps")) != (
        report.get("warmup"),
        report.get("reps"),
    ):
        problems.append(
            "re-measure used warmup/reps "
            f"{second_report.get('warmup')}/{second_report.get('reps')} instead of "
            f"{report.get('warmup')}/{report.get('reps')}, so it is not a comparable "
            "second measurement of the same profile"
        )
    if problems:
        block = remeasurement_block(
            first=first,
            second_report=second_report,
            tolerance_pct=tolerance,
            problem="; ".join(problems),
            corpus=second_report.get("corpus_roots"),
        )
        return Verdict(
            1,
            lines
            + ["perf re-measure not comparable; the first breach stands:"]
            + [f"  {item.describe(tolerance)}" for item in first]
            + [f"  {problem}" for problem in problems],
            block,
        )

    second_medians = median_by_path(second_report)
    cleared: list[str] = []
    still_breaching: list[tuple[Breach, float]] = []
    for item in first:
        again = second_medians[item.path]
        if again > item.ceiling_ms:
            still_breaching.append((item, again))
        else:
            cleared.append(item.path)
    block = remeasurement_block(
        first=first,
        second_report=second_report,
        tolerance_pct=tolerance,
        corpus=second_report.get("corpus_roots"),
    )
    for path in cleared:
        row = next(row for row in block["paths"] if row["path"] == path)
        lines.append(
            f"notice: {path}: first median {row['first_median_ms']:.3f} ms exceeded "
            f"ceiling {row['ceiling_ms']:.3f} ms, re-measured median "
            f"{row['remeasure_median_ms']:.3f} ms is within budget — one retry, "
            "not failing the run"
        )
    if not still_breaching:
        return Verdict(
            0,
            lines + [f"perf budgets ok after one re-measure of {', '.join(cleared)}"],
            block,
        )
    lines.append("perf budget exceeded on re-measure:")
    for item, again in still_breaching:
        lines.append(
            f"  {item.path}: first median {item.median_ms:.3f} ms, re-measured median "
            f"{again:.3f} ms; both exceed budget {item.budget_ms:.3f} ms + "
            f"{tolerance:g}% (ceiling {item.ceiling_ms:.3f} ms)"
        )
    return Verdict(1, lines, block)


def remeasure_command(
    *,
    harness: Path,
    output_dir: Path,
    report: dict,
    names: list[str],
    python: str = sys.executable,
) -> list[str]:
    """The exact command that re-profiles only `names`, as the report measured them.

    Corpus pin, warmup and reps come from the report under test, not from this
    process, so the second measurement is of the same profile. The scratch
    directory and tick width are replayed too, because they change what the
    paths actually do; the database resolves through the same environment the
    first run used.
    """
    command = [python, str(harness), "--output-dir", str(output_dir)]
    command += ["--warmup", str(int(report["warmup"])), "--reps", str(int(report["reps"]))]
    for root in report["corpus_roots"]:
        command += ["--corpus", str(root)]
    for name in names:
        command += ["--paths", name]
    if report.get("database_url_kind") == "cpu-only-recording":
        command.append("--cpu-only")
    if report.get("scratch_dir"):
        command += ["--scratch-dir", str(report["scratch_dir"])]
    if report.get("tick_n") is not None:
        command += ["--tick-n", str(int(report["tick_n"]))]
    return command


def run_remeasure(
    *,
    report: dict,
    harness: Path,
    output_dir: Path,
    names: list[str],
    python: str | None = None,
    timeout: float = REMEASURE_TIMEOUT_S,
) -> dict | None:
    """Re-profile the breached paths once. Returns the second report, or None."""
    command = remeasure_command(
        harness=harness,
        output_dir=output_dir,
        report=report,
        names=names,
        python=python or sys.executable,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"perf re-measure: re-profiling {', '.join(names)} once (same corpus pin, warmup, reps)")
    try:
        completed = subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        print(f"perf re-measure: harness exceeded {timeout:g}s and was killed")
        return None
    if completed.returncode != 0:
        if completed.stdout:
            sys.stdout.write(completed.stdout)
        if completed.stderr:
            sys.stderr.write(completed.stderr)
        print(f"perf re-measure: harness exited {completed.returncode}")
        return None
    try:
        return load_remeasure_report(output_dir / "profile-report.json", names)
    except (OSError, ValueError, KeyError) as exc:
        print(f"perf re-measure: report unusable: {exc}")
        return None


def record_remeasurement(report_path: Path, report: dict, block: dict) -> None:
    """Keep both medians in the report CI uploads as evidence."""
    report["remeasure"] = block
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(f"perf re-measure: both medians recorded in {report_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument(
        "--budgets",
        type=Path,
        default=Path(__file__).resolve().parent / "budgets.json",
    )
    parser.add_argument(
        "--reprofile",
        action="store_true",
        help=(
            "Re-profile the breached paths exactly once before failing the run. "
            "perf.yml passes this; without it a first breach fails immediately."
        ),
    )
    parser.add_argument(
        "--reprofile-dir",
        type=Path,
        default=None,
        help="Where the one re-measure writes its own report (default: <report dir>/remeasure).",
    )
    parser.add_argument(
        "--harness",
        type=Path,
        default=HARNESS_PATH,
        help="Profile harness re-invoked for the one re-measure.",
    )
    args = parser.parse_args(argv)
    report = load_report(args.report)
    budgets = load_budgets(args.budgets)
    corpus_problems = assert_corpus_shape(report, budgets)
    if corpus_problems:
        print("perf corpus mismatch:")
        for item in corpus_problems:
            print(f"  {item}")
        return 1
    remeasure: Callable[[list[str]], dict | None] | None = None
    if args.reprofile:
        output_dir = args.reprofile_dir or args.report.resolve().parent / "remeasure"

        def remeasure_breached(names: list[str]) -> dict | None:
            return run_remeasure(
                report=report,
                harness=args.harness,
                output_dir=output_dir,
                names=names,
            )

        remeasure = remeasure_breached

    verdict = judge(report, budgets, remeasure)
    for line in verdict.lines:
        print(line)
    if verdict.remeasurement is not None:
        record_remeasurement(args.report, report, verdict.remeasurement)
    return verdict.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
