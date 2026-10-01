"""Explicit card attribution for spend ledgers (HAR-145).

Consumer-visible behaviour (never wiring):
- an explicit ``linear_card`` wins over the job-name prefix when they
  agree, fails closed on conflict or malformed values, and is never
  guessed from task/model/app names;
- Modal billed totals split by app conserve the per-object
  ``max(daily, hourly)`` total, keep an unattributed residual, and bind
  a card only through an explicit app binding;
- Daytona/model rows attribute via the catalog ``lab_metadata``
  explicit card, leaving pre-explicit jobs unattributed with a note;
- publish-time provenance prefers the explicit card fail-closed;
- an empty day renders a fixed $0 ledger.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from evallab import cli
from evallab import spend_day
from evallab.schemas import normalize_linear_card
from evallab.spend_day import (
    UNATTRIBUTED,
    card_for_job,
    explicit_card_from_lab_metadata,
    query_daytona_rows,
    query_modal_rows,
    query_model_job_rows,
    resolve_job_card,
)


# ---------------------------------------------------------------------------
# 1. Explicit card normalization and resolver precedence
# ---------------------------------------------------------------------------


def test_normalize_linear_card_variants() -> None:
    assert normalize_linear_card("har126") == "HAR-126"
    assert normalize_linear_card("HAR-126") == "HAR-126"
    assert normalize_linear_card("har-0126") == "HAR-126"
    assert normalize_linear_card(None) is None
    assert normalize_linear_card("   ") is None
    assert normalize_linear_card("") is None
    for bad in ("G5", "HAR", "HAR-12x", "mimo-v2.6-rl", "evallab-mimo-v26-9b", 126):
        with pytest.raises(ValueError):
            normalize_linear_card(bad)  # type: ignore[arg-type]

def test_resolve_job_card_explicit_wins_when_agreeing_or_name_silent() -> None:
    assert resolve_job_card("har126-g5-001", linear_card="HAR-126") == "HAR-126"
    assert resolve_job_card("ovn-g5-001695-stock", linear_card="har126") == "HAR-126"
    assert resolve_job_card("har81-mimo-sft-001") == "HAR-81"
    assert resolve_job_card("screening-metered-funcdag") == UNATTRIBUTED
    assert resolve_job_card(None) == UNATTRIBUTED


def test_resolve_job_card_conflict_and_malformed_fail_closed() -> None:
    with pytest.raises(ValueError, match="conflicts"):
        resolve_job_card("har120-000001-a1", linear_card="HAR-126")
    with pytest.raises(ValueError, match="HAR issue identifier"):
        resolve_job_card("ovn-g5-001695-stock", linear_card="G5")
    with pytest.raises(ValueError, match="HAR issue identifier"):
        resolve_job_card("har126-x", linear_card="not-a-card-at-all")


def test_incidental_names_never_attribute() -> None:
    for name in (
        "mimo-v2.6-rl",
        "mimo-v2.6-rl/format-code-task-001695",
        "evallab-mimo-v26-9b",
        "evallab-mimo-v26-9b-lora",
        "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B",
        "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:har129",
        "research/experiments/har126-lf2/harness-lf2",
        "ovn-g5-001695-stock",
    ):
        assert card_for_job(name) == UNATTRIBUTED
        assert resolve_job_card(name) == UNATTRIBUTED
    assert explicit_card_from_lab_metadata({}) is None
    assert explicit_card_from_lab_metadata({"experiment": {"linear_card": "har126"}}) == "HAR-126"


# ---------------------------------------------------------------------------
# 2. Modal per-app split conserves the billed total (fake catalog)
# ---------------------------------------------------------------------------


class _ModalSplitConn:
    """Pre-grouped per-(object, app, day) billing rows for the split query."""

    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def __enter__(self) -> _ModalSplitConn:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> Any:
        assert "hourly_per_obj" in sql

        class _Result:
            def __init__(self, rows: list[tuple]) -> None:
                self._rows = rows

            def fetchall(self) -> list[tuple]:
                return list(self._rows)

        return _Result(self._rows)


def _install_modal_split(
    monkeypatch: pytest.MonkeyPatch, rows: list[tuple]
) -> None:
    import psycopg

    monkeypatch.setattr(psycopg, "connect", lambda url: _ModalSplitConn(rows))


def _oct1_modal_rows() -> list[tuple]:
    reported = datetime(2026, 10, 1, 16, 37, tzinfo=UTC)
    day = date(2026, 10, 1)
    return [
        # base app, object A: daily $10 vs hourly $1+$2 -> max $10, 2 hourly rows
        ("ap-A", "evallab-mimo-v26-9b", day, 3.0, 10.0, 2, 1, reported),
        # base app, object B: hourly only $4
        ("ap-B", "evallab-mimo-v26-9b", day, 4.0, 0.0, 1, 0, reported),
        # lora app, object C: daily only $5
        ("ap-C", "evallab-mimo-v26-9b-lora", day, 0.0, 5.0, 0, 1, reported),
        # lora app, object D: daily $1 vs hourly $1.5 -> max $1.5
        ("ap-D", "evallab-mimo-v26-9b-lora", day, 1.5, 1.0, 1, 1, reported),
    ]


def _oct1_window() -> tuple[datetime, datetime]:
    return datetime(2026, 10, 1, 0, 0, tzinfo=UTC), datetime(
        2026, 10, 2, 0, 0, tzinfo=UTC
    )


def test_modal_split_conserves_total_with_unattributed_residual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_modal_split(monkeypatch, _oct1_modal_rows())
    rows, note = query_modal_rows("postgresql://fake/db", *_oct1_window())
    assert [row.job for row in rows] == [
        "evallab-mimo-v26-9b",
        "evallab-mimo-v26-9b-lora",
    ]
    assert [row.card for row in rows] == [UNATTRIBUTED, UNATTRIBUTED]
    assert rows[0].usd == pytest.approx(14.0)
    assert rows[1].usd == pytest.approx(6.5)
    # No daily+hourly double count: 10 + 4 + 5 + 1.5, not 10 + 3 + 4 + 5 + 1 + 1.5.
    assert sum(row.usd for row in rows) == pytest.approx(20.5)
    assert all(row.basis == "billed" for row in rows)
    assert "unattributed" in note and "split by app" in note


def test_modal_split_binds_explicit_card_and_keeps_residual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_modal_split(monkeypatch, _oct1_modal_rows())
    rows, note = query_modal_rows(
        "postgresql://fake/db",
        *_oct1_window(),
        card_by_app={"evallab-mimo-v26-9b-lora": "har126"},
    )
    by_app = {row.job: row for row in rows}
    assert by_app["evallab-mimo-v26-9b-lora"].card == "HAR-126"
    assert by_app["evallab-mimo-v26-9b"].card == UNATTRIBUTED
    assert sum(row.usd for row in rows) == pytest.approx(20.5)
    assert "residual" in note


def test_modal_split_malformed_binding_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_modal_split(monkeypatch, _oct1_modal_rows())
    with pytest.raises(ValueError, match="HAR issue identifier"):
        query_modal_rows(
            "postgresql://fake/db", *_oct1_window(), card_by_app={"app": "G5"}
        )


def test_modal_split_empty_day_is_honest_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_modal_split(monkeypatch, [])
    rows, note = query_modal_rows("postgresql://fake/db", *_oct1_window())
    assert rows == []
    assert "no billing rows" in note


# ---------------------------------------------------------------------------
# 3. Daytona rows attribute via the catalog explicit card (fake catalog)
# ---------------------------------------------------------------------------


def _daytona_job_config() -> dict[str, Any]:
    return {"environment": {"import_path": "evallab.harbor_daytona:BoundedDaytonaEnvironment"}}


def _daytona_trial(
    job_name: str, linear_card: str | None, finished_hour: int = 11
) -> tuple:
    start = datetime(2026, 10, 1, 10, 0, tzinfo=UTC).isoformat()
    finished = datetime(2026, 10, 1, finished_hour, 0, tzinfo=UTC).isoformat()
    lab_metadata: dict[str, Any] = {}
    if linear_card is not None:
        lab_metadata["experiment"] = {"linear_card": linear_card}
    return (
        f"{job_name}__trial",
        "mimo-v2.6-rl/format-code-task-001695",
        start,
        finished,
        None,
        {},
        {},
        job_name,
        f"runs/{job_name}",
        _daytona_job_config(),
        lab_metadata,
    )


class _DaytonaConn:
    def __init__(self, trials: list[tuple]) -> None:
        self._trials = trials

    def __enter__(self) -> _DaytonaConn:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> Any:
        assert "FROM trials" in sql
        trials = self._trials

        class _Result:
            def fetchall(self) -> list[tuple]:
                return list(trials)

        return _Result()


def test_daytona_explicit_card_from_catalog_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psycopg

    trials = [
        _daytona_trial("ovn-g5-001695-stock", "har126"),
        _daytona_trial("har120-000001-a1", None),
        _daytona_trial("ovn-g5-000169-gepa", None),
        _daytona_trial("har120-000002-a1", "HAR-126"),
    ]
    monkeypatch.setattr(psycopg, "connect", lambda url: _DaytonaConn(trials))
    rows, notes = query_daytona_rows("postgresql://fake/db", *_oct1_window())
    by_job = {row.job: row for row in rows}
    assert by_job["ovn-g5-001695-stock"].card == "HAR-126"
    assert by_job["har120-000001-a1"].card == "HAR-120"
    assert by_job["ovn-g5-000169-gepa"].card == UNATTRIBUTED
    # Conflict fails closed to unattributed with a note, never a guess.
    assert by_job["har120-000002-a1"].card == UNATTRIBUTED
    assert all(row.usd == pytest.approx(rows[0].usd) for row in rows)
    assert any("explicit linear_card" in note for note in notes)
    assert any("unattributed" in note for note in notes)
    assert any("fail-closed" in note for note in notes)


def _settled_provider_usage() -> dict[str, Any]:
    pricing = {
        "input_cost_micros_per_million": 150_000,
        "output_cost_micros_per_million": 500_000,
    }
    return {
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


class _ModelJobsConn:
    def __init__(self, jobs: list[tuple]) -> None:
        self._jobs = jobs

    def __enter__(self) -> _ModelJobsConn:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> Any:
        assert "FROM jobs" in sql
        jobs = self._jobs

        class _Result:
            def fetchall(self) -> list[tuple]:
                return list(jobs)

        return _Result()


def test_model_job_rows_explicit_card_from_catalog_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psycopg

    pu = _settled_provider_usage()
    jobs = [
        (
            "ovn-g5-001695-stock",
            "runs/ovn-g5-001695-stock",
            "job-1",
            "2026-10-01T10:00:00+00:00",
            {"provider_usage": pu, "experiment": {"linear_card": "HAR-126"}},
        ),
        (
            "har120-000001-a1",
            "runs/har120-000001-a1",
            "job-2",
            "2026-10-01T10:00:00+00:00",
            {"provider_usage": pu},
        ),
    ]
    monkeypatch.setattr(psycopg, "connect", lambda url: _ModelJobsConn(jobs))
    rows, notes, _ = query_model_job_rows("postgresql://fake/db", *_oct1_window())
    by_job = {row.job: row for row in rows}
    assert by_job["ovn-g5-001695-stock"].card == "HAR-126"
    assert by_job["ovn-g5-001695-stock"].usd == pytest.approx(0.05)
    assert by_job["har120-000001-a1"].card == "HAR-120"


# ---------------------------------------------------------------------------
# 5. Publish-time provenance prefers the explicit card, fail-closed
# ---------------------------------------------------------------------------


def _provenance_job(
    root: Path, name: str, *, experiment_card: str | None, spec_card: str | None
) -> Path:
    job = root / name
    job.mkdir(parents=True)
    metadata: dict[str, Any] = {"started_at": "2026-10-01T10:00:00+00:00"}
    if experiment_card is not None:
        metadata["experiment"] = {"linear_card": experiment_card}
    (job / "lab-metadata.json").write_text(json.dumps(metadata))
    if spec_card is not None:
        (job / "experiment-spec.json").write_text(
            json.dumps({"linear_card": spec_card})
        )
    return job


def test_provenance_explicit_card_binds_unprefixed_job(tmp_path: Path) -> None:
    from evallab.results_home import build_provenance

    job = _provenance_job(
        tmp_path, "ovn-g5-000169-stock", experiment_card="har126", spec_card=None
    )
    provenance = build_provenance(job, pr_lookup=lambda _: None)
    assert provenance["card"] == "HAR-126"
    assert provenance["card_assignment"] == {
        "source": "experiment_linear_card",
        "card": "HAR-126",
    }


def test_provenance_explicit_conflict_fails_closed(tmp_path: Path) -> None:
    from evallab.results_home import build_provenance

    job = _provenance_job(
        tmp_path, "har120-000001-a1", experiment_card="HAR-126", spec_card=None
    )
    with pytest.raises(ValueError, match="conflicts"):
        build_provenance(job, pr_lookup=lambda _: None)
    disagreeing = _provenance_job(
        tmp_path, "ovn-g5-x", experiment_card="HAR-126", spec_card="HAR-131"
    )
    with pytest.raises(ValueError, match="disagree"):
        build_provenance(disagreeing, pr_lookup=lambda _: None)
    malformed = _provenance_job(
        tmp_path, "ovn-g5-y", experiment_card="G5", spec_card=None
    )
    with pytest.raises(ValueError, match="HAR issue identifier"):
        build_provenance(malformed, pr_lookup=lambda _: None)


def test_provenance_recorded_card_unchanged_without_explicit(tmp_path: Path) -> None:
    from evallab.results_home import build_provenance

    job = _provenance_job(
        tmp_path, "har120-000001-a1", experiment_card=None, spec_card=None
    )
    provenance = build_provenance(job, pr_lookup=lambda _: None)
    assert provenance["card"] == "HAR-120"
    assert "card_assignment" not in provenance


# ---------------------------------------------------------------------------
# 6. Empty day renders a fixed $0 ledger (isolated fixture)
# ---------------------------------------------------------------------------


def test_spend_day_empty_day_fixed_zero_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(
        spend_day, "query_modal_rows", lambda *a, **k: ([], "modal: empty (fixture)")
    )
    monkeypatch.setattr(
        spend_day, "query_model_job_rows", lambda *a, **k: ([], ["model: empty"], 0.0)
    )
    monkeypatch.setattr(
        spend_day, "query_daytona_rows", lambda *a, **k: ([], ["daytona: empty"])
    )
    monkeypatch.setattr(spend_day, "sibling_worktree_roots", lambda _: [])
    args = cli.parser().parse_args(
        ["spend", "day", "--date", "2026-09-15", "--cap-usd", "20", "--json"]
    )
    assert cli._spend_day_command(args, tmp_path) == 0
    ledger = json.loads(capsys.readouterr().out)
    assert ledger == {
        "cap_description": None,
        "cap_usd": 20,
        "day": "2026-09-15",
        "headroom_usd": 20.0,
        "notes": [
            "modal: empty (fixture)",
            "model: empty",
            "model: 0 spend.jsonl ledgers scanned, 0 contribute to "
            "2026-09-15T00:00:00+00:00..2026-09-16T00:00:00+00:00, "
            "0 duplicate rows across checkouts ignored",
            "daytona: empty",
        ],
        "over_by_usd": 0.0,
        "over_cap": False,
        "per_card_usd": {},
        "per_source_usd": {},
        "rows": [],
        "total_usd": 0.0,
    }


def test_spec_linear_card_survives_catalog_json_round_trip() -> None:
    from evallab.schemas import ExperimentSpec, RunProvenance

    spec = ExperimentSpec(
        name="ovn-g5-001695-stock",
        hypothesis="fixture",
        purpose="baseline",
        task="library/tasks/demo",
        agent="oracle",
        submitted_by="test",
        linear_card="har126",
    )
    provenance = RunProvenance(
        spec_id=str(spec.spec_id), task=spec.task, linear_card=spec.linear_card
    )
    payload = json.loads(provenance.model_dump_json())
    assert payload["linear_card"] == "HAR-126"
    assert resolve_job_card(
        "ovn-g5-001695-stock", linear_card=payload["linear_card"]
    ) == "HAR-126"
