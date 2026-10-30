"""Time publishing one job against results homes of growing size (HAR-237).

HAR-223 measured ``publish_job``'s INDEX rewrite as 98% of post-run wall time,
1.2-1.6 s at 8.2k published jobs, growing linearly with the home. This measures
the same publish on mirrors of the real home at four sizes, with the incremental
splice (``update_index``, what publishing uses now) and with the full rebuild
(``write_index``) it replaced, and checks at every size that the splice wrote
exactly the bytes the rebuild writes.

The mirror holds the files the INDEX reads for each published job
(``provenance.json``, ``config.json``, ``lab-metadata.json``,
``processed/job.json``), copied from ``~/Developer/eval-lab-results``; the real
home is only read and nothing is published into it. One mirror is built and
smaller homes are made by parking whole day directories beside it, so the
subsets are the same published jobs the real home holds.

Reproduce:
``uv run python research/explorations/har237-incremental-index-2026-10-30/measure.py``
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import statistics
import time
from pathlib import Path

from evallab import results_home

DEFAULT_REAL_HOME = Path.home() / "Developer" / "eval-lab-results"
DEFAULT_REPO = Path.home() / "Developer" / "eval-lab"
INDEX_INPUTS = ("provenance.json", "config.json", "lab-metadata.json", "processed/job.json")
INDEX_FILES = ("INDEX.md", "INDEX-all.md")
#: The measured job publishes into its own day, so it never collides with real days.
STAGE_DAY = "2099-12-31"


def published_jobs(home: Path) -> list[Path]:
    """Every ``<home>/<day>/<job>`` with provenance, in ``write_index``'s walk order."""
    jobs: list[Path] = []
    for day in sorted(home.iterdir()):
        if not day.is_dir() or day.is_symlink():
            continue
        for job in sorted(day.iterdir()):
            if job.is_dir() and not job.is_symlink() and (job / "provenance.json").is_file():
                jobs.append(job)
    return jobs


def build_mirror(real: Path, mirror: Path) -> int:
    """Copy every published job's INDEX inputs into ``mirror``, keeping ``<day>/<job>``."""
    shutil.rmtree(mirror, ignore_errors=True)
    jobs = published_jobs(real)
    for job in jobs:
        dest = mirror / job.parent.name / job.name
        dest.mkdir(parents=True, exist_ok=True)
        for name in INDEX_INPUTS:
            source = job / name
            if source.is_file():
                (dest / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, dest / name)
    return len(jobs)


def keep_days(mirror: Path, parked: Path, days: list[str]) -> int:
    """Leave only ``days`` under ``mirror``; return the job count that remains.

    The measured job publishes into its own day (``STAGE_DAY``), which no real
    day uses: it is discarded when parking rather than parked, so the mirror and
    the parked set never claim the same day directory.
    """
    parked.mkdir(parents=True, exist_ok=True)
    keep = set(days)
    for day in sorted(mirror.iterdir()):
        if not day.is_dir() or day.is_symlink() or day.name in keep:
            continue
        if day.name == STAGE_DAY:
            shutil.rmtree(day)
            continue
        day.rename(parked / day.name)
    for day in sorted(parked.iterdir()):
        if day.name in keep:
            day.rename(mirror / day.name)
    return sum(1 for _ in published_jobs(mirror))


def stage_job(work: Path) -> Path:
    """A small, conventional single-trial Harbor job to publish."""
    job = work / "runs" / "har237-measure"
    if job.exists():
        shutil.rmtree(job)
    (job / "trial-one").mkdir(parents=True)
    (job / "config.json").write_text(
        json.dumps({"agents": [{"name": "nop", "model_name": None}]}), encoding="utf-8"
    )
    (job / "result.json").write_text('{"id": "har237-measure"}', encoding="utf-8")
    (job / "lab-metadata.json").write_text(
        json.dumps(
            {
                "started_at": f"{STAGE_DAY}T00:00:00+00:00",
                "command": ["harbor", "run", "--agent", "nop"],
                "repository": {"commit": "0" * 40, "dirty": False},
                "experiment": {"task_id": "format-code"},
            }
        ),
        encoding="utf-8",
    )
    (job / "experiment-spec.json").write_text(
        json.dumps({"question_ref": "har237-incremental-index"}), encoding="utf-8"
    )
    (job / "trial-one" / "result.json").write_text(
        json.dumps({"trial_name": "trial-one", "verifier_result": {"rewards": {"reward": 1.0}}}),
        encoding="utf-8",
    )
    processed = job / "processed"
    processed.mkdir()
    (processed / "job.json").write_text(
        json.dumps(
            {
                "schema": "process-job/v1",
                "job_name": job.name,
                "summary": {
                    "n_pass": 1,
                    "n_fail": 0,
                    "n_unscored": 0,
                    "n_counted_pass": 1,
                    "n_counted_fail": 0,
                    "n_excluded": 0,
                    "excluded_reasons": {},
                    "spend": None,
                },
            }
        ),
        encoding="utf-8",
    )
    return job


def timed(call, repeats: int) -> dict[str, float]:
    """Best, median and max wall time over ``repeats`` calls."""
    seconds: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        call()
        seconds.append(time.perf_counter() - started)
    return {"best": min(seconds), "median": statistics.median(seconds), "max": max(seconds)}


def measure(home: Path, job: Path, repo: Path, repeats: int) -> list[dict[str, object]]:
    """Time one incremental publish and one full rebuild of ``home``."""
    count = sum(1 for _ in published_jobs(home))
    # Rebuild once so the subset's INDEX pair is the canonical starting point.
    results_home.write_index(home, primary_checkout=repo)
    rows: list[dict[str, object]] = []
    steps = (
        (
            "publish (incremental)",
            lambda: results_home.publish_job(
                job, root=home, pr_lookup=lambda _commit: None, primary_checkout=repo
            ),
        ),
        ("index rebuild", lambda: results_home.write_index(home, primary_checkout=repo)),
    )
    for label, call in steps:
        samples = timed(call, repeats)
        rows.append({"jobs": count, "step": label, **samples})
        print(f"{count:>6} jobs  {label:<20} {samples['best'] * 1000:8.2f} ms best")
    results_home.publish_job(job, root=home, pr_lookup=lambda _commit: None, primary_checkout=repo)
    spliced = {name: (home / name).read_bytes() for name in INDEX_FILES}
    results_home.write_index(home, primary_checkout=repo)
    rebuilt = {name: (home / name).read_bytes() for name in INDEX_FILES}
    rows.append(
        {
            "jobs": count,
            "step": "splice == rebuild",
            "identical": spliced == rebuilt,
            "bytes": sum(len(value) for value in spliced.values()),
        }
    )
    print(f"{count:>6} jobs  splice == rebuild   {spliced == rebuilt}")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--real-home", type=Path, default=DEFAULT_REAL_HOME)
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--work", type=Path, default=Path("/tmp/har237-measure"))
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--sizes", type=int, nargs="*", default=[1000, 2000, 4000, 0])
    parser.add_argument(
        "--reuse", action="store_true", help="reuse an existing mirror instead of re-copying it"
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    real = args.real_home.expanduser()
    home = args.work / "mirror"
    parked = args.work / "parked"
    args.work.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    if args.reuse and home.is_dir():
        if parked.is_dir():
            for day in sorted(parked.iterdir()):
                day.rename(home / day.name)
        total = len(published_jobs(home))
        print(f"reusing {home}: {total} published jobs")
    else:
        shutil.rmtree(parked, ignore_errors=True)
        total = build_mirror(real, home)
        print(f"{real}: {total} published jobs mirrored in {time.perf_counter() - started:.1f}s")
    days = [day.name for day in sorted(home.iterdir()) if day.is_dir() and day.name != STAGE_DAY]
    per_day = {day: len(list((home / day).iterdir())) for day in days}
    job = stage_job(args.work)

    rows: list[dict[str, object]] = []
    for size in args.sizes:
        wanted = total if size == 0 else size
        kept: list[str] = []
        seen = 0
        for day in days:
            if seen >= wanted:
                break
            kept.append(day)
            seen += per_day[day]
        keep_days(home, parked, kept)
        rows.extend(measure(home, job, args.repo.expanduser(), args.repeats))

    out = args.out or Path(__file__).with_name("process-job-index-timing.csv")
    with out.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=["jobs", "step", "best", "median", "max", "identical", "bytes"]
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: (f"{value:.4f}" if isinstance(value, float) else value)
                    for key, value in row.items()
                }
            )
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
