from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from random import Random
from typing import Any

from evallab.evidence.facts import (
    JobFacts,
    StateJournalRecord,
    extract_job_facts,
    extract_trial_fact,
    load_state_journal,
)
from evallab.results import JobRecord, load_job
from evallab.sim.engine import STEP_LIMIT, Simulation

_STEP_BUDGET = 20
_MAX_CHANGES = 4
_SHAPES = (
    "absent",
    "status-unreadable",
    "status-not-object",
    "schema-wrong",
    "status-missing",
    "diff-missing",
    "diff-invalid",
    "valid",
)
_CHANGE_TYPES = ("added", "modified", "deleted")
_STATUSES = ("available", "partial")
_REASONS = (None, "producer_note")
_NOT_OBJECTS: tuple[Any, ...] = ([1], "available", 7, None, True)
_WRONG_SCHEMAS: tuple[Any, ...] = (2, 0, "1", True, 1.0)
_MISSING_STATUSES: tuple[Any, ...] = ("", 7, None, ["available"])
_DIFF_FAULTS = 5
_JOURNAL = "state-journal"

Files = dict[str, bytes]


@dataclass(frozen=True, slots=True)
class _Shape:
    name: str
    trial: int
    changes: int
    status: str
    reason: str | None
    variant: int


@dataclass(frozen=True, slots=True)
class _Expect:
    status: str
    reason: str | None
    changes: int
    paths: tuple[str, ...]
    events: int
    event_status: str | None


def _plan(rng: Random) -> list[_Shape]:
    order = list(_SHAPES)
    rng.shuffle(order)
    shapes = [
        _Shape(
            name,
            trial,
            rng.randint(0, _MAX_CHANGES),
            rng.choice(_STATUSES),
            rng.choice(_REASONS),
            rng.randrange(1000),
        )
        for trial, name in enumerate(order)
    ]
    assert sorted(shape.name for shape in shapes) == sorted(_SHAPES)
    assert all(0 <= shape.changes <= _MAX_CHANGES for shape in shapes)
    return shapes


def _json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True).encode()


def _status(shape: _Shape, **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"schema_version": 1, "status": shape.status}
    if shape.reason is not None:
        payload["reason"] = shape.reason
    payload.update(fields)
    return payload


def _meta(path: str, size: int, now: datetime) -> dict[str, Any]:
    assert size >= 0
    digest = hashlib.sha256(f"{path}:{size}".encode()).hexdigest()
    return {
        "path": path,
        "mode": "0644",
        "size_bytes": size,
        "mtime_ns": int(now.timestamp()) * 1_000_000_000,
        "type": "file",
        "sha256": f"sha256:{digest}",
        "hash_status": "complete",
    }


def _change(shape: _Shape, index: int, now: datetime) -> dict[str, Any]:
    path = f"work/file-{index}.txt"
    change_type = _CHANGE_TYPES[(shape.variant + index) % len(_CHANGE_TYPES)]
    events = (shape.variant + index) % 3
    before = None if change_type == "added" else _meta(path, index, now)
    after = None if change_type == "deleted" else _meta(path, index + 7, now)
    first = (now + timedelta(seconds=1)).isoformat() if events else None
    last = (now + timedelta(seconds=1 + events)).isoformat() if events else None
    return {
        "path": path,
        "change_type": change_type,
        "before": before,
        "after": after,
        "event_count": events,
        "first_event_at": first,
        "last_event_at": last,
    }


def _diff(shape: _Shape, changes: int, now: datetime) -> dict[str, Any]:
    assert 0 <= changes <= _MAX_CHANGES
    entries = [_change(shape, index, now) for index in range(changes)]
    return {
        "schema_version": 1,
        "status": shape.status,
        "root": "/app",
        "before_captured_at": now.isoformat(),
        "after_captured_at": (now + timedelta(seconds=10)).isoformat(),
        "change_count": changes,
        "event_count": sum(entry["event_count"] for entry in entries),
        "dropped_event_count": 0,
        "changes": entries,
    }


def _events(diff: dict[str, Any], now: datetime) -> bytes:
    lines = []
    for sequence, change in enumerate(diff["changes"], start=1):
        deleted = change["change_type"] == "deleted"
        event = {
            "sequence": sequence,
            "timestamp": (now + timedelta(seconds=sequence)).isoformat(),
            "path": change["path"],
            "operations": ["delete"] if deleted else ["close_write"],
            "is_directory": False,
            "state": change["after"],
        }
        lines.append(json.dumps(event, sort_keys=True))
    return "".join(f"{line}\n" for line in lines).encode()


def _corrupt(shape: _Shape, now: datetime) -> bytes:
    payload = _diff(shape, max(shape.changes, 1), now)
    fault = shape.variant % _DIFF_FAULTS
    if fault == 0:
        text = _json(payload)
        return text[: 1 + shape.variant % (len(text) - 1)]
    if fault == 1:
        payload["change_count"] += 1
    elif fault == 2:
        payload["changes"][0]["change_type"] = "renamed"
    elif fault == 3:
        payload["after_captured_at"] = (now - timedelta(seconds=1)).isoformat()
    else:
        payload["changes"].append(payload["changes"][0])
        payload["change_count"] += 1
    return _json(payload)


def _invalid_only(status: str, reason: str) -> _Expect:
    return _Expect(status, reason, 0, (), 1, "invalid")


def _paths(diff: dict[str, Any]) -> tuple[str, ...]:
    return tuple(change["path"] for change in diff["changes"])


def _build_status(shape: _Shape) -> tuple[Files, _Expect]:
    status = f"{_JOURNAL}/status.json"
    if shape.name == "status-unreadable":
        if shape.variant % 2:
            return {status: b"\xff\xfe" + _json(_status(shape))}, _invalid_only(
                "invalid", "status_unreadable:UnicodeDecodeError"
            )
        text = _json(_status(shape))
        cut = text[: shape.variant % len(text)]
        return {status: cut}, _invalid_only("invalid", "status_unreadable:JSONDecodeError")
    if shape.name == "status-not-object":
        value = _NOT_OBJECTS[shape.variant % len(_NOT_OBJECTS)]
        return {status: _json(value)}, _invalid_only("invalid", "status_invalid")
    assert shape.name == "schema-wrong"
    payload = _status(shape)
    choice = shape.variant % (len(_WRONG_SCHEMAS) + 1)
    if choice == len(_WRONG_SCHEMAS):
        del payload["schema_version"]
    else:
        payload["schema_version"] = _WRONG_SCHEMAS[choice]
    return {status: _json(payload)}, _invalid_only("invalid", "status_schema_invalid")


def _build_journal(shape: _Shape, now: datetime) -> tuple[Files, _Expect]:
    status = f"{_JOURNAL}/status.json"
    diff_path = f"{_JOURNAL}/state-diff.json"
    diff = _diff(shape, shape.changes, now)
    if shape.name == "absent":
        files = {diff_path: _json(diff)} if shape.variant % 2 else {}
        return files, _Expect("absent", "not_recorded", 0, (), 0, None)
    if shape.name == "status-missing":
        payload = _status(shape)
        choice = shape.variant % (len(_MISSING_STATUSES) + 1)
        if choice == len(_MISSING_STATUSES):
            del payload["status"]
        else:
            payload["status"] = _MISSING_STATUSES[choice]
        files = {status: _json(payload), diff_path: _json(diff)}
        paths = _paths(diff)
        return files, _Expect("invalid", shape.reason, len(paths), paths, 1, "invalid")
    if shape.name == "diff-missing":
        reason = shape.reason or "state_diff_missing"
        return {status: _json(_status(shape))}, _invalid_only(shape.status, reason)
    if shape.name == "diff-invalid":
        files = {status: _json(_status(shape)), diff_path: _corrupt(shape, now)}
        journal = "invalid" if shape.status == "available" else shape.status
        return files, _invalid_only(journal, shape.reason or "state_diff_invalid")
    if shape.name == "valid":
        files = {status: _json(_status(shape)), diff_path: _json(diff)}
        files[f"{_JOURNAL}/state-events.jsonl"] = _events(diff, now)
        paths = _paths(diff)
        event_status = "valid" if paths else None
        return files, _Expect(
            shape.status, shape.reason, len(paths), paths, len(paths), event_status
        )
    return _build_status(shape)


def _trial_view(facts: JobFacts) -> tuple[Any, ...]:
    return tuple(
        (
            fact.trial_id,
            fact.state_journal_status,
            fact.state_journal_reason,
            fact.state_change_count,
        )
        for fact in facts.trials
    )


def _digest(records: list[StateJournalRecord], facts: JobFacts) -> str:
    view = (tuple(records), _trial_view(facts), facts.state_changes, facts.state_events)
    return hashlib.sha256(repr(view).encode()).hexdigest()


class JournalSimulation:
    def __init__(self, sim: Simulation, root: Path) -> None:
        assert sim.step + _STEP_BUDGET <= STEP_LIMIT
        self.sim = sim
        self.root = root
        self.start_step = sim.step
        root.mkdir(parents=True, exist_ok=True)
        assert root.is_dir()

    def run(self) -> None:
        sim = self.sim
        plan_seed = sim.rng.getrandbits(64)
        shapes = _plan(Random(plan_seed))
        sim.require(_plan(Random(plan_seed)) == shapes, "journal_plan_deterministic")
        sim.begin("journal-plan", shapes=[[s.trial, s.name, s.changes] for s in shapes])
        now = sim.runtime.now()
        built = [_build_journal(shape, now) for shape in shapes]
        expects = [expect for _, expect in built]
        self._write_job(self.root / "first", [files for files, _ in built])
        self._write_job(self.root / "second", [files for files, _ in built])
        job = load_job(self.root / "first")
        sim.require(len(job.trials) == len(shapes), "journal_job_trial_count")
        records: list[StateJournalRecord] = []
        for trial, shape, want in zip(job.trials, shapes, expects, strict=True):
            sim.begin("journal-trial", trial=shape.trial, shape=shape.name, changes=want.changes)
            record = load_state_journal(trial)
            records.append(record)
            self._check_record(record, want)
            fact = extract_trial_fact(job, trial, projection=None)
            sim.require(fact.state_journal_status == want.status, "journal_fact_status")
            sim.require(fact.state_journal_reason == want.reason, "journal_fact_reason")
            sim.require(fact.state_change_count == want.changes, "journal_fact_change_count")
        sim.begin("journal-job-facts", trials=len(shapes))
        facts = extract_job_facts(job)
        self._check_job(job, facts, expects)
        sim.begin("journal-replay", trials=len(shapes))
        second = load_job(self.root / "second")
        replayed = [load_state_journal(trial) for trial in second.trials]
        sim.require(replayed == records, "journal_replay_identical_records")
        second_facts = extract_job_facts(second)
        digest = _digest(records, facts)
        sim.require(_digest(replayed, second_facts) == digest, "journal_replay_identical_facts")
        sim.note(journal_digest=digest)
        sim.require(sim.step - self.start_step <= _STEP_BUDGET, "journal_step_budget")

    def _write_job(self, job_dir: Path, trials: list[Files]) -> None:
        assert len(trials) == len(_SHAPES)
        now = self.sim.runtime.now().isoformat()
        for index, files in enumerate(trials):
            trial_dir = job_dir / f"trial-{index}"
            trial_dir.mkdir(parents=True)
            result = {"id": f"journal-{self.sim.seed}-{index}", "task_name": "simulation-task"}
            result.update(trial_name=f"journal-{index}", finished_at=now)
            (trial_dir / "result.json").write_bytes(_json(result))
            for relative, data in files.items():
                target = trial_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                assert not target.exists()
                target.write_bytes(data)
        job = {"id": f"journal-job-{self.sim.seed}", "finished_at": now}
        job.update(n_total_trials=len(trials), stats={})
        (job_dir / "result.json").write_bytes(_json(job))

    def _check_record(self, record: StateJournalRecord, want: _Expect) -> None:
        sim = self.sim
        sim.note(status=record.status, reason=record.reason, changes=len(record.changes))
        sim.require(record.status == want.status, "journal_status")
        sim.require(record.reason == want.reason, "journal_reason")
        sim.require(len(record.changes) == want.changes, "journal_change_count")
        paths = tuple(change.get("path") for change in record.changes)
        sim.require(paths == want.paths, "journal_change_paths")

    def _check_job(self, job: JobRecord, facts: JobFacts, expects: list[_Expect]) -> None:
        sim = self.sim
        ids = [trial.id for trial in job.trials]
        sim.require([fact.trial_id for fact in facts.trials] == sorted(ids), "journal_fact_trials")
        wanted = {trial_id: want for trial_id, want in zip(ids, expects, strict=True)}
        for fact in facts.trials:
            want = wanted[fact.trial_id]
            view = (fact.state_journal_status, fact.state_journal_reason, fact.state_change_count)
            sim.require(view == (want.status, want.reason, want.changes), "journal_job_trial_fact")
        changes = Counter(fact.trial_id for fact in facts.state_changes)
        events = Counter(fact.trial_id for fact in facts.state_events)
        for trial_id, want in wanted.items():
            sim.require(changes[trial_id] == want.changes, "journal_state_change_fact_count")
            sim.require(events[trial_id] == want.events, "journal_state_event_fact_count")
        for fact in facts.state_changes:
            want = wanted[fact.trial_id]
            sim.require(fact.journal_status == want.status, "journal_state_change_status")
            sim.require(fact.path in want.paths, "journal_state_change_path")
        for fact in facts.state_events:
            want = wanted[fact.trial_id]
            sim.require(fact.evidence_status == want.event_status, "journal_state_event_status")
