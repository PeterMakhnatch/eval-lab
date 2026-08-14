from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from evallab.atif import export_trajectories
from evallab.database import ingest, ingest_job
from evallab.facts import export_facts

ROOT = Path(__file__).resolve().parents[1]
HARNESS_PATH = ROOT / "scripts/profile/harness.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("speed_profile_harness", HARNESS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_harness_binds_shipped_ingest_projection_and_facts() -> None:
    harness = _load_harness()
    source = HARNESS_PATH.read_text()
    assert "from evallab.database import ingest, ingest_job, initialize" in source
    assert "from evallab.atif import export_trajectories" in source
    assert "from evallab.facts import export_facts" in source
    assert harness.ingest is ingest
    assert harness.ingest_job is ingest_job
    assert harness.export_trajectories is export_trajectories
    assert harness.export_facts is export_facts


def test_cpu_only_profile_names_six_paths_and_never_uses_shared_catalog(
    tmp_path: Path,
) -> None:
    harness = _load_harness()
    report = harness.run_profile(
        corpus_roots=[ROOT / "research/evidence/runs"],
        warmup=1,
        reps=5,
        database_url=None,
        admin_url="postgresql://unused.example/evallab",
        cpu_only=True,
        inject_ms={},
        work_dir=tmp_path,
        tick_n=5,
        fleet_fn=lambda: None,
    )
    assert report.path_names() == list(harness.PATH_NAMES)
    assert report.harbor_dispatch == "stubbed"
    assert report.database_url_kind == "cpu-only-recording"
    assert report.corpus_jobs >= 2
    assert report.warmup == 1
    assert report.reps == 5
    for item in report.paths:
        assert item.median_ms >= 0
        assert item.min_ms <= item.median_ms <= item.max_ms
        assert item.reps == 5
    markdown = harness.render_markdown(report)
    for name in harness.PATH_NAMES:
        assert f"| {name} |" in markdown


def test_refuses_shared_catalog_url() -> None:
    harness = _load_harness()
    try:
        harness.assert_not_shared_catalog(
            "postgresql://evallab:local-development-only@127.0.0.1:54329/evallab"
        )
    except ValueError as exc:
        assert "shared evallab catalog" in str(exc)
    else:
        raise AssertionError("shared catalog URL was accepted")


def test_injected_slowdown_raises_named_path_median(tmp_path: Path) -> None:
    harness = _load_harness()
    baseline = harness.run_profile(
        corpus_roots=[ROOT / "research/evidence/runs"],
        warmup=1,
        reps=5,
        database_url=None,
        admin_url="postgresql://unused.example/evallab",
        cpu_only=True,
        inject_ms={},
        work_dir=tmp_path / "base",
        tick_n=5,
        fleet_fn=lambda: None,
    )
    slowed = harness.run_profile(
        corpus_roots=[ROOT / "research/evidence/runs"],
        warmup=1,
        reps=5,
        database_url=None,
        admin_url="postgresql://unused.example/evallab",
        cpu_only=True,
        inject_ms={"digest": 80.0},
        work_dir=tmp_path / "slow",
        tick_n=5,
        fleet_fn=lambda: None,
    )
    base = next(item.median_ms for item in baseline.paths if item.path == "digest")
    slow = next(item.median_ms for item in slowed.paths if item.path == "digest")
    assert slow >= base + 40.0


def test_check_budgets_fails_when_ceiling_exceeded(tmp_path: Path) -> None:
    harness = _load_harness()
    spec = importlib.util.spec_from_file_location(
        "speed_check_budgets",
        ROOT / "scripts/profile/check_budgets.py",
    )
    assert spec is not None and spec.loader is not None
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)

    report = harness.run_profile(
        corpus_roots=[ROOT / "research/evidence/runs"],
        warmup=1,
        reps=5,
        database_url=None,
        admin_url="postgresql://unused.example/evallab",
        cpu_only=True,
        inject_ms={"facts": 120.0},
        work_dir=tmp_path / "report",
        tick_n=5,
        fleet_fn=lambda: None,
    )
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(harness.report_to_json(report)))
    tight = tmp_path / "budgets.json"
    tight.write_text(
        json.dumps(
            {
                "tolerance_pct": 10,
                "paths": {
                    "ingest": 10_000,
                    "projection": 10_000,
                    "facts": 1,
                    "digest": 10_000,
                    "queue-tick-100": 10_000,
                    "fleet-status": 10_000,
                },
            }
        )
    )
    assert checker.main([str(report_path), "--budgets", str(tight)]) == 1
    loose = tmp_path / "loose.json"
    loose.write_text(
        json.dumps(
            {
                "tolerance_pct": 50,
                "paths": {
                    "ingest": 10_000,
                    "projection": 10_000,
                    "facts": 10_000,
                    "digest": 10_000,
                    "queue-tick-100": 10_000,
                    "fleet-status": 10_000,
                },
            }
        )
    )
    assert checker.main([str(report_path), "--budgets", str(loose)]) == 0


def test_fleet_status_script_runs_with_gh_stub() -> None:
    harness = _load_harness()
    harness._time_fleet_status({})
