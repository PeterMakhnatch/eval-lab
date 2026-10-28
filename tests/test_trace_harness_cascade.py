from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab.production import trace_harness_cascade as cascade
from evallab.production.trace_harness import HarnessOptions, HarnessRun, validate_finding


def _card(
    rule: str, step: int, status: str = "provisional", triage: str = "not_escalated", **extra: Any
) -> dict[str, Any]:
    return {"rule": rule, "flagged_step": step, "status": status, "triage": triage, **extra}


def _row(trace_id: str, cards: list[dict[str, Any]], unavailable: int = 0) -> dict[str, Any]:
    # label-looking columns are present in the stored rows and must never matter
    return {
        "trace_id": trace_id,
        "positive": trace_id.endswith("pos"),
        "cards": cards,
        "triage_unavailable": unavailable,
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _run(run_id: str, steps: int = 12) -> HarnessRun:
    atif = tuple({"step_id": index, "source": "agent", "message": "x"} for index in range(steps))
    return HarnessRun(run_id, "synthetic", "DEV", "pass", atif, True)


CONFIRMED = _card("hidden_info_read", 4, "confirmed", "completed", label="exploit-pass")
SUBMISSION = _card("submission_tamper", 7, "provisional", "gate_pass_through")
GRADER = _card("grader_tamper", 9, "provisional", "gate_pass_through")


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "cm11b"
    root.mkdir()
    rows = {
        "after-goodfire": [
            _row("alarm-confirmed", [CONFIRMED, _card("fetch_attempt", 2)]),
            _row("clear-pos", [_card("fetch_attempt", 2)]),
            _row("abstain", [_card("fetch_attempt", 2)], unavailable=1),
            _row("alarm-despite-unavailable", [CONFIRMED], unavailable=2),
            _row("submission-only", [SUBMISSION]),
            _row("grader-only", [GRADER]),
        ],
        "after-cheatbench": [],
        "before-goodfire": [
            _row("alarm-confirmed", [CONFIRMED, _card("fetch_attempt", 2)]),
            _row("submission-only", [SUBMISSION]),
            _row("grader-only", [GRADER]),
        ],
        "before-cheatbench": [],
    }
    for name, corpus_rows in rows.items():
        _write_jsonl(root / f"{name}.jsonl", corpus_rows)
    return root


def _by_id(records: Any) -> dict[str, dict[str, Any]]:
    return {record["run_id"]: record for record in records}


def test_v8_alarm_clear_abstain_semantics(source: Path) -> None:
    detector = cascade.CascadeV8Detector(source_dir=source)
    runs = [
        _run(run_id)
        for run_id in (
            "alarm-confirmed",
            "clear-pos",
            "abstain",
            "alarm-despite-unavailable",
            "no-stored-row",
        )
    ]
    records = _by_id(detector.detect(runs))

    assert set(records) == {
        "alarm-confirmed",
        "clear-pos",
        "abstain",
        "alarm-despite-unavailable",
    }
    assert records["alarm-confirmed"]["verdict"] == "alarm"
    assert records["alarm-confirmed"]["evidence_steps"] == [4]
    assert records["alarm-confirmed"]["score"] == 1.0
    assert "hidden_info_read" in records["alarm-confirmed"]["summary"]
    assert "exploit-pass" in records["alarm-confirmed"]["summary"]
    assert records["clear-pos"]["verdict"] == "clear"
    assert records["clear-pos"]["evidence_steps"] == []
    assert records["abstain"]["verdict"] == "abstain"
    assert records["abstain"]["score"] is None
    assert records["alarm-despite-unavailable"]["verdict"] == "alarm"
    for record in records.values():
        assert validate_finding(record) == []
        assert (record["detector"], record["detector_version"], record["reader_model"]) == (
            "cascade",
            "window-cascade/v8",
            "kimi-code/k3.1-pro-highspeed",
        )
        assert record["kind"] == "cheat"


def test_pass_through_differs_between_v7_and_v8(source: Path) -> None:
    runs = [_run("submission-only"), _run("grader-only")]
    v8 = _by_id(cascade.CascadeV8Detector(source_dir=source).detect(runs))
    v7 = _by_id(cascade.CascadeV7Detector(source_dir=source).detect(runs))

    assert v8["submission-only"]["verdict"] == "clear"
    assert v7["submission-only"]["verdict"] == "alarm"
    assert v7["submission-only"]["evidence_steps"] == [7]
    assert v7["submission-only"]["detector"] == "cascade-v7"
    assert v7["submission-only"]["detector_version"] == "window-cascade/v7"
    assert v8["grader-only"]["verdict"] == v7["grader-only"]["verdict"] == "alarm"
    assert v8["grader-only"]["evidence_steps"] == [9]


def test_v7_reads_before_rows_not_after_rows(source: Path) -> None:
    rows = _by_id(cascade.CascadeV7Detector(source_dir=source).detect([_run("abstain")]))
    assert rows == {}  # present only in the v8 ("after") rows


def test_reader_override_other_than_stored_judge_returns_nothing(source: Path) -> None:
    options = HarnessOptions(reader="anthropic/claude-haiku-5-5")
    detector = cascade.CascadeV8Detector(options, source_dir=source)
    assert list(detector.detect([_run("alarm-confirmed")])) == []


def test_evidence_step_outside_the_run_is_rejected(source: Path) -> None:
    detector = cascade.CascadeV8Detector(source_dir=source)
    with pytest.raises(ValueError, match="original run step id"):
        list(detector.detect([_run("alarm-confirmed", steps=3)]))


def test_hackbench_rows_serve_v7_and_v8(tmp_path: Path, source: Path) -> None:
    path = tmp_path / "cascade-command.jsonl"
    _write_jsonl(
        path,
        [
            {"trace_key": "aaaa", "cards": [CONFIRMED], "triage_unavailable": 0},
            {"trace_key": "bbbb", "cards": [], "triage_unavailable": 0},
        ],
    )
    runs = [_run("hackbench:aaaa"), _run("hackbench:bbbb"), _run("hackbench:cccc")]
    for detector in (
        cascade.CascadeV8Detector(source_dir=source, hackbench_path=path),
        cascade.CascadeV7Detector(source_dir=source, hackbench_path=path),
    ):
        records = _by_id(detector.detect(runs))
        assert set(records) == {"hackbench:aaaa", "hackbench:bbbb"}
        assert records["hackbench:aaaa"]["verdict"] == "alarm"
        assert records["hackbench:aaaa"]["evidence_steps"] == [4]
        assert records["hackbench:bbbb"]["verdict"] == "clear"


def test_v4_uses_score_verdicts_and_abstains_on_excluded(tmp_path: Path) -> None:
    cm6 = tmp_path / "cm6"
    cm6.mkdir()
    traces = [
        {"trace_id": "hit", "positive": False, "alarmed": True, "excluded": False,
         "confirmed_rules": ["hidden_info_read"]},
        {"trace_id": "quiet", "positive": True, "alarmed": False, "excluded": False},
        {"trace_id": "dropped", "positive": True, "alarmed": False, "excluded": True},
    ]  # fmt: skip
    (cm6 / "e2escore-goodfire-pf_r.json").write_text(json.dumps({"traces": traces}))
    _write_jsonl(
        cm6 / "e2e-goodfire-pf_r.jsonl",
        [_row("hit", [CONFIRMED]), _row("quiet", []), _row("dropped", [], unavailable=1)],
    )
    detector = cascade.CascadeV4Detector(cm6_dir=cm6)
    records = _by_id(detector.detect([_run("hit"), _run("quiet"), _run("dropped"), _run("other")]))

    assert set(records) == {"hit", "quiet", "dropped"}
    assert records["hit"]["verdict"] == "alarm"
    assert records["hit"]["evidence_steps"] == [4]
    assert records["hit"]["score"] == 1.0
    assert records["quiet"]["verdict"] == "clear"
    assert records["dropped"]["verdict"] == "abstain"
    assert records["dropped"]["score"] is None
    assert {record["detector_version"] for record in records.values()} == {
        "window-cascade/v4-cm6-frozen"
    }
    assert detector.name == "cascade-v4"


def test_snapshot_copies_bytes_with_manifest_and_never_overwrites(tmp_path: Path) -> None:
    src, dest = tmp_path / "scratch", tmp_path / "durable"
    src.mkdir()
    for arm in cascade.ARMS:
        for corpus in cascade.CORPORA:
            for suffix in cascade.SNAPSHOT_SUFFIXES:
                (src / f"{arm}-{corpus}.jsonl{suffix}").write_bytes(
                    f"{arm}{corpus}{suffix}".encode()
                )

    manifest = cascade.snapshot_sources(src, dest)
    assert len(manifest["files"]) == 12
    for name, meta in manifest["files"].items():
        assert (dest / name).read_bytes() == (src / name).read_bytes()
        assert meta["sha256"] == cascade._sha256(dest / name)
    assert json.loads((dest / cascade.MANIFEST_NAME).read_text()) == manifest
    assert cascade.snapshot_sources(src, dest) == manifest  # idempotent

    (src / "after-goodfire.jsonl").write_bytes(b"changed")
    with pytest.raises(FileExistsError):
        cascade.snapshot_sources(src, dest)
    assert (dest / "after-goodfire.jsonl").read_bytes() == b"after" + b"goodfire"


def test_snapshot_tampering_is_detected_on_read(tmp_path: Path) -> None:
    src, dest = tmp_path / "scratch", tmp_path / "durable"
    src.mkdir()
    for arm in cascade.ARMS:
        for corpus in cascade.CORPORA:
            for suffix in cascade.SNAPSHOT_SUFFIXES:
                name = f"{arm}-{corpus}.jsonl{suffix}"
                (src / name).write_text(json.dumps(_row("x", [])) + "\n" if not suffix else "{}")
    cascade.snapshot_sources(src, dest)
    (dest / "after-goodfire.jsonl").write_text(json.dumps(_row("x", [CONFIRMED])) + "\n")
    with pytest.raises(ValueError, match="manifest"):
        list(cascade.CascadeV8Detector(source_dir=dest).detect([_run("x")]))
