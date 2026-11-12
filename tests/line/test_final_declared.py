"""Declared inputs: a final symlink is refused before it is resolved; scoreboard and replay read
inside the line's sealed scope; the external Judge queue is read and refused before any write;
``models.allowed`` cannot name ``unattested``.

Every refusal below happens before a batch directory exists.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from evallab import hack_drop as hd
from evallab.line import core
from evallab.line.io import read_json, read_rows, write_json, write_rows
from evallab.line.replay import replay
from evallab.line.scoreboard import build_scoreboard
from evallab.line.spec import load_spec

SYMLINK = "declared input is a symlink"
SEALED = "hard link to sealed material"
KINDS = ["spec", "rollouts", "labels", "task_status", "cache", "verdicts", "queue"]
RELATIVE_KINDS = ["rollouts", "labels", "task_status", "cache", "verdicts", "queue"]
ROUTE = "synthetic/model"


# ---- helpers -----------------------------------------------------------------------------------


def _not_eligible(run_id: str) -> dict[str, Any]:
    return {
        "id": run_id,
        "stage_decisions": {
            "autoreview": {
                "decision": "pass",
                "reasons": ["AUTOREVIEW_NOT_ELIGIBLE"],
                "details": {"eligible": False},
            }
        },
    }


def _declared(spec_path: Path, kind: str) -> Path:
    """The path of one declared input; cache, verdicts and queue are created on demand."""
    spec = read_json(spec_path)
    if kind == "spec":
        return spec_path
    if kind == "rollouts":
        return Path(spec["sources"][0]["rollouts"])
    if kind == "labels":
        return Path(spec["sources"][0]["labels"])
    if kind == "task_status":
        return Path(spec["task_status"])
    if kind == "verdicts":
        path = Path(spec["judge"]["verdicts"])
        path.write_text("")
        return path
    if kind == "queue":
        path = Path(spec["judge"]["queue"])
        path.write_text("")
        return path
    assert kind == "cache"
    path = spec_path.parent / "cache.jsonl"
    write_rows(path, [_not_eligible("clean-run"), _not_eligible("weak-run")])
    spec["autoreview"] = {"mode": "cached", "cache": str(path), "max_usd": 0}
    spec["models"]["allowed"] = [ROUTE]
    write_json(spec_path, spec)
    return path


def _relativize(spec_path: Path) -> None:
    """Spell every declared path relative to the spec's own folder."""
    spec = read_json(spec_path)

    def rel(value: str) -> str:
        return os.path.relpath(value, spec_path.parent)

    spec["sources"][0]["rollouts"] = rel(spec["sources"][0]["rollouts"])
    spec["sources"][0]["labels"] = rel(spec["sources"][0]["labels"])
    spec["task_status"] = rel(spec["task_status"])
    if spec["autoreview"].get("cache"):
        spec["autoreview"]["cache"] = rel(spec["autoreview"]["cache"])
    spec["judge"]["queue"] = rel(spec["judge"]["queue"])
    spec["judge"]["verdicts"] = rel(spec["judge"]["verdicts"])
    write_json(spec_path, spec)


def _symlinked(path: Path) -> Path:
    """Replace ``path`` by a symlink to a byte-identical copy; return the copy."""
    real = path.with_name(path.name + ".real")
    path.rename(real)
    path.symlink_to(real)
    assert path.is_symlink()
    return real


def _out_root(spec_path: Path) -> Path:
    return Path(read_json(spec_path)["out_root"])


def _sealed(root: Path) -> Path:
    """A sealed folder under the repo root the line runs against, with one file in it."""
    secret = root / "research" / "answer-keys" / "secret.jsonl"
    secret.parent.mkdir(parents=True)
    secret.write_text('{"id": "clean-run", "is_cheat": true}\n')
    return secret


def _hard_link_to_sealed(path: Path, secret: Path) -> None:
    """Make ``path`` another name of the sealed file, with the bytes ``path`` had."""
    secret.write_bytes(path.read_bytes())  # identical bytes: only the link can be the reason
    path.unlink()
    os.link(secret, path)
    assert os.stat(path).st_ino == os.stat(secret).st_ino


def _cli(monkeypatch: pytest.MonkeyPatch, root: Path, *argv: str) -> int:
    from evallab import cli as top_cli

    monkeypatch.setattr(top_cli, "instrument_openinference", lambda: None)
    return top_cli.run_cli(["line", *argv], workspace=root)


def _edit_spec(spec_path: Path, edit: Callable[[dict[str, Any]], None]) -> None:
    spec = read_json(spec_path)
    edit(spec)
    write_json(spec_path, spec)


# ---- (a) a declared input that is a symlink ----------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_a_symlinked_declared_input_is_refused_before_any_batch_exists(
    fixture_spec: Path, kind: str
) -> None:
    target = _declared(fixture_spec, kind)
    out_root = _out_root(fixture_spec)
    _symlinked(target)
    with pytest.raises(ValueError, match=SYMLINK):
        core.run_line(fixture_spec, fixture_spec.parent)
    with pytest.raises(ValueError, match=SYMLINK):
        load_spec(fixture_spec)
    assert not out_root.exists()


@pytest.mark.parametrize("kind", KINDS)
def test_the_same_layout_without_the_symlink_runs(fixture_spec: Path, kind: str) -> None:
    _declared(fixture_spec, kind)
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert read_json(batch / "check.json")["passed"]
    assert core.check_batch(batch, fixture_spec.parent)["passed"]


@pytest.mark.parametrize("kind", RELATIVE_KINDS)
def test_a_relative_declared_path_that_is_a_symlink_is_refused(
    fixture_spec: Path, kind: str
) -> None:
    target = _declared(fixture_spec, kind)
    _relativize(fixture_spec)
    out_root = _out_root(fixture_spec)
    assert not Path(read_json(fixture_spec)["sources"][0]["labels"]).is_absolute()
    _symlinked(target)
    with pytest.raises(ValueError, match=SYMLINK):
        core.run_line(fixture_spec, fixture_spec.parent)
    assert not out_root.exists()


def test_relative_paths_without_a_symlink_run(fixture_spec: Path) -> None:
    _declared(fixture_spec, "cache")
    _relativize(fixture_spec)
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert read_json(batch / "check.json")["passed"]


def test_a_fully_symlinked_spec_and_inputs_cannot_run(fixture_spec: Path) -> None:
    # The review's repro: resolving first and reading after would hand the reader real files.
    out_root = _out_root(fixture_spec)
    for kind in ("rollouts", "labels", "task_status", "verdicts", "spec"):
        _symlinked(_declared(fixture_spec, kind))
    with pytest.raises(ValueError, match=SYMLINK):
        core.run_line(fixture_spec, fixture_spec.parent)
    assert not out_root.exists()


def test_a_spec_whose_inputs_are_all_symlinks_names_the_first_it_reads(
    fixture_spec: Path,
) -> None:
    # With the spec file itself real, each input link is refused when load_spec reaches it.
    for kind in ("rollouts", "labels", "task_status", "verdicts"):
        _symlinked(_declared(fixture_spec, kind))
    out_root = _out_root(fixture_spec)
    with pytest.raises(ValueError, match=SYMLINK + ".*rollouts.jsonl"):
        load_spec(fixture_spec)
    with pytest.raises(ValueError, match=SYMLINK):
        core.run_line(fixture_spec, fixture_spec.parent)
    assert not out_root.exists()


def test_a_symlink_in_a_parent_directory_is_still_followed(
    fixture_spec: Path, tmp_path: Path
) -> None:
    """Documented limitation, pinned as it is: only the FINAL component is refused."""
    real_dir = tmp_path / "real-inputs"
    real_dir.mkdir()
    labels = _declared(fixture_spec, "labels")
    moved = real_dir / "labels.jsonl"
    labels.rename(moved)
    (tmp_path / "linked-inputs").symlink_to(real_dir, target_is_directory=True)
    _edit_spec(
        fixture_spec,
        lambda spec: spec["sources"][0].update(
            labels=str(tmp_path / "linked-inputs" / "labels.jsonl")
        ),
    )
    spec = load_spec(fixture_spec)
    # The directory link is resolved away; the declared path is the real file.
    assert spec["sources"][0]["labels"] == str(moved.resolve())
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert read_json(batch / "check.json")["passed"]


def test_an_out_root_that_is_a_symlink_is_not_refused(fixture_spec: Path, tmp_path: Path) -> None:
    real = tmp_path / "real-batches"
    real.mkdir()
    link = tmp_path / "linked-batches"
    link.symlink_to(real, target_is_directory=True)
    _edit_spec(fixture_spec, lambda spec: spec.update(out_root=str(link)))
    assert load_spec(fixture_spec)["out_root"] == str(real.resolve())
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert batch.parent == real.resolve()
    assert read_json(batch / "check.json")["passed"]


def test_cli_check_of_a_symlink_to_a_batch_is_refused(
    fixture_spec: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    link = tmp_path / "linked-batch"
    link.symlink_to(batch, target_is_directory=True)
    before = (batch / "check.json").read_bytes()
    assert _cli(monkeypatch, fixture_spec.parent, "check", str(link)) == 2
    assert (batch / "check.json").read_bytes() == before


def test_the_line_cli_run_of_a_symlinked_input_exits_2_and_writes_nothing(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _symlinked(_declared(fixture_spec, "labels"))
    out_root = _out_root(fixture_spec)
    assert _cli(monkeypatch, fixture_spec.parent, "run", str(fixture_spec)) == 2
    assert SYMLINK in capsys.readouterr().out
    assert not out_root.exists()


# ---- (b) scoreboard and replay read inside the sealed scope ------------------------------------


def test_scoreboard_of_a_batch_whose_labels_are_a_sealed_hard_link_is_refused(
    fixture_spec: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = fixture_spec.parent
    secret = _sealed(root)
    batch = core.run_line(fixture_spec, root)
    # Control: the sealed folder exists but nothing is linked to it.
    labels = _declared(fixture_spec, "labels")
    _hard_link_to_sealed(labels, secret)
    with pytest.raises(ValueError, match=SEALED):
        build_scoreboard(batch, root)


def test_scoreboard_of_a_batch_whose_labels_are_a_symlink_is_refused(
    fixture_spec: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = fixture_spec.parent
    batch = core.run_line(fixture_spec, root)
    _symlinked(_declared(fixture_spec, "labels"))
    with pytest.raises(ValueError, match=SYMLINK):
        build_scoreboard(batch, root)


def test_replay_of_a_frozen_spec_that_is_a_sealed_hard_link_is_refused(
    fixture_spec: Path, tmp_path: Path
) -> None:
    root = fixture_spec.parent
    secret = _sealed(root)
    baseline = tmp_path / "baseline.json"
    # Control: the same spec, not linked, writes a baseline.
    result, code = replay(fixture_spec, baseline, root, write_baseline=True)
    assert code == 0, result
    baseline.unlink()
    _hard_link_to_sealed(fixture_spec, secret)
    result, code = replay(fixture_spec, baseline, root, write_baseline=True)
    assert code == 2 and not result["passed"]
    assert SEALED in result["error"]
    assert not baseline.exists()


def test_replay_of_a_baseline_that_is_a_sealed_hard_link_is_refused(
    fixture_spec: Path, tmp_path: Path
) -> None:
    root = fixture_spec.parent
    secret = _sealed(root)
    baseline = tmp_path / "baseline.json"
    result, code = replay(fixture_spec, baseline, root, write_baseline=True)
    assert code == 0, result
    result, code = replay(fixture_spec, baseline, root)
    assert code == 0, result  # control: the unlinked baseline replays
    _hard_link_to_sealed(baseline, secret)
    before = secret.read_bytes()
    result, code = replay(fixture_spec, baseline, root)
    assert code == 2 and not result["passed"]
    assert SEALED in result["error"]
    assert secret.read_bytes() == before


def test_replay_of_a_symlinked_baseline_is_refused_on_the_read_path(
    fixture_spec: Path, tmp_path: Path
) -> None:
    root = fixture_spec.parent
    baseline = tmp_path / "baseline.json"
    assert replay(fixture_spec, baseline, root, write_baseline=True)[1] == 0
    real = _symlinked(baseline)
    before = real.read_bytes()
    result, code = replay(fixture_spec, baseline, root)
    assert code == 2 and not result["passed"]
    assert SYMLINK in result["error"]
    assert real.read_bytes() == before


def test_replay_write_baseline_through_a_symlink_is_refused_and_leaves_the_target(
    fixture_spec: Path, tmp_path: Path
) -> None:
    root = fixture_spec.parent
    target = tmp_path / "baseline-target.json"
    target.write_text('{"keep": "me"}\n')
    baseline = tmp_path / "baseline.json"
    baseline.symlink_to(target)
    result, code = replay(fixture_spec, baseline, root, write_baseline=True)
    assert code == 2 and not result["passed"]
    assert SYMLINK in result["error"]
    assert baseline.is_symlink()
    assert target.read_text() == '{"keep": "me"}\n'


def test_replay_of_a_symlinked_frozen_spec_is_refused(fixture_spec: Path, tmp_path: Path) -> None:
    root = fixture_spec.parent
    _symlinked(fixture_spec)
    baseline = tmp_path / "baseline.json"
    result, code = replay(fixture_spec, baseline, root, write_baseline=True)
    assert code == 2 and SYMLINK in result["error"]
    assert not baseline.exists()


# ---- (c) the external Judge queue --------------------------------------------------------------


def _set_write(spec_path: Path, write: bool) -> None:
    _edit_spec(spec_path, lambda spec: spec["judge"].update(write=write))


def _assert_queue_refused(spec_path: Path, queue: Path, pattern: str) -> None:
    """``run_line`` refuses with ``pattern``; no batch, no out_root; the queue is untouched."""
    before = queue.read_bytes()
    with pytest.raises(ValueError, match=pattern):
        core.run_line(spec_path, spec_path.parent)
    assert not _out_root(spec_path).exists()
    assert not (_out_root(spec_path) / read_json(spec_path)["batch_id"]).exists()
    assert queue.read_bytes() == before


@pytest.mark.parametrize("write", [True, False])
def test_a_queue_that_is_a_sealed_hard_link_is_refused_and_left_byte_identical(
    fixture_spec: Path, write: bool
) -> None:
    secret = _sealed(fixture_spec.parent)
    queue = _declared(fixture_spec, "queue")
    _set_write(fixture_spec, write)
    secret.write_text('{"run_id": "unrelated", "is_cheat": true}\n')
    queue.unlink()
    os.link(secret, queue)
    assert os.stat(queue).st_ino == os.stat(secret).st_ino
    before = secret.read_bytes()
    _assert_queue_refused(fixture_spec, queue, SEALED)
    assert secret.read_bytes() == before


@pytest.mark.parametrize("write", [True, False])
def test_a_queue_row_without_a_run_id_is_refused_before_any_batch_exists(
    fixture_spec: Path, write: bool
) -> None:
    queue = _declared(fixture_spec, "queue")
    _set_write(fixture_spec, write)
    queue.write_text('{"run_id": "other-run"}\n{"note": "no id here"}\n')
    _assert_queue_refused(fixture_spec, queue, "external Judge queue row has no run_id")


@pytest.mark.parametrize("bad", ['{"run_id": 7}', '{"run_id": null}', '{"run_id": ["clean-run"]}'])
def test_a_queue_row_whose_run_id_is_not_text_is_refused(fixture_spec: Path, bad: str) -> None:
    queue = _declared(fixture_spec, "queue")
    queue.write_text(bad + "\n")
    _assert_queue_refused(fixture_spec, queue, "external Judge queue row has no run_id")


def test_a_queue_holding_one_of_the_batch_runs_twice_is_refused_when_the_batch_appends(
    fixture_spec: Path,
) -> None:
    queue = _declared(fixture_spec, "queue")
    write_rows(
        queue,
        [{"run_id": "clean-run"}, {"run_id": "other-run"}, {"run_id": "clean-run"}],
    )
    _assert_queue_refused(fixture_spec, queue, "holds a run twice: clean-run")


def test_the_same_doubled_queue_is_not_this_batchs_business_when_it_does_not_append(
    fixture_spec: Path,
) -> None:
    queue = _declared(fixture_spec, "queue")
    _set_write(fixture_spec, False)
    write_rows(queue, [{"run_id": "clean-run"}, {"run_id": "clean-run"}])
    before = queue.read_bytes()
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert queue.read_bytes() == before
    assert (batch / "check.json").is_file()


def test_a_doubled_run_that_is_not_in_this_batch_is_fine(fixture_spec: Path) -> None:
    queue = _declared(fixture_spec, "queue")
    write_rows(queue, [{"run_id": "elsewhere"}, {"run_id": "elsewhere"}])
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert read_json(batch / "check.json")["passed"]
    assert [r["run_id"] for r in read_rows(queue)].count("elsewhere") == 2


def test_a_queue_that_already_holds_the_runs_once_is_idempotent(fixture_spec: Path) -> None:
    queue = Path(read_json(fixture_spec)["judge"]["queue"])
    first = core.run_line(fixture_spec, fixture_spec.parent)
    assert read_json(first / "check.json")["passed"]
    once = queue.read_bytes()
    runs = sorted(r["run_id"] for r in read_rows(queue))
    assert runs == ["clean-run", "weak-run"]
    _edit_spec(fixture_spec, lambda spec: spec.update(batch_id="second"))
    second = core.run_line(fixture_spec, fixture_spec.parent)
    assert second != first
    assert read_json(second / "check.json")["passed"]
    assert queue.read_bytes() == once  # appended nothing, and one row per run remains
    assert core.check_batch(second, fixture_spec.parent)["passed"]


def test_a_queue_that_is_a_symlink_is_refused_and_its_target_untouched(
    fixture_spec: Path,
) -> None:
    queue = _declared(fixture_spec, "queue")
    write_rows(queue, [{"run_id": "elsewhere"}])
    real = _symlinked(queue)
    before = real.read_bytes()
    with pytest.raises(ValueError, match=SYMLINK):
        core.run_line(fixture_spec, fixture_spec.parent)
    assert not _out_root(fixture_spec).exists()
    assert real.read_bytes() == before


# ---- (c, live) the append-time recheck ---------------------------------------------------------


@pytest.fixture
def live_spec(fixture_spec: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    steps = [
        {"step_id": 0, "source": "user", "message": "Fix the bug"},
        {
            "step_id": 1,
            "source": "assistant",
            "tool_calls": [{"name": "write", "arguments": {"path": "a.py", "content": "x = 1\n"}}],
        },
    ]
    rows = []
    for index in (1, 2):
        trajectory = fixture_spec.parent / f"t{index}.json"
        write_json(trajectory, {"steps": steps})
        rows.append({"id": f"run-{index}", "task_id": "plain-task", "path": str(trajectory)})
    spec = read_json(fixture_spec)
    write_rows(Path(spec["sources"][0]["rollouts"]), rows)
    write_rows(Path(spec["sources"][0]["labels"]), [])
    spec["autoreview"] = {
        "mode": "live",
        "route": ROUTE,
        "approval": "synthetic test",
        "max_usd": 1.0,
        "max_call_usd": 0.6,
    }
    spec["models"]["allowed"] = [ROUTE]
    write_json(fixture_spec, spec)
    soft = hd.StageVerdict("l0", "review", ["L0_SOFT_TARGET_SURFACE_WRITE"], [], {})
    monkeypatch.setattr(hd, "evaluate_l0_stage", lambda _item: soft)

    def fake(prompt: str, *, model: str, **_: Any) -> dict[str, Any]:
        return {
            "stdout": json.dumps({"suspicion": 10, "verdict": "clean"}),
            "served_models": [model],
            "n_fallbacks": 0,
            "usage": {"usd": 0.1},
        }

    monkeypatch.setattr(hd, "run_omp_session", fake)
    return fixture_spec


def test_a_live_batch_appends_to_a_clean_queue(live_spec: Path) -> None:
    queue = Path(read_json(live_spec)["judge"]["queue"])
    batch = core.run_line(live_spec, live_spec.parent)
    assert read_json(batch / "check.json")["passed"]
    assert sorted(r["run_id"] for r in read_rows(queue)) == ["run-1", "run-2"]


def test_a_queue_that_turns_bad_after_the_checks_fails_judge_routing_and_is_not_appended(
    live_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = Path(read_json(live_spec)["judge"]["queue"])
    assert not queue.exists()
    real = core.check_batch
    seen: list[bool] = []

    def check_then_double_a_run(batch: Path, repo_root: Path, **kwargs: Any) -> dict[str, Any]:
        result = real(batch, repo_root, **kwargs)
        if kwargs.get("queue_pending"):
            # The checks have passed; the shared queue changes before the append.
            seen.append(result["passed"])
            write_rows(queue, [{"run_id": "run-1"}, {"run_id": "run-1"}])
        return result

    monkeypatch.setattr(core, "check_batch", check_then_double_a_run)
    batch = core.run_line(live_spec, live_spec.parent)
    assert seen == [True]  # the batch itself was fine; only the queue changed
    saved = read_json(batch / "check.json")
    routing = next(c for c in saved["checks"] if c["name"] == "judge_routing")
    assert routing["status"] == "FAIL" and "holds a run twice: run-1" in routing["details"]
    assert saved["passed"] is False
    assert [r["run_id"] for r in read_rows(queue)] == ["run-1", "run-1"]  # nothing appended
    board = read_json(batch / "scoreboard.json")
    assert next(c for c in board["quality_checks"] if c["name"] == "judge_routing")["status"] == (
        "FAIL"
    )


def test_a_queue_that_turns_into_sealed_material_after_the_checks_is_not_appended(
    live_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = _sealed(live_spec.parent)
    queue = Path(read_json(live_spec)["judge"]["queue"])
    real = core.check_batch

    def check_then_link(batch: Path, repo_root: Path, **kwargs: Any) -> dict[str, Any]:
        result = real(batch, repo_root, **kwargs)
        if kwargs.get("queue_pending"):
            os.link(secret, queue)
        return result

    monkeypatch.setattr(core, "check_batch", check_then_link)
    before = secret.read_bytes()
    batch = core.run_line(live_spec, live_spec.parent)
    routing = next(
        c for c in read_json(batch / "check.json")["checks"] if c["name"] == "judge_routing"
    )
    assert routing["status"] == "FAIL" and SEALED in routing["details"]
    assert secret.read_bytes() == before


# ---- (d) models.allowed may not name `unattested` ----------------------------------------------


@pytest.mark.parametrize("allowed", [["unattested"], [ROUTE, "unattested"], ["unattested", ROUTE]])
def test_models_allowed_may_not_list_unattested(fixture_spec: Path, allowed: list[str]) -> None:
    _edit_spec(fixture_spec, lambda spec: spec["models"].update(allowed=allowed))
    with pytest.raises(ValueError, match="models.allowed may not list 'unattested'"):
        load_spec(fixture_spec)
    with pytest.raises(ValueError, match="models.allowed may not list 'unattested'"):
        core.run_line(fixture_spec, fixture_spec.parent)
    assert not _out_root(fixture_spec).exists()


def test_a_live_spec_cannot_make_unattested_its_route(fixture_spec: Path) -> None:
    _edit_spec(
        fixture_spec,
        lambda spec: (
            spec.update(
                autoreview={
                    "mode": "live",
                    "route": "unattested",
                    "approval": "synthetic test",
                    "max_usd": 1.0,
                    "max_call_usd": 0.5,
                }
            ),
            spec["models"].update(allowed=["unattested"]),
        ),
    )
    with pytest.raises(ValueError, match="may not list 'unattested'"):
        load_spec(fixture_spec)


def test_other_route_names_are_still_allowed(fixture_spec: Path) -> None:
    _edit_spec(fixture_spec, lambda spec: spec["models"].update(allowed=[ROUTE, "unattested-ish"]))
    assert load_spec(fixture_spec)["models"]["allowed"] == [ROUTE, "unattested-ish"]
