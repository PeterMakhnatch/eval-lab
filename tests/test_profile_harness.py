from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

from evallab.atif import export_trajectories, ingest_and_project
from evallab.database import ingest, ingest_job
from evallab.facts import export_facts
from evallab.results import discover_job_dirs

ROOT = Path(__file__).resolve().parents[1]
HARNESS_PATH = ROOT / "scripts/profile/harness.py"
BUDGETS_PATH = ROOT / "scripts/profile/budgets.json"


def _load_harness():
    spec = importlib.util.spec_from_file_location("speed_profile_harness", HARNESS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "speed_check_budgets",
        ROOT / "scripts/profile/check_budgets.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pinned_roots(harness) -> list[Path]:
    return harness.resolve_corpus_roots(list(harness.DEFAULT_CORPUS))


# A per-path budget no measurement can reach, so tests that assert the checker's
# *pass* path never depend on how fast the host happened to be. At 10_000 ms the
# 50% ceiling was 15 s against a locally measured fleet-status median of 2994 ms
# -- only 5x headroom on a path that spawns git and gh subprocesses. Tests that
# need a budget to be *exceeded* inject a delay and set that one path low.
UNREACHABLE_BUDGET_MS = 1_000_000


def _budgets_for(report: dict, *, tolerance_pct: float, paths: dict) -> dict:
    """A synthetic budgets payload whose corpus block matches the given report."""
    return {
        "tolerance_pct": tolerance_pct,
        "corpus": {
            "roots": list(report["corpus_roots"]),
            "jobs": report["corpus_jobs"],
            "result_json": report["corpus_result_json"],
        },
        "paths": paths,
    }


def test_harness_binds_shipped_ingest_projection_and_facts() -> None:
    harness = _load_harness()
    source = HARNESS_PATH.read_text()
    assert "from evallab.database import ingest, ingest_job, initialize" in source
    assert "from evallab.atif import export_trajectories, ingest_and_project" in source
    assert "from evallab.facts import export_facts" in source
    assert harness.ingest is ingest
    assert harness.ingest_job is ingest_job
    assert harness.export_trajectories is export_trajectories
    assert harness.ingest_and_project is ingest_and_project
    assert harness.export_facts is export_facts


def test_cpu_only_profile_names_six_paths_and_never_uses_shared_catalog(
    tmp_path: Path,
) -> None:
    harness = _load_harness()
    report = harness.run_profile(
        corpus_roots=_pinned_roots(harness),
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
    assert report.corpus_jobs == len(harness.DEFAULT_CORPUS)
    assert report.corpus_roots == list(harness.DEFAULT_CORPUS)
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


def test_inject_delay_sleeps_only_the_named_path_for_its_configured_amount() -> None:
    """The injection contract, asserted without measuring the wall clock.

    `agents/CHECKS.md` forbids a test that depends on host timing. The previous
    version of this test compared two measured medians and failed on CI as
    `assert 82.315 >= 94.207 + 40.0`: the *baseline* absorbed ~92 ms of runner
    noise while the run carrying a deliberate 80 ms injection measured 82.3 ms,
    only 2.3 ms above the floor the sleep guarantees. Because the baseline
    exceeded the injected run outright, no additive or relative margin could
    have held -- not even a zero margin. So the clock is gone from the
    assertion: the seam reports which path was delayed, and by how much.
    """
    harness = _load_harness()
    slept: list[float] = []

    harness._inject_delay({"digest": 80.0}, "digest", slept.append)
    assert slept == [0.080], "the named path must sleep its configured milliseconds"

    slept.clear()
    harness._inject_delay({"digest": 80.0}, "facts", slept.append)
    assert slept == [], "a path with no injection configured must not sleep"

    for ignored in (0.0, -5.0):
        slept.clear()
        harness._inject_delay({"digest": ignored}, "digest", slept.append)
        assert slept == [], f"an injection of {ignored} ms must not sleep"


def test_injection_reaches_each_named_path_in_measurement_order(tmp_path: Path) -> None:
    """Every timed path must request its own injection, proven through the seam.

    The two amounts differ, so the recorded sequence proves routing rather than
    merely counting: `facts` is measured before `digest` (`PATH_NAMES` order),
    and each path is exercised `warmup + reps` times. A path whose injection
    was dropped, or misrouted to another path, changes this sequence.
    """
    harness = _load_harness()
    slept: list[float] = []
    report = harness.run_profile(
        corpus_roots=_pinned_roots(harness),
        warmup=1,
        reps=5,
        database_url=None,
        admin_url="postgresql://unused.example/evallab",
        cpu_only=True,
        inject_ms={"facts": 30.0, "digest": 80.0},
        work_dir=tmp_path,
        tick_n=5,
        fleet_fn=lambda: None,
        sleeper=slept.append,
    )
    assert report.path_names() == list(harness.PATH_NAMES)
    assert slept == [0.030] * 6 + [0.080] * 6


def test_check_budgets_fails_when_ceiling_exceeded(tmp_path: Path) -> None:
    harness = _load_harness()
    checker = _load_checker()

    report = harness.run_profile(
        corpus_roots=_pinned_roots(harness),
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
    payload = harness.report_to_json(report)
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(payload))
    generous = dict.fromkeys(checker.REQUIRED_PATHS, UNREACHABLE_BUDGET_MS)
    tight = tmp_path / "budgets.json"
    tight.write_text(
        json.dumps(_budgets_for(payload, tolerance_pct=10, paths={**generous, "facts": 1}))
    )
    assert checker.main([str(report_path), "--budgets", str(tight)]) == 1
    loose = tmp_path / "loose.json"
    loose.write_text(
        json.dumps(_budgets_for(payload, tolerance_pct=50, paths=dict(generous)))
    )
    assert checker.main([str(report_path), "--budgets", str(loose)]) == 0


def test_fleet_status_script_runs_with_gh_stub() -> None:
    harness = _load_harness()
    harness._time_fleet_status(lambda _name: None)


def test_default_corpus_is_pinned_to_job_directories_not_the_evidence_directory() -> None:
    """The perf gate must measure code speed, never the size of committed evidence.

    Three of the six profiled paths -- ingest, projection and facts -- take the
    loaded job list, so profiling the *directory* `research/evidence/runs` made
    every evidence promotion move the gate. The default must name job
    directories, which discovery returns verbatim and cannot expand.
    """
    harness = _load_harness()
    assert "research/evidence/runs" not in harness.DEFAULT_CORPUS
    assert harness.DEFAULT_CORPUS, "the default corpus must not be empty"

    roots = _pinned_roots(harness)
    for root in roots:
        assert (root / "result.json").is_file(), (
            f"{root} is not a Harbor job directory; a pinned entry must be a job "
            "directory, otherwise discovery sweeps up whatever is committed beside it"
        )
    assert discover_job_dirs(roots) == sorted(root.resolve() for root in roots)


def test_promoting_a_job_directory_does_not_change_a_pinned_corpus(tmp_path: Path) -> None:
    """Naming job directories, not their container, is what makes the pin hold.

    Built in a scratch tree so the test never writes to `research/evidence/`.
    """
    harness = _load_harness()
    pinned = _pinned_roots(harness)
    container = tmp_path / "runs"
    container.mkdir()
    for root in pinned:
        shutil.copytree(root, container / root.name)
    pinned_copies = [container / root.name for root in pinned]

    assert discover_job_dirs([container]) == discover_job_dirs(pinned_copies)

    shutil.copytree(pinned[0], container / "zz-newly-promoted-evidence")

    assert (container / "zz-newly-promoted-evidence") in discover_job_dirs([container])
    assert discover_job_dirs(pinned_copies) == sorted(
        path.resolve() for path in pinned_copies
    )


def test_committed_budgets_declare_the_pinned_corpus_shape() -> None:
    """budgets.json and the harness default must not drift apart."""
    harness = _load_harness()
    checker = _load_checker()
    budgets = checker.load_budgets(BUDGETS_PATH)

    assert budgets["corpus"]["roots"] == list(harness.DEFAULT_CORPUS)

    roots = _pinned_roots(harness)
    job_dirs, result_json, _ = harness.corpus_stats(roots)
    assert budgets["corpus"]["jobs"] == len(job_dirs)
    assert budgets["corpus"]["result_json"] == result_json


def test_check_budgets_rejects_a_report_measured_on_another_corpus(tmp_path: Path) -> None:
    harness = _load_harness()
    checker = _load_checker()
    report = harness.run_profile(
        corpus_roots=_pinned_roots(harness),
        warmup=1,
        reps=5,
        database_url=None,
        admin_url="postgresql://unused.example/evallab",
        cpu_only=True,
        inject_ms={},
        work_dir=tmp_path / "report",
        tick_n=5,
        fleet_fn=lambda: None,
    )
    payload = harness.report_to_json(report)
    generous = dict.fromkeys(checker.REQUIRED_PATHS, UNREACHABLE_BUDGET_MS)
    budgets = _budgets_for(payload, tolerance_pct=50, paths=dict(generous))
    assert checker.assert_corpus_shape(payload, budgets) == []

    grown = dict(payload)
    grown["corpus_roots"] = ["research/evidence/runs"]
    grown["corpus_jobs"] = payload["corpus_jobs"] + 3
    grown["corpus_result_json"] = payload["corpus_result_json"] + 6
    problems = checker.assert_corpus_shape(grown, budgets)
    assert len(problems) == 1
    assert "re-baseline sample" in problems[0]

    report_path = tmp_path / "grown.json"
    report_path.write_text(json.dumps(grown))
    budgets_path = tmp_path / "budgets.json"
    budgets_path.write_text(json.dumps(budgets))
    assert checker.main([str(report_path), "--budgets", str(budgets_path)]) == 1


def test_budgets_without_a_corpus_block_are_rejected(tmp_path: Path) -> None:
    checker = _load_checker()
    stale = tmp_path / "stale.json"
    stale.write_text(
        json.dumps(
            {
                "tolerance_pct": 50,
                "paths": dict.fromkeys(checker.REQUIRED_PATHS, UNREACHABLE_BUDGET_MS),
            }
        )
    )
    try:
        checker.load_budgets(stale)
    except ValueError as exc:
        assert "corpus" in str(exc)
    else:
        raise AssertionError("budgets without a corpus block were accepted")


def test_a_vanished_pinned_corpus_entry_fails_loudly() -> None:
    harness = _load_harness()
    try:
        harness.resolve_corpus_roots(["research/evidence/runs/does-not-exist"])
    except ValueError as exc:
        assert "pinned corpus" in str(exc)
    else:
        raise AssertionError("a missing pinned corpus entry was silently ignored")
