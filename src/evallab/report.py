from __future__ import annotations

import json
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import duckdb
from pydantic import ValidationError

from evallab.cohort import summarize_job_evidence
from evallab.facts import digest_json
from evallab.results import JobRecord, TrialRecord, load_job, load_jobs
from evallab.schemas import ExperimentSpec

JsonObject = dict[str, Any]
_VERIFY_PATTERN = re.compile(
    r"(?:^|[\s/])(?:pytest|ruff|mypy|pyright|go\s+test|cargo\s+test|npm\s+test|"
    r"pnpm\s+test|make\s+(?:test|check|verify|premerge)|[^\s]*verify[^\s]*|"
    r"[^\s]*check[^\s]*)",
    re.IGNORECASE,
)


def _read_parquet_rows(root: Path, filename: str) -> list[JsonObject]:
    files = sorted(root.glob(f"**/{filename}"))
    if not files:
        return []
    parquet_glob = (root / "**" / filename).as_posix()
    with duckdb.connect(database=":memory:") as connection:
        cursor = connection.execute(
            "SELECT * FROM read_parquet(?, hive_partitioning = true, union_by_name = true)",
            [parquet_glob],
        )
        names = [str(item[0]) for item in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _task_matches(observed: object, requested: str) -> bool:
    value = str(observed or "")
    return value == requested or value.rsplit("/", 1)[-1] == requested


def _json_payloads(value: Any) -> Iterable[JsonObject]:
    if not isinstance(value, dict):
        return
    schema = value.get("schema_version")
    if isinstance(schema, str) and schema.startswith("ATIF-"):
        yield value
    children = value.get("subagent_trajectories")
    if isinstance(children, list):
        for child in children:
            yield from _json_payloads(child)


def _trial_atif_payloads(trial: TrialRecord) -> list[JsonObject]:
    agent_root = trial.path / "agent"
    if not agent_root.is_dir():
        return []
    payloads: list[JsonObject] = []
    for path in sorted(agent_root.rglob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        payloads.extend(_json_payloads(value))
    return payloads


def _string_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _string_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _string_values(item)


def _has_verification_call(payloads: list[JsonObject]) -> bool:
    for payload in payloads:
        steps = payload.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            calls = step.get("tool_calls")
            if not isinstance(calls, list):
                continue
            for call in calls:
                if not isinstance(call, dict):
                    continue
                function_name = str(call.get("function_name") or "")
                arguments = " ".join(_string_values(call.get("arguments")))
                if _VERIFY_PATTERN.search(f"{function_name} {arguments}"):
                    return True
    return False


def _raw_trials(roots: Iterable[Path]) -> dict[str, tuple[JobRecord, TrialRecord]]:
    return {
        trial.id: (job, trial)
        for job in load_jobs(roots)
        for trial in job.trials
    }


def _number_summary(values: list[float]) -> JsonObject:
    if not values:
        return {"n": 0, "total": None, "median": None, "min": None, "max": None}
    return {
        "n": len(values),
        "total": sum(values),
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
    }


def family_report(
    task: str,
    *,
    parquet_root: Path,
    raw_roots: Iterable[Path],
) -> JsonObject:
    raw_root_list = list(raw_roots)
    trial_rows = [
        row
        for row in _read_parquet_rows(parquet_root, "trial_facts.parquet")
        if _task_matches(row.get("task_name"), task)
    ]
    if not trial_rows:
        available = sorted(
            {
                str(row.get("task_name"))
                for row in _read_parquet_rows(parquet_root, "trial_facts.parquet")
                if row.get("task_name")
            }
        )
        suffix = f" Available tasks: {', '.join(available)}" if available else ""
        raise ValueError(f"no Parquet trials found for task {task!r}.{suffix}")

    trial_ids = {str(row["trial_id"]) for row in trial_rows}
    observation_rows = [
        row
        for row in _read_parquet_rows(parquet_root, "observations.parquet")
        if str(row.get("trial_id")) in trial_ids
    ]
    tool_rows = [
        row
        for row in _read_parquet_rows(parquet_root, "tool_calls.parquet")
        if str(row.get("trial_id")) in trial_ids
    ]
    raw_by_trial = _raw_trials(raw_root_list)

    first_failure_by_trial: dict[str, int] = {}
    for row in observation_rows:
        exit_code = row.get("command_exit_code")
        if exit_code in (None, 0):
            continue
        trial_id = str(row["trial_id"])
        step_id = int(row["step_id"])
        first_failure_by_trial[trial_id] = min(
            step_id, first_failure_by_trial.get(trial_id, step_id)
        )

    calls_by_trial: dict[str, Counter[tuple[str, str]]] = defaultdict(Counter)
    for row in tool_rows:
        calls_by_trial[str(row["trial_id"])][
            (str(row.get("function_name") or "unknown"), str(row.get("arguments_sha256") or ""))
        ] += 1
    looping_trials = {
        trial_id: sum(count - 1 for count in calls.values() if count > 1)
        for trial_id, calls in calls_by_trial.items()
        if any(count > 1 for count in calls.values())
    }

    verification: dict[str, bool | None] = {}
    raw_missing: list[str] = []
    for trial_id in sorted(trial_ids):
        raw = raw_by_trial.get(trial_id)
        if raw is None:
            verification[trial_id] = None
            raw_missing.append(trial_id)
            continue
        payloads = _trial_atif_payloads(raw[1])
        verification[trial_id] = _has_verification_call(payloads) if payloads else None

    failure_counts = Counter(first_failure_by_trial.values())
    costs = [float(row["cost_usd"]) for row in trial_rows if row.get("cost_usd") is not None]
    steps = [
        float(row["step_count"])
        for row in trial_rows
        if row.get("step_count") is not None and int(row.get("trajectory_count") or 0) > 0
    ]
    rewards = [
        float(row["primary_reward"])
        for row in trial_rows
        if row.get("primary_reward") is not None and row.get("exception_class") is None
    ]
    yes = sum(value is True for value in verification.values())
    no = sum(value is False for value in verification.values())
    unknown = sum(value is None for value in verification.values())
    return {
        "schema_version": 1,
        "task": task,
        "sources": {
            "parquet_root": parquet_root.resolve().as_posix(),
            "raw_roots": [path.resolve().as_posix() for path in raw_root_list],
        },
        "n_jobs": len({str(row["job_id"]) for row in trial_rows}),
        "n_trials": len(trial_rows),
        "n_scored": len(rewards),
        "exceptions": sum(row.get("exception_class") is not None for row in trial_rows),
        "first_failure_step_distribution": [
            {"step": step, "trials": count} for step, count in sorted(failure_counts.items())
        ],
        "trials_without_observed_command_failure": len(trial_rows) - len(first_failure_by_trial),
        "loop_detection": {
            "heuristic": "repeated identical function name and arguments digest within one trial",
            "trials": len(looping_trials),
            "repeated_calls": sum(looping_trials.values()),
            "trial_ids": sorted(looping_trials),
        },
        "verification_before_done": {
            "heuristic": (
                "recognizable test, lint, typecheck, check, or verify tool call in raw ATIF"
            ),
            "yes": yes,
            "no": no,
            "unknown": unknown,
        },
        "cost_usd": _number_summary(costs),
        "steps": _number_summary(steps),
        "raw_trials_missing": raw_missing,
    }


def _count_phrase(count: int, denominator: int) -> str:
    if denominator == 0:
        return "no measurable trials"
    return f"{count} of {denominator} trials ({count / denominator:.0%})"


def render_family_report(report: JsonObject) -> str:
    trial_count = int(report["n_trials"])
    failures = report["first_failure_step_distribution"]
    verification = report["verification_before_done"]
    loops = report["loop_detection"]
    cost = report["cost_usd"]
    steps = report["steps"]
    lines = [
        f"# Trajectory family report: {report['task']}",
        "",
        (
            f"This family contains {trial_count} trials across {report['n_jobs']} jobs. "
            f"{report['n_scored']} trials have a scored capability result and "
            f"{report['exceptions']} ended with a harness or execution exception."
        ),
        "",
        "## Where commands first failed",
        "",
    ]
    if failures:
        lines.append(
            "Among trials with a structured non-zero command exit, the first failure appeared at:"
        )
        lines.extend(f"- Step {item['step']}: {item['trials']} trial(s)" for item in failures)
    else:
        lines.append("No structured non-zero command exit was observed in this family.")
    lines.append(
        f"{report['trials_without_observed_command_failure']} trial(s) had no observed command "
        "failure; this does not prove that the final answer was correct."
    )
    lines.extend(
        [
            "",
            "## Repeated work",
            "",
            (
                f"The loop heuristic flagged {_count_phrase(int(loops['trials']), trial_count)}. "
                f"It found {loops['repeated_calls']} repeated identical tool call(s)."
            ),
            f"Heuristic: {loops['heuristic']}.",
            "",
            "## Verification before completion",
            "",
            (
                f"Recognizable verification ran in {verification['yes']} trial(s), did not run in "
                f"{verification['no']} trial(s), and was unknown for {verification['unknown']} "
                "trial(s) without readable ATIF."
            ),
            f"Heuristic: {verification['heuristic']}.",
            "",
            "## Cost and length",
            "",
        ]
    )
    if cost["n"]:
        lines.append(
            f"Cost was reported for {cost['n']} trial(s): ${cost['total']:.4f} total, "
            f"${cost['median']:.4f} median, range ${cost['min']:.4f}–${cost['max']:.4f}."
        )
    else:
        lines.append("No trial in this family reported cost.")
    if steps["n"]:
        lines.append(
            f"Trajectory length was available for {steps['n']} trial(s): median "
            f"{steps['median']:g} steps, range {steps['min']:g}–{steps['max']:g}."
        )
    else:
        lines.append("No trajectory step counts were available.")
    lines.extend(
        [
            "",
            "## Reading boundary",
            "",
            "These are deterministic trajectory indicators, not causal claims. Repeated calls may "
            "be deliberate, command failures may be recovered, and verification detection is a "
            "conservative command-name/argument heuristic.",
            "",
        ]
    )
    return "\n".join(lines)


def _safe_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    if not normalized:
        raise ValueError("report name has no safe filename characters")
    return normalized


def write_family_report(
    task: str,
    *,
    parquet_root: Path,
    raw_roots: Iterable[Path],
    output_root: Path,
) -> tuple[Path, Path, JsonObject]:
    raw_root_list = list(raw_roots)
    report = family_report(task, parquet_root=parquet_root, raw_roots=raw_root_list)
    output_root.mkdir(parents=True, exist_ok=True)
    stem = f"family-{_safe_name(task)}"
    json_path = output_root / f"{stem}.json"
    markdown_path = output_root / f"{stem}.md"
    for path, content in (
        (json_path, json.dumps(report, indent=2, sort_keys=True) + "\n"),
        (markdown_path, render_family_report(report)),
    ):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    return json_path, markdown_path, report


def load_completed_spec(path: Path) -> ExperimentSpec:
    try:
        return ExperimentSpec.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise ValueError(f"invalid completed experiment spec {path}: {exc}") from exc


def _completed_job(root: Path, spec: ExperimentSpec) -> JobRecord:
    direct = root / spec.jobs_dir / spec.name
    if (direct / "result.json").is_file():
        candidates = [load_job(direct)]
    else:
        candidates = [
            job
            for job in load_jobs(
                [root / spec.jobs_dir, root / "research/evidence/runs", root / "evidence/runs"]
            )
            if job.name == spec.name
            or (
                spec.spec_id is not None
                and _json_experiment(job).get("spec_id") == spec.spec_id
            )
        ]
    if len(candidates) != 1:
        raise ValueError(
            f"expected one completed job for spec {spec.name!r}, found {len(candidates)}"
        )
    job = candidates[0]
    expected = job.result.get("n_total_trials")
    if isinstance(expected, int) and expected != len(job.trials):
        raise ValueError(
            f"job {job.name!r} is incomplete: {len(job.trials)} of {expected} trials recorded"
        )
    return job


def _json_experiment(job: JobRecord) -> JsonObject:
    value = job.metadata.get("experiment")
    return value if isinstance(value, dict) else {}


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def build_eval_card(
    spec_path: Path,
    *,
    repo_root: Path,
) -> tuple[str, JsonObject]:
    spec = load_completed_spec(spec_path)
    job = _completed_job(repo_root, spec)
    evidence = summarize_job_evidence(
        job,
        repo_root=repo_root,
        k=spec.attempts,
    )
    metric = evidence["pass_at_k"]
    interval = metric["bootstrap_95"]
    threats = ["One completed job captures one time and execution environment."]
    if evidence["n_tasks"] < 20:
        threats.append(
            f"Only {evidence['n_tasks']} task evidence unit(s); generalization is weak."
        )
    if evidence["exception_count"]:
        threats.append(
            f"{evidence['exception_count']} exception trial(s) were excluded from capability."
        )
    if evidence["elicitation"] is None:
        threats.extend(evidence["elicitation_reasons"])
    if evidence["insufficient_tasks"]:
        threats.append(
            f"{len(evidence['insufficient_tasks'])} task(s) had fewer than k scored attempts."
        )
    card = {
        "schema_version": 1,
        "title": spec.name,
        "spec_path": _relative(spec_path, repo_root),
        "spec_digest": digest_json(spec.model_dump(mode="json")),
        "job_path": _relative(job.path, repo_root),
        "job_id": job.id,
        "job_lock_digest": digest_json(job.lock),
        "task": spec.task,
        "hypothesis": spec.hypothesis,
        "numbers": {
            "n_tasks": evidence["n_tasks"],
            "n_trials": evidence["n_trials"],
            "k": spec.attempts,
            "pass_at_k": metric["rate"],
            "bootstrap_95": interval,
            "exceptions": evidence["exception_count"],
        },
        "elicitation": evidence["elicitation"],
        "contamination_note": (
            "Not determined automatically. Before publication, document benchmark exposure, "
            "training-data plausibility, task reuse, and whether any attempt could observe "
            "another attempt's artifacts."
        ),
        "threats": threats,
    }
    template_path = repo_root / "research/cards/TEMPLATE.md"
    if not template_path.is_file():
        raise ValueError(f"eval-card template is missing: {template_path}")
    interval_text = (
        "unavailable"
        if interval is None
        else f"[{float(interval[0]):.3f}, {float(interval[1]):.3f}]"
    )
    replacements = {
        "{{TITLE}}": spec.name,
        "{{HYPOTHESIS}}": spec.hypothesis,
        "{{SPEC_PATH}}": card["spec_path"],
        "{{SPEC_DIGEST}}": card["spec_digest"],
        "{{JOB_PATH}}": card["job_path"],
        "{{JOB_ID}}": job.id,
        "{{JOB_LOCK_DIGEST}}": card["job_lock_digest"],
        "{{TASK}}": spec.task,
        "{{N_TASKS}}": str(evidence["n_tasks"]),
        "{{N_TRIALS}}": str(evidence["n_trials"]),
        "{{K}}": str(spec.attempts),
        "{{PASS_AT_K}}": "unavailable" if metric["rate"] is None else f"{metric['rate']:.3f}",
        "{{INTERVAL}}": interval_text,
        "{{EXCEPTIONS}}": str(evidence["exception_count"]),
        "{{ELICITATION}}": json.dumps(evidence["elicitation"], indent=2, sort_keys=True),
        "{{CONTAMINATION}}": card["contamination_note"],
        "{{THREATS}}": "\n".join(f"- {item}" for item in threats),
    }
    rendered = template_path.read_text(encoding="utf-8")
    for marker, value in replacements.items():
        rendered = rendered.replace(marker, str(value))
    if "{{" in rendered:
        raise ValueError("eval-card template contains an unresolved marker")
    return rendered, card


def draft_eval_card(
    spec_path: Path,
    *,
    repo_root: Path,
    output_path: Path | None = None,
) -> tuple[Path, JsonObject]:
    rendered, card = build_eval_card(spec_path, repo_root=repo_root)
    destination = (
        output_path
        or repo_root / "research/cards" / f"{_safe_name(str(card['title']))}.md"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite eval card: {destination}")
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(destination)
    return destination, card
