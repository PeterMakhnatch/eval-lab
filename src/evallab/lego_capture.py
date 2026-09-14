"""Read-only structural assessment of saved LEGO-RL ``proxy_capture`` JSON.

This consumer does not run a proxy, authenticate sampling identity, export a
training dataset, or authorize training. A passing assessment is only the checks
reported below, not original-probability or live-model qualification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal, TypeGuard

RECORD_ORIGINS = (
    "live_trial",
    "cpu_proxy_session_synthetic",
    "actual_source_extraction_smoke",
    "diagnostic_recompute",
)
LIMITS = {
    "identity": "Authenticate model, tokenizer, policy/checkpoint and sampled weight identity; "
    "session IDs and step numbers here are unverified source claims, not identity proofs.",
    "probabilities": "Bind original generation-time probabilities to tokens and sampling settings; "
    "numeric validity alone cannot prove authenticity or exclude undeclared recomputation.",
    "actor": "Verify actor/task/trajectory lineage, collection authorization and contamination controls.",
    "license": "Verify source and derivative-data licenses and permitted training use.",
    "admission": "Obtain explicit dataset/task admission, execution approval and training authorization.",
    "routing": "Only row alignment is checked when supplied; expert/model routing semantics remain unverified.",
}


@dataclass(frozen=True)
class Issue:
    code: str
    field: str
    count: int = 1
    indices: tuple[int, ...] = ()


@dataclass(frozen=True)
class WeightAssessment:
    span_status: Literal["missing", "malformed", "reversed", "complete"]
    min_global_steps: int | None
    max_global_steps: int | None
    policy_result: Literal["not_requested", "passed", "rejected"]
    learner_step: int | None
    max_weight_lag: int | None
    oldest_weight_lag: int | None
    issues: tuple[Issue, ...]


@dataclass(frozen=True)
class StructureAssessment:
    valid: bool
    prompt_tokens: int | None
    response_tokens: int | None
    trained_response_tokens: int | None
    excluded_response_tokens: int | None
    routing_present: bool
    routing_rows: int | None
    issues: tuple[Issue, ...]


@dataclass(frozen=True)
class DeclaredProvenance:
    cli_record_origin: str | None
    source_record_origin: str | None
    verified: bool = False


@dataclass(frozen=True)
class CaptureAssessment:
    source: str
    source_sha256: str | None
    session_id: str | None
    declared_provenance: DeclaredProvenance
    structure: StructureAssessment | None
    weight: WeightAssessment | None
    assessment_passed: bool
    issues: tuple[Issue, ...]
    training_authorization: Literal["not_assessed"] = "not_assessed"
    context_overflow: bool | None = None
    limits: dict[str, str] = field(default_factory=lambda: dict(LIMITS))


def _nonnegative_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _finite_number(value: object) -> TypeGuard[int | float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _valid_logprob(value: object) -> bool:
    return _finite_number(value) and value <= 0


def _indexed_issue(code: str, name: str, invalid: list[int]) -> Issue:
    return Issue(code, name, len(invalid), tuple(invalid[:8]))


def _weight_assessment(
    record: dict[str, object], learner_step: int | None, max_weight_lag: int | None
) -> WeightAssessment:
    minimum, maximum = record.get("min_global_steps"), record.get("max_global_steps")
    issues: list[Issue] = []
    status: Literal["missing", "malformed", "reversed", "complete"]
    if minimum is None and maximum is None:
        status = "missing"
    elif not _nonnegative_int(minimum) or not _nonnegative_int(maximum):
        status = "malformed"
        issues.append(Issue("weight_span_malformed", "min_global_steps,max_global_steps"))
    elif minimum > maximum:
        status = "reversed"
        issues.append(Issue("weight_span_reversed", "min_global_steps,max_global_steps"))
    else:
        status = "complete"
    oldest_lag = None
    policy: Literal["not_requested", "passed", "rejected"] = "not_requested"
    if learner_step is not None and max_weight_lag is not None:
        if status == "missing":
            issues.append(Issue("weight_span_missing", "min_global_steps,max_global_steps"))
        if status == "complete" and _nonnegative_int(minimum) and _nonnegative_int(maximum):
            oldest_lag = learner_step - minimum
            if maximum > learner_step:
                issues.append(Issue("weight_span_future", "max_global_steps"))
            if oldest_lag > max_weight_lag:
                issues.append(Issue("weight_span_stale", "min_global_steps"))
        policy = "rejected" if issues else "passed"
    return WeightAssessment(
        status,
        minimum if _nonnegative_int(minimum) else None,
        maximum if _nonnegative_int(maximum) else None,
        policy,
        learner_step,
        max_weight_lag,
        oldest_lag,
        tuple(issues),
    )


def _structure(record: dict[str, object]) -> StructureAssessment:
    issues: list[Issue] = []
    acc = record.get("traj_acc_ids")
    mask = record.get("traj_response_mask")
    logprobs = record.get("traj_response_logprobs")
    prompt = record.get("initial_prompt_token_len")
    response = None
    trained = excluded = None
    if not isinstance(acc, list):
        issues.append(Issue("token_ids_required", "traj_acc_ids"))
    else:
        invalid = [i for i, value in enumerate(acc) if not _nonnegative_int(value)]
        if invalid:
            issues.append(_indexed_issue("token_id_invalid", "traj_acc_ids", invalid))
    if not _nonnegative_int(prompt):
        issues.append(Issue("prompt_length_invalid", "initial_prompt_token_len"))
    elif isinstance(acc, list):
        response = len(acc) - prompt
        if prompt == 0:
            issues.append(Issue("prompt_empty", "initial_prompt_token_len"))
        if response < 0:
            issues.append(Issue("prompt_length_exceeds_tokens", "initial_prompt_token_len"))
        elif response == 0:
            issues.append(Issue("response_empty", "traj_acc_ids"))
    if not isinstance(mask, list):
        issues.append(Issue("response_mask_required", "traj_response_mask"))
    else:
        invalid = [
            i for i, value in enumerate(mask) if not _nonnegative_int(value) or value not in (0, 1)
        ]
        if invalid:
            issues.append(_indexed_issue("response_mask_nonbinary", "traj_response_mask", invalid))
        else:
            trained = sum(mask)
            excluded = len(mask) - trained
            if trained == 0:
                issues.append(Issue("no_trained_response_tokens", "traj_response_mask"))
        if response is not None and len(mask) != response:
            issues.append(Issue("response_split_mismatch", "traj_response_mask"))
    if not isinstance(logprobs, list):
        issues.append(Issue("response_logprobs_required", "traj_response_logprobs"))
    elif isinstance(mask, list):
        if len(logprobs) != len(mask):
            issues.append(Issue("logprob_alignment_mismatch", "traj_response_logprobs"))
        invalid = [
            i
            for i, value in enumerate(mask)
            if _nonnegative_int(value)
            and value == 1
            and (i >= len(logprobs) or not _valid_logprob(logprobs[i]))
        ]
        if invalid:
            issues.append(
                _indexed_issue("trained_logprob_invalid", "traj_response_logprobs", invalid)
            )
        invalid_excluded = [
            i
            for i, value in enumerate(mask)
            if _nonnegative_int(value)
            and value == 0
            and (i >= len(logprobs) or not _finite_number(logprobs[i]))
        ]
        if invalid_excluded:
            issues.append(
                _indexed_issue(
                    "excluded_logprob_invalid", "traj_response_logprobs", invalid_excluded
                )
            )
    routing = record.get("traj_response_routing")
    routing_present = False
    routing_rows = None
    if routing is not None:
        if not isinstance(routing, list):
            issues.append(Issue("routing_malformed", "traj_response_routing"))
        else:
            routing_rows = len(routing)
            routing_present = any(row is not None for row in routing)
            if routing and (not isinstance(mask, list) or len(routing) != len(mask)):
                issues.append(Issue("routing_alignment_mismatch", "traj_response_routing"))
    return StructureAssessment(
        not issues,
        prompt if _nonnegative_int(prompt) else None,
        response,
        trained,
        excluded,
        routing_present,
        routing_rows,
        tuple(issues),
    )


def _validate_options(
    record_origin: str | None, learner_step: int | None, max_weight_lag: int | None
) -> None:
    if record_origin is not None and record_origin not in RECORD_ORIGINS:
        raise ValueError("record_origin must be a supported unverified declaration")
    if (learner_step is None) != (max_weight_lag is None):
        raise ValueError("learner_step and max_weight_lag must be supplied together")
    if learner_step is not None and (
        not _nonnegative_int(learner_step) or not _nonnegative_int(max_weight_lag)
    ):
        raise ValueError(
            "learner_step and max_weight_lag must be nonnegative integers, not booleans"
        )


def _assess_record(
    record: dict[str, object],
    source: str,
    sha256: str,
    record_origin: str | None,
    learner_step: int | None,
    max_weight_lag: int | None,
) -> CaptureAssessment:
    issues: list[Issue] = []
    session = record.get("session_id")
    if not isinstance(session, str) or not session.strip() or len(session) > 256:
        issues.append(Issue("session_id_invalid", "session_id"))
        session = None
    source_origin = record.get("record_origin")
    if "record_origin" in record and (
        not isinstance(source_origin, str) or source_origin not in RECORD_ORIGINS
    ):
        issues.append(Issue("record_origin_invalid", "record_origin"))
        source_origin = None
    if record_origin is not None and source_origin is not None and record_origin != source_origin:
        issues.append(Issue("record_origin_conflict", "record_origin"))
    for name in ("disable_proxy_trajectory", "context_overflow"):
        if not isinstance(record.get(name), bool):
            issues.append(Issue("required_boolean_invalid", name))
        elif name == "disable_proxy_trajectory" and record[name] is True:
            issues.append(Issue("proxy_trajectory_disabled", name))
    error = record.get("trajectory_logprobs_error")
    if "trajectory_logprobs_error" not in record or (
        error is not None and (not isinstance(error, str) or not error)
    ):
        issues.append(Issue("capture_error_field_invalid", "trajectory_logprobs_error"))
    elif error is not None:
        issues.append(Issue("capture_logprobs_error", "trajectory_logprobs_error"))
    diagnostic = record.get("diagnostic_logprobs_complete")
    if "diagnostic_logprobs_complete" not in record or (
        diagnostic is not None and not isinstance(diagnostic, bool)
    ):
        issues.append(Issue("diagnostic_flag_invalid", "diagnostic_logprobs_complete"))
    elif diagnostic is not None:
        # False records an attempted diagnostic; it cannot certify original scores.
        issues.append(Issue("diagnostic_recomputation", "diagnostic_logprobs_complete"))
    if "diagnostic_recompute" in (record_origin, source_origin):
        issues.append(Issue("diagnostic_recomputation", "record_origin"))
    for name in ("sampling_lineage", "sampling_fidelity"):
        if name not in record:
            continue
        label = record[name]
        if not isinstance(label, str) or not label:
            issues.append(Issue("provenance_label_invalid", name))
        elif label in ("diagnostic_recompute", "diagnostic_recomputation", "diagnostic_only"):
            issues.append(Issue("diagnostic_recomputation", name))
    structure = _structure(record)
    weight = _weight_assessment(record, learner_step, max_weight_lag)
    context_overflow = record.get("context_overflow")
    return CaptureAssessment(
        source,
        sha256,
        session if isinstance(session, str) else None,
        DeclaredProvenance(
            record_origin, source_origin if isinstance(source_origin, str) else None
        ),
        structure,
        weight,
        not issues and structure.valid and not weight.issues,
        tuple(issues),
        context_overflow=context_overflow if isinstance(context_overflow, bool) else None,
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    record: dict[str, object] = {}
    for key, value in pairs:
        if key in record:
            raise ValueError("duplicate JSON key")
        record[key] = value
    return record


def assess_file(
    path: str | Path,
    *,
    record_origin: str | None = None,
    learner_step: int | None = None,
    max_weight_lag: int | None = None,
) -> CaptureAssessment:
    """Assess original file bytes; input failures become bounded per-file issues.

    Invalid caller options raise ValueError. No output path or data-store writes
    are supported. The optional weight policy uses the oldest *declared* sampled
    step, never a dispatch step or a fallback default.
    """
    _validate_options(record_origin, learner_step, max_weight_lag)
    source = str(path)
    digest = None
    try:
        data = Path(path).read_bytes()
    except OSError:
        code = "source_read_error"
    else:
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        try:
            record = json.loads(data, object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError, RecursionError):
            code = "source_json_invalid"
        else:
            if isinstance(record, dict):
                return _assess_record(
                    record, source, digest, record_origin, learner_step, max_weight_lag
                )
            code = "capture_object_required"
    return CaptureAssessment(
        source,
        digest,
        None,
        DeclaredProvenance(record_origin, None),
        None,
        None,
        False,
        (Issue(code, "source"),),
    )


def main(argv: list[str] | None = None) -> int:
    """Emit one JSON report; exit 1 for any rejected input, 2 for CLI misuse."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument(
        "--record-origin",
        choices=RECORD_ORIGINS,
        help="unverified declaration for all inputs; live_trial does not authenticate a model",
    )
    parser.add_argument("--learner-step", type=int)
    parser.add_argument("--max-weight-lag", type=int)
    args = parser.parse_args(argv)
    try:
        _validate_options(args.record_origin, args.learner_step, args.max_weight_lag)
    except ValueError as exc:
        parser.error(str(exc))
    assessments = [
        assess_file(
            path,
            record_origin=args.record_origin,
            learner_step=args.learner_step,
            max_weight_lag=args.max_weight_lag,
        )
        for path in args.inputs
    ]
    passed = sum(item.assessment_passed for item in assessments)
    print(
        json.dumps(
            {
                "schema_version": 1,
                "assessments": [asdict(item) for item in assessments],
                "summary": {
                    "files": len(assessments),
                    "passed": passed,
                    "rejected": len(assessments) - passed,
                    "structurally_valid": sum(
                        item.structure is not None and item.structure.valid for item in assessments
                    ),
                    "weight_policy_passed": sum(
                        item.weight is not None and item.weight.policy_result == "passed"
                        for item in assessments
                    ),
                    "weight_policy_rejected": sum(
                        item.weight is not None and item.weight.policy_result == "rejected"
                        for item in assessments
                    ),
                },
            },
            indent=2,
            allow_nan=False,
        )
    )
    return 0 if passed == len(assessments) else 1


if __name__ == "__main__":
    raise SystemExit(main())
