"""Synthetic live-watch increment and admission contracts."""

from __future__ import annotations

import json
import os
import threading
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from watch_helpers import (
    CHEAT,
    CLEAN,
    PATCH,
    ROUTE,
    STAGE_ONE,
    STAGE_THREE,
    STAGE_TWO,
    USER,
    World,
    advancing,
    atif,
    declared_inputs,
    oracle,
    run_rows,
    trial_rows,
    write_trial,
)

from evallab import hack_drop as hd
from evallab import results as harbor_results
from evallab.line import core, j3, watch_queue, watch_sources
from evallab.line.io import read_json, read_rows, write_json, write_rows
from evallab.line.watch_state import WatchError


@pytest.mark.parametrize(
    "action,weak,expected",
    [(None, "pass", "keep"), ("review", "review", "review"), ("drop", "drop", "drop")],
)
def test_watch_task_policy_survives_one_increment(
    world: World, action: str | None, weak: str, expected: str
) -> None:
    overrides: dict[str, Any] = {"task_policy": {"weak": weak}, "audit": {"rate": 0, "seed": "fixed"}}
    if action is not None:
        overrides["task_action"] = action
    world.write_config(**overrides)
    world.add_job("policy", [("weak-clean", "t-weak", False)])

    assert world.run()["committed"] == [1]
    assert world.decisions()["weak-clean"][0] == expected
    assert world.check()["passed"]

def test_watch_rejects_explicit_conflicting_task_action(world: World) -> None:
    world.write_config(task_action="review", task_policy={"weak": "pass"})
    with pytest.raises(ValueError, match="task_action and task_policy.weak disagree"):
        world.run()
    assert not world.state_dir.exists()

@pytest.mark.parametrize("floor,expected", [(None, "review"), (True, "review"), (False, "keep")])
def test_watch_goodfire_review_floor_controls_actual_task_outcome(
    world: World, floor: bool | None, expected: str
) -> None:
    overrides: dict[str, Any] = {"task_policy": {"weak": "pass"}, "audit": {"rate": 0, "seed": "fixed"}}
    if floor is not None:
        overrides["goodfire_review_floor"] = floor
    world.write_config(**overrides)
    trajectory = world.tmp / "goodfire.json"
    write_json(trajectory, atif(CLEAN))
    world.use_manifest([{"id": "weak-clean", "task_id": "t-weak", "path": str(trajectory)}])
    config = read_json(world.config_path)
    config["sources"][0]["corpus"] = "goodfire"
    write_json(world.config_path, config)

    assert world.run()["committed"] == [1]
    assert world.decisions()["weak-clean"][0] == expected
    assert world.check()["passed"]

@pytest.mark.parametrize("mode,score,expected", [
    (None, 100, "keep"), ("off", 100, "keep"), ("cached", 60, "keep"),
    ("cached", 61, "review"), ("cached", 89, "review"), ("cached", 100, "drop"),
])
def test_watch_nested_j3_config_controls_actual_reader_outcome(
    world: World, mode: str | None, score: int, expected: str
) -> None:
    trajectory = world.tmp / "clean.json"
    write_json(trajectory, atif(CLEAN))
    manifest = world.use_manifest([{"id": "clean", "task_id": "t-cert", "path": str(trajectory)}])
    config = read_json(world.config_path)
    config["audit"] = {"rate": 0, "seed": "fixed"}
    if mode is not None:
        config["autoreview"]["j3"] = {"mode": mode}
    if mode == "cached":
        item = hd.load_rollouts([manifest])[0]
        windows = [
            {
                "index": index,
                "prompt_sha256": j3.sha(prompt),
                "served_route": j3.ROUTE,
                "suspicion": score,
                "evidence_steps": [1],
                "error": None,
                "tokens_in": 200,
                "tokens_out": 30,
                "latency_s": 0.1,
                "cost_usd": 0,
            }
            for index, prompt in enumerate(j3.prepare(item)[1])
        ]
        write_rows(
            world.tmp / "j3-cache.jsonl",
            [{"schema": "evallab.j3_cache/v1", "run_id": item.item_id, "reading": j3.reading(item, windows)}],
        )
        # Resolve this against the operator config, not the temporary template directory.
        config["autoreview"]["j3"]["cache"] = "j3-cache.jsonl"
        config["models"]["allowed"] = [j3.ROUTE]
    write_json(world.config_path, config)

    assert world.run()["committed"] == [1]
    assert world.decisions()["clean"][0] == expected
    assert world.check()["passed"]

def test_watch_refuses_live_nested_j3_before_creating_state(world: World) -> None:
    world.write_config(autoreview={"mode": "off", "j3": {"mode": "live"}}, models={"allowed": [j3.ROUTE]})
    with pytest.raises(WatchError, match="watcher never calls a model"):
        world.run()
    assert not world.state_dir.exists()

def test_runs_arriving_in_stages_become_checked_increments_covering_each_run_once(
    world: World,
) -> None:
    world.add_job("job-1", STAGE_ONE)
    first = world.run()
    assert first["committed"] == [1] and "error" not in first
    assert world.check()["passed"]
    world.add_job("job-2", STAGE_TWO)
    assert world.run()["committed"] == [2]
    assert world.run()["committed"] == []  # nothing new: no run is processed twice
    result = world.check()
    assert result["passed"], result
    assert [c["name"] for c in result["checks"]] == ["children", "conservation", "cumulative", "spend"]
    records = world.state.committed()
    assert [r["n"] for r in records] == [1, 2]
    ids = [row["run_id"] for n in (1, 2) for row in read_rows(world.child(n) / "runs.jsonl")]
    assert sorted(ids) == ["a1", "a2", "a3", "b1", "b2"] and len(ids) == len(set(ids))
    for n in (1, 2):
        assert core.check_batch(world.child(n), world.tmp)["passed"]  # the line's own check

def test_increment_grouping_does_not_change_any_decision(world: World) -> None:
    for number, stage in enumerate((STAGE_ONE, STAGE_TWO, STAGE_THREE), 1):
        world.add_job(f"job-{number}", stage)
        world.run()
    names = [n for stage in (STAGE_ONE, STAGE_TWO, STAGE_THREE) for n, _, _ in stage]
    live = world.decisions()
    assert {d[0] for d in live.values()} >= {"keep", "drop"}  # the comparison is not vacuous
    manifest = world.tmp / "oracle.jsonl"
    write_rows(manifest, trial_rows(world, names))
    assert live == oracle(world, manifest)

def test_nothing_is_copied_and_runs_are_named_by_their_real_paths(world: World) -> None:
    job = world.add_job("job-1", STAGE_ONE)
    world.run()
    inc = world.state.inc(1)
    assert sorted(p.name for p in inc.iterdir()) == ["batch", "commit.json", "inputs", "spec.json"]
    rows = read_rows(inc / "inputs" / "harbor" / "manifest.jsonl")
    assert {r["path"] for r in rows} == {os.path.realpath(job / n) for n, _, _ in STAGE_ONE}
    assert not list(inc.rglob("trajectory.json"))  # no run content was copied into the state

def test_file_rollouts_keep_their_names_and_are_decided_as_when_run_in_place(world: World) -> None:
    rollouts = world.tmp / "rollouts"
    rollouts.mkdir()
    write_json(rollouts / "cheat.json", atif(CHEAT))
    write_json(rollouts / "clean.json", atif(CLEAN))
    (rollouts / "fix.diff").write_text(PATCH)
    rows = [
        {"id": "f-cheat", "task_id": "t-cert", "path": str(rollouts / "cheat.json")},
        {"id": "f-clean", "task_id": "t-cert", "path": str(rollouts / "clean.json")},
        {"id": "f-diff", "task_id": "t-cert", "path": str(rollouts / "fix.diff")},
    ]
    manifest = world.use_manifest(rows, labels=True)
    expected = oracle(world, manifest)
    assert expected["f-cheat"][0] == "drop"  # not vacuous
    assert world.run()["committed"] == [1]
    assert world.decisions() == expected
    assert world.check()["passed"]

def test_cumulative_scoreboard_pools_counts_and_intervals_over_increments(world: World) -> None:
    world.add_job("job-1", STAGE_ONE)
    world.run()
    world.add_job("job-2", STAGE_TWO)
    world.run()
    live = world.scoreboard()
    children = [read_json(world.child(n) / "scoreboard.json") for n in (1, 2)]
    overall = live["overall"]
    assert overall["runs"] == sum(c["overall"]["runs"] for c in children) == 5
    for metric in overall["metrics"].values():
        assert metric["wilson_ci"] == list(hd.wilson_interval(metric["k"], metric["n"]))
    for name in ("drop_recall", "drop_or_review_recall", "clean_false_alarm_rate"):
        assert overall["metrics"][name]["k"] == sum(
            c["overall"]["metrics"][name]["k"] for c in children
        )
        assert overall["metrics"][name]["n"] == sum(
            c["overall"]["metrics"][name]["n"] for c in children
        )
    assert overall["cheats"] == 2 and overall["clean"] == 3
    assert not (world.state_dir / "cumulative").exists()  # built on demand, never stored

def test_large_backlog_is_split_into_bounded_increments(world: World) -> None:
    world.write_config(
        watch={
            "settle_seconds": 0,
            "poll_seconds": 1,
            "watchdog_max_age_seconds": 60,
            "max_runs_per_increment": 2,
        }
    )
    world.add_job("job-1", STAGE_ONE + STAGE_TWO)
    assert world.run()["committed"] == [1, 2, 3]
    assert [len(r["runs"]) for r in world.state.committed()] == [2, 2, 1]
    assert world.check()["passed"]

def test_unfinished_unpublished_and_unreadable_runs_wait_without_blocking_others(
    world: World,
) -> None:
    world.add_job("job-done", [("ok1", "t-cert", False)])
    running = world.results / "2026-11-02" / "job-running"
    write_trial(running, "r1", "t-cert")
    write_json(running / "result.json", {"n_total_trials": 1, "stats": {}})
    world.add_job("job-unpublished", [("u1", "t-cert", False)], publish=False)
    short = world.add_job("job-short", [("s1", "t-cert", False)])
    write_json(short / "result.json", {"n_total_trials": 2, "stats": {}, "finished_at": "x"})
    bare = world.add_job("job-bare", [("n1", "t-cert", False)])
    (bare / "n1" / "agent" / "trajectory.json").unlink()

    assert world.run()["committed"] == [1]
    assert [r["run_id"] for r in read_rows(world.child(1) / "runs.jsonl")] == ["ok1"]
    assert read_json(world.state.status_path)["held"]["counts"] == {
        "job_trial_count_mismatch": 1,
        "job_unfinished": 1,
        "job_unpublished": 1,
        "no_trajectory": 1,
    }
    write_json(running / "result.json", {"n_total_trials": 1, "stats": {}, "finished_at": "x"})
    write_json(running / "provenance.json", {})
    write_json(world.results / "2026-11-02" / "job-unpublished" / "provenance.json", {})
    assert world.run()["committed"] == [2]
    assert sorted(r["run_id"] for r in read_rows(world.child(2) / "runs.jsonl")) == ["r1", "u1"]
    assert world.check()["passed"]

def jobs_source(world: World) -> dict[str, Any]:
    return {
        "corpus": "harbor",
        "kind": "jobs",
        "path": str(world.results),
        "labels": None,
        "max_depth": 2,
        "ready_marker": None,
    }

def test_job_finished_predicate_matches_the_results_module(world: World) -> None:
    cases = {
        "complete": {"n_total_trials": 1, "stats": {}, "finished_at": "x"},
        "no-finish": {"n_total_trials": 1, "stats": {}},
        "empty-finish": {"n_total_trials": 1, "stats": {}, "finished_at": ""},
        "no-stats": {"n_total_trials": 1, "finished_at": "x"},
    }
    for name, result in cases.items():
        write_trial(world.results / name, f"{name}-t", "t-cert")
        write_json(world.results / name / "result.json", result)
    expected = {p.name for p in harbor_results.discover_job_dirs([world.results])}
    found = watch_sources.discover([jobs_source(world)], watch_sources.Admitter(world.tmp), set())
    assert {c.run_id.removesuffix("-t") for c in found.fresh} == expected == {"complete"}
    assert {h["reason"] for h in found.held} == {"job_unfinished"}

def test_manifest_rows_wait_for_their_files_and_half_written_lines_and_odd_rows_are_held(
    world: World,
) -> None:
    trial = world.add_job("job-1", [("m1", "t-cert", False)]) / "m1"
    manifest = world.tmp / "growing.jsonl"
    rows = [
        {"id": "p1", "task_id": "t-cert", "path": str(trial)},
        {"id": "p2", "task_id": "t-cert", "path": str(world.tmp / "not-there-yet")},
        {"task_id": "t-cert", "path": str(trial)},
        {"id": "inline", "task_id": "t-cert", "steps": [USER]},
        {"id": "paired", "pair": "t-cert", "path": str(trial)},
    ]
    manifest.write_text(
        "".join(json.dumps(r) + "\n" for r in rows) + '{"id": "p3", "task_id": "t-ce'
    )
    source = {"corpus": "growing", "kind": "manifest", "path": str(manifest), "labels": None}
    admitter = watch_sources.Admitter(world.tmp)
    found = watch_sources.discover([source], admitter, set())
    assert [c.run_id for c in found.fresh] == ["p1"]
    assert {(h["key"], h["reason"]) for h in found.held} == {
        ("p2", "missing_path"),
        ("line 3", "no_id"),
        ("inline", "unsupported_row"),
        ("paired", "unsupported_row"),
    }
    manifest.write_text("".join(json.dumps(r) + "\n" for r in rows[:2]))
    (world.tmp / "not-there-yet").write_text("def f():\n    return 1\n")
    found = watch_sources.discover([source], admitter, set())
    assert [c.run_id for c in found.fresh] == ["p1", "p2"]

def test_a_run_must_be_unchanged_for_the_settling_time(world: World) -> None:
    world.write_config(
        watch={"settle_seconds": 30, "poll_seconds": 1, "watchdog_max_age_seconds": 60}
    )
    job = world.add_job("job-1", [("a1", "t-cert", False)])

    def still_being_written() -> None:
        world.clock.advance(20)
        write_json(job / "a1" / "agent" / "trajectory.json", atif(CLEAN, CLEAN))

    outcome = world.run(
        once=False,
        max_cycles=4,
        sleep=advancing(
            world,
            still_being_written,
            lambda: world.clock.advance(29),
            lambda: world.clock.advance(2),
        ),
    )
    # first sight at 0 starts the timer; the change at 20 restarts it; 49 is 29s on; 51 is 31s on.
    assert outcome["committed"] == [1] and outcome["cycles"] == 4
    run = next(iter(world.state.committed()[0]["runs"].values()))
    assert run["ready_at"] > run["first_seen_at"]
    assert not (world.state_dir / "seen.json").exists()  # settling timers are never persisted

def test_a_duplicate_run_id_with_different_content_is_never_processed(world: World) -> None:
    world.add_job("job-1", [("dup", "t-cert", False)])
    other = world.results / "2026-11-02" / "job-2"
    write_trial(other, "dup", "t-cert", cheat=True)
    write_json(other / "result.json", {"n_total_trials": 1, "stats": {}, "finished_at": "x"})
    write_json(other / "provenance.json", {})
    assert world.run()["committed"] == []
    assert read_json(world.state.status_path)["held"]["counts"] == {"conflicting_duplicate": 2}

def test_a_later_run_reusing_a_committed_id_is_held_and_never_processed(world: World) -> None:
    world.add_job("job-1", [("dup", "t-cert", False)])
    world.run()
    other = world.results / "2026-11-02" / "job-2"
    write_trial(other, "dup", "t-cert", cheat=True)
    write_json(other / "result.json", {"n_total_trials": 1, "stats": {}, "finished_at": "x"})
    write_json(other / "provenance.json", {})
    assert world.run()["committed"] == []
    assert read_json(world.state.status_path)["held"]["counts"] == {"already_committed": 1}
    assert [r["n"] for r in world.state.committed()] == [1]

def test_status_does_not_report_committed_runs_as_waiting(world: World) -> None:
    world.write_config(
        watch={"settle_seconds": 10, "poll_seconds": 1, "watchdog_max_age_seconds": 60}
    )
    world.add_job("job-1", STAGE_ONE)
    seen: list[dict[str, Any]] = []

    def wait_out_the_settling() -> None:
        seen.append(read_json(world.state.status_path))
        world.clock.advance(25)

    world.run(once=False, max_cycles=2, sleep=advancing(world, wait_out_the_settling))
    assert seen[0]["settling_runs"] == 3 and seen[0]["ready_runs"] == 0
    done = read_json(world.state.status_path)
    assert done["phase"] == "stopped" and done["runs_committed"] == 3 and done["ready_runs"] == 0
    assert done["lag"]["discovery_to_commit_seconds"]["max"] >= 25

def jobfile(job: Path, where: str) -> Path:
    return {
        "job_result": job / "result.json",
        "job_config": job / "config.json",
        "job_provenance": job / "provenance.json",
        "trial_result": job / "a1" / "result.json",
        "trial_config": job / "a1" / "config.json",
        "trial_trajectory": job / "a1" / "agent" / "trajectory.json",
    }[where]

@pytest.mark.parametrize(
    "where",
    [
        "job_result",
        "job_config",
        "job_provenance",
        "trial_result",
        "trial_config",
        "trial_trajectory",
    ],
)
def test_a_split_other_than_dev_anywhere_in_a_job_stops_the_watcher_and_leaves_no_increment(
    world: World, where: str
) -> None:
    job = world.add_job("job-1", STAGE_ONE)
    path = jobfile(job, where)
    original = read_json(path) if path.exists() else None
    write_json(path, {**(original or {}), "split": "test"})
    outcome = world.run()
    assert "error" in outcome and not world.state.folders()
    assert not (world.state_dir / "judge-queue.jsonl").exists()
    if original is None:  # removing the offence is all it takes
        path.unlink()
    else:
        write_json(path, original)
    assert world.run()["committed"] == [1]
    assert world.check()["passed"]

def test_a_task_the_registry_assigns_to_test_stops_the_watcher(world: World) -> None:
    world.registry.tests.add("t-secret")
    world.add_job("job-1", [("s1", "t-secret", False)])
    assert "assigned TEST" in world.run()["error"]
    assert not world.state.folders()

def secret_keys(world: World) -> Path:
    keys = world.tmp / "research" / "answer-keys"
    keys.mkdir(parents=True)
    write_json(keys / "secret.json", atif(CLEAN))
    return keys / "secret.json"

def test_a_link_to_sealed_keys_inside_a_trial_stops_the_watcher(world: World) -> None:
    secret = secret_keys(world)
    job = world.add_job("job-1", STAGE_ONE)
    trajectory = job / "a1" / "agent" / "trajectory.json"
    trajectory.unlink()
    trajectory.symlink_to(secret)
    assert "error" in world.run() and not world.state.folders()

def test_a_hard_link_to_sealed_material_inside_a_trial_stops_the_watcher(world: World) -> None:
    secret = secret_keys(world)
    job = world.add_job("job-1", STAGE_ONE)
    os.link(secret, job / "a1" / "agent" / "notes.json")
    assert "hard link" in world.run()["error"] and not world.state.folders()

def test_a_link_inside_a_trial_stops_the_watcher_even_when_it_points_nowhere(world: World) -> None:
    first = world.add_job("job-1", [("d1", "t-cert", False)])
    (first / "d1" / "agent" / "gone").symlink_to(world.tmp / "nowhere")
    world.add_job("job-2", [("ok1", "t-cert", False)])
    assert "symlink" in world.run()["error"] and not world.state.folders()

def test_a_symlinked_folder_inside_the_manifests_folder_stops_the_watcher(world: World) -> None:
    real = world.tmp / "real"
    real.mkdir()
    write_json(real / "clean.json", atif(CLEAN))
    (world.tmp / "linked").symlink_to(real)
    world.use_manifest([{"id": "r1", "task_id": "t-cert", "path": "linked/clean.json"}])
    assert "link" in world.run()["error"] and not world.state.folders()

@pytest.mark.parametrize(
    "extra", [{"is_cheat": True}, {"meta": {"nested": [{"behavior": "x"}]}}], ids=["top", "nested"]
)
def test_an_evaluator_field_in_a_manifest_row_stops_the_watcher_and_is_never_passed_on(
    world: World, extra: dict[str, Any]
) -> None:
    job = world.add_job("job-1", [("l1", "t-cert", True)])
    world.use_manifest([{"id": "l1", "task_id": "t-cert", "path": str(job / "l1"), **extra}])
    assert "evaluator/label field" in world.run()["error"]
    assert not world.state.folders()

def test_the_rows_the_line_is_given_carry_no_label_fields_and_labels_reach_only_the_scoreboard(
    world: World,
) -> None:
    job = world.add_job("job-1", [("l1", "t-cert", True), ("l2", "t-cert", False)])
    world.use_manifest(
        [
            {"id": n, "task_id": "t-cert", "path": str(job / n), "variant": "x"}
            for n in ("l1", "l2")
        ],
        labels=True,
    )
    assert world.run()["committed"] == [1]
    rows = read_rows(world.state.inc(1) / "inputs" / "rows" / "manifest.jsonl")
    assert all(r["variant"] == "x" for r in rows)  # the row reaches the line as written
    assert not any(set(r) & {"is_cheat", "ground_truth", "label", "split"} for r in rows)
    assert world.scoreboard()["overall"]["cheats"] == 1

def task_status_split(world: World) -> None:
    write_json(world.tmp / "status.json", {"split": "test", "tasks": []})

def labels_split(world: World) -> None:
    world.add_job("job-1", STAGE_ONE)
    write_rows(world.labels, [{"id": "a1", "is_cheat": False, "split": "test"}])

def config_text(world: World, old: str, new: str) -> None:
    text = json.dumps(read_json(world.config_path))
    assert old in text
    world.config_path.write_text(text.replace(old, new))

def cache_split(world: World) -> None:
    cache = world.tmp / "cache"
    write_rows(cache / "review.jsonl", [{"id": "x", "split": "test", "stage_decisions": {}}])
    world.write_config(
        autoreview={"mode": "cached", "cache": str(cache), "max_usd": 0},
        models={"allowed": [ROUTE]},
    )

REFUSED_INPUTS: dict[str, Callable[[World], None]] = {
    "task_status_split": task_status_split,
    "labels_split": labels_split,
    "labels_file_missing": lambda w: w.labels.unlink(),
    "cache_split": cache_split,
    "live_autoreview": lambda w: w.write_config(
        autoreview={
            "mode": "live",
            "route": ROUTE,
            "approval": "x",
            "max_usd": 1,
            "max_call_usd": 0.1,
        },
        models={"allowed": [ROUTE]},
    ),
    "nan_tuning": lambda w: config_text(w, '"poll_seconds": 1', '"poll_seconds": NaN'),
    "infinite_batch": lambda w: config_text(
        w, '"poll_seconds": 1', '"poll_seconds": 1, "max_runs_per_increment": Infinity'
    ),
    "duplicate_split_key": lambda w: config_text(
        w, '"split": "dev"', '"split": "test", "split": "dev"'
    ),
    "other_split": lambda w: w.write_config(split="test"),
    "unsafe_watch_id": lambda w: w.write_config(watch_id="../escape"),
    "unknown_tuning": lambda w: w.write_config(watch={"unknown_knob": 1}),
    "watchdog_age_below_two_polls": lambda w: w.write_config(
        watch={"poll_seconds": 30, "watchdog_max_age_seconds": 40}
    ),
}

@pytest.mark.parametrize("case", sorted(REFUSED_INPUTS))
def test_a_refused_input_creates_no_state_directory_at_all(world: World, case: str) -> None:
    REFUSED_INPUTS[case](world)
    with pytest.raises((ValueError, OSError)):
        world.run()
    assert not world.state_dir.exists()

def test_two_sources_may_not_share_a_corpus_name(world: World) -> None:
    source = read_json(world.config_path)["sources"][0]
    world.write_config(sources=[source, dict(source)])
    with pytest.raises(WatchError, match="unique"):
        world.run()

def test_queue_publication_is_idempotent_and_refuses_a_damaged_file(tmp_path: Path) -> None:
    queue = tmp_path / "q" / "queue.jsonl"
    rows = [
        {"run_id": "r1", "reason": "x", "priority": 1, "source": "line", "reveal": {}},
        {"run_id": "r2", "reason": "y", "priority": 0, "source": "line", "reveal": {}},
    ]
    assert watch_queue.publish(queue, rows) == 2
    assert watch_queue.publish(queue, rows) == 0
    assert watch_queue.publish(queue, [*rows, {**rows[0], "run_id": "r3"}]) == 1
    assert watch_queue.read_queue_ids(queue) == ["r1", "r2", "r3"]
    intact = queue.read_bytes()
    queue.write_bytes(intact + b'{"run_id": "r4", "rea')  # a torn last line from a crashed writer
    with pytest.raises(WatchError, match="not a queue row"):
        watch_queue.publish(queue, [{**rows[0], "run_id": "r9"}])
    assert queue.read_bytes() == intact + b'{"run_id": "r4", "rea'
    queue.write_bytes(intact + intact.splitlines(keepends=True)[0])
    with pytest.raises(WatchError, match="duplicate"):
        watch_queue.publish(queue, [{**rows[0], "run_id": "r9"}])

def test_concurrent_publishers_never_queue_a_run_twice(tmp_path: Path) -> None:
    queue = tmp_path / "queue.jsonl"
    rows = [
        {"run_id": f"r{i}", "reason": "x", "priority": 0, "source": "line", "reveal": {}}
        for i in range(30)
    ]
    added: list[int] = []
    threads = [
        threading.Thread(target=lambda: added.append(watch_queue.publish(queue, rows)))
        for _ in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(added) == 30
    assert Counter(watch_queue.read_queue_ids(queue)) == Counter(r["run_id"] for r in rows)

def test_cached_autoreview_is_written_per_increment_from_the_validated_cache(world: World) -> None:
    cache = world.tmp / "cache"
    verdict = {
        "decision": "pass",
        "reasons": ["AUTOREVIEW_CLEAN"],
        "details": {"model": ROUTE, "attested": True, "suspicion": 3, "verdict": "clean"},
    }
    write_rows(
        cache / "review.jsonl",
        [{"id": n, "stage_decisions": {"autoreview": verdict}} for n, _, _ in STAGE_ONE],
    )
    world.write_config(
        autoreview={"mode": "cached", "cache": str(cache), "max_usd": 0},
        models={"allowed": [ROUTE]},
    )
    world.add_job("job-1", STAGE_ONE)
    world.run()
    world.add_job("job-2", STAGE_TWO)  # no stored reading yet: the line sends these to review
    world.run()
    frozen = read_rows(world.state.inc(1) / "inputs" / "autoreview-cache.jsonl")
    assert sorted(r["id"] for r in frozen) == ["a1", "a2", "a3"]
    assert read_rows(world.state.inc(2) / "inputs" / "autoreview-cache.jsonl") == []
    assert world.check()["passed"]
    write_rows(
        cache / "review.jsonl", []
    )  # the stored cache can change without breaking old receipts
    assert world.check()["passed"]

def test_a_manifest_reward_reaches_the_line_as_written(world: World) -> None:
    world.add_job("job-1", STAGE_ONE)
    rows = [{**row, "reward": 0.5} for row in run_rows(world)]
    manifest = world.tmp / "rows.jsonl"
    write_rows(manifest, rows)
    spec, config = declared_inputs(world, manifest, {})
    write_json(world.tmp / "diff-spec.json", spec)
    batch = core.run_line(world.tmp / "diff-spec.json", world.tmp)
    world.write_config(**config)
    assert world.run()["committed"] == [1]
    expected = {r["run_id"]: r["reward"] for r in read_rows(batch / "runs.jsonl")}
    assert expected == {r["run_id"]: r["reward"] for r in read_rows(world.child() / "runs.jsonl")}
    assert set(expected.values()) == {0.5}
