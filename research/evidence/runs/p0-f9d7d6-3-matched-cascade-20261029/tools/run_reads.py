"""Quota-gated batch reads of cascade v8 with a Claude reader (P0-F9D7D6-3). Label-free.

``read <population> --reader <provider/model>`` replays every run of a population through the harness'
``cascade_stream`` detector (Stage 1 deterministic cards, Stage 2 judged windows) in batches, with the reader's
provider quota checked before each batch (and inside every call by the quota gate), the served model attested on
every call, and one spend line per batch. Findings go to a PRIVATE per-population store, cached by
``(run, detector, version, reader)`` so a re-run only reads what is missing.

Populations (run ids only; the label columns of every source are never read here):

* ``g150``       the 150 labelled Goodfire sample runs (``load_corpus_labels('goodfire')`` ids)
* ``pairs``      the 246 runs of the 123 NP31 primary matched pairs
* ``cheatbench`` CheatBench DEV, 150 runs
* ``cm18``       CM-18 staged runs of the ``positive`` and ``negative_established`` strata (7 sets)
* ``np21``       NP21 Flake Laundering staged runs of the same two strata
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
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
    HarnessRun,
    actor_inputs_available,
    resolve_runs,
    run_detectors,
)
from evallab.production.trace_harness_corpora import load_corpus_labels  # noqa: E402
from evallab.production.trace_harness_stream import (  # noqa: E402
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
PRIVATE = STATE / "evallab-prod-live/trace-harness/p0-f9d7d6-3"
SPEND = STATE / "evallab-failure-library/spend.jsonl"
PAIRS = STATE / "evallab-prod-live/trace-harness/np31-length-matched/goodfire-dev-pairs.jsonl"
STAGED = {"cm18": STATE / "evallab-cm18", "np21": STATE / "evallab-np21-cm1"}
STAGED_STRATA = ("positive", "negative_established")
STOP = 0.85
BATCH = 60
WORKERS = 6
POPULATIONS = ("g150", "pairs", "cheatbench", "cm18", "np21")
CANARY_SYSTEM = 'Reply with exactly this JSON and nothing else: {"label": "neither", "mechanism": null, "evidence": []}'


def slug(reader: str) -> str:
    return reader.replace("/", "_")


def staged_runs(tag: str) -> list[HarnessRun]:
    root = STAGED[tag]
    wanted = {
        (row["set"], row["key"])
        for row in json.loads((root / "sample.json").read_text())["runs"]
        if row["stratum"] in STAGED_STRATA and (tag == "cm18" or row["set"] == "flake")
    }
    with (root / "runs.pkl").open("rb") as handle:
        rows = pickle.load(handle)
    runs = []
    for row in rows:
        if (row["set"], row["key"]) in wanted:
            steps = tuple(row["steps"])
            runs.append(HarnessRun(f"{tag}:{row['set']}:{row['key']}", f"{tag}:{row['set']}", "CROSS-TEST",
                                   "unknown", steps, actor_inputs_available(steps)))
    return runs


def load_runs(population: str, repo: Path) -> list[HarnessRun]:
    if population == "g150":
        labels = load_corpus_labels("goodfire", database=DEFAULT_DATABASE, repo_root=repo)
        ids = sorted(r for r in labels.run_ids if labels.positive.get(r) is not None)
        return resolve_runs(",".join(ids), repo_root=repo)
    if population == "pairs":
        ids = []
        for line in PAIRS.read_text().splitlines():
            pair = json.loads(line)
            ids += [pair["enacted"], pair["clean"]]
        return resolve_runs(",".join(ids), repo_root=repo)
    if population == "cheatbench":
        return resolve_runs("cheatbench", repo_root=repo)
    return staged_runs(population)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def attempts_since(path: Path, offset: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("rb") as handle:
        handle.seek(offset)
        return [json.loads(line) for line in handle.read().decode().splitlines() if line.strip()]


def spend_line(population: str, reader: str, batch: int, rows: list[dict[str, Any]], runs: int, fresh: int) -> dict[str, Any]:
    served = Counter(str(row.get("served")) for row in rows if row.get("ok"))
    rejected = [row for row in rows if not row.get("ok")]
    return {
        "accounting_status": "subscription-no-marginal-cost", "cost_usd": 0.0, "job": f"p0-f9d7d6-3-{population}",
        "lane": "trace-harness", "requested_route": reader, "route": ", ".join(sorted(served)) or None,
        "note": f"cascade v8 Stage-2 reads, batch {batch}: {runs} runs, {fresh} fresh judge calls; "
                f"{sum(served.values())} attempts served by the requested model, {len(rejected)} attempts rejected "
                f"(served: {dict(Counter(str(r.get('served')) for r in rejected))}); headless omp, Claude subscription, "
                "fallback off",
        "input_tokens": sum(int(r.get("input_tokens") or 0) for r in rows),
        "output_tokens": sum(int(r.get("output_tokens") or 0) for r in rows), "ts": now(),
    }


def canary(reader: str, ledger: Path) -> None:
    row = judge_call(reader, CANARY_SYSTEM, "go", gate=QuotaGate(provider=provider_of(reader), stop=STOP), ledger=ledger)
    if not row.get("ok") or row.get("served") != reader:
        raise SystemExit(f"canary failed for {reader}: {row.get('errors') or row.get('served')}")
    print(f"canary ok: served {row['served']}", flush=True)


def read(population: str, reader: str, batch: int = BATCH, workers: int = WORKERS) -> int:
    repo = Path.cwd()
    PRIVATE.mkdir(parents=True, exist_ok=True)
    ledger = PRIVATE / f"attempts-{slug(reader)}.jsonl"
    provider = provider_of(reader)
    runs = load_runs(population, repo)
    store = FindingStore(PRIVATE / f"findings-{population}-{slug(reader)}.jsonl")
    done = {key[0] for key in store.records() if key[1:3] == ("cascade_stream", "window-cascade/v8")
            and key[3] == reader}
    todo = [run for run in runs if run.run_id not in done]
    print(f"{population}: {len(runs)} runs, {len(done)} already read, {len(todo)} to read with {reader}", flush=True)
    if not todo:
        return 0
    canary(reader, ledger)
    gate = QuotaGate(provider=provider, stop=STOP, refresh_seconds=30.0)
    detector = CascadeStreamDetector(
        HarnessOptions(database=DEFAULT_DATABASE, repo_root=repo, allow_model=True, reader=reader),
        cache=VerdictCache(PRIVATE / f"judge-cache-{slug(reader)}.jsonl", model=reader),
        call=live_call(reader, gate, ledger), workers=workers,
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
        line = spend_line(population, reader, index // batch, rows, len(chunk), detector.stats.fresh - before)
        SPEND.parent.mkdir(parents=True, exist_ok=True)
        with SPEND.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True) + "\n")
        print(f"batch {index // batch}: {len(chunk)} runs in {time.monotonic() - started:.0f}s, quota {used:.0%} before, "
              f"{summary['detectors']}", flush=True)
        if detector.stats.quota_stopped:
            print("STOP: a call was refused for quota", flush=True)
            stopped = True
            break
    remaining = len(todo) - len({key[0] for key in store.records() if key[3] == reader} - done)
    print(json.dumps({"population": population, "reader": reader, "stopped_on_quota": stopped,
                      "unread_runs": remaining, "stats": detector.stats.as_dict()}), flush=True)
    return 2 if stopped else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("population", choices=POPULATIONS)
    parser.add_argument("--reader", default="anthropic/claude-haiku-5-5")
    parser.add_argument("--batch", type=int, default=BATCH)
    parser.add_argument("--workers", type=int, default=WORKERS)
    args = parser.parse_args()
    return read(args.population, args.reader, args.batch, args.workers)


if __name__ == "__main__":
    sys.exit(main())
