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
(multi-step sources) is present. Only the local `$0` Docker path runs;
anything else raises before any work starts, with no bypass flag.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from evallab.benchmark_program_contracts import canonical_json, compute_prefixed_sha256
from evallab.schemas import ContractModel

__all__ = [
    "LOCAL_REGRADE_ENVIRONMENTS",
    "REGRADE_JOB_RECEIPT_FILENAME",
    "REGRADE_TRIAL_RECEIPT_FILENAME",
    "RegradeExecution",
    "RegradeInvocation",
    "RegradeJobInvocation",
    "RegradeJobPlan",
    "RegradeJobPlanTrial",
    "RegradeJobReceiptV1",
    "RegradeJobVerdict",
    "RegradeReceiptV1",
    "RegradeRefusalCode",
    "RegradeVerdict",
    "RewardObservation",
    "SourceTrialIdentity",
    "VerifierIdentity",
    "build_job_regrade_command",
    "build_regrade_command",
    "default_regrade_job_name",
    "expand_task_dirs",
    "plan_regrade_job",
    "read_reward_observation",
    "regrade_job",
    "regrade_many",
    "regrade_trial",
    "render_job_plan",
    "render_job_receipt",
    "render_receipt",
    "source_trial_identity",
    "verifier_identity",
]


#: Marker appended to generated regrade trial names. Regrade outputs are trials
#: in their own right; the suffix keeps them recognisable in a jobs tree without
#: parsing `config.source_trial`.
REGRADE_TRIAL_NAME_SUFFIX = "regrade"

#: The only verifier environment the regrade path may use. Regrade is a `$0`
#: local-Docker operation: it never invokes a model, and the verifier runs
#: inside Harbor's Docker environment. Anything else — cloud runners, custom
#: import paths — raises before any work starts. There is no opt-in flag: a
#: caller boolean is not the queue's paid approval ledger.
LOCAL_REGRADE_ENVIRONMENTS = frozenset({"docker"})

#: Job-level receipt filename, written at the new job root. The source job is
#: never written to.
REGRADE_JOB_RECEIPT_FILENAME = "regrade-job-receipt.json"

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


class RegradeExecution(ContractModel):
    """Outcome of running the Harbor regrade command."""

    exit_code: int
    stderr_tail: str = ""


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


class RegradeJobVerdict(StrEnum):
    """Outcome of a job-level regrade."""

    #: Every in-scope source trial was re-scored and compared.
    COMPLETE = "complete"
    #: At least one trial was compared and at least one was refused.
    PARTIAL = "partial"
    #: Nothing was compared. Either preconditions failed before Harbor ran,
    #: or the Harbor invocation produced no usable reward comparison.
    REFUSED = "refused"


class RegradeJobInvocation(ContractModel):
    """The exact `harbor job regrade` command a job receipt corresponds to."""

    command: list[str]
    jobs_dir: str
    job_name: str


class RegradeJobPlanTrial(ContractModel):
    """What a preview decided about one source trial, before anything runs."""

    source_trial_dir: str
    task_name: str | None = None
    #: Matched task; held-out mode derives a separate verifier from this source package.
    task_dir: str | None = None
    eligible: bool = False
    refusals: list[RegradeRefusalCode] = Field(default_factory=list)
    input_digests: dict[str, str] = Field(default_factory=dict)
    details: list[str] = Field(default_factory=list)


class RegradeJobPlan(ContractModel):
    """A runnable-or-refused preview of `regrade_job`. No side effects."""

    source_job_dir: str
    source_harbor_version: str | None = None
    job_name: str
    jobs_dir: str
    #: Where Harbor would write the new job. Never inside the source job.
    job_dir: str
    task_dirs: list[str] = Field(default_factory=list)
    mode: Literal["standard", "held-out"] = "standard"
    heldout_bundle_digest: str | None = None
    skipped_trials: list[str] = Field(default_factory=list)
    environment: str = "docker"
    #: Held-out mode materializes per-trial commands only after cached-image preflight.
    command: list[str] = Field(default_factory=list)
    trials: list[RegradeJobPlanTrial] = Field(default_factory=list)
    refusals: list[RegradeRefusalCode] = Field(default_factory=list)
    runnable: bool = False


class RegradeJobReceiptV1(ContractModel):
    """Re-scoring of a recorded job, with separately attributed per-trial evidence.

    Standard mode uses one native job invocation. Held-out mode groups sequential
    native trial regrades so only recorded passes execute; their actual commands
    live in the trial receipts, not a fabricated native job invocation.
    Refused inputs remain explicit and unscored. Non-passing source trials are
    listed separately in ``skipped_trials`` in held-out mode.
    """

    schema_version: str = "regrade-job-receipt/v1"
    source_job_dir: str
    #: `harbor.version` from the source job's lock.json, when present. Kept
    #: as provenance; lock-content digests differ across Harbor versions, so
    #: any lock comparison must key on this.
    source_harbor_version: str | None = None
    job_name: str
    jobs_dir: str
    #: The new Harbor job directory in standard jobs-root/job/trial shape.
    job_dir: str
    mode: Literal["standard", "held-out"] = "standard"
    heldout_bundle_digest: str | None = None
    skipped_trials: list[str] = Field(default_factory=list)
    #: Verifier task directories passed as `-p`, in command order.
    task_dirs: list[str] = Field(default_factory=list)
    environment: str = "docker"
    verdict: RegradeJobVerdict
    trials: list[RegradeReceiptV1] = Field(default_factory=list)
    refusals: list[RegradeRefusalCode] = Field(default_factory=list)
    invocation: RegradeJobInvocation | None = None
    execution: RegradeExecution | None = None

    @property
    def refused(self) -> bool:
        """Whether the receipt carries no usable reward comparison."""
        return self.verdict is RegradeJobVerdict.REFUSED

    @property
    def n_compared(self) -> int:
        """Trials with a real recorded-vs-regraded comparison."""
        return sum(1 for trial in self.trials if not trial.refused)

    @property
    def n_refused(self) -> int:
        """Trials refused closed, with reasons on each entry."""
        return sum(1 for trial in self.trials if trial.refused)


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


def _check_regrade_environment(environment: str) -> None:
    """Refuse any verifier environment other than local Docker.

    A regrade never invokes a model, but a cloud `--env` would still spend on
    a sandbox, and a custom import path would execute unknown code. This task
    is `$0`: anything but `"docker"` raises before any work starts, with no
    bypass flag.
    """
    if environment in LOCAL_REGRADE_ENVIRONMENTS:
        return
    raise ValueError(
        f"Refusing {environment!r} verifier environment: regrade runs on local "
        "Docker only."
    )


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


def _task_regradable_error(config: Mapping[str, Any]) -> str | None:
    """Mirror Harbor's `check_task_regradable` over a parsed task.toml.

    Single-step tasks must resolve to a separate verifier; multi-step tasks
    need every step separate. Returns the reason, or None when Harbor itself
    would accept the task as a regrade verifier.
    """
    verifier = config.get("verifier")
    verifier_map: Mapping[str, Any] = verifier if isinstance(verifier, Mapping) else {}
    steps = config.get("steps")
    if steps:
        if not isinstance(steps, list):
            return "task steps are unreadable"
        shared = sorted(
            str(step.get("name", index))
            for index, step in enumerate(steps)
            if isinstance(step, Mapping)
            and _step_effective_mode(verifier_map, step) != "separate"
        )
        if shared:
            return f"shared-mode verifier step(s): {', '.join(shared)}"
        return None
    if _verifier_effective_mode(verifier_map) != "separate":
        return "task resolves to a shared-mode verifier"
    return None


def _step_effective_mode(
    task_verifier: Mapping[str, Any], step: Mapping[str, Any]
) -> str | None:
    """Resolve one step's verifier mode, inheriting the task level.

    Mirrors Harbor's `resolve_step_verifier_mode`: explicit step mode first,
    then a step `[verifier.environment]` table, then the task-level
    resolution.
    """
    step_verifier = step.get("verifier")
    step_map: Mapping[str, Any] = (
        step_verifier if isinstance(step_verifier, Mapping) else {}
    )
    mode = step_map.get("environment_mode")
    if isinstance(mode, str) and mode:
        return mode
    if isinstance(step_map.get("environment"), Mapping):
        return "separate"
    return _verifier_effective_mode(task_verifier)


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
        environment,
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
) -> RegradeReceiptV1:
    """Re-score one recorded trial with a task's current verifier.

    Never mutates the source trial. Refuses closed with typed reason codes when
    any precondition fails, rather than producing an unattributable reward.
    Only local Docker runs: any other verifier environment raises.
    """
    _check_regrade_environment(environment)
    trial_dir = Path(trial_dir)
    task_dir = Path(task_dir)
    trials_dir = Path(trials_dir)

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
    )

    trials_dir.mkdir(parents=True, exist_ok=True)
    completed = runner(command, capture_output=True, text=True, check=False)
    exit_code = int(getattr(completed, "returncode", 1))
    stderr = getattr(completed, "stderr", "") or ""
    execution = RegradeExecution(exit_code=exit_code, stderr_tail=stderr[-2000:])

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


def render_receipt(receipt: RegradeReceiptV1) -> str:
    """One-line human summary of a receipt for CLI output."""
    head = f"{receipt.verdict.value}: {receipt.source.trial_name}"
    if receipt.refused:
        codes = ", ".join(code.value for code in receipt.refusals)
        return f"{head} [{codes or 'no reason recorded'}]"
    recorded = receipt.recorded.primary if receipt.recorded else None
    regraded = receipt.regraded.primary if receipt.regraded else None
    delta = (
        " delta " + ", ".join(f"{k}{v:+g}" for k, v in sorted(receipt.reward_delta.items()))
        if receipt.reward_delta
        else ""
    )
    return (
        f"{head} reward {recorded} -> {regraded}{delta} (verifier {receipt.verifier.digest[:15]})"
    )


def regrade_many(
    *,
    trial_dirs: Sequence[Path],
    task_dir: Path,
    trials_dir: Path,
    environment: str = "docker",
    runner: Any = subprocess.run,
) -> list[RegradeReceiptV1]:
    """Re-score several recorded trials against one verifier."""
    return [
        regrade_trial(
            trial_dir=Path(trial_dir),
            task_dir=task_dir,
            trials_dir=trials_dir,
            environment=environment,
            runner=runner,
        )
        for trial_dir in trial_dirs
    ]


def default_regrade_job_name(job_dir: Path) -> str:
    """Name a regrade job after its source so reruns stay traceable."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%d__%H-%M-%S")
    base = Path(job_dir).name or "job"
    return f"{base}__regrade-{stamp}"


def build_job_regrade_command(
    *,
    job_dir: Path,
    task_dirs: Sequence[Path],
    jobs_dir: Path,
    job_name: str,
    environment: str = "docker",
) -> list[str]:
    """Build the exact `harbor job regrade` invocation for a job re-scoring.

    One place constructs Harbor argv and the receipt records it verbatim. The
    command carries no agent or model flags: Harbor derives regrade trials
    that restore recorded outputs and run only separate verifiers, so no model
    is ever invoked. Task scripts are never executed on the host either — the
    verifier runs inside Harbor's Docker environment.
    """
    command = ["harbor", "job", "regrade", str(job_dir)]
    for task_dir in task_dirs:
        command += ["--task-path", str(task_dir)]
    command += ["--env", environment, "--jobs-dir", str(jobs_dir), "--job-name", job_name]
    return command


def expand_task_dirs(path: Path) -> list[Path]:
    """Expand a `-p` argument into verifier task directories.

    Mirrors Harbor's `expand_task_path`: either a task directory (contains
    `task.toml`) or a parent whose children are task directories.
    """
    path = Path(path)
    if not path.is_dir():
        raise ValueError(f"Task path does not exist or is not a directory: {path}")
    if (path / "task.toml").is_file():
        return [path]
    children = sorted(
        child for child in path.iterdir() if child.is_dir() and (child / "task.toml").is_file()
    )
    if not children:
        raise ValueError(
            f"{path} is neither a task directory (no task.toml) nor a "
            "directory containing task directories."
        )
    return children


def _read_task_name(task_dir: Path) -> str | None:
    """A task's `[task].name` from task.toml, else the directory name.

    Mirrors Harbor's `local_task_name`. None when task.toml is unreadable.
    """
    config = _load_json_toml(task_dir / "task.toml")
    if config is None:
        return None
    task = config.get("task")
    if isinstance(task, Mapping):
        name = _optional_str(task.get("name"))
        if name:
            return name
    return task_dir.name


def _trial_task_name(trial_dir: Path) -> str | None:
    """The task a recorded trial ran, from its own result.json."""
    result = _load_json(trial_dir / "result.json")
    if isinstance(result, Mapping):
        return _optional_str(result.get("task_name"))
    return None


def _trial_result_id(trial_dir: Path) -> str | None:
    """A recorded trial's id, used to match regrade outputs back to sources."""
    result = _load_json(trial_dir / "result.json")
    if isinstance(result, Mapping):
        raw = result.get("id")
        return str(raw) if isinstance(raw, str | int) and str(raw) else None
    return None


def _saved_task_dir(source_trial: Path) -> Path | None:
    """The task directory a trial's own config says scored it, if recorded."""
    config = _load_json(source_trial / "config.json")
    if not isinstance(config, Mapping):
        return None
    task = config.get("task")
    if not isinstance(task, Mapping):
        return None
    raw = task.get("path")
    if not isinstance(raw, str) or not raw:
        return None
    return Path(raw)


def _harbor_version_of(job_dir: Path) -> str | None:
    """`harbor.version` from a job's lock.json, when present."""
    lock = _load_json(job_dir / "lock.json")
    if isinstance(lock, Mapping):
        harbor = lock.get("harbor")
        if isinstance(harbor, Mapping):
            return _optional_str(harbor.get("version"))
    return None


def _is_multi_step_source(trial_dir: Path) -> bool:
    """Whether a recorded trial ran a multi-step task.

    The trial-level machinery (identity digests, reward readers) is
    single-step shaped, so multi-step sources are an unsupported layout and
    refuse closed rather than producing an unverifiable comparison.
    """
    if (trial_dir / "steps").is_dir():
        return True
    config = _load_json(trial_dir / "config.json")
    return isinstance(config, Mapping) and bool(config.get("steps"))


def _source_job_trials(job_dir: Path) -> list[Path]:
    """Source trials Harbor would derive from: subdirs with a scored result.

    Hidden scratch (`.sources`) and job-level files are skipped, mirroring
    Harbor's derivation, which only iterates trial directories — and like
    Harbor (whose `TrialResult` requires `task_name`), directories without a
    named scored result are not trials to re-score.
    """
    found: list[Path] = []
    for child in sorted(job_dir.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or child.name.startswith("."):
            continue
        result = _load_json(child / "result.json")
        if isinstance(result, Mapping) and _optional_str(result.get("task_name")):
            found.append(child)
    return found


def _gate_matched_tasks(
    dirs_by_name: dict[str, Path], task_names: set[str]
) -> list[RegradeRefusalCode]:
    """Refuse when Harbor itself would reject a matched verifier task.

    Mirrors the derivation gate in Harbor's regrade job builder: only tasks
    that actually grade trials matter, and every one of those must resolve to
    separate verifiers (implicit `[verifier.environment]` counts).
    """
    for name in sorted(task_names):
        task_dir = dirs_by_name[name]
        config = _load_json_toml(task_dir / "task.toml")
        if config is None or _task_regradable_error(config) is not None:
            return [RegradeRefusalCode.VERIFIER_NOT_ISOLATED]
    return []


def _resolve_job_task_dirs(
    *,
    source_trials: list[Path],
    task_dir: Path | None,
    require_separate: bool = True,
) -> tuple[list[Path], dict[str, Path], list[RegradeRefusalCode]]:
    """Resolve verifier task directories for every source task name.

    Returns `(ordered task dirs for -p, dirs by source task name, refusals)`.
    A given `task_dir` is expanded like Harbor's `-p` and must cover every
    task in the job — a single task that matches nothing is a name mismatch,
    never a silent scoring of unrelated tasks. An omitted `task_dir` resolves
    each trial's task from its own saved config, and only when that config
    still points at a live task with the same name.
    """
    source_names: dict[str, list[Path]] = {}
    for source in source_trials:
        name = _trial_task_name(source)
        if name is not None:
            source_names.setdefault(name, []).append(source)

    if task_dir is not None:
        given = Path(task_dir)
        if not given.is_dir():
            return ([], {}, [RegradeRefusalCode.TASK_DIR_MISSING])
        try:
            expanded = expand_task_dirs(given)
        except ValueError:
            return ([], {}, [RegradeRefusalCode.TASK_CONFIG_UNREADABLE])
        dirs_by_name: dict[str, Path] = {}
        for candidate in expanded:
            name = _read_task_name(candidate)
            if name is None:
                return ([], {}, [RegradeRefusalCode.TASK_CONFIG_UNREADABLE])
            if name in dirs_by_name:
                return ([], {}, [RegradeRefusalCode.TASK_AMBIGUOUS])
            dirs_by_name[name] = candidate
        uncovered = sorted(set(source_names) - set(dirs_by_name))
        if uncovered:
            if len(expanded) == 1 and not (set(source_names) & set(dirs_by_name)):
                return ([], {}, [RegradeRefusalCode.TASK_NAME_MISMATCH])
            return ([], {}, [RegradeRefusalCode.TASK_COVERAGE_GAP])
        gate = _gate_matched_tasks(dirs_by_name, set(source_names)) if require_separate else []
        if gate:
            return ([], {}, gate)
        return (expanded, {name: dirs_by_name[name] for name in source_names}, [])

    dirs_by_name: dict[str, Path] = {}
    for name, trials in source_names.items():
        saved: Path | None = None
        for trial in trials:
            candidate = _saved_task_dir(trial)
            if (
                candidate is None
                or not candidate.is_dir()
                or _read_task_name(candidate) != name
            ):
                return ([], {}, [RegradeRefusalCode.TASK_UNRESOLVED])
            if saved is None:
                saved = candidate
            elif saved.resolve() != candidate.resolve():
                return ([], {}, [RegradeRefusalCode.TASK_AMBIGUOUS])
        assert saved is not None
        dirs_by_name[name] = saved
    gate = _gate_matched_tasks(dirs_by_name, set(source_names)) if require_separate else []
    if gate:
        return ([], {}, gate)
    ordered: list[Path] = []
    seen: set[str] = set()
    for name in source_names:
        key = str(dirs_by_name[name].resolve())
        if key not in seen:
            seen.add(key)
            ordered.append(dirs_by_name[name])
    return (ordered, dirs_by_name, [])


def _validate_regrade_destination(source: Path, parent: Path, name: str) -> None:
    """A replay must never create or resume output within its immutable input."""
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError("regrade name must be a single directory name")
    source = source.resolve()
    destination = (parent / name).resolve()
    if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("regrade output must be disjoint from the source evidence")
    if destination.exists():
        raise ValueError("regrade output already exists; choose a new explicit name")


def plan_regrade_job(
    *,
    job_dir: Path,
    task_dir: Path | None = None,
    jobs_dir: Path,
    name: str | None = None,
    environment: str = "docker",
) -> RegradeJobPlan:
    """Preview a job regrade without running anything or writing anywhere.

    Resolves verifier tasks, checks every source trial the way `regrade_job`
    will, and reports the exact command Harbor would run. The preview for a
    parent CLI dry-run: print it, exit non-zero when not runnable.
    """
    _check_regrade_environment(environment)
    job_dir = Path(job_dir)
    jobs_dir = Path(jobs_dir)
    job_name = name or default_regrade_job_name(job_dir)
    _validate_regrade_destination(job_dir, jobs_dir, job_name)
    base: dict[str, Any] = {
        "source_job_dir": str(job_dir),
        "source_harbor_version": _harbor_version_of(job_dir) if job_dir.is_dir() else None,
        "job_name": job_name,
        "jobs_dir": str(jobs_dir),
        "job_dir": str(jobs_dir / job_name),
        "environment": environment,
    }
    if not job_dir.is_dir():
        return RegradeJobPlan(
            **base, trials=[], refusals=[RegradeRefusalCode.SOURCE_JOB_MISSING]
        )
    sources = _source_job_trials(job_dir)
    if not sources:
        return RegradeJobPlan(
            **base, trials=[], refusals=[RegradeRefusalCode.SOURCE_JOB_EMPTY]
        )
    ordered, dirs_by_name, task_refusals = _resolve_job_task_dirs(
        source_trials=sources, task_dir=task_dir
    )
    trials: list[RegradeJobPlanTrial] = []
    for source in sources:
        task_name = _trial_task_name(source)
        matched = dirs_by_name.get(task_name) if task_name else None
        if _is_multi_step_source(source):
            trials.append(
                RegradeJobPlanTrial(
                    source_trial_dir=str(source),
                    task_name=task_name,
                    task_dir=str(matched) if matched else None,
                    eligible=False,
                    refusals=[RegradeRefusalCode.MULTI_STEP_TASK],
                )
            )
        elif matched is None:
            # Unreachable when the plan is runnable: coverage then holds for
            # every enumerated trial. Otherwise the job-level refusals are
            # the operative cause, echoed here so no trial looks unexplained.
            trials.append(
                RegradeJobPlanTrial(
                    source_trial_dir=str(source),
                    task_name=task_name,
                    eligible=False,
                    refusals=list(task_refusals),
                )
            )
        else:
            preflight = _preflight(source, matched)
            trials.append(
                RegradeJobPlanTrial(
                    source_trial_dir=str(source),
                    task_name=task_name,
                    task_dir=str(matched),
                    eligible=not preflight,
                    refusals=preflight,
                )
            )
    runnable = not task_refusals and any(trial.eligible for trial in trials)
    return RegradeJobPlan(
        **base,
        task_dirs=[str(path) for path in ordered],
        command=(
            build_job_regrade_command(
                job_dir=job_dir,
                task_dirs=ordered,
                jobs_dir=jobs_dir,
                job_name=job_name,
                environment=environment,
            )
            if runnable
            else []
        ),
        trials=trials,
        refusals=list(dict.fromkeys([
            *task_refusals,
            *(code for trial in trials for code in trial.refusals),
        ])),
        runnable=runnable,
    )


def _refused_job_trial(
    *,
    source: Path,
    task_dir: Path,
    refusals: Sequence[RegradeRefusalCode],
    new_trial_dir: Path | None,
) -> RegradeReceiptV1:
    """A closed refusal for one job trial: identities, no comparison."""
    verifier = verifier_identity(task_dir)
    recorded = read_reward_observation(source)
    prior_digest = _recorded_verifier_digest(source)
    return RegradeReceiptV1(
        source=source_trial_identity(source),
        verifier=verifier,
        verdict=RegradeVerdict.REFUSED,
        recorded=recorded,
        same_verifier=prior_digest is not None and prior_digest == verifier.digest,
        refusals=list(refusals),
        regrade_trial_dir=str(new_trial_dir) if new_trial_dir is not None else None,
    )


def _compare_job_trial(
    *,
    source: Path,
    task_dir: Path,
    new_trial_dir: Path,
) -> RegradeReceiptV1:
    """Compare one regraded job trial against its recorded source.

    Both rewards are read from Harbor-written result.json files and kept
    separately attributed. Anything missing yields a refusal, never a zero
    standing in for a score Harbor never produced.
    """
    recorded = read_reward_observation(source)
    regraded = read_reward_observation(new_trial_dir)
    verifier = verifier_identity(task_dir)
    prior_digest = _recorded_verifier_digest(source)
    same_verifier = prior_digest is not None and prior_digest == verifier.digest
    base: dict[str, Any] = {
        "source": source_trial_identity(source),
        "verifier": verifier,
        "recorded": recorded,
        "regraded": regraded,
        "same_verifier": same_verifier,
        "regrade_trial_dir": str(new_trial_dir),
    }
    if recorded is None or regraded is None:
        missing = (
            RegradeRefusalCode.REGRADE_REWARD_ABSENT
            if regraded is None
            else RegradeRefusalCode.SOURCE_REWARD_ABSENT
        )
        return RegradeReceiptV1(
            **base, verdict=RegradeVerdict.REFUSED, refusals=[missing]
        )
    verdict, delta = _classify(
        recorded=recorded, regraded=regraded, same_verifier=same_verifier
    )
    return RegradeReceiptV1(**base, verdict=verdict, reward_delta=delta)


def _index_regrade_outputs(
    new_job_dir: Path,
) -> tuple[dict[str, Path], dict[str, Path]]:
    """Map regrade output trials back to their sources.

    Harbor records the derivation in each new trial's config.json
    (`source_trial.path` for local sources, `trial_id` for hub ones); that
    record — not name guessing — is the mapping. Returns `(by source path,
    by source trial id)`.
    """
    by_path: dict[str, Path] = {}
    by_id: dict[str, Path] = {}
    for child in sorted(new_job_dir.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or child.name.startswith("."):
            continue
        config = _load_json(child / "config.json")
        if not isinstance(config, Mapping):
            continue
        source_trial = config.get("source_trial")
        if not isinstance(source_trial, Mapping):
            continue
        raw_path = source_trial.get("path")
        if isinstance(raw_path, str) and raw_path:
            by_path.setdefault(str(Path(raw_path).resolve()), child)
        raw_id = source_trial.get("trial_id")
        if isinstance(raw_id, str | int) and str(raw_id):
            by_id.setdefault(str(raw_id), child)
    return by_path, by_id


def _collect_job_trials(
    *,
    plan: RegradeJobPlan,
    new_job_dir: Path,
) -> list[RegradeReceiptV1]:
    """Build one trial receipt per planned source trial from Harbor outputs."""
    by_path, by_id = _index_regrade_outputs(new_job_dir)
    entries: list[RegradeReceiptV1] = []
    for planned in plan.trials:
        source = Path(planned.source_trial_dir)
        task_dir = Path(planned.task_dir) if planned.task_dir else None
        if task_dir is None:  # Unreachable when the plan was runnable.
            continue
        new_trial_dir = by_path.get(str(source.resolve()))
        if new_trial_dir is None:
            source_id = _trial_result_id(source)
            if source_id is not None:
                new_trial_dir = by_id.get(source_id)
        if not planned.eligible:
            entries.append(
                _refused_job_trial(
                    source=source,
                    task_dir=task_dir,
                    refusals=planned.refusals,
                    new_trial_dir=new_trial_dir,
                )
            )
        elif new_trial_dir is None or not new_trial_dir.is_dir():
            entries.append(
                _refused_job_trial(
                    source=source,
                    task_dir=task_dir,
                    refusals=[RegradeRefusalCode.REGRADE_RESULT_UNREADABLE],
                    new_trial_dir=new_trial_dir,
                )
            )
        else:
            entries.append(
                _compare_job_trial(
                    source=source, task_dir=task_dir, new_trial_dir=new_trial_dir
                )
            )
    return entries


def _write_job_receipts(receipt: RegradeJobReceiptV1, new_job_dir: Path) -> None:
    """Persist per-trial and job receipts inside the new job only.

    The source job is never written to. Per-trial receipts pin the verifier
    digest beside the regraded reward so later determinism probes work.
    """
    for entry in receipt.trials:
        if entry.regrade_trial_dir is None:
            continue
        target = Path(entry.regrade_trial_dir)
        if target.is_dir():
            (target / REGRADE_TRIAL_RECEIPT_FILENAME).write_text(
                json.dumps(entry.model_dump(mode="json"), indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
    (new_job_dir / REGRADE_JOB_RECEIPT_FILENAME).write_text(
        json.dumps(receipt.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def regrade_job(
    *,
    job_dir: Path,
    task_dir: Path | None = None,
    jobs_dir: Path,
    name: str | None = None,
    environment: str = "docker",
    runner: Any = subprocess.run,
    write_receipt: bool = True,
) -> RegradeJobReceiptV1:
    """Re-score every trial of a recorded Harbor job at zero model cost.

    Runs the real `harbor job regrade` (Harbor derives one regrade trial per
    recorded source trial, matched by task name) and receipts each output as
    a trial-level comparison with its own source/verifier identity and
    old/new reward dimensions. `task_dir` is optional only when every
    trial's saved config still resolves to its live task; a given `task_dir`
    must cover the job's tasks or the run refuses instead of silently
    scoring unrelated tasks.

    Never mutates the source job: `lock.json`, `artifacts/manifest.json`,
    and all provenance stay byte-identical; receipts land in the new job
    directory, which has the standard jobs-root/job/trial shape. A failed
    Harbor invocation refuses the whole job rather than cherry-picking
    partial outputs. Only local Docker runs: any other verifier environment
    raises.
    """
    _check_regrade_environment(environment)
    plan = plan_regrade_job(
        job_dir=job_dir,
        task_dir=task_dir,
        jobs_dir=jobs_dir,
        name=name,
        environment=environment,
    )
    base: dict[str, Any] = {
        "source_job_dir": plan.source_job_dir,
        "source_harbor_version": plan.source_harbor_version,
        "job_name": plan.job_name,
        "jobs_dir": plan.jobs_dir,
        "job_dir": plan.job_dir,
        "task_dirs": plan.task_dirs,
        "environment": plan.environment,
    }
    if not plan.runnable:
        return RegradeJobReceiptV1(
            **base, verdict=RegradeJobVerdict.REFUSED, trials=[], refusals=plan.refusals
        )

    invocation = RegradeJobInvocation(
        command=plan.command, jobs_dir=plan.jobs_dir, job_name=plan.job_name
    )
    Path(plan.jobs_dir).mkdir(parents=True, exist_ok=True)
    completed = runner(plan.command, capture_output=True, text=True, check=False)
    exit_code = int(getattr(completed, "returncode", 1))
    stderr = getattr(completed, "stderr", "") or ""
    execution = RegradeExecution(exit_code=exit_code, stderr_tail=stderr[-2000:])

    new_job_dir = Path(plan.job_dir)
    if exit_code != 0 or not new_job_dir.is_dir():
        return RegradeJobReceiptV1(
            **base,
            verdict=RegradeJobVerdict.REFUSED,
            trials=[],
            refusals=[
                RegradeRefusalCode.HARBOR_INVOCATION_FAILED
                if exit_code != 0
                else RegradeRefusalCode.REGRADE_RESULT_UNREADABLE
            ],
            invocation=invocation,
            execution=execution,
        )

    entries = _collect_job_trials(plan=plan, new_job_dir=new_job_dir)
    compared = sum(1 for entry in entries if not entry.refused)
    verdict = (
        RegradeJobVerdict.COMPLETE
        if compared == len(entries) and entries
        else RegradeJobVerdict.PARTIAL
        if compared
        else RegradeJobVerdict.REFUSED
    )
    receipt = RegradeJobReceiptV1(
        **base,
        verdict=verdict,
        trials=entries,
        invocation=invocation,
        execution=execution,
    )
    if write_receipt:
        _write_job_receipts(receipt, new_job_dir)
    return receipt


def render_job_plan(plan: RegradeJobPlan) -> str:
    """Human summary of a preview for CLI dry-run output."""
    head = (
        f"{'runnable' if plan.runnable else 'refused'}: "
        f"{sum(1 for trial in plan.trials if trial.eligible)}/{len(plan.trials)} "
        f"trial(s) {plan.source_job_dir} -> {plan.job_dir}"
    )
    lines = [head]
    if plan.skipped_trials:
        lines.append(f"not passing, not replayed: {len(plan.skipped_trials)} trial(s)")
    if plan.refusals:
        lines.append("job refusals: " + ", ".join(code.value for code in plan.refusals))
    for trial in plan.trials:
        state = "eligible" if trial.eligible else "refused"
        detail = "" if trial.eligible else " [" + ", ".join(
            code.value for code in trial.refusals
        ) + "]"
        lines.append(
            f"  {state}: {Path(trial.source_trial_dir).name} "
            f"task={trial.task_name}{detail}"
            + (": " + "; ".join(trial.details) if trial.details else "")
        )
    return "\n".join(lines)


def render_job_receipt(receipt: RegradeJobReceiptV1) -> str:
    """Human summary of a job receipt for CLI output."""
    head = (
        f"{receipt.verdict.value}: {receipt.n_compared} compared, "
        f"{receipt.n_refused} refused — {receipt.source_job_dir} -> {receipt.job_dir}"
    )
    if receipt.skipped_trials:
        head += f"; {len(receipt.skipped_trials)} non-passing trial(s) not replayed"
    if receipt.refused and receipt.refusals:
        head += " [" + ", ".join(code.value for code in receipt.refusals) + "]"
    return "\n".join([head, *(render_receipt(trial) for trial in receipt.trials)])
