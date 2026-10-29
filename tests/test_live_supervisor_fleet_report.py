"""The fleet-health report: keeper-log arithmetic, STORM boundaries, null-not-zero, region writer, CLI.

Pure and offline: the log is built in memory, files live under ``tmp_path`` and nothing reads a
session, a clock (``now`` is passed) or the real bridge directory.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from evallab.production import live_supervisor_cli as cli
from evallab.production import live_supervisor_fleet_report as fr

ROOT = Path(__file__).resolve().parents[1]
PLIST = ROOT / "scripts/ops/launchd/com.petermakhnatch.evallab.live-supervisor-fleet-report.plist"
EDT = timezone(timedelta(hours=-4))
T0 = datetime(2026, 10, 28, 10, 0, tzinfo=EDT)
NOW = datetime(2026, 10, 28, 20, 0, tzinfo=EDT)
PANE = "w1C:pT"


def line(at: datetime, body: str, zone: str = "EDT") -> str:
    return f"{at:%Y-%m-%d %H:%M:%S} {zone} {body}"


def dispatch(at: datetime, pane: str, item: str) -> str:
    return line(at, f"dispatch {pane} {item} status=idle -> ok")


def outcome(at: datetime, pane: str, item: str, result: str) -> str:
    return line(at, f"outcome {pane} {item}: {result}")


def report(lines: list[str], *, queue: dict[str, Any] | None = None, state: dict[str, Any] | None = None,
           now: datetime = NOW, hours: float = 24) -> dict[str, Any]:
    return fr.fleet_report("\n".join(lines) + "\n", queue or {}, state or {}, now=now, window_hours=hours)


def minutes(n: float) -> timedelta:
    return timedelta(minutes=n)


# --------------------------------------------------------------------------- durations and gaps


def test_durations_span_a_keeper_restart_and_a_superseded_dispatch_is_no_outcome() -> None:
    lines = [
        dispatch(T0, PANE, "A"),
        line(T0 + minutes(10), "queue keeper start: 4 panes, 9 items"),
        outcome(T0 + minutes(40), PANE, "A", "done"),
        dispatch(T0 + minutes(60), PANE, "B"),
        line(T0 + minutes(70), "queue keeper start: 4 panes, 9 items"),
        dispatch(T0 + minutes(90), PANE, "C"),  # the restart re-queued the lane: B is superseded, not an outcome
        outcome(T0 + minutes(130), PANE, "C", "blocked"),
    ]
    data = report(lines, now=T0 + minutes(130))

    assert data["items"]["done"]["n"] == 1
    assert data["items"]["done"]["median_min"] == pytest.approx(40)
    assert data["items"]["blocked"]["n"] == 1
    assert data["items"]["blocked"]["median_min"] == pytest.approx(40)
    assert data["items"]["superseded"] == {"n": 1, "total_h": pytest.approx(0.5)}
    assert data["items"]["blocked_share_of_hours"] == pytest.approx(0.5)
    assert data["window"]["keeper_starts"] == 2
    # B was never an outcome, so the only idle gap is A done 10:40 -> B dispatched 11:00
    assert data["idle"]["n_gaps"] == 1
    assert data["idle"]["total_h"] == pytest.approx(20 / 60)


def test_duration_arithmetic_uses_the_timezone_label_across_the_fall_back_night() -> None:
    # 2026-11-01 01:30 EDT is 05:30Z; 01:10 EST is 06:10Z: 40 minutes later although the clock reads earlier
    dispatched = line(datetime(2026, 11, 1, 1, 30), f"dispatch {PANE} A -> ok", "EDT")
    closed = line(datetime(2026, 11, 1, 1, 10), f"outcome {PANE} A: done", "EST")
    reopened = line(datetime(2026, 11, 1, 3, 0), f"dispatch {PANE} B -> ok", "EST")
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

    data = fr.fleet_report("\n".join([dispatched, closed, reopened]) + "\n", {}, {}, now=now, window_hours=24)

    assert data["items"]["done"]["median_min"] == pytest.approx(40)
    # EST 01:10 -> 03:00 is 110 minutes: one hold
    assert data["idle"]["total_h"] == pytest.approx(110 / 60)
    assert data["idle"]["holds"] == {"threshold_min": 60.0, "n": 1, "total_h": pytest.approx(110 / 60)}


def test_percentiles_interpolate_and_carry_n() -> None:
    lines: list[str] = []
    for k, length in enumerate((10, 20, 30, 40, 100)):
        start = T0 + minutes(k * 200)
        lines += [dispatch(start, PANE, f"I{k}"), outcome(start + minutes(length), PANE, f"I{k}", "done")]
    done = report(lines, now=T0 + minutes(1500), hours=48)["items"]["done"]

    assert done["n"] == 5
    assert done["median_min"] == pytest.approx(30)
    assert done["p90_min"] == pytest.approx(76)  # 40 + 0.6 * (100 - 40)
    assert done["total_h"] == pytest.approx(200 / 60)


def test_orphan_and_late_outcomes_are_counted_not_timed() -> None:
    lines = [
        outcome(T0, PANE, "X", "blocked"),  # dispatched before the log began
        dispatch(T0 + minutes(30), PANE, "A"),
        outcome(T0 + minutes(40), PANE, "A", "done"),
        outcome(T0 + minutes(50), PANE, "A", "done"),  # a repeat of an outcome already recorded
        outcome(T0 + minutes(55), "w1C:pQ", "Y", "done"),
    ]
    data = report(lines)

    assert data["items"]["orphan_outcomes"] == 2
    assert data["items"]["late_outcomes"] == 1
    assert data["items"]["done"]["n"] == 1
    assert data["items"]["blocked"] == {"n": 0, "median_min": None, "p90_min": None, "total_h": None}
    # the orphan outcome still starts a gap: X blocked 10:00 -> A dispatched 10:30
    assert data["idle"]["n_gaps"] == 3  # X -> A, then PANE and w1C:pQ idle until now
    open_h = ((NOW - (T0 + minutes(40))) + (NOW - (T0 + minutes(55)))).total_seconds() / 3600
    assert data["idle"]["total_h"] == pytest.approx(0.5 + open_h)


def test_an_orphan_item_with_two_outcome_lines_is_one_orphan_and_one_gap() -> None:
    lines = [outcome(T0, PANE, "TT-1", "blocked"), outcome(T0 + minutes(80), PANE, "TT-1", "done"),
             dispatch(T0 + minutes(100), PANE, "A")]
    data = report(lines)

    assert data["items"]["orphan_outcomes"] == 1
    assert data["items"]["late_outcomes"] == 1
    assert data["idle"]["n_gaps"] == 1
    assert data["idle"]["total_h"] == pytest.approx(100 / 60)



def test_a_failed_dispatch_and_a_redispatch_of_the_open_item_open_nothing() -> None:
    lines = [
        line(T0, f"dispatch {PANE} LB-16 status=done -> FAILED: BLOCKED: Target is currently FOCUSED"),
        dispatch(T0 + minutes(5), PANE, "A"),
        dispatch(T0 + minutes(6), PANE, "A"),
        outcome(T0 + minutes(25), PANE, "A", "done"),
    ]
    data = report(lines)

    assert data["items"]["done"]["median_min"] == pytest.approx(20)
    assert data["items"]["superseded"]["n"] == 0


def test_idle_gaps_are_clipped_to_the_window_and_holds_are_strictly_over_an_hour() -> None:
    lines = [
        dispatch(T0, "w1C:pA", "A"), outcome(T0 + minutes(10), "w1C:pA", "A", "done"),
        dispatch(T0 + minutes(70), "w1C:pA", "A2"),  # exactly 60 minutes: a gap, not a hold
        dispatch(T0, "w1C:pB", "B"), outcome(T0 + minutes(10), "w1C:pB", "B", "done"),
        dispatch(T0 + minutes(71), "w1C:pB", "B2"),  # 61 minutes: a hold
    ]
    data = report(lines, now=T0 + minutes(200), hours=24)
    assert data["idle"]["n_gaps"] == 2
    assert data["idle"]["holds"]["n"] == 1
    assert data["idle"]["holds"]["total_h"] == pytest.approx(61 / 60)
    assert data["idle"]["total_h"] == pytest.approx(121 / 60)

    # a window that opens 20 minutes into the gaps sees only the rest of them
    clipped = report(lines, now=T0 + minutes(200), hours=(200 - 30) / 60)
    assert clipped["idle"]["total_h"] == pytest.approx((40 + 41) / 60)
    assert clipped["idle"]["holds"]["total_h"] == pytest.approx(41 / 60)


def test_open_gaps_name_the_idle_panes_with_pending_items() -> None:
    queue = {"panes": {
        "w1C:pA": {"items": [{"id": "A"}, {"id": "A2"}]},
        "w1C:pB": {"items": [{"id": "B"}]},
        "w1C:pC": {"items": [{"id": "C"}]},
    }}
    state = {
        "w1C:pA": {"index": 0, "ledger": {"1": {"id": "A", "outcome": "done"}}},
        "w1C:pB": {"index": 0, "ledger": {"1": {"id": "B", "outcome": "done"}}},
        "w1C:pC": {"index": 0, "ledger": {"1": {"id": "C", "outcome": "done"}}, "parked": "quota"},
    }
    lines = [dispatch(T0, p, i) for p, i in (("w1C:pA", "A"), ("w1C:pB", "B"), ("w1C:pC", "C"), ("w1C:pR", "R"))]
    lines += [outcome(T0 + minutes(30), p, i, "done") for p, i in (("w1C:pA", "A"), ("w1C:pB", "B"), ("w1C:pC", "C"), ("w1C:pR", "R"))]
    data = report(lines, queue=queue, state=state, now=T0 + minutes(90))

    rows = {row["pane"]: row for row in data["idle"]["idle_now"]}
    assert set(rows) == {"w1C:pA", "w1C:pB", "w1C:pC"}  # pR is no longer a queue lane
    assert data["idle"]["unmanaged_idle_panes"] == ["w1C:pR"]
    assert rows["w1C:pA"]["pending"] == 1
    assert rows["w1C:pB"]["pending"] == 0
    assert rows["w1C:pC"] == {**rows["w1C:pC"], "parked": True, "pending": None}
    assert data["idle"]["idle_now_with_pending"] == ["w1C:pA"]
    assert data["idle"]["total_h"] == pytest.approx(3 * 1.0)


def test_a_clean_keeper_stop_ends_open_gaps_at_the_stop_line() -> None:
    lines = [dispatch(T0, PANE, "A"), outcome(T0 + minutes(10), PANE, "A", "done"),
             line(T0 + minutes(70), "queue keeper stop")]
    data = report(lines, now=T0 + minutes(600))

    assert data["window"]["keeper_stopped"] is True
    assert data["idle"]["total_h"] == pytest.approx(1.0)


# --------------------------------------------------------------------------- STORM


def quick_blocked(count: int, item_s: int, span_s: int, *, start: datetime = T0, pane: str = PANE,
                  prefix: str = "S") -> list[str]:
    """``count`` items each blocked ``item_s`` after dispatch whose first dispatch to last outcome is ``span_s``."""
    step = (span_s - item_s) / (count - 1)
    lines: list[str] = []
    for k in range(count):
        opened = start + timedelta(seconds=k * step)
        lines += [dispatch(opened, pane, f"{prefix}{k}"),
                  outcome(opened + timedelta(seconds=item_s), pane, f"{prefix}{k}", "blocked")]
    return lines


@pytest.mark.parametrize(("count", "item_s", "span_s", "flagged"), [
    (3, 60, 600, True),
    (2, 60, 600, False),      # two blocked items are not a storm
    (3, 300, 1800, True),     # exactly 300 s each, exactly a 30-minute window
    (3, 301, 1800, False),    # one second too slow
    (3, 300, 1801, False),    # one second outside the window
    (3, 60, 1860, False),     # 31 minutes
    (5, 60, 1800, True),
])
def test_storm_boundaries(count: int, item_s: int, span_s: int, flagged: bool) -> None:
    data = report(quick_blocked(count, item_s, span_s))["storm"]

    assert data["n_intervals"] == count
    if not flagged:
        assert data["n_episodes"] == 0
        assert data["wasted_lane_min"] is None
        return
    assert data["n_episodes"] == 1
    (episode,) = data["episodes"]
    assert episode["pane"] == PANE
    assert episode["n_items"] == count
    assert episode["wasted_lane_min"] == pytest.approx(span_s / 60)
    assert episode["item_min"] == pytest.approx(count * item_s / 60)


def test_a_slow_blocked_item_or_a_done_item_does_not_join_a_storm() -> None:
    lines = quick_blocked(2, 60, 300)
    lines += [dispatch(T0 + minutes(6), PANE, "S2"), outcome(T0 + minutes(8), PANE, "S2", "done")]
    lines += [dispatch(T0 + minutes(9), PANE, "S3"), outcome(T0 + minutes(19), PANE, "S3", "blocked")]  # 10 min

    assert report(lines)["storm"]["n_episodes"] == 0


def test_episodes_merge_when_the_gap_is_within_a_window_and_split_beyond_it() -> None:
    first = quick_blocked(3, 60, 600, prefix="A")
    last_end = T0 + timedelta(seconds=600)
    joined = report(first + quick_blocked(3, 60, 600, start=last_end + timedelta(seconds=1800), prefix="B"))["storm"]
    split = report(first + quick_blocked(3, 60, 600, start=last_end + timedelta(seconds=1801), prefix="B"))["storm"]

    assert joined["n_episodes"] == 1
    assert joined["episodes"][0]["n_items"] == 6
    assert joined["episodes"][0]["wasted_lane_min"] == pytest.approx((600 + 1800 + 600) / 60)
    assert split["n_episodes"] == 2
    assert split["wasted_lane_min"] == pytest.approx(20)


def test_storms_are_per_pane() -> None:
    lines = quick_blocked(2, 60, 300, pane="w1C:pA", prefix="A") + quick_blocked(2, 60, 300, pane="w1C:pB", prefix="B")

    assert report(lines)["storm"]["n_episodes"] == 0


# --------------------------------------------------------------------------- null, not zero


def test_an_empty_log_has_no_numbers_not_zeros() -> None:
    data = fr.fleet_report("", {}, {}, now=NOW, window_hours=24)

    assert data["window"]["log_first_at"] is None
    assert data["window"]["covered_h"] is None
    assert data["items"]["n_intervals"] == 0
    assert data["items"]["done"] == {"n": 0, "median_min": None, "p90_min": None, "total_h": None}
    assert data["items"]["blocked_share_of_hours"] is None
    assert data["idle"]["total_h"] is None
    assert data["idle"]["holds"]["total_h"] is None
    assert data["storm"]["n_episodes"] is None
    assert data["storm"]["wasted_lane_min"] is None
    assert data["open"]["n"] == 0


def test_a_window_that_misses_every_event_reports_n_zero_and_null_stats() -> None:
    lines = [dispatch(T0, PANE, "A"), outcome(T0 + minutes(30), PANE, "A", "done"),
             dispatch(T0 + minutes(40), PANE, "B"), outcome(T0 + minutes(50), PANE, "B", "done")]
    data = report(lines, now=T0 + timedelta(days=3), hours=2)

    assert data["items"]["done"]["n"] == 0
    assert data["items"]["done"]["median_min"] is None
    assert data["storm"]["n_episodes"] is None
    # the pane is idle since 10:50 on day 1: the window holds 2 h of it, which is data
    assert data["idle"]["total_h"] == pytest.approx(2.0)


def test_covered_hours_say_where_the_log_starts() -> None:
    lines = [dispatch(T0, PANE, "A")]
    data = report(lines, now=T0 + timedelta(hours=10), hours=28)

    assert data["window"]["starts_before_log"] is True
    assert data["window"]["covered_h"] == pytest.approx(10)
    assert report(lines, now=T0 + timedelta(hours=10), hours=4)["window"]["starts_before_log"] is False


def test_bad_arguments_are_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        fr.fleet_report("", {}, {}, now=datetime(2026, 10, 28, 20, 0))
    for hours in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="window_hours"):
            fr.fleet_report("", {}, {}, now=NOW, window_hours=hours)


# --------------------------------------------------------------------------- open items


def test_open_items_flag_strictly_past_three_hours() -> None:
    lines = [dispatch(NOW - minutes(180), "w1C:pA", "EXACT"), dispatch(NOW - minutes(181), "w1C:pB", "OVER"),
             dispatch(NOW - minutes(30), "w1C:pC", "FRESH"),
             dispatch(NOW - minutes(500), "w1C:pD", "GONE")]
    queue = {"panes": {p: {"items": []} for p in ("w1C:pA", "w1C:pB", "w1C:pC")}}
    data = report(lines, queue=queue)["open"]

    flagged = {row["item"]: row for row in data["items"]}
    assert data["n"] == 4
    assert data["n_over_limit"] == 2
    assert not flagged["EXACT"]["over_limit"]
    assert flagged["OVER"]["over_limit"] and flagged["OVER"]["managed"]
    assert flagged["GONE"]["over_limit"] and not flagged["GONE"]["managed"]
    assert [row["item"] for row in data["items"]][0] == "GONE"  # oldest first


def test_a_superseded_dispatch_is_not_open() -> None:
    lines = [dispatch(T0, PANE, "A"), dispatch(T0 + minutes(5), PANE, "B")]
    data = report(lines)["open"]

    assert [row["item"] for row in data["items"]] == ["B"]
    assert data["items"][0]["parked"] is False


def test_an_open_item_on_a_parked_lane_is_marked_parked() -> None:
    lines = [dispatch(NOW - minutes(300), "w1C:pJ", "OQ-6")]
    queue = {"panes": {"w1C:pJ": {"items": [{"id": "OQ-6"}]}}}
    data = report(lines, queue=queue, state={"w1C:pJ": {"parked": "quota block on OQ-6"}})["open"]

    assert data["items"][0]["parked"] is True
    section = fr.render_section(report(lines, queue=queue, state={"w1C:pJ": {"parked": "quota"}}), now=NOW)
    assert "(1 on parked lanes)" in section


# --------------------------------------------------------------------------- rendering


def test_the_section_is_short_labelled_in_new_york_time_and_states_its_limits() -> None:
    lines = quick_blocked(3, 60, 600) + [dispatch(T0 + minutes(120), PANE, "OPEN")]
    now = datetime(2026, 10, 28, 20, 0, tzinfo=UTC)  # 4:00 PM EDT
    section = fr.render_section(report(lines, now=now), now=now)

    rows = section.splitlines()
    assert rows[0] == "## Fleet health (auto, refreshed 10-28 4:00 PM ET)"
    assert len(rows) <= 25
    assert "STORM had 1 episode" in section and "precision 1/1" in section and "2/5 precise" in section
    assert "log covers" in section  # the window starts before the log: said, not hidden
    assert "1 episode(s) in 3 intervals" in section
    assert "10-28 10:00 AM ET" in section


def test_the_section_of_an_empty_log_says_nothing_is_known() -> None:
    section = fr.render_section(fr.fleet_report("", {}, {}, now=NOW), now=NOW)

    assert "log is empty" in section
    assert "n=0" in section
    assert "no outcome→dispatch gap" in section


# --------------------------------------------------------------------------- the marked region


BEGIN, END = fr.BEGIN_MARK.decode(), fr.END_MARK.decode()


def test_write_section_appends_the_region_and_is_idempotent(tmp_path: Path) -> None:
    doc = tmp_path / "live-supervisor.md"
    original = "# Live Supervisor\n\nhand-written notes\r\nno trailing newline"
    doc.write_bytes(original.encode())

    assert fr.write_section(doc, "## Fleet health\n\nbody") is True
    first = doc.read_bytes()
    assert first.startswith(original.encode() + b"\n\n" + fr.BEGIN_MARK)
    assert first.endswith(fr.END_MARK + b"\n")
    assert fr.write_section(doc, "## Fleet health\n\nbody") is False
    assert doc.read_bytes() == first


def test_write_section_replaces_only_the_region_and_keeps_every_other_byte(tmp_path: Path) -> None:
    doc = tmp_path / "live-supervisor.md"
    head = b"# Notes\n\n  trailing spaces  \n\tTabs\n---\n"
    tail = "\n\n## Later section\n\nÜnïcode ✓\r\nlast line".encode()
    doc.write_bytes(head + fr.BEGIN_MARK + b"\nOLD\nOLD\n" + fr.END_MARK + tail)

    assert fr.write_section(doc, "NEW\n") is True
    assert doc.read_bytes() == head + fr.BEGIN_MARK + b"\nNEW\n" + fr.END_MARK + tail
    assert fr.write_section(doc, "NEWER") is True
    assert doc.read_bytes() == head + fr.BEGIN_MARK + b"\nNEWER\n" + fr.END_MARK + tail
    assert sorted(p.name for p in tmp_path.iterdir()) == ["live-supervisor.md", "live-supervisor.md.lock"]


def test_write_section_creates_a_missing_file_and_keeps_the_mode(tmp_path: Path) -> None:
    new = tmp_path / "new.md"
    assert fr.write_section(new, "x") is True
    assert new.read_bytes() == fr.BEGIN_MARK + b"\nx\n" + fr.END_MARK + b"\n"

    doc = tmp_path / "mode.md"
    doc.write_text("a\n")
    doc.chmod(0o600)
    fr.write_section(doc, "y")
    assert doc.stat().st_mode & 0o777 == 0o600


def test_write_section_follows_a_symlink_instead_of_replacing_it(tmp_path: Path) -> None:
    real = tmp_path / "real.md"
    real.write_text("keep\n")
    link = tmp_path / "link.md"
    link.symlink_to(real)

    fr.write_section(link, "z")

    assert link.is_symlink()
    assert real.read_bytes().startswith(b"keep\n\n" + fr.BEGIN_MARK)


@pytest.mark.parametrize("content", [
    f"{BEGIN}\nhalf a region",
    f"half a region\n{END}",
    f"{END}\n{BEGIN}",
    f"{BEGIN}\na\n{END}\n{BEGIN}\nb\n{END}",
])
def test_write_section_refuses_broken_markers_and_leaves_the_file(tmp_path: Path, content: str) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text(content)

    with pytest.raises(ValueError, match="fleet-health"):
        fr.write_section(doc, "x")
    assert doc.read_text() == content
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_a_failed_write_leaves_the_file_and_no_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("before\n")

    def boom(*_args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(fr.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        fr.write_section(doc, "x")
    assert doc.read_text() == "before\n"
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_two_concurrent_writers_are_serialised_and_the_file_stays_well_formed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = tmp_path / "doc.md"
    surround = b"top\n" + fr.BEGIN_MARK + b"\nseed\n" + fr.END_MARK + b"\nbottom"
    doc.write_bytes(surround)
    trace: list[str] = []
    real: Callable[[bytes, str], bytes] = fr.replace_region

    def slow(data: bytes, text: str) -> bytes:
        trace.append(f"enter {text}")
        time.sleep(0.15)  # long enough for the other writer to arrive and block on the lock
        out = real(data, text)
        trace.append(f"exit {text}")
        return out

    monkeypatch.setattr(fr, "replace_region", slow)
    start = threading.Barrier(2)
    errors: list[BaseException] = []

    def writer(text: str) -> None:
        try:
            start.wait()
            fr.write_section(doc, text)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(name,)) for name in ("first", "second")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    kinds = [entry.split()[0] for entry in trace]
    assert kinds == ["enter", "exit", "enter", "exit"], trace  # never interleaved
    assert trace[1].split()[1] == trace[0].split()[1]
    assert doc.read_bytes() in (surround.replace(b"seed", b"first"), surround.replace(b"seed", b"second"))


# --------------------------------------------------------------------------- CLI and launchd


def _parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="evallab")
    cli.register_parser(parser.add_subparsers(dest="action", required=True))
    return parser.parse_args(["live-supervisor", "fleet-report", *argv])


def _bridge(tmp_path: Path) -> Path:
    """A bridge directory whose log is relative to the real clock, since the CLI reads it."""
    now = datetime.now(UTC).astimezone(EDT).replace(microsecond=0)
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    (bridge / "_queue.log").write_text("\n".join([
        dispatch(now - minutes(100), PANE, "A"), outcome(now - minutes(60), PANE, "A", "done"),
        dispatch(now - minutes(30), PANE, "B"),
    ]) + "\n")
    (bridge / "queue.json").write_text(json.dumps({"panes": {PANE: {"items": [{"id": "A"}, {"id": "B"}]}}}))
    (bridge / "queue-state.json").write_text(json.dumps({PANE: {"index": 1}}))
    return bridge


def test_fleet_report_registers_with_defaults_and_without_importing_the_report() -> None:
    args = _parse()

    assert args.func is cli._fleet_report_command
    assert (args.bridge, args.window_hours, args.out, args.write, args.json) == (None, 24.0, None, False, False)
    assert "live_supervisor_fleet_report" not in cli.__dict__


def test_fleet_report_prints_by_default_and_never_writes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bridge = _bridge(tmp_path)
    args = _parse("--bridge", str(bridge), "--window-hours", "6")

    assert args.func(args, tmp_path) == 0

    out, err = capsys.readouterr()
    assert out.startswith("## Fleet health (auto, refreshed ")
    assert err == ""
    assert not (bridge / "live-supervisor.md").exists()


def test_fleet_report_json_prints_the_numbers(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = _parse("--bridge", str(_bridge(tmp_path)), "--json")

    assert args.func(args, tmp_path) == 0

    data = json.loads(capsys.readouterr().out)
    assert data["schema"] == fr.SCHEMA
    assert data["items"]["done"]["n"] == 1
    assert data["open"]["n"] == 1


def test_fleet_report_write_updates_the_bridge_document_in_place(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path)
    doc = bridge / "live-supervisor.md"
    doc.write_text("# Live Supervisor\n\nlane notes\n")
    args = _parse("--bridge", str(bridge), "--write")

    assert args.func(args, tmp_path) == 0

    text = doc.read_text()
    assert text.startswith("# Live Supervisor\n\nlane notes\n\n" + BEGIN)
    assert "## Fleet health (auto, refreshed " in text and text.endswith(END + "\n")


def test_fleet_report_out_needs_write_and_writes_the_named_file(tmp_path: Path,
                                                                  capsys: pytest.CaptureFixture[str]) -> None:
    bridge = _bridge(tmp_path)
    target = tmp_path / "copy.md"
    refused = _parse("--bridge", str(bridge), "--out", str(target))

    assert refused.func(refused, tmp_path) == 2
    assert "--write" in capsys.readouterr().err
    assert not target.exists()

    accepted = _parse("--bridge", str(bridge), "--out", str(target), "--write")
    assert accepted.func(accepted, tmp_path) == 0
    assert BEGIN in target.read_text()
    assert not (bridge / "live-supervisor.md").exists()


def test_fleet_report_reports_a_missing_log_and_a_bad_window(tmp_path: Path,
                                                               capsys: pytest.CaptureFixture[str]) -> None:
    missing = _parse("--bridge", str(tmp_path / "nowhere"))
    assert missing.func(missing, tmp_path) == 2
    assert "_queue.log" in capsys.readouterr().err

    bad = _parse("--bridge", str(tmp_path), "--window-hours", "0")
    assert bad.func(bad, tmp_path) == 2
    assert "--window-hours" in capsys.readouterr().err


def test_launchd_template_refreshes_the_report_every_four_hours() -> None:
    template = PLIST.read_text(encoding="utf-8")
    loaded = plistlib.loads(PLIST.read_bytes())

    assert loaded["Label"] == "com.petermakhnatch.evallab.live-supervisor-fleet-report"
    assert loaded["StartInterval"] == 14400
    assert loaded["RunAtLoad"] is True
    assert loaded["ProgramArguments"] == ["__EVALLAB__", "live-supervisor", "fleet-report", "--write"]
    assert loaded["EnvironmentVariables"]["EVALLAB_DISABLE_LOCAL_DOCKER"] == "1"
    for placeholder in ("__EVALLAB__", "__WORKDIR__", "__HOME__", "__LOG_DIR__"):
        assert placeholder in template
