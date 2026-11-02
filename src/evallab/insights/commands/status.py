"""Plain status of the engine: is it alive, what is waiting, what came out."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from evallab.insights.commands._common import add_state_dir, emit, state_dir
from evallab.insights.store import DB_NAME, InsightStore


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("status", help="Engine health, ledger counts and what each lens waits for")
    add_state_dir(parser)
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.set_defaults(func=_cmd)


def collect_status(root: Path, *, now: datetime) -> dict[str, object]:
    heartbeat_path = root / "heartbeat.json"
    heartbeat = json.loads(heartbeat_path.read_text("utf-8")) if heartbeat_path.exists() else None
    age_minutes = None
    if heartbeat is not None and heartbeat.get("at"):
        age_minutes = round((now - datetime.fromisoformat(heartbeat["at"])).total_seconds() / 60, 1)
    with InsightStore.open_readonly(root / DB_NAME) as store:
        last_tick_raw = store.get_meta("last_tick")
        last_tick = json.loads(last_tick_raw) if last_tick_raw else {}
        day_ago = now - timedelta(hours=24)
        passes = store.list_passes(since=day_ago)
        by_lens: dict[str, dict[str, int]] = {}
        for record in passes:
            by_lens.setdefault(record.lens, {}).setdefault(record.status, 0)
            by_lens[record.lens][record.status] += 1
        failures = [
            {"pass_id": r.pass_id, "lens": r.lens, "status": r.status, "error": r.error}
            for r in passes
            if r.status in {"failed", "invalid_output", "timed_out", "parked_quota"}
        ][:10]
        return {
            "state_dir": str(root),
            "engine_alive": age_minutes is not None and age_minutes < 30,
            "heartbeat_age_minutes": age_minutes,
            "running": heartbeat.get("running", []) if heartbeat else [],
            "waiting": last_tick.get("waiting", {}),
            "last_tick_errors": last_tick.get("errors", []),
            "sources": len(store.list_sources()),
            "sections": store.section_status_counts(),
            "claims": store.claim_counts(),
            "open_signals": len(store.list_signals(status="open", limit=100_000)),
            "insights": store.insight_counts(),
            "predictions": {
                status: len(store.list_predictions(status=status))
                for status in ("open", "hit", "miss", "untestable")
            },
            "rederivations": {
                "total": len(store.list_rederivations()),
                "agree": sum(1 for r in store.list_rederivations() if r.agrees),
            },
            "passes_24h": by_lens,
            "recent_failures": failures,
            "board": str(root / "board" / "board.md"),
            "headline": store.get_meta("board_headline"),
        }


def _cmd(args: argparse.Namespace, root: Path, *, harbor: object | None = None) -> int:
    target = state_dir(args)
    if not (target / DB_NAME).exists():
        print(f"no insight ledger at {target}; run `evallab insights init` first")
        return 2
    status = collect_status(target, now=datetime.now(UTC))
    if args.json:
        emit(status)
        return 0
    alive = "running" if status["engine_alive"] else "NOT running (no heartbeat in 30 min)"
    print(f"Insight engine: {alive}; state {status['state_dir']}")
    if status["headline"]:
        print(f"Board headline: {status['headline']}")
    print(f"Claims: {status['claims']}  Insights: {status['insights']}  "
          f"Open signals: {status['open_signals']}")
    print(f"Sections: {status['sections']}  Predictions: {status['predictions']}")
    running = status["running"]
    if isinstance(running, list) and running:
        print("Running now: " + ", ".join(f"{r['lens']} ({r['route']})" for r in running))
    waiting = status["waiting"]
    if isinstance(waiting, dict):
        for lens, reason in waiting.items():
            print(f"  {lens:<12} {reason}")
    failures = status["recent_failures"]
    if isinstance(failures, list) and failures:
        print("Recent failures:")
        for failure in failures:
            print(f"  {failure['lens']}: {failure['status']} {failure['error'] or ''}")
    print(f"Board: {status['board']}")
    return 0
