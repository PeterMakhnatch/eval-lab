"""Zero-cost verifier re-scoring over recorded trials (`harbor trial regrade`).

Harbor can re-run a task's verifier against a *finished* trial's agent logs and
artifacts without invoking a model, in seconds, provided the task is single-step
and its verifier resolves to ``environment_mode = "separate"``. Every lab
generator already emits that mode and ``task_workbench`` rejects tasks that do
not, so the precondition holds across lab-authored families. Externally
imported packs frequently do not (terminal-bench 2.1 records
``verifier_environment_mode = "shared"``), so the precondition is checked, never
assumed.

Two capabilities come out of one mechanism:

1. **Verifier hardening.** Change ``tests/``, re-score the recorded trial, and
   read the reward delta at zero model cost. A cheat trial that used to score
   1.0 and now scores 0.0 is a hardened verifier, demonstrated on the exact
   trajectory that exploited it.
2. **Grader determinism.** Re-score with a *byte-identical* verifier. The reward
   must be identical. If it is not, the verifier is nondeterministic, which
   silently poisons every downstream contrast. Nobody probes for this because
   re-running trials is expensive; regrading is not.

The reward-integrity rule this module exists to enforce: **a regrade reward is
never the trial's reward.** A receipt records both, each bound to the bytes it
came from — the source trajectory digest and the verifier identity digest — so a
re-scoring can never be mistaken for, or silently substituted into, the original
observation. Harbor's own regrade never modifies the source trial; neither does
anything here.

Job-level regrading (`regrade_job`) extends the same machinery to whole Harbor
jobs via `harbor job regrade`, which Harbor derives as one regrade trial per
recorded source trial, matched by task name. The job receipt carries one
trial-level receipt per source trial — each with its own source/verifier
identity and old/new reward dimensions — plus the exact job command Harbor ran.
`plan_regrade_job` previews the same resolution without invoking anything, for
CLI dry-runs. Both refuse closed (typed reason codes, no silent skips) when the
source job is missing or empty, when verifier tasks cannot be resolved or do
not cover the job's tasks, or when a layout this module cannot verify
(multi-step sources) is present. Job regrades run on local Docker only; anything
else raises before any work starts, with no bypass flag. A *trial* regrade may
also run on Modal, and only under a :class:`CloudSpendGate` (a recorded approval
and a USD cap, charged before every execution) with the lab's pinned Harbor.
"""

from __future__ import annotations

import importlib.metadata
import json
import math
import re
import shutil
import subprocess
import threading
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import Field

from evallab.benchmark_program_contracts import canonical_json, compute_prefixed_sha256
from evallab.execution_backend import require_local_docker
from evallab.harbor_telemetry import harbor_child_environment
from evallab.schemas import ContractModel

#: Marker appended to generated regrade trial names. Regrade outputs are trials
#: in their own right; the suffix keeps them recognisable in a jobs tree without
#: parsing `config.source_trial`.
REGRADE_TRIAL_NAME_SUFFIX = "regrade"

#: The only verifier environment a job regrade may use, and the default for a
#: trial regrade. Regrade never invokes a model; the verifier runs inside
#: Harbor's Docker environment at `$0`.
LOCAL_REGRADE_ENVIRONMENTS = frozenset({"docker"})

#: Paid verifier environments a *trial* regrade may use, mapped to Harbor's
#: `--env` value. `harbor trial regrade` takes no `--environment-kwarg`, so the
#: class fixes the shared setup/task interpreter boundary and bounded sandbox
#: lifetime. Admitted only under a :class:`CloudSpendGate`.
CLOUD_REGRADE_ENVIRONMENTS: Mapping[str, str] = {
    "modal": "evallab.harbor_modal:RegradeModalEnvironment",
}

#: Job-level receipt filename, written at the new job root. The source job is
#: never written to.

#: Per-trial receipt filename inside a regrade output trial. A later
#: determinism probe reads it to learn which verifier digest scored the trial.
REGRADE_TRIAL_RECEIPT_FILENAME = "regrade-receipt.json"

#: The verifier's build inputs live here relative to the task directory. Harbor
#: builds the separate verifier image from this context, so its content is the
#: verifier's identity.
_VERIFIER_CONTEXT_DIR = "tests"

#: Ignored inside the verifier build context: caches are not verifier semantics.
_CONTEXT_IGNORED_DIRS = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache"})


class RegradeRefusalCode(StrEnum):
    """Fail-closed reasons a trial cannot be re-scored. Never a silent skip."""

    SOURCE_TRIAL_MISSING = "source_trial_missing"
    SOURCE_RESULT_UNREADABLE = "source_result_unreadable"
    SOURCE_AGENT_LOGS_MISSING = "source_agent_logs_missing"
    SOURCE_ARTIFACT_MANIFEST_MISSING = "source_artifact_manifest_missing"
    SOURCE_REWARD_ABSENT = "source_reward_absent"
    TASK_DIR_MISSING = "task_dir_missing"
    TASK_CONFIG_UNREADABLE = "task_config_unreadable"
    VERIFIER_NOT_ISOLATED = "verifier_not_isolated"
    VERIFIER_DISABLED = "verifier_disabled"
    VERIFIER_CONTEXT_MISSING = "verifier_context_missing"
    MULTI_STEP_TASK = "multi_step_task"
    HARBOR_INVOCATION_FAILED = "harbor_invocation_failed"
    REGRADE_RESULT_UNREADABLE = "regrade_result_unreadable"
    REGRADE_REWARD_ABSENT = "regrade_reward_absent"
    #: The source job directory does not exist.
    SOURCE_JOB_MISSING = "source_job_missing"
    #: The source job holds no trial with a result.json to re-score.
    SOURCE_JOB_EMPTY = "source_job_empty"
    #: task_dir was omitted and no trial's saved config resolves to a live task.
    TASK_UNRESOLVED = "task_unresolved"
    #: A given task_dir leaves source tasks without a verifier.
    TASK_COVERAGE_GAP = "task_coverage_gap"
    #: A single given task names nothing in the source job. Refusing beats
    #: silently scoring unrelated tasks.
    TASK_NAME_MISMATCH = "task_name_mismatch"
    #: Two task directories claim the same task name.
    TASK_AMBIGUOUS = "task_ambiguous"
    SOURCE_PATCH_UNAVAILABLE = "source_patch_unavailable"
    SOURCE_NO_PASSING_TRIALS = "source_no_passing_trials"
    HELD_OUT_BUNDLE_INVALID = "held_out_bundle_invalid"
    HELD_OUT_SUITE_UNAVAILABLE = "held_out_suite_unavailable"
    HELD_OUT_SOURCE_MISMATCH = "held_out_source_mismatch"
    HELD_OUT_RUNTIME_UNAVAILABLE = "held_out_runtime_unavailable"
    HELD_OUT_INPUT_CHANGED = "held_out_input_changed"
    #: A paid execution would take the run past its approved USD cap.
    SPEND_CAP_REACHED = "spend_cap_reached"


def modal_resource_blocker(package: Path) -> str | None:
    """Provider-free admission; an unpriced accelerator is never treated as a CPU."""
    config = tomllib.loads((package / "task.toml").read_text(encoding="utf-8"))
    environments = [
        config.get("environment") or {},
        (config.get("verifier") or {}).get("environment") or {},
    ]
    if any(env.get("gpus") or env.get("gpu") or env.get("tpu") for env in environments):
        return "Modal GPU/TPU resources have no approved price estimate"
    if any(str(env.get("os", "linux")).lower() != "linux" for env in environments):
        return "Modal script controls require Linux environments"
    if config.get("steps"):
        return "Modal script controls do not support declared task steps"
    if any(
        (package / directory / name).is_file()
        for directory in ("environment", "tests")
        for name in ("docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml")
    ):
        return "Modal does not support Compose services; no Docker fallback is permitted"
    return None


#: Estimate for short Modal CPU controls, not a provider price or billing hard kill.
#: October 17, 2026: 338 native allocations at 2 CPUs/8 GiB averaged $0.003223
#: in resource/lifecycle estimates cross-checked against the app's metered usage.
#: Two snapshot-create reservations give $0.005: >50% above that observed mean;
#: ordinary starts also reserve image preparation. Long tails/larger resources
#: need an explicit higher estimate. Logical replays have a separate authority.
MODAL_USD_PER_ALLOCATION_UNIT = 0.0025

#: Passed only to the admitted regrade subprocess; never installed in this process.
_REGRADE_MODAL_POLICY_ENV = "EVALLAB_REGRADE_MODAL_POLICY"


def _modal_environment_kwargs(
    modal_app_name: str | None, billing_tag: str | None
) -> dict[str, Any]:
    """Use the native grader's owned-app identifier rules without importing an SDK."""
    if modal_app_name is None and billing_tag is None:
        return {}
    if not isinstance(billing_tag, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,62}", billing_tag
    ):
        raise ValueError("Modal requires an explicit safe billing_tag")
    if (
        not isinstance(modal_app_name, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", modal_app_name)
        or not (modal_app_name == billing_tag or modal_app_name.startswith(billing_tag + "-"))
    ):
        raise ValueError("Modal app must be the owned billing tag or a tag-prefixed dedicated app")
    return {"app_name": modal_app_name, "labels": {"evallab.billing_tag": billing_tag}}


class SpendCapReached(RuntimeError):
    """A paid execution would take the run past its approved USD cap."""


@dataclass
class CloudSpendGate:
    """Approval for paid executions: a recorded approval and a USD cap.

    Every paid execution reserves ``usd_per_execution`` before it starts; one
    that would pass ``cap_usd`` raises :class:`SpendCapReached` instead of
    running. Reservations are estimates, not bills: a failed or short execution
    keeps its reservation. An optional native spend ledger shares the immutable
    approval/cap across gates and restarts; its entries remain estimates, not bills.
    Thread-safe: one gate is shared by a run's workers.
    """

    approval: str
    cap_usd: float
    usd_per_execution: float
    executions: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    spend_ledger: Path | str | None = field(default=None, kw_only=True)
    modal_app_name: str | None = field(default=None, kw_only=True)
    billing_tag: str | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.approval, str) or not self.approval.strip():
            raise ValueError("a cloud spend gate needs the recorded approval text")
        for name in ("cap_usd", "usd_per_execution"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive finite number, got {value!r}")
        environment_kwargs = _modal_environment_kwargs(self.modal_app_name, self.billing_tag)
        if self.spend_ledger is not None:
            if not environment_kwargs:
                raise ValueError("a persisted cloud spend gate requires modal_app_name and billing_tag")
            from evallab.upstream_grader import _read_ledger

            self.spend_ledger = Path(self.spend_ledger).expanduser().resolve()
            _read_ledger(self.spend_ledger, approval=self.approval, cap=self.cap_usd)

    def _check_ledger_location(self, *immutable_or_evidence: Path) -> None:
        if self.spend_ledger is None:
            return
        ledger = Path(self.spend_ledger).expanduser().resolve()
        if any(ledger.is_relative_to(path.resolve()) for path in immutable_or_evidence):
            raise ValueError("spend ledger must be outside immutable inputs and execution evidence")

    def charge(self) -> None:
        """Reserve one execution, or raise :class:`SpendCapReached`."""
        with self._lock:
            reserved = (self.executions + 1) * self.usd_per_execution
            if reserved > self.cap_usd + 1e-9:
                raise SpendCapReached(
                    f"execution {self.executions + 1} would reserve ${reserved:.2f}, "
                    f"over the approved ${self.cap_usd:.2f}"
                )
            if self.spend_ledger is not None:
                # upstream_grader imports this class for shared arithmetic, so the
                # durable authority must be imported only at the reservation seam.
                from evallab.upstream_grader import _reserve_spend

                _reserve_spend(
                    Path(self.spend_ledger),
                    approval=self.approval,
                    cap=self.cap_usd,
                    reservation={
                        "reserved_usd": self.usd_per_execution,
                        "usd_per_execution": self.usd_per_execution,
                        "estimate_semantics": "fixed QA execution-unit estimate, not a provider bill",
                    },
                    identity={
                        "primitive": "qa-execution",
                        "modal_app_name": self.modal_app_name,
                        "billing_tag": self.billing_tag,
                    },
                )
            self.executions += 1

    def to_json(self) -> dict[str, Any]:
        with self._lock:
            executions = self.executions
            reserved = round(executions * self.usd_per_execution, 4)
            attribution: dict[str, Any] = {}
            if self.spend_ledger is not None:
                from evallab.upstream_grader import _read_ledger

                ledger = _read_ledger(
                    Path(self.spend_ledger), approval=self.approval, cap=self.cap_usd
                )
                attribution.update(
                    spend_ledger=str(self.spend_ledger),
                    this_gate_reserved_usd=reserved,
                )
                reserved = ledger["reserved_usd"]
            if self.modal_app_name is not None or self.billing_tag is not None:
                attribution.update(
                    modal_app_name=self.modal_app_name, billing_tag=self.billing_tag
                )
            return {
                "approval": self.approval,
                "cap_usd": self.cap_usd,
                "usd_per_execution": self.usd_per_execution,
                "executions": executions,
                "reserved_usd": reserved,
                **attribution,
            }


_HARBOR_VERSION_TIMEOUT_SECONDS = 60
_harbor_problem: list[str | None] = []


def lab_harbor_problem() -> str | None:
    """Why the `harbor` on PATH is not the lab's pinned Harbor, or None.

    Paid backends need the lock's Harbor. Harbor 0.21 starts a Modal separate
    verifier from the agent's prebuilt image (no `/tests`), so every grade
    comes back as infra; 0.24 builds `tests/Dockerfile`. Checked once per process.
    """
    if not _harbor_problem:
        _harbor_problem.append(_probe_harbor())
    return _harbor_problem[0]


def _probe_harbor() -> str | None:
    try:
        pinned = importlib.metadata.version("harbor")
    except importlib.metadata.PackageNotFoundError:
        return "the lab environment has no pinned Harbor; run `uv sync --extra laminar`"
    executable = shutil.which("harbor")
    found = None
    if executable is not None:
        try:
            completed = subprocess.run(
                [executable, "--version"],
                capture_output=True,
                text=True,
                check=False,
                timeout=_HARBOR_VERSION_TIMEOUT_SECONDS,
                env=harbor_child_environment(),
            )
            words = (completed.stdout or completed.stderr).split()
            found = words[-1] if words else None
        except (OSError, subprocess.TimeoutExpired):
            found = None
    if found != pinned:
        return (
            f"`harbor` on PATH is {found or 'unavailable'} ({executable}), not the lab's "
            f"pinned {pinned}; run through `uv run` after `uv sync --extra laminar`"
        )
    return None


class RegradeVerdict(StrEnum):
    """Comparison of a regrade reward against the recorded trial reward."""

    #: Different verifier, identical reward on every dimension.
    UNCHANGED = "unchanged"
    #: Different verifier, a reward dimension decreased: the verifier got stricter
    #: on this exact trajectory.
    TIGHTENED = "tightened"
    #: Different verifier, a reward dimension increased: the verifier got more
    #: permissive on this exact trajectory.
    LOOSENED = "loosened"
    #: Different verifier, dimensions moved in both directions or the dimension
    #: set changed. Not summarisable as tighter or looser.
    REPARTITIONED = "repartitioned"
    #: Byte-identical verifier reproduced the recorded reward exactly.
    DETERMINISTIC = "deterministic"
    #: Byte-identical verifier produced a different reward. A grader defect.
    NONDETERMINISTIC = "nondeterministic"
    #: A precondition or the invocation failed; no reward comparison exists.
    REFUSED = "refused"


class RewardObservation(ContractModel):
    """One reward reading, bound to the verifier that produced it."""

    rewards: dict[str, float] = Field(
        description="verifier reward dimensions, exactly as recorded",
    )
    verifier_environment_mode: str | None = Field(
        default=None,
        description="mode Harbor actually ran the verifier in, from result.json",
    )

    @property
    def primary(self) -> float | None:
        """The `reward` dimension, which Harbor treats as the headline score."""
        return self.rewards.get("reward")


class SourceTrialIdentity(ContractModel):
    """Immutable identity of the trial being re-scored."""

    trial_dir: str
    trial_name: str | None = None
    task_name: str | None = None
    agent_name: str | None = None
    model_name: str | None = None
    #: Digest over the ATIF trajectory bytes actually re-scored. A receipt whose
    #: source digest no longer reproduces is a receipt about different bytes.
    trajectory_digest: str | None = None
    #: Digest over the collected artifact manifest, the other regrade input.
    artifact_manifest_digest: str | None = None
    #: Recorded reward/result bytes and the pre-hidden-test Git patch, when present.
    result_digest: str | None = None
    agent_patch_digest: str | None = None


class VerifierIdentity(ContractModel):
    """Content identity of the verifier used for a re-scoring.

    Digested over the declared `[verifier]` table plus every file in the
    verifier build context, so "did the verifier change?" is answerable without
    trusting a version string.
    """

    task_dir: str
    task_name: str | None = None
    #: Effective mode under Harbor's resolution: an explicit
    #: `environment_mode`, else `"separate"` when `[verifier.environment]` is
    #: present, else None. Identity comparison uses `digest`, never this.
    environment_mode: str | None = None
    context_file_count: int = Field(ge=0)
    digest: str


class RegradeInvocation(ContractModel):
    """The exact Harbor command a receipt corresponds to."""

    command: list[str]
    trials_dir: str
    trial_name: str
    cloud_spend: dict[str, Any] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class RegradeExecution(ContractModel):
    """Outcome of running the Harbor regrade command."""

    exit_code: int
    stderr_tail: str = ""
    #: Harbor prints its own refusal messages (``RegradeError``) to stdout.
    stdout_tail: str = ""


class RegradeReceiptV1(ContractModel):
    """A re-scoring of fixed trajectory bytes under a declared verifier.

    Both rewards are retained and separately attributed. `regraded` never
    replaces `recorded`; consumers that want the trial's reward read `recorded`.
    """

    schema_version: str = "regrade-receipt/v1"
    source: SourceTrialIdentity
    verifier: VerifierIdentity
    verdict: RegradeVerdict
    recorded: RewardObservation | None = None
    regraded: RewardObservation | None = None
    #: Per-dimension `regraded - recorded`, only for dimensions present in both.
    reward_delta: dict[str, float] = Field(default_factory=dict)
    #: True when the verifier digest matches the one recorded for the source
    #: trial, making this a determinism probe rather than a hardening check.
    same_verifier: bool = False
    refusals: list[RegradeRefusalCode] = Field(default_factory=list)
    invocation: RegradeInvocation | None = None
    execution: RegradeExecution | None = None
    regrade_trial_dir: str | None = None

    @property
    def refused(self) -> bool:
        """Whether the receipt carries no usable reward comparison."""
        return self.verdict is RegradeVerdict.REFUSED


def _load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _digest_file(path: Path) -> str | None:
    try:
        return compute_prefixed_sha256(path.read_bytes())
    except OSError:
        return None


def _reward_mapping(result: Mapping[str, Any]) -> dict[str, float] | None:
    verifier_result = result.get("verifier_result")
    if not isinstance(verifier_result, Mapping):
        return None
    rewards = verifier_result.get("rewards")
    if not isinstance(rewards, Mapping) or not rewards:
        return None
    numeric: dict[str, float] = {}
    for key, value in rewards.items():
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        numeric[str(key)] = float(value)
    return numeric or None


def read_reward_observation(trial_dir: Path) -> RewardObservation | None:
    """Read the reward a trial actually recorded, or None when absent."""
    result = _load_json(trial_dir / "result.json")
    if not isinstance(result, Mapping):
        return None
    rewards = _reward_mapping(result)
    if rewards is None:
        return None
    mode = result.get("verifier_environment_mode")
    return RewardObservation(
        rewards=rewards,
        verifier_environment_mode=str(mode) if isinstance(mode, str) else None,
    )


def source_trial_identity(trial_dir: Path) -> SourceTrialIdentity:
    """Identify a recorded trial and digest the bytes a regrade will consume."""
    result = _load_json(trial_dir / "result.json")
    result_map: Mapping[str, Any] = result if isinstance(result, Mapping) else {}
    agent_info = result_map.get("agent_info")
    agent_map: Mapping[str, Any] = agent_info if isinstance(agent_info, Mapping) else {}
    model_info = agent_map.get("model_info")
    model_map: Mapping[str, Any] = model_info if isinstance(model_info, Mapping) else {}
    return SourceTrialIdentity(
        trial_dir=str(trial_dir),
        trial_name=_optional_str(result_map.get("trial_name")) or trial_dir.name,
        task_name=_optional_str(result_map.get("task_name")),
        agent_name=_optional_str(agent_map.get("name")),
        model_name=_optional_str(model_map.get("name")),
        trajectory_digest=_digest_file(trial_dir / "agent" / "trajectory.json"),
        artifact_manifest_digest=_digest_file(trial_dir / "artifacts" / "manifest.json"),
        result_digest=_digest_file(trial_dir / "result.json"),
        agent_patch_digest=_digest_file(trial_dir / "verifier" / "agent.diff"),
    )


def _optional_str(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _iter_context_files(context: Path) -> Iterable[Path]:
    for path in sorted(context.rglob("*")):
        if not path.is_file():
            continue
        if _CONTEXT_IGNORED_DIRS & set(path.relative_to(context).parts):
            continue
        yield path


def verifier_identity(task_dir: Path) -> VerifierIdentity:
    """Digest a task's verifier declaration together with its build context."""
    config = _load_json_toml(task_dir / "task.toml")
    config_map: Mapping[str, Any] = config if isinstance(config, Mapping) else {}
    verifier = config_map.get("verifier")
    verifier_map: Mapping[str, Any] = verifier if isinstance(verifier, Mapping) else {}
    task_table = config_map.get("task")
    task_map: Mapping[str, Any] = task_table if isinstance(task_table, Mapping) else {}

    context = task_dir / _VERIFIER_CONTEXT_DIR
    files: list[tuple[str, str]] = []
    if context.is_dir():
        for path in _iter_context_files(context):
            digest = _digest_file(path)
            if digest is not None:
                files.append((path.relative_to(context).as_posix(), digest))

    payload = {
        "verifier": json.loads(canonical_json(_jsonable(verifier_map))),
        "context": [list(entry) for entry in files],
    }
    return VerifierIdentity(
        task_dir=str(task_dir),
        task_name=_optional_str(task_map.get("name")),
        environment_mode=_verifier_effective_mode(verifier_map),
        context_file_count=len(files),
        digest=compute_prefixed_sha256(payload),
    )


def _jsonable(value: Any) -> Any:
    """Coerce TOML scalars that json cannot serialise (datetimes) to strings."""
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, bool | int | float | str) or value is None:
        return value
    return str(value)


def _load_json_toml(path: Path) -> Mapping[str, Any] | None:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _check_trial_regrade_environment(environment: str, spend_gate: CloudSpendGate | None) -> None:
    """Admit local Docker, or a paid backend under a spend gate and the pinned Harbor.

    Other providers and custom import paths raise before any work starts.
    """
    if environment in LOCAL_REGRADE_ENVIRONMENTS:
        require_local_docker()
        return
    if environment not in CLOUD_REGRADE_ENVIRONMENTS:
        raise ValueError(
            f"Refusing {environment!r} verifier environment: a trial regrade runs on "
            f"local Docker, or on {sorted(CLOUD_REGRADE_ENVIRONMENTS)} under a spend gate."
        )
    if spend_gate is None:
        raise ValueError(
            f"Refusing {environment!r} verifier environment without a spend gate "
            "(recorded approval and USD cap)."
        )
    problem = lab_harbor_problem()
    if problem is not None:
        raise ValueError(f"Refusing {environment!r} regrade: {problem}")


def _verifier_effective_mode(verifier: Mapping[str, Any] | None) -> str | None:
    """Resolve a `[verifier]` table to its effective environment mode.

    Mirrors Harbor's authoritative resolution (`verifier_mode._resolve_mode`):
    an explicit `environment_mode` wins, otherwise a `[verifier.environment]`
    table implies `"separate"`. Anything else (including an explicit
    `"shared"`, which Harbor rejects alongside an environment table) is not
    isolated and cannot be regraded.
    """
    verifier_map: Mapping[str, Any] = verifier if isinstance(verifier, Mapping) else {}
    mode = verifier_map.get("environment_mode")
    if isinstance(mode, str) and mode:
        return mode
    if isinstance(verifier_map.get("environment"), Mapping):
        return "separate"
    return None


def _source_preflight(trial_dir: Path) -> list[RegradeRefusalCode]:
    refusals: list[RegradeRefusalCode] = []

    if not trial_dir.is_dir():
        return [RegradeRefusalCode.SOURCE_TRIAL_MISSING]
    result = _load_json(trial_dir / "result.json")
    if not isinstance(result, Mapping):
        refusals.append(RegradeRefusalCode.SOURCE_RESULT_UNREADABLE)
    elif _reward_mapping(result) is None:
        refusals.append(RegradeRefusalCode.SOURCE_REWARD_ABSENT)
    if not (trial_dir / "agent").is_dir():
        refusals.append(RegradeRefusalCode.SOURCE_AGENT_LOGS_MISSING)
    if not (trial_dir / "artifacts" / "manifest.json").is_file():
        refusals.append(RegradeRefusalCode.SOURCE_ARTIFACT_MANIFEST_MISSING)
    return refusals


def _preflight(trial_dir: Path, task_dir: Path) -> list[RegradeRefusalCode]:
    refusals = _source_preflight(trial_dir)
    if RegradeRefusalCode.SOURCE_TRIAL_MISSING in refusals:
        return refusals

    if not task_dir.is_dir():
        refusals.append(RegradeRefusalCode.TASK_DIR_MISSING)
        return refusals
    config = _load_json_toml(task_dir / "task.toml")
    if config is None:
        refusals.append(RegradeRefusalCode.TASK_CONFIG_UNREADABLE)
        return refusals
    if config.get("steps"):
        refusals.append(RegradeRefusalCode.MULTI_STEP_TASK)
    verifier = config.get("verifier")
    verifier_map: Mapping[str, Any] = verifier if isinstance(verifier, Mapping) else {}
    if verifier_map.get("disable") is True:
        refusals.append(RegradeRefusalCode.VERIFIER_DISABLED)
    if _verifier_effective_mode(verifier_map) != "separate":
        refusals.append(RegradeRefusalCode.VERIFIER_NOT_ISOLATED)
    if not (task_dir / _VERIFIER_CONTEXT_DIR).is_dir():
        refusals.append(RegradeRefusalCode.VERIFIER_CONTEXT_MISSING)
    return refusals


def build_regrade_command(
    *,
    trial_dir: Path,
    task_dir: Path,
    trials_dir: Path,
    trial_name: str,
    environment: str = "docker",
) -> list[str]:
    """Build the exact `harbor trial regrade` invocation for a re-scoring.

    Mirrors `execution_contracts.build_command`: one place constructs Harbor
    argv, and the receipt records it verbatim.
    """
    return [
        "harbor",
        "trial",
        "regrade",
        str(trial_dir),
        "--task-path",
        str(task_dir),
        "--env",
        CLOUD_REGRADE_ENVIRONMENTS.get(environment, environment),
        "--trials-dir",
        str(trials_dir),
        "--trial-name",
        trial_name,
    ]


def _regrade_trial_name(source: SourceTrialIdentity, verifier: VerifierIdentity) -> str:
    """Name a regrade output after its source and the verifier that scored it.

    The verifier digest is in the name so two hardening rounds over one source
    trial cannot collide in the same trials directory.
    """
    base = (source.trial_name or Path(source.trial_dir).name).replace("/", "-")
    short = verifier.digest.removeprefix("sha256:")[:8]
    return f"{base}__{REGRADE_TRIAL_NAME_SUFFIX}-{short}"


def _classify(
    *,
    recorded: RewardObservation,
    regraded: RewardObservation,
    same_verifier: bool,
) -> tuple[RegradeVerdict, dict[str, float]]:
    shared = sorted(set(recorded.rewards) & set(regraded.rewards))
    delta = {
        key: regraded.rewards[key] - recorded.rewards[key]
        for key in shared
        if regraded.rewards[key] != recorded.rewards[key]
    }
    dimensions_match = set(recorded.rewards) == set(regraded.rewards)

    if same_verifier:
        if not delta and dimensions_match:
            return RegradeVerdict.DETERMINISTIC, {}
        return RegradeVerdict.NONDETERMINISTIC, delta
    if not dimensions_match:
        return RegradeVerdict.REPARTITIONED, delta
    if not delta:
        return RegradeVerdict.UNCHANGED, {}
    directions = {value > 0 for value in delta.values()}
    if directions == {False}:
        return RegradeVerdict.TIGHTENED, delta
    if directions == {True}:
        return RegradeVerdict.LOOSENED, delta
    return RegradeVerdict.REPARTITIONED, delta


def _recorded_verifier_digest(trial_dir: Path) -> str | None:
    """Read the verifier digest a prior receipt recorded beside this trial.

    A regrade trial produced by this module carries its own receipt, which is
    how a later determinism probe knows the verifier was byte-identical.
    """
    receipt = _load_json(trial_dir / REGRADE_TRIAL_RECEIPT_FILENAME)
    if isinstance(receipt, Mapping):
        verifier = receipt.get("verifier")
        if isinstance(verifier, Mapping):
            return _optional_str(verifier.get("digest"))
    return None


def regrade_trial(
    *,
    trial_dir: Path,
    task_dir: Path,
    trials_dir: Path,
    environment: str = "docker",
    runner: Any = subprocess.run,
    write_receipt: bool = True,
    spend_gate: CloudSpendGate | None = None,
) -> RegradeReceiptV1:
    """Re-score one recorded trial with a task's current verifier.

    Never mutates the source trial. Refuses closed with typed reason codes when
    any precondition fails, rather than producing an unattributable reward.
    Local Docker by default; Modal only under ``spend_gate``, which is charged
    once just before Harbor runs. A charge over the cap refuses the trial.
    """
    _check_trial_regrade_environment(environment, spend_gate)
    trial_dir = Path(trial_dir)
    task_dir = Path(task_dir)
    trials_dir = Path(trials_dir)
    if spend_gate is not None and environment not in LOCAL_REGRADE_ENVIRONMENTS:
        spend_gate._check_ledger_location(trial_dir, task_dir, trials_dir)

    source = source_trial_identity(trial_dir)
    verifier = verifier_identity(task_dir)
    recorded = read_reward_observation(trial_dir)
    prior_digest = _recorded_verifier_digest(trial_dir)
    same_verifier = prior_digest is not None and prior_digest == verifier.digest

    refusals = _preflight(trial_dir, task_dir)
    if refusals:
        return RegradeReceiptV1(
            source=source,
            verifier=verifier,
            verdict=RegradeVerdict.REFUSED,
            recorded=recorded,
            same_verifier=same_verifier,
            refusals=refusals,
        )

    trial_name = _regrade_trial_name(source, verifier)
    _validate_regrade_destination(trial_dir, trials_dir, trial_name)
    command = build_regrade_command(
        trial_dir=trial_dir,
        task_dir=task_dir,
        trials_dir=trials_dir,
        trial_name=trial_name,
        environment=environment,
    )
    invocation = RegradeInvocation(
        command=command,
        trials_dir=str(trials_dir),
        trial_name=trial_name,
        cloud_spend=spend_gate.to_json()
        if spend_gate is not None and environment not in LOCAL_REGRADE_ENVIRONMENTS
        else None,
    )
    trials_dir.mkdir(parents=True, exist_ok=True)
    if spend_gate is not None and environment not in LOCAL_REGRADE_ENVIRONMENTS:
        try:
            spend_gate.charge()
        except SpendCapReached:
            return RegradeReceiptV1(
                source=source,
                verifier=verifier,
                verdict=RegradeVerdict.REFUSED,
                recorded=recorded,
                same_verifier=same_verifier,
                refusals=[RegradeRefusalCode.SPEND_CAP_REACHED],
                invocation=invocation,
            )
        invocation.cloud_spend = spend_gate.to_json()
    runner_kwargs: dict[str, Any] = {"env": harbor_child_environment()}
    if environment == "modal" and spend_gate is not None:
        environment_kwargs = _modal_environment_kwargs(
            spend_gate.modal_app_name, spend_gate.billing_tag
        )
        if environment_kwargs:
            # Harbor's regrade CLI cannot express environment kwargs. Bind the
            # validated gate to this one output trial in the child process only.
            runner_kwargs["env"] = {
                **harbor_child_environment(),
                _REGRADE_MODAL_POLICY_ENV: json.dumps(
                    {
                        "trial_dir": str((trials_dir / trial_name).resolve()),
                        "approval": spend_gate.approval,
                        "cap_usd": spend_gate.cap_usd,
                        "usd_per_execution": spend_gate.usd_per_execution,
                        "spend_ledger": str(spend_gate.spend_ledger)
                        if spend_gate.spend_ledger is not None
                        else None,
                        "modal_app_name": spend_gate.modal_app_name,
                        "billing_tag": spend_gate.billing_tag,
                    }
                ),
            }
    else:
        require_local_docker()
    completed = runner(command, capture_output=True, text=True, check=False, **runner_kwargs)
    exit_code = int(getattr(completed, "returncode", 1))
    stderr = getattr(completed, "stderr", "") or ""
    stdout = getattr(completed, "stdout", "") or ""
    execution = RegradeExecution(
        exit_code=exit_code, stderr_tail=stderr[-2000:], stdout_tail=stdout[-2000:]
    )

    regrade_dir = trials_dir / trial_name
    if exit_code != 0:
        return RegradeReceiptV1(
            source=source,
            verifier=verifier,
            verdict=RegradeVerdict.REFUSED,
            recorded=recorded,
            same_verifier=same_verifier,
            refusals=[RegradeRefusalCode.HARBOR_INVOCATION_FAILED],
            invocation=invocation,
            execution=execution,
            regrade_trial_dir=str(regrade_dir) if regrade_dir.is_dir() else None,
        )

    if not regrade_dir.is_dir() or _load_json(regrade_dir / "result.json") is None:
        return RegradeReceiptV1(
            source=source,
            verifier=verifier,
            verdict=RegradeVerdict.REFUSED,
            recorded=recorded,
            same_verifier=same_verifier,
            refusals=[RegradeRefusalCode.REGRADE_RESULT_UNREADABLE],
            invocation=invocation,
            execution=execution,
            regrade_trial_dir=str(regrade_dir) if regrade_dir.is_dir() else None,
        )

    regraded = read_reward_observation(regrade_dir)
    if regraded is None or recorded is None:
        missing = (
            RegradeRefusalCode.REGRADE_REWARD_ABSENT
            if regraded is None
            else RegradeRefusalCode.SOURCE_REWARD_ABSENT
        )
        return RegradeReceiptV1(
            source=source,
            verifier=verifier,
            verdict=RegradeVerdict.REFUSED,
            recorded=recorded,
            regraded=regraded,
            same_verifier=same_verifier,
            refusals=[missing],
            invocation=invocation,
            execution=execution,
            regrade_trial_dir=str(regrade_dir),
        )

    verdict, delta = _classify(
        recorded=recorded,
        regraded=regraded,
        same_verifier=same_verifier,
    )
    receipt = RegradeReceiptV1(
        source=source,
        verifier=verifier,
        verdict=verdict,
        recorded=recorded,
        regraded=regraded,
        reward_delta=delta,
        same_verifier=same_verifier,
        invocation=invocation,
        execution=execution,
        regrade_trial_dir=str(regrade_dir),
    )
    if write_receipt:
        (regrade_dir / REGRADE_TRIAL_RECEIPT_FILENAME).write_text(
            json.dumps(receipt.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return receipt


def _validate_regrade_destination(source: Path, parent: Path, name: str) -> None:
    """A replay must never create or resume output within its immutable input."""
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError("regrade name must be a single directory name")
    source = source.resolve()
    destination = (parent / name).resolve()
    if (
        destination == source
        or destination.is_relative_to(source)
        or source.is_relative_to(destination)
    ):
        raise ValueError("regrade output must be disjoint from the source evidence")
    if destination.exists():
        raise ValueError("regrade output already exists; choose a new explicit name")


