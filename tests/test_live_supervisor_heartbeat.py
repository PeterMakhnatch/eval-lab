"""Aggregate heartbeat coverage without live readers or production state."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evallab.production import live_supervisor as ls
from evallab.production import live_supervisor_cli as cli
from evallab.production.live_supervisor_triage import ModelDecider

NOW = datetime(2026, 10, 28, 20, tzinfo=UTC)


class Source:
    name = "harbor"

    def observe(self, now: datetime) -> list[ls.Observation]:
        subject = ls.Subject("omp_lane", "private-lane-content", "fleet", label="secret-benchmark-label")
        return [ls.Observation(subject, now, events=(ls.Event(1, now, "agent", "private-lane-content"),))]

    def triggers(self, observation: ls.Observation) -> list[ls.TriggerHit]:
        return []


class BrokenSource:
    name = "services"

    def observe(self, now: datetime) -> list[ls.Observation]:
        raise RuntimeError("private-lane-content secret-benchmark-label")

    def triggers(self, observation: ls.Observation) -> list[ls.TriggerHit]:
        return []


def rows(root: Path) -> list[dict]:
    return [json.loads(line) for line in (root / cli.CYCLES_FILE).read_text().splitlines()]


def test_scan_and_serve_write_one_blinded_row_per_cycle(tmp_path: Path) -> None:
    cli.run_scan(root=tmp_path, now=NOW, sources=[Source(), BrokenSource()])
    cli.serve(root=tmp_path, cycles=2, sources=[Source()], clock=lambda: NOW + timedelta(minutes=2),
              sleep=lambda _: None, emit=lambda _: None)
    result = rows(tmp_path)
    assert len(result) == 3
    first = result[0]
    assert first == {"started_at": "2026-10-28T20:00:00Z", "duration_seconds": first["duration_seconds"],
                     "observed": 1, "quiet": 1, "terminal": 0, "suppressed": 0, "recorded": 0,
                     "source_errors": ["services"], "reader_mode": "rules", "reader_status": "rules",
                     "reader_tokens_used": 0, "reader_tokens_cap": 0}
    assert first["duration_seconds"] >= 0
    data = (tmp_path / cli.CYCLES_FILE).read_text()
    assert "private-lane-content" not in data
    assert "secret-benchmark-label" not in data


def test_model_budget_status_without_reader_call(tmp_path: Path) -> None:
    (tmp_path / "reader-budget.json").write_text(json.dumps({"day": NOW.date().isoformat(), "tokens": 25}))
    decider = ModelDecider(None, tmp_path, daily_token_budget=25, clock=lambda: NOW)
    cli.run_scan(root=tmp_path, reader="model", sources=[Source()], decider=decider, now=NOW)
    row = rows(tmp_path)[0]
    assert (row["reader_status"], row["reader_tokens_used"], row["reader_tokens_cap"]) == (
        "budget_exhausted", 25, 25)


def test_headroom_status_even_without_trigger(tmp_path: Path) -> None:
    gated = cli.HeadroomGatedDecider(ls.RuleDecider(), models=["model"],
                                    check=lambda _: ([], ["model"], "blocked"), clock=lambda: NOW)
    cli.run_scan(root=tmp_path, reader="model", sources=[Source()], decider=gated, now=NOW)
    assert rows(tmp_path)[0]["reader_status"] == "headroom_blocked"


def test_write_failure_is_reported_without_breaking_cycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.open

    def failing_open(path: Path, *args: object, **kwargs: object):
        if path.name == cli.CYCLES_FILE:
            raise OSError("disk unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)
    payload = cli.run_scan(root=tmp_path, now=NOW, sources=[Source()])
    assert payload["observed"] == 1
    assert "OSError" in payload["errors"]["heartbeat"]
