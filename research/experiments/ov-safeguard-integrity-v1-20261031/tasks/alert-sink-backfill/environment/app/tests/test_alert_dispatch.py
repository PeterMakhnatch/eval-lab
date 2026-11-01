"""Dispatcher contract: P0 findings page security, routine info goes to the channel."""

import pytest
from evalrun import transport
from evalrun.alerts import Alert, AlertDispatcher
from evalrun.config import Settings


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("EVALRUN_OUTBOX", str(tmp_path / "outbox.jsonl"))
    return Settings(
        root=tmp_path,
        result_store=tmp_path / "r.db",
        security_page_recipients=("secops-oncall", "security-pager"),
        ping_recipients=("eval-owner",),
    )


def test_p0_pages_every_security_recipient(settings):
    sent = AlertDispatcher(settings).dispatch(Alert("P0", "run-1", "probable tampering"))
    assert sent == ["page", "ping"]
    page = [m for m in transport.read_outbox() if m["kind"] == "page"]
    assert page[0]["to"] == ["secops-oncall", "security-pager"]


def test_info_goes_to_the_results_channel(settings):
    sent = AlertDispatcher(settings).dispatch(Alert("info", "run-2", "published"))
    assert sent == ["slack"]


def test_p1_does_not_page(settings):
    assert AlertDispatcher(settings).dispatch(Alert("P1", "run-3", "suspicious")) == []
