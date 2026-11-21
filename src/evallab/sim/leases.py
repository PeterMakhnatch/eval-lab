from __future__ import annotations

import errno
import json
from dataclasses import dataclass, replace
from pathlib import Path

from evallab.queue import DirectoryQueue
from evallab.sim.engine import Fault, ProcessCrash, Simulation, spec_for

_TTL = 8
_WORKERS = (11, 12, 13)


@dataclass(frozen=True, slots=True)
class Lease:
    generation: str | None
    worker: int
    mtime: int


class LeaseSimulation:
    def __init__(self, sim: Simulation, root: Path) -> None:
        self.sim = sim
        self.queue = DirectoryQueue(root, runtime=sim.runtime)
        self.spec = spec_for("lease-job").model_copy(update={"spec_id": "lease-job"})
        self.path = self.queue.lease_path(self.spec)
        self.lease: Lease | None = None
        self.cancels: set[str] = set()
        self.handles: dict[int, str] = {}
        self.messages: list[str] = []

    def run(self) -> None:
        rng = self.sim.rng
        first, second, third = rng.sample(_WORKERS, 3)
        self.claim(first)
        old = self.handles[first]
        self.cancel()
        self.crash(first)
        self.advance(_TTL)
        self.claim(second)
        self.advance(1)
        self.claim(second)
        self.cancel()
        self.heartbeat(old)
        self.release(old)
        self.release(self.handles[second])
        self.claim(third, rng.choice(("torn", "disk-full")))
        self.cancel()
        self.advance(_TTL + 1)
        self.claim(first)
        for _ in range(24):
            self.interleave()
        if self.lease is not None:
            expiry = self.lease.mtime + _TTL + 1
            self.advance(max(0, expiry - self.sim.runtime.seconds))
            self.claim(rng.choice(_WORKERS))
        if self.lease is not None and self.lease.generation is not None:
            self.release(self.lease.generation)
        for token in sorted(self.cancels):
            self.release(token)
        self.sim.begin("lease-drained")
        self.check()
        self.sim.require(self.lease is None and not self.cancels, "lease_drained")

    def interleave(self) -> None:
        rng = self.sim.rng
        worker = rng.choice(_WORKERS)
        action = rng.randrange(6)
        token = self.handles.get(worker) or rng.choice(self.messages)
        if action == 0:
            self.advance(rng.randint(-3, _TTL * 2))
        elif action == 1:
            self.claim(worker, rng.choice(("none", "none", "none", "torn", "disk-full")))
        elif action == 2:
            self.heartbeat(token)
        elif action == 3:
            self.release(token)
        elif action == 4:
            self.cancel()
        else:
            self.crash(worker)

    def stale(self) -> bool:
        return self.lease is None or self.sim.runtime.seconds - self.lease.mtime > _TTL

    def claim(self, worker: int, fault: Fault = "none") -> None:
        sim = self.sim
        token = sim.runtime.new_generation()
        sim.begin("claim", worker=worker, generation=token, fault=fault)
        expected = ("claimed" if fault == "none" else fault) if self.stale() else "blocked"
        sim.runtime.lease_fault = fault
        try:
            path = self.queue.acquire_lease(
                self.spec, owner_pid=worker, stale_seconds=_TTL, lease_generation=token
            )
            outcome = "blocked" if path is None else "claimed"
        except ProcessCrash:
            outcome = "torn"
        except OSError as exc:
            if exc.errno != errno.ENOSPC:
                raise
            outcome = "disk-full"
        finally:
            sim.runtime.lease_fault = "none"
        sim.require(outcome == expected, "exclusive_lease_claim")
        if expected != "blocked":
            self.lease = Lease(token if fault == "none" else None, worker, sim.runtime.seconds)
        if expected == "claimed":
            self.handles[worker] = token
            self.messages.append(token)
        self.check()

    def cancel(self) -> None:
        self.sim.begin("cancel")
        current = self.lease.generation if self.lease is not None else None
        self.sim.require(
            self.queue.request_cancel(self.spec) == bool(current), "cancel_fails_closed"
        )
        if current is not None:
            self.cancels.add(current)
        self.check()

    def heartbeat(self, token: str) -> None:
        self.sim.begin("heartbeat", generation=token)
        lease = self.lease
        owns = lease is not None and lease.generation == token
        actual = self.queue.heartbeat_lease(self.spec, lease_generation=token)
        self.sim.require(actual == owns, "heartbeat_fences_stale_generation")
        if owns and lease is not None:
            self.lease = replace(lease, mtime=self.sim.runtime.seconds)
        self.check()

    def release(self, token: str) -> None:
        self.sim.begin("release", generation=token)
        owns = self.lease is not None and self.lease.generation == token
        actual = self.queue.release_lease(self.spec, lease_generation=token)
        self.sim.require(actual == owns, "release_fences_stale_generation")
        if owns:
            self.lease = None
        self.cancels.discard(token)
        self.check()

    def advance(self, seconds: int) -> None:
        self.sim.begin("clock", seconds=seconds)
        self.sim.runtime.seconds += seconds
        self.check()

    def crash(self, worker: int) -> None:
        self.sim.begin("crash-and-restart", worker=worker)
        self.handles.pop(worker, None)
        self.queue = DirectoryQueue(self.queue.root, runtime=self.sim.runtime)
        self.check()

    def check(self) -> None:
        sim = self.sim
        lease = self.lease
        paths = self.queue.list_leases()
        sim.require(paths == ([] if lease is None else [self.path]), "lease_file_cardinality")
        generation = self.queue.lease_generation(self.path)
        sim.require(generation == (lease.generation if lease else None), "lease_generation_durable")
        sim.require(
            self.queue.is_lease_stale(self.path, stale_seconds=_TTL) == self.stale(), "lease_expiry"
        )
        if lease is not None and generation is not None:
            payload = json.loads(self.path.read_bytes())
            sim.require(payload["pid"] == lease.worker, "lease_owner_durable")
            sim.require(self.path.stat().st_mtime == lease.mtime, "lease_mtime_durable")
        markers = sorted(self.path.parent.glob(f"{self.path.name}.cancel.*"))
        marker_generations = []
        for marker in markers:
            payload = json.loads(marker.read_bytes())
            token = payload["lease_generation"]
            sim.require(
                marker.name == f"{self.path.name}.cancel.{token}"
                and payload["spec_id"] == self.spec.spec_id,
                "cancel_marker_bound_to_generation",
            )
            marker_generations.append(token)
        sim.require(marker_generations == sorted(self.cancels), "cancel_markers_durable")
        sim.note(generation=generation, stale=self.stale(), cancels=marker_generations)
