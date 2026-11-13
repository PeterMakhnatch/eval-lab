"""Behavioural tests for the pre-launch spend-cap check (HAR-129).

Covers, against consumer-visible behaviour (never wiring):
- half-open window boundary arithmetic (overlap, spend.jsonl ts bounds);
- Modal hourly granularity (a row counts whole when its hour starts in
  the window) and the stale-rows note;
- in-flight reservation ``max(cost_limit_usd, est_cost_usd)``;
- refusal strictly above the cap; exactly-at-cap allows (documented);
- fail-closed paths: unreachable catalog, unratable cloud specs, and
  stale Modal billing rows (>4h);
- Modal refresh on staleness with injected fetcher;
- ``--allow-stale-modal`` override downgrading refusal to warning;
- CLI exit codes 0 / 3 / 2.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from evallab.spend_day import (
    day_to_window,
    window_overlap_seconds,
)

UTC_DAY = date(2026, 10, 1)
DAY_START = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)






# ---------------------------------------------------------------------------
# 1. Half-open window boundary arithmetic
# ---------------------------------------------------------------------------


def test_window_overlap_exact_slice_and_half_open_edges() -> None:
    start = datetime(2026, 10, 1, 4, 0, tzinfo=UTC)
    end = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    # Interval fully inside the window counts whole.
    assert window_overlap_seconds(
        datetime(2026, 10, 1, 5, 0, tzinfo=UTC),
        datetime(2026, 10, 1, 6, 0, tzinfo=UTC),
        start,
        end,
    ) == pytest.approx(3600.0)
    # Half-open edges: touching the window is not overlapping it.
    assert window_overlap_seconds(datetime(2026, 10, 1, 3, 0, tzinfo=UTC), start, start, end) == 0.0
    assert window_overlap_seconds(end, datetime(2026, 10, 1, 13, 0, tzinfo=UTC), start, end) == 0.0
    # Window fully inside the interval counts the whole window.
    assert window_overlap_seconds(
        datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 10, 2, 0, 0, tzinfo=UTC),
        start,
        end,
    ) == pytest.approx(8 * 3600.0)


def test_adjacent_windows_sum_to_whole_and_match_day() -> None:
    trial_start = datetime(2026, 9, 30, 23, 30, tzinfo=UTC)
    trial_end = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)
    midnight = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    before = window_overlap_seconds(
        trial_start, trial_end, datetime(2026, 9, 30, 0, 0, tzinfo=UTC), midnight
    )
    after = window_overlap_seconds(trial_start, trial_end, midnight, DAY_START + timedelta(days=1))
    assert before == pytest.approx(1800.0)
    assert after == pytest.approx(3600.0)
    assert before + after == pytest.approx((trial_end - trial_start).total_seconds())
    # A day is the special case of a window.
    assert window_overlap_seconds(trial_start, trial_end, *day_to_window(UTC_DAY)) == pytest.approx(
        after
    )
    assert window_overlap_seconds(
        trial_start, trial_end, *day_to_window(date(2026, 9, 30))
    ) == pytest.approx(before)






# ---------------------------------------------------------------------------
# 2. Modal hourly granularity (fake catalog)
# ---------------------------------------------------------------------------










# ---------------------------------------------------------------------------
# 3. Modal staleness & refresh paths (HAR-129 Part A fixes)
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# 4. In-flight reservation
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# 5. Commit-vs-cap verdict (pure arithmetic)
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# 6. Fail-closed orchestration
# ---------------------------------------------------------------------------

REFUSED_PORT_URL = "postgresql://127.0.0.1:1/evallab"






# ---------------------------------------------------------------------------
# 7. CLI: parser + exit codes 0 / 3 / 2
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# 6. Regression tests for PR #623 review findings
# ---------------------------------------------------------------------------






















