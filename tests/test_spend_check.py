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

import json
import sys
import types
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from evallab import cli
from evallab.spend_day import (
    REASON_CAP_UNVERIFIED,
    REASON_CEILING_EXCEEDED,
    REASON_STALE_MODAL,
    REASON_UNRATABLE_SPEC,
    aggregate_spend_jsonl_calls,
    check_launch,
    collect_in_flight,
    collect_spend_jsonl_rows,
    day_to_window,
    decide_launch,
    parse_launch_since,
    query_modal_rows,
    render_decision,
    summarize_window,
    window_overlap_seconds,
)

UTC_DAY = date(2026, 10, 1)
DAY_START = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


def _spec_payload(
    name: str,
    *,
    environment: str = "docker",
    est_cost_usd: float = 0.0,
    cost_limit_usd: float | None = None,
    agent: str = "oracle",
) -> str:
    from evallab.schemas import ExperimentSpec

    spec = ExperimentSpec(
        name=name,
        hypothesis="spend check fixture",
        purpose="baseline",
        task="library/tasks/demo",
        agent=agent,
        submitted_by="test",
        environment=environment,
        est_cost_usd=est_cost_usd,
        cost_limit_usd=cost_limit_usd,
    )
    return spec.model_dump_json()


def _write_queue_spec(queue_root: Path, state: str, filename: str, payload: str) -> None:
    state_dir = queue_root / state
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / filename).write_text(payload + "\n", encoding="utf-8")


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


def test_spend_jsonl_window_bounds_are_half_open() -> None:
    start = datetime(2026, 10, 1, 4, 0, tzinfo=UTC)
    end = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    calls = [
        {"ts": "2026-10-01T04:00:00Z", "cost_usd": 1.0},  # window start: in
        {"ts": "2026-10-01T11:59:59Z", "cost_usd": 2.0},  # just inside: in
        {"ts": "2026-10-01T12:00:00Z", "cost_usd": 4.0},  # window end: out
        {"ts": "2026-10-01T03:59:59Z", "cost_usd": 8.0},  # before start: out
        {"ts": "not-a-time", "cost_usd": 16.0},  # unparsable: out, never dollars
        {"ts": "2026-10-01T05:00:00Z", "cost_usd": "free"},  # non-numeric: out
    ]
    total, in_window, out_of_window = aggregate_spend_jsonl_calls(calls, start, end)
    assert total == pytest.approx(3.0)
    assert in_window == 2
    assert out_of_window == 4


def test_day_jsonl_collection_is_the_window_special_case(tmp_path: Path) -> None:
    day = date(2026, 9, 30)
    ledger_dir = tmp_path / "har111-probe"
    ledger_dir.mkdir()
    (ledger_dir / "spend.jsonl").write_text(
        json.dumps({"ts": "2026-09-30T10:00:00Z", "cost_usd": 0.5})
        + "\n"
        + json.dumps({"ts": "2026-10-01T01:00:00Z", "cost_usd": 9.0})
        + "\n",
        encoding="utf-8",
    )
    day_rows, day_notes = collect_spend_jsonl_rows(
        tmp_path, *day_to_window(day), label=day.isoformat()
    )
    window_rows, window_notes = collect_spend_jsonl_rows(
        tmp_path,
        datetime(2026, 9, 30, 0, 0, tzinfo=UTC),
        datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
        label=day.isoformat(),
    )
    assert day_rows == window_rows
    assert day_notes == window_notes
    assert len(day_rows) == 1
    assert day_rows[0].usd == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 2. Modal hourly granularity (fake catalog)
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple]:
        return list(self._rows)


class _FakeConnection:
    """Mimics Postgres filtering for the spend queries under test."""

    def __init__(
        self,
        modal_rows: list[tuple[datetime, float, datetime]],
        latest: datetime | None,
    ) -> None:
        self._modal_rows = modal_rows
        self._latest = latest

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> _FakeResult:
        if "WITH day_has_hourly" in sql:
            start, end = params[0], params[1]
            matching = [row for row in self._modal_rows if start <= row[0] < end]
            total = sum(row[1] for row in matching)
            reported = max((row[2] for row in matching), default=None)
            return _FakeResult([(total, len(matching), reported)])
        if "SELECT DISTINCT" in sql:
            start_d, end_d = params
            matching_days = {
                row[0].date()
                for row in self._modal_rows
                if start_d <= row[0].date() <= end_d and (len(row) < 4 or row[3] == "h")
            }
            return _FakeResult([(d,) for d in sorted(matching_days)])
        if "max(interval_start)" in sql and "coalesce" not in sql:
            return _FakeResult([(self._latest,)])
        if "FROM modal_billing_rows" in sql:
            start, end = params[0], params[1]
            matching = [row for row in self._modal_rows if start <= row[0] < end]
            total = sum(row[1] for row in matching)
            reported = max((row[2] for row in matching), default=None)
            return _FakeResult([(total, len(matching), reported)])
        if "FROM jobs" in sql:
            return _FakeResult([])
        if "FROM trials" in sql:
            return _FakeResult([])
        raise AssertionError(f"unexpected SQL in spend test double: {sql!r}")


def _install_fake_catalog(
    monkeypatch: pytest.MonkeyPatch,
    modal_rows: list[tuple[datetime, float, datetime]],
    latest: datetime | None,
) -> None:
    fake = types.ModuleType("psycopg")
    fake.connect = lambda database_url: _FakeConnection(modal_rows, latest)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", fake)


def test_modal_window_counts_hour_starting_in_window_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reported = datetime(2026, 10, 1, 6, 30, tzinfo=UTC)
    _install_fake_catalog(
        monkeypatch,
        [
            (datetime(2026, 10, 1, 0, 0, tzinfo=UTC), 1.5, reported),  # hour starts before: out
            (datetime(2026, 10, 1, 5, 0, tzinfo=UTC), 2.5, reported),  # hour starts in: whole
            (datetime(2026, 10, 1, 6, 0, tzinfo=UTC), 9.0, reported),  # hour starts at end: out
        ],
        latest=datetime(2026, 10, 1, 6, 0, tzinfo=UTC),
    )
    row, _note = query_modal_rows(
        "postgresql://fake/db",
        datetime(2026, 10, 1, 4, 0, tzinfo=UTC),
        datetime(2026, 10, 1, 6, 0, tzinfo=UTC),
    )
    assert row is not None
    # The 05:00 hourly row counts whole even though its hour runs past 06:00.
    assert row.usd == pytest.approx(2.5)
    assert row.basis == "billed"


# ---------------------------------------------------------------------------
# 3. Modal staleness & refresh paths (HAR-129 Part A fixes)
# ---------------------------------------------------------------------------


def test_check_launch_stale_modal_refreshes_and_uses_fresh_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Stale stored rows trigger a refresh; fresh rows are then included in settled spend."""
    initial_rows = [
        (datetime(2026, 10, 1, 0, 0, tzinfo=UTC), 1.0, datetime(2026, 10, 1, 0, 30, tzinfo=UTC))
    ]
    conn = _FakeConnection(list(initial_rows), latest=datetime(2026, 10, 1, 0, 0, tzinfo=UTC))
    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.connect = lambda url: conn
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

    refresher_called = []

    def _mock_refresh(start: date, end: date) -> int:
        refresher_called.append((start, end))
        # The refresher fetches new rows and updates the catalog
        new_row = (
            datetime(2026, 10, 1, 4, 0, tzinfo=UTC),
            0.9142,
            datetime(2026, 10, 1, 5, 10, tzinfo=UTC),
        )
        conn._modal_rows.append(new_row)
        conn._latest = datetime(2026, 10, 1, 4, 0, tzinfo=UTC)
        return 1

    decision = check_launch(
        repo_root=tmp_path,
        queue_root=tmp_path / "queue",
        database_url="postgresql://fake/db",
        window_start=datetime(2026, 10, 1, 4, 0, tzinfo=UTC),
        window_end=datetime(2026, 10, 1, 5, 30, tzinfo=UTC),
        candidate_usd=1.0,
        cap_usd=30.0,
        now=datetime(2026, 10, 1, 5, 30, tzinfo=UTC),  # 5.5h after 00:00 -> stale
        modal_refresher=_mock_refresh,
    )
    assert len(refresher_called) == 1
    assert refresher_called[0] == (date(2026, 10, 1), date(2026, 10, 2))
    assert decision.allowed is True
    # The fresh $0.9142 row from 04:00 is included in settled spend
    assert decision.settled_usd == pytest.approx(0.9142)
    assert any("reporting lag" in note for note in decision.notes)


def test_check_launch_stale_modal_fails_closed_when_refresh_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When Modal rows are stale and refresh fails, launch is refused (fail closed, exit 2)."""
    conn = _FakeConnection([], latest=datetime(2026, 10, 1, 0, 0, tzinfo=UTC))
    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.connect = lambda url: conn
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

    def _failing_refresh(start: date, end: date) -> int:
        raise RuntimeError("modal credentials missing")

    decision = check_launch(
        repo_root=tmp_path,
        queue_root=tmp_path / "queue",
        database_url="postgresql://fake/db",
        window_start=datetime(2026, 10, 1, 4, 0, tzinfo=UTC),
        window_end=datetime(2026, 10, 1, 5, 30, tzinfo=UTC),
        candidate_usd=1.0,
        cap_usd=30.0,
        now=datetime(2026, 10, 1, 5, 30, tzinfo=UTC),
        modal_refresher=_failing_refresh,
    )
    assert decision.allowed is False
    assert decision.reason_code == REASON_STALE_MODAL
    # Refusal message names the stale day/hour
    assert "2026-10-01" in decision.notes[0]
    assert any("reporting lag" in note for note in decision.notes)


def test_check_launch_allow_stale_modal_downgrades_to_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--allow-stale-modal permits launch despite stale rows with an explicit printed warning."""
    conn = _FakeConnection([], latest=datetime(2026, 10, 1, 0, 0, tzinfo=UTC))
    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.connect = lambda url: conn
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

    decision = check_launch(
        repo_root=tmp_path,
        queue_root=tmp_path / "queue",
        database_url="postgresql://fake/db",
        window_start=datetime(2026, 10, 1, 4, 0, tzinfo=UTC),
        window_end=datetime(2026, 10, 1, 5, 30, tzinfo=UTC),
        candidate_usd=1.0,
        cap_usd=30.0,
        now=datetime(2026, 10, 1, 5, 30, tzinfo=UTC),
        allow_stale_modal=True,
        modal_refresher=lambda s, e: 0,
    )
    assert decision.allowed is True
    assert decision.reason_code is None
    assert any("allowed despite staleness" in note for note in decision.notes)
    assert any("reporting lag" in note for note in decision.notes)


# ---------------------------------------------------------------------------
# 4. In-flight reservation
# ---------------------------------------------------------------------------


def test_in_flight_reserves_max_of_cost_limit_and_estimate(tmp_path: Path) -> None:
    queue_root = tmp_path / "queue"
    _write_queue_spec(
        queue_root,
        "running",
        "mini-01SPEC1.json",
        _spec_payload(
            "har129-probe-a",
            environment="daytona",
            est_cost_usd=1.0,
            cost_limit_usd=2.5,
            agent="mini-swe-agent",
        ),
    )
    _write_queue_spec(
        queue_root,
        "approved",
        "mini-01SPEC2.json",
        _spec_payload(
            "har129-probe-b", environment="daytona", est_cost_usd=0.75, agent="mini-swe-agent"
        ),
    )
    _write_queue_spec(queue_root, "approved", "nop-01SPEC3.json", _spec_payload("har129-nop-c"))
    items, unratable, _notes = collect_in_flight(queue_root)
    assert unratable == []
    assert len(items) == 3
    by_name = {item.name: item for item in items}
    assert by_name["har129-probe-a"].reservation_usd == pytest.approx(2.5)
    assert by_name["har129-probe-b"].reservation_usd == pytest.approx(0.75)
    assert by_name["har129-nop-c"].reservation_usd == pytest.approx(0.0)
    assert sum(item.reservation_usd for item in items) == pytest.approx(3.25)


def test_in_flight_missing_queue_directory_reserves_nothing(tmp_path: Path) -> None:
    items, unratable, notes = collect_in_flight(tmp_path / "no-such-queue")
    assert items == []
    assert unratable == []
    assert any("no queue directory" in note for note in notes)


def test_cloud_spec_without_estimate_is_unratable(tmp_path: Path) -> None:
    queue_root = tmp_path / "queue"
    _write_queue_spec(
        queue_root,
        "approved",
        "mini-01SPEC9.json",
        _spec_payload(
            "har129-nostimate", environment="daytona", est_cost_usd=0.0, agent="mini-swe-agent"
        ),
    )
    _write_queue_spec(queue_root, "approved", "nop-01SPEC8.json", _spec_payload("har129-local-nop"))
    _items, unratable, _notes = collect_in_flight(queue_root)
    assert [spec.name for spec in unratable] == ["har129-nostimate"]
    assert "est_cost_usd" in unratable[0].reason


# ---------------------------------------------------------------------------
# 5. Commit-vs-cap verdict (pure arithmetic)
# ---------------------------------------------------------------------------


def test_refusal_is_strictly_above_cap_equal_allows() -> None:
    window = (DAY_START, DAY_START + timedelta(days=1))
    allowed = decide_launch(
        window_start=window[0],
        window_end=window[1],
        settled_usd=18.0,
        in_flight_usd=1.0,
        in_flight_count=1,
        candidate_usd=1.0,
        cap_usd=20.0,
    )
    assert allowed.allowed is True
    assert allowed.reason_code is None
    assert allowed.committed_usd == pytest.approx(20.0)
    assert allowed.headroom_usd == pytest.approx(0.0)

    refused = decide_launch(
        window_start=window[0],
        window_end=window[1],
        settled_usd=18.0,
        in_flight_usd=1.0,
        in_flight_count=1,
        candidate_usd=1.01,
        cap_usd=20.0,
    )
    assert refused.allowed is False
    assert refused.reason_code == REASON_CEILING_EXCEEDED
    assert refused.headroom_usd == pytest.approx(-0.01)


def test_negative_candidate_is_rejected() -> None:
    with pytest.raises(ValueError, match="candidate_usd"):
        decide_launch(
            window_start=DAY_START,
            window_end=DAY_START + timedelta(hours=1),
            settled_usd=0.0,
            candidate_usd=-1.0,
        )


# ---------------------------------------------------------------------------
# 6. Fail-closed orchestration
# ---------------------------------------------------------------------------

REFUSED_PORT_URL = "postgresql://127.0.0.1:1/evallab"


def test_unreachable_catalog_is_unverified_never_zero(tmp_path: Path) -> None:
    decision = check_launch(
        repo_root=tmp_path,
        queue_root=tmp_path / "no-such-queue",
        database_url=REFUSED_PORT_URL,
        window_start=datetime(2026, 10, 1, 4, 0, tzinfo=UTC),
        window_end=datetime(2026, 10, 1, 6, 0, tzinfo=UTC),
        candidate_usd=1.0,
        cap_usd=30.0,
    )
    assert decision.allowed is False
    assert decision.reason_code == REASON_CAP_UNVERIFIED
    assert any("unknown, never $0" in note for note in decision.notes)


def test_unratable_spec_fails_closed_with_ledger_shown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    queue_root = tmp_path / "queue"
    _write_queue_spec(
        queue_root,
        "running",
        "mini-01SPEC7.json",
        _spec_payload(
            "har129-nostimate", environment="daytona", est_cost_usd=0.0, agent="mini-swe-agent"
        ),
    )
    monkeypatch.setattr(
        "evallab.spend_day.query_modal_latest_interval",
        lambda url: datetime(2026, 10, 1, 5, 0, tzinfo=UTC),
    )
    monkeypatch.setattr(
        "evallab.spend_day.query_modal_dates_with_hourly_coverage",
        lambda url, s, e: {date(2026, 10, 1)},
    )
    monkeypatch.setattr(
        "evallab.spend_day.build_window_ledger",
        lambda *a, **k: summarize_window(
            datetime(2026, 10, 1, 4, 0, tzinfo=UTC),
            datetime(2026, 10, 1, 6, 0, tzinfo=UTC),
            [],
            cap_usd=30.0,
        ),
    )
    decision = check_launch(
        repo_root=tmp_path,
        queue_root=queue_root,
        database_url=REFUSED_PORT_URL,
        window_start=datetime(2026, 10, 1, 4, 0, tzinfo=UTC),
        window_end=datetime(2026, 10, 1, 6, 0, tzinfo=UTC),
        candidate_usd=1.0,
        cap_usd=30.0,
        now=datetime(2026, 10, 1, 5, 30, tzinfo=UTC),
    )
    assert decision.allowed is False
    assert decision.reason_code == REASON_UNRATABLE_SPEC
    assert [spec.name for spec in decision.unratable] == ["har129-nostimate"]


# ---------------------------------------------------------------------------
# 7. CLI: parser + exit codes 0 / 3 / 2
# ---------------------------------------------------------------------------


def test_spend_check_cli_parser_defaults() -> None:
    args = cli.parser().parse_args(["spend", "check", "--candidate-usd", "6"])
    assert args.spend_command == "check"
    assert args.candidate_usd == 6.0
    assert args.cap_usd is None
    assert args.since is None
    assert args.queue_root is None
    assert args.allow_stale_modal is False
    assert args.json is False


def test_parse_launch_since_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="--since"):
        parse_launch_since("tomorrow-ish")
    assert parse_launch_since("2026-10-01T04:00:00Z") == datetime(2026, 10, 1, 4, 0, tzinfo=UTC)


def _run_check(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str]) -> int:
    monkeypatch.setattr(cli, "instrument_openinference", lambda: None)
    return cli.run_cli(argv, workspace=tmp_path)


def test_cli_unreachable_catalog_exits_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    rc = _run_check(
        monkeypatch,
        tmp_path,
        [
            "spend",
            "check",
            "--candidate-usd",
            "1",
            "--cap-usd",
            "30",
            "--since",
            "2026-10-01T04:00:00Z",
            "--queue-root",
            str(tmp_path / "no-such-queue"),
            "--database-url",
            REFUSED_PORT_URL,
        ],
    )
    assert rc == 2
    assert REASON_CAP_UNVERIFIED in capsys.readouterr().out


def test_cli_rejects_bad_arguments(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert _run_check(monkeypatch, tmp_path, ["spend", "check", "--candidate-usd", "-1"]) == 2
    assert (
        _run_check(
            monkeypatch,
            tmp_path,
            ["spend", "check", "--candidate-usd", "1", "--since", "not-a-time"],
        )
        == 2
    )


def test_cli_allowed_refused_and_unratable_exits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    queue_root = tmp_path / "queue"
    _write_queue_spec(queue_root, "approved", "nop-01SPEC5.json", _spec_payload("har129-local-nop"))

    monkeypatch.setattr(
        "evallab.spend_day.query_modal_latest_interval",
        lambda url: datetime(2026, 10, 1, 5, 0, tzinfo=UTC),
    )
    monkeypatch.setattr(
        "evallab.spend_day.query_modal_dates_with_hourly_coverage",
        lambda url, s, e: {date(2026, 10, 1)},
    )

    def _settled_two_dollars(*args: object, **kwargs: object):
        from evallab.spend_day import SpendRow

        return summarize_window(
            datetime(2026, 10, 1, 4, 0, tzinfo=UTC),
            datetime(2026, 10, 1, 6, 0, tzinfo=UTC),
            [
                SpendRow(
                    source="modal",
                    card="unattributed",
                    job="modal-account",
                    usd=2.0,
                    basis="billed",
                    evidence="double",
                )
            ],
            cap_usd=30.0,
            notes=["double: settled $2.00"],
        )

    monkeypatch.setattr("evallab.spend_day.build_window_ledger", _settled_two_dollars)

    def _check(candidate: str, *extra: str) -> int:
        return _run_check(
            monkeypatch,
            tmp_path,
            [
                "spend",
                "check",
                "--candidate-usd",
                candidate,
                "--cap-usd",
                "30",
                "--since",
                "2026-10-01T04:00:00Z",
                "--queue-root",
                str(queue_root),
                "--database-url",
                REFUSED_PORT_URL,
                *extra,
            ],
        )

    assert _check("1", "--allow-stale-modal") == 0
    out = capsys.readouterr().out
    assert "ALLOWED" in out
    assert "billed $2.0000" in out

    # Settled $2 + candidate $29 = $31 > $30 refuses with exit 3.
    assert _check("29", "--allow-stale-modal") == 3
    assert REASON_CEILING_EXCEEDED in capsys.readouterr().out

    # A queued cloud spec with no estimate fails closed with exit 2.
    _write_queue_spec(
        queue_root,
        "running",
        "mini-01SPEC6.json",
        _spec_payload(
            "har129-nostimate", environment="daytona", est_cost_usd=0.0, agent="mini-swe-agent"
        ),
    )
    assert _check("1", "--allow-stale-modal") == 2
    assert REASON_UNRATABLE_SPEC in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 6. Regression tests for PR #623 review findings
# ---------------------------------------------------------------------------


def test_defect1_reject_nonfinite_candidate_and_cap(tmp_path: Path) -> None:
    """Defect 1: Non-finite candidate and cap amounts must be rejected (fail closed)."""
    # 1. decide_launch raises ValueError for NaN / inf
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="candidate_usd"):
            decide_launch(
                window_start=DAY_START,
                window_end=DAY_START + timedelta(hours=1),
                settled_usd=1.0,
                candidate_usd=bad,
                cap_usd=20.0,
            )
        with pytest.raises(ValueError, match="cap_usd"):
            decide_launch(
                window_start=DAY_START,
                window_end=DAY_START + timedelta(hours=1),
                settled_usd=1.0,
                candidate_usd=1.0,
                cap_usd=bad,
            )

    # 2. check_launch returns unverified decision for NaN / inf
    for bad in (float("nan"), float("inf")):
        d_cand = check_launch(
            repo_root=tmp_path,
            queue_root=tmp_path / "queue",
            database_url=REFUSED_PORT_URL,
            window_start=DAY_START,
            window_end=DAY_START + timedelta(hours=1),
            candidate_usd=bad,
            cap_usd=20.0,
        )
        assert not d_cand.allowed
        assert d_cand.reason_code == REASON_CAP_UNVERIFIED

        d_cap = check_launch(
            repo_root=tmp_path,
            queue_root=tmp_path / "queue",
            database_url=REFUSED_PORT_URL,
            window_start=DAY_START,
            window_end=DAY_START + timedelta(hours=1),
            candidate_usd=1.0,
            cap_usd=bad,
        )
        assert not d_cap.allowed
        assert d_cap.reason_code == REASON_CAP_UNVERIFIED

    # 3. CLI rejects NaN and exits 2
    rc_cand = cli.run_cli(
        [
            "spend",
            "check",
            "--candidate-usd",
            "nan",
            "--cap-usd",
            "30",
            "--since",
            "2026-10-01T04:00:00Z",
        ],
        workspace=tmp_path,
    )
    assert rc_cand == 2

    rc_cap = cli.run_cli(
        [
            "spend",
            "check",
            "--candidate-usd",
            "1",
            "--cap-usd",
            "nan",
            "--since",
            "2026-10-01T04:00:00Z",
        ],
        workspace=tmp_path,
    )
    assert rc_cap == 2


def test_defect2_in_flight_concurrent_approved_to_running_transition_deduped(
    tmp_path: Path,
) -> None:
    """Defect 2: Spec transitioning approved->running is deduplicated and never dropped."""
    queue_root = tmp_path / "queue"
    # Write the identical spec to both approved and running (simulating concurrent transition)
    _write_queue_spec(
        queue_root,
        "approved",
        "spec-concurrent.json",
        _spec_payload("concurrent-job", cost_limit_usd=25.0, est_cost_usd=10.0),
    )
    _write_queue_spec(
        queue_root,
        "running",
        "spec-concurrent.json",
        _spec_payload("concurrent-job", cost_limit_usd=25.0, est_cost_usd=10.0),
    )
    items, unratable, _ = collect_in_flight(queue_root)
    # Must be deduplicated by spec_id so reservation is not doubled
    assert len(items) == 1
    assert items[0].state == "running"
    assert items[0].reservation_usd == pytest.approx(25.0)


def test_defect3_negative_utc_offset_in_fallback_finish_time_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect 3: Negative UTC offsets like -07:00 must be preserved, not overwritten with host tz."""
    from evallab.spend_day import query_model_job_rows

    # Job finished at 00:30:00-07:00, which is 07:30:00 UTC
    fake_rows = [
        ("job-neg-offset", "runs/neg-offset", 1, "2026-10-01T00:30:00-07:00", {}),
    ]

    class _JobsConn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, sql: str, params: tuple = ()):
            return _FakeResult(fake_rows)

    fake = types.ModuleType("psycopg")
    fake.connect = lambda url: _JobsConn()
    monkeypatch.setitem(sys.modules, "psycopg", fake)

    # 1. Window [07:00, 08:00) UTC must find the job
    rows_correct, _, _ = query_model_job_rows(
        "postgresql://fake/db",
        datetime(2026, 10, 1, 7, 0, tzinfo=UTC),
        datetime(2026, 10, 1, 8, 0, tzinfo=UTC),
    )
    # The job had empty provider usage so no settled dollars, but it was matched in window
    # Let's verify by adding a settled provider usage block
    pricing = {"input_cost_micros_per_million": 150_000, "output_cost_micros_per_million": 500_000}
    pu = {
        "schema_version": 2,
        "pricing": pricing,
        "totals": {
            "requests": 1,
            "input_tokens": 1000,
            "output_tokens": 500,
            "total_tokens": 1500,
            "cost_micros": 50000,
        },
        "attempted": {
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_micros": 0,
        },
        "unresolved_requests": 0,
        "calls": [
            {
                "state": "reconciled",
                "call_id": 1,
                "input_tokens": 1000,
                "output_tokens": 500,
                "cost_micros": 50000,
            }
        ],
    }
    fake_rows[0] = (
        "job-neg-offset",
        "runs/neg-offset",
        1,
        "2026-10-01T00:30:00-07:00",
        {"provider_usage": pu},
    )

    rows_correct, _, _ = query_model_job_rows(
        "postgresql://fake/db",
        datetime(2026, 10, 1, 7, 0, tzinfo=UTC),
        datetime(2026, 10, 1, 8, 0, tzinfo=UTC),
    )
    assert len(rows_correct) == 1
    assert rows_correct[0].usd == pytest.approx(0.05)

    # 2. Window [04:00, 05:00) UTC (where America/New_York EDT = UTC-4 would have erroneously landed) must find 0 jobs
    rows_wrong, _, _ = query_model_job_rows(
        "postgresql://fake/db",
        datetime(2026, 10, 1, 4, 0, tzinfo=UTC),
        datetime(2026, 10, 1, 5, 0, tzinfo=UTC),
    )
    assert len(rows_wrong) == 0


def test_defect4_unresolved_finished_job_charges_reserved_or_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Defect 4: Finished jobs with unresolved provider calls must have their charges reserved."""
    from evallab.ledger import split_calls

    pricing = {"input_cost_micros_per_million": 100_000, "output_cost_micros_per_million": 100_000}
    calls = [
        {
            "state": "reconciled",
            "call_id": 1,
            "input_tokens": 10_000,
            "output_tokens": 0,
            "cost_micros": 1_000_000,
        },
        {
            "state": "reserved",
            "call_id": 2,
            "reserved_input_tokens": 250_000,
            "reserved_output_tokens": 0,
            "reserved_cost_micros": 25_000_000,
        },
    ]
    recomputed = split_calls(calls)
    pu = {
        "schema_version": 2,
        "pricing": pricing,
        "totals": recomputed["used"],
        "attempted": recomputed["attempted"],
        "unresolved_requests": recomputed["unresolved_requests"],
        "calls": calls,
    }

    class _FakeUnresolvedConn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, sql: str, params: tuple = ()):
            if "WITH day_has_hourly" in sql:
                return _FakeResult([(0.0, 0, None)])
            if "SELECT DISTINCT" in sql:
                return _FakeResult([(date(2026, 10, 1),)])
            if "max(interval_start)" in sql:
                return _FakeResult([(datetime(2026, 10, 1, 5, 0, tzinfo=UTC),)])
            if "FROM jobs" in sql:
                return _FakeResult(
                    [
                        (
                            "job-finished-unresolved",
                            "runs/unresolved",
                            1,
                            "2026-10-01T04:30:00Z",
                            {"provider_usage": pu},
                        )
                    ]
                )
            if "FROM trials" in sql:
                return _FakeResult([])
            raise AssertionError(f"unexpected SQL: {sql!r}")

    fake = types.ModuleType("psycopg")
    fake.connect = lambda url: _FakeUnresolvedConn()
    monkeypatch.setitem(sys.modules, "psycopg", fake)

    # Candidate $5, Cap $20. Settled $1 + Unresolved $24 + Candidate $5 = $30 > $20 -> REFUSED!
    decision = check_launch(
        repo_root=tmp_path,
        queue_root=tmp_path / "queue",
        database_url="postgresql://fake/db",
        window_start=datetime(2026, 10, 1, 4, 0, tzinfo=UTC),
        window_end=datetime(2026, 10, 1, 6, 0, tzinfo=UTC),
        candidate_usd=5.0,
        cap_usd=20.0,
    )
    assert not decision.allowed
    assert decision.reason_code == REASON_CEILING_EXCEEDED
    assert decision.settled_usd == pytest.approx(1.0)
    assert decision.unresolved_jobs_usd == pytest.approx(24.0)
    assert decision.committed_usd == pytest.approx(30.0)

    rendered = render_decision(decision)
    assert "unresolved-jobs: $24.0000" in rendered
    assert "committed (settled + in-flight + unresolved-jobs + candidate): $30.0000" in rendered

    # Also test: if unresolved provider calls have NO pricing, launch check fails closed (exit 2)
    pu_unratable = dict(pu)
    pu_unratable["pricing"] = None
    del pu_unratable["calls"]  # force unreadable pricing

    class _FakeUnratableConn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, sql: str, params: tuple = ()):
            if "WITH day_has_hourly" in sql:
                return _FakeResult([(0.0, 0, None)])
            if "SELECT DISTINCT" in sql:
                return _FakeResult([(date(2026, 10, 1),)])
            if "max(interval_start)" in sql:
                return _FakeResult([(datetime(2026, 10, 1, 5, 0, tzinfo=UTC),)])
            if "FROM jobs" in sql:
                return _FakeResult(
                    [
                        (
                            "job-finished-unratable",
                            "runs/unratable",
                            1,
                            "2026-10-01T04:30:00Z",
                            {"provider_usage": pu_unratable},
                        )
                    ]
                )
            if "FROM trials" in sql:
                return _FakeResult([])
            raise AssertionError(f"unexpected SQL: {sql!r}")

    fake.connect = lambda url: _FakeUnratableConn()
    d_unratable = check_launch(
        repo_root=tmp_path,
        queue_root=tmp_path / "queue",
        database_url="postgresql://fake/db",
        window_start=datetime(2026, 10, 1, 4, 0, tzinfo=UTC),
        window_end=datetime(2026, 10, 1, 6, 0, tzinfo=UTC),
        candidate_usd=1.0,
        cap_usd=20.0,
    )
    assert not d_unratable.allowed
    assert d_unratable.reason_code == REASON_CAP_UNVERIFIED


def test_defect5_modal_coverage_required_for_intraday_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Defect 5: Intra-day window on date with only daily rows requires hourly rows or fails closed."""

    # Catalog has only daily row at 00:00 ($25.00)
    class _DailyOnlyConn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, sql: str, params: tuple = ()):
            if "WITH day_has_hourly" in sql:
                return _FakeResult([(25.0, 1, datetime(2026, 10, 1, 0, 0, tzinfo=UTC))])
            if "SELECT DISTINCT" in sql:
                # No hourly rows exist
                return _FakeResult([])
            if "max(interval_start)" in sql:
                return _FakeResult([(datetime(2026, 10, 1, 0, 0, tzinfo=UTC),)])
            if "FROM jobs" in sql:
                return _FakeResult([])
            if "FROM trials" in sql:
                return _FakeResult([])
            raise AssertionError(f"unexpected SQL: {sql!r}")

    fake = types.ModuleType("psycopg")
    fake.connect = lambda url: _DailyOnlyConn()
    monkeypatch.setitem(sys.modules, "psycopg", fake)

    # Window is intra-day slice [01:00, 03:00). Without hourly rows, must fail closed with REASON_STALE_MODAL
    refused = check_launch(
        repo_root=tmp_path,
        queue_root=tmp_path / "queue",
        database_url="postgresql://fake/db",
        window_start=datetime(2026, 10, 1, 1, 0, tzinfo=UTC),
        window_end=datetime(2026, 10, 1, 3, 0, tzinfo=UTC),
        candidate_usd=5.0,
        cap_usd=20.0,
        allow_stale_modal=False,
    )
    assert not refused.allowed
    assert refused.reason_code == REASON_STALE_MODAL
    assert any("intra-day slice" in n for n in refused.notes)

    # With allow_stale_modal=True, the daily row's spend is allowed with warning
    allowed_override = check_launch(
        repo_root=tmp_path,
        queue_root=tmp_path / "queue",
        database_url="postgresql://fake/db",
        window_start=datetime(2026, 10, 1, 1, 0, tzinfo=UTC),
        window_end=datetime(2026, 10, 1, 3, 0, tzinfo=UTC),
        candidate_usd=1.0,
        cap_usd=30.0,
        allow_stale_modal=True,
    )
    # Settled $25 + candidate $1 = $26 <= $30 -> allowed with warning
    assert allowed_override.allowed
    assert allowed_override.settled_usd == pytest.approx(25.0)


def test_defect6_modal_nonoverlapping_resolution_query_and_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect 6: Mixed resolution coexistence must not double-count, and storage must be safe in both directions."""
    import psycopg

    from evallab.modal_billing import BillingRow, store_billing_rows

    executed_sqls: list[tuple[str, tuple]] = []

    class _CaptureConn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, sql: str, params: tuple = ()):
            executed_sqls.append((sql.strip(), params))
            return _FakeResult([])

    monkeypatch.setattr(psycopg, "connect", lambda url: _CaptureConn())

    # 1. Storing hourly rows must delete daily rows for that day
    row_h = BillingRow(
        object_id="app-1",
        description="hourly app",
        environment="test",
        interval_start=datetime(2026, 10, 1, 4, 0, tzinfo=UTC),
        resource="gpu",
        cost_usd=1.5,
    )
    store_billing_rows("postgresql://fake/db", [row_h], resolution="h")
    assert any(
        "DELETE FROM modal_billing_rows" in s and "resolution = %s" in s and p[0] == "d"
        for s, p in executed_sqls
    )

    executed_sqls.clear()
    # 2. Storing daily rows must delete hourly rows for that day (bidirectional safety)
    row_d = BillingRow(
        object_id="app-1",
        description="daily app",
        environment="test",
        interval_start=datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
        resource="gpu",
        cost_usd=2.5,
    )
    store_billing_rows("postgresql://fake/db", [row_d], resolution="d")
    assert any(
        "DELETE FROM modal_billing_rows" in s and "resolution = %s" in s and p[0] == "h"
        for s, p in executed_sqls
    )
