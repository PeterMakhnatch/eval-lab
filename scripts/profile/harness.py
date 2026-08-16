"""Repeatable SPEED profile: six paths, median after warmup, no Harbor, no shared catalog."""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from evallab.atif import export_trajectories, ingest_and_project  # noqa: E402
from evallab.database import ingest, ingest_job, initialize  # noqa: E402
from evallab.digest import DigestRenderer, DigestTrial  # noqa: E402
from evallab.facts import export_facts  # noqa: E402
from evallab.queue import DirectoryQueue, Executor, load_policy  # noqa: E402
from evallab.results import JobRecord, discover_job_dirs, load_jobs  # noqa: E402
from evallab.schemas import ExperimentSpec  # noqa: E402

PATH_NAMES = (
    "ingest",
    "projection",
    "facts",
    "digest",
    "queue-tick-100",
    "fleet-status",
)

SHARED_CATALOG_MARKERS = ("/evallab?", "/evallab", "dbname=evallab")
DEFAULT_SCRATCH_URL = (
    "postgresql://evallab:local-development-only@127.0.0.1:54329/evallab_speed_prof"
)
DEFAULT_ADMIN_URL = (
    "postgresql://evallab:local-development-only@127.0.0.1:54329/evallab"
)
# The profiled corpus is PINNED to an explicit set of Harbor job directories.
#
# It is deliberately NOT the directory `research/evidence/runs`. Profiling a
# directory measures the volume of whatever is committed under it, so every
# evidence promotion silently re-scoped this gate and moved all six budgets for
# reasons that have nothing to do with code speed. Naming the job directories
# means growing the evidence corpus cannot change the measurement unless
# someone deliberately edits this tuple and re-baselines `budgets.json`.
#
# `scripts/profile/budgets.json` carries the expected shape of this corpus and
# `check_budgets.py` fails loudly if a report was produced against a different
# one. Use `--corpus` for ad-hoc profiling against anything else.
DEFAULT_CORPUS = (
    "research/evidence/runs/event-summary-nop-evidence",
    "research/evidence/runs/event-summary-oracle-evidence",
)


@dataclass(frozen=True)
class PathTiming:
    path: str
    median_ms: float
    min_ms: float
    max_ms: float
    reps: int
    notes: str


@dataclass(frozen=True)
class ProfileReport:
    schema_version: int
    generated_at: str
    machine: str
    python: str
    corpus_jobs: int
    corpus_result_json: int
    corpus_bytes: int
    corpus_roots: list[str]
    database_url_kind: str
    harbor_dispatch: str
    warmup: int
    reps: int
    paths: list[PathTiming]

    def path_names(self) -> list[str]:
        return [item.path for item in self.paths]


class RecordingConnection:
    """Accepts the psycopg surface ingest_job uses. No server."""

    def execute(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def cursor(self) -> RecordingConnection:
        return self

    def executemany(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def __enter__(self) -> RecordingConnection:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


def assert_not_shared_catalog(url: str) -> None:
    normalized = url.rstrip("/")
    if normalized.endswith("/evallab") or normalized.endswith("dbname=evallab"):
        raise ValueError(
            "refusing to profile against the shared evallab catalog; "
            "use a scratch database such as evallab_speed_prof"
        )
    if "/evallab?" in normalized:
        raise ValueError("refusing to profile against the shared evallab catalog")


def resolve_corpus_roots(entries: list[str]) -> list[Path]:
    """Turn repo-relative corpus entries into paths, refusing ones that vanished.

    A pinned entry that no longer exists must fail loudly. Silently profiling a
    smaller corpus is exactly the failure mode this pin exists to prevent.
    """
    roots: list[Path] = []
    missing: list[str] = []
    for entry in entries:
        root = REPO_ROOT / entry
        if not root.exists():
            missing.append(entry)
        roots.append(root)
    if missing:
        raise ValueError(
            f"pinned corpus entries do not exist: {missing}. The profiled corpus "
            "is pinned in harness.py DEFAULT_CORPUS; if a job directory moved, "
            "update the pin and re-baseline scripts/profile/budgets.json."
        )
    return roots


def relative_to_repo(path: Path) -> str:
    """Repo-relative string when possible, so reports compare across machines."""
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def corpus_stats(roots: list[Path]) -> tuple[list[Path], int, int]:
    job_dirs = discover_job_dirs(roots)
    result_files = 0
    total_bytes = 0
    for job_dir in job_dirs:
        for path in job_dir.rglob("result.json"):
            result_files += 1
            total_bytes += path.stat().st_size
        for path in job_dir.rglob("*"):
            if path.is_file() and path.name != "result.json":
                total_bytes += path.stat().st_size
    return job_dirs, result_files, total_bytes


def measure(fn: Callable[[], None], *, warmup: int, reps: int) -> tuple[float, float, float]:
    for _ in range(warmup):
        fn()
    samples: list[float] = []
    for _ in range(reps):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000.0)
    return statistics.median(samples), min(samples), max(samples)


def _inject_delay(inject_ms: dict[str, float], name: str) -> None:
    delay = inject_ms.get(name)
    if delay and delay > 0:
        time.sleep(delay / 1000.0)


def ensure_scratch_database(url: str, admin_url: str) -> None:
    assert_not_shared_catalog(url)
    import psycopg

    dbname = url.rsplit("/", 1)[-1].split("?")[0]
    if dbname in {"evallab", "postgres"}:
        raise ValueError(f"scratch database name is not isolated: {dbname}")
    try:
        with psycopg.connect(url, connect_timeout=3) as connection:
            connection.execute("SELECT 1")
        return
    except Exception:
        pass
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=3) as admin:
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
        ).fetchone()
        if not exists:
            admin.execute(f'CREATE DATABASE "{dbname}"')


def _stub_runner(request: Any) -> Path:
    destination = request.jobs_dir / request.name
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def _time_ingest(
    jobs: list[JobRecord],
    *,
    root: Path,
    database_url: str | None,
    cpu_only: bool,
    inject_ms: dict[str, float],
) -> None:
    _inject_delay(inject_ms, "ingest")
    if cpu_only or database_url is None:
        connection = RecordingConnection()
        for job in jobs:
            ingest_job(connection, job, root=root)
        return
    assert_not_shared_catalog(database_url)
    initialize(database_url)
    ingest(database_url, jobs, root=root)


def _time_ingest_and_project(
    jobs: list[JobRecord],
    *,
    root: Path,
    database_url: str,
    output: Path,
    inject_ms: dict[str, float],
) -> None:
    _inject_delay(inject_ms, "ingest+projection")
    if output.exists():
        for child in output.rglob("*"):
            if child.is_file():
                child.unlink()
    assert_not_shared_catalog(database_url)
    ingest_and_project(database_url, jobs, root=root, output_root=output)


def _time_projection(jobs: list[JobRecord], output: Path, inject_ms: dict[str, float]) -> None:
    _inject_delay(inject_ms, "projection")
    if output.exists():
        for child in output.rglob("*"):
            if child.is_file():
                child.unlink()
    export_trajectories(jobs, output)


def _time_facts(jobs: list[JobRecord], output: Path, inject_ms: dict[str, float]) -> None:
    _inject_delay(inject_ms, "facts")
    if output.exists():
        for child in output.rglob("*"):
            if child.is_file():
                child.unlink()
    export_facts(jobs, output)


def _time_digest(repo_root: Path, inject_ms: dict[str, float]) -> None:
    _inject_delay(inject_ms, "digest")
    queue = DirectoryQueue(repo_root / "queue")
    policy = load_policy(REPO_ROOT / "policy/standing-approvals.yaml")

    def trials(_day: date) -> list[DigestTrial]:
        return [
            DigestTrial(
                job_name="profile-stub",
                task_name="event-summary",
                agent_name="oracle",
                model_name=None,
                reward=1.0,
                exception_type=None,
                cost_usd=0.0,
                finished_at="2026-08-14T00:00:00",
            )
        ]

    renderer = DigestRenderer(
        repo_root=repo_root,
        queue=queue,
        policy=policy,
        trial_loader=trials,
        drift_loader=lambda _day: [],
    )
    renderer.write(report_date=date(2026, 8, 15), dispatched=0)


def _time_queue_tick(
    scratch: Path, inject_ms: dict[str, float], *, tick_n: int
) -> None:
    _inject_delay(inject_ms, "queue-tick-100")
    queue_root = scratch / "queue-tick"
    if queue_root.exists():
        for child in queue_root.rglob("*"):
            if child.is_file():
                child.unlink()
    (scratch / "tasks" / "event-summary").mkdir(parents=True, exist_ok=True)
    (scratch / "tasks" / "event-summary" / "task.toml").write_text(
        "schema_version = \"1.4\"\n"
    )
    queue = DirectoryQueue(queue_root)
    policy = load_policy(REPO_ROOT / "policy/standing-approvals.yaml")
    for index in range(tick_n):
        spec = ExperimentSpec(
            spec_id=f"speed-tick-{index:03d}",
            name=f"speed-tick-{index:03d}",
            hypothesis="synthetic approved spec for a 100-wide tick profile",
            task="tasks/event-summary",
            agent="oracle",
            submitted_by="speed-profile",
            policy_rule="local-controls",
            jobs_dir="runs",
        )
        filename = f"oracle-{spec.name}.json"
        payload = spec.model_dump_json(indent=2, exclude_none=True) + "\n"
        (queue.state_dir("approved") / filename).write_text(payload)
    executor = Executor(
        repo_root=scratch,
        queue=queue,
        policy=policy,
        runner=_stub_runner,
        ingester=lambda _path: None,
        spent_today=lambda: 0.0,
        consecutive_harness_failures=lambda: 0,
        credential_probe=lambda: frozenset(),
    )
    dispatched = executor.tick()
    if dispatched != tick_n:
        raise RuntimeError(f"expected {tick_n} stub dispatches, got {dispatched}")


def _time_fleet_status(inject_ms: dict[str, float]) -> None:
    _inject_delay(inject_ms, "fleet-status")
    stub = Path(__file__).resolve().parent / "gh_stub.sh"
    env = os.environ.copy()
    env["PATH"] = f"{stub.parent}:{env.get('PATH', '')}"
    # Make `gh` resolve to the stub without renaming the real binary globally.
    gh_dir = stub.parent / "_gh_bin"
    gh_dir.mkdir(exist_ok=True)
    gh_link = gh_dir / "gh"
    if not gh_link.exists():
        gh_link.symlink_to(stub)
    env["PATH"] = f"{gh_dir}:{env.get('PATH', '')}"
    completed = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts/fleet-status.sh"), "--since", "1h"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"fleet-status.sh exited {completed.returncode}")


def run_profile(
    *,
    corpus_roots: list[Path],
    warmup: int,
    reps: int,
    database_url: str | None,
    admin_url: str,
    cpu_only: bool,
    inject_ms: dict[str, float],
    work_dir: Path,
    tick_n: int = 100,
    fleet_fn: Callable[[], None] | None = None,
) -> ProfileReport:
    if warmup < 1 or reps < 5:
        raise ValueError("FORGE §4 requires warmup >= 1 and reps >= 5")
    job_dirs, result_json, corpus_bytes = corpus_stats(corpus_roots)
    jobs = load_jobs(corpus_roots)
    if not jobs:
        raise ValueError(f"no Harbor jobs under {corpus_roots}")

    scratch = work_dir / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    digest_root = scratch / "digest-repo"
    (digest_root / "digests").mkdir(parents=True, exist_ok=True)
    DirectoryQueue(digest_root / "queue")

    db_kind = "cpu-only-recording"
    if not cpu_only:
        if database_url is None:
            database_url = os.environ.get("EVAL_LAB_PROFILE_DATABASE_URL", DEFAULT_SCRATCH_URL)
        assert_not_shared_catalog(database_url)
        ensure_scratch_database(database_url, admin_url)
        db_kind = "scratch-postgres"

    timings: list[PathTiming] = []

    def add(name: str, fn: Callable[[], None], notes: str) -> None:
        median_ms, min_ms, max_ms = measure(fn, warmup=warmup, reps=reps)
        timings.append(
            PathTiming(
                path=name,
                median_ms=round(median_ms, 3),
                min_ms=round(min_ms, 3),
                max_ms=round(max_ms, 3),
                reps=reps,
                notes=notes,
            )
        )

    add(
        "ingest",
        lambda: _time_ingest(
            jobs,
            root=REPO_ROOT,
            database_url=database_url,
            cpu_only=cpu_only,
            inject_ms=inject_ms,
        ),
        "database.ingest on scratch DB"
        if not cpu_only
        else "database.ingest_job via recording connection",
    )
    add(
        "projection",
        lambda: _time_projection(jobs, scratch / "parquet-atif", inject_ms),
        "evallab.atif.export_trajectories",
    )
    add(
        "facts",
        lambda: _time_facts(jobs, scratch / "parquet-facts", inject_ms),
        "evallab.facts.export_facts",
    )
    add(
        "digest",
        lambda: _time_digest(digest_root, inject_ms),
        "DigestRenderer.write with catalog seams stubbed",
    )
    add(
        "queue-tick-100",
        lambda: _time_queue_tick(scratch / "tick-root", inject_ms, tick_n=tick_n),
        f"Executor.tick N={tick_n}; runner/ingester/catalog stubbed",
    )
    add(
        "fleet-status",
        lambda: (fleet_fn or (lambda: _time_fleet_status(inject_ms)))(),
        "scripts/fleet-status.sh with gh stubbed"
        if fleet_fn is None
        else "injected fleet-status callable",
    )
    if not cpu_only and database_url is not None:
        add(
            "ingest+projection",
            lambda: _time_ingest_and_project(
                jobs,
                root=REPO_ROOT,
                database_url=database_url,
                output=scratch / "merged-parquet",
                inject_ms=inject_ms,
            ),
            "atif.ingest_and_project (PIPELINE unified path)",
        )

    missing = [name for name in PATH_NAMES if name not in {item.path for item in timings}]
    if missing:
        raise RuntimeError(f"report missing paths: {missing}")

    return ProfileReport(
        schema_version=1,
        generated_at=datetime.now(UTC).isoformat(),
        machine=platform.platform(),
        python=sys.version.split()[0],
        corpus_jobs=len(job_dirs),
        corpus_result_json=result_json,
        corpus_bytes=corpus_bytes,
        corpus_roots=[relative_to_repo(path) for path in corpus_roots],
        database_url_kind=db_kind,
        harbor_dispatch="stubbed",
        warmup=warmup,
        reps=reps,
        paths=timings,
    )


def render_markdown(report: ProfileReport) -> str:
    lines = [
        "# Eval Lab profile",
        "",
        f"- generated_at: {report.generated_at}",
        f"- machine: {report.machine}",
        f"- python: {report.python}",
        (
            f"- corpus: {report.corpus_jobs} jobs, "
            f"{report.corpus_result_json} result.json, "
            f"{report.corpus_bytes} bytes"
        ),
        f"- corpus_roots: {', '.join(report.corpus_roots)}",
        f"- database: {report.database_url_kind}",
        f"- harbor_dispatch: {report.harbor_dispatch}",
        f"- method: median of {report.reps} reps after {report.warmup} warmup",
        "",
        "| path | median_ms | min_ms | max_ms | reps | notes |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for item in report.paths:
        lines.append(
            f"| {item.path} | {item.median_ms:.3f} | {item.min_ms:.3f} | "
            f"{item.max_ms:.3f} | {item.reps} | {item.notes} |"
        )
    lines.append("")
    return "\n".join(lines)


def report_to_json(report: ProfileReport) -> dict[str, Any]:
    payload = asdict(report)
    payload["paths"] = [asdict(item) for item in report.paths]
    return payload


def parse_inject(raw: list[str]) -> dict[str, float]:
    parsed: dict[str, float] = {}
    for item in raw:
        if "=" not in item:
            raise ValueError(f"inject must be PATH=MS, got {item!r}")
        name, value = item.split("=", 1)
        parsed[name] = float(value)
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        action="append",
        default=[],
        help=(
            "Repo-relative job root or job directory (repeatable) for ad-hoc "
            "profiling. Omit it to profile the pinned default corpus: "
            + ", ".join(DEFAULT_CORPUS)
        ),
    )
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--cpu-only", action="store_true")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--admin-url", default=DEFAULT_ADMIN_URL)
    parser.add_argument("--inject-ms", action="append", default=[])
    parser.add_argument("--tick-n", type=int, default=100)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "runs" / "_speed",
    )
    args = parser.parse_args(argv)
    roots = resolve_corpus_roots(list(args.corpus) or list(DEFAULT_CORPUS))
    report = run_profile(
        corpus_roots=roots,
        warmup=args.warmup,
        reps=args.reps,
        database_url=args.database_url,
        admin_url=args.admin_url,
        cpu_only=args.cpu_only,
        inject_ms=parse_inject(args.inject_ms),
        work_dir=args.output_dir,
        tick_n=args.tick_n,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    markdown = render_markdown(report)
    (args.output_dir / "profile-report.md").write_text(markdown)
    (args.output_dir / "profile-report.json").write_text(
        json.dumps(report_to_json(report), indent=2) + "\n"
    )
    sys.stdout.write(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
