"""Owner-only execution policy for Task QA's native grading tools.

The owner (never the model) picks the backend, approval, spend cap, shared
ledger, app/billing names, platform and any PM-port mapping. EnvTools only
supplies pinned task/candidate paths and a time budget. Grading is the real
native machinery: ``evallab.upstream_grader.run_upstream_grader`` for the
unchanged upstream verifier and ``evallab.candidate_runner.run_candidate`` for
the pinned PM port. Nothing here prices, schedules, emulates or collects
anything: the runner's admission, exact resources, image cache and cost
reconciliation are used as-is, and its result dictionary is returned raw with
one added ``execution_policy`` key.

Policy file (strict JSON, no credentials, unknown/duplicate keys refused)::

    {
      "schema": "evallab.qa.execution-policy/v1",
      "backend": "docker" | "modal",
      "max_usd": 0                      # docker: exactly 0; modal: positive, finite
      "approval": "recorded text",      # modal only, required
      "spend_ledger": "path.json",      # modal only, required; one ledger per campaign
      "modal_app_name": "tag-suffix",   # modal only, required (native: equals tag or tag-prefixed)
      "billing_tag": "tag",             # modal only, required
      "platform": "linux/amd64",        # optional owner platform (modal: amd64 only)
      "port": {                         # optional; enables grader="port"
        "source_pin": "<40 hex>",       # must equal the runner's fixed PM scorer pin
        "tasks": {"<task id>": {"path": "<port dir named <task id>>", "tree_digest": "sha256:<hex>"}}
      }
    }

Relative paths resolve against ``repo_root``. The file's bytes, the resolved
file/ledger/port paths and every port tree are pinned at load and revalidated
before each call. At most one native grading call per campaign is active:
calls serialize on a lock beside the ledger (host lock directory for Docker);
waiting counts against the call's timeout. The lock is distinct from the
runner's own ledger lock.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.docker_lock import default_state_dir

POLICY_SCHEMA = "evallab.qa.execution-policy/v1"
RESULT_SCHEMA = "evallab.qa.execution/v1"
_MAX_POLICY_BYTES = 64 * 1024
_PLATFORMS = ("linux/amd64", "linux/arm64")
_TAG = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_PIN = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_POLL_SECONDS = 0.1
_TOP_KEYS = frozenset(
    {"schema", "backend", "max_usd", "approval", "spend_ledger", "modal_app_name",
     "billing_tag", "platform", "port"}
)
_MODAL_ONLY = ("approval", "spend_ledger", "modal_app_name", "billing_tag")


class ExecutionPolicyError(ValueError):
    """Invalid, drifted or unavailable owner policy; nothing was executed."""


@dataclass(frozen=True)
class Runtime:
    """The four native bindings, resolved lazily so planning/policy loading import nothing heavy."""

    run_upstream: Callable[..., dict[str, Any]]
    run_candidate: Callable[..., dict[str, Any]]
    tree_digest: Callable[[Path], str]
    port_pin: Callable[[], str]


def _native_runtime() -> Runtime:
    def run_upstream(*args: Any, **kwargs: Any) -> dict[str, Any]:
        from evallab.upstream_grader import run_upstream_grader

        return run_upstream_grader(*args, **kwargs)

    def run_candidate(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            from evallab.candidate_runner import run_candidate as native
        except ModuleNotFoundError as exc:
            if exc.name != "evallab.candidate_runner":
                raise
            raise ExecutionPolicyError(
                "PM-port grading needs evallab.candidate_runner.run_candidate, which is not "
                "importable in this checkout (Execution lane's candidate API is not merged here)"
            ) from exc
        return native(*args, **kwargs)

    def tree_digest(path: Path) -> str:
        from evallab.upstream_grader import _tree_digest

        return _tree_digest(path)

    def port_pin() -> str:
        try:
            from evallab.candidate_port import PM_FIXED_PIN
        except ModuleNotFoundError as exc:
            if exc.name != "evallab.candidate_port":
                raise
            raise ExecutionPolicyError(
                "PM-port grading needs evallab.candidate_port.PM_FIXED_PIN, which is not "
                "importable in this checkout (Execution lane's candidate API is not merged here)"
            ) from exc
        return PM_FIXED_PIN

    return Runtime(run_upstream, run_candidate, tree_digest, port_pin)


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _strict_json(data: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _ in items]
        if len(keys) != len(set(keys)):
            raise ExecutionPolicyError("policy JSON has a duplicate key")
        return dict(items)

    def constant(name: str) -> Any:
        raise ExecutionPolicyError(f"policy JSON has a non-finite number: {name}")

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExecutionPolicyError(f"policy is not valid UTF-8 JSON: {exc}") from exc


def _text(value: Any, label: str, *, pattern: re.Pattern[str] | None = None) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > 512
        or any(ord(char) < 32 for char in value)
        or (pattern is not None and not pattern.fullmatch(value))
    ):
        raise ExecutionPolicyError(f"policy {label} is missing or malformed")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ExecutionPolicyError(f"policy {label} must be a finite JSON number")
    return float(value)


def _read_regular(path: Path, *, limit: int) -> bytes:
    """Read a regular, non-symlink file; any drift is an error, never a fallback."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise ExecutionPolicyError(f"policy file is unavailable or a symlink: {path}: {exc}") from exc
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ExecutionPolicyError(f"policy file is not a regular file: {path}")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ExecutionPolicyError(f"policy file exceeds {limit} bytes: {path}")
    return data


@dataclass(frozen=True)
class _Bound:
    """A declared path plus the exact real location it had when pinned."""

    declared: Path
    resolved: Path

    @classmethod
    def pin(cls, raw: Any, base: Path, label: str) -> _Bound:
        text = _text(raw, label)
        if "\x00" in text:
            raise ExecutionPolicyError(f"policy {label} contains NUL")
        declared = Path(os.path.abspath(base / text))
        bound = cls(declared, Path(os.path.realpath(declared)))
        bound.check(label)
        return bound

    def check(self, label: str) -> None:
        try:
            if stat.S_ISLNK(os.lstat(self.declared).st_mode):
                raise ExecutionPolicyError(f"{label} is now a symlink: {self.declared}")
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ExecutionPolicyError(f"{label} is unavailable: {exc}") from exc
        if Path(os.path.realpath(self.declared)) != self.resolved:
            raise ExecutionPolicyError(f"{label} moved since the policy was pinned: {self.declared}")


@dataclass(frozen=True)
class _Port:
    bound: _Bound
    tree_digest: str


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


class ExecutionPolicy:
    """Immutable owner policy. Construct with ``from_file`` or ``local``."""

    def __init__(
        self,
        *,
        digest: str,
        backend: str,
        max_usd: float,
        approval: str | None,
        ledger: _Bound | None,
        app: str | None,
        tag: str | None,
        platform: str | None,
        port_pin: str | None,
        ports: Mapping[str, _Port],
        source: _Bound | None,
        runtime: Runtime,
    ) -> None:
        self._digest = digest
        self._backend = backend
        self._max_usd = max_usd
        self._approval = approval
        self._ledger = ledger
        self._app = app
        self._tag = tag
        self._platform = platform
        self._port_pin = port_pin
        self._ports = dict(ports)
        self._source = source
        self._runtime = runtime

    # -- construction -------------------------------------------------

    @classmethod
    def local(cls, *, repo_root: Path, runtime: Runtime | None = None) -> ExecutionPolicy:
        """Legacy free Docker control path. Not a metered fallback: cap 0, no PM port."""
        _repo(repo_root)
        document = {"schema": POLICY_SCHEMA, "backend": "docker", "max_usd": 0}
        return cls(
            digest=_sha(_canonical(document)), backend="docker", max_usd=0.0, approval=None,
            ledger=None, app=None, tag=None, platform=None, port_pin=None, ports={},
            source=None, runtime=runtime or _native_runtime(),
        )

    @classmethod
    def from_file(
        cls, path: Path, *, repo_root: Path, runtime: Runtime | None = None
    ) -> ExecutionPolicy:
        base = _repo(repo_root)
        declared = Path(os.path.abspath(path))
        source = _Bound(declared, Path(os.path.realpath(declared)))
        data = _read_regular(declared, limit=_MAX_POLICY_BYTES)
        raw = _strict_json(data)
        if not isinstance(raw, dict):
            raise ExecutionPolicyError("policy must be a JSON object")
        if raw.get("schema") != POLICY_SCHEMA:
            raise ExecutionPolicyError(f"policy schema must be {POLICY_SCHEMA}")
        unknown = sorted(set(raw) - _TOP_KEYS)
        if unknown:
            raise ExecutionPolicyError(f"policy has unknown keys: {unknown}")
        backend = raw.get("backend")
        if backend not in {"docker", "modal"}:
            raise ExecutionPolicyError("policy backend must be 'docker' or 'modal' (no auto fallback)")
        cap = _number(raw.get("max_usd"), "max_usd")
        platform = raw.get("platform")
        if platform is not None and platform not in _PLATFORMS:
            raise ExecutionPolicyError(f"policy platform must be one of {_PLATFORMS}")
        approval = ledger = app = tag = None
        if backend == "docker":
            stray = [key for key in _MODAL_ONLY if key in raw]
            if stray or cap != 0:
                raise ExecutionPolicyError(
                    f"docker policy is cap 0 and carries no Modal settings (found {stray}, max_usd={cap})"
                )
        else:
            missing = [key for key in _MODAL_ONLY if raw.get(key) is None]
            if missing:
                raise ExecutionPolicyError(f"modal policy requires {missing}")
            if cap <= 0:
                raise ExecutionPolicyError("modal policy requires a positive finite max_usd")
            if platform not in {None, "linux/amd64"}:
                raise ExecutionPolicyError("modal policy platform must be linux/amd64")
            approval = _text(raw["approval"], "approval")
            tag = _text(raw["billing_tag"], "billing_tag", pattern=_TAG)
            app = _text(raw["modal_app_name"], "modal_app_name", pattern=_TAG)
            if not (app == tag or app.startswith(tag + "-")):
                raise ExecutionPolicyError("modal_app_name must equal billing_tag or extend it with '-'")
            ledger = _Bound.pin(raw["spend_ledger"], base, "spend_ledger")
            if not ledger.resolved.parent.is_dir() or ledger.resolved.is_dir():
                raise ExecutionPolicyError("spend_ledger must be a file path in an existing directory")
            if _overlaps(ledger.resolved, source.resolved):
                raise ExecutionPolicyError("spend_ledger must not be the policy file")
        pin, ports = _port_section(raw.get("port"), base)
        return cls(
            digest=_sha(data), backend=backend, max_usd=cap, approval=approval, ledger=ledger,
            app=app, tag=tag, platform=platform, port_pin=pin, ports=ports, source=source,
            runtime=runtime or _native_runtime(),
        )

    # -- read-only surface -------------------------------------------

    @property
    def digest(self) -> str:
        return self._digest

    def public_config(self) -> dict[str, Any]:
        """Safe summary for the model: no approval text, no paths, no credentials."""
        return {
            "schema": POLICY_SCHEMA,
            "digest": self._digest,
            "backend": self._backend,
            "max_usd": self._max_usd,
            "metered": self._backend == "modal",
            "approval_recorded": self._approval is not None,
            "platform": self._platform,
            "spend_ledger": self._ledger.declared.name if self._ledger else None,
            "billing_tag": self._tag,
            "modal_app_name": self._app,
            "graders": {
                "upstream": True,
                "port": {"configured_tasks": len(self._ports), "source_pin": self._port_pin}
                if self._ports else None,
            },
        }

    # -- execution ----------------------------------------------------

    def run(
        self,
        task_dir: Path,
        solution_dir: Path | None = None,
        *,
        grader: str = "upstream",
        oracle: bool = False,
        blank: bool = False,
        timeout_seconds: float,
        output_dir: Path,
        plan: bool = False,
        platform: str | None = None,
    ) -> dict[str, Any]:
        """One native grade, or the runner's plan. Policy/admission trouble is a returned
        ``refused`` result (``executed`` False, ``reward`` None), never a raise or a reward 0.
        Caller misuse (selectors, types, non-positive timeout) raises ``ValueError``.
        """
        if grader not in {"upstream", "port"}:
            raise ValueError("grader must be 'upstream' or 'port'")
        if not isinstance(oracle, bool) or not isinstance(blank, bool) or type(plan) is not bool:
            raise ValueError("oracle, blank and plan must be booleans")
        if sum((solution_dir is not None, oracle, blank)) != 1:
            raise ValueError("select exactly one of solution_dir, oracle or blank")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number")
        if platform is not None and platform not in _PLATFORMS:
            raise ValueError(f"platform must be one of {_PLATFORMS}")
        started = time.monotonic()
        task = Path(os.path.abspath(task_dir))
        output = Path(os.path.realpath(os.path.abspath(output_dir)))
        control = "oracle" if oracle else "blank" if blank else "solution"
        context = {"grader": grader, "control": control, "plan": plan}
        try:
            self._revalidate(output)
            effective_platform = self._effective_platform(platform)
            port = self._port_for(task) if grader == "port" else None
            if plan:
                return self._invoke(
                    task, solution_dir, context, output, effective_platform, port,
                    max(1, math.floor(timeout_seconds)), waited=0.0,
                )
            with self._admission(started + timeout_seconds) as waited:
                remaining = math.floor(timeout_seconds - (time.monotonic() - started))
                if remaining < 1:
                    raise ExecutionPolicyError(
                        f"less than one second of the {timeout_seconds}s budget remained after "
                        f"{waited:.2f}s waiting for the campaign grading lock"
                    )
                return self._invoke(
                    task, solution_dir, context, output, effective_platform, port, remaining,
                    waited=waited,
                )
        except ExecutionPolicyError as exc:
            return self._unrun("refused", str(exc), context, executed=False)

    def _invoke(
        self,
        task: Path,
        solution_dir: Path | None,
        context: dict[str, Any],
        output: Path,
        platform: str | None,
        port: _Port | None,
        native_timeout: int,
        *,
        waited: float,
    ) -> dict[str, Any]:
        metered = self._backend == "modal"
        common: dict[str, Any] = {
            "backend": self._backend, "platform": platform, "timeout_seconds": native_timeout,
            "max_usd": self._max_usd if metered else None,
            "approval": self._approval,
            "spend_ledger": str(self._ledger.declared) if self._ledger else None,
            "modal_app_name": self._app, "billing_tag": self._tag,
            "output_dir": str(output), "plan": context["plan"],
        }
        control = context["control"]
        try:
            if context["grader"] == "upstream":
                native = self._runtime.run_upstream(
                    task, solution_dir, oracle=control == "oracle", blank=control == "blank",
                    **common,
                )
            else:
                assert port is not None
                if control == "oracle":
                    selected: dict[str, Any] = {"solution_bundle": task / "solution"}
                elif control == "blank":
                    selected = {"files": {}}
                else:
                    selected = {"solution_bundle": solution_dir}
                native = self._runtime.run_candidate(
                    task, grader="port", port_task=port.bound.declared, **selected, **common,
                )
        except ExecutionPolicyError as exc:
            return self._unrun("refused", str(exc), context, executed=False)
        except Exception as exc:
            # Unknown whether anything was allocated: executed stays null, never False.
            return self._unrun("error", f"{type(exc).__name__}: {exc}", context, executed=None)
        if not isinstance(native, Mapping):
            return self._unrun("error", "native runner returned a non-object", context, executed=None)
        result = dict(native)
        summary = self._summary(context)
        summary.update(lock_wait_seconds=round(waited, 3), native_timeout_seconds=native_timeout)
        if context["grader"] == "port" and port is not None:
            summary["port_source_pin"] = self._port_pin
            summary["port_pinned_tree_digest"] = port.tree_digest
        summary["selected_grader"] = _grader_identity(result, context["grader"])
        summary["original_task"] = _original_identity(result)
        result["execution_policy"] = summary
        if not context["plan"] and result.get("status") == "measured":
            problem = self._identity_problem(result, task, context["grader"], port)
            if problem:
                result.update(status="error", reward=None, rewards=None, passed=None)
                result["error"] = "; ".join(filter(None, [result.get("error"), problem]))
        return result

    def _identity_problem(
        self, result: Mapping[str, Any], task: Path, grader: str, port: _Port | None
    ) -> str | None:
        identity = result.get("task")
        if not isinstance(identity, Mapping) or identity.get("path") != os.path.realpath(task):
            return "native result is not for the pinned task directory"
        if identity.get("exact_tree_digest") != self._runtime.tree_digest(task):
            return "native result's original task bytes differ from the pinned source"
        if grader == "port":
            provenance = result.get("grader_provenance")
            port_identity = provenance.get("port_identity") if isinstance(provenance, Mapping) else None
            if (
                result.get("grader") != "port"
                or not isinstance(port_identity, Mapping)
                or port is None
                or port_identity.get("exact_tree_digest") != port.tree_digest
            ):
                return "native result's grader is not the pinned PM port tree"
        return None

    # -- policy checks --------------------------------------------------

    def _revalidate(self, output: Path) -> None:
        if self._source is not None:
            self._source.check("policy file")
            if _sha(_read_regular(self._source.declared, limit=_MAX_POLICY_BYTES)) != self._digest:
                raise ExecutionPolicyError("policy file bytes changed since they were pinned")
        if self._ledger is not None:
            self._ledger.check("spend ledger")
            with contextlib.suppress(FileNotFoundError):
                if not stat.S_ISREG(os.lstat(self._ledger.declared).st_mode):
                    raise ExecutionPolicyError("spend ledger is not a regular file")
        protected = [self._ledger.resolved] if self._ledger else []
        if self._source is not None:
            protected.append(self._source.resolved)
        if any(_overlaps(output, path) for path in protected):
            raise ExecutionPolicyError("output directory overlaps the policy file or spend ledger")

    def _effective_platform(self, requested: str | None) -> str | None:
        if requested is not None and self._platform is not None and requested != self._platform:
            raise ExecutionPolicyError(
                f"actor platform {requested} conflicts with the owner platform {self._platform}"
            )
        return self._platform or requested

    def _port_for(self, task: Path) -> _Port:
        if not self._ports:
            raise ExecutionPolicyError("this policy pins no PM port; grader='port' is unavailable")
        port = self._ports.get(task.name)
        if port is None:
            raise ExecutionPolicyError(f"no PM port is pinned for task id {task.name!r}")
        port.bound.check("PM port directory")
        try:
            fixed = self._runtime.port_pin()
            actual = self._runtime.tree_digest(port.bound.declared)
        except ExecutionPolicyError:
            raise
        except (OSError, ValueError) as exc:
            raise ExecutionPolicyError(f"PM port cannot be verified: {exc}") from exc
        if fixed != self._port_pin:
            raise ExecutionPolicyError(
                f"policy port source_pin {self._port_pin} is not the runner's fixed scorer pin {fixed}"
            )
        if actual != port.tree_digest:
            raise ExecutionPolicyError(
                f"PM port tree for {task.name!r} drifted from its pinned digest ({actual})"
            )
        return port

    @contextlib.contextmanager
    def _admission(self, deadline: float) -> Iterator[float]:
        """Bounded, process-wide serialization of native grading for this campaign."""
        if self._ledger is not None:
            path = self._ledger.declared.with_name(self._ledger.declared.name + ".qa-grade.lock")
        else:
            directory = default_state_dir()
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "qa-env-grade.lock"
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError as exc:
            raise ExecutionPolicyError(f"grading admission lock is unavailable: {exc}") from exc
        began = time.monotonic()
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK}:
                        raise ExecutionPolicyError(f"grading admission lock failed: {exc}") from exc
                left = deadline - time.monotonic()
                if left <= 0:
                    raise ExecutionPolicyError(
                        "another native grading call holds the campaign lock for the whole time budget"
                    )
                time.sleep(min(_POLL_SECONDS, left))
            try:
                yield time.monotonic() - began
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    # -- result shaping -------------------------------------------------

    def _summary(self, context: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "digest": self._digest, "backend": self._backend, "max_usd": self._max_usd,
            "platform": self._platform, "grader": context["grader"],
            "control": context["control"], "plan": context["plan"],
            "control_semantics": "original reference or blank graded by the selected grader; "
            "expected rewards are never supplied",
        }

    def _unrun(
        self, status: str, error: str, context: Mapping[str, Any], *, executed: bool | None
    ) -> dict[str, Any]:
        return {
            "schema": RESULT_SCHEMA, "status": status, "reward": None, "rewards": None,
            "passed": None, "per_test": {"status": "unavailable", "tests": [], "sources": []},
            "logs": {"agent": [], "verifier": []}, "error": error, "native_error": None,
            "executed": executed, "spend": None, "cost": None, "output_dir": None,
            "runnable": False, "refusals": [error], "grader": context["grader"], "task": None,
            "execution_policy": {**self._summary(context), "selected_grader": None,
                                 "original_task": None},
        }


def _repo(repo_root: Path) -> Path:
    base = Path(os.path.realpath(repo_root))
    if not base.is_dir():
        raise ExecutionPolicyError(f"repo_root is not a directory: {repo_root}")
    return base


def _port_section(raw: Any, base: Path) -> tuple[str | None, dict[str, _Port]]:
    if raw is None:
        return None, {}
    if not isinstance(raw, dict) or set(raw) != {"source_pin", "tasks"}:
        raise ExecutionPolicyError("policy port needs exactly source_pin and tasks")
    pin = _text(raw["source_pin"], "port.source_pin", pattern=_PIN)
    tasks = raw["tasks"]
    if not isinstance(tasks, dict) or not tasks:
        raise ExecutionPolicyError("policy port.tasks must be a nonempty object")
    ports: dict[str, _Port] = {}
    for identity, entry in tasks.items():
        _text(identity, "port task id", pattern=_TASK_ID)
        if not isinstance(entry, dict) or set(entry) != {"path", "tree_digest"}:
            raise ExecutionPolicyError(f"port task {identity!r} needs exactly path and tree_digest")
        bound = _Bound.pin(entry["path"], base, f"port path for {identity}")
        if bound.resolved.name != identity or not bound.resolved.is_dir():
            raise ExecutionPolicyError(
                f"port path for {identity!r} must be an existing directory named {identity!r}"
            )
        digest = _text(entry["tree_digest"], f"port tree_digest for {identity}", pattern=_DIGEST)
        ports[identity] = _Port(bound, digest)
    return pin, ports


def _grader_identity(result: Mapping[str, Any], grader: str) -> dict[str, Any] | None:
    """The grader that actually scored, exactly as the runner reported it (None if unreported)."""
    if grader == "upstream":
        task = result.get("task")
        if not isinstance(task, Mapping):
            return None
        return {"kind": "upstream", "exact_tree_digest": task.get("exact_tree_digest"),
                "tests_digest": task.get("tests_digest")}
    provenance = result.get("grader_provenance")
    if not isinstance(provenance, Mapping):
        return None
    port_identity = provenance.get("port_identity")
    return {
        "kind": "port", "grader_digest": provenance.get("grader_digest"),
        "port_identity": dict(port_identity) if isinstance(port_identity, Mapping) else None,
        "prepared_task_digest": provenance.get("prepared_task_digest"),
    }


def _original_identity(result: Mapping[str, Any]) -> dict[str, Any] | None:
    """The original task as the runner identified it; a prepared PM task is never relabeled as it."""
    task = result.get("task")
    return dict(task) if isinstance(task, Mapping) else None
