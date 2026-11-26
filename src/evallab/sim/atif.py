from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from random import Random
from typing import Any

from evallab.evidence import atif
from evallab.evidence.atif import TrialTrajectoryProjection, project_trial
from evallab.results import JobRecord, TrialRecord, load_job
from evallab.sim.engine import STEP_LIMIT, Simulation

_STEP_BUDGET = 20
_MIN_TRIALS = 4
_MAX_TRIALS = 8
_VALIDATOR = "internal-atif-v1"
_UNSUPPORTED = ("ATIF-v0.9", "ATIF-v1.9", "ATIF-v1.10", "ATIF-v2.0")
_SHAPES = (
    "valid",
    "unsupported",
    "no-agent-name",
    "empty-steps",
    "broken-json",
    "embedded",
    "sibling-ref",
    "missing-ref",
    "escape-ref",
    "absolute-ref",
    "cycle",
)
_MISSING_PATH = "subagents/missing-child.json"
_CYCLE_PATHS = ("agent/trajectory-cycle.json", "subagents/cycle-peer.json")
_SHARED_PATH = "subagents/shared.json"

Calls = tuple[int, ...]
Ref = tuple[int, dict[str, str]]


class _LoadBound(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _Shape:
    name: str
    trial: int
    calls: Calls
    children: tuple[Calls, ...]
    minor: int
    variant: int


@dataclass(frozen=True, slots=True)
class _Expect:
    source_path: str
    embedded_path: str | None
    status: str
    validator: str
    error: str | None
    steps: int
    tool_calls: int


Built = tuple[dict[str, str], list[_Expect]]


def _calls(rng: Random, low: int, high: int) -> Calls:
    assert 1 <= low <= high <= 4
    return tuple(rng.randint(0, 2) for _ in range(rng.randint(low, high)))


def _plan(rng: Random) -> list[_Shape]:
    trials = rng.randint(_MIN_TRIALS, _MAX_TRIALS)
    order = list(_SHAPES)
    rng.shuffle(order)
    shapes: list[_Shape] = []
    for index, name in enumerate(order):
        trial = index if index < trials else rng.randrange(trials)
        children = tuple(_calls(rng, 1, 3) for _ in range(rng.randint(1, 2)))
        shapes.append(
            _Shape(name, trial, _calls(rng, 2, 4), children, rng.randrange(9), rng.randrange(1000))
        )
    assert len(shapes) == len(_SHAPES)
    assert {shape.trial for shape in shapes} == set(range(trials))
    return sorted(shapes, key=lambda shape: (shape.trial, _SHAPES.index(shape.name)))


def _step(index: int, count: int, refs: tuple[Ref, ...]) -> dict[str, Any]:
    assert index >= 1 and 0 <= count <= 2
    step: dict[str, Any] = {"step_id": index, "source": "agent", "message": f"step {index}"}
    calls = [
        {
            "tool_call_id": f"call-{index}-{position}",
            "function_name": "bash",
            "arguments": {"command": f"echo {index}.{position}"},
        }
        for position in range(count)
    ]
    results: list[dict[str, Any]] = [
        {"source_call_id": call["tool_call_id"], "content": f"out {call['tool_call_id']}"}
        for call in calls
    ]
    results.extend(
        {"content": "subagent", "subagent_trajectory_ref": [ref]} for at, ref in refs if at == index
    )
    if calls:
        step["tool_calls"] = calls
    if results:
        step["observation"] = {"results": results}
    return step


def _payload(
    calls: Calls, minor: int, refs: tuple[Ref, ...] = (), continued: str | None = None
) -> dict[str, Any]:
    assert all(1 <= at <= len(calls) for at, _ in refs)
    payload: dict[str, Any] = {
        "schema_version": f"ATIF-v1.{minor}",
        "session_id": "sim-session",
        "agent": {"name": "sim-agent", "version": "1.0"},
        "steps": [_step(index, count, refs) for index, count in enumerate(calls, start=1)],
    }
    if continued is not None:
        payload["continued_trajectory_ref"] = continued
    return payload


def _text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True)


def _doc(path: str, calls: Calls, status: str = "valid", error: str | None = None) -> _Expect:
    return _Expect(path, None, status, _VALIDATOR, error, len(calls), sum(calls))


def _ref(path: str) -> dict[str, str]:
    return {"trajectory_id": Path(path).stem, "trajectory_path": path}


def _valid(shape: _Shape) -> Built:
    path = "agent/trajectory.json"
    return {path: _text(_payload(shape.calls, shape.minor))}, [_doc(path, shape.calls)]


def _unsupported(shape: _Shape) -> Built:
    path = "agent/run-unsupported.json"
    version = _UNSUPPORTED[shape.variant % len(_UNSUPPORTED)]
    payload = _payload(shape.calls, shape.minor)
    payload["schema_version"] = version
    error = f"unsupported schema_version {version!r}"
    return {path: _text(payload)}, [_doc(path, shape.calls, "unsupported", error)]


def _no_agent_name(shape: _Shape) -> Built:
    path = "agent/trajectory-no-agent-name.json"
    payload = _payload(shape.calls, shape.minor)
    variant = shape.variant % 3
    if variant == 0:
        del payload["agent"]
    else:
        payload["agent"] = {"version": "1.0"} if variant == 1 else {"name": 7, "version": "1.0"}
    error = "agent.name must be a string"
    return {path: _text(payload)}, [_doc(path, shape.calls, "invalid", error)]


def _empty_steps(shape: _Shape) -> Built:
    path = "agent/trajectory-empty-steps.json"
    payload = _payload(shape.calls, shape.minor)
    payload["steps"] = []
    error = "steps must be a non-empty array"
    return {path: _text(payload)}, [_doc(path, (), "invalid", error)]


def _broken_json(shape: _Shape) -> Built:
    path = "agent/trajectory-broken-json.json"
    text = _text(_payload(shape.calls, shape.minor))
    cut = 1 + shape.variant % (len(text) - 1)
    assert 1 <= cut < len(text)
    expect = _Expect(path, None, "invalid", "json", "JSONDecodeError: ", 0, 0)
    return {path: text[:cut]}, [expect]


def _embedded(shape: _Shape) -> Built:
    path = "agent/trajectory-embedded.json"
    payload = _payload(shape.calls, shape.minor, ((1, {"trajectory_id": "sub-0"}),))
    children = []
    expects = [_doc(path, shape.calls)]
    for index, calls in enumerate(shape.children):
        child = _payload(calls, shape.minor)
        child["trajectory_id"] = f"sub-{index}"
        children.append(child)
        embedded = f"subagent:sub-{index}"
        expects.append(_Expect(path, embedded, "valid", _VALIDATOR, None, len(calls), sum(calls)))
    payload["subagent_trajectories"] = children
    return {path: _text(payload)}, expects


def _sibling_ref(shape: _Shape) -> Built:
    path = "agent/trajectory-sibling.json"
    in_agent = shape.variant % 2 == 0
    child = "agent/sibling-child.json" if in_agent else "subagents/sibling-child.json"
    reference = "sibling-child.json" if in_agent else "../subagents/sibling-child.json"
    child_calls = shape.children[0]
    files = {
        path: _text(_payload(shape.calls, shape.minor, ((1, _ref(reference)),))),
        child: _text(_payload(child_calls, shape.minor)),
    }
    return files, [_doc(path, shape.calls), _doc(child, child_calls)]


def _missing_ref(shape: _Shape) -> Built:
    path = "agent/trajectory-missing.json"
    reference = f"../{_MISSING_PATH}"
    at = 1 + shape.variant % len(shape.calls)
    files = {path: _text(_payload(shape.calls, shape.minor, ((at, _ref(reference)),)))}
    error = f"trajectory reference is missing: {reference!r}"
    return files, [_doc(path, shape.calls, "invalid", error)]


def _escape_ref(shape: _Shape) -> Built:
    path = "agent/trajectory-escape.json"
    reference = "../../escape.json"
    at = 1 + shape.variant % len(shape.calls)
    files = {
        path: _text(_payload(shape.calls, shape.minor, ((at, _ref(reference)),))),
        "../escape.json": _text(_payload(shape.children[0], shape.minor)),
    }
    error = f"trajectory reference escapes trial directory: {reference!r}"
    return files, [_doc(path, shape.calls, "invalid", error)]


def _absolute_ref(shape: _Shape) -> Built:
    path = "agent/trajectory-absolute.json"
    child = "subagents/absolute-child.json"
    child_calls = shape.children[0]
    files = {
        path: _text(_payload(shape.calls, shape.minor, ((1, _ref(f"/{child}")),))),
        child: _text(_payload(child_calls, shape.minor)),
    }
    return files, [_doc(path, shape.calls), _doc(child, child_calls)]


def _cycle(shape: _Shape) -> Built:
    root, peer = _CYCLE_PATHS
    twice = "agent/trajectory-twice.json"
    peer_calls = shape.children[0]
    shared_calls = shape.children[-1]
    shared = _ref(f"../{_SHARED_PATH}")
    files = {
        root: _text(_payload(shape.calls, shape.minor, ((1, _ref(f"../{peer}")),))),
        peer: _text(_payload(peer_calls, shape.minor, continued=f"../{root}")),
        twice: _text(_payload(shape.calls, shape.minor, ((1, shared), (2, shared)))),
        _SHARED_PATH: _text(_payload(shared_calls, shape.minor)),
    }
    expects = [
        _doc(root, shape.calls),
        _doc(peer, peer_calls),
        _doc(twice, shape.calls),
        _doc(_SHARED_PATH, shared_calls),
    ]
    return files, expects


_BUILDERS: dict[str, Callable[[_Shape], Built]] = {
    "valid": _valid,
    "unsupported": _unsupported,
    "no-agent-name": _no_agent_name,
    "empty-steps": _empty_steps,
    "broken-json": _broken_json,
    "embedded": _embedded,
    "sibling-ref": _sibling_ref,
    "missing-ref": _missing_ref,
    "escape-ref": _escape_ref,
    "absolute-ref": _absolute_ref,
    "cycle": _cycle,
}


@contextmanager
def _bounded_loads(limit: int) -> Iterator[list[int]]:
    assert limit > 0
    original = atif._load_json
    loads = [0]

    def counted(path: Path) -> tuple[Any | None, str | None]:
        loads[0] += 1
        if loads[0] > limit:
            raise _LoadBound(path.name)
        return original(path)

    namespace = vars(atif)
    assert namespace["_load_json"] is original
    namespace["_load_json"] = counted
    try:
        yield loads
    finally:
        namespace["_load_json"] = original


def _digest(projection: TrialTrajectoryProjection) -> str:
    return hashlib.sha256(repr(projection).encode()).hexdigest()


class AtifSimulation:
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
        sim.require(_plan(Random(plan_seed)) == shapes, "atif_plan_deterministic")
        trials = 1 + max(shape.trial for shape in shapes)
        sim.begin("atif-plan", trials=trials, shapes=[[s.trial, s.name] for s in shapes])
        expects = self._write_job(self.root / "first", shapes, trials)
        self._write_job(self.root / "second", shapes, trials)
        first = load_job(self.root / "first")
        sim.require(len(first.trials) == trials, "atif_job_trial_count")
        projected: list[TrialTrajectoryProjection] = []
        for index, trial in enumerate(first.trials):
            sim.begin(
                "atif-trial", trial=index, shapes=[s.name for s in shapes if s.trial == index]
            )
            projection = self._project(first, trial)
            projected.append(projection)
            self._check_trial(
                projection, expects[index], {s.name for s in shapes if s.trial == index}
            )
            sim.require(self._project(first, trial) == projection, "atif_reprojection_identical")
        sim.begin("atif-replay", trials=trials)
        second = load_job(self.root / "second")
        replayed = [self._project(second, trial) for trial in second.trials]
        sim.require(replayed == projected, "atif_replay_identical_facts")
        sim.note(atif_digests=[_digest(projection) for projection in projected])
        sim.require(sim.step - self.start_step <= _STEP_BUDGET, "atif_step_budget")

    def _write_job(self, job_dir: Path, shapes: list[_Shape], trials: int) -> list[list[_Expect]]:
        assert _MIN_TRIALS <= trials <= _MAX_TRIALS
        now = self.sim.runtime.now().isoformat()
        expects: list[list[_Expect]] = []
        for index in range(trials):
            trial_dir = job_dir / f"trial-{index}"
            (trial_dir / "agent").mkdir(parents=True)
            result = {"id": f"trial-{self.sim.seed}-{index}", "task_name": "simulation-task"}
            result.update(trial_name=f"atif-{index}", finished_at=now)
            (trial_dir / "result.json").write_text(json.dumps(result, sort_keys=True))
            trial_expects: list[_Expect] = []
            for shape in shapes:
                if shape.trial != index:
                    continue
                files, documents = _BUILDERS[shape.name](shape)
                for relative, text in files.items():
                    target = (trial_dir / relative).resolve()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    assert not target.exists()
                    target.write_text(text)
                trial_expects.extend(documents)
            expects.append(trial_expects)
        job = {"id": f"atif-job-{self.sim.seed}", "finished_at": now}
        job.update(n_total_trials=trials, stats={})
        (job_dir / "result.json").write_text(json.dumps(job, sort_keys=True))
        return expects

    def _project(self, job: JobRecord, trial: TrialRecord) -> TrialTrajectoryProjection:
        files = sum(1 for path in trial.path.rglob("*.json") if path.is_file())
        assert files >= 2
        try:
            with _bounded_loads(2 * files) as loads:
                projection = project_trial(job, trial)
        except _LoadBound:
            self.sim.require(False, "atif_projection_load_bound")
            raise AssertionError("unreachable") from None
        self.sim.require(loads[0] <= 2 * files, "atif_projection_load_bound")
        return projection

    def _check_trial(
        self, projection: TrialTrajectoryProjection, expects: list[_Expect], names: set[str]
    ) -> None:
        sim = self.sim
        facts = projection.trajectories
        keys = Counter((fact.source_path, fact.embedded_path) for fact in facts)
        wanted = Counter((want.source_path, want.embedded_path) for want in expects)
        sim.note(documents=len(facts), expected=len(expects))
        sim.require(keys == wanted, "atif_documents_projected_once")
        by_key = {(fact.source_path, fact.embedded_path): fact for fact in facts}
        for want in expects:
            fact = by_key[(want.source_path, want.embedded_path)]
            sim.require(fact.validation_status == want.status, "atif_validation_status")
            sim.require(fact.validator == want.validator, "atif_validator")
            if want.error is None:
                sim.require(fact.validation_error is None, "atif_valid_has_no_error")
            else:
                error = fact.validation_error or ""
                sim.require(error.startswith(want.error), "atif_validation_error_prefix")
            steps = [step for step in projection.steps if step.document_id == fact.document_id]
            calls = [call for call in projection.tool_calls if call.document_id == fact.document_id]
            sim.require(fact.step_count == want.steps == len(steps), "atif_step_fact_count")
            sim.require(len(calls) == want.tool_calls, "atif_tool_call_fact_count")
        sim.require(len(projection.steps) == sum(w.steps for w in expects), "atif_step_total")
        sim.require(
            len(projection.tool_calls) == sum(w.tool_calls for w in expects), "atif_tool_call_total"
        )
        self._check_references(Counter(fact.source_path for fact in facts), names)

    def _check_references(self, paths: Counter[str], names: set[str]) -> None:
        sim = self.sim
        sim.require(
            all(not path.startswith(("..", "/")) for path in paths), "atif_escape_never_followed"
        )
        if "missing-ref" in names:
            sim.require(paths[_MISSING_PATH] == 0, "atif_missing_never_followed")
        if "cycle" in names:
            sim.require(all(paths[path] == 1 for path in _CYCLE_PATHS), "atif_cycle_projects_once")
            sim.require(paths[_SHARED_PATH] == 1, "atif_shared_reference_projects_once")
