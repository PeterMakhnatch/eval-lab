"""Additional-test regrades of recorded passing patches, through native Harbor.

Suites are immutable extraction products, not a new task registry. This module
stages a verifier per source patch and calls the existing trial regrade API
sequentially. A grouped receipt is not fabricated into a native Harbor job.
Planning reads files only; execution requires preinstalled local Docker images.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from evallab import heldout_verifier
from evallab.benchmark_program_contracts import (
    compute_prefixed_sha256,
    validate_safe_relative_path,
)
from evallab.heldout_tests import HeldoutSuite, load_suite
from evallab.regrade import (
    RegradeJobPlan,
    RegradeJobPlanTrial,
    RegradeJobReceiptV1,
    RegradeJobVerdict,
    RegradeReceiptV1,
    RegradeRefusalCode,
    RegradeVerdict,
    _harbor_version_of,
    _is_multi_step_source,
    _load_json,
    _load_json_toml,
    _refused_job_trial,
    _resolve_job_task_dirs,
    _source_job_trials,
    _source_preflight,
    _trial_task_name,
    _validate_regrade_destination,
    _write_job_receipts,
    default_regrade_job_name,
    read_reward_observation,
    regrade_trial,
)
from evallab.schemas import ContractModel


class HeldoutReplayTask(ContractModel):
    task_name: str
    suite: str
    framework: Literal["pytest", "unittest"]
    command: list[str] = Field(min_length=1)
    env: dict[str, str] = Field(default_factory=dict)
    bootstrap: str | None = None
    timeout_sec: int = Field(default=1800, ge=1, le=3600)


class HeldoutReplayBundle(ContractModel):
    schema_version: Literal["heldout-regrade/v1"]
    tasks: list[HeldoutReplayTask]
    cohort_sha256: str | None = None


@dataclass(frozen=True)
class _PreparedReplay:
    source: Path
    task: Path
    suite: HeldoutSuite
    replay: HeldoutReplayTask
    patch: bytes
    instruction: bytes
    bootstrap: bytes | None
    cpus: int
    memory_mb: int
    bindings: dict[Path, str]


def _read_bound(path: Path, bindings: dict[Path, str]) -> bytes:
    data = path.read_bytes()
    bindings[path] = compute_prefixed_sha256(data)
    return data


def _bundle_file(root: Path, relative: str) -> Path:
    path = root / validate_safe_relative_path(relative)
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"bundle file is missing or escapes its directory: {relative}")
    return path


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"invalid CPU-only verifier {name}")
    return value


def _prepare_trial(
    source: Path,
    task: Path,
    replay: HeldoutReplayTask,
    bundle: Path,
    bundle_digest: str,
) -> _PreparedReplay:
    bindings = {bundle: bundle_digest}
    suite_path = _bundle_file(bundle.parent, replay.suite)
    _read_bound(suite_path, bindings)
    suite = load_suite(suite_path)
    if suite.status != "ready" or not suite.tests:
        raise ValueError(f"held-out suite is not ready: {suite.status}")
    if suite.task_name != replay.task_name or _trial_task_name(source) != suite.task_name:
        raise ValueError("held-out suite does not match the recorded task")
    config = _load_json_toml(task / "task.toml")
    if config is None or config.get("steps"):
        raise ValueError("held-out replay requires a readable single-step task")
    _read_bound(task / "task.toml", bindings)
    environment = config.get("environment")
    if not isinstance(environment, dict):
        raise ValueError("source task has no environment")
    if environment.get("docker_image") != suite.image:
        raise ValueError("suite image differs from the source task image")
    if environment.get("workdir") != suite.workdir:
        raise ValueError("suite workspace differs from the source task workspace")
    if environment.get("gpus") not in (None, 0) or environment.get("tpu") is not None:
        raise ValueError("held-out regrade is CPU-only; accelerator tasks are not eligible")
    if environment.get("os", "linux") != "linux":
        raise ValueError("held-out offline regrade requires a Linux task image")
    image_parts = suite.image.rsplit("@sha256:", 1)
    if len(image_parts) != 2 or len(image_parts[1]) != 64 or any(
        char not in "0123456789abcdef" for char in image_parts[1]
    ):
        raise ValueError("held-out replay requires an immutable source image digest")
    hidden_patch = _read_bound(task / "tests" / "test.patch", bindings)
    if hashlib.sha256(hidden_patch).hexdigest() != suite.hidden_patch_sha256:
        raise ValueError("held-out subtraction used different hidden-test bytes")
    patch = _read_bound(source / "verifier" / "agent.diff", bindings)
    patch.decode("utf-8")
    if any(line.startswith(b"Binary files ") for line in patch.splitlines()):
        raise ValueError("recorded diff contains binary placeholders, not reconstructible bytes")
    for path in (source / "result.json", source / "artifacts" / "manifest.json"):
        _read_bound(path, bindings)
    trajectory = source / "agent" / "trajectory.json"
    if trajectory.is_file():
        _read_bound(trajectory, bindings)
    result = _load_json(source / "result.json")
    if not isinstance(result, dict) or result.get("exception_info") is not None:
        raise ValueError("source trial has an exception or unreadable result")
    instruction = _read_bound(task / "instruction.md", bindings)
    bootstrap = (
        _read_bound(_bundle_file(bundle.parent, replay.bootstrap), bindings)
        if replay.bootstrap is not None
        else None
    )
    if replay.bootstrap is not None and replay.framework != "unittest":
        raise ValueError("bootstrap is supported only by the unittest adapter")
    if any(not arg or "\x00" in arg for arg in replay.command):
        raise ValueError("test command contains an empty or invalid argument")
    if any("\x00" in key + value or "=" in key or not key for key, value in replay.env.items()):
        raise ValueError("invalid explicit test environment")
    return _PreparedReplay(
        source=source,
        task=task,
        suite=suite,
        replay=replay,
        patch=patch,
        instruction=instruction,
        bootstrap=bootstrap,
        cpus=_positive_integer(environment.get("cpus", 2), "cpus"),
        memory_mb=_positive_integer(environment.get("memory_mb", 8192), "memory_mb"),
        bindings=bindings,
    )


def _prepare_job(
    *, job_dir: Path, held_out: Path, task_dir: Path | None, jobs_dir: Path, name: str | None
) -> tuple[RegradeJobPlan, dict[str, _PreparedReplay]]:
    job_dir, held_out, jobs_dir = Path(job_dir), Path(held_out), Path(jobs_dir)
    job_name = name or default_regrade_job_name(job_dir)
    _validate_regrade_destination(job_dir, jobs_dir, job_name)
    base: dict[str, Any] = {
        "source_job_dir": str(job_dir),
        "source_harbor_version": _harbor_version_of(job_dir) if job_dir.is_dir() else None,
        "job_name": job_name,
        "jobs_dir": str(jobs_dir),
        "job_dir": str(jobs_dir / job_name),
        "mode": "held-out",
    }
    if not job_dir.is_dir():
        return RegradeJobPlan(**base, refusals=[RegradeRefusalCode.SOURCE_JOB_MISSING]), {}
    sources = _source_job_trials(job_dir)
    passing = [
        source for source in sources
        if (observation := read_reward_observation(source)) is not None
        and observation.primary == 1.0
    ]
    base["skipped_trials"] = [str(source) for source in sources if source not in passing]
    if not passing:
        return RegradeJobPlan(
            **base, refusals=[RegradeRefusalCode.SOURCE_NO_PASSING_TRIALS]
        ), {}
    try:
        bundle_bytes = held_out.read_bytes()
        bundle_digest = compute_prefixed_sha256(bundle_bytes)
        bundle = HeldoutReplayBundle.model_validate_json(bundle_bytes)
        entries = {entry.task_name: entry for entry in bundle.tasks}
        if len(entries) != len(bundle.tasks):
            raise ValueError("duplicate task names in held-out bundle")
    except (OSError, ValueError):
        return RegradeJobPlan(
            **base, refusals=[RegradeRefusalCode.HELD_OUT_BUNDLE_INVALID]
        ), {}
    base["heldout_bundle_digest"] = bundle_digest
    ordered, by_name, task_refusals = _resolve_job_task_dirs(
        source_trials=passing, task_dir=task_dir, require_separate=False
    )
    trials: list[RegradeJobPlanTrial] = []
    prepared: dict[str, _PreparedReplay] = {}
    for source in passing:
        task_name = _trial_task_name(source)
        task = by_name.get(task_name) if task_name else None
        entry = entries.get(task_name) if task_name else None
        refusals = _source_preflight(source)
        details: list[str] = []
        if _is_multi_step_source(source):
            refusals.append(RegradeRefusalCode.MULTI_STEP_TASK)
        if task is None:
            refusals.extend(task_refusals or [RegradeRefusalCode.TASK_UNRESOLVED])
        if entry is None:
            refusals.append(RegradeRefusalCode.HELD_OUT_SUITE_UNAVAILABLE)
        if not (source / "verifier" / "agent.diff").is_file():
            refusals.append(RegradeRefusalCode.SOURCE_PATCH_UNAVAILABLE)
        if not refusals and task is not None and entry is not None:
            try:
                candidate = _prepare_trial(source, task, entry, held_out, bundle_digest)
                output = Path(base["job_dir"]).resolve()
                if output.is_relative_to(task.resolve()) or task.resolve().is_relative_to(output):
                    raise ValueError("regrade output overlaps immutable task input")
                if held_out.resolve().is_relative_to(output):
                    raise ValueError("regrade output overlaps held-out bundle input")
                prepared[str(source)] = candidate
            except (OSError, ValueError) as exc:
                refusals.append(RegradeRefusalCode.HELD_OUT_SOURCE_MISMATCH)
                details.append(str(exc))
        candidate = prepared.get(str(source))
        trials.append(RegradeJobPlanTrial(
            source_trial_dir=str(source), task_name=task_name,
            task_dir=str(task) if task else None, eligible=not refusals,
            refusals=list(dict.fromkeys(refusals)), details=details,
            input_digests={str(path): digest for path, digest in candidate.bindings.items()}
            if candidate else {},
        ))
    return RegradeJobPlan(
        **base, task_dirs=[str(path) for path in ordered], trials=trials,
        refusals=list(dict.fromkeys(code for trial in trials for code in trial.refusals)),
        runnable=any(trial.eligible for trial in trials),
    ), prepared


def plan_heldout_job(
    *, job_dir: Path, held_out: Path, jobs_dir: Path,
    task_dir: Path | None = None, name: str | None = None,
) -> RegradeJobPlan:
    """Preview input eligibility without staging files, probing Docker or executing code."""
    return _prepare_job(
        job_dir=job_dir, held_out=held_out, task_dir=task_dir, jobs_dir=jobs_dir, name=name
    )[0]


def _docker_read(command: list[str], runner: Any) -> str:
    completed = runner(command, capture_output=True, text=True, check=False, timeout=15)
    if completed.returncode != 0:
        raise ValueError("local Docker prerequisite is unavailable: " + " ".join(command))
    return str(completed.stdout).strip()


def _offline_runtime(images: set[str], runner: Any) -> dict[str, Any]:
    """Read-only cache checks; never build/pull or invoke Harbor's container probe."""
    from harbor.environments.docker.docker import DockerEnvironment  # ty: ignore[unresolved-import]
    from harbor.environments.docker.utils import (  # ty: ignore[unresolved-import]
        _compute_image_name,
    )
    from harbor.utils.container_cache import (  # ty: ignore[unresolved-import]
        docker_build_context_hash,
    )

    # DOCKER_CONTEXT takes precedence over DOCKER_HOST in the Docker CLI.
    host = os.environ.get("DOCKER_HOST")
    if os.environ.get("DOCKER_CONTEXT") or not host:
        host = json.loads(_docker_read(
            ["docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"],
            runner,
        ))
    if not isinstance(host, str) or not host.startswith("unix://"):
        raise ValueError("held-out regrade requires a local Unix-socket Docker endpoint")
    platform = _docker_read(
        ["docker", "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"], runner
    )
    if platform not in {"linux/amd64", "linux/arm64"}:
        raise ValueError("held-out regrade requires a local Linux Docker engine")
    context = DockerEnvironment._EGRESS_CONTROL_SIDECAR_CONTEXT_PATH
    sidecar = _compute_image_name(
        DockerEnvironment._EGRESS_CONTROL_SIDECAR_DOCKER_NAME,
        docker_build_context_hash(
            context=context, dockerfile_path=context / "Dockerfile", build_args={},
            platform=platform,
        ),
    )
    required = sorted(images | {sidecar, DockerEnvironment._EGRESS_CONTROL_KERNEL_PROBE_IMAGE})
    inspected = json.loads(_docker_read(["docker", "image", "inspect", *required], runner))
    if not isinstance(inspected, list) or len(inspected) != len(required):
        raise ValueError("incomplete local Docker image inventory")
    image_ids: dict[str, str] = {}
    for reference, image in zip(required, inspected, strict=True):
        if not isinstance(image, dict) or image.get("Os") != "linux":
            raise ValueError("non-Linux or missing cached verifier image")
        identity = image.get("Id")
        if not isinstance(identity, str) or not identity.startswith("sha256:"):
            raise ValueError("cached verifier image has no immutable identity")
        image_ids[reference] = identity
    return {"docker_endpoint": host, "platform": platform, "images": image_ids,
            "sidecar": sidecar, "network": "no-network", "pull_policy": "never"}


def _task_files(candidate: _PreparedReplay, runtime: dict[str, Any]) -> dict[str, bytes]:
    replay, suite = candidate.replay, candidate.suite
    quote = json.dumps
    task_toml = (
        'schema_version = "1.4"\n\n[task]\n'
        f'name = {quote(suite.task_name)}\n\n'
        '[verifier]\nenvironment_mode = "separate"\n'
        'network_mode = "no-network"\nuser = "root"\n'
        f'timeout_sec = {replay.timeout_sec + 60}\n\n'
        '[environment]\n'
        f'docker_image = {quote(suite.image)}\nworkdir = {quote(suite.workdir)}\n'
        f'cpus = {candidate.cpus}\nmemory_mb = {candidate.memory_mb}\n'
        'gpus = 0\nnetwork_mode = "no-network"\nos = "linux"\n'
    )
    # Explicit networking is deliberate and trusted here: no source-task Compose
    # files are inherited. Native resource/log mounts still come from Harbor.
    compose = {
        "services": {
            "main": {
                "image": suite.image, "pull_policy": "never",
                "network_mode": "none", "platform": "linux/amd64",
            },
            "harbor-docker-egress-control-sidecar": {"pull_policy": "never"},
        }
    }
    config = {
        "schema_version": "heldout-run/v1", "suite": suite.model_dump(mode="json"),
        "agent_patch": {"path": "agent.patch", "sha256": hashlib.sha256(candidate.patch).hexdigest()},
        "framework": replay.framework, "command": replay.command, "env": replay.env,
        "bootstrap": "bootstrap.py" if candidate.bootstrap is not None else None,
        "timeout_sec": replay.timeout_sec,
    }
    bindings = {str(path): digest for path, digest in candidate.bindings.items()}
    files = {
        "task.toml": task_toml.encode(),
        "instruction.md": candidate.instruction,
        "environment/docker-compose.yaml": json.dumps(compose, indent=2).encode(),
        "tests/test.sh": (
            b'#!/bin/sh\nset -eu\nexec python3 /tests/heldout_verifier.py '
            b'--config /tests/heldout-run.json --logs /logs/verifier\n'
        ),
        "tests/heldout_verifier.py": Path(heldout_verifier.__file__).read_bytes(),
        "tests/heldout-run.json": json.dumps(config, sort_keys=True, indent=2).encode(),
        "tests/agent.patch": candidate.patch,
        "tests/input-bindings.json": json.dumps(
            {
                "source_trial": str(candidate.source), "digests": bindings, "runtime": runtime,
                "effective_task_config_sha256": hashlib.sha256(task_toml.encode()).hexdigest(),
            },
            sort_keys=True, indent=2,
        ).encode(),
    }
    if candidate.bootstrap is not None:
        files["tests/bootstrap.py"] = candidate.bootstrap
    return files


def _inputs_unchanged(candidate: _PreparedReplay) -> bool:
    try:
        return all(
            compute_prefixed_sha256(path.read_bytes()) == digest
            for path, digest in candidate.bindings.items()
        )
    except OSError:
        return False


def _heldout_observation_valid(receipt: RegradeReceiptV1) -> bool:
    if receipt.refused or receipt.regraded is None or receipt.regrade_trial_dir is None:
        return False
    rewards = receipt.regraded.rewards
    if set(rewards) != {"holdout_pass"} or rewards["holdout_pass"] not in (0.0, 1.0):
        return False
    output = Path(receipt.regrade_trial_dir)
    result = _load_json(output / "result.json")
    report = _load_json(output / "verifier" / "heldout-result.json")
    if not isinstance(result, dict) or result.get("exception_info") is not None:
        return False
    if not isinstance(report, dict) or report.get("schema_version") != "heldout-result/v1":
        return False
    value = report.get("holdout_pass")
    counts = report.get("counts")
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return False
    if not isinstance(counts, dict) or any(
        isinstance(counts.get(key), bool) or not isinstance(counts.get(key), int)
        or counts[key] < 0 for key in ("tests", "failures", "errors", "skipped")
    ):
        return False
    if counts["tests"] < 1 or counts["errors"] or counts["skipped"]:
        return False
    expected = 0.0 if counts["failures"] else 1.0
    return (
        value == rewards["holdout_pass"] == expected
        and report.get("outcome") == ("passed" if expected else "failed")
    )


def regrade_heldout_job(
    *, job_dir: Path, held_out: Path, jobs_dir: Path, task_dir: Path | None = None,
    name: str | None = None, runner: Any = subprocess.run, write_receipt: bool = True,
) -> RegradeJobReceiptV1:
    """Run eligible recorded passes sequentially using native trial regrade.

    This launches local verifier containers. A caller's research approval is
    distinct from $0 model cost; --dry-run must use plan_heldout_job instead.
    """
    plan, prepared = _prepare_job(
        job_dir=job_dir, held_out=held_out, task_dir=task_dir, jobs_dir=jobs_dir, name=name
    )
    base = {
        key: getattr(plan, key) for key in (
            "source_job_dir", "source_harbor_version", "job_name", "jobs_dir", "job_dir",
            "mode", "heldout_bundle_digest", "skipped_trials", "environment",
        )
    }
    if not plan.runnable:
        return RegradeJobReceiptV1(
            **base, verdict=RegradeJobVerdict.REFUSED, refusals=plan.refusals
        )
    try:
        runtime = _offline_runtime({item.suite.image for item in prepared.values()}, runner)
    except (ImportError, OSError, ValueError, subprocess.SubprocessError):
        return RegradeJobReceiptV1(
            **base, verdict=RegradeJobVerdict.REFUSED,
            refusals=[RegradeRefusalCode.HELD_OUT_RUNTIME_UNAVAILABLE],
        )
    output = Path(plan.job_dir)
    output.mkdir(parents=True, exist_ok=False)
    (output / "heldout-plan.json").write_text(
        plan.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    entries: list[RegradeReceiptV1] = []
    staged_tasks: list[str] = []
    for trial in plan.trials:
        source = Path(trial.source_trial_dir)
        # A runnable resolution covers every passing source task.
        assert trial.task_dir is not None
        original_task = Path(trial.task_dir)
        candidate = prepared.get(str(source))
        if candidate is None:
            entries.append(_refused_job_trial(
                source=source, task_dir=original_task, refusals=trial.refusals,
                new_trial_dir=None,
            ))
            continue
        if not _inputs_unchanged(candidate):
            entries.append(_refused_job_trial(
                source=source, task_dir=original_task,
                refusals=[RegradeRefusalCode.HELD_OUT_INPUT_CHANGED],
                new_trial_dir=None,
            ))
            continue
        task = output / ".heldout-tasks" / source.name
        for relative, content in _task_files(candidate, runtime).items():
            destination = task / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        (task / "tests" / "test.sh").chmod(0o755)
        staged_tasks.append(str(task))
        receipt = regrade_trial(
            trial_dir=source, task_dir=task, trials_dir=output,
            runner=runner, write_receipt=False,
        )
        reason = None
        if not _inputs_unchanged(candidate):
            reason = RegradeRefusalCode.HELD_OUT_INPUT_CHANGED
        elif not receipt.refused and not _heldout_observation_valid(receipt):
            reason = RegradeRefusalCode.REGRADE_REWARD_ABSENT
        if reason is not None:
            receipt = receipt.model_copy(update={
                "verdict": RegradeVerdict.REFUSED, "refusals": [reason],
                "regraded": None, "reward_delta": {},
            })
        entries.append(receipt)
    compared = sum(not entry.refused for entry in entries)
    verdict = (
        RegradeJobVerdict.COMPLETE if compared == len(entries) and entries
        else RegradeJobVerdict.PARTIAL if compared else RegradeJobVerdict.REFUSED
    )
    result = RegradeJobReceiptV1(
        **base, task_dirs=staged_tasks, trials=entries, verdict=verdict,
        refusals=list(dict.fromkeys(code for entry in entries for code in entry.refusals)),
    )
    if write_receipt:
        _write_job_receipts(result, output)
    return result
