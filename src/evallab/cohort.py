from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from evallab.facts import TrialFact, digest_json, extract_trial_fact
from evallab.results import JobRecord, TrialRecord, load_job, load_jobs
from evallab.schemas import CohortComparisonSpec, CohortSelector

CONSEQUENTIAL_FIELDS = (
    "task_digest",
    "verifier_digest",
    "environment_digest",
    "agent_name",
    "agent_version",
    "model_name",
    "model_settings_digest",
    "preamble_hash",
    "preamble_content_sha256",
    "toolset_digest",
    "factor_values_digest",
    "factor_bindings_digest",
    "bound_execution_values_digest",
)

BOOTSTRAP_RESAMPLES = 4_000
NOT_COMPARABLE = "not distinguishable / not comparable"


@dataclass(frozen=True)
class CohortMember:
    cohort: str
    experiment_id: str
    job_id: str
    trial_id: str
    source_path: str
    trial_name: str
    task_name: str | None
    task_digest: str | None
    verifier_digest: str
    environment_digest: str
    grid_id: str | None
    point_id: str | None
    arm_id: str | None
    factor_values_json: str | None
    factor_values_digest: str | None
    factor_bindings_json: str | None
    factor_bindings_digest: str | None
    bound_execution_values_json: str | None
    bound_execution_values_digest: str | None
    preamble_path: str | None
    preamble_content_sha256: str | None
    task_family: str | None
    task_id: str | None
    task_instance_id: str | None
    generator_seed_json: str | None
    task_block_inputs_json: str | None
    task_block_id: str | None
    agent_name: str | None
    agent_version: str | None
    model_name: str | None
    model_settings_digest: str
    preamble_hash: str | None
    toolset: dict[str, Any] | None
    toolset_digest: str | None
    reward: float | None
    exception_class: str | None
    duration_seconds: float | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    tool_call_count: int

    def condition(self, field: str) -> str | None:
        value = getattr(self, field)
        return str(value) if value is not None else None


def load_spec(path: Path) -> CohortComparisonSpec:
    try:
        return CohortComparisonSpec.model_validate_json(path.read_text())
    except (OSError, ValidationError) as exc:
        raise ValueError(f"Invalid cohort comparison spec {path}: {exc}") from exc


def wilson_interval(
    successes: int,
    denominator: int,
    z: float = 1.959963984540054,
) -> tuple[float, float] | None:
    if denominator == 0:
        return None
    proportion = successes / denominator
    z_squared = z * z
    scale = 1 + z_squared / denominator
    center = (proportion + z_squared / (2 * denominator)) / scale
    half_width = (
        z
        * math.sqrt(
            proportion * (1 - proportion) / denominator
            + z_squared / (4 * denominator * denominator)
        )
        / scale
    )
    lower = max(0.0, center - half_width)
    upper = min(1.0, center + half_width)
    return lower, upper


def _safe_path(root: Path, value: str) -> Path:
    path = (root / value).resolve()
    resolved_root = root.resolve()
    if path != resolved_root and resolved_root not in path.parents:
        raise ValueError(f"cohort path escapes repository: {value}")
    return path


def _selected_trials(root: Path, selector: CohortSelector) -> list[tuple[JobRecord, TrialRecord]]:
    selected: dict[str, tuple[JobRecord, TrialRecord]] = {}
    requested_names = set(selector.trial_names)
    for raw_path in selector.paths:
        path = _safe_path(root, raw_path)
        if (path / "result.json").is_file():
            value = json.loads((path / "result.json").read_text())
            if isinstance(value, dict) and "trial_name" in value and "task_name" in value:
                job = load_job(path.parent)
                jobs = [job]
                requested_names.add(path.name)
            else:
                jobs = [load_job(path)]
        else:
            jobs = load_jobs([path])
        for job in jobs:
            for trial in job.trials:
                if requested_names and trial.name not in requested_names:
                    continue
                selected[trial.id] = (job, trial)
    if not selected:
        raise ValueError(f"cohort {selector.label!r} selected no completed trials")
    return [selected[key] for key in sorted(selected)]


def _json_object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _named_items(value: Any) -> list[str]:
    if isinstance(value, dict):
        return sorted(str(key) for key in value)
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            name = item.get("name") or item.get("id") or item.get("type")
            names.append(str(name) if name is not None else digest_json(item))
        else:
            names.append(str(item))
    return sorted(names)


def _configured_toolset(
    agent_name: str | None,
    agent_lock: dict[str, Any],
    trial_lock: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if not agent_name:
        return None, None
    kwargs = _json_object(agent_lock.get("kwargs"))
    tool_overrides = sorted(
        str(key)
        for key in kwargs
        if "tool" in str(key).lower() or "command" in str(key).lower()
    )
    toolset = {
        "profile": f"{agent_name}-default",
        "skills": _named_items(agent_lock.get("skills") or trial_lock.get("skills")),
        "mcp_servers": _named_items(agent_lock.get("mcp_servers")),
        "tool_override_keys": tool_overrides,
    }
    return toolset, digest_json(toolset)


def _preamble_entries(config: dict[str, Any]) -> list[tuple[str, Any]]:
    entries: list[tuple[str, Any]] = []
    for key in ("preamble", "system_prompt", "extra_instructions"):
        value = config.get(key)
        if value not in (None, "", []):
            entries.append((key, value))
    paths = config.get("extra_instruction_paths")
    if isinstance(paths, list):
        entries.extend(("path", value) for value in paths)
    agent = _json_object(config.get("agent"))
    kwargs = _json_object(agent.get("kwargs"))
    for key in ("preamble", "system_prompt", "extra_instructions"):
        value = kwargs.get(key)
        if value not in (None, "", []):
            entries.append((key, value))
    return entries


def _configured_preamble_hash(root: Path, trial: TrialRecord) -> str | None:
    result_config = _json_object(trial.result.get("config"))
    sources = (trial.lock, result_config, trial.config)
    entries: list[tuple[str, Any]] = []
    for source in sources:
        entries = _preamble_entries(source)
        if entries:
            break
    if not entries:
        return digest_json({"preamble": "none"})

    normalized: list[dict[str, Any]] = []
    for kind, value in entries:
        if kind != "path":
            normalized.append({"kind": kind, "sha256": digest_json(value)})
            continue
        if not isinstance(value, str):
            return None
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        resolved_root = root.resolve()
        if resolved_root not in candidate.parents or not candidate.is_file():
            return None
        normalized.append(
            {
                "kind": "path",
                "path": candidate.relative_to(resolved_root).as_posix(),
                "sha256": f"sha256:{hashlib.sha256(candidate.read_bytes()).hexdigest()}",
            }
        )
    return digest_json(normalized)


def _member(
    root: Path,
    experiment_id: str,
    label: str,
    job: JobRecord,
    trial: TrialRecord,
    reward_name: str,
) -> CohortMember:
    fact: TrialFact = extract_trial_fact(job, trial)
    agent_lock = _json_object(trial.lock.get("agent"))
    model_settings = {
        key: value
        for key, value in agent_lock.items()
        if key not in {"name", "model_name", "import_path"}
    }
    agent_name = fact.agent_name
    agent_version = fact.agent_version
    if agent_version is None and agent_lock.get("version") is not None:
        agent_version = str(agent_lock["version"])
    model_name = fact.model_name
    if model_name is None and agent_lock.get("model_name") is not None:
        model_name = str(agent_lock["model_name"])
    if model_name is None and agent_name in {"oracle", "nop"}:
        model_name = "not-applicable"
    toolset, toolset_digest = _configured_toolset(agent_name, agent_lock, trial.lock)
    try:
        source_path = trial.path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        source_path = trial.path.resolve().as_posix()
    return CohortMember(
        cohort=label,
        experiment_id=experiment_id,
        job_id=job.id,
        trial_id=trial.id,
        source_path=source_path,
        trial_name=trial.name,
        task_name=fact.task_name,
        task_digest=fact.task_digest,
        verifier_digest=fact.verifier_digest,
        environment_digest=fact.environment_digest,
        grid_id=fact.grid_id,
        point_id=fact.point_id,
        arm_id=fact.arm_id,
        factor_values_json=fact.factor_values_json,
        factor_values_digest=fact.factor_values_digest,
        factor_bindings_json=fact.factor_bindings_json,
        factor_bindings_digest=fact.factor_bindings_digest,
        bound_execution_values_json=fact.bound_execution_values_json,
        bound_execution_values_digest=fact.bound_execution_values_digest,
        preamble_path=fact.preamble_path,
        preamble_content_sha256=fact.preamble_content_sha256,
        task_family=fact.task_family,
        task_id=fact.task_id,
        task_instance_id=fact.task_instance_id,
        generator_seed_json=fact.generator_seed_json,
        task_block_inputs_json=fact.task_block_inputs_json,
        task_block_id=fact.task_block_id,
        agent_name=agent_name,
        agent_version=agent_version,
        model_name=model_name,
        model_settings_digest=digest_json(model_settings),
        preamble_hash=_configured_preamble_hash(root, trial),
        toolset=toolset,
        toolset_digest=toolset_digest,
        reward=trial.rewards.get(reward_name),
        exception_class=fact.exception_class,
        duration_seconds=fact.duration_seconds,
        input_tokens=fact.input_tokens,
        output_tokens=fact.output_tokens,
        cost_usd=fact.cost_usd,
        tool_call_count=fact.tool_call_count,
    )


def assemble_members(root: Path, spec: CohortComparisonSpec) -> list[CohortMember]:
    members: list[CohortMember] = []
    owner_by_trial: dict[str, str] = {}
    for selector in spec.cohorts:
        for job, trial in _selected_trials(root, selector):
            previous = owner_by_trial.get(trial.id)
            if previous is not None and previous != selector.label:
                raise ValueError(
                    f"trial {trial.id} belongs to both {previous!r} and {selector.label!r}"
                )
            owner_by_trial[trial.id] = selector.label
            members.append(
                _member(
                    root,
                    spec.experiment_id,
                    selector.label,
                    job,
                    trial,
                    spec.reward_name,
                )
            )
    return sorted(members, key=lambda item: (item.cohort, item.task_digest or "", item.trial_id))


def _validate_comparability(
    spec: CohortComparisonSpec, members: list[CohortMember]
) -> list[str]:
    observed = {
        field: sorted(
            {member.condition(field) for member in members},
            key=lambda value: "" if value is None else value,
        )
        for field in CONSEQUENTIAL_FIELDS
    }
    treatment_fields = {
        "agent_name",
        "agent_version",
        "model_name",
        "model_settings_digest",
        "environment_digest",
        "preamble_hash",
        "toolset_digest",
        "factor_values_digest",
        "bound_execution_values_digest",
        "factor_bindings_digest",
        "preamble_content_sha256",
    }
    differing_fields = [
        field for field in treatment_fields if len(observed[field]) > 1
    ]
    warnings: list[str] = []
    if spec.declared_variable in {
        "factor_values_digest",
        "bound_execution_values_digest",
    }:
        required = (
            "factor_values_digest",
            "factor_bindings_digest",
            "bound_execution_values_digest",
        )
        for field in required:
            if any(member.condition(field) is None for member in members):
                warnings.append(
                    f"controlled factor provenance is missing {field!r}"
                )
    if (
        spec.declared_variable in {"preamble_hash", "preamble_content_sha256"}
        and any(
            member.preamble_path is not None
            and member.preamble_content_sha256 is None
            for member in members
        )
    ):
        warnings.append("controlled preamble provenance is missing content sha256")
    for field, expected in spec.constraints.items():
        actual = set(observed[field])
        if actual != {expected}:
            observed_values = sorted(str(value) for value in actual)
            warnings.append(
                f"constraint {field}={expected!r} does not match observed {observed_values}"
            )
    allowed_differences = {spec.declared_variable}
    if spec.declared_variable == "agent_name":
        allowed_differences.update(
            {"agent_version", "model_name", "model_settings_digest", "toolset_digest"}
        )
    elif spec.declared_variable == "model_name":
        allowed_differences.add("model_settings_digest")
    elif spec.declared_variable == "factor_values_digest":
        allowed_differences.add("bound_execution_values_digest")
    elif spec.declared_variable == "bound_execution_values_digest":
        allowed_differences.add("factor_values_digest")
    elif spec.declared_variable == "preamble_hash":
        allowed_differences.add("preamble_content_sha256")
    elif spec.declared_variable == "preamble_content_sha256":
        allowed_differences.add("preamble_hash")
    undeclared = [field for field in differing_fields if field not in allowed_differences]
    if spec.declared_variable not in differing_fields:
        warnings.append(f"declared variable {spec.declared_variable!r} does not differ")
    warnings.extend(
        f"undeclared consequential variable differs: {field} ({observed[field]})"
        for field in undeclared
    )
    by_task: dict[str, list[CohortMember]] = defaultdict(list)
    for member in members:
        key = _pairing_value(member, spec.pairing_key)
        if key is not None:
            by_task[key].append(member)
    invariants = {"task_digest", "verifier_digest"}
    if spec.declared_variable != "environment_digest":
        invariants.add("environment_digest")
    for task_key, task_members in sorted(by_task.items()):
        for field in sorted(invariants):
            values = sorted(
                {member.condition(field) for member in task_members},
                key=lambda value: "" if value is None else value,
            )
            if len(values) > 1:
                warnings.append(
                    f"causal invariant differs within task {task_key}: {field} ({values})"
                )
    return list(dict.fromkeys(warnings))


def _numeric_summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"n": 0, "min": None, "median": None, "mean": None, "max": None}
    return {
        "n": len(values),
        "min": min(values),
        "median": statistics.median(values),
        "mean": statistics.fmean(values),
        "max": max(values),
    }


def _scored_reward(member: CohortMember) -> float:
    if member.reward is None:
        raise ValueError(f"trial {member.trial_id} has no scored reward")
    return float(member.reward)


def _pairing_value(member: CohortMember, pairing_key: str) -> str | None:
    value = getattr(member, pairing_key)
    return str(value) if value is not None else None


def _quantile(values: list[float], probability: float) -> float:
    if not values:
        raise ValueError("cannot take a quantile of no values")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def bootstrap_mean_interval(
    values: list[float],
    *,
    confidence: float = 0.95,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = 0,
) -> tuple[float, float] | None:
    """Percentile interval that resamples the supplied evidence units."""
    if not values:
        return None
    if not 0 < confidence < 1:
        raise ValueError("confidence must be between zero and one")
    if resamples < 1:
        raise ValueError("resamples must be positive")
    if len(values) == 1:
        return values[0], values[0]
    generator = random.Random(seed)
    count = len(values)
    means = [
        statistics.fmean(values[generator.randrange(count)] for _ in range(count))
        for _ in range(resamples)
    ]
    tail = (1 - confidence) / 2
    return _quantile(means, tail), _quantile(means, 1 - tail)


def _bootstrap_seed(*parts: object) -> int:
    payload = "\0".join(str(part) for part in parts).encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def pass_at_k_probability(attempt_probability: float, k: int) -> float:
    if not 0 <= attempt_probability <= 1:
        raise ValueError("attempt probability must be between zero and one")
    if k < 1:
        raise ValueError("k must be positive")
    return 1 - (1 - attempt_probability) ** k


def _normal_cutoff(alpha: float, target_power: float) -> float:
    if not 0 < alpha < 1:
        raise ValueError("alpha must be between zero and one")
    if not 0 < target_power < 1:
        raise ValueError("power must be between zero and one")
    normal = statistics.NormalDist()
    return normal.inv_cdf(1 - alpha / 2) + normal.inv_cdf(target_power)


def _paired_variance(p0: float, p1: float, correlation: float) -> float:
    if not -0.99 <= correlation <= 0.99:
        raise ValueError("pair correlation must be between -0.99 and 0.99")
    covariance = correlation * math.sqrt(p0 * (1 - p0) * p1 * (1 - p1))
    return max(p0 * (1 - p0) + p1 * (1 - p1) - 2 * covariance, 0.0)


def required_tasks_for_effect(
    *,
    baseline: float,
    attempt_effect: float,
    k: int,
    alpha: float = 0.05,
    target_power: float = 0.8,
    pair_correlation: float = 0.0,
) -> int | None:
    if attempt_effect <= 0 or baseline + attempt_effect > 1:
        raise ValueError("target effect must be positive and keep probability at or below one")
    p0 = pass_at_k_probability(baseline, k)
    p1 = pass_at_k_probability(baseline + attempt_effect, k)
    task_effect = p1 - p0
    if task_effect <= 0:
        return None
    variance = _paired_variance(p0, p1, pair_correlation)
    required = (_normal_cutoff(alpha, target_power) ** 2) * variance / (task_effect**2)
    return max(2, math.ceil(required))


def minimum_detectable_effect(
    *,
    n_tasks: int,
    k: int,
    baseline: float,
    alpha: float = 0.05,
    target_power: float = 0.8,
    pair_correlation: float = 0.0,
) -> float | None:
    if n_tasks < 2:
        raise ValueError("n_tasks must be at least two")
    if not 0 <= baseline < 1:
        raise ValueError("baseline must be at least zero and below one")

    def detectable(attempt_effect: float) -> bool:
        required = required_tasks_for_effect(
            baseline=baseline,
            attempt_effect=attempt_effect,
            k=k,
            alpha=alpha,
            target_power=target_power,
            pair_correlation=pair_correlation,
        )
        return required is not None and required <= n_tasks

    upper = 1 - baseline
    if not detectable(upper):
        return None
    lower = 0.0
    for _ in range(60):
        midpoint = (lower + upper) / 2
        if detectable(midpoint):
            upper = midpoint
        else:
            lower = midpoint
    return upper


def power_requirements(
    *,
    baseline: float,
    attempt_effect: float,
    max_k: int,
    alpha: float = 0.05,
    target_power: float = 0.8,
    pair_correlation: float = 0.0,
) -> list[dict[str, float | int | None]]:
    if max_k < 1:
        raise ValueError("max_k must be positive")
    rows: list[dict[str, float | int | None]] = []
    for k in range(1, max_k + 1):
        p0 = pass_at_k_probability(baseline, k)
        p1 = pass_at_k_probability(baseline + attempt_effect, k)
        n_tasks = required_tasks_for_effect(
            baseline=baseline,
            attempt_effect=attempt_effect,
            k=k,
            alpha=alpha,
            target_power=target_power,
            pair_correlation=pair_correlation,
        )
        rows.append(
            {
                "k": k,
                "baseline_pass_at_k": p0,
                "comparison_pass_at_k": p1,
                "task_level_effect": p1 - p0,
                "required_n_tasks": n_tasks,
                "total_attempts_two_cohorts": 2 * k * n_tasks if n_tasks else None,
            }
        )
    return rows


def _task_evidence(
    members: list[CohortMember],
    *,
    pairing_key: str,
    k: int,
    threshold: float,
) -> tuple[dict[str, dict[str, Any]], list[str], int]:
    groups: dict[str, list[CohortMember]] = defaultdict(list)
    missing_pairing_key = 0
    for member in members:
        if member.exception_class is not None or member.reward is None:
            continue
        key = _pairing_value(member, pairing_key)
        if key is None:
            missing_pairing_key += 1
            continue
        groups[key].append(member)

    evidence: dict[str, dict[str, Any]] = {}
    insufficient: list[str] = []
    for key in sorted(groups):
        attempts = sorted(groups[key], key=lambda item: item.trial_id)
        if len(attempts) < k:
            insufficient.append(key)
            continue
        selected = attempts[:k]
        rewards = [float(item.reward) for item in selected if item.reward is not None]
        evidence[key] = {
            "success": float(any(reward >= threshold for reward in rewards)),
            "mean_reward": statistics.fmean(rewards),
            "members": selected,
        }
    return evidence, insufficient, missing_pairing_key


def _pass_at_k(
    members: list[CohortMember],
    *,
    pairing_key: str,
    k: int,
    threshold: float,
    seed: int,
) -> dict[str, Any]:
    evidence, insufficient, missing_pairing_key = _task_evidence(
        members,
        pairing_key=pairing_key,
        k=k,
        threshold=threshold,
    )
    outcomes = [float(evidence[key]["success"]) for key in sorted(evidence)]
    successes = int(sum(outcomes))
    interval = bootstrap_mean_interval(outcomes, seed=seed)
    return {
        "k": k,
        "evidence_unit": "task",
        "selection": "first-k-by-trial-id-per-task",
        "passes": successes,
        "n_tasks": len(outcomes),
        "denominator": len(outcomes),
        "rate": statistics.fmean(outcomes) if outcomes else None,
        "bootstrap_95": list(interval) if interval is not None else None,
        "insufficient_attempt_groups": insufficient,
        "missing_pairing_key_trials": missing_pairing_key,
        "selected_trials": {
            key: [item.trial_id for item in evidence[key]["members"]]
            for key in sorted(evidence)
        },
    }


def _summarize_cohort(
    label: str,
    members: list[CohortMember],
    spec: CohortComparisonSpec,
) -> dict[str, Any]:
    cohort = [member for member in members if member.cohort == label]
    exceptions = Counter(
        member.exception_class for member in cohort if member.exception_class is not None
    )
    missing_rewards = [
        member.trial_id
        for member in cohort
        if member.exception_class is None and member.reward is None
    ]
    capability = [
        member
        for member in cohort
        if member.exception_class is None and member.reward is not None
    ]
    return {
        "label": label,
        "n_total": len(cohort),
        "capability_denominator": len(capability),
        "exception_count": sum(exceptions.values()),
        "exceptions": dict(sorted(exceptions.items())),
        "missing_reward_count": len(missing_rewards),
        "missing_reward_trials": sorted(missing_rewards),
        "trial_pass_count": sum(
            _scored_reward(member) >= spec.pass_threshold for member in capability
        ),
        "reward": _numeric_summary([_scored_reward(member) for member in capability]),
        "duration_seconds": _numeric_summary(
            [
                float(member.duration_seconds)
                for member in capability
                if member.duration_seconds is not None
            ]
        ),
        "input_tokens": _numeric_summary(
            [float(member.input_tokens) for member in capability if member.input_tokens is not None]
        ),
        "output_tokens": _numeric_summary(
            [
                float(member.output_tokens)
                for member in capability
                if member.output_tokens is not None
            ]
        ),
        "cost_usd": _numeric_summary(
            [float(member.cost_usd) for member in capability if member.cost_usd is not None]
        ),
        "tool_call_count": _numeric_summary(
            [float(member.tool_call_count) for member in capability]
        ),
        "pass_at_k": [
            _pass_at_k(
                cohort,
                pairing_key=spec.pairing_key,
                k=k,
                threshold=spec.pass_threshold,
                seed=_bootstrap_seed(spec.comparison_id, label, k),
            )
            for k in spec.pass_k
        ],
        "members": [asdict(member) for member in cohort],
    }


def _elicitation_tuple(
    label: str,
    members: list[CohortMember],
    *,
    k: int,
) -> tuple[dict[str, Any] | None, list[str]]:
    if not members:
        return None, [f"cohort {label!r} contributes no paired task attempts"]
    values = {
        (
            member.agent_version,
            member.model_name,
            member.preamble_hash,
            member.toolset_digest,
            json.dumps(member.toolset, sort_keys=True) if member.toolset is not None else None,
        )
        for member in members
    }
    if len(values) != 1:
        return None, [f"cohort {label!r} mixes {len(values)} elicitation tuples"]
    agent_version, model_pin, preamble_hash, toolset_digest, toolset_json = next(iter(values))
    missing = [
        name
        for name, value in (
            ("agent version", agent_version),
            ("model pin", model_pin),
            ("preamble hash", preamble_hash),
            ("toolset", toolset_digest),
        )
        if not value
    ]
    if missing:
        return None, [f"cohort {label!r} is missing {', '.join(missing)}"]
    return {
        "agent_version": agent_version,
        "model_pin": model_pin,
        "preamble_hash": preamble_hash,
        "toolset": json.loads(toolset_json) if toolset_json is not None else None,
        "toolset_digest": toolset_digest,
        "k": k,
    }, []


def _paired_results(
    members: list[CohortMember],
    spec: CohortComparisonSpec,
    warnings: list[str],
) -> list[dict[str, Any]]:
    baseline = spec.cohorts[0].label
    results: list[dict[str, Any]] = []
    for selector in spec.cohorts[1:]:
        for k in spec.pass_k:
            baseline_members = [item for item in members if item.cohort == baseline]
            comparison_members = [item for item in members if item.cohort == selector.label]
            baseline_tasks, baseline_insufficient, baseline_missing = _task_evidence(
                baseline_members,
                pairing_key=spec.pairing_key,
                k=k,
                threshold=spec.pass_threshold,
            )
            comparison_tasks, comparison_insufficient, comparison_missing = _task_evidence(
                comparison_members,
                pairing_key=spec.pairing_key,
                k=k,
                threshold=spec.pass_threshold,
            )
            paired_keys = sorted(set(baseline_tasks) & set(comparison_tasks))
            pass_deltas = [
                float(comparison_tasks[key]["success"])
                - float(baseline_tasks[key]["success"])
                for key in paired_keys
            ]
            reward_deltas = [
                float(comparison_tasks[key]["mean_reward"])
                - float(baseline_tasks[key]["mean_reward"])
                for key in paired_keys
            ]
            interval = bootstrap_mean_interval(
                pass_deltas,
                seed=_bootstrap_seed(spec.comparison_id, baseline, selector.label, k),
            )
            selected_baseline = [
                member for key in paired_keys for member in baseline_tasks[key]["members"]
            ]
            selected_comparison = [
                member for key in paired_keys for member in comparison_tasks[key]["members"]
            ]
            baseline_elicitation, baseline_reasons = _elicitation_tuple(
                baseline, selected_baseline, k=k
            )
            comparison_elicitation, comparison_reasons = _elicitation_tuple(
                selector.label, selected_comparison, k=k
            )
            unpaired = sorted(set(baseline_tasks) ^ set(comparison_tasks))
            reasons: list[str] = []
            if spec.pairing_key not in {"task_block_id", "task_digest", "task_name"}:
                reasons.append(f"pairing key {spec.pairing_key!r} is not a task identity")
            if warnings:
                reasons.extend(warnings)
            if len(paired_keys) < 2:
                reasons.append(f"only {len(paired_keys)} paired task(s); at least 2 are required")
            if unpaired:
                reasons.append(f"{len(unpaired)} eligible task(s) are not paired across cohorts")
            if baseline_insufficient or comparison_insufficient:
                reasons.append(
                    "fewer than k scored attempts for "
                    f"{len(set(baseline_insufficient + comparison_insufficient))} task(s)"
                )
            if baseline_missing or comparison_missing:
                reasons.append(
                    f"{baseline_missing + comparison_missing} scored trial(s) lack task identity"
                )
            reasons.extend(baseline_reasons)
            reasons.extend(comparison_reasons)
            if interval is None:
                reasons.append("the paired task interval is unavailable")
            elif interval[0] <= 0 <= interval[1]:
                reasons.append(
                    f"the paired 95% interval [{interval[0]:.3f}, {interval[1]:.3f}] includes zero"
                )
            reasons = list(dict.fromkeys(reasons))
            if reasons:
                statement = f"{NOT_COMPARABLE}: {'; '.join(reasons)}"
                ranking = None
            elif interval is not None:
                if interval[0] > 0:
                    ranking = f"{selector.label} > {baseline}"
                else:
                    ranking = f"{baseline} > {selector.label}"
                statement = (
                    f"Ranking: {ranking}; n_tasks={len(paired_keys)}, k={k}, "
                    f"paired bootstrap 95% interval=[{interval[0]:.3f}, {interval[1]:.3f}]."
                )
            else:  # Guarded above; retained so static analysis sees total assignment.
                raise AssertionError("interval unexpectedly unavailable")
            results.append(
                {
                    "baseline": baseline,
                    "comparison": selector.label,
                    "pairing_key": spec.pairing_key,
                    "evidence_unit": "task",
                    "n_tasks": len(paired_keys),
                    "n_pairs": len(paired_keys),
                    "k": k,
                    "mean_pass_at_k_delta": (
                        statistics.fmean(pass_deltas) if pass_deltas else None
                    ),
                    "bootstrap_95": list(interval) if interval is not None else None,
                    "mean_reward_delta": (
                        statistics.fmean(reward_deltas) if reward_deltas else None
                    ),
                    "wins": sum(value > 0 for value in pass_deltas),
                    "ties": sum(value == 0 for value in pass_deltas),
                    "losses": sum(value < 0 for value in pass_deltas),
                    "elicitation": {
                        baseline: baseline_elicitation,
                        selector.label: comparison_elicitation,
                    },
                    "rankable": ranking is not None,
                    "ranking": ranking,
                    "statement": statement,
                    "refusal_reasons": reasons,
                    "unpaired_tasks": unpaired,
                    "pairs": [
                        {
                            "key": key,
                            "pass_at_k_delta": pass_deltas[index],
                            "reward_delta": reward_deltas[index],
                        }
                        for index, key in enumerate(paired_keys)
                    ],
                }
            )
    return results


def compare(spec: CohortComparisonSpec, *, repo_root: Path) -> dict[str, Any]:
    members = assemble_members(repo_root, spec)
    warnings = _validate_comparability(spec, members)
    report = {
        "schema_version": 1,
        "comparison_id": spec.comparison_id,
        "experiment_id": spec.experiment_id,
        "spec_digest": digest_json(spec.model_dump(mode="json")),
        "mode": spec.mode,
        "declared_variable": spec.declared_variable,
        "reward_name": spec.reward_name,
        "pass_threshold": spec.pass_threshold,
        "pairing_key": spec.pairing_key,
        "validity_warnings": warnings,
        "cohorts": [
            _summarize_cohort(selector.label, members, spec) for selector in spec.cohorts
        ],
        "paired": _paired_results(members, spec, warnings),
    }
    return report


def summarize_job_evidence(
    job: JobRecord,
    *,
    repo_root: Path,
    k: int,
    reward_name: str = "reward",
    pass_threshold: float = 1.0,
) -> dict[str, Any]:
    members = [
        _member(repo_root, job.name, job.name, job, trial, reward_name)
        for trial in job.trials
    ]
    evidence, insufficient, missing_pairing_key = _task_evidence(
        members,
        pairing_key="task_digest",
        k=k,
        threshold=pass_threshold,
    )
    selected = [member for key in sorted(evidence) for member in evidence[key]["members"]]
    elicitation, elicitation_reasons = _elicitation_tuple(job.name, selected, k=k)
    metric = _pass_at_k(
        members,
        pairing_key="task_digest",
        k=k,
        threshold=pass_threshold,
        seed=_bootstrap_seed(job.id, k),
    )
    return {
        "job_id": job.id,
        "job_name": job.name,
        "n_trials": len(members),
        "n_tasks": metric["n_tasks"],
        "k": k,
        "pass_at_k": metric,
        "elicitation": elicitation,
        "elicitation_reasons": elicitation_reasons,
        "exception_count": sum(member.exception_class is not None for member in members),
        "missing_reward_count": sum(
            member.exception_class is None and member.reward is None for member in members
        ),
        "insufficient_tasks": insufficient,
        "missing_task_identity_trials": missing_pairing_key,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Cohort comparison: {report['comparison_id']}",
        "",
        f"Mode: `{report['mode']}`. Declared variable: `{report['declared_variable']}`. ",
        f"Experiment: `{report['experiment_id']}`. Reward: `{report['reward_name']}`.",
        "",
    ]
    warnings = report["validity_warnings"]
    if warnings:
        lines.extend(["## Validity warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)
        lines.append("")
    lines.extend(
        [
            "## Outcomes",
            "",
            "| cohort | total | capability denominator | exceptions | pass@k |",
            "|---|---:|---:|---:|---|",
        ]
    )
    for cohort in report["cohorts"]:
        pass_cells = []
        for metric in cohort["pass_at_k"]:
            interval = metric["bootstrap_95"]
            if metric["rate"] is None:
                value = "n/a"
            else:
                value = (
                    f"{metric['rate']:.3f} "
                    f"({metric['passes']}/{metric['n_tasks']} tasks)"
                )
                if interval is not None:
                    value += f" [{interval[0]:.3f}, {interval[1]:.3f}]"
            pass_cells.append(f"@{metric['k']} {value}")
        lines.append(
            f"| {cohort['label']} | {cohort['n_total']} | "
            f"{cohort['capability_denominator']} | {cohort['exception_count']} | "
            f"{'<br>'.join(pass_cells)} |"
        )
    lines.extend(
        [
            "",
            "Exceptions are reported beside, and excluded from, the capability denominator.",
            "",
        ]
    )
    lines.extend(["## Paired by task", ""])
    for paired in report["paired"]:
        lines.append(f"### pass@{paired['k']}: {paired['comparison']} vs {paired['baseline']}")
        lines.append("")
        lines.append(paired["statement"])
        lines.append("")
        lines.append(
            f"Paired task delta={paired['mean_pass_at_k_delta']}; "
            f"wins/ties/losses={paired['wins']}/{paired['ties']}/{paired['losses']}."
        )
        lines.append("")
        lines.extend(["Elicitation tuples:", ""])
        for label, elicitation in paired["elicitation"].items():
            value = json.dumps(elicitation, sort_keys=True) if elicitation else "unavailable"
            lines.append(f"- `{label}`: {value}")
        lines.append("")
    lines.extend(
        [
            "",
            "## Interpretation boundary",
            "",
            "Attempts within one task are clustered into one evidence unit. Every interval above ",
            "resamples tasks, and every two-cohort decision uses task-paired deltas. A ranking is ",
            "printed only when the paired interval excludes zero and both elicitation tuples are ",
            "complete; otherwise the report states the refusal reason.",
            "",
        ]
    )
    return "\n".join(lines)


def write_comparison(
    spec_path: Path,
    *,
    repo_root: Path,
    output_root: Path | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    spec = load_spec(spec_path)
    report = compare(spec, repo_root=repo_root)
    destination = (output_root or repo_root / "derived/comparisons").resolve()
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / f"{spec.comparison_id}.json"
    markdown_path = destination / f"{spec.comparison_id}.md"
    json_payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    markdown_payload = render_markdown(report)
    for path, payload in ((json_path, json_payload), (markdown_path, markdown_payload)):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(payload)
        temporary.replace(path)
    return json_path, markdown_path, report


def index_comparison_associations(
    database_url: str,
    *,
    spec_path: Path,
    report: dict[str, Any],
    repo_root: Path,
) -> None:
    """Associate legacy/raw jobs only when a reviewed cohort spec declares it."""
    spec = load_spec(spec_path)
    if report.get("spec_digest") != digest_json(spec.model_dump(mode="json")):
        raise ValueError("comparison report does not match the supplied spec")
    provenance = {
        "comparison_spec": (
            spec_path.resolve().relative_to(repo_root.resolve()).as_posix()
            if repo_root.resolve() in spec_path.resolve().parents
            else spec_path.resolve().as_posix()
        ),
        "spec_digest": report["spec_digest"],
        "declared_variable": spec.declared_variable,
        "mode": spec.mode,
    }
    job_ids = sorted(
        {
            member["job_id"]
            for cohort in report["cohorts"]
            for member in cohort["members"]
        }
    )
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            INSERT INTO experiments (id, source_kind, raw_provenance)
            VALUES (%s, 'cohort-spec', %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (spec.experiment_id, Jsonb(provenance)),
        )
        for job_id in job_ids:
            row = connection.execute(
                "SELECT experiment_id FROM jobs WHERE id = %s", (job_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"comparison job is not indexed: {job_id}")
            if row[0] not in (None, spec.experiment_id):
                raise ValueError(
                    f"job {job_id} is already associated with experiment {row[0]!r}"
                )
            connection.execute(
                "UPDATE jobs SET experiment_id = %s WHERE id = %s",
                (spec.experiment_id, job_id),
            )
