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
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import Field

from evallab.benchmark_program_contracts import canonical_json, compute_prefixed_sha256
from evallab.schemas import ContractModel

__all__ = [
    "REGRADE_TRIAL_NAME_SUFFIX",
    "RegradeExecution",
    "RegradeInvocation",
    "RegradeReceiptV1",
    "RegradeRefusalCode",
    "RegradeVerdict",
    "RewardObservation",
    "SourceTrialIdentity",
    "VerifierIdentity",
    "build_regrade_command",
    "read_reward_observation",
    "regrade_trial",
    "source_trial_identity",
    "verifier_identity",
]

#: Marker appended to generated regrade trial names. Regrade outputs are trials
#: in their own right; the suffix keeps them recognisable in a jobs tree without
#: parsing `config.source_trial`.
REGRADE_TRIAL_NAME_SUFFIX = "regrade"

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


class VerifierIdentity(ContractModel):
    """Content identity of the verifier used for a re-scoring.

    Digested over the declared `[verifier]` table plus every file in the
    verifier build context, so "did the verifier change?" is answerable without
    trusting a version string.
    """

    task_dir: str
    task_name: str | None = None
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


def _load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _digest_file(path: Path) -> str | None:
    try:
        return compute_prefixed_sha256(path.read_bytes().decode("utf-8", errors="surrogateescape"))
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
    mode = verifier_map.get("environment_mode")
    return VerifierIdentity(
        task_dir=str(task_dir),
        task_name=_optional_str(task_map.get("name")),
        environment_mode=str(mode) if isinstance(mode, str) else None,
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


def _preflight(trial_dir: Path, task_dir: Path) -> list[RegradeRefusalCode]:
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
    if verifier_map.get("environment_mode") != "separate":
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
    receipt = _load_json(trial_dir / "regrade-receipt.json")
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
    """
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
        (regrade_dir / "regrade-receipt.json").write_text(
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
