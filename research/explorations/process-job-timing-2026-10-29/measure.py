"""Time ``evallab process-job`` phases on read-only copies of retained jobs (HAR-223).

Usage (from the repo root, locked env)::

    uv run python research/explorations/process-job-timing-2026-10-29/measure.py \\
        --jobs-file research/explorations/process-job-timing-2026-10-29/jobs.txt \\
        --out /tmp/process-job-timing.csv

``--jobs-file`` lines are ``results:<date>/<published-dir>`` (resolved under
``--results-home``) or ``path:<job dir>``; ``#`` starts a comment. Nothing is
processed in place: every job is copied under ``--work-dir`` first, results
are published into a mirror of the results home (only the files
``write_index`` reads), and the real results home is never written.

Two call shapes are timed, matching the production callers:

* ``post-run``: ``process_job(job, root=repo)`` as the runner calls it, with
  ``ingest=False`` (catalog ingest needs a live Postgres) and the publish into
  the mirror, so the INDEX rewrite sees a realistic job count.
* ``nightly``: ``ingest=False, publish=False`` plus the Parquet projection, as
  ``nightly_refresh`` calls it.

Each phase is wrapped in a wall-clock timer (inclusive). Rows report the
repeat with the lowest wall time, plus median/max wall over all repeats, so a
busy machine shows up as spread instead of inflating the phase columns.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import statistics
import time
from functools import wraps
from pathlib import Path
from typing import Any

from evallab import auto_watch, counts, database, process_job, results, results_home
from evallab.evidence import atif
from evallab.interpretation import features
from evallab.model_capture import iter_trial_dirs

TIMED: tuple[tuple[Any, str, str], ...] = (
    (process_job, "_iter_trial_dirs", "iter_trial_dirs"),
    (process_job, "_job_ledger_block", "ledger"),
    (process_job, "_job_task_identity", "task_identity"),
    (process_job, "_process_trial", "process_trial"),
    (process_job, "_selfhosted_estimate", "estimate"),
    (process_job, "_attach_trial_counts", "trial_counts"),
    (process_job, "_attach_decision", "decision"),
    (process_job, "_render_trial_markdown", "render_trial"),
    (process_job, "_render_job_markdown", "render_job"),
    (process_job, "_project_job", "projection"),
    (results_home, "publish_job", "publish"),
    (results_home, "build_provenance", "publish_provenance"),
    (results_home, "_copy_tree", "publish_copy"),
    (results_home, "write_index", "publish_index"),
    (counts, "find_label_root", "label_root"),
    (auto_watch, "read_watch_alerts", "watch"),
    (auto_watch, "read_watch_acks", "watch"),
    (database, "initialize", "ingest"),
    (database, "ingest", "ingest"),
    (results, "load_job", "load_job"),
    (atif, "project_jobs", "proj_atif"),
    (features, "trial_features", "proj_features"),
    (features, "write_features", "proj_features"),
)
SETUP = ("iter_trial_dirs", "ledger", "task_identity", "watch", "label_root")
LOOP = ("process_trial", "estimate", "trial_counts", "decision")
RENDER = ("render_trial", "render_job")
MIRROR_FILES = ("provenance.json", "config.json", "lab-metadata.json", "processed/job.json")

acc: dict[str, float] = {}


def install_timers() -> None:
    for module, name, key in TIMED:
        original = getattr(module, name)

        @wraps(original)
        def inner(*args: Any, _fn: Any = original, _key: str = key, **kwargs: Any) -> Any:
            start = time.perf_counter()
            try:
                return _fn(*args, **kwargs)
            finally:
                acc[_key] = acc.get(_key, 0.0) + time.perf_counter() - start

        setattr(module, name, inner)


def parse_jobs(jobs_file: Path, home: Path) -> list[tuple[Path, str | None]]:
    """``(source dir, published key or None)`` for each non-comment line."""
    jobs: list[tuple[Path, str | None]] = []
    for raw in jobs_file.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        kind, _, value = line.partition(":")
        if kind == "results":
            jobs.append((home / value, value))
        elif kind == "path":
            jobs.append((Path(value).expanduser(), None))
        else:
            raise ValueError(f"Unknown jobs-file entry: {line!r}")
    return jobs


def build_mirror(home: Path, mirror: Path, skip: set[str]) -> int:
    shutil.rmtree(mirror, ignore_errors=True)
    copied = 0
    for day in sorted(p for p in home.iterdir() if p.is_dir() and len(p.name) == 10):
        for job in sorted(p for p in day.iterdir() if p.is_dir()):
            if f"{day.name}/{job.name}" in skip or not (job / "provenance.json").is_file():
                continue
            for relative in MIRROR_FILES:
                source = job / relative
                if source.is_file():
                    target = mirror / day.name / job.name / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
            copied += 1
    return copied


def stage_job(source: Path, work: Path) -> Path:
    """Copy a retained job as a raw Harbor job: publication files removed."""
    provenance = source / "provenance.json"
    name = json.loads(provenance.read_text())["job_name"] if provenance.is_file() else source.name
    target = work / name
    if not target.exists():
        shutil.copytree(source, target, symlinks=True)
        for extra in ("provenance.json", "uncommitted.diff"):
            (target / extra).unlink(missing_ok=True)
    return target


def run_once(job: Path, mode: str, scratch: Path, mirror: Path, repo: Path) -> dict[str, Any]:
    acc.clear()
    if mode == "post-run":
        options: dict[str, Any] = {
            "ingest": False,
            "publish": True,
            "results_home": mirror,
            "root": repo,
        }
    else:
        shutil.rmtree(scratch / "out", ignore_errors=True)
        shutil.rmtree(scratch / "parquet", ignore_errors=True)
        options = {
            "ingest": False,
            "publish": False,
            "output_dir": scratch / "out",
            "parquet_root": scratch / "parquet",
            "root": repo,
        }
    start = time.perf_counter()
    process_job.process_job(job, **options)
    wall = time.perf_counter() - start
    phases = dict(acc)

    def part(keys: tuple[str, ...]) -> float:
        return sum(phases.get(key, 0.0) for key in keys)

    publish = phases.get("publish", 0.0)
    projection = phases.get("projection", 0.0)
    row = {
        "wall_s": wall,
        "setup_s": part(SETUP),
        "trial_loop_s": part(LOOP),
        "render_s": part(RENDER),
        "projection_s": projection,
        "proj_load_job_s": phases.get("load_job", 0.0) if projection else 0.0,
        "proj_atif_s": phases.get("proj_atif", 0.0),
        "proj_features_s": phases.get("proj_features", 0.0),
        "publish_s": publish,
        "publish_provenance_s": phases.get("publish_provenance", 0.0),
        "publish_copy_s": phases.get("publish_copy", 0.0),
        "publish_index_s": phases.get("publish_index", 0.0),
    }
    row["publish_other_s"] = (
        publish - row["publish_provenance_s"] - row["publish_copy_s"] - row["publish_index_s"]
    )
    row["other_s"] = (
        wall - row["setup_s"] - row["trial_loop_s"] - row["render_s"] - projection - publish
    )
    row["per_trial_work_s"] = row["trial_loop_s"] + projection
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--jobs-file", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--results-home", type=Path, default=Path("~/Developer/eval-lab-results").expanduser()
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--work-dir", type=Path, default=Path("/private/tmp/process-job-timing"))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--modes", default="post-run,nightly")
    args = parser.parse_args()

    install_timers()
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    mirror = work / "home"
    jobs = parse_jobs(args.jobs_file, args.results_home)
    mirrored = build_mirror(args.results_home, mirror, {key for _, key in jobs if key})
    print(f"mirror: {mirrored} published jobs")

    staged = [(stage_job(source, work / "jobs"), source) for source, _ in jobs]
    warm = staged[0][0]
    for mode in args.modes.split(","):  # lazy imports are not charged to the first job
        run_once(warm, mode, work / "scratch" / "_warmup", mirror, args.repo_root)

    rows: list[dict[str, Any]] = []
    for job, source in staged:
        trials = len(iter_trial_dirs(job))
        size = sum(f.stat().st_size for f in job.rglob("*") if f.is_file())
        for mode in args.modes.split(","):
            runs = []
            for _ in range(args.repeats):
                scratch = work / "scratch" / job.name
                scratch.mkdir(parents=True, exist_ok=True)
                runs.append(run_once(job, mode, scratch, mirror, args.repo_root))
            best = min(runs, key=lambda run: run["wall_s"])
            walls = [run["wall_s"] for run in runs]
            row = {
                "mode": mode,
                "job": job.name,
                "source": source.parent.name + "/" + source.name,
                "trials": trials,
                "bytes": size,
                "repeats": len(runs),
                **best,
                "wall_median_s": statistics.median(walls),
                "wall_max_s": max(walls),
            }
            row["per_trial_work_share"] = row["per_trial_work_s"] / row["wall_s"]
            rows.append(row)
            print(f"{job.name[:48]:48} {mode:8} best={best['wall_s']:.3f}s max={max(walls):.2f}s")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow({k: f"{v:.4f}" if isinstance(v, float) else v for k, v in row.items()})


if __name__ == "__main__":
    main()
