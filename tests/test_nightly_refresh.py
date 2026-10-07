"""HAR-199: local nightly refresh orchestration, without paid or network work."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from evallab import nightly_refresh
from evallab.nightly_refresh import DIGEST_SCHEMA, SCHEMA, RefreshConfig, run_refresh

# 2026-10-07 06:30 UTC is 02:30 ET, before the 07:00 next-report boundary.
NOW = datetime(2026, 10, 7, 6, 30, tzinfo=UTC)
REPORT_DAY = "2026-10-07"
# Earlier than any high-water mark a fresh fixture file can record.
OLD_NS = 1_000_000_000_000_000_000


def _stable_key(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:20]


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _completed_job(parent: Path, name: str, *, trials: int = 0) -> Path:
    job = parent / name
    job.mkdir(parents=True)
    (job / "result.json").write_text(
        json.dumps(
            {
                "id": name,
                "n_total_trials": trials,
                "stats": {"n_completed_trials": trials, "n_errored_trials": 0},
                "finished_at": "2026-10-06T12:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return job


def _checkout(tmp: Path, name: str = "checkout") -> Path:
    root = tmp.resolve() / name
    (root / "queue").mkdir(parents=True)
    (root / "runs").mkdir()
    (root / "jobs").mkdir()
    return root


def _config(tmp: Path, *checkouts: Path, facts_name: str = "facts") -> RefreshConfig:
    base = tmp.resolve()
    repo = base / "repo"
    (repo / "src").mkdir(parents=True)
    data = base / "data"
    data.mkdir()
    readers = base / "readers"
    readers.mkdir()
    facts = base / facts_name
    facts.mkdir()
    return RefreshConfig(
        repo_root=repo,
        state_dir=base / "state",
        queue_roots=checkouts,
        facts_root=facts,
        readers_store=readers,
        data_root=data,
    )


def _campaigns(roots: tuple[Path, ...]) -> dict[str, Any]:
    root = roots[0]
    return {
        f"demo@{root.name}": {
            "queue_root": str(root),
            "reserved": 1.5,
            "settled": 0.25,
            "total": 1.75,
            "sources": {
                "modal_gpu": 0.0,
                "daytona": 0.0,
                "model_api": 0.25,
                "unmeasured_reserved": 1.5,
            },
        }
    }


def _tree(root: Path) -> dict[str, object]:
    found: dict[str, object] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            found[relative] = ("symlink", str(path.readlink()))
        elif path.is_dir():
            found[relative] = ("dir",)
        elif path.is_file():
            found[relative] = ("file", path.stat().st_mtime_ns, path.read_bytes())
    return found


def _backdate(path: Path) -> None:
    os.utime(path, ns=(OLD_NS, OLD_NS))


@dataclass
class Seams:
    process: list[dict[str, Any]] = field(default_factory=list)
    trials: list[list[Path]] = field(default_factory=list)
    verdicts: list[RefreshConfig] = field(default_factory=list)
    replay: list[list[Path]] = field(default_factory=list)
    pages: list[list[Path]] = field(default_factory=list)
    spend: list[tuple[Path, ...]] = field(default_factory=list)
    fail_process: bool = False
    fail_pages: bool = False

    def install(self, monkeypatch: pytest.MonkeyPatch, *, process: bool = True) -> None:
        if process:
            monkeypatch.setattr(nightly_refresh, "process_job", self._process)
        monkeypatch.setattr(nightly_refresh, "refresh_trials", self._trials)
        monkeypatch.setattr(nightly_refresh, "refresh_verdicts", self._verdicts)
        monkeypatch.setattr(nightly_refresh, "replay_history", self._replay)
        monkeypatch.setattr(nightly_refresh, "refresh_pages", self._pages)
        monkeypatch.setattr(nightly_refresh, "campaign_spend", self._spend)

    def clear(self) -> None:
        self.process.clear()
        self.trials.clear()
        self.verdicts.clear()
        self.replay.clear()
        self.pages.clear()
        self.spend.clear()

    def _process(self, job: Path, **kwargs: Any) -> dict[str, Any]:
        self.process.append({"job": Path(job), **kwargs})
        if self.fail_process:
            raise RuntimeError("processing failed")
        return {"schema": "test"}

    def _trials(self, config: RefreshConfig, roots: list[Path]) -> dict[str, Any]:
        self.trials.append(list(roots))
        return {"total": 4, "legit": 3, "projection_errors": 0}

    def _verdicts(self, config: RefreshConfig) -> dict[str, Any]:
        self.verdicts.append(config)
        return {"task-a": {"verdict": "keep", "evidence": "fixture"}}

    def _replay(self, config: RefreshConfig, changed_jobs: list[Path]) -> dict[str, Any]:
        self.replay.append(list(changed_jobs))
        return {"history_mining": 1, "jobs": len(changed_jobs)}

    def _pages(self, config: RefreshConfig, jobs: list[Path]) -> dict[str, Any]:
        self.pages.append(list(jobs))
        if self.fail_pages:
            raise RuntimeError("pages failed")
        return {"synced": len(jobs), "failed": []}

    def _spend(self, config: RefreshConfig, roots: tuple[Path, ...]) -> dict[str, Any]:
        self.spend.append(tuple(roots))
        return _campaigns(roots)


def _refresh(config: RefreshConfig, seams: Seams) -> dict[str, Any]:
    seams.clear()
    return run_refresh(config, now=NOW)


def test_first_refresh_processes_new_completed_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _checkout(tmp_path)
    job = _completed_job(checkout / "runs", "harbor-job")
    config = _config(tmp_path, checkout)
    seams = Seams()
    seams.install(monkeypatch)

    result = run_refresh(config, now=NOW)

    assert result["status"] == "refreshed"
    assert result["new_runs"] == 1
    assert result["changed_runs"] == 1
    assert len(seams.process) == 1
    call = seams.process[0]
    assert call["job"].resolve() == job.resolve()
    assert call["ingest"] is False
    assert call["publish"] is False
    assert call["root"] == config.repo_root
    assert call["output_dir"] == config.state_dir / "processed" / _stable_key(job)
    assert call["parquet_root"] == config.state_dir / "parquet"
    assert seams.trials == [[checkout / "runs", checkout / "jobs"]]
    assert seams.replay == [[job.resolve()]]
    assert seams.pages == [[job.resolve()]]
    assert seams.spend == [(checkout.resolve(),)]
    state = _load(config.state_dir / "state.json")
    assert state["schema"] == SCHEMA
    assert str(job.resolve()) in state["selection"]["jobs"]
    assert state["high_water_mark"] == (job / "result.json").stat().st_mtime_ns
    assert state["high_water_mark"] == result["high_water_mark"]
    assert state["verdicts"]["task-a"]["verdict"] == "keep"
    digest = _load(config.facts_root / "inputs" / "evallab-nightly.json")
    assert digest["schema"] == DIGEST_SCHEMA
    assert digest["generated_at"] == NOW.isoformat()
    assert digest["campaigns"] == _campaigns((checkout.resolve(),))
    assert len(digest["lines"]) == 10
    assert digest["lines"][0] == "## Eval Lab nightly"
    assert "refresh spend $0" in digest["lines"][1]
    assert "3 / 4" in "\n".join(digest["lines"])


def test_second_refresh_is_noop_and_preserves_mtimes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _checkout(tmp_path)
    _completed_job(checkout / "runs", "harbor-job")
    config = _config(tmp_path, checkout)
    seams = Seams()
    seams.install(monkeypatch)
    first = run_refresh(config, now=NOW)
    watched = [
        config.state_dir / "state.json",
        config.facts_root / "inputs" / "evallab-nightly.json",
        config.facts_root / "runs" / REPORT_DAY / "facts.json",
        config.facts_root / "runs" / REPORT_DAY / "facts.md",
    ]
    before = {path: (path.stat().st_mtime_ns, path.read_bytes()) for path in watched}

    second = _refresh(config, seams)

    assert second == {
        "status": "noop",
        "jobs": 1,
        "high_water_mark": first["high_water_mark"],
    }
    assert seams.process == []
    assert seams.trials == []
    assert seams.verdicts == []
    assert seams.replay == []
    assert seams.pages == []
    assert seams.spend == []
    after = {path: (path.stat().st_mtime_ns, path.read_bytes()) for path in watched}
    assert after == before


def test_changed_result_is_processed_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = _checkout(tmp_path)
    job = _completed_job(checkout / "runs", "harbor-job")
    config = _config(tmp_path, checkout)
    seams = Seams()
    seams.install(monkeypatch)
    run_refresh(config, now=NOW)
    before = _load(config.state_dir / "state.json")["selection"]["jobs"][str(job.resolve())]
    payload = _load(job / "result.json")
    payload["finished_at"] = "2026-10-06T18:00:00Z"
    (job / "result.json").write_text(json.dumps(payload) + "\n", encoding="utf-8")

    result = _refresh(config, seams)

    assert result["status"] == "refreshed"
    assert result["new_runs"] == 0
    assert result["changed_runs"] == 1
    assert [call["job"].resolve() for call in seams.process] == [job.resolve()]
    assert seams.process[0]["output_dir"] == config.state_dir / "processed" / _stable_key(job)
    assert seams.process[0]["ingest"] is False
    assert seams.process[0]["publish"] is False
    assert seams.replay == [[job.resolve()]]
    after = _load(config.state_dir / "state.json")["selection"]["jobs"][str(job.resolve())]
    assert after != before


def test_failed_processing_does_not_advance_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _checkout(tmp_path)
    job = _completed_job(checkout / "runs", "harbor-job")
    config = _config(tmp_path, checkout)
    seams = Seams()
    seams.fail_process = True
    seams.install(monkeypatch)
    state_path = config.state_dir / "state.json"
    facts_path = config.facts_root / "inputs" / "evallab-nightly.json"

    with pytest.raises(RuntimeError, match="processing failed"):
        run_refresh(config, now=NOW)

    assert not state_path.exists()
    assert not facts_path.exists()
    assert seams.trials == []

    seams.fail_process = False
    first = run_refresh(config, now=NOW)
    committed = state_path.read_bytes()
    facts_stamp = (facts_path.stat().st_mtime_ns, facts_path.read_bytes())
    payload = _load(job / "result.json")
    payload["finished_at"] = "2026-10-06T19:00:00Z"
    (job / "result.json").write_text(json.dumps(payload) + "\n", encoding="utf-8")
    seams.fail_process = True

    with pytest.raises(RuntimeError, match="processing failed"):
        run_refresh(config, now=NOW)

    assert state_path.read_bytes() == committed
    assert _load(state_path)["high_water_mark"] == first["high_water_mark"]
    assert (facts_path.stat().st_mtime_ns, facts_path.read_bytes()) == facts_stamp

    seams.fail_process = False
    recovered = _refresh(config, seams)
    assert recovered["status"] == "refreshed"
    assert [call["job"].resolve() for call in seams.process] == [job.resolve()]
    assert _load(state_path)["high_water_mark"] >= first["high_water_mark"]


def test_failed_helper_does_not_advance_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _checkout(tmp_path)
    job = _completed_job(checkout / "runs", "harbor-job")
    config = _config(tmp_path, checkout)
    seams = Seams()
    seams.install(monkeypatch)
    run_refresh(config, now=NOW)
    state_path = config.state_dir / "state.json"
    facts_path = config.facts_root / "inputs" / "evallab-nightly.json"
    committed = state_path.read_bytes()
    facts_stamp = (facts_path.stat().st_mtime_ns, facts_path.read_bytes())
    payload = _load(job / "result.json")
    payload["stats"] = {"n_completed_trials": 0, "n_errored_trials": 1}
    (job / "result.json").write_text(json.dumps(payload) + "\n", encoding="utf-8")
    seams.fail_pages = True

    with pytest.raises(RuntimeError, match="pages failed"):
        run_refresh(config, now=NOW)

    assert state_path.read_bytes() == committed
    assert (facts_path.stat().st_mtime_ns, facts_path.read_bytes()) == facts_stamp
    seams.fail_pages = False
    recovered = _refresh(config, seams)
    assert recovered["status"] == "refreshed"
    assert seams.process[0]["job"].resolve() == job.resolve()


def test_competing_flock_returns_locked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = _checkout(tmp_path)
    _completed_job(checkout / "runs", "harbor-job")
    config = _config(tmp_path, checkout)
    seams = Seams()
    seams.install(monkeypatch)
    lock = config.state_dir / "refresh.lock"
    script = (
        "import fcntl, sys\n"
        "from pathlib import Path\n"
        "path = Path(sys.argv[1])\n"
        "path.parent.mkdir(parents=True, exist_ok=True)\n"
        "with path.open('a+') as handle:\n"
        "    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)\n"
        "    sys.stdout.write('locked\\n')\n"
        "    sys.stdout.flush()\n"
        "    sys.stdin.readline()\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(lock)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None and proc.stdout.readline() == "locked\n"
        result = run_refresh(config, now=NOW)
        assert result == {"status": "locked"}
        assert not (config.state_dir / "state.json").exists()
        assert seams.process == []
        assert seams.trials == []
    finally:
        if proc.poll() is None and proc.stdin is not None:
            proc.stdin.write("\n")
            proc.stdin.close()
            proc.wait(timeout=5)


def test_configured_queue_roots_are_not_modified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = _checkout(tmp_path, "primary")
    other = _checkout(tmp_path, "other")
    job = _completed_job(primary / "runs", "harbor-job")
    (primary / "queue" / "approved").mkdir()
    (primary / "queue" / "approved" / "keep.json").write_text("{}\n", encoding="utf-8")
    (other / "runs" / "untouched.txt").write_text("source\n", encoding="utf-8")
    config = _config(tmp_path, primary, other)
    seams = Seams()
    seams.install(monkeypatch)
    before = {primary: _tree(primary), other: _tree(other)}

    result = run_refresh(config, now=NOW)

    assert result["status"] == "refreshed"
    assert {primary: _tree(primary), other: _tree(other)} == before
    assert seams.spend == [(primary.resolve(), other.resolve())]
    output = seams.process[0]["output_dir"]
    assert output == config.state_dir / "processed" / _stable_key(job)
    assert not output.resolve().is_relative_to(primary.resolve())
    assert not (job / "processed").exists()


def test_ten_line_digest_merges_into_facts_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _checkout(tmp_path)
    job = _completed_job(checkout / "runs", "harbor-job")
    config = _config(tmp_path, checkout)
    report = config.facts_root / "runs" / REPORT_DAY
    report.mkdir(parents=True)
    (report / "facts.json").write_text(json.dumps({"weather": "clear"}) + "\n", encoding="utf-8")
    (report / "facts.md").write_text(
        "# Report\n\nintro stays\n\n## Eval Lab nightly\n- stale line\n\n## Later\nkeep me\n",
        encoding="utf-8",
    )
    seams = Seams()
    seams.install(monkeypatch)

    run_refresh(config, now=NOW)
    digest = _load(config.facts_root / "inputs" / "evallab-nightly.json")
    merged = _load(report / "facts.json")
    text = (report / "facts.md").read_text(encoding="utf-8")

    assert digest["schema"] == DIGEST_SCHEMA
    assert digest["generated_at"] == NOW.isoformat()
    assert digest["campaigns"] == _campaigns((checkout.resolve(),))
    assert len(digest["lines"]) == 10
    assert merged["weather"] == "clear"
    assert merged["evallab_nightly"] == digest
    assert text.count("## Eval Lab nightly") == 1
    assert "stale line" not in text
    assert "intro stays" in text
    assert "keep me" in text
    assert "\n".join(digest["lines"]) in text
    assert "demo@checkout" in text

    payload = _load(job / "result.json")
    payload["finished_at"] = "2026-10-06T20:00:00Z"
    (job / "result.json").write_text(json.dumps(payload) + "\n", encoding="utf-8")
    run_refresh(config, now=NOW)
    second = _load(config.facts_root / "inputs" / "evallab-nightly.json")
    merged_again = _load(report / "facts.json")
    rewritten = (report / "facts.md").read_text(encoding="utf-8")

    assert second["lines"] != digest["lines"]
    assert len(second["lines"]) == 10
    assert second["campaigns"] == digest["campaigns"]
    assert merged_again["weather"] == "clear"
    assert merged_again["evallab_nightly"] == second
    assert rewritten.count("## Eval Lab nightly") == 1
    assert "\n".join(second["lines"]) in rewritten
    assert "\n".join(digest["lines"]) not in rewritten
    assert "intro stays" in rewritten
    assert "keep me" in rewritten
    assert "stale line" not in rewritten


def test_source_addition_older_than_high_water_is_not_missed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _checkout(tmp_path)
    job = _completed_job(checkout / "runs", "harbor-job")
    config = _config(tmp_path, checkout)
    seams = Seams()
    seams.install(monkeypatch)
    first = run_refresh(config, now=NOW)
    added = job / "older-source.txt"
    added.write_text("copied with an old timestamp\n", encoding="utf-8")
    _backdate(added)
    older = _completed_job(checkout / "jobs", "older-job")
    _backdate(older / "result.json")
    assert added.stat().st_mtime_ns < first["high_water_mark"]
    assert (older / "result.json").stat().st_mtime_ns < first["high_water_mark"]

    result = _refresh(config, seams)

    assert result["status"] == "refreshed"
    assert {call["job"].resolve() for call in seams.process} == {job.resolve(), older.resolve()}
    assert result["high_water_mark"] == first["high_water_mark"]
    state = _load(config.state_dir / "state.json")
    assert str(older.resolve()) in state["selection"]["jobs"]


def test_result_only_jobs_under_runs_and_jobs_are_discovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _checkout(tmp_path)
    from_runs = _completed_job(checkout / "runs", "from-runs", trials=0)
    from_jobs = _completed_job(checkout / "jobs", "from-jobs", trials=1)
    incomplete = checkout / "runs" / "incomplete"
    incomplete.mkdir()
    (incomplete / "result.json").write_text(
        json.dumps({"id": "incomplete", "n_total_trials": 1, "stats": {}}) + "\n",
        encoding="utf-8",
    )
    hidden = checkout / "runs" / ".sources" / "cached"
    _completed_job(hidden, "hidden-job")
    config = _config(tmp_path, checkout)
    seams = Seams()
    seams.install(monkeypatch)

    run_refresh(config, now=NOW)

    assert {call["job"].resolve() for call in seams.process} == {
        from_runs.resolve(),
        from_jobs.resolve(),
    }


def test_queue_meter_scan_ignores_events_leases_and_non_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab.queue import QUEUE_STATES

    checkout = _checkout(tmp_path)
    _completed_job(checkout / "runs", "harbor-job")
    config = _config(tmp_path, checkout)
    seams = Seams()
    seams.install(monkeypatch)
    run_refresh(config, now=NOW)
    state_name = QUEUE_STATES[0]
    ignored = [
        checkout / "queue" / "events" / "noise.json",
        checkout / "queue" / "leases" / "noise.json",
        checkout / "queue" / "spec.json",
        checkout / "queue" / state_name / "note.txt",
        checkout / "queue" / state_name / "nested" / "spec.json",
    ]
    for path in ignored:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")

    assert _refresh(config, seams)["status"] == "noop"
    assert seams.process == []

    watched = checkout / "queue" / state_name / "spec.json"
    watched.parent.mkdir(parents=True, exist_ok=True)
    watched.write_text('{"campaign_id": "demo"}\n', encoding="utf-8")
    again = _refresh(config, seams)
    assert again["status"] == "refreshed"
    assert seams.process == []
    assert seams.trials

    campaign = checkout / "runs" / "campaigns" / "demo" / "campaign.json"
    campaign.parent.mkdir(parents=True)
    campaign.write_text("{}\n", encoding="utf-8")
    third = _refresh(config, seams)
    assert third["status"] == "refreshed"
    assert seams.process == []
    assert seams.spend


def test_state_guards_metadata_and_config_not_only_job_stamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _checkout(tmp_path)
    _completed_job(checkout / "runs", "harbor-job")
    config = _config(tmp_path, checkout)
    seams = Seams()
    seams.install(monkeypatch)
    run_refresh(config, now=NOW)
    decoy = config.repo_root / "library" / "task-variants" / "decoy.json"
    decoy.parent.mkdir(parents=True)
    decoy.write_text("{}\n", encoding="utf-8")

    assert _refresh(config, seams)["status"] == "noop"

    metadata = config.data_root / "library" / "task-variants" / "note.json"
    assert config.data_root is not None
    metadata.parent.mkdir(parents=True)
    metadata.write_text("{}\n", encoding="utf-8")
    changed = _refresh(config, seams)
    assert changed["status"] == "refreshed"
    assert seams.process == []

    assert config.readers_store is not None
    (config.readers_store / "verdict.json").write_text("{}\n", encoding="utf-8")
    readers = _refresh(config, seams)
    assert readers["status"] == "refreshed"
    assert seams.process == []

    old_facts = config.facts_root
    assert old_facts is not None
    old_input = old_facts / "inputs" / "evallab-nightly.json"
    old_stamp = (old_input.stat().st_mtime_ns, old_input.read_bytes())
    moved = RefreshConfig(
        repo_root=config.repo_root,
        state_dir=config.state_dir,
        queue_roots=config.queue_roots,
        facts_root=tmp_path.resolve() / "other-facts",
        readers_store=config.readers_store,
        data_root=config.data_root,
    )
    relocated = _refresh(moved, seams)
    assert relocated["status"] == "refreshed"
    assert seams.process == []
    assert (old_input.stat().st_mtime_ns, old_input.read_bytes()) == old_stamp
    assert (moved.facts_root / "inputs" / "evallab-nightly.json").is_file()


def test_producer_fingerprint_ignores_pycache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _checkout(tmp_path)
    _completed_job(checkout / "runs", "harbor-job")
    config = _config(tmp_path, checkout)
    seams = Seams()
    seams.install(monkeypatch)
    run_refresh(config, now=NOW)
    cached = config.repo_root / "src" / "pkg" / "__pycache__" / "mod.cpython-312.pyc"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"\x00pyc")

    assert _refresh(config, seams)["status"] == "noop"

    (config.repo_root / "src" / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    result = _refresh(config, seams)
    assert result["status"] == "refreshed"
    assert seams.process == []
    assert seams.trials


def test_real_process_job_writes_under_state_not_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _checkout(tmp_path)
    job = _completed_job(checkout / "runs", "harbor-job", trials=0)
    config = _config(tmp_path, checkout)
    seams = Seams()
    seams.install(monkeypatch, process=False)
    before = _tree(checkout)

    result = run_refresh(config, now=NOW)

    report_path = config.state_dir / "processed" / _stable_key(job) / "job.json"
    report = _load(report_path)
    assert result["status"] == "refreshed"
    assert report["summary"]["ingest"] == "ingest disabled by caller"
    assert report["results_home"] is None
    assert report_path.resolve().is_relative_to(config.state_dir.resolve())
    assert not report_path.resolve().is_relative_to(checkout.resolve())
    assert not (job / "processed").exists()
    assert _tree(checkout) == before
    assert seams.trials
    assert seams.pages


def test_nightly_refresh_command_never_constructs_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from evallab import cli

    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("paid nightly must not be constructed")

    monkeypatch.setattr(cli.Executor, "from_repo", explode)
    monkeypatch.setattr(cli, "NightlyCycle", explode)
    seen: list[RefreshConfig] = []

    def fake_run(config: RefreshConfig, *, now: datetime | None = None) -> dict[str, Any]:
        seen.append(config)
        return {"status": "noop", "jobs": 0, "high_water_mark": 7}

    monkeypatch.setattr(nightly_refresh, "run_refresh", fake_run)
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    facts = tmp_path / "facts"
    readers = tmp_path / "readers"
    data = tmp_path / "data"
    checkout = tmp_path / "checkout"
    args = argparse.Namespace(
        refresh=True,
        queue_root=[checkout],
        state_dir=state,
        facts_root=facts,
        readers_store=readers,
        data_root=data,
        report_date=None,
    )

    assert cli._nightly_command(args, runtime) == 0

    printed = json.loads(capsys.readouterr().out)
    assert printed == {"high_water_mark": 7, "jobs": 0, "status": "noop"}
    config = seen[0]
    assert config.repo_root == runtime
    assert config.state_dir == state.resolve()
    assert config.queue_roots == (checkout,)
    assert config.facts_root == facts
    assert config.readers_store == readers
    assert config.data_root == data

    absent = argparse.Namespace(
        refresh=True,
        queue_root=[],
        state_dir=state,
        facts_root=None,
        readers_store=None,
        data_root=None,
        report_date=None,
    )
    assert cli._nightly_command(absent, runtime) == 0
    assert seen[1].data_root is None
    assert seen[1].facts_root is None
    assert seen[1].readers_store is None


def test_legacy_nightly_does_not_call_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab import cli

    def paid(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("paid-path")

    def forbid(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("refresh")

    monkeypatch.setattr(cli.Executor, "from_repo", paid)
    monkeypatch.setattr(nightly_refresh, "run_refresh", forbid)
    args = argparse.Namespace(refresh=False, report_date=None)

    with pytest.raises(RuntimeError, match="paid-path"):
        cli._nightly_command(args, tmp_path)


def test_nightly_refresh_flag_defaults_off() -> None:
    from evallab.cli import parser

    assert parser().parse_args(["nightly"]).refresh is False
    assert parser().parse_args(["nightly", "--refresh"]).refresh is True


def test_installer_hardcodes_refresh_flag() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts/ops/launchd/install-nightly-refresh.sh").read_text(encoding="utf-8")
    assert '"nightly", "--refresh"' in script


def test_naive_now_is_rejected(tmp_path: Path) -> None:
    config = _config(tmp_path, _checkout(tmp_path))
    with pytest.raises(ValueError, match="timezone"):
        run_refresh(config, now=datetime(2026, 10, 7, 6, 30))


def test_ongoing_trial_changes_refresh_view_pages_and_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _checkout(tmp_path)
    job = checkout / "runs" / "ongoing"
    agent = job / "format-code-task-000001__trial" / "agent"
    agent.mkdir(parents=True)
    trajectory = agent / "trajectory.json"
    trajectory.write_text('{"steps": []}\n')
    config = _config(tmp_path, checkout)
    seams = Seams()
    seams.install(monkeypatch)

    first = _refresh(config, seams)
    assert first["status"] == "refreshed"
    assert first["new_runs"] == 1
    assert seams.process == []  # Native job result is not finished yet.
    assert seams.replay == [[job]]
    assert seams.pages == [[job]]
    assert _refresh(config, seams)["status"] == "noop"

    trajectory.write_text('{"steps": [{"step_id": 1}]}\n')
    changed = _refresh(config, seams)
    assert changed["status"] == "refreshed"
    assert changed["new_runs"] == 0
    assert seams.process == []
    assert seams.trials == [[checkout / "runs", checkout / "jobs"]]
    assert seams.replay == [[job]]
    assert seams.pages == [[job]]
