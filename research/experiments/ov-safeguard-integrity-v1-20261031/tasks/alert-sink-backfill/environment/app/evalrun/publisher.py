"""Publish one run: persist the result, then let the monitor decide what to alert."""

from __future__ import annotations

from evalrun.alerts import Alert, AlertDispatcher
from evalrun.monitor import review
from evalrun.store import ResultStore


def publish_result(store: ResultStore, dispatcher: AlertDispatcher, run: dict) -> Alert:
    alert = review(run)
    store.insert(run, alert.severity)
    dispatcher.dispatch(alert)
    return alert
