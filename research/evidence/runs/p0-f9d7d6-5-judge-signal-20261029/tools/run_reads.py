"""Quota-gated batch reads of cascade v8 (Haiku judge) on the P0-SELF-F9D7D6-5 sample. Label-free.

Same plumbing as p0-f9d7d6-3 ``run_reads.py`` (``cascade_stream`` detector: Stage 1 cards, Stage 2 judged windows,
prompt ``window-cascade/v8``; no tools, fallback off, served-model attestation, per-reader verdict cache, quota gate)
with: batches of 16 runs and ``omp usage`` before each; a ``CallGuard`` (stop after 3 consecutive refusals, attempt
budget 1,500); one ``usd: 0`` line per batch in the failure-library spend ledger. Reads run ids from ``sample.jsonl``
(run id, stratum, steps only); labels are never loaded here.
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
    judge_call,
    live_call,
    provider_of,
    quota_used,
)

STATE = Path.home() / ".local/state"
PRIVATE = STATE / "evallab-prod-live/trace-harness/p0f5"
SPEND = STATE / "evallab-failure-library/spend.jsonl"
PURPOSE = "p0-SELF-f9d7d6-5"
READER = "anthropic/claude-haiku-5-5"
STOP = 0.85
BATCH = 16
WORKERS = 16
CALL_CAP = 1500
CANARY_SYSTEM = 'Reply with exactly this JSON and nothing else: {"label": "neither", "mechanism": null, "evidence": []}'


def slug(reader: str) -> str:
    return reader.replace("/", "_")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def attempts_since(path: Path, offset: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("rb") as handle:
        handle.seek(offset)
        return [json.loads(line) for line in handle.read().decode().splitlines() if line.strip()]


def spend_line(batch: int, rows: list[dict[str, Any]], runs: int, fresh: int) -> dict[str, Any]:
    served = Counter(str(row.get("served")) for row in rows if row.get("ok"))
    rejected = [row for row in rows if not row.get("ok")]
    return {
        "ts": now(), "route": READER, "usd": 0, "purpose": PURPOSE, "calls": fresh,
        "input_tokens": sum(int(r.get("input_tokens") or 0) for r in rows),
        "output_tokens": sum(int(r.get("output_tokens") or 0) for r in rows),
        "note": f"batch {batch}: {runs} runs, {sum(served.values())} attempts served by {dict(served)}, "
                f"{len(rejected)} rejected ({dict(Counter(str(r.get('served')) for r in rejected))}); "
                "subscription, fallback off, headless omp",
    }


def read(batch: int = BATCH, workers: int = WORKERS) -> int:
    repo = Path.cwd()
    PRIVATE.mkdir(parents=True, exist_ok=True)
    ledger = PRIVATE / f"attempts-{slug(READER)}.jsonl"
    ids = [json.loads(line)["run_id"] for line in (PRIVATE / "sample.jsonl").read_text().splitlines()]
    runs = {run.run_id: run for run in resolve_runs(",".join(ids), repo_root=repo)}
    ordered = [runs[i] for i in ids]
    store = FindingStore(PRIVATE / "findings.jsonl")
    done = {key[0] for key in store.records() if key[1:3] == ("cascade_stream", "window-cascade/v8") and key[3] == READER}
    todo = [run for run in ordered if run.run_id not in done]
    print(f"{len(ordered)} runs, {len(done)} already read, {len(todo)} to read with {READER}", flush=True)
    if not todo:
        return 0
    gate = QuotaGate(provider=provider_of(READER), stop=STOP, refresh_seconds=30.0)
    canary_row = judge_call(READER, CANARY_SYSTEM, "go", gate=gate, ledger=ledger)
    if not canary_row.get("ok") or canary_row.get("served") != READER:
        raise SystemExit(f"canary failed: {canary_row.get('errors') or canary_row.get('served')}")
    print(f"canary ok: served {canary_row['served']}", flush=True)
    guard = CallGuard(max_attempts=CALL_CAP)
    detector = CascadeStreamDetector(
        HarnessOptions(database=DEFAULT_DATABASE, repo_root=repo, allow_model=True, reader=READER),
        cache=VerdictCache(PRIVATE / f"judge-cache-{slug(READER)}.jsonl", model=READER),
        call=live_call(READER, gate, ledger, guard), workers=workers,
    )
    stopped = None
    for index in range(0, len(todo), batch):
        chunk = todo[index:index + batch]
        try:
            used = quota_used(provider_of(READER))
        except QuotaStop as error:
            stopped = f"STOP before batch {index // batch}: {error}"
            break
        if used >= STOP:
            stopped = f"STOP before batch {index // batch}: window at {used:.0%}"
            break
        offset = ledger.stat().st_size if ledger.exists() else 0
        before = detector.stats.fresh
        started = time.monotonic()
        run_detectors(chunk, [detector], store, log=lambda message: print(message, flush=True))
        rows = attempts_since(ledger, offset)
        SPEND.parent.mkdir(parents=True, exist_ok=True)
        with SPEND.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(spend_line(index // batch, rows, len(chunk), detector.stats.fresh - before),
                                    sort_keys=True) + "\n")
        print(f"batch {index // batch}: {len(chunk)} runs in {time.monotonic() - started:.0f}s, quota {used:.0%} before, "
              f"guard {guard.as_dict()}", flush=True)
        if guard.stop_reason or detector.stats.quota_stopped:
            stopped = f"STOP: guard={guard.stop_reason} quota_stopped={detector.stats.quota_stopped}"
            break
    if stopped:
        print(stopped, flush=True)
    print(json.dumps({"reader": READER, "stopped": stopped, "guard": guard.as_dict(), "stats": detector.stats.as_dict()}),
          flush=True)
    return 2 if stopped else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--batch", type=int, default=BATCH)
    parser.add_argument("--workers", type=int, default=WORKERS)
    args = parser.parse_args()
    return read(args.batch, args.workers)


if __name__ == "__main__":
    sys.exit(main())
