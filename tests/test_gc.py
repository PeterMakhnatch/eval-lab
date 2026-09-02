from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from evallab.gc import (
    COMPRESS_AFTER,
    PRUNE_AFTER,
    CatalogEntry,
    apply_gc,
    archive_path,
    catalog_path_exists,
    doctor_disk_line,
    filesystem_catalog,
    format_plan,
    nightly_gc_plan,
    plan_gc,
    run_gc,
    tombstone_path,
)
from evallab.queue import DirectoryQueue


class MemoryCatalog:
    def __init__(self, entries: list[CatalogEntry]) -> None:
        self._entries = {entry.job_id: entry for entry in entries}

    def get(self, job_id: str) -> CatalogEntry | None:
        return self._entries.get(job_id)

    def set_path(self, job_id: str, evidence_path: str) -> None:
        entry = self._entries[job_id]
        entry.evidence_path = evidence_path


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def make_job(
    root: Path,
    *,
    name: str,
    job_id: str,
    finished_at: str,
    spec_id: str | None = None,
    reward: float = 1.0,
) -> Path:
    job = root / name
    trial = job / f"{name}__abc"
    write_json(job / "config.json", {"job_name": name, "spec_id": spec_id})
    write_json(job / "lock.json", {"harbor": {"version": "0.21.0"}})
    write_json(
        job / "result.json",
        {
            "id": job_id,
            "started_at": finished_at,
            "finished_at": finished_at,
            "n_total_trials": 1,
            "stats": {"n_completed_trials": 1, "n_errored_trials": 0},
        },
    )
    if spec_id is not None:
        write_json(job / "lab-metadata.json", {"spec_id": spec_id})
    write_json(
        trial / "result.json",
        {
            "id": job_id.replace("00000000", "11111111"),
            "trial_name": trial.name,
            "task_name": "local-lab/sample-task",
            "agent_info": {"name": "oracle", "version": "1.0.0"},
            "verifier_result": {"rewards": {"reward": reward}},
            "exception_info": None,
        },
    )
    (trial / "verifier").mkdir(parents=True, exist_ok=True)
    (trial / "verifier/reward.json").write_text(json.dumps({"reward": reward}))
    return job


def catalog_for(*jobs: Path) -> MemoryCatalog:
    from evallab.results import load_job

    entries = []
    for path in jobs:
        job = load_job(path)
        entries.append(
            CatalogEntry(
                job_id=job.id,
                evidence_path=path.as_posix(),
                ingested=True,
                projected=True,
            )
        )
    return MemoryCatalog(entries)


def test_plan_is_dry_run_and_compresses_at_14_days(tmp_path: Path) -> None:
    now = datetime(2026, 3, 1, tzinfo=UTC)
    runs = tmp_path / "runs"
    young = make_job(
        runs,
        name="young-job",
        job_id="00000000-0000-0000-0000-000000000001",
        finished_at=(now - timedelta(days=2)).isoformat(),
        spec_id="spec-young",
    )
    ripe = make_job(
        runs,
        name="ripe-job",
        job_id="00000000-0000-0000-0000-000000000002",
        finished_at=(now - COMPRESS_AFTER - timedelta(hours=1)).isoformat(),
        spec_id="spec-ripe",
    )
    catalog = catalog_for(young, ripe)
    plan, applied = run_gc(
        tmp_path,
        apply=False,
        clock=lambda: now,
        catalog=catalog,
        runs_dir=runs,
    )
    assert applied is None
    assert (ripe / "result.json").is_file()
    assert [item.action for item in plan.actions] == ["compress"]
    assert plan.actions[0].job_name == "ripe-job"
    assert plan.actions[0].spec_id == "spec-ripe"
    assert any(skip.job_name == "young-job" for skip in plan.skipped)


def test_apply_compresses_then_prunes_with_tombstones_and_catalog(
    tmp_path: Path,
) -> None:
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    finished = t0.isoformat()
    runs = tmp_path / "runs"
    job = make_job(
        runs,
        name="aging-job",
        job_id="00000000-0000-0000-0000-000000000010",
        finished_at=finished,
        spec_id="spec-aging",
        reward=1.0,
    )
    catalog = catalog_for(job)
    events: list[object] = []

    at_compress = t0 + COMPRESS_AFTER + timedelta(minutes=1)
    plan = plan_gc(repo_root=tmp_path, runs_dir=runs, clock=lambda: at_compress, catalog=catalog)
    assert [item.action for item in plan.actions] == ["compress"]
    apply_gc(
        plan,
        repo_root=tmp_path,
        runs_dir=runs,
        catalog=catalog,
        clock=lambda: at_compress,
        append_event=events.append,
    )
    assert not job.exists()
    stone = tombstone_path(runs, "00000000-0000-0000-0000-000000000010")
    archive = archive_path(runs, "00000000-0000-0000-0000-000000000010")
    assert stone.is_file()
    assert archive.is_file()
    payload = json.loads(stone.read_text())
    assert payload["job_id"] == "00000000-0000-0000-0000-000000000010"
    assert payload["spec_id"] == "spec-aging"
    assert "result.json" in payload["digests"]
    assert payload["reward_summary"]["reward"] == 1.0
    assert payload["why"] == "compress_14d"
    assert catalog.get("00000000-0000-0000-0000-000000000010") is not None
    assert catalog_path_exists(tmp_path, catalog, "00000000-0000-0000-0000-000000000010")
    assert events[-1].event == "gc_compressed"

    at_prune = t0 + PRUNE_AFTER + timedelta(minutes=1)
    prune_plan = plan_gc(repo_root=tmp_path, runs_dir=runs, clock=lambda: at_prune, catalog=catalog)
    assert [item.action for item in prune_plan.actions] == ["prune"]
    apply_gc(
        prune_plan,
        repo_root=tmp_path,
        runs_dir=runs,
        catalog=catalog,
        clock=lambda: at_prune,
        append_event=events.append,
    )
    assert not archive.exists()
    assert stone.is_file()
    pruned = json.loads(stone.read_text())
    assert pruned["why"] == "prune_60d"
    assert catalog_path_exists(tmp_path, catalog, "00000000-0000-0000-0000-000000000010")
    entry = catalog.get("00000000-0000-0000-0000-000000000010")
    assert entry is not None
    assert (tmp_path / entry.evidence_path).is_file()
    assert events[-1].event == "gc_pruned"


def test_exclusions_evidence_digest_unprojected_tombstone(tmp_path: Path) -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    old = (now - PRUNE_AFTER - timedelta(days=1)).isoformat()
    runs = tmp_path / "runs"
    evidence_job = make_job(
        tmp_path / "research/evidence/runs",
        name="promoted-job",
        job_id="00000000-0000-0000-0000-000000000021",
        finished_at=old,
        spec_id="spec-promoted",
    )
    cited = make_job(
        runs,
        name="cited-job",
        job_id="00000000-0000-0000-0000-000000000022",
        finished_at=old,
        spec_id="spec-cited",
    )
    make_job(
        runs,
        name="unprojected-job",
        job_id="00000000-0000-0000-0000-000000000023",
        finished_at=old,
        spec_id="spec-unprojected",
    )
    eligible = make_job(
        runs,
        name="eligible-job",
        job_id="00000000-0000-0000-0000-000000000024",
        finished_at=old,
        spec_id="spec-eligible",
    )
    catalog = catalog_for(evidence_job, cited, eligible)
    (tmp_path / "digests").mkdir()
    (tmp_path / "digests/DISCOVERIES.md").write_text(
        "- Evidence: [runs/cited-job](../runs/cited-job/result.json)\n"
    )
    (tmp_path / "digests/2026-01-01.md").write_text("| cited-job | oracle |\n")

    plan = plan_gc(
        repo_root=tmp_path,
        runs_dir=runs,
        clock=lambda: now,
        catalog=catalog,
    )
    names = {item.job_name for item in plan.actions}
    skip_reasons = {skip.job_name: skip.reason for skip in plan.skipped}
    assert names == {"eligible-job"}
    assert "cited-job" in skip_reasons
    assert "digest" in skip_reasons["cited-job"] or "DISCOVERIES" in skip_reasons["cited-job"]
    assert "unprojected-job" in skip_reasons
    assert "ingested" in skip_reasons["unprojected-job"]
    assert "promoted-job" not in names
    assert not any(skip.job_name == "promoted-job" for skip in plan.skipped)

    apply_gc(plan, repo_root=tmp_path, runs_dir=runs, catalog=catalog, clock=lambda: now)
    stone = tombstone_path(runs, "00000000-0000-0000-0000-000000000024")
    assert stone.is_file()
    second = plan_gc(repo_root=tmp_path, runs_dir=runs, clock=lambda: now, catalog=catalog)
    assert all(item.path != stone for item in second.actions)
    assert (tmp_path / "research/evidence/runs/promoted-job/result.json").is_file()


def test_apply_appends_queue_events(tmp_path: Path) -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    runs = tmp_path / "runs"
    job = make_job(
        runs,
        name="event-job",
        job_id="00000000-0000-0000-0000-000000000030",
        finished_at=(now - COMPRESS_AFTER - timedelta(days=1)).isoformat(),
        spec_id="spec-event",
    )
    catalog = catalog_for(job)
    queue = DirectoryQueue(tmp_path / "queue")
    plan = plan_gc(repo_root=tmp_path, runs_dir=runs, clock=lambda: now, catalog=catalog)
    apply_gc(
        plan,
        repo_root=tmp_path,
        runs_dir=runs,
        catalog=catalog,
        clock=lambda: now,
        append_event=queue.append_event,
    )
    lines = (tmp_path / "queue/events.jsonl").read_text().splitlines()
    assert lines
    payload = json.loads(lines[-1])
    assert payload["event"] == "gc_compressed"
    assert payload["job_name"] == "event-job"
    assert payload["reason_code"] == "compress_14d"
    assert payload["spec_id"] == "spec-event"


def test_doctor_and_nightly_plan_do_not_apply(tmp_path: Path) -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    runs = tmp_path / "runs"
    job = make_job(
        runs,
        name="nightly-job",
        job_id="00000000-0000-0000-0000-000000000040",
        finished_at=(now - COMPRESS_AFTER - timedelta(days=1)).isoformat(),
        spec_id="spec-nightly",
    )
    catalog = catalog_for(job)
    line = doctor_disk_line(tmp_path, clock=lambda: now, catalog=catalog, runs_dir=runs)
    assert "compress-candidates=1" in line
    assert line.startswith("disk  ")
    plan = nightly_gc_plan(tmp_path, clock=lambda: now, catalog=catalog, runs_dir=runs)
    assert (job / "result.json").is_file()
    assert "WOULD" in format_plan(plan) or plan.actions[0].action == "compress"
    digest = tmp_path / "digests/today.md"
    digest.parent.mkdir(exist_ok=True)
    digest.write_text("# digest\n")
    from evallab.gc import append_gc_plan_to_digest

    append_gc_plan_to_digest(digest, plan)
    text = digest.read_text()
    assert "WOULD compress" in text
    assert "plan-only" in text
    assert (job / "result.json").is_file()


def test_apply_persists_catalog_so_fresh_load_resolves_existing_path(tmp_path: Path) -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    runs = tmp_path / "runs"
    job_id = "00000000-0000-0000-0000-000000000050"
    job = make_job(
        runs,
        name="ledger-job",
        job_id=job_id,
        finished_at=(now - COMPRESS_AFTER - timedelta(days=1)).isoformat(),
        spec_id="spec-ledger",
    )
    ledger = tmp_path / "derived" / "ingest-ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps(
            {
                "job_id": job_id,
                "evidence_path": "runs/ledger-job",
                "ingested": True,
                "projected": True,
            }
        )
        + "\n"
    )
    plan, applied = run_gc(
        tmp_path,
        apply=True,
        clock=lambda: now,
        runs_dir=runs,
    )
    assert applied is not None
    assert not job.exists()

    fresh = filesystem_catalog(tmp_path)
    entry = fresh.get(job_id)
    assert entry is not None
    assert catalog_path_exists(tmp_path, fresh, job_id)
    resolved = tmp_path / entry.evidence_path
    assert resolved.is_file()
    assert resolved.name == f"{job_id}.json"
    persisted = json.loads((tmp_path / "derived" / "gc-catalog.json").read_text())
    assert persisted["jobs"][job_id]["evidence_path"] == entry.evidence_path


def test_retarget_postgres_updates_job_and_trial_paths(monkeypatch) -> None:
    executed: list[tuple[str, tuple[str, str]]] = []

    class FakeConnection:
        def __enter__(self) -> FakeConnection:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def execute(self, sql: str, params: tuple[str, str]) -> None:
            executed.append((sql, params))

    monkeypatch.setattr("psycopg.connect", lambda url: FakeConnection())
    from evallab.gc import retarget_postgres

    retarget_postgres("postgresql://example", "job-1", "runs/.tombstones/job-1.json")
    statements = " ".join(sql for sql, _ in executed)
    assert "UPDATE jobs SET evidence_path" in statements
    assert "UPDATE trials SET evidence_path" in statements
    assert all(params[0] == "runs/.tombstones/job-1.json" for _, params in executed)
