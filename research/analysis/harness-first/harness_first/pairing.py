from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from evallab.explorer import redact_text
from evallab.schemas import CohortComparisonSpec

from .collector import TrialObservation, canonical_trial_id


@dataclass(frozen=True)
class DeltaMetrics:
    raw_reward_delta: float | None
    effective_reward_delta: float | None
    wall_time_delta_seconds: float | None
    agent_time_delta_seconds: float | None
    root_input_tokens_delta: int | None
    root_output_tokens_delta: int | None
    root_cost_delta_usd: float | None
    worker_input_tokens_delta: int | None
    worker_output_tokens_delta: int | None
    worker_cost_delta_usd: float | None
    total_input_tokens_delta: int | None
    total_output_tokens_delta: int | None
    total_cost_delta_usd: float | None
    native_input_tokens_delta: int | None
    native_output_tokens_delta: int | None
    native_cost_delta_usd: float | None
    classification: str  # 'positive', 'neutral', 'negative', 'unsupported'

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_reward_delta": self.raw_reward_delta,
            "effective_reward_delta": self.effective_reward_delta,
            "wall_time_delta_seconds": self.wall_time_delta_seconds,
            "agent_time_delta_seconds": self.agent_time_delta_seconds,
            "root_input_tokens_delta": self.root_input_tokens_delta,
            "root_output_tokens_delta": self.root_output_tokens_delta,
            "root_cost_delta_usd": self.root_cost_delta_usd,
            "worker_input_tokens_delta": self.worker_input_tokens_delta,
            "worker_output_tokens_delta": self.worker_output_tokens_delta,
            "worker_cost_delta_usd": self.worker_cost_delta_usd,
            "total_input_tokens_delta": self.total_input_tokens_delta,
            "total_output_tokens_delta": self.total_output_tokens_delta,
            "total_cost_delta_usd": self.total_cost_delta_usd,
            "native_input_tokens_delta": self.native_input_tokens_delta,
            "native_output_tokens_delta": self.native_output_tokens_delta,
            "native_cost_delta_usd": self.native_cost_delta_usd,
            "classification": self.classification,
        }


@dataclass(frozen=True)
class PairQualification:
    is_qualified: bool
    disqualification_reasons: list[str]
    task_match: bool
    verifier_match: bool
    model_match: bool
    model_qualification: (
        str  # 'proven_match', 'proven_mismatch', 'unknown_revision', 'unknown_missing'
    )
    agent_version_qualification: dict[str, Any]
    environment_match: bool
    constraints_satisfied: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_qualified": self.is_qualified,
            "disqualification_reasons": list(self.disqualification_reasons),
            "task_match": self.task_match,
            "verifier_match": self.verifier_match,
            "model_match": self.model_match,
            "model_qualification": self.model_qualification,
            "agent_version_qualification": dict(self.agent_version_qualification),
            "environment_match": self.environment_match,
            "constraints_satisfied": self.constraints_satisfied,
        }


@dataclass(frozen=True)
class TaskPairRecord:
    pairing_key: str
    pairing_key_value: str
    pairing_status: str  # 'unambiguous_pair', 'ambiguous_duplicate_attempts', 'missing_candidate', 'missing_baseline'
    qualification: PairQualification
    baseline: TrialObservation | None
    candidate: TrialObservation | None
    baseline_duplicate_attempts: list[TrialObservation]
    candidate_duplicate_attempts: list[TrialObservation]
    delta: DeltaMetrics | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pairing_key": self.pairing_key,
            "pairing_key_value": self.pairing_key_value,
            "pairing_status": self.pairing_status,
            "qualification": self.qualification.to_dict(),
            "baseline": self.baseline.to_dict() if self.baseline else None,
            "candidate": self.candidate.to_dict() if self.candidate else None,
            "baseline_duplicate_attempts": [t.to_dict() for t in self.baseline_duplicate_attempts],
            "candidate_duplicate_attempts": [
                t.to_dict() for t in self.candidate_duplicate_attempts
            ],
            "delta": self.delta.to_dict() if self.delta else None,
        }


def _get_trial_key_value(trial: TrialObservation, pairing_key: str) -> str | None:
    if pairing_key == "task_digest":
        return trial.task_digest
    if pairing_key == "task_name":
        return trial.task_name
    if pairing_key == "task_block_id":
        return trial.task_block_id
    if pairing_key == "trial_name":
        return trial.trial_name
    return getattr(trial, pairing_key, None)


def _delta_int(cand: int | None, base: int | None) -> int | None:
    if cand is not None and base is not None:
        return cand - base
    return None


def _delta_float(cand: float | None, base: float | None) -> float | None:
    if cand is not None and base is not None:
        return cand - base
    return None


def evaluate_pairs(
    spec: CohortComparisonSpec,
    baseline_trials: list[TrialObservation],
    candidate_trials: list[TrialObservation],
) -> list[TaskPairRecord]:
    """Evaluate pairs enforcing invariant task/verifier matches and explicit model/environment checks."""
    pairing_key = spec.pairing_key

    # Group trials by pairing key. Missing keys are never equated!
    baseline_by_key: dict[str, list[TrialObservation]] = {}
    for t in baseline_trials:
        val = _get_trial_key_value(t, pairing_key)
        if not val:
            val = f"__missing_key_baseline_{t.trial_id}__"
        baseline_by_key.setdefault(val, []).append(t)

    candidate_by_key: dict[str, list[TrialObservation]] = {}
    for t in candidate_trials:
        val = _get_trial_key_value(t, pairing_key)
        if not val:
            val = f"__missing_key_candidate_{t.trial_id}__"
        candidate_by_key.setdefault(val, []).append(t)

    all_keys = sorted(set(baseline_by_key.keys()) | set(candidate_by_key.keys()))
    records: list[TaskPairRecord] = []

    for key in all_keys:
        b_list = baseline_by_key.get(key, [])
        c_list = candidate_by_key.get(key, [])

        disqualification_reasons: list[str] = []
        baseline_trial: TrialObservation | None = None
        candidate_trial: TrialObservation | None = None
        b_dups: list[TrialObservation] = []
        c_dups: list[TrialObservation] = []

        # Check for ambiguous duplicate attempts
        # Reject ambiguous repeat groups: no silent zipping, no picking favorable runs
        if len(b_list) > 1 or len(c_list) > 1:
            pairing_status = "ambiguous_duplicate_attempts"
            disqualification_reasons.append("pairing_ambiguity:multiple_attempts_observed")
            b_dups = b_list if len(b_list) > 1 else []
            c_dups = c_list if len(c_list) > 1 else []
            baseline_trial = b_list[0] if b_list else None
            candidate_trial = c_list[0] if c_list else None
            if len(b_list) == 0:
                disqualification_reasons.append("missing_baseline_arm")
            if len(c_list) == 0:
                disqualification_reasons.append("missing_candidate_arm")
        elif len(b_list) == 1 and len(c_list) == 0:
            pairing_status = "missing_candidate"
            disqualification_reasons.append("missing_candidate_arm")
            baseline_trial = b_list[0]
        elif len(b_list) == 0 and len(c_list) == 1:
            pairing_status = "missing_baseline"
            disqualification_reasons.append("missing_baseline_arm")
            candidate_trial = c_list[0]
        else:
            pairing_status = "unambiguous_pair"
            baseline_trial = b_list[0]
            candidate_trial = c_list[0]

        # Qualification evaluation
        task_match = False
        verifier_match = False
        model_match = False
        model_qualification = "unknown_missing"
        agent_version_qualification: dict[str, Any] = {
            "baseline": baseline_trial.agent_version if baseline_trial else None,
            "candidate": candidate_trial.agent_version if candidate_trial else None,
            "status": "unknown",
        }
        environment_match = False
        constraints_satisfied = True

        if (
            baseline_trial is not None
            and candidate_trial is not None
            and pairing_status == "unambiguous_pair"
        ):
            # 1. Same physical trial across arms
            if canonical_trial_id(baseline_trial.trial_id) == canonical_trial_id(
                candidate_trial.trial_id
            ):
                disqualification_reasons.append("same_physical_trial_across_arms")

            # Check for contradictory identity sources in trial issues
            for issue in baseline_trial.issues:
                if issue.startswith("identity_conflict:") or "root_identity_conflict" in issue:
                    disqualification_reasons.append(issue)
                    disqualification_reasons.append(f"baseline_{issue}")
            for issue in candidate_trial.issues:
                if issue.startswith("identity_conflict:") or "root_identity_conflict" in issue:
                    disqualification_reasons.append(issue)
                    disqualification_reasons.append(f"candidate_{issue}")

            has_task_conflict = any(
                i.startswith("identity_conflict:task")
                for i in baseline_trial.issues + candidate_trial.issues
            )
            has_verifier_conflict = any(
                i.startswith("identity_conflict:verifier")
                for i in baseline_trial.issues + candidate_trial.issues
            )
            has_model_name_conflict = any(
                i == "identity_conflict:model_name" or "root_identity_conflict" in i
                for i in baseline_trial.issues + candidate_trial.issues
            )
            has_model_rev_conflict = any(
                i == "identity_conflict:model_revision"
                for i in baseline_trial.issues + candidate_trial.issues
            )

            # 2. Enforce Task equality: invariant regardless of pairing key!
            # Full missing identities never equated
            if has_task_conflict:
                task_match = False
            elif baseline_trial.task_digest is None or candidate_trial.task_digest is None:
                task_match = False
                disqualification_reasons.append("task_identity_missing_unequated")
            elif baseline_trial.task_digest != candidate_trial.task_digest:
                task_match = False
                disqualification_reasons.append("task_digest_mismatch")
            else:
                task_match = True

            # 3. Enforce Verifier equality: invariant regardless of pairing key!
            # Derived verifier digest cannot certify missing task/empty lock
            if has_verifier_conflict:
                verifier_match = False
            elif (
                baseline_trial.verifier_identity_kind == "empty_fallback_unverified"
                or candidate_trial.verifier_identity_kind == "empty_fallback_unverified"
            ):
                verifier_match = False
                disqualification_reasons.append("verifier_empty_fallback_unverified")
            elif baseline_trial.verifier_digest is None or candidate_trial.verifier_digest is None:
                verifier_match = False
                disqualification_reasons.append("verifier_identity_missing_unequated")
            elif baseline_trial.verifier_digest != candidate_trial.verifier_digest:
                verifier_match = False
                disqualification_reasons.append("verifier_digest_mismatch")
            else:
                verifier_match = True

            # 4. Model matching & revision qualification
            b_model = baseline_trial.model_name
            c_model = candidate_trial.model_name
            if has_model_name_conflict:
                model_match = False
                model_qualification = "proven_mismatch"
            elif b_model is None or c_model is None:
                model_match = False
                model_qualification = "unknown_missing"
                disqualification_reasons.append("model_identity_missing_unequated")
            elif b_model != c_model:
                model_match = False
                model_qualification = "proven_mismatch"
                disqualification_reasons.append("model_mismatch")
            else:
                model_match = True
                b_rev = baseline_trial.model_revision
                c_rev = candidate_trial.model_revision
                if has_model_rev_conflict:
                    model_match = False
                    model_qualification = "proven_mismatch"
                elif b_rev is not None and c_rev is not None:
                    if b_rev != c_rev:
                        model_match = False
                        model_qualification = "proven_mismatch"
                        disqualification_reasons.append("model_revision_mismatch")
                    else:
                        model_qualification = "recorded_revision_match"
                else:
                    # Unknown model revision allows explicitly configured-only descriptive comparison
                    model_qualification = "unknown_revision"
            # 5. Agent version qualification
            b_ver = baseline_trial.agent_version
            c_ver = candidate_trial.agent_version
            if b_ver is None or c_ver is None:
                disqualification_reasons.append("harness_version_missing")
                agent_version_qualification = {
                    "baseline": b_ver,
                    "candidate": c_ver,
                    "status": "unknown",
                }
            elif b_ver == c_ver:
                agent_version_qualification = {
                    "baseline": b_ver,
                    "candidate": c_ver,
                    "status": "observed_versions",
                }
            else:
                agent_version_qualification = {
                    "baseline": b_ver,
                    "candidate": c_ver,
                    "status": "observed_distinct_harness_versions",
                }
            if baseline_trial.agent_name is None or candidate_trial.agent_name is None:
                disqualification_reasons.append("harness_identity_missing")

            # 6. Environment equality
            if spec.declared_variable != "environment_digest":
                b_env = baseline_trial.environment_digest
                c_env = candidate_trial.environment_digest
                if b_env is None or c_env is None:
                    environment_match = False
                    disqualification_reasons.append("environment_identity_missing_unequated")
                elif b_env != c_env:
                    environment_match = False
                    disqualification_reasons.append("environment_digest_mismatch")
                else:
                    environment_match = True
            else:
                environment_match = True

            # 7. Constraints verification
            for field_name, expected_val in spec.constraints.items():
                b_val = getattr(baseline_trial, field_name, None)
                c_val = getattr(candidate_trial, field_name, None)
                if b_val != expected_val:
                    constraints_satisfied = False
                    disqualification_reasons.append(
                        redact_text(
                            f"baseline_constraint_violation:{field_name}={b_val!r}_expected_{expected_val!r}"
                        )
                    )
                if c_val != expected_val:
                    constraints_satisfied = False
                    disqualification_reasons.append(
                        redact_text(
                            f"candidate_constraint_violation:{field_name}={c_val!r}_expected_{expected_val!r}"
                        )
                    )

            # 8. Infrastructure exception suppression & reward availability
            if baseline_trial.reward_suppressed:
                disqualification_reasons.append(
                    f"baseline_reward_suppressed:{baseline_trial.suppression_reason}"
                )
            if candidate_trial.reward_suppressed:
                disqualification_reasons.append(
                    f"candidate_reward_suppressed:{candidate_trial.suppression_reason}"
                )

            if baseline_trial.effective_reward is None or candidate_trial.effective_reward is None:
                disqualification_reasons.append("effective_reward_unavailable")

        is_qualified = (len(disqualification_reasons) == 0) and (
            pairing_status == "unambiguous_pair"
        )

        # Delta computation
        delta: DeltaMetrics | None = None
        if is_qualified and baseline_trial is not None and candidate_trial is not None:
            eff_delta = candidate_trial.effective_reward - baseline_trial.effective_reward
            raw_delta = _delta_float(candidate_trial.raw_reward, baseline_trial.raw_reward)
            wall_delta = _delta_float(
                candidate_trial.wall_time_seconds, baseline_trial.wall_time_seconds
            )
            agent_delta = _delta_float(
                candidate_trial.agent_execution_seconds, baseline_trial.agent_execution_seconds
            )

            # Root deltas
            root_in_d = _delta_int(
                candidate_trial.root_usage.input_tokens, baseline_trial.root_usage.input_tokens
            )
            root_out_d = _delta_int(
                candidate_trial.root_usage.output_tokens, baseline_trial.root_usage.output_tokens
            )
            root_cost_d = _delta_float(
                candidate_trial.root_usage.cost_usd, baseline_trial.root_usage.cost_usd
            )

            # Worker deltas
            worker_in_d = _delta_int(
                candidate_trial.worker_usage.input_tokens, baseline_trial.worker_usage.input_tokens
            )
            worker_out_d = _delta_int(
                candidate_trial.worker_usage.output_tokens,
                baseline_trial.worker_usage.output_tokens,
            )
            worker_cost_d = _delta_float(
                candidate_trial.worker_usage.cost_usd, baseline_trial.worker_usage.cost_usd
            )

            # Total deltas
            total_in_d = _delta_int(
                candidate_trial.total_usage.input_tokens, baseline_trial.total_usage.input_tokens
            )
            total_out_d = _delta_int(
                candidate_trial.total_usage.output_tokens, baseline_trial.total_usage.output_tokens
            )
            total_cost_d = _delta_float(
                candidate_trial.total_usage.cost_usd, baseline_trial.total_usage.cost_usd
            )

            # Native deltas
            native_in_d = _delta_int(
                candidate_trial.native_aggregate_usage.input_tokens,
                baseline_trial.native_aggregate_usage.input_tokens,
            )
            native_out_d = _delta_int(
                candidate_trial.native_aggregate_usage.output_tokens,
                baseline_trial.native_aggregate_usage.output_tokens,
            )
            native_cost_d = _delta_float(
                candidate_trial.native_aggregate_usage.cost_usd,
                baseline_trial.native_aggregate_usage.cost_usd,
            )

            if eff_delta > 0:
                classification = "positive"
            elif eff_delta == 0:
                classification = "neutral"
            else:
                classification = "negative"

            delta = DeltaMetrics(
                raw_reward_delta=raw_delta,
                effective_reward_delta=eff_delta,
                wall_time_delta_seconds=wall_delta,
                agent_time_delta_seconds=agent_delta,
                root_input_tokens_delta=root_in_d,
                root_output_tokens_delta=root_out_d,
                root_cost_delta_usd=root_cost_d,
                worker_input_tokens_delta=worker_in_d,
                worker_output_tokens_delta=worker_out_d,
                worker_cost_delta_usd=worker_cost_d,
                total_input_tokens_delta=total_in_d,
                total_output_tokens_delta=total_out_d,
                total_cost_delta_usd=total_cost_d,
                native_input_tokens_delta=native_in_d,
                native_output_tokens_delta=native_out_d,
                native_cost_delta_usd=native_cost_d,
                classification=classification,
            )

        qualification = PairQualification(
            is_qualified=is_qualified,
            disqualification_reasons=sorted(dict.fromkeys(disqualification_reasons)),
            task_match=task_match,
            verifier_match=verifier_match,
            model_match=model_match,
            model_qualification=model_qualification,
            agent_version_qualification=agent_version_qualification,
            environment_match=environment_match,
            constraints_satisfied=constraints_satisfied,
        )

        records.append(
            TaskPairRecord(
                pairing_key=pairing_key,
                pairing_key_value=key,
                pairing_status=pairing_status,
                qualification=qualification,
                baseline=baseline_trial,
                candidate=candidate_trial,
                baseline_duplicate_attempts=b_dups,
                candidate_duplicate_attempts=c_dups,
                delta=delta,
            )
        )

    return sorted(records, key=lambda r: r.pairing_key_value)


def _summarize_metric_coverage(
    values: list[Any], n_total: int, is_float: bool = False
) -> dict[str, Any]:
    valid_vals = [v for v in values if v is not None]
    covered_count = len(valid_vals)
    total = sum(valid_vals) if covered_count == n_total and n_total > 0 else None
    observed_subtotal = sum(valid_vals) if valid_vals else None
    return {
        "total": total,
        "observed_subtotal": observed_subtotal,
        "covered_count": covered_count,
        "n_total": n_total,
    }


def summarize_cohort(
    label: str,
    trials: list[TrialObservation],
    pass_threshold: float = 1.0,
) -> dict[str, Any]:
    """Summarize a cohort retaining compute and timing across all attempts."""
    n_total = len(trials)
    raw_rewards = [t.raw_reward for t in trials if t.raw_reward is not None]
    effective_rewards = [t.effective_reward for t in trials if t.effective_reward is not None]

    # Compute metrics MUST be retained across all attempts, including exceptions!
    wall_times = [t.wall_time_seconds for t in trials if t.wall_time_seconds is not None]
    agent_times = [
        t.agent_execution_seconds for t in trials if t.agent_execution_seconds is not None
    ]

    # Pass count based on spec.pass_threshold
    n_passed = sum(
        1
        for t in trials
        if (
            (t.effective_reward is not None and t.effective_reward >= pass_threshold)
            or (t.effective_reward is None and t.passed is True)
        )
    )
    pass_rate = round(n_passed / n_total, 4) if n_total > 0 else None

    # Native tokens/cost
    native_in = [t.native_aggregate_usage.input_tokens for t in trials]
    native_out = [t.native_aggregate_usage.output_tokens for t in trials]
    native_cost = [t.native_aggregate_usage.cost_usd for t in trials]

    # Root tokens/cost
    root_in = [t.root_usage.input_tokens for t in trials]
    root_out = [t.root_usage.output_tokens for t in trials]
    root_cost = [t.root_usage.cost_usd for t in trials]

    # Worker tokens/cost
    worker_in = [t.worker_usage.input_tokens for t in trials]
    worker_out = [t.worker_usage.output_tokens for t in trials]
    worker_cost = [t.worker_usage.cost_usd for t in trials]

    # Total tokens/cost
    total_in = [t.total_usage.input_tokens for t in trials]
    total_out = [t.total_usage.output_tokens for t in trials]
    total_cost = [t.total_usage.cost_usd for t in trials]

    # Exceptions and suppressions
    exceptions_by_class: dict[str, int] = {}
    exceptions_by_phase: dict[str, int] = {}
    exceptions_by_category: dict[str, int] = {}
    n_suppressed = 0

    for t in trials:
        if t.reward_suppressed:
            n_suppressed += 1
        if t.exception_class:
            exceptions_by_class[t.exception_class] = (
                exceptions_by_class.get(t.exception_class, 0) + 1
            )
        if t.exception_phase:
            exceptions_by_phase[t.exception_phase] = (
                exceptions_by_phase.get(t.exception_phase, 0) + 1
            )
        if t.exception_category != "none":
            exceptions_by_category[t.exception_category] = (
                exceptions_by_category.get(t.exception_category, 0) + 1
            )

    return {
        "cohort_label": label,
        "n_total_attempts": n_total,
        "n_passed": n_passed,
        "pass_rate": pass_rate,
        "pass_threshold": pass_threshold,
        "n_raw_rewards_recorded": len(raw_rewards),
        "n_effective_rewards_recorded": len(effective_rewards),
        "n_reward_suppressed_attempts": n_suppressed,
        "n_suppressed_infrastructure_attempts": sum(
            t.reward_suppressed and t.exception_category in {"infrastructure", "verifier"}
            for t in trials
        ),
        "raw_reward": {
            "mean": round(sum(raw_rewards) / len(raw_rewards), 4) if raw_rewards else None,
            "min": min(raw_rewards) if raw_rewards else None,
            "max": max(raw_rewards) if raw_rewards else None,
        },
        "effective_reward": {
            "mean": round(sum(effective_rewards) / len(effective_rewards), 4)
            if effective_rewards
            else None,
            "min": min(effective_rewards) if effective_rewards else None,
            "max": max(effective_rewards) if effective_rewards else None,
        },
        "compute_and_timing": {
            "total_wall_time_seconds": sum(wall_times)
            if len(wall_times) == n_total and n_total
            else None,
            "observed_wall_time_seconds": sum(wall_times) if wall_times else None,
            "wall_time_covered_count": len(wall_times),
            "mean_wall_time_seconds": round(sum(wall_times) / len(wall_times), 3)
            if wall_times
            else None,
            "total_agent_time_seconds": sum(agent_times)
            if len(agent_times) == n_total and n_total
            else None,
            "observed_agent_time_seconds": sum(agent_times) if agent_times else None,
            "agent_time_covered_count": len(agent_times),
            "mean_agent_time_seconds": round(sum(agent_times) / len(agent_times), 3)
            if agent_times
            else None,
        },
        "native_aggregate_usage": {
            "input_tokens": _summarize_metric_coverage(native_in, n_total, is_float=False),
            "output_tokens": _summarize_metric_coverage(native_out, n_total, is_float=False),
            "cost_usd": _summarize_metric_coverage(native_cost, n_total, is_float=True),
        },
        "root_usage": {
            "input_tokens": _summarize_metric_coverage(root_in, n_total, is_float=False),
            "output_tokens": _summarize_metric_coverage(root_out, n_total, is_float=False),
            "cost_usd": _summarize_metric_coverage(root_cost, n_total, is_float=True),
        },
        "worker_usage": {
            "input_tokens": _summarize_metric_coverage(worker_in, n_total, is_float=False),
            "output_tokens": _summarize_metric_coverage(worker_out, n_total, is_float=False),
            "cost_usd": _summarize_metric_coverage(worker_cost, n_total, is_float=True),
        },
        "total_usage": {
            "input_tokens": _summarize_metric_coverage(total_in, n_total, is_float=False),
            "output_tokens": _summarize_metric_coverage(total_out, n_total, is_float=False),
            "cost_usd": _summarize_metric_coverage(total_cost, n_total, is_float=True),
        },
        "exceptions": {
            "by_class": exceptions_by_class,
            "by_phase": exceptions_by_phase,
            "by_category": exceptions_by_category,
        },
    }
