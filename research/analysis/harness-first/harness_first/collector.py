from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from evallab.evidence.atif import TrialTrajectoryProjection, project_trial
from evallab.evidence.facts import TrialFact, digest_json, extract_trial_fact
from evallab.explorer import redact_text
from evallab.results import (
    JobRecord,
    TrialRecord,
    duration_seconds,
    load_job,
    load_jobs,
    load_trial,
)
from evallab.schemas import CohortSelector

INFRASTRUCTURE_EXCEPTION_KEYWORDS = (
    "docker",
    "container",
    "sandbox",
    "harbor",
    "runner",
    "daemon",
    "infrastructure",
    "environment",
    "network",
    "connectionrefused",
    "dockertimeouterror",
    "dockerfilebuilderror",
)


def safe_nonnegative_int(name: str, value: Any, issues: list[str]) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        issues.append(f"invalid_type_{name}:{type(value).__name__}")
        return None
    if value < 0:
        issues.append(f"negative_{name}:{value}")
        return None
    return value


def safe_nonnegative_float(name: str, value: Any, issues: list[str]) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        issues.append(f"invalid_type_{name}:{type(value).__name__}")
        return None
    try:
        val = float(value)
    except (ValueError, TypeError):
        issues.append(f"unparseable_{name}:{value!r}")
        return None
    if not math.isfinite(val):
        issues.append(f"non_finite_{name}:{val}")
        return None
    if val < 0.0:
        issues.append(f"negative_{name}:{val}")
        return None
    return val


def safe_finite_float(name: str, value: Any, issues: list[str]) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        issues.append(f"invalid_type_{name}:{type(value).__name__}")
        return None
    try:
        val = float(value)
    except (ValueError, TypeError):
        issues.append(f"unparseable_{name}:{value!r}")
        return None
    if not math.isfinite(val):
        issues.append(f"non_finite_{name}:{val}")
        return None
    return val


def safe_repo_path(repo_root: Path, raw_path: str) -> Path:
    resolved_root = repo_root.resolve()
    candidate = (resolved_root / raw_path).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"Path escapes repository root: {raw_path!r}") from exc
    return candidate


def classify_exception(
    exception_class: str | None,
    exception_message: str | None,
) -> tuple[str, str | None]:
    """Classify an exception into (category, phase).

    Categories: 'infrastructure', 'agent', 'verifier', 'none'
    Phases: 'environment', 'verifier', 'agent', 'infrastructure', None
    """
    if not exception_class and not exception_message:
        return "none", None

    cls_str = (exception_class or "").lower()
    msg_str = (exception_message or "").lower()
    combined = f"{cls_str} {msg_str}"

    if cls_str.startswith("verifier") or cls_str.startswith("reward"):
        return "verifier", "verifier"

    if any(kw in combined for kw in INFRASTRUCTURE_EXCEPTION_KEYWORDS):
        return "infrastructure", "environment"

    if cls_str.startswith("agent") or "model" in cls_str or "api" in cls_str:
        return "agent", "agent"

    if "timeout" in cls_str:
        return "agent", "agent"

    return "infrastructure", "unknown"


@dataclass(frozen=True)
class UsageBreakdown:
    input_tokens: int | None
    output_tokens: int | None
    cache_tokens: int | None
    cost_usd: float | None
    coverage_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_tokens": self.cache_tokens,
            "cost_usd": self.cost_usd,
            "coverage_reason": self.coverage_reason,
        }


@dataclass(frozen=True)
class TrialObservation:
    cohort_label: str
    job_id: str
    trial_id: str
    trial_name: str
    task_name: str | None
    task_digest: str | None
    verifier_digest: str | None
    verifier_identity_kind: str
    verifier_strength: str
    environment_digest: str | None
    model_name: str | None
    agent_name: str | None
    agent_version: str | None
    task_block_id: str | None
    raw_reward: float | None
    effective_reward: float | None
    reward_suppressed: bool
    suppression_reason: str | None
    exception_class: str | None
    exception_message: str | None
    exception_phase: str | None
    exception_category: str
    wall_time_seconds: float | None
    agent_execution_seconds: float | None
    native_aggregate_usage: UsageBreakdown
    root_usage: UsageBreakdown
    worker_usage: UsageBreakdown
    total_usage: UsageBreakdown
    source_path: str
    model_revision: str | None = None
    record_status: str = "valid"
    evidence_kind: str = "unknown"
    issues: list[str] = field(default_factory=list)
    passed: bool | None = None
    role_identities: dict[str, str] = field(default_factory=dict)
    native_source_path: str | None = None
    finished: bool = True
    source_native_accounting: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cohort_label": self.cohort_label,
            "job_id": self.job_id,
            "trial_id": self.trial_id,
            "trial_name": self.trial_name,
            "task_name": self.task_name,
            "task_digest": self.task_digest,
            "verifier_digest": self.verifier_digest,
            "verifier_identity_kind": self.verifier_identity_kind,
            "verifier_strength": self.verifier_strength,
            "environment_digest": self.environment_digest,
            "model_name": self.model_name,
            "model_revision": self.model_revision,
            "agent_name": self.agent_name,
            "agent_version": self.agent_version,
            "task_block_id": self.task_block_id,
            "raw_reward": self.raw_reward,
            "effective_reward": self.effective_reward,
            "reward_suppressed": self.reward_suppressed,
            "suppression_reason": self.suppression_reason,
            "exception_class": self.exception_class,
            "exception_message": self.exception_message,
            "exception_phase": self.exception_phase,
            "exception_category": self.exception_category,
            "wall_time_seconds": self.wall_time_seconds,
            "agent_execution_seconds": self.agent_execution_seconds,
            "native_aggregate_usage": self.native_aggregate_usage.to_dict(),
            "root_usage": self.root_usage.to_dict(),
            "worker_usage": self.worker_usage.to_dict(),
            "total_usage": self.total_usage.to_dict(),
            "source_path": self.source_path,
            "native_source_path": self.native_source_path,
            "record_status": self.record_status,
            "evidence_kind": self.evidence_kind,
            "issues": list(self.issues),
            "passed": self.passed,
            "role_identities": dict(self.role_identities),
            "finished": self.finished,
            "source_native_accounting": dict(self.source_native_accounting),
        }


def _identity(
    value: Any, field_name: str, issues: list[str], *, digest: bool = False
) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or value.strip().lower()
        in {
            "unknown",
            "unavailable",
            "unspecified",
            "not-applicable",
        }
    ):
        issues.append(f"{field_name}:missing_or_invalid_identity")
        return None
    value = value.strip()
    if digest:
        bare = value.removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", bare):
            issues.append(f"{field_name}:invalid_sha256")
            return None
        return "sha256:" + bare.lower()
    return value


def extract_trial_observation(
    job: JobRecord,
    trial: TrialRecord,
    *,
    cohort_label: str,
    reward_name: str,
    repo_root: Path,
    evidence_kind: str = "unknown",
    pass_threshold: float = 1.0,
    budget_exhaustion_is_failure: bool = False,
) -> TrialObservation:
    issues: list[str] = []

    # Canonical IDs
    job_id = job.id
    trial_id = trial.id
    trial_name = trial.name

    result = trial.result
    lock = trial.lock

    # Finished status
    try:
        finished_at = datetime.fromisoformat(str(result.get("finished_at")).replace("Z", "+00:00"))
        finished = finished_at.tzinfo is not None
    except ValueError:
        finished = False
    if not finished:
        issues.append("trial_not_finished")

    # Reuse extract_trial_fact and project_trial
    projection: TrialTrajectoryProjection | None = None
    try:
        projection = project_trial(job, trial)
    except Exception as exc:
        issues.append(f"projection_error:{exc}")

    fact: TrialFact | None = None
    try:
        fact = extract_trial_fact(job, trial, projection=projection)
    except Exception as exc:
        issues.append(f"fact_extraction_error:{exc}")

    # Task identity
    task_name = result.get("task_name")
    if task_name is not None:
        task_name = str(task_name)
    elif fact and fact.task_name:
        task_name = fact.task_name

    task_lock = lock.get("task")
    task_digest = None
    if isinstance(task_lock, dict) and task_lock.get("digest"):
        task_digest = str(task_lock["digest"])
    elif result.get("task_checksum"):
        task_digest = str(result["task_checksum"])
    elif fact and fact.task_digest:
        task_digest = fact.task_digest
    task_digest = _identity(task_digest, "task_digest", issues, digest=True)

    # Verifier identity: strict protection against false equality on empty fallback hashes
    experiment = job.metadata.get("experiment")
    verifier_digest = None
    verifier_identity_kind = "missing"
    verifier_strength = "missing"

    verifier_lock = lock.get("verifier")
    has_verifier_lock = isinstance(verifier_lock, dict) and bool(verifier_lock)

    if isinstance(experiment, dict) and experiment.get("verifier_digest"):
        verifier_digest = str(experiment["verifier_digest"])
        verifier_identity_kind = "explicit_evidence_digest"
        verifier_strength = "explicit_evidence"
    elif has_verifier_lock and verifier_lock.get("digest"):
        verifier_digest = str(verifier_lock["digest"])
        verifier_identity_kind = "explicit_evidence_digest"
        verifier_strength = "explicit_evidence"
    elif task_digest is not None or has_verifier_lock:
        verifier_config = verifier_lock if has_verifier_lock else {}
        derived = digest_json(
            {
                "task_digest": task_digest,
                "verifier": verifier_config,
            }
        )
        verifier_digest = derived
        verifier_identity_kind = "derived_configuration_identity"
        verifier_strength = "derived_configuration"
    else:
        # Both task_digest is None and verifier lock is empty/absent.
        # DO NOT return a pseudo-hash that creates false equivalence across empty trials!
        verifier_digest = None
        verifier_identity_kind = "empty_fallback_unverified"
        verifier_strength = "missing"
    verifier_digest = _identity(verifier_digest, "verifier_digest", issues, digest=True)

    # Environment digest
    env_lock = lock.get("environment") or {}
    environment_digest = digest_json(env_lock) if env_lock else None

    # Agent info
    agent_info = result.get("agent_info")
    if not isinstance(agent_info, dict):
        agent_info = {}
    model_info = agent_info.get("model_info")
    if not isinstance(model_info, dict):
        model_info = {}

    agent_name = agent_info.get("name")
    if agent_name is None and isinstance(lock.get("agent"), dict):
        agent_name = lock["agent"].get("name")
    agent_name = _identity(agent_name, "agent_name", issues)

    agent_version = agent_info.get("version")
    if agent_version is None and isinstance(lock.get("agent"), dict):
        agent_version = lock["agent"].get("version")
    agent_version = _identity(agent_version, "agent_version", issues)

    model_name = model_info.get("name") or model_info.get("model_name")
    if model_name is None and isinstance(lock.get("agent"), dict):
        model_name = lock["agent"].get("model_name")
    model_name = _identity(model_name, "model_name", issues)

    # Model revision: MUST come from native model_info.revision/checkpoint_revision or explicit model lock revision
    model_rev = None
    if isinstance(model_info, dict):
        model_rev = model_info.get("revision") or model_info.get("checkpoint_revision")
    if model_rev is None and isinstance(lock.get("agent"), dict):
        model_rev = lock["agent"].get("model_revision")
    model_revision = _identity(model_rev, "model_revision", issues)
    if model_revision in {"main", "master", "latest", "HEAD"}:
        issues.append("model_revision:floating_ref")
        model_revision = None

    provenance = experiment if isinstance(experiment, dict) else {}
    task_block_id = provenance.get("task_block_id")
    if task_block_id is not None:
        task_block_id = str(task_block_id)
    elif fact and fact.task_block_id:
        task_block_id = fact.task_block_id

    # Rewards: NEVER fallback requested reward_name to primary reward!
    # Explicitly check trial.rewards[reward_name]
    native_verifier = result.get("verifier_result") or {}
    native_rewards = native_verifier.get("rewards", {}) if isinstance(native_verifier, dict) else {}
    raw_reward_unvalidated = native_rewards.get(reward_name, trial.rewards.get(reward_name))
    raw_reward = safe_finite_float("raw_reward", raw_reward_unvalidated, issues)

    # Exceptions
    exc_info = result.get("exception_info")
    exception_class = None
    exception_message = None
    if isinstance(exc_info, dict):
        exception_class = exc_info.get("exception_type")
        exception_message = exc_info.get("exception_message")
    elif exc_info:
        exception_class = str(exc_info)

    if exception_message:
        exception_message = redact_text(str(exception_message))
    if exception_class:
        exception_class = redact_text(str(exception_class))

    exception_category, exception_phase = classify_exception(exception_class, exception_message)

    # Effective Reward & Suppression:
    # Suppress all exception/partial/non-finished effective rewards
    # except explicitly spec-enabled AgentTimeout budget-exhaustion->0
    is_budget_timeout = exception_class == "AgentTimeoutError" and finished

    if is_budget_timeout:
        if budget_exhaustion_is_failure:
            effective_reward = 0.0
            reward_suppressed = False
            suppression_reason = None
        else:
            effective_reward = None
            reward_suppressed = True
            suppression_reason = f"budget_exhaustion_excluded:{exception_class}"
    elif exception_category != "none" or exception_class is not None:
        effective_reward = None
        reward_suppressed = True
        suppression_reason = f"exception:{exception_class or 'unspecified'}"
    elif not finished:
        effective_reward = None
        reward_suppressed = True
        suppression_reason = "trial_not_finished"
    else:
        reward_suppressed = False
        effective_reward = raw_reward
        suppression_reason = None

    # Passed check based on spec.pass_threshold
    passed: bool | None = None
    if effective_reward is not None:
        passed = effective_reward >= pass_threshold

    # Timing: preserved across all attempts, including failures!
    started_at = result.get("started_at")
    finished_at = result.get("finished_at")
    wall_duration = None
    if started_at and finished_at:
        try:
            wall_duration = duration_seconds(str(started_at), str(finished_at))
        except Exception:
            wall_duration = None
    elif fact and fact.duration_seconds is not None:
        wall_duration = fact.duration_seconds
    wall_time_seconds = safe_nonnegative_float("wall_time_seconds", wall_duration, issues)

    agent_timing = result.get("agent_execution")
    agent_duration = None
    if isinstance(agent_timing, dict):
        a_start = agent_timing.get("started_at")
        a_fin = agent_timing.get("finished_at")
        if a_start and a_fin:
            try:
                agent_duration = duration_seconds(str(a_start), str(a_fin))
            except Exception:
                agent_duration = None
    elif fact and fact.agent_seconds is not None:
        agent_duration = fact.agent_seconds
    agent_execution_seconds = safe_nonnegative_float(
        "agent_execution_seconds", agent_duration, issues
    )

    # Usage Accounting: Separate native aggregate, root, worker, and total
    agent_result = result.get("agent_result")
    if isinstance(agent_result, dict):
        nat_in = safe_nonnegative_int(
            "native_input_tokens", agent_result.get("n_input_tokens"), issues
        )
        nat_out = safe_nonnegative_int(
            "native_output_tokens", agent_result.get("n_output_tokens"), issues
        )
        nat_cache = safe_nonnegative_int(
            "native_cache_tokens", agent_result.get("n_cache_tokens"), issues
        )
        nat_cost = safe_nonnegative_float("native_cost_usd", agent_result.get("cost_usd"), issues)
        native_aggregate = UsageBreakdown(
            input_tokens=nat_in,
            output_tokens=nat_out,
            cache_tokens=nat_cache,
            cost_usd=nat_cost,
            coverage_reason="trial_agent_result_unpartitioned",
        )
    else:
        native_aggregate = UsageBreakdown(
            input_tokens=None,
            output_tokens=None,
            cache_tokens=None,
            cost_usd=None,
            coverage_reason="agent_result_absent_in_trial",
        )

    # Disjoint per-generation metrics avoid inclusive summary double counting.
    # These are retained-capture amounts, not verified full episode compute.
    role_identities: dict[str, str] = {}
    missing = UsageBreakdown(None, None, None, None, "role_usage_not_observed")
    root_usage = worker_usage = total_usage = missing
    if projection is not None and projection.trajectories:
        roots = [t for t in projection.trajectories if t.embedded_path is None]
        workers = [t for t in projection.trajectories if t.embedded_path is not None]
        for document in projection.trajectories:
            role_identities[document.document_id] = document.model_name or "unknown_model"

        def step_usage(documents: list[Any], role: str) -> UsageBreakdown:
            if not documents:
                return UsageBreakdown(None, None, None, None, f"{role}_documents_not_observed")
            if any(document.validation_status != "valid" for document in documents):
                issues.append(f"{role}:invalid_or_unsupported_atif")
                return UsageBreakdown(None, None, None, None, "unqualified_atif")
            ids = {document.document_id for document in documents}
            steps = [
                step
                for step in projection.steps
                if step.document_id in ids
                and step.source in {"agent", "assistant"}
                and not step.is_copied_context
            ]
            if not steps:
                return UsageBreakdown(None, None, None, None, "no_retained_generation_steps")
            values: dict[str, Any] = {}
            for output_name, attribute in (
                ("input_tokens", "prompt_tokens"),
                ("output_tokens", "completion_tokens"),
                ("cache_tokens", "cached_tokens"),
                ("cost_usd", "cost_usd"),
            ):
                validate = (
                    safe_nonnegative_float if output_name == "cost_usd" else safe_nonnegative_int
                )
                observations = [
                    validate(f"{role}_{attribute}", getattr(step, attribute), issues)
                    for step in steps
                ]
                values[output_name] = (
                    sum(observations) if all(v is not None for v in observations) else None
                )
            return UsageBreakdown(**values, coverage_reason=f"retained_{role}_step_metrics")

        if len(roots) == 1:
            root_usage = step_usage(roots, "root")
            worker_usage = step_usage(workers, "worker")
            unresolved_workers = not workers and any(
                item.subagent_ref_count for item in projection.observations
            )
            if not unresolved_workers:
                total_usage = step_usage([*roots, *workers], "total")
            else:
                total_usage = UsageBreakdown(
                    None, None, None, None, "worker_references_without_usage"
                )
        else:
            issues.append("ambiguous_root_or_external_trajectory_roles")
            total_usage = UsageBreakdown(None, None, None, None, "ambiguous_trajectory_roles")
    else:
        total_usage = UsageBreakdown(None, None, None, None, "native_aggregate_not_confirmed_total")

    # HAR-12's published source-native record is not ATIF and does not claim
    # worker token coverage. Decode only that explicit format; no fixture proxy.
    source_accounting: dict[str, Any] = {}
    accounting_path = trial.path / "agent" / "rlm" / "root-messages.json"
    if accounting_path.is_file() and not accounting_path.is_symlink():
        safe_repo_path(repo_root, str(accounting_path))
        try:
            payload = json.loads(accounting_path.read_text())
            if (
                payload.get("source_format") != "authors-rlm-root-messages"
                or payload.get("schema_version") is not None
            ):
                raise ValueError("unsupported HAR-12 source accounting format")
            metadata = agent_result.get("metadata", {}) if isinstance(agent_result, dict) else {}
            if not isinstance(metadata, dict):
                metadata = {}
            source_accounting = {
                "source_format": payload["source_format"],
                "source_path": str(accounting_path.relative_to(repo_root)),
                "root_calls": safe_nonnegative_int(
                    "reported_root_calls", payload.get("root_calls"), issues
                ),
                "worker_calls": safe_nonnegative_int(
                    "reported_worker_calls", payload.get("worker_calls"), issues
                ),
                "physical_request_attempts": None,
                "request_coverage": "logical source-reported counts; not a physical attempt ledger",
                "worker_usage": None,
                "worker_model": metadata.get("worker_model"),
                "root_model": metadata.get("root_model"),
                "prompt_source_sha256": payload.get("prompt_source_sha256"),
                "parsing_source_sha256": payload.get("parsing_source_sha256"),
            }
            reported_model = metadata.get("root_model")
            if reported_model is not None and reported_model != model_name:
                issues.append("source_native_root_identity_conflict")
                effective_reward, passed = None, None
                reward_suppressed = True
                suppression_reason = "source_native_root_identity_conflict"
            if metadata.get("atif") is False and reported_model == model_name:
                reported_root = UsageBreakdown(
                    safe_nonnegative_int(
                        "har12_root_input_tokens", metadata.get("root_input_tokens"), issues
                    ),
                    safe_nonnegative_int(
                        "har12_root_output_tokens", metadata.get("root_output_tokens"), issues
                    ),
                    None,
                    None,
                    "har12_reported_root_only_completeness_unknown",
                )
                source_accounting["reported_root_usage"] = reported_root.to_dict()
                if root_usage.input_tokens is None and root_usage.output_tokens is None:
                    root_usage = reported_root
                total_usage = UsageBreakdown(
                    None, None, None, None, "har12_worker_usage_unavailable"
                )
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            issues.append(f"source_native_accounting_unavailable:{exc}")

    native_source_path = trial.path.resolve().as_posix()
    try:
        source_path = trial.path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        source_path = native_source_path

    record_status = "valid" if not issues else "incomplete_metadata"

    return TrialObservation(
        cohort_label=cohort_label,
        job_id=job_id,
        trial_id=trial_id,
        trial_name=trial_name,
        task_name=task_name,
        task_digest=task_digest,
        verifier_digest=verifier_digest,
        verifier_identity_kind=verifier_identity_kind,
        verifier_strength=verifier_strength,
        environment_digest=environment_digest,
        model_name=model_name,
        model_revision=model_revision,
        agent_name=agent_name,
        agent_version=agent_version,
        task_block_id=task_block_id,
        raw_reward=raw_reward,
        effective_reward=effective_reward,
        reward_suppressed=reward_suppressed,
        suppression_reason=suppression_reason,
        exception_class=exception_class,
        exception_message=exception_message,
        exception_phase=exception_phase,
        exception_category=exception_category,
        wall_time_seconds=wall_time_seconds,
        agent_execution_seconds=agent_execution_seconds,
        native_aggregate_usage=native_aggregate,
        root_usage=root_usage,
        worker_usage=worker_usage,
        total_usage=total_usage,
        source_path=source_path,
        native_source_path=native_source_path,
        record_status=record_status,
        evidence_kind=str(job.metadata.get("evidence_kind") or evidence_kind),
        issues=[redact_text(issue) for issue in issues],
        passed=passed,
        role_identities=role_identities,
        source_native_accounting=source_accounting,
        finished=finished,
    )


def _selected_job(path: Path, warnings: list[str]) -> JobRecord:
    """Use completed reader normally; preserve typed partial native records."""
    payload = json.loads((path / "result.json").read_text())
    if payload.get("finished_at"):
        return load_job(path)
    if not payload.get("id") or "n_total_trials" not in payload or "stats" not in payload:
        raise ValueError("Incomplete path lacks a native job identity")
    warnings.append(f"Partial native job: {path}; only existing trial records are observed")
    trials = []
    for directory in sorted(path.iterdir()):
        if (
            directory.is_dir()
            and not directory.is_symlink()
            and (directory / "result.json").is_file()
        ):
            trial = load_trial(directory)
            if all(trial.result.get(key) for key in ("id", "trial_name", "task_name")):
                trials.append(trial)

    def metadata(name: str) -> dict[str, Any]:
        source = path / name
        value = json.loads(source.read_text()) if source.exists() else {}
        if not isinstance(value, dict):
            raise ValueError(f"{source} must contain a JSON object")
        return value

    return JobRecord(
        path,
        payload,
        metadata("config.json"),
        metadata("lock.json"),
        metadata("lab-metadata.json"),
        tuple(trials),
    )


def collect_cohort_trials(
    repo_root: Path,
    selector: CohortSelector,
    reward_name: str,
    evidence_kind: str = "unknown",
    pass_threshold: float = 1.0,
    budget_exhaustion_is_failure: bool = False,
) -> tuple[list[TrialObservation], list[str]]:
    warnings: list[str] = []
    selected: dict[str, TrialObservation] = {}
    identities: dict[str, str] = {}
    requested = set(selector.trial_names)
    for raw_path in selector.paths:
        try:
            path = safe_repo_path(repo_root, raw_path)
            if not path.exists():
                warnings.append(f"{selector.label}: missing selected path {raw_path}")
                continue
            trial_scope = None
            if (path / "result.json").is_file():
                content = json.loads((path / "result.json").read_text())
                if "trial_name" in content and "task_name" in content:
                    trial_scope = {path.name}
                    jobs = [_selected_job(path.parent, warnings)]
                else:
                    jobs = [_selected_job(path, warnings)]
            else:
                jobs = load_jobs([path])
                warnings.append(
                    f"{selector.label}: discovery selects completed jobs only; select partial jobs explicitly"
                )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            warnings.append(redact_text(f"{selector.label}: unavailable input {raw_path}: {exc}"))
            continue
        for job in jobs:
            for trial in job.trials:
                if requested and trial.name not in requested:
                    continue
                if trial_scope is not None and trial.name not in trial_scope:
                    continue
                source = str(trial.path.resolve())
                safe_repo_path(repo_root, source)
                if source in selected:
                    continue
                if trial.id in identities and identities[trial.id] != source:
                    raise ValueError(f"Duplicate trial UUID at distinct source paths: {trial.id}")
                identities[trial.id] = source
                observation = extract_trial_observation(
                    job,
                    trial,
                    cohort_label=selector.label,
                    reward_name=reward_name,
                    repo_root=repo_root,
                    evidence_kind=evidence_kind,
                    pass_threshold=pass_threshold,
                    budget_exhaustion_is_failure=budget_exhaustion_is_failure,
                )
                selected[source] = observation
    if not selected:
        warnings.append(
            f"{selector.label}: no selected trial records; this is not a zero-reward observation"
        )
    return sorted(selected.values(), key=lambda item: (item.trial_id, item.source_path)), warnings
