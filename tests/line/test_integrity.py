"""Tamper detection, side-effect ordering, label loading and Station snapshots."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from evallab import cli
from evallab import hack_drop as hd
from evallab.line import core
from evallab.line.io import digest, read_json, read_rows, write_json, write_rows
from evallab.line.replay import compare_baseline, replay


def _check(batch: Path, root: Path, name: str) -> dict[str, str]:
    return next(c for c in core.check_batch(batch, root)["checks"] if c["name"] == name)


def _refresh_receipt(batch: Path, stage: str) -> None:
    """Re-hash a receipt's outputs so only the content rule, not a hash, can notice."""
    path = batch / "receipts" / f"{stage}.json"
    receipt = read_json(path)
    for ref in receipt["outputs"]:
        target = core.resolve_ref(batch, ref["path"])
        ref["sha256"] = digest(target)
        ref["rows"] = len(read_rows(target))
    write_json(path, receipt)


def test_swapped_export_is_caught_even_with_a_matching_receipt(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    kept = read_rows(batch / "export" / "kept.jsonl")
    review = read_rows(batch / "export" / "review.jsonl")
    assert kept and review
    write_rows(batch / "export" / "kept.jsonl", kept + review)  # a review row smuggled into kept
    _refresh_receipt(batch, "export")
    result = _check(batch, fixture_spec.parent, "conservation")
    assert result["status"] == "FAIL" and "export/kept.jsonl" in result["details"]


def test_emptied_receipt_references_are_caught(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    for stage in ("export", "decisions", "judge"):
        path = batch / "receipts" / f"{stage}.json"
        receipt = read_json(path)
        receipt["inputs"], receipt["outputs"] = [], []
        write_json(path, receipt)
    result = _check(batch, fixture_spec.parent, "provenance")
    assert result["status"] == "FAIL" and "does not list" in result["details"]


def test_non_dev_label_file_fails_before_any_side_effect(fixture_spec: Path) -> None:
    spec = read_json(fixture_spec)
    labels = Path(spec["sources"][0]["labels"])
    write_rows(labels, [{"id": "clean-run", "is_cheat": False, "split": "test"}])
    with pytest.raises(Exception, match="non-DEV"):
        core.run_line(fixture_spec, fixture_spec.parent)
    assert not (Path(spec["out_root"]) / spec["batch_id"]).exists()
    assert not Path(spec["judge"]["queue"]).exists()


def test_dev_gate_checks_label_files_of_an_existing_batch(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    labels = Path(read_json(fixture_spec)["sources"][0]["labels"])
    write_rows(labels, [{"id": "clean-run", "is_cheat": False, "split": "test"}])
    assert _check(batch, fixture_spec.parent, "dev_only")["status"] == "FAIL"


def test_check_never_leaves_a_stale_pass_after_a_crash(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert read_json(batch / "check.json")["passed"]
    spec = read_json(batch / "spec.json")
    spec["split"] = "test"  # load_spec refuses this before any gate runs
    write_json(batch / "spec.json", spec)
    code = cli.run_cli(["line", "check", str(batch), "--json"], workspace=fixture_spec.parent)
    assert code == 2
    assert read_json(batch / "check.json")["passed"] is False
    # the refused check is a finished failing result everywhere, never a stale pass
    assert {c["status"] for c in read_json(batch / "scoreboard.json")["quality_checks"]} == {"FAIL"}


@pytest.mark.parametrize(
    "status,expected",
    [("gold", "certified"), ("hardened", "certified"), ("exploitable", "weak")],
)
def test_hack_drop_vocabulary_maps_onto_line_statuses(
    fixture_spec: Path, status: str, expected: str
) -> None:
    spec = read_json(fixture_spec)
    write_json(
        Path(spec["task_status"]),
        {"tasks": [{"task_id": "weak-task", "status": status, "reason": "r"}]},
    )
    spec.update(task_action="drop")
    write_json(fixture_spec, spec)
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    task = next(t for t in read_rows(batch / "tasks.jsonl") if t["task_id"] == "weak-task")
    assert task["status"] == expected
    decision = next(d for d in read_rows(batch / "decisions.jsonl") if d["run_id"] == "weak-run")
    assert decision["decision"] == ("drop" if expected == "weak" else "keep")


def test_station_file_growing_after_the_run_does_not_break_provenance(
    fixture_spec: Path,
) -> None:
    station = Path(read_json(fixture_spec)["judge"]["verdicts"])
    write_rows(
        station,
        [{"schema": "judge-verdict/v1", "id": "v1", "run_id": "weak-run", "verdict": "cheat"}],
    )
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert [r["id"] for r in read_rows(batch / "verdicts.jsonl")] == ["v1"]
    with station.open("a") as handle:
        handle.write('{"schema": "judge-verdict/v1", "id": "v2", "run_id": "clean-run"}\n')
    assert core.check_batch(batch, fixture_spec.parent)["passed"]
    receipt = read_json(batch / "receipts" / "judge.json")
    assert any("station-inputs" in ref["path"] for ref in receipt["inputs"])
    assert str(station) not in {ref["path"] for ref in receipt["inputs"]}


def test_a_symlink_cannot_launder_a_forbidden_path(tmp_path: Path) -> None:
    keys = tmp_path / "answer-keys"
    keys.mkdir()
    secret = keys / "rollouts.jsonl"
    secret.write_text("{}\n")
    innocent = tmp_path / "dev-rollouts.jsonl"
    innocent.symlink_to(secret)
    with pytest.raises(hd.RefusedRun):
        hd.check_dev_path_allowed(innocent)
    inside = keys / "link.jsonl"
    real = tmp_path / "real.jsonl"
    real.write_text("{}\n")
    inside.symlink_to(real)
    with pytest.raises(hd.RefusedRun):
        hd.check_dev_path_allowed(inside)


def test_json_list_manifest_is_refused(fixture_spec: Path) -> None:
    spec = read_json(fixture_spec)
    manifest = fixture_spec.parent / "list.json"
    manifest.write_text('[{"id": "x", "split": "test", "path": "nowhere"}]')
    spec["sources"][0]["rollouts"] = str(manifest)
    write_json(fixture_spec, spec)
    with pytest.raises(ValueError, match="JSON list"):
        core.run_line(fixture_spec, fixture_spec.parent)


class _Registry:
    def assigned_test_anywhere(self, *, task_id: str | None = None, item_id: str | None = None):
        return item_id == "sealed-item" or task_id == "sealed-task"


@pytest.mark.parametrize(
    "body",
    [
        '{"sealed-item": true}',  # flat run-id map
        '{"split": "test", "rows": [{"id": "a", "is_cheat": true}]}',  # wrapper declaration
        '[{"item_id": "sealed-item", "is_cheat": true}]',  # item_id key in the row
    ],
)
def test_load_labels_applies_dev_checks_to_every_label_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    monkeypatch.setattr(hd, "_get_corpus_registry", lambda **_: _Registry())
    labels = tmp_path / "labels.json"
    labels.write_text(body)
    with pytest.raises(hd.RefusedRun):
        hd.load_labels(labels, repo_root=tmp_path)


def test_load_labels_reads_item_id_rows_and_keeps_legacy_shapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hd, "_get_corpus_registry", lambda **_: _Registry())
    cases: dict[str, dict[str, bool]] = {
        '[{"item_id": "dev-item", "is_cheat": true}]': {"dev-item": True},
        '[{"id": "a", "is_cheat": false}]': {"a": False},
        '{"rows": [{"run_id": "b", "is_cheat": true}]}': {"b": True},
        '{"a": true, "b": false}': {"a": True, "b": False},
    }
    for body, expected in cases.items():
        path = tmp_path / "labels.json"
        path.write_text(body)
        assert hd.load_labels(path, repo_root=tmp_path) == expected


def test_replay_refuses_a_baseline_under_a_test_path(fixture_spec: Path) -> None:
    forbidden = fixture_spec.parent / "test-split" / "baseline.json"
    value, code = replay(fixture_spec, forbidden, fixture_spec.parent, write_baseline=True)
    assert code == 2 and not forbidden.exists()
    assert "TEST split" in value["error"]


def test_a_scoreboard_missing_checks_cannot_be_compared(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    score = read_json(batch / "scoreboard.json")
    baseline: dict[str, Any] = {
        "schema": "evallab.line_baseline/v1",
        "scoreboard": score,
        "decisions": read_rows(batch / "decisions.jsonl"),
    }
    score["quality_checks"] = score["quality_checks"][:1]
    with pytest.raises(ValueError, match="all eight quality checks"):
        compare_baseline(baseline, score, read_rows(batch / "decisions.jsonl"))
