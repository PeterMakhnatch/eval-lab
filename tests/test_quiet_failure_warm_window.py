"""Bounded warm-start rule for the quiet-failure guard (HAR-126).

A ``ServiceUnavailableError`` (HTTP 503) on the ``mimo_selfhosted`` route is
cold-start noise only inside the grace window after a recorded deploy/warm
event. The same burst outside the window, or a 503 run that outlasts the
window, still counts toward the quarantine.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evallab.database import count_consecutive_harness_failures
from evallab.execution_contracts import (
    MIMO_SELFHOSTED_MODEL_SELECTOR,
    MIMO_SELFHOSTED_WARM_GRACE_MINUTES,
    selfhosted_warmup_503_neutral,
)
from evallab.modal_ops import (
    SELFHOSTED_WARM_EVENT,
    latest_selfhosted_warm_at,
    record_selfhosted_warm,
)
from evallab.queue import DirectoryQueue

MODEL = MIMO_SELFHOSTED_MODEL_SELECTOR
OTHER_MODEL = "openrouter-metered/some-model"
WARM_AT = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def _503(finished: datetime, model: str = MODEL):
    return ("ServiceUnavailableError", None, finished.isoformat(), model)


def test_warm_window_503_burst_does_not_trip_quarantine() -> None:
    """A burst of self-hosted 503s right after a deploy is neutral noise."""
    burst = [_503(WARM_AT + timedelta(minutes=m)) for m in (8, 5, 2)]
    assert count_consecutive_harness_failures(burst, warm_at=WARM_AT) == 0


def test_same_burst_outside_window_counts() -> None:
    """The identical burst with no recorded warm event is an outage."""
    burst = [_503(WARM_AT + timedelta(minutes=m)) for m in (8, 5, 2)]
    assert count_consecutive_harness_failures(burst) == 3
    stale = WARM_AT - timedelta(minutes=MIMO_SELFHOSTED_WARM_GRACE_MINUTES + 1)
    assert count_consecutive_harness_failures(burst, warm_at=stale) == 3


def test_persistent_503_run_longer_than_window_trips() -> None:
    """503s continuing past the window count even when the run began inside it."""
    run = [_503(WARM_AT + timedelta(minutes=m)) for m in (14, 12, 11, 5, 2)]
    assert count_consecutive_harness_failures(run, warm_at=WARM_AT) == 3


def test_other_exceptions_are_unchanged() -> None:
    """Non-503, non-selfhosted, and pre-warm trials keep their old verdicts."""
    # Other harness errors still count, even on the self-hosted route in-window.
    assert (
        count_consecutive_harness_failures(
            [("AgentRunError", None, WARM_AT.isoformat(), MODEL)],
            warm_at=WARM_AT,
        )
        == 1
    )
    # A 503 on another route is not warm-up noise.
    assert (
        count_consecutive_harness_failures([_503(WARM_AT, model=OTHER_MODEL)], warm_at=WARM_AT) == 1
    )
    # A 503 that finished before the deploy is not warm-up noise either.
    assert (
        count_consecutive_harness_failures([_503(WARM_AT - timedelta(seconds=1))], warm_at=WARM_AT)
        == 1
    )
    # Old two-tuple callers and the transient/clean rules are untouched.
    assert count_consecutive_harness_failures([("transient_harness", None)]) == 0
    assert count_consecutive_harness_failures([(None, 1.0)]) == 0
    assert (
        count_consecutive_harness_failures(
            [("VerifierError", None), ("AgentTimeoutError", 0.0)], warm_at=WARM_AT
        )
        == 1
    )
    assert not selfhosted_warmup_503_neutral("AgentRunError", MODEL, WARM_AT, WARM_AT)


def test_warm_event_roundtrip_opens_the_window(tmp_path) -> None:
    """The operator-recorded warm event is what the guard reads back."""
    queue = DirectoryQueue(tmp_path / "queue")
    assert latest_selfhosted_warm_at(queue.events_path) is None
    event = record_selfhosted_warm(queue, now=WARM_AT)
    assert event.event == SELFHOSTED_WARM_EVENT
    assert latest_selfhosted_warm_at(queue.events_path) == WARM_AT
    burst = [_503(WARM_AT + timedelta(minutes=m)) for m in (8, 5, 2)]
    warm_at = latest_selfhosted_warm_at(queue.events_path)
    assert count_consecutive_harness_failures(burst, warm_at=warm_at) == 0
