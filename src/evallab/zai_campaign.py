"""Cost-bounded Action Memory campaign compiler and durable runner (Z.ai lane).

This module turns the *cost-bounded Action Memory plan* into a deterministic,
fail-closed campaign: a 38-trial conditional design with a 36-trial measured-dose
phase A (4k/16k/64k) and a two-trial 128k cost canary in phase B. The design is
derived from the committed analyst roadmap spec
``research/roadmap/specs/campaign-0-action-memory-dose-ladder.json`` and the
certified generator envelope in ``library/benchmarks/action-memory-v1``; every
count (36 + 2 = 38) and ceiling (7,000,000 + 2,500,000 = 9,500,000 prompt tokens)
is cross-asserted there by ``verify_roadmap_claims.py``.

SECURITY BOUNDARY — read before using this lane.

The campaign consumes existing materialized task directories as inputs; it never
generates tasks. It stages a *provider-only* OpenCode auth document by reading
the OpenCode auth file (``~/.codex/auth.json`` by default), retaining only the
Z.ai provider entries, and writing them to a restrictive-permission file. The
values are never printed, logged, or serialised into a manifest: every public
surface (manifests, status, coverage reports, CLI output) carries only an
``AuthShape`` whose fields are provider-key names and booleans. Failures are
refused without echoing a secret.

Durable invariants this module holds:

- **Fail-closed refusal.** The campaign is refused (raises) when the projected
  phase-A token spend exceeds its ceiling, when a phase admits an unmeasured
  dose, when a model is outside the allowlist, when a Z.ai provider entry is
  absent, or when the trial count / provider budget are inconsistent.
- **Deterministic, resumable identity.** Every trial derives a stable
  ``trial_id`` and ``job_identity`` from its (phase, cell, rep) coordinates, so
  a resumed run reuses the exact job identity of a partially settled run.
- **Conditional phase B.** Phase B (the 128k cost canary) runs only after phase
  A settles and its measured usage stays inside the phase-A ceiling; otherwise
  it is recorded as ``skipped`` with a reason and never executes.
- **Non-scored classification.** Provider-access refusals (e.g. the
  unsubscribed ``zai-coding-plan/glm-5.3-highspeed`` HTTP 429) and harness
  infrastructure exceptions are classified separately from scored trials and
  are **excluded from reward denominators** — they are never relabelled as
  reward 0.0.
- **Cleanup in ``finally``.** The staged auth file is removed and the state lock
  released even when a trial or the whole run raises.
- **Prompt-token ceilings.** Per-trial (phase B 1,250,000; phase A derived from
  the measured per-dose cost), per-phase (7,000,000 / 2,500,000) and provider
  (9,500,000) ceilings are validated before dispatch and enforced while the run
  progresses.

The trial execution backend is pluggable (``TrialRunner`` protocol) so the state
machine, budgets, classification, gating, and cleanup are fully testable without
a live Harbor or a paid provider. The production backend
(:class:`HarborZaiTrialRunner`) composes a pinned Harbor 0.21 run through the
repo-owned Z.ai/OpenCode adapter and is only invoked under an explicit
``allow_billable`` authorization, matching ``policy``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import ConfigDict, Field, model_validator

from evallab.schemas import ContractModel

# --------------------------------------------------------------------------- #
# Identity, schema and design constants
# --------------------------------------------------------------------------- #

#: The campaign identity pinned by the roadmap spec.
CAMPAIGN_ID = "campaign-0-action-memory"
CAMPAIGN_DESIGN_VERSION = "zai-campaign-design/v1"
SCHEMA_MANIFEST = "zai-campaign-manifest/v1"
SCHEMA_STATUS = "zai-campaign-status/v1"
SCHEMA_AUTH_SHAPE = "zai-auth-shape/v1"

#: The single allowed provider prefix. The Z.ai credential is scoped to it; a
#: run can never point the credential at another vendor.
PROVIDER_PREFIX = "zai-coding-plan/"

#: The two Z.ai Coding Plan models admitted by the subscription (2026-08-29
#: pilot: both ran successfully through the pinned adapter).
ALLOWED_MODELS: frozenset[str] = frozenset(
    {"zai-coding-plan/glm-5.3", "zai-coding-plan/glm-5.3-flash"}
)

#: The unsubscribed model. The provider answers HTTP 429 with no model outcome;
#: it is an access-gated lane at n=0, never a scored trial or a refusal rate.
HIGHSPEED_SELECTOR = "zai-coding-plan/glm-5.3-highspeed"
HIGHSPEED_MODEL_NAME = "glm-5.3-highspeed"

#: OpenCode release every install uses unless an explicit override is passed.
PINNED_OPENCODE_VERSION = "1.18.25"
#: Trajectory schema every trial must record (per the roadmap spec lane block).
TRAJECTORY_SCHEMA = "ATIF-v1.7"
#: Harbor release the campaign lane targets.
HARBOR_VERSION = "0.21"

#: Top-level OpenCode auth provider key(s) that carry the Z.ai Coding Plan
#: credential. Filtering keys by identity (never by scanning values) guarantees
#: a token value is never read to decide retention.
ZAI_AUTH_PROVIDER_KEYS: tuple[str, ...] = ("zai",)

#: Default OpenCode auth file location (Codex-compatible subscription auth).
DEFAULT_OPENCODE_AUTH = Path("~/.codex/auth.json").expanduser()

#: Restrictive permission bits for the staged provider-only auth document.
AUTH_FILE_MODE = 0o600

# --------------------------------------------------------------------------- #
# Certified design numbers (cross-asserted by research/roadmap/verify_roadmap_claims.py)
# --------------------------------------------------------------------------- #

DOSE_AXIS_VERSION = "am-dose-ladder-v1"
PHASE_A_DOSES: tuple[int, ...] = (4096, 16384, 65536)
PHASE_B_DOSES: tuple[int, ...] = (131072,)
ARMS: tuple[str, ...] = ("neutral_padding", "semantic_distractor")
SEEDS: tuple[int, ...] = (42, 1337, 2026)
PHASE_A_REPS = 2
PHASE_B_REPS = 2

#: Measured per-trial input tokens by dose (recomputed from promoted bundles).
#: 128k has NEVER run, so its cost is None and it can only be admitted through
#: the phase-B cost canary (per-trial ceiling, aborted on exceed).
MEASURED_COST_BASIS: dict[int, int | None] = {
    4096: 32_056,
    16384: 79_497,
    65536: 412_753,
    131072: None,
}

PHASE_A_CELLS = len(PHASE_A_DOSES) * len(ARMS) * len(SEEDS)  # 18
PHASE_A_TRIALS = PHASE_A_CELLS * PHASE_A_REPS  # 36
PHASE_B_TRIALS = len(PHASE_B_DOSES) * 1 * 1 * PHASE_B_REPS  # 2 (neutral, s42)
TOTAL_TRIALS = PHASE_A_TRIALS + PHASE_B_TRIALS  # 38

PHASE_A_PROJECTED_INPUT_TOKENS = 6_291_672
PHASE_A_CEILING_INPUT_TOKENS = 7_000_000
PHASE_B_CEILING_INPUT_TOKENS = 2_500_000
PHASE_B_PER_TRIAL_CEILING_INPUT_TOKENS = 1_250_000
PROVIDER_TOKEN_BUDGET = 9_500_000

#: Guardrail multiplier on the measured per-dose cost used to derive the phase-A
#: per-trial ceiling. A single trial may not run away from its measured basis.
PHASE_A_PER_TRIAL_GUARDRAIL = 1.25

#: Default concurrency ceiling for the billable lane. Single-trial concurrency
#: is the deterministic, rate-safe default; the definition may raise it.
DEFAULT_MAX_CONCURRENCY = 1

STATE_ROOT_DEFAULT = Path("runs/zai-campaigns")

SAFE_JOB_PREFIX = "zai-am-campaign"
SAFE_JOB_MAX_LENGTH = 80


class ZaiCampaignError(RuntimeError):
    """Base failure for the Z.ai Action Memory campaign."""


class ZaiCampaignBudgetError(ZaiCampaignError):
    """Refused: a projected or observed budget violates a declared ceiling."""


class ZaiCampaignModelError(ZaiCampaignError):
    """Refused: a model selector is outside the Z.ai Coding Plan allowlist."""


class ZaiCampaignAuthError(ZaiCampaignError):
    """Refused: no Z.ai provider credential is available for staging."""


class ZaiCampaignTaskError(ZaiCampaignError):
    """Refused: a required task directory is missing or unsafe."""


class ZaiCampaignPreconditionError(ZaiCampaignError):
    """Refused: a lane precondition (isolation/pin/ceiling-consistency) is unmet."""


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _mkstemp_0600(directory: Path) -> tuple[int, str]:
    """``tempfile.mkstemp`` under ``AUTH_FILE_MODE``; content is never world-readable."""
    descriptor, name = tempfile.mkstemp(dir=str(directory), prefix=".zai-tmp-")
    os.chmod(name, AUTH_FILE_MODE)
    return descriptor, name


def _write_atomic(path: Path, payload: bytes) -> None:
    """Write ``payload`` to ``path`` atomically under restrictive permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, tmp_name = _mkstemp_0600(path.parent)
    try:
        os.write(descriptor, payload)
        os.close(descriptor)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _safe_job_name(value: str) -> str:
    cleaned = "".join(
        character if (character.isalnum() or character == "-") else "-"
        for character in value.lower()
    ).strip("-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    if not cleaned:
        raise ValueError("cannot derive a safe job identity from an empty name")
    if len(cleaned) > SAFE_JOB_MAX_LENGTH:
        digest = hashlib.sha256(cleaned.encode()).hexdigest()[:8]
        cleaned = f"{cleaned[:SAFE_JOB_MAX_LENGTH - 9]}-{digest}"
    return cleaned


def dose_measured_input_tokens(dose_bytes: int) -> int | None:
    """Return the measured per-trial input tokens for ``dose_bytes`` (None when unmeasured)."""
    return MEASURED_COST_BASIS.get(dose_bytes)


def base_task_pair_id(dose_bytes: int, seed: int) -> str:
    return f"{DOSE_AXIS_VERSION}-s{seed}-d{dose_bytes}"


def cell_id_for(arm: str, dose_bytes: int, seed: int) -> str:
    return f"dl-{arm.replace('_', '-')}-{dose_bytes}-s{seed}"


def pairing_key_of(trial: "ZaiTrial | Mapping[str, Any]") -> tuple[str, int, int]:
    """The matched-contrast pairing key ``(task_block_id, dose_bytes, seed)``."""
    if isinstance(trial, ZaiTrial):
        return trial.task_block_id, trial.dose_bytes, trial.seed
    return (
        str(trial["task_block_id"]),
        int(trial["dose_bytes"]),
        int(trial["seed"]),
    )


# --------------------------------------------------------------------------- #
# Pydantic contracts
# --------------------------------------------------------------------------- #


class _FrozenContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ZaiAuthShape(_FrozenContract):
    """Non-secret description of what a provider-only auth staging produced.

    Deliberately contains no credential values: provider keys, booleans, and a
    count only. Constructed by :func:`describe_auth_shape`, which never reads a
    token value to decide retention.
    """

    schema_version: Literal["zai-auth-shape/v1"] = SCHEMA_AUTH_SHAPE
    source_present: bool
    zai_present: bool
    retained_provider_keys: tuple[str, ...]
    retained_entry_count: int
    staged_path: str | None = None

    def to_redacted(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_present": self.source_present,
            "zai_present": self.zai_present,
            "retained_provider_keys": list(self.retained_provider_keys),
            "retained_entry_count": self.retained_entry_count,
            "staged_path": self.staged_path,
        }


class ZaiPhaseSpec(_FrozenContract):
    """One campaign phase: dose/arm/seed space, repetitions and token ceilings."""

    name: Literal["a", "b"]
    doses: tuple[int, ...] = Field(min_length=1)
    arms: tuple[str, ...] = Field(min_length=1)
    seeds: tuple[int, ...] = Field(min_length=1)
    reps: int = Field(ge=1)
    ceiling_input_tokens: int = Field(ge=1)
    per_trial_ceiling_input_tokens: int | None = Field(default=None, ge=1)


class ZaiCampaignLimits(_FrozenContract):
    """Provider-scoped ceilings and lane pins."""

    max_trials: int = Field(ge=1)
    prompt_token_budget: int = Field(ge=1)
    max_concurrency: int = Field(default=DEFAULT_MAX_CONCURRENCY, ge=1)
    opencode_pin: str = PINNED_OPENCODE_VERSION
    harbor_version: str = HARBOR_VERSION
    trajectory_schema: str = TRAJECTORY_SCHEMA
    #: Host-isolation and credential-proxy preconditions are owned by the lane
    #: operator (E2 certification). The runner records whether they were declared
    #: satisfied; it never fabricates evidence for them.
    host_isolation_enforced: bool = False
    credential_proxy_holds_secret: bool = False


class ZaiCampaignDefinition(_FrozenContract):
    """The complete, versioned cost-bounded Action Memory campaign design."""

    schema_version: Literal["zai-campaign-design/v1"] = CAMPAIGN_DESIGN_VERSION
    campaign_id: str = CAMPAIGN_ID
    design_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    lane_model: str = Field(min_length=1)
    phases: tuple[ZaiPhaseSpec, ...] = Field(min_length=2)
    limits: ZaiCampaignLimits

    @model_validator(mode="after")
    def _validate_digest_and_lane(self) -> "ZaiCampaignDefinition":
        if campaign_design_digest(self) != self.design_digest:
            raise ValueError("zai campaign design digest mismatch")
        if self.lane_model not in ALLOWED_MODELS:
            raise ZaiCampaignModelError(
                f"lane model {self.lane_model!r} is outside the Z.ai Coding Plan "
                "allowlist"
            )
        names = [phase.name for phase in self.phases]
        if names != ["a", "b"]:
            raise ValueError("zai campaign phases must be exactly phase-a then phase-b")
        return self

    def phase(self, name: Literal["a", "b"]) -> ZaiPhaseSpec:
        return next(phase for phase in self.phases if phase.name == name)


class ZaiTrial(_FrozenContract):
    """One compiled trial with a deterministic identity and token ceiling."""

    trial_id: str = Field(min_length=1)
    phase: Literal["a", "b"]
    cell_id: str = Field(min_length=1)
    task_block_id: str = Field(min_length=1)
    task_path: str = Field(min_length=1)
    dose_bytes: int = Field(ge=1)
    arm: str = Field(min_length=1)
    seed: int
    rep: int = Field(ge=1)
    model: str = Field(min_length=1)
    prompt_token_ceiling: int = Field(ge=1)
    job_identity: str = Field(min_length=1)


class ZaiManifest(_FrozenContract):
    """Frozen result of a deterministic compile: the 38-trial conditional design."""

    schema_version: Literal["zai-campaign-manifest/v1"] = SCHEMA_MANIFEST
    campaign_id: str = CAMPAIGN_ID
    design_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    phase_a: tuple[ZaiTrial, ...] = Field(min_length=1)
    phase_b: tuple[ZaiTrial, ...] = Field(min_length=1)
    manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @property
    def trials(self) -> tuple[ZaiTrial, ...]:
        return self.phase_a + self.phase_b

    @property
    def total_trials(self) -> int:
        return len(self.phase_a) + len(self.phase_b)

    @model_validator(mode="after")
    def _immutable_and_consistent(self) -> "ZaiManifest":
        if self.campaign_id != CAMPAIGN_ID:
            raise ValueError("zai campaign manifest identity mismatch")
        if self.total_trials != TOTAL_TRIALS:
            raise ZaiCampaignError(
                f"compiled campaign has {self.total_trials} trials, expected {TOTAL_TRIALS}"
            )
        if zai_manifest_digest(self) != self.manifest_digest:
            raise ValueError("zai campaign manifest digest mismatch")
        ids = [trial.trial_id for trial in self.trials]
        if len(ids) != len(set(ids)):
            raise ValueError("zai campaign manifest contains duplicate trial ids")
        return self


#: A trial that settled with a non-scored classification, or a planned attempt.
AttemptKind = Literal[
    "scored",
    "provider_access_refused",
    "harness_infra_exception",
    "skipped",
    "quarantined",
    "unresolved",
]


class ZaiAttemptRecord(_FrozenContract):
    """Durable per-trial record: identity, classification, and measured usage."""

    trial_id: str
    job_identity: str
    kind: AttemptKind
    reward: float | None = None
    prompt_tokens: int | None = None
    reason: str | None = None
    occurred_at: str | None = None


class ZaiCampaignStatus(_FrozenContract):
    """Resumable status snapshot of a running/settled campaign."""

    schema_version: Literal["zai-campaign-status/v1"] = SCHEMA_STATUS
    campaign_id: str = CAMPAIGN_ID
    manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    state: Literal["planned", "running", "phase_a_complete", "complete", "refused"]
    attempts: tuple[ZaiAttemptRecord, ...]
    phase_b_reason: str | None = None
    phase_b_skipped: bool = False
    prompt_tokens_used: int = 0


@runtime_checkable
class TrialRunner(Protocol):
    """Execution backend for a single compiled trial."""

    def run_trial(
        self,
        trial: ZaiTrial,
        *,
        staged_auth_path: Path,
        attempt_id: str,
    ) -> "TrialOutcome": ...


@dataclass(frozen=True)
class TrialOutcome:
    """Outcome of one trial as returned by a :class:`TrialRunner`."""

    trial_id: str
    job_identity: str
    kind: AttemptKind
    reward: float | None = None
    prompt_tokens: int | None = None
    reason: str | None = None
    #: Optional per-trial verifier retrieval evidence (expected/issued handles).
    verifier_evidence: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class RetrievalFidelity:
    """Separate coverage, unknown, omitted, duplicate and order-fidelity figures."""

    coverage_unique: float | None = None  # unique issued / expected
    unknown: int = 0
    omitted: int = 0
    duplicate: int = 0
    order_fidelity: bool | None = None


@dataclass(frozen=True)
class MatchedContrastRow:
    """One pairing-key row in the matched-contrast coverage report."""

    pairing_key: tuple[str, int, int]
    task_block_id: str
    dose_bytes: int
    seed: int
    planned_trials: int = 0
    scored: int = 0
    non_scored: int = 0
    unknown: int = 0
    omitted: int = 0
    duplicate: int = 0
    order_fidelity: bool | None = None
    arms_present: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Model allowlist
# --------------------------------------------------------------------------- #


def validate_model(model: str | None) -> str:
    """Return ``model`` when it is in the Z.ai Coding Plan allowlist.

    Raises :class:`ZaiCampaignModelError` otherwise. A model selector is not a
    secret, so the offending value may appear in the message.
    """
    if not model:
        raise ZaiCampaignModelError("a Z.ai trial requires a provider/model selector")
    if model not in ALLOWED_MODELS:
        raise ZaiCampaignModelError(
            f"model {model!r} is outside the Z.ai Coding Plan allowlist "
            f"{sorted(ALLOWED_MODELS)}"
        )
    return model


def model_access_kind(model: str | None) -> AttemptKind | None:
    """Classify an unsubscribed model as provider-access (never scored)."""
    if model == HIGHSPEED_SELECTOR or (
        model and model.rsplit("/", 1)[-1] == HIGHSPEED_MODEL_NAME
    ):
        return "provider_access_refused"
    return None


# --------------------------------------------------------------------------- #
# Provider-only auth staging (never prints or serialises a value)
# --------------------------------------------------------------------------- #


def read_opencode_auth(path: Path) -> Mapping[str, Any]:
    """Read and parse an OpenCode auth document.

    Raises :class:`ZaiCampaignAuthError` when the file is missing or unreadable.
    Returns the parsed mapping; values are never printed by this module.
    """
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise ZaiCampaignAuthError(
            f"OpenCode auth file is not a regular file: {resolved}"
        )
    try:
        raw = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise ZaiCampaignAuthError(
            f"cannot read OpenCode auth file: {resolved}"
        ) from exc
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ZaiCampaignAuthError(
            f"OpenCode auth file is not valid JSON: {resolved}"
        ) from exc
    if not isinstance(doc, dict):
        raise ZaiCampaignAuthError("OpenCode auth document must be a JSON object")
    return doc


def _is_zai_provider_key(key: str) -> bool:
    return key in ZAI_AUTH_PROVIDER_KEYS or "zai" in key.lower()


def filter_zai_auth(auth: Mapping[str, Any]) -> dict[str, Any]:
    """Return only the top-level Z.ai provider entries.

    Retention is decided by provider-key identity alone; a credential value is
    never read, copied, or logged to decide retention. Entries that mention the
    Z.ai provider under a non-matching key are deliberately dropped rather than
    risk copying a foreign vendor's token.
    """
    return {key: value for key, value in auth.items() if _is_zai_provider_key(key)}


def describe_auth_shape(
    auth: Mapping[str, Any],
    *,
    staged_path: Path | None = None,
) -> ZaiAuthShape:
    """Non-secret shape of an auth document / staging result."""
    zai_entries = filter_zai_auth(auth)
    return ZaiAuthShape(
        source_present=True,
        zai_present=bool(zai_entries),
        retained_provider_keys=tuple(sorted(zai_entries)),
        retained_entry_count=len(zai_entries),
        staged_path=staged_path.as_posix() if staged_path is not None else None,
    )


def stage_provider_auth(
    auth_path: Path,
    destination: Path,
) -> tuple[Path, ZaiAuthShape]:
    """Stage a provider-only (Z.ai) OpenCode auth document.

    Reads ``auth_path``, retains only the Z.ai provider entries, and writes them
    to ``destination`` with restrictive permissions (``0o600``) atomically.
    Returns the staged path and a non-secret :class:`ZaiAuthShape`. Raises
    :class:`ZaiCampaignAuthError` when no Z.ai provider entry is present; the
    staged file is removed on refusal.
    """
    doc = read_opencode_auth(auth_path)
    zai_entries = filter_zai_auth(doc)
    if not zai_entries:
        raise ZaiCampaignAuthError(
            "OpenCode auth contains no Z.ai provider entry; the campaign cannot "
            "stage a provider-only credential"
        )
    destination = Path(destination)
    payload = (_canonical_json(zai_entries) + "\n").encode("utf-8")
    _write_atomic(destination, payload)
    shape = describe_auth_shape(doc, staged_path=destination)
    return destination, shape


def require_zai_auth(auth_path: Path) -> ZaiAuthShape:
    """Fail-closed auth precondition: a Z.ai provider entry must exist."""
    doc = read_opencode_auth(auth_path)
    shape = describe_auth_shape(doc)
    if not shape.zai_present:
        raise ZaiCampaignAuthError(
            "no Z.ai provider entry is present in the OpenCode auth document"
        )
    return shape


# --------------------------------------------------------------------------- #
# Deterministic compilation
# --------------------------------------------------------------------------- #


def default_phase_a_spec() -> ZaiPhaseSpec:
    return ZaiPhaseSpec(
        name="a",
        doses=PHASE_A_DOSES,
        arms=ARMS,
        seeds=SEEDS,
        reps=PHASE_A_REPS,
        ceiling_input_tokens=PHASE_A_CEILING_INPUT_TOKENS,
    )


def default_phase_b_spec() -> ZaiPhaseSpec:
    return ZaiPhaseSpec(
        name="b",
        doses=PHASE_B_DOSES,
        arms=("neutral_padding",),
        seeds=(42,),
        reps=PHASE_B_REPS,
        ceiling_input_tokens=PHASE_B_CEILING_INPUT_TOKENS,
        per_trial_ceiling_input_tokens=PHASE_B_PER_TRIAL_CEILING_INPUT_TOKENS,
    )


def default_campaign_limits(
    *,
    host_isolation_enforced: bool = False,
    credential_proxy_holds_secret: bool = False,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
) -> ZaiCampaignLimits:
    return ZaiCampaignLimits(
        max_trials=TOTAL_TRIALS,
        prompt_token_budget=PROVIDER_TOKEN_BUDGET,
        max_concurrency=max_concurrency,
        host_isolation_enforced=host_isolation_enforced,
        credential_proxy_holds_secret=credential_proxy_holds_secret,
    )


def build_default_definition(
    *,
    lane_model: str = "zai-coding-plan/glm-5.3-flash",
    host_isolation_enforced: bool = False,
    credential_proxy_holds_secret: bool = False,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
) -> ZaiCampaignDefinition:
    """Build the certified Action Memory campaign definition."""
    validate_model(lane_model)
    definition = ZaiCampaignDefinition(
        campaign_id=CAMPAIGN_ID,
        design_digest="sha256:" + "0" * 64,  # placeholder, recomputed below
        lane_model=lane_model,
        phases=(default_phase_a_spec(), default_phase_b_spec()),
        limits=default_campaign_limits(
            host_isolation_enforced=host_isolation_enforced,
            credential_proxy_holds_secret=credential_proxy_holds_secret,
            max_concurrency=max_concurrency,
        ),
    )
    object.__setattr__(definition, "design_digest", campaign_design_digest(definition))
    return definition


def campaign_design_digest(
    definition: ZaiCampaignDefinition | Mapping[str, Any],
) -> str:
    if isinstance(definition, ZaiCampaignDefinition):
        payload = definition.model_dump(mode="json", exclude={"design_digest"})
    else:
        payload = {
            key: value
            for key, value in dict(definition).items()
            if key != "design_digest"
        }
    return _digest(payload)


def trial_prompt_token_ceiling(phase: ZaiPhaseSpec, dose_bytes: int) -> int:
    """Per-trial prompt-token ceiling for a trial at ``dose_bytes`` in ``phase``."""
    if phase.per_trial_ceiling_input_tokens is not None:
        return phase.per_trial_ceiling_input_tokens
    measured = dose_measured_input_tokens(dose_bytes)
    if measured is None:
        raise ZaiCampaignBudgetError(
            f"dose {dose_bytes} is unmeasured and has no per-trial ceiling; it can "
            "only be admitted via the phase-B cost canary"
        )
    return max(1, int(measured * PHASE_A_PER_TRIAL_GUARDRAIL))


def _compile_phase(
    phase: ZaiPhaseSpec,
    *,
    lane_model: str,
    task_root: Path,
) -> list[ZaiTrial]:
    trials: list[ZaiTrial] = []
    for dose_bytes in sorted(phase.doses):
        for seed in sorted(phase.seeds):
            for arm in sorted(phase.arms):
                block = base_task_pair_id(dose_bytes, seed)
                cell = cell_id_for(arm, dose_bytes, seed)
                task_path = str(task_root / f"{cell}.task")
                ceiling = trial_prompt_token_ceiling(phase, dose_bytes)
                for rep in range(1, phase.reps + 1):
                    trial_id = f"{phase.name}-{cell}-r{rep}"
                    job_identity = _safe_job_name(
                        f"{SAFE_JOB_PREFIX}-{phase.name}-{cell}-r{rep}"
                    )
                    trials.append(
                        ZaiTrial(
                            trial_id=trial_id,
                            phase=phase.name,
                            cell_id=cell,
                            task_block_id=block,
                            task_path=task_path,
                            dose_bytes=dose_bytes,
                            arm=arm,
                            seed=seed,
                            rep=rep,
                            model=lane_model,
                            prompt_token_ceiling=ceiling,
                            job_identity=job_identity,
                        )
                    )
    return trials


def compile_phase_a(
    definition: ZaiCampaignDefinition, *, task_root: Path
) -> list[ZaiTrial]:
    trials = _compile_phase(
        definition.phase("a"), lane_model=definition.lane_model, task_root=task_root
    )
    if len(trials) != PHASE_A_TRIALS:
        raise ZaiCampaignError(
            f"phase A compiled {len(trials)} trials, expected {PHASE_A_TRIALS}"
        )
    return trials


def compile_phase_b(
    definition: ZaiCampaignDefinition, *, task_root: Path
) -> list[ZaiTrial]:
    trials = _compile_phase(
        definition.phase("b"), lane_model=definition.lane_model, task_root=task_root
    )
    if len(trials) != PHASE_B_TRIALS:
        raise ZaiCampaignError(
            f"phase B compiled {len(trials)} trials, expected {PHASE_B_TRIALS}"
        )
    return trials


def compile_campaign(
    definition: ZaiCampaignDefinition,
    *,
    task_root: Path,
) -> ZaiManifest:
    """Deterministically compile the 38-trial conditional campaign.

    Task directories are inputs only: every trial is bound to an existing task
    directory under ``task_root`` (``<cell_id>.task``), which is validated here
    so a compile cannot reference a missing package. Generation is out of scope.
    """
    validate_task_root(task_root)
    phase_a = compile_phase_a(definition, task_root=task_root)
    phase_b = compile_phase_b(definition, task_root=task_root)
    raw: dict[str, Any] = {
        "schema_version": SCHEMA_MANIFEST,
        "campaign_id": CAMPAIGN_ID,
        "design_digest": definition.design_digest,
        "phase_a": [trial.model_dump(mode="json") for trial in phase_a],
        "phase_b": [trial.model_dump(mode="json") for trial in phase_b],
        "manifest_digest": "sha256:" + "0" * 64,
    }
    digest = zai_manifest_digest(raw)
    raw["manifest_digest"] = digest
    return ZaiManifest.model_validate(raw)


def zai_manifest_digest(manifest: ZaiManifest | Mapping[str, Any]) -> str:
    if isinstance(manifest, ZaiManifest):
        payload = manifest.model_dump(mode="json", exclude={"manifest_digest"})
    else:
        payload = {
            key: value
            for key, value in dict(manifest).items()
            if key != "manifest_digest"
        }
    return _digest(payload)


# --------------------------------------------------------------------------- #
# Budget admission (fail closed)
# --------------------------------------------------------------------------- #


def project_phase_a_input_tokens(definition: ZaiCampaignDefinition) -> int:
    """Recompute the phase-A projection from the measured per-dose basis."""
    total = 0
    phase = definition.phase("a")
    trials_at_dose = (
        len(phase.arms) * len(phase.seeds) * phase.reps
    )
    for dose in phase.doses:
        measured = dose_measured_input_tokens(dose)
        if measured is None:
            raise ZaiCampaignBudgetError(
                f"phase A dose {dose} has no measured cost and cannot be budgeted"
            )
        total += measured * trials_at_dose
    return total


def check_budget_admission(definition: ZaiCampaignDefinition) -> None:
    """Refuse the campaign when any token ceiling is violated (fail closed).

    Raises :class:`ZaiCampaignBudgetError` on the first unmet ceiling. This is
    called before dispatch and again after phase A settles.
    """
    phase_a = definition.phase("a")
    phase_b = definition.phase("b")

    # Phase-A projection must stay under its own ceiling.
    projected = project_phase_a_input_tokens(definition)
    if projected > phase_a.ceiling_input_tokens:
        raise ZaiCampaignBudgetError(
            f"phase A projects {projected} input tokens, exceeding its "
            f"{phase_a.ceiling_input_tokens} ceiling"
        )
    # Phase ceilings must sum into the provider token budget.
    if (
        phase_a.ceiling_input_tokens + phase_b.ceiling_input_tokens
        > definition.limits.prompt_token_budget
    ):
        raise ZaiCampaignBudgetError(
            "phase ceilings exceed the provider prompt-token budget"
        )
    # An unmeasured dose must never be admitted into a budgetable phase.
    for dose in phase_a.doses:
        if dose_measured_input_tokens(dose) is None:
            raise ZaiCampaignBudgetError(
                f"phase A dose {dose} is unmeasured; an unmeasured dose cannot be budgeted"
            )
    # Trial-count ceiling consistency: the provider max_trials must admit the design.
    if definition.limits.max_trials < TOTAL_TRIALS:
        raise ZaiCampaignBudgetError(
            f"provider max_trials {definition.limits.max_trials} admits fewer than "
            f"the {TOTAL_TRIALS} runnable trials"
        )


def check_lane_preconditions(definition: ZaiCampaignDefinition) -> None:
    """Refuse when host-isolation or credential-proxy preconditions are unmet."""
    if not definition.limits.host_isolation_enforced:
        raise ZaiCampaignPreconditionError(
            "host-isolation precondition unmet: network_isolation_enforced is "
            "False on this lane; causal-grade promotion requires a Linux "
            "enforced-isolation host"
        )
    if not definition.limits.credential_proxy_holds_secret:
        raise ZaiCampaignPreconditionError(
            "credential-proxy precondition unmet: the credential must be held "
            "outside the task container by a proxy"
        )


# --------------------------------------------------------------------------- #
# Task root validation (inputs only, generation out of scope)
# --------------------------------------------------------------------------- #


def validate_task_root(task_root: Path) -> None:
    """Confirm ``task_root`` is a real directory (inputs exist)."""
    if not task_root.is_dir():
        raise ZaiCampaignTaskError(f"task root is not a directory: {task_root}")


def require_trial_task(trial: ZaiTrial) -> None:
    """Confirm the trial's bound task package directory exists."""
    path = Path(trial.task_path)
    if not path.is_dir():
        raise ZaiCampaignTaskError(
            f"trial {trial.trial_id} references a missing task directory: {trial.task_path}"
        )


# --------------------------------------------------------------------------- #
# Attempt classification (non-scored attempts never enter reward denominators)
# --------------------------------------------------------------------------- #


def is_scored(kind: AttemptKind) -> bool:
    return kind == "scored"


def classify_attempt(outcome: Mapping[str, Any] | TrialOutcome) -> tuple[AttemptKind, str | None]:
    """Classify an attempt outcome.

    Rules (fail closed):

    - A scored verifier reward is ``scored`` and enters the reward denominator.
    - ``provider_http_429`` / a provider-access marker, or an unsubscribed
      model (Highspeed), is ``provider_access_refused`` — never a reward 0.0.
    - A harness infrastructure exception (timeout/build/compose/harbor) is
      ``harness_infra_exception`` — non-scored.
    - An explicit skip/hold marker is ``skipped``.
    - Otherwise the attempt is ``unresolved``.
    """
    if isinstance(outcome, TrialOutcome):
        return outcome.kind, outcome.reason
    exception = outcome.get("exception_class") or outcome.get("exception_type")
    message = str(outcome.get("exception_message") or "")
    model = outcome.get("model")
    marker = str(outcome.get("classification") or "")
    if outcome.get("skip_reason") is not None:
        return "skipped", str(outcome["skip_reason"])
    if outcome.get("reward_present"):
        return "scored", None
    if (
        "provider_http_429" in marker
        or "429" in message
        or "subscription plan does not yet include access" in message.lower()
        or model_access_kind(model) == "provider_access_refused"
    ):
        return "provider_access_refused", "provider subscription does not admit the model"
    if exception in {
        "AgentTimeoutError",
        "EnvironmentBuildError",
        "DockerComposeError",
        "HarborError",
        "NonZeroAgentExitCodeError",
    } or marker == "harness_infra_exception":
        return "harness_infra_exception", f"harness infrastructure exception: {exception}"
    return "unresolved", "attempt did not settle into a scored or classified outcome"


def classify_verifier_retrieval(evidence: Mapping[str, Any] | None) -> RetrievalFidelity:
    """Separate coverage, unknown, omitted, duplicate and order fidelity.

    Reads expected/issued opaque-handle lists from per-trial verifier evidence.
    No evidence yields ``None`` coverage/fidelity with zero counts.
    """
    if not evidence:
        return RetrievalFidelity()
    expected = list(evidence.get("expected_handles") or [])
    issued = list(evidence.get("issued_handles") or [])
    if not expected:
        return RetrievalFidelity(
            coverage_unique=1.0 if issued else None,
            unknown=len(issued),
            omitted=0,
            duplicate=0,
            order_fidelity=None,
        )
    unique = list(dict.fromkeys(issued))
    unknown = len([handle for handle in unique if handle not in expected])
    omitted = len([handle for handle in expected if handle not in unique])
    duplicate = len(issued) - len(unique)
    coverage = len([handle for handle in unique if handle in expected]) / len(expected)
    return RetrievalFidelity(
        coverage_unique=round(coverage, 4),
        unknown=unknown,
        omitted=omitted,
        duplicate=duplicate,
        order_fidelity=list(unique) == list(expected),
    )


# --------------------------------------------------------------------------- #
# Matched-contrast coverage report
# --------------------------------------------------------------------------- #


def matched_contrast_report(
    manifest: ZaiManifest,
    attempts: Sequence[ZaiAttemptRecord] = (),
    evidence_by_trial: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[MatchedContrastRow]:
    """Report matched-contrast fidelity per pairing key ``(task_block_id, dose, seed)``.

    Pairs the neutral/semantic arms within a block. ``attempts`` (when supplied)
    contribute scored/non-scored counts; per-trial ``evidence_by_trial``
    contributes unknown/omitted/duplicate/order fidelity separately.
    """
    evidence_by_trial = evidence_by_trial or {}
    by_key: dict[tuple[str, int, int], MatchedContrastRow] = {}
    attempt_by_trial = {attempt.trial_id: attempt for attempt in attempts}
    for trial in manifest.trials:
        key = pairing_key_of(trial)
        row = by_key.setdefault(
            key,
            MatchedContrastRow(
                pairing_key=key,
                task_block_id=trial.task_block_id,
                dose_bytes=trial.dose_bytes,
                seed=trial.seed,
            ),
        )
        row = MatchedContrastRow(
            pairing_key=row.pairing_key,
            task_block_id=row.task_block_id,
            dose_bytes=row.dose_bytes,
            seed=row.seed,
            planned_trials=row.planned_trials + 1,
            arms_present=tuple(sorted({*row.arms_present, trial.arm})),
            scored=row.scored,
            non_scored=row.non_scored,
            unknown=row.unknown,
            omitted=row.omitted,
            duplicate=row.duplicate,
            order_fidelity=row.order_fidelity,
        )
        record = attempt_by_trial.get(trial.trial_id)
        if record is not None:
            if is_scored(record.kind):
                row = _row_with(row, scored=row.scored + 1)
            else:
                row = _row_with(row, non_scored=row.non_scored + 1)
        fidelity = classify_verifier_retrieval(evidence_by_trial.get(trial.trial_id))
        row = _row_with(
            row,
            unknown=row.unknown + fidelity.unknown,
            omitted=row.omitted + fidelity.omitted,
            duplicate=row.duplicate + fidelity.duplicate,
            order_fidelity=fidelity.order_fidelity,
        )
        by_key[key] = row
    return [by_key[key] for key in sorted(by_key)]


def _row_with(row: MatchedContrastRow, **changes: Any) -> MatchedContrastRow:
    return MatchedContrastRow(
        pairing_key=row.pairing_key,
        task_block_id=row.task_block_id,
        dose_bytes=row.dose_bytes,
        seed=row.seed,
        planned_trials=row.planned_trials,
        scored=changes.get("scored", row.scored),
        non_scored=changes.get("non_scored", row.non_scored),
        unknown=changes.get("unknown", row.unknown),
        omitted=changes.get("omitted", row.omitted),
        duplicate=changes.get("duplicate", row.duplicate),
        order_fidelity=changes.get("order_fidelity", row.order_fidelity),
        arms_present=row.arms_present,
    )


# --------------------------------------------------------------------------- #
# Concurrency gate
# --------------------------------------------------------------------------- #


class ConcurrencyGate:
    """Bound the number of concurrently executing trials.

    A ``threading.BoundedSemaphore`` so releases cannot over-release, plus an
    explicit ``active`` counter for observability.
    """

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("concurrency limit must be at least 1")
        self._limit = limit
        self._semaphore = threading.BoundedSemaphore(limit)
        self._active = 0

    @contextlib.contextmanager
    def slot(self) -> Iterator[None]:
        self._semaphore.acquire()
        self._active += 1
        try:
            yield
        finally:
            self._active -= 1
            self._semaphore.release()

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def active(self) -> int:
        return self._active


# --------------------------------------------------------------------------- #
# Durable state (resumable job identities)
# --------------------------------------------------------------------------- #


class ZaiCampaignState:
    """Resumable status store for one campaign, rooted under ``state_root``.

    The status file is rewritten atomically (temp + ``os.replace``) so a crash
    never leaves a partial snapshot. A process lock (``campaign.lock``) prevents
    two runners from mutating the same campaign concurrently.
    """

    def __init__(self, state_root: Path, campaign_id: str, manifest_digest: str) -> None:
        self.root = state_root / campaign_id
        self.status_path = self.root / "status.json"
        self.lock_path = self.root / "campaign.lock"

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.flock(descriptor, os.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                os.flock(descriptor, os.LOCK_UN)
            os.close(descriptor)

    def load(self) -> ZaiCampaignStatus | None:
        if not self.status_path.is_file():
            return None
        try:
            return ZaiCampaignStatus.model_validate_json(
                self.status_path.read_text(encoding="utf-8")
            )
        except Exception:
            return None

    def write(self, status: ZaiCampaignStatus) -> None:
        payload = (status.model_dump_json(indent=2) + "\n").encode("utf-8")
        _write_atomic(self.status_path, payload)


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


class RecorderTrialRunner:
    """Non-dispatch backend: records a planned outcome without a paid call.

    Used for deterministic ``launch --dry-run`` and tests. Never performs a
    provider/model call and never creates a billable authorization.
    """

    def __init__(
        self,
        kind: AttemptKind = "scored",
        reward: float = 1.0,
        prompt_tokens: int | None = None,
    ) -> None:
        self._kind = kind
        self._reward = reward
        self._prompt_tokens = prompt_tokens
        self.calls: list[str] = []

    def run_trial(
        self, trial: ZaiTrial, *, staged_auth_path: Path, attempt_id: str
    ) -> TrialOutcome:
        self.calls.append(trial.trial_id)
        return TrialOutcome(
            trial_id=trial.trial_id,
            job_identity=trial.job_identity,
            kind=self._kind,
            reward=self._reward if self._kind == "scored" else None,
            prompt_tokens=self._prompt_tokens,
        )


class ZaiCampaignRunner:
    """Durable state machine that compiles, gates, and settles the campaign.

    Holds the resumable identity, budget enforcement, non-scored classification,
    conditional phase-B gating, and ``finally`` cleanup of the staged auth and
    the state lock.
    """

    def __init__(
        self,
        *,
        definition: ZaiCampaignDefinition,
        task_root: Path,
        auth_path: Path,
        state_root: Path = STATE_ROOT_DEFAULT,
        runner: TrialRunner | None = None,
        clock: Any = None,
    ) -> None:
        self.definition = definition
        self.task_root = task_root
        self.auth_path = auth_path
        self.manifest = compile_campaign(definition, task_root=task_root)
        self.state = ZaiCampaignState(
            state_root, definition.campaign_id, self.manifest.manifest_digest
        )
        self.runner = runner or RecorderTrialRunner()
        self._clock = clock or (lambda: datetime.now(UTC))

    # -- preflight -------------------------------------------------------- #

    def preflight(self, *, require_isolation: bool = False) -> None:
        """Refuse on any unmet budget/model/auth/task precondition.

        Never leaks a credential value: only the non-secret auth shape is used.
        """
        check_budget_admission(self.definition)
        validate_model(self.definition.lane_model)
        if require_isolation:
            check_lane_preconditions(self.definition)
        require_zai_auth(self.auth_path)
        validate_task_root(self.task_root)
        for trial in self.manifest.trials:
            require_trial_task(trial)

    # -- execution -------------------------------------------------------- #

    def run(self, *, resume: bool = False, dry_run: bool = False) -> ZaiCampaignStatus:
        staged_auth: Path | None = None
        try:
            # Preflight before any dispatch; refusal is fail-closed.
            self.preflight()
            # Stage the provider-only auth once for the whole campaign session.
            staged_auth, _shape = self._stage_session_auth()
            with self.state.lock():
                return self._run_locked(
                    resume=resume,
                    dry_run=dry_run,
                    staged_auth_path=staged_auth,
                )
        finally:
            # Cleanup in finally: the staged auth file is removed even on raise,
            # and the lock is released by the ``lock()`` context manager.
            if staged_auth is not None:
                with contextlib.suppress(OSError):
                    staged_auth.unlink()
                with contextlib.suppress(OSError):
                    staged_auth.parent.rmdir()

    def _stage_session_auth(self) -> tuple[Path, ZaiAuthShape]:
        staging_dir = self.state.root / ".staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        return stage_provider_auth(self.auth_path, staging_dir / "zai-auth.json")

    def _run_locked(
        self,
        *,
        resume: bool,
        dry_run: bool,
        staged_auth_path: Path,
    ) -> ZaiCampaignStatus:
        existing = self.state.load()
        if existing is not None and not resume:
            if existing.state == "complete":
                return existing
            raise ZaiCampaignError("campaign already started; pass resume=True to continue")

        attempts = self._settle_phase(
            self.manifest.phase_a,
            attempts=existing.attempts if existing else (),
            staged_auth_path=staged_auth_path,
        )
        usage = sum(attempt.prompt_tokens or 0 for attempt in attempts)
        if usage > self.definition.phase("a").ceiling_input_tokens:
            status = ZaiCampaignStatus(
                campaign_id=self.definition.campaign_id,
                manifest_digest=self.manifest.manifest_digest,
                state="complete",
                attempts=attempts,
                phase_b_skipped=True,
                phase_b_reason="phase A exceeded its token ceiling; phase B is held",
                prompt_tokens_used=usage,
            )
            self.state.write(status)
            return status

        if any(attempt.kind == "unresolved" for attempt in attempts):
            status = ZaiCampaignStatus(
                campaign_id=self.definition.campaign_id,
                manifest_digest=self.manifest.manifest_digest,
                state="phase_a_complete",
                attempts=attempts,
                phase_b_skipped=True,
                phase_b_reason="phase A left unresolved attempts; phase B is held",
                prompt_tokens_used=usage,
            )
            self.state.write(status)
            return status

        phase_b_attempts = self._settle_phase(
            self.manifest.phase_b,
            attempts=attempts,
            staged_auth_path=staged_auth_path,
        )
        all_attempts = attempts + phase_b_attempts
        status = ZaiCampaignStatus(
            campaign_id=self.definition.campaign_id,
            manifest_digest=self.manifest.manifest_digest,
            state="complete",
            attempts=all_attempts,
            phase_b_skipped=False,
            prompt_tokens_used=sum(
                attempt.prompt_tokens or 0 for attempt in all_attempts
            ),
        )
        self.state.write(status)
        return status

    def _settle_phase(
        self,
        trials: Sequence[ZaiTrial],
        *,
        attempts: Sequence[ZaiAttemptRecord],
        staged_auth_path: Path,
    ) -> list[ZaiAttemptRecord]:
        prior = {record.trial_id: record for record in attempts}
        settled: list[ZaiAttemptRecord] = []
        gate = ConcurrencyGate(self.definition.limits.max_concurrency)
        for trial in trials:
            if trial.trial_id in prior:
                settled.append(prior[trial.trial_id])
                continue
            with gate.slot():
                outcome = self.runner.run_trial(
                    trial,
                    staged_auth_path=staged_auth_path,
                    attempt_id=trial.job_identity,
                )
                record = ZaiAttemptRecord(
                    trial_id=outcome.trial_id,
                    job_identity=outcome.job_identity,
                    kind=outcome.kind,
                    reward=outcome.reward,
                    prompt_tokens=outcome.prompt_tokens,
                    reason=outcome.reason,
                    occurred_at=self._clock().isoformat(),
                )
                settled.append(record)
        return settled


def load_definition(path: Path) -> ZaiCampaignDefinition:
    """Load and validate a campaign design document."""
    try:
        return ZaiCampaignDefinition.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ZaiCampaignError(f"cannot load zai campaign definition: {exc}") from exc
