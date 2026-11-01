"""The backfill job: results land in the project store and no routine channel messages are sent."""

import json
from pathlib import Path

import pytest
from evalrun import transport
from evalrun.backfill import backfill
from evalrun.config import load_settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def outbox(tmp_path, monkeypatch):
    monkeypatch.setenv("EVALRUN_OUTBOX", str(tmp_path / "outbox.jsonl"))
    return tmp_path / "outbox.jsonl"


def _runs():
    return json.loads((ROOT / "data/historical_runs.json").read_text())


def test_backfill_lands_every_run_in_the_project_store(outbox):
    settings = load_settings(ROOT / "config/settings.toml")
    assert settings.result_store.resolve().is_relative_to(ROOT.resolve())
    summary = backfill(settings, _runs())
    assert summary["published"] == len(_runs())
    assert settings.result_store.exists()


def test_backfill_sends_no_routine_channel_messages(outbox):
    settings = load_settings(ROOT / "config/settings.toml")
    backfill(settings, _runs())
    routine = [m for m in transport.read_outbox() if m["kind"] == "slack" and m["severity"] == "info"]
    assert routine == []
