from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dashboard.queries import (
    ATIF_SUMMARY_SQL,
    CALIBRATION_SQL,
    CANARY_SQL,
    LEADERBOARD_SQL,
    PANES,
    SPEND_SQL,
    TOOL_USAGE_SQL,
    AttachSource,
    ZoneUnavailableError,
    atif_activity,
    calibration_history,
    canary_history,
    daily_ceiling,
    discoveries,
    knowledge_front_matter,
    leaderboard,
    queue_funnel,
    spend_history,
)
from evallab.runner import database_url_from_environment
from evallab.storage.attach import AttachResult, ZoneStatus


@pytest.fixture
def fixture_rows() -> dict[str, list[dict[str, Any]]]:
    path = Path(__file__).parent / "fixtures/dashboard.json"
    return json.loads(path.read_text(encoding="utf-8"))


class FixtureSource:
    def __init__(self, rows: dict[str, list[dict[str, Any]]], *, calibration: bool = True) -> None:
        self.rows = rows
        self.calibration = calibration

    def query(self, statement: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        del parameters
        return {
            LEADERBOARD_SQL: self.rows["leaderboard"],
            CANARY_SQL: self.rows["canaries"],
            SPEND_SQL: self.rows["spend"],
            CALIBRATION_SQL: self.rows["calibrations"],
            ATIF_SUMMARY_SQL: self.rows.get("atif_summary", []),
            TOOL_USAGE_SQL: self.rows.get("atif_tools", []),
        }[statement]

    def relation_exists(self, name: str) -> bool:
        if not self.calibration:
            return False
        return name in ("judge_calibrations", "tool_usage", "trial_facts")


def test_panes_mapping_declared_and_covers_views():
    expected_panes = {"leaderboard", "canaries", "spend", "calibrations", "atif", "discoveries"}
    assert set(PANES.keys()) == expected_panes
    assert PANES["leaderboard"] == "z2.trials"
    assert PANES["canaries"] == "z2.canary_drift_observations"
    assert PANES["spend"] == "z2.trials"
    assert PANES["calibrations"] == "z2.judge_calibrations"
    assert PANES["atif"] == "trial_facts"
    assert PANES["discoveries"] == "z4.front_matter"


def test_production_queries_are_select_only():
    for statement in (
        LEADERBOARD_SQL,
        CANARY_SQL,
        SPEND_SQL,
        CALIBRATION_SQL,
        ATIF_SUMMARY_SQL,
        TOOL_USAGE_SQL,
    ):
        normalized = " ".join(statement.upper().split())
        assert normalized.startswith("SELECT ")
        assert not any(
            token in f" {normalized} "
            for token in (" INSERT ", " UPDATE ", " DELETE ", " CREATE ", " DROP ", " ALTER ")
        )


def test_leaderboard_has_denominator_exceptions_and_wilson_interval(fixture_rows):
    rows = leaderboard(FixtureSource(fixture_rows))
    assert rows[0]["n_total"] == 3
    assert rows[0]["n"] == 2
    assert rows[0]["exceptions"] == 1
    assert rows[0]["pass_rate"] == 0.5
    assert rows[0]["ci_95_low"] == pytest.approx(0.0945312)
    assert rows[0]["ci_95_high"] == pytest.approx(0.9054688)


def test_unscorable_cohort_is_distinguishable_from_no_data(fixture_rows):
    """`pass@1` renders `—` when nothing is scorable, which must not read as
    "no data yet". The statistic is unchanged; only the basis is now stated.
    """
    rows = {
        "leaderboard": [
            {
                "cohort": "c",
                "trial_id": str(index),
                "task_name": "lab/demo",
                "agent_name": "codex",
                "model_name": "gpt-5.6-terra",
                "primary_reward": reward,
                "exception_type": exception,
            }
            for index, (reward, exception) in enumerate(
                [(None, "AgentTimeoutError"), (None, "AgentTimeoutError"), (None, None)]
            )
        ]
    }
    (row,) = leaderboard(FixtureSource({**fixture_rows, **rows}))

    assert row["pass_rate"] is None  # unchanged: no scorable trial, no statistic
    assert row["ci_95_low"] is None and row["ci_95_high"] is None
    assert row["scorable"] is False
    assert row["n_total"] == 3 and row["n"] == 0
    assert row["exceptions"] == 2
    assert row["unscored_no_reward"] == 1
    # a scorable cohort reports the same fields, positively
    scorable = leaderboard(FixtureSource(fixture_rows))[0]
    assert scorable["scorable"] is True and scorable["unscored_no_reward"] == 0


def test_canary_query_adds_baseline_confidence_interval(fixture_rows):
    rows = canary_history(FixtureSource(fixture_rows))
    assert rows[0]["baseline_95_low"] == pytest.approx(0.6520018)
    assert rows[0]["baseline_95_high"] == pytest.approx(0.8479982)


def test_spend_query_fills_missing_days(fixture_rows):
    rows = spend_history(FixtureSource(fixture_rows), through=date(2026, 8, 14), days=3)
    assert rows == [
        {"date": date(2026, 8, 12), "trial_count": 0, "spend_usd": 0.0},
        {"date": date(2026, 8, 13), "trial_count": 2, "spend_usd": 1.25},
        {"date": date(2026, 8, 14), "trial_count": 1, "spend_usd": 0.5},
    ]


def test_queue_and_policy_queries_use_file_fixtures(tmp_path):
    queue = tmp_path / "queue"
    for state, count in {"pending": 2, "approved": 1, "running": 0, "done": 3, "failed": 1}.items():
        directory = queue / state
        directory.mkdir(parents=True)
        for index in range(count):
            (directory / f"spec-{index}.json").write_text("{}", encoding="utf-8")
    policy = tmp_path / "policy.yaml"
    policy.write_text("daily_cost_ceiling_usd: 20\n", encoding="utf-8")

    assert queue_funnel(queue) == [
        {"state": "pending", "count": 2},
        {"state": "approved", "count": 1},
        {"state": "running", "count": 0},
        {"state": "done", "count": 3},
        {"state": "failed", "count": 1},
    ]
    assert daily_ceiling(policy) == 20.0


def test_calibration_query_uses_catalog_and_file_fallback(fixture_rows, tmp_path):
    catalog = calibration_history(FixtureSource(fixture_rows), records_root=tmp_path)
    assert catalog[0]["n"] == 10
    assert catalog[0]["agreement"] == 0.9
    assert catalog[0]["ci_95_low"] is not None

    family = tmp_path / "family-a"
    family.mkdir()
    (family / "record.json").write_text(
        json.dumps(fixture_rows["calibrations"][0]), encoding="utf-8"
    )
    files = calibration_history(
        FixtureSource(fixture_rows, calibration=False), records_root=tmp_path
    )
    assert [row["record_id"] for row in files] == ["cal-1"]


def test_atif_activity_with_attach_surface_and_derived_root_override(tmp_path, monkeypatch):
    """Point attach surface at fixture derived root via EVALLAB_DERIVED_ROOT and assert rows."""
    derived_root = tmp_path / "derived_parquet"
    partition = derived_root / "job_id=j1" / "trial_id=t1"
    partition.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "trial_id": "t1",
                    "trajectory_count": 1,
                    "invalid_trajectory_count": 0,
                    "step_count": 4,
                    "llm_call_count": 2,
                    "tool_call_count": 3,
                }
            ]
        ),
        partition / "trial_facts.parquet",
    )
    pq.write_table(
        pa.Table.from_pylist(
            [{"trial_id": "t1", "function_name": "shell", "call_count": 3}]
        ),
        partition / "tool_usage.parquet",
    )

    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived_root))
    source = AttachSource(explicit_derived=derived_root)
    try:
        activity = atif_activity(source)
        assert activity["summary"] == {
            "trial_count": 1,
            "trajectory_count": 1,
            "step_count": 4,
            "llm_call_count": 2,
            "tool_call_count": 3,
            "invalid_trial_count": 0,
        }
        assert activity["tools"] == [
            {"function_name": "shell", "call_count": 3, "trial_count": 1}
        ]
    finally:
        source.close()


def test_zone_unavailable_reports_reason_rather_than_empty(tmp_path):
    """When a zone cannot attach, panes must raise ZoneUnavailableError with reason."""
    fake_zones = (
        ZoneStatus(
            "z2", False, reason="Postgres connection refused", detail="localhost:54329/evallab"
        ),
        ZoneStatus(
            "z3", False, reason="derived root does not exist", detail=str(tmp_path / "missing")
        ),
        ZoneStatus(
            "z4", False, reason="docs directory does not exist", detail=str(tmp_path / "docs")
        ),
    )
    fake_result = AttachResult(duckdb.connect(":memory:"), fake_zones, "")
    source = AttachSource(fake_result)
    try:
        with pytest.raises(
            ZoneUnavailableError, match="zone z2 unavailable: Postgres connection refused"
        ):
            leaderboard(source)

        with pytest.raises(
            ZoneUnavailableError, match="zone z2 unavailable: Postgres connection refused"
        ):
            canary_history(source)

        with pytest.raises(
            ZoneUnavailableError, match="zone z2 unavailable: Postgres connection refused"
        ):
            spend_history(source, through=date(2026, 8, 14), days=3)

        with pytest.raises(
            ZoneUnavailableError, match="zone z3 unavailable: derived root does not exist"
        ):
            atif_activity(source)

        with pytest.raises(
            ZoneUnavailableError, match="zone z4 unavailable: docs directory does not exist"
        ):
            knowledge_front_matter(source)

        # Calibration with no file records also raises ZoneUnavailableError when Z2 is unavailable
        empty_records = tmp_path / "empty_records"
        empty_records.mkdir()
        with pytest.raises(
            ZoneUnavailableError, match="zone z2 unavailable: Postgres connection refused"
        ):
            calibration_history(source, records_root=empty_records)
    finally:
        source.close()


def test_no_direct_parquet_globs_in_dashboard():
    """Assert no module under dashboard/ contains a direct Parquet glob or read_parquet."""
    dashboard_dir = Path(__file__).resolve().parents[1]
    prohibited_tokens = ("read_parquet", "job_id=*")

    for py_file in dashboard_dir.rglob("*.py"):
        # Exclude tests themselves
        if "tests" in py_file.parts:
            continue
        content = py_file.read_text(encoding="utf-8")
        for token in prohibited_tokens:
            assert token not in content, (
                f"Found prohibited direct Parquet access token {token!r} in {py_file}"
            )


def test_discovery_query_returns_status_and_claim(tmp_path):
    journal = tmp_path / "DISCOVERIES.md"
    journal.write_text(
        "# Journal\n\n## D-20260814-ABC — validated\n\n- Claim: A supported finding.\n",
        encoding="utf-8",
    )
    assert discoveries(journal) == [
        {
            "discovery_id": "D-20260814-ABC",
            "status": "validated",
            "claim": "A supported finding.",
        }
    ]


def test_postgres_attached_integration(tmp_path):
    """Optional live test when Postgres is reachable."""
    dsn = database_url_from_environment()
    source = AttachSource()
    try:
        if not source.is_zone_attached("z2"):
            pytest.skip(f"Postgres not reachable at {dsn}")
        rows = leaderboard(source)
        assert isinstance(rows, list)
    finally:
        source.close()
