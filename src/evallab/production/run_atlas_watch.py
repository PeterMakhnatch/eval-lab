"""Poll committed cloud traces for missing, admitted DEV Run Atlas facets.

The DB, not a production journal event, is the attachment point: upsert_trace
and corpus_expansion's transactional imports do not publish a shared committed
trace event. Public replay's run.completed identifies a replay session, not a
cloud trace_id. Polling therefore also catches imports and restarts.

``add_watch_parser`` only registers arguments; the parent atlas parser calls it.
The launchd template under scripts/ops/launchd follows the existing KeepAlive
convention and is deliberately not installed or loaded by this module.

The optional UTC daily budget sums actual input/output token receipts and
reserves a conservative UTF-8 prompt bound plus the requested output ceiling
before each concurrent call. Requests that do not fit wait until a later cycle;
budget refusals are not trace failures. Provider windows from ``omp usage
--json`` bench providers at 90%; unavailable usage falls back to RoutePool's
quota benching. No paid/key-backed routes are permitted.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.production import run_atlas
from evallab.production.corpus_queue import CorpusRegistry

DEFAULT_MAX_FAILURES = 3
USAGE_TIMEOUT_SECONDS = 30


class WatchAlreadyRunning(RuntimeError):
    """Another watcher holds the process-lifetime flock."""


class DailyTokenBudgetExceeded(run_atlas.TransportError):
    """A request cannot fit within the remaining daily token reservation."""


@contextmanager
def watch_lock(root: Path) -> Iterator[None]:
    """Hold one inode for the whole watcher lifetime; never unlink a lock file."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / "watch.lock").open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WatchAlreadyRunning(f"Run Atlas watcher already running: {root / 'watch.lock'}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def load_failures(path: Path) -> dict[str, int]:
    """A malformed state must not silently clear the retry ceiling."""
    if not path.exists():
        return {}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("version") != 1:
        raise ValueError(f"invalid watcher state: {path}")
    failures = state.get("failures")
    if not isinstance(failures, dict) or any(
        not isinstance(key, str) or type(value) is not int or value < 0
        for key, value in failures.items()
    ):
        raise ValueError(f"invalid watcher failures: {path}")
    return failures


def save_failures(path: Path, failures: Mapping[str, int]) -> None:
    """Replace state atomically on the same filesystem, with private permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump({"version": 1, "failures": failures}, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def pending_trace_ids(
    database: Path, *, registry: CorpusRegistry, facets: Path,
    failures: Mapping[str, int], max_failures: int,
) -> list[str]:
    """Reapply cross-key admission every cycle, then prefer newest insertions."""
    done = run_atlas.load_facets(facets)
    connection = run_atlas.connect_readonly(database)
    try:
        admitted = run_atlas.admitted_task_ids(connection, registry)
        eligible = run_atlas.cloud_trace_ids(connection, admitted=admitted)
        pending = {
            trace_id for trace_id in eligible
            if trace_id not in done and failures.get(trace_id, 0) < max_failures
        }
        try:
            rows = connection.execute("SELECT trace_id FROM agent_traces ORDER BY rowid DESC")
            return [row["trace_id"] for row in rows if row["trace_id"] in pending]
        except sqlite3.OperationalError as exc:
            if "no such column: rowid" not in str(exc).lower():
                raise
            # WITHOUT ROWID schemas still retain cloud_trace_ids' stable order.
            return [trace_id for trace_id in eligible if trace_id in pending]
    finally:
        connection.close()


def _call_rows(path: Path, *, offset: int = 0) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        handle.seek(offset)
        for line in handle:
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"invalid call receipt: {path}")
                yield value


def tokens_used_today(path: Path, *, now: datetime | None = None) -> int:
    """Sum call receipts, not the unmetered purpose=run summary rows, in UTC."""
    today = (now or datetime.now(UTC)).astimezone(UTC).date()
    tokens = 0
    for row in _call_rows(path):
        stamp = row.get("at")
        if not isinstance(stamp, str):
            continue
        instant = datetime.fromisoformat(stamp)
        if instant.tzinfo is None:
            raise ValueError(f"call receipt has no timezone: {path}")
        if instant.astimezone(UTC).date() != today:
            continue
        for field in ("input_tokens", "output_tokens"):
            count = row.get(field, 0)
            if type(count) is not int or count < 0:
                raise ValueError(f"invalid {field} in call receipt: {path}")
            tokens += count
    return tokens


class _BudgetTransport:
    """Reserve concurrent requests before the extractor emits their receipts."""

    def __init__(self, transport: run_atlas.GatewayTransport, *, used: int, maximum: int) -> None:
        self.transport = transport
        self.used = used
        self.maximum = maximum
        self.reserved = 0
        self.exhausted = False
        self.lock = threading.Lock()

    def complete(
        self, model: str, messages: Sequence[Mapping[str, str]], *, max_tokens: int,
    ) -> run_atlas.Completion:
        # One byte per token is deliberately pessimistic. Include generous chat
        # framing overhead; no tokenizer or additional paid request is needed.
        prompt_bound = 1024 + sum(
            len(json.dumps(dict(message), ensure_ascii=False).encode("utf-8")) + 256
            for message in messages
        )
        reservation = prompt_bound + max_tokens
        with self.lock:
            if self.used + self.reserved + reservation > self.maximum:
                self.exhausted = True
                raise DailyTokenBudgetExceeded("request exceeds remaining UTC daily token budget")
            self.reserved += reservation
        completion = None
        try:
            completion = self.transport.complete(model, messages, max_tokens=max_tokens)
            return completion
        finally:
            with self.lock:
                self.reserved -= reservation
                # Unknown usage on a failed request remains reserved conservatively
                # for this cycle; successful requests settle to provider receipts.
                self.used += (
                    reservation if completion is None
                    else completion.input_tokens + completion.output_tokens
                )


def routes_with_headroom(routes: Sequence[str]) -> tuple[list[str], list[str], str]:
    """Use the observed omp usage JSON contract, without printing account data."""
    try:
        result = subprocess.run(
            ["omp", "usage", "--json"], check=True, capture_output=True, text=True,
            timeout=USAGE_TIMEOUT_SECONDS,
        )
        snapshot = json.loads(result.stdout)
        reports = snapshot["reports"]
        if not isinstance(reports, list):
            raise ValueError("usage reports must be a list")
        blocked = set()
        for report in reports:
            for limit in report.get("limits", []):
                if not limit.get("window"):
                    continue
                amount = limit.get("amount", {})
                used = amount.get("usedFraction")
                if used is None and amount.get("limit", 0) > 0:
                    used = amount.get("used", 0) / amount["limit"]
                if isinstance(used, int | float) and used >= 0.9:
                    blocked.add(report["provider"])
        allowed = [route for route in routes if route.partition("/")[0] not in blocked]
        return allowed, sorted(blocked), "omp-usage-json"
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError):
        # OMP versions/providers without usable reports still bench real quota
        # failures inside RoutePool; no key-backed route is substituted.
        return list(routes), [], "route-pool-quota-only"


def _ordered_runs(
    database: Path, registry: CorpusRegistry, trace_ids: Sequence[str],
) -> Iterator[run_atlas.AtlasRun]:
    # cloud_runs rechecks admission but returns trace-id order. One-id iterators
    # preserve insertion priority without loading every pending trajectory.
    for trace_id in trace_ids:
        yield from run_atlas.cloud_runs(database, registry=registry, trace_ids=[trace_id])


def watch_cycle(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    """One bounded cycle. Non-dry callers must hold ``watch_lock``."""
    state_path = run_atlas.ATLAS_ROOT / "watch-state.json"
    failures = load_failures(state_path)
    registry = CorpusRegistry(root)
    pending = pending_trace_ids(
        args.database, registry=registry, facets=args.facets,
        failures=failures, max_failures=args.max_failures,
    )
    used = tokens_used_today(args.calls)
    summary: dict[str, Any] = {
        "pending": len(pending), "selected": 0, "written": 0, "failed": 0,
        "tokens_today": used, "dry_run": args.dry_run,
    }
    if args.dry_run or not pending:
        return summary
    if args.max_tokens_per_day is not None and used >= args.max_tokens_per_day:
        return summary | {"budget_exhausted": True}
    routes, blocked, usage_mode = routes_with_headroom(args.routes or run_atlas.DEFAULT_ROUTES)
    summary.update(blocked_providers=blocked, usage_mode=usage_mode)
    if not routes:
        return summary | {"headroom_exhausted": True}
    selected = pending[:args.max_per_cycle]
    summary["selected"] = len(selected)
    offset = args.calls.stat().st_size if args.calls.exists() else 0
    transport = run_atlas.GatewayTransport()
    budget = (
        _BudgetTransport(transport, used=used, maximum=args.max_tokens_per_day)
        if args.max_tokens_per_day is not None else None
    )
    try:
        result = run_atlas.extract_runs(
            _ordered_runs(args.database, registry, selected),
            extractor=run_atlas.FacetExtractor(
                budget or transport, pattern=run_atlas.database_source_pattern(args.database),
            ),
            pool=run_atlas.RoutePool(routes, per_route=args.per_route),
            facets_path=args.facets, calls_path=args.calls, limit=args.max_per_cycle,
        )
    finally:
        transport.close()
    selected_ids = set(selected)
    for row in _call_rows(args.calls, offset=offset):
        run_id = row.get("run_id")
        if row.get("purpose") != "run" or run_id not in selected_ids:
            continue
        if row.get("status") == "ok":
            failures.pop(run_id, None)
        elif row.get("status") == "failed":
            if str(row.get("error", "")).startswith("DailyTokenBudgetExceeded:"):
                result["failed"] -= 1
                result["budget_skipped"] = result.get("budget_skipped", 0) + 1
            else:
                failures[run_id] = failures.get(run_id, 0) + 1
    save_failures(state_path, failures)
    summary.update(result)
    summary["tokens_today"] = tokens_used_today(args.calls)
    if budget is not None:
        summary["budget_exhausted"] = budget.exhausted or budget.used >= budget.maximum
    return summary


@contextmanager
def _stop_signals(stop: threading.Event) -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for sig in previous:
            signal.signal(sig, lambda _signum, _frame: stop.set())
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _watch_command(args: argparse.Namespace, root: Path, *, harbor: Any = None) -> int:
    del harbor
    if not 1 <= args.per_route <= 8:
        raise ValueError("--per-route must be between 1 and 8")
    if not math.isfinite(args.interval) or args.interval <= 0:
        raise ValueError("--interval must be finite and positive")
    if args.max_per_cycle < 1 or args.max_failures < 1:
        raise ValueError("--max-per-cycle and --max-failures must be positive")
    if args.max_tokens_per_day is not None and args.max_tokens_per_day < 0:
        raise ValueError("--max-tokens-per-day must not be negative")
    for route in args.routes or run_atlas.DEFAULT_ROUTES:
        run_atlas.require_subscription_route(route)
    # A read-only diagnostic never creates the state directory, lock or gateway,
    # and exits after one snapshot even when --once was not supplied.
    if args.dry_run:
        print(json.dumps(watch_cycle(args, root), sort_keys=True), flush=True)
        return 0
    stop = threading.Event()
    try:
        with watch_lock(run_atlas.ATLAS_ROOT), _stop_signals(stop):
            while not stop.is_set():
                summary = watch_cycle(args, root)
                print(json.dumps(summary, sort_keys=True), flush=True)
                if args.once:
                    return 0 if summary["failed"] == 0 else 1
                stop.wait(args.interval)
    except WatchAlreadyRunning as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 1
    return 0


def add_watch_parser(actions: Any) -> None:
    """Register ``evallab atlas watch`` without I/O or gateway startup."""
    parser = actions.add_parser(
        "watch", help="Automatically facet newly committed, admitted DEV cloud traces",
        description=__doc__,
    )
    parser.add_argument("--database", type=Path, default=run_atlas.DEFAULT_DATABASE)
    parser.add_argument("--facets", type=Path, default=run_atlas.DEFAULT_FACETS)
    parser.add_argument("--calls", type=Path, default=run_atlas.DEFAULT_CALLS)
    parser.add_argument("--route", dest="routes", action="append", help="Subscription route; repeatable")
    parser.add_argument("--per-route", type=int, default=1, help="Concurrent runs per route (1-8)")
    parser.add_argument("--interval", type=float, default=300, help="Seconds between cycles (default: 300)")
    parser.add_argument("--max-per-cycle", type=int, default=25)
    parser.add_argument("--max-failures", type=int, default=DEFAULT_MAX_FAILURES,
                        help="Stop retrying a trace after this many failed cycles (default: 3)")
    parser.add_argument("--max-tokens-per-day", type=int,
                        help="UTC input+output token budget; conservatively reserve each request")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="Print pending count once; no writes or model calls")
    parser.set_defaults(func=_watch_command)
