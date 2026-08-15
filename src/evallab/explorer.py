"""Read-only run and analysis explorer (M005).

Assembles linked, provenance-labeled views over the evidence the lab already
holds — raw Harbor job directories, analysis sidecars, and (optionally) the
status snapshot — so an operator can select a task, job, trial, trajectory,
or analysis and understand what ran, what happened, why it was classified,
and the exact safe command for the next action.

Guarantees, enforced by tests:

- **Zero writes.** Every loader opens files read-only; building an index
  leaves the evidence byte-identical.
- **Every field labels its provenance**: ``observed`` (read from evidence),
  ``derived`` (computed here), ``draft`` (unreviewed model output),
  ``unavailable`` (missing/malformed, with a reason).
- **Infrastructure exceptions are never conflated with reward failures.**
- **Path jail.** Nothing outside the configured roots is ever read or
  linked; ``..`` escapes resolve to a refusal, not a file.
- **No secrets, no hidden verifier content.** Key-shaped names are redacted
  from any rendered mapping; task ``tests/`` and ``solution/`` contents are
  never listed or read.
- **Next Action emits commands, never executes them.**
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from evallab.schemas import TrialAnalysisSidecar

Provenance = Literal["observed", "derived", "draft", "unavailable"]

_SECRET_MARKERS = ("API_KEY", "API_TOKEN", "_SECRET", "ACCESS_KEY", "PASSWORD", "TOKEN")
_HIDDEN_TASK_DIRS = frozenset({"tests", "solution"})
_VERIFY_HINTS = ("pytest", "test", "verify", "check", "lint", "validate")
CONTROL_AGENTS = frozenset({"oracle", "nop"})


@dataclass(frozen=True)
class Labeled:
    """A value plus the provenance label the UI must render beside it."""

    value: Any
    provenance: Provenance
    reason: str | None = None


def observed(value: Any) -> Labeled:
    return Labeled(value, "observed")


def derived(value: Any, reason: str | None = None) -> Labeled:
    return Labeled(value, "derived", reason)


def unavailable(reason: str) -> Labeled:
    return Labeled(None, "unavailable", reason)


def redact_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    """Drop values whose keys look credential-shaped; keys stay visible."""
    clean: dict[str, Any] = {}
    for key, value in mapping.items():
        if any(marker in key.upper() for marker in _SECRET_MARKERS):
            clean[key] = "[redacted]"
        elif isinstance(value, dict):
            clean[key] = redact_mapping(value)
        else:
            clean[key] = value
    return clean


def jail(root: Path, candidate: str) -> Path | None:
    """Resolve *candidate* strictly inside *root*; None on any escape."""
    if candidate.startswith(("/", "~")):
        return None
    resolved = (root / candidate).resolve()
    root_resolved = root.resolve()
    if resolved == root_resolved or root_resolved in resolved.parents:
        # hidden verifier inputs are never exposed even inside the jail
        relative = resolved.relative_to(root_resolved)
        if any(part in _HIDDEN_TASK_DIRS for part in relative.parts[:-1]) or (
            relative.parts and relative.parts[0] in _HIDDEN_TASK_DIRS
        ):
            return None
        return resolved
    return None


def _load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        return None, f"missing: {path.name}"
    except (OSError, ValueError) as exc:
        return None, f"malformed {path.name}: {exc.__class__.__name__}"
    if not isinstance(payload, dict):
        return None, f"malformed {path.name}: not an object"
    return payload, None


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCallRow:
    step_id: int
    tool_call_id: str
    function: str
    exit_code: int | None  # observed from linked observation when present


@dataclass(frozen=True)
class TrajectoryView:
    step_count: Labeled
    steps: tuple[dict[str, Any], ...]  # step_id, source, n_tool_calls
    tool_calls: tuple[ToolCallRow, ...]
    repeated_signatures: Labeled  # derived: [(function, count)] repeats > 1
    verify_before_done: Labeled  # derived tri-state True/False


@dataclass(frozen=True)
class ArtifactLink:
    name: str
    relative_path: str  # jailed, trial-relative; safe to open read-only
    size_bytes: int


@dataclass(frozen=True)
class CitationResolution:
    citation_path: str
    step_id: int | None
    tool_call_id: str | None
    supports: str
    resolution: Labeled  # derived "resolved" | unavailable(reason)


@dataclass(frozen=True)
class AnalysisView:
    analysis_id: str
    trial_key: str | None
    status: Labeled  # observed validation_status
    validity: Labeled  # draft — model output until reviewed
    category: Labeled  # draft
    summary: Labeled  # draft
    confidence: Labeled  # draft
    citations: tuple[CitationResolution, ...]
    alternatives: Labeled  # draft
    provenance: Labeled  # observed analysis_provenance (agent/model/digests)


@dataclass(frozen=True)
class TrialView:
    trial_key: str  # job_name/trial_name
    job_name: str
    trial_name: str
    trial_dir: str
    task_name: Labeled
    agent: Labeled
    model: Labeled
    reward: Labeled
    outcome_class: Labeled  # derived: pass|reward-failure|infra-exception|no-verdict
    exception: Labeled  # observed; infra — NEVER merged with reward
    timing: Labeled
    cost: Labeled
    config: Labeled  # observed, redacted
    trajectory: TrajectoryView | Labeled
    artifacts: tuple[ArtifactLink, ...]


@dataclass(frozen=True)
class JobView:
    job_name: str
    job_dir: str
    task_names: Labeled
    trial_keys: tuple[str, ...]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskView:
    task_name: str
    registration: Labeled  # derived from library/registry presence when given
    control_state: Labeled  # derived: which control agents have evidence here
    trial_keys: tuple[str, ...]


@dataclass(frozen=True)
class NextAction:
    label: str
    command: str  # copyable; NEVER executed by the explorer


@dataclass(frozen=True)
class ExplorerIndex:
    tasks: tuple[TaskView, ...]
    jobs: tuple[JobView, ...]
    trials: dict[str, TrialView]
    analyses: tuple[AnalysisView, ...]
    notes: tuple[str, ...]  # degradation reasons; cold start stays navigable


# ---------------------------------------------------------------------------
# Trajectory assembly
# ---------------------------------------------------------------------------


def _trajectory_view(trial_dir: Path) -> TrajectoryView | Labeled:
    path = trial_dir / "agent" / "trajectory.json"
    payload, error = _load_json(path)
    if payload is None:
        return unavailable(error or "trajectory missing")
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list):
        return unavailable("trajectory has no steps array")

    steps: list[dict[str, Any]] = []
    calls: list[ToolCallRow] = []
    signature_counts: dict[tuple[str, str], int] = {}
    exit_by_call: dict[str, int] = {}
    for step in raw_steps:
        if not isinstance(step, dict):
            continue
        for obs in step.get("observations") or []:
            if isinstance(obs, dict):
                call_ref = obs.get("source_call_id")
                exit_code = obs.get("command_exit_code")
                if isinstance(call_ref, str) and isinstance(exit_code, int):
                    exit_by_call[call_ref] = exit_code
    for step in raw_steps:
        if not isinstance(step, dict):
            continue
        step_id = step.get("step_id")
        step_calls = [c for c in (step.get("tool_calls") or []) if isinstance(c, dict)]
        steps.append(
            {
                "step_id": step_id,
                "source": step.get("source"),
                "n_tool_calls": len(step_calls),
            }
        )
        for call in step_calls:
            function = str(
                (call.get("function") or {}).get("name")
                if isinstance(call.get("function"), dict)
                else call.get("function_name") or call.get("name") or "?"
            )
            arguments = json.dumps(
                (call.get("function") or {}).get("arguments")
                if isinstance(call.get("function"), dict)
                else call.get("arguments"),
                sort_keys=True,
                default=str,
            )
            signature_counts[(function, arguments)] = (
                signature_counts.get((function, arguments), 0) + 1
            )
            call_id = str(call.get("tool_call_id") or "?")
            calls.append(
                ToolCallRow(
                    step_id=int(step_id) if isinstance(step_id, int) else -1,
                    tool_call_id=call_id,
                    function=function,
                    exit_code=exit_by_call.get(call_id),
                )
            )

    repeats = sorted(
        ((fn, count) for (fn, _args), count in signature_counts.items() if count > 1),
        key=lambda item: -item[1],
    )
    tail_functions = [c.function.lower() for c in calls[-5:]]
    verify = any(any(h in fn for h in _VERIFY_HINTS) for fn in tail_functions)
    return TrajectoryView(
        step_count=observed(len(steps)),
        steps=tuple(steps),
        tool_calls=tuple(calls),
        repeated_signatures=derived(tuple(repeats), "identical (function, arguments)"),
        verify_before_done=derived(
            verify if calls else None,
            "any verification-shaped tool call within the final five calls",
        ),
    )


# ---------------------------------------------------------------------------
# Trial / job assembly
# ---------------------------------------------------------------------------


def _artifact_links(trial_dir: Path) -> tuple[ArtifactLink, ...]:
    links: list[ArtifactLink] = []
    for sub in ("artifacts", "verifier", "agent"):
        base = trial_dir / sub
        if not base.is_dir():
            continue
        for item in sorted(base.rglob("*")):
            if not item.is_file():
                continue
            relative = item.relative_to(trial_dir).as_posix()
            if jail(trial_dir, relative) is None:
                continue  # path jail: hidden or escaping entries never linked
            links.append(
                ArtifactLink(
                    name=item.name,
                    relative_path=relative,
                    size_bytes=item.stat().st_size,
                )
            )
    return tuple(links)


def _trial_view(job_name: str, trial_dir: Path) -> TrialView:
    result, result_error = _load_json(trial_dir / "result.json")
    result = result or {}
    trial_name = trial_dir.name
    trial_key = f"{job_name}/{trial_name}"

    agent_info = result.get("agent_info") or {}
    exception = result.get("exception_info")
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    reward_value = rewards.get("reward")

    if result_error:
        outcome = unavailable(result_error)
        reward = unavailable(result_error)
    elif exception:
        outcome = derived("infra-exception", "exception_info present; not a score")
        reward = (
            observed(reward_value) if reward_value is not None
            else unavailable("no reward recorded (exception before verdict)")
        )
    elif reward_value is None:
        outcome = derived("no-verdict", "no exception and no reward recorded")
        reward = unavailable("no reward recorded")
    else:
        outcome = derived("pass" if float(reward_value) >= 1.0 else "reward-failure")
        reward = observed(reward_value)

    started, finished = result.get("started_at"), result.get("finished_at")
    timing = (
        observed({"started_at": started, "finished_at": finished})
        if started or finished
        else unavailable("no timestamps in result.json")
    )
    agent_result = result.get("agent_result") or {}
    cost = (
        observed(agent_result.get("cost_usd"))
        if agent_result.get("cost_usd") is not None
        else unavailable("no cost recorded (controls and subscription runs bill nothing)")
    )
    config, config_error = _load_json(trial_dir / "config.json")
    return TrialView(
        trial_key=trial_key,
        job_name=job_name,
        trial_name=trial_name,
        trial_dir=str(trial_dir),
        task_name=(
            observed(result.get("task_name")) if result.get("task_name")
            else unavailable(result_error or "task name absent")
        ),
        agent=(
            observed(agent_info.get("name")) if agent_info.get("name")
            else unavailable("agent name absent")
        ),
        model=(
            observed((agent_info.get("model_info") or {}).get("name"))
            if agent_info.get("model_info")
            else unavailable("no model recorded (controls run without one)")
        ),
        reward=reward,
        outcome_class=outcome,
        exception=(
            observed(exception) if exception else derived(None, "no exception recorded")
        ),
        timing=timing,
        cost=cost,
        config=(
            observed(redact_mapping(config)) if config
            else unavailable(config_error or "config missing")
        ),
        trajectory=_trajectory_view(trial_dir),
        artifacts=_artifact_links(trial_dir),
    )


def _is_trial_dir(path: Path) -> bool:
    return path.is_dir() and (path / "result.json").is_file()


# ---------------------------------------------------------------------------
# Analyses
# ---------------------------------------------------------------------------


def _resolve_citation(
    citation: dict[str, Any], trial: TrialView | None
) -> CitationResolution:
    path = str(citation.get("path") or "")
    step_id = citation.get("step_id")
    call_id = citation.get("tool_call_id")
    supports = str(citation.get("supports") or "")

    if trial is None:
        resolution = unavailable("cited trial not found in this index")
    else:
        trial_dir = Path(trial.trial_dir)
        jailed = jail(trial_dir, path) if path else None
        if path and jailed is None:
            resolution = unavailable(f"citation path refused (escape or hidden): {path!r}")
        elif path and jailed is not None and not jailed.is_file():
            resolution = unavailable(f"cited file does not exist: {path!r}")
        else:
            trajectory = trial.trajectory
            if step_id is not None and isinstance(trajectory, TrajectoryView):
                known_steps = {s["step_id"] for s in trajectory.steps}
                if step_id not in known_steps:
                    resolution = unavailable(f"cited step {step_id} not in trajectory")
                elif call_id is not None and call_id not in {
                    c.tool_call_id for c in trajectory.tool_calls
                }:
                    resolution = unavailable(f"cited tool call {call_id!r} not found")
                else:
                    resolution = derived("resolved", "file, step, and call verified")
            elif step_id is not None:
                resolution = unavailable("cited a step but the trajectory is unavailable")
            else:
                resolution = derived("resolved", "file verified")
    return CitationResolution(
        citation_path=path, step_id=step_id, tool_call_id=call_id,
        supports=supports, resolution=resolution,
    )


def _analysis_views(
    analyses_dir: Path, trials: dict[str, TrialView]
) -> tuple[tuple[AnalysisView, ...], tuple[str, ...]]:
    views: list[AnalysisView] = []
    notes: list[str] = []
    if not analyses_dir.is_dir():
        return (), (f"analyses: none at {analyses_dir.name}/ (cold start ok)",)
    trials_by_id = {
        str((_load_json(Path(t.trial_dir) / "result.json")[0] or {}).get("id")): t
        for t in trials.values()
    }
    for path in sorted(analyses_dir.rglob("*.json")):
        try:
            sidecar = TrialAnalysisSidecar.model_validate_json(path.read_text())
        except (OSError, ValidationError) as exc:
            notes.append(f"analysis {path.name}: unreadable ({exc.__class__.__name__})")
            continue
        trial = trials_by_id.get(str(sidecar.source_trial_id))
        citations = tuple(
            _resolve_citation(c.model_dump(), trial) for c in sidecar.output.evidence
        )
        views.append(
            AnalysisView(
                analysis_id=str(sidecar.analysis_id),
                trial_key=trial.trial_key if trial else None,
                status=observed(sidecar.validation_status),
                validity=Labeled(sidecar.output.validity, "draft"),
                category=Labeled(sidecar.output.primary_category, "draft"),
                summary=Labeled(sidecar.output.summary, "draft"),
                confidence=Labeled(sidecar.output.confidence, "draft"),
                citations=citations,
                alternatives=Labeled(tuple(sidecar.output.alternative_explanations), "draft"),
                provenance=observed(
                    redact_mapping(sidecar.analysis_provenance.model_dump(mode="json"))
                ),
            )
        )
    return tuple(views), tuple(notes)


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


def build_index(
    jobs_roots: list[Path],
    analyses_dir: Path | None = None,
) -> ExplorerIndex:
    """Assemble the full linked index. Read-only; degrades, never raises."""
    notes: list[str] = []
    trials: dict[str, TrialView] = {}
    jobs: list[JobView] = []
    seen_trial_keys: set[str] = set()

    for root in jobs_roots:
        if not root.is_dir():
            notes.append(f"jobs root unavailable: {root}")
            continue
        for job_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            job_notes: list[str] = []
            trial_keys: list[str] = []
            task_names: set[str] = set()
            for trial_dir in sorted(p for p in job_dir.iterdir() if _is_trial_dir(p)):
                view = _trial_view(job_dir.name, trial_dir)
                if view.trial_key in seen_trial_keys:
                    job_notes.append(f"duplicate trial key skipped: {view.trial_key}")
                    continue
                seen_trial_keys.add(view.trial_key)
                trials[view.trial_key] = view
                trial_keys.append(view.trial_key)
                if view.task_name.provenance == "observed":
                    task_names.add(str(view.task_name.value))
            jobs.append(
                JobView(
                    job_name=job_dir.name,
                    job_dir=str(job_dir),
                    task_names=(
                        observed(sorted(task_names)) if task_names
                        else unavailable("no readable trial results")
                    ),
                    trial_keys=tuple(trial_keys),
                    notes=tuple(job_notes),
                )
            )

    by_task: dict[str, list[str]] = {}
    controls_by_task: dict[str, set[str]] = {}
    for key, view in trials.items():
        if view.task_name.provenance != "observed":
            continue
        task = str(view.task_name.value)
        by_task.setdefault(task, []).append(key)
        if view.agent.provenance == "observed" and view.agent.value in CONTROL_AGENTS:
            controls_by_task.setdefault(task, set()).add(str(view.agent.value))
    tasks = tuple(
        TaskView(
            task_name=task,
            registration=derived(
                "evidence-only view", "registry state needs library/registry (not loaded here)"
            ),
            control_state=derived(sorted(controls_by_task.get(task, set()))),
            trial_keys=tuple(sorted(keys)),
        )
        for task, keys in sorted(by_task.items())
    )

    analyses, analysis_notes = (
        _analysis_views(analyses_dir, trials) if analyses_dir else ((), ())
    )
    notes.extend(analysis_notes)
    if not trials:
        notes.append("cold start: no readable trials; views render empty, not broken")
    return ExplorerIndex(
        tasks=tasks, jobs=tuple(jobs), trials=trials,
        analyses=analyses, notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# Next Action: copyable commands only. Nothing here executes anything.
# ---------------------------------------------------------------------------


def next_actions_for_task(task_name: str, task_path: str | None = None) -> tuple[NextAction, ...]:
    slug = task_name.rsplit("/", 1)[-1].replace("_", "-")
    path = task_path or f"<path-to>/{slug}"
    return (
        NextAction("Run the oracle control (free, local)",
                   f"uv run evallab run --task {path} --agent oracle --name {slug}-oracle"),
        NextAction("Run the nop control (free, local)",
                   f"uv run evallab run --task {path} --agent nop --name {slug}-nop"),
    )


def next_actions_for_trial(trial: TrialView) -> tuple[NextAction, ...]:
    actions = [
        NextAction("Inspect this trial's trajectory in Harbor's browser",
                   f"harbor view {trial.trial_dir}"),
        NextAction("Show the no-call stage-5 analysis plan",
                   f"uv run evallab analyze plan {trial.trial_dir}"),
    ]
    if trial.outcome_class.value == "infra-exception":
        actions.append(
            NextAction("Re-run this job's controls before drawing any conclusion",
                       f"uv run evallab status --from {Path(trial.trial_dir).parent}")
        )
    return tuple(actions)


def next_actions_for_queue() -> tuple[NextAction, ...]:
    return (
        NextAction("Submit an experiment spec (policy-gated)",
                   "uv run evallab submit <spec.json>"),
        NextAction("Approve one waiting experiment (Peter's ceilings still apply)",
                   "uv run evallab approve <spec-id> --actor peter"),
    )
