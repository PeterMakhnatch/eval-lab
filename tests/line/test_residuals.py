"""Empty label rows, AutoReview cache misses and relocatable batches."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from evallab import hack_drop as hd
from evallab.line import core
from evallab.line.io import read_bytes, read_json, read_rows, write_json, write_rows


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


# ---- file identity: hard links, case variants, declared set, descriptors ---------------------


def test_a_hard_link_to_an_answer_key_inside_a_batch_is_refused(
    fixture_spec: Path, tmp_path: Path
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    secret = _secret(tmp_path)
    os.link(secret, batch / "hard_secret.jsonl")
    ref = {"path": "hard_secret.jsonl", "sha256": core.digest(secret), "rows": 1}
    _edit_receipt(batch, "decisions", lambda r: r["inputs"].append(ref))
    with pytest.raises(ValueError, match="hard-linked"):
        core.check_batch(batch, fixture_spec.parent)


def test_a_hard_link_to_a_label_file_inside_a_batch_is_refused(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    labels = Path(read_json(fixture_spec)["sources"][0]["labels"])
    os.link(labels, batch / "hard_labels.jsonl")
    ref = {"path": "hard_labels.jsonl", "sha256": core.digest(labels), "rows": 2}
    _edit_receipt(batch, "decisions", lambda r: r["inputs"].append(ref))
    with pytest.raises(ValueError, match="hard-linked"):
        core.check_batch(batch, fixture_spec.parent)


def test_a_stage_may_list_only_its_own_batch_files(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    (batch / "extra.jsonl").write_text('{"a": 1}\n')
    ref = {"path": "extra.jsonl", "sha256": core.digest(batch / "extra.jsonl"), "rows": 1}
    _edit_receipt(batch, "decisions", lambda r: r["inputs"].append(ref))
    result = _check(batch, fixture_spec.parent, "provenance")
    assert result["status"] == "FAIL" and "unexpected files" in result["details"]


def test_the_judge_may_list_its_station_snapshots_and_nothing_else(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    judge = read_json(batch / "receipts" / "judge.json")
    snapshots = [r["path"] for r in judge["inputs"] if "station-inputs" in r["path"]]
    assert snapshots and core.check_batch(batch, fixture_spec.parent)["passed"]
    (batch / "evidence" / "station-inputs" / "nested").mkdir()
    (batch / "evidence" / "station-inputs" / "nested" / "x.jsonl").write_text("{}\n")
    ref = {
        "path": "evidence/station-inputs/nested/x.jsonl",
        "sha256": core.digest(batch / "evidence" / "station-inputs" / "nested" / "x.jsonl"),
    }
    _edit_receipt(batch, "judge", lambda r: r["inputs"].append(ref))
    assert _check(batch, fixture_spec.parent, "provenance")["status"] == "FAIL"


def _labels_as_task_status(fixture_spec: Path, link: Path, *, spelled: str | None = None) -> None:
    """One file serving as both the label source and the task-status source."""
    spec = read_json(fixture_spec)
    labels = Path(spec["sources"][0]["labels"])
    write_json(
        labels,
        {
            "rows": [{"id": "clean-run", "is_cheat": False}, {"id": "weak-run", "is_cheat": True}],
            "tasks": [
                {"task_id": "plain-task", "status": "certified", "reason": "ALL_GATES_PASS"},
                {"task_id": "weak-task", "status": "weak", "reason": "ATTACK_ACCEPTED"},
            ],
        },
    )
    if spelled is None:
        os.link(labels, link)
        spec["task_status"] = str(link)
    else:
        spec["task_status"] = spelled
    write_json(fixture_spec, spec)


def test_label_isolation_sees_a_hard_linked_name_for_a_label_file(
    fixture_spec: Path, tmp_path: Path
) -> None:
    _labels_as_task_status(fixture_spec, tmp_path / "other-name.json")
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    result = _check(batch, fixture_spec.parent, "label_isolation")
    assert result["status"] == "FAIL" and "labels entered" in result["details"]


def test_label_isolation_sees_a_case_variant_of_a_label_file(
    fixture_spec: Path, tmp_path: Path
) -> None:
    labels = Path(read_json(fixture_spec)["sources"][0]["labels"])
    variant = labels.with_name(labels.name.upper())
    if not variant.exists() or not labels.samefile(variant):
        pytest.skip("case-sensitive filesystem")
    _labels_as_task_status(fixture_spec, tmp_path / "unused", spelled=str(variant))
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert _check(batch, fixture_spec.parent, "label_isolation")["status"] == "FAIL"


def test_an_edited_runs_file_cannot_admit_an_outside_input(
    fixture_spec: Path, tmp_path: Path
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    stray = tmp_path / "private-notes.txt"
    stray.write_text("not a rollout\n")
    runs = read_rows(batch / "runs.jsonl")
    runs[0]["rollout_path"] = str(stray)
    write_rows(batch / "runs.jsonl", runs)
    ref = {"path": str(stray), "sha256": core.digest(stray)}
    _edit_receipt(batch, "intake", lambda r: r["inputs"].append(ref))
    result = _check(batch, fixture_spec.parent, "provenance")
    assert result["status"] == "FAIL" and "declared" in result["details"]


def test_reads_refuse_a_final_symlink_a_directory_and_other_hard_links(tmp_path: Path) -> None:
    real = tmp_path / "real.jsonl"
    real.write_text('{"a": 1}\n')
    assert read_bytes(real) == b'{"a": 1}\n'
    (tmp_path / "link.jsonl").symlink_to(real)
    with pytest.raises(OSError):
        read_bytes(tmp_path / "link.jsonl")
    os.link(real, tmp_path / "second.jsonl")
    assert read_bytes(real) == b'{"a": 1}\n'  # tolerated unless the caller asks for one link
    with pytest.raises(ValueError, match="hard links"):
        read_bytes(real, single_link=True)
    with pytest.raises(ValueError, match="regular file"):
        read_bytes(tmp_path)


def test_a_declared_input_swapped_for_a_symlink_fails_provenance(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    labels = Path(read_json(fixture_spec)["sources"][0]["labels"])
    copy = labels.with_name("labels-copy.jsonl")
    shutil.copy(labels, copy)
    labels.unlink()
    labels.symlink_to(copy)  # same bytes, but the descriptor read refuses the link
    result = _check(batch, fixture_spec.parent, "provenance")
    assert result["status"] == "FAIL"


# ---- scoreboard schema versions -------------------------------------------------------------


def _as_version_1(batch: Path) -> None:
    """Rewrite a scoreboard into the shape batches had before it carried a version."""
    path = batch / "scoreboard.json"
    board = read_json(path)
    del board["schema_version"], board["autoreview"]
    write_json(path, board)


def test_a_batch_with_the_pre_versioning_scoreboard_still_passes_check(
    fixture_spec: Path,
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert read_json(batch / "scoreboard.json")["schema_version"] == 2
    _as_version_1(batch)
    assert core.check_batch(batch, fixture_spec.parent)["passed"]


def test_a_pre_versioning_scoreboard_is_still_compared_on_its_counted_fields(
    fixture_spec: Path,
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    _as_version_1(batch)
    board = read_json(batch / "scoreboard.json")
    board["judge_load"]["review"] += 1
    write_json(batch / "scoreboard.json", board)
    result = _check(batch, fixture_spec.parent, "scoreboard_recompute")
    assert result["status"] == "FAIL" and "differs" in result["details"]


def test_a_scoreboard_newer_than_this_code_is_refused_plainly(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    board = read_json(batch / "scoreboard.json")
    board["schema_version"] = 3
    write_json(batch / "scoreboard.json", board)
    result = _check(batch, fixture_spec.parent, "scoreboard_recompute")
    assert result["status"] == "FAIL" and "newer line" in result["details"]


def test_downgrading_the_version_does_not_hide_a_current_scoreboard_section(
    fixture_spec: Path,
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    board = read_json(batch / "scoreboard.json")
    board["schema_version"] = 1  # claims the old shape but still carries the new section
    write_json(batch / "scoreboard.json", board)
    assert _check(batch, fixture_spec.parent, "scoreboard_recompute")["status"] == "FAIL"


def test_a_pre_versioning_scoreboard_renders_without_the_new_section(fixture_spec: Path) -> None:
    from evallab.line.scoreboard import render_scoreboard

    batch = core.run_line(fixture_spec, fixture_spec.parent)
    _as_version_1(batch)
    assert "autoreview_cache_misses" not in render_scoreboard(read_json(batch / "scoreboard.json"))
