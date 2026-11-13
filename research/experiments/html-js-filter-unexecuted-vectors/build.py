"""Build a table from published result evidence; all input evidence is read-only."""

import argparse
import csv
import difflib
import io
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path

TASK_NAME = "terminal-bench/html-js-filter"
# library/tasks/terminal-bench-html-js-filter/tests/test_outputs.py calls
# make_batches(filtered_files, batch_size=16).
BATCH_SIZE = 16
COLUMNS = (
    "job",
    "trial",
    "role",
    "reward",
    "batches_run",
    "timed_out_batches",
    "vectors_unexecuted",
)
HERE = Path(__file__).resolve().parent
DEFAULT_RESULTS_ROOT = Path.home() / "Developer" / "eval-lab-results"


def parse_stdout(text: str) -> tuple[int, int]:
    """Count batch starts and batches with at least one URL load failure."""
    batches_run = 0
    timed_out_batches = 0
    batch_failed = False
    for line in text.splitlines():
        if re.fullmatch(r"RUN BATCH \d+ \(contains \d+ tests\)", line):
            batches_run += 1
            batch_failed = False
        elif batches_run and not batch_failed and line.strip().startswith("Failed to load URL:"):
            timed_out_batches += 1
            batch_failed = True
    return batches_run, timed_out_batches


def scan_results(results_root: Path) -> list[dict]:
    """Read the published date/job/trial layout without descending into artifacts."""
    rows = []
    for result_path in sorted(results_root.glob("*/*/*/result.json")):
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if result.get("task_name") != TASK_NAME:
            continue
        trial_dir = result_path.parent
        role = result.get("config", {}).get("agent", {}).get("name")
        config_path = trial_dir / "config.json"
        if not role and config_path.exists():
            config = json.loads(config_path.read_text(encoding="utf-8"))
            role = config.get("agent", {}).get("name")
        reward_path = trial_dir / "verifier" / "reward.txt"
        stdout_path = trial_dir / "verifier" / "test-stdout.txt"
        reward = reward_path.read_text(encoding="utf-8").strip() if reward_path.exists() else ""
        stdout = stdout_path.read_text(encoding="utf-8") if stdout_path.exists() else ""
        batches_run, timed_out_batches = parse_stdout(stdout)
        rows.append(
            {
                "job": trial_dir.parent.name,
                "trial": trial_dir.name,
                "role": role or "unknown",
                "reward": reward,
                "batches_run": batches_run,
                "timed_out_batches": timed_out_batches,
                "vectors_unexecuted": timed_out_batches * BATCH_SIZE,
            }
        )
    return sorted(rows, key=lambda row: (row["job"], row["trial"]))


def render_csv(rows: list[dict]) -> str:
    """Render a stable CSV with integer counts and the shared column order."""
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS_ROOT)
    parser.add_argument("--output", type=Path, default=HERE / "trials.csv")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if not args.results_root.is_dir():
        print(f"Results root does not exist or is not a directory: {args.results_root}", file=sys.stderr)
        return 2

    rows = scan_results(args.results_root)
    rendered = render_csv(rows)
    if args.check:
        current = ""
        if args.output.exists():
            with args.output.open(encoding="utf-8", newline="") as output:
                current = output.read()
            if current == rendered:
                return 0
        diff = difflib.unified_diff(
            current.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile=str(args.output),
            tofile="regenerated trials.csv",
        )
        sys.stderr.writelines(diff)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as output:
        output.write(rendered)
    print(f"Wrote {len(rows)} rows to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
