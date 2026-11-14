"""Synthetic live-watch state contracts."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from watch_helpers import (
    CLEAN,
    I06A,
    I06B,
    I12_ROWS,
    I15,
    STAGE_ONE,
    STAGE_THREE,
    STAGE_TWO,
    Clock,
    NoTestRegistry,
    World,
    advancing,
    atif,
    edit_commit,
    put_fields,
    reseal,
    tree_hashes,
)

from evallab import cli
from evallab import hack_drop as hd
from evallab.line import core, watch
from evallab.line.io import read_json, read_rows, write_json, write_rows
from evallab.line.watch_state import State, WatchError, owner_lock


def test_a_failed_line_check_stops_the_watcher_and_commits_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = core.run_line

    def line_whose_check_fails(spec: Path, repo_root: Path) -> Path:
        batch = real(spec, repo_root)
        failing = {"name": "determinism", "status": "FAIL", "details": "boom"}
        write_json(batch / "check.json", {"checks": [failing], "passed": False})
        return batch

    monkeypatch.setattr(core, "run_line", line_whose_check_fails)
    world.add_job("job-1", STAGE_ONE)
    outcome = world.run()
    assert "failed the line checks" in outcome["error"] and "determinism" in outcome["error"]
    assert world.state.committed() == [] and not (world.state_dir / "judge-queue.jsonl").exists()
    status = read_json(world.state.status_path)
    assert status["phase"] == "failed" and read_rows(world.state.errors_path)
    assert [p.name for p in world.state.open_folders()] == ["000001"]  # the evidence stays
    assert (
        cli.run_cli(
            [
                "line",
                "watch",
                str(world.config_path),
                "--state-dir",
                str(world.state_dir),
                "--once",
            ],
            workspace=world.tmp,
        )
        == 2
    )
    monkeypatch.setattr(core, "run_line", real)  # the cause is gone: the next start plans again
    assert world.run()["committed"] == [1]
    assert any(p.name.startswith("000001-") for p in world.state.abandoned.iterdir())
    assert world.check()["passed"]

def test_a_tampered_frozen_task_status_stops_the_watcher_and_nothing_new_is_committed(
    world: World,
) -> None:
    world.add_job("job-1", STAGE_ONE)
    world.run()
    (world.state.frozen / "task-status.json").write_text(
        json.dumps({"tasks": [{"task_id": "t-weak", "status": "certified", "reason": "x"}]})
    )
    world.add_job("job-2", STAGE_TWO)
    outcome = world.run()
    assert "task status" in outcome["error"]
    assert [r["n"] for r in world.state.committed()] == [1]
    assert read_json(world.state.status_path)["phase"] == "failed"

def test_a_state_edited_while_the_watcher_runs_is_caught_before_the_next_increment(
    world: World,
) -> None:
    def tamper_and_add_runs(_seconds: float) -> None:
        (world.state.frozen / "task-status.json").write_text("{}")
        world.add_job("job-1", STAGE_ONE)

    outcome = world.run(once=False, max_cycles=3, sleep=tamper_and_add_runs)
    assert "task status" in outcome["error"] and not world.state.folders()

def test_a_failed_check_after_a_commit_stops_the_watcher(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.add_job("job-1", STAGE_ONE)
    real = watch.verify

    def verify(state: State, root: Path, *, deep: bool) -> dict[str, Any]:
        if not state.committed():
            return real(state, root, deep=deep)
        failure = {"name": "spend", "status": "FAIL", "details": "boom"}
        return {"passed": False, "deep": False, "checks": [failure], "increments": 1, "runs": 3}

    monkeypatch.setattr(watch, "verify", verify)
    outcome = world.run()
    assert "after committing increment 1" in outcome["error"] and "boom" in outcome["error"]
    assert read_json(world.state.status_path)["phase"] == "failed"

def test_a_damaged_committed_state_is_refused_at_start_up_and_not_built_upon(world: World) -> None:
    world.add_job("job-1", STAGE_ONE)
    world.run()
    world.add_job("job-2", STAGE_TWO)
    path = world.child(1) / "decisions.jsonl"
    write_rows(path, [{**r, "decision": "keep", "audit": False} for r in read_rows(path)])
    outcome = world.run()
    assert "live check failed at start-up" in outcome["error"]
    assert [r["n"] for r in world.state.committed()] == [1]

def test_crash_after_the_queue_write_resumes_without_double_queueing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.add_job("job-1", STAGE_ONE)
    real_publish = watch.publish

    def publish_then_die(queue: Path, rows: list[dict[str, Any]]) -> int:
        real_publish(queue, rows)
        raise RuntimeError("killed after the queue write")

    monkeypatch.setattr(watch, "publish", publish_then_die)
    assert "killed after the queue write" in world.run()["error"]
    queued = world.queue_ids()
    assert queued and world.state.committed() == []  # queued, not committed
    monkeypatch.setattr(watch, "publish", real_publish)
    assert world.run()["committed"] == [1]
    assert sorted(world.queue_ids()) == sorted(queued) == sorted(set(queued))
    assert world.check()["passed"]

def test_restart_after_a_crash_equals_an_uninterrupted_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = NoTestRegistry()
    monkeypatch.setattr(hd, "_get_corpus_registry", lambda *_a, **_k: registry)
    monkeypatch.setattr(core, "_code", lambda _: ("a" * 40, False))
    outcomes = []
    for label, crash in (("clean", False), ("crashed", True)):
        root = tmp_path / label
        root.mkdir()
        world = World(root, Clock(), registry)
        world.add_job("job-1", STAGE_ONE)
        world.add_job("job-2", STAGE_TWO)
        if crash:
            real = core.run_line

            def dies_midway(spec: Path, repo_root: Path) -> Path:
                partial = Path(read_json(spec)["out_root"]) / read_json(spec)["batch_id"]
                (partial / "receipts").mkdir(parents=True)
                write_json(partial / "spec.json", {"half": "written"})
                raise RuntimeError("killed mid-batch")

            monkeypatch.setattr(core, "run_line", dies_midway)
            assert "killed mid-batch" in world.run()["error"]
            monkeypatch.setattr(core, "run_line", real)
        assert "error" not in world.run()
        assert world.check()["passed"], world.check()
        outcomes.append(
            (
                world.decisions(),
                sorted(world.queue_ids()),
                [sorted(r["runs"]) for r in world.state.committed()],
                world.scoreboard_without_timing(),
            )
        )
        if crash:
            assert any(world.state.abandoned.iterdir())  # the partial increment is kept aside
    assert outcomes[0] == outcomes[1]

def test_an_increment_without_a_commit_record_is_moved_aside_on_restart(world: World) -> None:
    world.run()  # creates the state directory
    junk = world.state.increments / "000001"
    (junk / "inputs").mkdir(parents=True)
    (junk / "inputs" / "half").write_text("x")
    world.add_job("job-1", STAGE_ONE)
    outcome = world.run()
    assert outcome["committed"] == [1] and outcome["recovered"]["abandoned"]
    assert (
        world.state.abandoned / outcome["recovered"]["abandoned"][0] / "inputs" / "half"
    ).is_file()
    assert world.check()["passed"]

def test_a_second_owner_of_a_state_directory_is_refused(world: World) -> None:
    world.add_job("job-1", STAGE_ONE)
    with owner_lock(world.state), pytest.raises(WatchError, match="another watcher owns"):
        world.run()
    assert world.state.committed() == []
    assert world.run()["committed"] == [1]  # the lock is released with its owner

def test_a_state_directory_belongs_to_one_configuration(world: World) -> None:
    world.add_job("job-1", STAGE_ONE)
    world.run()
    config = read_json(world.config_path)
    config["audit"] = {"rate": 0.9, "seed": "other"}
    write_json(world.config_path, config)
    with pytest.raises(WatchError, match="different configuration"):
        world.run()
    config["audit"] = {"rate": 0.5, "seed": "fixed"}
    config["watch"] = {"settle_seconds": 5, "poll_seconds": 1, "watchdog_max_age_seconds": 60}
    write_json(world.config_path, config)
    assert "error" not in world.run()  # tuning may change; decisions-relevant settings may not
    assert world.state.config()["watch"]["settle_seconds"] == 5

def test_the_cli_watches_checks_and_scores_a_state_directory(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.add_job("job-1", STAGE_ONE)
    args = [
        "line",
        "watch",
        str(world.config_path),
        "--state-dir",
        str(world.state_dir),
        "--once",
        "--json",
    ]
    assert cli.run_cli(args, workspace=world.tmp) == 0
    assert json.loads(capsys.readouterr().out)["committed"] == [1]
    for target in (world.state_dir,):
        assert cli.run_cli(["line", "check", str(target), "--json"], workspace=world.tmp) == 0
        reported = json.loads(capsys.readouterr().out)
        assert [c["name"] for c in reported["checks"]] == ["children", "conservation", "cumulative", "spend"]

def test_checking_a_state_or_a_child_never_writes_into_a_committed_increment(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.add_job("job-1", STAGE_ONE)
    world.run()
    before = tree_hashes(world.state.increments)
    assert world.check()["passed"]
    assert cli.run_cli(["line", "check", str(world.child(1)), "--json"], workspace=world.tmp) == 0
    assert json.loads(capsys.readouterr().out)["passed"] is True
    assert tree_hashes(world.state.increments) == before  # SCOREBOARD.md, check.json, all of it

def test_a_corrupt_commit_record_fails_the_live_check_with_the_reason(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.add_job("job-1", STAGE_ONE)
    world.run()
    (world.state.inc(1) / "commit.json").write_text("{corrupt")
    assert cli.run_cli(["line", "check", str(world.state_dir), "--json"], workspace=world.tmp) == 2
    reported = json.loads(capsys.readouterr().out)
    assert not reported["passed"] and "commit records" in reported["checks"][0]["details"]

def test_a_state_directory_without_watch_json_fails_the_live_check(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.add_job("job-1", STAGE_ONE)
    world.run()
    world.state.watch_json.unlink()
    assert cli.run_cli(["line", "check", str(world.state_dir), "--json"], workspace=world.tmp) == 2
    reported = json.loads(capsys.readouterr().out)
    assert [c["name"] for c in reported["checks"]] == ["children", "conservation", "cumulative", "spend"]
    assert not (world.state_dir / "check.json").exists()  # not treated as a batch folder

def edit_increment_spec(world: World) -> None:
    path = world.state.inc(1) / "spec.json"
    write_json(path, {**read_json(path), "audit": {"rate": 1.0, "seed": "other"}})

def edit_receipt(world: World) -> None:
    path = world.child(1) / "receipts" / "intake.json"
    write_json(path, {**read_json(path), "seconds": 0.0})

def edit_decisions(world: World) -> None:
    path = world.child(1) / "decisions.jsonl"
    write_rows(path, [{**r, "decision": "keep", "audit": False} for r in read_rows(path)])

def edit_trace(world: World) -> None:
    path = world.child(1) / "trace.jsonl"
    rows = read_rows(path)
    rows[0]["note"] = "edited"
    write_rows(path, rows)

def change_a_source(world: World) -> None:
    path = next(iter(world.results.rglob("a1/agent/trajectory.json")))
    write_json(path, atif(CLEAN, CLEAN))

def run_in_two_increments(world: World) -> None:
    rows = read_rows(world.child(1) / "runs.jsonl")
    write_rows(world.child(2) / "runs.jsonl", rows)

def inflate_spend(world: World) -> None:
    path = world.child(1) / "scoreboard.json"
    score = read_json(path)
    score["spend"]["usd"] = 5.0
    write_json(path, score)

def edit_child_totals(world: World) -> None:
    path = world.child(1) / "scoreboard.json"
    score = read_json(path)
    score["overall"]["runs"] += 1
    write_json(path, score)
    reseal(world, 1)

DAMAGE: dict[str, tuple[Callable[[World], None], set[str], bool]] = {
    # name: (damage, checks that must fail, needs the full check)
    "commit_record_removed": (
        lambda w: (w.state.inc(1) / "commit.json").unlink(),
        {"conservation"},
        False,
    ),
    "commit_number_edited": (lambda w: edit_commit(w, 2, n=7), {"conservation"}, False),
    "commit_batch_path_edited": (lambda w: edit_commit(w, 2, batch="x/y"), {"conservation"}, False),
    "commit_decisions_hash_edited": (
        lambda w: edit_commit(w, 1, decisions_sha256="0" * 64),
        {"conservation"},
        False,
    ),
    "commit_schema_edited": (lambda w: edit_commit(w, 1, schema="other"), {"conservation"}, False),
    "orphan_increment_folder": (lambda w: w.state.inc(9).mkdir(), {"conservation"}, False),
    "scratch_folder_in_increments": (
        lambda w: (w.state.increments / "000003.tmp").mkdir(),
        {"conservation"},
        False,
    ),
    "increment_left_open_by_a_dead_watcher": (
        lambda w: w.state.inc(3).mkdir(),
        {"conservation"},
        False,
    ),
    "child_decision_edited": (edit_decisions, {"children", "conservation"}, False),
    "child_scoreboard_md_edited": (
        lambda w: (w.child(1) / "SCOREBOARD.md").write_text("edited\n"),
        {"conservation"},
        False,
    ),
    "child_receipt_edited": (edit_receipt, {"conservation"}, False),
    "child_check_result_edited": (
        lambda w: write_json(w.child(1) / "check.json", {"checks": [], "passed": True}),
        {"conservation"},
        False,
    ),
    "increment_spec_edited": (edit_increment_spec, {"conservation"}, False),
    "older_child_trace_edited": (edit_trace, {"children"}, True),
    "source_changed_after_commit": (change_a_source, {"children"}, True),
    "child_deleted": (lambda w: shutil.rmtree(w.child(1)), {"children", "conservation"}, False),
    "run_in_two_increments": (run_in_two_increments, {"conservation"}, False),
    "spend_inflated": (inflate_spend, {"children", "spend"}, False),
    "child_scoreboard_totals_resealed": (edit_child_totals, {"cumulative"}, False),
    "watch_json_deleted": (
        lambda w: w.state.watch_json.unlink(),
        {"children", "conservation", "cumulative", "spend"},
        False,
    ),
}

@pytest.mark.parametrize("damage", sorted(DAMAGE))
def test_the_live_check_names_the_damage(world: World, damage: str) -> None:
    world.add_job("job-1", STAGE_ONE)
    world.run()
    world.add_job("job-2", STAGE_TWO)
    world.run()
    assert world.check()["passed"]
    apply, failed, _full = DAMAGE[damage]
    apply(world)
    names = world.failing()
    assert failed <= names, names
    assert not world.check()["passed"]

def test_the_quick_check_runs_the_line_check_on_the_newest_child_and_the_full_check_on_all(
    world: World,
) -> None:
    world.add_job("job-1", STAGE_ONE)
    world.run()
    world.add_job("job-2", STAGE_TWO)
    world.run()
    edit_trace(world)  # an older child's trace: every child's folder hash is always checked
    assert world.failing(deep=False) == {"conservation"}
    assert world.failing(deep=True) == {"children", "conservation"}

def test_an_open_increment_is_not_damage_while_its_watcher_runs(world: World) -> None:
    world.add_job("job-1", STAGE_ONE)
    world.run()
    world.state.inc(2).mkdir()
    with owner_lock(world.state):
        assert world.check()["passed"]
    assert "conservation" in world.failing()  # nobody owns it any more: a leftover

def test_lag_is_measured_from_recorded_times_only() -> None:
    record = {
        "times": {
            "committed_at": "2026-11-02T10:01:40+00:00",
            "planned_at": "2026-11-02T10:01:30+00:00",
        },
        "runs": {
            "x": {
                "first_seen_at": "2026-11-02T10:00:00+00:00",
                "ready_at": "2026-11-02T10:01:00+00:00",
                "finished_at": "2026-11-02T09:59:00+00:00",
            },
            "y": {
                "first_seen_at": "2026-11-02T10:01:00+00:00",
                "ready_at": "2026-11-02T10:01:30+00:00",
                "finished_at": "2026-11-02T10:01:00",
            },  # naive: unknown timezone, not used
        },
    }
    lag = watch.lag_summary([record])
    assert lag["discovery_to_commit_seconds"] == {"n": 2, "p50": 40.0, "p95": 100.0, "max": 100.0}
    assert lag["ready_to_commit_seconds"]["max"] == 40.0
    assert lag["completion_to_commit_seconds"] == {"n": 1, "p50": 160.0, "p95": 160.0, "max": 160.0}
    assert lag["increment_processing_seconds"]["max"] == 10.0
    assert watch.lag_summary([])["discovery_to_commit_seconds"]["n"] == 0

def settling_config(world: World) -> None:
    world.write_config(
        watch={"settle_seconds": 30, "poll_seconds": 1, "watchdog_max_age_seconds": 60}
    )

def test_i02b_a_job_file_edited_while_a_manifest_trial_settles_is_admitted_again(
    world: World,
) -> None:
    settling_config(world)
    job = world.add_job("job-1", [("l1", "t-cert", False)])
    world.use_manifest([{"id": "l1", "task_id": "t-cert", "path": str(job / "l1")}])

    def edit_the_job_config() -> None:
        write_json(job / "config.json", {"split": "test"})
        world.clock.advance(5)

    outcome = world.run(once=False, max_cycles=2, sleep=advancing(world, edit_the_job_config))
    assert "error" in outcome and not world.state.folders()

def test_i18_a_link_among_the_jobs_of_a_results_home_stops_the_watcher(world: World) -> None:
    job = world.add_job("job-1", STAGE_ONE)
    (job.parent / "alias").symlink_to(job, target_is_directory=True)
    assert "a link among the jobs" in world.run()["error"] and not world.state.folders()

def test_p8b_an_answer_key_sealed_after_a_run_was_admitted_stops_the_watcher(world: World) -> None:
    settling_config(world)
    job = world.add_job("job-1", [("l1", "t-cert", False)])
    stash = world.tmp / "stash.json"
    write_json(stash, atif(CLEAN))
    os.link(stash, job / "l1" / "agent" / "notes.json")  # not sealed yet, so it is admitted

    def seal_the_stash() -> None:
        keys = world.tmp / "research" / "answer-keys"
        keys.mkdir(parents=True)
        os.link(stash, keys / "sealed.json")
        world.clock.advance(5)

    outcome = world.run(once=False, max_cycles=2, sleep=advancing(world, seal_the_stash))
    assert "error" in outcome and not world.state.folders()

def test_p13_a_link_above_a_manifest_row_outside_the_manifests_folder_is_refused(
    world: World,
) -> None:
    real = world.tmp / "real"
    real.mkdir()
    write_json(real / "clean.json", atif(CLEAN))
    outside = world.tmp / "outside"
    outside.mkdir()
    (outside / "linked").symlink_to(real, target_is_directory=True)
    manifests = world.tmp / "manifests"
    manifests.mkdir()
    row = {"id": "r1", "task_id": "t-cert", "path": str(outside / "linked" / "clean.json")}
    write_rows(manifests / "rows.jsonl", [row])
    config = read_json(world.config_path)
    config["sources"] = [
        {
            "corpus": "rows",
            "kind": "manifest",
            "path": str(manifests / "rows.jsonl"),
            "labels": None,
        }
    ]
    write_json(world.config_path, config)
    assert "link" in world.run()["error"] and not world.state.folders()

@pytest.mark.parametrize("case", sorted(I12_ROWS))
def test_i12_i14_a_manifest_row_naming_test_material_stops_the_watcher_even_if_held(
    world: World, case: str
) -> None:
    world.registry.tests.add("t-secret-test")
    trial = world.add_job("job-1", [("l1", "t-cert", False)]) / "l1"
    world.use_manifest(
        [{"id": "l1", "task_id": "t-cert", "path": str(trial)}, I12_ROWS[case](trial)]
    )
    assert "error" in world.run() and not world.state.folders()

@pytest.mark.parametrize(("where", "key"), I06A)
def test_i06a_an_evaluator_field_in_any_run_json_stops_the_watcher(
    world: World, where: str, key: str
) -> None:
    job = world.add_job("job-1", STAGE_ONE)
    put_fields(job, where, {key: True})
    outcome = world.run()
    assert "error" in outcome and not world.state.committed()

@pytest.mark.parametrize(("where", "fields"), I06B)
def test_i06b_a_non_dev_marker_or_sealed_task_in_any_run_json_stops_the_watcher(
    world: World, where: str, fields: dict[str, Any]
) -> None:
    world.registry.tests.add("t-secret-test")
    job = world.add_job("job-1", STAGE_ONE)
    put_fields(job, where, fields)
    outcome = world.run()
    assert "error" in outcome and not world.state.committed()

@pytest.mark.parametrize("row", I15, ids=["split", "is_cheat", "ground_truth"])
def test_i15_a_judge_verdict_file_with_test_or_label_fields_stops_the_watcher(
    world: World, row: dict[str, Any]
) -> None:
    world.add_job("job-1", STAGE_ONE)
    verdicts = world.tmp / "verdicts.jsonl"
    write_rows(verdicts, [row])
    config = read_json(world.config_path)
    config["judge"] = {"queue": None, "write": True, "verdicts": str(verdicts)}
    write_json(world.config_path, config)
    with pytest.raises(ValueError, match="Station verdicts line 1"):
        world.run()
    assert not world.state_dir.exists()  # refused before anything was written

def test_a_source_run_edited_after_its_commit_stops_the_watcher_before_it_commits_more(
    world: World,
) -> None:
    first = world.add_job("job-1", STAGE_ONE)
    assert world.run()["committed"] == [1]
    world.add_job("job-2", STAGE_TWO)
    assert world.run()["committed"] == [2]
    trajectory = first / "a1" / "agent" / "trajectory.json"
    write_json(trajectory, {**read_json(trajectory), "tampered": True})
    world.add_job("job-3", STAGE_THREE)
    outcome = world.run()
    assert "error" in outcome
    assert [r["n"] for r in world.state.committed()] == [1, 2]
