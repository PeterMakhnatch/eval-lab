from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.insights import validity
from evallab.insights.validity import check_folder_validity


def test_markers_loaders_and_private_rows(tmp_path: Path) -> None:
    private = "DO NOT COPY THIS PRIVATE CASE TEXT"
    (tmp_path / "TEST-receipt.json").write_text(json.dumps({
        "n": 3,
        "rows": [
            {"text": private, "split": "test", "label_loader": "first-loader",
             "label_sha256": "hash-a", "model": "model-a", "cost_usd": 0.25},
            {"text": "answer_keys and held_out", "label_loader": "second-loader",
             "label_source": "private-label-path", "served_model": "model-b", "usd": 0.5},
            {"text": "permission refused 403", "in_sample": True},
        ],
    }), encoding="utf-8")
    report = check_folder_validity(tmp_path)
    codes = {finding.code for finding in report.findings}
    assert {"heldout_marker", "multiple_label_loaders", "missing_label_source"} <= codes
    assert report.rows_checked == 3
    assert report.rows_without_label_source == 1
    assert len([item for item in report.label_sources if item.field == "label_loader"]) == 2
    assert len(report.models_routes) == 2
    assert report.spend_usd == 0.75
    assert report.receipts[0].sample_sizes["/n"] == 3
    assert report.access_failures == {"403": 1, "permission": 1, "refusal": 1}
    serialized = report.model_dump_json()
    assert private not in serialized
    assert "permission refused 403" not in serialized
    assert "first-loader" in serialized and "private-label-path" in serialized
    assert {item.value for item in report.models_routes} == {"model-a", "model-b"}
    assert all(finding.finding_id.startswith("V-") for finding in report.findings)
    assert any("/rows/1/text" in finding.pointers for finding in report.findings)
    assert all(finding.message and finding.severity for finding in report.findings)


def test_jsonl_csv_and_inherited_label_source(tmp_path: Path) -> None:
    (tmp_path / "a.jsonl").write_text(
        '{"label_source":"source","n":4,"spend":1.25}\n'
        '{"n":2,"spend":0.75}\n', encoding="utf-8",
    )
    (tmp_path / "b.csv").write_text(
        "label_source,total,cost_usd\nsource,6,0.5\n", encoding="utf-8",
    )
    (tmp_path / "c.json").write_text(
        '{"label_source":"shared","rows":[{"caught":true},{"caught":false}]}',
        encoding="utf-8",
    )
    report = check_folder_validity(tmp_path)
    assert report.rows_checked == 5
    assert report.rows_without_label_source == 1
    assert report.spend_usd == 2.5
    assert report.files_checked == 3


def test_skips_large_links_and_bad_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "input"
    folder.mkdir()
    external = tmp_path / "external.json"
    external.write_text('{"text":"EXTERNAL-ROW-SENTINEL"}', encoding="utf-8")
    (folder / "linked.json").symlink_to(external)
    (folder / "huge.json").write_text('"' + "x" * 60 + '"', encoding="utf-8")
    (folder / "bad.json").write_text("{", encoding="utf-8")
    monkeypatch.setattr(validity, "MAX_BYTES", 32)
    report = check_folder_validity(folder)
    assert report.files_skipped == 3
    assert {finding.code for finding in report.findings} == {
        "path_escape", "oversize", "unreadable", "scan_summary",
    }
    assert "EXTERNAL-ROW-SENTINEL" not in report.model_dump_json()


def test_metadata_values_are_bounded_but_counts_continue(tmp_path: Path) -> None:
    rows = [{"model": f"model-{index}", "task_text": "PRIVATE TASK"} for index in range(51)]
    rows.extend([{"model": "model-0"}] * 3)
    rows.append({"served_model": "long-private-value-" * 20})
    (tmp_path / "models.json").write_text(json.dumps(rows), encoding="utf-8")
    report = check_folder_validity(tmp_path)
    models = [item for item in report.models_routes if item.field == "model"]
    assert len(models) == 50
    assert next(item for item in models if item.value == "model-0").occurrences == 4
    long = next(item for item in report.models_routes if item.field == "served_model")
    assert long.value is None and len(long.sha256) == 64
    assert "PRIVATE TASK" not in report.model_dump_json()
    assert "long-private-value-" not in report.model_dump_json()
    assert any(finding.code == "metadata_limit" for finding in report.findings)


def test_field_and_scalar_markers_and_output_exclusion(tmp_path: Path) -> None:
    (tmp_path / "receipt.json").write_text(
        '{"answer_keys": ["heldout"], "split": "held_out"}', encoding="utf-8",
    )
    output = tmp_path / "output"
    output.mkdir()
    (output / "old.json").write_text('{"split":"test"}', encoding="utf-8")
    report = check_folder_validity(tmp_path, exclude=(output,))
    assert report.files_checked == 1
    assert report.marker_counts["answer_keys"] == 1
    assert report.marker_counts["heldout"] == 1
    assert report.split_counts["heldout"] == 1
