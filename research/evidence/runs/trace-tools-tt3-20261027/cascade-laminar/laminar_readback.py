"""Read existing Laminar Signals for the own-run cohort; never ingest or run a Signal.

Only GET /v1/signals and POST /v1/sql/query SELECTs are used. Cache identity
mapping reads only id + trial.path; labels and family fields stay out of requests.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import uuid
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from evallab.laminar import (
    LAMINAR_ENDPOINT,
    _json_call,
    _sql,
    _trial_cloud_trace_uuid,
    laminar_trace_uuid,
)
from evallab.readers.laminar_import import read_signal_state

STATE = Path.home() / ".local/state/evallab-prod-live/trace-tools"
CANONICAL_SPEND = Path.home() / ".local/state/evallab-failure-library/spend.jsonl"
CACHE_SIGNALS = {
    "laminar_copied": "copied_upstream_fix",
    "laminar_false_completion": "false_completion_claim",
    "laminar_stuck_loop": "stuck_loop",
}
SCAN = "tt3-own-laminar-readback-20261027"


def rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def append(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as sink:
        sink.write(json.dumps(row) + "\n")


def source_chars(trial: Path) -> tuple[int, list[dict[str, Any]]]:
    """Count raw UTF-8-decoded ATIF chars, including all continuation documents."""
    path = trial / "agent/trajectory.json"
    seen = set()
    total = 0
    documents = []
    while True:
        path = path.resolve()
        if path in seen or path.parent != (trial / "agent").resolve():
            raise ValueError("invalid ATIF continuation chain")
        seen.add(path)
        data = path.read_text()
        total += len(data)
        documents.append({"file": path.name, "chars": len(data),
                          "sha256": hashlib.sha256(data.encode()).hexdigest()})
        ref = json.loads(data).get("continued_trajectory_ref")
        if not ref:
            return total, documents
        path = path.parent / ref


def evidence_steps(payload: Any) -> list[int]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return []
    if not isinstance(payload, dict):
        return []
    found = []
    for key in ("evidence_step", "step", "start_step"):
        value = payload.get(key)
        if isinstance(value, str):
            value = value.removeprefix("head#")
        with contextlib.suppress(ValueError, TypeError):
            found.append(int(value))
    return sorted(set(found))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=STATE)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    state, repo = args.state, args.repo
    cohort = [{"run_id": r["run_id"], "trial_dir": r["trial_dir"]}
              for r in rows(state / "tt3-cohort.jsonl")]
    by_trial = {Path(r["trial_dir"]).name: r["run_id"] for r in cohort}
    traces: dict[str, set[str]] = {r["run_id"]: set() for r in cohort}
    for r in cohort:
        trial = Path(r["trial_dir"])
        traces[r["run_id"]].add(laminar_trace_uuid(trial.name))
        sdk = _trial_cloud_trace_uuid(trial)
        if sdk:
            traces[r["run_id"]].add(sdk)
    mapped: dict[str, str] = {}
    for r in rows(repo / "research/calibration/detectors-v1/manifest.jsonl"):
        path = (r.get("trial") or {}).get("path")
        if path and Path(path).name in by_trial:
            mapped[r["id"]] = by_trial[Path(path).name]
    candidates: dict[tuple[str, str], list[dict[str, Any]]] = {}
    cache_count = 0
    for detector, name in CACHE_SIGNALS.items():
        for eval_id, run_id in mapped.items():
            path = repo / "research/calibration/detectors-v1/cache" / detector / f"{eval_id}.json"
            if not path.exists():
                continue
            cached = json.loads(path.read_text())
            trace_url = cached.get("trace_url")
            if trace_url:
                with contextlib.suppress(ValueError):
                    traces[run_id].add(str(uuid.UUID(trace_url.rstrip("/").rsplit("/", 1)[-1])))
            flag = cached.get("flagged")
            if flag is not None and not isinstance(flag, bool):
                raise ValueError(f"non-tristate cached flag for {eval_id}")
            candidates.setdefault((run_id, name), []).append({
                "flag": flag, "provenance": "committed-cache", "cache_id": eval_id,
                "cache_path": str(path.relative_to(repo)),
                "cache_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "explanation": cached.get("explanation"), "model": cached.get("model"),
                "trace_url": trace_url, "tokens": cached.get("tokens"),
            })
            cache_count += 1
    key = os.environ.get("LMNR_PROJECT_API_KEY", "").strip()
    if not key:
        raise RuntimeError("LMNR_PROJECT_API_KEY missing; launch with keys run --")
    endpoint = os.environ.get("EVALLAB_LAMINAR_ENDPOINT", "").strip() or LAMINAR_ENDPOINT
    call = {"api_key": key, "endpoint": endpoint}
    listed = (_json_call("GET", "/v1/signals", **call) or {}).get("signals", [])
    # The public Signal payload has no credentials; retain the observed config.
    (state / "laminar-own-signals.json").write_text(json.dumps(listed, indent=2) + "\n")
    signal_map = {s["name"]: s["id"] for s in listed}
    all_trace_ids = sorted({t for ids in traces.values() for t in ids})
    trace_to_runs: dict[str, list[str]] = {}
    for run_id, ids in traces.items():
        for trace in ids:
            trace_to_runs.setdefault(trace, []).append(run_id)
    cloud = read_signal_state(all_trace_ids, signal_map, **call)
    (state / "laminar-own-cloud-state.json").write_text(json.dumps(cloud, indent=2) + "\n")
    # Prove trace presence separately from Signal completion. This is a SELECT,
    # not an import: a missing trace is not a negative detection.
    present_rows = _sql("SELECT id FROM traces WHERE id IN {ids:Array(UUID)}", {"ids": all_trace_ids}, **call)
    present = {str(r["id"]) for r in present_rows}
    for composite, entry in cloud.items():
        trace, name = composite.split("\x00", 1)
        run = entry.get("run") or {}
        fired = entry.get("fired")
        status = str(run.get("status") or "")
        flag = True if fired is not None else (False if status == "COMPLETED" else None)
        for run_id in trace_to_runs.get(trace, []):
            candidates.setdefault((run_id, name), []).append({
                "flag": flag, "provenance": "cloud-sql-readback", "trace_id": trace,
                "signal_run": run, "event_payload": fired, "status": status,
            })
    at = datetime.now(UTC).isoformat()
    records = []
    conflicts = []
    for (run_id, name), options in sorted(candidates.items()):
        cloud_options = [x for x in options if x["provenance"] == "cloud-sql-readback"]
        observed = {x["flag"] for x in options if x["flag"] is not None}
        # Multiple cached ids can point at one immutable trial. Never majority-vote
        # or silently overwrite a conflicting observation into a negative.
        if len(observed) > 1:
            flag = None
            conflicts.append({"run_id": run_id, "signal": name, "reason": "conflicting historical results"})
        else:
            flag = next(iter(observed)) if observed else None
        steps = sorted({n for x in cloud_options for n in evidence_steps(x.get("event_payload"))})
        records.append({
            "schema": "evallab.trace_tool_result/v1", "run_id": run_id, "source": "own-harbor",
            "tool": "laminar", "detector": name, "detector_version": "existing-signals/readback-v1",
            "value": {"observations": options, "conflicting": len(observed) > 1},
            "flag": flag, "evidence_steps": steps, "scan": SCAN, "at": at,
        })
    store = state / "results-v1.jsonl"
    previous = {(r["run_id"], r["detector"], r["scan"]) for r in rows(store)
                if r.get("tool") == "laminar"} if store.exists() else set()
    for r in records:
        if (r["run_id"], r["detector"], r["scan"]) not in previous:
            append(store, r)
    (state / "laminar-own.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    covered = {r["run_id"] for r in records if r["flag"] is not None}
    # "Remaining" = runs without any determinate historical Signal verdict.
    # Importing these now fires every enabled realtime eval-lab Signal, even
    # though one may not be a reward-hacking detector (e.g. stuck_loop).
    enabled = [s for s in listed if s.get("name") in {*CACHE_SIGNALS.values(), "infra_not_model"}
               and s.get("mode") == "realtime" and not s.get("disabled", False)]
    nsignals = len(enabled)
    per_run = []
    for r in cohort:
        if r["run_id"] in covered:
            continue
        chars, documents = source_chars(Path(r["trial_dir"]))
        input_tokens = Decimal(chars) / 4
        output_tokens = 1000 * nsignals
        usd = input_tokens * nsignals * Decimal("0.15") / 1_000_000 + Decimal(output_tokens) * Decimal("0.50") / 1_000_000
        per_run.append({
            "run_id": r["run_id"], "trajectory_chars": chars,
            "input_tokens_per_signal": str(input_tokens), "signals": nsignals,
            "output_tokens": output_tokens, "usd": str(usd), "documents": documents,
            "trace_already_present": bool(traces[r["run_id"]] & present),
        })
    receipt = {
        "at": at, "runs": len(cohort), "cache_observations": cache_count,
        "mapped_cache_ids": len(mapped), "queried_trace_ids": len(all_trace_ids),
        "present_cloud_trace_ids": len(present),
        "runs_present_in_cloud": sum(bool(ids & present) for ids in traces.values()),
        "runs_with_determinate_result": len(covered),
        "records": len(records), "conflicts": conflicts,
        "per_signal": {name: dict(Counter("abstain" if r["flag"] is None else str(r["flag"]).lower()
                                            for r in records if r["detector"] == name))
                       for name in sorted({r["detector"] for r in records})},
        "enabled_realtime_evallab_signals": [
            {k: s.get(k) for k in ("id", "name", "mode", "disabled", "trigger", "model", "llmProfileId", "version")}
            for s in enabled
        ],
        "backfill": {
            "remaining_runs": len(per_run), "signals_per_run": nsignals,
            "input_price_usd_per_million": "0.15", "output_price_usd_per_million": "0.50",
            "assumed_output_tokens_per_signal": 1000,
            "input_tokens_once": str(sum((Decimal(r["input_tokens_per_signal"]) for r in per_run), Decimal(0))),
            "input_tokens_billed": str(sum((Decimal(r["input_tokens_per_signal"]) * nsignals for r in per_run), Decimal(0))),
            "output_tokens": sum(r["output_tokens"] for r in per_run),
            "zai_usd": str(sum((Decimal(r["usd"]) for r in per_run), Decimal(0))),
            "basis": "sum(raw ATIF + continuation document chars)/4 per Signal; no cache discount; exact arithmetic, estimated tokens",
            "laminar_failure_detector": [
                {k: s.get(k) for k in ("name", "disabled", "mode", "filters", "version")}
                for s in listed if "failure" in str(s.get("name", "")).lower()
            ],
            "additional_laminar_credits": "not priced; enabled realtime Failure Detector also fires on traces over 1,000 tokens; provider estimate is not an all-in bill",
            "ingested": 0, "usd_spent": 0,
        },
    }
    (state / "laminar-own-backfill.json").write_text(json.dumps(per_run, indent=2) + "\n")
    (state / "laminar-own-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    spend = {
        "at": at, "tool": "laminar", "route": "Laminar Cloud read-only REST + SQL SELECT",
        "model": None, "runs": len(cohort), "input_tokens": 0, "output_tokens": 0, "usd": 0,
        "basis": "GET signals; SELECT signal_runs, signal_events, traces; no ingest or Signal execution",
    }
    append(state / "spend.jsonl", spend)
    append(CANONICAL_SPEND, spend)
    print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    main()
