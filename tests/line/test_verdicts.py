"""Human-verdict import: idempotence, honest metrics and append-only provenance."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from evallab import cli
from evallab.line import core
from evallab.line.io import digest, read_json, read_rows, write_json, write_rows
from evallab.line.verdicts import import_verdicts


def _station(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    write_rows(path / "verdicts.jsonl", rows)
    return path


def _verdict(identity: str, run_id: str, verdict: str, judge: str = "person") -> dict[str, Any]:
    return {
        "schema": "judge-verdict/v1",
        "id": identity,
        "run_id": run_id,
        "judge": judge,
        "verdict": verdict,
    }


def _check(batch: Path, root: Path, name: str) -> dict[str, Any]:
    return next(c for c in core.check_batch(batch, root)["checks"] if c["name"] == name)


def test_round_trip_is_idempotent_and_never_changes_decisions(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "instrument_openinference", lambda: None)
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    decisions = digest(batch / "decisions.jsonl")
    state = _station(
        fixture_spec.parent / "station",
        [
            _verdict("one", "clean-run", "cheat"),
            _verdict("two", "weak-run", "legitimate"),
            _verdict("elsewhere", "not-in-this-batch", "cheat"),
        ],
    )
    first = import_verdicts(batch, state, fixture_spec.parent)
    assert (first["imported"], first["verdicts"], first["passed"]) == (2, 2, True)
    frozen = {
        p: digest(p) for p in (batch / "verdicts.jsonl", batch / "receipts" / "verdicts.json")
    }
    again = import_verdicts(batch, state, fixture_spec.parent)
    assert (again["imported"], again["verdicts"]) == (0, 2)
    assert frozen == {p: digest(p) for p in frozen}
    args = ["line", "verdicts", str(batch), "--judge-state", str(state), "--json"]
    assert cli.run_cli(args, workspace=fixture_spec.parent) == 0
    assert digest(batch / "decisions.jsonl") == decisions
    assert core.check_batch(batch, fixture_spec.parent)["passed"]
    human = read_json(batch / "scoreboard.json")["human"]
    assert human["counts"] == {
        "review": {"cheated": 0, "honest": 1, "unsure": 0},
        "audited_keep": {"cheated": 1, "honest": 0, "unsure": 0},
        "other": {"cheated": 0, "honest": 0, "unsure": 0},
    }
    assert human["review_precision"] == {"k": 0, "n": 1, "rate": 0.0, "wilson_ci": [0.0, 0.7935]}
    assert human["audit_miss_estimate"] == {"k": 1, "n": 1, "rate": 1.0, "wilson_ci": [0.2065, 1.0]}
    assert human["label_agreement"] == {
        "cheated": {"cheat": 0, "clean": 1, "attempted_cheat": 0, "unlabelled": 0},
        "honest": {"cheat": 1, "clean": 0, "attempted_cheat": 0, "unlabelled": 0},
        "unsure": {"cheat": 0, "clean": 0, "attempted_cheat": 0, "unlabelled": 0},
    }


def test_unsure_is_reported_separately_and_never_counted(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    state = _station(
        fixture_spec.parent / "station",
        [_verdict("a", "weak-run", "unclear"), _verdict("b", "clean-run", "unclear")],
    )
    import_verdicts(batch, state, fixture_spec.parent)
    human = read_json(batch / "scoreboard.json")["human"]
    assert human["unsure"] == 2
    assert human["review_precision"]["n"] == 0 and human["review_precision"]["rate"] is None
    assert human["audit_miss_estimate"]["n"] == 0
    assert "unsure" in (batch / "SCOREBOARD.md").read_text()


def test_appends_keep_older_receipts_valid(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    judge_before = digest(batch / "receipts" / "judge.json")
    state = _station(fixture_spec.parent / "station", [_verdict("one", "clean-run", "legitimate")])
    import_verdicts(batch, state, fixture_spec.parent)
    write_rows(
        state / "verdicts.jsonl",
        [_verdict("one", "clean-run", "legitimate"), _verdict("two", "weak-run", "unclear")],
    )
    import_verdicts(batch, state, fixture_spec.parent)
    write_rows(
        state / "verdicts.jsonl",
        [
            _verdict("one", "clean-run", "legitimate"),
            _verdict("two", "weak-run", "unclear"),
            _verdict("three", "weak-run", "cheat", judge="second"),
        ],
    )
    import_verdicts(batch, state, fixture_spec.parent)
    assert core.check_batch(batch, fixture_spec.parent)["passed"]
    assert digest(batch / "receipts" / "judge.json") == judge_before
    assert len(list((batch / "receipts" / "verdicts-history").glob("*.json"))) == 2
    assert len(read_rows(batch / "verdicts.jsonl")) == 3


def test_snapshot_makes_later_station_mutation_irrelevant(fixture_spec: Path) -> None:
    spec = read_json(fixture_spec)
    station_file = Path(spec["judge"]["verdicts"])
    write_rows(station_file, [_verdict("one", "clean-run", "cheat")])
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert len(read_rows(batch / "verdicts.jsonl")) == 1
    station_file.write_text("rewritten after the batch\n")
    assert core.check_batch(batch, fixture_spec.parent)["passed"]


def test_unsnapshotted_v1_batch_with_mutated_station_fails_closed(fixture_spec: Path) -> None:
    spec = read_json(fixture_spec)
    station_file = Path(spec["judge"]["verdicts"])
    write_rows(station_file, [_verdict("one", "clean-run", "cheat")])
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    # Rebuild the v1 shape: the receipt points at the external file, not at a snapshot.
    receipt = read_json(batch / "receipts" / "judge.json")
    receipt["inputs"][1] = {"path": str(station_file), "sha256": digest(station_file)}
    write_json(batch / "receipts" / "judge.json", receipt)
    assert _check(batch, fixture_spec.parent, "provenance")["status"] == "PASS"
    station_file.write_text("mutated\n")
    result = _check(batch, fixture_spec.parent, "provenance")
    assert result["status"] == "FAIL"
    assert "never snapshotted" in result["details"]


@pytest.mark.parametrize("mode", ["edit-earlier-line", "truncate", "reorder"])
def test_tampering_with_earlier_verdict_lines_fails_provenance(
    fixture_spec: Path, mode: str
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    state = _station(
        fixture_spec.parent / "station",
        [_verdict("one", "clean-run", "cheat"), _verdict("two", "weak-run", "legitimate")],
    )
    import_verdicts(batch, state, fixture_spec.parent)
    write_rows(
        state / "verdicts.jsonl",
        [
            _verdict("one", "clean-run", "cheat"),
            _verdict("two", "weak-run", "legitimate"),
            _verdict("three", "clean-run", "unclear", judge="second"),
        ],
    )
    import_verdicts(batch, state, fixture_spec.parent)
    rows = read_rows(batch / "verdicts.jsonl")
    if mode == "edit-earlier-line":
        rows[0]["verdict"] = "unclear"
    elif mode == "truncate":
        rows = rows[:1]
    else:
        rows = [rows[1], rows[0], rows[2]]
    write_rows(batch / "verdicts.jsonl", rows)
    assert _check(batch, fixture_spec.parent, "provenance")["status"] == "FAIL"


def test_receipt_claiming_more_rows_than_the_file_has_fails(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    state = _station(fixture_spec.parent / "station", [_verdict("one", "clean-run", "cheat")])
    import_verdicts(batch, state, fixture_spec.parent)
    write_rows(
        state / "verdicts.jsonl",
        [_verdict("one", "clean-run", "cheat"), _verdict("two", "weak-run", "legitimate")],
    )
    import_verdicts(batch, state, fixture_spec.parent)
    # Inflate the archived receipt's row count and re-link it consistently, so only the
    # row-count rule can catch the claim.
    receipts = batch / "receipts"
    archived = receipts / "verdicts-history" / "0001.json"
    older = read_json(archived)
    older["outputs"][0]["rows"] = 99
    write_json(archived, older)
    newest = read_json(receipts / "verdicts.json")
    newest["previous"]["sha256"] = digest(archived)
    newest["previous"]["verdicts_rows"] = 99
    write_json(receipts / "verdicts.json", newest)
    result = _check(batch, fixture_spec.parent, "provenance")
    assert result["status"] == "FAIL"
    assert "more verdict rows" in result["details"]


def test_newest_receipt_must_cover_the_whole_file(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    state = _station(fixture_spec.parent / "station", [_verdict("one", "clean-run", "cheat")])
    import_verdicts(batch, state, fixture_spec.parent)
    with (batch / "verdicts.jsonl").open("a") as handle:
        handle.write('{"unreceipted": true}\n')
    assert _check(batch, fixture_spec.parent, "provenance")["status"] == "FAIL"


@pytest.mark.parametrize(
    "rows,message",
    [
        ([_verdict("one", "clean-run", "maybe")], "unknown human verdict"),
    ],
)
def test_station_rows_are_validated(fixture_spec: Path, rows: list, message: str) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    state = _station(fixture_spec.parent / "station", rows)
    with pytest.raises(ValueError, match=message):
        import_verdicts(batch, state, fixture_spec.parent)
    assert read_rows(batch / "verdicts.jsonl") == []


def test_same_id_with_changed_content_is_refused(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    state = _station(fixture_spec.parent / "station", [_verdict("one", "clean-run", "cheat")])
    import_verdicts(batch, state, fixture_spec.parent)
    before = digest(batch / "verdicts.jsonl")
    write_rows(state / "verdicts.jsonl", [_verdict("one", "clean-run", "legitimate")])
    with pytest.raises(ValueError, match="conflicting verdict id"):
        import_verdicts(batch, state, fixture_spec.parent)
    assert digest(batch / "verdicts.jsonl") == before


def test_undone_station_verdicts_are_not_imported(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    state = _station(
        fixture_spec.parent / "station",
        [
            _verdict("one", "clean-run", "cheat"),
            {"schema": "judge-undo/v1", "undoes": "one"},
            _verdict("two", "weak-run", "legitimate"),
        ],
    )
    import_verdicts(batch, state, fixture_spec.parent)
    assert [r["id"] for r in read_rows(batch / "verdicts.jsonl")] == ["two"]
