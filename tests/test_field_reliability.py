"""Field reliability is a measured, validated receipt: absent means "not measured"."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from evallab import cli
from evallab.production.field_reliability import (
    FILE,
    SCHEMA,
    FieldReliabilityCache,
    from_vf11_receipt,
    load_field_reliability,
    wilson,
    write_field_reliability,
)


def _document(**changes: Any) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "measured_at": "2026-10-28T03:42:26+00:00",
        "source": "VF-11 blind re-rate",
        "design": "5 runs per cluster, rated blind",
        "sample_runs": 60,
        "fields": [
            {"field": "eval_awareness", "agree_n": 33, "n": 44, "basis": None},
            {"field": "grader_or_harness_touched", "agree_n": 46, "n": 46, "basis": None},
            {"field": "suspicion", "agree_n": 48, "n": 50, "basis": "within_1"},
        ],
    } | changes


def _receipt() -> dict[str, Any]:
    return {
        "item": "VF-11 (12/12)",
        "checked_at_utc": "2026-10-28T03:42:26.212551+00:00",
        "blind_rerate_60": {
            "design": "5 runs from each of the 12 level-1 clusters, rated blind by 4 workers",
            "per_field_agreement": {
                "goal": "35/40 (87.5%)",
                "grader_or_harness_touched": "46/46 (100.0%)",
                "suspicion_within_1": "48/50 (96.0%)",
                "eval_awareness": "33/44 (75.0%)",
            },
            "material_misses": {"detail": ["7c58289bea39 record suspicion 0 ... steps 9, 11-13"]},
        },
        "tt7_bakeoff": "not present yet",
    }


def test_the_wilson_interval_matches_an_independent_calculation() -> None:
    assert wilson(46, 46) == [92.3, 100.0]
    assert wilson(35, 42) == [69.4, 91.7]
    assert wilson(0, 10)[0] == 0.0


def test_a_valid_file_is_served_most_reliable_first_with_sample_sizes(tmp_path: Path) -> None:
    (tmp_path / FILE).write_text(json.dumps(_document()), encoding="utf-8")
    served = load_field_reliability(tmp_path)
    assert served["measured"] is True and served["error"] is None
    rows = served["reliability"]["fields"]
    assert [row["field"] for row in rows] == ["grader_or_harness_touched", "suspicion", "eval_awareness"]
    assert (rows[2]["agree_n"], rows[2]["n"], rows[2]["agreement"]) == (33, 44, 75.0)
    assert rows[1]["basis"] == "within_1" and rows[0]["ci95"][0] == 92.3
    assert served["reliability"]["sample_runs"] == 60


def test_an_absent_file_is_not_measured_and_a_bad_one_says_why(tmp_path: Path) -> None:
    absent = load_field_reliability(tmp_path)
    assert absent["measured"] is False and absent["reliability"] is None
    assert absent["error"].startswith("not measured")
    (tmp_path / FILE).write_text("{broken", encoding="utf-8")
    broken = load_field_reliability(tmp_path)
    assert broken["measured"] is False and broken["error"].startswith("unreadable")


@pytest.mark.parametrize(
    "change",
    [
        {"schema": "evallab.field_reliability/v2"},
        {"measured_at": "2026-10-28T03:42:26"},  # no time zone
        {"sample_runs": 40},  # a field was rated on more runs than were sampled
        {"fields": []},
        {"fields": [{"field": "goal", "agree_n": 9, "n": 8, "basis": None}]},
        {"fields": [{"field": "goal", "agree_n": 1, "n": 0, "basis": None}]},
        {"fields": [{"field": "not_a_facet_field", "agree_n": 1, "n": 2, "basis": None}]},
        {"fields": [{"field": "goal", "agree_n": 1, "n": 2, "basis": None}] * 2},
        {"fields": [{"field": "goal", "agree_n": 1, "n": 2, "basis": "loose"}]},
        {"extra": "field"},
    ],
)
def test_inconsistent_documents_are_not_served(tmp_path: Path, change: dict[str, Any]) -> None:
    (tmp_path / FILE).write_text(json.dumps(_document(**change)), encoding="utf-8")
    served = load_field_reliability(tmp_path)
    assert served["measured"] is False and served["error"].startswith("unreadable")


def test_a_symlinked_or_oversized_file_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text(json.dumps(_document()), encoding="utf-8")
    os.symlink(real, tmp_path / FILE)
    assert load_field_reliability(tmp_path)["measured"] is False
    (tmp_path / FILE).unlink()
    (tmp_path / FILE).write_text(" " * (300 * 1024), encoding="utf-8")
    assert load_field_reliability(tmp_path)["error"].startswith("unreadable")


def test_the_cache_rereads_only_when_the_file_changes(tmp_path: Path) -> None:
    cache = FieldReliabilityCache(tmp_path)
    assert cache.get()["measured"] is False
    (tmp_path / FILE).write_text(json.dumps(_document()), encoding="utf-8")
    first = cache.get()
    assert first["measured"] is True and cache.get() is first
    (tmp_path / FILE).write_text(
        json.dumps(_document(fields=[{"field": "goal", "agree_n": 1, "n": 2, "basis": None}])),
        encoding="utf-8",
    )
    assert [row["field"] for row in cache.get()["reliability"]["fields"]] == ["goal"]


def test_the_receipt_is_reduced_to_agreement_and_sample_size_only() -> None:
    document = from_vf11_receipt(_receipt())
    assert document["sample_runs"] == 60 and document["source"] == "VF-11 blind re-rate"
    by_field = {row["field"]: row for row in document["fields"]}
    assert (by_field["goal"]["agree_n"], by_field["goal"]["n"]) == (35, 40)
    assert by_field["suspicion"]["basis"] == "within_1" and "suspicion_within_1" not in by_field
    blob = json.dumps(document)
    assert "7c58289bea39" not in blob and "tt7" not in blob and "material_misses" not in blob


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["blind_rerate_60"]["per_field_agreement"].update(goal="35/40 (90.0%)"),
        lambda r: r["blind_rerate_60"]["per_field_agreement"].update(goal="not a ratio"),
        lambda r: r["blind_rerate_60"]["per_field_agreement"].update(goal="0/0 (0.0%)"),
        lambda r: r["blind_rerate_60"]["per_field_agreement"].update(unknown_field="1/2 (50.0%)"),
        lambda r: r.update(blind_rerate_30=r["blind_rerate_60"]),  # two sample blocks
        lambda r: r["blind_rerate_60"]["per_field_agreement"].update(goal="70/80 (87.5%)"),
    ],
)
def test_a_receipt_that_contradicts_itself_is_refused(mutate: Any) -> None:
    receipt = _receipt()
    mutate(receipt)
    with pytest.raises((ValueError, KeyError)):
        from_vf11_receipt(receipt)


def test_writing_refuses_to_silently_replace_a_different_file(tmp_path: Path) -> None:
    first = write_field_reliability(tmp_path, _document())
    assert first["fields"] == 3 and len(first["sha256"]) == 64
    assert write_field_reliability(tmp_path, _document())["sha256"] == first["sha256"]  # same bytes
    changed = _document(design="another design")
    with pytest.raises(FileExistsError):
        write_field_reliability(tmp_path, changed)
    assert json.loads((tmp_path / FILE).read_text())["design"] == "5 runs per cluster, rated blind"
    write_field_reliability(tmp_path, changed, replace=True)
    assert json.loads((tmp_path / FILE).read_text())["design"] == "another design"
    with pytest.raises(ValueError):
        write_field_reliability(tmp_path / "other", _document(sample_runs=0))
    assert not (tmp_path / "other" / FILE).exists()
    assert [p.name for p in tmp_path.iterdir()] == [FILE]  # no temporary file is left behind


def test_the_cli_seeds_the_file_from_a_receipt_and_it_is_then_served(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    receipt = tmp_path / "vf11-receipt.json"
    receipt.write_text(json.dumps(_receipt()), encoding="utf-8")
    target = tmp_path / "run-atlas"
    arguments = [
        "prod", "import-field-reliability", "--receipt", str(receipt),
        "--output-directory", str(target),
    ]
    assert cli.run_cli(arguments, workspace=tmp_path) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["command"] == "import-field-reliability" and result["fields"] == 4
    served = load_field_reliability(target)
    assert served["measured"] is True
    assert {row["field"] for row in served["reliability"]["fields"]} == {
        "goal", "grader_or_harness_touched", "suspicion", "eval_awareness",
    }
