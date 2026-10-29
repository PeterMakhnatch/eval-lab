"""The trace-findings exporter keeps what the cloud loader keeps and never stages a label or a bad line."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from evallab.production import findings_export as export
from evallab.production import harness_view
from evallab.production.deploy_preflight import count_forbidden, run_preflight

NEEDLE = "planted-needle-0042"


def _finding(run: str, detector: str, at: str, **changes: Any) -> dict[str, Any]:
    return {
        "run_id": run, "detector": detector, "detector_version": "v1", "reader_model": None,
        "verdict": "alarm", "kind": "cheat", "score": 0.9, "evidence_steps": [3, 1, 3],
        "summary": "  edits the grader  ", "input_tokens": 0, "output_tokens": 0, "cost_usd": 0,
        "latency_ms": 12, "produced_at": at,
    } | changes


def _jsonl(path: Path, rows: list[Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join((row if isinstance(row, str) else json.dumps(row)) + "\n" for row in rows), encoding="utf-8")
    return path


GOOD_A = [
    _finding("r1", "trace_reader", "2026-10-28T10:00:00+00:00", verdict="clear", kind="other"),
    _finding("r1", "trace_reader", "2026-10-28T11:00:00+00:00"),  # newer: wins
    _finding("r2", "run_health.loop", "2026-10-28T10:00:00Z", kind="loop", reader_model=None),
    _finding("r2", "trace_reader", "2026-10-28T10:00:00+00:00", reader_model="anthropic/claude-x", verdict="abstain"),
]
GOOD_B = [
    _finding("r2", "run_health.loop", "2026-10-28T10:00:00Z", kind="loop", verdict="clear"),  # tie: later file wins
    _finding("r1", "trace_reader", "2026-10-28T09:00:00+00:00", verdict="clear"),  # older than file A: dropped
    _finding("r3", "run_health.runaway", "2026-10-28T08:00:00-04:00", kind="runaway", verdict="unknown", outcome="fail"),
]
BAD = [
    "",
    "not json " + NEEDLE,
    '{"run_id": "r9", "run_id": "r8"}',
    json.dumps(_finding("r4", "trace_reader", "2026-10-28T10:00:00+00:00", label=NEEDLE)),
    json.dumps(_finding("r5", "trace_reader", "2026-10-28T10:00:00+00:00", extra={"reward": 1.0})),
    json.dumps(_finding("r6", "trace_reader", "2026-10-28T10:00:00+00:00", hackbench_label=NEEDLE)),
    json.dumps(_finding("r7", "trace_reader", "2026-10-28T10:00:00+00:00", verdict="exploit")),
    json.dumps(_finding("r7", "trace_reader", "2026-10-28 10:00:00")),
    json.dumps(_finding("r7", "Trace Reader", "2026-10-28T10:00:00+00:00")),
]


@pytest.fixture
def sources(tmp_path: Path) -> list[Path]:
    return [_jsonl(tmp_path / "lane/findings-v1.jsonl", GOOD_A + BAD), _jsonl(tmp_path / "rh/findings-run-health-3.jsonl", GOOD_B)]


def _served(path: Path) -> dict[str, Any]:
    loaded = harness_view.load_findings(path)
    return {run: {key: {k: v for k, v in record.items() if k != "_moment"} for key, record in by_key.items()}
            for run, by_key in loaded["by_run"].items()}


def test_the_export_keeps_exactly_what_the_cloud_loader_keeps_and_refuses_bad_lines(
    tmp_path: Path, sources: list[Path]
) -> None:
    receipt = export.export_findings(sources, tmp_path / "out")
    target = tmp_path / "out" / export.FILE
    body = target.read_bytes()

    concatenated = _jsonl(tmp_path / "clean.jsonl", GOOD_A + GOOD_B)
    assert _served(target) == _served(concatenated)
    assert harness_view.load_findings(target)["refused"] == {}
    served = _served(target)
    assert served["r1"][("trace_reader", "v1", "")]["produced_at"] == "2026-10-28T11:00:00+00:00"
    assert served["r2"][("run_health.loop", "v1", "")]["verdict"] == "clear"
    assert set(served) == {"r1", "r2", "r3"}

    assert receipt["lines_read"] == len(GOOD_A) + len(GOOD_B) + len(BAD) - 1  # the blank line is not read
    assert receipt["kept"] == 4 and receipt["runs"] == 3 and receipt["superseded"] == 3
    assert receipt["refused"] == {
        export.LABELLED: 3,
        export.NOT_JSON: 2,
        "verdict or kind outside the contract": 1,
        "produced_at names no time zone": 1,
        "detector name is not an identifier": 1,
    }
    assert receipt["sha256"] == hashlib.sha256(body).hexdigest() and receipt["bytes"] == len(body)
    assert [source["lines"] for source in receipt["sources"]] == [len(GOOD_A) + len(BAD) - 1, len(GOOD_B)]
    loop = next(row for row in receipt["detectors"] if row["detector"] == "run_health.loop")
    assert loop == {"detector": "run_health.loop", "detector_version": "v1", "reader_model": None,
                    "records": 1, "alarm": 0, "clear": 1, "abstain": 0, "unknown": 0}

    assert NEEDLE not in body.decode() and NEEDLE not in json.dumps(receipt)
    for line in body.decode().splitlines():
        record = json.loads(line)
        assert not count_forbidden(record, "findings")
        assert set(record) == {
            "run_id", "detector", "detector_version", "reader_model", "verdict", "kind", "score", "evidence_steps",
            "summary", "input_tokens", "output_tokens", "cost_usd", "latency_ms", "produced_at",
        }  # contract fields only: the stray ``outcome`` is not carried


def test_an_existing_file_is_kept_unless_replace_and_links_are_refused(tmp_path: Path, sources: list[Path]) -> None:
    out = tmp_path / "out"
    first = export.export_findings(sources, out)
    assert export.export_findings(sources, out)["sha256"] == first["sha256"]  # identical rerun is fine
    (out / export.FILE).write_text("{}\n", encoding="utf-8")
    with pytest.raises(FileExistsError, match="already differs"):
        export.export_findings(sources, out)
    assert export.export_findings(sources, out, replace=True)["sha256"] == first["sha256"]

    link = tmp_path / "linked.jsonl"
    link.symlink_to(sources[0])
    with pytest.raises(ValueError, match="regular file"):
        export.export_findings([link], tmp_path / "elsewhere")
    assert not (tmp_path / "elsewhere").exists()

    linked_dir = tmp_path / "linked-out"
    linked_dir.symlink_to(out, target_is_directory=True)
    with pytest.raises(ValueError, match="not a link"):
        export.export_findings(sources, linked_dir, replace=True)

    (out / export.FILE).unlink()
    (out / export.FILE).symlink_to(sources[0])
    with pytest.raises(ValueError, match="regular file"):
        export.export_findings(sources, out, replace=True)
    assert [path.name for path in out.iterdir()] == [export.FILE]  # no temporary file left behind


def test_nothing_is_written_when_no_record_validates(tmp_path: Path) -> None:
    only_bad = _jsonl(tmp_path / "bad.jsonl", BAD)
    with pytest.raises(ValueError, match="no finding validated"):
        export.export_findings([only_bad], tmp_path / "out")
    assert not (tmp_path / "out").exists()
    with pytest.raises(ValueError, match="does not exist"):
        export.export_findings([tmp_path / "missing.jsonl"], tmp_path / "out")


def test_the_cli_exports_and_deploy_preflight_accepts_the_file(
    tmp_path: Path, sources: list[Path], capsys: pytest.CaptureFixture[str]
) -> None:
    from evallab import cli

    state = tmp_path / "state"
    arguments = ["prod", "export-trace-findings", "--output-directory", str(state / "trace-harness")]
    for source in sources:
        arguments += ["--findings", str(source)]
    assert cli.run_cli(arguments, workspace=tmp_path) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["command"] == "export-trace-findings" and receipt["kept"] == 4

    report = run_preflight(state, repo_root=tmp_path, inventory=None, registry=None, stage_directory=tmp_path / "stage")
    row = next(entry for entry in report["files"] if entry["key"] == "harness_findings")
    assert row["status"] == "ok" and row["sha256"] == receipt["sha256"]
    assert (tmp_path / "stage" / harness_view.FINDINGS_FILE).read_bytes() == (state / harness_view.FINDINGS_FILE).read_bytes()

    link = tmp_path / "linked.jsonl"
    link.symlink_to(sources[0])
    linked = ["prod", "export-trace-findings", "--findings", str(link), "--output-directory", str(tmp_path / "x")]
    assert cli.run_cli(linked, workspace=tmp_path) != 0
    assert "regular file" in capsys.readouterr().err
