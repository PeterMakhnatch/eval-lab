"""Source-neutral, append-only outcome authority for Eval Lab.

Invariants:
1. Harbor and Inspect are source adapters into one canonical OutcomeRecord model.
2. Historical outcomes are never mutated; superseded records remain visible.
3. Outcome state is a multi-axis vector (agent, verifier, artifact, authority,
   admissibility), never a single scalar.
4. Regrade validity requires cryptographic parity of artifact, source, and
   verifier digests.
5. Non-summable outcomes are excluded from reward aggregations and trigger
   structured refusal when differenced.
6. Inspect scorer facts remain non-decision and unsummable unless exact parity
   binding explicitly promotes them.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import Field, model_validator

from evallab.results import ArtifactRecord, TrialRecord
from evallab.schemas import ContractModel


class OutcomeKind(StrEnum):
    original_verifier = "original_verifier"
    verifier_regrade = "verifier_regrade"
    inspect_scorer = "inspect_scorer"
    synthetic_fallback = "synthetic_fallback"
    manual_audit = "manual_audit"


class AuthorityState(StrEnum):
    authoritative = "authoritative"
    superseded = "superseded"
    non_decision = "non_decision"
    provisional = "provisional"
    disputed = "disputed"


class AgentOutcomeStatus(StrEnum):
    completed = "completed"
    timed_out = "timed_out"
    crashed = "crashed"
    budget_exhausted = "budget_exhausted"
    unknown = "unknown"


class VerifierOutcomeStatus(StrEnum):
    completed = "completed"
    timed_out_without_result = "timed_out_without_result"
    regrade_valid = "regrade_valid"
    error = "error"
    not_run = "not_run"
    unknown = "unknown"


class ArtifactOutcomeStatus(StrEnum):
    preserved = "preserved"
    missing = "missing"
    corrupted = "corrupted"
    unknown = "unknown"


class OutcomeRecord(ContractModel):
    outcome_id: str = Field(default_factory=lambda: str(uuid4()))
    trial_id: str
    source_trial_id: str | None = None
    outcome_kind: OutcomeKind
    outcome_namespace: str = "harbor_verifier"
    outcome_name: str = "reward"
    reward_value: float | None = None
    is_valid_reward: bool = False
    valid_fraction: float | None = None
    agent_status: AgentOutcomeStatus = AgentOutcomeStatus.unknown
    agent_exception: str | None = None
    verifier_status: VerifierOutcomeStatus = VerifierOutcomeStatus.unknown
    artifact_status: ArtifactOutcomeStatus = ArtifactOutcomeStatus.unknown
    artifact_digest: str | None = None
    source_digest: str = Field(min_length=1)
    verifier_digest: str = Field(min_length=1)
    evidence_digest: str | None = None
    authority_state: AuthorityState = AuthorityState.provisional
    superseded_by_outcome_id: str | None = None
    supersession_reason: str | None = None
    is_summable: bool = False
    cas_uri: str | None = None
    evidence_path: str | None = None
    recorded_at: str | None = None
    network_isolation_evidence_digest: str | None = None
    network_isolation_status: str | None = None
    network_isolation_reason: str | None = None
    analysis_eligibility: str | None = None
    trial_admissibility_digest: str | None = None
    trial_admissibility_decision: str | None = None
    trial_admissibility_reason: str | None = None
    trial_allowed_use: str | None = None

    @model_validator(mode="after")
    def validate_outcome_invariants(self) -> OutcomeRecord:
        if self.reward_value is not None and not math.isfinite(self.reward_value):
            raise ValueError("reward_value must be finite")
        if self.valid_fraction is not None and not math.isfinite(self.valid_fraction):
            raise ValueError("valid_fraction must be finite")
        if self.is_valid_reward and self.reward_value is None:
            raise ValueError("a valid reward requires reward_value")
        if self.is_summable and not self.is_valid_reward:
            raise ValueError("a summable outcome must carry a valid reward")
        if self.outcome_kind == OutcomeKind.synthetic_fallback and self.is_valid_reward:
            raise ValueError("a synthetic fallback cannot be valid reward evidence")
        if self.authority_state == AuthorityState.non_decision and self.is_summable:
            raise ValueError("a non-decision outcome cannot be summable")
        authority_values = (
            self.network_isolation_status,
            self.analysis_eligibility,
            self.trial_admissibility_decision,
            self.trial_allowed_use,
        )
        if any(value is not None for value in authority_values) and any(
            value is None for value in authority_values
        ):
            raise ValueError("outcome isolation/admissibility authority must be complete")
        if (
            self.is_summable
            and authority_values[0] is not None
            and authority_values != ("enforced", "causal-eligible", "admissible", "causal")
        ):
            raise ValueError("a summable outcome requires causal isolation and trial admissibility")
        return self


class CompositeOutcomeVector(ContractModel):
    agent_axis: str
    verifier_axis: str
    artifact_axis: str
    authority_axis: str
    is_admissible_for_aggregation: bool
    is_valid_result: bool
    resolved_reward: float | None
    authoritative_outcome_id: str | None


class OutcomeAuthorityResolution(ContractModel):
    trial_id: str
    authoritative_outcome: OutcomeRecord | None
    superseded_outcomes: list[OutcomeRecord]
    composite_vector: CompositeOutcomeVector
    refusal_reason: str | None


def bind_outcome_admissibility(
    record: OutcomeRecord,
    *,
    network_isolation_evidence_digest: str | None,
    network_isolation_status: str,
    network_isolation_reason: str | None,
    analysis_eligibility: str,
    trial_admissibility_digest: str | None,
    trial_admissibility_decision: str,
    trial_admissibility_reason: str,
    trial_allowed_use: str,
) -> OutcomeRecord:
    """Bind normalized outcome authority without allowing descriptive rows to summarize."""
    causal = (
        network_isolation_status == "enforced"
        and analysis_eligibility == "causal-eligible"
        and trial_admissibility_decision == "admissible"
        and trial_allowed_use == "causal"
    )
    rebound = OutcomeRecord.model_validate(
        {
            **record.model_dump(mode="json", exclude={"outcome_id"}),
            "network_isolation_evidence_digest": network_isolation_evidence_digest,
            "network_isolation_status": network_isolation_status,
            "network_isolation_reason": network_isolation_reason,
            "analysis_eligibility": analysis_eligibility,
            "trial_admissibility_digest": trial_admissibility_digest,
            "trial_admissibility_decision": trial_admissibility_decision,
            "trial_admissibility_reason": trial_admissibility_reason,
            "trial_allowed_use": trial_allowed_use,
            "is_summable": record.is_summable and causal,
        }
    )
    return rebound.model_copy(update={"outcome_id": _stable_outcome_id(rebound)})


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_bytes(value)).hexdigest()}"


def _stable_outcome_id(record: OutcomeRecord) -> str:
    identity = record.model_dump(
        mode="json",
        exclude={
            "outcome_id",
            "authority_state",
            "superseded_by_outcome_id",
            "supersession_reason",
            "is_summable",
            "evidence_path",
            "recorded_at",
        },
    )
    return _digest(identity)


def _normalize_digest(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.lower()
    return normalized if normalized.startswith("sha256:") else f"sha256:{normalized}"


def _optional_float(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        converted = float(value)
        return converted if math.isfinite(converted) else None
    return None


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _classify_agent_status(result: dict[str, Any]) -> tuple[AgentOutcomeStatus, str | None]:
    exception = result.get("exception_info") or {}
    if not isinstance(exception, dict):
        exception = {}
    exc_type = exception.get("exception_type")
    if not exc_type:
        return AgentOutcomeStatus.completed, None
    exc_str = str(exc_type)
    lowered = exc_str.lower()
    if "timeout" in lowered:
        return AgentOutcomeStatus.timed_out, exc_str
    if "budget" in lowered:
        return AgentOutcomeStatus.budget_exhausted, exc_str
    if "crash" in lowered or "aborted" in lowered or "systemexit" in lowered:
        return AgentOutcomeStatus.crashed, exc_str
    if exception.get("exception_phase") == "agent" or exception.get("phase") == "agent":
        return AgentOutcomeStatus.crashed, exc_str
    return AgentOutcomeStatus.unknown, exc_str


def _classify_verifier_status(
    verifier_result: dict[str, Any],
    exception_info: Any,
    valid_fraction: float | None,
    explicit_status: str | None,
    outcome_kind: OutcomeKind,
) -> VerifierOutcomeStatus:
    if explicit_status and explicit_status in VerifierOutcomeStatus:
        return VerifierOutcomeStatus(explicit_status)
    status = verifier_result.get("status")
    if isinstance(status, str) and status in VerifierOutcomeStatus:
        return VerifierOutcomeStatus(status)
    rewards = verifier_result.get("rewards") or {}
    reward_value = _optional_float(rewards.get("reward"))
    if valid_fraction is not None and valid_fraction > 0 and reward_value is not None:
        if outcome_kind == OutcomeKind.verifier_regrade:
            return VerifierOutcomeStatus.regrade_valid
        return VerifierOutcomeStatus.completed
    if reward_value is not None:
        if outcome_kind == OutcomeKind.verifier_regrade:
            return VerifierOutcomeStatus.regrade_valid
        return VerifierOutcomeStatus.completed
    exception = exception_info if isinstance(exception_info, dict) else {}
    exc_type = str(exception.get("exception_type", "")).lower()
    if "timeout" in exc_type:
        return VerifierOutcomeStatus.timed_out_without_result
    if not verifier_result:
        return VerifierOutcomeStatus.not_run
    return VerifierOutcomeStatus.unknown


def _artifact_target_digest(trial: TrialRecord) -> str | None:
    result = trial.result
    for key in ("artifact_digest", "final_artifact_digest", "selected_artifact_digest"):
        if result.get(key):
            return str(result[key])
    trace = result.get("autonomous_research_trace")
    if isinstance(trace, dict) and trace.get("final_artifact_digest"):
        return str(trace["final_artifact_digest"])
    existing = [a for a in trial.artifacts if a.exists and a.sha256]
    if len(existing) == 1:
        return f"sha256:{existing[0].sha256}"
    return None


def _artifact_status_for_digest(
    artifacts: Sequence[ArtifactRecord],
    target_digest: str | None,
) -> tuple[ArtifactOutcomeStatus, str | None]:
    if not target_digest:
        if not artifacts:
            return ArtifactOutcomeStatus.unknown, None
        if all(a.exists for a in artifacts):
            return ArtifactOutcomeStatus.preserved, None
        if any(a.exists for a in artifacts):
            return ArtifactOutcomeStatus.corrupted, None
        return ArtifactOutcomeStatus.missing, None

    normalized = target_digest.lower()
    if not normalized.startswith("sha256:"):
        normalized = f"sha256:{normalized}"

    for a in artifacts:
        if not a.sha256:
            continue
        art_digest = f"sha256:{a.sha256.lower()}"
        if art_digest == normalized:
            return (
                ArtifactOutcomeStatus.preserved if a.exists else ArtifactOutcomeStatus.missing
            ), target_digest

    # A target was specified, but no artifact matches it. If any artifact exists,
    # the expected artifact is missing/corrupted relative to the target.
    if any(a.exists for a in artifacts):
        return ArtifactOutcomeStatus.corrupted, target_digest
    return ArtifactOutcomeStatus.missing, target_digest


def _source_digest_for_trial(trial: TrialRecord) -> str:
    result_path = trial.path / "result.json"
    if result_path.is_file():
        digest = hashlib.sha256(result_path.read_bytes()).hexdigest()
        return f"sha256:{digest}"
    return _digest(trial.result)


def _task_digest_for_trial(trial: TrialRecord) -> str | None:
    task = trial.lock.get("task") if isinstance(trial.lock, dict) else None
    if isinstance(task, dict) and task.get("digest"):
        return str(task["digest"])
    value = trial.result.get("task_checksum")
    return str(value) if value is not None else None


def _verifier_digest_for_trial(trial: TrialRecord) -> str:
    explicit = trial.result.get("verifier_digest")
    if explicit:
        return str(explicit)
    lock = trial.lock if isinstance(trial.lock, dict) else {}
    return _digest(
        {
            "task_digest": _task_digest_for_trial(trial),
            "verifier": lock.get("verifier") or {},
        }
    )


def outcome_record_from_trial(
    trial: TrialRecord,
    *,
    outcome_kind: OutcomeKind = OutcomeKind.original_verifier,
    source_trial_id: str | None = None,
    reward_value: float | None = None,
    is_valid_reward: bool | None = None,
    valid_fraction: float | None = None,
    verifier_status: VerifierOutcomeStatus | None = None,
    artifact_digest: str | None = None,
    source_digest: str | None = None,
    verifier_digest: str | None = None,
    recorded_at: str | None = None,
) -> OutcomeRecord:
    """Build an immutable outcome fact from a Harbor trial directory."""
    result = trial.result
    agent_status, agent_exception = _classify_agent_status(result)
    verifier_result = result.get("verifier_result") or {}

    if reward_value is None:
        reward_value = _optional_float((verifier_result.get("rewards") or {}).get("reward"))
    if valid_fraction is None:
        valid_fraction = _optional_float(verifier_result.get("valid_fraction"))

    derived_verifier_status = _classify_verifier_status(
        verifier_result,
        result.get("exception_info"),
        valid_fraction,
        trial.verifier_status,
        outcome_kind,
    )
    if verifier_status is None:
        verifier_status = derived_verifier_status

    if artifact_digest is None:
        artifact_digest = _artifact_target_digest(trial)
    artifact_status, artifact_digest = _artifact_status_for_digest(trial.artifacts, artifact_digest)

    evidence_digest = _source_digest_for_trial(trial)
    if source_digest is None:
        source_digest = evidence_digest
    if verifier_digest is None:
        verifier_digest = _verifier_digest_for_trial(trial)

    if is_valid_reward is None:
        is_valid_reward = (
            verifier_status
            in (VerifierOutcomeStatus.completed, VerifierOutcomeStatus.regrade_valid)
            and reward_value is not None
            and outcome_kind != OutcomeKind.synthetic_fallback
        )

    if recorded_at is None:
        recorded_at = datetime.now(UTC).isoformat()

    record = OutcomeRecord(
        trial_id=trial.id,
        source_trial_id=source_trial_id,
        outcome_kind=outcome_kind,
        outcome_namespace="harbor_verifier",
        outcome_name="reward",
        reward_value=reward_value,
        is_valid_reward=is_valid_reward,
        valid_fraction=valid_fraction,
        agent_status=agent_status,
        agent_exception=agent_exception,
        verifier_status=verifier_status,
        artifact_status=artifact_status,
        artifact_digest=artifact_digest,
        source_digest=source_digest,
        verifier_digest=verifier_digest,
        evidence_digest=evidence_digest,
        authority_state=AuthorityState.provisional,
        is_summable=is_valid_reward,
        evidence_path=str(trial.path),
        recorded_at=recorded_at,
    )
    return record.model_copy(update={"outcome_id": _stable_outcome_id(record)})


def outcome_record_from_regrade(
    regrade_trial: TrialRecord,
    source_trial_id: str,
    *,
    source_digest: str,
    source_artifact_digest: str | None = None,
    source_artifact_status: ArtifactOutcomeStatus = ArtifactOutcomeStatus.unknown,
    source_agent_status: AgentOutcomeStatus | None = None,
    source_agent_exception: str | None = None,
    recorded_at: str | None = None,
) -> OutcomeRecord:
    """Build a regrade fact without copying unverified lineage from its source."""
    record = outcome_record_from_trial(
        regrade_trial,
        outcome_kind=OutcomeKind.verifier_regrade,
        source_trial_id=source_trial_id,
        source_digest=source_digest,
        recorded_at=recorded_at,
    )
    updates: dict[str, Any] = {}
    if source_agent_status is not None:
        updates.update(
            agent_status=source_agent_status,
            agent_exception=source_agent_exception,
        )

    evaluated_digest = _normalize_digest(_artifact_target_digest(regrade_trial))
    expected_digest = _normalize_digest(source_artifact_digest)
    if (
        evaluated_digest is not None
        and evaluated_digest == expected_digest
        and source_artifact_status == ArtifactOutcomeStatus.preserved
    ):
        updates.update(
            artifact_digest=evaluated_digest,
            artifact_status=ArtifactOutcomeStatus.preserved,
        )

    if (
        record.reward_value is not None
        and record.verifier_status == VerifierOutcomeStatus.completed
    ):
        updates["verifier_status"] = VerifierOutcomeStatus.regrade_valid

    record = record.model_copy(update=updates)
    return record.model_copy(update={"outcome_id": _stable_outcome_id(record)})


def synthetic_fallback_record(
    original: OutcomeRecord,
    *,
    reward_value: float,
    evidence_path: str,
) -> OutcomeRecord:
    """Record an explicitly observed job-summary fallback as non-reward evidence."""
    if not math.isfinite(reward_value):
        raise ValueError("synthetic fallback reward must be finite")
    record = original.model_copy(
        update={
            "outcome_kind": OutcomeKind.synthetic_fallback,
            "reward_value": reward_value,
            "is_valid_reward": False,
            "is_summable": False,
            "authority_state": AuthorityState.provisional,
            "evidence_path": evidence_path,
        }
    )
    return record.model_copy(update={"outcome_id": _stable_outcome_id(record)})


def _pick_agent_representative(
    outcomes: Sequence[OutcomeRecord],
) -> OutcomeRecord | None:
    preferred_order = (
        OutcomeKind.original_verifier,
        OutcomeKind.synthetic_fallback,
        OutcomeKind.manual_audit,
        OutcomeKind.verifier_regrade,
        OutcomeKind.inspect_scorer,
    )
    for kind in preferred_order:
        for o in outcomes:
            if o.outcome_kind == kind and o.agent_status != AgentOutcomeStatus.unknown:
                return o
    for kind in preferred_order:
        for o in outcomes:
            if o.outcome_kind == kind:
                return o
    return None


def _primary_verifier_status(outcomes: Sequence[OutcomeRecord]) -> str:
    for o in outcomes:
        if o.verifier_status != VerifierOutcomeStatus.unknown:
            return o.verifier_status.value
    return VerifierOutcomeStatus.unknown.value


def _primary_artifact_status(outcomes: Sequence[OutcomeRecord]) -> str:
    for o in outcomes:
        if o.artifact_status != ArtifactOutcomeStatus.unknown:
            return o.artifact_status.value
    return ArtifactOutcomeStatus.unknown.value


def _resolve_authority_axis(authoritative: OutcomeRecord | None, refused: bool) -> str:
    if refused:
        return "disputed"
    if authoritative is None:
        return "unresolved_verifier_timeout"
    if authoritative.outcome_kind == OutcomeKind.verifier_regrade:
        return "regrade_authoritative"
    if authoritative.outcome_kind == OutcomeKind.original_verifier:
        return "original_verifier_authoritative"
    if authoritative.outcome_kind == OutcomeKind.synthetic_fallback:
        return "synthetic_fallback_authoritative"
    if authoritative.outcome_kind == OutcomeKind.manual_audit:
        return "manual_audit_authoritative"
    if authoritative.outcome_kind == OutcomeKind.inspect_scorer:
        return "non_decision"
    return authoritative.authority_state.value


def _resolve_verifier_axis(
    authoritative: OutcomeRecord | None, fallback_outcomes: Sequence[OutcomeRecord]
) -> str:
    if authoritative is None:
        return _primary_verifier_status(fallback_outcomes)
    if authoritative.outcome_kind == OutcomeKind.verifier_regrade:
        return (
            authoritative.verifier_status.value
            if authoritative.verifier_status == VerifierOutcomeStatus.regrade_valid
            else VerifierOutcomeStatus.regrade_valid.value
        )
    return authoritative.verifier_status.value


def resolve_outcome_authority(
    outcomes: Sequence[OutcomeRecord],
    primary_trial_info: dict[str, Any] | None = None,
) -> OutcomeAuthorityResolution:
    """Resolve one source trial without mutating its append-only evidence facts."""
    if not outcomes:
        raise ValueError("at least one outcome record is required to resolve authority")

    primary_trial_info = primary_trial_info or {}
    trial_id = primary_trial_info.get("trial_id")
    if not trial_id:
        for outcome in outcomes:
            if outcome.outcome_kind in (
                OutcomeKind.original_verifier,
                OutcomeKind.synthetic_fallback,
                OutcomeKind.manual_audit,
            ):
                trial_id = outcome.trial_id
                break
    if not trial_id:
        trial_id = next(
            (outcome.source_trial_id for outcome in outcomes if outcome.source_trial_id),
            outcomes[0].trial_id,
        )

    regrades = [
        outcome for outcome in outcomes if outcome.outcome_kind == OutcomeKind.verifier_regrade
    ]
    originals = [
        outcome
        for outcome in outcomes
        if outcome.outcome_kind
        in (
            OutcomeKind.original_verifier,
            OutcomeKind.synthetic_fallback,
            OutcomeKind.manual_audit,
        )
    ]
    auxiliary = [
        outcome for outcome in outcomes if outcome.outcome_kind == OutcomeKind.inspect_scorer
    ]

    source_digest = primary_trial_info.get("source_digest") or next(
        (outcome.source_digest for outcome in originals if outcome.source_digest),
        None,
    )
    verifier_digest = primary_trial_info.get("verifier_digest") or next(
        (outcome.verifier_digest for outcome in originals if outcome.verifier_digest),
        None,
    )
    artifact_digest = primary_trial_info.get("artifact_digest") or next(
        (
            outcome.artifact_digest
            for outcome in originals
            if outcome.artifact_status == ArtifactOutcomeStatus.preserved
            and outcome.artifact_digest
        ),
        None,
    )

    agent_rep = _pick_agent_representative(originals) or _pick_agent_representative(outcomes)
    agent_axis = agent_rep.agent_status.value if agent_rep else AgentOutcomeStatus.unknown.value

    invalid_reasons: list[str] = []
    valid_regrades: list[OutcomeRecord] = []
    for regrade in regrades:
        reason: str | None = None
        if regrade.source_trial_id != trial_id:
            reason = f"source trial mismatch: {regrade.source_trial_id!r} != {trial_id!r}"
        elif not source_digest or regrade.source_digest != source_digest:
            reason = "source digest missing or mismatched"
        elif not verifier_digest or regrade.verifier_digest != verifier_digest:
            reason = "verifier digest missing or mismatched"
        elif not artifact_digest or regrade.artifact_digest != artifact_digest:
            reason = "artifact digest missing or mismatched"
        elif regrade.artifact_status != ArtifactOutcomeStatus.preserved:
            reason = f"artifact is not preserved ({regrade.artifact_status.value})"
        elif regrade.verifier_status != VerifierOutcomeStatus.regrade_valid:
            reason = f"verifier status is not regrade_valid ({regrade.verifier_status.value})"
        elif not regrade.is_valid_reward or regrade.reward_value is None:
            reason = "regrade has no valid reward"
        if reason is None:
            valid_regrades.append(regrade)
        else:
            invalid_reasons.append(f"regrade {regrade.outcome_id}: {reason}")

    refusal_reason: str | None = None
    authoritative: OutcomeRecord | None = None
    superseded: list[OutcomeRecord] = []

    if invalid_reasons:
        refusal_reason = "invalid_regrades: " + "; ".join(sorted(invalid_reasons))
    elif valid_regrades:
        rewards = {regrade.reward_value for regrade in valid_regrades}
        artifacts = {regrade.artifact_digest for regrade in valid_regrades}
        if len(rewards) > 1 or len(artifacts) > 1:
            refusal_reason = "conflicting_regrades: divergent rewards or artifact digests"
        else:
            authoritative = min(valid_regrades, key=lambda outcome: outcome.outcome_id)
    else:
        original_candidates = [
            outcome
            for outcome in originals
            if outcome.outcome_kind in (OutcomeKind.original_verifier, OutcomeKind.manual_audit)
            and outcome.is_valid_reward
            and outcome.reward_value is not None
            and outcome.verifier_status == VerifierOutcomeStatus.completed
            and outcome.artifact_status == ArtifactOutcomeStatus.preserved
        ]
        if original_candidates:
            authoritative = min(original_candidates, key=lambda outcome: outcome.outcome_id)

    authority_allows_summation = authoritative is not None and (
        authoritative.network_isolation_status is None
        or (
            authoritative.network_isolation_status == "enforced"
            and authoritative.analysis_eligibility == "causal-eligible"
            and authoritative.trial_admissibility_decision == "admissible"
            and authoritative.trial_allowed_use == "causal"
        )
    )
    if refusal_reason:
        for outcome in (*originals, *regrades):
            superseded.append(
                outcome.model_copy(
                    update={
                        "authority_state": AuthorityState.disputed,
                        "is_summable": False,
                    }
                )
            )
    elif authoritative is not None:
        authoritative = authoritative.model_copy(
            update={
                "authority_state": AuthorityState.authoritative,
                "is_summable": authority_allows_summation,
            }
        )
        for outcome in (*originals, *regrades):
            if outcome.outcome_id == authoritative.outcome_id:
                continue
            superseded.append(
                outcome.model_copy(
                    update={
                        "authority_state": AuthorityState.superseded,
                        "is_summable": False,
                        "superseded_by_outcome_id": authoritative.outcome_id,
                        "supersession_reason": (
                            "regrade_authoritative"
                            if authoritative.outcome_kind == OutcomeKind.verifier_regrade
                            else "original_verifier_authoritative"
                        ),
                    }
                )
            )
    else:
        for outcome in originals:
            if outcome.outcome_kind == OutcomeKind.synthetic_fallback:
                superseded.append(
                    outcome.model_copy(
                        update={
                            "authority_state": AuthorityState.superseded,
                            "is_summable": False,
                            "supersession_reason": "unresolved_verifier_timeout",
                        }
                    )
                )
            else:
                superseded.append(
                    outcome.model_copy(
                        update={
                            "authority_state": AuthorityState.provisional,
                            "is_summable": False,
                        }
                    )
                )

    superseded.extend(
        outcome.model_copy(
            update={
                "authority_state": AuthorityState.non_decision,
                "is_summable": False,
            }
        )
        for outcome in auxiliary
    )

    authority_axis = _resolve_authority_axis(authoritative, refusal_reason is not None)
    verifier_axis = _resolve_verifier_axis(authoritative, originals)
    artifact_axis = (
        authoritative.artifact_status.value
        if authoritative is not None
        else _primary_artifact_status(originals)
    )
    resolved_reward = authoritative.reward_value if authoritative is not None else None
    admissible = bool(
        authoritative is not None
        and authoritative.is_summable
        and authoritative.artifact_status == ArtifactOutcomeStatus.preserved
        and authoritative.is_valid_reward
    )
    valid_result = bool(
        authoritative is not None
        and authoritative.is_valid_reward
        and authoritative.artifact_status == ArtifactOutcomeStatus.preserved
    )

    return OutcomeAuthorityResolution(
        trial_id=trial_id,
        authoritative_outcome=authoritative,
        superseded_outcomes=superseded,
        composite_vector=CompositeOutcomeVector(
            agent_axis=agent_axis,
            verifier_axis=verifier_axis,
            artifact_axis=artifact_axis,
            authority_axis=authority_axis,
            is_admissible_for_aggregation=admissible,
            is_valid_result=valid_result,
            resolved_reward=resolved_reward,
            authoritative_outcome_id=(
                authoritative.outcome_id if authoritative is not None else None
            ),
        ),
        refusal_reason=refusal_reason,
    )


def outcome_record_from_inspect_score(
    *,
    trial_id: str,
    score_name: str,
    value: Any,
    source_digest: str,
    verifier_digest: str,
    recorded_at: str | None = None,
) -> OutcomeRecord:
    """Build a non-decision outcome fact from an Inspect scorer value."""
    if recorded_at is None:
        recorded_at = datetime.now(UTC).isoformat()
    record = OutcomeRecord(
        trial_id=trial_id,
        source_trial_id=None,
        outcome_kind=OutcomeKind.inspect_scorer,
        outcome_namespace="inspect",
        outcome_name=score_name,
        reward_value=_optional_float(value),
        is_valid_reward=False,
        agent_status=AgentOutcomeStatus.unknown,
        verifier_status=VerifierOutcomeStatus.not_run,
        artifact_status=ArtifactOutcomeStatus.unknown,
        source_digest=source_digest,
        verifier_digest=verifier_digest,
        evidence_digest=source_digest,
        authority_state=AuthorityState.non_decision,
        is_summable=False,
        recorded_at=recorded_at,
    )
    return record.model_copy(update={"outcome_id": _stable_outcome_id(record)})


def outcome_record_from_dict(d: dict[str, Any]) -> OutcomeRecord:
    """Deserialize an OutcomeRecord from a plain dictionary, rejecting unknown fields."""
    return OutcomeRecord.model_validate(d)


def bind_supersession(
    superseded_outcome: OutcomeRecord,
    authoritative_outcome: OutcomeRecord,
    reason: str = "superseded_by_authoritative",
) -> OutcomeRecord:
    """Return a copy of ``superseded_outcome`` bound to its authoritative successor."""
    return superseded_outcome.model_copy(
        update={
            "authority_state": AuthorityState.superseded,
            "superseded_by_outcome_id": authoritative_outcome.outcome_id,
            "supersession_reason": reason,
            "is_summable": False,
        }
    )


def check_scale_binding(
    a: OutcomeRecord,
    b: OutcomeRecord,
) -> tuple[bool, float | None, str | None]:
    """Check whether two outcomes are on the same reward scale.

    Returns ``(compatible, transfer_gap, reason)``.  A non-``None`` transfer gap
    is only returned when the two outcomes share namespace, name, verifier, and
    artifact digests and both rewards are valid.
    """
    if a.outcome_namespace != b.outcome_namespace or a.outcome_name != b.outcome_name:
        return False, None, "outcome namespace/name mismatch"
    if a.verifier_digest != b.verifier_digest:
        return False, None, "verifier digest mismatch"
    if (
        a.artifact_status != ArtifactOutcomeStatus.preserved
        or b.artifact_status != ArtifactOutcomeStatus.preserved
        or a.artifact_digest is None
        or b.artifact_digest is None
        or a.artifact_digest != b.artifact_digest
    ):
        return False, None, "artifact binding missing or mismatched"
    if not a.is_valid_reward or not b.is_valid_reward:
        return False, None, "one or both rewards are not valid"
    if a.reward_value is None or b.reward_value is None:
        return False, None, "missing reward value"
    return True, b.reward_value - a.reward_value, None


def assert_outcome_differencing_allowed(
    a: OutcomeRecord | OutcomeAuthorityResolution,
    b: OutcomeRecord | OutcomeAuthorityResolution,
) -> None:
    """Fail closed if two authoritative outcomes cannot be differenced.

    Raises ``ValueError`` when either outcome is missing, not summable, or not
    on the same validated reward scale.
    """
    a_auth = a if isinstance(a, OutcomeRecord) else a.authoritative_outcome
    b_auth = b if isinstance(b, OutcomeRecord) else b.authoritative_outcome
    if a_auth is None or b_auth is None:
        raise ValueError("differencing refused: missing authoritative outcome")
    if not a_auth.is_summable or not b_auth.is_summable:
        raise ValueError("differencing refused: one or both outcomes are not summable")
    compatible, _, reason = check_scale_binding(a_auth, b_auth)
    if not compatible:
        raise ValueError(f"differencing refused: scale binding incompatible: {reason}")


def aggregate_outcome_rewards(
    outcomes: Sequence[OutcomeRecord],
) -> dict[str, Any]:
    """Aggregate authoritative, summable rewards and report exclusions.

    The gate is fail-closed: any outcome that is not authoritative, not
    summable, or does not carry a valid reward is excluded from the total.
    """
    total = 0.0
    count = 0
    excluded: list[dict[str, Any]] = []
    for o in outcomes:
        if (
            o.authority_state == AuthorityState.authoritative
            and o.is_summable
            and o.is_valid_reward
            and o.reward_value is not None
        ):
            total += o.reward_value
            count += 1
        else:
            excluded.append(
                {
                    "outcome_id": o.outcome_id,
                    "reason": "not authoritative, not summable, or invalid reward",
                }
            )
    return {
        "count": count,
        "total_reward": total if count else None,
        "excluded_count": len(excluded),
        "excluded": excluded,
    }
