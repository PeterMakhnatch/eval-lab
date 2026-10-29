"""Coverage reporting over heartbeat records; no live supervisor or sleep log required."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from evallab.production import live_supervisor_fleet_report as report


def row(at: str, status: str = "ok") -> str:
    return json.dumps({"started_at": at, "reader_status": status})


def test_coverage_crosses_fall_dst_and_counts_bad_rows() -> None:
    now = datetime(2026, 11, 1, 7, 0, tzinfo=UTC)
    text = "\n".join([
        row("2026-11-01T01:58:00-04:00"),
        row("2026-11-01T06:00:00+00:00", "budget_exhausted"),
        "{broken",
        row("2026-11-01T01:30:00-05:00", "rules"),
        row("2026-11-01T06:32:00+00:00", "headroom_blocked"),
    ])
    sleep = datetime(2026, 11, 1, 6, 10, tzinfo=UTC)
    data = report.supervisor_coverage(text, now=now, window_hours=1, sleep_times=[sleep])
    assert data["awake_h"] == pytest.approx(2 / 60)
    assert data["cycles"] == 3
    assert data["non_ok"] == 3
    assert data["malformed"] == 1
    assert data["first_cap_today"] == "2026-11-01T06:00:00+00:00"
    assert data["gaps"][0] == {"start": "2026-11-01T06:00:00+00:00",
                                "end": "2026-11-01T06:30:00+00:00", "cause": "sleep observed"}
    assert data["gaps"][1]["cause"] == "unknown"
    assert report._et(data["gaps"][0]["end"]) == "11-01 1:30 AM ET"


def test_missing_and_empty_heartbeats_are_null(tmp_path: Path) -> None:
    bridge, root = tmp_path / "bridge", tmp_path / "root"
    bridge.mkdir()
    root.mkdir()
    (bridge / "_queue.log").write_text("")
    for content in (None, "", "bad\n"):
        if content is not None:
            (root / "cycles-v1.jsonl").write_text(content)
        data, markdown = report.run_fleet_report(bridge, root=root, now=datetime(2026, 10, 29, tzinfo=UTC))
        coverage = data["supervisor_coverage"]
        assert coverage["awake_h"] is None
        assert coverage["cycles"] is None
        assert coverage["gaps"] is None
        assert coverage["malformed"] == (1 if content == "bad\n" else 0)
        assert "Supervisor coverage" in markdown and "n/a" in markdown


def test_coverage_clips_adjacent_intervals_and_strict_gap_threshold() -> None:
    now = datetime(2026, 10, 29, 12, tzinfo=UTC)
    records = [now - timedelta(minutes=m) for m in (61, 59, 49, 46, 35, 0)]
    data = report.supervisor_coverage("\n".join(row(at.isoformat()) for at in records), now=now, window_hours=1)
    assert data["awake_h"] == pytest.approx(4 / 60)
    assert len(data["gaps"]) == 2
    assert all(gap["cause"] == "unknown" for gap in data["gaps"])


def test_pmset_only_marks_explicit_sleep_and_fails_closed() -> None:
    text = ("2026-10-29 01:36:00 -0400 Sleep                Entering Sleep state\n"
            "2026-10-29 08:45:00 -0400 Wake                 Wake from Sleep\n")
    with patch.object(report.subprocess, "run") as run:
        run.return_value.stdout = text
        assert report._sleep_times() == [datetime.fromisoformat("2026-10-29T01:36:00-04:00")]
        run.side_effect = FileNotFoundError()
        assert report._sleep_times() == []
