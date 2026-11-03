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


# ---- readings are decided by reason codes, and the current L0 decides eligibility ----------------


def test_a_not_eligible_row_carrying_model_details_is_still_no_reading(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _not_eligible("clean-run")
    row["stage_decisions"]["autoreview"]["details"].update(model="synthetic/model", attested=True)
    _cached_spec(fixture_spec, [row, _not_eligible("weak-run")])
    _soft_l0(monkeypatch, {"clean-run"})
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    decision = next(r for r in read_rows(batch / "decisions.jsonl") if r["run_id"] == "clean-run")
    assert decision["decision"] == "review" and "AUTOREVIEW_CACHE_MISS" in decision["reasons"]
    assert read_json(batch / "scoreboard.json")["autoreview"]["cache_misses"] == 1


def test_a_stale_reading_never_applies_when_the_current_l0_says_ineligible(
    fixture_spec: Path,
) -> None:
    flagged = _reading("clean-run")
    flagged["stage_decisions"]["autoreview"].update(
        decision="review",
        reasons=["AUTOREVIEW_SUSPICION_80"],
        details={
            **flagged["stage_decisions"]["autoreview"]["details"],
            "suspicion": 80,
            "verdict": "suspicious",
        },
    )
    _cached_spec(fixture_spec, [flagged, _reading("weak-run")])
    batch = core.run_line(fixture_spec, fixture_spec.parent)  # real L0: nothing is eligible
    row = next(r for r in read_rows(batch / "decisions.jsonl") if r["run_id"] == "clean-run")
    assert row["decision"] == "keep"
    trace = read_rows(batch / "trace.jsonl")
    assert all(t["autoreview"]["reasons"] == ["AUTOREVIEW_NOT_ELIGIBLE"] for t in trace)
    assert all("call" not in t["autoreview"] for t in trace)
    assert read_json(batch / "receipts" / "trace.json")["models"] == []


def test_a_line_export_with_cache_misses_can_be_reused_as_a_cache(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cached_spec(fixture_spec, [_not_eligible("clean-run"), _not_eligible("weak-run")])
    _soft_l0(monkeypatch, {"clean-run"})
    first = core.run_line(fixture_spec, fixture_spec.parent)
    spec = read_json(fixture_spec)
    spec["batch_id"] = "reuse"
    spec["autoreview"]["cache"] = str(first / "export")
    write_json(fixture_spec, spec)
    second = core.run_line(fixture_spec, fixture_spec.parent)
    assert read_json(second / "scoreboard.json")["autoreview"]["cache_misses"] == 1


def test_single_row_label_object_with_a_labels_tag_list_is_a_row(tmp_path: Path) -> None:
    path = tmp_path / "labels.json"
    path.write_text('{"id": "run-1", "is_cheat": true, "labels": []}')
    assert hd.load_labels(path, repo_root=tmp_path) == {"run-1": True}


# ---- receipts form a closed set --------------------------------------------------------------


def _edit_receipt(batch: Path, stage: str, change: Any) -> None:
    path = batch / "receipts" / f"{stage}.json"
    receipt = read_json(path)
    change(receipt)
    write_json(path, receipt)


def _secret(tmp_path: Path) -> Path:
    keys = tmp_path / "research" / "answer-keys"
    keys.mkdir(parents=True)
    secret = keys / "secret.jsonl"
    write_rows(secret, [{"secret": "top-secret-answer"}])
    return secret


def test_a_symlink_anywhere_in_a_batch_is_refused(fixture_spec: Path, tmp_path: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    secret = _secret(tmp_path)
    (batch / "inside_link.jsonl").symlink_to(secret)
    with pytest.raises(ValueError, match="symlink"):
        core.check_batch(batch, fixture_spec.parent)


def test_a_symlinked_station_snapshot_directory_is_refused(
    fixture_spec: Path, tmp_path: Path
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    outside = tmp_path / "ext_station"
    shutil.copytree(batch / "evidence" / "station-inputs", outside)
    shutil.rmtree(batch / "evidence" / "station-inputs")
    (batch / "evidence" / "station-inputs").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        core.check_batch(batch, fixture_spec.parent)


def test_the_cli_check_of_a_symlinked_batch_fails_and_leaves_no_pass(
    fixture_spec: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab import cli

    monkeypatch.setattr(cli, "instrument_openinference", lambda: None)
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    (batch / "inside_link.jsonl").symlink_to(_secret(tmp_path))
    assert cli.run_cli(["line", "check", str(batch)], workspace=fixture_spec.parent) == 2
    assert read_json(batch / "check.json")["passed"] is False


def test_an_absolute_receipt_path_to_an_answer_key_is_not_read(
    fixture_spec: Path, tmp_path: Path
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    secret = _secret(tmp_path)
    ref = {"path": str(secret), "sha256": core.digest(secret), "rows": 1}
    _edit_receipt(batch, "decisions", lambda r: r["inputs"].append(ref))
    result = _check(batch, fixture_spec.parent, "provenance")
    assert result["status"] == "FAIL" and "declared" in result["details"]


def test_an_undeclared_outside_input_is_refused_even_if_its_hash_matches(
    fixture_spec: Path, tmp_path: Path
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    stray = tmp_path / "stray.txt"
    stray.write_text("x")
    ref = {"path": str(stray), "sha256": core.digest(stray)}
    _edit_receipt(batch, "export", lambda r: r["inputs"].append(ref))
    assert _check(batch, fixture_spec.parent, "provenance")["status"] == "FAIL"


def test_an_output_outside_the_batch_is_refused(fixture_spec: Path, tmp_path: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    stray = tmp_path / "stray.jsonl"
    write_rows(stray, [{"a": 1}])
    ref = {"path": str(stray), "sha256": core.digest(stray), "rows": 1}
    _edit_receipt(batch, "export", lambda r: r["outputs"].append(ref))
    result = _check(batch, fixture_spec.parent, "provenance")
    assert result["status"] == "FAIL" and "not a file of this batch" in result["details"]


def test_a_batch_whose_spec_declares_a_forbidden_source_is_refused(
    fixture_spec: Path, tmp_path: Path
) -> None:
    # Declaring an outside input does not admit it: the DEV gate still applies to the spec.
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    spec = read_json(batch / "spec.json")
    forbidden = tmp_path / "test-split" / "rollouts.jsonl"
    forbidden.parent.mkdir()
    shutil.copy(spec["sources"][0]["rollouts"], forbidden)
    spec["sources"][0]["rollouts"] = str(forbidden)
    write_json(batch / "spec.json", spec)
    ref = {"path": str(forbidden), "sha256": core.digest(forbidden)}
    _edit_receipt(batch, "intake", lambda r: r["inputs"].append(ref))
    with pytest.raises(ValueError, match="TEST split"):
        core.check_batch(batch, fixture_spec.parent)


def test_label_isolation_compares_canonical_paths(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    labels = Path(read_json(fixture_spec)["sources"][0]["labels"])
    spelled = f"{labels.parent}/./{labels.name}"  # same file, different spelling (pathlib hides it)
    ref = {"path": spelled, "sha256": core.digest(labels)}
    _edit_receipt(batch, "decisions", lambda r: r["inputs"].append(ref))
    result = _check(batch, fixture_spec.parent, "label_isolation")
    assert result["status"] == "FAIL" and "labels entered" in result["details"]


def test_scoreboard_row_counting_cannot_leave_the_batch(fixture_spec: Path, tmp_path: Path) -> None:
    from evallab.line.scoreboard import build_scoreboard

    batch = core.run_line(fixture_spec, fixture_spec.parent)
    _secret(tmp_path)
    escape = "../" * 12 + str(tmp_path / "research" / "answer-keys" / "secret.jsonl").lstrip("/")

    def change(receipt: dict[str, Any]) -> None:
        receipt["counts"] = {}
        receipt["inputs"].append({"path": escape})

    _edit_receipt(batch, "decisions", change)
    with pytest.raises(ValueError, match="leaves its batch"):
        build_scoreboard(batch, fixture_spec.parent)
