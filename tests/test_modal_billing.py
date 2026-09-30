"""Modal billing reconcile: pure parse/aggregate/compare logic.

Database upserts and the live ``modal billing report`` call are proved by
the real reconcile run in the delivery receipt, not by unit tests (tests
never touch the network or the shared catalog).
"""

from __future__ import annotations

from datetime import date

import pytest

from evallab import cli
from evallab.modal_billing import (
    BillingRow,
    aggregate_daily,
    compare_days,
    normalize_report_rows,
    render_comparisons,
)


def test_normalize_report_rows_accepts_daily_payload() -> None:
    rows = normalize_report_rows(
        [
            {
                "object_id": "ap-1",
                "description": "evallab-mimo-v26-9b",
                "environment": "main",
                "interval_start": "2026-09-29T00:00:00",
                "cost": "2.04560628",
            },
            {
                "object_id": "ap-2",
                "description": "evallab-mimo-v26-9b",
                "environment": "main",
                "interval_start": "2026-09-29T00:00:00",
                "resource": "A100-80GB",
                "cost": "0.67231152",
            },
        ],
        resolution="d",
    )
    assert rows[0] == BillingRow(
        object_id="ap-1",
        description="evallab-mimo-v26-9b",
        environment="main",
        interval_start=rows[0].interval_start,
        resource="",
        cost_usd=pytest.approx(2.04560628),
    )
    assert rows[1].resource == "A100-80GB"
    assert rows[0].interval_start.date() == date(2026, 9, 29)


@pytest.mark.parametrize(
    "payload",
    [
        {"not": "a list"},
        ["a string row"],
        [{"object_id": "ap-1"}],
        [
            {
                "object_id": "ap-1",
                "description": "x",
                "environment": "main",
                "interval_start": "not-a-date",
                "cost": "1.0",
            }
        ],
    ],
)
def test_normalize_report_rows_rejects_bad_payload(payload: object) -> None:
    with pytest.raises(ValueError):
        normalize_report_rows(payload, resolution="d")


def test_aggregate_daily_sums_across_objects() -> None:
    rows = normalize_report_rows(
        [
            {
                "object_id": "ap-1",
                "description": "evallab-mimo-v26-9b",
                "environment": "main",
                "interval_start": "2026-09-29T00:00:00",
                "cost": "2.0",
            },
            {
                "object_id": "ap-2",
                "description": "evallab-mimo-v26-9b",
                "environment": "main",
                "interval_start": "2026-09-29T05:00:00",
                "cost": "0.5",
            },
            {
                "object_id": "ap-1",
                "description": "evallab-mimo-v26-9b",
                "environment": "main",
                "interval_start": "2026-09-30T00:00:00",
                "cost": "1.0",
            },
        ],
        resolution="d",
    )
    totals = aggregate_daily(rows)
    assert totals[date(2026, 9, 29)] == pytest.approx(2.5)
    assert totals[date(2026, 9, 30)] == pytest.approx(1.0)


def test_compare_days_marks_missing_sides_with_reasons() -> None:
    days = [date(2026, 9, 28), date(2026, 9, 29)]
    comparisons = compare_days(
        days,
        {date(2026, 9, 29): 13.68},
        {
            date(2026, 9, 28): (None, 0, 0, "no self-hosted trials in catalog"),
            date(2026, 9, 29): (None, 52, 52, "self-hosted trials carry no per-trial cost (time-billed)"),
        },
    )
    first, second = comparisons
    assert first.modal_billed_usd is None
    assert first.modal_reason == "no billing rows reported"
    assert second.modal_billed_usd == pytest.approx(13.68)
    assert second.modal_reason is None
    assert second.lab_selfhosted_usd is None
    assert second.lab_trials == 52
    rendered = render_comparisons(comparisons)
    assert "2026-09-28 | n/a | n/a | 0 | no billing rows reported" in rendered
    assert "$13.68" in rendered


def test_billing_reconcile_cli_parses_day_and_range() -> None:
    day_args = cli.parser().parse_args(["modal", "billing-reconcile", "--for", "2026-09-29"])
    assert day_args.for_day == "2026-09-29"
    range_args = cli.parser().parse_args(
        ["modal", "billing-reconcile", "--start", "2026-09-28", "--end", "2026-10-01", "--json"]
    )
    assert (range_args.start, range_args.end, range_args.json) == ("2026-09-28", "2026-10-01", True)
