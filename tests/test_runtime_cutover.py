"""Generated reports and live Parquet must not mutate reviewed source."""

from __future__ import annotations

import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from evallab.digest import DigestRenderer
from evallab.evidence.parquet_io import write_table_atomic
from evallab.lessons import generate_lessons_file
from evallab.preflight import build_preflight_report
from evallab.queue import DirectoryQueue
from evallab.schemas import AutoRunRule, ExperimentSpec, StandingApprovalsPolicy
from evallab.status_generator import update_status_file
from evallab.storage.paths import derived_root_from_environment, runtime_reports_dir

REPORT_DATE = date(2026, 8, 16)
NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _init_repo(repo: Path) -> str:
    for directory in ("queue/waiting", "docs", "research/observations", "sql"):
        (repo / directory).mkdir(parents=True)
    # The repository already keeps queue state outside versioned source.
    (repo / ".gitignore").write_text("/queue/\n")
    sql = Path(__file__).resolve().parents[1] / "sql/lessons.sql"
    (repo / "sql/lessons.sql").write_bytes(sql.read_bytes())
    (repo / "docs/STATUS.md").write_text("# reviewed status snapshot\n")
    (repo / "research/lessons.md").write_text("# reviewed lessons snapshot\n")
    spec = ExperimentSpec(
        spec_id="runtime-output-proof",
        name="runtime-report-proposal",
        hypothesis="reports preserve the reviewed source snapshots",
        purpose="practice",
        task="local/report-proof",
        agent="nop",
        submitted_by="test",
        submitted_at=NOW,
    )
    (repo / "queue/waiting/proof.json").write_text(spec.model_dump_json())
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "reviewed snapshots")
    return _git(repo, "rev-parse", "HEAD")


def _renderer(repo: Path) -> DigestRenderer:
    report = build_preflight_report(repo, now=NOW, paid_agents=(), quota_roots=())
    return DigestRenderer(
        repo_root=repo,
        queue=DirectoryQueue(repo / "queue"),
        policy=StandingApprovalsPolicy(
            daily_cost_ceiling_usd=20.0,
            per_job_cost_ceiling_usd=2.0,
            quiet_failure_rule=3,
            auto_run=[AutoRunRule(name="local-controls", agents=["nop"])],
        ),
        trial_loader=lambda _day: [],
        drift_loader=lambda _day: [],
        preflight_loader=lambda: report,
        storm_loader=lambda _day: [],
    )


def test_runtime_writers_leave_source_and_head_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EVALLAB_DERIVED_ROOT", raising=False)
    repo = tmp_path / "repo"
    head_before = _init_repo(repo)
    snapshots = {
        path: (repo / path).read_bytes()
        for path in ("docs/STATUS.md", "research/lessons.md")
    }
    derived = derived_root_from_environment(repo, notify=lambda _: None)
    facts_path = derived / "runtime-proof.parquet"
    write_table_atomic(facts_path, [{"reward": 0.5}], pa.schema([("reward", pa.float64())]))
    assert pq.read_table(facts_path).to_pylist() == [{"reward": 0.5}]
    assert not derived.is_relative_to(repo)

    status = update_status_file(repo, target_date=REPORT_DATE, database_url="")
    lessons = generate_lessons_file(repo)
    digest = _renderer(repo).write(report_date=REPORT_DATE)
    assert {status, lessons, digest} == {
        runtime_reports_dir(repo) / "STATUS.md",
        runtime_reports_dir(repo) / "lessons.md",
        runtime_reports_dir(repo) / "2026-08-16.md",
    }
    assert "runtime-report-proposal" in status.read_text()
    assert "runtime-report-proposal" in digest.read_text()
    assert "generated-by: lessons v1" in lessons.read_text()
    assert all((repo / path).read_bytes() == before for path, before in snapshots.items())
    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all") == ""


def test_snapshot_promotion_requires_explicit_destinations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EVALLAB_DERIVED_ROOT", raising=False)
    repo = tmp_path / "repo"
    head_before = _init_repo(repo)
    status = repo / "docs/STATUS.md"
    lessons = repo / "research/lessons.md"
    digest = repo / "digests/promoted.md"
    update_status_file(repo, target_date=REPORT_DATE, database_url="", destination=status)
    generate_lessons_file(repo, output_path=lessons)
    _renderer(repo).write(report_date=REPORT_DATE, destination=digest)
    assert "runtime-report-proposal" in status.read_text()
    assert "runtime-report-proposal" in digest.read_text()
    assert "generated-by: lessons v1" in lessons.read_text()
    assert _git(repo, "diff", "--name-only").splitlines() == [
        "docs/STATUS.md",
        "research/lessons.md",
    ]
    assert _git(repo, "rev-parse", "HEAD") == head_before
