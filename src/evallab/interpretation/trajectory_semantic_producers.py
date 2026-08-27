"""Focused semantic-fact producers for AgentAbstain and Recovery evidence.

The producers have a deliberately narrow evidence boundary: verifier artifacts and
profile-derived semantic action facts are authoritative.  They never derive a verdict
from trajectory prose, rewards, or a missing artifact.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from evallab.interpretation.trajectory_semantics import SemanticActionFact
from evallab.semantic_facts import (
    CapabilityOpportunity,
    EvidenceCoverage,
    PairedConditionFact,
)

_DIGEST_PREFIX = "sha256:"


def _digest(value: Any) -> str:
    """Return a stable digest for JSON-like evidence, including missing evidence."""
    if isinstance(value, bytes):
        payload = value
    else:
        payload = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
    return _DIGEST_PREFIX + hashlib.sha256(payload).hexdigest()


def _model_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return dict(value.model_dump(mode="python"))
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    raise TypeError(f"expected a mapping or typed model, got {type(value).__name__}")


class AgentAbstainTrialInput(BaseModel):
    """Typed identity/configuration for one AgentAbstain trial variant.

    ``verdict_artifact`` must be an emitted verifier artifact (a mapping, a typed
    artifact object, or a path to one).  It is intentionally nullable so callers can
    represent an artifact that was deleted without manufacturing a verdict.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    pair_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    variant: Literal["act", "abstain"]
    trigger: str = Field(min_length=1)
    verdict_artifact: Any | None = None
    trial_id: str | None = None
    session_id: str | None = None
    critical_action: str | None = None
    state_diff: str | None = None
    source_ref: str | None = None


class RecoveryTrialInput(BaseModel):
    """Typed recovery inputs at the producer boundary.

    ``recovery_fact`` and ``certificate`` are the existing Recovery-Bench result and
    certificate shapes; they are accepted as models or mappings to avoid copying or
    altering those mechanical schemas.  ``semantic_actions`` must be facts produced by
    :func:`extract_semantic_actions`, not raw trajectory steps.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    recovery_fact: Any
    certificate: Any | None = None
    semantic_actions: tuple[Any, ...] = ()
    trial_id: str | None = None
    task_id: str | None = None
    source_ref: str | None = None
    fault_exposed: bool | None = None


@dataclass(frozen=True)
class AgentAbstainProjection:
    paired_condition_facts: tuple[PairedConditionFact, ...]
    capability_opportunities: tuple[CapabilityOpportunity, ...]
    evidence_coverage: tuple[EvidenceCoverage, ...]

    @property
    def facts(self) -> tuple[Any, ...]:
        return self.paired_condition_facts + self.capability_opportunities + self.evidence_coverage


@dataclass(frozen=True)
class RecoveryProjection:
    capability_opportunities: tuple[CapabilityOpportunity, ...]
    evidence_coverage: tuple[EvidenceCoverage, ...]

    @property
    def facts(self) -> tuple[Any, ...]:
        return self.capability_opportunities + self.evidence_coverage


def _variant_identity(raw: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """Resolve explicit ``__act``/``__abstain`` lineage without guessing a pair."""
    variant = str(raw.get("variant") or raw.get("task_type") or "").lower()
    explicit_trial = str(raw.get("trial_id") or "")
    if variant not in {"act", "abstain"}:
        for suffix in ("__act", "__abstain", ":act", ":abstain"):
            if explicit_trial.endswith(suffix):
                variant = suffix.lstrip(":_")
                break
    if variant not in {"act", "abstain"}:
        raise ValueError("AgentAbstain input requires explicit act/abstain variant lineage")
    pair_id = str(raw.get("pair_id") or "")
    if not pair_id and explicit_trial:
        pair_id = explicit_trial
        for suffix in ("__act", "__abstain", ":act", ":abstain"):
            if pair_id.endswith(suffix):
                pair_id = pair_id[: -len(suffix)]
                break
    if not pair_id:
        raise ValueError("AgentAbstain input requires pair_id or a suffixed trial_id")
    task_id = str(raw.get("task_id") or pair_id.rsplit("/", 1)[-1])
    trial_id = explicit_trial or f"{pair_id}__{variant}"
    return pair_id, task_id, variant, trial_id


def _read_verdict_artifact(artifact: Any) -> tuple[str, str | None, str | None]:
    """Read an explicit verdict artifact as (normalized verdict, reason, digest).

    A missing path/None is distinct from an artifact containing ``unknown``.  Both
    remain unknown to the fact schema, but only the former is marked missing evidence.
    """
    if artifact is None:
        return "unknown", "missing_verdict_artifact", None
    source_bytes: bytes | None = None
    value = artifact
    if isinstance(artifact, Path):
        path = artifact
        if not path.is_file():
            return "unknown", "missing_verdict_artifact", None
        source_bytes = path.read_bytes()
        try:
            value = json.loads(source_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            value = source_bytes.decode("utf-8", errors="replace").strip()
    elif isinstance(artifact, str):
        path = Path(artifact)
        if path.is_file():
            source_bytes = path.read_bytes()
            try:
                value = json.loads(source_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                value = source_bytes.decode("utf-8", errors="replace").strip()
        elif "/" in artifact or artifact.endswith((".json", ".txt")):
            return "unknown", "missing_verdict_artifact", None
        else:
            value = artifact
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        value = model_dump(mode="python")
    if isinstance(value, Mapping):
        raw = value.get("verdict", value.get("primary_verdict", value.get("status")))
        if raw is None:
            return "unknown", "verdict_field_missing", _digest(source_bytes or value)
    else:
        raw = value
    normalized = str(raw).strip().lower()
    verdict = {
        "pass": "satisfied",
        "passed": "satisfied",
        "satisfied": "satisfied",
        "success": "satisfied",
        "true": "satisfied",
        "fail": "violated",
        "failed": "violated",
        "violated": "violated",
        "failure": "violated",
        "false": "violated",
        "unknown": "unknown",
    }.get(normalized)
    if verdict is None:
        return "unknown", "verdict_value_unknown", _digest(source_bytes or value)
    if verdict == "unknown":
        return "unknown", "verdict_unknown", _digest(source_bytes or value)
    return verdict, None, _digest(source_bytes or value)


def _agent_source(
    raw: Mapping[str, Any], trial_id: str, artifact_digest: str | None
) -> tuple[str, str]:
    artifact_ref = raw.get("verdict_artifact", raw.get("verdict_path"))
    ref = str(raw.get("source_ref") or artifact_ref or f"agentabstain:{trial_id}")
    digest = artifact_digest or _digest({"trial_id": trial_id, "artifact": "missing"})
    return ref, digest


def project_agentabstain(
    trials: Iterable[AgentAbstainTrialInput | Mapping[str, Any] | Any],
) -> AgentAbstainProjection:
    """Project real AgentAbstain verdict artifacts into shared semantic fact rows."""
    paired: list[PairedConditionFact] = []
    opportunities: list[CapabilityOpportunity] = []
    coverage: list[EvidenceCoverage] = []
    for item in trials:
        raw = _model_dict(item)
        pair_id, task_id, variant, trial_id = _variant_identity(raw)
        artifact = raw.get(
            "verdict_artifact",
            raw.get(
                "verdict_path",
                raw.get(
                    "verdict_record",
                    raw if "verdict" in raw or "primary_verdict" in raw else None,
                ),
            ),
        )
        verdict, reason, artifact_digest = _read_verdict_artifact(artifact)
        source_ref, source_digest = _agent_source(raw, trial_id, artifact_digest)
        unknown_reason = f"reason:{reason}" if verdict == "unknown" and reason else None
        required = ("verdict_artifact",) + ((unknown_reason,) if unknown_reason else ())
        missing = required if verdict == "unknown" else ()
        observed = ("verdict_artifact",) if verdict != "unknown" else ()
        critical = raw.get("critical_action", raw.get("critical_actions"))
        if isinstance(critical, (list, tuple)):
            critical = ",".join(str(value) for value in critical) or None
        state_diff = raw.get("state_diff")
        if isinstance(state_diff, bool):
            state_diff = "unchanged" if state_diff else "changed"
        paired.append(
            PairedConditionFact.model_validate(
                {
                    "trial_id": trial_id,
                    "pair_id": pair_id,
                    "session_id": raw.get("session_id"),
                    "task_id": task_id,
                    "variant": variant,
                    "condition": "should_act" if variant == "act" else "should_abstain",
                    "trigger": str(raw.get("trigger") or raw.get("category") or "unspecified"),
                    "critical_action": critical,
                    "state_diff": state_diff,
                    "primary_verdict": verdict,
                    "secondary_verdict": "unknown",
                    "source_ref": source_ref,
                    "source_digest": source_digest,
                    "provenance_kind": "benchmark_verifier",
                }
            )
        )
        opportunities.append(
            CapabilityOpportunity.model_validate(
                {
                    "opportunity_id": f"agentabstain:{pair_id}:{variant}",
                    "trial_id": trial_id,
                    "benchmark": "AgentAbstain",
                    "construct": "abstention_pair",
                    "eligible": True if not missing else None,
                    "required_evidence": required,
                    "missing_evidence": missing,
                    "source_ref": source_ref,
                    "source_digest": source_digest,
                    "provenance_kind": "benchmark_verifier",
                }
            )
        )
        coverage.append(
            EvidenceCoverage.model_validate(
                {
                    "trial_id": trial_id,
                    "benchmark": "AgentAbstain",
                    "construct": "abstention_pair",
                    "exposed": True,
                    "eligible": True if not missing else None,
                    "required_evidence": required,
                    "observed_evidence": observed,
                    "missing_evidence": missing,
                    "analysis_ready": True if not missing else None,
                    "source_ref": source_ref,
                    "source_digest": source_digest,
                    "provenance_kind": "benchmark_verifier",
                }
            )
        )
    return AgentAbstainProjection(tuple(paired), tuple(opportunities), tuple(coverage))


def _action_dict(action: Any) -> dict[str, Any]:
    if isinstance(action, SemanticActionFact):
        return action.model_dump(mode="python")
    return _model_dict(action)


def _recovery_status(raw: Mapping[str, Any], certificate: Any) -> str:
    cert = _model_dict(certificate) if certificate is not None else {}
    cert_status = str(cert.get("overall_status", cert.get("certificate_status", "UNKNOWN"))).upper()
    success = raw.get("recovery_success")
    if cert_status == "PASS" and success is True:
        return "satisfied"
    if cert_status == "FAIL" or success is False:
        return "violated"
    return "unknown"


def _recovery_exposure(raw: Mapping[str, Any], actions: Sequence[Mapping[str, Any]]) -> bool:
    explicit = raw.get("fault_exposed")
    if isinstance(explicit, bool):
        return explicit
    # Exposure is a semantic action fact, never a reward/certificate inference.
    return any(
        bool(action.get("fault_exposed")) or str(action.get("outcome", "")) == "error"
        for action in actions
    )


def project_recovery(
    inputs: Iterable[RecoveryTrialInput | Mapping[str, Any] | Any],
) -> RecoveryProjection:
    """Project recovery facts/certificates and semantic action provenance.

    One opportunity is emitted for autonomous evidence and one for user/system-assisted
    evidence. A trial with no explicit fault exposure emits unexposed, unknown coverage;
    a failed reward/certificate cannot turn that control into a failure.
    """
    opportunities: list[CapabilityOpportunity] = []
    coverage: list[EvidenceCoverage] = []
    for item in inputs:
        raw_input = _model_dict(item)
        recovery_value = raw_input.get("recovery_fact", raw_input)
        raw = _model_dict(recovery_value)
        certificate = raw_input.get("certificate")
        certificate_data = _model_dict(certificate) if certificate is not None else None
        actions = [_action_dict(action) for action in raw_input.get("semantic_actions", ())]
        trial_id = str(
            raw_input.get("trial_id")
            or raw.get("recovery_trial_id")
            or raw.get("initial_trial_id")
            or ""
        )
        if not trial_id:
            raise ValueError(
                "recovery input requires recovery_trial_id, initial_trial_id, or trial_id"
            )
        source_ref = str(raw_input.get("source_ref") or f"recovery:{trial_id}")
        source_digest = _digest(
            {"recovery_fact": raw, "certificate": certificate_data, "semantic_actions": actions}
        )
        exposed = _recovery_exposure(raw_input, actions)
        status = _recovery_status(raw, certificate_data)
        autonomous = any(str(a.get("intervention_provenance")) == "autonomous" for a in actions)
        assisted = any(
            str(a.get("intervention_provenance"))
            in {"user_assisted", "system_assisted", "environment_recovery", "harness_retry"}
            for a in actions
        )
        base_required = ["fault_exposure", "recovery_outcome"]
        mode_flags = (("autonomous", autonomous), ("user_system_assisted", assisted))
        for mode, present in mode_flags:
            required = tuple(base_required + [f"{mode}_action"])
            if not exposed:
                eligible: bool | None = None
                missing = required
            else:
                missing = tuple(
                    evidence
                    for evidence in required
                    if (
                        evidence == "fault_exposure"
                        and not exposed
                        or evidence == "recovery_outcome"
                        and status == "unknown"
                        or evidence == f"{mode}_action"
                        and not present
                    )
                )
                eligible = True if not missing else None
            opportunities.append(
                CapabilityOpportunity.model_validate(
                    {
                        "opportunity_id": f"recovery:{trial_id}:{mode}",
                        "trial_id": trial_id,
                        "benchmark": "Recovery",
                        "construct": "recovery",
                        "eligible": eligible,
                        "required_evidence": required,
                        "missing_evidence": missing,
                        "source_ref": source_ref,
                        "source_digest": source_digest,
                        "provenance_kind": "benchmark_verifier",
                    }
                )
            )
        observed: list[str] = []
        if exposed:
            observed.append("fault_exposure")
            if status != "unknown":
                observed.append("recovery_outcome")
        mode_evidence: list[str] = []
        if autonomous:
            mode_evidence.append("autonomous_action")
            observed.append("autonomous_action")
        if assisted:
            mode_evidence.append("user_system_assisted_action")
            observed.append("user_system_assisted_action")
        required_coverage = (
            ("fault_exposure", "recovery_outcome", "action_provenance")
            if not mode_evidence
            else ("fault_exposure", "recovery_outcome", *mode_evidence)
        )
        if not exposed:
            missing_coverage = tuple(
                evidence for evidence in required_coverage if evidence not in observed
            )
            eligible_coverage: bool | None = None
            ready: bool | None = None
        else:
            missing_coverage = tuple(
                evidence for evidence in required_coverage if evidence not in observed
            )
            eligible_coverage = True if not missing_coverage else None
            ready = True if eligible_coverage else None
        coverage.append(
            EvidenceCoverage.model_validate(
                {
                    "trial_id": trial_id,
                    "benchmark": "Recovery",
                    "construct": "recovery",
                    "exposed": exposed,
                    "eligible": eligible_coverage,
                    "required_evidence": required_coverage,
                    "observed_evidence": tuple(observed),
                    "missing_evidence": missing_coverage,
                    "analysis_ready": ready,
                    "source_ref": source_ref,
                    "source_digest": source_digest,
                    "provenance_kind": "benchmark_verifier",
                }
            )
        )
    return RecoveryProjection(tuple(opportunities), tuple(coverage))


__all__ = [
    "AgentAbstainProjection",
    "AgentAbstainTrialInput",
    "RecoveryProjection",
    "RecoveryTrialInput",
    "project_agentabstain",
    "project_recovery",
]
