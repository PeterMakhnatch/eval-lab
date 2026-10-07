"""Campaign-scoped concurrency and native-evidence smoke qualification.

This extends the existing once-approved campaign; it neither approves work nor
starts a server. A qualification removes only the serialized full-trial smoke.
Ordinary admission, readiness, provider capacity and budget gates still apply.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evallab.execution_contracts import (
    BOUNDED_DAYTONA_ENVIRONMENT_IMPORT_PATH,
    LOCKED_DOCKER_ENVIRONMENT_IMPORT_PATH,
    RunRequest,
    resolve_harbor_agent,
)
from evallab.results import load_trial
from evallab.setup_fingerprint import lock_setup_fingerprint

_EVIDENCE_FILES = ("config.json", "lock.json", "result.json", "egress-lock.json")
_BACKEND_IMPORTS = {
    "daytona": BOUNDED_DAYTONA_ENVIRONMENT_IMPORT_PATH,
    "docker": LOCKED_DOCKER_ENVIRONMENT_IMPORT_PATH,
}


class CampaignSetupQualification(BaseModel):
    """An immutable successful native trial, not a caller's qualified boolean."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    trial_dir: str = Field(min_length=1)
    evidence_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class CampaignExecutionPolicy(BaseModel):
    """Digest-covered execution limits; absent on historical campaigns."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    max_concurrent_trials: int = Field(ge=1, le=40, strict=True)
    model_host: Literal["modal", "runpod"]
    qualification: CampaignSetupQualification | None = None

    @model_validator(mode="after")
    def _qualification_needs_recorded_host(self) -> CampaignExecutionPolicy:
        # Native trial evidence does not record the model host, so it cannot
        # prove a Runpod server; those campaigns keep the serialized smoke.
        if self.qualification is not None and self.model_host != "modal":
            raise ValueError("setup qualification is only recorded for the Modal model host")
        return self


def qualification_evidence_digest(trial_dir: Path) -> str:
    """Hash the exact native evidence bytes, including applied containment."""
    digest = hashlib.sha256()
    for name in _EVIDENCE_FILES:
        path = trial_dir / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"qualification requires a regular native {name}")
        content = path.read_bytes()
        digest.update(name.encode() + b"\0")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return "sha256:" + digest.hexdigest()


def _read_qualification(
    qualification: CampaignSetupQualification, *, repo_root: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    path = Path(qualification.trial_dir)
    if not path.is_absolute():
        path = repo_root / path
    if qualification_evidence_digest(path) != qualification.evidence_digest:
        raise ValueError("qualification evidence changed or does not match its digest")
    trial = load_trial(path)
    result = trial.result
    if not result.get("finished_at") or result.get("exception_info") is not None:
        raise ValueError("qualification requires a completed trial without an exception")
    verifier = result.get("verifier_result")
    reward = (verifier.get("rewards") or {}).get("reward") if isinstance(verifier, dict) else None
    if isinstance(reward, bool) or not isinstance(reward, int | float) or not math.isfinite(reward):
        raise ValueError("qualification requires a finite native verifier grade")
    lock = trial.lock
    if not isinstance(lock, dict) or lock.get("install_only") is not False:
        raise ValueError("qualification requires an actual native trial lock")
    agent = lock.get("agent")
    environment = lock.get("environment")
    if not isinstance(agent, dict) or not isinstance(environment, dict):
        raise ValueError("qualification native agent/environment identity is missing")
    if not isinstance(agent.get("env"), dict):
        raise ValueError("qualification has no lock-covered setup fingerprint")
    fingerprint = agent["env"].get("EVALLAB_SETUP_FINGERPRINT")
    if not isinstance(fingerprint, str):
        raise ValueError("qualification has no lock-covered setup fingerprint")
    setup = json.loads(fingerprint)
    if not isinstance(setup, dict):
        raise ValueError("qualification setup fingerprint must be an object")
    for record in (trial.config, result.get("config")):
        if not isinstance(record, dict):
            raise ValueError("qualification config/result disagree with the native lock")
        actual_agent = record.get("agent") or {}
        actual_env = record.get("environment") or {}
        if (
            actual_agent.get("name") != agent.get("name")
            or actual_agent.get("model_name") != agent.get("model_name")
            or (actual_agent.get("env") or {}).get("EVALLAB_SETUP_FINGERPRINT") != fingerprint
            or actual_env.get("import_path") != environment.get("import_path")
            or (actual_env.get("kwargs") or {}).get("egress_lock") is not True
        ):
            raise ValueError("qualification config/result disagree with the native lock")
    egress = json.loads((path / "egress-lock.json").read_text())
    if not isinstance(egress, dict) or egress.get("applied") is not True or egress.get("error"):
        raise ValueError("qualification has no applied egress lock")
    if (environment.get("kwargs") or {}).get("egress_lock") is not True:
        raise ValueError("qualification did not request the backend lock")
    return lock, setup


def validate_qualification(qualification: CampaignSetupQualification, *, repo_root: Path) -> None:
    """Check the source evidence before freezing or approving a campaign."""
    _read_qualification(qualification, repo_root=repo_root)


def qualification_matches_request(
    qualification: CampaignSetupQualification, request: RunRequest, *, repo_root: Path
) -> bool:
    """Fail closed on unavailable evidence or any task-independent setup drift."""
    try:
        lock, recorded_setup = _read_qualification(qualification, repo_root=repo_root)
        expected_setup = json.loads(lock_setup_fingerprint(request, repo_root=repo_root))
        return (
            request.egress_lock is True
            and recorded_setup == expected_setup
            and lock["agent"].get("name") == resolve_harbor_agent(request.agent, request.model)
            and lock["agent"].get("model_name") == request.model
            and lock["environment"].get("import_path") == _BACKEND_IMPORTS.get(request.environment)
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def docker_available_resources() -> tuple[float, int]:
    """Observe the selected Linux daemon, subtracting bounded existing containers.

    The cheap engine uses one dedicated Docker host and one queue owner. Unknown
    or unlimited competing containers fail closed; this is not a cross-host
    reservation service. Two CPUs and two GiB remain for the trusted controller.
    """

    def query(*args: str) -> str:
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, check=True, timeout=10
        ).stdout

    info = json.loads(query("info", "--format", "{{json .}}"))
    if info.get("OSType") != "linux":
        raise ValueError("locked Docker requires a Linux daemon")
    cpus = info.get("NCPU")
    memory_bytes = info.get("MemTotal")
    if (
        isinstance(cpus, bool)
        or not isinstance(cpus, int)
        or cpus < 1
        or isinstance(memory_bytes, bool)
        or not isinstance(memory_bytes, int)
        or memory_bytes < 1
    ):
        raise ValueError("Docker daemon resource capacity is unavailable")
    available_cpus = float(cpus - 2)
    available_memory_mb = memory_bytes // (1024 * 1024) - 2048
    ids = query("ps", "--quiet").split()
    if ids:
        for container in json.loads(query("inspect", *ids)):
            host = container.get("HostConfig") or {}
            cpu_nanos = host.get("NanoCpus") or 0
            if not cpu_nanos:
                period, quota = host.get("CpuPeriod") or 0, host.get("CpuQuota") or 0
                cpu_nanos = quota * 1_000_000_000 / period if period > 0 and quota > 0 else 0
            memory_limit = host.get("Memory") or 0
            if cpu_nanos <= 0 or memory_limit <= 0:
                raise ValueError("an existing Docker container has unbounded CPU or memory")
            available_cpus -= cpu_nanos / 1_000_000_000
            available_memory_mb -= math.ceil(memory_limit / (1024 * 1024))
    return max(0.0, available_cpus), max(0, available_memory_mb)


def docker_task_resources(task_dir: Path) -> tuple[float, int]:
    """Use declared task limits unchanged, never a cheaper assumed 4-GiB size."""
    environment = tomllib.loads((task_dir / "task.toml").read_text()).get("environment") or {}
    cpus, memory_mb = environment.get("cpus"), environment.get("memory_mb")
    if (
        isinstance(cpus, bool)
        or not isinstance(cpus, int | float)
        or not math.isfinite(cpus)
        or cpus <= 0
        or isinstance(memory_mb, bool)
        or not isinstance(memory_mb, int)
        or memory_mb < 1
    ):
        raise ValueError("locked Docker requires explicit finite task CPU and memory limits")
    return float(cpus), memory_mb
