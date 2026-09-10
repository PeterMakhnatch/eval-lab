"""Behavioral tests for coverage and reconciliation report (M029 / Data Engineer)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from evallab.coverage_report import build_coverage_report
from evallab.evidence.atif import project_jobs
from evallab.results import load_job

FIXTURES = (Path(__file__).parent / "fixtures" / "explorer" / "jobs").resolve()


def test_coverage_report_projected_passing_job(tmp_path: Path) -> None:
    """A projected passing job shows under projected with correct trajectory availability."""
    job = load_job(FIXTURES / "job-pass")
    derived_root = tmp_path / "derived"
    project_jobs([job], derived_root)

    report = build_coverage_report(
        root=tmp_path,
        derived_root=derived_root,
        database_url="postgresql://invalid:5432/none",
    )

    assert report.projected.count == 1
    assert "job-pass" in report.projected.jobs
    assert report.projected.truncated is False

    agent_stats = report.trajectory_availability_by_agent["codex"]
    assert agent_stats.trials == 1
    assert agent_stats.with_trajectory == 1
    assert agent_stats.with_usage == 1
    assert agent_stats.reason is None


def test_coverage_report_non_atif_agent_expected(tmp_path: Path) -> None:
    """Oracle and nop trials legitimately have no model trajectory: with_trajectory=False."""
    job = load_job(FIXTURES / "job-notraj")
    derived_root = tmp_path / "derived"
    project_jobs([job], derived_root)

    report = build_coverage_report(
        root=tmp_path,
        derived_root=derived_root,
        database_url="postgresql://invalid:5432/none",
    )

    assert report.projected.count == 1
    assert "job-notraj" in report.projected.jobs

    agent_stats = report.trajectory_availability_by_agent["oracle"]
    assert agent_stats.trials == 1
    assert agent_stats.with_trajectory is False
    assert agent_stats.with_usage is False
    assert agent_stats.reason == "non_atif_agent_expected"


def test_coverage_report_unfinished_and_excluded_jobs(tmp_path: Path) -> None:
    """An unfinished or excluded job appears under excepted with reason and repair path."""
    # 1. Unfinished job on disk (has started_at but no finished_at)
    runs_dir = tmp_path / "runs"
    unfinished_dir = runs_dir / "job-unfinished-1"
    unfinished_dir.mkdir(parents=True)
    (unfinished_dir / "result.json").write_text(
        json.dumps({"id": "j-unf", "started_at": "2026-09-01T00:00:00Z"})
    )

    # 2. Excluded job outside this checkout via catalog loader
    def mock_catalog(db_url: str | None) -> tuple[dict, dict]:
        jobs = {
            "j-unf": {
                "id": "j-unf",
                "name": "job-unfinished-1",
                "path": "runs/job-unfinished-1",
            },
            "j-ext": {
                "id": "j-ext",
                "name": "job-external-1",
                "path": "/private/tmp/other-workspace/runs/job-external-1",
            },
        }
        return jobs, {}

    report = build_coverage_report(
        root=tmp_path,
        derived_root=tmp_path / "derived",
        database_url="postgresql://invalid:5432/none",
        catalog_loader=mock_catalog,
    )

    assert "job-unfinished-1" in report.excepted.jobs
    assert "job-external-1" in report.excepted.jobs
    assert report.reasons.get("job_unfinished", 0) >= 1
    assert report.reasons.get("outside_checkout", 0) >= 1

    repairs = {entry.job_name: entry for entry in report.repair_path}
    assert "job-unfinished-1" in repairs
    assert repairs["job-unfinished-1"].reason == "job_unfinished"
    assert repairs["job-unfinished-1"].origin == "catalog"
    assert repairs["job-unfinished-1"].resumable_command == (
        "unfinishable-by-ingest: re-run the job to completion, "
        "then evallab ingest runs/job-unfinished-1; or abandon"
    )

    assert "job-external-1" in repairs
    assert repairs["job-external-1"].reason == "outside_checkout"
    assert repairs["job-external-1"].origin == "catalog"
    assert repairs["job-external-1"].resumable_command.startswith("unresolvable-from-this-checkout")
    assert "/private/tmp/other-workspace/runs/job-external-1" in repairs["job-external-1"].resumable_command


def test_coverage_report_unfinished_fixture_yields_unfinishable_marker(tmp_path: Path) -> None:
    """An unfinished fixture job yields the unfinishable marker, never an evallab ingest command."""
    # Copy a real fixture job to scratch runs directory, but remove finished_at from result.json
    fixture_job_dir = FIXTURES / "job-fail"
    runs_dir = tmp_path / "runs"
    unfinished_job_dir = runs_dir / "fixture-unfinished"
    runs_dir.mkdir(parents=True)
    shutil.copytree(fixture_job_dir, unfinished_job_dir)

    result_json_path = unfinished_job_dir / "result.json"
    result_data = json.loads(result_json_path.read_text())
    result_data.pop("finished_at", None)
    result_json_path.write_text(json.dumps(result_data))

    report = build_coverage_report(
        root=tmp_path,
        derived_root=tmp_path / "derived",
        database_url="postgresql://invalid:5432/none",
    )

    assert "fixture-unfinished" in report.excepted.jobs
    repairs = {entry.job_name: entry for entry in report.repair_path}
    assert "fixture-unfinished" in repairs

    entry = repairs["fixture-unfinished"]
    assert entry.reason == "job_unfinished"
    assert entry.origin == "disk-only"

    # Must yield the unfinishable marker, NEVER a bare evallab ingest command
    assert not entry.resumable_command.startswith("evallab ingest")
    assert entry.resumable_command == (
        "unfinishable-by-ingest: re-run the job to completion, "
        "then evallab ingest runs/fixture-unfinished; or abandon"
    )


def test_coverage_report_as_dict_json_roundtrip(tmp_path: Path) -> None:
    """CoverageReport.as_dict() produces clean JSON that round-trips identically."""
    job = load_job(FIXTURES / "job-pass")
    derived_root = tmp_path / "derived"
    project_jobs([job], derived_root)

    report = build_coverage_report(
        root=tmp_path,
        derived_root=derived_root,
        database_url="postgresql://invalid:5432/none",
    )

    data = report.as_dict()
    assert isinstance(data, dict)

    # Required top-level keys
    expected_keys = {
        "native_jobs_present",
        "catalogued",
        "projected",
        "excepted",
        "failed",
        "trajectory_availability_by_agent",
        "reasons",
        "repair_path",
    }
    assert set(data.keys()) == expected_keys

    # Each repair path entry includes origin
    assert all("origin" in entry for entry in data["repair_path"])

    # JSON round-trip
    serialized = json.dumps(data)
    deserialized = json.loads(serialized)
    assert deserialized == data


def _scoped_fixture(tmp_path: Path) -> tuple[str, Any]:
    """Project job-pass under tmp root; return (job_id, fake loader)."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True)
    shutil.copytree(FIXTURES / "job-pass", runs_dir / "job-a")
    job = load_job(runs_dir / "job-a")
    project_jobs([job], tmp_path / "derived")
    job_id = str(job.id)
    jobs = {
        job_id: {"id": job_id, "name": "job-a", "path": "runs/job-a"},
        "00000000-0000-0000-0000-000000000000": {
            "id": "00000000-0000-0000-0000-000000000000",
            "name": "job-b",
            "path": "runs/job-b",
        },
    }

    def fake_loader(db_url: str | None) -> tuple[dict, dict]:
        return dict(jobs), {}

    return job_id, fake_loader


def test_scoped_loader_keeps_exactly_selected_ids() -> None:
    """A scoped loader exposes exactly the selected jobs and their trials."""
    from evallab.coverage_report import scoped_catalog_loader

    def fake_loader(db_url: str | None) -> tuple[dict, dict]:
        return (
            {
                "id-1": {"id": "id-1", "name": "a", "path": "runs/a"},
                "id-2": {"id": "id-2", "name": "b", "path": "runs/b"},
            },
            {
                "t-1": {"id": "t-1", "job_id": "id-1", "name": "t1"},
                "t-2": {"id": "t-2", "job_id": "id-2", "name": "t2"},
            },
        )

    jobs, trials = scoped_catalog_loader(fake_loader, {"id-1"})("unused-url")
    assert set(jobs) == {"id-1"}
    assert set(trials) == {"t-1"}


def test_scoped_loader_preserves_agent_fields() -> None:
    """Enrichment must not clobber agent/usage fields a base loader provides."""
    from evallab.coverage_report import scoped_catalog_loader

    def fake_loader(db_url: str | None) -> tuple[dict, dict]:
        return (
            {"id-1": {"id": "id-1", "name": "a", "path": "runs/a"}},
            {
                "t-1": {
                    "id": "t-1",
                    "job_id": "id-1",
                    "name": "t1",
                    "agent_name": "codex",
                    "input_tokens": 10,
                    "output_tokens": 5,
                }
            },
        )

    # Invalid DB: enrichment query fails silently, base fields must survive.
    _, trials = scoped_catalog_loader(fake_loader, {"id-1"})(
        "postgresql://invalid:5432/none"
    )
    assert trials["t-1"]["agent_name"] == "codex"
    assert trials["t-1"]["input_tokens"] == 10


def test_scope_bound_product_is_deterministic(tmp_path: Path) -> None:
    """Same inputs rebuild byte-identical product whose name matches its sha."""
    import hashlib

    from evallab.coverage_report import write_scope_bound_product

    job_id, fake_loader = _scoped_fixture(tmp_path)
    links = [{"kind": "post-training-qualification", "path": "/x/manifest.json"}]
    kwargs: dict[str, Any] = {
        "root": tmp_path,
        "derived_root": tmp_path / "derived",
        "database_url": "postgresql://invalid:5432/none",
        "job_ids": {job_id},
        "external_links": links,
        "wrong_root": tmp_path / "wrong",
        "catalog_loader": fake_loader,
    }
    first = write_scope_bound_product(out_dir=tmp_path / "out", **kwargs)
    second = write_scope_bound_product(out_dir=tmp_path / "out2", **kwargs)
    assert first.read_bytes() == second.read_bytes()
    payload = json.loads(first.read_text())
    digest = hashlib.sha256(
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    ).hexdigest()
    assert first.name == f"coverage-scope-{digest[:12]}.json"
    assert payload["external_links"] == links
    assert payload["job_ids"] == [job_id]


def test_scope_bound_product_wrong_root_excludes(tmp_path: Path) -> None:
    """Same job set under a wrong root lands excluded with reasons preserved."""
    from evallab.coverage_report import write_scope_bound_product

    job_id, fake_loader = _scoped_fixture(tmp_path)
    path = write_scope_bound_product(
        root=tmp_path,
        derived_root=tmp_path / "derived",
        database_url="postgresql://invalid:5432/none",
        job_ids={
            job_id,
            "00000000-0000-0000-0000-000000000000",
        },
        out_dir=tmp_path / "out",
        wrong_root=tmp_path / "wrong",
        catalog_loader=fake_loader,
    )
    payload = json.loads(path.read_text())
    proof = payload["binding_proof"]
    assert proof["excluded"] == 2
    assert proof["reasons"].get("evidence_absent", 0) >= 1
    # The bound product itself still accounts for the missing job explicitly:
    # selected IDs listed, job-b excepted with its reason and repair entry.
    assert "00000000-0000-0000-0000-000000000000" in payload["job_ids"]
    assert "job-b" in payload["coverage"]["excepted"]["jobs"]
    repairs = {e["job_name"]: e for e in payload["coverage"]["repair_path"]}
    assert repairs["job-b"]["reason"] == "evidence_absent"


def _solo_finished_job(tmp_path: Path, job_id: str = "aaaaaaaa-0000-4000-8000-000000000001") -> str:
    """A finished on-disk job with no catalog row and no partition (the R2 shape)."""
    job_dir = tmp_path / "runs" / "job-solo"
    job_dir.mkdir(parents=True)
    (job_dir / "result.json").write_text(
        json.dumps(
            {
                "id": job_id,
                "started_at": "2026-09-08T00:00:00Z",
                "finished_at": "2026-09-08T00:01:00Z",
                "n_total_trials": 1,
                "stats": {"n_completed_trials": 1},
            }
        )
    )
    return job_id


def test_selected_finished_uncataloged_job_is_not_cataloged(tmp_path: Path) -> None:
    """A finished disk job with no catalog row reports not_cataloged, never silently absent."""
    from evallab.coverage_report import build_coverage_report

    job_id = _solo_finished_job(tmp_path)

    def fake_loader(db_url: str | None) -> tuple[dict, dict]:
        return {}, {}

    report = build_coverage_report(
        root=tmp_path,
        derived_root=tmp_path / "derived",
        database_url="postgresql://invalid:5432/none",
        catalog_loader=fake_loader,
        selected_job_ids={job_id},
    )
    assert report.catalogued.count == 0
    assert report.projected.count == 0
    assert report.reasons.get("not_cataloged", 0) == 1
    repairs = {entry.job_name: entry for entry in report.repair_path}
    assert repairs["job-solo"].reason == "not_cataloged"
    assert repairs["job-solo"].resumable_command == "evallab ingest runs/job-solo"
    assert "job-solo" in report.excepted.jobs


def test_selected_scope_never_lists_unselected_partitions(tmp_path: Path) -> None:
    """Regression: a singleton scope must not list all 175 lake partitions as projected."""
    import shutil

    from evallab.coverage_report import build_coverage_report

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True)
    shutil.copytree(FIXTURES / "job-pass", runs_dir / "job-a")
    job = load_job(runs_dir / "job-a")
    project_jobs([job], tmp_path / "derived")
    solo_id = _solo_finished_job(tmp_path)

    def fake_loader(db_url: str | None) -> tuple[dict, dict]:
        return {}, {}

    report = build_coverage_report(
        root=tmp_path,
        derived_root=tmp_path / "derived",
        database_url="postgresql://invalid:5432/none",
        catalog_loader=fake_loader,
        selected_job_ids={solo_id},
    )
    # job-a is projected on disk but NOT selected: it must not appear anywhere.
    assert report.projected.count == 0
    assert "job-a" not in report.native_jobs_present.jobs
    assert report.reasons.get("not_cataloged", 0) == 1


def test_section_counts_agree_with_listed_names(tmp_path: Path) -> None:
    """Regression: count=78 with 20 names and truncated=false must be impossible.

    Every section's count and names describe the same population: an
    untruncated section lists exactly count names; a truncated one lists
    exactly LIST_CAP.
    """
    from evallab.coverage_report import LIST_CAP, build_coverage_report

    _, fake_loader = _scoped_fixture(tmp_path)
    report = build_coverage_report(
        root=tmp_path,
        derived_root=tmp_path / "derived",
        database_url="postgresql://invalid:5432/none",
        catalog_loader=fake_loader,
    )
    for section in (
        report.native_jobs_present,
        report.catalogued,
        report.projected,
        report.excepted,
        report.failed,
    ):
        if section.truncated:
            assert len(section.jobs) == LIST_CAP
            assert section.count > len(section.jobs)
        else:
            assert section.count == len(section.jobs)


def test_catalogued_lists_only_retained_scope(tmp_path: Path) -> None:
    """Scoped-out jobs never inflate the catalogued section: count and names agree."""
    from evallab.coverage_report import build_coverage_report

    _, fake_loader = _scoped_fixture(tmp_path)
    report = build_coverage_report(
        root=tmp_path,
        derived_root=tmp_path / "derived",
        database_url="postgresql://invalid:5432/none",
        catalog_loader=fake_loader,
    )
    # job-b has no evidence and no parquet: excluded with a reason, while the
    # catalogued section describes exactly the retained set.
    assert report.catalogued.count == len(report.catalogued.jobs)
    assert "job-b" not in report.catalogued.jobs
    assert "job-b" in report.excepted.jobs
