"""Deterministic Modal billing normalization and comparison regressions.

Provider responses are recorded-shape JSON. Persistence is exercised separately
with a throwaway PostgreSQL smoke, never through the automatic unit suite.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import pytest

from evallab.modal_billing import BillingRow, normalize_report_rows


def _report_entry(**overrides: Any) -> dict[str, Any]:
    return {
        "object_id": "ap-1",
        "description": "mimo-clean-census-python",
        "environment": "main",
        "interval_start": "2026-10-15T00:00:00+00:00",
        "cost": "2.04560628",
        **overrides,
    }


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
    assert rows[0].tags == rows[1].tags == {}


@pytest.mark.parametrize("encoded", [False, True], ids=["object", "sdk-cli-string"])
def test_normalize_report_rows_preserves_provider_tags_and_cost(encoded: bool) -> None:
    tags = {"spend_tag": "tag2026-10-15", "unknown/team": "équipe", "empty": ""}
    rows = normalize_report_rows(
        [_report_entry(tags=json.dumps(tags) if encoded else tags)],
        resolution="h",
    )

    assert rows[0].tags == tags
    assert rows[0].cost_usd == pytest.approx(2.04560628)


@pytest.mark.parametrize(
    "tags",
    [
        None,
        "{broken",
        "[]",
        {"key": None},
        {1: "value"},
    ],
)
def test_normalize_report_rows_rejects_malformed_tags(tags: object) -> None:
    with pytest.raises(ValueError):
        normalize_report_rows([_report_entry(tags=tags)], resolution="h")


@pytest.mark.parametrize("tags", [{}, "{}"], ids=["object", "sdk-cli-string"])
def test_normalize_report_rows_accepts_empty_tags(tags: object) -> None:
    assert normalize_report_rows([_report_entry(tags=tags)], resolution="h")[0].tags == {}


def test_billing_row_rejects_non_string_tags() -> None:
    with pytest.raises(ValueError):
        BillingRow(
            object_id="ap-1",
            description="mimo-clean-census-python",
            environment="main",
            interval_start=datetime(2026, 10, 15, tzinfo=UTC),
            resource="",
            cost_usd=1.0,
            tags={"spend_tag": 15},
        )


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


