"""Live Supervisor core: triggered subjects -> one decision -> one ``trace-findings/v1`` record.

Design: ``docs/live-supervisor.md``. A *source* observes one class of live work (Harbor
trials, Modal apps and launchd services, the OMP fleet) and returns an
:class:`Observation` per subject plus the deterministic :class:`TriggerHit` list for it.
Only triggered subjects reach a decider. :func:`propose` gives the rule-table action. A
decider (rules only, or a model reader) may overrule it. :func:`guard` then enforces the
safety invariants regardless of who decided. Every decision is appended as a
``trace-findings/v1`` record with detector ``live_supervisor``.

Shadow mode is the default: decisions are recorded and nothing is acted on. An actuator
runs only in ``act`` mode and only for subjects marked killable, which requires a
lab-owned subject.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

FINDING_SCHEMA = "trace-findings/v1"
DETECTOR = "live_supervisor"
DETECTOR_VERSION = "live-supervisor/1"
DEFAULT_ROOT = Path("~/.local/state/evallab-prod-live/live-supervisor")
DECISIONS_FILE = "decisions-v1.jsonl"
STATE_FILE = "state.json"

ACTIONS = ("continue", "nudge", "kill_and_retry", "kill_and_mark_infra", "flag_cheating", "alert_research")
KILL_ACTIONS = frozenset({"kill_and_retry", "kill_and_mark_infra"})
KINDS = ("cheat", "loop", "runaway", "latency", "infra", "other")
#: Trigger names, in decision priority order (first wins when several fire).
TRIGGERS = ("trace_alarm", "error_signature", "repetition", "wall_time", "burn", "silence")
OWNERS = ("lab", "fleet", "peter", "production", "other")
MODES = ("shadow", "act")
#: Subjects whose owner can receive a nudge (a queue-append item); everything else
#: that would be nudged goes to research instead.
NUDGEABLE_KINDS = frozenset({"omp_lane"})
#: A silence hit proposes a kill only at this multiple of its threshold (15 min -> 45 min,
#: the Run Health latency gap); below it a killable subject is left to continue.
SILENCE_KILL_RATIO = 3.0
#: The same subject with the same trigger fingerprint is recorded at most once per window.
DEFAULT_COOLDOWN = timedelta(minutes=30)
SUMMARY_WORDS = 40
SUMMARY_CHARS = 600
MAX_EVIDENCE = 5


# --------------------------------------------------------------------------- contract types


@dataclass(frozen=True)
class Subject:
    """One thing that runs live. ``killable`` is only ever true for lab-owned subjects."""

    kind: str
    id: str
    owner: str
    killable: bool = False
    #: Store trace id when the subject is a stored run; otherwise ``live:<kind>:<id>``.
    run_id: str = ""
    label: str = ""

    def __post_init__(self) -> None:
        if self.owner not in OWNERS:
            raise ValueError(f"owner {self.owner!r} not in {OWNERS}")
        if self.killable and self.owner != "lab":
            raise ValueError(f"{self.kind}:{self.id} is owned by {self.owner}; only lab subjects are killable")
        if not self.kind or not self.id:
            raise ValueError("subject kind and id are required")

    @property
    def finding_run_id(self) -> str:
        return self.run_id or f"live:{self.kind}:{self.id}"

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "owner": self.owner, "killable": self.killable,
                "label": self.label}


@dataclass(frozen=True)
class Event:
    """One step, log line or session event in a subject's recent window."""

    index: int
    at: datetime | None
    source: str
    text: str


@dataclass(frozen=True)
class Observation:
    """What a source saw of one subject at ``observed_at``."""

    subject: Subject
    observed_at: datetime
    events: tuple[Event, ...] = ()
    metrics: Mapping[str, Any] = field(default_factory=dict)
    #: False without an actor transcript: the finding is always ``unknown``.
    actor_inputs_available: bool = True
    #: The subject already finished; it is reported by Run Health, not supervised.
    terminal: bool = False


@dataclass(frozen=True)
class TriggerHit:
    """One deterministic trigger that fired; ``score`` is value / threshold (>= 1.0)."""

    trigger: str
    kind: str
    score: float
    detail: str
    evidence_steps: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.trigger not in TRIGGERS:
            raise ValueError(f"trigger {self.trigger!r} not in {TRIGGERS}")
        if self.kind not in KINDS:
            raise ValueError(f"kind {self.kind!r} not in {KINDS}")
        if not self.score >= 1.0:
            raise ValueError(f"a fired trigger has score >= 1.0, got {self.score!r}")

    def as_dict(self) -> dict[str, Any]:
        return {"trigger": self.trigger, "kind": self.kind, "score": round(self.score, 4),
                "detail": self.detail, "evidence_steps": list(self.evidence_steps)}


@dataclass(frozen=True)
class Decision:
    action: str
    kind: str
    reason: str
    reader_model: str | None = None
    confidence: float | None = None
    evidence_steps: tuple[int, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"action {self.action!r} not in {ACTIONS}")
        if self.kind not in KINDS:
            raise ValueError(f"kind {self.kind!r} not in {KINDS}")


class Source(Protocol):
    """One class of live work. ``observe`` must be read-only toward the subjects."""

    name: str

    def observe(self, now: datetime) -> Iterable[Observation]: ...

    def triggers(self, observation: Observation) -> list[TriggerHit]: ...


class Decider(Protocol):
    def decide(self, observation: Observation, hits: Sequence[TriggerHit], proposal: Decision) -> Decision: ...


class Actuator(Protocol):
    """Carries out a decision in ``act`` mode; returns whether anything was done."""

    def act(self, observation: Observation, decision: Decision) -> bool: ...


class WindowSink(Protocol):
    """Persists the observation behind one recorded decision (``live_supervisor_windows.WindowLog``)."""

    def write(self, record: Mapping[str, Any], observation: Observation, hits: Sequence[TriggerHit]) -> None: ...


# --------------------------------------------------------------------------- rules


def _ordered(hits: Sequence[TriggerHit]) -> list[TriggerHit]:
    return sorted(hits, key=lambda h: (TRIGGERS.index(h.trigger), -h.score))


def propose(subject: Subject, hits: Sequence[TriggerHit]) -> Decision:
    """The rule-table action for the highest-priority hit (no model)."""
    if not hits:
        return Decision("continue", "other", "no trigger fired")
    top = _ordered(hits)[0]
    lane = subject.kind in NUDGEABLE_KINDS
    if top.trigger == "trace_alarm":
        action = "flag_cheating"
    elif top.trigger == "error_signature":
        action = "kill_and_mark_infra" if subject.killable else "alert_research"
    elif top.trigger == "silence":
        if lane:
            action = "nudge"
        elif subject.killable:
            action = "kill_and_retry" if top.score >= SILENCE_KILL_RATIO else "continue"
        else:
            action = "alert_research"
    elif subject.killable:
        action = "kill_and_retry"
    elif lane and top.trigger in ("repetition", "wall_time"):
        action = "nudge"
    else:
        action = "alert_research"
    steps = tuple(dict.fromkeys(s for h in _ordered(hits) for s in h.evidence_steps))[:MAX_EVIDENCE]
    return Decision(action, top.kind, f"{top.trigger}: {top.detail}", evidence_steps=steps)


def guard(subject: Subject, decision: Decision) -> Decision:
    """Safety invariants that hold whoever decided: never kill a subject that is not
    killable, and never nudge a subject that has no nudge channel."""
    if decision.action in KILL_ACTIONS and not subject.killable:
        return replace(decision, action="alert_research",
                       reason=f"guard: {decision.action} refused for {subject.owner}-owned "
                              f"{subject.kind}; {decision.reason}")
    if decision.action == "nudge" and subject.kind not in NUDGEABLE_KINDS:
        return replace(decision, action="alert_research",
                       reason=f"guard: no nudge channel for {subject.kind}; {decision.reason}")
    return decision


class RuleDecider:
    """Accepts the rule-table proposal unchanged (no model, $0)."""

    def decide(self, observation: Observation, hits: Sequence[TriggerHit], proposal: Decision) -> Decision:
        del observation, hits
        return proposal


# --------------------------------------------------------------------------- records


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _summary(text: str) -> str:
    words = " ".join(text.split()).split(" ")
    out = " ".join(words[:SUMMARY_WORDS])
    return out[:SUMMARY_CHARS]


def verdict_for(observation: Observation, decision: Decision) -> str:
    if not observation.actor_inputs_available:
        return "unknown"
    return "alarm" if decision.action != "continue" else "clear"


def finding(observation: Observation, hits: Sequence[TriggerHit], decision: Decision, *,
            proposal: Decision, mode: str, acted: bool, produced_at: datetime) -> dict[str, Any]:
    """One ``trace-findings/v1`` record. Extra keys are ignored by the CE-23 importer."""
    if mode not in MODES:
        raise ValueError(f"mode {mode!r} not in {MODES}")
    if acted and mode != "act":
        raise ValueError("a shadow-mode decision cannot have acted")
    known = {e.index for e in observation.events} | {s for h in hits for s in h.evidence_steps}
    cited = decision.evidence_steps or proposal.evidence_steps
    evidence = [s for s in dict.fromkeys(cited) if isinstance(s, int) and s in known][:MAX_EVIDENCE]
    return {
        "run_id": observation.subject.finding_run_id,
        "detector": DETECTOR,
        "detector_version": DETECTOR_VERSION,
        "reader_model": decision.reader_model,
        "verdict": verdict_for(observation, decision),
        "score": round(max((h.score for h in hits), default=0.0), 4),
        "kind": decision.kind,
        "evidence_steps": evidence,
        "summary": _summary(f"{decision.action}: {decision.reason}"),
        "input_tokens": decision.input_tokens,
        "output_tokens": decision.output_tokens,
        "cost_usd": decision.cost_usd,
        "latency_ms": decision.latency_ms,
        "produced_at": _utc(produced_at),
        "action": decision.action,
        "proposal": proposal.action,
        "confidence": decision.confidence,
        "mode": mode,
        "acted": acted,
        "subject": observation.subject.as_dict(),
        "triggers": [h.as_dict() for h in _ordered(hits)],
        "observed_at": _utc(observation.observed_at),
    }


def fingerprint(subject: Subject, hits: Sequence[TriggerHit], proposal: Decision) -> str:
    key = [subject.kind, subject.id, proposal.action, sorted({(h.trigger, h.kind) for h in hits})]
    return hashlib.sha256(json.dumps(key).encode()).hexdigest()[:16]


class DecisionLog:
    """Append-only decision JSONL plus a small cooldown state (one file each under ``root``)."""

    def __init__(self, root: Path, *, cooldown: timedelta = DEFAULT_COOLDOWN) -> None:
        self.root = root
        self.cooldown = cooldown
        self.path = root / DECISIONS_FILE
        self.state_path = root / STATE_FILE
        self._state: dict[str, dict[str, str]] = {}
        if self.state_path.is_file():
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._state = dict(loaded.get("recorded", {}))

    def due(self, subject: Subject, key: str, now: datetime) -> bool:
        last = self._state.get(f"{subject.kind}:{subject.id}")
        if last is None or last.get("fingerprint") != key:
            return True
        return now - datetime.fromisoformat(last["at"]) >= self.cooldown

    def append(self, subject: Subject, key: str, record: Mapping[str, Any], now: datetime) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        self._state[f"{subject.kind}:{subject.id}"] = {"fingerprint": key, "at": now.isoformat()}
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"recorded": self._state}, sort_keys=True), encoding="utf-8")
        tmp.replace(self.state_path)


# --------------------------------------------------------------------------- one cycle


@dataclass
class ScanReport:
    observed: int = 0
    quiet: int = 0
    terminal: int = 0
    suppressed: int = 0
    records: list[dict[str, Any]] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        actions: dict[str, int] = {}
        for record in self.records:
            actions[record["action"]] = actions.get(record["action"], 0) + 1
        return {"observed": self.observed, "quiet": self.quiet, "terminal": self.terminal,
                "suppressed": self.suppressed, "recorded": len(self.records), "actions": actions,
                "errors": self.errors}


def scan_once(sources: Sequence[Source], decider: Decider, log: DecisionLog, *, now: datetime,
              mode: str = "shadow", actuator: Actuator | None = None,
              windows: WindowSink | None = None) -> ScanReport:
    """Observe every source once, decide on triggered subjects and record each decision.

    A failing source is reported and skipped. A reader failure alerts research instead
    of executing an unconfirmed proposal. Action failures remain recorded and do not
    prevent the remaining subjects from being observed. With ``windows``, each recorded
    decision is followed by the window it was made on; that write comes after the decision
    is durable, and a failure is recorded against the subject without touching the decision.
    """
    if mode not in MODES:
        raise ValueError(f"mode {mode!r} not in {MODES}")
    report = ScanReport()
    for source in sources:
        try:
            observations = list(source.observe(now))
        except Exception as exc:  # noqa: BLE001 - one broken source must not stop the others
            report.errors[source.name] = f"{type(exc).__name__}: {exc}"
            continue
        for observation in observations:
            report.observed += 1
            if observation.terminal:
                report.terminal += 1
                continue
            try:
                hits = source.triggers(observation)
            except Exception as exc:  # noqa: BLE001 - isolate one malformed subject
                report.errors[f"trigger:{source.name}:{observation.subject.id}"] = f"{type(exc).__name__}: {exc}"
                continue
            if not hits:
                report.quiet += 1
                continue
            subject = observation.subject
            proposal = guard(subject, propose(subject, hits))
            key = fingerprint(subject, hits, proposal)
            if not log.due(subject, key, now):
                report.suppressed += 1
                continue
            try:
                decision = decider.decide(observation, hits, proposal)
            except Exception as exc:  # noqa: BLE001 - reader failure must never authorize a stop
                report.errors[f"reader:{subject.kind}:{subject.id}"] = f"{type(exc).__name__}: {exc}"
                decision = Decision("alert_research", "infra",
                                    f"reader failed: {type(exc).__name__}; proposed {proposal.action}",
                                    evidence_steps=proposal.evidence_steps)
            decision = guard(subject, decision)
            acted = False
            if mode == "act" and actuator is not None and decision.action != "continue":
                try:
                    acted = actuator.act(observation, decision)
                except Exception as exc:  # noqa: BLE001 - retain the decision even if the action fails
                    report.errors[f"action:{subject.kind}:{subject.id}"] = f"{type(exc).__name__}: {exc}"
            record = finding(observation, hits, decision, proposal=proposal, mode=mode, acted=acted,
                             produced_at=now)
            log.append(subject, key, record, now)
            report.records.append(record)
            if windows is not None:
                try:
                    windows.write(record, observation, hits)
                except Exception as exc:  # noqa: BLE001 - the decision above is already recorded
                    report.errors[f"window:{subject.kind}:{subject.id}"] = f"{type(exc).__name__}: {exc}"
    return report
