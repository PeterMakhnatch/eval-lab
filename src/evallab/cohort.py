from __future__ import annotations

import json
import math
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
)


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
    agent_name: str | None
    agent_version: str | None
    model_name: str | None
    model_settings_digest: str
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
    return center - half_width, center + half_width


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


def _member(
    root: Path,
    experiment_id: str,
    label: str,
    job: JobRecord,
    trial: TrialRecord,
    reward_name: str,
) -> CohortMember:
    fact: TrialFact = extract_trial_fact(job, trial)
    agent_lock = trial.lock.get("agent") if isinstance(trial.lock.get("agent"), dict) else {}
    model_settings = {
        key: value
        for key, value in agent_lock.items()
        if key not in {"name", "model_name", "import_path"}
    }
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
        agent_name=fact.agent_name,
        agent_version=fact.agent_version,
        model_name=fact.model_name,
        model_settings_digest=digest_json(model_settings),
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
    differences = {
        field: sorted(
            {member.condition(field) for member in members},
            key=lambda value: "" if value is None else value,
        )
        for field in CONSEQUENTIAL_FIELDS
    }
    differing_fields = [field for field, values in differences.items() if len(values) > 1]
    warnings: list[str] = []
    for field, expected in spec.constraints.items():
        actual = set(differences[field])
        if actual != {expected}:
            observed = sorted(str(value) for value in actual)
            warnings.append(
                f"constraint {field}={expected!r} does not match observed {observed}"
            )
    invariant_mismatches = [
        field
        for field in ("task_digest", "verifier_digest")
        if field in differing_fields
    ]
    undeclared = [field for field in differing_fields if field != spec.declared_variable]
    if spec.declared_variable not in differing_fields:
        warnings.append(f"declared variable {spec.declared_variable!r} does not differ")
    warnings.extend(
        f"undeclared consequential variable differs: {field} ({differences[field]})"
        for field in undeclared
    )
    warnings.extend(
        f"causal invariant differs: {field} ({differences[field]})"
        for field in invariant_mismatches
    )
    if spec.mode == "causal" and warnings:
        raise ValueError("comparison refused: " + "; ".join(dict.fromkeys(warnings)))
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


def _pairing_value(member: CohortMember, pairing_key: str) -> str | None:
    value = getattr(member, pairing_key)
    return str(value) if value is not None else None


def _pass_at_k(
    members: list[CohortMember],
    *,
    pairing_key: str,
    k: int,
    threshold: float,
) -> dict[str, Any]:
    eligible_trials = [
        member
        for member in members
        if member.exception_class is None and member.reward is not None
    ]
    if k == 1:
        successes = sum(float(member.reward) >= threshold for member in eligible_trials)
        interval = wilson_interval(successes, len(eligible_trials))
        return {
            "k": 1,
            "selection": "all-exception-free-scored-trials",
            "passes": successes,
            "denominator": len(eligible_trials),
            "rate": successes / len(eligible_trials) if eligible_trials else None,
            "wilson_95": list(interval) if interval is not None else None,
            "insufficient_attempt_groups": [],
            "missing_pairing_key_trials": 0,
            "selected_trials": {
                "all": [
                    member.trial_id
                    for member in sorted(eligible_trials, key=lambda item: item.trial_id)
                ]
            },
        }
    groups: dict[str, list[CohortMember]] = defaultdict(list)
    missing_pairing_key = 0
    for member in eligible_trials:
        key = _pairing_value(member, pairing_key)
        if key is None:
            missing_pairing_key += 1
            continue
        groups[key].append(member)
    successes = 0
    eligible = 0
    insufficient: list[str] = []
    selected_trials: dict[str, list[str]] = {}
    for key in sorted(groups):
        attempts = sorted(groups[key], key=lambda item: item.trial_id)
        if len(attempts) < k:
            insufficient.append(key)
            continue
        selected = attempts[:k]
        selected_trials[key] = [item.trial_id for item in selected]
        eligible += 1
        successes += any(float(item.reward) >= threshold for item in selected)
    interval = wilson_interval(successes, eligible)
    return {
        "k": k,
        "selection": "first-k-by-trial-id-per-pairing-key",
        "passes": successes,
        "denominator": eligible,
        "rate": successes / eligible if eligible else None,
        "wilson_95": list(interval) if interval is not None else None,
        "insufficient_attempt_groups": insufficient,
        "missing_pairing_key_trials": missing_pairing_key,
        "selected_trials": selected_trials,
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
            float(member.reward) >= spec.pass_threshold for member in capability
        ),
        "reward": _numeric_summary([float(member.reward) for member in capability]),
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
            )
            for k in spec.pass_k
        ],
        "members": [asdict(member) for member in cohort],
    }


def _paired_results(
    members: list[CohortMember], spec: CohortComparisonSpec
) -> list[dict[str, Any]]:
    baseline = spec.cohorts[0].label
    grouped: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for member in members:
        if member.exception_class is not None or member.reward is None:
            continue
        key = _pairing_value(member, spec.pairing_key)
        if key is not None:
            grouped[key][member.cohort].append(float(member.reward))
    results: list[dict[str, Any]] = []
    for selector in spec.cohorts[1:]:
        deltas: list[tuple[str, float]] = []
        for key in sorted(grouped):
            if baseline not in grouped[key] or selector.label not in grouped[key]:
                continue
            baseline_mean = statistics.fmean(grouped[key][baseline])
            comparison_mean = statistics.fmean(grouped[key][selector.label])
            deltas.append((key, comparison_mean - baseline_mean))
        results.append(
            {
                "baseline": baseline,
                "comparison": selector.label,
                "pairing_key": spec.pairing_key,
                "n_pairs": len(deltas),
                "mean_reward_delta": (
                    statistics.fmean(value for _, value in deltas) if deltas else None
                ),
                "wins": sum(value > 0 for _, value in deltas),
                "ties": sum(value == 0 for _, value in deltas),
                "losses": sum(value < 0 for _, value in deltas),
                "pairs": [
                    {"key": key, "reward_delta": value} for key, value in deltas
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
        "paired": _paired_results(members, spec),
    }
    return report


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
            interval = metric["wilson_95"]
            if metric["rate"] is None:
                value = "n/a"
            else:
                value = f"{metric['rate']:.3f} ({metric['passes']}/{metric['denominator']})"
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
        lines.append(
            f"- `{paired['comparison']}` minus `{paired['baseline']}`: "
            f"n={paired['n_pairs']}, mean reward delta={paired['mean_reward_delta']}, "
            f"wins/ties/losses={paired['wins']}/{paired['ties']}/{paired['losses']}."
        )
    lines.extend(
        [
            "",
            "## Interpretation boundary",
            "",
            "These are deterministic summaries of the selected trials. Wilson intervals describe ",
            "the pass@1 trial proportion and realized task-level first-k proportions; they do "
            "not establish broad model ",
            "capability or statistical significance.",
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
