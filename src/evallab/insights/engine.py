"""The insight engine's scheduler: one ``tick`` moves every stage forward.

A tick (1) collects finished agent passes and imports their output through the
deterministic checks, (2) refreshes the read-only results mirror when due,
(3) scans sources for new or changed report sections, (4) recomputes
statistical signals over the whole claim ledger, (5) starts the next passes
the schedule allows, and (6) re-renders the board. ``watch`` runs ticks
forever under a single-instance lock; workers run as separate ``omp -p``
processes, so a restarted engine adopts or collects them instead of
starting duplicates.

Every scheduling decision is deterministic and recorded: the trigger, the
route and why it was chosen, and the model that actually served the pass.
Lens order matters when slots are scarce: grading and extraction come first
because every later stage waits on them.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import secrets
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ValidationError

from evallab.insights import board as board_mod
from evallab.insights import check as check_mod
from evallab.insights import recompute as recompute_mod
from evallab.insights import signals as signals_mod
from evallab.insights import sources as sources_mod
from evallab.insights.config import (
    EngineConfig,
    LensSchedule,
    RouteTable,
    load_config,
    load_routes,
    model_family,
    provider_of,
)
from evallab.insights.models import (
    BoardUpdate,
    ClaimExtraction,
    ClaimRecord,
    InsightBatch,
    InsightRecord,
    InsightStatus,
    Lens,
    PassRecord,
    PredictionRecord,
    RecomputeResult,
    RederivationRecord,
    RederiveBatch,
    RefereeBatch,
    claim_fingerprint,
    claim_identity,
    insight_identity,
    prediction_identity,
)
from evallab.insights.packs import OUTPUT_FILES, PassInputs, build_pass_dir
from evallab.insights.routes import choose_route
from evallab.insights.runner import (
    Launcher,
    OmpLauncher,
    UsageGate,
    WorkerHandle,
    WorkerSpec,
    attest_session,
    classify_failure,
    host_ok,
)
from evallab.insights.store import DB_NAME, InsightStore, utcnow

ANALYST_LENSES: tuple[Lens, ...] = ("skeptic", "anomaly", "connector", "theorist", "ideator")
#: Planning order when slots are scarce: grading and extraction unblock everything else.
PLAN_ORDER: tuple[Lens, ...] = (
    "referee",
    "extractor",
    "rederiver",
    "skeptic",
    "anomaly",
    "connector",
    "synthesizer",
    "theorist",
    "ideator",
)
WORKER_TOOLS = "read,bash,grep,glob,write"
TIMEOUT_GRACE = timedelta(minutes=5)
PARK_DEFAULT = timedelta(minutes=60)
ACTIVE_STATUSES = ("planned", "running")
REDERIVE_FRACTION = 0.2
_REFEREE_STATUS: dict[str, InsightStatus] = {
    "accept": "refereed",
    "revise": "needs_revision",
    "reject": "rejected",
}


class EngineBusy(RuntimeError):
    """Another engine process holds the state directory's lock."""


class UsageLike(Protocol):
    def used_percent(self, provider: str, model: str | None = None) -> float | None: ...


@dataclass
class TickReport:
    started_at: str
    finished_at: str = ""
    scanned: dict[str, object] = field(default_factory=dict)
    new_signals: int = 0
    collected: list[dict[str, str]] = field(default_factory=list)
    launched: list[dict[str, str]] = field(default_factory=list)
    waiting: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def rederive_sample(claim_ids: Iterable[str], fraction: float = REDERIVE_FRACTION) -> list[str]:
    """Deterministic sample: a claim is in it when its id hashes below ``fraction``.

    Membership depends only on the claim id, so a claim's selection never
    changes as the ledger grows. At least one claim is returned when any exist.
    """
    if not 0 < fraction <= 1:
        raise ValueError("fraction must be in (0, 1]")
    ranked = sorted(
        (int(hashlib.sha256(claim_id.encode()).hexdigest()[:12], 16) / 16**12, claim_id)
        for claim_id in set(claim_ids)
    )
    chosen = [claim_id for score, claim_id in ranked if score < fraction]
    if not chosen and ranked:
        chosen = [ranked[0][1]]
    return sorted(chosen)


@contextmanager
def engine_lock(state_root: Path) -> Iterator[None]:
    state_root.mkdir(parents=True, exist_ok=True)
    handle = (state_root / "engine.lock").open("a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise EngineBusy(f"another insight engine holds {state_root / 'engine.lock'}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        yield
    finally:
        with suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", "utf-8")
    os.replace(tmp, path)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _primary_model(served: Mapping[str, int]) -> str | None:
    if not served:
        return None
    return max(sorted(served), key=lambda model: served[model])


class Engine:
    def __init__(
        self,
        state_root: Path,
        *,
        config: EngineConfig | None = None,
        routes: RouteTable | None = None,
        launcher: Launcher | None = None,
        usage: UsageLike | None = None,
        host_check: Callable[[], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
        python_executable: str | None = None,
        mirror_refresher: Callable[[Path, Path], object] | None = None,
    ) -> None:
        self.state_root = state_root.expanduser()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_root / DB_NAME
        self.store = InsightStore(self.db_path)
        self.config = config if config is not None else load_config(self.state_root)
        self.routes = routes if routes is not None else load_routes(self.state_root)
        self.launcher: Launcher = launcher if launcher is not None else OmpLauncher()
        self.usage: UsageLike = usage if usage is not None else UsageGate()
        self.host_check = host_check or (
            lambda: host_ok(self.config.host_min_memory_level, self.config.host_max_swap_gb)
        )
        self.clock = clock or utcnow
        self.python_executable = python_executable or sys.executable
        self.mirror_refresher = mirror_refresher or sources_mod.refresh_mirror
        self._handles: dict[str, WorkerHandle] = {}

    # ------------------------------------------------------------------ tick
    def tick(self, *, dry_run: bool = False) -> TickReport:
        now = self.clock()
        report = TickReport(started_at=_iso(now))
        if not dry_run:
            report.collected = self.collect()
            self._refresh_mirror_if_due(report)
            try:
                scan = self.scan()
                report.scanned = dict(asdict(scan))
            except (OSError, ValueError) as exc:
                report.errors.append(f"scan failed: {exc}")
            report.new_signals = self.refresh_signals()
        report.launched, report.waiting = self.plan_and_launch(dry_run=dry_run)
        if not dry_run:
            self.render_board(report)
        report.finished_at = _iso(self.clock())
        if not dry_run:
            self.store.set_meta("last_tick", json.dumps(report.as_dict(), sort_keys=True))
            self._write_heartbeat(report)
        return report

    def scan(self) -> sources_mod.ScanResult:
        return sources_mod.scan(self.store, self.config, now=self.clock())

    def refresh_signals(self) -> int:
        claims = self.store.list_claims()
        detected = signals_mod.detect_signals(claims, now=self.clock())
        current = {signal.signal_id for signal in detected}
        inserted = self.store.upsert_signals(detected)
        stale = [
            signal.signal_id
            for signal in self.store.list_signals(status="open", limit=10_000)
            if signal.signal_id not in current
        ]
        if stale:
            self.store.mark_signals(stale, "stale")
        return inserted

    def render_board(self, report: TickReport | None = None) -> list[Path]:
        try:
            return board_mod.render_board(
                self.store,
                self.state_root / "board",
                now=self.clock(),
                top_n=self.config.board_top_n,
            )
        except (OSError, ValueError, KeyError) as exc:
            if report is not None:
                report.errors.append(f"board render failed: {exc}")
            self.store.log_event("board_failed", {"error": str(exc)})
            return []

    # ------------------------------------------------------------- planning
    def plan_and_launch(
        self, *, dry_run: bool = False
    ) -> tuple[list[dict[str, str]], dict[str, str]]:
        launched: list[dict[str, str]] = []
        waiting: dict[str, str] = {}
        running = self.store.list_passes(statuses=ACTIVE_STATUSES)
        running_by_lens: dict[str, int] = {}
        for record in running:
            running_by_lens[record.lens] = running_by_lens.get(record.lens, 0) + 1
        slots = self.config.max_concurrent_passes - len(running)
        host_checked: bool | None = None
        for lens in PLAN_ORDER:
            schedule = self.config.lenses.get(lens)
            if schedule is None or not schedule.enabled:
                waiting[lens] = "disabled"
                continue
            # Only extraction runs in parallel: its inputs (pending sections) are
            # claimed per pass, so two passes never see the same section.
            allowed = schedule.max_parallel if lens == "extractor" else 1
            if running_by_lens.get(lens, 0) >= allowed:
                waiting[lens] = "a pass of this lens is already running"
                continue
            reason = self._schedule_block(lens, schedule)
            if reason:
                waiting[lens] = reason
                continue
            while running_by_lens.get(lens, 0) < allowed:
                inputs, why = self.build_inputs(lens, schedule)
                if inputs is None:
                    waiting.setdefault(lens, why)
                    break
                if slots <= 0:
                    waiting[lens] = (
                        f"ready ({why}) but all {self.config.max_concurrent_passes} slots busy"
                    )
                    break
                if host_checked is None:
                    host_checked = self.host_check()
                if not host_checked:
                    waiting[lens] = "ready but the Mac is under memory pressure"
                    break
                if dry_run:
                    waiting[lens] = f"would launch: {why}"
                    slots -= 1
                    break
                record = self.launch(lens, inputs, trigger=why)
                if record.status != "running":
                    waiting[lens] = f"not launched: {record.error or record.route_reason}"
                    break
                launched.append({"lens": lens, "pass_id": record.pass_id, "route": record.route})
                running_by_lens[lens] = running_by_lens.get(lens, 0) + 1
                slots -= 1
                waiting.pop(lens, None)
        return launched, waiting

    def _schedule_block(self, lens: Lens, schedule: LensSchedule) -> str:
        now = self.clock()
        recent = self.store.list_passes(lens=lens, since=now - timedelta(hours=24))
        started = [record for record in recent if record.started_at is not None]
        if len(started) >= schedule.daily_cap:
            return f"daily cap reached ({len(started)} of {schedule.daily_cap} in 24h)"
        if recent:
            last = recent[0].created_at
            ready_at = last + timedelta(minutes=schedule.cooldown_minutes)
            if ready_at > now:
                minutes = math.ceil((ready_at - now).total_seconds() / 60)
                return f"cooling down ({minutes} min left)"
        return ""

    def _last_pass_time(self, lens: Lens) -> datetime | None:
        """Watermark for "new since the last pass": only a pass that delivered counts.

        A failed or parked pass must not swallow the claims it never analysed.
        """
        passes = self.store.list_passes(lens=lens, statuses=["succeeded"], limit=1)
        return passes[0].created_at if passes else None

    def build_inputs(self, lens: Lens, schedule: LensSchedule) -> tuple[PassInputs | None, str]:
        """Pick the inputs for one pass of ``lens``; return (None, why) when not ready."""
        if lens == "extractor":
            return self._extractor_inputs(schedule)
        if lens == "referee":
            checked = self.store.list_insights(statuses=["checked"])
            checked.sort(key=lambda record: (record.created_at, record.insight_id))
            if len(checked) < max(1, schedule.min_pending):
                return None, f"waiting: {len(checked)} cards to grade"
            batch = checked[: schedule.batch_size]
            return (
                PassInputs(lens=lens, insight_ids=tuple(r.insight_id for r in batch)),
                f"{len(checked)} checked cards waiting for a referee",
            )
        if lens == "rederiver":
            return self._rederiver_inputs(schedule)
        if lens == "synthesizer":
            return self._synthesizer_inputs(schedule)
        return self._analyst_inputs(lens, schedule)

    def _extractor_inputs(self, schedule: LensSchedule) -> tuple[PassInputs | None, str]:
        pending = self.store.pending_sections(limit=500)
        if not pending:
            return None, "waiting: no new report sections"
        priorities = {family.family: family.priority for family in self.config.sources}
        override_families = {override.family for override in self.config.overrides}
        sources_by_id = {}
        for section in pending:
            if section.source_id not in sources_by_id:
                sources_by_id[section.source_id] = self.store.get_source(section.source_id)

        def source_rank(source_id: str) -> tuple[int, str]:
            source = sources_by_id.get(source_id)
            if source is None:
                return (0, source_id)
            priority = priorities.get(source.family, 100 if source.family in override_families else 0)
            newest = max(
                (_iso(s.as_of) for s in pending if s.source_id == source_id and s.as_of), default=""
            )
            return (priority, newest)

        source_id = max(sorted(sources_by_id), key=source_rank)
        source = sources_by_id[source_id]
        if source is None:
            return None, "waiting: pending sections lost their source"
        chosen: list[str] = []
        budget = schedule.max_pack_chars
        for section in sorted(
            (s for s in pending if s.source_id == source_id), key=lambda s: s.ordinal
        ):
            try:
                size = len(sources_mod.section_text(source, section))
            except OSError:
                self.store.mark_sections([section.section_id], "failed")
                continue
            if chosen and size > budget:
                break
            chosen.append(section.section_id)
            budget -= size
            if len(chosen) >= schedule.batch_size:
                break
        if not chosen:
            return None, "waiting: pending sections unreadable"
        return (
            PassInputs(lens="extractor", section_ids=tuple(chosen)),
            f"{len(pending)} report sections pending; extracting {len(chosen)} from {source.path}",
        )

    def _rederiver_inputs(self, schedule: LensSchedule) -> tuple[PassInputs | None, str]:
        done = self.store.rederived_claim_ids()
        with_recipe = [
            record.claim_id
            for record in self.store.list_claims()
            if record.claim.recipe is not None and record.recompute is not None
        ]
        sample = [claim_id for claim_id in rederive_sample(with_recipe) if claim_id not in done]
        need = max(1, schedule.min_new_claims)
        if len(sample) < need:
            return None, f"waiting: {len(sample)} sampled claims to re-derive (need {need})"
        return (
            PassInputs(lens="rederiver", claim_ids=tuple(sample[: schedule.batch_size])),
            f"{len(sample)} sampled recipe-backed claims not yet re-derived",
        )

    def _synthesizer_inputs(self, schedule: LensSchedule) -> tuple[PassInputs | None, str]:
        last = self._last_pass_time("synthesizer")
        graded = self.store.list_insights(statuses=["refereed", "needs_revision", "rejected"])
        fresh = [r for r in graded if last is None or r.created_at > last]
        open_predictions = self.store.list_predictions(status="open")
        new_claims = self.store.list_claims(since=last) if last is not None else []
        need = max(1, schedule.min_new_insights)
        if len(fresh) < need and not (open_predictions and len(new_claims) >= 5 and last):
            return None, f"waiting: {len(fresh)} newly graded cards (need {need})"
        board_ids = [r.insight_id for r in graded if r.status in {"refereed", "needs_revision"}]
        return (
            PassInputs(
                lens="synthesizer",
                insight_ids=tuple(board_ids),
                claim_ids=tuple(r.claim_id for r in new_claims[-200:]),
                prediction_ids=tuple(p.prediction_id for p in open_predictions),
            ),
            f"{len(fresh)} newly graded cards, {len(open_predictions)} open predictions",
        )

    def _analyst_inputs(self, lens: Lens, schedule: LensSchedule) -> tuple[PassInputs | None, str]:
        last = self._last_pass_time(lens)
        claims = self.store.list_claims()
        new_claims = [c for c in claims if last is None or c.created_at > last]
        open_signals = self.store.list_signals(status="open", limit=200)
        graded = self.store.list_insights(statuses=["refereed"])
        new_graded = [r for r in graded if last is None or r.created_at > last]
        if not claims:
            return None, "waiting: the claim ledger is empty"
        if lens == "anomaly":
            if len(open_signals) < max(1, schedule.min_open_signals):
                return None, f"waiting: {len(open_signals)} open signals"
            top = open_signals[: schedule.batch_size]
            cited = sorted({cid for signal in top for cid in signal.claim_ids})
            return (
                PassInputs(
                    lens=lens,
                    claim_ids=tuple(cited),
                    signal_ids=tuple(s.signal_id for s in top),
                    insight_ids=tuple(r.insight_id for r in graded[:20]),
                ),
                f"{len(open_signals)} open statistical signals",
            )
        if lens in {"theorist", "ideator"}:
            if len(new_graded) < max(1, schedule.min_new_insights):
                return None, f"waiting: {len(new_graded)} new graded insights"
            return (
                PassInputs(
                    lens=lens,
                    claim_ids=tuple(c.claim_id for c in claims),
                    insight_ids=tuple(r.insight_id for r in graded),
                    signal_ids=tuple(s.signal_id for s in open_signals[:30]),
                    prediction_ids=tuple(
                        p.prediction_id for p in self.store.list_predictions(status="open")
                    ),
                ),
                f"{len(new_graded)} new graded insights",
            )
        if len(new_claims) < max(1, schedule.min_new_claims):
            return None, f"waiting: {len(new_claims)} new claims (need {schedule.min_new_claims})"
        focus = new_claims if lens == "skeptic" else claims
        return (
            PassInputs(
                lens=lens,
                claim_ids=tuple(c.claim_id for c in focus),
                signal_ids=tuple(s.signal_id for s in open_signals[:30]),
                insight_ids=tuple(r.insight_id for r in graded[:30]),
            ),
            f"{len(new_claims)} new claims since the last {lens} pass",
        )

    # ------------------------------------------------------------- launching
    def _usage_by_route(self, routes: Sequence[str]) -> dict[str, float | None]:
        usage: dict[str, float | None] = {}
        for route in routes:
            try:
                usage[route] = self.usage.used_percent(provider_of(route), route)
            except (OSError, ValueError) as exc:
                self.store.log_event("usage_probe_failed", {"route": route, "error": str(exc)})
                usage[route] = None
        return usage

    def _parked(self) -> dict[str, datetime]:
        raw = self.store.get_meta("parked_routes")
        if not raw:
            return {}
        now = self.clock()
        parked = {route: _parse_iso(until) for route, until in json.loads(raw).items()}
        return {route: until for route, until in parked.items() if until > now}

    def _park(self, route: str, until: datetime) -> None:
        parked = {route: _iso(stamp) for route, stamp in self._parked().items()}
        parked[route] = _iso(until)
        self.store.set_meta("parked_routes", json.dumps(parked, sort_keys=True))

    def _avoid_families(self, lens: Lens, inputs: PassInputs) -> set[str]:
        spec = self.routes.spec(lens)
        if not spec.independent_of:
            return set()
        families: set[str] = set()
        for insight_id in inputs.insight_ids:
            record = self.store.get_insight(insight_id)
            if record is not None and record.model_served:
                families.add(model_family(record.model_served))
        for claim_id in inputs.claim_ids:
            claim = self.store.get_claim(claim_id)
            if claim is not None and lens == "rederiver":
                families.add(model_family(claim.extracted_by))
        if not families:
            for author in spec.independent_of:
                author_spec = self.routes.lenses.get(author)
                if author_spec is not None:
                    families.update(model_family(route) for route in author_spec.routes)
        return families

    def launch(self, lens: Lens, inputs: PassInputs, *, trigger: str) -> PassRecord:
        now = self.clock()
        pass_id = f"P-{now.strftime('%Y%m%dT%H%M%SZ')}-{lens}-{secrets.token_hex(3)}"
        spec = self.routes.spec(lens)
        choice = choose_route(
            spec,
            usage=self._usage_by_route(spec.routes),
            parked=self._parked(),
            avoid_families=self._avoid_families(lens, inputs),
            max_used_percent=self.config.max_provider_used_percent,
            now=now,
        )
        schedule = self.config.schedule(lens)
        record = PassRecord(
            pass_id=pass_id,
            lens=lens,
            route=choice.route or "",
            route_reason=_route_reason(choice.reason, choice.skipped),
            trigger=trigger,
            status="planned",
            pass_dir=str(self.state_root / "passes" / pass_id),
            created_at=now,
        )
        if choice.route is None:
            record = record.model_copy(update={"status": "failed", "error": choice.reason})
            self.store.save_pass(record)
            self.store.log_event("no_route", {"lens": lens, "reason": choice.reason})
            return record
        try:
            directory = build_pass_dir(
                self.store,
                inputs,
                pass_id=pass_id,
                state_root=self.state_root,
                db_path=self.db_path,
                max_pack_chars=schedule.max_pack_chars,
                python_executable=self.python_executable,
            )
        except (OSError, ValueError) as exc:
            record = record.model_copy(
                update={"status": "failed", "error": f"pass directory: {_short_error(exc)}"}
            )
            self.store.save_pass(record)
            self.store.log_event("pass_build_failed", {"lens": lens, "error": record.error})
            return record
        record = record.model_copy(
            update={"pass_dir": str(directory.path), "inputs_digest": directory.inputs_digest}
        )
        self.store.save_pass(record)
        if lens == "extractor":
            self.store.mark_sections(inputs.section_ids, "in_flight", pass_id=pass_id)
        worker = WorkerSpec(
            pass_id=pass_id,
            route=choice.route,
            thinking=spec.thinking,
            max_minutes=spec.max_minutes,
            pass_dir=directory.path,
            prompt_file=directory.prompt_file,
            tools=WORKER_TOOLS,
        )
        try:
            handle = self.launcher.launch(worker)
        except OSError as exc:
            record = record.model_copy(update={"status": "failed", "error": f"launch failed: {exc}"})
            self.store.save_pass(record)
            if lens == "extractor":
                self.store.mark_sections(inputs.section_ids, "pending")
            return record
        self._handles[pass_id] = handle
        record = record.model_copy(
            update={"status": "running", "pid": handle.pid, "started_at": self.clock()}
        )
        self.store.save_pass(record)
        self.store.log_event(
            "pass_started",
            {"pass_id": pass_id, "lens": lens, "route": choice.route, "trigger": trigger},
        )
        return record

    def run_lens(
        self,
        lens: Lens,
        *,
        wait: bool = True,
        trigger: str = "manual",
        inputs: PassInputs | None = None,
        poll_seconds: float = 3.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> PassRecord | None:
        """Start one pass now (ignoring cooldown and caps); optionally wait for it."""
        if inputs is None:
            built, why = self.build_inputs(lens, self.config.schedule(lens))
            if built is None:
                self.store.log_event("run_lens_skipped", {"lens": lens, "reason": why})
                return None
            inputs = built
        record = self.launch(lens, inputs, trigger=trigger)
        if not wait or record.status != "running":
            return record
        while True:
            collected = self.collect(only={record.pass_id})
            if collected:
                return self.store.get_pass(record.pass_id)
            sleep(poll_seconds)

    # ------------------------------------------------------------ collecting
    def recover(self) -> list[dict[str, str]]:
        """After a restart: adopt live workers, collect the ones that finished."""
        return self.collect()

    def collect(self, *, only: set[str] | None = None) -> list[dict[str, str]]:
        done: list[dict[str, str]] = []
        for record in self.store.list_passes(statuses=ACTIVE_STATUSES):
            if only is not None and record.pass_id not in only:
                continue
            handle = self._handles.get(record.pass_id)
            if handle is None and record.pid is not None:
                adopted = self.launcher.adopt(record.pid, Path(record.pass_dir))
                if adopted is not None:
                    self._handles[record.pass_id] = adopted
                    handle = adopted
            exit_code: int | None = None
            timed_out = False
            if handle is not None:
                exit_code = handle.poll()
                if exit_code is None:
                    spec = self.routes.lenses.get(record.lens)
                    limit = timedelta(minutes=spec.max_minutes if spec else 30) + TIMEOUT_GRACE
                    started = record.started_at or record.created_at
                    if self.clock() - started <= limit:
                        continue
                    handle.kill()
                    timed_out = True
            finished = self.finalize(record, exit_code=exit_code, timed_out=timed_out)
            self._handles.pop(record.pass_id, None)
            done.append({"pass_id": finished.pass_id, "lens": finished.lens, "status": finished.status})
        return done

    def _inputs_of(self, record: PassRecord) -> PassInputs:
        path = Path(record.pass_dir) / "pack.json"
        if not path.exists():
            return PassInputs(lens=record.lens)
        raw = json.loads(path.read_text("utf-8"))
        return PassInputs(
            lens=raw["lens"],
            section_ids=tuple(raw.get("section_ids", ())),
            claim_ids=tuple(raw.get("claim_ids", ())),
            insight_ids=tuple(raw.get("insight_ids", ())),
            signal_ids=tuple(raw.get("signal_ids", ())),
            prediction_ids=tuple(raw.get("prediction_ids", ())),
        )

    def finalize(
        self, record: PassRecord, *, exit_code: int | None, timed_out: bool = False
    ) -> PassRecord:
        now = self.clock()
        pass_dir = Path(record.pass_dir)
        inputs = self._inputs_of(record)
        update: dict[str, object] = {"finished_at": now}
        try:
            attestation = attest_session(pass_dir / "session", record.route)
            update.update(
                served=dict(attestation.served),
                off_route=attestation.off_route,
                tokens=dict(attestation.tokens),
            )
            served_model = _primary_model(attestation.served)
        except (OSError, ValueError) as exc:
            served_model = None
            update["error"] = f"attestation failed: {exc}"
        output = pass_dir / "out" / OUTPUT_FILES[record.lens]
        status: str
        if output.exists():
            try:
                counts = self._import_output(record, inputs, output, served_model=served_model)
                update["outputs"] = counts
                status = "succeeded"
            except (ValidationError, ValueError) as exc:
                status = "invalid_output"
                update["error"] = _short_error(exc)
        else:
            log_text = _tail(pass_dir / "worker.log")
            kind, reset_at = classify_failure(log_text)
            if timed_out:
                status = "timed_out"
                update["error"] = "worker exceeded its time limit and was stopped"
            elif kind == "quota":
                status = "parked_quota"
                self._park(record.route, reset_at or now + PARK_DEFAULT)
                update["error"] = f"quota exhausted on {record.route}"
            else:
                status = "failed"
                code = "unknown" if exit_code is None else str(exit_code)
                update["error"] = f"no output file ({kind}; exit {code})"
        if record.lens == "extractor" and status != "succeeded":
            self._release_sections(inputs.section_ids)
        if update.get("off_route"):
            # The gateway served a different model than requested: stop using this
            # route for a while so the next pass takes the next route in order.
            self._park(record.route, now + PARK_DEFAULT)
            self.store.log_event(
                "route_served_other_model",
                {"pass_id": record.pass_id, "route": record.route, "served": update.get("served")},
            )
        update["status"] = status
        finished = record.model_copy(update=update)
        self.store.save_pass(finished)
        self.store.log_event(
            "pass_finished",
            {
                "pass_id": record.pass_id,
                "lens": record.lens,
                "status": status,
                "served": update.get("served", {}),
                "error": update.get("error"),
            },
        )
        return finished

    def _release_sections(self, section_ids: Sequence[str]) -> None:
        retry: list[str] = []
        failed: list[str] = []
        for section_id in section_ids:
            row = self.store.query("SELECT attempts FROM sections WHERE section_id = ?", (section_id,))
            if not row:
                continue
            (failed if int(row[0]["attempts"]) >= 3 else retry).append(section_id)
        self.store.mark_sections(retry, "pending")
        self.store.mark_sections(failed, "failed")

    # -------------------------------------------------------------- imports
    def _import_output(
        self, record: PassRecord, inputs: PassInputs, output: Path, *, served_model: str | None
    ) -> dict[str, int]:
        text = output.read_text("utf-8")
        lens = record.lens
        if lens == "extractor":
            return self._import_claims(record, inputs, ClaimExtraction.model_validate_json(text), served_model)
        if lens in ANALYST_LENSES:
            return self._import_cards(record, InsightBatch.model_validate_json(text), served_model)
        if lens == "referee":
            return self._import_verdicts(inputs, RefereeBatch.model_validate_json(text), served_model)
        if lens == "synthesizer":
            return self._import_board(record, BoardUpdate.model_validate_json(text))
        if lens == "rederiver":
            return self._import_rederivations(
                record, inputs, RederiveBatch.model_validate_json(text), served_model
            )
        raise ValueError(f"no importer for lens {lens}")

    def _import_claims(
        self,
        record: PassRecord,
        inputs: PassInputs,
        extraction: ClaimExtraction,
        served_model: str | None,
    ) -> dict[str, int]:
        allowed = set(inputs.section_ids)
        now = self.clock()
        records: list[ClaimRecord] = []
        refused = 0
        with_claims: set[str] = set()
        for claim in extraction.claims:
            if claim.section_id not in allowed:
                refused += 1
                continue
            section = self.store.get_section(claim.section_id)
            source = None if section is None else self.store.get_source(section.source_id)
            if section is None or source is None:
                refused += 1
                continue
            text = sources_mod.section_text(source, section)
            checked = check_mod.check_extracted_claim(claim, text)
            recomputed: RecomputeResult | None
            try:
                folder = Path(source.folder) if source.folder else None
                recomputed = recompute_mod.recompute_claim(claim, folder)
            except (OSError, ValueError) as exc:
                recomputed = RecomputeResult(status="not_recomputable", detail=_short_error(exc))
            records.append(
                ClaimRecord(
                    claim_id=claim_identity(source.source_id, claim.section_id, claim.quote, claim.statement),
                    source_id=source.source_id,
                    section_sha256=section.sha256,
                    fingerprint=claim_fingerprint(claim),
                    as_of=section.as_of,
                    pass_id=record.pass_id,
                    extracted_by=served_model or record.route,
                    created_at=now,
                    claim=claim,
                    check=checked,
                    recompute=recomputed,
                )
            )
            with_claims.add(claim.section_id)
        inserted = self.store.insert_claims(records)
        for experiment in extraction.experiments:
            self.store.upsert_experiment(experiment)
        self.store.upsert_entities(extraction.entities)
        covered = (set(extraction.section_ids) | set(extraction.skipped_section_ids) | with_claims) & allowed
        self.store.mark_sections(sorted(covered), "extracted", pass_id=record.pass_id)
        self._release_sections(sorted(allowed - covered))
        failed_checks = sum(1 for claim in records if claim.check.status == "fail")
        return {
            "claims": len(records),
            "inserted": inserted,
            "check_failed": failed_checks,
            "refused": refused,
            "experiments": len(extraction.experiments),
            "sections_covered": len(covered),
        }

    def _import_cards(
        self, record: PassRecord, batch: InsightBatch, served_model: str | None
    ) -> dict[str, int]:
        if batch.lens != record.lens:
            raise ValueError(f"cards.json says lens {batch.lens!r}; this pass is {record.lens!r}")
        now = self.clock()
        known = {row["insight_id"] for row in self.store.query("SELECT insight_id FROM insights")}
        records: list[InsightRecord] = []
        predictions: list[PredictionRecord] = []
        for card in batch.cards:
            cited = {ref.claim_id for ref in card.evidence}
            if card.challenge is not None:
                cited |= set(card.challenge.target_claim_ids)
            claims = {cid: c for cid in cited if (c := self.store.get_claim(cid)) is not None}
            checked = check_mod.check_card(card, claims=claims, known_insights=known)
            insight_id = insight_identity(record.pass_id, card)
            records.append(
                InsightRecord(
                    insight_id=insight_id,
                    lens=batch.lens,
                    pass_id=record.pass_id,
                    model_served=served_model,
                    created_at=now,
                    card=card,
                    check=checked,
                    status="check_failed" if checked.status == "fail" else "checked",
                )
            )
            predictions.extend(
                PredictionRecord(
                    prediction_id=prediction_identity(insight_id, index),
                    insight_id=insight_id,
                    spec=spec,
                    created_at=now,
                )
                for index, spec in enumerate(card.predictions)
            )
        inserted = self.store.insert_insights(records)
        self.store.insert_predictions(predictions)
        if record.lens == "anomaly":
            self.store.mark_signals(self._inputs_of(record).signal_ids, "consumed")
        return {
            "cards": len(records),
            "inserted": inserted,
            "check_failed": sum(1 for r in records if r.status == "check_failed"),
            "predictions": len(predictions),
        }

    def _import_verdicts(
        self, inputs: PassInputs, batch: RefereeBatch, served_model: str | None
    ) -> dict[str, int]:
        allowed = set(inputs.insight_ids)
        referee_family = model_family(served_model) if served_model else None
        applied = 0
        not_independent = 0
        for verdict in batch.verdicts:
            if verdict.insight_id not in allowed:
                continue
            insight = self.store.get_insight(verdict.insight_id)
            if insight is None:
                continue
            author_family = model_family(insight.model_served) if insight.model_served else None
            if referee_family is not None and referee_family == author_family:
                not_independent += 1
                self.store.log_event(
                    "referee_not_independent",
                    {"insight_id": insight.insight_id, "family": referee_family},
                )
                continue
            support = {"yes": 1.0, "partial": 0.5, "no": 0.0}[verdict.supported]
            score = verdict.importance * verdict.non_obviousness * support * (1.0 if verdict.novel else 0.5)
            self.store.save_insight(
                insight.model_copy(
                    update={
                        "referee": verdict,
                        "referee_model": served_model,
                        "status": _REFEREE_STATUS[verdict.verdict],
                        "score": round(score, 3),
                    }
                )
            )
            applied += 1
        return {"verdicts": len(batch.verdicts), "applied": applied, "not_independent": not_independent}

    def _import_board(self, record: PassRecord, update: BoardUpdate) -> dict[str, int]:
        now = self.clock()
        board_statuses: list[InsightStatus] = ["refereed", "needs_revision"]
        current = {r.insight_id: r for r in self.store.list_insights(statuses=board_statuses)}
        ranks = {entry.insight_id: entry.rank for entry in update.ranked if entry.insight_id in current}
        for insight_id, insight in current.items():
            rank = ranks.get(insight_id)
            if insight.rank != rank:
                self.store.save_insight(insight.model_copy(update={"rank": rank}))
        merged = 0
        for merge in update.merges:
            if merge.keep not in current:
                continue
            for duplicate in merge.duplicates:
                found = self.store.get_insight(duplicate)
                if found is None or duplicate == merge.keep:
                    continue
                self.store.save_insight(
                    found.model_copy(
                        update={"status": "duplicate", "duplicate_of": merge.keep, "rank": None}
                    )
                )
                merged += 1
        superseded = 0
        for item in update.supersedes:
            old = self.store.get_insight(item.old)
            if old is None or self.store.get_insight(item.new) is None or item.old == item.new:
                continue
            self.store.save_insight(
                old.model_copy(update={"status": "superseded", "superseded_by": item.new, "rank": None})
            )
            superseded += 1
        resolved = 0
        for resolution in update.resolutions:
            prediction = self.store.get_prediction(resolution.prediction_id)
            if prediction is None or resolution.status == "open":
                continue
            if any(self.store.get_claim(cid) is None for cid in resolution.claim_ids):
                continue
            self.store.save_prediction(
                prediction.model_copy(
                    update={"status": resolution.status, "resolution": resolution, "resolved_at": now}
                )
            )
            resolved += 1
        promoted = self._write_proposals(update.promote, record)
        self.store.set_meta("board_headline", update.headline)
        self.store.set_meta("board_summary", update.summary)
        self.store.set_meta("board_pass", record.pass_id)
        history = self.state_root / "board" / "history" / f"{record.pass_id}.json"
        _atomic_write_json(history, update.model_dump(mode="json"))
        return {
            "ranked": len(ranks),
            "merged": merged,
            "superseded": superseded,
            "resolved": resolved,
            "promoted": promoted,
        }

    def _write_proposals(self, insight_ids: Sequence[str], record: PassRecord) -> int:
        written = 0
        for insight_id in insight_ids:
            insight = self.store.get_insight(insight_id)
            if insight is None or insight.card.proposed_experiment is None:
                continue
            payload = {
                "schema": "evallab.insights.proposal/v1",
                "insight_id": insight_id,
                "title": insight.card.title,
                "statement": insight.card.statement,
                "proposed_experiment": insight.card.proposed_experiment.model_dump(mode="json"),
                "evidence": [ref.model_dump(mode="json") for ref in insight.card.evidence],
                "lens": insight.lens,
                "model_served": insight.model_served,
                "referee": None if insight.referee is None else insight.referee.model_dump(mode="json"),
                "promoted_by_pass": record.pass_id,
                "execution_authorized": False,
                "note": "A proposal is not an approval. Paid or cloud runs need Peter's recorded OK.",
            }
            _atomic_write_json(self.state_root / "proposals" / f"{insight_id}.json", payload)
            written += 1
        return written

    def _import_rederivations(
        self,
        record: PassRecord,
        inputs: PassInputs,
        batch: RederiveBatch,
        served_model: str | None,
    ) -> dict[str, int]:
        allowed = set(inputs.claim_ids)
        now = self.clock()
        saved = agreed = 0
        for item in batch.claims:
            if item.claim_id not in allowed:
                continue
            stored = self.store.get_claim(item.claim_id)
            if stored is None:
                continue
            recomputed: RecomputeResult | None = None
            if item.recipe is not None:
                source = self.store.get_source(stored.source_id)
                folder = Path(source.folder) if source is not None and source.folder else None
                candidate = stored.claim.model_copy(update={"recipe": item.recipe})
                try:
                    recomputed = recompute_mod.recompute_claim(candidate, folder)
                except (OSError, ValueError) as exc:
                    recomputed = RecomputeResult(status="not_recomputable", detail=_short_error(exc))
            agrees = _agreement(stored.recompute, recomputed)
            self.store.save_rederivation(
                RederivationRecord(
                    claim_id=item.claim_id,
                    pass_id=record.pass_id,
                    model_served=served_model,
                    rederived=item,
                    recompute=recomputed,
                    agrees=agrees,
                    created_at=now,
                )
            )
            saved += 1
            agreed += int(bool(agrees))
        return {"rederived": saved, "agree": agreed}

    # ------------------------------------------------------------- watching
    def watch(
        self,
        *,
        max_ticks: int | None = None,
        stop: Callable[[], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> int:
        ticks = 0
        with engine_lock(self.state_root):
            self.store.log_event("engine_started", {"pid": os.getpid()})
            self.recover()
            while True:
                try:
                    self.tick()
                except Exception as exc:  # noqa: BLE001 - a tick failure must not stop the engine
                    self.store.log_event("tick_failed", {"error": _short_error(exc)})
                ticks += 1
                if max_ticks is not None and ticks >= max_ticks:
                    break
                waited = 0.0
                while waited < self.config.tick_seconds:
                    if stop is not None and stop():
                        self.store.log_event("engine_stopped", {"ticks": ticks})
                        return ticks
                    sleep(1.0)
                    waited += 1.0
        return ticks

    def _refresh_mirror_if_due(self, report: TickReport) -> None:
        mirror = self.config.repo_mirror
        if mirror is None:
            return
        last = self.store.get_meta("mirror_refreshed_at")
        now = self.clock()
        if last and now - _parse_iso(last) < timedelta(minutes=mirror.refresh_minutes):
            return
        try:
            head = self.mirror_refresher(
                Path(mirror.repo).expanduser(), Path(mirror.path).expanduser()
            )
            self.store.set_meta("mirror_refreshed_at", _iso(now))
            self.store.set_meta("mirror_head", str(head))
        except (OSError, ValueError, RuntimeError) as exc:
            report.errors.append(f"mirror refresh failed: {_short_error(exc)}")
            self.store.set_meta("mirror_refreshed_at", _iso(now))

    def _write_heartbeat(self, report: TickReport) -> None:
        running = self.store.list_passes(statuses=ACTIVE_STATUSES)
        _atomic_write_json(
            self.state_root / "heartbeat.json",
            {
                "pid": os.getpid(),
                "at": report.finished_at,
                "running": [
                    {"pass_id": r.pass_id, "lens": r.lens, "route": r.route} for r in running
                ],
                "launched": report.launched,
                "errors": report.errors,
            },
        )


def _route_reason(reason: str, skipped: Sequence[tuple[str, str]]) -> str:
    if not skipped:
        return reason
    details = "; ".join(f"{route}: {why}" for route, why in skipped)
    return f"{reason} Skipped {details}"


def _agreement(original: RecomputeResult | None, rederived: RecomputeResult | None) -> bool | None:
    if original is None or rederived is None:
        return None
    if original.status != rederived.status:
        return False
    if original.value is None or rederived.value is None:
        return original.value is None and rederived.value is None
    tolerance = max(1e-9, 1e-6 * max(abs(original.value), abs(rederived.value)))
    return abs(original.value - rederived.value) <= tolerance


def _short_error(exc: BaseException, limit: int = 600) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _tail(path: Path, limit: int = 20_000) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - limit))
        return handle.read().decode("utf-8", errors="replace")


def model_dump_lines(records: Iterable[BaseModel]) -> list[str]:
    return [json.dumps(r.model_dump(mode="json"), sort_keys=True) for r in records]
