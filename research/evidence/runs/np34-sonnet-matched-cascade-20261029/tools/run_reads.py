"""Quota-gated, call-guarded reads of cascade v8 with a Sonnet judge on the 246 runs of the 123 matched pairs (NP34-TT-1).

Label-free. Replays every run through the harness' ``cascade_stream:<reader>`` detector (Stage 1 deterministic cards,
Stage 2 judged windows, prompt ``window-cascade/v8``) in batches of at most 60 runs. Before each batch the reader
provider's quota is read (``omp usage``) and no batch starts at or above 85%; inside every call the quota gate and a
:class:`CallGuard` (3 consecutive refusals, 3 consecutive failed calls, 1,500-attempt hard cap, budget reserved before a
call) apply; the served model is attested on every attempt and every attempt is appended to the private ledger. One
``usd: 0`` spend line per batch. Findings go to the PRIVATE store ``np34/findings.jsonl``, cached by
``(run, detector, version, reader)`` so a rerun reads only what is missing. A run whose call was stopped writes no
finding. The run list is the pair file of NP31; no label column is read.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

os.environ.setdefault("EVALLAB_DISABLE_LOCAL_DOCKER", "1")

from evallab.production.trace_harness import (  # noqa: E402
    DEFAULT_DATABASE,
    FindingStore,
    HarnessOptions,
    resolve_runs,
    run_detectors,
)
from evallab.production.trace_harness_stream import (  # noqa: E402
    CallGuard,
    CascadeStreamDetector,
    QuotaGate,
    QuotaStop,
    VerdictCache,
    live_call,
    provider_of,
    quota_used,
)

STATE = Path.home() / ".local/state"
PRIVATE = STATE / "evallab-prod-live/trace-harness/np34"
SPEND = STATE / "evallab-failure-library/spend.jsonl"
PAIRS = STATE / "evallab-prod-live/trace-harness/np31-length-matched/goodfire-dev-pairs.jsonl"
READER = "anthropic/claude-sonnet-5-5"
STOP = 0.85
BATCH = 60
WORKERS = 6
MAX_ATTEMPTS = 1500
CANARY_SYSTEM = 'Reply with exactly this JSON and nothing else: {"label": "neither", "mechanism": null, "evidence": []}'


def slug(reader: str) -> str:
    return reader.replace("/", "_")


def pair_run_ids() -> list[str]:
    ids: list[str] = []
    for line in PAIRS.read_text().splitlines():
        pair = json.loads(line)
        ids += [pair["enacted"], pair["clean"]]
    return ids


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def attempts_since(path: Path, offset: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("rb") as handle:
        handle.seek(offset)
        return [json.loads(line) for line in handle.read().decode().splitlines() if line.strip()]


def spend_line(reader: str, batch: int, rows: list[dict[str, Any]], runs: int, fresh: int, guard: CallGuard) -> dict[str, Any]:
    served = Counter(str(row.get("served")) for row in rows if row.get("ok"))
    rejected = [row for row in rows if not row.get("ok")]
    return {
        "accounting_status": "subscription-no-marginal-cost", "cost_usd": 0.0, "usd": 0.0, "job": "np34-tt-1-pairs",
        "lane": "trace-harness", "requested_route": reader, "route": ", ".join(sorted(served)) or None,
        "note": f"cascade v8 Stage-2 reads, batch {batch}: {runs} runs, {fresh} fresh judge calls, {len(rows)} omp attempts; "
                f"{sum(served.values())} attempts served by the requested model, {len(rejected)} attempts rejected "
                f"(served: {dict(Counter(str(r.get('served')) for r in rejected))}); guard {json.dumps(guard.as_dict())}; "
                "headless omp, Claude subscription, fallback off",
        "input_tokens": sum(int(r.get("input_tokens") or 0) for r in rows),
        "output_tokens": sum(int(r.get("output_tokens") or 0) for r in rows), "ts": now(),
    }


def read(reader: str, batch: int = BATCH, workers: int = WORKERS, max_attempts: int = MAX_ATTEMPTS) -> int:
    repo = Path.cwd()
    PRIVATE.mkdir(parents=True, exist_ok=True)
    ledger = PRIVATE / f"attempts-{slug(reader)}.jsonl"
    provider = provider_of(reader)
    runs = resolve_runs(",".join(pair_run_ids()), database=DEFAULT_DATABASE, repo_root=repo)
    store = FindingStore(PRIVATE / "findings.jsonl")
    done = {key[0] for key in store.records() if key[1:3] == ("cascade_stream", "window-cascade/v8") and key[3] == reader}
    todo = [run for run in runs if run.run_id not in done]
    print(f"{len(runs)} runs, {len(done)} already read, {len(todo)} to read with {reader}", flush=True)
    guard = CallGuard(max_attempts=max_attempts)
    # The attempt budget counts what earlier invocations of this driver already used.
    guard.attempts = len(attempts_since(ledger, 0))
    if not todo:
        return 0
    gate = QuotaGate(provider=provider, stop=STOP, refresh_seconds=30.0)
    call = live_call(reader, gate, ledger, guard)
    canary = call(reader, CANARY_SYSTEM, "go")
    if not canary.get("ok") or canary.get("served") != reader:
        raise SystemExit(f"canary failed for {reader}: {canary.get('errors') or canary.get('served')}")
    print(f"canary ok: served {canary['served']}", flush=True)
    detector = CascadeStreamDetector(
        HarnessOptions(database=DEFAULT_DATABASE, repo_root=repo, allow_model=True, reader=reader),
        cache=VerdictCache(PRIVATE / f"judge-cache-{slug(reader)}.jsonl", model=reader), call=call, workers=workers,
    )
    stopped = False
    for index in range(0, len(todo), batch):
        chunk = todo[index:index + batch]
        try:
            used = quota_used(provider)
        except QuotaStop as error:
            print(f"STOP before batch {index // batch}: {error}", flush=True)
            stopped = True
            break
        if used >= STOP:
            print(f"STOP before batch {index // batch}: {provider} window at {used:.0%}", flush=True)
            stopped = True
            break
        offset = ledger.stat().st_size if ledger.exists() else 0
        before = detector.stats.fresh
        started = time.monotonic()
        summary = run_detectors(chunk, [detector], store, log=lambda message: print(message, flush=True))
        rows = attempts_since(ledger, offset)
        line = spend_line(reader, index // batch, rows, len(chunk), detector.stats.fresh - before, guard)
        SPEND.parent.mkdir(parents=True, exist_ok=True)
        with SPEND.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True) + "\n")
        print(f"batch {index // batch}: {len(chunk)} runs in {time.monotonic() - started:.0f}s, quota {used:.0%} before, "
              f"{summary['detectors']}, guard {json.dumps(guard.as_dict())}", flush=True)
        if guard.stop_reason or detector.stats.quota_stopped:
            print(f"STOP: {guard.stop_reason or 'a call was refused for quota'}", flush=True)
            stopped = True
            break
    remaining = len(todo) - len({key[0] for key in store.records() if key[3] == reader} - done)
    result = {"reader": reader, "stopped": stopped, "stop_reason": guard.stop_reason, "unread_runs": remaining,
              "guard": guard.as_dict(), "stats": detector.stats.as_dict(), "finished": now()}
    print(json.dumps(result), flush=True)
    with (PRIVATE / "run-summary.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(result, sort_keys=True) + "\n")
    return 2 if stopped else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--reader", default=READER)
    parser.add_argument("--batch", type=int, default=BATCH)
    parser.add_argument("--workers", type=int, default=WORKERS)
    parser.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS)
    args = parser.parse_args()
    return read(args.reader, args.batch, args.workers, args.max_attempts)


if __name__ == "__main__":
    sys.exit(main())
