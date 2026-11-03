"""Empty label rows, AutoReview cache misses and relocatable batches."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from evallab import hack_drop as hd
from evallab.line import core
from evallab.line.io import read_json, read_rows, write_json, write_rows


def _check(batch: Path, root: Path, name: str) -> dict[str, str]:
    return next(c for c in core.check_batch(batch, root)["checks"] if c["name"] == name)


def test_empty_rows_list_is_an_empty_label_set_not_a_run_named_rows(tmp_path: Path) -> None:
    for body in ('{"rows": []}', '{"items": []}', '{"labels": []}'):
        path = tmp_path / "labels.json"
        path.write_text(body)
        assert hd.load_labels(path, repo_root=tmp_path) == {}
    path.write_text('{"rows": [], "other-run": true}')
    assert hd.load_labels(path, repo_root=tmp_path) == {}


@pytest.mark.parametrize(
    "l0,eligible",
    [
        (hd.StageVerdict("l0", "drop", ["X"], [], {"writes_target_surface": True}), False),
        (hd.StageVerdict("l0", "review", ["X"], [], {}), True),
        (hd.StageVerdict("l0", "pass", [], [], {"writes_target_surface": True}), True),
        (hd.StageVerdict("l0", "pass", [], [], {}), False),
    ],
)
def test_eligibility_helper_matches_the_stage_gate(l0: hd.StageVerdict, eligible: bool) -> None:
    item = hd.RolloutItem(item_id="r", task_id="t")
    stage = hd.evaluate_autoreview_stage(item, l0, enabled=True)
    assert hd.autoreview_eligible(l0) is eligible
    assert ("AUTOREVIEW_NOT_ELIGIBLE" in stage.reasons) is (not eligible)


def _cached_spec(spec_path: Path, rows: list[dict[str, Any]]) -> None:
    spec = read_json(spec_path)
    cache = spec_path.parent / "cache"
    write_rows(cache / "review.jsonl", rows)
    spec["autoreview"] = {"mode": "cached", "cache": str(cache), "max_usd": 0}
    spec["models"]["allowed"] = ["synthetic/model"]
    write_json(spec_path, spec)


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


def _reading(run_id: str) -> dict[str, Any]:
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
                    "cost_usd": 0.0,
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


def test_eligible_run_without_a_stored_reading_goes_to_review_and_is_counted(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cached_spec(fixture_spec, [_not_eligible("clean-run"), _not_eligible("weak-run")])
    _soft_l0(monkeypatch, {"clean-run"})
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    rows = {r["run_id"]: r for r in read_rows(batch / "decisions.jsonl")}
    assert rows["clean-run"]["decision"] == "review"
    assert "AUTOREVIEW_CACHE_MISS" in rows["clean-run"]["reasons"]
    assert "AUTOREVIEW_CACHE_MISS" not in rows["weak-run"]["reasons"]
    score = read_json(batch / "scoreboard.json")
    assert score["autoreview"] == {"cache_misses": 1}
    assert "autoreview_cache_misses: 1" in (batch / "SCOREBOARD.md").read_text()
    assert core.check_batch(batch, fixture_spec.parent)["passed"]


def test_missing_cache_row_is_a_miss_only_when_the_run_needs_a_reading(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cached_spec(fixture_spec, [])
    # Nothing is eligible: absent rows are fine, no model reading was ever needed.
    quiet = core.run_line(fixture_spec, fixture_spec.parent)
    assert read_json(quiet / "scoreboard.json")["autoreview"] == {"cache_misses": 0}
    spec = read_json(fixture_spec)
    spec["batch_id"] = "needs-reading"
    write_json(fixture_spec, spec)
    _soft_l0(monkeypatch, {"weak-run"})
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    rows = {r["run_id"]: r for r in read_rows(batch / "decisions.jsonl")}
    assert "AUTOREVIEW_CACHE_MISS" in rows["weak-run"]["reasons"]
    assert read_json(batch / "scoreboard.json")["autoreview"]["cache_misses"] == 1


def test_a_real_stored_reading_is_used_and_is_not_a_miss(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cached_spec(fixture_spec, [_reading("clean-run"), _reading("weak-run")])
    _soft_l0(monkeypatch, {"clean-run", "weak-run"})
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert read_json(batch / "scoreboard.json")["autoreview"]["cache_misses"] == 0
    assert all(
        "AUTOREVIEW_CACHE_MISS" not in r["reasons"] for r in read_rows(batch / "decisions.jsonl")
    )
    assert core.check_batch(batch, fixture_spec.parent)["passed"]


# ---- relocatable batches ---------------------------------------------------------------------


def test_receipts_record_the_batchs_own_files_relatively(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    receipt = read_json(batch / "receipts" / "decisions.json")
    assert [r["path"] for r in receipt["inputs"]] == ["tasks.jsonl", "trace.jsonl"]
    export = read_json(batch / "receipts" / "export.json")
    assert {r["path"] for r in export["outputs"]} == {
        "export/kept.jsonl",
        "export/dropped.jsonl",
        "export/review.jsonl",
    }
    # inputs outside the batch stay absolute and hashed
    intake = read_json(batch / "receipts" / "intake.json")
    external = [r for r in intake["inputs"] if Path(r["path"]).is_absolute()]
    assert external and all(r["sha256"] for r in external)


def test_a_byte_copy_of_a_batch_verifies_and_tampering_the_copy_fails(
    fixture_spec: Path, tmp_path: Path
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    moved = tmp_path / "results-home" / "copied"
    shutil.copytree(batch, moved)
    assert core.check_batch(moved, fixture_spec.parent)["passed"]
    tampered = tmp_path / "results-home" / "tampered"
    shutil.copytree(batch, tampered)
    kept = read_rows(tampered / "export" / "kept.jsonl")
    write_rows(tampered / "export" / "kept.jsonl", [*kept, {**kept[0], "run_id": "smuggled"}])
    result = core.check_batch(tampered, fixture_spec.parent)
    assert not result["passed"]
    assert {c["name"] for c in result["checks"] if c["status"] == "FAIL"} >= {"provenance"}
    assert core.check_batch(batch, fixture_spec.parent)["passed"]  # the original is untouched


def test_a_relative_receipt_path_cannot_escape_the_batch(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    path = batch / "receipts" / "decisions.json"
    receipt = read_json(path)
    receipt["inputs"][0]["path"] = "../spec.json"
    write_json(path, receipt)
    result = _check(batch, fixture_spec.parent, "provenance")
    assert result["status"] == "FAIL"


def _absolutize(batch: Path) -> None:
    """Rewrite receipts the way batches were written before they were relocatable."""
    for path in (batch / "receipts").glob("*.json"):
        receipt = read_json(path)
        for ref in receipt["inputs"] + receipt["outputs"]:
            if not Path(ref["path"]).is_absolute():
                ref["path"] = str(batch / ref["path"])
        write_json(path, receipt)


def test_old_absolute_receipts_stay_readable_in_place_and_fail_plainly_when_copied(
    fixture_spec: Path, tmp_path: Path
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    _absolutize(batch)
    assert core.check_batch(batch, fixture_spec.parent)["passed"]
    moved = tmp_path / "elsewhere" / "copy"
    shutil.copytree(batch, moved)
    result = _check(moved, fixture_spec.parent, "provenance")
    assert result["status"] == "FAIL"
    assert "another location" in result["details"]
