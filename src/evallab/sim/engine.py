from __future__ import annotations

import errno
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
from typing import Literal

from evallab.execution_contracts import new_ulid
from evallab.queue import QueueRuntime
from evallab.schemas import ExperimentSpec


class InvariantViolation(AssertionError):
    def __init__(self, seed: int, step: int, subsystem: str, invariant: str) -> None:
        super().__init__(f"seed={seed} step={step} subsystem={subsystem} invariant={invariant}")
        self.seed = seed
        self.step = step
        self.subsystem = subsystem
        self.invariant = invariant


class ProcessCrash(BaseException):
    pass


Fault = Literal["none", "disk-full", "torn"]


class VirtualRuntime(QueueRuntime):
    def __init__(self, rng: Random) -> None:
        self.rng = rng
        self.seconds = 1_700_000_000
        self.lease_fault: Fault = "none"
        self.event_fault: Fault = "none"

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.seconds, UTC)

    def wall_time(self) -> float:
        return self.seconds

    def new_id(self) -> str:
        return new_ulid(timestamp_ms=self.seconds * 1000, randomness=self.rng.getrandbits(80))

    def new_generation(self) -> str:
        return f"{self.rng.getrandbits(128):032x}"

    def owner_pid(self) -> int:
        return 10

    def hostname(self) -> str:
        return "simulation"

    def write_event(self, descriptor: int, payload: memoryview) -> int:
        fault = self.event_fault
        self.event_fault = "none"
        if fault == "disk-full":
            raise OSError(errno.ENOSPC, "simulated disk full")
        if fault == "torn":
            super().write_event(descriptor, payload[: len(payload) // 2])
            raise ProcessCrash
        return super().write_event(descriptor, payload[: self.rng.randint(64, 512)])

    def write_lease(self, descriptor: int, payload: bytes) -> None:
        self.heartbeat(descriptor)
        if self.lease_fault == "disk-full":
            raise OSError(errno.ENOSPC, "simulated disk full")
        if self.lease_fault == "torn":
            super().write_lease(descriptor, payload[: len(payload) // 2])
            self.heartbeat(descriptor)
            raise ProcessCrash
        super().write_lease(descriptor, payload)
        self.heartbeat(descriptor)

    def heartbeat(self, descriptor: int) -> None:
        os.utime(descriptor, (self.seconds, self.seconds))


def spec_for(name: str) -> ExperimentSpec:
    return ExperimentSpec(
        name=name,
        task="simulation-task",
        agent="oracle",
        hypothesis="Queue recovery preserves work and evidence",
        purpose="practice",
        submitted_by="simulation",
        jobs_dir="runs",
        timeout_seconds=10,
    )


STEP_LIMIT = 240


class Simulation:
    def __init__(self, seed: int) -> None:
        assert 0 <= seed < 2**64
        self.seed = seed
        self.rng = Random(seed)
        roll = self.rng.randrange(2048)
        self.terminal_write_fault: Fault = (
            "disk-full" if roll == 0 else "torn" if roll == 1 else "none"
        )
        self.runtime = VirtualRuntime(self.rng)
        self.step = 0
        self.subsystem = "queue-leases"
        self.checks = 0
        self.trace = bytearray()
        self.note(seed=seed)

    def note(self, **fields: object) -> None:
        self.trace.extend(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode())
        self.trace.append(10)

    def begin(self, action: str, **fields: object) -> None:
        self.step += 1
        assert self.step <= STEP_LIMIT
        self.note(
            step=self.step,
            subsystem=self.subsystem,
            time=self.runtime.seconds,
            action=action,
            **fields,
        )

    def require(self, condition: bool, invariant: str) -> None:
        self.checks += 1
        if not condition:
            raise InvariantViolation(self.seed, self.step, self.subsystem, invariant)


@dataclass(frozen=True, slots=True)
class RunResult:
    trace: bytes
    checks: int
    failure: InvariantViolation | None

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.trace).hexdigest()


def run(seed: int) -> RunResult:
    from evallab.sim.anatomy import AnatomySimulation
    from evallab.sim.execution import ExecutionSimulation
    from evallab.sim.hack import HackSimulation
    from evallab.sim.hypotheses import HypothesisSimulation
    from evallab.sim.jobs import ProcessJobSimulation
    from evallab.sim.leases import LeaseSimulation
    from evallab.sim.lint import LintSimulation
    from evallab.sim.mechanics import MechanicsSimulation
    from evallab.sim.mutation import MutationSimulation
    from evallab.sim.overlap import OverlapSimulation
    from evallab.sim.protected import ProtectedPathSimulation
    from evallab.sim.rewards import RewardSimulation
    from evallab.sim.rigor import RigorSimulation
    from evallab.sim.surface import SurfaceSimulation
    from evallab.sim.verifier import VerifierSimulation

    sim = Simulation(seed)
    failure = None
    with TemporaryDirectory(prefix="evallab-sim-") as directory:
        root = Path(directory).resolve()
        try:
            LeaseSimulation(sim, root / "leases").run()
            sim.subsystem = "queue-execution"
            ExecutionSimulation(sim, root).run()
            sim.subsystem = "task-lint"
            LintSimulation(sim, root / "packages").run()
            sim.subsystem = "reward-readers"
            RewardSimulation(sim, root / "rewards").run()
            sim.subsystem = "reward-hack"
            HackSimulation(sim, root / "hack").run()
            sim.subsystem = "process-job"
            ProcessJobSimulation(sim, root / "jobs").run()
            sim.subsystem = "verifier-mutation"
            MutationSimulation(sim).run()
            sim.subsystem = "protected-path-read"
            ProtectedPathSimulation(sim).run()
            sim.subsystem = "separate-verifier"
            VerifierSimulation(sim, root / "verifier").run()
            sim.subsystem = "package-anatomy"
            AnatomySimulation(sim, root / "anatomy").run()
            sim.subsystem = "test-suite-rigor"
            RigorSimulation(sim, root / "rigor").run()
            sim.subsystem = "grader-script-mechanics"
            MechanicsSimulation(sim, root / "mechanics").run()
            sim.subsystem = "instruction-patch-surface"
            SurfaceSimulation(sim).run()
            sim.subsystem = "baked-script-overlap"
            OverlapSimulation(sim, root / "overlap").run()
            sim.subsystem = "anatomy-hypotheses"
            HypothesisSimulation(sim, root / "hypotheses").run()
        except InvariantViolation as exc:
            failure = exc
        except (Exception, ProcessCrash) as exc:
            name = type(exc).__name__
            sim.note(error=str(exc).replace(str(root), "<scratch>"), error_type=name)
            failure = InvariantViolation(seed, sim.step, sim.subsystem, f"unexpected_{name}")
    if failure is not None:
        sim.note(failure=failure.invariant)
    return RunResult(bytes(sim.trace), sim.checks, failure)
