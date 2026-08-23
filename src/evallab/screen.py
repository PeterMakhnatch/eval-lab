"""Difficulty screening and follow-up generation for eval-lab (v2 §4).

Implements staged difficulty screening across ordered model capability levels:
1. Stage 1: Dispatches k=1 screen across registered tasks and ordered model levels.
2. Analysis: Classifies each task into one of five states:
   - saturated-pass (all models score 1.0; ceiling effect; stop)
   - saturated-fail (all models score 0.0; floor effect; stop)
   - separating (informative task/model pairs; low < medium / capability delta)
   - broken/error (execution errors or harness exceptions)
   - insufficient (missing or incomplete results)
3. Stage 2: Emits k=3 follow-ups ONLY for separating tasks, preserving human approval.
4. Difficulty Variant Contract: Enforces verifier preservation and authoring boundary.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import Field, field_validator, model_validator

if TYPE_CHECKING:
    from evallab.ladder import (
        DedupeRecord,
        GridGenerationResult,
    )
from evallab.power import plan_power_spec
from evallab.profiles import CONTROL_ADAPTERS, builtin_profiles
from evallab.registry import TaskNotRegisteredError, TaskRegistry
from evallab.schemas import (
    EXPLORATION_JOBS_ROOT,
    ContractModel,
    ExperimentPurpose,
    ExperimentSpec,
    PreregSpec,
)

ScreenClassification = Literal[
    "saturated-pass",
    "saturated-fail",
    "separating",
    "broken/error",
    "insufficient",
]


class ModelLevelSpec(ContractModel):
    """Specification of one model capability level in the screening ladder."""

    name: str = Field(min_length=1)
    agent: str = Field(min_length=1)
    model: str | None = None

    @classmethod
    def from_input(cls, item: Any, default_name: str | None = None) -> ModelLevelSpec:
        """Coerce strings, tuples, dicts, or existing specs into a ModelLevelSpec."""
        if isinstance(item, ModelLevelSpec):
            return item
        if isinstance(item, dict):
            name = item.get("name") or default_name or item.get("agent", "model")
            return cls(name=name, agent=item["agent"], model=item.get("model"))
        if isinstance(item, (list, tuple)) and len(item) == 2:
            level_name, target = item
            if isinstance(target, str):
                builtins = builtin_profiles()
                if target in builtins:
                    p = builtins[target]
                    return cls(name=str(level_name), agent=p.adapter, model=p.model)
                return cls(name=str(level_name), agent=target, model=None)
            if isinstance(target, dict):
                return cls(
                    name=str(level_name),
                    agent=target["agent"],
                    model=target.get("model"),
                )
        if isinstance(item, str):
            builtins = builtin_profiles()
            if item in builtins:
                p = builtins[item]
                lvl_name = default_name or item.split("-")[-1]
                return cls(name=lvl_name, agent=p.adapter, model=p.model)
            return cls(name=default_name or item, agent=item, model=None)
        raise ValueError(f"Cannot parse ModelLevelSpec from {item!r}")


DEFAULT_SCREEN_MODEL_LEVELS = [
    ModelLevelSpec(name="low", agent="antigravity-cli", model="gemini-3.7-flash-low"),
    ModelLevelSpec(
        name="medium", agent="antigravity-cli", model="gemini-3.7-flash-medium"
    ),
]


class ScreenDecisionRules(ContractModel):
    """Decision boundaries for classifying task separation."""

    min_separation_delta: float = Field(default=0.5, ge=0.0, le=1.0)
    pass_threshold: float = Field(default=1.0, ge=0.0, le=1.0)
    fail_threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    require_monotonic: bool = False


class ScreenSpec(ContractModel):
    """Specification for a multi-stage difficulty screen."""

    schema_version: Literal[1] = 1
    screen_id: str = Field(min_length=3, pattern=r"^[a-z0-9][a-z0-9-]+$")
    purpose: ExperimentPurpose = "comparison"
    tasks: list[str] = Field(min_length=1)
    model_levels: list[ModelLevelSpec] = Field(
        default_factory=lambda: list(DEFAULT_SCREEN_MODEL_LEVELS), min_length=2
    )
    initial_k: int = Field(default=1, ge=1)
    followup_k: int = Field(default=3, ge=1)
    expected_baseline: float = Field(default=0.0, ge=0.0, le=1.0)
    decision_rules: ScreenDecisionRules = Field(default_factory=ScreenDecisionRules)
    jobs_dir: str = EXPLORATION_JOBS_ROOT
    submitted_by: str = Field(default="ladder-screen", min_length=1)
    hypothesis_template: str | None = None

    @field_validator("model_levels", mode="before")
    @classmethod
    def _normalize_model_levels(cls, value: Any) -> list[ModelLevelSpec]:
        if not value:
            return list(DEFAULT_SCREEN_MODEL_LEVELS)
        if isinstance(value, dict):
            return [
                ModelLevelSpec.from_input(v, default_name=k) for k, v in value.items()
            ]
        if isinstance(value, list):
            res: list[ModelLevelSpec] = []
            for idx, item in enumerate(value):
                fallback_name = (
                    "low" if idx == 0 else ("medium" if idx == 1 else f"level-{idx}")
                )
                res.append(ModelLevelSpec.from_input(item, default_name=fallback_name))
            return res
        raise ValueError(f"Invalid model_levels structure: {value!r}")
    @field_validator("tasks")
    @classmethod
    def _tasks_are_unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("tasks must be unique within one screen cohort")
        return value

    @field_validator("model_levels")
    @classmethod
    def _model_levels_are_ordered_and_unique(
        cls, value: list[ModelLevelSpec]
    ) -> list[ModelLevelSpec]:
        names = [level.name for level in value]
        if len(names) != len(set(names)):
            raise ValueError("model level names must be unique and ordered")
        return value

    @model_validator(mode="after")
    def _screen_has_comparable_levels(self) -> ScreenSpec:
        if len(self.model_levels) < 2:
            raise ValueError("a screen requires at least two ordered model levels")
        return self


@dataclass(frozen=True)
class DifficultyVariantContract:
    """Contract for deterministic, verifier-preserving difficulty mutations.

    Invariants:
    1. Deterministic: Given a task ID and parameters, variant generation is pure and reproducible.
    2. Verifier-Preserving: Ground-truth test/verifier semantics must remain valid.
       Fake prose-only perturbations that decouple task instructions from verifier tests are
       forbidden.
    3. Authoring Boundary: Structural mutations requiring environment or verifier changes
       must pass through `evallab.authoring` and explicit candidate registration with controls.
    """

    task_id: str
    base_version: str
    verifier_preserved: bool = True
    authoring_boundary_enforced: bool = True
    contract_statement: str = (
        "Difficulty variants must preserve verifier ground-truth contracts. "
        "Mutations requiring environment or test alterations must be registered via "
        "TaskRegistry rather than synthesized as unverified prompt variations."
    )

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("difficulty variant contract requires a task_id")
        if not self.base_version.strip():
            raise ValueError("difficulty variant contract requires a base_version")
        if not self.verifier_preserved:
            raise ValueError(
                "difficulty variants must preserve verifier ground-truth contracts"
            )
        if not self.authoring_boundary_enforced:
            raise ValueError(
                "difficulty mutations requiring authoring must cross the authoring boundary"
            )


@dataclass(frozen=True)
class TaskScreenResult:
    """Screening outcome and classification for a single task."""

    task_id: str
    classification: ScreenClassification
    reason: str
    level_scores: dict[str, float | None]
    level_errors: dict[str, str | None]
    trial_counts: dict[str, int]
    selected_for_followup: bool
    followup_reason: str


@dataclass
class ScreenAnalysisReport:
    """Comprehensive report for a difficulty screening analysis."""

    screen_id: str
    stage: int
    total_tasks: int
    classifications: dict[ScreenClassification, int]
    tasks: list[TaskScreenResult]
    separating_tasks: list[str]
    stopped_tasks: list[str]

    def summary(self) -> str:
        """Render a readable summary table explaining task selection or stopping."""
        lines = [
            f"Screen Analysis: {self.screen_id} (Stage {self.stage})",
            f"Total tasks evaluated: {self.total_tasks}",
            "Classifications: "
            + ", ".join(f"{k}: {v}" for k, v in sorted(self.classifications.items())),
            "",
            "TASK EVALUATION BREAKDOWN:",
        ]
        for t in self.tasks:
            status_tag = (
                "SELECTED (k=3 follow-up)" if t.selected_for_followup else "STOPPED"
            )
            lines.append(f"  * {t.task_id}: [{t.classification}] -> {status_tag}")
            lines.append(f"    Reason: {t.reason}")
            scores_str = ", ".join(
                f"{lvl}={score:.2f}" if score is not None else f"{lvl}=N/A"
                for lvl, score in t.level_scores.items()
            )
            lines.append(f"    Scores: ({scores_str}) | Action: {t.followup_reason}")
            if any(t.level_errors.values()):
                errs = [
                    f"{lvl}: {err}" for lvl, err in t.level_errors.items() if err
                ]
                lines.append(f"    Errors: {'; '.join(errs)}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Convert report to JSON-serializable dictionary."""
        return {
            "screen_id": self.screen_id,
            "stage": self.stage,
            "total_tasks": self.total_tasks,
            "classifications": self.classifications,
            "separating_tasks": self.separating_tasks,
            "stopped_tasks": self.stopped_tasks,
            "tasks": [
                {
                    "task_id": t.task_id,
                    "classification": t.classification,
                    "reason": t.reason,
                    "level_scores": t.level_scores,
                    "level_errors": t.level_errors,
                    "trial_counts": t.trial_counts,
                    "selected_for_followup": t.selected_for_followup,
                    "followup_reason": t.followup_reason,
                }
                for t in self.tasks
            ],
        }


def load_screen_spec(path_or_data: Path | str | dict[str, Any]) -> ScreenSpec:
    """Load and validate a ScreenSpec from a path or dict."""
    if isinstance(path_or_data, dict):
        return ScreenSpec.model_validate(path_or_data)
    path = Path(path_or_data)
    if not path.is_file():
        raise FileNotFoundError(f"Screen specification file not found: {path}")
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) if path.suffix in (".yaml", ".yml") else json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"Screen specification in {path} must be a dictionary")
    return ScreenSpec.model_validate(data)



def generate_stage1_screen(
    spec: ScreenSpec | Path | str | dict[str, Any],
    *,
    repo_root: Path | None = None,
    output_dir: Path | str | None = None,
    submit: bool = False,
    dry_run: bool = False,
) -> GridGenerationResult:
    """Generate Stage 1 screening specs (k=1) across tasks and model levels.

    Validates all tasks against TaskRegistry and preserves human approval.
    """
    screen_spec = load_screen_spec(spec) if not isinstance(spec, ScreenSpec) else spec
    from evallab.ladder import (
        DedupeRecord,
        GridGenerationResult,
        ProviderStats,
        _point_id,
        find_existing_grid_points,
        generate_spec_name,
        sanitize_slug,
    )

    root = (repo_root or Path.cwd()).resolve()

    # 1. Validate all tasks in TaskRegistry
    registry = TaskRegistry.from_repo(root)
    records: dict[str, Any] = {}
    for task_id in screen_spec.tasks:
        record = registry.get(task_id)
        if record is None or record.state != "registered":
            raise TaskNotRegisteredError(
                f"Task {task_id!r} is not registered in task registry (required for screening)"
            )
        records[task_id] = record

    # 2. Check existing points for deduplication and cohort isolation
    extra_dirs = [Path(output_dir)] if output_dir else []
    existing_points = find_existing_grid_points(
        screen_spec.screen_id, repo_root=root, extra_dirs=extra_dirs
    )

    specs: list[ExperimentSpec] = []
    deduped: list[DedupeRecord] = []
    written_paths: list[Path] = []
    total_trials = 0
    total_cost = 0.0

    power_plan = plan_power_spec(
        n_tasks=len(screen_spec.tasks),
        k=screen_spec.initial_k,
        baseline=screen_spec.expected_baseline,
    )

    out_target: Path | None = None
    if not dry_run:
        if output_dir:
            out_target = Path(output_dir).resolve()
            out_target.mkdir(parents=True, exist_ok=True)
        elif submit:
            out_target = root / "queue" / "proposed"
            out_target.mkdir(parents=True, exist_ok=True)

    for task_id in screen_spec.tasks:
        record = records[task_id]
        for level in screen_spec.model_levels:
            agent_key = (
                f"{level.agent}-{level.model}"
                if level.model and level.agent not in CONTROL_ADAPTERS
                else level.agent
            )

            point_id = _point_id(
                task_id,
                agent_key,
                "none",
                screen_spec.initial_k,
                arm_id=None,
                factor_values={},
            )
            fallback_point_id = _point_id(
                task_id,
                level.agent,
                "none",
                screen_spec.initial_k,
                arm_id=None,
                factor_values={},
            )
            if point_id in existing_points or fallback_point_id in existing_points:
                deduped.append(
                    DedupeRecord(
                        grid_id=screen_spec.screen_id,
                        task=task_id,
                        agent=agent_key,
                        preamble="none",
                        attempts=screen_spec.initial_k,
                    )
                )
                continue

            spec_name = generate_spec_name(
                screen_spec.screen_id,
                sanitize_slug(task_id),
                sanitize_slug(agent_key),
                "none",
                screen_spec.initial_k,
            )

            grid_point = {
                "screen_id": screen_spec.screen_id,
                "stage": 1,
                "task": task_id,
                "model_level": level.name,
                "agent": level.agent,
                "model": level.model,
                "k": screen_spec.initial_k,
                "point_id": point_id,
            }

            if screen_spec.hypothesis_template:
                hypothesis = screen_spec.hypothesis_template.format(
                    task=task_id,
                    agent=agent_key,
                    level=level.name,
                    k=screen_spec.initial_k,
                )
            else:
                hypothesis = (
                    f"Difficulty screen Stage 1: testing {agent_key} ({level.name}) "
                    f"on {task_id} at k={screen_spec.initial_k}"
                )

            prereg = PreregSpec(
                expected=f"Difficulty screening stage 1 at k={screen_spec.initial_k}",
                decision_rule=(
                    f"Classify separation across ordered capability levels with delta >= "
                    f"{screen_spec.decision_rules.min_separation_delta}; "
                    f"emit k={screen_spec.followup_k} follow-up for separating tasks"
                ),
            )

            exp_spec = ExperimentSpec(
                name=spec_name,
                hypothesis=hypothesis,
                purpose=screen_spec.purpose,
                task=task_id,
                task_path=record.task_path,
                task_version=record.version,
                verifier_digest=record.digests.verifier,
                agent=level.agent,
                model=level.model,
                attempts=screen_spec.initial_k,
                jobs_dir=screen_spec.jobs_dir,
                submitted_by=screen_spec.submitted_by,
                grid_id=screen_spec.screen_id,
                grid_point=grid_point,
                prereg=prereg,
                power=power_plan,
            )

            specs.append(exp_spec)
            total_trials += screen_spec.initial_k

            if out_target is not None:
                dest_file = out_target / f"{spec_name}.json"
                if dest_file.exists():
                    # Deduplication fallback
                    deduped.append(
                        DedupeRecord(
                            grid_id=screen_spec.screen_id,
                            task=task_id,
                            agent=agent_key,
                            preamble="none",
                            attempts=screen_spec.initial_k,
                        )
                    )
                else:
                    dest_file.write_text(
                        json.dumps(
                            exp_spec.model_dump(mode="json", exclude_none=True),
                            indent=2,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    written_paths.append(dest_file)

    return GridGenerationResult(
        grid_id=screen_spec.screen_id,
        specs=specs,
        submitted_specs=[s.name for s in specs] if submit and not dry_run else [],
        written_paths=written_paths,
        skipped=[],
        deduped=deduped,
        total_specs=len(specs),
        total_trials=total_trials,
        total_estimated_cost_usd=total_cost,
        by_provider={
            "screen": ProviderStats(
                provider="screen",
                specs_count=len(specs),
                trials_count=total_trials,
            )
        },
    )


def _find_trial_results_for_screen(
    screen_id: str,
    root: Path,
    search_dirs: Sequence[Path] | None = None,
) -> list[dict[str, Any]]:
    """Discover trial result records matching screen_id from job directories."""
    discovered: list[dict[str, Any]] = []
    roots_to_search: list[Path] = (
        list(search_dirs)
        if search_dirs
        else [
            root / "runs",
            root / "research/evidence/runs",
            root / "evidence/runs",
        ]
    )

    seen_trial_dirs: set[Path] = set()

    for r in roots_to_search:
        if not r.is_dir():
            continue
        for res_path in r.rglob("result.json"):
            trial_dir = res_path.parent
            if trial_dir in seen_trial_dirs:
                continue
            seen_trial_dirs.add(trial_dir)

            try:
                data = json.loads(res_path.read_text(encoding="utf-8"))
            except Exception:
                continue

            if not isinstance(data, dict):
                continue

            # Check if this trial or job belongs to this screen_id
            spec_data: dict[str, Any] = {}
            for spec_file_name in (
                "spec.json",
                "config.json",
                "manifest.json",
                "lab-metadata.json",
            ):
                cand = trial_dir / spec_file_name
                if not cand.is_file():
                    cand = trial_dir.parent / spec_file_name
                if cand.is_file():
                    try:
                        loaded = json.loads(cand.read_text(encoding="utf-8"))
                        if isinstance(loaded, dict):
                            if spec_file_name == "lab-metadata.json":
                                loaded = loaded.get("experiment") or {}
                            if isinstance(loaded, dict):
                                spec_data = loaded
                                break
                    except Exception:
                        pass

            grid_id = (
                spec_data.get("grid_id")
                or (spec_data.get("grid_point") or {}).get("screen_id")
                or data.get("grid_id")
                or data.get("screen_id")
            )
            if grid_id != screen_id:
                continue
            # Extract task, agent, model, level, reward, error
            grid_point = spec_data.get("grid_point") or {}
            task_name = (
                grid_point.get("task")
                or spec_data.get("task")
                or data.get("task_name")
                or data.get("task")
            )
            agent_name = (
                grid_point.get("agent")
                or spec_data.get("agent")
                or data.get("agent_name")
                or data.get("agent")
            )
            model_name = (
                grid_point.get("model")
                or spec_data.get("model")
                or data.get("model_name")
                or data.get("model")
            )
            model_level = grid_point.get("model_level")

            # Determine reward from trial result or catalog-style fact fields.
            reward: float | None = None
            verifier_res = data.get("verifier_result") or {}
            rewards_dict = verifier_res.get("rewards") or {}
            candidates = (
                verifier_res.get("reward"),
                rewards_dict.get("reward"),
                data.get("reward"),
                data.get("primary_reward"),
                data.get("reward_value"),
            )
            for candidate in candidates:
                if isinstance(candidate, (int, float)):
                    reward = float(candidate)
                    break
            if reward is None and isinstance(data.get("stats"), dict):
                evals = data["stats"].get("evals") or {}
                for rep in evals.values():
                    if isinstance(rep, dict) and isinstance(rep.get("reward"), (int, float)):
                        reward = float(rep["reward"])
                        break

            # Determine execution/harness error separately from reward failure.
            error_msg: str | None = None
            for error_key in ("exception", "exception_class", "exception_type", "error"):
                if data.get(error_key):
                    error_msg = str(data[error_key])
                    break
            if error_msg is None and (trial_dir / "exception.txt").is_file():
                error_msg = (
                    (trial_dir / "exception.txt").read_text(encoding="utf-8").strip()
                )
            if (
                error_msg is None
                and data.get("status") in ("failed", "error")
                and reward is None
            ):
                error_msg = f"Trial failed with status {data.get('status')}"

            discovered.append(
                {
                    "trial_dir": trial_dir,
                    "screen_id": grid_id or screen_id,
                    "task": task_name,
                    "agent": agent_name,
                    "model": model_name,
                    "model_level": model_level,
                    "stage": grid_point.get("stage", 1),
                    "reward": reward,
                    "error": error_msg,
                }
            )

    return discovered


def analyze_screen_results(
    screen_id: str,
    spec: ScreenSpec | Path | str | dict[str, Any] | None = None,
    *,
    repo_root: Path | None = None,
    jobs_dir: Path | str | None = None,
    trial_records: list[dict[str, Any]] | None = None,
) -> ScreenAnalysisReport:
    """Analyze completed Stage 1 results and classify each task.

    Classification States:
    - saturated-pass: all models score 1.0 (ceiling effect, stop)
    - saturated-fail: all models score 0.0 (floor effect, stop)
    - separating: informative task/model pair (low < medium / delta >= min_separation_delta)
    - broken/error: trial encountered execution or harness errors
    - insufficient: results are missing or incomplete across model levels
    """
    root = (repo_root or Path.cwd()).resolve()
    screen_spec: ScreenSpec | None = None
    if spec is not None:
        screen_spec = (
            load_screen_spec(spec) if not isinstance(spec, ScreenSpec) else spec
        )

    # Resolve search directories
    search_dirs = [Path(jobs_dir).resolve()] if jobs_dir else None
    discovered_trials = (
        trial_records
        if trial_records is not None
        else _find_trial_results_for_screen(screen_id, root, search_dirs=search_dirs)
    )

    tasks_to_evaluate: list[str] = (
        screen_spec.tasks
        if screen_spec
        else sorted({str(d["task"]) for d in discovered_trials if d.get("task")})
    )
    model_levels: list[ModelLevelSpec] = (
        screen_spec.model_levels if screen_spec else list(DEFAULT_SCREEN_MODEL_LEVELS)
    )
    rules: ScreenDecisionRules = (
        screen_spec.decision_rules if screen_spec else ScreenDecisionRules()
    )

    task_results: list[TaskScreenResult] = []
    classifications_count: dict[ScreenClassification, int] = {
        "saturated-pass": 0,
        "saturated-fail": 0,
        "separating": 0,
        "broken/error": 0,
        "insufficient": 0,
    }
    separating_tasks: list[str] = []
    stopped_tasks: list[str] = []

    for task_id in tasks_to_evaluate:
        level_scores: dict[str, float | None] = {}
        level_errors: dict[str, str | None] = {}
        trial_counts: dict[str, int] = {}

        # Collect results for each model level
        for lvl in model_levels:
            lvl_name = lvl.name
            matching = [
                d
                for d in discovered_trials
                if (
                    d.get("screen_id") == screen_id
                    or d.get("grid_id") == screen_id
                    or d.get("cohort_id") == screen_id
                )
                and int(d.get("stage", 1) or 1) == 1
                and str(d.get("task") or d.get("task_name")) == task_id
                and (
                    (
                        d.get("model_level") is not None
                        and d.get("model_level") == lvl_name
                    )
                    or (
                        d.get("model_level") is None
                        and (d.get("agent") or d.get("agent_name")) in (None, lvl.agent)
                        and (
                            lvl.model is None
                            or (d.get("model") or d.get("model_name")) == lvl.model
                        )
                    )
                )
            ]

            trial_counts[lvl_name] = len(matching)
            if not matching:
                level_scores[lvl_name] = None
                level_errors[lvl_name] = None
                continue

            # Check for errors in any trial of this level.
            errors = [
                str(d.get("error") or d.get("exception_class") or d.get("exception_type"))
                for d in matching
                if d.get("error") or d.get("exception_class") or d.get("exception_type")
            ]
            if errors:
                level_errors[lvl_name] = "; ".join(errors)
            else:
                level_errors[lvl_name] = None

            # Calculate average reward from result or catalog fact fields.
            valid_rewards: list[float] = []
            for record in matching:
                value = record.get("reward")
                if value is None:
                    value = record.get("primary_reward", record.get("reward_value"))
                if isinstance(value, (int, float)):
                    valid_rewards.append(float(value))
            level_scores[lvl_name] = (
                sum(valid_rewards) / len(valid_rewards) if valid_rewards else None
            )

        # Determine Classification
        classification: ScreenClassification
        reason: str
        selected_for_followup: bool
        followup_reason: str

        # 1. Check for errors
        has_errors = any(err is not None for err in level_errors.values())
        if has_errors:
            classification = "broken/error"
            err_details = [f"{lvl}: {err}" for lvl, err in level_errors.items() if err]
            reason = f"Execution error observed: {'; '.join(err_details)}"
            selected_for_followup = False
            followup_reason = (
                "Stopped: execution error / harness exception; fix task before follow-up"
            )
        # 2. Check for missing/insufficient results
        elif any(score is None for score in level_scores.values()):
            missing = [lvl for lvl, score in level_scores.items() if score is None]
            classification = "insufficient"
            reason = f"Missing trial results for levels: {', '.join(missing)}"
            selected_for_followup = False
            followup_reason = (
                "Stopped: incomplete stage 1 results; run remaining levels first"
            )
        else:
            # All levels have valid float scores
            scores_list = [score for score in level_scores.values() if score is not None]
            scores_repr = ", ".join(
                f"{lvl}={score:.2f}" for lvl, score in level_scores.items()
            )

            # 3. Check for all-pass saturation (e.g. event-summary scoring 1.0 on Low/Med/High)
            if all(s >= rules.pass_threshold for s in scores_list):
                classification = "saturated-pass"
                reason = (
                    f"Ceiling saturation ({scores_repr}): all models pass; "
                    f"task is too easy for these models"
                )
                selected_for_followup = False
                followup_reason = (
                    "Stopped: saturated-pass ceiling effect; no stage 2 follow-up needed"
                )
            # 4. Check for all-fail saturation (all models score 0.0)
            elif all(s <= rules.fail_threshold for s in scores_list):
                classification = "saturated-fail"
                reason = (
                    f"Floor saturation ({scores_repr}): all models fail; "
                    f"task is too hard or broken"
                )
                selected_for_followup = False
                followup_reason = (
                    "Stopped: saturated-fail floor effect; no stage 2 follow-up needed"
                )
            else:
                # Calculate ordered separation spread. The configured ordering is
                # provenance, not a license to pool unrelated model results.
                ordered_scores = list(level_scores.values())
                min_score = min(scores_list)
                max_score = max(scores_list)
                delta = max_score - min_score
                non_monotonic = any(
                    left > right
                    for left, right in zip(ordered_scores, ordered_scores[1:], strict=False)
                    if left is not None and right is not None
                )

                if rules.require_monotonic and non_monotonic:
                    classification = "insufficient"
                    reason = (
                        f"Non-monotonic ordered scores ({scores_repr}); "
                        "decision rule requires monotonic capability ordering"
                    )
                    selected_for_followup = False
                    followup_reason = (
                        "Stopped: non-monotonic stage 1 results; inspect model-level provenance"
                    )
                elif delta >= rules.min_separation_delta:
                    classification = "separating"
                    reason = (
                        f"Separation observed ({scores_repr}, "
                        f"delta={delta:.2f} >= {rules.min_separation_delta:.2f})"
                    )
                    selected_for_followup = True
                    k_val = screen_spec.followup_k if screen_spec else 3
                    followup_reason = f"Selected for Stage 2 follow-up (k={k_val})"
                else:
                    classification = "insufficient"
                    reason = (
                        f"Low separation ({scores_repr}, "
                        f"delta={delta:.2f} < {rules.min_separation_delta:.2f})"
                    )
                    selected_for_followup = False
                    followup_reason = "Stopped: insufficient capability separation"

        classifications_count[classification] += 1
        if selected_for_followup:
            separating_tasks.append(task_id)
        else:
            stopped_tasks.append(task_id)

        task_results.append(
            TaskScreenResult(
                task_id=task_id,
                classification=classification,
                reason=reason,
                level_scores=level_scores,
                level_errors=level_errors,
                trial_counts=trial_counts,
                selected_for_followup=selected_for_followup,
                followup_reason=followup_reason,
            )
        )

    return ScreenAnalysisReport(
        screen_id=screen_id,
        stage=1,
        total_tasks=len(tasks_to_evaluate),
        classifications=classifications_count,
        tasks=task_results,
        separating_tasks=separating_tasks,
        stopped_tasks=stopped_tasks,
    )


def generate_stage2_screen(
    analysis: ScreenAnalysisReport,
    spec: ScreenSpec | Path | str | dict[str, Any],
    *,
    repo_root: Path | None = None,
    output_dir: Path | str | None = None,
    submit: bool = False,
    dry_run: bool = False,
) -> GridGenerationResult:
    """Generate Stage 2 follow-up specs (k=3) ONLY for separating tasks.

    Preserves human approval and avoids automatic paid dispatch.
    """
    screen_spec = load_screen_spec(spec) if not isinstance(spec, ScreenSpec) else spec
    root = (repo_root or Path.cwd()).resolve()

    from evallab.ladder import (
        DedupeRecord,
        GridGenerationResult,
        ProviderStats,
        _point_id,
        find_existing_grid_points,
        generate_spec_name,
        sanitize_slug,
    )

    if not analysis.separating_tasks:
        return GridGenerationResult(
            grid_id=screen_spec.screen_id,
            specs=[],
            submitted_specs=[],
            written_paths=[],
            skipped=[],
            deduped=[],
            total_specs=0,
            total_trials=0,
            total_estimated_cost_usd=0.0,
            by_provider={
                "screen": ProviderStats(
                    provider="screen",
                    specs_count=0,
                    trials_count=0,
                )
            },
        )

    registry = TaskRegistry.from_repo(root)
    records: dict[str, Any] = {}
    for task_id in analysis.separating_tasks:
        record = registry.get(task_id)
        if record is None or record.state != "registered":
            raise TaskNotRegisteredError(
                f"Separating task {task_id!r} is not registered in task registry"
            )
        records[task_id] = record

    extra_dirs = [Path(output_dir)] if output_dir else []
    existing_points = find_existing_grid_points(
        screen_spec.screen_id, repo_root=root, extra_dirs=extra_dirs
    )

    specs: list[ExperimentSpec] = []
    deduped: list[DedupeRecord] = []
    written_paths: list[Path] = []
    total_trials = 0

    power_plan = plan_power_spec(
        n_tasks=len(analysis.separating_tasks),
        k=screen_spec.followup_k,
        baseline=screen_spec.expected_baseline,
    )

    out_target: Path | None = None
    if not dry_run:
        if output_dir:
            out_target = Path(output_dir).resolve()
            out_target.mkdir(parents=True, exist_ok=True)
        elif submit:
            out_target = root / "queue" / "proposed"
            out_target.mkdir(parents=True, exist_ok=True)

    for task_id in analysis.separating_tasks:
        record = records[task_id]
        for level in screen_spec.model_levels:
            agent_key = (
                f"{level.agent}-{level.model}"
                if level.model and level.agent not in CONTROL_ADAPTERS
                else level.agent
            )

            point_id = _point_id(
                task_id,
                agent_key,
                "none",
                screen_spec.followup_k,
                arm_id=None,
                factor_values={},
            )
            fallback_point_id = _point_id(
                task_id,
                level.agent,
                "none",
                screen_spec.followup_k,
                arm_id=None,
                factor_values={},
            )
            if point_id in existing_points or fallback_point_id in existing_points:
                deduped.append(
                    DedupeRecord(
                        grid_id=screen_spec.screen_id,
                        task=task_id,
                        agent=agent_key,
                        preamble="none",
                        attempts=screen_spec.followup_k,
                    )
                )
                continue

            spec_name = generate_spec_name(
                screen_spec.screen_id,
                sanitize_slug(task_id),
                sanitize_slug(agent_key),
                "none",
                screen_spec.followup_k,
            )

            grid_point = {
                "screen_id": screen_spec.screen_id,
                "stage": 2,
                "task": task_id,
                "model_level": level.name,
                "agent": level.agent,
                "model": level.model,
                "k": screen_spec.followup_k,
                "point_id": point_id,
            }

            hypothesis = (
                f"Difficulty screen Stage 2 follow-up: {agent_key} ({level.name}) "
                f"on separating task {task_id} at k={screen_spec.followup_k}"
            )

            prereg = PreregSpec(
                expected=f"Difficulty follow-up stage 2 at k={screen_spec.followup_k}",
                decision_rule=(
                    f"Characterize pass@{screen_spec.followup_k} distribution "
                    f"for separating task {task_id}"
                ),
            )

            exp_spec = ExperimentSpec(
                name=spec_name,
                hypothesis=hypothesis,
                purpose=screen_spec.purpose,
                task=task_id,
                task_path=record.task_path,
                task_version=record.version,
                verifier_digest=record.digests.verifier,
                agent=level.agent,
                model=level.model,
                attempts=screen_spec.followup_k,
                jobs_dir=screen_spec.jobs_dir,
                submitted_by=screen_spec.submitted_by,
                grid_id=screen_spec.screen_id,
                grid_point=grid_point,
                prereg=prereg,
                power=power_plan,
            )

            specs.append(exp_spec)
            total_trials += screen_spec.followup_k

            if out_target is not None:
                dest_file = out_target / f"{spec_name}.json"
                if dest_file.exists():
                    deduped.append(
                        DedupeRecord(
                            grid_id=screen_spec.screen_id,
                            task=task_id,
                            agent=agent_key,
                            preamble="none",
                            attempts=screen_spec.followup_k,
                        )
                    )
                else:
                    dest_file.write_text(
                        json.dumps(
                            exp_spec.model_dump(mode="json", exclude_none=True),
                            indent=2,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    written_paths.append(dest_file)

    return GridGenerationResult(
        grid_id=screen_spec.screen_id,
        specs=specs,
        submitted_specs=[s.name for s in specs] if submit and not dry_run else [],
        written_paths=written_paths,
        skipped=[],
        deduped=deduped,
        total_specs=len(specs),
        total_trials=total_trials,
        total_estimated_cost_usd=0.0,
        by_provider={
            "screen": ProviderStats(
                provider="screen",
                specs_count=len(specs),
                trials_count=total_trials,
            )
        },
    )
