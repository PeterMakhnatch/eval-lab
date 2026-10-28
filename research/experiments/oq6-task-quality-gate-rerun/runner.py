"""OQ-6 v3 scheduling only; scientific and native helpers remain frozen v2 bytes.

Run one independent worker with --protocol PATH --route EXACT --arm A|B.
PATH has a sibling PATH.with_suffix('.sha256'). The protocol retains frozen.verify's
config/runtime/code_files/pre_cross_data_files/models fields, plus execution:
raw_root, science_root (immutable v2 helper/key root), plan_path, cutoff_at (UTC),
quota_recheck_seconds=60, priority_wait_seconds=5, disabled_routes={route: reason},
and optional route_order (default Sonnet/Opus/Kimi). The plan is
{cases: [original manifest metadata + key_count + scheduling_priority +
keys_sha256 + keys_provenance_path + keys_provenance_sha256],
workers: [{route, arm, case_ids: [ordered IDs]}]}. No case is removed by priority.
keys_path and its provenance must remain the original v2 cache paths; both bytes
are bound by the plan and rechecked before execute_cell (no cache construction).

Before launching ANY worker, the parent writes every entry returned by
initial_states(protocol, plan, freeze_sha256, plan_sha256, now=...). Entries are
keyed by '<route.replace("/", "--")>__<arm>' and go to pending/<key>.json:
  {schema: 'oq6.scheduler-worker/v1', route, arm, freeze_sha256, plan_sha256,
   next_index: 0, next_case_id: ID|null, next_priority: int|null,
   status: 'pending'|'finished', blocked_until: null, blocked_route: null,
   quota_admission_path: null, wait_basis: null, updated_at: UTC_ISO}
Subsequent status values are running, quota_pending, priority_wait, provider_wait.
blocked_until is a UTC ISO timestamp, NOT permission to send: every new request
still requires fresh admission. A missing preinitialized worker is a hard error.

Only pending/ and slices/ JSON is replaced atomically. events/<worker>/<uuid>.json,
outcomes/<worker>/<case>.json, route-stops/, and quota-admissions/ are immutable.
Outcome status is complete iff the existing paired evaluator says complete;
otherwise it is missing. Scheduler-only missing outcomes have cell_path=null and
retain audit/prediction references, null findings when unobserved, and a canonical
paired missing row. They never fabricate a native receipt or a final cell.json.
'status: finished' means a worker exhausted its plan, NOT scientific completeness.
A slice reports exact planned/attempted/answered/pending/missing denominators and
is checkpoint_partial or blocked unless every paired cell really is complete.

Import is stdlib-only. Clock, sleeper, science loader, verifier, and dispatcher
are injectable; no private files or native commands are touched during import.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
import types
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path

ROUTES = (
    "anthropic/claude-sonnet-5-5",
    "anthropic/claude-opus-5-5",
    "kimi-code/kimi-for-coding",
)
HELPERS = ("transport", "quota", "frozen", "scorer", "paired", "gold",
           "prepare_inputs", "run_slice")
ACTIVE = {"pending", "running", "quota_pending", "priority_wait", "provider_wait"}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def utc(seconds):
    return dt.datetime.fromtimestamp(seconds, dt.UTC).isoformat()


def timestamp(value):
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise ValueError("scheduler timestamps must explicitly use UTC")
    return parsed.timestamp()


def worker_key(route, arm):
    if (not isinstance(route, str) or
            not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9][A-Za-z0-9._:/-]*", route) or
            arm not in ("A", "B")):
        raise ValueError("worker requires an exact provider/model route and arm")
    return route.replace("/", "--") + "__" + arm


def read_json(path):
    return json.loads(Path(path).read_bytes())


def write_json(path, value, *, immutable=False):
    """Publish whole JSON atomically; link, never replace, immutable records."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".write-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write((json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        if immutable:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class LockBusy(Exception):
    pass


@contextmanager
def file_lock(path, *, nonblocking=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        try:
            fcntl.flock(handle, flags)
        except BlockingIOError:
            raise LockBusy(str(path)) from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def verify_protocol(path, *, expected_sha256=None, deep=False):
    """The frozen.verify guards, with an explicit protocol filename."""
    path = Path(path)
    raw = path.read_bytes()
    expected = path.with_suffix(".sha256").read_text().strip()
    if sha(raw) != expected or (expected_sha256 is not None and expected != expected_sha256):
        raise ValueError("frozen protocol bytes drifted")
    value = json.loads(raw)
    if sha(Path(value["config"]["path"]).read_bytes()) != value["config"]["sha256"]:
        raise ValueError("frozen invocation controls drifted")
    runtime = value["runtime"]
    invocation = runtime["invocation_path"]
    if shutil.which("omp") != invocation or Path(invocation).resolve() != Path(runtime["path"]):
        raise ValueError("native invocation resolves to a different frozen runtime")
    stat = Path(runtime["path"]).stat()
    if (stat.st_ino, stat.st_size, stat.st_mtime_ns) != (
        runtime["inode"], runtime["bytes"], runtime["mtime_ns"]
    ):
        raise ValueError("frozen native runtime changed; no model call is allowed")
    if deep:
        for row in value["code_files"] + value["pre_cross_data_files"]:
            if sha(Path(row["path"]).read_bytes()) != row["sha256"]:
                raise ValueError("frozen source/input drift: " + row["path"])
    return value, expected


def load_science(protocol):
    """Load checked v2 bytes without writing bytecode into immutable evidence."""
    for row in protocol["code_files"] + protocol.get("pre_cross_data_files", []):
        if sha(Path(row["path"]).read_bytes()) != row["sha256"]:
            raise ValueError("frozen scientific source/input drift: " + row["path"])
    root = Path(protocol["execution"]["science_root"]).resolve()
    rows = {Path(row["path"]).resolve(): row for row in protocol["code_files"]}
    paths = {name: root / (name + ".py") for name in HELPERS}
    sources = {}
    for name, path in paths.items():
        if path not in rows:
            raise ValueError("scientific helper is absent from the code freeze: " + str(path))
        sources[name] = path.read_bytes()
        if sha(sources[name]) != rows[path]["sha256"]:
            raise ValueError("frozen scientific helper drift: " + str(path))
    # Select the frozen checkout, not an editable install in the integration tree.
    worker_paths = [path for path in rows if path.parts[-3:] == ("evallab", "production", "tq_worker.py")]
    source_roots = {path.parents[2] for path in worker_paths}
    if len(source_roots) > 1:
        raise ValueError("multiple frozen production parser checkouts")
    old_path, old_bytecode = list(sys.path), sys.dont_write_bytecode
    previous = {name: sys.modules.get(name) for name in HELPERS}
    modules = {name: types.ModuleType(name) for name in HELPERS}
    try:
        sys.dont_write_bytecode = True
        sys.path[:0] = [str(path) for path in source_roots]
        for name, module in modules.items():
            module.__file__ = str(paths[name])
            module.__package__ = ""
            sys.modules[name] = module
        for name in HELPERS:
            exec(compile(sources[name], str(paths[name]), "exec"), modules[name].__dict__)
        for path in rows:
            if "src" not in path.parts or "evallab" not in path.parts:
                continue
            parts = path.parts[path.parts.index("evallab"):]
            module_name = ".".join(parts).removesuffix(".py")
            module = sys.modules.get(module_name)
            if module is not None and Path(module.__file__).resolve() != path:
                raise ValueError("imported scientific dependency is not the frozen path: " + module_name)
    finally:
        sys.path[:] = old_path
        sys.dont_write_bytecode = old_bytecode
        for name, previous_module in previous.items():
            if previous_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous_module
    if Path(modules["gold"].ROOT).resolve() != root:
        raise ValueError("gold cache root must remain the immutable v2 root")
    return types.SimpleNamespace(**modules)


def route_order(protocol):
    order = tuple(protocol["execution"].get(
        "route_order", [route for route in ROUTES if route in protocol["models"]]))
    if not order or len(order) != len(set(order)) or any(route not in protocol["models"] for route in order):
        raise ValueError("route_order must name distinct frozen scheduled models")
    for route in order:
        worker_key(route, "A")
    return order


def validate_plan(protocol, plan):
    cases = {}
    for case in plan["cases"]:
        case_id = case["case_id"]
        if not isinstance(case_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", case_id) or case_id in {".", ".."} or case_id in cases:
            raise ValueError("duplicate or unsafe plan case ID")
        if case["stage"] not in ("dev", "cross"):
            raise ValueError("unknown case stage")
        if type(case["key_count"]) is not int or case["key_count"] < 0:
            raise ValueError("case key_count must be a nonnegative integer")
        if type(case["scheduling_priority"]) is not int:
            raise ValueError("case scheduling_priority must be an integer")
        cases[case_id] = case
    workers = {}
    for worker in plan["workers"]:
        key = worker_key(worker["route"], worker["arm"])
        if key in workers or worker["route"] not in protocol["models"]:
            raise ValueError("duplicate or unfrozen plan worker")
        ids = worker["case_ids"]
        if not isinstance(ids, list) or len(ids) != len(set(ids)) or any(cid not in cases for cid in ids):
            raise ValueError("worker has duplicate or unknown cases")
        # Every worker is the full frozen cohort; priority is not cohort selection.
        if set(ids) != set(cases):
            raise ValueError("worker plan must retain every case denominator")
        workers[key] = worker
    expected = {worker_key(route, arm) for route in route_order(protocol) for arm in ("A", "B")}
    if set(workers) != expected:
        raise ValueError("plan must initialize both arms for every native model")
    for worker in workers.values():
        order = [(cohort(cases[cid]), cases[cid]["scheduling_priority"]) for cid in worker["case_ids"]]
        if order != sorted(order):
            raise ValueError("worker case_ids must be ordered cross, keyed DEV, unkeyed DEV, then priority")
    return cases, workers


def cohort(case):
    return 0 if case["stage"] == "cross" else (1 if case["key_count"] else 2)


def initial_states(protocol, plan, freeze_sha256, plan_sha256, *, now=None):
    cases, workers = validate_plan(protocol, plan)
    at = utc(time.time() if now is None else now)
    states = {}
    for key, worker in workers.items():
        first = cases[worker["case_ids"][0]] if worker["case_ids"] else None
        states[key] = {
            "schema": "oq6.scheduler-worker/v1", "route": worker["route"], "arm": worker["arm"],
            "freeze_sha256": freeze_sha256, "plan_sha256": plan_sha256, "next_index": 0,
            "next_case_id": first["case_id"] if first else None,
            "next_priority": first["scheduling_priority"] if first else None,
            "status": "pending" if first else "finished", "blocked_until": None,
            "blocked_route": None, "quota_admission_path": None, "wait_basis": None, "updated_at": at,
        }
    return states


class QuotaPending(Exception):
    """Unsent work, not a model outcome and never an empty model response."""

    def __init__(self, until, route, *, status="quota_pending", admission_path=None, basis="provider_reset"):
        super().__init__(status + ": " + route)
        self.until, self.route, self.status = until, route, status
        self.admission_path, self.basis = admission_path, basis


class CutoffReached(Exception):
    pass


class RouteUnavailable(Exception):
    def __init__(self, route, reason):
        super().__init__(reason)
        self.route, self.reason = route, reason


class Worker:
    def __init__(self, protocol, plan, route, arm, freeze_sha256, plan_sha256, *,
                 verifier, clock=time.time, sleeper=time.sleep, science_loader=load_science,
                 dispatcher=None):
        self.protocol, self.plan = protocol, plan
        self.cases, self.workers = validate_plan(protocol, plan)
        self.route, self.arm, self.key = route, arm, worker_key(route, arm)
        if self.key not in self.workers:
            raise ValueError("worker is not in the frozen plan")
        self.freeze_sha256, self.plan_sha256 = freeze_sha256, plan_sha256
        self.execution = protocol["execution"]
        self.root = Path(self.execution["raw_root"]).resolve()
        if self.root.is_relative_to(Path(self.execution["science_root"]).resolve()):
            raise ValueError("v3 output cannot be the immutable science root")
        self.cutoff = timestamp(self.execution["cutoff_at"])
        self.route_order = route_order(protocol)
        if self.execution["quota_recheck_seconds"] != 60 or self.execution["priority_wait_seconds"] != 5:
            raise ValueError("frozen scheduling intervals must remain 60s/5s")
        self.clock, self.sleeper, self.verifier = clock, sleeper, verifier
        self.science_loader, self.dispatcher, self._science = science_loader, dispatcher, None
        self.state_path = self.root / "pending" / (self.key + ".json")
        self.current = None
        self._states()  # Missing not-yet-started workers may never disappear from priority.

    @classmethod
    def from_protocol(cls, path, route, arm, **injected):
        path = Path(path).resolve()
        protocol, freeze_sha256 = verify_protocol(path, deep=True)
        plan_path = Path(protocol["execution"]["plan_path"])
        plan_raw = plan_path.read_bytes()
        plan_sha256 = sha(plan_raw)
        expected = protocol["execution"].get("plan_sha256")
        if expected is not None and expected != plan_sha256:
            raise ValueError("frozen scheduling plan drifted")

        def verify():
            verify_protocol(path, expected_sha256=freeze_sha256)
            if sha(plan_path.read_bytes()) != plan_sha256:
                raise ValueError("scheduling plan changed while waiting")

        return cls(protocol, json.loads(plan_raw), route, arm, freeze_sha256, plan_sha256,
                   verifier=verify, **injected)

    def science(self):
        if self._science is None:
            self._science = self.science_loader(self.protocol)
            self._science.run_slice.ROOT = self.root
            self._science.run_slice.quota_call = self.quota_call
        return self._science

    def _require_key_cache(self, case):
        cache = Path(self.execution["science_root"]).resolve() / "scorer-only-keys"
        for field, suffix in (("keys", ".json"), ("keys_provenance", ".provenance.json")):
            path = Path(case[field + "_path"]).resolve()
            expected_path = cache / (case["case_id"] + suffix)
            if path != expected_path or not path.is_file():
                raise ValueError("immutable v2 scorer key cache is unavailable: " + field)
            if sha(path.read_bytes()) != case[field + "_sha256"]:
                raise ValueError("frozen scorer key cache custody drift: " + field)

    def _states(self):
        states = {}
        for key, worker in self.workers.items():
            path = self.root / "pending" / (key + ".json")
            if not path.is_file():
                raise ValueError("all workers must be preinitialized: " + key)
            state = read_json(path)
            if (state["schema"], state["route"], state["arm"], state["freeze_sha256"], state["plan_sha256"]) != (
                "oq6.scheduler-worker/v1", worker["route"], worker["arm"], self.freeze_sha256, self.plan_sha256
            ):
                raise ValueError("worker state identity/freeze drift: " + key)
            index = state["next_index"]
            ids = worker["case_ids"]
            if type(index) is not int or not 0 <= index <= len(ids):
                raise ValueError("worker cursor outside frozen plan")
            case = self.cases[ids[index]] if index < len(ids) else None
            if (state["next_case_id"], state["next_priority"]) != (
                case["case_id"] if case else None, case["scheduling_priority"] if case else None
            ):
                raise ValueError("worker cursor metadata drift")
            if state["status"] not in ACTIVE | {"finished"} or (state["status"] == "finished") != (case is None):
                raise ValueError("worker status/cursor drift")
            states[key] = state
        return states

    def _save(self, state):
        state["updated_at"] = utc(self.clock())
        write_json(self.state_path, state)

    def _event(self, kind, **fields):
        value = {"schema": "oq6.scheduler-event/v1", "kind": kind, "at": utc(self.clock()),
                 "route": self.route, "arm": self.arm, "freeze_sha256": self.freeze_sha256,
                 "case_id": self.current["case_id"] if self.current else None, **fields}
        path = self.root / "events" / self.key / (uuid.uuid4().hex + ".json")
        write_json(path, value, immutable=True)

    def _cell_directory(self, case):
        return self.root / "calls" / self.route.replace("/", "--") / self.arm / case["stage"] / case["case_id"]

    def _outcome_path(self, case):
        return self.root / "outcomes" / self.key / (case["case_id"] + ".json")

    def _route_reason(self, route):
        reason = self.execution["disabled_routes"].get(route)
        if reason is not None:
            return reason
        path = self.root / "route-stops" / (route.replace("/", "--") + ".json")
        return read_json(path)["reason"] if path.exists() else None

    def _priority(self, state):
        case = self.cases[state["next_case_id"]]
        rank = self.route_order.index(state["route"])
        tier = rank * 2 + cohort(case) if cohort(case) < 2 else 2 * len(self.route_order) + rank
        return tier, case["scheduling_priority"], state["next_index"], case["case_id"], state["arm"], state["route"]

    def _priority_boundary(self, requested_route):
        now = self.clock()
        with file_lock(self.root / "locks" / "scheduler.lock"):
            states = self._states()
            own = states[self.key]
            active = [state for state in states.values() if state["status"] != "finished"
                      and self._route_reason(state["route"]) is None]
            blocked_routes = {}
            for state in active:
                if (state["status"] == "quota_pending" and state["blocked_until"] is not None
                        and timestamp(state["blocked_until"]) > now):
                    existing = blocked_routes.get(state["blocked_route"])
                    if existing is None or timestamp(state["blocked_until"]) > timestamp(existing["blocked_until"]):
                        blocked_routes[state["blocked_route"]] = state
            if requested_route in blocked_routes:
                blocked = blocked_routes[requested_route]
                raise QuotaPending(timestamp(blocked["blocked_until"]), requested_route,
                                   admission_path=blocked["quota_admission_path"], basis=blocked["wait_basis"])
            if cohort(self.current) == 2:
                # Even a quota-blocked keyed queue prevents unkeyed consumption.
                for state in active:
                    remaining = self.workers[worker_key(state["route"], state["arm"])]["case_ids"][state["next_index"]:]
                    if any(cohort(self.cases[cid]) < 2 for cid in remaining):
                        raise QuotaPending(min(now + 5, self.cutoff), requested_route,
                                           status="priority_wait", basis="keyed_cross_barrier")
            for state in active:
                if worker_key(state["route"], state["arm"]) == self.key:
                    continue
                blocked = (state["route"] in blocked_routes or
                           state["status"] == "quota_pending" and state["blocked_until"] is not None
                           and timestamp(state["blocked_until"]) > now)
                if not blocked and self._priority(state) < self._priority(own):
                    raise QuotaPending(min(now + 5, self.cutoff), requested_route,
                                       status="priority_wait", basis="higher_priority_worker")

    def _reset(self, admission):
        now = self.clock()
        blocked_pools = [pool for pool in admission.get("pool_values", [])
                         if pool.get("status") == "exhausted" or pool.get("used_fraction", 0) >= 0.9 - 1e-12]
        resets = [pool.get("resets_at_ms") for pool in blocked_pools]
        if resets and all(type(reset) in (int, float) and math.isfinite(reset) and reset / 1000 > now for reset in resets):
            return max(resets) / 1000, "provider_reset"
        return now + 60, "quota_recheck"

    def _consumed(self, directory):
        artifacts = getattr(self.science().transport, "OWNED_ARTIFACTS", ("call-consumed.json",))
        return (directory / "receipt.json").exists() or any((directory / name).exists() for name in artifacts)

    def _quarantine(self, directory, route, receipt):
        if receipt["status"] == "quarantined_identity" or any(
                receipt.get(key) for key in ("tool_events", "memory_events", "retry_events", "context_violations")):
            path = self.root / "route-stops" / (route.replace("/", "--") + ".json")
            # A concurrent cached quarantine may already have stopped this route.
            with suppress(FileExistsError):
                write_json(path, {"at": utc(self.clock()), "route": route,
                                  "reason": "native_identity_or_isolation_quarantine",
                                  "receipt_path": str(directory / "receipt.json")}, immutable=True)

    def quota_call(self, directory, *, route, **kwargs):
        directory = Path(directory)
        if route not in self.protocol["models"]:
            raise ValueError("request route is not in the frozen model census")
        science = self.science()
        # Preserve the original timeout as part of the exact immutable request.
        if self._consumed(directory):
            for name in ("receipt.json", "call-consumed.json"):
                path = directory / name
                if path.exists():
                    try:
                        previous = read_json(path)
                    except (ValueError, OSError):
                        continue  # Native transport owns corrupt/unfinished receipt classification.
                    if type(previous.get("timeout_seconds")) is int:
                        kwargs["timeout_seconds"] = previous["timeout_seconds"]
                        break
            receipt = science.transport.call(directory, route=route, **kwargs)
            self._quarantine(directory, route, receipt)
            self._event("cached_receipt", requested_route=route, receipt_path=str(directory / "receipt.json"))
            return receipt
        if self.clock() >= self.cutoff:
            raise CutoffReached()
        reason = self._route_reason(route)
        if reason is not None:
            raise RouteUnavailable(route, reason)
        self._priority_boundary(route)
        self.verifier()
        provider_path = self.root / "locks" / ("provider-" + route.split("/", 1)[0] + ".lock")
        try:
            with file_lock(provider_path, nonblocking=True):
                if self.clock() >= self.cutoff:
                    raise CutoffReached()
                reason = self._route_reason(route)
                if reason is not None:
                    raise RouteUnavailable(route, reason)
                self._priority_boundary(route)
                admission = science.quota.admit_route(
                    route, output_directory=self.root / "quota-admissions" / self.key / self.current["case_id"] / directory.name)
                self._event("quota_admission", requested_route=route, eligible=admission["eligible"],
                            quota_admission_path=admission["receipt_path"])
                if not admission["eligible"]:
                    until, basis = self._reset(admission)
                    raise QuotaPending(until, route, admission_path=admission["receipt_path"], basis=basis)
                # Neither elapsed time nor a reset timestamp admits a request.
                self._priority_boundary(route)
                self.verifier()
                timeout = min(kwargs.get("timeout_seconds", 600), math.floor(self.cutoff - self.clock()))
                if timeout < 1:
                    raise CutoffReached()
                kwargs["timeout_seconds"] = timeout
                self._event("dispatch", requested_route=route, timeout_seconds=timeout)
                # Filesystem event publication may wait: recheck the frozen runtime too.
                self.verifier()
                timeout = min(timeout, math.floor(self.cutoff - self.clock()))
                if timeout < 1:
                    raise CutoffReached()
                kwargs["timeout_seconds"] = timeout
                receipt = science.transport.call(directory, route=route, **kwargs)
                self._quarantine(directory, route, receipt)
                return receipt
        except LockBusy:
            raise QuotaPending(min(self.clock() + 5, self.cutoff), route,
                               status="provider_wait", basis="provider_lock") from None

    def _missing(self, case, reason, *, blocked_route=None):
        directory = self._cell_directory(case)
        audit_path, prediction_path = directory / "audit" / "receipt.json", directory / "prediction.json"
        audit = read_json(audit_path) if audit_path.exists() else None
        prediction = read_json(prediction_path) if prediction_path.exists() else None
        if prediction is not None and (
                prediction["audit_receipt_sha256"] != (sha(audit_path.read_bytes()) if audit_path.exists() else None) or
                prediction["source_sha256"] != case.get("source_sha256")):
            raise ValueError("frozen prediction custody drift")
        paired = self.science().paired.evaluate_cell(
            case, prediction.get("parsed_payloads") if prediction else None, None, None,
            audit_status=audit["status"] if audit else (prediction.get("audit_status", "unavailable") if prediction else "unavailable"),
            raw_findings_count=prediction.get("raw_findings_count") if prediction else None)
        if paired["complete"]:
            # Locally evaluable data is retained, but a cell not finalized by the
            # boundary cannot silently become a completed prospective outcome.
            paired = self.science().paired.evaluate_cell(
                case, None, None, None, audit_status="unfinalized_at_boundary",
                raw_findings_count=prediction.get("raw_findings_count") if prediction else None)
        return {"status": "missing", "reason": reason, "blocked_route": blocked_route,
                "cell_path": None, "paired": paired,
                "audit": audit, "prediction": prediction,
                "audit_receipt_path": str(audit_path) if audit else None,
                "prediction_path": str(prediction_path) if prediction else None}

    def _finish(self, state, case, outcome):
        path = self._outcome_path(case)
        if not path.exists():
            write_json(path, {"schema": "oq6.scheduler-outcome/v1", "route": self.route,
                             "arm": self.arm, "case_id": case["case_id"], "stage": case["stage"],
                             "freeze_sha256": self.freeze_sha256, "plan_sha256": self.plan_sha256,
                             "key_count": case["key_count"], "at": utc(self.clock()),
                             "paid_spend_usd": 0, **outcome}, immutable=True)
        stored = read_json(path)
        if (stored["route"], stored["arm"], stored["case_id"], stored["freeze_sha256"], stored["plan_sha256"]) != (
                self.route, self.arm, case["case_id"], self.freeze_sha256, self.plan_sha256):
            raise ValueError("immutable scheduler outcome identity drift")
        state["next_index"] += 1
        ids = self.workers[self.key]["case_ids"]
        following = self.cases[ids[state["next_index"]]] if state["next_index"] < len(ids) else None
        state.update(next_case_id=following["case_id"] if following else None,
                     next_priority=following["scheduling_priority"] if following else None,
                     status="pending" if following else "finished", blocked_until=None,
                     blocked_route=None, quota_admission_path=None, wait_basis=None)
        self._save(state)
        return state

    def step(self):
        """Advance one cell or persist an unsent boundary; never sleep here."""
        with file_lock(self.root / "locks" / ("worker-" + self.key + ".lock"), nonblocking=True):
            state = self._states()[self.key]
            if state["status"] == "finished":
                return state
            case = self.current = self.cases[state["next_case_id"]]
            if self._outcome_path(case).exists():
                return self._finish(state, case, read_json(self._outcome_path(case)))
            final = self._cell_directory(case) / "cell.json"
            if not final.exists():
                if self.clock() >= self.cutoff:
                    return self._finish(state, case, self._missing(case, "cutoff_unsent_or_pending", blocked_route=state["blocked_route"]))
                reason = self._route_reason(self.route)
                if reason is not None:
                    return self._finish(state, case, self._missing(case, "route_disabled: " + reason, blocked_route=self.route))
                for phase in ("audit", "scorer"):
                    attempt = self._cell_directory(case) / phase
                    if (attempt / "call-consumed.json").exists() and not (attempt / "receipt.json").exists():
                        return self._finish(state, case, self._missing(case, "consumed_unfinished_no_retry"))
                if state["blocked_until"] is not None and timestamp(state["blocked_until"]) > self.clock():
                    return state
            state.update(status="running", blocked_until=None, blocked_route=None,
                         quota_admission_path=None, wait_basis=None)
            self._save(state)
            try:
                self.verifier()
                if self.dispatcher is None:
                    science = self.science()
                    # Refuse both reconstruction and jointly mutated key/provenance caches.
                    self._require_key_cache(case)
                    result = science.run_slice.execute_cell(case, self.route, self.arm, self.protocol, self.freeze_sha256)
                else:
                    result = self.dispatcher(case, self.route, self.arm, self.protocol, self.freeze_sha256,
                                             quota_call=self.quota_call)
            except QuotaPending as pending:
                state.update(status=pending.status, blocked_until=utc(pending.until),
                             blocked_route=pending.route, quota_admission_path=pending.admission_path,
                             wait_basis=pending.basis)
                self._event("pending", blocked_until=state["blocked_until"], blocked_route=pending.route,
                            wait_basis=pending.basis, quota_admission_path=pending.admission_path)
                self._save(state)
                return state
            except CutoffReached:
                return self._finish(state, case, self._missing(case, "cutoff_unsent_or_pending"))
            except RouteUnavailable as unavailable:
                return self._finish(state, case, self._missing(case, "route_disabled: " + unavailable.reason,
                                                              blocked_route=unavailable.route))
            except FileNotFoundError as error:
                # Source disappearance is missing input, not a model attempt.
                source_paths = {Path(case.get("source_path", "")), Path(case.get("package", ""))}
                source_paths.update(Path(case.get("package", "")) / row["path"] for row in case.get("files", []))
                if error.filename is None or not any(path.is_relative_to(Path(error.filename)) for path in source_paths):
                    raise
                return self._finish(state, case, self._missing(case, "unavailable_source"))
            return self._finish(state, case, {
                "status": "complete" if result["paired"]["complete"] else "missing",
                "reason": result["paired"].get("missing_reason"), "paired": result["paired"],
                "cell_path": str(final), "audit": result["audit"],
                "prediction": result.get("prediction"), "blocked_route": None})

    def snapshot(self):
        state = self._states()[self.key]
        outcomes = []
        attempted_cells = answered_cells = attempted_calls = answered_calls = 0
        for case_id in self.workers[self.key]["case_ids"]:
            case = self.cases[case_id]
            path = self._outcome_path(case)
            if path.exists():
                outcomes.append(read_json(path))
            audit_attempted = audit_answered = False
            for phase in ("audit", "scorer"):
                directory = self._cell_directory(case) / phase
                receipt = read_json(directory / "receipt.json") if (directory / "receipt.json").exists() else {}
                consumed = (directory / "call-consumed.json").exists() or receipt.get("attempt_consumed") is True
                answered = receipt.get("status") == "answered"
                attempted_calls += int(consumed)
                answered_calls += int(answered)
                if phase == "audit":
                    audit_attempted, audit_answered = consumed, answered
            attempted_cells += int(audit_attempted)
            answered_cells += int(audit_answered)
        complete = sum(row["status"] == "complete" for row in outcomes)
        planned = len(self.workers[self.key]["case_ids"])
        pending = planned - len(outcomes)
        missing = len(outcomes) - complete
        status = "complete" if complete == planned else ("blocked" if not attempted_cells else "checkpoint_partial")
        return {"schema": "oq6.scheduler-slice/v1", "route": self.route, "arm": self.arm,
                "freeze_sha256": self.freeze_sha256, "plan_sha256": self.plan_sha256,
                "status": status, "worker_status": state["status"], "at": utc(self.clock()),
                "planned_cells": planned, "finished_cells": len(outcomes), "complete_paired_cells": complete,
                "attempted_cells": attempted_cells, "answered_cells": answered_cells,
                "pending_cells": pending, "missing_cells": missing,
                "attempted_calls": attempted_calls, "answered_calls": answered_calls,
                "outcome_paths": [str(self._outcome_path(self.cases[row["case_id"]])) for row in outcomes],
                "paid_spend_usd": 0, "subscription_usd": None}

    def run(self):
        while True:
            state = self.step()
            snapshot = self.snapshot()
            write_json(self.root / "slices" / (self.key + ".json"), snapshot)
            if state["status"] == "finished":
                return snapshot
            if state["blocked_until"] is not None:
                wake = min(timestamp(state["blocked_until"]), self.cutoff)
                delay = wake - self.clock()
                if delay > 0:
                    self.sleeper(delay)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--route", required=True)
    parser.add_argument("--arm", choices=("A", "B"), required=True)
    args = parser.parse_args(argv)
    result = Worker.from_protocol(args.protocol, args.route, args.arm).run()
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
