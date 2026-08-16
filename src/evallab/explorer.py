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
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from evallab.registry import TaskRegistry
from evallab.schemas import ANALYSIS_SIDECAR_FILENAME, TrialAnalysisSidecar

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
        elif isinstance(value, list):
            clean[key] = [
                redact_mapping(item) if isinstance(item, dict) else item
                for item in value
            ]
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
    jobs_root: str
    status_root: str
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


def _status_root_for_jobs_root(jobs_root: Path) -> Path:
    """Return the root accepted by ``evallab status --from`` for a jobs root."""
    resolved = jobs_root.resolve()
    if (
        resolved.name == "runs"
        and resolved.parent.name == "evidence"
        and resolved.parent.parent.name == "research"
    ):
        return resolved.parents[2]
    return resolved.parent


def _as_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _trial_view(job_name: str, trial_dir: Path, jobs_root: Path) -> TrialView:
    result, result_error = _load_json(trial_dir / "result.json")
    result = result or {}
    trial_name = trial_dir.name
    trial_key = f"{job_name}/{trial_name}"

    agent_info = _as_mapping(result.get("agent_info"))
    exception = result.get("exception_info")
    verifier_result = _as_mapping(result.get("verifier_result"))
    rewards = _as_mapping(verifier_result.get("rewards"))
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
        try:
            numeric_reward = float(reward_value)
        except (TypeError, ValueError):
            outcome = unavailable("reward is not numeric")
            reward = unavailable("reward is not numeric")
        else:
            outcome = derived("pass" if numeric_reward >= 1.0 else "reward-failure")
            reward = observed(reward_value)

    started, finished = result.get("started_at"), result.get("finished_at")
    timing = (
        observed({"started_at": started, "finished_at": finished})
        if started or finished
        else unavailable("no timestamps in result.json")
    )
    agent_result = _as_mapping(result.get("agent_result"))
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
        jobs_root=str(jobs_root.resolve()),
        status_root=str(_status_root_for_jobs_root(jobs_root)),
        task_name=(
            observed(result.get("task_name")) if result.get("task_name")
            else unavailable(result_error or "task name absent")
        ),
        agent=(
            observed(agent_info.get("name")) if agent_info.get("name")
            else unavailable("agent name absent")
        ),
        model=(
            observed(_as_mapping(agent_info.get("model_info")).get("name"))
            if _as_mapping(agent_info.get("model_info")).get("name")
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
            observed(redact_mapping(config)) if config is not None
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
                elif call_id is not None and not any(
                    c.step_id == step_id and c.tool_call_id == call_id
                    for c in trajectory.tool_calls
                ):
                    resolution = unavailable(
                        f"cited tool call {call_id!r} not found in step {step_id}"
                    )
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


def _analyses_relative(path: Path, analyses_dir: Path) -> str:
    """Name a sidecar by its analysis directory; every file is ``analysis.json``."""
    try:
        return path.relative_to(analyses_dir).as_posix()
    except ValueError:
        return path.name


def _analysis_views(
    analyses_dir: Path, trials: dict[str, TrialView]
) -> tuple[tuple[AnalysisView, ...], tuple[str, ...]]:
    views: list[AnalysisView] = []
    notes: list[str] = []
    if not analyses_dir.is_dir():
        return (), (f"analyses: none at {analyses_dir.name}/ (cold start ok)",)
    trials_by_id: dict[str, TrialView] = {}
    duplicate_trial_ids: set[str] = set()
    for trial in trials.values():
        result = _load_json(Path(trial.trial_dir) / "result.json")[0] or {}
        trial_id = result.get("id")
        if not trial_id:
            continue
        key = str(trial_id)
        if key in trials_by_id:
            duplicate_trial_ids.add(key)
        else:
            trials_by_id[key] = trial
    # Positive discovery: a sidecar is the file named ``analysis.json``. Any
    # other JSON under the destination root — reviews written by
    # ``evallab analyze review``, and whatever artifact type comes next — is
    # not a sidecar and must not be parsed as one (M009 F-03).
    for path in sorted(analyses_dir.rglob(ANALYSIS_SIDECAR_FILENAME)):
        try:
            sidecar = TrialAnalysisSidecar.model_validate_json(path.read_text())
        except (OSError, ValidationError) as exc:
            notes.append(
                f"analysis {_analyses_relative(path, analyses_dir)}: "
                f"unreadable ({exc.__class__.__name__})"
            )
            continue
        source_trial_id = str(sidecar.source_trial_id)
        trial = (
            None
            if source_trial_id in duplicate_trial_ids
            else trials_by_id.get(source_trial_id)
        )
        if source_trial_id in duplicate_trial_ids:
            notes.append(
                f"analysis {sidecar.analysis_id}: source trial id "
                f"{source_trial_id} is duplicated; analysis left unlinked"
            )
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
    registry_dir: Path | None = None,
) -> ExplorerIndex:
    """Assemble the full linked index. Read-only; degrades, never raises."""
    notes: list[str] = []
    trials: dict[str, TrialView] = {}
    jobs: list[JobView] = []
    seen_trial_keys: set[str] = set()
    registry = None
    if registry_dir is not None:
        try:
            registry = TaskRegistry.from_dir(registry_dir)
        except ValueError as exc:
            notes.append(f"registry unavailable: {exc}")

    for root in jobs_roots:
        if not root.is_dir():
            notes.append(f"jobs root unavailable: {root}")
            continue
        for job_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            job_notes: list[str] = []
            trial_keys: list[str] = []
            task_names: set[str] = set()
            for trial_dir in sorted(p for p in job_dir.iterdir() if _is_trial_dir(p)):
                view = _trial_view(job_dir.name, trial_dir, root)
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
            registration=(
                observed(registry.records[task].state)
                if registry is not None and task in registry.records
                else observed("not registered")
                if registry is not None
                else unavailable("registry not configured for this explorer root")
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


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if len(slug) < 3:
        slug = f"task-{slug or 'unknown'}"
    return slug[:60].rstrip("-")


def next_actions_for_task(task_name: str, task_path: str | None = None) -> tuple[NextAction, ...]:
    slug = _safe_slug(task_name.rsplit("/", 1)[-1])
    path = task_path or f"path/to/{slug}"
    quoted_path = shlex.quote(path)
    return (
        NextAction("Run the oracle control (free, local)",
                   f"uv run evallab run --task {quoted_path} --agent oracle "
                   f"--name {slug}-oracle"),
        NextAction("Run the nop control (free, local)",
                   f"uv run evallab run --task {quoted_path} --agent nop "
                   f"--name {slug}-nop"),
    )


def next_actions_for_trial(trial: TrialView) -> tuple[NextAction, ...]:
    actions = [
        NextAction("Open Harbor's viewer for this trial's jobs root",
                   f"harbor view {shlex.quote(trial.jobs_root)} --jobs"),
        NextAction("Show the no-call stage-5 analysis plan",
                   f"uv run evallab analyze plan {shlex.quote(trial.trial_dir)}"),
    ]
    if trial.outcome_class.value == "infra-exception":
        actions.append(
            NextAction("Re-run this job's controls before drawing any conclusion",
                       f"uv run evallab status --from {shlex.quote(trial.status_root)}")
        )
    return tuple(actions)


def next_actions_for_queue() -> tuple[NextAction, ...]:
    return (
        NextAction("Submit an experiment spec (policy-gated)",
                   "uv run evallab submit path/to/spec.json"),
        NextAction("Approve one waiting experiment (Peter's ceilings still apply)",
                   "uv run evallab approve SPEC_ID --actor peter"),
    )
