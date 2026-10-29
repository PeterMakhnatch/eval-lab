"""Live Supervisor source ``harbor``: live Harbor trials on this Mac, read-only.

A trial is live when it has an ``agent/`` directory (or a root trajectory), no ``result.json``,
its job has not finished, and its newest file is at most 24 h old. ``HarborSource.observe``
turns each one into an ``Observation`` with Run Health features
(:mod:`evallab.production.run_health`), the last 20 steps, the job's unacknowledged
``live_watch`` alerts (``<job>/watch/alerts.jsonl`` minus ``acks.jsonl``) and any trace-harness
``alarm`` findings for the run. ``HarborSource.triggers`` is a pure function of that observation,
so a replay can build observations at simulated times with :func:`trial_observation` and reuse
the same trigger code.

Triggers (thresholds fixed a priori, not tuned on a label set; each comment names its origin):
``silence`` (15 min), ``repetition`` (Run Health loop alarm), ``wall_time`` (agent steps or
per-source p99 wall), ``error_signature`` (Run Health infra tail or live_watch infra alerts),
``burn`` (prompt tokens per step) and ``trace_alarm`` (live_watch integrity alerts or a trace
harness ``cheat`` alarm). Nothing here writes, signals or stops a process.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from evallab.auto_watch import annotate_alerts_with_acks, read_watch_acks, read_watch_alerts
from evallab.harbor_watch_hooks import read_hook_journal
from evallab.production import run_health
from evallab.production.live_supervisor import Event, Observation, Subject, TriggerHit
from evallab.production.run_health import HealthRun, _epoch, _observation_text
from evallab.token_flow import _step_calls, _stitched_steps

SOURCE_NAME = "harbor"
DEFAULT_EVAL_LAB = Path("/Users/petermakhnatch/Developer/eval-lab")
DEFAULT_FINDINGS = Path("~/.local/state/evallab-prod-live/trace-harness/findings-v1.jsonl")
LIVE_WINDOW = timedelta(hours=24)
EVENT_WINDOW = 20
EVENT_TEXT_CHARS = 600

#: No new step and no trajectory write for 15 min. Run Health's latency gap alarm is 2,700 s
#: (3x this, ``LATENCY_MAX_GAP_SECONDS``); the live watch ``stalled`` rule is 10 min.
SILENCE_SECONDS = 900.0
#: Wall-time p99 per benchmark source (DEV distribution, run-health.md RH-1: Goodfire 5,877 s,
#: TB4 5,371 s); any other source uses the Run Health runaway wall (``RUNAWAY_WALL_SECONDS``).
WALL_P99_SECONDS: Mapping[str, float] = {"goodfire": 5877.0, "tb4": 5371.0}
#: TB4 p99 of prompt tokens per agent step (run-health.md RH-1).
PROMPT_TOKENS_PER_STEP = 227_937.0

#: ``live_watch`` rules by the trigger they feed. Anything else (``stalled``, ``repetition``,
#: ``parse_errors``, ...) is not a trigger here: silence and Run Health cover them.
INTEGRITY_RULES = frozenset({"hidden_test_read", "grader_tamper", "history_mining", "git_object_read",
                             "copy_acquired", "harness_log_read"})
INFRA_RULES = frozenset({"infra_error", "infra_spike", "proxy_errors", "proxy_error_spike"})
BURN_RULES = frozenset({"budget_burn", "spend"})
BURN_RULE_PREFIX = "spend_cap_"

_SOURCE_MARKERS = (("goodfire", "goodfire"), ("tb4", "tb4"), ("tb-4", "tb4"), ("terminal-bench", "tb4"),
                   ("terminal_bench", "tb4"))


# --------------------------------------------------------------------------- pure observation


def _iso(epoch: float | None) -> datetime | None:
    return None if epoch is None else datetime.fromtimestamp(epoch, UTC)


def _step_index(step: Mapping[str, Any], position: int) -> int:
    step_id = step.get("step_id")
    if isinstance(step_id, int) and not isinstance(step_id, bool):
        return step_id
    if isinstance(step_id, str) and step_id.strip().isdigit():
        return int(step_id)
    return position + 1


#: A Run Health ``infra`` class is a dead or failed environment, not a one-off, only when it matches
#: this many consecutive tool outputs (the Run Health tail length). The post-hoc detector gets
#: persistence for free because the failure ended the run; live, the last step is always "now", so
#: a single output the agent provoked or printed (``strings``, an offline ``pip``, its own OOM
#: experiment) fired a kill proposal and the run went on. A later clean tool output resets the run.
INFRA_PERSIST_STEPS = run_health.INFRA_TAIL_STEPS


def _infra_runs(steps: Sequence[Mapping[str, Any]], classes: Iterable[str]) -> dict[str, list[int]]:
    """Per Run Health infra class, the newest ``INFRA_PERSIST_STEPS`` consecutive tool outputs
    (step indices, oldest first) that carry it; stops at the first output that does not.

    Blank outputs are skipped. Looked up through ``run_health`` so a replay's memoised
    ``_output_text`` and signature patterns apply."""
    wanted = set(classes)
    patterns = {name: pattern for name, pattern in run_health.INFRA_SIGNATURES if name in wanted}
    runs: dict[str, list[int]] = {name: [] for name in patterns}
    live = set(patterns)
    for position in range(len(steps) - 1, -1, -1):
        if not live:
            break
        text = run_health._output_text(steps[position])
        if not text.strip():
            continue
        for name in tuple(live):
            if not patterns[name].search(text):
                live.discard(name)
                continue
            runs[name].append(_step_index(steps[position], position))
            if len(runs[name]) >= INFRA_PERSIST_STEPS:
                live.discard(name)
    return {name: ids[::-1] for name, ids in runs.items()}


def _event_text(step: Mapping[str, Any]) -> str:
    message = step.get("message")
    parts = [" ".join(message.split())[:300]] if isinstance(message, str) and message.strip() else []
    parts += [f"$ {' '.join(text.split())[:200]}" for _, text in _step_calls(dict(step)) if text.strip()]
    output = " ".join(_observation_text(step).split())
    if output:
        parts.append(f"-> {output[-200:]}")
    return " | ".join(parts)[:EVENT_TEXT_CHARS]


def _step_ref(alert: Mapping[str, Any]) -> tuple[int, ...]:
    ref = alert.get("step_ref")
    tail = ref.rsplit("#", 1)[-1] if isinstance(ref, str) else ""
    return (int(tail),) if tail.isdigit() else ()


def _alert_view(alert: Mapping[str, Any]) -> dict[str, Any]:
    return {"rule": str(alert.get("rule") or ""), "detail": str(alert.get("detail") or alert.get("quote") or "")[:200],
            "step_ref": alert.get("step_ref"), "trial": alert.get("trial"),
            "first_seen": alert.get("first_seen")}


def _seen_by(alert: Mapping[str, Any], now: datetime) -> bool:
    """An alert recorded after ``now`` does not exist yet at ``now`` (replay)."""
    seen = alert.get("first_seen")
    moment = _epoch(seen) if isinstance(seen, str) else None
    return moment is None or moment <= now.timestamp()


def _wall_p99(source: str) -> float:
    return WALL_P99_SECONDS.get(source, float(run_health.RUNAWAY_WALL_SECONDS))


def trial_observation(run: HealthRun, *, subject: Subject, now: datetime, alerts: Sequence[Mapping[str, Any]] = (),
                      mtime: datetime | None = None,
                      trace_alarms: Sequence[Mapping[str, Any]] = ()) -> Observation:
    """What the supervisor sees of ``run`` at ``now``; steps stamped after ``now`` are ignored.

    ``alerts`` are ``live_watch`` alert rows for this trial (a row with ``acknowledged_by`` or a
    ``first_seen`` after ``now`` is dropped); ``trace_alarms`` are trace-harness findings for this
    run (only ``verdict == "alarm"``, ``kind == "cheat"`` count). ``mtime`` is the newest trajectory
    write, used with the last step time for ``silence_seconds``; without it silence is measured from
    the last timestamped step only and is None when there is none. ``run.source`` selects the
    wall-time p99; ``run.meta['started_at']`` (epoch seconds) is the trial start, else the first step.
    """
    cutoff = now.timestamp()
    steps = [s for s in run.steps if (t := _epoch(s.get("timestamp"))) is None or t <= cutoff]
    cut = HealthRun(run_id=run.run_id, source=run.source, steps=steps, meta=run.meta)
    feats = run_health.features(cut)
    health = {}
    for record in run_health.detect(cut, feats):
        if record["kind"] in ("loop", "infra") and record["verdict"] == "alarm":
            health[record["kind"]] = {"score": record["score"], "evidence_steps": record["evidence_steps"],
                                      "summary": record["summary"]}
    if "infra" in health:
        health["infra"]["runs"] = _infra_runs(steps, feats["infra_tail"])

    stamps = [t for s in steps if (t := _epoch(s.get("timestamp"))) is not None]
    written = mtime.timestamp() if mtime is not None and mtime.timestamp() <= cutoff else None
    active = [t for t in (max(stamps, default=None), written) if t is not None]
    start = _epoch(run.meta.get("started_at"))
    if start is None:
        start = min(stamps, default=None)
    open_alerts = [a for a in alerts if _seen_by(a, now) and not (a.get("acknowledged_by") or a.get("acked"))]
    metrics: dict[str, Any] = {
        "source": run.source,
        "silence_seconds": round(max(0.0, cutoff - max(active)), 3) if active else None,
        "elapsed_seconds": round(cutoff - start, 3) if start is not None and start <= cutoff else None,
        "agent_steps": feats["agent_steps"],
        "prompt_tokens_per_step": feats["prompt_tokens_per_agent_step"],
        "wall_p99_seconds": _wall_p99(run.source),
        "last_step": _step_index(steps[-1], len(steps) - 1) if steps else None,
        "health": health,
        "alerts": [_alert_view(a) for a in open_alerts],
        "trace_alarms": [{"detector": str(r.get("detector") or ""), "score": float(r.get("score") or 1.0),
                          "summary": str(r.get("summary") or "")[:200],
                          "evidence_steps": [s for s in r.get("evidence_steps") or [] if isinstance(s, int)][:5]}
                         for r in trace_alarms if r.get("verdict") == "alarm" and r.get("kind") == "cheat"],
        "features": feats,
    }
    events = tuple(Event(index=_step_index(s, position), at=_iso(_epoch(s.get("timestamp"))),
                         source=str(s.get("source") or ""), text=_event_text(s))
                   for position, s in list(enumerate(steps))[-EVENT_WINDOW:])
    return Observation(subject=subject, observed_at=now, events=events, metrics=metrics,
                       actor_inputs_available=bool(feats["actor_inputs_available"]))


# --------------------------------------------------------------------------- triggers


def triggers_for(observation: Observation) -> list[TriggerHit]:
    """Every trigger that fired for ``observation`` (score = value / threshold >= 1.0)."""
    m = observation.metrics
    last = m.get("last_step")
    tail = (last,) if isinstance(last, int) else ()
    hits: list[TriggerHit] = []

    for alert in m.get("alerts", ()):
        rule, detail, steps = alert["rule"], f"live_watch {alert['rule']}: {alert['detail']}", _step_ref(alert)
        if rule in INTEGRITY_RULES:
            hits.append(TriggerHit("trace_alarm", "cheat", 1.0, detail, steps))
        elif rule in INFRA_RULES:
            hits.append(TriggerHit("error_signature", "infra", 1.0, detail, steps))
        elif rule in BURN_RULES or rule.startswith(BURN_RULE_PREFIX):
            hits.append(TriggerHit("burn", "runaway", 1.0, detail, steps))
    for record in m.get("trace_alarms", ()):
        hits.append(TriggerHit("trace_alarm", "cheat", max(1.0, record["score"]),
                               f"trace harness {record['detector']}: {record['summary']}",
                               tuple(record["evidence_steps"])))

    infra = m.get("health", {}).get("infra")
    if infra:
        persistent = {name: ids for name, ids in infra["runs"].items() if len(ids) >= INFRA_PERSIST_STEPS}
        if persistent:
            classes = "; ".join(f"tail:{name}" for name in persistent)
            steps = tuple(dict.fromkeys(step for ids in persistent.values() for step in ids))
            hits.append(TriggerHit("error_signature", "infra", max(1.0, infra["score"]),
                                   f"run_health infra: {classes} in the last {INFRA_PERSIST_STEPS} tool outputs", steps))
    loop = m.get("health", {}).get("loop")
    if loop:
        hits.append(TriggerHit("repetition", "loop", max(1.0, loop["score"]),
                               f"run_health loop: {loop['summary']}", tuple(loop["evidence_steps"])))

    steps_seen, elapsed, p99 = m.get("agent_steps"), m.get("elapsed_seconds"), m.get("wall_p99_seconds")
    reasons: list[tuple[float, str]] = []
    if isinstance(steps_seen, int) and steps_seen >= run_health.RUNAWAY_AGENT_STEPS:
        reasons.append((steps_seen / run_health.RUNAWAY_AGENT_STEPS,
                        f"agent_steps={steps_seen} (>= {run_health.RUNAWAY_AGENT_STEPS})"))
    if elapsed is not None and p99 and elapsed >= p99:
        reasons.append((elapsed / p99, f"elapsed={elapsed:.0f}s (>= {p99:.0f}s source p99)"))
    if reasons:
        score, _ = max(reasons)
        hits.append(TriggerHit("wall_time", "runaway", score, "; ".join(r for _, r in reasons), tail))

    prompt = m.get("prompt_tokens_per_step")
    if prompt is not None and prompt >= PROMPT_TOKENS_PER_STEP:
        hits.append(TriggerHit("burn", "runaway", prompt / PROMPT_TOKENS_PER_STEP,
                               f"prompt tokens per step {prompt:.0f} (>= {PROMPT_TOKENS_PER_STEP:.0f})", tail))

    silence = m.get("silence_seconds")
    if silence is not None and silence >= SILENCE_SECONDS:
        hits.append(TriggerHit("silence", "latency", silence / SILENCE_SECONDS,
                               f"no step or trajectory write for {silence:.0f}s (>= {SILENCE_SECONDS:.0f}s)", tail))
    return hits


# --------------------------------------------------------------------------- discovery


def default_roots(eval_lab: Path = DEFAULT_EVAL_LAB, home: Path | None = None) -> list[Path]:
    """Run roots that can hold lab-started Harbor jobs: the checkout, its ``.worktrees`` and the
    ``~/Developer/eval-lab-worktrees`` checkouts."""
    base = (home or Path.home()) / "Developer" / "eval-lab-worktrees"
    return [eval_lab / "runs", *sorted(eval_lab.glob(".worktrees/*/runs")), *sorted(base.glob("*/runs"))]


@dataclass(frozen=True)
class LiveTrial:
    job_dir: Path
    trial_dir: Path
    #: Newest mtime of the trial's own files (any of them), for the 24 h liveness window.
    newest: float


def _entries(path: Path) -> Iterator[os.DirEntry[str]]:
    try:
        with os.scandir(path) as scan:
            yield from list(scan)
    except OSError:
        return


def _job_dirs(root: Path) -> Iterator[Path]:
    """Job directories under a runs root, including the jobs staged in ``.exec-stage``."""
    for entry in _entries(root):
        if not entry.is_dir():
            continue
        if entry.name == ".exec-stage":
            yield from (Path(e.path) for e in _entries(Path(entry.path)) if e.is_dir())
        elif not entry.name.startswith("."):
            yield Path(entry.path)


def _newest(path: Path) -> float:
    """Newest mtime among a directory and its direct entries (shallow, one stat per entry)."""
    newest = 0.0
    try:
        newest = path.stat().st_mtime
    except OSError:
        return newest
    for entry in _entries(path):
        try:
            newest = max(newest, entry.stat().st_mtime)
        except OSError:
            continue
    return newest


def _job_finished(job_dir: Path) -> bool:
    try:
        payload = json.loads((job_dir / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("finished_at") is not None


def live_trials(roots: Iterable[Path], now: datetime, *, window: timedelta = LIVE_WINDOW) -> list[LiveTrial]:
    """Live trials under ``roots`` (see module docstring), oldest job path first."""
    horizon = now.timestamp() - window.total_seconds()
    found: list[LiveTrial] = []
    for root in roots:
        for job_dir in _job_dirs(root):
            candidates = [Path(e.path) for e in _entries(job_dir) if e.is_dir() and "__" in e.name]
            live = [t for t in candidates
                    if ((t / "agent").is_dir() or (t / "trajectory.json").is_file()) and not (t / "result.json").exists()]
            if not live or _job_finished(job_dir):
                continue
            for trial_dir in live:
                newest = max(_newest(trial_dir), _newest(trial_dir / "agent"))
                if newest >= horizon:
                    found.append(LiveTrial(job_dir, trial_dir, newest))
    return found


def _source_of(*configs: Mapping[str, Any]) -> str:
    """Benchmark source named by the task/dataset fields of the trial and job configs, else ``harbor``."""
    text = json.dumps([c.get(key) for c in configs for key in ("task", "tasks", "datasets")]).lower()
    return next((name for marker, name in _SOURCE_MARKERS if marker in text), "harbor")


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _job_started(job_dir: Path) -> float | None:
    return _epoch(_read_json(job_dir / "lock.json").get("created_at"))


def _is_under(path: Path, roots: Iterable[Path]) -> bool:
    return any(path.is_relative_to(root) for root in roots)


# --------------------------------------------------------------------------- source


class HarborSource:
    """Live Harbor trials as supervisor subjects (``name == "harbor"``).

    ``roots`` default to :func:`default_roots` evaluated at each ``observe``. ``lab_roots`` (default:
    the same roots) are the roots whose jobs the lab started, the only killable ones.
    ``findings_path`` is the trace-harness findings JSONL read for ``cheat`` alarms.
    """

    name = SOURCE_NAME

    def __init__(self, roots: Sequence[Path] | None = None, *, lab_roots: Sequence[Path] | None = None,
                 eval_lab: Path = DEFAULT_EVAL_LAB, home: Path | None = None,
                 findings_path: Path = DEFAULT_FINDINGS) -> None:
        self._roots = None if roots is None else [Path(r) for r in roots]
        self._lab_roots = None if lab_roots is None else [Path(r) for r in lab_roots]
        self._eval_lab = eval_lab
        self._home = home
        self._findings_path = findings_path.expanduser()

    def triggers(self, observation: Observation) -> list[TriggerHit]:
        return triggers_for(observation)

    def observe(self, now: datetime) -> Iterator[Observation]:
        roots = self._roots if self._roots is not None else default_roots(self._eval_lab, self._home)
        lab_roots = self._lab_roots if self._lab_roots is not None else roots
        alarms = self._trace_alarms()
        seen: set[str] = set()
        for live in live_trials(roots, now):
            subject_id = f"{live.job_dir.name}/{live.trial_dir.name}"
            if subject_id in seen:
                continue
            seen.add(subject_id)
            try:
                yield self._observe_trial(live, subject_id, now, lab_roots, alarms)
            except (OSError, ValueError):
                continue  # the trial finished and was rewritten mid-scan; the next scan sees it whole

    def _observe_trial(self, live: LiveTrial, subject_id: str, now: datetime, lab_roots: Sequence[Path],
                       alarms: Mapping[str, list[Mapping[str, Any]]]) -> Observation:
        job_dir, trial_dir = live.job_dir, live.trial_dir
        config = _read_json(trial_dir / "config.json")
        agent = config.get("agent")
        agent_name = agent.get("name") if isinstance(agent, Mapping) else None
        label = f"{trial_dir.name} ({agent_name})" if agent_name else trial_dir.name
        subject = Subject(kind="harbor_trial", id=subject_id, owner="lab",
                          killable=_is_under(job_dir, lab_roots), run_id=trial_dir.name, label=label)
        hooks = read_hook_journal(job_dir).get(trial_dir.name)
        start = next((_epoch(at) for event, at in (hooks or {}).get("events", ()) if event == "start"), None)
        if start is None:
            start = _job_started(job_dir)
        steps, _reason = _stitched_steps(trial_dir)
        meta: dict[str, Any] = {"started_at": start}
        run = HealthRun(run_id=trial_dir.name, source=_source_of(config, _read_json(job_dir / "config.json")),
                        steps=steps or [], meta=meta)
        written = max(_newest(trial_dir / "agent"), (hooks or {}).get("last_at") or 0.0)
        acks = read_watch_acks(job_dir)
        alerts = [a for a in annotate_alerts_with_acks(read_watch_alerts(job_dir), acks)
                  if a.get("trial") == trial_dir.name or str(a.get("trial") or "").startswith("fleet:")]
        observation = trial_observation(run, subject=subject, now=now, alerts=alerts,
                                        mtime=_iso(written) if written else None,
                                        trace_alarms=[*alarms.get(trial_dir.name, []), *alarms.get(subject_id, [])])
        if hooks and hooks.get("terminal"):
            return Observation(subject=subject, observed_at=now, events=observation.events,
                               metrics=observation.metrics,
                               actor_inputs_available=observation.actor_inputs_available, terminal=True)
        return observation

    def _trace_alarms(self) -> dict[str, list[Mapping[str, Any]]]:
        """``cheat`` alarm findings by run id (the live supervisor's own records excluded)."""
        alarms: dict[str, list[Mapping[str, Any]]] = {}
        try:
            lines = self._findings_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return alarms
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if (isinstance(record, dict) and record.get("verdict") == "alarm" and record.get("kind") == "cheat"
                    and record.get("detector") != "live_supervisor" and isinstance(record.get("run_id"), str)):
                alarms.setdefault(record["run_id"], []).append(record)
        return alarms
