"""The detector scope keeps DEV and cross-test measures apart and holds counts only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from evallab.production import detector_scope as scope

SECRET = "trace-secret-0001"


def _cell(**overrides: Any) -> dict[str, Any]:
    cell = {
        "detector": "cascade", "cohort": "goodfire_dev", "measure": "run_flagged",
        "hits": 10, "positives": 80, "negatives": 70, "false_alarms": 2,
        "route": "judge/route", "recorded_at": "2026-10-28T07:56:46+00:00", "date_basis": "cascade measurement",
        "source": "receipt/measurement.json", "version": "window-cascade/v8",
    }
    return {**cell, **overrides}


def _hackbench(**overrides: Any) -> dict[str, Any]:
    cell = {
        "detector": "facets", "cohort": "hackbench_natural", "measure": "exact_command",
        "hits": 20, "positives": 41, "route": "judge/route", "recorded_at": "2026-10-28T00:54:07+00:00",
        "date_basis": "freeze", "source": "receipt/union-command.json",
        "version": "window-cascade/v7", "note": "v7-to-v8 compatibility checked in the receipt",
    }
    return {**cell, **overrides}


def _document(*cells: dict[str, Any]) -> dict[str, Any]:
    return {"schema": scope.SCHEMA, "cells": list(cells)}


@pytest.mark.parametrize(
    "bad",
    [
        _hackbench(false_alarms=0),
        _hackbench(negatives=5),
        _hackbench(measure="run_flagged"),
        _hackbench(detector="cascade", version="window-cascade/v4"),
        _hackbench(detector="cascade", version=None),
        _hackbench(detector="cascade", note=None),
        _cell(detector="facets"),
        _cell(measure="exact_command"),
        _cell(negatives=None, false_alarms=None),
        _cell(hits=81),
        _cell(false_alarms=71),
        _cell(positives=0, hits=0),
        _cell(version="window-cascade/v7"),
        _cell(recorded_at="28 Oct 2026"),
        _cell(recorded_at="2026-10-28T07:56:46"),
        _cell(run_id=SECRET),
        _cell(trajectory="text"),
    ],
)
def test_a_cell_that_could_hide_a_false_alarm_rate_or_carry_a_record_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        scope.parse_scope(_document(bad))


def test_a_detector_cannot_appear_twice_on_one_cohort_and_extra_document_fields_are_refused() -> None:
    with pytest.raises(ValidationError, match="twice"):
        scope.parse_scope(_document(_cell(), _cell(hits=9)))
    with pytest.raises(ValidationError):
        scope.parse_scope({**_document(_cell()), "labels": [1]})


def test_conflicting_cascade_judges_cannot_be_hidden_under_one_row_route() -> None:
    with pytest.raises(ValidationError, match="route must agree"):
        scope.parse_scope(_document(
            _cell(),
            _cell(cohort="cheatbench_dev", route="other/judge", positives=75, negatives=75),
        ))


def test_intervals_are_wilson_and_match_the_recorded_receipts() -> None:
    cells = scope.parse_scope(_document(
        _cell(), _cell(cohort="cheatbench_dev", hits=19, positives=75, false_alarms=0, negatives=75),
        _hackbench(detector="cascade", hits=1), _hackbench(),
    ))
    served = scope.table(cells, None, [], {})
    by_key = {row["tool"]: row["cohorts"] for row in served["rows"]}
    assert by_key["cascade"]["goodfire_dev"]["hit_ci"] == [6.9, 21.5]
    assert by_key["cascade"]["goodfire_dev"]["false_alarm_ci"] == [0.8, 9.8]
    assert by_key["cascade"]["cheatbench_dev"]["false_alarm_ci"] == [0.0, 4.9]
    assert by_key["cascade"]["hackbench_natural"]["hit_ci"] == [0.4, 12.6]
    assert by_key["facets"]["hackbench_natural"]["hit_ci"] == [34.3, 63.5]
    assert served["hackbench_runs"] == 41


def test_a_hackbench_cell_serves_no_false_alarm_rate() -> None:
    served = scope.table(scope.parse_scope(_document(_hackbench())), None, [], {})
    cell = served["rows"][0]["cohorts"]["hackbench_natural"]
    assert cell["measure"] == "exact_command"
    assert cell["negatives"] is None and cell["false_alarms"] is None and cell["false_alarm_ci"] is None


def _tools() -> list[dict[str, Any]]:
    def entry(tp: int, fp: int, read: int, clean_read: int, positives: int = 80, negatives: int = 70) -> dict[str, Any]:
        return {"tp": tp, "fp": fp, "positives": positives, "negatives": negatives,
                "positives_decided": read, "negatives_decided": clean_read}

    return [
        {"tool": "facets", "populations": {"goodfire_cm9": entry(7, 1, 80, 70), "cheatbench_dev": None}},
        {"tool": "scout", "populations": {"goodfire_cm9": entry(0, 0, 80, 70), "cheatbench_dev": entry(19, 34, 75, 75, 75, 75)}},
        {"tool": "scout_metr", "populations": {
            "goodfire_cm9": entry(1, 0, 3, 5), "cheatbench_dev": entry(0, 0, 0, 0)}},
    ]


def test_scorecard_rows_use_decided_runs_and_say_when_nothing_was_read() -> None:
    served = scope.table(None, "not retained", _tools(), {"tool_scorecard": "2026-10-27T09:00:00+00:00", "atlas_scorecard": None})
    rows = {row["tool"]: row["cohorts"] for row in served["rows"]}
    metr = rows["scout_metr"]["goodfire_dev"]
    assert (metr["hits"], metr["read"], metr["positives"]) == (1, 3, 80)
    assert metr["hit_ci"] == scope.wilson(1, 3) and metr["false_alarm_ci"] == scope.wilson(0, 5)
    assert rows["scout_metr"]["cheatbench_dev"] == {"state": "absent", "reason": "no runs read"}
    assert rows["scout"]["goodfire_dev"]["recorded_at"] == "2026-10-27T09:00:00+00:00"
    # No dated source gives no time, never a guessed one.
    assert rows["facets"]["goodfire_dev"]["recorded_at"] is None and rows["facets"]["goodfire_dev"]["date_basis"] is None
    assert rows["facets"]["cheatbench_dev"] == {"state": "absent", "reason": "no retained scorecard row"}


@pytest.mark.parametrize(("read", "clean_read"), [(0, 5), (3, 0)])
def test_one_unread_class_is_not_presented_as_a_zero_percent_result(read: int, clean_read: int) -> None:
    tools = _tools()
    entry = tools[-1]["populations"]["goodfire_cm9"]
    entry.update(positives_decided=read, negatives_decided=clean_read, tp=0)
    served = scope.table(None, None, tools, {})
    cell = next(row for row in served["rows"] if row["tool"] == "scout_metr")["cohorts"]["goodfire_dev"]
    assert cell["state"] == "measured" and cell["read"] == read and cell["negatives_read"] == clean_read
    assert (cell["hit_ci"] is None) == (read == 0)
    assert (cell["false_alarm_ci"] is None) == (clean_read == 0)


def test_without_the_scope_file_every_receipt_cell_is_absent_and_says_why() -> None:
    served = scope.table(None, "not retained", _tools(), {})
    assert served["present"] is False and served["error"] == "not retained"
    rows = {row["tool"]: row["cohorts"] for row in served["rows"]}
    for tool in ("facets", "cascade", "stage1"):
        assert rows[tool]["hackbench_natural"]["state"] == "absent"
        assert "no detector scope retained" in rows[tool]["hackbench_natural"]["reason"]
    for tool in ("cascade", "stage1"):
        assert rows[tool]["goodfire_dev"]["state"] == "absent"
    # Scanners never ran on HackBench: that is a fact, not a missing file.
    assert rows["scout"]["hackbench_natural"]["reason"] == "not run on HackBench"
    assert rows["scout_metr"]["hackbench_natural"]["reason"] == "not run on HackBench"
    # What the scorecards hold is still shown.
    assert rows["scout"]["cheatbench_dev"]["hits"] == 19
    assert served["hackbench_runs"] is None


# --- exporter -----------------------------------------------------------------------------


def _write(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _receipts(root: Path, **changes: Any) -> dict[str, Path]:
    arms = {name: {"natural": {"n": 41, "exact_hit": hits}} for name, hits in
            (("stage1", 4), ("cascade", 1), ("facets", 20))}
    hackbench = root / "cm11-hackbench"
    _write(hackbench / "union-command.json", {
        "schema": "cm11-union/v1", "layout": "command", "judge": "judge/route", "arms": arms,
        "freeze_git_head": "frozen-revision",
        "submission_tamper": {"hackbench": {"traces_with_card": 0}},
        **changes.get("union", {}),
    })
    _write(hackbench / "freeze.json", {
        "schema": "cm11-freeze/v1", "frozen_at_utc": "2026-10-28T00:54:07Z",
        "cascade_version": "window-cascade/v7", "git_head": "frozen-revision",
        **changes.get("freeze", {}),
    })

    def after(hits: int, fa: int, positives: int, negatives: int) -> dict[str, Any]:
        return {"after": {"recall": {"count": hits, "denominator": positives},
                          "false_alarms": {"count": fa, "denominator": negatives}}}

    measurement = _write(root / "cm11b" / "measurement.json", {
        "schema_version": "cm11b-tamper-demotion/v1",
        "scope": {"dev_only": True, "hackbench_used_for_tuning": False, "strict_cross_key_guard": True, **changes.get("scope", {})},
        "versions": {"before": "window-cascade/v7", "after": "window-cascade/v8"}, "model_route": "judge/route", "arm": "pf_r",
        "created_at_utc": "2026-10-28T07:56:46.086928+00:00",
        "comparison": {"goodfire": after(10, 2, 80, 70), "cheatbench": after(19, 0, 75, 75)},
        **changes.get("measurement", {}),
    })

    def scores(positives: int, clean: int, hit: int, false_alarm: int, summary: tuple[int, int], corpus: str) -> dict[str, Any]:
        rows = [{"trace_id": f"{SECRET}-p{i}", "positive": True, "stratum": "enacted_yes", "stage1_any": i < hit}
                for i in range(positives)]
        rows += [{"trace_id": f"{SECRET}-c{i}", "positive": False, "stratum": "clean_no", "stage1_any": i < false_alarm}
                 for i in range(clean)]
        # Unscored rows are neither cheating nor clean.
        rows += [{"trace_id": f"{SECRET}-u{i}", "positive": False, "stratum": "considered_only", "stage1_any": True}
                 for i in range(5)]
        return {"summary": {"corpus": corpus, "positives": summary[0], "negatives": summary[1]}, "traces": rows}

    goodfire = _write(root / "cm9" / "g.json", scores(80, 70, 60, 32, changes.get("summary", (80, 70)), "goodfire"))
    cheatbench = _write(root / "cm9" / "cb.json", scores(75, 75, 41, 26, (75, 75), "cheatbench"))
    return {"hackbench": hackbench, "measurement": measurement, "goodfire": goodfire, "cheatbench": cheatbench}


def _export(paths: dict[str, Path]) -> list[dict[str, Any]]:
    return [
        *scope.from_hackbench(paths["hackbench"]),
        *scope.from_cascade_measurement(paths["measurement"]),
        *scope.from_stage1_scores(paths["goodfire"], paths["cheatbench"]),
    ]


def test_the_exporter_builds_seven_counts_only_cells_from_the_receipts(tmp_path: Path) -> None:
    document = scope.build_scope(_export(_receipts(tmp_path)))
    cells = {(cell["detector"], cell["cohort"]): cell for cell in document["cells"]}
    assert len(cells) == 7
    assert [(cells["facets", "hackbench_natural"][key]) for key in ("hits", "positives")] == [20, 41]
    assert cells["cascade", "hackbench_natural"]["hits"] == 1 and cells["stage1", "hackbench_natural"]["hits"] == 4
    assert cells["cascade", "hackbench_natural"]["version"] == "window-cascade/v7"
    assert (cells["cascade", "goodfire_dev"]["hits"], cells["cascade", "goodfire_dev"]["false_alarms"]) == (10, 2)
    assert cells["cascade", "cheatbench_dev"]["version"] == "window-cascade/v8"
    # Unscored rows are not clean runs: 70 clean, not 75.
    assert (cells["stage1", "goodfire_dev"]["negatives"], cells["stage1", "goodfire_dev"]["false_alarms"]) == (70, 32)
    assert (cells["stage1", "cheatbench_dev"]["hits"], cells["stage1", "cheatbench_dev"]["positives"]) == (41, 75)
    assert cells["facets", "hackbench_natural"]["recorded_at"] == "2026-10-28T00:54:07+00:00"
    assert cells["cascade", "goodfire_dev"]["source"] == "cm11b/measurement.json"


def test_the_written_file_has_no_run_id_path_or_label_and_replaces_only_on_request(tmp_path: Path) -> None:
    paths = _receipts(tmp_path)
    document = scope.build_scope(_export(paths))
    receipt = scope.write_scope(tmp_path / "out", document)
    text = (tmp_path / "out" / scope.FILE).read_text(encoding="utf-8")
    assert receipt["cells"] == 7 and SECRET not in text and str(tmp_path) not in text
    assert scope.write_scope(tmp_path / "out", document)["sha256"] == receipt["sha256"]
    changed = scope.build_scope(_export(paths)[:3])
    with pytest.raises(FileExistsError):
        scope.write_scope(tmp_path / "out", changed)
    assert scope.write_scope(tmp_path / "out", changed, replace=True)["cells"] == 3


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"union": {"submission_tamper": {"hackbench": {"traces_with_card": 2}}}}, "submission_tamper fired"),
        ({"union": {"arms": {
            "stage1": {"natural": {"n": 41, "exact_hit": 4}},
            "cascade": {"natural": {"n": 40, "exact_hit": 1}},
            "facets": {"natural": {"n": 41, "exact_hit": 20}}}}}, "different natural"),
        ({"union": {"schema": "other/v1"}}, "cm11-union/v1"),
        ({"freeze": {"git_head": "other-revision"}}, "match its v7"),
        ({"freeze": {"git_head": None}, "union": {"freeze_git_head": None}}, "match its v7"),
        ({"freeze": {"cascade_version": "window-cascade/v6"}}, "match its v7"),
        ({"measurement": {"versions": {"before": "window-cascade/v7", "after": "window-cascade/v9"}}}, "v7-to-v8"),
        ({"measurement": {"arm": "not-frozen-arm"}}, "pf_r"),
        ({"scope": {"strict_cross_key_guard": False}}, "strict cross-key"),
        ({"scope": {"hackbench_used_for_tuning": True}}, "DEV-only"),
        ({"summary": (79, 70)}, "disagree with their own summary"),
    ],
)
def test_the_exporter_refuses_receipts_that_do_not_support_the_claim(
    tmp_path: Path, changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _export(_receipts(tmp_path, **changes))


def test_the_exporter_refuses_a_linked_receipt(tmp_path: Path) -> None:
    paths = _receipts(tmp_path)
    real = paths["measurement"]
    link = tmp_path / "linked.json"
    link.symlink_to(real)
    with pytest.raises(ValueError, match="regular file"):
        scope.from_cascade_measurement(link)


def test_the_cli_writes_the_file_and_refuses_to_overwrite_a_different_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from evallab import cli

    paths = _receipts(tmp_path)
    arguments = [
        "prod", "export-detector-scope", "--hackbench-directory", str(paths["hackbench"]),
        "--cascade-measurement", str(paths["measurement"]), "--goodfire-scores", str(paths["goodfire"]),
        "--cheatbench-scores", str(paths["cheatbench"]), "--output-directory", str(tmp_path / "trace-tools"),
    ]
    assert cli.run_cli(arguments, workspace=tmp_path) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["command"] == "export-detector-scope" and receipt["cells"] == 7
    target = tmp_path / "trace-tools" / scope.FILE
    target.write_text("{}", encoding="utf-8")
    assert cli.run_cli(arguments, workspace=tmp_path) != 0
    assert "already differs" in capsys.readouterr().err
    assert cli.run_cli([*arguments, "--replace"], workspace=tmp_path) == 0


def test_stage1_scores_cannot_be_swapped_between_corpora(tmp_path: Path) -> None:
    paths = _receipts(tmp_path)
    with pytest.raises(ValueError, match="different corpus"):
        scope.from_stage1_scores(paths["cheatbench"], paths["goodfire"])


@pytest.mark.parametrize("key", ["positive", "stage1_any"])
def test_stage1_decisions_are_not_coerced_from_strings(tmp_path: Path, key: str) -> None:
    paths = _receipts(tmp_path)
    document = json.loads(paths["goodfire"].read_text(encoding="utf-8"))
    document["traces"][0][key] = "false"
    _write(paths["goodfire"], document)
    with pytest.raises(ValueError, match="must be booleans"):
        scope.from_stage1_scores(paths["goodfire"], paths["cheatbench"])
