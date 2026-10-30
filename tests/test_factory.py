"""Seed, step, watchdog and measure for the local DEV production factory."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from evallab.production import factory
from evallab.production.corpus_queue import ContinuousWorkQueue, CorpusRegistry

REPO = Path(__file__).resolve().parents[1]
DATASET = "factory-test-corpus"


def _step(step_id: int, source: str, message: str, command: str | None = None) -> dict:
    step: dict = {"step_id": step_id, "source": source, "message": message}
    if command is not None:
        step["tool_calls"] = [
            {
                "tool_call_id": f"call-{step_id}",
                "function_name": "bash",
                "arguments": {"command": command},
            }
        ]
        step["observation"] = {"results": [{"content": "ok"}]}
    return step


def _document(commands: list[str]) -> dict:
    steps = [
        _step(1, "system", "You are a helpful assistant."),
        _step(2, "user", "Fix the failing test."),
    ]
    steps += [_step(3 + n, "agent", f"running {n}", command) for n, command in enumerate(commands)]
    return {
        "schema_version": "ATIF-v1.6",
        "agent": {"name": "agent", "version": "unknown"},
        "steps": steps,
    }


@pytest.fixture
def export(tmp_path: Path) -> tuple[Path, Path]:
    directory = tmp_path / "export"
    directory.mkdir()
    join = {}
    for name, commands in {
        "run-aaa": ["ls", "git log --oneline -5", "pytest -q"],
        "run-bbb": ["ls", "cat setup.py"],
        "run-ccc": ["ls"],
    }.items():
        (directory / f"{name}.json").write_text(json.dumps(_document(commands)))
        join[name] = {"trace_id": f"store/{name}", "reward": 0.0}
    manifest = tmp_path / "export.manifest.json"
    manifest.write_text(json.dumps(join))
    return directory, manifest


def _seed(root: Path, export: tuple[Path, Path], **overrides) -> dict:
    options = {
        "database": root / factory.JOURNAL,
        "export_directory": export[0],
        "manifest": export[1],
        "dataset_id": DATASET,
        "license_text": "test-only",
        "repo_root": REPO,
    }
    return factory.seed(**{**options, **overrides})


def test_seed_is_idempotent_and_gives_every_run_its_own_run_id(tmp_path: Path, export) -> None:
    first = _seed(tmp_path, export)
    assert (first["selected"], first["new_events"], first["already_queued"], first["excluded"]) == (
        3,
        3,
        0,
        0,
    )
    second = _seed(tmp_path, export)
    assert (second["new_events"], second["already_queued"]) == (0, 3)
    with sqlite3.connect(tmp_path / factory.JOURNAL) as connection:
        run_ids = [
            row[0]
            for row in connection.execute(
                "SELECT run_id FROM events WHERE event_type = ?", (factory.REPLAY_JOB,)
            )
        ]
    assert sorted(run_ids) == ["factory-run-aaa", "factory-run-bbb", "factory-run-ccc"]


def test_seed_limit_and_offset_walk_the_same_sha256_order(tmp_path: Path, export) -> None:
    ordered = sorted(["run-aaa", "run-bbb", "run-ccc"], key=factory._blind_order)
    _seed(tmp_path, export, limit=1)
    _seed(tmp_path, export, offset=1, limit=1)
    with sqlite3.connect(tmp_path / factory.JOURNAL) as connection:
        seeded = [
            row[0].removeprefix("factory-")
            for row in connection.execute(
                "SELECT run_id FROM events WHERE event_type = ? ORDER BY seq",
                (factory.REPLAY_JOB,),
            )
        ]
    assert seeded == ordered[:2]


def test_seed_refuses_a_run_outside_the_shared_dev_gate(
    tmp_path: Path, export, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        factory, "_admitted_trace_ids", lambda *_: frozenset({"store/run-aaa", "store/run-bbb"})
    )
    with pytest.raises(ValueError, match="not admitted DEV traces"):
        _seed(tmp_path, export, store_snapshot=tmp_path / "snapshot.sqlite", store_dataset="x")
    assert not (tmp_path / factory.JOURNAL).exists()


def test_registry_sync_registers_seeded_items_in_a_fresh_process(tmp_path: Path, export) -> None:
    _seed(tmp_path, export)
    fresh = CorpusRegistry(REPO)
    last = factory.sync_external_items(tmp_path / factory.JOURNAL, fresh)
    assert last > 0
    assert fresh.admit(DATASET, "run-aaa").split == "dev"
    assert factory.sync_external_items(tmp_path / factory.JOURNAL, fresh, last) == last


def test_step_scores_every_prefix_once_and_heartbeats(tmp_path: Path, export) -> None:
    _seed(tmp_path, export)
    with factory.Factory(tmp_path, REPO, port=None) as service:
        lines = [service.step() for _ in range(6)]
    assert [line["errors"] for line in lines] == [[]] * 6
    with sqlite3.connect(tmp_path / factory.JOURNAL) as connection:
        scored = dict(
            connection.execute(
                "SELECT run_id, COUNT(*) FROM events WHERE event_type = 'monitor.prefix_scored' GROUP BY run_id"
            )
        )
        cards = connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = 'monitor.suspicion'"
        ).fetchone()[0]
        heartbeats = connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = 'collector.heartbeat'"
        ).fetchone()[0]
    # system + user + the agent steps of each run, each scored once
    assert scored == {"factory-run-aaa": 5, "factory-run-bbb": 4, "factory-run-ccc": 3}
    assert cards >= 1  # the git history peek
    assert heartbeats == 6
    status = json.loads((tmp_path / "factory-status.json").read_text())
    assert status["cycle"] == 6 and status["supervisor_id"] == factory.SUPERVISOR_ID
    assert len((tmp_path / "factory-cycles.jsonl").read_text().splitlines()) == 6


def test_a_second_instance_cannot_start(tmp_path: Path, export) -> None:
    _seed(tmp_path, export)
    with (
        factory.Factory(tmp_path, REPO, port=None),
        pytest.raises(SystemExit, match="already running"),
    ):
        factory.Factory(tmp_path, REPO, port=None).__enter__()


def test_watchdog_reports_ok_stalled_and_failed(tmp_path: Path, export) -> None:
    assert factory.watchdog(tmp_path)["status"] == "failed"
    _seed(tmp_path, export)
    starting = factory.watchdog(tmp_path, max_age=60)
    assert (starting["status"], starting["phase"]) == ("ok", "starting")
    never_started = factory.watchdog(tmp_path, max_age=60, now=time.time() + 3600)
    assert never_started["status"] == "stalled"
    assert never_started["reason"] == "no heartbeat and no recent events"
    with factory.Factory(tmp_path, REPO, port=None) as service:
        service.step()
    fresh = factory.watchdog(tmp_path, max_age=60)
    assert (fresh["status"], fresh["phase"]) == ("ok", "running")
    assert fresh["age_seconds"] < 60 and fresh["heartbeat_age_seconds"] < 60
    late = factory.watchdog(tmp_path, max_age=60, now=time.time() + 3600)
    assert late["status"] == "stalled" and late["age_seconds"] > 3500
    assert late["reason"] == "no recent events"


def test_measure_reports_runs_latency_and_failure_classes(tmp_path: Path, export) -> None:
    _seed(tmp_path, export)
    with factory.Factory(tmp_path, REPO, port=None) as service:
        for _ in range(6):
            service.step()
    with (tmp_path / "factory-cycles.jsonl").open("a") as handle:
        handle.write(
            json.dumps(
                {
                    "seconds": 0.1,
                    "load1": 3.0,
                    "errors": [
                        {
                            "stage": "misalignment",
                            "error": "ValueError: boom",
                            "class": "ValueError",
                        }
                    ],
                }
            )
            + "\n"
        )
    report = factory.measure(tmp_path)
    assert report["runs"] == 3 and report["prefix_scores"] == 12
    assert report["latency_seconds"]["scoring_first_to_last_prefix"]["n"] == 3
    assert report["latency_seconds"]["admitted_to_first_card"]["n"] >= 1
    assert report["failures_by_class"] == {"misalignment:ValueError": 1}
    assert factory.measure(tmp_path, first_runs=2)["runs"] == 2
    assert factory.measure(tmp_path, dataset=DATASET)["runs"] == 3
    assert factory.measure(tmp_path, dataset="another-dataset")["runs"] == 0


def test_hook_failure_is_counted_and_does_not_stop_the_loop(tmp_path: Path, export) -> None:
    _seed(tmp_path, export)

    def broken() -> dict:
        raise RuntimeError("hook down")

    with factory.Factory(tmp_path, REPO, port=None) as service:
        service.hooks.add("findings", broken)
        line = service.step()
        assert line["errors"] == [
            {"stage": "findings", "error": "RuntimeError: hook down", "class": "RuntimeError"}
        ]
        assert service.step()["cycle"] == 2


def test_queue_journal_can_be_reopened_after_the_service_stops(tmp_path: Path, export) -> None:
    _seed(tmp_path, export)
    with factory.Factory(tmp_path, REPO, port=None) as service:
        service.step()
    with ContinuousWorkQueue(tmp_path / factory.JOURNAL, registry=CorpusRegistry(REPO)) as queue:
        assert queue.queue_snapshot()["last_seq"] > 3


def _count(root: Path, event_type: str) -> int:
    with sqlite3.connect(root / factory.JOURNAL) as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = ?", (event_type,)
        ).fetchone()[0]


def test_replay_appends_each_step_once_and_completes_the_run(tmp_path: Path, export) -> None:
    _seed(tmp_path, export)
    with factory.Factory(tmp_path, REPO, port=None) as service:
        first = service.step()
    assert first["replayed_steps"] > 0 and first["replayed_run"].startswith("factory-run-")
    with factory.Factory(tmp_path, REPO, port=None) as service:
        for _ in range(5):
            service.step()
    assert _count(tmp_path, "trace.step") == 5 + 4 + 3
    assert _count(tmp_path, "misalignment.replay_completed") == 3
    assert _count(tmp_path, factory.REPLAY_JOB) == 3


def test_a_changed_export_file_is_refused_and_counted(tmp_path: Path, export) -> None:
    _seed(tmp_path, export)
    ordered = sorted(["run-aaa", "run-bbb", "run-ccc"], key=factory._blind_order)
    (export[0] / f"{ordered[0]}.json").write_text(json.dumps(_document(["rm -rf x"])))
    with factory.Factory(tmp_path, REPO, port=None) as service:
        line = service.step()
        assert line["errors"][0]["stage"] == "replay"
        assert line["errors"][0]["class"] == "ValueError"
        assert line["idle"] is False
        for _ in range(4):
            service.step()
    replayed = {run for run in ("run-aaa", "run-bbb", "run-ccc") if run != ordered[0]}
    with sqlite3.connect(tmp_path / factory.JOURNAL) as connection:
        done = {
            row[0].removeprefix("factory-")
            for row in connection.execute(
                "SELECT run_id FROM events WHERE event_type = 'misalignment.replay_completed'"
            )
        }
    assert done == replayed


def test_a_restarted_service_does_not_score_a_prefix_twice(tmp_path: Path, export) -> None:
    _seed(tmp_path, export)
    with factory.Factory(tmp_path, REPO, port=None) as service:
        service.step()
        service.step()
    scored = _count(tmp_path, "monitor.prefix_scored")
    assert scored > 0
    with factory.Factory(tmp_path, REPO, port=None) as service:
        for _ in range(6):
            service.step()
    assert _count(tmp_path, "monitor.prefix_scored") == 12
    assert _count(tmp_path, "monitor.prefix_scored") > scored
