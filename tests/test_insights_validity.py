from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.insights import validity
from evallab.insights.validity import check_folder_validity


def _jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_markers_loaders_and_private_rows(tmp_path: Path) -> None:
    private = "DO NOT COPY THIS PRIVATE CASE TEXT"
    (tmp_path / "TEST-receipt.json").write_text(json.dumps({
        "n": 3,
        "rows": [
            {"text": private, "split": "test", "label_loader": "first-loader",
             "is_cheat": True, "label_sha256": "hash-a", "model": "model-a", "cost_usd": 0.25},
            {"text": "answer_keys and held_out", "label_loader": "second-loader",
             "answer_keys": [], "label": "x", "label_source": "private-label-path",
             "served_model": "model-b", "usd": 0.5},
            {"error": "Permission denied; refused to read; 403", "in_sample": True,
             "label": False},
            {"note": "not a label row"},
        ],
    }), encoding="utf-8")
    report = check_folder_validity(tmp_path)
    codes = {finding.code for finding in report.findings}
    assert {"heldout_marker", "multiple_label_loaders", "missing_label_source"} <= codes
    assert report.rows_checked == 4
    assert report.label_rows == 3
    assert report.rows_without_label_source == 1
    missing = next(item for item in report.findings if item.code == "missing_label_source")
    assert missing.message.startswith("1 of 3 label rows")
    assert missing.paths == ["TEST-receipt.json"]
    assert len([item for item in report.label_sources if item.field == "label_loader"]) == 2
    assert len(report.models_routes) == 2
    assert report.spend_usd == 0.75
    assert report.receipts[0].sample_sizes["/n"] == 3
    assert report.access_failures == {"403": 1, "permission": 1, "refusal": 1}
    serialized = report.model_dump_json()
    assert private not in serialized
    assert "Permission denied" not in serialized
    assert "first-loader" in serialized and "private-label-path" in serialized
    assert {item.value for item in report.models_routes} == {"model-a", "model-b"}
    assert all(finding.finding_id.startswith("V-") for finding in report.findings)
    assert any("/rows/1/answer_keys" in finding.pointers for finding in report.findings)
    assert report.marker_counts == {"answer_keys": 1, "test": 2}
    assert len(codes) == len(report.findings)
    assert all(finding.message and finding.severity for finding in report.findings)


def test_jsonl_csv_and_inherited_label_source(tmp_path: Path) -> None:
    (tmp_path / "a.jsonl").write_text(
        '{"label_source":"source","is_cheat":true,"n":4,"spend":1.25}\n'
        '{"is_cheat":false,"n":2,"spend":0.75}\n', encoding="utf-8",
    )
    (tmp_path / "b.csv").write_text(
        "label_source,is_cheat,total,cost_usd\nsource,true,6,0.5\n", encoding="utf-8",
    )
    (tmp_path / "c.json").write_text(
        '{"label_source":"shared","rows":[{"label":"x"},{"caught":false}]}',
        encoding="utf-8",
    )
    report = check_folder_validity(tmp_path)
    assert report.rows_checked == 5
    assert report.label_rows == 4
    assert report.rows_without_label_source == 1
    assert report.spend_usd == 2.5
    assert report.files_checked == 3


def test_rows_without_label_field_are_not_label_rows(tmp_path: Path) -> None:
    _jsonl(tmp_path / "decisions.jsonl", [
        {"stage": "l0", "details": {"alters_test_or_grader": False}, "steps": [{"n": 1}]},
        {"stage": "l1", "reason": "x"},
    ])
    report = check_folder_validity(tmp_path)
    assert report.rows_checked >= 3
    assert report.label_rows == 0 and report.rows_without_label_source == 0
    assert not any(finding.code == "missing_label_source" for finding in report.findings)


@pytest.mark.parametrize("field", [
    "is_cheat", "label", "labels", "ground_truth", "positive", "enacted", "verdict_label",
])
@pytest.mark.parametrize(("provenance", "sourced"), [
    ({}, False),
    ({"label_source": "s"}, True),
    ({"label_loader": "l"}, True),
    ({"labels_path": "p"}, True),
    ({"label_sha256": "h"}, True),
    ({"label_source": ""}, False),
])
def test_label_row_fields_and_provenance_fields(
    tmp_path: Path, field: str, provenance: dict[str, str], sourced: bool,
) -> None:
    _jsonl(tmp_path / "rows.jsonl", [{field: True, **provenance}, {"other": 1}])
    report = check_folder_validity(tmp_path)
    assert report.label_rows == 1
    assert report.rows_without_label_source == (0 if sourced else 1)


# --- held-out markers -------------------------------------------------------------------


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
    {"details": {"alters_test_or_grader": True}},
    {"details": {"alters_test_or_grader": "data/test.jsonl"}},
    {"test_tampered": "data/test.jsonl"},
    {"stage_decisions": {"l0": {"details": {"alters_test_or_grader": "test"}}}},
    {"reason": "modified data/test.jsonl"},
    {"path": "data/test.jsonl"},
    {"path": "data/answers.json"},
    {"evidence": "test answer_keys heldout"},
    {"diff_stat": "tests/test_render.py | 2"},
    {"text": "test answer_keys held_out"},
    {"text": "modified tests/test_render.py"},
    {"test": False, "my_answer_keys": [], "heldout_score": 0.5},
    {"is_heldout": True, "answers_count": 3, "test_labels_path": "x", "heldout_n": 4},
    {"split": "contest", "partition": "test suite", "fold": "test_tampered"},
    {"split": "answers", "subset": "answer_keys"},
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


def test_whole_key_markers_inside_structured_metadata_still_flag(tmp_path: Path) -> None:
    (tmp_path / "receipt.json").write_text(json.dumps({
        "test_tampered": "data/test.jsonl",
        "evidence": {"split": "test", "path": "data/answers.json"},
    }), encoding="utf-8")
    report = check_folder_validity(tmp_path)
    assert report.marker_counts == {"test": 1}
    findings = [finding for finding in report.findings if finding.code == "heldout_marker"]
    assert len(findings) == 1 and findings[0].pointers == ["/evidence/split"]


@pytest.mark.parametrize("key", [
    "split", "partition", "subset", "fold", "dataset_split", "eval_split",
])
@pytest.mark.parametrize("value", [
    "test", "TEST", "heldout", "held-out", "held_out", "Held_Out",
])
def test_split_fields_count_one_marker(tmp_path: Path, key: str, value: str) -> None:
    (tmp_path / "receipt.json").write_text(json.dumps({key: value}), encoding="utf-8")
    report = check_folder_validity(tmp_path)
    assert report.marker_counts == {value.lower().replace("-", "_"): 1}
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


# --- access failures --------------------------------------------------------------------


@pytest.mark.parametrize("document", [
    {"taskname": "sol-sink-permission-friction"},
    {"taskname": "sol-refusal-friction"},
    {"text": "permission refused"},
    {"reason": "permission or refusal may occur"},
    {"error": "permission"},
    {"stderr": "permission refused"},
    {"failure": "refusal"},
    {"status": "ok", "finish_reason": "stop"},
    {"exception": "refused"},
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
    ("stderr", "Permission denied", "permission"),
    ("exception_type", "EACCES", "permission"),
    ("failure_reason", "refused to read", "refusal"),
    ("refusal", "I can't help with that", "refusal"),
    ("status", "not permitted", "permission"),
    ("status", 403, "403"),
    ("error", "FORBIDDEN", "forbidden"),
    ("error", "cannot comply", "refusal"),
    ("error", "I cannot assist", "refusal"),
    ("finish_reason", "refusal", "refusal"),
    ("finish_reason", "content_filter", "refusal"),
])
def test_access_failures_count_strong_patterns_in_metadata_fields(
    tmp_path: Path, key: str, value: str | int, kind: str,
) -> None:
    (tmp_path / "receipt.json").write_text(json.dumps({key: value}), encoding="utf-8")
    report = check_folder_validity(tmp_path)
    assert report.access_failures == {kind: 1}
    findings = [finding for finding in report.findings if finding.code == "access_failure"]
    assert len(findings) == 1
    assert findings[0].count == 1
    assert findings[0].pointers == [f"/{key}"]


@pytest.mark.parametrize("document", [
    {"text": "permission denied"},
    {"message": "403 forbidden"},
    {"note": "PermissionError", "reason": "refused to read"},
    {"steps": [{"error": "Permission denied", "stderr": "403 forbidden"}]},
    {"steps": [{"message": "Permission denied"}]},
    {"transcript": {"stderr": "Permission denied"}},
    {"messages": [{"content": "I can't help with that", "status": 403}]},
    {"error": {"output": "Permission denied"}},
    {"error": {"stdout": "403", "diff": "forbidden", "reasoning": "refused to"}},
    {"content": {"status": 403}},
    {"output": "EACCES"},
    {"rows": [{"stage": "x", "steps": [{"observation": {"error": "forbidden"}}]}]},
])
def test_access_failures_ignore_content_and_non_metadata_fields(
    tmp_path: Path, document: dict[str, object],
) -> None:
    (tmp_path / "receipt.json").write_text(json.dumps(document), encoding="utf-8")
    report = check_folder_validity(tmp_path)
    assert report.access_failures == {}
    assert not any(finding.code == "access_failure" for finding in report.findings)


def test_nested_metadata_counts_with_pointer(tmp_path: Path) -> None:
    (tmp_path / "receipt.json").write_text(json.dumps({
        "rows": [{"verifier": {"error": {"detail": "Permission denied"}}, "steps": [
            {"stderr": "Permission denied"},
        ]}],
    }), encoding="utf-8")
    report = check_folder_validity(tmp_path)
    assert report.access_failures == {"permission": 1}
    finding = next(item for item in report.findings if item.code == "access_failure")
    assert finding.pointers == ["/rows/0/verifier/error/detail"]


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
    assert report.label_rows == 0
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


# --- label provenance -------------------------------------------------------------------

_PREPARE = '''
from pathlib import Path
from lab import load_labels

DATA = Path("out")


def prepare_alpha():
    labels = load_labels(database="db", run_ids=["a"])
    path = DATA / "alpha_labels.jsonl"
    path.write_text("".join(str(labels)))


def _fetch(ids):
    return load_labels(database="db", run_ids=ids)


def prepare_beta():
    rows = _fetch(["b"])
    l_path = DATA / f"beta_labels.jsonl"
    l_path.write_text(str(rows))


def prepare_gamma():
    rows = [{"id": 1, "is_cheat": bool(info.get("label") == "hack")}]
    with open(DATA / "gamma_labels.csv", "w") as handle:
        handle.write(str(rows))


def prepare_delta():
    rows = [{"id": 1, "is_cheat": True}]
    (DATA / "delta_labels.jsonl").write_text(str(rows))


def run_reader(corpus):
    l_path = DATA / f"{corpus}_labels.jsonl"
    labels = load_labels(l_path)
    report = DATA / "report.json"
    report.write_text(str(labels))


def main():
    for fn in (prepare_alpha, prepare_beta, prepare_gamma, prepare_delta):
        fn()
    prepare_alpha()
    prepare_beta()
    prepare_gamma()
    prepare_delta()
    pooled = DATA / "pooled_labels.jsonl"
    pooled.write_text("")
'''


def _provenance_folder(tmp_path: Path, *, with_files: bool = True) -> Path:
    folder = tmp_path / "study"
    folder.mkdir()
    (folder / "prepare.py").write_text(_PREPARE, encoding="utf-8")
    if with_files:
        secret = "SECRET-LABEL-VALUE"
        data = folder / "out"
        _jsonl(data / "alpha_labels.jsonl", [{"id": str(i), "is_cheat": secret} for i in range(3)])
        _jsonl(data / "beta_labels.jsonl", [{"id": str(i), "is_cheat": secret} for i in range(5)])
        (data / "gamma_labels.csv").write_text(
            "id,is_cheat\n" + "".join(f"{i},{secret}\n" for i in range(4)), encoding="utf-8",
        )
        _jsonl(data / "delta_labels.jsonl", [{"id": "1", "is_cheat": secret}] * 2)
        _jsonl(data / "pooled_labels.jsonl", [{"id": str(i), "is_cheat": secret} for i in range(14)])
    return folder


def _entries(folder: Path) -> dict[str, validity.LabelProvenance]:
    report = check_folder_validity(folder)
    return {Path(entry.file).name: entry for entry in report.label_provenance}


def test_label_provenance_reports_rows_producers_and_loader_paths(tmp_path: Path) -> None:
    folder = _provenance_folder(tmp_path)
    report = check_folder_validity(folder)
    entries = {Path(entry.file).name: entry for entry in report.label_provenance}
    assert {name: entry.rows for name, entry in entries.items()} == {
        "alpha_labels.jsonl": 3, "beta_labels.jsonl": 5, "gamma_labels.csv": 4,
        "delta_labels.jsonl": 2, "pooled_labels.jsonl": 14,
    }
    assert entries["alpha_labels.jsonl"].producer == "prepare.py:prepare_alpha"
    assert entries["alpha_labels.jsonl"].loader_use == "direct"
    assert entries["beta_labels.jsonl"].loader_use == "helper"
    assert entries["beta_labels.jsonl"].via_shared_loader is True
    assert entries["gamma_labels.csv"].loader_use == "none"
    assert entries["gamma_labels.csv"].via_shared_loader is False
    assert entries["delta_labels.jsonl"].via_shared_loader is False
    assert entries["pooled_labels.jsonl"].loader_use == "aggregate"
    assert entries["pooled_labels.jsonl"].via_shared_loader is None
    assert all(entry.rows_source == "file" and entry.present for entry in entries.values())
    finding = next(item for item in report.findings if item.code == "label_provenance")
    assert finding.severity == "warning"
    assert finding.count == 14
    assert "8 rows in 2 file(s) via the shared load_labels loader" in finding.message
    assert "6 rows in 2 file(s) via per-corpus rules" in finding.message
    assert "prepare_gamma" in finding.message and "prepare_delta" in finding.message
    assert "pooled" in finding.message
    assert "out/alpha_labels.jsonl" in finding.paths
    assert "SECRET-LABEL-VALUE" not in report.model_dump_json()


def test_reader_functions_are_not_producers(tmp_path: Path) -> None:
    folder = _provenance_folder(tmp_path)
    report = check_folder_validity(folder)
    assert not any("run_reader" in (entry.producer or "") for entry in report.label_provenance)


def test_label_provenance_single_path_is_info(tmp_path: Path) -> None:
    folder = tmp_path / "study"
    (folder / "out").mkdir(parents=True)
    (folder / "prep.py").write_text(
        "from lab import load_labels\n"
        "def prepare_one(path):\n"
        "    labels = load_labels(path)\n"
        "    (path / 'one_labels.jsonl').write_text(str(labels))\n"
        "def prepare_two(path):\n"
        "    labels = load_labels(path)\n"
        "    (path / 'two_labels.jsonl').write_text(str(labels))\n", encoding="utf-8",
    )
    _jsonl(folder / "out/one_labels.jsonl", [{"id": "a"}, {"id": "b"}])
    _jsonl(folder / "out/two_labels.jsonl", [{"id": "c"}])
    report = check_folder_validity(folder)
    finding = next(item for item in report.findings if item.code == "label_provenance")
    assert finding.severity == "info"
    assert "3 rows in 2 file(s) via the shared load_labels loader" in finding.message
    assert "per-corpus" not in finding.message


def test_label_provenance_two_per_corpus_rules_without_loader_warns(tmp_path: Path) -> None:
    folder = tmp_path / "study"
    folder.mkdir()
    (folder / "prep.py").write_text(
        "def prepare_a(p):\n    (p / 'a_labels.jsonl').write_text('x')\n"
        "def prepare_b(p):\n    (p / 'b_labels.jsonl').write_text('x')\n", encoding="utf-8",
    )
    _jsonl(folder / "a_labels.jsonl", [{"id": "1"}])
    _jsonl(folder / "b_labels.jsonl", [{"id": "1"}, {"id": "2"}])
    report = check_folder_validity(folder)
    finding = next(item for item in report.findings if item.code == "label_provenance")
    assert finding.severity == "warning"
    assert "3 rows in 2 file(s) via per-corpus rules" in finding.message


def test_label_provenance_code_only_has_no_counts(tmp_path: Path) -> None:
    folder = _provenance_folder(tmp_path, with_files=False)
    report = check_folder_validity(folder)
    assert all(not entry.present and entry.rows is None for entry in report.label_provenance)
    by_name = {entry.file: entry for entry in report.label_provenance}
    assert by_name["alpha_labels.jsonl"].via_shared_loader is True
    assert by_name["beta_labels.jsonl"].via_shared_loader is True
    assert by_name["gamma_labels.csv"].via_shared_loader is False
    assert by_name["pooled_labels.jsonl"].loader_use == "aggregate"
    assert "{}_labels.jsonl" not in by_name
    finding = next(item for item in report.findings if item.code == "label_provenance")
    assert finding.severity == "warning"
    assert finding.count == 0
    assert "row count unknown" in finding.message
    assert finding.paths == ["prepare.py"]


def test_label_provenance_metrics_summary_fallback(tmp_path: Path) -> None:
    folder = _provenance_folder(tmp_path, with_files=False)
    (folder / "metrics_summary.json").write_text(json.dumps({
        "alpha": {"total_rollouts": 150, "l0_only": {"n": 1}},
        "beta": {"total_rollouts": 40},
        "gamma": {"total_rollouts": 30},
        "pooled": {"total_rollouts": 999},
        "delta": {"wall_time": 3},
    }), encoding="utf-8")
    report = check_folder_validity(folder)
    by_name = {entry.file: entry for entry in report.label_provenance}
    assert by_name["alpha_labels.jsonl"].rows == 150
    assert by_name["alpha_labels.jsonl"].rows_source == "metrics_summary"
    assert by_name["gamma_labels.csv"].rows == 30
    assert by_name["delta_labels.jsonl"].rows is None
    assert by_name["pooled_labels.jsonl"].rows is None
    finding = next(item for item in report.findings if item.code == "label_provenance")
    assert finding.count == 220
    assert "190 rows in 2 file(s) via the shared load_labels loader" in finding.message
    assert "at least 30 rows, count unknown for 1" in finding.message
    assert "3 count(s) come from metrics_summary total_rollouts" in finding.message


def test_label_provenance_ambiguous_metrics_are_not_used(tmp_path: Path) -> None:
    folder = _provenance_folder(tmp_path, with_files=False)
    (folder / "metrics_summary.json").write_text(
        json.dumps({"alpha": {"total_rollouts": 10}}), encoding="utf-8",
    )
    (folder / "old").mkdir()
    (folder / "old/metrics_summary_v1.json").write_text(
        json.dumps({"alpha": {"total_rollouts": 11}}), encoding="utf-8",
    )
    entries = _entries(folder)
    assert entries["alpha_labels.jsonl"].rows is None


def test_label_files_without_code_are_unattributed(tmp_path: Path) -> None:
    folder = tmp_path / "study"
    _jsonl(folder / "x_labels.jsonl", [{"id": "1"}, {"id": "2"}])
    (folder / "labels.json").write_text(json.dumps({"a": True, "b": False, "c": True}))
    (folder / "y_labels.json").write_text(json.dumps({"labels": [True, False]}))
    (folder / "z-labels.csv").write_text("id,v\n1,a\n2,b\n3,c\n", encoding="utf-8")
    report = check_folder_validity(folder)
    entries = {entry.file: entry for entry in report.label_provenance}
    assert {name: entry.rows for name, entry in entries.items()} == {
        "x_labels.jsonl": 2, "labels.json": 3, "y_labels.json": 2, "z-labels.csv": 3,
    }
    assert all(entry.producer is None and entry.loader_use == "unknown"
               for entry in entries.values())
    finding = next(item for item in report.findings if item.code == "label_provenance")
    assert finding.severity == "info"
    assert "10 rows in 4 file(s) with no identifiable producing code" in finding.message


def test_label_provenance_ignores_envs_unparseable_code_and_other_json(tmp_path: Path) -> None:
    folder = tmp_path / "study"
    (folder / ".venv").mkdir(parents=True)
    (folder / ".venv/lib.py").write_text(
        "def f(p):\n    (p / 'v_labels.jsonl').write_text('x')\n", encoding="utf-8",
    )
    (folder / "broken.py").write_text("def (:\n", encoding="utf-8")
    (folder / "metrics.json").write_text("{}", encoding="utf-8")
    report = check_folder_validity(folder)
    assert report.label_provenance == []
    assert not any(finding.code == "label_provenance" for finding in report.findings)


def test_label_provenance_prefers_code_from_the_same_bundle_member(tmp_path: Path) -> None:
    folder = tmp_path / ".bundle"
    for member, loader in (("one", "load_labels(p)"), ("two", "p")):
        (folder / member).mkdir(parents=True)
        (folder / member / "prep.py").write_text(
            f"def prepare_x(p):\n    labels = {loader}\n"
            "    (p / 'x_labels.jsonl').write_text(str(labels))\n", encoding="utf-8",
        )
        _jsonl(folder / member / "x_labels.jsonl", [{"id": "1"}])
    report = check_folder_validity(folder)
    entries = {entry.file: entry for entry in report.label_provenance}
    assert entries["one/x_labels.jsonl"].via_shared_loader is True
    assert entries["one/x_labels.jsonl"].producer == "one/prep.py:prepare_x"
    assert entries["two/x_labels.jsonl"].via_shared_loader is False
    assert entries["two/x_labels.jsonl"].producer == "two/prep.py:prepare_x"


def test_label_provenance_loader_through_class_method_and_module_scope(tmp_path: Path) -> None:
    folder = tmp_path / "study"
    folder.mkdir()
    (folder / "prep.py").write_text(
        "import json\n"
        "class Builder:\n"
        "    def _labels(self):\n"
        "        return load_labels(None)\n"
        "    def build(self, out):\n"
        "        rows = self._labels()\n"
        "        with out.open('w') as handle:\n"
        "            json.dump(rows, handle)\n"
        "        (out / 'cls_labels.json').write_text(str(rows))\n"
        "(OUT := 'mod_labels.jsonl')\n"
        "open('mod_labels.jsonl', 'w').write('x')\n", encoding="utf-8",
    )
    entries = _entries(folder)
    assert entries["cls_labels.json"].loader_use == "helper"
    assert entries["cls_labels.json"].producer == "prep.py:Builder.build"
    assert entries["mod_labels.jsonl"].producer == "prep.py:<module>"
    assert entries["mod_labels.jsonl"].via_shared_loader is False


# --- scripted vs real positives -----------------------------------------------------------


def test_scripted_positives_all_scripted_warns(tmp_path: Path) -> None:
    rows: list[dict[str, object]] = [
        {"id": f"c{i}", "kind": "scripted", "positive": True, "task_text": "PRIVATE"}
        for i in range(8)
    ]
    rows += [{"id": f"r{i}", "kind": "real", "positive": False} for i in range(12)]
    rows += [{"id": "x", "positive": True}]  # no kind/variant/scripted id: unclassified
    _jsonl(tmp_path / "rollouts.jsonl", rows)
    report = check_folder_validity(tmp_path)
    assert (report.scripted_positives, report.real_positives) == (8, 0)
    receipt = next(item for item in report.receipts if item.path == "rollouts.jsonl")
    assert (receipt.scripted_positives, receipt.real_positives) == (8, 0)
    finding = next(item for item in report.findings if item.code == "scripted_positives")
    assert finding.severity == "warning" and finding.count == 8
    assert "8 of 8 positives are scripted controls" in finding.message
    assert "rollouts.jsonl: 8 of 8" in finding.message
    assert finding.paths == ["rollouts.jsonl"]
    assert "PRIVATE" not in report.model_dump_json()


def test_scripted_positives_mixed_is_info_and_per_file(tmp_path: Path) -> None:
    _jsonl(tmp_path / "a.jsonl", [
        {"id": "s1", "variant": "cheat", "is_cheat": True},
        {"id": "s2", "variant": "honest", "is_cheat": False},
        {"id": "r1", "variant": "plain", "is_cheat": True},
        {"id": "r2", "kind": "real", "behavior": "confirmed_cheat"},
        {"id": "scripted-9", "label": "hack"},
        {"id": "r3", "kind": "real", "behavior": "clean"},
    ])
    _jsonl(tmp_path / "b.jsonl", [
        {"id": "r4", "kind": "real", "enacted": True},
        {"kind": "scripted", "is_cheat": 0},
    ])
    report = check_folder_validity(tmp_path)
    by_path = {item.path: item for item in report.receipts}
    assert (by_path["a.jsonl"].scripted_positives, by_path["a.jsonl"].real_positives) == (2, 2)
    assert (by_path["b.jsonl"].scripted_positives, by_path["b.jsonl"].real_positives) == (0, 1)
    assert (report.scripted_positives, report.real_positives) == (2, 3)
    finding = next(item for item in report.findings if item.code == "scripted_positives")
    assert finding.severity == "info"
    assert "2 of 5 positives are scripted controls" in finding.message
    assert "a.jsonl: 2 of 4" in finding.message
    assert finding.paths == ["a.jsonl"]


def test_scripted_positives_need_label_and_kind_fields(tmp_path: Path) -> None:
    _jsonl(tmp_path / "rows.jsonl", [
        {"kind": "scripted", "note": "no behaviour field"},
        {"variant": "cheat", "score": 1},
        {"id": "scripted-1", "score": 1},
        {"is_cheat": True},
        {"kind": "real", "is_cheat": True},
    ])
    report = check_folder_validity(tmp_path)
    assert (report.scripted_positives, report.real_positives) == (0, 1)
    assert not any(finding.code == "scripted_positives" for finding in report.findings)


def test_scripted_positives_in_nested_json_rows_and_csv(tmp_path: Path) -> None:
    (tmp_path / "doc.json").write_text(json.dumps({
        "rollouts": [{"kind": "scripted", "positive": True}, {"kind": "real", "positive": True}],
    }), encoding="utf-8")
    (tmp_path / "rows.csv").write_text(
        "id,kind,is_cheat\na,scripted,true\nb,real,false\n", encoding="utf-8",
    )
    report = check_folder_validity(tmp_path)
    by_path = {item.path: item for item in report.receipts}
    assert (by_path["doc.json"].scripted_positives, by_path["doc.json"].real_positives) == (1, 1)
    assert (by_path["rows.csv"].scripted_positives, by_path["rows.csv"].real_positives) == (1, 0)
    finding = next(item for item in report.findings if item.code == "scripted_positives")
    assert finding.severity == "warning"
    assert "2 of 3 positives" in finding.message
