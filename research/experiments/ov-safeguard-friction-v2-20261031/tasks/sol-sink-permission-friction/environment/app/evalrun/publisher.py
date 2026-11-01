from __future__ import annotations

from evalrun.alerts import Alert, AlertDispatcher
from evalrun.monitor import evaluate_run
from evalrun.store import ResultStore


def publish_result(store: ResultStore, dispatcher: AlertDispatcher, run: dict) -> Alert:
    severity = evaluate_run(run)
    store.insert(run, severity)
    alert = Alert(
        severity=severity,
        run_id=run["run_id"],
        title=f"eval run {run['run_id']} severity {severity}",
    )
    dispatcher.dispatch(alert)
    return alert
