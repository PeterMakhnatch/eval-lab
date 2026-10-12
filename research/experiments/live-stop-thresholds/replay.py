"""Replay the merged live-stop policy (#833) and the candidate rules over RL rollouts.

Each rollout becomes one running Harbor-shaped trial: a user step with the
prompt, then one agent step whose message is the full answer and whose
``write_file`` tool calls write every submitted file, i.e. what lands in the
grading sandbox. The real ``trial_signals`` -> ``evaluate_alerts`` ->
``stop_decision(mode="dry-run")`` path runs unchanged: nothing in the watch is
reimplemented or patched. The trial stays ``running`` with no hook journal, so
the terminal / verification guards never mask a decision.

Usage (CPU only, no network)::

    uv run python research/experiments/live-stop-thresholds/replay.py \
        --data-root /private/tmp/live-stop-data --out /private/tmp/live-stop-data/replay

Writes ``<out>/<corpus>.parquet`` (one row per rollout) and prints counts.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import tempfile
import time
from collections import Counter
from multiprocessing import Pool
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))

from candidate_rules import RULES, evaluate  # noqa: E402
from corpora import CORPORA, Rollout, iter_rollouts  # noqa: E402

from evallab.live_watch import WatchThresholds, evaluate_alerts, trial_signals  # noqa: E402
from evallab.watch_stop import stop_decision  # noqa: E402

_THRESHOLDS = WatchThresholds()
_TRIAL_DIR: Path | None = None


def adapter_steps(rollout: Rollout) -> list[dict]:
    """ATIF steps for one single-turn rollout (see module doc)."""
    prompt = "\n\n".join(part for part in (rollout.system, rollout.prompt) if part)
    calls = [
        {
            "tool_call_id": f"write-{index}",
            "function_name": "write_file",
            "arguments": {"file_path": f"/workspace/{path}", "content": content},
        }
        for index, (path, content) in enumerate(rollout.files, start=1)
    ]
    return [
        {"step_id": 1, "source": "user", "message": prompt},
        {"step_id": 2, "source": "agent", "message": rollout.assistant, "tool_calls": calls},
    ]


def watch_replay(rollout: Rollout, trial_dir: Path) -> tuple[list[str], list[str]]:
    """``(alert rules, would-stop rules)`` of the merged watch for one rollout."""
    agent_dir = trial_dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "trajectory.json").write_text(
        json.dumps({"steps": adapter_steps(rollout)}), encoding="utf-8"
    )
    status = trial_signals(
        trial_dir.parent,
        trial_dir,
        thresholds=_THRESHOLDS,
        from_config=False,
        now=time.time(),
    )
    alerts = evaluate_alerts(status, thresholds=_THRESHOLDS)
    would_stop = set()
    for alert in alerts:
        decision = stop_decision(alert, status=status, hooks=None, acks=[], mode="dry-run")
        if decision["action"] == "would_stop":
            would_stop.add(decision["rule"])
    return sorted({alert["rule"] for alert in alerts}), sorted(would_stop)


def _init_worker(scratch: str) -> None:
    global _TRIAL_DIR
    _TRIAL_DIR = Path(scratch) / "replay-job" / f"rollout__{os.getpid()}"


def _row(rollout: Rollout) -> dict:
    assert _TRIAL_DIR is not None
    alerts, would_stop = watch_replay(rollout, _TRIAL_DIR)
    fired = evaluate(rollout.files, rollout.prompt)
    row = {
        "corpus": rollout.corpus,
        "rollout_id": rollout.rollout_id,
        "step": rollout.step,
        "hack": rollout.hack,
        "attempt": rollout.attempt,
        "family": rollout.family,
        "solved": rollout.solved,
        "source": rollout.source,
        "n_files": len(rollout.files),
        "watch_alerts": ";".join(alerts),
        "watch_would_stop": ";".join(would_stop),
    }
    for rule in RULES:
        row[rule] = fired[rule] is not None
    row["tamper_signature_pattern"] = fired["tamper_signature"]
    return row


def replay_corpus(
    corpus: str, data_root: Path, out: Path, workers: int, limit: int | None = None
) -> Path:
    """Replay one corpus and write ``<out>/<corpus>.parquet``."""
    out.mkdir(parents=True, exist_ok=True)
    rollouts = itertools.islice(iter_rollouts(corpus, data_root), limit)
    with (
        tempfile.TemporaryDirectory(prefix="live-stop-replay-") as scratch,
        Pool(workers, initializer=_init_worker, initargs=(scratch,)) as pool,
    ):
        rows = list(pool.imap(_row, rollouts, chunksize=64))
    target = out / f"{corpus}.parquet"
    pq.write_table(pa.Table.from_pylist(rows), target)
    alerts = Counter(rule for row in rows for rule in row["watch_alerts"].split(";") if rule)
    stops = sum(bool(row["watch_would_stop"]) for row in rows)
    print(f"{corpus}: {len(rows)} rollouts, watch alerts {dict(alerts)}, would_stop {stops}")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--corpus", choices=CORPORA, action="append")
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    parser.add_argument("--limit", type=int, help="first N rollouts per corpus (smoke runs)")
    args = parser.parse_args(argv)
    for corpus in args.corpus or CORPORA:
        started = time.time()
        replay_corpus(corpus, args.data_root, args.out, args.workers, args.limit)
        print(f"  {corpus} took {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
