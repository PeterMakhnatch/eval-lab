"""Score imported Harbor-shaped trials with Laminar Signals (HAR-187).

Signals run inside Laminar Cloud on every finished trace (``rootSpanFinished``,
realtime, no filters), so this module only *imports*: for each Harbor-shaped
trial dir (``result.json`` + ``agent/trajectory.json`` in ATIF) it projects the
same spans the live watch projects (:func:`evallab.laminar.step_spans` /
:func:`evallab.laminar.root_span`, POSTed to ``/v1/traces``) and then collects
the Signal runs. No Signal config is created or changed here.

Cost model (Z.ai pay-as-you-go list prices, same as
:func:`evallab.readers.harbor_analyze.flash_cost`): every imported trace fires
the three enabled realtime LLM Signals (``copied_upstream_fix``,
``false_completion_claim``, ``stuck_loop``); ``stuck_loop`` is not scored but
the existing unfiltered triggers do not permit avoiding it per import, so its
tokens count toward the spend cap. Laminar also meters Signal tokens against
its own credits (see https://laminar.sh/pricing); those credits are distinct
from the Z.ai list-price spend recorded here.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from evallab.laminar import (
    LAMINAR_ENDPOINT,
    _json_call,
    _sql,
)
from evallab.readers.harbor_analyze import flash_cost

#: Scorecard reader -> Laminar Cloud Signal name.
READERS = {
    "laminar_copied": "copied_upstream_fix",
    "laminar_false_completion": "false_completion_claim",
}
#: Enabled realtime LLM Signal that fires on imports but is not scored.
OVERHEAD_SIGNAL = "stuck_loop"
#: Signals whose runs bill our Z.ai profile.
BILLED_SIGNALS = {*READERS.values(), OVERHEAD_SIGNAL}
#: Neutral job name stamped on imported root spans (never a label).
IMPORT_JOB = "har187-import"


def signal_ids(
    *,
    api_key: str,
    endpoint: str = LAMINAR_ENDPOINT,
    opener: Any = urlopen,
) -> dict[str, str]:
    """Laminar Cloud Signal name -> id for the scorecard Signals."""
    listed = _json_call("GET", "/v1/signals", api_key=api_key, endpoint=endpoint, opener=opener)
    return {
        s["name"]: s["id"] for s in (listed or {}).get("signals", []) if s["name"] in BILLED_SIGNALS
    }


def import_trial(
    trial_dir: Path,
    *,
    api_key: str,
    endpoint: str = LAMINAR_ENDPOINT,
    state_dir: Path,
    job: str = IMPORT_JOB,
    transport: Any = None,
) -> dict[str, Any]:
    """Project one finished trial dir as a Laminar trace; return its Cloud trace id.

    The Cloud trace id is deterministic in the trial dir name
    (:func:`evallab.laminar.laminar_trace_uuid`), so re-imports are idempotent
    (exporter state in ``state_dir`` skips already-posted spans) and never
    duplicate traces. Only the dir *name* (an opaque eval id) reaches Laminar;
    the abs path, labels, and families stay local.
    """
    from evallab.laminar import LaminarExporter

    trial_dir = Path(trial_dir)
    kwargs: dict[str, Any] = {"api_key": api_key, "out_dir": state_dir, "endpoint": endpoint}
    if transport is not None:
        kwargs["transport"] = transport
    exporter = LaminarExporter(**kwargs)
    trace = exporter.sync_trial(Path(job), trial_dir, finished=True)
    outcome = exporter.flush()
    return {"trial": trial_dir.name, "trace_id": trace, **outcome}


def read_signal_state(
    trace_ids: list[str],
    signal_map: dict[str, str],
    *,
    api_key: str,
    endpoint: str = LAMINAR_ENDPOINT,
    opener: Any = urlopen,
) -> dict[str, dict[str, Any]]:
    """Per ``(trace, signal-name)``: latest run row + whether an event fired."""
    if not trace_ids or not signal_map:
        return {}
    call: dict[str, Any] = {"api_key": api_key, "endpoint": endpoint, "opener": opener}
    params = {"ids": list(trace_ids), "signals": list(signal_map.values())}
    inv = {v: k for k, v in signal_map.items()}
    state: dict[str, dict[str, Any]] = {}
    for row in _sql(
        "SELECT trace_id, signal_id, run_id, status, input_tokens, cache_read_tokens,"
        " output_tokens, credit_applied, error_message, updated_at FROM signal_runs"
        " WHERE trace_id IN {ids:Array(UUID)} AND signal_id IN {signals:Array(UUID)}"
        " ORDER BY updated_at ASC",
        params,
        **call,
    ):
        name = inv.get(str(row["signal_id"]), str(row["signal_id"]))
        state[f"{row['trace_id']}\x00{name}"] = {"run": dict(row), "fired": None}
    for row in _sql(
        "SELECT trace_id, signal_id, payload FROM signal_events"
        " WHERE trace_id IN {ids:Array(UUID)} AND signal_id IN {signals:Array(UUID)}",
        params,
        **call,
    ):
        name = inv.get(str(row["signal_id"]), str(row["signal_id"]))
        key = f"{row['trace_id']}\x00{name}"
        entry = state.setdefault(key, {"run": None, "fired": None})
        entry["fired"] = row.get("payload")
    return state


def await_signal_runs(
    trace_ids: list[str],
    signal_map: dict[str, str],
    *,
    api_key: str,
    endpoint: str = LAMINAR_ENDPOINT,
    opener: Any = urlopen,
    deadline_s: float = 600.0,
    interval_s: float = 20.0,
    sleep: Any = time.sleep,
) -> dict[str, dict[str, Any]]:
    """Poll until every trace has a terminal run for every billed Signal."""
    wanted = {f"{t}\x00{name}" for t in trace_ids for name in signal_map}
    state: dict[str, dict[str, Any]] = {}
    start = time.monotonic()
    while True:
        state = read_signal_state(
            trace_ids, signal_map, api_key=api_key, endpoint=endpoint, opener=opener
        )
        done = {
            key
            for key, entry in state.items()
            if entry["run"] is not None
            and str(entry["run"].get("status")) in ("COMPLETED", "FAILED")
        }
        if wanted <= done or time.monotonic() - start >= deadline_s:
            return state
        sleep(interval_s)


def estimate_cost(num_traces: int, per_trace_usd: float) -> float:
    """Worst-case Z.ai spend for importing ``num_traces`` more traces."""
    return num_traces * per_trace_usd


def verdict_for(
    eval_id: str,
    reader: str,
    *,
    trace_id: str,
    project_id: str,
    model: str,
    entry: dict[str, Any] | None,
) -> dict[str, Any]:
    """One scorecard verdict (``flagged``/``score``/tokens/cost) from Signal state."""
    run = (entry or {}).get("run") or {}
    fired = (entry or {}).get("fired")
    status = str(run.get("status") or "")
    if fired is not None:
        flagged: bool | None = True
        explanation = _payload_text(fired)[:500]
    elif status == "COMPLETED":
        flagged = False
        explanation = "signal completed without firing"
    elif status == "FAILED":
        flagged = None
        explanation = f"signal run failed: {run.get('error_message') or 'unknown error'}"[:500]
    else:
        flagged = None
        explanation = "signal has not run on this trace"
    tokens = {
        "input": int(run.get("input_tokens") or 0),
        "cached": int(run.get("cache_read_tokens") or 0),
        "output": int(run.get("output_tokens") or 0),
    }
    return {
        "id": eval_id,
        "reader": reader,
        "flagged": flagged,
        "score": None,
        "explanation": explanation,
        "model": model,
        "tokens": tokens,
        "cost_usd": flash_cost(tokens["input"], tokens["output"], tokens["cached"]),
        "raw": None,
        "trace_id": trace_id,
        "trace_url": (
            f"https://lmnr.ai/project/{project_id}/traces/{trace_id}" if project_id else None
        ),
    }


def _payload_text(payload: Any) -> str:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return payload
    return json.dumps(payload, sort_keys=True) if not isinstance(payload, str) else payload


def convert_reused(stored: dict[str, Any], eval_id: str, reader: str) -> dict[str, Any]:
    """Scorecard verdict reusing an existing ``laminar_signals.json`` (no new spend)."""
    check = {"laminar_copied": "copied", "laminar_false_completion": "false_completion"}[reader]
    checks = stored.get("checks") or {}
    explanations = stored.get("explanations") or {}
    return {
        "id": eval_id,
        "reader": reader,
        "flagged": checks.get(check),
        "score": None,
        "explanation": explanations.get(check, ""),
        "model": stored.get("model"),
        "tokens": {"input": 0, "cached": 0, "output": 0},
        "cost_usd": 0.0,
        "raw": None,
        "reused": True,
        "trace_id": stored.get("trace_id"),
        "trace_url": stored.get("trace_url"),
    }


def write_verdicts(
    verdicts: list[dict[str, Any]],
    raws: dict[str, dict[str, Any]],
    root: Path,
) -> list[Path]:
    """Write ``<root>/<reader>/<id>.json`` + raw Signal payloads; return paths."""
    paths = []
    for verdict in verdicts:
        key = f"{verdict['reader']}\x00{verdict['id']}"
        raw_path = root / "raw" / f"{verdict['reader']}-{verdict['id']}.json"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(json.dumps(raws.get(key, {}), indent=1, sort_keys=True) + "\n")
        verdict["raw"] = str(raw_path)
        dest = root / verdict["reader"] / f"{verdict['id']}.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(verdict, indent=1, sort_keys=True) + "\n")
        paths.append(dest)
    return paths


def append_spend(
    spend_path: Path,
    *,
    reader: str,
    ids: list[str],
    signal_state: dict[str, dict[str, Any]],
    signals: dict[str, str],
    at: str | None = None,
) -> dict[str, Any]:
    """One ledger line per import batch; returns the appended entry."""
    totals = {"input": 0, "cached": 0, "output": 0}
    for entry in signal_state.values():
        run = entry.get("run") or {}
        totals["input"] += int(run.get("input_tokens") or 0)
        totals["cached"] += int(run.get("cache_read_tokens") or 0)
        totals["output"] += int(run.get("output_tokens") or 0)
    entry_out = {
        "reader": reader,
        "ids": list(ids),
        "tokens": totals,
        "cost_usd": flash_cost(totals["input"], totals["output"], totals["cached"]),
        "signals": dict(signals),
        "at": at or datetime.now(UTC).isoformat(),
    }
    spend_path.parent.mkdir(parents=True, exist_ok=True)
    with spend_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry_out, sort_keys=True) + "\n")
    return entry_out
