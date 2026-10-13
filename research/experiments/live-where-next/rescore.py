"""Replay the merged live-stop policy over published trial dirs ($0).

Each published trial becomes one counterfactual: the real
``trial_signals`` + ``evaluate_alerts`` pipeline runs over the recorded
trajectory, then every alert goes through the real ``stop_decision`` in
``dry-run`` mode. Per-trial JSON goes to ``--out`` (outside git by default).

Two replay assumptions, both documented in the experiment README:

- ``state`` is overridden to ``"running"``: every published trial already
  finished, and the terminal guard would skip everything. The override asks
  the counterfactual the policy cares about (would this alert have stopped
  a live trial?).
- ``hooks=None``, ``acks=[]``: published dirs rarely carry the hook journal,
  so verification-start and ack guards never fire in replay.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from multiprocessing import Pool
from pathlib import Path

from evallab.live_watch import WatchThresholds, evaluate_alerts, trial_signals
from evallab.watch_stop import stop_decision

_THRESHOLDS = WatchThresholds()


def _alert_step(alert: dict) -> int | None:
    ref = alert.get("step_ref") or ""
    head, _, num = str(ref).partition("#")
    if head == "head" and num.strip().isdigit():
        return int(num.strip())
    step = alert.get("step")
    return step if isinstance(step, int) else None


def _decisions(status: dict, alerts: list[dict]) -> list[dict]:
    running = dict(status, state="running")
    out = []
    for alert in alerts:
        decision = stop_decision(alert, status=running, hooks=None, acks=[], mode="dry-run")
        out.append(
            {
                "rule": alert.get("rule"),
                "step": _alert_step(alert),
                "action": decision["action"],
                "reason": decision["reason"],
            }
        )
    return out


def _slim_hit(hit: dict) -> dict:
    return {
        "step": hit.get("step"),
        "reason": hit.get("reason"),
        "kind": hit.get("kind"),
        "command": str(hit.get("command") or "")[:160],
    }


def _slim_read(hit: dict) -> dict:
    return {
        "step": hit.get("step"),
        "path": hit.get("path"),
        "events": hit.get("events"),
    }


def rescore_one(item: dict) -> dict:
    trial_dir = Path(item["trial_dir"])
    job_dir = trial_dir.parent
    try:
        status = trial_signals(
            job_dir, trial_dir, thresholds=_THRESHOLDS, from_config=False, now=time.time()
        )
    except Exception as exc:  # noqa: BLE001
        return {"trial": item["trial"], "error": f"{type(exc).__name__}: {exc}"}
    alerts = evaluate_alerts(status, thresholds=_THRESHOLDS)
    decisions = _decisions(status, alerts)
    stops = [d for d in decisions if d["action"] == "would_stop"]
    first = min(stops, key=lambda d: (d["step"] is None, d["step"]), default=None)
    return {
        "trial": item["trial"],
        "task": status.get("task"),
        "steps": status.get("steps"),
        "episodes": status.get("episodes"),
        "state": status.get("state"),
        "history": [_slim_hit(h) for h in status.get("history_mining", [])],
        "browsing": [_slim_hit(h) for h in status.get("history_browsing", [])],
        "sensor": [_slim_read(h) for h in status.get("hidden_test_reads", [])],
        "corroboration": [_slim_hit(h) for h in status.get("hidden_test_corroboration", [])],
        "alerts": [
            {"rule": a.get("rule"), "step": _alert_step(a), "severity": a.get("severity")}
            for a in alerts
        ],
        "decisions": decisions,
        "first_would_stop": first,
    }


def _audit_items(results: Path) -> list[dict]:
    items = []
    for name in ("cheated_passes.jsonl", "clean_passes.jsonl"):
        path = results / name
        if not path.is_file():
            continue
        label = "cheat" if name.startswith("cheated") else "clean"
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            items.append(
                {
                    "trial": row["trial"],
                    "trial_dir": row["trial_dir"],
                    "corpus": f"audit-{label}",
                    "mechanism": row.get("mechanism"),
                    "source": row.get("source"),
                }
            )
    return items


def _batch_items(results: Path, batch: str) -> list[dict]:
    items = []
    path = results / "batches" / batch / "trials.jsonl"
    if not path.is_file():
        return items
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        traj = (row.get("artifacts") or {}).get("trajectory")
        if not traj:
            continue
        trial_dir = row.get("trial_dir") or str(Path(traj).parent.parent)
        verdicts = row.get("verdicts") or {}
        if isinstance(verdicts, str):
            try:
                verdicts = json.loads(verdicts.replace("'", '"'))
            except ValueError:
                verdicts = {}
        flags = [v.get("cheated") for v in verdicts.values() if isinstance(v, dict)]
        items.append(
            {
                "trial": row.get("trial_id"),
                "trial_dir": trial_dir,
                "corpus": f"batch-{batch}",
                "family": row.get("family"),
                "reward": row.get("reward"),
                "cheat_step": row.get("cheat_step"),
                "batch_cheated": any(flags) if flags else None,
            }
        )
    return items


def collect_items(experiments: Path) -> list[dict]:
    """Unique trajectory-bearing trials: audit passes plus batch-only trials."""
    items = _audit_items(experiments / "contamination-audit" / "results")
    cheat_results = experiments / "cheatbench-port" / "results"
    for batch in (
        "2026-10-09-initial",
        "2026-10-12-cb-or-stop",
        "2026-10-11-cb-ready-e2e-specs",
        "2026-10-12-cb-or-smoke3",
    ):
        items.extend(_batch_items(cheat_results, batch))
    seen = set()
    unique = []
    for item in items:
        if item["trial"] in seen:
            continue
        seen.add(item["trial"])
        unique.append(item)
    return unique


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--experiments", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    parser.add_argument("--limit", type=int, help="First N trials per corpus (smoke runs)")
    parser.add_argument("--only", action="append", default=[], help="Trial name substring filter")
    args = parser.parse_args(argv)
    items = collect_items(args.experiments)
    if args.only:
        items = [it for it in items if any(s in it["trial"] for s in args.only)]
    if args.limit:
        by_corpus: dict[str, list[dict]] = {}
        for item in items:
            by_corpus.setdefault(item["corpus"], []).append(item)
        items = [it for group in by_corpus.values() for it in group[: args.limit]]
    args.out.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with Pool(args.workers) as pool:
        rows = pool.map(rescore_one, items, chunksize=16)
    meta = {it["trial"]: it for it in items}
    with open(args.out / "rescore.jsonl", "w") as fh:
        for row in rows:
            for key, value in meta.get(row["trial"], {}).items():
                if key not in ("trial", "trial_dir"):
                    row[key] = value
            fh.write(json.dumps(row) + "\n")
    print(f"{len(rows)} trials took {time.time() - started:.0f}s -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
