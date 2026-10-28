"""Live Supervisor actuator (LS-4): carry out one decision on one lab subject, capped and receipted.

Design: ``docs/live-supervisor.md`` (Actions, Safety). :class:`LabActuator` implements the core
``Actuator`` protocol and dispatches on ``subject.kind``:

- ``harbor_trial`` kill: a stop request through the ``evallab watch stop`` path
  (:mod:`evallab.watch_stop`): refused once the trial has a ``result.json``, is terminal in the
  hook journal, or has started verification; first request wins.
- ``modal_app`` kill: ``modal app stop <app_id>`` through an injected runner, only for a lab-owned,
  killable app; ``evallab-cloud-production`` is never stopped.
- ``omp_lane`` nudge: one ``queue-append.jsonl`` item for a fleet-owned lane. Nothing else ever
  goes to a lane.
- ``alert_research``: one line under ``## Live alerts`` of the lane's markdown, once per subject
  per UTC day.
- ``flag_cheating``: handed to an injected router (LS-5); without one nothing is written.

Kills are capped by a file-locked ledger under ``root``: one kill per subject ever, three per
rolling hour and ten per UTC day across the lab. Every real kill attempt counts. A dry run
(the default) checks the same guards and caps, consumes nothing, runs nothing, and records the
exact command it would have run. Every attempt appends one receipt row to ``actions-v1.jsonl``.

A lab-looking name or path is not proof that this lab started the target, so a real kill needs
two more things besides the guards and caps:

- **Registration** (``registrations-v1.json`` under ``root``, written by an operator through
  :func:`register_app` / :func:`register_trial`): the immutable identity — the Modal ``app_id``,
  or the ``job/trial`` pair — must be registered by a named operator, with an optional expiry.
  A prefix or label match never counts.
- **An adjudication gate** (:class:`KillGate`, injected): LS-3's per-action precision evidence.
  Without a gate nothing real is killed, so an unadjudicated rule fallback from ``scan_once``
  can never execute. Dry runs report both conditions without consuming caps.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from evallab.harbor_watch_hooks import read_hook_journal
from evallab.production.live_supervisor import (
    DEFAULT_ROOT,
    KILL_ACTIONS,
    Decision,
    Observation,
    Subject,
)
from evallab.watch_stop import (
    capture_host_snapshot,
    read_stop_request,
    request_path_for,
    stop_records_dir,
    trial_terminal,
    verification_started,
    write_stop_record,
    write_stop_request,
)

ACTIONS_FILE = "actions-v1.jsonl"
CAPS_FILE = "caps-v1.json"
REGISTRATIONS_FILE = "registrations-v1.json"
LOCK_FILE = "actions.lock"
RECEIPT_SCHEMA = "live-supervisor-actions/v1"
REQUESTED_BY = "live_supervisor"

KILLS_PER_SUBJECT = 1
KILLS_PER_HOUR = 3
KILLS_PER_DAY = 10

#: The Modal control plane; report only, never stopped by the supervisor.
PRODUCTION_MODAL_APP = "evallab-cloud-production"
#: Modal credentials come from the ``keys`` store, as for every other Modal call in the lab.
MODAL_STOP = ("keys", "run", "--", "modal", "app", "stop")
MODAL_STOP_TIMEOUT_S = 120.0

BRIDGE_DIR = Path("~/Developer/research-context/bridge/2026-10-27-run-atlas")
DEFAULT_QUEUE_APPEND = BRIDGE_DIR / "queue-append.jsonl"
DEFAULT_ALERTS_MD = BRIDGE_DIR / "live-supervisor.md"
ALERTS_HEADING = "## Live alerts"
ALERT_MARKER = "ls-alert"
NUDGE_PREFIX = "LS-NUDGE-"
#: Lane owner whose panes may receive a nudge; Peter's chats and pinned tabs never do.
NUDGE_OWNER = "fleet"
EVIDENCE_CHARS = 160
ET = ZoneInfo("America/New_York")

#: ``runner(argv) -> exit code``; tests inject a fake, production runs the command.
Runner = Callable[[Sequence[str]], int]
#: ``router(observation, decision) -> acted``; the LS-5 Trace Watcher router.
CheatRouter = Callable[[Observation, Decision], bool]
#: ``gate(observation, decision) -> bool``; the LS-3 adjudication gate, required for a real kill.
KillGate = Callable[[Observation, Decision], bool]


def run_command(argv: Sequence[str]) -> int:
    """Run one command with a deadline; its exit code (output is not needed)."""
    return subprocess.run(list(argv), capture_output=True, check=False,
                          timeout=MODAL_STOP_TIMEOUT_S).returncode


def _subject_key(subject: Subject) -> str:
    return f"{subject.kind}:{subject.id}"


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


# --------------------------------------------------------------------------- caps ledger


@dataclass
class KillLedger:
    """Kill history plus retry requests, as one JSON document under the actuator root.

    Only read or written while :meth:`LabActuator._locked` holds the root lock.
    """

    kills: list[dict[str, Any]]
    retries: list[dict[str, Any]]

    @classmethod
    def load(cls, path: Path) -> KillLedger:
        if not path.is_file():
            return cls([], [])
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(list(payload.get("kills", [])), list(payload.get("retries", [])))

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"kills": self.kills, "retries": self.retries}, indent=2,
                                  sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    def refusal(self, subject: Subject, now: datetime) -> str | None:
        """Why one more kill would break a cap (``None`` when it is allowed)."""
        key = _subject_key(subject)
        if sum(1 for k in self.kills if k["subject"] == key) >= KILLS_PER_SUBJECT:
            return f"cap_subject: {key} already killed once"
        moments = [_parse(k["at"]) for k in self.kills]
        hour = sum(1 for at in moments if now - timedelta(hours=1) < at <= now)
        if hour >= KILLS_PER_HOUR:
            return f"cap_hour: {hour} kills in the last hour (max {KILLS_PER_HOUR})"
        day = now.astimezone(UTC).date()
        today = sum(1 for at in moments if at.astimezone(UTC).date() == day)
        if today >= KILLS_PER_DAY:
            return f"cap_day: {today} kills on {day.isoformat()} UTC (max {KILLS_PER_DAY})"
        return None

    def record_kill(self, subject: Subject, action: str, channel: str, now: datetime) -> None:
        self.kills.append({"subject": _subject_key(subject), "action": action, "channel": channel,
                           "at": _iso(now)})

    def record_retry(self, subject: Subject, reason: str, now: datetime) -> None:
        """``kill_and_retry`` requeue intent; v1 records it and dispatches nothing."""
        self.retries.append({"subject": _subject_key(subject), "status": "retry_requested",
                             "reason": reason, "at": _iso(now)})


# --------------------------------------------------------------------------- registration


def registrations_path(root: Path) -> Path:
    return Path(root).expanduser() / REGISTRATIONS_FILE


def _read_registrations(path: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_registration(path: Path, key: str, entry: Mapping[str, Any]) -> None:
    if not str(entry.get("registered_by") or "").strip():
        raise ValueError("a registration needs a non-blank registered_by")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _read_registrations(path)
    payload[key] = dict(entry)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def register_app(root: Path, app_id: str, *, registered_by: str, note: str = "",
                 expires_at: datetime | None = None) -> Path:
    """Operator registration of one Modal app by its immutable ``app_id``; returns the file.

    A prefix, label or description match is never a registration.
    """
    path = registrations_path(root)
    entry: dict[str, Any] = {"kind": "modal_app", "app_id": app_id, "note": note,
                             "registered_by": registered_by,
                             "registered_at": _iso(datetime.now(UTC))}
    if expires_at is not None:
        entry["expires_at"] = _iso(expires_at)
    _write_registration(path, _subject_key(Subject("modal_app", app_id, "lab")), entry)
    return path


def register_trial(root: Path, job: str, trial: str, *, registered_by: str, note: str = "",
                   expires_at: datetime | None = None) -> Path:
    """Operator registration of one Harbor trial by its ``job/trial`` identity; returns the file."""
    path = registrations_path(root)
    entry: dict[str, Any] = {"kind": "harbor_trial", "job": job, "trial": trial, "note": note,
                             "registered_by": registered_by,
                             "registered_at": _iso(datetime.now(UTC))}
    if expires_at is not None:
        entry["expires_at"] = _iso(expires_at)
    _write_registration(path, _subject_key(Subject("harbor_trial", f"{job}/{trial}", "lab")), entry)
    return path


def registration_refusal(path: Path, subject: Subject, now: datetime) -> str | None:
    """Why ``subject`` is not operator-registered (``None`` when it is)."""
    entry = _read_registrations(path).get(_subject_key(subject))
    if not isinstance(entry, dict):
        return f"unregistered: {_subject_key(subject)} has no operator registration"
    if not str(entry.get("registered_by") or "").strip():
        return f"unregistered: {_subject_key(subject)} registration has no registered_by"
    expected = {"modal_app": "app_id", "harbor_trial": "trial"}.get(subject.kind)
    if expected is None:
        return f"unregistered: {subject.kind} has no registration identity"
    if str(entry.get("kind") or "") != subject.kind:
        return f"unregistered: {_subject_key(subject)} registration is for {entry.get('kind')!r}"
    if expected == "app_id" and str(entry.get("app_id")) != subject.id:
        return f"unregistered: app_id mismatch for {subject.id}"
    if expected == "trial":
        job, _, trial = subject.id.partition("/")
        if (str(entry.get("job")), str(entry.get("trial"))) != (job, trial):
            return f"unregistered: job/trial mismatch for {subject.id}"
    expires = entry.get("expires_at")
    if isinstance(expires, str) and expires:
        try:
            if now >= _parse(expires):
                return f"unregistered: {_subject_key(subject)} registration expired {expires}"
        except ValueError:
            return f"unregistered: {_subject_key(subject)} bad expires_at {expires!r}"
    return None


# --------------------------------------------------------------------------- outcomes


@dataclass(frozen=True)
class Outcome:
    """What one channel did (or would do) for one decision."""

    status: str  # done | dry_run | refused | failed
    channel: str
    detail: str = ""
    command: tuple[str, ...] = ()
    target: str | None = None
    payload: Mapping[str, Any] | None = None

    @property
    def acted(self) -> bool:
        return self.status == "done"


def _refused(channel: str, why: str, **extra: Any) -> Outcome:
    return Outcome("refused", channel, why, **extra)


def _evidence(observation: Observation, decision: Decision) -> str:
    events = {e.index: e for e in observation.events}
    parts = [f"step {s}: {' '.join(events[s].text.split())[:EVIDENCE_CHARS]}"
             for s in decision.evidence_steps if s in events]
    return "; ".join(parts)


# --------------------------------------------------------------------------- actuator


class LabActuator:
    """The act-mode actuator. ``dry_run`` (default) runs and writes nothing but receipts.

    Every external effect is injected: ``runner`` (Modal CLI), ``harbor_roots`` (runs roots
    that hold lab job directories), ``queue_path`` (lane nudges), ``alerts_path`` (research
    alerts), ``cheat_router`` (LS-5) and ``clock``.
    """

    def __init__(self, *, root: Path = DEFAULT_ROOT, dry_run: bool = True,
                 runner: Runner = run_command, harbor_roots: Sequence[Path] = (),
                 queue_path: Path = DEFAULT_QUEUE_APPEND, alerts_path: Path = DEFAULT_ALERTS_MD,
                 cheat_router: CheatRouter | None = None, gate: KillGate | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.root = root.expanduser()
        self.dry_run = dry_run
        self.runner = runner
        self.harbor_roots = tuple(Path(r).expanduser() for r in harbor_roots)
        self.queue_path = queue_path.expanduser()
        self.alerts_path = alerts_path.expanduser()
        self.cheat_router = cheat_router
        #: LS-3's adjudication gate; none means no real kill can happen.
        self.gate = gate
        self.clock = clock
        self.actions_path = self.root / ACTIONS_FILE
        self.caps_path = self.root / CAPS_FILE
        self.registrations = registrations_path(self.root)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / LOCK_FILE).open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def act(self, observation: Observation, decision: Decision) -> bool:
        """Carry out ``decision``; True only when something real was done."""
        now = self.clock()
        with self._locked():
            ledger = KillLedger.load(self.caps_path)
            retry = False
            if decision.action in KILL_ACTIONS:
                outcome = self._kill(observation, decision, ledger, now)
                if outcome.acted and decision.action == "kill_and_retry":
                    ledger.record_retry(observation.subject, decision.reason, now)
                    retry = True
                if outcome.status in ("done", "failed"):  # every real attempt counts
                    ledger.save(self.caps_path)
            elif decision.action == "nudge":
                outcome = self._nudge(observation, decision)
            elif decision.action == "alert_research":
                outcome = self._alert(observation, decision, now)
            elif decision.action == "flag_cheating":
                outcome = self._flag(observation, decision)
            else:
                outcome = Outcome("refused", "none", f"no effect for {decision.action}")
            self._receipt(observation, decision, outcome, now, retry_requested=retry)
        return outcome.acted

    # ----------------------------------------------------------------------- kills

    def _kill(self, observation: Observation, decision: Decision, ledger: KillLedger,
              now: datetime) -> Outcome:
        subject = observation.subject
        if subject.kind == "harbor_trial":
            plan = self._harbor_plan(observation, decision)
        elif subject.kind == "modal_app":
            plan = self._modal_plan(observation)
        else:
            return _refused("none", f"no kill channel for {subject.kind}")
        if plan.status == "refused":
            return plan
        if not subject.killable or subject.owner != "lab":
            return _refused(plan.channel, f"not lab-killable ({subject.owner}-owned)",
                            command=plan.command, target=plan.target)
        blockers = self._blockers(observation, decision, now)
        cap = ledger.refusal(subject, now)
        if self.dry_run:
            detail = "would kill"
            if blockers:
                detail += f" (blocked: {'; '.join(blockers)})"
            if cap is not None:
                detail += f" (blocked: {cap})"
            return Outcome("dry_run", plan.channel, detail, plan.command, plan.target)
        if blockers:
            return _refused(plan.channel, blockers[0], command=plan.command, target=plan.target)
        if cap is not None:
            return _refused(plan.channel, cap, command=plan.command, target=plan.target)
        ledger.record_kill(subject, decision.action, plan.channel, now)
        if plan.channel == "harbor_stop_request":
            return self._harbor_stop(decision, plan, now)
        return self._modal_stop(subject, plan)

    def _blockers(self, observation: Observation, decision: Decision, now: datetime) -> list[str]:
        """Why a real kill would not be allowed: registration, then the LS-3 gate."""
        out: list[str] = []
        missing = registration_refusal(self.registrations, observation.subject, now)
        if missing is not None:
            out.append(missing)
        if self.gate is None:
            out.append("no adjudication gate: real kills need LS-3 precision evidence")
        else:
            try:
                if not self.gate(observation, decision):
                    out.append("adjudication gate declined: not an adjudicated kill case")
            except Exception as exc:  # noqa: BLE001 - a broken gate must block, never allow
                out.append(f"adjudication gate failed: {type(exc).__name__}: {exc}"[:200])
        return out

    def _job_dir(self, observation: Observation, job: str) -> Path | None:
        hinted = observation.metrics.get("job_dir")
        candidates = [Path(hinted)] if isinstance(hinted, str) and hinted else []
        for root in self.harbor_roots:
            candidates += [root / job, root / ".exec-stage" / job]
        return next((c for c in candidates if c.is_dir() and c.name == job), None)

    def _harbor_plan(self, observation: Observation, decision: Decision) -> Outcome:
        """Resolve the trial and apply the ``watch stop`` refusals (no write)."""
        channel = "harbor_stop_request"
        job, sep, trial = observation.subject.id.partition("/")
        if not sep or not job or not trial or "/" in trial:
            return _refused(channel, f"subject id {observation.subject.id!r} is not <job>/<trial>")
        job_dir = self._job_dir(observation, job)
        if job_dir is None:
            return _refused(channel, f"job {job!r} not under a lab runs root")
        hooks = read_hook_journal(job_dir).get(trial)
        finished = (job_dir / trial / "result.json").is_file()
        watch_dir = job_dir / "watch"
        target = str(request_path_for(watch_dir, trial))
        command = ("evallab", "watch", "stop", str(job_dir), trial, "--rule",
                   f"{REQUESTED_BY}:{decision.kind}", "--reason", decision.reason)
        if trial_terminal({"state": "finished" if finished else "running"}, hooks):
            return _refused(channel, "trial already finished", command=command, target=target)
        if verification_started(hooks):
            return _refused(channel, "trial already entered verification", command=command,
                            target=target)
        if read_stop_request(watch_dir, trial) is not None:
            return _refused(channel, "stop request already exists", command=command, target=target)
        return Outcome("planned", channel, "", command, target,
                       {"job_dir": str(job_dir), "trial": trial, "hooks": hooks})

    def _harbor_stop(self, decision: Decision, plan: Outcome, now: datetime) -> Outcome:
        assert plan.payload is not None
        job_dir, trial = Path(plan.payload["job_dir"]), str(plan.payload["trial"])
        hooks = plan.payload["hooks"]
        watch_dir = job_dir / "watch"
        rule = f"{REQUESTED_BY}:{decision.kind}"
        step_ref = f"step {decision.evidence_steps[0]}" if decision.evidence_steps else None
        moment = now.timestamp()
        dest = stop_records_dir(watch_dir) / Path(plan.target or trial).stem
        try:
            manifest = capture_host_snapshot(job_dir=job_dir, trial=trial, hooks=hooks, dest_dir=dest)
        except Exception as exc:  # noqa: BLE001 - evidence capture never blocks a stop (as watch stop)
            manifest = {"files": [], "missing": [], "errors": [f"{type(exc).__name__}: {exc}"]}
        write_stop_record(watch_dir, trial=trial, outcome="requested", rule=rule, step_ref=step_ref,
                          requested_by=REQUESTED_BY, mode="on", manifest=manifest,
                          verifier_started=verification_started(hooks) if hooks is not None else None,
                          reason=decision.reason, now=moment)
        path, created = write_stop_request(watch_dir, trial=trial, rule=rule, step_ref=step_ref,
                                           alert=None, requested_by=REQUESTED_BY, mode="on",
                                           reason=decision.reason, now=moment)
        if not created:
            return Outcome("failed", plan.channel, "a stop request appeared concurrently",
                           plan.command, str(path))
        return Outcome("done", plan.channel, "stop request written", plan.command, str(path))

    def _modal_plan(self, observation: Observation) -> Outcome:
        subject = observation.subject
        command = (*MODAL_STOP, subject.id)
        names = {subject.id, subject.label, str(observation.metrics.get("description") or "")}
        if PRODUCTION_MODAL_APP in names or subject.owner == "production":
            return _refused("modal_app_stop", f"{PRODUCTION_MODAL_APP} is never stopped",
                            command=command)
        return Outcome("planned", "modal_app_stop", "", command)

    def _modal_stop(self, subject: Subject, plan: Outcome) -> Outcome:
        assert subject.owner == "lab" and subject.killable and PRODUCTION_MODAL_APP not in plan.command
        try:
            code = self.runner(plan.command)
        except Exception as exc:  # noqa: BLE001 - a failed stop is receipted, never raised into scan_once
            return Outcome("failed", plan.channel, f"{type(exc).__name__}: {exc}"[:200], plan.command)
        if code != 0:
            return Outcome("failed", plan.channel, f"exit {code}", plan.command)
        return Outcome("done", plan.channel, "modal app stopped", plan.command)

    # ----------------------------------------------------------------------- non-kill channels

    def _nudge(self, observation: Observation, decision: Decision) -> Outcome:
        subject = observation.subject
        channel = "queue_append"
        if subject.kind != "omp_lane":
            return _refused(channel, f"no nudge channel for {subject.kind}")
        if subject.owner != NUDGE_OWNER:
            return _refused(channel, f"{subject.owner}-owned pane is never nudged")
        evidence = _evidence(observation, decision)
        seed = f"{subject.id}|{decision.reason}|{_iso(observation.observed_at)}"
        line = {
            "pane": subject.id,
            "id": NUDGE_PREFIX + hashlib.sha256(seed.encode()).hexdigest()[:8],
            "text": f"Live Supervisor ({decision.kind}): {decision.reason}"
                    + (f". Evidence: {evidence}" if evidence else ""),
        }
        target = str(self.queue_path)
        if self.dry_run:
            return Outcome("dry_run", channel, "would append", target=target, payload=line)
        with self.queue_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
        return Outcome("done", channel, "queue item appended", target=target, payload=line)

    def _alert(self, observation: Observation, decision: Decision, now: datetime) -> Outcome:
        subject = observation.subject
        channel = "research_alert"
        target = str(self.alerts_path)
        marker = f"<!-- {ALERT_MARKER} {_subject_key(subject)} {now.astimezone(UTC).date().isoformat()} -->"
        text = self.alerts_path.read_text(encoding="utf-8") if self.alerts_path.is_file() else ""
        if marker in text:
            return _refused(channel, "already alerted for this subject today", target=target)
        evidence = _evidence(observation, decision)
        stamp = now.astimezone(ET).strftime("%-I:%M %p ET")
        reader = decision.reader_model or "rules"
        number = f"{decision.kind}" + (f", confidence {decision.confidence:.2f}"
                                       if decision.confidence is not None else "")
        line = (f"- **Does {subject.kind} `{subject.id}` ({subject.owner}) need research?** → "
                f"live supervisor ({reader}) at {stamp} → **{number}** → {decision.reason}"
                + (f" (evidence: {evidence})" if evidence else "") + f" {marker}")
        if self.dry_run:
            return Outcome("dry_run", channel, "would append", target=target, payload={"line": line})
        self.alerts_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.alerts_path.with_suffix(".ls-tmp")
        tmp.write_text(insert_alert(text, line), encoding="utf-8")
        tmp.replace(self.alerts_path)
        return Outcome("done", channel, "alert appended", target=target, payload={"line": line})

    def _flag(self, observation: Observation, decision: Decision) -> Outcome:
        channel = "cheat_router"
        if self.cheat_router is None:
            return _refused(channel, "no cheat router configured")
        if self.dry_run:
            return Outcome("dry_run", channel, "would route to the Trace Watcher")
        try:
            routed = bool(self.cheat_router(observation, decision))
        except Exception as exc:  # noqa: BLE001 - a router failure is receipted, never raised
            return Outcome("failed", channel, f"{type(exc).__name__}: {exc}"[:200])
        return Outcome("done" if routed else "failed", channel,
                       "routed" if routed else "router declined")

    # ----------------------------------------------------------------------- receipts

    def _receipt(self, observation: Observation, decision: Decision, outcome: Outcome,
                 now: datetime, *, retry_requested: bool) -> None:
        row = {
            "schema": RECEIPT_SCHEMA,
            "at": _iso(now),
            "observed_at": _iso(observation.observed_at),
            "run_id": observation.subject.finding_run_id,
            "subject": observation.subject.as_dict(),
            "action": decision.action,
            "kind": decision.kind,
            "reason": decision.reason,
            "reader_model": decision.reader_model,
            "dry_run": self.dry_run,
            "channel": outcome.channel,
            "status": outcome.status,
            "acted": outcome.acted,
            "detail": outcome.detail,
            "command": list(outcome.command),
            "target": outcome.target,
            "payload": _jsonable(outcome.payload),
            "retry_requested": retry_requested,
        }
        with self.actions_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")


def _jsonable(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    return {k: v for k, v in payload.items() if k != "hooks"}


def insert_alert(text: str, line: str) -> str:
    """``text`` with ``line`` appended at the end of its ``## Live alerts`` section (created
    at the end of the file when absent)."""
    lines = text.splitlines()
    try:
        start = lines.index(ALERTS_HEADING)
    except ValueError:
        body = text.rstrip("\n")
        return (f"{body}\n\n" if body else "") + f"{ALERTS_HEADING}\n\n{line}\n"
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith(("# ", "## "))),
               len(lines))
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    insert = [line] if end > start + 1 else ["", line]
    rest = lines[end:]
    out = lines[:end] + insert + ([""] if rest and rest[0].strip() else []) + rest
    return "\n".join(out) + "\n"
