"""Live Supervisor CLI: ``evallab live-supervisor`` scan, serve, rows, replay, adjudicate and fleet-report.

Design: ``docs/live-supervisor.md``. This is the operator-facing layer: it selects sources
by name with lazy imports (so registration opens nothing), builds the rules decider or the
model reader, runs :func:`live_supervisor.scan_once` in **shadow** mode under one flock and
prints the report. Nothing is acted on: ``act`` mode is not reachable from here yet, LS-4
adds it together with its caps.

``scan`` is one cycle (the launchd shape, every 120 s); ``serve`` loops in one process under
``<root>/supervisor.lock``; ``replay`` and ``adjudicate`` forward their whole command line to
the sibling modules' ``main`` so those tools keep a single flag surface.
"""

from __future__ import annotations

import argparse
import fcntl
import importlib
import json
import math
import os
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from evallab.production import live_supervisor as ls

#: One cycle per launchd ``StartInterval``; the plist renders this interval.
DEFAULT_INTERVAL = 120.0
LOCK_FILE = "supervisor.lock"
MODULE_PREFIX = "evallab.production."
READERS = ("rules", "model")
#: A model reader without a daily cap is refused: no unattended run spends tokens uncapped.
CAP_REQUIRED = "--reader model needs --max-tokens-per-day (a model reader never runs uncapped)"
#: The ``omp usage`` headroom check is reused for this long, so one cycle pays for one check.
HEADROOM_TTL = timedelta(minutes=5)


class SupervisorAlreadyRunning(RuntimeError):
    """Another live-supervisor process holds ``<root>/supervisor.lock``."""


def utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- sources


@dataclass(frozen=True)
class SourceSpec:
    """One selectable source name and how to build it: no module import until ``build``."""

    name: str
    module: str
    attr: str
    #: Constructor keyword for a per-root state file, when the source keeps state of its own.
    state_kwarg: str | None = None
    state_file: str | None = None

    def build(self, root: Path) -> ls.Source:
        module = _import(self.module, f"source {self.name!r}")
        cls = _attr(module, self.attr, f"source {self.name!r}")
        if self.state_kwarg is None or self.state_file is None:
            return cls()
        return cls(**{self.state_kwarg: root / self.state_file})


SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec("harbor", "live_supervisor_harbor", "HarborSource"),
    SourceSpec("services", "live_supervisor_services", "LaunchdSource"),
    SourceSpec("modal", "live_supervisor_services", "ModalSource"),
    SourceSpec("processes", "live_supervisor_services", "ProcessSource"),
    SourceSpec("fleet", "live_supervisor_fleet", "FleetSource", "state_path", "fleet-state.json"),
)
SOURCE_NAMES: tuple[str, ...] = tuple(spec.name for spec in SOURCES)
_SPECS = {spec.name: spec for spec in SOURCES}


def _import(module: str, what: str) -> Any:
    """Import a sibling module lazily; a missing one is an error, not a crash."""
    dotted = f"{MODULE_PREFIX}{module}"
    try:
        return importlib.import_module(dotted)
    except ModuleNotFoundError as exc:
        if exc.name != dotted:
            raise
        raise ValueError(f"{what} is not available in this checkout: {dotted} is missing") from None


def _attr(module: Any, name: str, what: str) -> Any:
    try:
        return getattr(module, name)
    except AttributeError:
        raise ValueError(f"{what} changed: {module.__name__}.{name} is missing") from None


def specs_for(names: Sequence[str]) -> list[SourceSpec]:
    """The :class:`SourceSpec` for each name, in order; unknown or repeated names are errors."""
    chosen: list[SourceSpec] = []
    for name in names:
        spec = _SPECS.get(name)
        if spec is None:
            raise ValueError(f"source {name!r} not in {', '.join(SOURCE_NAMES)}")
        if spec in chosen:
            raise ValueError(f"source {name!r} repeated")
        chosen.append(spec)
    if not chosen:
        raise ValueError(f"at least one source is required ({', '.join(SOURCE_NAMES)})")
    return chosen


def build_sources(names: Sequence[str], *, root: Path) -> list[ls.Source]:
    """One instance per named source; constructs nothing until called."""
    return [spec.build(root) for spec in specs_for(names)]


# --------------------------------------------------------------------------- decider


class HeadroomGatedDecider:
    """A model reader that sends nothing while a reader route's window is at 90% or more.

    The check reuses :func:`run_atlas_watch.routes_with_headroom` (the ``omp usage`` contract)
    at most once per ``ttl``, so one cycle pays for one check. A refusal becomes an
    ``alert_research`` naming the blocked provider, never a rule-table fallback kill.
    """

    def __init__(self, decider: ls.Decider, *, models: Sequence[str],
                 check: Callable[[Sequence[str]], tuple[list[str], list[str], str]] | None = None,
                 clock: Callable[[], datetime] = utcnow, ttl: timedelta = HEADROOM_TTL) -> None:
        self.decider = decider
        self.models = tuple(models)
        self.check = check or _headroom_check()
        self.clock = clock
        self.ttl = ttl
        self._checked_at: datetime | None = None
        self._blocked: str | None = None

    def blocked(self) -> str | None:
        """Why no reader request may be sent right now, or None; refreshed once per TTL."""
        moment = self.clock()
        if self._checked_at is None or moment - self._checked_at >= self.ttl:
            allowed, excluded, mode = self.check(self.models)
            missing = [route for route in self.models if route not in set(allowed)]
            self._checked_at = moment
            self._blocked = (f"{', '.join(missing)} at or above the 90% window "
                             f"({', '.join(excluded) or 'blocked'}; {mode})") if missing else None
        return self._blocked

    def decide(self, observation: ls.Observation, hits: Sequence[ls.TriggerHit],
               proposal: ls.Decision) -> ls.Decision:
        problem = self.blocked()
        if problem is not None:
            return ls.Decision("alert_research", "other", f"reader unavailable: {problem}")
        return self.decider.decide(observation, hits, proposal)


def _headroom_check() -> Callable[[Sequence[str]], tuple[list[str], list[str], str]]:
    """The ``omp usage`` headroom check, imported on first use (never at registration)."""
    from evallab.production.run_atlas_watch import routes_with_headroom

    return routes_with_headroom


def _model_decider(root: Path, daily_token_budget: int) -> tuple[ls.Decider, Callable[[], None]]:
    """The model reader plus a closer for the gateway child it starts on the first call."""
    from evallab.production.run_atlas import GatewayTransport

    cls = _attr(_import("live_supervisor_triage", "the model reader"), "ModelDecider", "the model reader")
    transport = GatewayTransport()
    try:
        return cls(transport, root, daily_token_budget=daily_token_budget), transport.close
    except BaseException:
        transport.close()
        raise


@contextmanager
def decider_for(reader: str, *, root: Path, daily_token_budget: int | None = None,
                override: ls.Decider | None = None, check: Callable[..., Any] | None = None,
                clock: Callable[[], datetime] = utcnow) -> Iterator[ls.Decider]:
    """``rules`` (free, the rule table) or ``model`` (triage/decide, capped, headroom-gated)."""
    if override is not None:
        yield override
        return
    if reader not in READERS:
        raise ValueError(f"reader {reader!r} not in {', '.join(READERS)}")
    if reader == "rules":
        yield ls.RuleDecider()
        return
    if daily_token_budget is None:
        raise ValueError(CAP_REQUIRED)
    if daily_token_budget < 0:
        raise ValueError("--max-tokens-per-day must not be negative")
    decider, close = _model_decider(root, daily_token_budget)
    models = [route for route in (getattr(decider, attr, None)
                                  for attr in ("triage_model", "decide_model", "deep_model"))
              if isinstance(route, str) and route]
    try:
        yield HeadroomGatedDecider(decider, models=models, check=check, clock=clock) if models else decider
    finally:
        close()


# --------------------------------------------------------------------------- lock


@contextmanager
def hold_lock(path: Path) -> Iterator[None]:
    """Exclusive non-blocking flock for the whole run; the lock file is never unlinked."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SupervisorAlreadyRunning(f"another live-supervisor process holds {path}") from exc
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid {os.getpid()} at {utcnow():%Y-%m-%dT%H:%M:%SZ}\n")
            handle.flush()
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# --------------------------------------------------------------------------- cycles


def _window_log(root: Path) -> ls.WindowSink:
    """The ``windows-v1.jsonl`` writer under ``root``, imported on use (it pulls in the adjudicator)."""
    return _attr(_import("live_supervisor_windows", "the window log"), "WindowLog", "the window log")(root)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _payload(report: ls.ScanReport, *, sources: Sequence[ls.Source], moment: datetime) -> dict[str, Any]:
    return {**report.as_dict(), "mode": "shadow", "sources": [source.name for source in sources],
            "records": report.records, "observed_at": _stamp(moment)}


def run_scan(*, root: Path, names: Sequence[str] = (), reader: str = "rules",
             daily_token_budget: int | None = None, now: datetime | None = None,
             sources: Sequence[ls.Source] | None = None,
             decider: ls.Decider | None = None) -> dict[str, Any]:
    """One shadow-mode cycle under the lock; returns the printed report payload."""
    moment = now or utcnow()
    root = root.expanduser()
    with hold_lock(root / LOCK_FILE):
        chosen = list(sources) if sources is not None else build_sources(names, root=root)
        with decider_for(reader, root=root, daily_token_budget=daily_token_budget, override=decider) as active:
            report = ls.scan_once(chosen, active, ls.DecisionLog(root), now=moment, windows=_window_log(root))
    return _payload(report, sources=chosen, moment=moment)


def serve(*, root: Path, names: Sequence[str] = (), interval: float = DEFAULT_INTERVAL,
          reader: str = "rules", daily_token_budget: int | None = None, cycles: int | None = None,
          clock: Callable[[], datetime] = utcnow, sleep: Callable[[float], None] = time.sleep,
          emit: Callable[[dict[str, Any]], None] | None = None,
          sources: Sequence[ls.Source] | None = None,
          decider: ls.Decider | None = None) -> int:
    """Scan every ``interval`` seconds under one lock for the process lifetime.

    ``cycles`` bounds the loop (tests and one-off batches); ``None`` means forever. Sources
    and the decider are built once, so a model gateway and the sources' own state persist
    across cycles.
    """
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("--interval must be finite and positive")
    if cycles is not None and cycles < 1:
        raise ValueError("--cycles must be positive")
    root = root.expanduser()
    say = emit or print_cycle
    with hold_lock(root / LOCK_FILE):
        chosen = list(sources) if sources is not None else build_sources(names, root=root)
        with decider_for(reader, root=root, daily_token_budget=daily_token_budget, override=decider) as active:
            log, windows = ls.DecisionLog(root), _window_log(root)
            done = 0
            while cycles is None or done < cycles:
                moment = clock()
                report = ls.scan_once(chosen, active, log, now=moment, windows=windows)
                say(_payload(report, sources=chosen, moment=moment))
                done += 1
                if cycles is not None and done >= cycles:
                    break
                sleep(interval)
    return 0


def print_cycle(payload: dict[str, Any]) -> None:
    """One JSON object per cycle, flushed: the launchd log is a JSONL report."""
    print(json.dumps(payload, sort_keys=True), flush=True)


def print_report(payload: dict[str, Any], *, as_json: bool) -> None:
    """``--json`` prints the payload; otherwise one summary line, one line per record."""
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return
    actions = payload.get("actions") or {}
    print(f"live-supervisor scan ({payload['mode']}): observed={payload['observed']} "
          f"quiet={payload['quiet']} terminal={payload['terminal']} "
          f"suppressed={payload['suppressed']} recorded={payload['recorded']} "
          f"actions={json.dumps(actions, sort_keys=True)} "
          f"sources={','.join(payload['sources'])}")
    for record in payload["records"]:
        print(f"  {record['action']} {record['kind']} {record['run_id']}: {record['summary']}")
    for name, error in payload["errors"].items():
        print(f"  source {name} failed: {error}", file=sys.stderr)


# --------------------------------------------------------------------------- delegation


def _forward(args: argparse.Namespace, options: Sequence[tuple[str, str, bool]]) -> list[str]:
    """Rebuild the sibling tool's argv from parsed flags, then any ``--`` extras verbatim."""
    argv: list[str] = []
    for attr, flag, multi in options:
        value = getattr(args, attr, None)
        if value is None:
            continue
        for item in list(value) if multi else [value]:
            if isinstance(item, bool):
                if item:
                    argv.append(flag)
            else:
                argv += [flag, str(item)]
    extra = list(getattr(args, "extra", None) or ())
    if extra[:1] == ["--"]:  # argparse hands the separator to REMAINDER; it is not a flag
        extra = extra[1:]
    return argv + extra


REPLAY_OPTIONS: tuple[tuple[str, str, bool], ...] = (
    ("manifest", "--manifest", False),
    ("out", "--out", False),
    ("set", "--set", True),
    ("limit_per_set", "--limit-per-set", False),
    ("workers", "--workers", False),
    ("score_out", "--score-out", False),
    ("findings_out", "--findings-out", False),
    ("store_copy", "--store-copy", False),
)
ADJUDICATE_OPTIONS: tuple[tuple[str, str, bool], ...] = (
    ("rows", "--rows", False),
    ("windows", "--windows", False),
    ("out", "--out", False),
    ("model", "--model", False),
    ("limit", "--limit", False),
    ("per_cell", "--per-cell", False),
    ("seed", "--seed", False),
    ("score_out", "--score-out", False),
)


def replay_argv(args: argparse.Namespace) -> list[str]:
    return _forward(args, REPLAY_OPTIONS)


def adjudicate_argv(args: argparse.Namespace) -> list[str]:
    return _forward(args, ADJUDICATE_OPTIONS)


def delegate(module: str, argv: Sequence[str], *, what: str) -> int:
    """Run a sibling module's ``main(argv)``; its flags and exit codes are the tool's own."""
    entry = getattr(_import(module, what), "main", None)
    if entry is None:
        raise ValueError(f"{what} has no main(): expected {MODULE_PREFIX}{module}.main(argv)")
    return int(entry(list(argv)))


# --------------------------------------------------------------------------- alarms

ALARM_CURSOR = "alarm-cursor.json"
ALARM_CANDIDATE = "alarm-cursor.candidate.json"
ROUTE_LOCK = "route-alarms.lock"


def _trial_dir(record: Mapping[str, Any], roots: Sequence[Path]) -> Path | None:
    """The live trial directory behind an alarm record, when one of ``roots`` holds it."""
    raw_subject = record.get("subject")
    subject = raw_subject if isinstance(raw_subject, Mapping) else {}
    run_id = str(record.get("run_id") or "")
    names = [str(subject.get("id") or ""), run_id]
    if run_id.startswith("live:"):
        names.append(run_id.split(":", 2)[-1])
    for name in names:
        relative = Path(name)
        if not name or relative.is_absolute() or ".." in relative.parts:
            continue
        for root in roots:
            trial = root / relative
            if (trial / "agent").is_dir():
                return trial
    return None


def _alarm_metric(record: Mapping[str, Any]) -> dict[str, Any]:
    """The ``metrics['trace_alarms']`` shape ``trial_observation`` writes (card input)."""
    score = record.get("score")
    return {"detector": str(record.get("detector") or ""),
            "score": float(score) if isinstance(score, (int, float)) else 1.0,
            "summary": str(record.get("summary") or "")[:200],
            "evidence_steps": [s for s in record.get("evidence_steps") or () if isinstance(s, int)][:5]}


def _alarm_subject(record: Mapping[str, Any], *, live: bool) -> ls.Subject:
    """The subject behind an alarm; never killable, because routing only cards and queues."""
    raw_subject = record.get("subject")
    subject = raw_subject if isinstance(raw_subject, Mapping) else {}
    owner = subject.get("owner")
    return ls.Subject(
        kind=str(subject.get("kind") or ("harbor_trial" if live else "stored_run")),
        id=str(subject.get("id") or record.get("run_id") or ""),
        owner=owner if owner in ls.OWNERS else "other",
        run_id=str(record.get("run_id") or ""),
        label=str(subject.get("label") or ""),
    )


def flagged(record: Mapping[str, Any], *, now: datetime,
            trial_roots: Sequence[Path] = ()) -> tuple[ls.Observation, ls.Decision]:
    """One cheat alarm as ``(observation, flag_cheating)`` with real step excerpts only.

    A live trial under ``trial_roots`` supplies the events (``agent/trajectory.json`` through
    :func:`live_supervisor_harbor.trial_observation`); without one there are no events, the
    alarm row itself is the evidence, and the card says to open the run.
    """
    trial = _trial_dir(record, trial_roots)
    subject = _alarm_subject(record, live=trial is not None)
    metric = _alarm_metric(record)
    if trial is None:
        observation = ls.Observation(subject, now, (), {"trace_alarms": [metric]},
                                     actor_inputs_available=False)
    else:
        from evallab.production import run_health

        harbor = _import("live_supervisor_harbor", "the Harbor source")
        run = run_health.trial_run(trial, run_id=subject.finding_run_id)
        observation = harbor.trial_observation(run, subject=subject, now=now, trace_alarms=(record,))
    detector = metric["detector"] or "alarm"
    summary = " ".join(str(record.get("summary") or "").split())
    decision = ls.Decision(
        "flag_cheating", "cheat", f"{detector}: {summary}".rstrip(": "),
        reader_model=str(record.get("reader_model") or "") or None,
        evidence_steps=tuple(metric["evidence_steps"])[: ls.MAX_EVIDENCE],
    )
    return observation, decision


def route_alarms(*, root: Path, findings: Sequence[Path] | None = None, queue: Path | None = None,
                 state_root: Path | None = None, trial_roots: Sequence[Path] | None = None,
                 replay_existing: bool = False, now: datetime | None = None,
                 subscriber: Callable[..., list[dict[str, Any]]] | None = None,
                 router: Any = None) -> dict[str, Any]:
    """Route new ``kind=cheat`` alarms to the Trace Watcher; advance the cursor only after routing.

    ``root`` holds the cursor and the cards; ``state_root`` is the production state root that
    holds ``trace-harness/`` and ``run-health/`` (default: the cheat module's ``STATE_ROOT``),
    never the supervisor root itself. The first start begins at EOF (history is consumed, not
    routed) unless ``replay_existing``. The cursor is read into a candidate file and replaces
    the durable one only once the router returns, so a crash replays the same records and the
    router's per-run dedupe prevents a second card. Nothing is paged and no document is
    edited: cards and a queue line, that is all.
    """
    moment = now or utcnow()
    root = root.expanduser()
    cheat = _import("live_supervisor_cheat", "the cheat router")
    state = (Path(state_root) if state_root is not None else Path(cheat.STATE_ROOT)).expanduser()
    paths = ([Path(p).expanduser() for p in findings] if findings is not None
             else [Path(p).expanduser() for p in cheat.default_findings_paths(state)])
    queue_path = (Path(queue) if queue is not None else Path(cheat.DEFAULT_QUEUE)).expanduser()
    roots = ([Path(r).expanduser() for r in trial_roots] if trial_roots is not None
             else [Path(r).expanduser() for r in
                   _import("live_supervisor_harbor", "the Harbor source").default_roots()])
    subscribe = subscriber or cheat.subscribe_alarms
    root.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"observed_at": _stamp(moment), "findings": [str(p) for p in paths],
                              "queue": str(queue_path)}
    with hold_lock(root / ROUTE_LOCK):  # launchd ticks may overlap: one pass at a time
        return _route_pass(root=root, paths=paths, subscribe=subscribe, cheat=cheat,
                           queue_path=queue_path, roots=roots, router=router, moment=moment,
                           replay_existing=replay_existing, report=report)


def _route_pass(*, root: Path, paths: Sequence[Path], subscribe: Callable[..., list[dict[str, Any]]],
                cheat: Any, queue_path: Path, roots: Sequence[Path], router: Any, moment: datetime,
                replay_existing: bool, report: dict[str, Any]) -> dict[str, Any]:
    """One cursor read, route and cursor advance; the caller holds ``ROUTE_LOCK``."""
    cursor, candidate = root / ALARM_CURSOR, root / ALARM_CANDIDATE
    if not cursor.exists() and not replay_existing:
        candidate.unlink(missing_ok=True)
        history = subscribe(paths, since_offsets_state=candidate)
        candidate.replace(cursor)
        return {**report, "started_at_eof": True, "alarms": len(history), "cheat": 0, "cards": []}
    candidate.write_text(cursor.read_text(encoding="utf-8") if cursor.exists()
                         else json.dumps({"files": {}}), encoding="utf-8")
    try:
        records = subscribe(paths, since_offsets_state=candidate)
        cheats = [record for record in records if record.get("kind") == "cheat"]
        engine = router if router is not None else cheat.CheatRouter(
            root, queue_path=queue_path, findings_paths=paths, trial_roots=roots)
        cards = [str(path) for path in
                 engine.route([flagged(record, now=moment, trial_roots=roots) for record in cheats])]
    except BaseException:
        candidate.unlink(missing_ok=True)
        raise
    candidate.replace(cursor)
    return {**report, "started_at_eof": False, "alarms": len(records), "cheat": len(cheats),
            "cards": cards}


def print_route(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return
    print(f"live-supervisor route-alarms: alarms={payload['alarms']} cheat={payload['cheat']} "
          f"cards={len(payload['cards'])} queue={payload['queue']}"
          + (" (started at EOF)" if payload["started_at_eof"] else ""))
    for card in payload["cards"]:
        print(f"  {card}")


# --------------------------------------------------------------------------- commands


def _scan_command(args: argparse.Namespace, root: Path, *, harbor: Any = None) -> int:
    del root, harbor
    payload = run_scan(names=args.source or SOURCE_NAMES, root=args.root, reader=args.reader,
                       daily_token_budget=args.max_tokens_per_day)
    print_report(payload, as_json=args.json)
    return 0


def _serve_command(args: argparse.Namespace, root: Path, *, harbor: Any = None) -> int:
    del root, harbor
    try:
        return serve(names=args.source or SOURCE_NAMES, root=args.root, interval=args.interval,
                     reader=args.reader, daily_token_budget=args.max_tokens_per_day)
    except KeyboardInterrupt:
        print("live-supervisor serve stopped", file=sys.stderr)
        return 0


def _replay_command(args: argparse.Namespace, root: Path, *, harbor: Any = None) -> int:
    del root, harbor
    return delegate("live_supervisor_replay", replay_argv(args), what="the replay harness")


def _rows_command(args: argparse.Namespace, root: Path, *, harbor: Any = None) -> int:
    del root, harbor
    export = _attr(_import("live_supervisor_windows", "the window log"), "export_rows", "the window log")
    try:
        summary = export(args.root, args.arm, args.out)
    except FileNotFoundError as exc:
        print(f"live-supervisor rows: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, sort_keys=True))
    return 0


def _adjudicate_command(args: argparse.Namespace, root: Path, *, harbor: Any = None) -> int:
    del root, harbor
    return delegate("live_supervisor_adjudicate", adjudicate_argv(args), what="the adjudicator")


def _route_alarms_command(args: argparse.Namespace, root: Path, *, harbor: Any = None) -> int:
    del root, harbor
    if args.interval is not None and not (math.isfinite(args.interval) and args.interval > 0):
        raise ValueError("--interval must be finite and positive")
    options = {"root": args.root, "findings": args.findings, "queue": args.queue,
               "state_root": args.state_root, "trial_roots": args.trial_root}
    replay = args.replay_existing
    try:
        while True:
            try:
                payload = route_alarms(**options, replay_existing=replay)
            except SupervisorAlreadyRunning:
                print("route-alarms: previous pass still running; skipped", file=sys.stderr)
                if args.once or args.interval is None:
                    return 0
                time.sleep(args.interval)
                continue
            print_route(payload, as_json=args.json)
            replay = False  # the history is consumed by the first pass
            if args.once or args.interval is None:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("live-supervisor route-alarms stopped", file=sys.stderr)
        return 0


def _fleet_report_command(args: argparse.Namespace, root: Path, *, harbor: Any = None) -> int:
    del root, harbor
    report = _import("live_supervisor_fleet_report", "the fleet report")
    bridge = args.bridge if args.bridge is not None else _attr(report, "DEFAULT_BRIDGE", "the fleet report")
    if args.out is not None and not args.write:
        print("live-supervisor fleet-report: --out names the file --write updates; add --write", file=sys.stderr)
        return 2
    if not (math.isfinite(args.window_hours) and args.window_hours > 0):
        print("live-supervisor fleet-report: --window-hours must be finite and positive", file=sys.stderr)
        return 2
    updated: Path | None = None
    try:
        data, text = report.run_fleet_report(bridge, window_hours=args.window_hours)
        if args.write:
            updated = (args.out if args.out is not None else bridge / report.TARGET_FILE).expanduser()
            report.write_section(updated, text)
    except (OSError, TypeError, ValueError) as exc:
        print(f"live-supervisor fleet-report: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(data, indent=2, sort_keys=True) if args.json else text)
    if updated is not None:
        print(f"live-supervisor fleet-report: updated {updated}", file=sys.stderr)
    return 0


def _add_cycle_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", action="append", choices=SOURCE_NAMES,
                        help="source to observe; repeatable (default: every source)")
    parser.add_argument("--reader", choices=READERS, default="rules",
                        help="rules = the rule table, $0 (default); model = triage and decide")
    parser.add_argument("--root", type=Path, default=ls.DEFAULT_ROOT,
                        help=f"state root for decisions and locks (default: {ls.DEFAULT_ROOT})")
    parser.add_argument("--max-tokens-per-day", type=int,
                        help="UTC input+output cap; required with --reader model")


def register_parser(commands: Any) -> None:
    """``evallab live-supervisor``; registration imports no source module and opens nothing."""
    parser = commands.add_parser(
        "live-supervisor", help="Live Supervisor: observe live work, decide in shadow mode, record",
        description=(
            "Shadow mode only: every decision is a trace-findings/v1 record under the state "
            "root and nothing is acted on. Actions arrive after adjudicated precision (LS-4)."
        ),
    )
    actions = parser.add_subparsers(dest="live_supervisor_command", required=True)
    scan = actions.add_parser("scan", help="one shadow-mode cycle, then print the report")
    _add_cycle_options(scan)
    scan.add_argument("--json", action="store_true", help="print one JSON object instead of lines")
    scan.set_defaults(func=_scan_command)
    serving = actions.add_parser("serve", help="scan every --interval seconds under <root>/supervisor.lock")
    _add_cycle_options(serving)
    serving.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                         help=f"seconds between cycles (default: {DEFAULT_INTERVAL:.0f})")
    serving.set_defaults(func=_serve_command)
    replay = actions.add_parser("replay", help="replay labelled cases through the rule decider")
    replay.add_argument("--manifest", type=Path, help="case manifest (default: the replay manifest-v1)")
    replay.add_argument("--out", type=Path, required=True, help="decision rows JSONL")
    replay.add_argument("--set", action="append", help="only this case set; repeatable")
    replay.add_argument("--limit-per-set", type=int)
    replay.add_argument("--workers", type=int)
    replay.add_argument("--score-out", type=Path)
    replay.add_argument("--findings-out", type=Path)
    replay.add_argument("--store-copy", action="store_true",
                        help="resolve store traces through a local copy")
    replay.add_argument("extra", nargs=argparse.REMAINDER,
                        help="further flags for the harness, after --")
    replay.set_defaults(func=_replay_command)
    adjudicate = actions.add_parser("adjudicate", help="blind adjudication of sampled decisions")
    adjudicate.add_argument("--rows", type=Path, required=True, help="decision rows JSONL")
    adjudicate.add_argument("--windows", type=Path, required=True, help="observation windows JSONL")
    adjudicate.add_argument("--out", type=Path, required=True, help="adjudicated rows JSONL")
    adjudicate.add_argument("--model", help="adjudicator route (default: the adjudicator's)")
    adjudicate.add_argument("--limit", type=int, help="sample size cap")
    adjudicate.add_argument("--per-cell", type=int)
    adjudicate.add_argument("--seed", type=int)
    adjudicate.add_argument("--score-out", type=Path, help="score JSON (default: <out stem>-score.json)")
    adjudicate.add_argument("extra", nargs=argparse.REMAINDER,
                            help="further flags for the adjudicator, after --")
    adjudicate.set_defaults(func=_adjudicate_command)
    rows = actions.add_parser(
        "rows", help="turn live decisions and their windows into adjudicator rows, one arm per file")
    rows.add_argument("--root", type=Path, default=ls.DEFAULT_ROOT,
                      help=f"state root holding decisions-v1.jsonl and windows-v1.jsonl (default: {ls.DEFAULT_ROOT})")
    rows.add_argument("--arm", choices=("model", "rules"), required=True,
                      help="model = decisions a model reader made; rules = the rule-table proposal")
    rows.add_argument("--out", type=Path, required=True,
                      help="rows JSONL for `adjudicate --rows`; pass <root>/windows-v1.jsonl as --windows")
    rows.set_defaults(func=_rows_command)
    alarms = actions.add_parser(
        "route-alarms", help="route new cheat alarms to the Trace Watcher queue (cards + one queue line)")
    alarms.add_argument("--findings", type=Path, action="append",
                        help="trace-findings/v1 JSONL to tail; repeatable (default: trace-harness plus Run Health)")
    alarms.add_argument("--state-root", type=Path,
                        help="production state root holding trace-harness/ and run-health/ "
                             "(default: ~/.local/state/evallab-prod-live)")
    alarms.add_argument("--queue", type=Path,
                        help="queue-append.jsonl for the Trace Watcher item (default: the bridge queue)")
    alarms.add_argument("--trial-root", type=Path, action="append",
                        help="runs root holding <job>/<trial>; repeatable (default: the Harbor roots)")
    alarms.add_argument("--root", type=Path, default=ls.DEFAULT_ROOT,
                        help=f"state root for cards and the cursor (default: {ls.DEFAULT_ROOT})")
    alarms.add_argument("--replay-existing", action="store_true",
                        help="route history on the first pass instead of starting at EOF")
    pacing = alarms.add_mutually_exclusive_group()
    pacing.add_argument("--once", action="store_true", help="one pass (the default; the launchd shape)")
    pacing.add_argument("--interval", type=float, help="seconds between passes; loops until interrupted")
    alarms.add_argument("--json", action="store_true", help="print one JSON object per pass")
    alarms.set_defaults(func=_route_alarms_command)
    fleet = actions.add_parser(
        "fleet-report",
        help="deterministic $0 fleet-health report (idle lane-hours, item durations, blocked loops) from the keeper log",
    )
    fleet.add_argument("--bridge", type=Path,
                       help="bridge run directory holding _queue.log, queue.json and queue-state.json "
                            "(default: ~/Developer/research-context/bridge/2026-10-27-run-atlas)")
    fleet.add_argument("--window-hours", type=float, default=24.0,
                       help="hours before now that the report covers (default: 24)")
    fleet.add_argument("--out", type=Path,
                       help="document whose fleet-health region --write updates (default: <bridge>/live-supervisor.md)")
    fleet.add_argument("--write", action="store_true",
                       help="update the marked fleet-health region of the document; otherwise only print")
    fleet.add_argument("--json", action="store_true", help="print the numbers as JSON instead of the markdown section")
    fleet.set_defaults(func=_fleet_report_command)
