"""Analyst recipe engine v1 (R1–R7).

Consumes EvidencePack (+ optional TrajectoryIR) sidecars and emits RecipeFinding
records. Producer is analyst-recipe/v1; automatic acceptance is disabled.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from evallab.schemas import ContractModel
from evallab.trajectory_ir import (
    DEFAULT_EXPECTED_NEGATIVE_PROGRAMS,
    _classify_exit_semantics,
)
from evallab.trajectory_judgment import (
    TRAJECTORY_ONTOLOGY_V1_CLASSES,
    canonical_json_digest,
)

PRODUCER = "analyst-recipe/v1"
CONTRACT_ID = "analysis-recipe-contracts-v1"
CONTRACT_DIGEST = canonical_json_digest({"contract": CONTRACT_ID, "producer": PRODUCER})

RecipeId = Literal["r1", "r2", "r3", "r4", "r5", "r6", "r7"]
Disposition = Literal[
    "candidate_hold", "deterministic_abstention", "screening_only", "alternative_explanations"
]
Validity = Literal["supported", "contradicted", "insufficient_evidence"]
SupportLevel = Literal["e0", "e1", "e2", "e3"]
ClaimType = Literal["success", "partial", "failure", "refusal", "none"]
AbstentionReason = Literal[
    "source_missing",
    "pack_incomplete",
    "linkage_unresolved",
    "profile_missing",
    "opportunity_unknown",
    "replay_oracle_unavailable",
    "pair_unavailable",
    "confounded",
    "mandatory_window_overflow",
    "ontology_gap",
    "digest_mismatch",
    "citation_unresolved",
    "contradicts_verifier_or_state",
    "quality_fail",
]

ABSTENTION_CODES: frozenset[str] = frozenset(
    {
        "source_missing",
        "pack_incomplete",
        "linkage_unresolved",
        "profile_missing",
        "opportunity_unknown",
        "replay_oracle_unavailable",
        "pair_unavailable",
        "confounded",
        "mandatory_window_overflow",
        "ontology_gap",
        "digest_mismatch",
        "citation_unresolved",
        "contradicts_verifier_or_state",
        "quality_fail",
    }
)

REASON_ALIASES: dict[str, str] = {
    "source_missing": "omitted_range",
    "pack_incomplete": "omitted_range",
    "linkage_unresolved": "citation_unresolved",
    "profile_missing": "profile_unpinned",
    "opportunity_unknown": "unknown_opportunity_window",
    "replay_oracle_unavailable": "replay_oracle_unavailable",
    "pair_unavailable": "pair_unavailable",
    "confounded": "confounded",
    "mandatory_window_overflow": "mandatory_window_overflow",
    "ontology_gap": "ontology_gap",
    "digest_mismatch": "digest_mismatch",
    "citation_unresolved": "citation_unresolved",
    "contradicts_verifier_or_state": "contradicts_verifier_or_state",
    "quality_fail": "quality_fail",
}
TAXONOMY_DIGEST = canonical_json_digest(sorted(TRAJECTORY_ONTOLOGY_V1_CLASSES))


LEGACY_BLOCKED_METRIC_NAMES: tuple[str, ...] = (
    "linear_innocence",
    "linear_innocence_screening",
    "tool_error_rate",
    "tool_error_rate_screening",
    "context_burn_velocity",
    "context_burn_velocity_screening",
    "cache_hit_rate",
    "cache_hit_rate_screening",
    "loop_index",
    "LI",
    "TER",
    "CBV",
)

CORPUS_LIMITS_BLOCK: dict[str, Any] = {
    "arms_executed": 1,
    "n_tasks": 5,
    "attempts_per_task": 1,
    "successes": 0,
    "wilson_95": [0.0, 0.434],
    "claims_class": (
        "accounting/descriptive only — no reliability, ranking, capability, or causal claim"
    ),
    "unit": "n_tasks",
    "degenerate_bootstrap": "suppressed",
}

_QUALITY_EXCLUDED = frozenset({"quarantine", "quarantined", "fail"})
_INFRA_EXCEPTION_MARKERS = (
    "timeout",
    "connection",
    "docker",
    "oserror",
    "memoryerror",
    "killed",
    "infrastructure",
    "sandbox",
    "network",
    "httperror",
    "brokenpipe",
    "protocolerror",
)
_REFUSAL_MARKERS = (
    "cannot fulfill",
    "can't fulfill",
    "i cannot",
    "i can't",
    "i am unable to",
    "i'm unable to",
    "unable to perform",
    "refuse",
    "not able to assist",
    "against my programming",
    "i won't",
    "i will not",
    "i am not able",
)
_SUCCESS_MARKERS = (
    "successfully",
    "all tests pass",
    "task is complete",
    "completed the task",
    "fixed the",
    "done.",
)
_FAILURE_MARKERS = (
    "i failed",
    "failed to",
    "could not complete",
    "i was unable",
    "giving up",
    "still failing",
    "tests still fail",
)
_PARTIAL_MARKERS = ("partially", "partial success", "partial completion")
_VERIFIER_FINDING_MARKERS = ("verifier_error", "verifier_failure", "verifier_defect")

_ACTION_EVENT_TYPES = frozenset({"tool_call"})
_ACTION_FAMILIES = frozenset(
    {"file_read", "file_write", "file_edit", "command_execution", "verification", "other"}
)


class RecipeFinding(ContractModel):
    """One recipe result for one (recipe, unit). Not a Platform MachineJudgment."""

    finding_id: str
    recipe_id: RecipeId
    trial_id: str
    disposition: Disposition
    validity: Validity | None
    class_id: str | None
    support_level: SupportLevel
    earliest_supported_ir_event_id: str | None
    citations: list[str]
    alternative_explanations: list[str]
    coverage_gaps: list[str]
    abstention_reason: str | None
    extras: dict[str, Any]
    namespace: Literal["traj.judge.v1"] = "traj.judge.v1"
    ontology_version: Literal["traj.judge.ontology.v1"] = "traj.judge.ontology.v1"
    index_convention: str = "undeclared"
    target_definition: str | None = None
    verbatim_quotes: list[dict[str, str]] = Field(default_factory=list)
    producer: Literal["analyst-recipe/v1"] = PRODUCER
    contract_digest: str = CONTRACT_DIGEST
    is_machine_judgment: Literal[False] = False

    @field_validator("class_id")
    @classmethod
    def validate_ontology_class(cls, value: str | None) -> str | None:
        if value is not None and value not in TRAJECTORY_ONTOLOGY_V1_CLASSES:
            raise ValueError("class_id is not in the frozen trajectory ontology v1")
        return value

    @field_validator("abstention_reason")
    @classmethod
    def validate_abstention_reason(cls, value: str | None) -> str | None:
        if value is not None and value not in ABSTENTION_CODES:
            raise ValueError(f"unknown abstention reason: {value}")
        return value

    @field_validator("citations", "alternative_explanations", "coverage_gaps")
    @classmethod
    def canonicalize_text_lists(cls, values: list[str]) -> list[str]:
        return sorted(set(values))

    @model_validator(mode="after")
    def validate_disposition_contract(self) -> RecipeFinding:
        if self.is_machine_judgment:
            raise ValueError("recipe findings are not machine judgments")
        if self.producer != PRODUCER:
            raise ValueError("producer must be analyst-recipe/v1")
        if self.disposition == "screening_only":
            if self.class_id is not None:
                raise ValueError("screening_only cannot carry a class")
            if self.validity is not None:
                raise ValueError("screening_only cannot carry validity")
        if self.disposition == "deterministic_abstention" and self.class_id is not None:
            raise ValueError("deterministic abstention cannot assign a class")
        if self.class_id is not None:
            if self.disposition != "candidate_hold":
                raise ValueError("only candidate_hold can carry a class")
            if self.validity not in {"supported", "contradicted"} or self.support_level not in {
                "e1",
                "e2",
                "e3",
            }:
                raise ValueError("a class requires supported/contradicted E1+ evidence")
        return self


class TrialArtifacts(ContractModel):
    """Loaded pack (+ optional IR) for one trial analysis sidecar directory."""

    trial_id: str
    pack: dict[str, Any]
    ir: dict[str, Any] | None = None
    alignment_record_ref: dict[str, Any] | None = None
    pack_path: str | None = None
    ir_path: str | None = None
    pack_only: bool = False


def pack_citation_ids(pack: dict[str, Any]) -> set[str]:
    """Citation ids that exist in the pack and may be emitted."""

    ids: set[str] = set()
    for episode in pack.get("episodes") or []:
        for citation in episode.get("key_citations") or []:
            cid = _citation_id(citation)
            if cid:
                ids.add(cid)
    for window in pack.get("selected_windows") or []:
        cid = _citation_id(window.get("reopening_citation"))
        if cid:
            ids.add(cid)
        for event in window.get("events") or []:
            cid = _citation_id(event.get("source_citation"))
            if cid:
                ids.add(cid)
    for omitted in pack.get("omitted_ranges") or []:
        cid = _citation_id(omitted.get("reopening_citation"))
        if cid:
            ids.add(cid)
    return ids


def assert_citations_in_pack(finding: RecipeFinding, pack: dict[str, Any]) -> None:
    """Raise if any cited id is absent from the pack citation universe."""

    allowed = pack_citation_ids(pack)
    unknown = [cid for cid in finding.citations if cid not in allowed]
    if unknown:
        raise ValueError(f"citation ids not in pack: {unknown}")
    _validate_evidence(finding, pack)


def load_trial_artifacts(
    analyses_dir: str | Path, trial_id: str, digest: str | None = None
) -> TrialArtifacts:
    """Load evidence_pack.json and optional trajectory_ir.json for a trial."""

    trial_root = Path(analyses_dir) / trial_id
    pack_paths = sorted(trial_root.glob("*/evidence_pack.json"))
    if not pack_paths:
        raise FileNotFoundError(f"no evidence_pack.json under {trial_root}")
    candidates = [(path, json.loads(path.read_text(encoding="utf-8"))) for path in pack_paths]
    if digest is not None:
        candidates = [
            (path, candidate)
            for path, candidate in candidates
            if candidate.get("pack_digest") == digest or path.parent.name == digest
        ]
        if not candidates:
            raise FileNotFoundError(f"no evidence pack matching digest {digest!r}")
    pack_path, pack = max(
        candidates, key=lambda item: (str(item[1].get("created_at") or ""), item[0].parent.name)
    )
    ir_path = pack_path.parent / "trajectory_ir.json"
    ir: dict[str, Any] | None = None
    pack_only = True
    if ir_path.is_file():
        ir = json.loads(ir_path.read_text(encoding="utf-8"))
        pack_only = False
    alignment = None
    if isinstance(pack.get("alignment_record_ref"), dict):
        alignment = pack["alignment_record_ref"]
    elif isinstance(ir, dict) and isinstance(ir.get("alignment_record_ref"), dict):
        alignment = ir["alignment_record_ref"]
    return TrialArtifacts(
        trial_id=str(pack.get("trial_id") or trial_id),
        pack=pack,
        ir=ir,
        alignment_record_ref=alignment,
        pack_path=str(pack_path),
        ir_path=str(ir_path) if ir is not None else None,
        pack_only=pack_only,
    )


def run_recipes(
    artifacts: TrialArtifacts,
    *,
    semantics_profile_digest: str | None = None,
    expected_identical_families: set[str] | None = None,
) -> list[RecipeFinding]:
    """Run R1–R7. Always emits at least one finding per recipe."""

    ctx = _RecipeContext(artifacts, semantics_profile_digest, expected_identical_families)
    findings: list[RecipeFinding] = [
        _run_r1(ctx),
        _run_r2(ctx),
        _run_r3(ctx),
        _run_r4(ctx),
    ]
    findings.extend(_run_r5(ctx))
    findings.append(_run_r6(ctx))
    findings.append(_run_r7(ctx))
    findings = _apply_precedence(findings)
    for finding in findings:
        assert_citations_in_pack(finding, artifacts.pack)
        _validate_evidence(finding, artifacts.pack)
    return findings


class _RecipeContext:
    def __init__(
        self,
        artifacts: TrialArtifacts,
        semantics_profile_digest: str | None,
        expected_identical_families: set[str] | None,
    ) -> None:
        self.artifacts = artifacts
        self.pack = artifacts.pack
        self.ir = artifacts.ir
        self.trial_id = artifacts.trial_id
        self.profile = semantics_profile_digest
        self.expected_identical_families = frozenset(
            expected_identical_families or {"poll", "wait", "status", "sleep"}
        )
        self.pack_only = artifacts.pack_only or artifacts.ir is None
        self.windows = list(self.pack.get("selected_windows") or [])
        self.window_events = selected_window_events(self.pack)
        self.ir_events: list[dict[str, Any]] = list((self.ir or {}).get("events") or [])
        self.events = self.ir_events or self.window_events
        self.base_gaps: list[str] = []
        if self.pack_only:
            self.base_gaps.append("ir_sidecar_missing")
        quality = str(self.pack.get("quality_status") or "").lower()
        self.quality_excluded = quality in _QUALITY_EXCLUDED
        overflow = str(self.pack.get("overflow_reason") or "")
        self.source_missing = (not self.pack.get("is_model_callable", True)) and (
            overflow.startswith("source_missing")
            or "missing_atif" in overflow
            or str(self.pack.get("final_verdict") or "") == "EVIDENCE_UNAVAILABLE"
        )
        self.overflow = bool(self.pack.get("tiered_pack_required")) or (
            "mandatory_window_budget_overflow" in overflow
        )

    def gaps(self, *extra: str) -> list[str]:
        return [*self.base_gaps, *extra]


def selected_window_events(pack: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for window in pack.get("selected_windows") or []:
        events.extend(list(window.get("events") or []))
    return events


def classify_terminal_claim(text: str | None) -> ClaimType:
    blob = (text or "").strip().lower()
    if not blob:
        return "none"
    if any(marker in blob for marker in _REFUSAL_MARKERS):
        return "refusal"
    if any(marker in blob for marker in _FAILURE_MARKERS):
        return "failure"
    if any(marker in blob for marker in _SUCCESS_MARKERS):
        return "success"
    if any(marker in blob for marker in _PARTIAL_MARKERS):
        return "partial"
    return "none"


def extract_hydrated_text(event: dict[str, Any]) -> str:
    raw = event.get("hydrated_content")
    if not isinstance(raw, str):
        return ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(parsed, dict):
        return next(
            (
                parsed[key]
                for key in ("message", "content", "text", "output")
                if isinstance(parsed.get(key), str)
            ),
            raw,
        )
    return raw


def action_digest(event: dict[str, Any]) -> str:
    payload = event.get("payload_digest")
    if isinstance(payload, str) and payload:
        return payload
    return canonical_json_digest(
        {
            "program": event.get("status_owning_program"),
            "skeleton": event.get("argument_skeleton"),
            "family": event.get("action_family"),
            "event_id": event.get("event_id"),
        }
    )


def ols_slope(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    n = len(xs)
    sum_x = sum(xs)
    sum_y = sum(ys)
    sum_xy = sum(x * y for x, y in zip(xs, ys, strict=True))
    sum_x2 = sum(x * x for x in xs)
    denom = (n * sum_x2) - (sum_x * sum_x)
    if denom == 0:
        return None
    return round(((n * sum_xy) - (sum_x * sum_y)) / denom, 4)


def exit_bucket(event: dict[str, Any], profile: str | None) -> str:
    program = event.get("status_owning_program")
    exit_code = event.get("exit_code")
    is_error = bool(event.get("is_error"))
    prog_base = (program or "").split("_")[0].lower()
    if profile is None:
        if exit_code == 1 and prog_base in DEFAULT_EXPECTED_NEGATIVE_PROGRAMS:
            return "unknown"
        if exit_code == 0:
            return "success"
        if is_error or (isinstance(exit_code, int) and exit_code != 0):
            return "error"
        return str(event.get("exit_semantics") or "unobserved")
    semantics, _true_error = _classify_exit_semantics(exit_code, program, is_error)
    return semantics


def _citation_id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        cid = value.get("citation_id")
        if isinstance(cid, str) and cid:
            return cid
    cid = getattr(value, "citation_id", None)
    if isinstance(cid, str) and cid:
        return cid
    return None


def _event_citation(event: dict[str, Any]) -> str | None:
    return _citation_id(event.get("source_citation"))


def _omitted_range_for_step(pack: dict[str, Any], step_index: int | None) -> dict[str, Any] | None:
    if step_index is None:
        return None
    for omitted in pack.get("omitted_ranges") or []:
        start = omitted.get("step_start")
        end = omitted.get("step_end")
        if isinstance(start, int) and isinstance(end, int) and start <= step_index <= end:
            return omitted
    return None


def _is_typed_infra_exception(exception_class: Any) -> bool:
    if not exception_class or not isinstance(exception_class, str):
        return False
    blob = exception_class.lower()
    return any(marker in blob for marker in _INFRA_EXCEPTION_MARKERS)


def _verifier_defect(pack: dict[str, Any], ir: dict[str, Any] | None) -> bool:
    verdict = str(pack.get("final_verdict") or (ir or {}).get("final_verdict") or "")
    if verdict == "VERIFIER_ERROR":
        return True
    findings = list(pack.get("quality_findings") or [])
    findings.extend(list((ir or {}).get("quality_findings") or []))
    blob = " ".join(str(item).lower() for item in findings)
    return any(marker in blob for marker in _VERIFIER_FINDING_MARKERS)


def _first_citation(ctx: _RecipeContext) -> str | None:
    if ctx.window_events:
        cid = _event_citation(ctx.window_events[0])
        if cid:
            return cid
    for window in ctx.windows:
        cid = _citation_id(window.get("reopening_citation"))
        if cid:
            return cid
    return None


def _event_id(event: dict[str, Any]) -> str | None:
    value = event.get("event_id")
    return value if isinstance(value, str) else None


def _index_convention(ctx: _RecipeContext) -> str:
    for name, source in (("ir", ctx.ir), ("pack", ctx.pack)):
        if isinstance(source, dict):
            for key in ("step_id_base", "step_index_base", "index_base"):
                if isinstance(source.get(key), int):
                    return f"{name}.{key}:{source[key]}"
    return "undeclared"


def _provenance_extras(ctx: _RecipeContext, extras: dict[str, Any] | None) -> dict[str, Any]:
    result = dict(extras or {})
    specific_alias = result.get("reason_alias")
    result.update(
        {
            "pack_digest": ctx.pack.get("pack_digest"),
            "taxonomy_digest": TAXONOMY_DIGEST,
            "pack_builder_digest": ctx.pack.get("pack_builder_digest"),
            "reason_alias": dict(REASON_ALIASES),
            "prompt_hash": None,
            "model_hash": None,
        }
    )
    if isinstance(specific_alias, str) and specific_alias:
        result["reason_alias"] = specific_alias
    return result


def _quote_records(ctx: _RecipeContext, citations: list[str]) -> list[dict[str, str]]:
    for event in ctx.window_events:
        citation = _event_citation(event)
        content = extract_hydrated_text(event)
        raw = event.get("hydrated_content")
        if citation in citations and isinstance(raw, str) and raw:
            quote = content if content and content.encode() in raw.encode() else raw
            return [{"citation_id": citation, "quote": quote}]
    return []


def _validate_evidence(finding: RecipeFinding, pack: dict[str, Any]) -> None:
    content: dict[str, str] = {}
    for event in selected_window_events(pack):
        citation_key = _event_citation(event)
        if citation_key is None:
            continue
        hydrated = event.get("hydrated_content")
        content[citation_key] = hydrated if isinstance(hydrated, str) else ""
    for quote in finding.verbatim_quotes:
        citation, text = quote.get("citation_id"), quote.get("quote")
        if (
            citation not in content
            or not isinstance(text, str)
            or text.encode() not in content[citation].encode()
        ):
            raise ValueError("verbatim quote is not a selected-window event byte-substring")
    if (
        finding.class_id is not None
        and finding.validity in {"supported", "contradicted"}
        and finding.support_level in {"e1", "e2", "e3"}
    ):
        if not finding.verbatim_quotes:
            raise ValueError("e1 label requires verbatim quote")
        if not set(finding.citations).issubset(set(content)):
            raise ValueError("e1 labels may cite only selected-window event citations")


def _emit(
    ctx: _RecipeContext,
    *,
    recipe_id: RecipeId,
    disposition: Disposition,
    validity: Validity | None,
    class_id: str | None,
    support_level: SupportLevel,
    earliest_supported_ir_event_id: str | None = None,
    citations: list[str] | None = None,
    alternative_explanations: list[str] | None = None,
    coverage_gaps: list[str] | None = None,
    abstention_reason: str | None = None,
    extras: dict[str, Any] | None = None,
    verbatim_quotes: list[dict[str, str]] | None = None,
) -> RecipeFinding:
    cited = list(citations or [])
    quotes = list(verbatim_quotes or [])
    if (
        class_id is not None
        and validity in {"supported", "contradicted"}
        and support_level in {"e1", "e2", "e3"}
    ):
        quotes = quotes or _quote_records(ctx, cited)
        if not quotes:
            return _abstain(
                ctx,
                recipe_id,
                "pack_incomplete",
                extras=extras,
                citations=cited,
                gaps=["verbatim_quote_unavailable"],
                earliest=earliest_supported_ir_event_id,
            )
    payload: dict[str, Any] = {
        "recipe_id": recipe_id,
        "trial_id": ctx.trial_id,
        "disposition": disposition,
        "validity": validity,
        "class_id": class_id,
        "support_level": support_level,
        "earliest_supported_ir_event_id": earliest_supported_ir_event_id,
        "citations": cited,
        "alternative_explanations": list(alternative_explanations or []),
        "coverage_gaps": list(coverage_gaps or []),
        "abstention_reason": abstention_reason,
        "extras": _provenance_extras(ctx, extras),
        "namespace": "traj.judge.v1",
        "ontology_version": "traj.judge.ontology.v1",
        "index_convention": _index_convention(ctx),
        "target_definition": "decisive_evidential" if recipe_id == "r2" else None,
        "verbatim_quotes": quotes,
        "producer": PRODUCER,
        "contract_digest": CONTRACT_DIGEST,
        "is_machine_judgment": False,
    }
    payload["finding_id"] = canonical_json_digest(payload)
    finding = RecipeFinding.model_validate(payload)
    assert_citations_in_pack(finding, ctx.pack)
    _validate_evidence(finding, ctx.pack)
    return finding


def _abstain(
    ctx: _RecipeContext,
    recipe_id: RecipeId,
    reason: str | None,
    *,
    extras: dict[str, Any] | None = None,
    citations: list[str] | None = None,
    gaps: list[str] | None = None,
    earliest: str | None = None,
) -> RecipeFinding:
    return _emit(
        ctx,
        recipe_id=recipe_id,
        disposition="deterministic_abstention",
        validity=None,
        class_id=None,
        support_level="e0",
        earliest_supported_ir_event_id=earliest,
        citations=citations,
        coverage_gaps=ctx.gaps(*(gaps or [])),
        abstention_reason=reason,
        extras=extras or {},
    )


def _r1_extras() -> dict[str, Any]:
    return {"attribution_basis": None, "task_package_suspect": False}


def _r2_extras() -> dict[str, Any]:
    return {"propagated_event_ids": [], "recovery_possible_at": None}


def _r4_extras() -> dict[str, Any]:
    return {
        "claim_quote_citation": None,
        "verification_actions_cited": [],
        "claim_type": "none",
    }


def _r5_extras() -> dict[str, Any]:
    return {
        "fault_event_id": None,
        "response_pattern": None,
        "intervention_provenance": None,
        "accidental_success_suspect": False,
    }


def _common_block(ctx: _RecipeContext, recipe_id: RecipeId) -> RecipeFinding | None:
    extras: dict[str, Any] = {}
    if recipe_id == "r1":
        extras = _r1_extras()
    elif recipe_id == "r2":
        extras = _r2_extras()
    elif recipe_id == "r4":
        extras = _r4_extras()
    elif recipe_id == "r5":
        extras = _r5_extras()
    if ctx.quality_excluded:
        return _abstain(ctx, recipe_id, "quality_fail", extras=extras, gaps=["quality_excluded"])
    if ctx.source_missing:
        return _abstain(ctx, recipe_id, "source_missing", extras=extras)
    if ctx.overflow and recipe_id != "r7":
        return _abstain(ctx, recipe_id, "mandatory_window_overflow", extras=extras)
    return None


def _span_unpaired(ctx: _RecipeContext, events: list[dict[str, Any]]) -> bool:
    coverage = ctx.pack.get("evidence_coverage") or {}
    unpaired = int(coverage.get("unpaired_tool_calls_count") or 0)
    if ctx.ir:
        unpaired = max(unpaired, int(ctx.ir.get("unpaired_tool_calls_count") or 0))
    if unpaired <= 0:
        return False
    for event in events:
        if event.get("event_type") == "tool_call" and not event.get("matched_result_digest"):
            return True
    return False


def _is_call_event(event: dict[str, Any]) -> bool:
    if event.get("event_type") in _ACTION_EVENT_TYPES:
        return True
    if event.get("event_type") in {
        "agent_message",
        "user_message",
        "context_management",
        "observation",
    }:
        return False
    family = event.get("action_family")
    return family in _ACTION_FAMILIES and event.get("status_owning_program") is not None


def _is_error_observation(event: dict[str, Any], profile: str | None) -> bool:
    bucket = exit_bucket(event, profile)
    if bucket == "error":
        return True
    if bucket in {"expected_negative", "unknown", "success", "unobserved"}:
        return False
    return bool(event.get("event_type") == "observation" and event.get("is_error"))


_TERMINAL_OMITTED_MARKERS = ("terminal", "outcome", "verdict", "final claim")
_DEPENDENCY_FIELDS = (
    "depends_on",
    "parent_event_id",
    "source_event_id",
    "caused_by",
    "dependency_event_id",
    "input_event_ids",
    "references",
    "citation_chain",
)


def _omitted_reopen(omitted: dict[str, Any] | None) -> str | None:
    if omitted is None:
        return None
    return _citation_id(omitted.get("reopening_citation"))


def _omitted_terminal_range(
    pack: dict[str, Any], ir: dict[str, Any] | None
) -> dict[str, Any] | None:
    omitted_ranges = list(pack.get("omitted_ranges") or [])
    if not omitted_ranges:
        return None
    window_end = 0
    for window in pack.get("selected_windows") or []:
        end = window.get("step_end")
        if isinstance(end, int):
            window_end = max(window_end, end)
    ir_steps = [
        event.get("step_index")
        for event in (ir or {}).get("events") or []
        if isinstance(event.get("step_index"), int)
    ]
    outcome_step = max(ir_steps) if ir_steps else None
    for omitted in omitted_ranges:
        start = omitted.get("step_start")
        end = omitted.get("step_end")
        summary = str(omitted.get("summary") or "").lower()
        if any(marker in summary for marker in _TERMINAL_OMITTED_MARKERS):
            return omitted
        if isinstance(start, int) and start > window_end:
            return omitted
        if (
            outcome_step is not None
            and isinstance(start, int)
            and isinstance(end, int)
            and start <= outcome_step <= end
        ):
            return omitted
    return None


def _omitted_before_event(pack: dict[str, Any], event: dict[str, Any]) -> dict[str, Any] | None:
    step = event.get("step_index")
    if not isinstance(step, int):
        return None
    for omitted in pack.get("omitted_ranges") or []:
        start = omitted.get("step_start")
        end = omitted.get("step_end")
        if isinstance(start, int) and start < step:
            return omitted
        if isinstance(end, int) and end < step:
            return omitted
    return None


def _observation_for_action(
    action: dict[str, Any],
    observations: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    action_ord = int(action.get("event_ordinal") or 0)
    action_step = action.get("step_index")
    action_id = _event_id(action)
    matched = action.get("matched_result_digest")
    next_ords = [
        int(event.get("event_ordinal") or 0)
        for event in events
        if _is_call_event(event) and int(event.get("event_ordinal") or 0) > action_ord
    ]
    horizon = min(next_ords) if next_ords else None
    explicit: dict[str, Any] | None = None
    same_step: dict[str, Any] | None = None
    next_obs: dict[str, Any] | None = None
    for obs in observations:
        obs_ord = int(obs.get("event_ordinal") or 0)
        if obs_ord < action_ord:
            continue
        if horizon is not None and obs_ord >= horizon:
            continue
        if action_id and obs.get("source_event_id") == action_id:
            explicit = obs
            break
        if action_id and obs.get("parent_event_id") == action_id:
            explicit = obs
            break
        if isinstance(matched, str) and matched and obs.get("payload_digest") == matched:
            explicit = obs
            break
        if action_step is not None and obs.get("step_index") == action_step and same_step is None:
            same_step = obs
        if next_obs is None:
            next_obs = obs
    return explicit or same_step or next_obs


def _paired_action_observation(
    events: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    observations = [event for event in events if event.get("event_type") == "observation"]
    for action in events:
        if not _is_call_event(action):
            continue
        obs = _observation_for_action(action, observations, events)
        if obs is not None:
            return action, obs
    return None


def _explicit_dependency(event: dict[str, Any], decisive: dict[str, Any]) -> bool:
    decisive_id = _event_id(decisive)
    decisive_cite = _event_citation(decisive)
    tokens = [item for item in (decisive_id, decisive_cite) if isinstance(item, str)]
    for field in _DEPENDENCY_FIELDS:
        value = event.get(field)
        items = value if isinstance(value, list) else [value]
        for item in items:
            if item in tokens:
                return True
            if isinstance(item, dict):
                cited = _citation_id(item) or item.get("event_id")
                if cited in tokens:
                    return True
    outputs = [
        item
        for item in (
            decisive.get("payload_digest"),
            decisive.get("matched_result_digest"),
            decisive_id,
        )
        if isinstance(item, str) and item
    ]
    blob = f"{extract_hydrated_text(event)} {event.get('argument_skeleton') or ''}"
    return any(token in blob for token in outputs)


def _source_digests(ctx: _RecipeContext) -> dict[str, Any]:
    pack_digests = dict(ctx.pack.get("source_digests") or {})
    if ctx.ir:
        pack_digests.update(ctx.ir.get("source_digests") or {})
    return pack_digests


def _run_r1(ctx: _RecipeContext) -> RecipeFinding:
    blocked = _common_block(ctx, "r1")
    if blocked:
        return blocked
    extras = _r1_extras()
    exception_class = ctx.pack.get("exception_class")
    if exception_class is None and ctx.ir:
        exception_class = ctx.ir.get("exception_class")
    citations = [cid for cid in [_first_citation(ctx)] if cid]
    earliest = _event_id(ctx.window_events[0]) if ctx.window_events else None

    if _is_typed_infra_exception(exception_class):
        extras["attribution_basis"] = "exception"
        return _emit(
            ctx,
            recipe_id="r1",
            disposition="candidate_hold",
            validity="supported",
            class_id="infrastructure_failure",
            support_level="e1",
            earliest_supported_ir_event_id=earliest,
            citations=citations,
            coverage_gaps=ctx.gaps(),
            extras=extras,
        )
    if _verifier_defect(ctx.pack, ctx.ir):
        extras["attribution_basis"] = "verifier_evidence"
        return _emit(
            ctx,
            recipe_id="r1",
            disposition="candidate_hold",
            validity="supported",
            class_id="verifier_failure",
            support_level="e1",
            earliest_supported_ir_event_id=earliest,
            citations=citations,
            coverage_gaps=ctx.gaps(),
            extras=extras,
        )

    digests = _source_digests(ctx)
    if not digests.get("task_digest") or not digests.get("verifier_digest"):
        extras["task_package_suspect"] = True
        return _abstain(
            ctx,
            "r1",
            "ontology_gap",
            extras=extras,
            citations=citations,
            gaps=["task_package_suspect"],
        )

    refusal = next(
        (
            event
            for event in ctx.window_events
            if event.get("event_type") == "agent_message"
            and classify_terminal_claim(extract_hydrated_text(event)) == "refusal"
        ),
        None,
    )
    if refusal is not None:
        citation = _event_citation(refusal)
        return _abstain(
            ctx,
            "r1",
            "ontology_gap",
            extras=extras,
            citations=[citation] if citation else None,
            gaps=["refusal_has_no_frozen_class"],
        )
    omitted_terminal = _omitted_terminal_range(ctx.pack, ctx.ir)
    if omitted_terminal is not None:
        reopen = _omitted_reopen(omitted_terminal)
        return _abstain(
            ctx,
            "r1",
            "pack_incomplete",
            extras=extras,
            citations=[reopen] if reopen else None,
            gaps=["terminal_window_omitted"],
        )

    paired = _paired_action_observation(ctx.window_events)
    if paired is not None:
        action, obs = paired
        cited = [c for c in (_event_citation(action), _event_citation(obs)) if c]
        if _span_unpaired(ctx, [action, obs]):
            return _abstain(ctx, "r1", "linkage_unresolved", extras=extras, citations=cited)
        extras["attribution_basis"] = "action_evidence"
        return _emit(
            ctx,
            recipe_id="r1",
            disposition="candidate_hold",
            validity="supported",
            class_id="wrong_target_or_action",
            support_level="e1",
            earliest_supported_ir_event_id=_event_id(action),
            citations=cited,
            coverage_gaps=ctx.gaps(),
            extras=extras,
        )

    omitted = list(ctx.pack.get("omitted_ranges") or [])
    has_terminal = any(
        event.get("event_type") == "agent_message"
        and classify_terminal_claim(extract_hydrated_text(event)) != "none"
        for event in ctx.window_events
    )
    if omitted and not has_terminal:
        reopen = _citation_id(omitted[0].get("reopening_citation"))
        return _abstain(
            ctx,
            "r1",
            "pack_incomplete",
            extras=extras,
            citations=[reopen] if reopen else None,
            gaps=["terminal_window_omitted"],
        )

    gaps = ["no_decisive_action_in_windows"]
    if not has_terminal:
        gaps.append("no_terminal_claim_in_windows")
    return _emit(
        ctx,
        recipe_id="r1",
        disposition="candidate_hold",
        validity="insufficient_evidence",
        class_id=None,
        support_level="e0",
        citations=citations,
        coverage_gaps=ctx.gaps(*gaps),
        extras=extras,
    )


def _run_r2(ctx: _RecipeContext) -> RecipeFinding:
    blocked = _common_block(ctx, "r2")
    if blocked:
        return blocked
    extras = _r2_extras()
    events = ctx.window_events
    if not events:
        omitted = list(ctx.pack.get("omitted_ranges") or [])
        if omitted:
            reopen = _citation_id(omitted[0].get("reopening_citation"))
            return _abstain(
                ctx,
                "r2",
                "pack_incomplete",
                extras=extras,
                citations=[reopen] if reopen else None,
            )
        return _abstain(ctx, "r2", "opportunity_unknown", extras=extras)

    profiled_negatives: list[dict[str, Any]] = []
    unprofiled_unknowns: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for event in events:
        if not _is_call_event(event) and event.get("event_type") != "observation":
            continue
        bucket = exit_bucket(event, ctx.profile)
        if bucket == "expected_negative":
            profiled_negatives.append(event)
        elif bucket == "unknown":
            unprofiled_unknowns.append(event)
        elif bucket == "error":
            errors.append(event)

    if unprofiled_unknowns and not profiled_negatives:
        event = unprofiled_unknowns[0]
        extras["exit_semantics"] = "unknown"
        return _abstain(
            ctx,
            "r2",
            "profile_missing",
            extras=extras,
            citations=[c for c in [_event_citation(event)] if c],
            earliest=_event_id(event),
            gaps=["unknown_exit_semantics"],
        )
    if profiled_negatives:
        event = profiled_negatives[0]
        cited = [c for c in [_event_citation(event)] if c]
        if _span_unpaired(ctx, [event]):
            return _abstain(ctx, "r2", "linkage_unresolved", extras=extras, citations=cited)
        extras["exit_semantics"] = "expected_negative"
        return _emit(
            ctx,
            recipe_id="r2",
            disposition="candidate_hold",
            validity="supported",
            class_id="expected_negative_exit",
            support_level="e1",
            earliest_supported_ir_event_id=_event_id(event),
            citations=cited,
            coverage_gaps=ctx.gaps(),
            extras=extras,
        )
    if errors:
        decisive = errors[0]
        earlier_omitted = _omitted_before_event(ctx.pack, decisive)
        if earlier_omitted is not None:
            reopen = _omitted_reopen(earlier_omitted)
            return _abstain(
                ctx,
                "r2",
                "pack_incomplete",
                extras=extras,
                citations=[reopen] if reopen else None,
                gaps=["earlier_range_omitted"],
            )
        dependents = [
            item
            for item in events
            if item.get("event_id") != decisive.get("event_id")
            and _explicit_dependency(item, decisive)
        ]
        cited = [c for c in [_event_citation(decisive)] if c]
        for dep in dependents:
            dep_c = _event_citation(dep)
            if dep_c:
                cited.append(dep_c)
        if _span_unpaired(ctx, [decisive, *dependents[:1]]):
            return _abstain(ctx, "r2", "linkage_unresolved", extras=extras, citations=cited)
        extras["propagated_event_ids"] = [
            item["event_id"] for item in dependents if isinstance(item.get("event_id"), str)
        ]
        summary = (
            f"{extract_hydrated_text(decisive)} {decisive.get('argument_skeleton') or ''}".lower()
        )
        class_id = "tool_schema_misuse" if "schema" in summary else "wrong_target_or_action"
        return _emit(
            ctx,
            recipe_id="r2",
            disposition="candidate_hold",
            validity="supported",
            class_id=class_id,
            support_level="e1",
            earliest_supported_ir_event_id=_event_id(decisive),
            citations=cited,
            alternative_explanations=[
                f"earlier_error:{item.get('event_id')}"
                for item in errors[1:]
                if item.get("event_id")
            ],
            coverage_gaps=ctx.gaps(),
            extras=extras,
        )
    return _abstain(ctx, "r2", "opportunity_unknown", extras=extras, gaps=["no_error_in_windows"])


def _k_star_suffix(ref: dict[str, Any]) -> Any:
    if "k_star_suffix" in ref:
        return ref["k_star_suffix"]
    local = list(ref.get("local_divergences") or [])
    unmatched_a = list(ref.get("unmatched_ranges_a") or [])
    unmatched_b = list(ref.get("unmatched_ranges_b") or [])
    if not local:
        return {"unmatched_ranges_a": unmatched_a, "unmatched_ranges_b": unmatched_b}

    def _reconv_key(item: dict[str, Any]) -> int:
        return max(
            int(item.get("reconvergence_step_a") or 0),
            int(item.get("reconvergence_step_b") or 0),
        )

    last = max(local, key=_reconv_key)
    after_a = int(last.get("reconvergence_step_a") or 0)
    after_b = int(last.get("reconvergence_step_b") or 0)

    def _after(ranges: list[Any], threshold: int) -> list[Any]:
        kept: list[Any] = []
        for item in ranges:
            if isinstance(item, (list, tuple)) and item:
                if int(item[0]) >= threshold:
                    kept.append(list(item))
            elif (
                isinstance(item, dict)
                and item.get("start") is not None
                and int(item["start"]) >= threshold
            ):
                kept.append(item)
        return kept

    return {
        "after_last_reconvergence_a": after_a,
        "after_last_reconvergence_b": after_b,
        "unmatched_ranges_a": _after(unmatched_a, after_a),
        "unmatched_ranges_b": _after(unmatched_b, after_b),
    }


def _run_r3(ctx: _RecipeContext) -> RecipeFinding:
    blocked = _common_block(ctx, "r3")
    if blocked:
        return blocked
    ref = ctx.artifacts.alignment_record_ref
    confounders = [
        "variable_trajectory_length",
        "alternate_valid_plans",
        "task_heterogeneity",
    ]
    if not ref:
        return _abstain(
            ctx,
            "r3",
            "pair_unavailable",
            extras={"confounders": confounders, "k_star_suffix": None},
        )
    validity = str(ref.get("validity") or "")
    if validity != "valid":
        return _abstain(
            ctx,
            "r3",
            "confounded",
            extras={"confounders": confounders, "alignment_validity": validity or "unknown"},
        )
    extras = {
        "local_divergences": list(ref.get("local_divergences") or []),
        "reconvergences": list(ref.get("reconvergences") or []),
        "k_star_suffix": _k_star_suffix(ref),
        "confounders": confounders,
    }
    allowed = pack_citation_ids(ctx.pack)
    citations: list[str] = []
    for key in ("citation_a", "citation_b", "citations"):
        value = ref.get(key)
        if isinstance(value, list):
            for item in value:
                cid = _citation_id(item)
                if cid and cid in allowed:
                    citations.append(cid)
        else:
            cid = _citation_id(value)
            if cid and cid in allowed:
                citations.append(cid)
    return _emit(
        ctx,
        recipe_id="r3",
        disposition="screening_only",
        validity=None,
        class_id=None,
        support_level="e0",
        citations=citations,
        coverage_gaps=ctx.gaps(),
        extras=extras,
    )


def _verification_citations(events: list[dict[str, Any]], before_ordinal: int) -> list[str]:
    cited: list[str] = []
    for event in events:
        if event.get("event_ordinal", 0) >= before_ordinal:
            continue
        family = str(event.get("action_family") or "")
        program = str(event.get("status_owning_program") or "").lower()
        if family == "verification" or program in {"pytest", "verify", "test.sh"}:
            cid = _event_citation(event)
            if cid:
                cited.append(cid)
    return cited


def _run_r4(ctx: _RecipeContext) -> RecipeFinding:
    blocked = _common_block(ctx, "r4")
    if blocked:
        return blocked
    extras = _r4_extras()
    if _verifier_defect(ctx.pack, ctx.ir):
        extras["reason_alias"] = "verifier_excluded"
        return _abstain(
            ctx,
            "r4",
            "contradicts_verifier_or_state",
            extras=extras,
            gaps=["verifier_failure_excluded"],
        )

    window_claims = [
        event for event in ctx.window_events if event.get("event_type") == "agent_message"
    ]
    if not window_claims:
        for event in ctx.ir_events:
            if event.get("event_type") != "agent_message":
                continue
            omitted = _omitted_range_for_step(ctx.pack, event.get("step_index"))
            if omitted is None:
                continue
            reopen = _citation_id(omitted.get("reopening_citation"))
            return _abstain(
                ctx,
                "r4",
                "pack_incomplete",
                extras=extras,
                citations=[reopen] if reopen else None,
                gaps=["terminal_claim_omitted"],
            )
        return _abstain(ctx, "r4", "opportunity_unknown", extras=extras)

    claim_event = window_claims[-1]
    claim_type = classify_terminal_claim(extract_hydrated_text(claim_event))
    extras["claim_type"] = claim_type
    extras["claim_quote_citation"] = _event_citation(claim_event)
    extras["verification_actions_cited"] = _verification_citations(
        ctx.window_events, int(claim_event.get("event_ordinal") or 0)
    )
    citations = [c for c in [extras["claim_quote_citation"]] if c]
    citations.extend(extras["verification_actions_cited"])

    if claim_type == "none":
        return _abstain(ctx, "r4", "opportunity_unknown", extras=extras, citations=citations)
    if claim_type == "refusal":
        return _emit(
            ctx,
            recipe_id="r4",
            disposition="deterministic_abstention",
            validity=None,
            class_id=None,
            support_level="e0",
            earliest_supported_ir_event_id=_event_id(claim_event),
            citations=citations,
            coverage_gaps=ctx.gaps("refusal_has_no_frozen_class"),
            abstention_reason="ontology_gap",
            extras=extras,
            verbatim_quotes=_quote_records(ctx, citations),
        )
    if claim_type in {"failure", "partial"}:
        return _emit(
            ctx,
            recipe_id="r4",
            disposition="candidate_hold",
            validity="insufficient_evidence",
            class_id=None,
            support_level="e1",
            earliest_supported_ir_event_id=_event_id(claim_event),
            citations=citations,
            coverage_gaps=ctx.gaps(),
            extras=extras,
        )

    verdict = str(ctx.pack.get("final_verdict") or "")
    reward = ctx.pack.get("primary_reward")
    verifier_failed = verdict == "FAIL" or reward == 0 or reward == 0.0
    cited_verify = extras["verification_actions_cited"]
    contradicting = False
    if cited_verify:
        for event in ctx.window_events:
            cid = _event_citation(event)
            if cid in cited_verify and (
                event.get("is_error") or exit_bucket(event, ctx.profile) == "error"
            ):
                contradicting = True
    if claim_type == "success" and verifier_failed and (not cited_verify or contradicting):
        return _emit(
            ctx,
            recipe_id="r4",
            disposition="candidate_hold",
            validity="supported",
            class_id="false_verification_or_unsupported_terminal_claim",
            support_level="e1",
            earliest_supported_ir_event_id=_event_id(claim_event),
            citations=citations,
            coverage_gaps=ctx.gaps(),
            extras=extras,
        )
    return _emit(
        ctx,
        recipe_id="r4",
        disposition="candidate_hold",
        validity="insufficient_evidence",
        class_id=None,
        support_level="e0",
        earliest_supported_ir_event_id=_event_id(claim_event),
        citations=citations,
        coverage_gaps=ctx.gaps(),
        extras=extras,
    )


def _intervention_provenance(events: list[dict[str, Any]]) -> str:
    for event in events:
        actor = str(event.get("actor") or "")
        event_type = str(event.get("event_type") or "")
        if actor == "user" or event_type == "user_message":
            return "user_assisted"
        if actor == "system" or event_type in {"system_message", "system"}:
            return "system_assisted"
    return "autonomous" if events else "unknown"


def _response_pattern(post_events: list[dict[str, Any]]) -> str:
    actions = [event for event in post_events if _is_call_event(event)]
    if not actions:
        return "abandoned"
    digests = [action_digest(event) for event in actions]
    counts: dict[str, int] = {}
    for digest in digests:
        counts[digest] = counts.get(digest, 0) + 1
    if any(count >= 2 for count in counts.values()):
        return "identical_retry"
    return "changed_strategy"


def _fault_episodes(ctx: _RecipeContext) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    events = ctx.window_events
    return [
        (event, events[index + 1 :])
        for index, event in enumerate(events)
        if _is_error_observation(event, ctx.profile)
    ]


def _run_r5(ctx: _RecipeContext) -> list[RecipeFinding]:
    blocked = _common_block(ctx, "r5")
    if blocked:
        return [blocked]
    episodes = _fault_episodes(ctx)
    if not episodes:
        return [_abstain(ctx, "r5", "opportunity_unknown", extras=_r5_extras())]
    findings: list[RecipeFinding] = []
    for fault, subsequent in episodes:
        citations = [
            citation for item in [fault, *subsequent] if (citation := _event_citation(item))
        ]
        extras = _r5_extras()
        extras["fault_event_id"] = fault.get("event_id")
        provenance = _intervention_provenance(subsequent)
        extras["intervention_provenance"] = provenance
        agent_turns = [
            item for item in subsequent if item.get("actor") == "agent" or _is_call_event(item)
        ]
        if not agent_turns:
            extras["censored"] = True
            findings.append(
                _abstain(
                    ctx,
                    "r5",
                    "opportunity_unknown",
                    extras=extras,
                    citations=citations,
                    earliest=_event_id(fault),
                    gaps=["right_censored_no_autonomous_turn"],
                )
            )
            continue
        actions = [item for item in agent_turns if _is_call_event(item)]
        repeated = len({action_digest(item) for item in actions}) < len(actions)
        expected = actions and all(
            any(
                family
                in str(item.get("status_owning_program") or item.get("tool_name") or "").lower()
                for family in ctx.expected_identical_families
            )
            for item in actions
        )
        extras.update(
            {
                "censored": False,
                "response_pattern": "identical_retry" if repeated else "changed_strategy",
                "intervention_provenance": provenance,
                "accidental_success_suspect": False,
            }
        )
        if provenance != "autonomous":
            findings.append(
                _emit(
                    ctx,
                    recipe_id="r5",
                    disposition="candidate_hold",
                    validity="insufficient_evidence",
                    class_id=None,
                    support_level="e0",
                    earliest_supported_ir_event_id=_event_id(fault),
                    citations=citations,
                    coverage_gaps=ctx.gaps("non_autonomous_intervention"),
                    extras=extras,
                )
            )
            continue
        if repeated and not expected:
            findings.append(
                _emit(
                    ctx,
                    recipe_id="r5",
                    disposition="candidate_hold",
                    validity="supported",
                    class_id="repeated_failure_or_thrashing",
                    support_level="e1",
                    earliest_supported_ir_event_id=_event_id(fault),
                    citations=citations,
                    coverage_gaps=ctx.gaps(),
                    extras=extras,
                )
            )
        else:
            findings.append(
                _abstain(
                    ctx,
                    "r5",
                    "replay_oracle_unavailable",
                    extras=extras,
                    citations=citations,
                    earliest=_event_id(fault),
                )
            )
    return findings


def _run_r6(ctx: _RecipeContext) -> RecipeFinding:
    blocked = _common_block(ctx, "r6")
    if blocked:
        return blocked
    window_events = ctx.window_events
    window_ids = {_event_id(event) for event in window_events if _event_id(event)}
    boundaries = [
        event for event in window_events if event.get("event_type") == "context_management"
    ]
    if not boundaries:
        return _abstain(ctx, "r6", "opportunity_unknown")
    boundary = boundaries[0]
    boundary_ord = int(boundary.get("event_ordinal") or 0)
    pre = [event for event in window_events if int(event.get("event_ordinal") or 0) < boundary_ord]
    post = [event for event in window_events if int(event.get("event_ordinal") or 0) > boundary_ord]
    pre_digests = {action_digest(event) for event in pre if _is_call_event(event)}
    post_digests = {action_digest(event) for event in post if _is_call_event(event)}
    lost = pre_digests - post_digests
    citations = [c for c in [_event_citation(boundary)] if c]
    extras = {"boundary_event_id": boundary.get("event_id")}
    if lost and pre and post:
        cited_pre = next(
            (event for event in pre if _is_call_event(event) and action_digest(event) in lost),
            None,
        )
        cited_post = next(
            (event for event in post if _event_citation(event)),
            post[0],
        )
        if cited_pre and _event_citation(cited_pre):
            citations.append(_event_citation(cited_pre) or "")
        post_cite = _event_citation(cited_post)
        if post_cite:
            citations.append(post_cite)
        citations = [c for c in citations if c]
        return _emit(
            ctx,
            recipe_id="r6",
            disposition="candidate_hold",
            validity="supported",
            class_id="context_or_constraint_loss",
            support_level="e1",
            earliest_supported_ir_event_id=_event_id(boundary),
            citations=citations,
            coverage_gaps=ctx.gaps(),
            extras=extras,
        )
    outside_post = [
        event
        for event in ctx.ir_events
        if int(event.get("event_ordinal") or 0) > boundary_ord
        and _event_id(event) not in window_ids
    ]
    if pre_digests and outside_post:
        omitted = None
        for event in outside_post:
            omitted = _omitted_range_for_step(ctx.pack, event.get("step_index"))
            if omitted is not None:
                break
        reason = "pack_incomplete" if omitted is not None else "opportunity_unknown"
        reopen = _omitted_reopen(omitted)
        cited = list(citations)
        if reopen:
            cited.append(reopen)
        return _abstain(
            ctx,
            "r6",
            reason,
            extras=extras,
            citations=cited or None,
            gaps=["post_boundary_outside_windows"],
        )
    return _emit(
        ctx,
        recipe_id="r6",
        disposition="candidate_hold",
        validity="insufficient_evidence",
        class_id=None,
        support_level="e1",
        earliest_supported_ir_event_id=_event_id(boundary),
        citations=citations,
        coverage_gaps=ctx.gaps(),
        extras=extras,
    )


def _prompt_token_points(events: list[dict[str, Any]]) -> tuple[list[float], list[float]]:
    xs: list[float] = []
    ys: list[float] = []
    ordinal = 0
    for event in events:
        tokens = event.get("prompt_tokens")
        if tokens is None:
            raw = event.get("hydrated_content")
            if isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict) and parsed.get("prompt_tokens") is not None:
                    tokens = parsed.get("prompt_tokens")
        is_llm = event.get("event_type") == "agent_message" or tokens is not None
        if not is_llm:
            continue
        if tokens is None:
            ordinal += 1
            continue
        xs.append(float(ordinal))
        ys.append(float(tokens))
        ordinal += 1
    return xs, ys


def _ngram_recurrence(digests: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for size in range(2, 6):
        if len(digests) < size:
            result[str(size)] = 0
            continue
        grams = [tuple(digests[i : i + size]) for i in range(len(digests) - size + 1)]
        counts: dict[tuple[str, ...], int] = {}
        for gram in grams:
            counts[gram] = counts.get(gram, 0) + 1
        result[str(size)] = sum(1 for count in counts.values() if count >= 2)
    return result


def _baseline_tokens(ctx: _RecipeContext) -> tuple[int | None, int | None]:
    baseline: dict[str, Any] = {}
    if ctx.ir and isinstance(ctx.ir.get("baseline_metrics"), dict):
        baseline = ctx.ir["baseline_metrics"]
    outline = ctx.pack.get("global_outline") or {}
    prompt = baseline.get("prompt_tokens", outline.get("prompt_tokens"))
    cached = baseline.get("cached_tokens", outline.get("cached_tokens"))
    prompt_i = int(prompt) if isinstance(prompt, (int, float)) else None
    cached_i = int(cached) if isinstance(cached, (int, float)) else None
    return prompt_i, cached_i


def _run_r7(ctx: _RecipeContext) -> RecipeFinding:
    events = ctx.ir_events or ctx.window_events
    actions = [event for event in events if _is_call_event(event)]
    xs, ys = _prompt_token_points(events)
    blocked_metric = [*LEGACY_BLOCKED_METRIC_NAMES]
    unknown = not actions or len(xs) < 2
    if unknown:
        blocked_metric.extend(["opportunity_unknown", "multi_call_dependent_metrics"])
        metrics = {
            "recipe_loop_index": None,
            "recipe_error_rate": None,
            "recipe_cbv": None,
            "recipe_cache_ratio": None,
            "digest_ngram_recurrence": None,
            "unchanged_read_count": None,
            "dead_branch_ratio": None,
        }
    else:
        digests = [action_digest(event) for event in actions]
        metrics = {
            "recipe_loop_index": round(1 - len(set(digests)) / len(digests), 4),
            "recipe_error_rate": None,
            "recipe_cbv": ols_slope(xs, ys),
            "recipe_cache_ratio": None,
            "digest_ngram_recurrence": _ngram_recurrence(digests),
            "unchanged_read_count": 0,
            "dead_branch_ratio": "unknown",
        }
    return _emit(
        ctx,
        recipe_id="r7",
        disposition="screening_only",
        validity=None,
        class_id=None,
        support_level="e0",
        coverage_gaps=ctx.gaps("opportunity_unknown") if unknown else ctx.gaps(),
        extras={**metrics, "blocked_metric": blocked_metric, "corpus_limits": CORPUS_LIMITS_BLOCK},
    )


def _recompute_finding_id(data: dict[str, Any]) -> RecipeFinding:
    payload = dict(data)
    payload.pop("finding_id", None)
    for key in ("citations", "alternative_explanations", "coverage_gaps"):
        payload[key] = sorted(set(payload.get(key) or []))
    payload["finding_id"] = canonical_json_digest(payload)
    return RecipeFinding.model_validate(payload)


def _apply_precedence(findings: list[RecipeFinding]) -> list[RecipeFinding]:
    order = {
        "infrastructure_failure": 0,
        "verifier_failure": 1,
        "expected_negative_exit": 2,
        "tool_schema_misuse": 3,
        "wrong_target_or_action": 4,
        "false_verification_or_unsupported_terminal_claim": 5,
        "repeated_failure_or_thrashing": 6,
        "context_or_constraint_loss": 7,
        "missed_recovery_opportunity": 8,
        "successful_recovery": 8,
        "appropriate_action": 9,
        "appropriate_abstention": 10,
    }
    candidates = [
        finding
        for finding in findings
        if finding.disposition == "candidate_hold" and finding.class_id
    ]
    if len(candidates) < 2:
        return findings
    winner = min(candidates, key=lambda finding: order.get(finding.class_id or "", 99))
    loser_classes = [
        class_id
        for finding in candidates
        if finding is not winner and (class_id := finding.class_id)
    ]
    output: list[RecipeFinding] = []
    for finding in findings:
        if finding is winner:
            data = finding.model_dump()
            data["alternative_explanations"] = [
                *finding.alternative_explanations,
                *loser_classes,
            ]
            output.append(_recompute_finding_id(data))
            continue
        if finding.class_id is None:
            output.append(finding)
            continue
        pre_id = finding.finding_id
        validity = finding.validity
        if validity in {"supported", "contradicted"}:
            validity = "insufficient_evidence"
        extras = {
            **finding.extras,
            "demoted_by_precedence": True,
            "pre_demotion_finding_id": pre_id,
        }
        data = finding.model_dump()
        data.update(
            {
                "class_id": None,
                "validity": validity,
                "disposition": "candidate_hold",
                "extras": extras,
            }
        )
        try:
            demoted = _recompute_finding_id(data)
        except ValueError:
            extras = {**extras, "reason_alias": "precedence_demoted"}
            data.update(
                {
                    "disposition": "deterministic_abstention",
                    "validity": None,
                    "abstention_reason": "ontology_gap",
                    "extras": extras,
                }
            )
            demoted = _recompute_finding_id(data)
        output.append(demoted)
    return output


__all__ = [
    "ABSTENTION_CODES",
    "CONTRACT_DIGEST",
    "CONTRACT_ID",
    "CORPUS_LIMITS_BLOCK",
    "LEGACY_BLOCKED_METRIC_NAMES",
    "PRODUCER",
    "RecipeFinding",
    "TrialArtifacts",
    "assert_citations_in_pack",
    "classify_terminal_claim",
    "load_trial_artifacts",
    "pack_citation_ids",
    "run_recipes",
]
