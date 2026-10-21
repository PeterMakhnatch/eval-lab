"""Run an unchanged Harbor task's native verifier in a fresh declared-resource environment.

The public API and ``--plan`` need no Harbor/provider imports. Actual execution
uses Harbor's Trial lifecycle, QA's script-only agent, and the existing isolated
Docker / setup-aware Modal boundaries. No model agent is constructed.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import contextvars
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from evallab.campaign_execution import docker_available_resources
from evallab.docker_lock import docker_slot
from evallab.registry import task_directory_digest
from evallab.regrade import CloudSpendGate, SpendCapReached, modal_resource_blocker

Backend = Literal["auto", "docker", "modal"]
_PLATFORMS = frozenset({"linux/amd64", "linux/arm64"})
_COMPOSE_NAMES = ("docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml")
_CLEANUP_SECONDS = 90
_MAX_STRUCTURED_BYTES = 25 * 1024 * 1024
# Preparation has no task-sized provider ceiling. Reserve an explicitly labelled
# larger builder allowance and an explicit 25% contingency, not a short-control rate.
_PRICE_BASIS: dict[str, Any] = {
    "version": "upstream-grader-resource-lifetime/v1",
    "cpu_usd_per_core_second": 0.00003942,
    "memory_usd_per_gib_second": 0.00000667,
    "preparation_cpus": 8,
    "preparation_memory_mb": 16384,
    "contingency_multiplier": 1.25,
    "sandbox_create_attempts": 1,
    "rates_source": "https://modal.com/pricing (Sandbox rates, observed 2026-10-20)",
    "preparation_rate_basis": "conservative Sandbox rates also used for the larger builder allowance; not assumed Functions pricing",
    "preparation_lifetime_basis": "full declared build_timeout_sec; provider builder timeout/cancellation is not claimed",
    "finalization_basis": "only measured native success plus affirmative completed image hydration and terminal public SDK wait for every owned sandbox; both role builder and task resources charged for rounded-up whole-lifecycle elapsed",
    "basis": "resource/lifetime reservation estimates, not invoices or a provider dollar kill switch",
    "spend_scope": "Modal infrastructure only; excludes task, solution and verifier external-API charges, which require separate caller authorization",
}


@dataclass(frozen=True)
class ContainerSnapshot:
    """A local tar of ONE directory's contents, not an image or live-container handle.

    Export, for example, with ``docker cp candidate:/app/. - > app.tar``.
    ``target`` is replaced in a fresh original task image; processes, volumes,
    other filesystem paths, image configuration, and credentials are not replayed.
    Only regular files/directories are supported, deliberately excluding links.
    """

    archive: Path | str
    target: str
    platform: str


class GraderRefusal(ValueError):
    """A request cannot be admitted; no task environment has been allocated."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def _tree_digest(root: Path) -> str:
    """Exact input identity, including modes/empty directories; reject escaping links."""
    if not root.is_dir():
        raise GraderRefusal(f"directory is missing: {root}")
    digest = hashlib.sha256()
    digest.update(
        json.dumps(
            [".", stat.S_IMODE(root.stat().st_mode), "directory"], separators=(",", ":")
        ).encode("utf-8")
        + b"\n"
    )
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if path.is_symlink():
            raise GraderRefusal(f"input symlinks are not supported: {path}")
        if path.is_file():
            identity = _file_digest(path)
        elif path.is_dir():
            identity = "directory"
        else:
            raise GraderRefusal(f"input special files are not supported: {path}")
        digest.update(json.dumps([relative, mode, identity], separators=(",", ":")).encode())
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _positive_integer(value: Any, label: str) -> int:
    if type(value) is not int or value < 1:
        raise GraderRefusal(f"{label} must be an explicit positive integer")
    return value


def _size_mb(raw: Mapping[str, Any], name: str, *, required: bool) -> int | None:
    value = raw.get(name + "_mb")
    if name in raw:
        legacy = raw[name]
        match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([GMK])", str(legacy).strip().upper())
        if match is None:
            raise GraderRefusal(f"unsupported legacy {name} declaration: {legacy!r}")
        factor = {"G": 1024, "M": 1, "K": 1 / 1024}[match[2]]
        converted = int(float(match[1]) * factor)
        if value is not None and value != converted:
            raise GraderRefusal(f"conflicting {name} and {name}_mb declarations")
        value = converted
    if value is None and not required:
        return None
    return _positive_integer(value, name + "_mb")


def _resources(raw: Mapping[str, Any], role: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise GraderRefusal(f"{role} environment declaration must be a table")
    if any(raw.get(key) for key in ("gpus", "gpu", "gpu_types", "tpu")):
        raise GraderRefusal(
            f"{role} requests an accelerator; this CPU-only grader has no approved accelerator budget"
        )
    if str(raw.get("os", "linux")).lower() != "linux":
        raise GraderRefusal(f"{role} requires a non-Linux environment")
    cpus = _positive_integer(raw.get("cpus"), f"{role}.cpus")
    memory = _size_mb(raw, "memory", required=True)
    build_timeout = raw.get("build_timeout_sec", 600)
    if _finite_number(build_timeout) is None or build_timeout <= 0:
        raise GraderRefusal(f"{role}.build_timeout_sec must be finite and positive")
    return {
        "role": role,
        "cpus": cpus,
        "memory_mb": memory,
        "storage_mb": _size_mb(raw, "storage", required=False),
        "build_timeout_sec": float(build_timeout),
        "docker_image": raw.get("docker_image"),
        "cpu_memory_enforcement": "exact declared limits; no overrides or downsizing",
        "storage_enforcement": "minimum free root filesystem capacity verified before task commands; not an exact disk quota",
    }


def _snapshot_members(archive: Path) -> Iterator[tarfile.TarInfo]:
    """Validate without extracting or allocating a runtime (also used by --plan)."""
    try:
        with tarfile.open(archive, "r:*") as stream:
            seen: set[str] = set()
            for member in stream:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or "\\" in member.name:
                    raise GraderRefusal(f"unsafe snapshot member: {member.name!r}")
                if not (member.isfile() or member.isdir()):
                    raise GraderRefusal(
                        f"snapshot links/devices/special files are unsupported: {member.name!r}"
                    )
                if member.mode & 0o7000:
                    raise GraderRefusal(
                        f"snapshot special permission bits are unsupported: {member.name!r}"
                    )
                normalized = path.as_posix()
                if normalized in seen:
                    raise GraderRefusal(f"duplicate snapshot member: {member.name!r}")
                seen.add(normalized)
                yield member
    except (tarfile.TarError, OSError) as exc:
        raise GraderRefusal(f"cannot read snapshot archive: {exc}") from exc


def _snapshot_identity(snapshot: ContainerSnapshot) -> dict[str, Any]:
    if not isinstance(snapshot.target, str) or not snapshot.target:
        raise GraderRefusal("snapshot target must be a normalized absolute directory")
    archive = Path(snapshot.archive).expanduser().resolve()
    if not archive.is_file():
        raise GraderRefusal(f"snapshot archive is missing: {archive}")
    target = PurePosixPath(snapshot.target)
    if (
        not target.is_absolute()
        or ".." in target.parts
        or str(target) != snapshot.target.rstrip("/")
    ):
        raise GraderRefusal("snapshot target must be a normalized absolute directory")
    # Application-state snapshots are useful, but replacing /, interpreters,
    # verifier inputs, or Harbor's log mounts is a different transport contract.
    if len(target.parts) < 2 or target.parts[1] not in {
        "app",
        "workspace",
        "testbed",
        "home",
        "root",
        "opt",
        "srv",
    }:
        raise GraderRefusal(
            "snapshot target must be under /app, /workspace, /testbed, /home, /root, /opt, or /srv"
        )
    if str(target) in {"/root", "/home", "/opt", "/srv"}:
        raise GraderRefusal("snapshot target cannot replace an entire system/home prefix")
    if snapshot.platform not in _PLATFORMS:
        raise GraderRefusal("snapshot platform must be linux/amd64 or linux/arm64")
    members = 0
    for _ in _snapshot_members(archive):
        members += 1
    if not members:
        raise GraderRefusal("snapshot archive is empty")
    return {
        "kind": "directory-tar-snapshot",
        "path": str(archive),
        "digest": _file_digest(archive),
        "target": str(target),
        "platform": snapshot.platform,
        "members": members,
        "semantics": "replace only the target subtree in a fresh original task image; no process/image/volume replay",
    }


def _declared_platform(task: Path, resources: Sequence[Mapping[str, Any]]) -> str | None:
    platforms: set[str] = set()
    for folder, uses_dockerfile in {
        (resource["build_context"], not resource["docker_image"]) for resource in resources
    }:
        dockerfile = task / folder / "Dockerfile"
        if uses_dockerfile and dockerfile.is_file():
            text = dockerfile.read_text(encoding="utf-8", errors="replace")
            platforms.update(
                re.findall(r"(?im)^\s*FROM\s+--platform=(linux/(?:amd64|arm64))\b", text)
            )
        wheels = task / folder / "wheels"
        if wheels.is_dir() and any(
            "x86_64" in path.name or "amd64" in path.name for path in wheels.glob("*.whl")
        ):
            platforms.add("linux/amd64")
    if len(platforms) > 1:
        raise GraderRefusal("task declares conflicting runtime architectures")
    return next(iter(platforms), None)


def _committed_spend(ledger: Mapping[str, Any]) -> float:
    return round(
        sum(
            item.get("finalized_estimate_usd", item["reserved_usd"])
            for item in ledger["reservations"]
        ),
        6,
    )


def _save_ledger(path: Path, ledger: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(ledger, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_ledger(path: Path, *, approval: str, cap: float) -> dict[str, Any]:
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema") != "evallab.upstream_grader.spend/v1":
            raise GraderRefusal("invalid upstream grader spend ledger")
        for key, expected in (
            ("approval", approval),
            ("max_usd", cap),
            ("price_basis", _PRICE_BASIS),
        ):
            if value.get(key) != expected:
                raise GraderRefusal(
                    f"existing spend ledger differs at {key}; do not reset a campaign's reservations"
                )
        reservations = value.get("reservations")
        if not isinstance(reservations, list):
            raise GraderRefusal("invalid spend ledger reservations")
        for item in reservations:
            amount = _finite_number(item.get("reserved_usd")) if isinstance(item, dict) else None
            if amount is None or amount <= 0:
                raise GraderRefusal("invalid persisted reservation amount")
            finalized = item.get("finalized_estimate_usd")
            if finalized is not None:
                closure = item.get("closure") or {}
                if (
                    _finite_number(finalized) is None
                    or not 0 < finalized <= amount
                    or not isinstance(closure, dict)
                    or closure.get("all_owned_sandboxes_terminated") is not True
                    or closure.get("all_image_preparations_completed") is not True
                ):
                    raise GraderRefusal(
                        "invalid finalized estimate or missing affirmative lifecycle closure"
                    )
        committed = _committed_spend(value)
        if (
            _finite_number(value.get("reserved_usd")) is None
            or committed > cap + 1e-9
            or value.get("reserved_usd") != committed
        ):
            raise GraderRefusal(
                "persisted ledger total is inconsistent or exceeds its immutable campaign cap"
            )
        return value
    return {
        "schema": "evallab.upstream_grader.spend/v1",
        "approval": approval,
        "max_usd": cap,
        "price_basis": dict(_PRICE_BASIS),
        "reservations": [],
        "reserved_usd": 0.0,
    }


def _resource_cost(seconds: float, *, cpus: int, memory_mb: int) -> float:
    try:
        cost = seconds * (
            cpus * _PRICE_BASIS["cpu_usd_per_core_second"]
            + memory_mb / 1024 * _PRICE_BASIS["memory_usd_per_gib_second"]
        )
    except OverflowError as exc:
        raise GraderRefusal(
            "declared resources exceed representable conservative cost accounting"
        ) from exc
    if not math.isfinite(cost) or cost < 0:
        raise GraderRefusal("declared resources require a non-finite conservative cost estimate")
    return cost


def _reservation(resources: Sequence[Mapping[str, Any]], timeout: int) -> dict[str, Any]:
    roles = []
    for resource in resources:
        preparation_seconds = resource["build_timeout_sec"]
        preparation = _resource_cost(
            preparation_seconds,
            cpus=max(resource["cpus"], _PRICE_BASIS["preparation_cpus"]),
            memory_mb=max(resource["memory_mb"], _PRICE_BASIS["preparation_memory_mb"]),
        )
        sandbox = _resource_cost(
            timeout + _CLEANUP_SECONDS, cpus=resource["cpus"], memory_mb=resource["memory_mb"]
        )
        roles.append(
            {
                "role": resource["role"],
                "cpus": resource["cpus"],
                "memory_mb": resource["memory_mb"],
                "sandbox_lifetime_seconds": timeout + _CLEANUP_SECONDS,
                "preparation_allowance_seconds": preparation_seconds,
                "preparation_estimate_usd": preparation,
                "sandbox_estimate_usd": sandbox,
            }
        )
    scaled = (
        sum(role["preparation_estimate_usd"] + role["sandbox_estimate_usd"] for role in roles)
        * _PRICE_BASIS["contingency_multiplier"]
        * 1_000_000
    )
    if not math.isfinite(scaled):
        raise GraderRefusal("full resource/lifetime reservation is not finite")
    amount = math.ceil(scaled) / 1_000_000
    return {"reserved_usd": amount, "roles": roles, "price_basis": dict(_PRICE_BASIS)}


def _reserve_spend(
    path: Path, *, approval: str, cap: float, reservation: dict[str, Any], identity: dict[str, Any]
) -> dict[str, Any]:
    """Persist full preparation/lifetime reserves before constructing any SDK."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            ledger = _read_ledger(path, approval=approval, cap=cap)
            reserved = _committed_spend(ledger)
            remaining = cap - reserved
            if reservation["reserved_usd"] > remaining + 1e-9:
                raise SpendCapReached(
                    "the full resource/lifetime reservation exceeds the remaining persisted campaign cap"
                )
            # Share admission arithmetic, but persist dollars for heterogeneous
            # shapes, not execution counts or one tiny calibrated unit.
            CloudSpendGate(approval, remaining, reservation["reserved_usd"]).charge()
            reservation_id = uuid.uuid4().hex
            ledger["reservations"].append(
                {"reservation_id": reservation_id, "at": _now(), **identity, **reservation}
            )
            ledger["reserved_usd"] = round(reserved + reservation["reserved_usd"], 6)
            _save_ledger(path, ledger)
            return {
                "ledger": str(path),
                "reservation_id": reservation_id,
                "approval": approval,
                "max_usd": cap,
                "reserved_usd": ledger["reserved_usd"],
                "this_run_reserved_usd": reservation["reserved_usd"],
                "basis": _PRICE_BASIS["basis"],
                "total_semantics": "closed finalized conservative estimates plus full still-open reservations",
            }
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _modal_admission(
    plan: Mapping[str, Any],
    *,
    approval: str | None,
    max_usd: float | None,
    spend_ledger: Path | None,
    modal_app_name: str | None,
    billing_tag: str | None,
) -> None:
    if not isinstance(approval, str) or not approval.strip():
        raise GraderRefusal("Modal requires recorded current --approval text")
    if (
        isinstance(max_usd, bool)
        or not isinstance(max_usd, int | float)
        or _finite_number(max_usd) is None
        or max_usd <= 0
    ):
        raise GraderRefusal("Modal requires a positive finite --max-usd campaign cap")
    if spend_ledger is None:
        raise GraderRefusal(
            "Modal requires one persisted --spend-ledger reused across the campaign"
        )
    immutable = [Path(plan["task"]["path"])]
    if plan["input"].get("path"):
        immutable.append(Path(plan["input"]["path"]))
    if any(spend_ledger == path or spend_ledger.is_relative_to(path) for path in immutable):
        raise GraderRefusal("spend ledger must not overlap immutable task or candidate inputs")
    if not isinstance(billing_tag, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,62}", billing_tag
    ):
        raise GraderRefusal("Modal requires an explicit safe --billing-tag")
    if (
        not isinstance(modal_app_name, str)
        or not (modal_app_name == billing_tag or modal_app_name.startswith(billing_tag + "-"))
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", modal_app_name)
    ):
        raise GraderRefusal(
            "Modal app must be the owned billing tag or a tag-prefixed dedicated app"
        )
    if plan["timeout_seconds"] + _CLEANUP_SECONDS > 3600:
        raise GraderRefusal(
            "Modal execution timeout plus the 90-second cleanup allowance must not exceed 3600 seconds"
        )
    if plan["platform"] not in {None, "linux/amd64"}:
        raise GraderRefusal(
            "Modal CPU snapshots/images require linux/amd64; no architecture fallback is permitted"
        )
    blocker = modal_resource_blocker(Path(plan["task"]["path"]))
    if blocker:
        raise GraderRefusal(blocker)
    ledger = _read_ledger(spend_ledger, approval=approval, cap=float(max_usd))
    prior = _committed_spend(ledger)
    if prior + plan["modal_reservation"]["reserved_usd"] > max_usd + 1e-9:
        raise SpendCapReached(
            "resource/lifetime reservation exceeds the remaining persisted campaign cap"
        )


def plan_upstream_grader(
    task: Path | str,
    solution_dir: Path | str | None = None,
    *,
    container_snapshot: ContainerSnapshot | None = None,
    oracle: bool = False,
    blank: bool = False,
    backend: Backend = "auto",
    platform: str | None = None,
    timeout_seconds: int = 600,
    max_usd: float | None = None,
    approval: str | None = None,
    spend_ledger: Path | str | None = None,
    modal_app_name: str | None = None,
    billing_tag: str | None = None,
) -> dict[str, Any]:
    """Read local inputs only: no backend, Docker probe, SDK, ledger write or allocation."""
    import tomllib

    package = Path(task).expanduser().resolve()
    if backend not in {"auto", "docker", "modal"}:
        raise GraderRefusal(f"unsupported backend: {backend}")
    _positive_integer(timeout_seconds, "timeout_seconds")
    if not isinstance(oracle, bool) or not isinstance(blank, bool):
        raise GraderRefusal("oracle and blank must be JSON booleans")
    if (
        sum((solution_dir is not None, container_snapshot is not None, bool(oracle), bool(blank)))
        != 1
    ):
        raise GraderRefusal(
            "select exactly one solution_dir, container_snapshot, oracle, or blank input"
        )
    if platform is not None and platform not in _PLATFORMS:
        raise GraderRefusal("platform must be linux/amd64 or linux/arm64")
    config = tomllib.loads((package / "task.toml").read_text(encoding="utf-8"))
    if config.get("steps"):
        raise GraderRefusal("multi-step task replay is unsupported; no partial task is graded")
    if not (package / "instruction.md").is_file() or not (package / "tests" / "test.sh").is_file():
        raise GraderRefusal("an original Harbor instruction.md and tests/test.sh are required")
    # Native Compose may add unbounded services not described by task.toml.
    # Refuse instead of allocating those services with fabricated/default limits.
    if any(
        (package / folder / name).exists()
        for folder in ("environment", "tests")
        for name in _COMPOSE_NAMES
    ):
        raise GraderRefusal(
            "Compose tasks are unsupported by this exact-resource primitive; sidecar resource admission is not implemented"
        )
    environment = config.get("environment") or {}
    verifier = config.get("verifier") or {}
    if not isinstance(environment, Mapping) or not isinstance(verifier, Mapping):
        raise GraderRefusal("upstream environment and verifier declarations must be tables")
    if verifier.get("environment_mode") not in {None, "shared", "separate"}:
        raise GraderRefusal("unsupported upstream verifier environment_mode")
    separate = verifier.get("environment_mode") == "separate" or (
        verifier.get("environment_mode") is None and verifier.get("environment") is not None
    )
    if verifier.get("environment_mode") == "shared" and verifier.get("environment") is not None:
        raise GraderRefusal("a shared verifier cannot declare a separate environment")
    resources = [_resources(environment, "agent")]
    if separate:
        resources.append(
            _resources(
                environment if verifier.get("environment") is None else verifier["environment"],
                "verifier",
            )
        )
    for resource in resources:
        folder = "environment"
        if resource["role"] == "verifier":
            declared_verifier = verifier.get("environment")
            if declared_verifier is not None and declared_verifier.get("docker_image"):
                folder = "tests"
            elif (package / "tests" / "Dockerfile").is_file():
                folder = "tests"
                resource["docker_image"] = None
            else:
                resource["docker_image"] = environment.get("docker_image")
        resource["build_context"] = folder
        if not resource["docker_image"] and not (package / folder / "Dockerfile").is_file():
            raise GraderRefusal(
                f"{resource['role']} has neither its original Dockerfile nor declared image"
            )
    exact = _tree_digest(package)
    if container_snapshot is not None:
        candidate = _snapshot_identity(container_snapshot)
        if platform is not None and platform != candidate["platform"]:
            raise GraderRefusal("requested platform differs from the snapshot's explicit platform")
        platform = candidate["platform"]
    elif blank:
        candidate = {
            "kind": "blank",
            "digest": "sha256:" + hashlib.sha256(b"").hexdigest(),
            "semantics": "fresh base; no candidate commands or solution upload",
        }
    else:
        solution = (
            package / "solution" if oracle else Path(str(solution_dir)).expanduser().resolve()
        )
        if not (solution / "solve.sh").is_file():
            raise GraderRefusal(f"solution directory must contain solve.sh: {solution}")
        candidate = {
            "kind": "oracle" if oracle else "solution-directory",
            "path": str(solution),
            "digest": _tree_digest(solution),
            "semantics": "copy to /solution and execute bash /solution/solve.sh with the task workdir and solution.env",
        }
    declared = _declared_platform(package, resources)
    if platform is not None and declared is not None and platform != declared:
        raise GraderRefusal(
            "requested/input platform conflicts with the original task's architecture"
        )
    platform = platform or declared
    result = {
        "schema": "evallab.upstream_grader.plan/v1",
        "task": {
            "path": str(package),
            "id": (config.get("task") or {}).get("name") or package.name,
            "digest": task_directory_digest(package),
            "exact_tree_digest": exact,
            "tests_digest": _tree_digest(package / "tests"),
        },
        "input": candidate,
        "backend": backend,
        "platform": platform,
        "resources": resources,
        "verifier_environment_mode": "separate" if separate else "shared",
        "environment_lifecycle": "fresh agent; native artifact collection then agent teardown before a fresh separate verifier",
        "timeout_seconds": timeout_seconds,
        "modal_reservation": _reservation(resources, timeout_seconds),
        "docker_admission": "unmeasured; strict live capacity/competing-container admission under the shared FIFO before allocation",
        "executed": False,
        "reserved_usd": 0.0,
        "agent_model_calls": 0,
        "task_external_api_calls": 0,
        "runnable": True,
        "refusals": [],
    }
    if backend == "modal":
        try:
            _modal_admission(
                result,
                approval=approval,
                max_usd=max_usd,
                spend_ledger=Path(spend_ledger).expanduser().resolve()
                if spend_ledger is not None
                else None,
                modal_app_name=modal_app_name,
                billing_tag=billing_tag,
            )
        except (ValueError, OSError, SpendCapReached) as exc:
            result["runnable"] = False
            result["refusals"] = [str(exc)]
    return result


def _docker_query(*arguments: str) -> Any:
    completed = subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, check=True, timeout=30
    )
    return json.loads(completed.stdout)


def _manifest_platforms(payload: Any) -> set[str]:
    entries = payload if isinstance(payload, list) else [payload]
    found: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        descriptor = entry.get("Descriptor") or entry
        platform = descriptor.get("platform") or {}
        if platform.get("os") and platform.get("architecture"):
            found.add(f"{platform['os']}/{platform['architecture']}")
        for manifest in entry.get("manifests") or []:
            found.update(_manifest_platforms(manifest))
    return found


def _admit_docker(plan: Mapping[str, Any]) -> dict[str, Any]:
    cpus, memory = docker_available_resources()
    required_cpus = max(resource["cpus"] for resource in plan["resources"])
    required_memory = max(resource["memory_mb"] for resource in plan["resources"])
    if cpus < required_cpus or memory < required_memory:
        raise GraderRefusal(
            f"Docker has {cpus:g} CPUs/{memory} MiB admitted after controller margin; task needs {required_cpus} CPUs/{required_memory} MiB (separate environments run sequentially)"
        )
    version = _docker_query("version", "--format", "{{json .Server}}")
    arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(version.get("Arch"), version.get("Arch"))
    native = f"{version.get('Os')}/{arch}"
    if native not in _PLATFORMS:
        raise GraderRefusal(f"unsupported Docker daemon platform: {native}")
    available = set(_PLATFORMS)
    images = sorted(
        {resource["docker_image"] for resource in plan["resources"] if resource["docker_image"]}
    )
    for image in images:
        platforms = _manifest_platforms(_docker_query("manifest", "inspect", "--verbose", image))
        if not platforms:
            raise GraderRefusal(f"image architecture is unavailable before allocation: {image}")
        available.intersection_update(platforms)
    requested = plan["platform"]
    selected = requested or (
        native if native in available else "linux/amd64" if "linux/amd64" in available else None
    )
    if selected not in available:
        raise GraderRefusal(
            "original task images and requested/input architecture have no supported common platform"
        )
    return {
        "platform": selected,
        "daemon_platform": native,
        "emulated": selected != native,
        "available_cpus": cpus,
        "available_memory_mb": memory,
        "image_platforms_checked": images,
    }


@dataclass
class _Execution:
    plan: Mapping[str, Any]
    environments: list[Any] = field(default_factory=list)
    claimed: list[tuple[int, int]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    cleanup_errors: list[str] = field(default_factory=list)
    evidence_errors: list[str] = field(default_factory=list)
    storage_observations: list[dict[str, Any]] = field(default_factory=list)
    started_monotonic: float = field(default_factory=time.monotonic)
    elapsed_seconds: float | None = None

    def claim(self, cpus: int, memory: int) -> str:
        resources = self.plan["resources"]
        if len(self.claimed) >= len(resources):
            raise GraderRefusal(
                "native environment startup exceeds the admitted lifecycle reservation"
            )
        expected = resources[len(self.claimed)]
        if (cpus, memory) != (expected["cpus"], expected["memory_mb"]):
            raise GraderRefusal(
                "native environment does not match its exact admitted role resources"
            )
        self.claimed.append((cpus, memory))
        return str(expected["role"])

    def closure_receipt(self) -> dict[str, Any] | None:
        if (
            self.cleanup_errors
            or self.elapsed_seconds is None
            or not math.isfinite(self.elapsed_seconds)
            or self.elapsed_seconds <= 0
        ):
            return None
        expected = len(self.plan["resources"])
        if len(self.environments) != expected or len(self.claimed) != expected:
            return None
        sandboxes = []
        for environment in self.environments:
            if not getattr(environment, "_upstream_image_build_completed", False) or not getattr(
                environment, "_upstream_termination_proven", False
            ):
                return None
            identity = getattr(environment, "_upstream_sandbox_id", None)
            exit_code = getattr(environment, "_upstream_exit_code", None)
            if not isinstance(identity, str) or not identity or type(exit_code) is not int:
                return None
            sandboxes.append(
                {
                    "sandbox_id": identity,
                    "exit_code": exit_code,
                    "evidence": "successful public SDK Sandbox.wait after termination",
                }
            )
        if len({sandbox["sandbox_id"] for sandbox in sandboxes}) != expected:
            return None
        return {
            "all_owned_sandboxes_terminated": True,
            "all_image_preparations_completed": True,
            "lifecycle_elapsed_seconds": self.elapsed_seconds,
            "sandboxes": sandboxes,
        }


def _reconcile_spend(
    spend: dict[str, Any], active: _Execution, *, measured: bool
) -> dict[str, Any]:
    closure = active.closure_receipt() if measured else None
    if closure is None:
        return {
            **spend,
            "reconciliation": {
                "status": "retained-full-reservation",
                "reason": "native measurement incomplete or image/sandbox closure not affirmatively proven",
                "cleanup_errors": list(active.cleanup_errors),
            },
        }
    seconds = math.ceil(closure["lifecycle_elapsed_seconds"])
    estimate = 0.0
    for resource in active.plan["resources"]:
        estimate += _resource_cost(
            seconds,
            cpus=max(resource["cpus"], _PRICE_BASIS["preparation_cpus"]),
            memory_mb=max(resource["memory_mb"], _PRICE_BASIS["preparation_memory_mb"]),
        )
        estimate += _resource_cost(seconds, cpus=resource["cpus"], memory_mb=resource["memory_mb"])
    estimate = math.ceil(estimate * _PRICE_BASIS["contingency_multiplier"] * 1_000_000) / 1_000_000
    if estimate >= spend["this_run_reserved_usd"]:
        return {
            **spend,
            "reconciliation": {
                "status": "retained-full-reservation",
                "reason": "closed elapsed-cost estimate leaves no unused reservation",
                "closed_estimate_usd": estimate,
                "closure": closure,
            },
        }
    path = Path(spend["ledger"])
    with path.with_name(path.name + ".lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            ledger = _read_ledger(path, approval=spend["approval"], cap=spend["max_usd"])
            entries = [
                item
                for item in ledger["reservations"]
                if item.get("reservation_id") == spend["reservation_id"]
            ]
            if len(entries) != 1 or "finalized_estimate_usd" in entries[0]:
                raise GraderRefusal(
                    "reservation identity missing, duplicated, or already finalized; full reserve not released"
                )
            entry = entries[0]
            entry["finalized_estimate_usd"] = estimate
            entry["finalized_at"] = _now()
            entry["closure"] = {**closure, "charged_lifecycle_seconds": seconds}
            ledger["reserved_usd"] = _committed_spend(ledger)
            _save_ledger(path, ledger)
            return {
                **spend,
                "reserved_usd": ledger["reserved_usd"],
                "this_run_finalized_estimate_usd": estimate,
                "unused_reservation_released_usd": round(
                    spend["this_run_reserved_usd"] - estimate, 6
                ),
                "reconciliation": {
                    "status": "finalized-conservative-estimate",
                    "closure": entry["closure"],
                    "not_a_provider_invoice": True,
                },
            }
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


_ACTIVE_EXECUTION: contextvars.ContextVar[_Execution | None] = contextvars.ContextVar(
    "upstream_grader_execution", default=None
)


def _active_execution() -> _Execution:
    active = _ACTIVE_EXECUTION.get()
    if active is None:
        raise GraderRefusal("upstream environment requires the admitted fresh-grader lifecycle")
    return active


async def _verify_storage_capacity(environment: Any) -> None:
    active = _active_execution()
    resource = next(
        item for item in active.plan["resources"] if item["role"] == environment._upstream_role
    )
    required = resource.get("storage_mb")
    if required is None:
        return
    observation: dict[str, Any] = {
        "role": resource["role"],
        "path": "/",
        "declared_minimum_mb": required,
        "hard_disk_quota_enforced": False,
        "minimum_verified": False,
    }
    try:
        result = await asyncio.wait_for(environment.exec("df -Pk /", timeout_sec=15), 15)
        observation["df_stdout"] = result.stdout
        if result.return_code != 0:
            raise ValueError(f"df exited {result.return_code}")
        row = (result.stdout or "").strip().splitlines()[-1].split()
        if len(row) < 6:
            raise ValueError("df did not emit a POSIX capacity row")
        available_kib = int(row[3])
        if available_kib < 0:
            raise ValueError("df emitted a negative available capacity")
        observation["available_kib"] = available_kib
        observation["minimum_verified"] = available_kib >= required * 1024
    except Exception as exc:
        observation["error"] = str(exc)
        active.storage_observations.append(observation)
        raise GraderRefusal(
            f"could not verify {resource['role']} declared minimum storage capacity: {exc}"
        ) from exc
    active.storage_observations.append(observation)
    if not observation["minimum_verified"]:
        raise GraderRefusal(
            f"{resource['role']} root filesystem has {available_kib} KiB available, below declared minimum {required} MiB; no disk quota or silent downsizing applied"
        )


async def _capture_extra_reports(environment: Any) -> None:
    """Preserve upstream reports that native Harbor does not download itself."""
    active = _active_execution()
    if getattr(environment, "_upstream_role", None) is None:
        return
    if environment._upstream_role != "verifier" and len(active.plan["resources"]) != 1:
        return
    name = str((active.plan.get("task") or {}).get("id", "")).rsplit("/", 1)[-1]
    reports = {
        "nextjs-performance": ("/tmp/playwright-report.json",),
        "cumulative-layout-shift": (
            "/tests/eval/results/eval-result.json",
            "/tests/eval/results/visual-results.json",
            "/tests/eval/results/cls-results.json",
        ),
    }.get(name, ())
    for remote in reports:
        destination = (
            environment._upstream_trial_dir
            / "verifier"
            / "upstream-extra"
            / PurePosixPath(remote).name
        )
        if destination.exists():
            continue
        try:
            if not await asyncio.wait_for(environment.is_file(remote), 5):
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.wait_for(environment.download_file(remote, destination), 5)
        except Exception as exc:
            active.evidence_errors.append(f"native extra report {remote}: {exc}")


def _observed_image_identity(
    environment: Any, *, container: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Read hydrated native handles only; never query a provider or infer a hit."""
    identity: dict[str, Any] = {
        "environment_id": None,
        "docker_image_reference": None,
        "docker_image_id": None,
        "modal_image_id": None,
        "cache_hit": None,
        "semantics": "observed native identities; repeated identity does not prove a cache hit",
    }
    errors = []
    try:
        identity["environment_id"] = getattr(environment, "environment_id", None)
    except Exception as exc:
        errors.append(f"native environment identity unavailable: {exc}")
    if container is not None:
        identity["docker_image_reference"] = (container.get("Config") or {}).get("Image")
        identity["docker_image_id"] = container.get("Image")
    else:
        try:
            image = getattr(environment, "_image", None)
            identity["modal_image_id"] = getattr(image, "object_id", None)
        except Exception as exc:
            # Optional evidence must not invalidate an otherwise completed grade.
            errors.append(f"native hydrated Modal image identity unavailable: {exc}")
    if errors:
        identity["errors"] = errors
    return identity


@cache
def _runtime_type(name: str) -> type:
    """Lazily expose native subclasses to Harbor's import-path factory, never a plan."""
    if name == "UpstreamDockerEnvironment":
        from evallab.harbor_host_isolation import HostIsolatedDockerEnvironment

        class UpstreamDockerEnvironment(HostIsolatedDockerEnvironment):
            def __init__(
                self, *, upstream_platform: str, session_id: str, trial_paths: Any, **kwargs: Any
            ) -> None:
                self._upstream_platform = upstream_platform
                self._upstream_cleaned = False
                self._upstream_role: str | None = None
                self._upstream_trial_dir = Path(trial_paths.trial_dir)
                identity = f"{Path(trial_paths.trial_dir).resolve()}\0{session_id}"
                super().__init__(
                    session_id="upstream-" + hashlib.sha256(identity.encode()).hexdigest()[:48],
                    trial_paths=trial_paths,
                    **kwargs,
                )
                _active_execution().environments.append(self)

            async def _write_resources_compose_file(self) -> Path:
                path = await super()._write_resources_compose_file()
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload["services"]["main"]["platform"] = self._upstream_platform
                path.write_text(json.dumps(payload), encoding="utf-8")
                return path

            async def start(self, force_build: bool = False) -> None:
                active = _active_execution()
                cpus, memory = self._effective_cpus, self._effective_memory_mb
                if cpus is None or memory is None:
                    raise GraderRefusal("native Docker resource limits are missing")
                self._upstream_role = active.claim(cpus, memory)
                available_cpus, available_memory = await asyncio.to_thread(
                    docker_available_resources
                )
                if available_cpus < cpus or available_memory < memory:
                    raise GraderRefusal(
                        "Docker capacity changed before fresh environment allocation"
                    )
                await super().start(force_build=force_build)
                if self._container_id is None:
                    raise RuntimeError("native Docker start returned no inspectable container")
                container = await self._daemon_json("inspect", self._container_id)
                host = container[0]["HostConfig"]
                cpu = host.get("NanoCpus") or 0
                if not cpu and (host.get("CpuPeriod") or 0) > 0:
                    cpu = (host.get("CpuQuota") or 0) * 1_000_000_000 / host["CpuPeriod"]
                if cpu != cpus * 1_000_000_000 or host.get("Memory") != memory * 1024 * 1024:
                    raise RuntimeError("Docker did not enforce the exact declared CPU/RAM limits")
                await _verify_storage_capacity(self)
                active.observations.append(
                    {
                        "backend": "docker",
                        "role": self._upstream_role,
                        "session_id": self.session_id,
                        "container_id": self._container_id,
                        "cpus": self._effective_cpus,
                        "memory_mb": self._effective_memory_mb,
                        "platform": self._upstream_platform,
                        "limits_verified": True,
                        "native_image_identity": _observed_image_identity(
                            self, container=container[0]
                        ),
                    }
                )

            async def stop(self, delete: bool = True) -> None:
                await _capture_extra_reports(self)
                await super().stop(delete=delete)
                self._upstream_cleaned = True

        return UpstreamDockerEnvironment
    if name == "UpstreamModalEnvironment":
        from evallab.harbor_modal import LockedModalEnvironment

        class UpstreamModalEnvironment(LockedModalEnvironment):
            def __init__(self, *, session_id: str, trial_paths: Any, **kwargs: Any) -> None:
                self._upstream_cleaned = False
                self._upstream_sandbox: Any = None
                self._upstream_sandbox_id: str | None = None
                self._upstream_exit_code: int | None = None
                self._upstream_image_build_completed = False
                self._upstream_termination_proven = False
                self._upstream_role: str | None = None
                self._upstream_trial_dir = Path(trial_paths.trial_dir)
                identity = f"{Path(trial_paths.trial_dir).resolve()}\0{session_id}"
                super().__init__(
                    session_id="upstream-" + hashlib.sha256(identity.encode()).hexdigest()[:48],
                    trial_paths=trial_paths,
                    **kwargs,
                )
                _active_execution().environments.append(self)

            async def start(self, force_build: bool = False) -> None:
                active = _active_execution()
                cpus, memory = self._effective_cpus, self._effective_memory_mb
                if cpus is None or memory is None:
                    raise GraderRefusal("native Modal resource limits are missing")
                self._upstream_role = active.claim(cpus, memory)
                cpu_config, memory_config = self._cpu_config(), self._memory_config()
                if (
                    not isinstance(cpu_config, tuple)
                    or cpu_config != (cpus, cpus)
                    or not isinstance(memory_config, tuple)
                    or memory_config != (memory, memory)
                ):
                    raise GraderRefusal(
                        "Modal must request and limit the exact declared CPU/RAM before image preparation"
                    )
                try:
                    await super().start(force_build=force_build)
                except BaseException:
                    try:
                        await asyncio.shield(
                            asyncio.wait_for(self.stop(delete=True), _CLEANUP_SECONDS)
                        )
                    except Exception as exc:
                        active.cleanup_errors.append(f"Modal failed-start cleanup: {exc}")
                    raise
                self._upstream_sandbox = self._sandbox
                self._upstream_sandbox_id = getattr(self._sandbox, "object_id", None)
                self._upstream_image_build_completed = self._sandbox is not None
                await _verify_storage_capacity(self)
                active.observations.append(
                    {
                        "backend": "modal",
                        "role": self._upstream_role,
                        "session_id": self.session_id,
                        "sandbox_id": getattr(self._sandbox, "object_id", None),
                        "cpus": self._effective_cpus,
                        "memory_mb": self._effective_memory_mb,
                        "platform": "linux/amd64",
                        "cpu_request_and_limit": list(cpu_config),
                        "memory_request_and_limit_mb": list(memory_config),
                        "sandbox_lifetime_seconds": self._sandbox_timeout,
                        "native_image_identity": _observed_image_identity(self),
                    }
                )

            async def _terminate_sandbox(self) -> None:
                try:
                    await super()._terminate_sandbox()
                except Exception as exc:
                    _active_execution().cleanup_errors.append(
                        f"native Modal termination error: {exc}"
                    )
                    raise

            async def stop(self, delete: bool = True) -> None:
                sandbox = self._upstream_sandbox or getattr(self, "_sandbox", None)
                if sandbox is not None:
                    self._upstream_sandbox = sandbox
                    self._upstream_sandbox_id = getattr(sandbox, "object_id", None)
                try:
                    await _capture_extra_reports(self)
                    await super().stop(delete=delete)
                    if sandbox is not None:
                        # Harbor swallows teardown failures and clears its handle.
                        # Retain ours and demand terminal public SDK evidence.
                        await asyncio.wait_for(
                            sandbox.wait.aio(raise_on_termination=False), _CLEANUP_SECONDS
                        )
                        code = sandbox.returncode
                        if type(code) is not int:
                            raise RuntimeError("Modal wait completed without a terminal returncode")
                        self._upstream_exit_code = code
                        self._upstream_termination_proven = True
                    elif self._upstream_image_build_completed:
                        raise RuntimeError("owned Modal sandbox handle missing at cleanup")
                    self._upstream_cleaned = True
                except Exception as exc:
                    _active_execution().cleanup_errors.append(f"Modal closure unconfirmed: {exc}")
                    # Harbor records raised agent-stop errors as grade failures.
                    # Keep this explicit cleanup uncertainty separate: no unused
                    # reservation is released, and outer cleanup may retry.

        return UpstreamModalEnvironment
    raise AttributeError(name)


def __getattr__(name: str) -> Any:
    if name in {"UpstreamDockerEnvironment", "UpstreamModalEnvironment"}:
        return _runtime_type(name)
    raise AttributeError(name)


def _stage_inputs(plan: Mapping[str, Any], output: Path) -> tuple[Path, Path | None]:
    inputs = output / "inputs"
    inputs.mkdir()
    source = Path(plan["task"]["path"])
    staged = inputs / "task" / source.name
    shutil.copytree(source, staged)
    if _tree_digest(staged) != plan["task"]["exact_tree_digest"]:
        raise GraderRefusal("task bytes or modes changed while staging; no execution")
    candidate = plan["input"]
    if candidate["kind"] == "blank":
        return staged, None
    if candidate["kind"] == "oracle":
        return staged, staged / "solution"
    solution = inputs / "solution"
    if candidate["kind"] == "solution-directory":
        shutil.copytree(Path(candidate["path"]), solution)
        if _tree_digest(solution) != candidate["digest"]:
            raise GraderRefusal("solution changed while staging; no execution")
        return staged, solution
    solution.mkdir()
    archive = inputs / "snapshot.tar"
    shutil.copyfile(candidate["path"], archive)
    if _file_digest(archive) != candidate["digest"]:
        raise GraderRefusal("snapshot changed while staging; no execution")
    payload = solution / "payload"
    payload.mkdir()
    # Already validated before admission, then revalidate the frozen copy. The
    # filter also blocks future tarfile extraction behavior from widening it.
    for _ in _snapshot_members(archive):
        pass
    with tarfile.open(archive, "r:*") as stream:
        stream.extractall(payload, filter="data")
    target = shlex.quote(candidate["target"])
    (solution / "solve.sh").write_text(
        f"#!/bin/bash\nset -euo pipefail\nrm -rf -- {target}\nmkdir -p -- {target}\ncp -a /solution/payload/. {target}/\n",
        encoding="utf-8",
    )
    return staged, solution


def _trial_config(
    plan: Mapping[str, Any],
    *,
    staged_task: Path,
    solution: Path | None,
    trial_dir: Path,
    backend: str,
    platform: str,
    modal_app_name: str | None,
    billing_tag: str | None,
) -> dict[str, Any]:
    import tomllib

    config = tomllib.loads((staged_task / "task.toml").read_text(encoding="utf-8"))
    environment_kwargs: dict[str, Any] = (
        {"upstream_platform": platform}
        if backend == "docker"
        else {
            "sandbox_timeout_secs": plan["timeout_seconds"] + _CLEANUP_SECONDS,
            "sandbox_create_max_attempts": 1,
            "app_name": modal_app_name,
            "labels": {"evallab.billing_tag": billing_tag, "evallab.primitive": "upstream-grader"},
            "evallab_egress_lock": True,
        }
    )
    return {
        "task": {"path": staged_task},
        "trial_name": trial_dir.name,
        "trials_dir": trial_dir.parent,
        "agent": {
            "import_path": "evallab.qa.task_modal:QAScriptAgent",
            "override_timeout_sec": plan["timeout_seconds"],
            "override_setup_timeout_sec": min(plan["timeout_seconds"], 360),
            "kwargs": {
                "qa_solution_dir": str(solution) if solution is not None else None,
                "qa_command": "bash /solution/solve.sh" if solution is not None else None,
                "qa_workdir": (config.get("environment") or {}).get("workdir"),
                "qa_timeout_seconds": plan["timeout_seconds"],
                "qa_solution_env": (config.get("solution") or {}).get("env", {})
                if solution is not None and plan["input"]["kind"] != "directory-tar-snapshot"
                else {},
            },
        },
        "environment": {
            "import_path": f"evallab.upstream_grader:Upstream{'Docker' if backend == 'docker' else 'Modal'}Environment",
            "delete": True,
            "cpu_enforcement_policy": "limit" if backend == "docker" else "guarantee",
            "memory_enforcement_policy": "limit" if backend == "docker" else "guarantee",
            "kwargs": environment_kwargs,
        },
        "verifier": {},
    }


async def _execute_native(config: dict[str, Any], active: _Execution) -> dict[str, Any]:
    from harbor.models.trial.config import TrialConfig  # ty: ignore[unresolved-import]
    from harbor.trial.trial import Trial  # ty: ignore[unresolved-import]

    token = _ACTIVE_EXECUTION.set(active)
    try:
        trial = await Trial.create(TrialConfig.model_validate(config))
        result = await asyncio.wait_for(trial.run(), timeout=active.plan["timeout_seconds"])
        return result.model_dump(mode="json")
    finally:
        try:
            for environment in reversed(active.environments):
                if environment._upstream_cleaned:
                    continue
                try:
                    await asyncio.shield(
                        asyncio.wait_for(environment.stop(delete=True), _CLEANUP_SECONDS)
                    )
                except Exception as exc:
                    active.cleanup_errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            active.elapsed_seconds = time.monotonic() - active.started_monotonic
            _ACTIVE_EXECUTION.reset(token)


def _safe_evidence_file(path: Path, root: Path) -> bool:
    return (
        path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve())
    )


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _json_float(value: str) -> float:
    number = _finite_number(float(value))
    if number is None:
        raise ValueError("nonfinite JSON number; original report retained")
    return number


def _read_report_json(path: Path) -> Any:
    return json.loads(
        path.read_text(encoding="utf-8"), parse_float=_json_float, parse_constant=_json_float
    )


def _read_native_rewards(trial_dir: Path) -> dict[str, float]:
    verifier = trial_dir / "verifier"
    json_path = verifier / "reward.json"
    text_path = verifier / "reward.txt"
    path = json_path if json_path.exists() else text_path
    if not _safe_evidence_file(path, verifier) or path.stat().st_size > 1024 * 1024:
        raise ValueError("native reward file is missing, unsafe or oversized")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("native reward file is empty")
    raw = json.loads(text) if path == json_path else {"reward": float(text)}
    if not isinstance(raw, dict) or not raw:
        raise ValueError("native reward JSON must be a nonempty mapping")
    rewards = {key: _finite_number(value) for key, value in raw.items()}
    if any(value is None for value in rewards.values()):
        raise ValueError("native reward contains a nonnumeric or nonfinite value")
    return {key: float(value) for key, value in rewards.items() if value is not None}


_TEST_STATUSES = {
    "passed": "passed",
    "pass": "passed",
    "failed": "failed",
    "fail": "failed",
    "error": "error",
    "broken": "error",
    "timedout": "error",
    "interrupted": "error",
    "skipped": "skipped",
    "skip": "skipped",
    "pending": "pending",
    "other": "unknown",
    "xfailed": "xfailed",
    "xpassed": "xpassed",
    "xfail": "xfailed",
    "xpass": "xpassed",
}


def _test_record(
    identity: str, status: str, source: Path, *, duration: Any = None, raw: Any = None
) -> dict[str, Any]:
    return {
        "id": identity,
        "status": _TEST_STATUSES.get(status.lower(), "unknown"),
        "raw_status": status,
        "duration_seconds": _finite_number(duration),
        "evidence": str(source),
        "raw": raw,
    }


def _reported_tests(
    kind: str, reports: list[tuple[Path, list[dict[str, Any]]]], errors: list[dict[str, str]]
) -> dict[str, Any]:
    by_id: dict[str, dict[str, Any]] = {}
    for _, records in reports:
        for record in records:
            prior = by_id.get(record["id"])
            if prior is None:
                by_id[record["id"]] = dict(record)
                continue
            observations = prior.setdefault("observations", [dict(prior)])
            observations.append(record)
            if any(item["raw_status"] != observations[0]["raw_status"] for item in observations):
                prior["status"] = "multiple-observations"
                prior["raw_status"] = None
    return {
        "status": "available",
        "format": kind,
        "tests": list(by_id.values()),
        "sources": [str(path) for path, _ in reports],
        "parse_errors": errors,
    }


def _visual_records(data: Any, path: Path) -> list[dict[str, Any]]:
    records = []
    for page in data if isinstance(data, list) else []:
        if (
            not isinstance(page, dict)
            or not isinstance(page.get("page"), str)
            or not isinstance(page.get("viewport"), str)
        ):
            continue
        checks = page.get("checks")
        for check in checks if isinstance(checks, list) else []:
            if (
                isinstance(check, dict)
                and isinstance(check.get("name"), str)
                and isinstance(check.get("passed"), bool)
            ):
                identity = f"{page['page']} @ {page['viewport']} :: {check['name']}"
                records.append(
                    _test_record(
                        identity,
                        "passed" if check["passed"] else "failed",
                        path,
                        raw={"page": page["page"], "viewport": page["viewport"], "check": check},
                    )
                )
    return records


def _playwright_records(suite: Any, path: Path) -> list[dict[str, Any]]:
    if not isinstance(suite, dict):
        return []
    records = []
    specs = suite.get("specs")
    for spec in specs if isinstance(specs, list) else []:
        if not isinstance(spec, dict):
            continue
        identity = spec.get("id") or spec.get("title")
        if not isinstance(identity, str):
            continue
        tests = spec.get("tests")
        for test in tests if isinstance(tests, list) else []:
            if not isinstance(test, dict):
                continue
            project = test.get("projectName")
            name = f"{identity} :: {project}" if isinstance(project, str) and project else identity
            results = test.get("results")
            for result in results if isinstance(results, list) else []:
                if not isinstance(result, dict) or not isinstance(result.get("status"), str):
                    continue
                duration = _finite_number(result.get("duration"))
                records.append(
                    _test_record(
                        name,
                        result["status"],
                        path,
                        duration=duration / 1000 if duration is not None else None,
                        raw={
                            "spec_id": spec.get("id"),
                            "title": spec.get("title"),
                            "projectName": project,
                            "result": result,
                        },
                    )
                )
    children = suite.get("suites")
    for child in children if isinstance(children, list) else []:
        records.extend(_playwright_records(child, path))
    return records


def parse_per_test_results(verifier_dir: Path | str) -> dict[str, Any]:
    """Expose emitted identities, never inferred tests or count-derived passes.

    Native trace checks/cases outrank a transport-wrapper CTRF. A canonical
    ctrf.json outranks numbered retry reports, which remain untouched evidence.
    Without a canonical report, repeated identities retain all observations.
    Continuous reward axes are raw evidence, not fabricated boolean tests.
    """
    root = Path(verifier_dir)
    errors: list[dict[str, str]] = []
    paths = sorted(root.rglob("*")) if root.is_dir() else []
    documents: list[tuple[Path, Any]] = []
    for path in paths:
        if (
            path.suffix.lower() != ".json"
            or path.name == "reward.json"
            or not _safe_evidence_file(path, root)
        ):
            continue
        try:
            if path.stat().st_size > _MAX_STRUCTURED_BYTES:
                raise ValueError(
                    "structured report exceeds the 25 MiB parsing limit; raw bytes retained"
                )
            data = _read_report_json(path)
            documents.append((path, data))
        except (OSError, ValueError) as exc:
            errors.append({"path": str(path), "error": str(exc)})
    for kind in (
        "trace-checks",
        "trace-cases",
        "visual-checks",
        "ctrf",
        "pytest-json",
        "playwright",
    ):
        reports: list[tuple[Path, list[dict[str, Any]]]] = []
        for path, data in documents:
            records: list[dict[str, Any]] = []
            if kind == "visual-checks":
                records = _visual_records(data, path) if path.name == "visual-results.json" else []
            elif not isinstance(data, dict):
                continue
            elif kind == "playwright":
                records = (
                    _playwright_records(data, path) if isinstance(data.get("suites"), list) else []
                )
            elif kind == "trace-checks" and path.name == "trace_results.json":
                checks = data.get("checks")
                for test in checks if isinstance(checks, list) else []:
                    if (
                        isinstance(test, dict)
                        and isinstance(test.get("name"), str)
                        and isinstance(test.get("passed"), bool)
                    ):
                        records.append(
                            _test_record(
                                test["name"],
                                "passed" if test["passed"] else "failed",
                                path,
                                raw=test,
                            )
                        )
            elif kind == "trace-cases" and path.name == "trace_results.json":
                cases = data.get("cases")
                for identity, case in cases.items() if isinstance(cases, dict) else []:
                    if isinstance(case, dict) and isinstance(case.get("passed"), bool):
                        records.append(
                            _test_record(
                                identity, "passed" if case["passed"] else "failed", path, raw=case
                            )
                        )
            elif kind in {"ctrf", "pytest-json"}:
                result = data.get("results")
                tests = (
                    result.get("tests")
                    if kind == "ctrf" and isinstance(result, dict)
                    else data.get("tests")
                    if kind == "pytest-json"
                    else None
                )
                for test in tests if isinstance(tests, list) else []:
                    if not isinstance(test, dict):
                        continue
                    identity = test.get("name") if kind == "ctrf" else test.get("nodeid")
                    status = test.get("status") if kind == "ctrf" else test.get("outcome")
                    if not isinstance(identity, str) or not isinstance(status, str):
                        continue
                    call = test.get("call")
                    duration = (
                        _finite_number(test.get("duration"))
                        if kind == "ctrf"
                        else _finite_number(
                            call.get("duration") if isinstance(call, dict) else None
                        )
                    )
                    if duration is not None and kind == "ctrf":
                        duration /= 1000
                    records.append(
                        _test_record(identity, status, path, duration=duration, raw=test)
                    )
            if records:
                reports.append((path, records))
        if reports:
            canonical = (
                [report for report in reports if report[0].name == "ctrf.json"]
                if kind == "ctrf"
                else []
            )
            selected = canonical or reports
            parsed = _reported_tests(kind, selected, errors)
            parsed["non_selected_reports"] = [
                str(path)
                for path, _ in reports
                if all(path != selected_path for selected_path, _ in selected)
            ]
            return parsed
    reports = []
    for path in paths:
        if path.suffix.lower() != ".xml" or not _safe_evidence_file(path, root):
            continue
        try:
            if path.stat().st_size > _MAX_STRUCTURED_BYTES:
                raise ValueError(
                    "structured report exceeds the 25 MiB parsing limit; raw bytes retained"
                )
            content = path.read_bytes()
            if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
                raise ValueError("XML entity/DTD declarations are unsupported; raw bytes retained")
            document = ET.fromstring(content)
            records = []
            for case in document.iter():
                if case.tag.rsplit("}", 1)[-1] != "testcase" or not case.get("name"):
                    continue
                tags = {child.tag.rsplit("}", 1)[-1] for child in case}
                status = (
                    "error"
                    if "error" in tags
                    else "failed"
                    if "failure" in tags
                    else "skipped"
                    if "skipped" in tags
                    else "passed"
                )
                name = "::".join(part for part in (case.get("classname"), case.get("name")) if part)
                try:
                    duration = float(case.get("time", "nan"))
                except ValueError:
                    duration = None
                records.append(
                    _test_record(name, status, path, duration=duration, raw=dict(case.attrib))
                )
            if records:
                reports.append((path, records))
        except (OSError, ValueError, ET.ParseError) as exc:
            errors.append({"path": str(path), "error": str(exc)})
    if reports:
        return _reported_tests("junit", reports, errors)
    reports = []
    for path in paths:
        if path.suffix not in {".txt", ".log"} or not _safe_evidence_file(path, root):
            continue
        records = []
        try:
            with path.open(encoding="utf-8", errors="replace") as stream:
                for raw_line in stream:
                    line = re.sub(r"\x1b\[[0-9;]*m", "", raw_line).strip()
                    match = re.match(
                        r"^(\S.*?\.py::.+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s|$)",
                        line,
                    )
                    summary = re.match(
                        r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S.*?\.py::.+?)(?:\s+-\s+.*)?$",
                        line,
                    )
                    if match:
                        identity, status = match[1], match[2]
                    elif summary:
                        status, identity = summary[1], summary[2]
                    else:
                        continue
                    records.append(_test_record(identity, status, path, raw=raw_line.rstrip("\n")))
            if records:
                reports.append((path, records))
        except OSError as exc:
            errors.append({"path": str(path), "error": str(exc)})
    if reports:
        return _reported_tests("pytest-output", reports, errors)
    return {
        "status": "unavailable",
        "format": None,
        "tests": [],
        "sources": [],
        "parse_errors": errors,
        "reason": "upstream verifier emitted no supported per-test report or explicit pytest node outcomes; continuous scores and raw logs retained without synthesizing tests",
    }


def _score_details(verifier_dir: Path) -> dict[str, Any] | None:
    path = verifier_dir / "reward_details.json"
    if not _safe_evidence_file(path, verifier_dir):
        return None
    try:
        if path.stat().st_size > _MAX_STRUCTURED_BYTES:
            raise ValueError(
                "native score details exceed the 25 MiB parsing limit; raw bytes retained"
            )
        return {
            "path": str(path),
            "data": _read_report_json(path),
            "semantics": "continuous native diagnostics; not independently boolean tests",
        }
    except (OSError, ValueError) as exc:
        return {"path": str(path), "error": str(exc)}


def _collect_execution(
    plan: Mapping[str, Any],
    output: Path,
    payload: dict[str, Any],
    *,
    backend: str | None,
    platform: str | None,
    error: str | None,
    status: str,
    active: _Execution | None,
    spend: dict[str, Any] | None,
    admission: dict[str, Any] | None,
) -> dict[str, Any]:
    trial_dir = output / "trial"
    errors = [error] if error else []
    if payload.get("exception_info"):
        exception = payload["exception_info"]
        errors.append(
            str(exception.get("exception_message") or exception)
            if isinstance(exception, dict)
            else str(exception)
        )
    native_rewards: dict[str, float] = {}
    try:
        native_rewards = _read_native_rewards(trial_dir)
        recorded = (payload.get("verifier_result") or {}).get("rewards")
        if recorded != native_rewards:
            errors.append(
                "native reward files disagree with, or are absent from, Harbor's verifier result"
            )
        if "reward" not in native_rewards:
            errors.append(
                "native verifier emitted no scalar 'reward'; other reward keys are preserved"
            )
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        if status != "refused":
            errors.append(str(exc))
    if (trial_dir / "agent" / "exit-code.txt").exists():
        errors.append(
            "candidate solution exited nonzero; native reward is retained only as an unusable observation"
        )
    for path, expected in (
        (Path(plan["task"]["path"]), plan["task"]["exact_tree_digest"]),
        (
            output / "inputs" / "task" / Path(plan["task"]["path"]).name,
            plan["task"]["exact_tree_digest"],
        ),
    ):
        try:
            if active is not None and not path.is_dir():
                errors.append(f"immutable task disappeared during execution: {path}")
            elif path.exists() and _tree_digest(path) != expected:
                errors.append(f"immutable task bytes/modes changed: {path}")
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
    candidate = plan["input"]
    try:
        if (
            candidate["kind"] in {"oracle", "solution-directory"}
            and _tree_digest(Path(candidate["path"])) != candidate["digest"]
        ):
            errors.append("original solution input changed during execution")
        if candidate["kind"] == "solution-directory":
            staged_solution = output / "inputs" / "solution"
            if active is not None and not staged_solution.is_dir():
                errors.append("staged solution disappeared during execution")
            elif staged_solution.is_dir() and _tree_digest(staged_solution) != candidate["digest"]:
                errors.append("staged solution input changed during execution")
        if (
            candidate["kind"] == "directory-tar-snapshot"
            and _file_digest(Path(candidate["path"])) != candidate["digest"]
        ):
            errors.append("original snapshot input changed during execution")
        if candidate["kind"] == "directory-tar-snapshot":
            staged_snapshot = output / "inputs" / "snapshot.tar"
            if active is not None and not staged_snapshot.is_file():
                errors.append("staged snapshot disappeared during execution")
            elif staged_snapshot.is_file() and _file_digest(staged_snapshot) != candidate["digest"]:
                errors.append("staged snapshot input changed during execution")
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
    verifier_dir = trial_dir / "verifier"
    evidence = []
    if verifier_dir.is_dir():
        for path in sorted(verifier_dir.rglob("*")):
            if _safe_evidence_file(path, verifier_dir):
                evidence.append(
                    {"path": str(path), "digest": _file_digest(path), "bytes": path.stat().st_size}
                )
            elif path.is_symlink():
                errors.append(f"unsafe verifier evidence symlink: {path}")
    if errors and status not in {"refused", "cancelled", "timeout"}:
        status = "error"
    elif not errors:
        status = "measured"
    reward = native_rewards.get("reward") if not errors and status == "measured" else None
    return {
        "schema": "evallab.upstream_grader.result/v1",
        "task": dict(plan["task"]),
        "input": dict(candidate),
        "backend": backend,
        "platform": platform,
        "resources": list(plan["resources"]),
        "resource_observations": active.observations if active else [],
        "lifecycle": {
            "elapsed_seconds": active.elapsed_seconds if active else None,
            "modal_budget_closure_proven": active.closure_receipt() is not None
            if active
            else False,
            "closure": active.closure_receipt() if active else None,
            "cleanup_errors": list(active.cleanup_errors) if active else [],
        },
        "evidence_errors": list(active.evidence_errors) if active else [],
        "storage_observations": list(active.storage_observations) if active else [],
        "docker_admission": admission,
        "status": status,
        "reward": reward,
        "rewards": native_rewards if reward is not None else None,
        "observed_native_rewards": native_rewards or None,
        "native_score_details": _score_details(verifier_dir),
        "per_test": parse_per_test_results(verifier_dir),
        "raw_verifier_evidence": evidence,
        "verifier_log_streams": "native test-stdout.txt combines stdout/stderr; all emitted verifier files retained unchanged",
        "trial_dir": str(trial_dir),
        "output_dir": str(output),
        "native_result": str(trial_dir / "result.json")
        if (trial_dir / "result.json").is_file()
        else None,
        "spend": spend,
        "error": "; ".join(dict.fromkeys(errors)) or None,
        "executed": active is not None,
        "agent_model_calls": 0,
        "task_external_api_calls": None if active is not None else 0,
        "finished_at": _now(),
    }


def run_upstream_grader(
    task: Path | str,
    solution_dir: Path | str | None = None,
    *,
    container_snapshot: ContainerSnapshot | None = None,
    oracle: bool = False,
    blank: bool = False,
    backend: Backend = "auto",
    platform: str | None = None,
    timeout_seconds: int = 600,
    max_usd: float | None = None,
    approval: str | None = None,
    spend_ledger: Path | str | None = None,
    modal_app_name: str | None = None,
    billing_tag: str | None = None,
    output_dir: Path | str | None = None,
    plan: bool = False,
) -> dict[str, Any]:
    """Grade one input with a script-only agent; errors are not reward=0 or pass.

    ``auto`` may select approved Modal ONLY when pre-execution Docker admission
    fails. Neither a Docker execution failure nor a Modal refusal triggers retry
    on another backend. Existing output directories are never resumed/overwritten.
    """
    prepared = plan_upstream_grader(
        task,
        solution_dir,
        container_snapshot=container_snapshot,
        oracle=oracle,
        blank=blank,
        backend=backend,
        platform=platform,
        timeout_seconds=timeout_seconds,
        max_usd=max_usd,
        approval=approval,
        spend_ledger=spend_ledger,
        modal_app_name=modal_app_name,
        billing_tag=billing_tag,
    )
    if plan:
        return prepared
    output = (
        Path(output_dir).expanduser().resolve()
        if output_dir is not None
        else (Path.cwd() / "runs" / ("upstream-grader-" + uuid.uuid4().hex)).resolve()
    )
    sources = [Path(prepared["task"]["path"])]
    if prepared["input"].get("path"):
        sources.append(Path(prepared["input"]["path"]))
    for source in sources:
        if output == source or output.is_relative_to(source) or source.is_relative_to(output):
            raise GraderRefusal("output directory must not overlap an immutable input")
    if spend_ledger is not None and Path(spend_ledger).expanduser().resolve().is_relative_to(
        output
    ):
        raise GraderRefusal(
            "the shared campaign spend ledger must be outside each fresh evidence directory"
        )
    if output.exists():
        raise GraderRefusal(
            "output directory already exists; choose a fresh immutable evidence directory"
        )
    output.mkdir(parents=True)
    (output / "plan.json").write_text(
        json.dumps(prepared, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    active: _Execution | None = None
    spend = None
    admission = None
    chosen: str | None = None
    chosen_platform = prepared["platform"]
    payload: dict[str, Any] = {}
    error = None
    status = "refused"
    try:
        if not prepared["runnable"]:
            raise GraderRefusal("; ".join(prepared["refusals"]))
        staged_task, solution = _stage_inputs(prepared, output)
        with contextlib.ExitStack() as stack:
            if backend in {"docker", "auto"}:
                # One native environment at a time, including separate verifiers;
                # retain the existing host-wide FIFO rather than private slots.
                slot = contextlib.ExitStack()
                try:
                    slot.enter_context(
                        docker_slot(
                            label=f"upstream-grader/{prepared['task']['id']}",
                            slots=1,
                            timeout=timeout_seconds,
                        )
                    )
                    admission = _admit_docker(prepared)
                except (OSError, ValueError, subprocess.SubprocessError, TimeoutError) as exc:
                    slot.close()
                    if backend == "docker":
                        raise GraderRefusal(f"Docker admission refused: {exc}") from exc
                    admission = {
                        "refusal": f"{type(exc).__name__}: {exc}",
                        "fallback_phase": "pre-execution admission only",
                    }
                else:
                    stack.enter_context(slot)
                    chosen = "docker"
                    chosen_platform = admission["platform"]
            if chosen is None:
                _modal_admission(
                    prepared,
                    approval=approval,
                    max_usd=max_usd,
                    spend_ledger=Path(spend_ledger).expanduser().resolve()
                    if spend_ledger is not None
                    else None,
                    modal_app_name=modal_app_name,
                    billing_tag=billing_tag,
                )
                assert spend_ledger is not None and approval is not None and max_usd is not None
                chosen = "modal"
                chosen_platform = "linux/amd64"
                spend = _reserve_spend(
                    Path(spend_ledger).expanduser().resolve(),
                    approval=approval,
                    cap=float(max_usd),
                    reservation=prepared["modal_reservation"],
                    identity={
                        "run_id": output.name,
                        "output_dir": str(output),
                        "task_digest": prepared["task"]["digest"],
                        "input_digest": prepared["input"]["digest"],
                        "modal_app_name": modal_app_name,
                        "billing_tag": billing_tag,
                    },
                )
            config = _trial_config(
                prepared,
                staged_task=staged_task,
                solution=solution,
                trial_dir=output / "trial",
                backend=chosen,
                platform=str(chosen_platform),
                modal_app_name=modal_app_name,
                billing_tag=billing_tag,
            )
            active = _Execution(prepared)
            status = "error"
            payload = asyncio.run(_execute_native(config, active))
    except KeyboardInterrupt:
        status, error = "cancelled", "execution cancelled; native cleanup was requested"
    except TimeoutError:
        status, error = (
            "timeout",
            f"fresh native trial exceeded the {timeout_seconds}-second execution watchdog",
        )
    except (GraderRefusal, SpendCapReached) as exc:
        status, error = "refused", str(exc)
    except Exception as exc:
        status, error = "error", f"{type(exc).__name__}: {exc}"
    if not payload and _safe_evidence_file(output / "trial" / "result.json", output / "trial"):
        try:
            recovered = _read_report_json(output / "trial" / "result.json")
            if isinstance(recovered, dict):
                payload = recovered
            else:
                status, error = "error", "native result.json was not an object"
        except (OSError, ValueError):
            pass
    result = _collect_execution(
        prepared,
        output,
        payload,
        backend=chosen,
        platform=chosen_platform,
        error=error,
        status=status,
        active=active,
        spend=spend,
        admission=admission,
    )
    if spend is not None and active is not None:
        try:
            result["spend"] = _reconcile_spend(
                spend, active, measured=result["status"] == "measured"
            )
        except (OSError, ValueError) as exc:
            # Budget bookkeeping uncertainty does not erase a genuinely native
            # completed grade; no unused reserve was released in this case.
            result["spend"] = {
                **spend,
                "reconciliation": {"status": "retained-full-reservation", "error": str(exc)},
            }
    (output / "upstream-grader.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", nargs="?")
    parser.add_argument(
        "--request", help="JSON request file, or - for stdin; flags override request fields"
    )
    candidate = parser.add_mutually_exclusive_group()
    candidate.add_argument("--solution-dir")
    candidate.add_argument(
        "--container-snapshot", help="tar of target-directory contents, NOT a Docker/Modal image"
    )
    candidate.add_argument("--oracle", action="store_true", default=None)
    candidate.add_argument("--blank", action="store_true", default=None)
    parser.add_argument("--snapshot-target")
    parser.add_argument("--snapshot-platform", choices=sorted(_PLATFORMS))
    parser.add_argument("--backend", choices=("auto", "docker", "modal"))
    parser.add_argument("--platform", choices=sorted(_PLATFORMS))
    parser.add_argument("--timeout-seconds", type=int)
    parser.add_argument("--plan", action="store_true", default=None)
    parser.add_argument("--max-usd", type=float)
    parser.add_argument("--approval")
    parser.add_argument("--spend-ledger")
    parser.add_argument("--modal-app-name")
    parser.add_argument("--billing-tag")
    parser.add_argument("--output-dir", help="new immutable native evidence directory")
    parser.add_argument("--output", help="also write the JSON response to a NEW file")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output_destination: Path | None = None
    try:
        request: dict[str, Any] = {}
        if args.request:
            raw = (
                sys.stdin.read()
                if args.request == "-"
                else Path(args.request).read_text(encoding="utf-8")
            )
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise GraderRefusal("JSON request must be an object")
        allowed = {
            "task",
            "solution_dir",
            "container_snapshot",
            "snapshot_target",
            "snapshot_platform",
            "oracle",
            "blank",
            "backend",
            "platform",
            "timeout_seconds",
            "plan",
            "max_usd",
            "approval",
            "spend_ledger",
            "modal_app_name",
            "billing_tag",
            "output_dir",
        }
        unknown = set(request) - allowed
        if unknown:
            raise GraderRefusal("unknown JSON request fields: " + ", ".join(sorted(unknown)))
        for name in allowed:
            value = getattr(args, name, None)
            if value is not None:
                request[name] = value
        snapshot = request.get("container_snapshot")
        if isinstance(snapshot, dict):
            if set(snapshot) != {"archive", "target", "platform"}:
                raise GraderRefusal(
                    "container_snapshot object needs exactly archive, target, platform"
                )
            request["container_snapshot"] = ContainerSnapshot(**snapshot)
        elif snapshot is not None:
            target = request.pop("snapshot_target", None)
            snapshot_platform = request.pop("snapshot_platform", None)
            if not target or not snapshot_platform:
                raise GraderRefusal(
                    "snapshot archive requires --snapshot-target and --snapshot-platform"
                )
            request["container_snapshot"] = ContainerSnapshot(snapshot, target, snapshot_platform)
        if "snapshot_target" in request or "snapshot_platform" in request:
            raise GraderRefusal(
                "snapshot target/platform must accompany an archive path, not another input mode"
            )
        if not request.get("task"):
            raise GraderRefusal("task directory is required")
        if args.output:
            destination = Path(args.output).expanduser().resolve()
            immutable = [Path(request["task"]).expanduser().resolve()]
            if request.get("solution_dir"):
                immutable.append(Path(request["solution_dir"]).expanduser().resolve())
            candidate = request.get("container_snapshot")
            if isinstance(candidate, ContainerSnapshot):
                immutable.append(Path(candidate.archive).expanduser().resolve())
            if request.get("output_dir"):
                immutable.append(Path(request["output_dir"]).expanduser().resolve() / "inputs")
            if any(destination == path or destination.is_relative_to(path) for path in immutable):
                raise GraderRefusal("JSON output cannot mutate an original or frozen input")
            if (
                request.get("spend_ledger")
                and destination == Path(request["spend_ledger"]).expanduser().resolve()
            ):
                raise GraderRefusal("JSON output cannot replace the campaign spend ledger")
            if destination.exists():
                raise GraderRefusal("JSON output already exists; refusing to overwrite evidence")
            output_destination = destination
        # Native SDK diagnostics must not corrupt the single JSON stdout value.
        with contextlib.redirect_stdout(sys.stderr):
            result = run_upstream_grader(**request)
        code = (
            2
            if result.get("status") == "refused" or result.get("runnable") is False
            else 130
            if result.get("status") == "cancelled"
            else 1
            if result.get("error")
            else 0
        )
    except (OSError, ValueError, TypeError) as exc:
        result = {
            "schema": "evallab.upstream_grader.result/v1",
            "status": "refused",
            "reward": None,
            "error": f"{type(exc).__name__}: {exc}",
            "executed": False,
        }
        code = 2
    serialized = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    sys.stdout.write(serialized)
    if output_destination is not None:
        try:
            with output_destination.open("x", encoding="utf-8") as stream:
                stream.write(serialized)
        except OSError as exc:
            print(f"cannot write JSON output: {exc}", file=sys.stderr)
            return 2
    return code


if __name__ == "__main__":
    # Harbor resolves environment classes by the canonical module import path;
    # its ContextVar must be the same one used by the CLI's execution lifecycle.
    from importlib import import_module

    raise SystemExit(import_module("evallab.upstream_grader").main())
