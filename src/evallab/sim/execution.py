from __future__ import annotations

import errno
import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

from evallab.execution_contracts import RunRequest, TransientHarnessFailure
from evallab.queue import QUEUE_STATES, DirectoryQueue, Executor, load_events
from evallab.results import load_job
from evallab.schemas import AutoRunRule, QueueState, StandingApprovalsPolicy
from evallab.sim.engine import Fault, ProcessCrash, Simulation, spec_for

Mode = Literal["success", "retry", "backend-error", "detached", "ingest-crash", "timeout"]
_MODES: tuple[Mode, ...] = (
    "success",
    "retry",
    "backend-error",
    "detached",
    "ingest-crash",
    "timeout",
)
_EDGES = {
    "submitted": (None, "pending"),
    "policy_admitted": ("pending", "approved"),
    "dispatch_started": ("approved", "running"),
    "dispatch_completed": ("running", "done"),
    "dispatch_failed": ("running", "failed"),
    "running_reconciled": ("running", "done"),
    "running_reconcile_failed": ("running", "failed"),
}
_TERMINAL: tuple[QueueState, ...] = ("done", "failed")


@dataclass(slots=True)
class Job:
    name: str
    mode: Mode
    job_id: str
    trial_id: str
    reward: int
    write_fault: Fault = "none"
    spec_id: str = ""
    attempts: int = 0
    ingests: int = 0
    replays_match: bool = True
    due: int | None = None
    source: tuple[tuple[str, str], ...] = ()

    @property
    def terminal(self) -> QueueState:
        return "failed" if self.mode in ("backend-error", "timeout") else "done"


class ExecutionSimulation:
    def __init__(self, sim: Simulation, root: Path) -> None:
        self.sim = sim
        self.root = root
        self.queue = DirectoryQueue(
            root / "queue", events_max_bytes=900, event_backups=20, runtime=sim.runtime
        )
        self.policy = StandingApprovalsPolicy(
            daily_cost_ceiling_usd=1,
            per_job_cost_ceiling_usd=1,
            quiet_failure_rule=2,
            auto_run=[AutoRunRule(name="simulation", agents=["oracle"])],
        )
        self.jobs: dict[str, Job] = {}
        self.catalog: dict[str, tuple[object, ...]] = {}
        self.probes: list[tuple[int, str | None]] = []
        self.probing = False
        self.launches_fenced = True
        self.seen_events = 0

    def executor(self) -> Executor:
        return Executor(
            repo_root=self.root,
            queue=self.queue,
            policy=self.policy,
            runner=self.backend,
            ingester=self.ingest,
            credential_probe=frozenset,
            spent_today=lambda: 0.0,
            consecutive_harness_failures=lambda: 0,
            sleeper=self.backoff,
            watch_enabled=False,
            parallel=1,
        )

    def run(self) -> None:
        rng = self.sim.rng
        modes = rng.sample(_MODES, 3)
        if self.sim.terminal_write_fault != "none":
            modes[0] = "success"
        for index, mode in enumerate(modes):
            job = Job(
                name=f"work-{index}",
                mode=mode,
                job_id=str(UUID(int=rng.getrandbits(128))),
                trial_id=str(UUID(int=rng.getrandbits(128))),
                reward=rng.randrange(2),
                write_fault=self.sim.terminal_write_fault if index == 0 else "none",
            )
            self.submit(job)
        jobs = list(self.jobs.values())
        for _ in range(13):
            if rng.randrange(3) == 0:
                self.advance(rng.randint(1, 6))
            else:
                self.tick(rng.choice(jobs))
        for _ in range(len(jobs) + 1):
            self.advance(11)
            self.tick()
        self.sim.begin("execution-drained")
        states = self.check()
        for job in jobs:
            self.sim.require(states[job.spec_id] == job.terminal, "admitted_job_reaches_terminal")
            self.sim.require(
                job.attempts == (2 if job.mode == "retry" else 1), "expected_launch_count"
            )
            ingests = 0 if job.terminal == "failed" else 2 if job.mode == "ingest-crash" else 1
            self.sim.require(job.ingests == ingests, "expected_ingest_count")

    def submit(self, job: Job) -> None:
        self.sim.begin("submit", job=job.name, mode=job.mode, write_fault=job.write_fault)
        path, decision = self.executor().submit(spec_for(job.name))
        spec_id = self.queue.load(path).spec_id
        self.sim.require(bool(spec_id) and decision.admitted, "free_spec_admitted")
        assert spec_id is not None
        job.spec_id = spec_id
        self.jobs[job.name] = job
        self.check()

    def tick(self, job: Job | None = None) -> None:
        sim = self.sim
        sim.begin("tick", target=job.name if job else None)
        fault = "none"
        try:
            dispatched = self.executor().tick(spec_ids=[job.spec_id] if job else None)
            sim.note(dispatched=dispatched)
        except ProcessCrash:
            fault = "process-crash"
        except OSError as exc:
            if exc.errno != errno.ENOSPC:
                raise
            fault = "disk-full"
        sim.note(tick_fault=fault)
        self.check()

    def advance(self, seconds: int) -> None:
        sim = self.sim
        sim.begin("clock", seconds=seconds)
        end = sim.runtime.seconds + seconds
        for job in self.jobs.values():
            if job.due is not None and job.due <= end:
                sim.runtime.seconds = job.due
                self.publish(job, self.root / "runs" / job.name)
                job.due = None
        sim.runtime.seconds = end
        self.check()

    def backoff(self, seconds: float) -> None:
        assert 0 < seconds <= 30 and seconds == int(seconds)
        self.sim.note(retry_backoff_seconds=seconds)
        self.sim.runtime.seconds += int(seconds)

    def backend(self, request: RunRequest) -> Path:
        job = self.jobs[request.name]
        self.launches_fenced &= (
            request.lease_path is not None
            and request.lease_generation is not None
            and self.queue.lease_generation(request.lease_path) == request.lease_generation
        )
        self.sim.note(backend=job.name, attempt=job.attempts + 1, lease=request.lease_generation)
        job.attempts += 1
        if not self.probing:
            self.probing = True
            try:
                contender = self.executor()
                dispatched = contender.tick(spec_ids=[job.spec_id])
                self.probes.append((dispatched, contender.last_tick_reason))
            finally:
                self.probing = False
        if job.mode == "backend-error":
            raise RuntimeError("simulated permanent backend error")
        if job.mode == "retry" and job.attempts == 1:
            raise TransientHarnessFailure("simulation_backend_unavailable")
        if job.mode == "detached":
            job.due = self.sim.runtime.seconds + self.sim.rng.randint(1, 8)
            self.sim.note(detached_completion_due=job.due)
            raise ProcessCrash
        if job.mode == "timeout":
            state = request.jobs_dir / ".executor" / f"{job.name}.state.json"
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text(
                json.dumps(
                    {
                        "status": "running",
                        "started_at": self.sim.runtime.now().isoformat(),
                        "job_timeout_seconds": request.timeout_seconds,
                    }
                )
            )
            raise ProcessCrash
        path = request.jobs_dir / request.name
        self.publish(job, path)
        return path

    def publish(self, job: Job, path: Path) -> None:
        contents = {
            "trial/result.json": {
                "id": job.trial_id,
                "task_name": "simulation-task",
                "trial_name": "trial",
                "verifier_result": {"rewards": {"reward": job.reward}},
            },
            "result.json": {
                "id": job.job_id,
                "finished_at": self.sim.runtime.now().isoformat(),
                "n_total_trials": 1,
                "stats": {},
            },
        }
        hashes = []
        for name, data in contents.items():
            target = path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(data, sort_keys=True).encode()
            with target.open("xb") as handle:
                handle.write(payload)
            hashes.append((name, hashlib.sha256(payload).hexdigest()))
        job.source = tuple(sorted(hashes))
        self.sim.note(published=job.name, source=job.source)

    def ingest(self, path: Path) -> None:
        job = self.jobs[path.name]
        result = load_job(path)
        row = (
            result.id,
            tuple((trial.id, trial.primary_reward) for trial in result.trials),
            tuple((file.relative_path, file.sha256) for file in result.files),
        )
        prior = self.catalog.get(result.id)
        self.catalog[result.id] = row
        job.ingests += 1
        job.replays_match &= prior is None or prior == row
        self.sim.note(ingested=job.name, delivery=job.ingests, replay_matches=job.replays_match)
        if job.mode == "ingest-crash" and job.ingests == 1:
            raise ProcessCrash
        if job.ingests == 1:
            self.sim.runtime.event_fault = job.write_fault

    def check(self) -> dict[str, QueueState]:
        sim = self.sim
        records = [
            (str(spec.spec_id), state)
            for state in QUEUE_STATES
            for _, spec in self.queue.list_specs(state)
        ]
        states = dict(records)
        sim.require(len(states) == len(records), "unique_spec_state")
        sim.require(
            states.keys() == {job.spec_id for job in self.jobs.values()}, "admitted_specs_retained"
        )
        sim.note(queue=states, catalog=sorted(self.catalog))
        self.check_events(states)
        sim.require(
            all(probe == (0, "executor_busy") for probe in self.probes), "competing_tick_fenced"
        )
        sim.require(self.launches_fenced, "backend_launch_requires_lease")
        sim.require(
            self.catalog.keys() <= {job.job_id for job in self.jobs.values()},
            "no_foreign_catalog_rows",
        )
        for job in self.jobs.values():
            self.check_job(job, states[job.spec_id])
        return states

    def check_job(self, job: Job, state: QueueState) -> None:
        sim = self.sim
        sim.require(
            job.attempts <= (2 if job.mode == "retry" else 1), "no_duplicate_backend_launch"
        )
        row = self.catalog.get(job.job_id)
        if job.source:
            root = self.root / "runs" / job.name
            hashes = tuple(
                (file, hashlib.sha256((root / file).read_bytes()).hexdigest())
                for file, _ in job.source
            )
            sim.require(hashes == job.source, "published_source_immutable")
        if job.ingests:
            sim.require(job.replays_match, "duplicate_ingest_is_idempotent")
            sim.require(
                row == (job.job_id, ((job.trial_id, job.reward),), job.source),
                "catalog_matches_source",
            )
        if state in _TERMINAL:
            sim.require(state == job.terminal, "terminal_outcome_matches_backend")
            sim.require(state != "done" or row is not None, "completion_requires_catalog")

    def check_events(self, states: dict[str, QueueState]) -> None:
        sim = self.sim
        try:
            events = load_events(self.queue.events_path)
        except (OSError, ValueError):
            sim.require(False, "event_ledger_readable")
            raise
        sim.require(True, "event_ledger_readable")
        sim.require(len({event.event_id for event in events}) == len(events), "event_ids_unique")
        replayed: dict[str, QueueState] = {}
        terminals: Counter[str] = Counter()
        for event in events:
            spec_id = event.spec_id
            if event.event == "dispatch_retry_scheduled":
                sim.require(replayed.get(spec_id) == "running", "retry_has_active_dispatch")
                continue
            edge = (event.from_state, event.to_state)
            sim.require(edge == _EDGES.get(event.event), "event_transition_legal")
            sim.require(replayed.get(spec_id) == event.from_state, "event_chain_ordered")
            assert event.to_state is not None
            replayed[spec_id] = event.to_state
            if event.to_state in _TERMINAL:
                terminals[spec_id] += 1
        sim.require(all(count == 1 for count in terminals.values()), "single_terminal_event")
        sim.require(replayed == states, "event_ledger_matches_queue")
        for event in events[self.seen_events :]:
            sim.note(event=event.model_dump(mode="json", exclude_none=True))
        self.seen_events = len(events)
