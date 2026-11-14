"""Synthetic live-watch review contracts."""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from watch_helpers import (
    I06A,
    I06B,
    I15,
    ROUTE,
    STAGE_ONE,
    World,
    details,
    put_fields,
    reseal,
    trial_rows,
    two_increments,
)

from evallab import cli
from evallab.line import core, watch, watch_queue, watch_rollup
from evallab.line.io import digest, read_json, read_rows, write_json, write_rows
from evallab.line.watch_state import WatchError, owner_lock


def test_a_run_in_two_increments_is_named_even_when_the_hashes_are_resealed(world: World) -> None:
    two_increments(world)
    ids = read_rows(world.child(1) / "runs.jsonl")
    write_rows(world.child(2) / "runs.jsonl", ids)
    edit_commit(world, 2, runs={row["run_id"]: {} for row in ids})  # a consistent record
    reseal(world, 2)
    assert "run in two increments" in details(world, "conservation")

def test_a_commit_record_naming_another_batch_id_fails_conservation(world: World) -> None:
    two_increments(world)
    edit_commit(world, 1, batch_id="live-000009")
    assert "names another batch" in details(world, "conservation")

def test_children_that_disagree_about_a_task_fail_the_cumulative_check(world: World) -> None:
    two_increments(world)
    path = world.child(2) / "tasks.jsonl"
    write_rows(path, [{**r, "weakness": "edited"} for r in read_rows(path)])
    reseal(world, 2)
    assert "disagree about task" in details(world, "cumulative")

def test_a_child_scoreboard_whose_totals_differ_fails_the_cumulative_check(world: World) -> None:
    two_increments(world)
    path = world.child(1) / "scoreboard.json"
    score = read_json(path)
    score["overall"]["cheats"] += 1
    write_json(path, score)
    reseal(world, 1)
    assert "differ from the sums" in details(world, "cumulative")

def test_a_manifest_row_declaring_another_split_stops_the_watcher(world: World) -> None:
    job = world.add_job("job-1", [("l1", "t-cert", False)])
    world.use_manifest(
        [{"id": "l1", "task_id": "t-cert", "path": str(job / "l1"), "split": "test"}]
    )
    assert "error" in world.run() and not world.state.folders()

def test_a_link_directly_inside_a_job_folder_stops_the_watcher(world: World) -> None:
    job = world.add_job("job-1", STAGE_ONE)
    elsewhere = world.tmp / "elsewhere"
    elsewhere.mkdir()
    (job / "extra-trial").symlink_to(elsewhere)
    assert "a link inside job" in world.run()["error"] and not world.state.folders()

def cache_reading(world: World, *, attested: bool, allowed: list[str]) -> None:
    cache = world.tmp / "cache"
    reasons = ["AUTOREVIEW_CLEAN"] if attested else ["AUTOREVIEW_UNATTESTED"]
    reading = {
        "decision": "pass" if attested else "review",
        "reasons": reasons,
        "details": {"model": ROUTE, "attested": attested, "suspicion": 3, "verdict": "clean"},
    }
    write_rows(cache / "review.jsonl", [{"id": "a1", "stage_decisions": {"autoreview": reading}}])
    world.write_config(
        autoreview={"mode": "cached", "cache": str(cache), "max_usd": 0},
        models={"allowed": allowed},
    )

@pytest.mark.parametrize(
    ("attested", "allowed"), [(True, ["other/route"]), (False, [ROUTE])], ids=["route", "attested"]
)
def test_a_cached_reading_from_a_disallowed_or_unattested_model_is_refused_before_any_state(
    world: World, attested: bool, allowed: list[str]
) -> None:
    cache_reading(world, attested=attested, allowed=allowed)
    world.add_job("job-1", STAGE_ONE)
    with pytest.raises(WatchError, match="attested and from models.allowed"):
        world.run()
    assert not world.state_dir.exists()

def test_a_cached_reading_that_is_attested_and_allowed_is_used(world: World) -> None:
    cache_reading(world, attested=True, allowed=[ROUTE])
    world.add_job("job-1", STAGE_ONE)
    assert world.run()["committed"] == [1]
    assert world.check()["passed"]

def test_repeated_failures_keep_each_abandoned_increment_under_its_own_name(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = core.run_line

    def failing(spec: Path, repo_root: Path) -> Path:
        batch = real(spec, repo_root)
        failure = {"name": "determinism", "status": "FAIL", "details": "boom"}
        write_json(batch / "check.json", {"checks": [failure], "passed": False})
        return batch

    monkeypatch.setattr(core, "run_line", failing)
    world.add_job("job-1", STAGE_ONE)
    for _ in range(3):  # the clock never moves: a time-stamped name would repeat and nest
        assert "error" in world.run()
    monkeypatch.setattr(core, "run_line", real)
    assert world.run()["committed"] == [1]
    names = sorted(p.name for p in world.state.abandoned.iterdir())
    assert len(names) == 3 and all(re.fullmatch(r"000001-[0-9a-f]{12}", n) for n in names)
    assert not any((world.state.abandoned / n / "000001").exists() for n in names)

def test_fast_check_refuses_a_state_directory_plainly_and_runs_on_a_child(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.add_job("job-1", STAGE_ONE)
    world.run()
    args = ["line", "check", str(world.state_dir), "--fast", "--json"]
    assert cli.run_cli(args, workspace=world.tmp) == 2
    assert "--fast checks one batch folder" in json.loads(capsys.readouterr().out)["error"]
    child = ["line", "check", str(world.child(1)), "--fast", "--json"]
    assert cli.run_cli(child, workspace=world.tmp) == 3  # nothing failed, determinism not verified
    assert not any(c["status"] == "FAIL" for c in json.loads(capsys.readouterr().out)["checks"])

def test_labels_policy_defaults_to_excluding_attempted_cheats(world: World) -> None:
    config = read_json(world.config_path)
    del config["labels_policy"]
    write_json(world.config_path, config)
    world.add_job("job-1", STAGE_ONE)
    assert world.run()["committed"] == [1]
    assert world.state.config()["line"]["labels_policy"] == {"attempted_cheat": "exclude"}

def test_the_cli_looks_the_line_up_at_call_time_so_a_patch_never_outlives_its_test(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Path] = []

    def stub(spec: Path, repo_root: Path) -> Path:
        seen.append(spec)
        batch = world.tmp / "stub-batch"
        write_json(batch / "check.json", {"checks": [], "passed": True})
        return batch

    monkeypatch.setattr(core, "run_line", stub)
    spec = world.tmp / "spec.json"
    assert cli.run_cli(["line", "run", str(spec), "--json"], workspace=world.tmp) == 0
    assert seen == [spec]  # the CLI used the patched module attribute, not a copy bound at import

def queue_path(world: World) -> Path:
    return world.state_dir / "judge-queue.jsonl"

def queue_damage_deleted(world: World) -> None:
    queue_path(world).unlink()

def queue_damage_extra_run(world: World) -> None:
    row = read_rows(queue_path(world))[0]
    write_rows(queue_path(world), [*read_rows(queue_path(world)), {**row, "run_id": "intruder"}])

def queue_damage_torn(world: World) -> None:
    queue_path(world).write_bytes(queue_path(world).read_bytes() + b'{"run_id": "torn')

def queue_damage_duplicate(world: World) -> None:
    rows = read_rows(queue_path(world))
    write_rows(queue_path(world), [*rows, rows[0]])

def queue_damage_label(world: World) -> None:
    rows = read_rows(queue_path(world))
    write_rows(queue_path(world), [{**rows[0], "is_cheat": True}, *rows[1:]])

def queue_damage_row_changed(world: World) -> None:
    rows = read_rows(queue_path(world))
    write_rows(queue_path(world), [{**rows[0], "reason": "edited"}, *rows[1:]])

def queue_damage_run_dropped(world: World) -> None:
    write_rows(queue_path(world), read_rows(queue_path(world))[1:])

QUEUE_DAMAGE: dict[str, tuple[Callable[[World], None], str]] = {
    "deleted": (queue_damage_deleted, "queue is missing"),
    "extra_run": (queue_damage_extra_run, "queue holds runs no committed increment routed"),
    "torn_line": (queue_damage_torn, "is not a queue row"),
    "duplicate": (queue_damage_duplicate, "twice"),
    "label_field": (queue_damage_label, "label"),
    "row_changed": (queue_damage_row_changed, "differs from the row its increment produced"),
    "run_dropped": (queue_damage_run_dropped, "queue is missing"),
}

@pytest.mark.parametrize("damage", sorted(QUEUE_DAMAGE))
def test_line_check_verifies_the_judge_queue_the_watcher_owns(world: World, damage: str) -> None:
    world.add_job("job-1", STAGE_ONE)
    world.run()
    assert world.queue_ids() and world.check()["passed"]
    apply, message = QUEUE_DAMAGE[damage]
    apply(world)
    assert message in details(world, "conservation"), damage

def test_queue_rows_of_the_one_open_increment_are_tolerated_only_while_its_watcher_runs(
    world: World,
) -> None:
    world.add_job("job-1", STAGE_ONE)
    world.run()
    row = {**read_rows(queue_path(world))[0], "run_id": "next-run"}
    open_batch = world.state.inc(2) / "batch" / "live-000002"
    write_rows(open_batch / "judge-queue.jsonl", [row])
    watch_queue.publish(queue_path(world), [row])  # the watcher queues just before it commits
    with owner_lock(world.state):
        assert world.check()["passed"]
    assert "conservation" in world.failing()  # nobody owns the open increment any more

def test_a_watcher_that_does_not_own_a_queue_has_none_to_verify(world: World) -> None:
    config = read_json(world.config_path)
    config["judge"] = {"queue": str(world.tmp / "elsewhere.jsonl"), "write": False}
    write_json(world.config_path, config)
    world.add_job("job-1", STAGE_ONE)
    assert world.run()["committed"] == [1] and not (world.tmp / "elsewhere.jsonl").exists()
    assert world.check()["passed"]

def edit_watch_json(world: World, **changes: Any) -> None:
    write_json(world.state.watch_json, {**read_json(world.state.watch_json), **changes})

def edit_watch_config(world: World) -> None:
    stored = read_json(world.state.watch_json)
    stored["config"]["audit"] = {"rate": 0.9, "seed": "other"}
    write_json(world.state.watch_json, stored)

def edit_frozen_task_status(world: World) -> None:
    (world.state.frozen / "task-status.json").write_text("{}")

def edit_frozen_status_and_its_recorded_hash(world: World) -> None:
    edit_frozen_task_status(world)
    edit_watch_json(
        world, frozen_task_status_sha256=digest(world.state.frozen / "task-status.json")
    )

WATCH_JSON_DAMAGE: dict[str, tuple[Callable[[World], None], str]] = {
    "schema": (lambda w: edit_watch_json(w, schema="other/v9"), "unknown schema"),
    "recorded_hash": (
        lambda w: edit_watch_json(w, config_sha256="0" * 64),
        "no longer matches its recorded hash",
    ),
    "configuration": (edit_watch_config, "no longer matches its recorded hash"),
    "frozen_task_status": (edit_frozen_task_status, "frozen task status no longer matches"),
    "frozen_status_and_hash": (
        edit_frozen_status_and_its_recorded_hash,
        "committed under another configuration or task status",
    ),
    "commit_config_hash": (
        lambda w: edit_commit(w, 1, config_sha256="0" * 64),
        "committed under another configuration or task status",
    ),
}

@pytest.mark.parametrize("damage", sorted(WATCH_JSON_DAMAGE))
def test_line_check_verifies_watch_json_against_what_each_commit_recorded(
    world: World, damage: str
) -> None:
    two_increments(world)
    apply, message = WATCH_JSON_DAMAGE[damage]
    apply(world)
    assert message in details(world, "conservation")

def test_a_commit_record_with_other_runs_than_its_child_fails_conservation(world: World) -> None:
    two_increments(world)
    edit_commit(world, 1, runs={"x1": {}, "x2": {}, "x3": {}})
    assert "not the child's runs" in details(world, "conservation")

def test_the_cumulative_check_counts_the_runs_each_commit_claims(world: World) -> None:
    two_increments(world)
    path = world.state.inc(1) / "commit.json"
    record = read_json(path)
    write_json(path, {**record, "runs": {**record["runs"], "ghost": {}}})
    assert "does not cover exactly the committed runs" in details(world, "cumulative")

def test_a_commit_naming_another_childs_folder_fails_conservation(world: World) -> None:
    two_increments(world)
    edit_commit(world, 2, batch=watch_rollup.child_path(world.state.config(), 1))
    assert "names another batch" in details(world, "conservation")

def test_a_child_that_is_a_link_to_a_copy_fails_conservation(world: World) -> None:
    two_increments(world)
    child = world.child(1)
    copy = world.tmp / "copy-of-child"
    shutil.move(str(child), str(copy))
    child.symlink_to(copy, target_is_directory=True)
    assert "child batch missing" in details(world, "conservation")

def declared_inputs(world: World, manifest: Path, declared: dict[str, Any]) -> tuple[Any, Any]:
    """The same declared inputs as a line spec and as a watch configuration."""
    labels = str(declared.get("labels", world.labels))
    status = str(declared.get("task_status", world.tmp / "status.json"))
    cache = declared.get("cache")
    autoreview = (
        {"mode": "cached", "cache": str(cache), "max_usd": 0}
        if cache
        else {"mode": "off", "max_usd": 0}
    )
    models = {"allowed": [ROUTE] if cache else []}
    verdicts = declared.get("verdicts")
    spec = {
        "schema": "evallab.line_spec/v1",
        "batch_id": "diff",
        "split": "dev",
        "sources": [{"corpus": "rows", "rollouts": str(manifest), "labels": labels}],
        "task_status": status,
        "autoreview": autoreview,
        "models": models,
        "audit": {"rate": 0.5, "seed": "fixed"},
        "judge": {
            "queue": str(world.tmp / "diff-queue.jsonl"),
            "write": False,
            "verdicts": str(verdicts or world.tmp / "diff-verdicts.jsonl"),
        },
        "labels_policy": {"attempted_cheat": "exclude"},
        "out_root": str(world.tmp / "diff-batches"),
    }
    config: dict[str, Any] = {
        "task_status": status,
        "autoreview": autoreview,
        "models": models,
        "sources": [
            {"corpus": "rows", "kind": "manifest", "path": str(manifest), "labels": labels}
        ],
    }
    if verdicts:
        config["judge"] = {"queue": None, "write": True, "verdicts": str(verdicts)}
    return spec, config

def both(world: World, rows: list[dict[str, Any]] | str, **declared: Any) -> tuple[bool, bool]:
    """(``line run`` refuses, ``line watch`` refuses) for the same manifest and declared inputs."""
    manifest = Path(declared.pop("manifest", world.tmp / "rows.jsonl"))
    if isinstance(rows, str):
        manifest.write_text(rows)
    elif not manifest.is_symlink():
        write_rows(manifest, rows)
    spec, config = declared_inputs(world, manifest, declared)
    write_json(world.tmp / "diff-spec.json", spec)
    try:
        core.run_line(world.tmp / "diff-spec.json", world.tmp)
        run_refuses = False
    except Exception:  # noqa: BLE001 - any refusal counts, whatever its type
        run_refuses = True
    world.write_config(**config)
    try:
        outcome = world.run()
        watch_refuses = "error" in outcome and not world.state.committed()
    except Exception:  # noqa: BLE001 - a refusal raised before the state exists is a refusal
        watch_refuses = True
    return run_refuses, watch_refuses

def test_the_differential_harness_accepts_clean_inputs(world: World) -> None:
    world.add_job("job-1", STAGE_ONE)
    assert both(world, trial_rows(world, ["a1", "a2", "a3"])) == (False, False)

Case = Callable[[World], tuple[list[dict[str, Any]] | str, dict[str, Any]]]

def run_rows(world: World) -> list[dict[str, Any]]:
    return trial_rows(world, ["a1", "a2", "a3"])

def in_a_run_json(where: str, fields: dict[str, Any]) -> Case:
    def build(world: World) -> tuple[list[dict[str, Any]] | str, dict[str, Any]]:
        world.registry.tests.add("t-secret-test")
        put_fields(world.add_job("job-1", STAGE_ONE), where, fields)
        return run_rows(world), {}

    return build

def manifest_text(template: str) -> Case:
    def build(world: World) -> tuple[list[dict[str, Any]] | str, dict[str, Any]]:
        trial = world.add_job("job-1", STAGE_ONE) / "a1"
        return template.replace("TRIAL", str(trial)) + "\n", {}

    return build

def manifest_row(case: str) -> Case:
    def build(world: World) -> tuple[list[dict[str, Any]] | str, dict[str, Any]]:
        world.registry.tests.add("t-secret-test")
        trial = world.add_job("job-1", [("l1", "t-cert", False)]) / "l1"
        return [{"id": "l1", "task_id": "t-cert", "path": str(trial)}, I12_ROWS[case](trial)], {}

    return build

def station_row(row: dict[str, Any]) -> Case:
    def build(world: World) -> tuple[list[dict[str, Any]] | str, dict[str, Any]]:
        world.add_job("job-1", STAGE_ONE)
        verdicts = world.tmp / "verdicts.jsonl"
        write_rows(verdicts, [row])
        return run_rows(world), {"verdicts": verdicts}

    return build

def a_second_copy_that_is_a_test_run(world: World) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    world.registry.tests.add("t-secret-test")
    job = world.add_job("job-1", STAGE_ONE)
    shutil.copytree(job / "a1", world.tmp / "dup")
    write_json(world.tmp / "dup" / "metadata.json", {"split": "test"})
    return [*run_rows(world), {"id": "a1", "task_id": "t-cert", "path": str(world.tmp / "dup")}], {}

def sealed_twin(world: World, content: Path) -> Path:
    """A file inside the sealed keys, with a DEV-looking name's content, and a hard link to it."""
    keys = world.tmp / "research" / "answer-keys"
    keys.mkdir(parents=True, exist_ok=True)
    sealed = keys / f"twin-{content.name}"
    sealed.write_bytes(content.read_bytes())
    link = world.tmp / f"linked-{content.name}"
    os.link(sealed, link)
    return link

def hard_linked(what: str) -> Case:
    def build(world: World) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        world.add_job("job-1", STAGE_ONE)
        rows = run_rows(world)
        if what == "task_status":
            return rows, {"task_status": sealed_twin(world, world.tmp / "status.json")}
        if what == "labels":
            return rows, {"labels": sealed_twin(world, world.labels)}
        if what == "cache":
            cache = world.tmp / "cache"
            cache.mkdir()
            seed = world.tmp / "review.jsonl"
            write_rows(seed, [])
            os.link(sealed_twin(world, seed), cache / "review.jsonl")
            return rows, {"cache": cache}
        raise AssertionError(what)

    return build

def hard_linked_manifest(world: World) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    world.add_job("job-1", STAGE_ONE)
    rows = run_rows(world)
    seed = world.tmp / "seed.jsonl"
    write_rows(seed, rows)
    return rows, {"manifest": sealed_twin(world, seed)}

def symlinked(what: str) -> Case:
    def build(world: World) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        world.add_job("job-1", STAGE_ONE)
        rows = run_rows(world)
        real = {
            "task_status": world.tmp / "status.json",
            "labels": world.labels,
            "manifest": world.tmp / "seed.jsonl",
        }[what]
        if what == "manifest":
            write_rows(real, rows)
        link = world.tmp / f"symlink-{what}.json"
        link.symlink_to(real)
        return rows, {what: link}

    return build

def labels_inside_a_run(world: World) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    job = world.add_job("job-1", STAGE_ONE)
    os.link(world.labels, job / "a1" / "agent" / "labels-copy.jsonl")
    return run_rows(world), {}

DIFFERENTIAL: dict[str, Case] = {
    **{f"i06a {w} {k}": in_a_run_json(w, {k: True}) for w, k in I06A},
    **{f"i06b {w} {sorted(f)[0]}": in_a_run_json(w, f) for w, f in I06B},
    "i09b duplicate split": manifest_text(
        '{"id": "a1", "split": "test", "split": "dev", "task_id": "t-cert", "path": "TRIAL"}'
    ),
    "i09b duplicate path": manifest_text(
        '{"id": "a1", "task_id": "t-cert", "path": "research/answer-keys/s.json", "path": "TRIAL"}'
    ),
    "i09b NaN reward": manifest_text(
        '{"id": "a1", "task_id": "t-cert", "path": "TRIAL", "reward": NaN}'
    ),
    **{f"i12 {name}": manifest_row(name) for name in sorted(I12_ROWS)},
    "i15 split": station_row(I15[0]),
    "i15 is_cheat": station_row(I15[1]),
    "i15 ground_truth": station_row(I15[2]),
    "i18 second copy is a test run": a_second_copy_that_is_a_test_run,
    "i19 task status hard link": hard_linked("task_status"),
    "i19 labels hard link": hard_linked("labels"),
    "i19 cache hard link": hard_linked("cache"),
    "i19 manifest hard link": hard_linked_manifest,
    "p5 task status symlink": symlinked("task_status"),
    "p5 labels symlink": symlinked("labels"),
    "p5 manifest symlink": symlinked("manifest"),
    "p5 labels also inside a run": labels_inside_a_run,
    "i12 homoglyph task": manifest_text(
        '{"id": "a1", "task": {"name": "t-c\\u0435rt"}, "path": "TRIAL"}'
    ),
}

@pytest.mark.parametrize("name", sorted(DIFFERENTIAL))
def test_what_line_run_refuses_line_watch_refuses_too(world: World, name: str) -> None:
    rows, declared = DIFFERENTIAL[name](world)
    run_refuses, watch_refuses = both(world, rows, **declared)
    assert run_refuses and watch_refuses

def test_a_forbidden_copy_of_an_already_committed_run_stops_the_watcher(world: World) -> None:
    job = world.add_job("job-1", STAGE_ONE)
    assert world.run()["committed"] == [1]
    dup = world.results / "2026-11-02" / "job-2"
    shutil.copytree(job / "a1", dup / "a1")  # the id of a committed run, now a TEST run
    write_json(dup / "a1" / "metadata.json", {"split": "test"})
    write_json(
        dup / "result.json",
        {"n_total_trials": 1, "stats": {}, "finished_at": "2026-11-02T11:00:00+00:00"},
    )
    write_json(dup / "provenance.json", {"source_path": str(dup)})
    outcome = world.run()
    assert "split" in outcome["error"]
    assert [r["n"] for r in world.state.committed()] == [1]

@pytest.mark.parametrize(
    "tamper",
    [
        {"runs": {"a1": "x", "a2": "x", "a3": "x"}},
        {"times": "x"},
        {"split": "test"},
        {"is_cheat": True},
    ],
    ids=["runs not objects", "times not an object", "split", "label field"],
)
def test_a_malformed_commit_record_fails_the_check_and_never_crashes_the_watcher(
    world: World, tamper: dict[str, Any]
) -> None:
    world.add_job("job-1", STAGE_ONE)
    world.run()
    edit_commit(world, 1, **tamper)
    assert "conservation" in world.failing()
    outcome = world.run()  # recorded as a failure, not raised
    assert "error" in outcome
    assert read_json(world.state.status_path)["phase"] == "failed"
    assert world.state.errors_path.is_file()

def test_a_configuration_that_is_a_hard_link_to_sealed_material_is_refused(world: World) -> None:
    sealed = sealed_twin(world, world.config_path)
    world.add_job("job-1", STAGE_ONE)
    with pytest.raises(ValueError, match="sealed"):
        watch.run_watch(sealed, world.state_dir, world.tmp, once=True)
    assert not world.state_dir.exists()

@pytest.mark.parametrize("damage", ["a repeated key", "NaN"])
def test_a_queue_row_with_a_repeated_key_or_nan_fails_the_live_check(
    world: World, damage: str
) -> None:
    world.add_job("job-1", STAGE_ONE)
    world.run()
    queue = world.state_dir / "judge-queue.jsonl"
    lines = queue.read_text().splitlines()
    first = lines[0]
    lines[0] = (
        '{"run_id": "evil", ' + first[1:]
        if damage == "a repeated key"
        else first[:-1] + ', "extra": NaN}'
    )
    queue.write_text("\n".join(lines) + "\n")
    assert "conservation" in world.failing()
