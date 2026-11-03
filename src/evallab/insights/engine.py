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
import subprocess
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
from evallab.insights import duplicates as duplicates_mod
from evallab.insights import packs as packs_mod
from evallab.insights import recompute as recompute_mod
from evallab.insights import scope as scope_mod
from evallab.insights import signals as signals_mod
from evallab.insights import sources as sources_mod
from evallab.insights.config import (
    EngineConfig,
    LensSchedule,
    RouteSpec,
    RouteTable,
    load_config,
    load_routes,
    model_family,
    provider_of,
)
from evallab.insights.models import (
    BoardUpdate,
    CalibrationRecord,
    ClaimExtraction,
    ClaimRecord,
    ExtractedClaim,
    InsightBatch,
    InsightRecord,
    InsightStatus,
    Lens,
    PassRecord,
    PredictionRecord,
    Recipe,
    RecomputeResult,
    RederivationRecord,
    RederiveBatch,
    RefereeBatch,
    SourceRecord,
    TwinBatch,
    claim_fingerprint,
    claim_identity,
    insight_identity,
    prediction_identity,
)
from evallab.insights.packs import OUTPUT_FILES, PassInputs, build_pass_dir, card_claim_ids
from evallab.insights.routes import choose_route
from evallab.insights.runner import (
    TRANSIENT_ERROR,
    TRANSIENT_EXHAUSTED_ERROR,
    TRANSIENT_MAX_RETRIES,
    TRANSIENT_STARTUP,
    Launcher,
    MemoryGate,
    OmpLauncher,
    UsageGate,
    WorkerHandle,
    WorkerSpec,
    attest_session,
    classify_failure,
    transient_retry_delay,
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
    "twinmaker",
)
WORKER_TOOLS = "read,bash,grep,glob,write"
TIMEOUT_GRACE = timedelta(minutes=5)
PARK_DEFAULT = timedelta(minutes=60)
AUTH_PARK = timedelta(hours=24)
BACKOFF_MAX = timedelta(hours=24)
MIRROR_GIT_TIMEOUT = 60
#: Between full ticks the watch loop polls worker handles this often (seconds).
COLLECT_POLL_SECONDS = 5.0
#: A transient-retry count is forgotten after this long without a startup failure.
TRANSIENT_FORGET = timedelta(hours=1)
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


def _safe_mirror_refresh(repo: Path, mirror: Path) -> str:
    """Run every mirror git command with a deadline and no credential prompt."""
    def run(
        args: list[str], *, check: bool, capture_output: bool, text: bool
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args, check=check, capture_output=capture_output, text=text,
            timeout=MIRROR_GIT_TIMEOUT, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )

    return sources_mod.refresh_mirror(repo, mirror, run=run)


class Engine:
    def __init__(
        self,
        state_root: Path,
        *,
        config: EngineConfig | None = None,
        routes: RouteTable | None = None,
        launcher: Launcher | None = None,
        usage: UsageLike | None = None,
        host_check: Callable[[], bool | str | None] | None = None,
        clock: Callable[[], datetime] | None = None,
        python_executable: str | None = None,
        mirror_refresher: Callable[[Path, Path], object] | None = None,
        readonly: bool = False,
        launch_sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.state_root = state_root.expanduser()
        if not readonly:
            self.state_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_root / DB_NAME
        self.store = InsightStore(self.db_path, readonly=readonly)
        self._config_reload_enabled = config is None
        self.config = config if config is not None else load_config(self.state_root)
        self.routes = routes if routes is not None else load_routes(self.state_root)
        self.launcher: Launcher = launcher if launcher is not None else OmpLauncher()
        self.usage: UsageLike = usage if usage is not None else UsageGate()
        self._memory_gate = MemoryGate()
        self.host_check = host_check or (
            lambda: self._memory_gate.status(
                self.config.host_min_memory_level, self.config.host_max_swap_growth_gb
            )
        )
        self.clock = clock or utcnow
        self.python_executable = python_executable or sys.executable
        self.mirror_refresher = mirror_refresher or _safe_mirror_refresh
        self._handles: dict[str, WorkerHandle] = {}
        self._launch_sleep = launch_sleep or time.sleep
        self._last_launch_at: datetime | None = None

    # ------------------------------------------------------------------ tick
    def tick(self, *, dry_run: bool = False) -> TickReport:
        now = self.clock()
        report = TickReport(started_at=_iso(now))
        if self._config_reload_enabled:
            try:
                self.config = load_config(self.state_root)
            except (OSError, ValueError, ValidationError) as exc:
                report.errors.append(f"config reload failed; keeping previous values: {exc}")
        if not dry_run:
            collected = self._step(report, "collect", self.collect)
            report.collected = [] if collected is None else collected
            self._step(report, "mirror refresh", lambda: self._refresh_mirror_if_due(report))
            scan = self._step(report, "scan", self.scan)
            if scan is not None:
                report.scanned = dict(asdict(scan))
            self._step(report, "card recheck", self.recheck_insights)
            new_signals = self._step(report, "signals", self.refresh_signals)
            report.new_signals = 0 if new_signals is None else new_signals
        planned = self._step(report, "planning", lambda: self.plan_and_launch(dry_run=dry_run))
        if planned is not None:
            report.launched, report.waiting = planned
        if not dry_run:
            self._step(report, "board", lambda: self.render_board(report))
        report.finished_at = _iso(self.clock())
        if not dry_run:
            # The heartbeat is written even when steps failed: it carries the errors.
            self._step(
                report, "last tick", lambda: self.store.set_meta(
                    "last_tick", json.dumps(report.as_dict(), sort_keys=True)
                ),
            )
            self._write_heartbeat(report)
        return report

    def _step[ResultT](
        self, report: TickReport, name: str, action: Callable[[], ResultT]
    ) -> ResultT | None:
        """Run one tick step; a failure becomes a report error, never an exception."""
        try:
            return action()
        except Exception as exc:  # noqa: BLE001 - one broken step must not stop the others
            error = f"{name} failed: {_short_error(exc)}"
            report.errors.append(error)
            self._log_event_safely("tick_step_failed", {"step": name, "error": error})
            return None

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
        host_evaluated = False
        host_reason: str | None = None
        for lens in PLAN_ORDER:
            schedule = self.config.lenses.get(lens)
            if schedule is None or not schedule.enabled:
                waiting[lens] = "disabled"
                continue
            # Extractor sections and skeptic experiments are claimed per pass;
            # all other lenses remain single-flight.
            allowed = schedule.max_parallel if lens in {"extractor", "skeptic"} else 1
            if running_by_lens.get(lens, 0) >= allowed:
                waiting[lens] = "a pass of this lens is already running"
                continue
            reason = self._schedule_block(lens, schedule)
            if reason:
                waiting[lens] = reason
                continue
            while running_by_lens.get(lens, 0) < allowed:
                inputs, why = self.build_inputs(lens, schedule, dry_run=dry_run)
                if inputs is None:
                    waiting.setdefault(lens, why)
                    break
                backoff = self._backoff_block(lens, self._input_content_digest(lens, inputs))
                if backoff:
                    waiting[lens] = backoff
                    break
                if slots <= 0:
                    waiting[lens] = (
                        f"ready ({why}) but all {self.config.max_concurrent_passes} slots busy"
                    )
                    break
                if not host_evaluated:
                    host_evaluated = True
                    result = self.host_check()
                    if result is False:
                        host_reason = "current memory pressure"
                    elif isinstance(result, str):
                        host_reason = result
                if host_reason is not None:
                    waiting[lens] = f"ready but {host_reason}"
                    break
                if dry_run:
                    waiting[lens] = f"would launch: {why}"
                    slots -= 1
                    break
                self._stagger_launch()
                record = self.launch(lens, inputs, trigger=why)
                if record.status != "running":
                    waiting[lens] = f"not launched: {record.error or record.route_reason}"
                    break
                self._last_launch_at = self.clock()
                launched.append({"lens": lens, "pass_id": record.pass_id, "route": record.route})
                running_by_lens[lens] = running_by_lens.get(lens, 0) + 1
                slots -= 1
                waiting.pop(lens, None)
        return launched, waiting

    def _stagger_launch(self) -> None:
        """Space `omp -p` starts so workers do not collide on omp's shared agent.db.

        Sleeps only the part of ``launch_stagger_seconds`` not already elapsed since the
        previous start, so it is bounded by slots x stagger within one refill.
        """
        stagger = self.config.launch_stagger_seconds
        if stagger <= 0 or self._last_launch_at is None:
            return
        wait = stagger - (self.clock() - self._last_launch_at).total_seconds()
        if wait > 0:
            self._launch_sleep(wait)

    def _backoff_state(self) -> dict[str, dict[str, dict[str, object]]]:
        raw = self.store.get_meta("lens_backoff")
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def _backoff_block(self, lens: Lens, content_digest: str) -> str:
        entry = self._backoff_state().get(lens, {}).get(content_digest)
        if not entry:
            return ""
        retry_at = _parse_iso(str(entry.get("retry_at")))
        now = self.clock()
        if retry_at <= now:
            return ""
        failures = _as_int(entry.get("failures"), 1)
        minutes = math.ceil((retry_at - now).total_seconds() / 60)
        return f"backing off after {failures} failures of the same inputs ({minutes} min left)"

    def _note_failure(self, lens: Lens, content_digest: str) -> None:
        if not content_digest:
            return
        state = self._backoff_state()
        per_lens = state.setdefault(lens, {})
        previous = per_lens.get(content_digest, {})
        failures = _as_int(previous.get("failures")) + 1
        base_minutes = max(1.0, self.config.schedule(lens).cooldown_minutes)
        delay = min(BACKOFF_MAX, timedelta(minutes=base_minutes * 2 ** (failures - 1)))
        per_lens[content_digest] = {
            "failures": failures,
            "retry_at": _iso(self.clock() + delay),
        }
        # Bound metadata growth from one-off malformed inputs.
        if len(per_lens) > 32:
            oldest = sorted(per_lens, key=lambda key: str(per_lens[key].get("retry_at", "")))[0]
            del per_lens[oldest]
        self.store.set_meta("lens_backoff", json.dumps(state, sort_keys=True))

    def _note_success(self, lens: Lens, content_digest: str) -> None:
        state = self._backoff_state()
        per_lens = state.get(lens, {})
        if content_digest in per_lens:
            del per_lens[content_digest]
            self.store.set_meta("lens_backoff", json.dumps(state, sort_keys=True))
        transient_key = f"transient:{lens}:{content_digest}"
        if self.store.get_meta(transient_key):
            self.store.set_meta(transient_key, "")

    def _transient_state(self, key: str) -> dict[str, object]:
        raw = self.store.get_meta(key)
        if not raw:
            return {}
        try:
            return _object_map(json.loads(raw))
        except ValueError:
            return {}

    def _claim_transient_retry(
        self, lens: Lens, content_digest: str, now: datetime, pass_id: str
    ) -> bool:
        """Grant one of ``TRANSIENT_MAX_RETRIES`` retries after a startup failure that wasn't ours.

        The retry waits a 15-45 s pause jittered by the failed pass id and ignores the lens
        cooldown. The count is per input set, forgotten after an hour or on success.
        """
        key = f"transient:{lens}:{content_digest}"
        state = self._transient_state(key)
        retries = _as_int(state.get("retries"))
        at = state.get("at")
        if isinstance(at, str) and now - _parse_iso(at) > TRANSIENT_FORGET:
            retries = 0
        if retries >= TRANSIENT_MAX_RETRIES:
            return False
        delay = transient_retry_delay(pass_id)
        self.store.set_meta(key, json.dumps({"retries": retries + 1, "at": _iso(now)}))
        self.store.set_meta(f"transient_until:{lens}", _iso(now + delay))
        self.store.log_event(
            "transient_startup_failure",
            {"lens": lens, "pass_id": pass_id, "retry": retries + 1,
             "delay_seconds": delay.total_seconds()},
        )
        return True

    def _no_route_block(self, lens: Lens) -> str:
        raw = self.store.get_meta(f"no_route:{lens}")
        if not raw:
            return ""
        try:
            entry = json.loads(raw)
            retry_at = _parse_iso(str(entry["retry_at"]))
        except (ValueError, KeyError):
            return ""
        now = self.clock()
        if retry_at <= now:
            return ""
        minutes = math.ceil((retry_at - now).total_seconds() / 60)
        return f"no route available; retry in {minutes} min ({entry.get('reason', '')})"

    def _note_no_route(self, lens: Lens, reason: str) -> None:
        key = f"no_route:{lens}"
        previous: dict[str, object] = {}
        raw = self.store.get_meta(key)
        if raw:
            try:
                previous = json.loads(raw)
            except ValueError:
                previous = {}
        failures = _as_int(previous.get("failures")) + 1
        base = max(5.0, self.config.schedule(lens).cooldown_minutes)
        delay = min(timedelta(hours=1), timedelta(minutes=base * 2 ** (failures - 1)))
        now = self.clock()
        self.store.set_meta(key, json.dumps({
            "failures": failures,
            "retry_at": _iso(now + delay),
            "reason": reason,
            "last_at": _iso(now),
        }, sort_keys=True))
        # Keep no-route events useful without crowding out real pass failures.
        last_at = previous.get("last_at")
        if not isinstance(last_at, str) or now - _parse_iso(last_at) >= timedelta(hours=1):
            self.store.log_event("no_route", {"lens": lens, "reason": reason})

    def _clear_no_route(self, lens: Lens) -> None:
        self.store.set_meta(f"no_route:{lens}", "")

    def _schedule_block(self, lens: Lens, schedule: LensSchedule) -> str:
        no_route = self._no_route_block(lens)
        if no_route:
            return no_route
        transient_until = self.store.get_meta(f"transient_until:{lens}")
        retry_due = False
        if transient_until:
            if _parse_iso(transient_until) > self.clock():
                return "retrying shortly after a transient startup failure"
            retry_due = True
        now = self.clock()
        recent = self.store.list_passes(lens=lens, since=now - timedelta(hours=24))
        started = [record for record in recent if record.started_at is not None]
        if len(started) >= schedule.daily_cap:
            return f"daily cap reached ({len(started)} of {schedule.daily_cap} in 24h)"
        if recent and not retry_due:
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

    def _launch_metadata(self, lens: Lens, inputs: PassInputs) -> dict[str, object]:
        if lens == "extractor":
            shas: dict[str, str] = {}
            for section_id in inputs.section_ids:
                section = self.store.get_section(section_id)
                if section is not None:
                    shas[section_id] = section.sha256
            return {"section_sha256": shas}
        if lens == "skeptic":
            selected = set(inputs.claim_ids)
            coverage = {
                str(candidate["exp_id"]): str(candidate["claims_digest"])
                for candidate in self.store.experiment_review_candidates()
                if selected.intersection(candidate["claim_ids"])
            }
            return {"coverage": coverage, "experiment_ids": sorted(coverage)}
        return {}

    @staticmethod
    def _digest_of(inputs: PassInputs, metadata: Mapping[str, object]) -> str:
        payload = {"inputs": asdict(inputs), "metadata": dict(metadata)}
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _input_content_digest(self, lens: Lens, inputs: PassInputs) -> str:
        return self._digest_of(inputs, self._launch_metadata(lens, inputs))

    def build_inputs(
        self, lens: Lens, schedule: LensSchedule, *, dry_run: bool = False
    ) -> tuple[PassInputs | None, str]:
        """Pick the inputs for one pass of ``lens``; return (None, why) when not ready."""
        if lens == "extractor":
            return self._extractor_inputs(schedule, dry_run=dry_run)
        if lens == "skeptic":
            return self._skeptic_inputs(schedule)
        if lens == "referee":
            return self._referee_inputs(schedule)
        if lens == "twinmaker":
            return self._twinmaker_inputs(schedule)
        if lens == "rederiver":
            return self._rederiver_inputs(schedule)
        if lens == "synthesizer":
            return self._synthesizer_inputs(schedule)
        return self._analyst_inputs(lens, schedule)

    def _referee_inputs(self, schedule: LensSchedule) -> tuple[PassInputs | None, str]:
        ordinary = self.store.list_insights(statuses=["checked"])
        ordinary.sort(key=lambda record: (record.created_at, record.insight_id))
        units: list[tuple[datetime, tuple[str, ...]]] = [
            (record.created_at, (record.insight_id,)) for record in ordinary
        ]
        for canary in self.store.pending_canaries():
            pair = [canary.insight_id]
            if canary.canary_of and self.store.get_insight(canary.canary_of) is not None:
                pair.append(canary.canary_of)
            units.append((canary.created_at, tuple(pair)))
        if len(units) < max(1, schedule.min_pending):
            return None, f"waiting: {len(ordinary)} cards to grade"
        units.sort(key=lambda item: (item[0], item[1]))
        chosen: list[str] = []
        oversized = 0
        for _, unit in units:
            if any(insight_id in chosen for insight_id in unit):
                continue
            candidate = [*chosen, *unit]
            try:
                size = packs_mod.referee_pack_size(self.store, candidate)
            except ValueError:
                oversized += 1
                continue
            if size <= schedule.max_pack_chars:
                chosen = candidate
            else:
                oversized += 1
        if not chosen:
            return None, f"waiting: {oversized} card(s) exceed max_pack_chars"
        waiting = len(units) - (1 if chosen else 0)
        return (
            PassInputs(lens="referee", insight_ids=tuple(chosen)),
            f"{len(ordinary)} checked cards plus calibration controls; "
            f"size-batched {len(chosen)} cards ({oversized} did not fit; {waiting} units waiting)",
        )

    def _twinmaker_inputs(self, schedule: LensSchedule) -> tuple[PassInputs | None, str]:
        accepted = self.store.list_insights(statuses=["refereed"])
        accepted = [record for record in accepted if record.referee is not None
                    and record.referee.verdict == "accept"]
        if len(accepted) < max(4, schedule.min_new_insights):
            return None, f"waiting: {len(accepted)} accepted cards (need at least 4)"
        used = self.store.canary_source_ids()
        accepted.sort(key=lambda record: (-record.created_at.timestamp(), record.insight_id))
        fresh = [record for record in accepted if record.insight_id not in used]
        if not fresh:
            return None, "waiting: every accepted card already has a calibration twin"
        selected = fresh[:schedule.batch_size]
        return (
            PassInputs(lens="twinmaker", insight_ids=tuple(r.insight_id for r in selected)),
            f"{len(fresh)} accepted cards available for hidden referee controls",
        )

    def _extractor_inputs(
        self, schedule: LensSchedule, *, dry_run: bool = False
    ) -> tuple[PassInputs | None, str]:
        summaries = self.store.pending_source_summaries()
        if not summaries:
            return None, "waiting: no new report sections"
        priorities = {family.family: family.priority for family in self.config.sources}
        override_families = {override.family for override in self.config.overrides}

        def source_rank(row: dict[str, object]) -> tuple[int, str, str]:
            source = self.store.get_source(str(row["source_id"]))
            priority = 0 if source is None else priorities.get(
                source.family, 100 if source.family in override_families else 0
            )
            return priority, str(row["newest"] or ""), str(row["source_id"])

        selected = max(summaries, key=source_rank)
        source_id = str(selected["source_id"])
        source = self.store.get_source(source_id)
        if source is None:
            return None, "waiting: pending sections lost their source"
        pending = self.store.pending_sections(source_id=source_id, limit=None)
        chosen: list[str] = []
        budget = schedule.max_pack_chars
        for section in sorted(pending, key=lambda item: item.ordinal):
            try:
                size = len(sources_mod.section_text(source, section))
            except OSError:
                if not dry_run:
                    self._release_sections([section.section_id], count_attempt=True)
                continue
            if chosen and size > budget:
                break
            chosen.append(section.section_id)
            budget -= size
            if len(chosen) >= schedule.batch_size:
                break
        if not chosen:
            return None, "waiting: pending sections unreadable"
        total = sum(int(row["pending_count"]) for row in summaries)
        return (
            PassInputs(lens="extractor", section_ids=tuple(chosen)),
            f"{total} report sections pending; extracting {len(chosen)} from {source.path}",
        )

    def _skeptic_in_flight_experiments(self) -> set[str]:
        claimed: set[str] = set()
        for record in self.store.list_passes(lens="skeptic", statuses=ACTIVE_STATUSES):
            stored = self.store.pass_inputs(record.pass_id)
            if stored is None:
                continue
            experiment_ids = stored["metadata"].get("experiment_ids", [])
            claimed.update(str(exp_id) for exp_id in experiment_ids)
        return claimed

    def _skeptic_inputs(self, schedule: LensSchedule) -> tuple[PassInputs | None, str]:
        reviewed = self.store.coverage_digests("skeptic")
        in_flight = self._skeptic_in_flight_experiments()
        candidates = [
            candidate
            for candidate in self.store.experiment_review_candidates()
            if candidate["claim_ids"]
            and candidate["exp_id"] not in in_flight
            and reviewed.get(str(candidate["exp_id"])) != candidate["claims_digest"]
        ]
        if not candidates:
            return None, "waiting: 0 experiments not yet reviewed"
        open_signals = self.store.list_signals(status="open", limit=10_000)
        signal_counts: dict[str, int] = {}
        for candidate in candidates:
            claim_ids = set(candidate["claim_ids"])
            signal_counts[str(candidate["exp_id"])] = sum(
                bool(claim_ids.intersection(signal.claim_ids)) for signal in open_signals
            )
        candidates.sort(key=lambda candidate: str(candidate["exp_id"]))
        candidates.sort(
            key=lambda candidate: (
                signal_counts[str(candidate["exp_id"])],
                int(candidate["claim_count"]),
                str(candidate["recency"]),
            ),
            reverse=True,
        )
        selected = candidates[: schedule.experiments_per_pass]
        selected_ids = {str(candidate["exp_id"]) for candidate in selected}
        claim_ids = sorted({
            claim_id for candidate in selected for claim_id in candidate["claim_ids"]
        })
        selected_claims = set(claim_ids)
        signals = [
            signal for signal in open_signals if selected_claims.intersection(signal.claim_ids)
        ]
        graded = self.store.list_insights(statuses=["refereed"])
        return (
            PassInputs(
                lens="skeptic",
                claim_ids=tuple(claim_ids),
                signal_ids=tuple(signal.signal_id for signal in signals),
                insight_ids=tuple(record.insight_id for record in graded[:30]),
            ),
            f"{len(candidates)} experiments not yet reviewed; reviewing {len(selected_ids)}",
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
        fresh = self.store.list_insights(
            statuses=["refereed", "needs_revision", "rejected"], graded_since=last
        )
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
        new_graded = self.store.list_insights(statuses=["refereed"], graded_since=last)
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
        focus = claims
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

    def _route_spec(self, lens: Lens) -> RouteSpec:
        # Twin authors come from the extractor's route family; only the referee
        # needs independence from the analysts, not from the calibration writer.
        return self.routes.spec("extractor" if lens == "twinmaker" else lens)

    def _avoid_families(self, lens: Lens, inputs: PassInputs) -> set[str]:
        spec = self._route_spec(lens)
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
        spec = self._route_spec(lens)
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
            # Blocked routes are a waiting reason, not a pass: recording one failed
            # pass per cooldown buried real failures. The plan retries with backoff.
            reason = _route_reason(choice.reason, choice.skipped)
            record = record.model_copy(update={"status": "failed", "error": choice.reason})
            self._note_no_route(lens, reason)
            return record
        self._clear_no_route(lens)
        self.store.set_meta(f"transient_until:{lens}", "")
        metadata = self._launch_metadata(lens, inputs)
        digest = self._digest_of(inputs, metadata)
        metadata["backoff_digest"] = digest
        try:
            directory = build_pass_dir(
                self.store,
                inputs,
                pass_id=pass_id,
                state_root=self.state_root,
                db_path=self.db_path,
                max_pack_chars=schedule.max_pack_chars,
                python_executable=self.python_executable,
                allowed_read_roots=scope_mod.allowed_read_roots(
                    self.config, self.state_root / "passes" / pass_id
                ),
            )
        except (OSError, ValueError) as exc:
            record = record.model_copy(
                update={"status": "failed", "error": f"pass directory: {_short_error(exc)}"}
            )
            self.store.save_pass(record)
            self.store.log_event("pass_build_failed", {"lens": lens, "error": record.error})
            self._note_failure(lens, digest)
            return record
        record = record.model_copy(
            update={"pass_dir": str(directory.path), "inputs_digest": directory.inputs_digest}
        )
        self.store.save_pass(record)
        # The ledger, not the worker-writable pack.json, is authoritative for what
        # this pass was given; inputs_digest only detects later tampering.
        self.store.save_pass_inputs(pass_id, lens, asdict(inputs), metadata, directory.inputs_digest)
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
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            record = record.model_copy(update={"status": "failed", "error": f"launch failed: {exc}"})
            self.store.save_pass(record)
            if lens == "extractor":
                self._release_sections(inputs.section_ids)
            self._note_failure(lens, digest)
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
        """Finalize every finished pass; one bad pass never blocks the others."""
        done: list[dict[str, str]] = []
        for record in self.store.list_passes(statuses=ACTIVE_STATUSES):
            if only is not None and record.pass_id not in only:
                continue
            try:
                finished = self._collect_one(record)
            except Exception as exc:  # noqa: BLE001 - isolate the pass, keep the engine alive
                finished = self._fail_pass(record, exc)
            if finished is not None:
                done.append(
                    {"pass_id": finished.pass_id, "lens": finished.lens, "status": finished.status}
                )
        return done

    def _collect_one(self, record: PassRecord) -> PassRecord | None:
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
                spec = self._route_spec(record.lens)
                limit = timedelta(minutes=spec.max_minutes) + TIMEOUT_GRACE
                started = record.started_at or record.created_at
                if self.clock() - started <= limit:
                    return None
                handle.kill()
                timed_out = True
        finished = self.finalize(record, exit_code=exit_code, timed_out=timed_out)
        self._handles.pop(record.pass_id, None)
        return finished

    def _fail_pass(self, record: PassRecord, exc: BaseException) -> PassRecord | None:
        """Mark a pass whose collection raised as failed and release what it held."""
        error = f"collect failed: {_short_error(exc)}"
        try:
            stored = self.store.pass_inputs(record.pass_id)
            sections = [] if stored is None else list(stored["inputs"].get("section_ids", ()))
            if record.lens == "extractor":
                self._release_sections(sections)
            finished = record.model_copy(
                update={"status": "failed", "finished_at": self.clock(), "error": error}
            )
            self.store.save_pass(finished)
            self.store.log_event(
                "pass_collect_failed", {"pass_id": record.pass_id, "lens": record.lens, "error": error}
            )
            if stored is not None:
                self._note_failure(record.lens, str(stored["metadata"].get("backoff_digest", "")))
            self._handles.pop(record.pass_id, None)
            return finished
        except Exception as inner:  # noqa: BLE001 - never let the cleanup path kill the loop
            self._log_event_safely(
                "pass_collect_failed",
                {"pass_id": record.pass_id, "error": error, "cleanup_error": _short_error(inner)},
            )
            return None

    def _inputs_of(self, record: PassRecord) -> PassInputs:
        """Inputs come from the ledger row written at launch, never from worker-writable files."""
        stored = self.store.pass_inputs(record.pass_id)
        if stored is not None:
            if stored["lens"] != record.lens:
                raise ValueError(f"stored inputs are for lens {stored['lens']!r}, not {record.lens!r}")
            raw = stored["inputs"]
        else:
            # A pass launched before inputs were stored in the ledger: read its
            # pack.json once, but never let it change the lens.
            path = Path(record.pass_dir) / "pack.json"
            if not path.exists():
                return PassInputs(lens=record.lens)
            raw = json.loads(path.read_text("utf-8"))
        return PassInputs(
            lens=record.lens,
            section_ids=tuple(raw.get("section_ids", ())),
            claim_ids=tuple(raw.get("claim_ids", ())),
            insight_ids=tuple(raw.get("insight_ids", ())),
            signal_ids=tuple(raw.get("signal_ids", ())),
            prediction_ids=tuple(raw.get("prediction_ids", ())),
        )

    def _tamper_error(self, record: PassRecord, stored: dict[str, object] | None) -> str | None:
        """pack.json and brief.md are worker-writable: only compare them with the launch digest.

        Passes launched before inputs were stored in the ledger have no row and are
        not checked (their digest predates the sealed pass directory).
        """
        if stored is None or not record.inputs_digest:
            return None
        try:
            actual = packs_mod.pass_inputs_digest(Path(record.pass_dir))
        except OSError:
            return "pack.json or brief.md is missing; the pass directory was modified after launch"
        if actual != record.inputs_digest:
            return "pack.json or brief.md changed after launch; the pass output was not imported"
        return None

    def finalize(
        self, record: PassRecord, *, exit_code: int | None, timed_out: bool = False
    ) -> PassRecord:
        now = self.clock()
        pass_dir = Path(record.pass_dir)
        stored = self.store.pass_inputs(record.pass_id)
        meta: dict[str, object] = {} if stored is None else dict(stored["metadata"])
        inputs = self._inputs_of(record)
        update: dict[str, object] = {"finished_at": now}
        scope_audit = scope_mod.audit_session(
            pass_dir / "session",
            state_root=self.state_root,
            pass_dir=pass_dir,
            roots=scope_mod.allowed_read_roots(self.config, pass_dir),
        )
        update["scope_violations"] = scope_audit
        scope_rejected = scope_audit.count > 0 and self.config.scope_policy == "reject"
        if scope_audit.count:
            self.store.log_event("worker_scope_violation", {
                "pass_id": record.pass_id, "policy": self.config.scope_policy,
                "count": scope_audit.count, "examples": scope_audit.examples,
            })
        diagnostics = ""
        try:
            attestation = attest_session(pass_dir / "session", record.route)
            update.update(
                served=dict(attestation.served),
                off_route=attestation.off_route,
                tokens=dict(attestation.tokens),
            )
            served_model = _primary_model(attestation.served)
            diagnostics = attestation.diagnostics
        except (OSError, ValueError) as exc:
            served_model = None
            update["error"] = f"attestation failed: {exc}"
        output = pass_dir / "out" / OUTPUT_FILES[record.lens]
        tamper = self._tamper_error(record, stored)
        status: str
        count_attempt = False
        skip_backoff = False
        if tamper is not None:
            status = "failed"
            update["error"] = tamper
            self.store.log_event("pass_tampered", {"pass_id": record.pass_id, "detail": tamper})
        elif scope_rejected:
            status = "failed"
            update["error"] = (
                f"worker read scope rejected the pass ({scope_audit.count} violation(s)): "
                + "; ".join(scope_audit.examples)
            )
        elif output.exists():
            try:
                counts = self._import_output(
                    record, inputs, output, served_model=served_model, meta=meta
                )
                if scope_audit.count:
                    self.store.mark_scope_warning(record.pass_id)
                update["outputs"] = counts
                status = "succeeded"
            except (ValidationError, ValueError) as exc:
                status = "invalid_output"
                count_attempt = True
                update["error"] = _short_error(exc)
        else:
            # Only the session's own error records and omp's stderr describe a
            # provider failure. worker.log is the analyst's answer and may
            # legitimately discuss quotas, 429s or rate limits. The one exception
            # is a worker that never got a model response: nothing it printed can
            # be analysis, so startup errors in worker.log (omp prints some there)
            # are diagnostics.
            startup_text = ""
            if not update.get("served"):
                startup_text = _tail(pass_dir / "worker.log", 4_000)
            diagnostic_text = "\n".join(
                (diagnostics, _tail(pass_dir / "worker.stderr"), startup_text)
            )
            kind, reset_at = classify_failure(diagnostic_text)
            # A worker that never got a model response and whose stderr shows omp's shared
            # database busy is a startup collision, however many times it repeats.
            startup_collision = (
                not timed_out
                and not update.get("served")
                and TRANSIENT_STARTUP.search(diagnostic_text) is not None
            )
            retry_granted = startup_collision and self._claim_transient_retry(
                record.lens, str(meta.get("backoff_digest", "")), now, record.pass_id
            )
            if timed_out:
                status = "timed_out"
                update["error"] = "worker exceeded its time limit and was stopped"
            elif retry_granted:
                status = "failed"
                skip_backoff = True
                update["error"] = TRANSIENT_ERROR
            elif startup_collision:
                status = "failed"
                update["error"] = TRANSIENT_EXHAUSTED_ERROR
            elif kind == "quota":
                status = "parked_quota"
                self._park(record.route, reset_at or now + PARK_DEFAULT)
                update["error"] = f"quota exhausted on {record.route}"
            elif kind == "auth":
                status = "failed"
                self._park(provider_of(record.route), now + AUTH_PARK)
                update["error"] = (
                    f"authentication failed on {record.route}; provider {provider_of(record.route)} "
                    f"parked until {_iso(now + AUTH_PARK)}"
                )
            else:
                status = "failed"
                count_attempt = exit_code == 0 or kind == "refusal"
                code = "unknown" if exit_code is None else str(exit_code)
                update["error"] = f"no output file ({kind}; exit {code})"
        launch_sha = {str(k): str(v) for k, v in _object_map(meta.get("section_sha256")).items()}
        if record.lens == "extractor" and status != "succeeded":
            self._release_sections(
                inputs.section_ids, count_attempt=count_attempt, launch_sha=launch_sha
            )
        digest = str(meta.get("backoff_digest", ""))
        if status == "succeeded":
            if record.lens == "skeptic":
                coverage = {str(k): str(v) for k, v in _object_map(meta.get("coverage")).items()}
                self.store.record_coverage("skeptic", record.pass_id, coverage, reviewed_at=now)
            self._note_success(record.lens, digest)
        elif status in {"failed", "invalid_output", "timed_out"} and not skip_backoff:
            self._note_failure(record.lens, digest)
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

    def _release_sections(
        self,
        section_ids: Sequence[str],
        *,
        count_attempt: bool = False,
        launch_sha: Mapping[str, str] | None = None,
    ) -> None:
        """Return sections to the queue; only content failures cost an attempt.

        Quota, timeout, launch and infrastructure failures leave ``attempts``
        alone. A section edited since launch has already had its attempts reset
        and never counts against the new text.
        """
        for section_id in section_ids:
            row = self.store.query(
                "SELECT attempts, sha256 FROM sections WHERE section_id = ?", (section_id,)
            )
            if not row:
                continue
            seen = None if launch_sha is None else launch_sha.get(section_id)
            counted = count_attempt and (seen is None or seen == row[0]["sha256"])
            attempts = int(row[0]["attempts"]) + (1 if counted else 0)
            self.store.mark_sections(
                [section_id],
                "failed" if attempts >= 3 else "pending",
                count_attempt=counted,
            )

    # -------------------------------------------------------------- imports
    def _import_output(
        self,
        record: PassRecord,
        inputs: PassInputs,
        output: Path,
        *,
        served_model: str | None,
        meta: Mapping[str, object] | None = None,
    ) -> dict[str, int]:
        text = output.read_text("utf-8")
        lens = record.lens
        if lens == "extractor":
            launch_sha = {
                str(k): str(v) for k, v in _object_map((meta or {}).get("section_sha256")).items()
            }
            return self._import_claims(
                record, inputs, ClaimExtraction.model_validate_json(text), served_model, launch_sha
            )
        if lens in ANALYST_LENSES:
            return self._import_cards(record, InsightBatch.model_validate_json(text), served_model)
        if lens == "twinmaker":
            return self._import_twins(record, TwinBatch.model_validate_json(text), served_model)
        if lens == "referee":
            return self._import_verdicts(
                record, inputs, RefereeBatch.model_validate_json(text), served_model
            )
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
        launch_sha: Mapping[str, str] | None = None,
    ) -> dict[str, int]:
        allowed = set(inputs.section_ids)
        launch_sha = launch_sha or {}
        now = self.clock()
        records: list[ClaimRecord] = []
        refused = 0
        with_claims: set[str] = set()
        edited: set[str] = set()
        for claim in extraction.claims:
            if claim.section_id not in allowed:
                refused += 1
                continue
            section = self.store.get_section(claim.section_id)
            source = None if section is None else self.store.get_source(section.source_id)
            if section is None or source is None:
                refused += 1
                continue
            seen = launch_sha.get(claim.section_id)
            if seen is not None and seen != section.sha256:
                # The section changed while the worker read the old text; its
                # claims would be stored against text they never saw.
                edited.add(claim.section_id)
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
        edited |= {
            section_id
            for section_id in allowed
            if (current := self.store.get_section(section_id)) is not None
            and launch_sha.get(section_id) not in (None, current.sha256)
        }
        covered -= edited
        self.store.mark_sections(sorted(covered), "extracted", pass_id=record.pass_id)
        # Edited sections go back to the queue without costing an attempt; sections
        # the worker simply failed to cover count against the attempt limit.
        self._release_sections(sorted(edited))
        self._release_sections(sorted(allowed - covered - edited), count_attempt=True)
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
        existing = self.store.list_insights()
        for card in batch.cards:
            cited = {ref.claim_id for ref in card.evidence}
            if card.challenge is not None:
                cited |= set(card.challenge.target_claim_ids)
            claims, source_records, section_texts = self._card_context(cited)
            checked = check_mod.check_card(
                card, claims=claims, known_insights=known,
                sources=source_records, section_texts=section_texts,
            )
            insight_id = insight_identity(record.pass_id, card)
            duplicate = None if checked.status == "fail" else duplicates_mod.duplicate_of(
                card, [*existing, *records]
            )
            status: InsightStatus = (
                "check_failed" if checked.status == "fail"
                else "duplicate" if duplicate is not None
                else "checked"
            )
            records.append(
                InsightRecord(
                    insight_id=insight_id,
                    lens=batch.lens,
                    pass_id=record.pass_id,
                    model_served=served_model,
                    created_at=now,
                    card=card,
                    check=checked,
                    status=status,
                    duplicate_of=None if duplicate is None else duplicate[0],
                )
            )
            if status != "duplicate":
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
            "duplicates": sum(1 for r in records if r.status == "duplicate"),
            "predictions": len(predictions),
        }

    def _import_twins(
        self, record: PassRecord, batch: TwinBatch, served_model: str | None
    ) -> dict[str, int]:
        allowed = set(self._inputs_of(record).insight_ids)
        known = {row["insight_id"] for row in self.store.query("SELECT insight_id FROM insights")}
        records: list[InsightRecord] = []
        rejected = 0
        for twin in batch.twins:
            source = self.store.get_insight(twin.source_id)
            if twin.source_id not in allowed or source is None or source.hidden:
                rejected += 1
                continue
            card = twin.card
            claims, source_records, section_texts = self._card_context(card_claim_ids(card))
            checked = check_mod.check_card(
                card, claims=claims, known_insights=known,
                sources=source_records, section_texts=section_texts,
            )
            if checked.status == "fail":
                rejected += 1
                continue
            records.append(InsightRecord(
                insight_id=insight_identity(record.pass_id, card),
                lens="twinmaker",
                pass_id=record.pass_id,
                model_served=served_model,
                created_at=self.clock(),
                card=card,
                check=checked,
                status="checked",
                hidden=True,
                canary_of=source.insight_id,
                corruption=twin.corruption,
            ))
        inserted = self.store.insert_insights(records)
        return {"twins": len(batch.twins), "planted": inserted, "rejected": rejected}

    def _import_verdicts(
        self, record: PassRecord, inputs: PassInputs, batch: RefereeBatch, served_model: str | None
    ) -> dict[str, int]:
        allowed = set(inputs.insight_ids)
        referee_family = model_family(served_model) if served_model else None
        canaries = {
            insight_id: canary
            for insight_id in inputs.insight_ids
            if (canary := self.store.get_insight(insight_id)) is not None
            and canary.hidden and canary.canary_of is not None
        }
        originals = {canary.canary_of: canary for canary in canaries.values()}
        applied = 0
        calibrated = 0
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
                ),
                graded_at=self.clock(),
            )
            applied += 1
            canary = canaries.get(insight.insight_id) or originals.get(insight.insight_id)
            if canary is not None:
                self.store.save_calibration(CalibrationRecord(
                    canary_id=canary.insight_id,
                    insight_id=insight.insight_id,
                    pass_id=record.pass_id,
                    arm="twin" if insight.hidden else "original",
                    corruption=canary.corruption,
                    verdict=verdict.verdict,
                    created_at=self.clock(),
                ))
                calibrated += 1
        return {
            "verdicts": len(batch.verdicts), "applied": applied,
            "not_independent": not_independent, "calibration": calibrated,
        }

    def _card_context(
        self, cited: Iterable[str]
    ) -> tuple[dict[str, ClaimRecord], dict[str, SourceRecord], dict[str, str]]:
        """Cited claims plus the source records and full section texts check_card uses."""
        claims: dict[str, ClaimRecord] = {}
        source_records: dict[str, SourceRecord] = {}
        section_texts: dict[str, str] = {}
        for claim_id in cited:
            claim = self.store.get_claim(claim_id)
            if claim is None:
                continue
            claims[claim_id] = claim
            source = source_records.get(claim.source_id) or self.store.get_source(claim.source_id)
            if source is not None:
                source_records[claim.source_id] = source
            section = self.store.get_section(claim.claim.section_id)
            if source is not None and section is not None and claim.claim.section_id not in section_texts:
                try:
                    section_texts[claim.claim.section_id] = sources_mod.section_text(source, section)
                except OSError:
                    continue
        return claims, source_records, section_texts

    def recheck_insights(self) -> int:
        """Re-run the card check for cards whose cited claims went stale or failed.

        ``needs_recheck`` is set by the store whenever a claim becomes stale or
        fails. A card that no longer passes moves to ``check_failed`` (so it
        leaves the board and the referee queue); one that still passes keeps its
        status. The flag is cleared either way.
        """
        known = {row["insight_id"] for row in self.store.query("SELECT insight_id FROM insights")}
        flagged = self.store.insights_needing_recheck()
        moved = 0
        for insight in flagged:
            card = insight.card
            cited = {ref.claim_id for ref in card.evidence}
            if card.challenge is not None:
                cited |= set(card.challenge.target_claim_ids)
            claims, source_records, section_texts = self._card_context(cited)
            bad = sorted(
                cid for cid in cited
                if (record := self.store.get_claim(cid)) is None
                or record.stale or record.check.status == "fail"
            )
            checked = check_mod.check_card(
                card, claims=claims, known_insights=known,
                sources=source_records, section_texts=section_texts,
            )
            if checked.status == "fail" or bad:
                notes = list(checked.notes)
                if bad:
                    notes.append(f"Recheck: cited claims are stale, failed or missing: {', '.join(bad)}.")
                self.store.save_insight(
                    insight.model_copy(
                        update={
                            "status": "check_failed",
                            "rank": None,
                            "check": checked.model_copy(update={"status": "fail", "notes": notes}),
                        }
                    )
                )
                moved += 1
            self.store.clear_recheck([insight.insight_id])
        return moved

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
        saved = agreed = not_independent = 0
        rederiver_family = model_family(served_model) if served_model else None
        for item in batch.claims:
            if item.claim_id not in allowed:
                continue
            stored = self.store.get_claim(item.claim_id)
            if stored is None:
                continue
            author_family = model_family(stored.extracted_by) if stored.extracted_by else None
            if rederiver_family is not None and rederiver_family == author_family:
                # Same-family agreement says nothing about independence: do not
                # let it count toward the agreement rate.
                not_independent += 1
                self.store.log_event(
                    "rederiver_not_independent",
                    {"claim_id": stored.claim_id, "family": rederiver_family},
                )
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
            agrees = _agreement(stored.recompute, recomputed, stored.claim, item.recipe)
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
        return {"rederived": saved, "agree": agreed, "not_independent": not_independent}

    # ------------------------------------------------------------- watching
    def watch(
        self,
        *,
        max_ticks: int | None = None,
        stop: Callable[[], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        collect_seconds: float = COLLECT_POLL_SECONDS,
    ) -> int:
        """Run ticks forever; no single failure may stop the loop.

        Between ticks the engine polls its worker handles every ``collect_seconds``
        and collects (and refills freed slots) as soon as one exits; the full scan
        still runs once per ``tick_seconds``.
        """
        ticks = 0
        with engine_lock(self.state_root):
            self._log_event_safely("engine_started", {"pid": os.getpid()})
            try:
                self.recover()
            except Exception as exc:  # noqa: BLE001 - a bad recovery must not crash-loop launchd
                self._tick_failed(exc, phase="recover")
            while True:
                try:
                    self.tick()
                except Exception as exc:  # noqa: BLE001 - a tick failure must not stop the engine
                    self._tick_failed(exc, phase="tick")
                ticks += 1
                if max_ticks is not None and ticks >= max_ticks:
                    break
                waited = 0.0
                since_poll = 0.0
                while waited < self.config.tick_seconds:
                    if stop is not None and stop():
                        self._log_event_safely("engine_stopped", {"ticks": ticks})
                        return ticks
                    sleep(1.0)
                    waited += 1.0
                    since_poll += 1.0
                    if since_poll >= collect_seconds:
                        since_poll = 0.0
                        self._fast_cycle()
        return ticks

    def _log_event_safely(self, kind: str, detail: dict[str, object]) -> None:
        # Logging must never be the thing that kills the loop.
        with suppress(Exception):
            self.store.log_event(kind, detail)

    def _tick_failed(self, exc: BaseException, *, phase: str) -> None:
        error = _short_error(exc)
        self._log_event_safely("tick_failed", {"phase": phase, "error": error})
        now = self.clock()
        report = TickReport(
            started_at=_iso(now), finished_at=_iso(now), errors=[f"{phase} failed: {error}"]
        )
        with suppress(Exception):
            self.store.set_meta("last_tick", json.dumps(report.as_dict(), sort_keys=True))
        with suppress(Exception):
            self._write_heartbeat(report)

    def _fast_cycle(self) -> None:
        """Collect exited workers and refill their slots without a full scan."""
        try:
            if not any(handle.poll() is not None for handle in list(self._handles.values())):
                return
            collected = self.collect()
            if not collected:
                return
            launched, _waiting = self.plan_and_launch()
            now = self.clock()
            self._write_heartbeat(
                TickReport(
                    started_at=_iso(now), finished_at=_iso(now), collected=collected,
                    launched=launched,
                )
            )
        except Exception as exc:  # noqa: BLE001 - the next full tick retries everything
            self._tick_failed(exc, phase="collect")

    def _refresh_mirror_if_due(self, report: TickReport) -> None:
        mirror = self.config.repo_mirror
        if mirror is None:
            return
        last = self.store.get_meta("mirror_refreshed_at")
        now = self.clock()
        if last and now - _parse_iso(last) < timedelta(minutes=mirror.refresh_minutes):
            return
        retry_at = self.store.get_meta("mirror_retry_at")
        if retry_at and now < _parse_iso(retry_at):
            return
        try:
            head = self.mirror_refresher(
                Path(mirror.repo).expanduser(), Path(mirror.path).expanduser()
            )
            self.store.set_meta("mirror_refreshed_at", _iso(now))
            self.store.set_meta("mirror_head", str(head))
            self.store.set_meta("mirror_failures", "0")
            self.store.set_meta("mirror_retry_at", "")
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            # An unreachable origin must not stall scanning: keep the last mirror
            # and retry with exponential backoff (5 min, 10, 20 ... up to the
            # normal refresh interval).
            failures = int(self.store.get_meta("mirror_failures") or "0") + 1
            delay = min(timedelta(minutes=mirror.refresh_minutes), timedelta(minutes=5 * 2 ** (failures - 1)))
            report.errors.append(f"mirror refresh failed: {_short_error(exc)}")
            self.store.set_meta("mirror_failures", str(failures))
            self.store.set_meta("mirror_retry_at", _iso(now + delay))
            self.store.log_event(
                "mirror_refresh_failed", {"error": _short_error(exc), "failures": failures}
            )

    def _write_heartbeat(self, report: TickReport) -> None:
        running = self.store.list_passes(statuses=ACTIVE_STATUSES)
        # `job`, `ok`, `detail` and `max_age_minutes` are the contract `jobs-health` reads from
        # ~/.local/state/*/heartbeat.json; without `ok` it reports the job as failed.
        _atomic_write_json(
            self.state_root / "heartbeat.json",
            {
                "job": "evallab-insights",
                "ok": not report.errors,
                "detail": "; ".join(report.errors)[:240],
                "max_age_minutes": max(15, 3 * self.config.tick_seconds // 60),
                "pid": os.getpid(),
                "at": report.finished_at,
                "tick_seconds": self.config.tick_seconds,
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


_SUPPORTS_CLAIM = frozenset({"reproduced", "partial"})


def _agreement(
    original: RecomputeResult | None,
    rederived: RecomputeResult | None,
    claim: ExtractedClaim | None = None,
    rederived_recipe: Recipe | None = None,
) -> bool | None:
    """Do two independent computations of one number agree?

    When both recipes compare the same claim quantity, values decide whenever both
    sides produced one (within the claim's printed rounding, the same tolerance
    ``recompute_claim`` uses). When they compare different quantities (a count
    against a ratio, k against n), their values are not comparable, so agreement is
    whether each side supports the claim (reproduced, or partial with what it checked
    matching): both do, or neither does. If only one side produced a value, one side
    failed and the other did not: disagreement. If neither did, the pair is not
    comparable: neither agree nor disagree.
    """
    if original is None or rederived is None:
        return None
    if original.value is not None and rederived.value is not None:
        if (
            claim is not None and claim.recipe is not None and rederived_recipe is not None
            and recompute_mod.recipe_target(claim, claim.recipe)
            != recompute_mod.recipe_target(claim, rederived_recipe)
        ):
            # ``partial`` means every number that was checked matched: it does not contradict
            # the claim, so it counts with ``reproduced`` here (and only here; reports and
            # confidence keep it apart from reproduced).
            return (original.status in _SUPPORTS_CLAIM) == (rederived.status in _SUPPORTS_CLAIM)
        tolerance = max(1e-9, 1e-6 * max(abs(original.value), abs(rederived.value)))
        if claim is not None:
            tolerance = max(tolerance, check_mod.value_printed_tolerance(claim))
        return abs(original.value - rederived.value) <= tolerance
    if (original.value is None) != (rederived.value is None):
        return False
    return None


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


def _as_int(value: object, default: int = 0) -> int:
    """A stored JSON number as an int; anything else is the default."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _object_map(value: object) -> dict[str, object]:
    """A stored JSON object; anything else is empty."""
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}
