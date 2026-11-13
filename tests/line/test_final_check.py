"""`line check` compares the saved view before rewriting it; station-input snapshots, receipt
shapes and code revisions are closed; the live budget walk and the cached non-reading outcomes
cannot be forged."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from evallab import cli
from evallab import hack_drop as hd
from evallab.line import core
from evallab.line.io import read_json, read_rows, write_json, write_rows
from evallab.line.scoreboard import render_scoreboard

ROUTE = "synthetic/model"
OTHER_ROUTE = "synthetic/other"


# ---- shared helpers ----------------------------------------------------------------------------


def _check(batch: Path, root: Path, name: str) -> dict[str, str]:
    return next(c for c in core.check_batch(batch, root)["checks"] if c["name"] == name)


def _failed(batch: Path, root: Path) -> dict[str, str]:
    return {
        c["name"]: c["details"]
        for c in core.check_batch(batch, root)["checks"]
        if c["status"] != "PASS"
    }


def _cli(monkeypatch: pytest.MonkeyPatch, root: Path, *argv: str) -> int:
    return cli.run_cli(["line", *argv], workspace=root)


def _edit_receipt(batch: Path, stage: str, edit: Callable[[dict[str, Any]], None]) -> None:
    path = batch / "receipts" / f"{stage}.json"
    receipt = read_json(path)
    edit(receipt)
    write_json(path, receipt)


def _checks(batch: Path) -> dict[str, dict[str, str]]:
    return {c["name"]: c for c in read_json(batch / "check.json")["checks"]}


# ---- (a) `line check` and the saved SCOREBOARD.md ----------------------------------------------


def test_cli_check_of_an_untouched_batch_exits_0(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert _cli(monkeypatch, fixture_spec.parent, "check", str(batch)) == 0
    saved = read_json(batch / "check.json")
    assert saved["passed"] is True
    assert {c["status"] for c in saved["checks"]} == {"PASS"}
    board = read_json(batch / "scoreboard.json")
    assert (batch / "SCOREBOARD.md").read_text() == render_scoreboard(board)


def _rewrite_text(batch: Path) -> None:
    (batch / "SCOREBOARD.md").write_text("# Everything passed, trust me\n")


def _delete_view(batch: Path) -> None:
    (batch / "SCOREBOARD.md").unlink()


def _not_utf8(batch: Path) -> None:
    (batch / "SCOREBOARD.md").write_bytes(b"\xff\xfe\x00 not utf-8 \x80")


VIEW_TAMPERS = [
    pytest.param(_rewrite_text, "differs", id="rewritten"),
    pytest.param(_delete_view, "missing", id="deleted"),
    pytest.param(_not_utf8, "unreadable", id="not-utf8"),
]


@pytest.mark.parametrize(("tamper", "view"), VIEW_TAMPERS)
def test_cli_check_of_a_tampered_view_exits_2_and_fails_recompute(
    fixture_spec: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: Callable[[Path], None],
    view: str,
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    tamper(batch)
    assert _cli(monkeypatch, fixture_spec.parent, "check", str(batch)) == 2
    assert batch.is_dir()
    saved = read_json(batch / "check.json")
    assert saved["passed"] is False
    assert not {c["status"] for c in saved["checks"]} <= {"PENDING"}
    recompute = _checks(batch)["scoreboard_recompute"]
    assert recompute["status"] == "FAIL"
    assert f"SCOREBOARD.md was {view} before this check" in recompute["details"]
    # Only the view gate fails: the data behind it is intact.
    assert {n for n, c in _checks(batch).items() if c["status"] != "PASS"} == {
        "scoreboard_recompute"
    }


@pytest.mark.parametrize(("tamper", "view"), VIEW_TAMPERS)
def test_invalidate_check_records_the_view_state_and_check_batch_then_fails(
    fixture_spec: Path, tamper: Callable[[Path], None], view: str
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    root = fixture_spec.parent
    assert "scoreboard_view" not in read_json(batch / "check.json")
    board_bytes = (batch / "scoreboard.json").read_bytes()
    tamper(batch)
    tampered = _view_bytes(batch)
    found = core.invalidate_check(batch)
    pending = read_json(batch / "check.json")
    assert pending["scoreboard_view"] == view
    assert pending["passed"] is False
    assert {c["status"] for c in pending["checks"]} == {"PENDING"}
    # A bad view is neither repaired nor re-rendered: both files stay exactly as found, and
    # what was found is what invalidate_check returns for the later restore.
    assert (batch / "scoreboard.json").read_bytes() == board_bytes
    assert _view_bytes(batch) == tampered
    assert found.files == {"scoreboard.json": board_bytes, "SCOREBOARD.md": tampered}
    assert found.view == view
    result = _check(batch, root, "scoreboard_recompute")
    assert result["status"] == "FAIL"
    assert f"SCOREBOARD.md was {view} before this check" in result["details"]


def _view_bytes(batch: Path) -> bytes | None:
    path = batch / "SCOREBOARD.md"
    return path.read_bytes() if path.exists() else None


def test_invalidate_check_of_an_untouched_batch_returns_the_original_bytes(
    fixture_spec: Path,
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    board_bytes = (batch / "scoreboard.json").read_bytes()
    view_bytes = (batch / "SCOREBOARD.md").read_bytes()
    found = core.invalidate_check(batch)
    assert found.view == "ok"
    assert found.files == {"scoreboard.json": board_bytes, "SCOREBOARD.md": view_bytes}


def test_invalidate_check_of_an_untouched_batch_records_an_ok_view(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    core.invalidate_check(batch)
    assert read_json(batch / "check.json")["scoreboard_view"] == "ok"
    assert _check(batch, fixture_spec.parent, "scoreboard_recompute")["status"] == "PASS"


@pytest.mark.parametrize(("tamper", "view"), VIEW_TAMPERS)
def test_a_failed_check_never_repairs_the_view_so_a_second_fails_too_until_it_is_restored(
    fixture_spec: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: Callable[[Path], None],
    view: str,
) -> None:
    root = fixture_spec.parent
    batch = core.run_line(fixture_spec, root)
    original = (batch / "SCOREBOARD.md").read_bytes()
    board_bytes = (batch / "scoreboard.json").read_bytes()
    tamper(batch)
    tampered = _view_bytes(batch)
    for _ in range(2):  # a second check meets exactly what the first one met
        assert _cli(monkeypatch, root, "check", str(batch)) == 2
        assert batch.is_dir()
        assert _view_bytes(batch) == tampered  # a deleted view stays deleted
        assert (batch / "scoreboard.json").read_bytes() == board_bytes
        saved = read_json(batch / "check.json")
        assert saved["passed"] is False
        recompute = _checks(batch)["scoreboard_recompute"]
        assert recompute["status"] == "FAIL"
        assert f"SCOREBOARD.md was {view} before this check" in recompute["details"]
    # Putting the genuine view back is what it takes.
    (batch / "SCOREBOARD.md").write_bytes(original)
    assert _cli(monkeypatch, root, "check", str(batch)) == 0
    assert read_json(batch / "check.json")["passed"] is True
    assert (batch / "SCOREBOARD.md").read_text() == render_scoreboard(
        read_json(batch / "scoreboard.json")
    )


def test_a_check_that_fails_elsewhere_still_refreshes_the_scoreboard_quality_list_and_view(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = fixture_spec.parent
    batch = core.run_line(fixture_spec, root)
    _set_receipt("trace", code_dirty=True)(batch)  # provenance fails; the scoreboard is sound
    assert _cli(monkeypatch, root, "check", str(batch)) == 2
    checks = _checks(batch)
    assert checks["provenance"]["status"] == "FAIL"
    assert checks["scoreboard_recompute"]["status"] == "PASS"
    board = read_json(batch / "scoreboard.json")
    assert board["quality_checks"] == read_json(batch / "check.json")["checks"]
    assert {c["name"] for c in board["quality_checks"] if c["status"] == "FAIL"} == {"provenance"}
    view = (batch / "SCOREBOARD.md").read_text()
    assert view == render_scoreboard(board)
    assert "provenance: **FAIL**" in view


def test_a_check_of_tampered_metrics_keeps_them_and_only_refreshes_the_quality_list(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = fixture_spec.parent
    batch = core.run_line(fixture_spec, root)
    board = read_json(batch / "scoreboard.json")
    board["judge_load"]["total"] += 1  # a plausible number the batch does not support
    tampered_total = board["judge_load"]["total"]
    write_json(batch / "scoreboard.json", board)
    (batch / "SCOREBOARD.md").write_text(render_scoreboard(board), encoding="utf-8")
    for _ in range(2):  # the view matches its board each time, so a second check meets the same
        assert _cli(monkeypatch, root, "check", str(batch)) == 2
        checks = read_json(batch / "check.json")["checks"]
        recompute = _checks(batch)["scoreboard_recompute"]
        assert recompute["status"] == "FAIL"
        assert "scoreboard differs from recomputation" in recompute["details"]
        assert not {c["status"] for c in checks} <= {"PENDING"}
        after = read_json(batch / "scoreboard.json")
        assert after["judge_load"]["total"] == tampered_total  # a wrong metric is never repaired
        assert after["quality_checks"] == checks  # only the quality list is refreshed
        assert {k: v for k, v in after.items() if k != "quality_checks"} == {
            k: v for k, v in board.items() if k != "quality_checks"
        }
        assert (batch / "SCOREBOARD.md").read_text() == render_scoreboard(after)
        assert "scoreboard_recompute: **FAIL**" in (batch / "SCOREBOARD.md").read_text()


def test_a_check_of_a_tampered_board_with_a_stale_view_restores_both_files(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = fixture_spec.parent
    batch = core.run_line(fixture_spec, root)
    board = read_json(batch / "scoreboard.json")
    board["judge_load"]["total"] += 1
    write_json(batch / "scoreboard.json", board)  # SCOREBOARD.md is left as it was
    board_bytes = (batch / "scoreboard.json").read_bytes()
    view_bytes = (batch / "SCOREBOARD.md").read_bytes()
    for _ in range(2):
        assert _cli(monkeypatch, root, "check", str(batch)) == 2
        recompute = _checks(batch)["scoreboard_recompute"]
        assert recompute["status"] == "FAIL"
        assert "SCOREBOARD.md was differs before this check" in recompute["details"]
        assert not {c["status"] for c in read_json(batch / "check.json")["checks"]} <= {"PENDING"}
        assert (batch / "scoreboard.json").read_bytes() == board_bytes
        assert (batch / "SCOREBOARD.md").read_bytes() == view_bytes


def _drop_quality_checks(batch: Path) -> None:
    board = read_json(batch / "scoreboard.json")
    del board["quality_checks"]
    write_json(batch / "scoreboard.json", board)


def _metrics_not_a_dict(batch: Path) -> None:
    board = read_json(batch / "scoreboard.json")
    board["overall"]["metrics"] = "not a mapping"
    write_json(batch / "scoreboard.json", board)


def _overall_removed(batch: Path) -> None:
    board = read_json(batch / "scoreboard.json")
    del board["overall"]
    write_json(batch / "scoreboard.json", board)


def _not_json(batch: Path) -> None:
    (batch / "scoreboard.json").write_text("{this is not json")


@pytest.mark.parametrize(
    "malform",
    [_drop_quality_checks, _metrics_not_a_dict, _overall_removed, _not_json],
    ids=["quality-checks-removed", "metrics-not-a-dict", "overall-removed", "not-json"],
)
def test_cli_check_of_a_malformed_scoreboard_exits_2_without_crashing_or_staying_pending(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch, malform: Callable[[Path], None]
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    malform(batch)
    assert _cli(monkeypatch, fixture_spec.parent, "check", str(batch)) == 2
    saved = read_json(batch / "check.json")
    assert saved["passed"] is False
    statuses = {c["status"] for c in saved["checks"]}
    assert statuses != {"PENDING"}
    assert "FAIL" in statuses
    assert _checks(batch)["scoreboard_recompute"]["status"] == "FAIL"
    assert batch.is_dir()


def test_a_check_json_without_scoreboard_view_is_read_as_ok(fixture_spec: Path) -> None:
    root = fixture_spec.parent
    batch = core.run_line(fixture_spec, root)
    assert "scoreboard_view" not in read_json(batch / "check.json")
    assert _check(batch, root, "scoreboard_recompute")["status"] == "PASS"
    for carried in ("differs", "missing", "unreadable"):
        saved = read_json(batch / "check.json")
        saved["scoreboard_view"] = carried
        write_json(batch / "check.json", saved)
        result = _check(batch, root, "scoreboard_recompute")
        assert result["status"] == "FAIL"
        assert f"SCOREBOARD.md was {carried} before this check" in result["details"]
    saved = read_json(batch / "check.json")
    saved["scoreboard_view"] = "ok"
    write_json(batch / "check.json", saved)
    assert _check(batch, root, "scoreboard_recompute")["status"] == "PASS"


def test_a_carried_bad_view_is_not_read_on_an_older_board(fixture_spec: Path) -> None:
    root = fixture_spec.parent
    batch = core.run_line(fixture_spec, root)
    board = read_json(batch / "scoreboard.json")
    board["schema_version"] = 2  # older boards never had their view compared
    write_json(batch / "scoreboard.json", board)
    saved = read_json(batch / "check.json")
    saved["scoreboard_view"] = "differs"
    write_json(batch / "check.json", saved)
    result = _check(batch, root, "scoreboard_recompute")
    assert result["status"] == "PASS", result["details"]


# ---- (b) evidence/station-inputs ---------------------------------------------------------------


def _snapshots(batch: Path) -> list[Path]:
    return sorted((batch / "evidence" / "station-inputs").iterdir())


def test_a_snapshot_the_judge_receipt_does_not_list_fails_provenance(fixture_spec: Path) -> None:
    root = fixture_spec.parent
    batch = core.run_line(fixture_spec, root)
    assert [p.name for p in _snapshots(batch)] == ["0001.jsonl"]
    assert _check(batch, root, "provenance")["status"] == "PASS"
    (batch / "evidence" / "station-inputs" / "0002.jsonl").write_text('{"run_id": "x"}\n')
    result = _check(batch, root, "provenance")
    assert result["status"] == "FAIL"
    assert (
        "evidence/station-inputs holds a file the judge receipt does not list"
        in (result["details"])
    )


@pytest.mark.parametrize(
    "name", ["0002.jsonl", "dev-split-0002.jsonl", "split-train.jsonl", "9999.jsonl"]
)
def test_any_extra_jsonl_in_station_inputs_is_not_listed_whatever_its_name(
    fixture_spec: Path, name: str
) -> None:
    root = fixture_spec.parent
    batch = core.run_line(fixture_spec, root)
    (batch / "evidence" / "station-inputs" / name).write_bytes(b"")
    failed = _failed(batch, root)
    assert "holds a file the judge receipt does not list" in failed["provenance"]


def test_a_missing_listed_snapshot_fails_provenance(fixture_spec: Path) -> None:
    root = fixture_spec.parent
    batch = core.run_line(fixture_spec, root)
    (_snapshots(batch)[0]).unlink()
    result = _check(batch, root, "provenance")
    assert result["status"] == "FAIL"
    assert (
        "evidence/station-inputs holds a file the judge receipt does not list"
        in (result["details"])
    )


def test_a_snapshot_swapped_for_another_file_fails_provenance(fixture_spec: Path) -> None:
    root = fixture_spec.parent
    batch = core.run_line(fixture_spec, root)
    listed = _snapshots(batch)[0]
    listed.unlink()
    (listed.parent / "0007.jsonl").write_bytes(b"")
    result = _check(batch, root, "provenance")
    assert result["status"] == "FAIL"
    assert "holds a file the judge receipt does not list" in result["details"]


def test_a_folder_in_station_inputs_is_refused_by_preflight(fixture_spec: Path) -> None:
    root = fixture_spec.parent
    batch = core.run_line(fixture_spec, root)
    (batch / "evidence" / "station-inputs" / "nested").mkdir()
    with pytest.raises(ValueError, match="unexpected folder: evidence/station-inputs/nested"):
        core.preflight_batch(batch)
    with pytest.raises(ValueError, match="unexpected folder: evidence/station-inputs/nested"):
        core.check_batch(batch, root)


def test_cli_check_with_a_folder_in_station_inputs_changes_nothing(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    (batch / "evidence" / "station-inputs" / "nested").mkdir()
    before = (batch / "check.json").read_bytes()
    assert _cli(monkeypatch, fixture_spec.parent, "check", str(batch)) == 2
    assert (batch / "check.json").read_bytes() == before


# ---- (c) receipt shapes and code revisions -----------------------------------------------------


def _first_input(stage: str, change: Callable[[dict[str, Any]], None]) -> Callable[[Path], None]:
    def mutate(batch: Path) -> None:
        _edit_receipt(batch, stage, lambda r: change(r["inputs"][0]))

    return mutate


def _first_output(stage: str, change: Callable[[dict[str, Any]], None]) -> Callable[[Path], None]:
    def mutate(batch: Path) -> None:
        _edit_receipt(batch, stage, lambda r: change(r["outputs"][0]))

    return mutate


def _duplicate(stage: str, side: str) -> Callable[[Path], None]:
    def mutate(batch: Path) -> None:
        _edit_receipt(batch, stage, lambda r: r[side].append(copy.deepcopy(r[side][0])))

    return mutate


def _set_receipt(stage: str, **fields: Any) -> Callable[[Path], None]:
    return lambda batch: _edit_receipt(batch, stage, lambda r: r.update(fields))


def _set_all_receipts(**fields: Any) -> Callable[[Path], None]:
    def mutate(batch: Path) -> None:
        for path in sorted((batch / "receipts").glob("*.json")):
            _edit_receipt(batch, path.stem, lambda r: r.update(fields))

    return mutate


RECEIPT_TAMPERS: dict[str, tuple[Callable[[Path], None], str]] = {
    "duplicate input ref": (
        _duplicate("intake", "inputs"),
        "intake receipt lists a file twice in inputs",
    ),
    "duplicate input ref in the judge receipt": (
        _duplicate("judge", "inputs"),
        "judge receipt lists a file twice in inputs",
    ),
    "duplicate output ref": (
        _duplicate("decisions", "outputs"),
        "decisions receipt lists a file twice in outputs",
    ),
    "extra field on an input ref": (
        _first_input("intake", lambda ref: ref.update(split="test")),
        "intake receipt inputs are not exactly ['path', 'sha256'] each",
    ),
    "missing field on an input ref": (
        _first_input("intake", lambda ref: ref.pop("sha256")),
        "intake receipt inputs are not exactly ['path', 'sha256'] each",
    ),
    "extra field on an output ref": (
        _first_output("decisions", lambda ref: ref.update(note="x")),
        "decisions receipt outputs are not exactly ['path', 'rows', 'sha256'] each",
    ),
    "missing rows on an output ref": (
        _first_output("decisions", lambda ref: ref.pop("rows")),
        "decisions receipt outputs are not exactly ['path', 'rows', 'sha256'] each",
    ),
    "sha256 as an int on an input": (
        _first_input("intake", lambda ref: ref.update(sha256=7)),
        "intake receipt inputs are not exactly ['path', 'sha256'] each",
    ),
    "sha256 as an int on an output": (
        _first_output("decisions", lambda ref: ref.update(sha256=7)),
        "decisions receipt outputs are not exactly ['path', 'rows', 'sha256'] each",
    ),
    "path as an int": (
        _first_output("decisions", lambda ref: ref.update(path=3)),
        "decisions receipt outputs are not exactly ['path', 'rows', 'sha256'] each",
    ),
    "rows as true": (
        _first_output("decisions", lambda ref: ref.update(rows=True)),
        "decisions receipt lists an output without a row count",
    ),
    "rows as a float": (
        _first_output("decisions", lambda ref: ref.update(rows=2.0)),
        "decisions receipt lists an output without a row count",
    ),
    "rows as a string": (
        _first_output("decisions", lambda ref: ref.update(rows="2")),
        "decisions receipt lists an output without a row count",
    ),
    "code_dirty as the string false in one receipt": (
        _set_receipt("intake", code_dirty="false"),
        "receipts come from different code revisions or working-tree states",
    ),
    "code_dirty as the string false in every receipt": (
        _set_all_receipts(code_dirty="false"),
        "decisions receipt has a bad code_dirty",  # receipts are checked in name order
    ),
    "code_dirty as 0 in one receipt": (  # (rev, 0) == (rev, False): the type check must catch it
        _set_receipt("intake", code_dirty=0),
        "intake receipt has a bad code_dirty",
    ),
    "code_dirty flipped in one receipt": (
        _set_receipt("trace", code_dirty=True),
        "receipts come from different code revisions or working-tree states",
    ),
    "code_rev changed in one receipt": (
        _set_receipt("export", code_rev="b" * 40),
        "receipts come from different code revisions or working-tree states",
    ),
}


@pytest.mark.parametrize("tamper", list(RECEIPT_TAMPERS))
def test_a_tampered_receipt_fails_provenance_with_its_message(
    fixture_spec: Path, tamper: str
) -> None:
    root = fixture_spec.parent
    batch = core.run_line(fixture_spec, root)
    assert _check(batch, root, "provenance")["status"] == "PASS"
    mutate, message = RECEIPT_TAMPERS[tamper]
    mutate(batch)
    result = _check(batch, root, "provenance")
    assert result["status"] == "FAIL"
    assert message in result["details"], result["details"]


def test_a_receipt_with_every_code_revision_state_the_same_still_passes(
    fixture_spec: Path,
) -> None:
    """The revision/dirty comparison is across receipts, not against the running code."""
    root = fixture_spec.parent
    batch = core.run_line(fixture_spec, root)
    _set_all_receipts(code_rev="c" * 40, code_dirty=True)(batch)
    assert _check(batch, root, "provenance")["status"] == "PASS"


def test_a_forged_receipt_timestamp_is_not_detected(fixture_spec: Path) -> None:
    """Pinned limit: the threat model says receipt timestamps cannot be verified (nothing
    outside the receipt attests when a stage ran), so a different valid timestamp passes."""
    root = fixture_spec.parent
    batch = core.run_line(fixture_spec, root)
    _edit_receipt(
        batch,
        "intake",
        lambda r: r.update(
            started_at="2001-01-01T00:00:00+00:00", finished_at="2001-01-01T00:00:01+00:00"
        ),
    )
    assert _check(batch, root, "provenance")["status"] == "PASS"
    assert core.check_batch(batch, root)["passed"] is True


# ---- (d) the live budget walk ------------------------------------------------------------------


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
    spec["models"]["allowed"] = [ROUTE, OTHER_ROUTE]
    write_json(fixture_spec, spec)
    soft = hd.StageVerdict("l0", "review", ["L0_SOFT_TARGET_SURFACE_WRITE"], [], {})
    monkeypatch.setattr(hd, "evaluate_l0_stage", lambda _item: soft)

    def fake(prompt: str, *, model: str, **_: Any) -> dict[str, Any]:
        return {
            "stdout": json.dumps({"suspicion": 10, "verdict": "clean"}),
            "served_models": [model],
            "n_fallbacks": 0,
            "usage": {"usd": 0.5},
        }

    monkeypatch.setattr(hd, "run_omp_session", fake)
    return fixture_spec


Trace = tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[hd.RolloutItem]]


def _trace(batch: Path, root: Path) -> Trace:
    spec = read_json(batch / "spec.json")
    items, _ = core._intake(spec, root)
    stored = read_rows(batch / "trace.jsonl")
    fresh = [{"run_id": r["run_id"], "l0": r["l0"]} for r in stored]
    return stored, fresh, spec, items


def _validate(trace: Trace) -> None:
    stored, fresh, spec, items = trace
    core._validate_live_trace(stored, fresh, spec, items)


def _ar(rows: list[dict[str, Any]], run_id: str) -> dict[str, Any]:
    return next(r for r in rows if r["run_id"] == run_id)["autoreview"]


def _call(route: str = ROUTE, cost: float = 0.0) -> dict[str, Any]:
    return {
        "requested_route": route,
        "served_models": [route],
        "n_fallbacks": 0,
        "cost_usd": cost,
    }


def _make_error(
    rows: list[dict[str, Any]], run_id: str, error: str, call: dict[str, Any] | None
) -> None:
    ar = _ar(rows, run_id)
    ar.update(
        decision="review",
        reasons=[f"AUTOREVIEW_ERROR:{error}"],
        evidence_steps=[],
        cited_steps=[],
        details={"error": error},
        score=None,
        call=call,
    )
    if call is None:
        ar.pop("call")


def _make_reading(rows: list[dict[str, Any]], run_id: str) -> None:
    """Give ``run_id`` a copy of the first run's honest 0.50-USD reading."""
    first = next(r for r in rows if r["run_id"] == "run-1")["autoreview"]
    row = next(r for r in rows if r["run_id"] == run_id)
    row["autoreview"] = copy.deepcopy(first)


@pytest.fixture
def refusable_trace(live_spec: Path) -> Trace:
    """run-1 read for 0.50 USD (cap 1.0, per-call reservation 0.6): run-2 is refusable."""
    batch = core.run_line(live_spec, live_spec.parent)
    assert core.check_batch(batch, live_spec.parent)["passed"]
    trace = _trace(batch, live_spec.parent)
    stored = trace[0]
    assert _ar(stored, "run-1")["reasons"] == ["AUTOREVIEW_CLEAN"]
    assert _ar(stored, "run-1")["call"]["cost_usd"] == 0.5
    assert _ar(stored, "run-2")["reasons"] == ["AUTOREVIEW_ERROR:BudgetReservationRefused"]
    assert _ar(stored, "run-2").get("call") is None
    return trace


def test_the_honest_refusal_of_a_run_whose_reservation_passes_the_cap_is_valid(
    refusable_trace: Trace,
) -> None:
    _validate(refusable_trace)


def test_a_forged_clean_reading_where_the_walk_required_a_refusal_is_refused(
    refusable_trace: Trace,
) -> None:
    stored = refusable_trace[0]
    _make_reading(stored, "run-2")
    assert _ar(stored, "run-2")["reasons"] == ["AUTOREVIEW_CLEAN"]
    with pytest.raises(
        ValueError, match="live reading where the budget walk required a refusal: run-2"
    ):
        _validate(refusable_trace)


def test_a_budget_refusal_that_carries_a_call_record_is_refused(refusable_trace: Trace) -> None:
    stored = refusable_trace[0]
    _ar(stored, "run-2")["call"] = _call(ROUTE, 0.1)
    with pytest.raises(ValueError, match="live error reading is not what the run could produce"):
        _validate(refusable_trace)


def test_a_budget_refusal_with_a_none_call_is_the_same_as_no_call(
    refusable_trace: Trace,
) -> None:
    _ar(refusable_trace[0], "run-2")["call"] = None
    _validate(refusable_trace)


def test_a_budget_refusal_on_a_run_the_budget_could_still_pay_for_is_refused(
    refusable_trace: Trace,
) -> None:
    stored = refusable_trace[0]
    _make_error(stored, "run-1", "BudgetReservationRefused", None)
    _make_reading(stored, "run-2")
    with pytest.raises(
        ValueError, match="live error reading is not what the run could produce: run-1"
    ):
        _validate(refusable_trace)


def test_an_error_row_on_a_refusable_run_with_a_call_of_any_route_is_refused(
    refusable_trace: Trace,
) -> None:
    stored = refusable_trace[0]
    _make_error(stored, "run-2", "Foo", _call(ROUTE, 0.0))
    with pytest.raises(
        ValueError, match="live error reading is not what the run could produce: run-2"
    ):
        _validate(refusable_trace)


def test_a_non_budget_error_before_the_reviewer_ran_on_a_refusable_run_is_valid(
    refusable_trace: Trace,
) -> None:
    _make_error(refusable_trace[0], "run-2", "Foo", None)
    _validate(refusable_trace)


def test_an_error_row_that_is_not_refusable_must_carry_a_call_on_the_spec_route(
    refusable_trace: Trace,
) -> None:
    stored = refusable_trace[0]
    # run-1's call cost nothing, so run-2 can again pay its reservation: it reads normally.
    _make_reading(stored, "run-2")
    _make_error(stored, "run-1", "Foo", _call(OTHER_ROUTE, 0.0))
    with pytest.raises(
        ValueError, match="live error reading is not what the run could produce: run-1"
    ):
        _validate(refusable_trace)
    _ar(stored, "run-1")["call"] = _call(ROUTE, 0.0)
    _validate(refusable_trace)


def test_an_error_row_that_is_not_refusable_may_have_no_call(refusable_trace: Trace) -> None:
    stored = refusable_trace[0]
    _make_reading(stored, "run-2")
    _make_error(stored, "run-1", "Foo", None)
    _validate(refusable_trace)


def test_a_non_budget_error_with_a_wrong_details_object_is_refused(
    refusable_trace: Trace,
) -> None:
    stored = refusable_trace[0]
    _make_error(stored, "run-2", "Foo", None)
    _ar(stored, "run-2")["details"] = {"error": "Bar"}
    with pytest.raises(ValueError, match="live error reading is not what the run could produce"):
        _validate(refusable_trace)


# ---- (d) cached verdicts: the non-reading outcomes carry no reading ----------------------------


def _stage(reasons: list[str], details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"decision": "review", "reasons": reasons, "details": details or {}}


REFUSED_STAGES = {
    "budget exceeded and a confirmed cheat": _stage(
        ["AUTOREVIEW_BUDGET_EXCEEDED", "AUTOREVIEW_CONFIRMED_CHEAT"],
        {"calls": 1, "spent_usd": 0.0},
    ),
    "budget exceeded and a clean reading": _stage(
        ["AUTOREVIEW_BUDGET_EXCEEDED", "AUTOREVIEW_CLEAN"], {"calls": 1, "spent_usd": 0.0}
    ),
    "budget exceeded with a suspicion": _stage(
        ["AUTOREVIEW_BUDGET_EXCEEDED"], {"calls": 1, "spent_usd": 0.0, "suspicion": 99}
    ),
    "budget exceeded with a verdict": _stage(
        ["AUTOREVIEW_BUDGET_EXCEEDED"], {"calls": 1, "spent_usd": 0.0, "verdict": "confirmed_cheat"}
    ),
    "budget exceeded with a cost": _stage(
        ["AUTOREVIEW_BUDGET_EXCEEDED"], {"calls": 1, "spent_usd": 0.0, "cost_usd": 0.1}
    ),
    "budget exceeded with a model": _stage(
        ["AUTOREVIEW_BUDGET_EXCEEDED"], {"calls": 1, "spent_usd": 0.0, "model": ROUTE}
    ),
    "budget exceeded with attested": _stage(
        ["AUTOREVIEW_BUDGET_EXCEEDED"], {"calls": 1, "spent_usd": 0.0, "attested": True}
    ),
    "cache miss with a model": _stage(["AUTOREVIEW_CACHE_MISS"], {"model": ROUTE}),
    "cache miss with a suspicion": _stage(["AUTOREVIEW_CACHE_MISS"], {"suspicion": 0}),
    "no trajectory with attested": _stage(["AUTOREVIEW_NO_TRAJECTORY"], {"attested": True}),
    "no trajectory with attested false": _stage(["AUTOREVIEW_NO_TRAJECTORY"], {"attested": False}),
    "an error with a suspicion": _stage(["AUTOREVIEW_ERROR:X"], {"error": "X", "suspicion": 50}),
    "an error with a verdict": _stage(["AUTOREVIEW_ERROR:X"], {"error": "X", "verdict": "clean"}),
    "two errors": _stage(["AUTOREVIEW_ERROR:X", "AUTOREVIEW_ERROR:Y"], {"error": "X"}),
    "an error and no trajectory": _stage(
        ["AUTOREVIEW_ERROR:X", "AUTOREVIEW_NO_TRAJECTORY"], {"error": "X"}
    ),
    "a passing budget refusal": {
        "decision": "pass",
        "reasons": ["AUTOREVIEW_BUDGET_EXCEEDED"],
        "details": {},
    },
}


@pytest.mark.parametrize("name", list(REFUSED_STAGES))
def test_a_cached_non_reading_outcome_carrying_a_reading_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="disagrees with its own evidence: cache-row"):
        core._autoreview_stage(copy.deepcopy(REFUSED_STAGES[name]), "cache-row", [])


HONEST_STAGES = {
    "budget exceeded": (["AUTOREVIEW_BUDGET_EXCEEDED"], {"calls": 1, "spent_usd": 0.0}),
    "budget exceeded without details": (["AUTOREVIEW_BUDGET_EXCEEDED"], {}),
    "cache miss": (["AUTOREVIEW_CACHE_MISS"], {}),
    "no trajectory": (["AUTOREVIEW_NO_TRAJECTORY"], {}),
    "no trajectory with a note": (
        ["AUTOREVIEW_NO_TRAJECTORY"],
        {"note": "AutoReview requires ATIF trajectory run"},
    ),
    "an error": (["AUTOREVIEW_ERROR:X"], {"error": "X"}),
}


@pytest.mark.parametrize("name", list(HONEST_STAGES))
def test_a_cached_non_reading_outcome_with_its_honest_details_is_accepted(name: str) -> None:
    reasons, details = HONEST_STAGES[name]
    verdict = core._autoreview_stage(_stage(list(reasons), dict(details)), "cache-row", [])
    assert verdict.stage == "autoreview" and verdict.decision == "review"
    assert verdict.reasons == reasons
    assert verdict.details == details


def _cached_spec(spec_path: Path, rows: list[dict[str, Any]], allowed: list[str]) -> None:
    spec = read_json(spec_path)
    cache = spec_path.parent / "cache"
    write_rows(cache / "review.jsonl", rows)
    spec["autoreview"] = {"mode": "cached", "cache": str(cache), "max_usd": 0}
    spec["models"]["allowed"] = allowed
    write_json(spec_path, spec)


def _cached_row(run_id: str, stage: dict[str, Any]) -> dict[str, Any]:
    return {"id": run_id, "stage_decisions": {"autoreview": stage}}


def _nothing_written(spec_path: Path) -> None:
    spec = read_json(spec_path)
    assert not (Path(spec["out_root"]) / spec["batch_id"]).exists()
    assert not Path(spec["out_root"]).exists()
    assert not Path(spec["judge"]["queue"]).exists()


def test_a_cached_line_run_with_a_forged_budget_exceeded_reading_is_refused(
    fixture_spec: Path,
) -> None:
    forged = _stage(
        ["AUTOREVIEW_BUDGET_EXCEEDED"],
        {
            "calls": 1,
            "spent_usd": 0.0,
            "suspicion": 99,
            "verdict": "confirmed_cheat",
            "model": ROUTE,
            "attested": True,
            "cost_usd": 0.0,
        },
    )
    _cached_spec(
        fixture_spec,
        [_cached_row("clean-run", forged), _cached_row("weak-run", forged)],
        [ROUTE],
    )
    with pytest.raises(ValueError, match="disagrees with its own evidence: review.jsonl:"):
        core.run_line(fixture_spec, fixture_spec.parent)
    _nothing_written(fixture_spec)


def test_a_cached_line_run_with_a_two_reason_budget_row_is_refused(fixture_spec: Path) -> None:
    two = _stage(
        ["AUTOREVIEW_BUDGET_EXCEEDED", "AUTOREVIEW_CONFIRMED_CHEAT"],
        {"calls": 1, "spent_usd": 0.0},
    )
    _cached_spec(fixture_spec, [_cached_row("clean-run", two)], [ROUTE])
    with pytest.raises(ValueError, match="disagrees with its own evidence: review.jsonl:"):
        core.run_line(fixture_spec, fixture_spec.parent)
    _nothing_written(fixture_spec)
