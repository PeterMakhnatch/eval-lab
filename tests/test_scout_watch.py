"""Completion-ledger boundaries for incremental deterministic scanning."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.readers.scout_watch import completed_runs

SCANNERS = ("repeated_assistant_message", "harness_parse_errors", "native_tool_call_markup", "restored_tool_calls")


def _row(run: str, scanner: str, **changes: object) -> dict:
    return {
        "run_id": run, "tool": "scout", "source": "cloud-trace-db",
        "detector": scanner, "detector_version": "inspect-scout/0.5.4:1",
        "value": 0, "flag": None if scanner == "restored_tool_calls" else False,
        **changes,
    }


def test_imported_or_partially_scanned_runs_remain_pending(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    rows = [_row("complete", scanner) for scanner in SCANNERS]
    rows += [_row("partial", scanner) for scanner in SCANNERS[:-1]]
    rows += [_row("error", scanner, value=None) for scanner in SCANNERS]
    rows += [_row("old-version", scanner, detector_version="inspect-scout/0.5.3:1")
             for scanner in SCANNERS]
    rows += [_row("external", scanner, source="cheatbench-actions-v1") for scanner in SCANNERS]
    results.write_text("".join(json.dumps(row) + "\n" for row in rows))
    # Informational null flags must not cause repeated scans, but missing values
    # and missing scanners must never be mistaken for completed negative scans.
    assert completed_runs(results, scanners=SCANNERS, scout_version="0.5.4") == {"complete"}


def test_recovered_scan_finishes_previously_failed_run(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    rows = [_row("recovered", scanner, value=None) for scanner in SCANNERS]
    rows += [_row("recovered", scanner + ":label", value=1) for scanner in SCANNERS]
    results.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert completed_runs(results, scanners=SCANNERS, scout_version="0.5.4") == {"recovered"}
    # A newly added scanner invalidates coverage, rather than silently treating
    # the former scanner set as complete for the new pipeline.
    assert completed_runs(results, scanners=(*SCANNERS, "new_rule"), scout_version="0.5.4") == set()


def test_missing_ledger_means_nothing_has_been_scanned(tmp_path: Path) -> None:
    assert completed_runs(tmp_path / "missing", scanners=SCANNERS, scout_version="0.5.4") == set()
    with pytest.raises(ValueError, match="At least one"):
        completed_runs(tmp_path / "missing", scanners=(), scout_version="0.5.4")
