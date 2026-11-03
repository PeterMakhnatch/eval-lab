"""Refusals happen before side effects; the shared Judge queue is touched last; `line check`
validates a batch before it rewrites anything."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from evallab import hack_drop as hd
from evallab.line import core
from evallab.line.io import read_json, read_rows, write_json, write_rows
from evallab.line.scoreboard import render_scoreboard


def _check(batch: Path, root: Path, name: str) -> dict[str, str]:
    return next(c for c in core.check_batch(batch, root)["checks"] if c["name"] == name)


def _cached_spec(spec_path: Path, rows: list[dict[str, Any]], allowed: list[str]) -> None:
    spec = read_json(spec_path)
    cache = spec_path.parent / "cache"
    write_rows(cache / "review.jsonl", rows)
    spec["autoreview"] = {"mode": "cached", "cache": str(cache), "max_usd": 0}
    spec["models"]["allowed"] = allowed
    write_json(spec_path, spec)


def _reading(run_id: str, cost: float = 0.0) -> dict[str, Any]:
    return {
        "id": run_id,
        "stage_decisions": {
            "autoreview": {
                "decision": "pass",
                "reasons": ["AUTOREVIEW_CLEAN"],
                "details": {
                    "suspicion": 5,
                    "verdict": "clean",
                    "model": "synthetic/model",
                    "attested": True,
                    "cost_usd": cost,
                },
            }
        },
    }


def _soft_l0(monkeypatch: pytest.MonkeyPatch, soft_runs: set[str]) -> None:
    real = hd.evaluate_l0_stage

    def fake(item: hd.RolloutItem, *args: Any, **kwargs: Any) -> hd.StageVerdict:
        if item.item_id in soft_runs:
            return hd.StageVerdict("l0", "review", ["L0_SOFT_TARGET_SURFACE_WRITE"], [], {})
        return real(item, *args, **kwargs)

    monkeypatch.setattr(hd, "evaluate_l0_stage", fake)


def _paths(spec_path: Path) -> tuple[Path, Path, Path]:
    """(batch dir, external queue, station verdicts) the spec points at."""
    spec = read_json(spec_path)
    return (
        Path(spec["out_root"]) / spec["batch_id"],
        Path(spec["judge"]["queue"]),
        Path(spec["judge"]["verdicts"]),
    )


def _assert_no_side_effects(spec_path: Path) -> None:
    batch, queue, _ = _paths(spec_path)
    assert not batch.exists()
    assert not queue.exists()


# ---- a bad Station file is refused before the batch directory or the queue exist -------------


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (b"\xff\xfe\x00\x00", "utf-8"),
        (b"[1, 2, 3]\n", "object"),
        (b'{"run_id": "clean-run", "run_id": "weak-run"}\n', "duplicate JSON key"),
        (b'{"run_id": "clean-run"}\n[1]\n', "object"),
    ],
    ids=["invalid-utf8", "non-object-row", "duplicate-key", "object-then-list"],
)
def test_a_bad_station_verdicts_file_is_refused_before_any_side_effect(
    fixture_spec: Path, body: bytes, match: str
) -> None:
    _, _, verdicts = _paths(fixture_spec)
    verdicts.write_bytes(body)
    with pytest.raises(ValueError, match=match):
        core.run_line(fixture_spec, fixture_spec.parent)
    _assert_no_side_effects(fixture_spec)


def test_a_station_line_that_is_not_json_is_skipped_like_the_station_does(
    fixture_spec: Path,
) -> None:
    _, queue, verdicts = _paths(fixture_spec)
    verdicts.write_bytes(b"this is not json\n\n")
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert read_json(batch / "check.json")["passed"] is True
    assert queue.is_file()


# ---- stored readings on a disallowed route or over the cap are refused up front ---------------


def test_a_stored_reading_on_a_route_outside_models_allowed_is_refused_before_side_effects(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cached_spec(fixture_spec, [_reading("clean-run"), _reading("weak-run")], ["other/model"])
    _soft_l0(monkeypatch, {"clean-run", "weak-run"})
    with pytest.raises(ValueError, match="models.allowed"):
        core.run_line(fixture_spec, fixture_spec.parent)
    _assert_no_side_effects(fixture_spec)


def test_the_same_readings_on_an_allowed_route_run_and_queue(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Control for the two refusals above: only the route/cap differs.
    _cached_spec(fixture_spec, [_reading("clean-run"), _reading("weak-run")], ["synthetic/model"])
    _soft_l0(monkeypatch, {"clean-run", "weak-run"})
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert read_json(batch / "check.json")["passed"] is True


# ---- the external Judge queue is appended only after every check passes ----------------------


def test_a_failed_check_leaves_the_external_judge_queue_untouched(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, queue, _ = _paths(fixture_spec)
    real = core.check_batch
    seen: list[bool] = []

    def failing(
        batch: Path, repo_root: Path, *, queue_pending: bool = False, **kwargs: Any
    ) -> dict[str, Any]:
        result = real(batch, repo_root, queue_pending=queue_pending, **kwargs)
        seen.append(queue_pending)
        if queue_pending:
            result["passed"] = False
        return result

    monkeypatch.setattr(core, "check_batch", failing)
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert seen == [True]  # run_line checks with the queue still pending
    assert read_json(batch / "check.json")["passed"] is False
    assert not queue.exists()


def test_a_passing_run_appends_one_row_per_queue_entry_and_verifies_the_external_queue(
    fixture_spec: Path,
) -> None:
    _, queue, _ = _paths(fixture_spec)
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    own = read_rows(batch / "judge-queue.jsonl")
    assert len(own) == 2
    external = read_rows(queue)
    assert sorted(r["run_id"] for r in external) == sorted(r["run_id"] for r in own)
    assert read_json(batch / "check.json")["passed"] is True
    assert core.check_batch(batch, fixture_spec.parent)["passed"]  # queue_pending is False


def test_an_external_queue_that_fails_verification_after_the_append_fails_judge_routing(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, queue, _ = _paths(fixture_spec)
    monkeypatch.setattr(core, "_external_queue_problem", lambda spec, decisions: "queue is wrong")
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert queue.is_file()  # the append happened; the verification afterwards failed
    saved = read_json(batch / "check.json")
    routing = next(c for c in saved["checks"] if c["name"] == "judge_routing")
    assert routing["status"] == "FAIL" and routing["details"] == "queue is wrong"
    assert saved["passed"] is False
    board = read_json(batch / "scoreboard.json")
    assert next(c for c in board["quality_checks"] if c["name"] == "judge_routing")["status"] == (
        "FAIL"
    )
    assert "queue is wrong" in (batch / "SCOREBOARD.md").read_text()


# ---- Station snapshots are read without following links --------------------------------------


def test_snapshot_station_does_not_follow_a_symlinked_source(tmp_path: Path) -> None:
    real = tmp_path / "real-verdicts.jsonl"
    real.write_text('{"run_id": "x"}\n')
    link = tmp_path / "linked-verdicts.jsonl"
    link.symlink_to(real)
    batch = tmp_path / "batch"
    batch.mkdir()
    with pytest.raises(OSError):
        core.snapshot_station(batch, link)
    assert not list((batch / "evidence" / "station-inputs").glob("*.jsonl"))
    assert core.snapshot_station(batch, real).read_bytes() == real.read_bytes()


# ---- `line check` validates the batch before invalidate_check writes anything -----------------


def _cli(monkeypatch: pytest.MonkeyPatch, root: Path, *argv: str) -> int:
    from evallab import cli

    monkeypatch.setattr(cli, "instrument_openinference", lambda: None)
    return cli.run_cli(["line", *argv], workspace=root)


def test_cli_check_of_a_missing_batch_exits_2_and_creates_nothing(
    fixture_spec: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "no-such-batch"
    assert _cli(monkeypatch, fixture_spec.parent, "check", str(missing)) == 2
    assert not missing.exists()
    assert not list(tmp_path.rglob("check.json"))


def test_cli_check_of_a_batch_with_a_hard_linked_scoreboard_changes_nothing(
    fixture_spec: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    outside = tmp_path / "outside-scoreboard.json"
    os.link(batch / "scoreboard.json", outside)
    check_before = (batch / "check.json").read_bytes()
    board_before = (batch / "scoreboard.json").read_bytes()
    assert _cli(monkeypatch, fixture_spec.parent, "check", str(batch)) == 2
    assert os.stat(batch / "scoreboard.json").st_nlink == 2  # not replaced by a fresh file
    assert (batch / "check.json").read_bytes() == check_before  # no PENDING written first
    assert (batch / "scoreboard.json").read_bytes() == board_before
    assert outside.read_bytes() == board_before


def test_cli_scoreboard_of_a_batch_with_a_hard_link_is_refused(
    fixture_spec: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    os.link(batch / "scoreboard.json", tmp_path / "outside-scoreboard.json")
    assert _cli(monkeypatch, fixture_spec.parent, "scoreboard", str(batch)) == 2


def test_an_extra_file_in_a_batch_is_refused_as_unexpected(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    (batch / "answer-keys-copy.jsonl").write_text('{"secret": 1}\n')
    with pytest.raises(ValueError, match="unexpected file: answer-keys-copy.jsonl"):
        core.check_batch(batch, fixture_spec.parent)
    with pytest.raises(ValueError, match="unexpected"):
        core.preflight_batch(batch)


def test_an_extra_nested_folder_in_a_batch_is_refused_as_unexpected(
    fixture_spec: Path,
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    keys = batch / "research" / "answer-keys"
    keys.mkdir(parents=True)
    (keys / "secret.jsonl").write_text('{"secret": 1}\n')
    with pytest.raises(ValueError, match="unexpected folder: research"):
        core.check_batch(batch, fixture_spec.parent)


def test_cli_check_of_a_batch_with_an_extra_file_changes_nothing(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    (batch / "answer-keys-copy.jsonl").write_text('{"secret": 1}\n')
    check_before = (batch / "check.json").read_bytes()
    assert _cli(monkeypatch, fixture_spec.parent, "check", str(batch)) == 2
    assert (batch / "check.json").read_bytes() == check_before


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_a_fifo_in_a_batch_is_refused_as_a_special_file(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    os.mkfifo(batch / "pipe.jsonl")
    with pytest.raises(ValueError, match="special file"):
        core.check_batch(batch, fixture_spec.parent)


def test_a_batch_path_that_is_a_symlink_to_a_batch_is_refused(
    fixture_spec: Path, tmp_path: Path
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    link = tmp_path / "linked-batch"
    link.symlink_to(batch)
    with pytest.raises(ValueError, match="symlink"):
        core.check_batch(link, fixture_spec.parent)
    with pytest.raises(ValueError, match="symlink"):
        core.invalidate_check(link)


# ---- invalidate_check re-renders the view; the view is compared on version-3 boards ---------


def test_invalidate_check_re_renders_scoreboard_md_with_pending_checks(
    fixture_spec: Path,
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    before = (batch / "SCOREBOARD.md").read_text()
    assert "**PASS**" in before and "**PENDING**" not in before
    core.invalidate_check(batch)
    after = (batch / "SCOREBOARD.md").read_text()
    assert after == render_scoreboard(read_json(batch / "scoreboard.json"))
    assert "**PASS**" not in after
    assert after.count("**PENDING**") == len(core.CHECK_NAMES)
    assert read_json(batch / "check.json")["passed"] is False


def _edit_view(batch: Path) -> None:
    path = batch / "SCOREBOARD.md"
    text = path.read_text()
    edited = text.replace("| Overall | 2 |", "| Overall | 9 |", 1)
    assert edited != text
    path.write_text(edited)


def test_an_edited_scoreboard_view_fails_recompute_on_a_version_3_board(
    fixture_spec: Path,
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert read_json(batch / "scoreboard.json")["schema_version"] == 3
    assert _check(batch, fixture_spec.parent, "scoreboard_recompute")["status"] == "PASS"
    _edit_view(batch)
    result = _check(batch, fixture_spec.parent, "scoreboard_recompute")
    assert result["status"] == "FAIL" and "SCOREBOARD.md differs" in result["details"]


def test_older_scoreboards_do_not_have_their_view_compared(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    board = read_json(batch / "scoreboard.json")
    board["schema_version"] = 2  # keeps the autoreview section; only the view check is skipped
    write_json(batch / "scoreboard.json", board)
    _edit_view(batch)
    result = _check(batch, fixture_spec.parent, "scoreboard_recompute")
    assert result["status"] == "PASS", result["details"]
