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
             "answer_keys": [], "label_source": "private-label-path", "served_model": "model-b",
             "usd": 0.5},
            {"error": "permission refused 403", "in_sample": True},
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
    assert any("/rows/1/answer_keys" in finding.pointers for finding in report.findings)
    assert report.marker_counts == {"answer_keys": 1, "test": 2}
    assert len(codes) == len(report.findings)
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
    for index in range(12):
        (folder / f"linked-{index}.json").symlink_to(external)
        (folder / f"huge-{index}.json").write_text('"' + "x" * 60 + '"', encoding="utf-8")
        (folder / f"bad-{index}.json").write_text("{", encoding="utf-8")
    monkeypatch.setattr(validity, "MAX_BYTES", 32)
    report = check_folder_validity(folder)
    assert report.files_skipped == 36
    assert {finding.code for finding in report.findings} == {
        "path_escape", "oversize", "unreadable", "scan_summary",
    }
    assert "EXTERNAL-ROW-SENTINEL" not in report.model_dump_json()
    assert len(report.findings) == 4
    for finding in report.findings:
        if finding.code != "scan_summary":
            assert finding.count == 12
            assert len(finding.paths) == 10


def test_metadata_values_are_bounded_but_counts_continue(tmp_path: Path) -> None:
    rows = [
        {"model": f"model-{index}", "route": f"route-{index}", "task_text": "PRIVATE TASK"}
        for index in range(51)
    ]
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
    limits = [finding for finding in report.findings if finding.code == "metadata_limit"]
    assert len(limits) == 1
    assert limits[0].count == 2
    assert limits[0].pointers == ["/model", "/route"]


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
    assert report.marker_counts["held_out"] == 1
    assert "heldout" not in report.marker_counts
    assert report.split_counts["heldout"] == 1
    findings = [finding for finding in report.findings if finding.code == "heldout_marker"]
    assert len(findings) == 1
    assert findings[0].count == 2


@pytest.mark.parametrize("document", [
    {"test_tampered": True, "reason": "modified tests/test_render.py"},
    {"reason": "modified data/test.jsonl"},
    {"evidence": "test answer_keys heldout"},
    {"diff_stat": "tests/test_render.py | 2"},
    {"text": "test answer_keys held_out"},
    {"text": "modified tests/test_render.py"},
    {"test": False, "my_answer_keys": [], "heldout_score": 0.5},
    {"split": "contest", "partition": "test suite", "fold": "test_tampered"},
    {"path": "data/test.py"},
    {"path": "data/heldout.sh"},
    {"path": "data/answer_keys.js"},
    {"path": "data/answers.ts"},
    {"path": "tests/test.jsonl"},
    {"path": "task-code/tests/heldout.json"},
    {"path": "data/tests/answers.csv"},
    {"path": "data/task-code/tests/test.tsv"},
    {"path": "data/test-results/receipt.json"},
    {"path": "data/contested.json"},
])
def test_non_data_and_free_text_do_not_mark_heldout(
    tmp_path: Path, document: dict[str, object],
) -> None:
    (tmp_path / "receipt.json").write_text(json.dumps(document), encoding="utf-8")
    report = check_folder_validity(tmp_path)
    assert report.marker_counts == {}
    assert not any(finding.code == "heldout_marker" for finding in report.findings)


@pytest.mark.parametrize(("document", "markers"), [
    ({"test_tampered": "data/test.jsonl"}, {"test": 1}),
    ({"reason": "data/test.jsonl"}, {"test": 1}),
    ({"diff_stat": "data/heldout.csv"}, {"heldout": 1}),
    ({"evidence": {"split": "test", "path": "data/answers.json"}}, {"test": 1, "answers": 1}),
])
def test_structured_metadata_and_paths_are_not_hidden_by_parent_keys(
    tmp_path: Path, document: dict[str, object], markers: dict[str, int],
) -> None:
    (tmp_path / "receipt.json").write_text(json.dumps(document), encoding="utf-8")
    report = check_folder_validity(tmp_path)
    assert report.marker_counts == markers
    findings = [finding for finding in report.findings if finding.code == "heldout_marker"]
    assert len(findings) == 1 and findings[0].count == sum(markers.values())


@pytest.mark.parametrize("key", [
    "split", "partition", "subset", "fold", "dataset_split", "eval_split",
])
@pytest.mark.parametrize("value", [
    "test", "heldout", "held-out", "held_out", "answer-key", "answer-keys",
    "answer_key", "answer_keys", "answers",
])
def test_split_fields_count_one_marker(tmp_path: Path, key: str, value: str) -> None:
    (tmp_path / "receipt.json").write_text(json.dumps({key: value}), encoding="utf-8")
    report = check_folder_validity(tmp_path)
    assert report.marker_counts == {value.replace("-", "_"): 1}
    finding = next(item for item in report.findings if item.code == "heldout_marker")
    assert finding.count == 1
    assert finding.pointers == [f"/{key}"]


@pytest.mark.parametrize(("key", "marker"), [
    ("answer_key", "answer_key"), ("answer_keys", "answer_keys"), ("answers", "answers"),
    ("heldout", "heldout"), ("test_labels", "test"),
])
def test_exact_label_keys_count_once(tmp_path: Path, key: str, marker: str) -> None:
    (tmp_path / "receipt.json").write_text(
        json.dumps({key: ["test", "heldout"]}), encoding="utf-8",
    )
    report = check_folder_validity(tmp_path)
    assert report.marker_counts == {marker: 1}


@pytest.mark.parametrize(("value", "marker"), [
    ("data/test.jsonl", "test"),
    ("data/TEST.json", "test"),
    ("data/held-out.csv", "held_out"),
    ("data/held_out.tsv", "held_out"),
    ("data/heldout.parquet", "heldout"),
    ("data/answer-key.json", "answer_key"),
    ("data/answer-keys.jsonl", "answer_keys"),
    ("data/answer_key.json", "answer_key"),
    ("data/answer_keys.csv", "answer_keys"),
    ("data/answers.parquet", "answers"),
    ("test/data.json", "test"),
    ("held-out/data.json", "held_out"),
    ("data/result.test.v2.json", "test"),
    ("data/result-held_out-v2.json", "held_out"),
    ("data/result_answer_keys_v2.json", "answer_keys"),
    ("task-code/heldout.json", "heldout"),
    ("task-code/data/answers.json", "answers"),
    ("task_code/data/answers.json", "answers"),
    ("data/task-code/test.tsv", "test"),
])
def test_data_path_values_mark_heldout(tmp_path: Path, value: str, marker: str) -> None:
    (tmp_path / "receipt.json").write_text(json.dumps({"path": value}), encoding="utf-8")
    report = check_folder_validity(tmp_path)
    assert report.marker_counts == {marker: 1}
    finding = next(item for item in report.findings if item.code == "heldout_marker")
    assert finding.count == 1 and finding.pointers == ["/path"]


@pytest.mark.parametrize(("extension", "contents"), [
    (".json", "{}"),
    (".jsonl", "{}\n"),
    (".csv", "label_source\nsource\n"),
    (".tsv", "label_source\nsource\n"),
])
def test_actual_data_receipt_path_marks_heldout(
    tmp_path: Path, extension: str, contents: str,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / f"test{extension}").write_text(contents, encoding="utf-8")
    report = check_folder_validity(tmp_path)
    assert report.marker_counts == {"test": 1}
    assert report.files_checked == 1 and report.files_skipped == 0
    finding = next(item for item in report.findings if item.code == "heldout_marker")
    assert finding.count == 1 and finding.paths == [f"data/test{extension}"]


@pytest.mark.parametrize("relative", [
    "tests/test.json", "task-code/tests/heldout.json", "task_code/tests/answers.json",
    "nested/tests/test.json", "nested/task-code/tests/answer_keys.json",
    "code/test.py", "code/test.sh", "code/test.js", "code/test.ts",
])
def test_code_receipts_never_mark_heldout(tmp_path: Path, relative: str) -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text('{"split":"test","path":"data/answers.json"}', encoding="utf-8")
    report = check_folder_validity(tmp_path)
    assert report.marker_counts == {}
    assert not any(finding.code == "heldout_marker" for finding in report.findings)


@pytest.mark.parametrize("relative", [
    "task-code/data/answers.json", "task_code/data/answers.json",
])
def test_task_code_data_receipts_still_mark_heldout(tmp_path: Path, relative: str) -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    report = check_folder_validity(tmp_path)
    assert report.marker_counts == {"answers": 1}
    assert report.files_checked == 1
    finding = next(item for item in report.findings if item.code == "heldout_marker")
    assert finding.count == 1 and finding.paths == [relative]


@pytest.mark.parametrize("document", [
    {"taskname": "sol-sink-permission-friction"},
    {"taskname": "sol-refusal-friction"},
    {"text": "permission refused"},
    {"reason": "permission or refusal may occur"},
])
def test_bare_access_words_do_not_count(
    tmp_path: Path, document: dict[str, object],
) -> None:
    (tmp_path / "receipt.json").write_text(json.dumps(document), encoding="utf-8")
    report = check_folder_validity(tmp_path)
    assert report.access_failures == {}
    assert not any(finding.code == "access_failure" for finding in report.findings)


@pytest.mark.parametrize(("key", "value", "kind"), [
    ("error", "PermissionError: [Errno 13]", "permission"),
    ("stderr", "permission", "permission"),
    ("exception_type", "permission", "permission"),
    ("failure_reason", "refused", "refusal"),
    ("refusal", "refusal", "refusal"),
    ("status", "permission", "permission"),
    ("status", 403, "403"),
    ("text", "permission denied", "permission"),
    ("text", "PERMISSIONERROR: denied", "permission"),
    ("text", "EACCES", "permission"),
    ("text", "403", "403"),
    ("text", "FORBIDDEN", "forbidden"),
    ("text", "not permitted", "permission"),
    ("text", "refused to read", "refusal"),
    ("text", "I can't help", "refusal"),
    ("text", "I cannot assist", "refusal"),
    ("text", "cannot comply", "refusal"),
])
def test_access_failures_use_context_or_strong_patterns(
    tmp_path: Path, key: str, value: str | int, kind: str,
) -> None:
    (tmp_path / "receipt.json").write_text(json.dumps({key: value}), encoding="utf-8")
    report = check_folder_validity(tmp_path)
    assert report.access_failures == {kind: 1}
    findings = [finding for finding in report.findings if finding.code == "access_failure"]
    assert len(findings) == 1
    assert findings[0].count == 1
    assert findings[0].pointers == [f"/{key}"]


def test_marker_and_access_findings_aggregate_bounded_examples(tmp_path: Path) -> None:
    rows = [
        {"split": "test", "error": "Permission denied; refused to read; 403 forbidden"}
        for _ in range(12)
    ]
    (tmp_path / "receipt.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8",
    )
    report = check_folder_validity(tmp_path)
    by_code = {finding.code: finding for finding in report.findings}
    assert len(by_code) == len(report.findings)
    assert report.marker_counts == {"test": 12}
    assert report.access_failures == {"403": 12, "forbidden": 12, "permission": 12, "refusal": 12}
    assert by_code["heldout_marker"].count == 12
    assert by_code["access_failure"].count == 48
    assert by_code["access_failure"].message == (
        "Access-failure markers appear in receipts: "
        "403=12, forbidden=12, permission=12, refusal=12."
    )
    for code in ("heldout_marker", "access_failure"):
        assert by_code[code].paths == ["receipt.jsonl"]
        assert len(by_code[code].pointers) == 10
    assert "Permission denied" not in report.model_dump_json()


def test_tsv_rows_are_inspected_with_tab_delimiters(tmp_path: Path) -> None:
    (tmp_path / "receipt.tsv").write_text(
        "label_source\tsplit\tn\tcost_usd\nsource\ttest\t3\t0.25\n", encoding="utf-8",
    )
    report = check_folder_validity(tmp_path)
    assert report.files_checked == 1 and report.files_skipped == 0
    assert report.rows_checked == 1 and report.rows_without_label_source == 0
    assert report.marker_counts == {"test": 1}
    assert report.receipts[0].sample_sizes == {"/line/2/n": 3}
    assert report.spend_usd == 0.25


def test_parquet_paths_are_inventoried_without_reading_contents(tmp_path: Path) -> None:
    for name in ("test-a.parquet", "test-b.parquet"):
        (tmp_path / name).write_bytes(b"\xff\x00Permission denied test heldout")
    report = check_folder_validity(tmp_path)
    assert report.files_checked == 0 and report.files_skipped == 2
    assert report.rows_checked == 0
    assert report.marker_counts == {"test": 2}
    assert report.access_failures == {}
    assert [receipt.path for receipt in report.receipts] == ["test-a.parquet", "test-b.parquet"]
    findings = {finding.code: finding for finding in report.findings}
    assert len(findings) == len(report.findings)
    assert findings["heldout_marker"].count == 2
    assert findings["content_not_inspected"].count == 2
    assert findings["content_not_inspected"].severity == "info"
    assert findings["content_not_inspected"].paths == ["test-a.parquet", "test-b.parquet"]
    assert "unreadable" not in findings


def test_oversize_parquet_obeys_receipt_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "data.parquet").write_bytes(b"\x00" * 33)
    monkeypatch.setattr(validity, "MAX_BYTES", 32)
    report = check_folder_validity(tmp_path)
    assert report.files_checked == 0 and report.files_skipped == 1
    assert report.receipts == []
    assert {finding.code for finding in report.findings} == {"oversize", "scan_summary"}
