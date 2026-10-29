"""Live Supervisor replay: run the supervisor over a historical case as if it were live.

A case (one row of the replay manifest) is turned into a :class:`Replayable`: the subject,
the timestamp of every step or event, and an ``observe(n_visible, now)`` function that builds
the source's :class:`Observation` from the first ``n_visible`` steps only. The supervisor is
then evaluated at simulated times ``t`` (every step or event timestamp, plus every
:data:`GRID_SECONDS` between them, up to the case end). At each ``t`` the source helper builds
the observation, ``source.triggers`` fires, ``propose`` + ``guard`` give the rule decision and
an optional injected decider may overrule it (``guard`` runs again). The FIRST non-continue
decision is the replay's result; the label is only read afterwards, to score it.

Nothing after ``t`` can reach the supervisor: the observation is built from a step prefix,
and run-level facts derived from the Harbor ``result.json`` (exception, stop reason, token
totals, finish time, verifier time) are dropped before the helper sees them.

``python -m evallab.production.live_supervisor_replay --manifest M --out O [--set S]``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import lru_cache
from itertools import pairwise
from pathlib import Path
from typing import Any

from evallab.production import live_supervisor as ls
from evallab.production import run_health
from evallab.production.run_health import HealthRun, _epoch

#: Evaluate the supervisor at least this often between steps (the live scan cadence is
#: coarser; one minute is the finest resolution the thresholds are expressed in).
GRID_SECONDS = 60.0
#: Step spacing for a case that carries no timestamps at all (flagged ``timing: synthetic``).
SYNTHETIC_STEP_SECONDS = 60.0
SYNTHETIC_T0 = 1_000_000_000.0
#: Run-level Harbor facts the replay may hand to a source. Everything else in the
#: ``trial_run`` meta comes from ``result.json`` (exception, stop reason, token totals,
#: finish time, verifier time) and describes how the run ended.
ALLOWED_META = frozenset({"started_at", "setup_seconds", "actor_inputs_available"})
DEFAULT_STORE_COPY = Path("/tmp/ls-store.sqlite")
DEFAULT_ROOT = Path("~/.local/state/evallab-prod-live/live-supervisor/replay")
STORE_CHUNK = 40
#: A lane observation looks back at most this far into the session (its windows are the trailing
#: hour, ten minutes and the latest consecutive tool calls). A session file can hold tens of
#: thousands of records, and the helper walks every record it is given.
LANE_LOOKBACK_SECONDS = 3 * 3600.0
#: The lane's token baseline (median active hour) is taken once, from this much history before the
#: window starts, and held fixed through the replay.
LANE_BASELINE_SECONDS = 48 * 3600.0
SUBJECT_KINDS = ("harbor_trial", "store_run", "seeker_turn", "omp_lane")

ObserveFn = Callable[[int, datetime], ls.Observation]


class UnsupportedCase(Exception):
    """No live-supervisor source covers this case; the row is reported as skipped, not scored."""


@dataclass(frozen=True)
class Replayable:
    """One case ready to replay. ``stamps[i]`` is the time of step/event ``i`` (sorted)."""

    subject: ls.Subject
    source: ls.Source
    stamps: tuple[float, ...]
    start: float
    end: float
    observe: ObserveFn
    onset: float | None = None
    #: ``timed`` (every step stamped), ``interpolated`` (some stamped), ``even_spread`` (none
    #: stamped, spread over the known start..end) or ``synthetic`` (none stamped, no span).
    timing: str = "timed"


# --------------------------------------------------------------------------- time


def to_dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=UTC)


def utc_text(epoch: float) -> str:
    return to_dt(epoch).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: Any) -> float | None:
    """ISO-8601 text or epoch -> epoch seconds; None when absent or unparseable."""
    return _epoch(value)


def time_grid(marks: Sequence[float], start: float, end: float, step: float = GRID_SECONDS) -> list[float]:
    """Every mark in ``[start, end]`` plus every ``step`` seconds between consecutive marks,
    plus ``start`` and ``end`` themselves. Sorted, de-duplicated."""
    if end < start:
        raise ValueError("end precedes start")
    points = sorted({start, end, *(m for m in marks if start <= m <= end)})
    grid: list[float] = []
    for a, b in pairwise(points):
        grid.append(a)
        k = 1
        while a + k * step < b:
            grid.append(a + k * step)
            k += 1
    grid.append(points[-1])
    return grid


def place_steps(raw: Sequence[float | None], start: float | None, end: float | None) -> tuple[list[float], str]:
    """One non-decreasing time per step from the stamps the trace carries.

    Stamped steps keep their time (clamped so time never runs backwards). Gaps between two
    stamped steps are interpolated linearly by position; steps before the first stamp sit at
    ``start`` (or the first stamp); steps after the last stamp sit on it. A trace with no
    stamps is spread evenly over ``start..end`` when both are known, else laid out every
    :data:`SYNTHETIC_STEP_SECONDS` from a fixed origin.
    """
    n = len(raw)
    stamped = [(i, float(v)) for i, v in enumerate(raw) if v is not None]
    if not stamped:
        if start is not None and end is not None and end > start:
            span = end - start
            return [start + span * (i + 1) / (n + 1) for i in range(n)], "even_spread"
        origin = start if start is not None else SYNTHETIC_T0
        return [origin + SYNTHETIC_STEP_SECONDS * (i + 1) for i in range(n)], "synthetic"
    (first, first_time), (last, last_time) = stamped[0], stamped[-1]
    out = [0.0] * n
    for i in range(first):
        out[i] = min(start, first_time) if start is not None else first_time
    for i in range(last + 1, n):
        out[i] = last_time
    for (a, ta), (b, tb) in pairwise(stamped):
        for i in range(a, b):
            out[i] = ta + (tb - ta) * (i - a) / (b - a)
    out[last] = last_time
    running = float("-inf")
    for i, value in enumerate(out):
        running = max(running, value)
        out[i] = running
    return out, "timed" if len(stamped) == n else "interpolated"


# --------------------------------------------------------------------------- replay core


class _MemoPattern:
    """A compiled pattern whose ``search`` is memoised on the text (run_health only calls ``search``)."""

    def __init__(self, pattern: Any) -> None:
        self.search = lru_cache(maxsize=None)(pattern.search)


def _by_object(fn: Callable[[Any], Any], key: Callable[[Any], tuple[Any, ...]]) -> Callable[[Any], Any]:
    """Memoise ``fn`` on the identity of the objects ``key`` picks out of its argument.

    The cached entry keeps those objects alive and a hit requires the very same objects, so an
    ``id`` reused after garbage collection can never produce a stale hit.
    """
    cache: dict[tuple[int, ...], tuple[tuple[Any, ...], Any]] = {}

    def wrapper(step: Any) -> Any:
        parts = key(step)
        ids = tuple(id(p) for p in parts)
        hit = cache.get(ids)
        if hit is not None and all(a is b for a, b in zip(hit[0], parts, strict=True)):
            return hit[1]
        value = fn(step)
        cache[ids] = (parts, value)
        return value

    return wrapper


def _by_prefix(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Memoise ``features``/``detect`` per (run, step count, meta): the replay hands every call at
    the same simulated step count the same prefix of one step list."""
    cache: dict[Any, Any] = {}

    def wrapper(run: HealthRun, *extra: Any) -> Any:
        try:
            key = (run.run_id, run.source, len(run.steps), frozenset(run.meta.items()), tuple(id(e) for e in extra))
        except TypeError:
            return fn(run, *extra)
        hit = cache.get(key)
        if hit is None:
            hit = cache[key] = (extra, fn(run, *extra))
        return hit[1]

    return wrapper


def _step(step: Any) -> tuple[Any, ...]:
    return (step,)


def _calls(step: Any) -> tuple[Any, ...]:
    return step.get("tool_calls"), step.get("extra")


@contextmanager
def memoized_run_health() -> Iterator[None]:
    """Make ``run_health.features`` cheap to call on a growing step prefix.

    A replay recomputes the features of the same steps at every simulated time and the
    per-step work (regex scans, command extraction) dominates: seconds per call on a
    100-step trial. All of it is a pure function of one step's content, so it is memoised here
    for the length of one case (the results are identical to the unpatched functions).

    The patched names are module globals, so this context is not thread-safe: one replay per
    process at a time (the CLI runs cases one after another inside each worker process), and the
    original functions are restored even when the body raises.
    """
    from evallab import token_flow

    names = ("INFRA_SIGNATURES", "TIMEOUT_MARKER_RE", "_COMPLETION_RE", "_error_signature",
             "_output_fingerprint", "_observation_text", "_step_text", "_output_text", "_signature",
             "_step_calls", "_is_edit", "features", "detect")
    saved = {name: getattr(run_health, name) for name in names}
    saved_flow = getattr(token_flow, "_step_calls")  # noqa: B009 - ty: a private function is not assignable
    patches: dict[str, Any] = {
        "INFRA_SIGNATURES": tuple((n, _MemoPattern(p)) for n, p in saved["INFRA_SIGNATURES"]),
        "TIMEOUT_MARKER_RE": _MemoPattern(saved["TIMEOUT_MARKER_RE"]),
        "_COMPLETION_RE": _MemoPattern(saved["_COMPLETION_RE"]),
        "_error_signature": lru_cache(maxsize=None)(saved["_error_signature"]),
        "_output_fingerprint": lru_cache(maxsize=None)(saved["_output_fingerprint"]),
        "_observation_text": _by_object(saved["_observation_text"], _step),
        "_step_text": _by_object(saved["_step_text"], _step),
        "_output_text": _by_object(saved["_output_text"], _step),
        "_signature": _by_object(saved["_signature"], _step),
        "_step_calls": _by_object(saved["_step_calls"], _calls),
        "_is_edit": _by_object(saved["_is_edit"], _calls),
        "features": _by_prefix(saved["features"]),
        "detect": _by_prefix(saved["detect"]),
    }
    _patch(token_flow, {"_step_calls": _by_object(saved_flow, _calls)})
    _patch(run_health, patches)
    try:
        yield
    finally:
        _patch(run_health, saved)
        _patch(token_flow, {"_step_calls": saved_flow})


def _patch(module: Any, values: Mapping[str, Any]) -> None:
    for name, value in values.items():
        setattr(module, name, value)


def window_record(key: str, observation: ls.Observation,
                  hits: Sequence[ls.TriggerHit] = ()) -> dict[str, Any]:
    """The label-free view of one case at its decision time (or at the last grid time when the
    replay said continue), for LS-3 adjudication. ``metrics`` keeps only scalars."""
    metrics = {k: v for k, v in observation.metrics.items()
               if isinstance(v, int | float | bool) or v is None or (isinstance(v, str) and len(v) <= 200)}
    return {"key": key, "subject": observation.subject.as_dict(), "observed_at": utc_text(observation.observed_at.timestamp()),
            "metrics": metrics, "actor_inputs_available": observation.actor_inputs_available,
            "events": [{"index": e.index, "at": None if e.at is None else utc_text(e.at.timestamp()),
                        "source": e.source, "text": e.text} for e in observation.events],
            "triggers": [h.as_dict() for h in hits]}


def _dec(observation: ls.Observation, hits: Sequence[ls.TriggerHit], proposal: ls.Decision,
         decider: ls.Decider | None) -> ls.Decision:
    if decider is None:
        return proposal
    try:
        return decider.decide(observation, hits, proposal)
    except Exception as exc:  # noqa: BLE001 - a failed reader never confirms a stop
        # A reader that cannot answer leaves the decision unconfirmed: escalate to research, never
        # keep a kill/flag proposal it did not endorse (the same rule the live scanner uses).
        return replace(proposal, action="alert_research",
                       reason=f"reader failed ({type(exc).__name__}); unconfirmed {proposal.action}: {proposal.reason}")


def hit_via(hit: ls.TriggerHit) -> str:
    """Which detector family produced ``hit``, from its detail text (the hit carries no source field)."""
    for prefix, via in (("run_health", "run_health"), ("live_watch", "live_watch"), ("trace harness", "trace_harness")):
        if hit.detail.startswith(prefix):
            return via
    return "clock"


def replay_timeline(case: Mapping[str, Any], rp: Replayable, *, decider: ls.Decider | None = None) -> dict[str, Any]:
    """Replay one prepared case; the row carries the first non-continue decision (or none)."""
    with memoized_run_health():
        return _replay(case, rp, decider)


def _replay(case: Mapping[str, Any], rp: Replayable, decider: ls.Decider | None) -> dict[str, Any]:
    stamps = rp.stamps
    grid = time_grid(stamps, rp.start, rp.end)
    first: tuple[float, ls.Observation, list[ls.TriggerHit], ls.Decision, ls.Decision] | None = None
    last_observation: ls.Observation | None = None
    evaluated = 0
    seen: dict[tuple[str, str, str], tuple[float, int, str]] = {}
    for t in grid:
        visible = bisect_right(stamps, t)
        observation = rp.observe(visible, to_dt(t))
        last_observation = observation
        evaluated += 1
        if observation.terminal:
            continue
        hits = rp.source.triggers(observation)
        if not hits:
            continue
        for h in hits:
            seen.setdefault((h.trigger, h.kind, hit_via(h)), (t, bisect_right(stamps, t), h.detail[:160]))
        proposal = ls.guard(rp.subject, ls.propose(rp.subject, hits))
        decision = ls.guard(rp.subject, _dec(observation, hits, proposal, decider))
        if decision.action != "continue":
            first = (t, observation, hits, decision, proposal)
            break
    onset = rp.onset
    observed = rp.timing == "timed"  # only a fully timestamped trace has an observed clock
    row: dict[str, Any] = {
        "case_id": case.get("case_id"), "set": case.get("set"),
        "subject_kind": case.get("subject_kind"),
        "label_positive": bool(case.get("label_positive")),
        "label_action": case.get("label_action"), "label_kind": case.get("label_kind"),
        "onset_at": None if onset is None else utc_text(onset),
        "timing": rp.timing, "clock": "observed" if observed else "inferred",
        "total_steps": len(stamps), "evaluations": evaluated,
        # True = the last view of the case carries no actor transcript: "continue" then means
        # unknown, not clean, and the case is not scorable (run_health's rule).
        "unknown_no_actor": bool(last_observation is not None and not last_observation.actor_inputs_available),
        "first_alarm_at": None, "action": "continue", "kind": None, "triggers": [],
        "detection_delay_s": None, "lead_before_end_s": None, "steps_at_alarm": None,
        "elapsed_to_first_alarm_s": None, "proposal": None, "reason": None, "reader_model": None,
        # Every distinct trigger that fired up to the first alarm (or the end), even when its rule
        # proposal was ``continue``: what a model reader would have been shown.
        "first_hits": [{"trigger": k[0], "kind": k[1], "via": k[2], "steps": v[1], "detail": v[2],
                        "elapsed_s": round(v[0] - rp.start, 3) if observed else None}
                       for k, v in sorted(seen.items(), key=lambda kv: kv[1][0])],
    }
    if observed or rp.timing != "synthetic":
        # Start and end come from the case's own metadata even when the step times do not.
        row["start_at"], row["end_at"] = utc_text(rp.start), utc_text(rp.end)
    if first is not None:
        t, observation, hits, decision, proposal = first
        row.update(
            action=decision.action, kind=decision.kind,
            triggers=[h.as_dict() for h in sorted(hits, key=lambda h: (ls.TRIGGERS.index(h.trigger), -h.score))],
            steps_at_alarm=bisect_right(stamps, t),
            proposal=proposal.action, reason=decision.reason, reader_model=decision.reader_model,
        )
        if observed:
            row.update(
                first_alarm_at=utc_text(t), elapsed_to_first_alarm_s=round(t - rp.start, 3),
                detection_delay_s=None if onset is None else round(t - onset, 3),
                lead_before_end_s=round(rp.end - t, 3),
            )
            row["finding"] = ls.finding(observation, hits, decision, proposal=proposal, mode="shadow",
                                        acted=False, produced_at=to_dt(t))
    if observed:
        window_observation = first[1] if first is not None else last_observation
        if window_observation is not None:
            row["window"] = window_record(str(case.get("case_id")), window_observation, first[2] if first else [])
    row["action_match"] = bool(case.get("label_positive")) and row["action"] == case.get("label_action")
    row["alarm_match"] = bool(case.get("label_positive")) and row["action"] != "continue"
    return row


# --------------------------------------------------------------------------- Harbor / store adapter


def strip_result_meta(meta: Mapping[str, Any]) -> dict[str, Any]:
    """Run-level facts a live supervisor could know mid-run: the ``trial_run`` meta without
    anything derived from ``result.json`` (exception, stop reason, token totals, finish and
    verifier time)."""
    return {k: v for k, v in meta.items() if k in ALLOWED_META}


def _step_epoch(step: Mapping[str, Any]) -> float | None:
    return _epoch(step.get("timestamp"))


def _onset_epoch(onset: Any, kind: Any, steps: Sequence[Mapping[str, Any]], stamps: Sequence[float]) -> float | None:
    if onset is None:
        return None
    if kind == "utc" or isinstance(onset, str):
        return parse_time(onset)
    for i, step in enumerate(steps):
        if step.get("step_id") == onset:
            return stamps[i]
    return None


def health_replayable(case: Mapping[str, Any], run: HealthRun, *, owner: str = "lab", killable: bool = True,
                      trial_observation: Callable[..., ls.Observation] | None = None,
                      source: ls.Source | None = None) -> Replayable:
    """A Harbor-shaped run (a trial directory or a store run) as a replayable subject."""
    if trial_observation is None or source is None:
        from evallab.production import live_supervisor_harbor as harbor

        trial_observation = trial_observation or harbor.trial_observation
        source = source or harbor.HarborSource()
    steps = list(run.steps)
    meta = strip_result_meta(run.meta)
    raw_stamps = [_step_epoch(s) for s in steps]
    stated_start = _epoch(run.meta.get("started_at"))
    case_end = parse_time(case.get("end_at"))
    stated_end = case_end if case_end is not None else _epoch(run.meta.get("finished_at"))
    stamps, timing = place_steps(raw_stamps, stated_start, stated_end)
    start = stated_start if stated_start is not None else (stamps[0] if stamps else SYNTHETIC_T0)
    if stamps:
        start = min(start, stamps[0])
    end = stated_end if stated_end is not None else (stamps[-1] if stamps else start)
    if case_end is None and stamps:
        end = max(end, stamps[-1])
    end = max(end, start)
    if timing in ("even_spread", "synthetic"):
        # A trace without step times has no real clock: leave the start unknown so the helper's
        # elapsed-time and silence triggers abstain instead of firing on invented times.
        meta.pop("started_at", None)
    else:
        meta["started_at"] = start
    subject = ls.Subject(kind="harbor_trial", id=str(case.get("case_id") or run.run_id), owner=owner,
                         killable=killable and owner == "lab", run_id=run.run_id)

    def observe(visible: int, now: datetime) -> ls.Observation:
        prefix = steps[:visible]
        visible_meta = {**meta, "actor_inputs_available": _actor_inputs(prefix)}
        return trial_observation(HealthRun(run.run_id, run.source, prefix, visible_meta),
                                 subject=subject, now=now)

    return Replayable(subject, source, tuple(stamps), start, end, observe,
                      onset=_onset_epoch(case.get("onset"), case.get("onset_kind"), steps, stamps), timing=timing)


def _actor_inputs(steps: Sequence[Mapping[str, Any]]) -> bool:
    from evallab.production.run_health import actor_inputs_available

    return actor_inputs_available(steps)


def ensure_store_copy(live: Path, copy: Path = DEFAULT_STORE_COPY) -> Path:
    """A point-in-time read-only copy of the cloud trace DB (``sqlite3 .backup``); reused if present."""
    if copy.is_file():
        return copy
    source = sqlite3.connect(f"file:{live.expanduser()}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(copy)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return copy


class Loader:
    """Turns a manifest row into a :class:`Replayable`; owns the store copy and a run cache."""

    def __init__(self, *, store_copy: Path = DEFAULT_STORE_COPY, registry: Any = None) -> None:
        self.store_copy = store_copy
        self._registry = registry
        self._store: dict[str, HealthRun] = {}
        self.not_admitted: set[str] = set()

    @property
    def registry(self) -> Any:
        if self._registry is None:
            from evallab.production.corpus_queue import CorpusRegistry

            self._registry = CorpusRegistry()
        return self._registry

    def preload_store(self, trace_ids: Sequence[str]) -> None:
        """Load admitted DEV store runs (registry admission) for ``trace_ids`` in one pass."""
        from evallab.production.run_health import store_runs

        wanted = [t for t in trace_ids if t not in self._store]
        for run in store_runs(self.store_copy, registry=self.registry, trace_ids=wanted):
            self._store[run.run_id] = run
        self.not_admitted |= {t for t in wanted if t not in self._store}

    def clear(self) -> None:
        self._store.clear()

    def __call__(self, case: Mapping[str, Any]) -> Replayable:
        kind = case.get("subject_kind")
        locator = case.get("locator") or {}
        if kind == "store_run" and locator.get("db"):
            trace_id = str(locator["trace_id"])
            if trace_id not in self._store:
                self.preload_store([trace_id])
            run = self._store.get(trace_id)
            if run is None:
                raise LookupError(f"{trace_id}: not returned by run_health.store_runs (registry admission refused)")
            return health_replayable(case, run)
        if kind in ("harbor_trial", "store_run"):
            from evallab.production.run_health import trial_run

            directory = Path(locator["trial_dir"])
            run_id = str(locator.get("trace_id") or directory.name)
            return health_replayable(case, trial_run(directory, run_id=run_id,
                                                     source="harbor" if kind == "harbor_trial" else "cheatbench"))
        if kind == "omp_lane":
            from evallab.production import live_supervisor_fleet as fleet

            return fleet_replayable(case, lane_observation=fleet.lane_observation, source=fleet.FleetSource())
        if kind == "seeker_turn":
            raise UnsupportedCase(
                "seeker_turn: the turn directory holds an untimed model response stream and no Harbor "
                "trajectory or OMP session, so neither source helper can observe it")
        raise ValueError(f"unsupported subject_kind {kind!r}; expected one of {SUBJECT_KINDS}")


def read_session_events(path: Path) -> list[dict[str, Any]]:
    """OMP session JSONL records that carry a timestamp, in time order (a bad line is skipped)."""
    events: list[tuple[float, int, dict[str, Any]]] = []
    for position, line in enumerate(path.expanduser().read_text(encoding="utf-8", errors="replace").splitlines()):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        stamp = _epoch(record.get("timestamp")) if isinstance(record, dict) else None
        if stamp is not None:
            events.append((stamp, position, record))
    events.sort(key=lambda e: (e[0], e[1]))
    return [e[2] for e in events]


def fleet_replayable(case: Mapping[str, Any], *, lane_observation: Callable[..., ls.Observation],
                     source: ls.Source, events: Sequence[Mapping[str, Any]] | None = None,
                     owner: str = "fleet") -> Replayable:
    """An OMP lane session window as a replayable subject.

    Every timestamped record of the session file (history before the window included, the lane
    baseline needs it) is placed on the timeline; the window bounds the evaluation. ``extra`` keys
    of the case may set ``owner``, ``agent_status`` and the queue state fed to the helper.
    """
    locator = case.get("locator") or {}
    extra = case.get("extra") or {}
    records = list(events) if events is not None else read_session_events(Path(locator["session_file"]))
    stamps = [float(t) for r in records if (t := _epoch(r.get("timestamp"))) is not None]
    records = [r for r in records if _epoch(r.get("timestamp")) is not None]
    start = parse_time(locator.get("window_start")) or (stamps[0] if stamps else SYNTHETIC_T0)
    end = parse_time(locator.get("window_end") or case.get("end_at")) or (stamps[-1] if stamps else start)
    end = max(end, start)
    subject = ls.Subject(kind="omp_lane", id=str(case.get("case_id")), owner=str(extra.get("owner") or owner),
                         killable=False, label=str(extra.get("label") or ""))
    kwargs: dict[str, Any] = {}
    if extra.get("agent_status"):
        kwargs["agent_status"] = str(extra["agent_status"])
    baseline_lo = bisect_left(stamps, start - LANE_BASELINE_SECONDS)
    baseline_hi = bisect_right(stamps, start)
    if baseline_hi - baseline_lo >= 2:
        probe = lane_observation(records[baseline_lo:baseline_hi], subject=subject, now=to_dt(start), **kwargs)
        if probe.metrics.get("median_hourly_tokens") is not None:
            kwargs["median_hourly_tokens"] = probe.metrics["median_hourly_tokens"]

    def observe(visible: int, now: datetime) -> ls.Observation:
        lo = bisect_left(stamps, now.timestamp() - LANE_LOOKBACK_SECONDS, 0, visible)
        return lane_observation(records[lo:visible], subject=subject, now=now, **kwargs)

    return Replayable(subject, source, tuple(stamps), start, end, observe,
                      onset=parse_time(case.get("onset")) if isinstance(case.get("onset"), str) else None)


def replay_case(case: Mapping[str, Any], *, decider: ls.Decider | None = None,
                loader: Callable[[Mapping[str, Any]], Replayable] | None = None) -> dict[str, Any]:
    """Load one manifest row and replay it; see :func:`replay_timeline`."""
    return replay_timeline(case, (loader or Loader())(case), decider=decider)


# --------------------------------------------------------------------------- scoring


def quantile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated quantile of ``values`` (None when empty)."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (position - low), 3)


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _spread(values: Sequence[float]) -> dict[str, Any]:
    return {"n": len(values), "p50": quantile(values, 0.5), "p90": quantile(values, 0.9)}


def _confusion(rows: Sequence[Mapping[str, Any]], predicted: Callable[[Mapping[str, Any]], bool],
               actual: Callable[[Mapping[str, Any]], bool]) -> dict[str, Any]:
    tp = [r for r in rows if predicted(r) and actual(r)]
    fp = sum(1 for r in rows if predicted(r) and not actual(r))
    fn = sum(1 for r in rows if not predicted(r) and actual(r))
    tn = len(rows) - len(tp) - fp - fn
    clocked = [r for r in tp if r.get("clock") == "observed"]
    return {
        "tp": len(tp), "fp": fp, "fn": fn, "tn": tn,
        "precision": _ratio(len(tp), len(tp) + fp), "recall": _ratio(len(tp), len(tp) + fn),
        # Real latency: only cases whose every step carried an observed timestamp.
        "detection_delay_s": _spread([r["detection_delay_s"] for r in clocked if r.get("detection_delay_s") is not None]),
        "elapsed_to_first_alarm_s": _spread([r["elapsed_to_first_alarm_s"] for r in clocked
                                             if r.get("elapsed_to_first_alarm_s") is not None]),
        "lead_before_end_s": _spread([r["lead_before_end_s"] for r in clocked if r.get("lead_before_end_s") is not None]),
        # Step-order diagnostics: clock-free, never reported as latency.
        "steps_at_alarm": _spread([r["steps_at_alarm"] for r in tp if r.get("steps_at_alarm") is not None]),
    }


def _block(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    positives = [r for r in rows if r["label_positive"]]
    alarm = _confusion(rows, lambda r: r["action"] != "continue", lambda r: bool(r["label_positive"]))
    actions = sorted(({r["action"] for r in rows} | {r["label_action"] for r in positives}) - {"continue", None})
    per_action = {
        a: _confusion(rows, lambda r, a=a: r["action"] == a, lambda r, a=a: bool(r["label_positive"]) and r["label_action"] == a)
        for a in actions
    }
    matched = sum(1 for r in positives if r["action"] == r["label_action"])
    clocks = {"observed": sum(1 for r in rows if r.get("clock") == "observed"),
              "inferred": sum(1 for r in rows if r.get("clock") != "observed")}
    return {
        "n": len(rows), "positives": len(positives), "clock": clocks,
        "alarm": alarm, "actions": per_action,
        "action_match": {"matched": matched, "of": len(positives), "rate": _ratio(matched, len(positives))},
        "alarm_match": {"matched": alarm["tp"], "of": len(positives), "rate": alarm["recall"]},
    }


def _not_scored(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("unknown_no_actor"):
            entry = out.setdefault(str(r["set"]), {"n": 0, "positives": 0,
                                                   "reason": "no actor transcript at the case end (unknown, not clean)"})
            entry["n"] += 1
            entry["positives"] += bool(r["label_positive"])
    return out


def _skipped(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("skipped"):
            entry = out.setdefault(str(r["set"]), {"n": 0, "positives": 0, "reason": r["skipped"]})
            entry["n"] += 1
            entry["positives"] += bool(r["label_positive"])
    return out


def score(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Per set and overall: alarm and per-action TP/FP/FN/TN, precision, recall, time-to-detection
    (p50/p90 of ``detection_delay_s``, ``elapsed_to_first_alarm_s`` and ``lead_before_end_s`` over
    true positives with an observed clock only; ``steps_at_alarm`` over all true positives, a
    clock-free step-order diagnostic), and action/alarm match rates over positives.

    ``action`` precision/recall are exact-action: a kill proposed where the label says
    ``alert_research`` is an FP for the kill and a FN for the alert. Rows with an ``error``, ``skipped`` or ``unknown_no_actor`` are
    not scored and are listed under ``errors`` / ``skipped`` / ``not_scored``.
    """
    all_rows = list(rows)
    scored = [r for r in all_rows if not r.get("error") and not r.get("skipped") and not r.get("unknown_no_actor")]
    by_set: dict[str, list[Mapping[str, Any]]] = {}
    for r in scored:
        by_set.setdefault(str(r["set"]), []).append(r)
    return {
        "schema": "live-supervisor-replay-score/v1",
        "n_rows": len(all_rows), "n_scored": len(scored),
        "errors": {str(r.get("case_id")): r["error"] for r in all_rows if r.get("error")},
        "skipped": _skipped(all_rows),
        "not_scored": _not_scored(all_rows),
        "sets": {name: _block(group) for name, group in sorted(by_set.items())},
        "overall": _block(scored),
    }


# --------------------------------------------------------------------------- CLI


def read_manifest(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.expanduser().read_text(encoding="utf-8").splitlines() if line.strip()]


def _error_row(case: Mapping[str, Any], exc: Exception) -> dict[str, Any]:
    row = {"case_id": case.get("case_id"), "set": case.get("set"), "subject_kind": case.get("subject_kind"),
           "label_positive": bool(case.get("label_positive")), "label_action": case.get("label_action"),
           "label_kind": case.get("label_kind")}
    if isinstance(exc, UnsupportedCase):
        return row | {"skipped": str(exc)}
    return row | {"error": f"{type(exc).__name__}: {exc}"}


def _is_store(case: Mapping[str, Any]) -> bool:
    return case.get("subject_kind") == "store_run" and bool((case.get("locator") or {}).get("db"))


def _run_serial(cases: Sequence[Mapping[str, Any]], decider: ls.Decider | None,
                loader: Loader) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    windows: list[dict[str, Any]] = []
    store_ids = [str(c["locator"]["trace_id"]) for c in cases if _is_store(c)]
    loaded: set[str] = set()
    for case in cases:
        if _is_store(case) and str(case["locator"]["trace_id"]) not in loaded:
            index = store_ids.index(str(case["locator"]["trace_id"]))
            batch = store_ids[index:index + STORE_CHUNK]
            loader.clear()
            try:
                loader.preload_store(batch)
            except Exception as exc:  # noqa: BLE001 - each case below reports its own failure
                print(f"store preload failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            loaded = set(batch)
        row: dict[str, Any]
        try:
            row = replay_case(case, decider=decider, loader=loader)
        except Exception as exc:  # noqa: BLE001 - one bad case must not stop the replay
            row = _error_row(case, exc)
        finding = row.pop("finding", None)
        if finding is not None:
            findings.append(finding)
        window = row.pop("window", None)
        if window is not None:
            windows.append(window)
        rows.append(row)
    return rows, findings, windows


def _run_chunk(cases: Sequence[Mapping[str, Any]], store_copy: Path
               ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    return _run_serial(cases, None, Loader(store_copy=store_copy))


def run_cases(cases: Sequence[Mapping[str, Any]], *, decider: ls.Decider | None = None,
              loader: Loader | None = None, workers: int = 1,
              store_copy: Path = DEFAULT_STORE_COPY
              ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Replay every case; returns (rows in case order, findings, adjudication windows). A case that cannot be loaded or
    replayed yields a row with ``error`` instead of stopping the run. ``workers > 1`` fans
    :data:`STORE_CHUNK`-bounded chunks (about four per worker) out to processes (rule decider only: a decider is not shipped)."""
    if workers <= 1:
        return _run_serial(cases, decider, loader or Loader(store_copy=store_copy))
    if decider is not None or loader is not None:
        raise ValueError("workers > 1 replays with the rule decider and a default loader only")
    size = max(1, min(STORE_CHUNK, len(cases) // (workers * 4) or 1))
    chunks = [list(cases[i:i + size]) for i in range(0, len(cases), size)]
    parts: list[tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]] = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_run_chunk, chunk, store_copy) for chunk in chunks]
        for done, future in enumerate(as_completed(futures), 1):
            parts.append(future.result())
            print(f"replay: {done}/{len(chunks)} chunks", file=sys.stderr, flush=True)
    position = {str(c.get("case_id")): i for i, c in enumerate(cases)}
    rows = sorted((r for rs, _, _ in parts for r in rs), key=lambda r: position.get(str(r.get("case_id")), 0))
    findings = [f for _, fs, _ in parts for f in fs]
    windows = [w for _, _, ws in parts for w in ws]
    return rows, findings, windows


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="live_supervisor_replay", description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, default=DEFAULT_ROOT / "manifest-v1.jsonl")
    parser.add_argument("--out", type=Path, required=True, help="rows JSONL (one per case)")
    parser.add_argument("--set", action="append", dest="sets", help="only this set (repeatable)")
    parser.add_argument("--score-out", type=Path, help="score table JSON (default: <out stem>-score.json)")
    parser.add_argument("--findings-out", type=Path, help="trace-findings/v1 JSONL (default: <out stem>-findings.jsonl)")
    parser.add_argument("--windows-out", type=Path, help="adjudication windows JSONL (default: <out stem>-windows.jsonl)")
    parser.add_argument("--store-copy", type=Path, default=DEFAULT_STORE_COPY,
                        help="read-only copy of the cloud trace DB; created with sqlite backup from the manifest's db if missing")
    parser.add_argument("--limit-per-set", type=int, help="replay only the first N cases of each set (smoke runs)")
    parser.add_argument("--workers", type=int, default=1, help="processes (rule decider only)")
    args = parser.parse_args(argv)
    cases = read_manifest(args.manifest)
    if args.sets:
        cases = [c for c in cases if c.get("set") in args.sets]
    if args.limit_per_set is not None:
        taken: dict[str, int] = {}
        kept = []
        for case in cases:
            taken[case["set"]] = taken.get(case["set"], 0) + 1
            if taken[case["set"]] <= args.limit_per_set:
                kept.append(case)
        cases = kept
    live = next((Path(c["locator"]["db"]) for c in cases
                 if c.get("subject_kind") == "store_run" and (c.get("locator") or {}).get("db")), None)
    if live is not None:
        ensure_store_copy(live, args.store_copy)
    rows, findings, windows = run_cases(cases, workers=args.workers, store_copy=args.store_copy)
    out = args.out.expanduser()
    _write_jsonl(out, rows)
    _write_jsonl(args.windows_out or out.with_name(f"{out.stem}-windows.jsonl"), windows)
    _write_jsonl(args.findings_out or out.with_name(f"{out.stem}-findings.jsonl"), findings)
    table = score(rows)
    (args.score_out or out.with_name(f"{out.stem}-score.json")).write_text(
        json.dumps(table, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"rows": len(rows), "findings": len(findings), "windows": len(windows),
                      "errors": len(table["errors"]),
                      "overall_alarm": {k: table["overall"]["alarm"][k] for k in ("tp", "fp", "fn", "tn", "precision", "recall")}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
