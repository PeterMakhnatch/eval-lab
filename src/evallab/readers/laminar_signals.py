"""Laminar Signals as a trace reader: one ``evallab.reader_verdict/v1`` per trial.

Signals run inside Laminar Cloud on every finished trace (realtime trigger), so
this reader only collects: for each trial it resolves the Cloud trace (native SDK
trace from ``laminar-trace.json``, else the projected watch trace), reads the
Signal runs and events, and records each kept Signal as a check:

- ``true``: the Signal fired an event on the trace;
- ``false``: it completed a run on the trace without firing;
- ``null``: it has not run there (yet).

``infra_not_model`` is not read: it found 0 of 5 labelled infra failures
(HAR-166), so infra stays on Eval Lab's deterministic rules.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from evallab.laminar import LAMINAR_ENDPOINT, _json_call, _sql, _trial_cloud_trace_uuid

READER = "laminar_signals"
SCHEMA = "evallab.reader_verdict/v1"
#: Signal name -> check name in the verdict.
CHECKS = {
    "copied_upstream_fix": "copied",
    "stuck_loop": "stuck_loop",
    "false_completion_claim": "false_completion",
}
TRACE_URL = "https://lmnr.ai/project/{project}/traces/{trace}"


def trace_url(project_id: str, trace_id: str) -> str:
    return TRACE_URL.format(project=project_id, trace=trace_id)


def collect(
    trial_dirs: Iterable[Path],
    *,
    api_key: str,
    endpoint: str = LAMINAR_ENDPOINT,
    opener: Callable[..., Any] = urlopen,
) -> list[dict[str, Any]]:
    """Verdicts for every trial with a resolvable Cloud trace."""
    call = {"api_key": api_key, "endpoint": endpoint, "opener": opener}
    project = (_json_call("GET", "/v1/project", **call) or {}).get("projectId", "")
    signals = {
        s["id"]: s
        for s in (_json_call("GET", "/v1/signals", **call) or {}).get("signals", [])
        if s["name"] in CHECKS
    }
    trials = {}
    for trial_dir in trial_dirs:
        trace = _trial_cloud_trace_uuid(Path(trial_dir))
        if trace is not None:
            trials[trace] = Path(trial_dir)
    if not trials or not signals:
        return []
    params = {"ids": list(trials), "signals": list(signals)}
    fired: dict[tuple[str, str], Any] = {}
    for row in _sql(
        "SELECT trace_id, signal_id, payload FROM signal_events"
        " WHERE trace_id IN {ids:Array(UUID)} AND signal_id IN {signals:Array(UUID)}",
        params,
        **call,
    ):
        fired[(str(row["trace_id"]), str(row["signal_id"]))] = row.get("payload")
    runs: dict[tuple[str, str], dict[str, Any]] = {}
    for row in _sql(
        "SELECT trace_id, signal_id, input_tokens, output_tokens FROM signal_runs"
        " WHERE trace_id IN {ids:Array(UUID)} AND signal_id IN {signals:Array(UUID)}"
        " AND status = 'COMPLETED'",
        params,
        **call,
    ):
        runs[(str(row["trace_id"]), str(row["signal_id"]))] = row

    now = datetime.now(UTC).isoformat()
    verdicts = []
    for trace, trial_dir in trials.items():
        checks: dict[str, bool | None] = {}
        explanations: dict[str, str] = {}
        tokens = {"input": 0, "output": 0}
        for signal_id, signal in signals.items():
            check = CHECKS[signal["name"]]
            key = (trace, signal_id)
            if key in fired:
                checks[check] = True
                explanations[check] = _payload_text(fired[key])[:300]
            elif key in runs:
                checks[check] = False
            else:
                checks[check] = None
            if key in runs:
                tokens["input"] += int(runs[key].get("input_tokens") or 0)
                tokens["output"] += int(runs[key].get("output_tokens") or 0)
        models = sorted({str(s.get("model")) for s in signals.values() if s.get("model")})
        verdicts.append(
            {
                "schema": SCHEMA,
                "reader": READER,
                "trial": trial_dir.name,
                "job": trial_dir.parent.name,
                "checks": checks,
                "explanations": explanations,
                "model": ",".join(models) or None,
                "tokens": tokens,
                "cost_usd": None,
                "at": now,
                "raw": None,
                "trace_id": trace,
                "trace_url": trace_url(project, trace) if project else None,
            }
        )
    return verdicts


def _payload_text(payload: Any) -> str:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return payload
    return json.dumps(payload, sort_keys=True) if not isinstance(payload, str) else payload


def write(verdicts: Iterable[dict[str, Any]], store: Path) -> int:
    """Write each verdict to ``<store>/<job>/<trial>/laminar_signals.json``; returns the count."""
    count = 0
    for verdict in verdicts:
        path = Path(store) / verdict["job"] / verdict["trial"] / f"{READER}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(verdict, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        count += 1
    return count
