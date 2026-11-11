"""Retained findings: identity, latest-record precedence, and invalid input accounting."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from evallab.score import metrics as harness

NEEDLE = "label-needle-0001"
RUN = "retained-trace:run-a"


def _finding(**overrides: Any) -> dict[str, Any]:
    record = {
        "run_id": RUN, "detector": "facets", "detector_version": "run-facets/v1", "reader_model": "provider/reader",
        "verdict": "alarm", "score": 0.8, "kind": "cheat", "evidence_steps": [2, 5], "summary": "Read the hidden tests.",
        "input_tokens": 900, "output_tokens": 120, "cost_usd": 0.01, "latency_ms": 1500,
        "produced_at": "2026-10-28T10:00:00+00:00",
    }
    return record | overrides


def _write_lines(path: Path, rows: list[Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join((row if isinstance(row, str) else json.dumps(row)) + "\n" for row in rows), encoding="utf-8")
    return path




def test_the_newest_record_per_detector_version_and_reader_wins(tmp_path: Path) -> None:
    path = _write_lines(tmp_path / "findings.jsonl", [
        _finding(verdict="alarm", produced_at="2026-10-28T11:00:00+00:00"),
        _finding(verdict="clear", produced_at="2026-10-28T09:00:00+00:00"),
        _finding(reader_model="provider/other", verdict="abstain"),
    ])
    loaded = harness.load_findings(path)
    kept = loaded["by_run"][RUN]
    assert kept[("facets", "run-facets/v1", "provider/reader")]["verdict"] == "alarm"
    assert kept[("facets", "run-facets/v1", "provider/other")]["verdict"] == "abstain"


def test_findings_drop_label_and_task_fields(tmp_path: Path) -> None:
    extra = {"label": NEEDLE, "reward": 1.0, "outcome": NEEDLE, "task_text": NEEDLE, "dataset_id": NEEDLE, "model_id": NEEDLE}
    path = _write_lines(tmp_path / "findings.jsonl", [_finding(**extra)])
    records = harness.load_findings(path)["by_run"][RUN]
    assert all(not set(extra).intersection(record) for record in records.values())


@pytest.mark.parametrize("version", [
    "metr-scanners/0.1.11@c12893d1+scanner-v0+native-actor-v2",  # as the harness writes a pinned scanner
    "tripwires/v3+window-cascade/v8+prefilter/7",
    "run-health/3",
])
def test_the_versions_the_harness_writes_are_accepted(version: str) -> None:
    assert harness.parse_finding(_finding(detector_version=version))["detector_version"] == version


@pytest.mark.parametrize("version", ["v1; scanner", "a" * 81, "-v1", "v 1", ""])
def test_a_version_that_is_not_an_identifier_is_refused(version: str) -> None:
    with pytest.raises(harness.Refused, match="version"):
        harness.parse_finding(_finding(detector_version=version))


REFUSED: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
    ("not a detector identifier", lambda r: r.update(detector="Facets Detector")),
    ("verdict outside the contract", lambda r: r.update(verdict="clean")),
    ("kind outside the contract", lambda r: r.update(kind="cheating")),
    ("reader without a provider", lambda r: r.update(reader_model="reader")),
    ("negative cost", lambda r: r.update(cost_usd=-0.1)),
    ("non-finite score", lambda r: r.update(score=float("inf"))),
    ("boolean token count", lambda r: r.update(input_tokens=True)),
    ("fractional latency", lambda r: r.update(latency_ms=1.5)),
    ("more than twenty steps", lambda r: r.update(evidence_steps=list(range(21)))),
    ("negative step", lambda r: r.update(evidence_steps=[-1])),
    ("summary over forty words", lambda r: r.update(summary=" ".join(["word"] * 41))),
    ("no time zone", lambda r: r.update(produced_at="2026-10-28T10:00:00")),
    ("no run", lambda r: r.pop("run_id")),
]


@pytest.mark.parametrize(("name", "mutate"), REFUSED, ids=[name for name, _ in REFUSED])
def test_a_record_outside_the_contract_is_refused_and_counted_never_stored(
    tmp_path: Path, name: str, mutate: Callable[[dict[str, Any]], None]
) -> None:
    bad = _finding()
    mutate(bad)
    path = _write_lines(tmp_path / "findings.jsonl", [bad, _finding(run_id="retained-trace:run-b"), "{not json", '{"a":1,"a":2}'])
    loaded = harness.load_findings(path)
    assert set(loaded["by_run"]) == {"retained-trace:run-b"}
    assert sum(loaded["refused"].values()) == 3 and loaded["lines"] == 4
    assert NEEDLE not in json.dumps(loaded["refused"])


def test_the_findings_file_states_absent_linked_and_oversize_never_echo_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert harness.load_findings(tmp_path / "missing.jsonl")["error"] == "not retained"
    real = _write_lines(tmp_path / "real.jsonl", [_finding()])
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)
    assert harness.load_findings(link)["error"] == "unreadable findings file"
    monkeypatch.setattr(harness, "MAX_FINDINGS_BYTES", 10)
    assert harness.load_findings(real)["present"] is False


