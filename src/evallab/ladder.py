"""LADDER: Evaluation grid generator (v2 §4).

Expands declared grid specifications (axes: task_refs x agents x preamble x k)
minus constraints into valid, purpose-tagged ExperimentSpec files.
Round-robins across providers under quota, respects daily budget units, and
records grid_id + coordinates on each spec so partially-run grids resume
instead of duplicating.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from evallab.profiles import CONTROL_ADAPTERS, builtin_profiles
from evallab.queue import Executor
from evallab.quota import (
    PAID_AGENTS,
    Headroom,
    default_roots,
    load_quota_report,
)
from evallab.schemas import (
    AgentSpec,
    ExperimentSpec,
    GridAxes,
    GridLimits,
    GridSpec,
    LadderGridSpec,
    ProviderLimit,
    TaskSpec,
)

# Re-export schema models for callers
__all__ = [
    "AgentSpec",
    "CandidatePoint",
    "DedupeRecord",
    "GridAxes",
    "GridGenerationResult",
    "GridLimits",
    "GridSpec",
    "LadderGridSpec",
    "ProviderLimit",
    "ProviderStats",
    "SkippedSpec",
    "TaskSpec",
    "build_arg_parser",
    "find_existing_grid_points",
    "generate_grid",
    "generate_spec_name",
    "load_grid_spec",
    "main",
    "sanitize_slug",
]

#: Default estimated cost per trial in USD when unspecified.
DEFAULT_COST_PER_TRIAL_USD: dict[str, float] = {
    "oracle": 0.0,
    "nop": 0.0,
    "codex": 0.05,
    "claude-code": 0.05,
    "gemini-cli": 0.05,
    "grok-cli": 0.05,
}

_SLUG_SANITIZE_RE = re.compile(r"[^a-z0-9-]+")
_CONSECUTIVE_HYPHENS_RE = re.compile(r"-+")
_KNOWN_EXTENSIONS = (".md", ".yaml", ".yml", ".json", ".toml", ".txt", ".py")


def sanitize_slug(value: str) -> str:
    """Normalize a string into a clean lowercase hyphen-separated slug preserving path segments."""
    cleaned_val = value.strip()
    for ext in _KNOWN_EXTENSIONS:
        if cleaned_val.lower().endswith(ext):
            cleaned_val = cleaned_val[: -len(ext)]
            break
    lowered = cleaned_val.lower()
    cleaned = _SLUG_SANITIZE_RE.sub("-", lowered)
    collapsed = _CONSECUTIVE_HYPHENS_RE.sub("-", cleaned).strip("-")
    return collapsed or "item"


def generate_spec_name(
    grid_name: str,
    task_slug: str,
    agent_slug: str,
    preamble_slug: str,
    attempts: int,
    max_len: int = 80,
) -> str:
    """Build a deterministic, valid ExperimentSpec name (^[a-z0-9][a-z0-9-]+$, len <= 80)."""
    grid_clean = sanitize_slug(grid_name)
    parts = [grid_clean, task_slug, agent_slug]
    if preamble_slug and preamble_slug != "none":
        parts.append(preamble_slug)
    parts.append(f"k{attempts}")

    candidate = "-".join(p for p in parts if p)
    candidate = _CONSECUTIVE_HYPHENS_RE.sub("-", candidate).strip("-")
    if not candidate or not candidate[0].isalnum():
        candidate = f"ladder-{candidate}".strip("-")

    if len(candidate) <= max_len:
        return candidate

    # Truncate while keeping structure, collision resistance via hash, and valid suffix
    k_suffix = f"-k{attempts}"
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:8]
    hash_suffix = f"-{digest}{k_suffix}"
    budget = max_len - len(hash_suffix)
    truncated = candidate[:budget].rstrip("-")
    if not truncated or not truncated[0].isalnum():
        truncated = "ladder"
    return f"{truncated}{hash_suffix}"


@dataclass(frozen=True)
class CandidatePoint:
    """A single coordinate point in the Cartesian expansion."""

    task_spec: TaskSpec
    agent_spec: AgentSpec
    agent_key: str
    provider_key: str
    preamble: str
    k: int


@dataclass(frozen=True)
class SkippedSpec:
    """Record of a candidate spec that was withheld due to quota or limit constraints."""

    name: str
    task: str
    agent: str
    preamble: str
    attempts: int
    reason: str


@dataclass(frozen=True)
class DedupeRecord:
    """Record of a grid point that was skipped because it was already present."""

    grid_id: str
    task: str
    agent: str
    preamble: str
    attempts: int
    reason: str = "already present in queue/evidence (resumed)"


@dataclass
class ProviderStats:
    """Statistics for generated specs grouped by provider."""

    provider: str
    specs_count: int = 0
    trials_count: int = 0
    estimated_cost_usd: float = 0.0


@dataclass
class GridGenerationResult:
    """Output of a LADDER grid generation run."""

    grid_id: str
    specs: list[ExperimentSpec]
    skipped: list[SkippedSpec] = field(default_factory=list)
    deduped: list[DedupeRecord] = field(default_factory=list)
    written_paths: list[Path] = field(default_factory=list)
    submitted_specs: list[str] = field(default_factory=list)
    total_specs: int = 0
    total_trials: int = 0
    total_estimated_cost_usd: float = 0.0
    by_provider: dict[str, ProviderStats] = field(default_factory=dict)

    def summary(self) -> str:
        """Render a concise human-readable summary of the generation result."""
        lines = [
            f"LADDER Grid Generation: {self.total_specs} specs generated for '{self.grid_id}', "
            f"{self.total_trials} total trials, "
            f"${self.total_estimated_cost_usd:.2f} est. cost."
        ]
        if self.deduped:
            lines.append(f"Resumed (already present): {len(self.deduped)} points")
        if self.by_provider:
            lines.append("Per-provider breakdown:")
            for p, stat in sorted(self.by_provider.items()):
                lines.append(
                    f"  - {p}: {stat.specs_count} specs, "
                    f"{stat.trials_count} trials, "
                    f"${stat.estimated_cost_usd:.2f}"
                )
        if self.skipped:
            lines.append(f"Withheld specs ({len(self.skipped)}):")
            for sk in self.skipped:
                lines.append(f"  - {sk.name} [{sk.agent}]: {sk.reason}")
        if self.submitted_specs:
            lines.append(f"Submitted {len(self.submitted_specs)} specs to the queue.")
        elif self.written_paths:
            lines.append(f"Written {len(self.written_paths)} spec files.")
        return "\n".join(lines)


def load_grid_spec(path_or_data: Path | str | dict[str, Any]) -> GridSpec:
    """Load and validate a GridSpec from a file path or dict."""
    if isinstance(path_or_data, dict):
        if "purpose" not in path_or_data:
            raise ValueError("Grid specification missing required field: 'purpose'")
        return GridSpec.model_validate(path_or_data)

    p = Path(path_or_data)
    if not p.is_file():
        raise FileNotFoundError(f"Grid spec file not found: {p}")

    content = p.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ValueError(f"Failed to parse YAML from {p}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"Grid spec in {p} must be a dictionary/mapping, got {type(data).__name__}"
        )
    if "purpose" not in data:
        raise ValueError(f"Grid specification in {p} missing required field: 'purpose'")
    return GridSpec.model_validate(data)


def _resolve_cost_per_trial(
    grid_spec: GridSpec,
    agent_spec: AgentSpec,
) -> float:
    """Determine estimated cost per trial for an agent."""
    if agent_spec.agent in CONTROL_ADAPTERS:
        return 0.0
    if agent_spec.est_cost_per_trial_usd is not None:
        return agent_spec.est_cost_per_trial_usd
    if (
        isinstance(grid_spec.est_cost_per_trial_usd, dict)
        and agent_spec.agent in grid_spec.est_cost_per_trial_usd
    ):
        return float(grid_spec.est_cost_per_trial_usd[agent_spec.agent])
    if isinstance(grid_spec.est_cost_per_trial_usd, (int, float)):
        return float(grid_spec.est_cost_per_trial_usd)
    return DEFAULT_COST_PER_TRIAL_USD.get(agent_spec.agent, 0.05)


def _render_hypothesis(
    grid_spec: GridSpec,
    task_spec: TaskSpec,
    agent_spec: AgentSpec,
    preamble: str,
    attempts: int,
) -> str:
    """Construct the hypothesis string for an expanded experiment spec."""
    grid_name = grid_spec.name or grid_spec.grid_id or "grid"
    if grid_spec.hypothesis_template:
        return grid_spec.hypothesis_template.format(
            grid=grid_name,
            task=task_spec.task,
            task_path=task_spec.task_path or task_spec.task,
            agent=agent_spec.agent,
            model=agent_spec.model or "default",
            preamble=preamble,
            attempts=attempts,
            k=attempts,
            purpose=grid_spec.purpose,
        )

    if grid_spec.hypothesis:
        if preamble and preamble != "none":
            return f"{grid_spec.hypothesis} (variant: {preamble}, k={attempts})"
        return f"{grid_spec.hypothesis} (k={attempts})"

    if preamble and preamble != "none":
        preamble_desc = f"with preamble '{preamble}'"
    else:
        preamble_desc = "with standard prompt"
    model_desc = f" ({agent_spec.model})" if agent_spec.model else ""
    return (
        f"LADDER evaluation of {agent_spec.agent}{model_desc} on {task_spec.task} "
        f"{preamble_desc} across {attempts} attempt(s)."
    )


def _normalize_agent_item(item: str | AgentSpec) -> AgentSpec:
    """Normalize a string or AgentSpec into a resolved AgentSpec."""
    if isinstance(item, AgentSpec):
        return item
    if item in (p := builtin_profiles()):
        return AgentSpec(agent=p[item].adapter, model=p[item].model)
    return AgentSpec(agent=item)


def _normalize_task_item(item: str | TaskSpec) -> TaskSpec:
    """Normalize a string or TaskSpec into a TaskSpec."""
    return item if isinstance(item, TaskSpec) else TaskSpec(task=item)


def find_existing_grid_points(
    grid_id: str,
    *,
    repo_root: Path | None = None,
    extra_dirs: Sequence[Path] = (),
) -> set[tuple[str, str, str, int]]:
    """Scan queue state directories and extra directories for existing points of a grid."""
    root = repo_root or Path.cwd()
    points: set[tuple[str, str, str, int]] = set()

    search_dirs: list[Path] = []
    queue_root = root / "queue"
    if queue_root.is_dir():
        states = (
            "proposed", "pending", "approved", "waiting",
            "rejected", "running", "done", "failed",
        )
        search_dirs.extend(queue_root / s for s in states if (queue_root / s).is_dir())
    for extra in extra_dirs:
        if extra.is_dir() and extra not in search_dirs:
            search_dirs.append(extra)

    for directory in search_dirs:
        for json_file in directory.glob("*.json"):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            spec_grid_id = data.get("grid_id")
            if spec_grid_id != grid_id:
                continue

            grid_point = data.get("grid_point")
            if isinstance(grid_point, dict):
                t_ref = str(grid_point.get("task_ref") or data.get("task") or "")
                ag = str(grid_point.get("agent") or data.get("agent") or "")
                mod = grid_point.get("model") or data.get("model")
                ag_key = f"{ag}-{mod}" if mod and ag not in CONTROL_ADAPTERS else ag
                preamble = str(grid_point.get("preamble") or "none")
                k = int(grid_point.get("k") or data.get("attempts") or 1)
                points.add((t_ref, ag_key, preamble, k))
                points.add((t_ref, ag, preamble, k))
            else:
                t_ref = str(data.get("task") or "")
                ag = str(data.get("agent") or "")
                mod = data.get("model")
                ag_key = f"{ag}-{mod}" if mod and ag not in CONTROL_ADAPTERS else ag
                k = int(data.get("attempts") or 1)
                points.add((t_ref, ag_key, "none", k))
                points.add((t_ref, ag, "none", k))

    return points


def _matches_str(target: str | None, val: Any) -> bool:
    return target in val if isinstance(val, (list, tuple, set)) else target == str(val)


def _point_matches_constraint(
    task_ref: str,
    agent_spec: AgentSpec,
    agent_key: str,
    preamble: str,
    k: int,
    constraint: dict[str, Any],
) -> bool:
    """Check whether a grid point matches an exclusion constraint."""
    for key, val in constraint.items():
        if key in {"task", "task_ref", "task_refs"}:
            if not _matches_str(task_ref, val):
                return False
        elif key in {"agent", "agents"}:
            allowed = val if isinstance(val, (list, tuple, set)) else [str(val)]
            if agent_spec.agent not in allowed and agent_key not in allowed:
                return False
        elif key in {"model", "models"}:
            if not _matches_str(agent_spec.model, val):
                return False
        elif key in {"preamble", "preambles"}:
            if not _matches_str(preamble, val):
                return False
        elif key in {"k", "attempts"}:
            allowed_k = [int(x) for x in val] if isinstance(val, (list, tuple, set)) else [int(val)]
            if k not in allowed_k:
                return False
        elif str(val) != str(constraint.get(key)):
            return False
    return True


def generate_grid(
    grid: GridSpec | Path | str | dict[str, Any],
    *,
    output_dir: Path | str | None = None,
    repo_root: Path | None = None,
    headroom_override: Headroom | None = None,
    submit: bool = False,
    dry_run: bool = False,
    check_quota_headroom: bool | None = None,
) -> GridGenerationResult:
    """Expand a grid specification into valid ExperimentSpecs while respecting limits."""
    grid_spec = load_grid_spec(grid) if not isinstance(grid, GridSpec) else grid
    root = repo_root or Path.cwd()
    grid_id = grid_spec.grid_id or grid_spec.name or "grid"

    should_check_quota = (
        check_quota_headroom
        if check_quota_headroom is not None
        else grid_spec.check_quota_headroom
    )

    # Query headroom if quota checking is enabled and provider is paid
    headroom: Headroom | None = headroom_override
    if headroom is None and should_check_quota:
        try:
            quota_rep = load_quota_report(default_roots(root), now=datetime.now(UTC))
            headroom = quota_rep.headroom
        except Exception:
            headroom = None

    # Determine provider-level exhaustion
    provider_exhausted: set[str] = set()
    if (
        headroom is not None
        and headroom.availability == "observed"
        and (
            headroom.rate_limit_reached_type is not None
            or (headroom.used_percent is not None and headroom.used_percent >= 100.0)
        )
    ):
        provider_exhausted.update(PAID_AGENTS)

    # 1. Discover already generated/run points for resume-not-duplicate
    extra_dirs = [Path(output_dir)] if output_dir else []
    existing_points = find_existing_grid_points(grid_id, repo_root=root, extra_dirs=extra_dirs)

    # 2. Build candidate points across axes
    assert grid_spec.axes is not None
    tasks = [_normalize_task_item(t) for t in grid_spec.axes.task_refs]
    agents = [_normalize_agent_item(a) for a in grid_spec.axes.agents]
    preambles = list(grid_spec.axes.preamble)
    attempts = list(grid_spec.axes.k)

    candidates: list[CandidatePoint] = []
    deduped: list[DedupeRecord] = []

    for task_spec in tasks:
        for agent_spec in agents:
            agent_key = (
                f"{agent_spec.agent}-{agent_spec.model}"
                if agent_spec.model and agent_spec.agent not in CONTROL_ADAPTERS
                else agent_spec.agent
            )
            provider_key = agent_spec.agent

            for preamble in preambles:
                for k in attempts:
                    # Check constraints
                    excluded_by_constraint = False
                    for constraint in grid_spec.constraints:
                        if _point_matches_constraint(
                            task_spec.task, agent_spec, agent_key, preamble, k, constraint
                        ):
                            excluded_by_constraint = True
                            break
                    if excluded_by_constraint:
                        continue

                    # Check deduplication (resume)
                    if (task_spec.task, agent_key, preamble, k) in existing_points or (
                        task_spec.task, agent_spec.agent, preamble, k
                    ) in existing_points:
                        deduped.append(
                            DedupeRecord(
                                grid_id=grid_id,
                                task=task_spec.task,
                                agent=agent_key,
                                preamble=preamble,
                                attempts=k,
                            )
                        )
                        continue

                    candidates.append(
                        CandidatePoint(
                            task_spec=task_spec, agent_spec=agent_spec, agent_key=agent_key,
                            provider_key=provider_key, preamble=preamble, k=k,
                        )
                    )

    # 3. Round-robin order candidate points across providers
    candidates_by_provider: dict[str, list[CandidatePoint]] = {}
    for cand in candidates:
        candidates_by_provider.setdefault(cand.provider_key, []).append(cand)

    ordered_candidates: list[CandidatePoint] = []
    # Preserve provider iteration order deterministically
    provider_keys = sorted(candidates_by_provider.keys())
    queues = {p: list(candidates_by_provider[p]) for p in provider_keys}
    while any(queues.values()):
        for p in provider_keys:
            if queues[p]:
                ordered_candidates.append(queues[p].pop(0))
    # 4. Assert uniqueness of candidate point names across all unskipped points
    candidate_names = [
        generate_spec_name(
            grid_spec.name or grid_id,
            sanitize_slug(c.task_spec.task),
            sanitize_slug(
                f"{c.agent_spec.agent}-{c.agent_spec.model}"
                if c.agent_spec.model
                else c.agent_spec.agent
            ),
            sanitize_slug(c.preamble),
            c.k,
        )
        for c in ordered_candidates
    ]
    if len(candidate_names) != len(set(candidate_names)):
        counts = Counter(candidate_names)
        duplicates = [name for name, cnt in counts.items() if cnt > 1]
        raise ValueError(
            f"Candidate grid point names are not unique: "
            f"{len(ordered_candidates)} points produced {len(set(candidate_names))} unique names. "
            f"Duplicate names: {duplicates}"
        )

    # 5. Filter and emit specs respecting quota, budget units, and limits
    total_specs = 0
    total_trials = 0
    total_cost = 0.0
    units_consumed = 0
    provider_stats: dict[str, ProviderStats] = {}
    specs: list[ExperimentSpec] = []
    skipped: list[SkippedSpec] = []

    for cand in ordered_candidates:
        task_spec = cand.task_spec
        agent_spec = cand.agent_spec
        provider_key = cand.provider_key
        preamble = cand.preamble
        k = cand.k

        task_slug = sanitize_slug(task_spec.task)
        agent_slug = sanitize_slug(
            f"{agent_spec.agent}-{agent_spec.model}" if agent_spec.model else agent_spec.agent
        )
        preamble_slug = sanitize_slug(preamble)
        spec_name = generate_spec_name(
            grid_spec.name or grid_id, task_slug, agent_slug, preamble_slug, k
        )

        if provider_key not in provider_stats:
            provider_stats[provider_key] = ProviderStats(provider=provider_key)
        p_stat = provider_stats[provider_key]

        cost_per_trial = _resolve_cost_per_trial(grid_spec, agent_spec)
        spec_cost = cost_per_trial * k

        # Check provider exhaustion from headroom
        if provider_key in provider_exhausted and agent_spec.agent not in CONTROL_ADAPTERS:
            skipped.append(
                SkippedSpec(
                    name=spec_name, task=task_spec.task, agent=agent_spec.agent,
                    preamble=preamble,
                    attempts=k,
                    reason="provider reported quota exhausted in current window",
                )
            )
            continue

        # Check daily_budget_units
        if grid_spec.daily_budget_units is not None:
            # Each attempt represents 1 budget unit
            point_units = k
            if (units_consumed + point_units) > grid_spec.daily_budget_units:
                skipped.append(
                    SkippedSpec(
                        name=spec_name,
                        task=task_spec.task,
                        agent=agent_spec.agent,
                        preamble=preamble,
                        attempts=k,
                        reason=(
                            f"daily_budget_units limit ({grid_spec.daily_budget_units}) "
                            "would be exceeded"
                        ),
                    )
                )
                continue

        # Global limits check
        if grid_spec.limits.max_specs is not None and total_specs >= grid_spec.limits.max_specs:
            skipped.append(
                SkippedSpec(
                    name=spec_name,
                    task=task_spec.task,
                    agent=agent_spec.agent, preamble=preamble,
                    attempts=k,
                    reason=f"global max_specs limit ({grid_spec.limits.max_specs}) reached",
                )
            )
            continue

        if (
            grid_spec.limits.max_trials is not None
            and (total_trials + k) > grid_spec.limits.max_trials
        ):
            skipped.append(
                SkippedSpec(
                    name=spec_name, task=task_spec.task,
                    agent=agent_spec.agent, preamble=preamble,
                    attempts=k,
                    reason=(
                        f"global max_trials limit ({grid_spec.limits.max_trials}) "
                        "would be exceeded"
                    ),
                )
            )
            continue

        if (
            grid_spec.limits.max_cost_usd is not None
            and (total_cost + spec_cost) > grid_spec.limits.max_cost_usd
        ):
            skipped.append(
                SkippedSpec(
                    name=spec_name,
                    task=task_spec.task,
                    agent=agent_spec.agent,
                    preamble=preamble,
                    attempts=k,
                    reason=(
                        f"global max_cost_usd limit "
                        f"(${grid_spec.limits.max_cost_usd:.2f}) would be exceeded"
                    ),
                )
            )
            continue
        # Per-provider limits check
        p_lim = grid_spec.limits.per_provider.get(provider_key)
        if p_lim is not None:
            if p_lim.max_specs is not None and p_stat.specs_count >= p_lim.max_specs:
                skipped.append(
                    SkippedSpec(
                        name=spec_name,
                        task=task_spec.task,
                        agent=agent_spec.agent,
                        preamble=preamble,
                        attempts=k,
                        reason=(
                            f"provider {provider_key} max_specs limit "
                            f"({p_lim.max_specs}) reached"
                        ),
                    )
                )
                continue

            if p_lim.max_trials is not None and (p_stat.trials_count + k) > p_lim.max_trials:
                skipped.append(
                    SkippedSpec(
                        name=spec_name,
                        task=task_spec.task,
                        agent=agent_spec.agent,
                        preamble=preamble,
                        attempts=k,
                        reason=(
                            f"provider {provider_key} max_trials limit "
                            f"({p_lim.max_trials}) would be exceeded"
                        ),
                    )
                )
                continue

            if (
                p_lim.max_cost_usd is not None
                and (p_stat.estimated_cost_usd + spec_cost) > p_lim.max_cost_usd
            ):
                skipped.append(
                    SkippedSpec(
                        name=spec_name,
                        task=task_spec.task,
                        agent=agent_spec.agent,
                        preamble=preamble,
                        attempts=k,
                        reason=(
                            f"provider {provider_key} max_cost_usd limit "
                            f"(${p_lim.max_cost_usd:.2f}) would be exceeded"
                        ),
                    )
                )
                continue

        # Construct ExperimentSpec
        hypothesis = _render_hypothesis(grid_spec, task_spec, agent_spec, preamble, k)
        environment = agent_spec.environment or grid_spec.environment

        spec = ExperimentSpec(
            name=spec_name,
            hypothesis=hypothesis,
            purpose=grid_spec.purpose,
            task=task_spec.task,
            task_path=task_spec.task_path,
            agent=agent_spec.agent,
            model=agent_spec.model,
            environment=environment,
            jobs_dir=grid_spec.jobs_dir,
            attempts=k,
            concurrency=grid_spec.concurrency,
            timeout_seconds=grid_spec.timeout_seconds,
            submitted_by=grid_spec.submitted_by,
            priority=grid_spec.priority,
            est_cost_usd=spec_cost,
            policy_rule=grid_spec.policy_rule,
            requires=list(grid_spec.requires),
            expected_reward=task_spec.expected_reward,
            task_version=task_spec.task_version,
            verifier_digest=task_spec.verifier_digest,
            grid_id=grid_id,
            grid_point={
                "task_ref": task_spec.task,
                "agent": agent_spec.agent,
                "model": agent_spec.model,
                "preamble": preamble,
                "k": k,
            },
        )

        specs.append(spec)
        total_specs += 1
        total_trials += k
        total_cost += spec_cost
        units_consumed += k
        p_stat.specs_count += 1
        p_stat.trials_count += k
        p_stat.estimated_cost_usd += spec_cost

    written_paths: list[Path] = []
    submitted_specs: list[str] = []

    # 6. Assert uniqueness of generated spec names before writing
    spec_names = [s.name for s in specs]
    if len(spec_names) != len(set(spec_names)):
        counts = Counter(spec_names)
        duplicates = [name for name, cnt in counts.items() if cnt > 1]
        raise ValueError(
            f"Generated spec names are not unique across grid points: "
            f"{len(specs)} specs produced {len(set(spec_names))} unique names. "
            f"Duplicate names: {duplicates}"
        )

    # Write or submit if requested and not in dry-run mode
    if submit:
        executor = Executor.from_repo(root)
        for s in specs:
            path, _decision = executor.submit(s)
            submitted_specs.append(s.name)
            written_paths.append(path)
    elif output_dir is not None:
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        for s in specs:
            file_path = out_path / f"{s.name}.json"
            if file_path.exists():
                raise FileExistsError(
                    f"Spec file already exists and cannot be overwritten: {file_path}. "
                    "Existing points must be resumed (deduped) or removed, never overwritten."
                )
            file_path.write_text(s.model_dump_json(indent=2), encoding="utf-8")
            written_paths.append(file_path)
    return GridGenerationResult(
        grid_id=grid_id,
        specs=specs,
        skipped=skipped,
        deduped=deduped,
        written_paths=written_paths,
        submitted_specs=submitted_specs,
        total_specs=total_specs,
        total_trials=total_trials,
        total_estimated_cost_usd=total_cost,
        by_provider=provider_stats,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for LADDER."""
    parser = argparse.ArgumentParser(
        prog="evallab.ladder",
        description="LADDER: Evaluation grid generator for evallab.",
    )
    subparsers = parser.add_subparsers(dest="command", help="LADDER subcommands")

    # generate subcommand
    gen_parser = subparsers.add_parser(
        "generate",
        help="Expand a declared grid specification into ExperimentSpec files.",
    )
    gen_parser.add_argument(
        "grid_spec",
        type=Path,
        help="Path to the grid specification YAML/JSON file.",
    )
    gen_parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=None,
        help="Directory to write generated ExperimentSpec JSON files.",
    )
    gen_parser.add_argument(
        "--submit",
        action="store_true",
        help="Submit generated ExperimentSpecs directly to the queue.",
    )
    gen_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print expansion and decisions without writing to disk (default).",
    )
    gen_parser.add_argument(
        "--no-quota-check",
        action="store_true",
        help="Disable automatic headroom/quota checking against existing runs.",
    )
    gen_parser.add_argument(
        "--json",
        action="store_true",
        help="Print generation summary and details in JSON format.",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for `python -m evallab.ladder`."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.command != "generate":
        if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
            args = parser.parse_args(["generate", *sys.argv[1:]])
        else:
            parser.print_help()
            return 1

    try:
        grid_spec = load_grid_spec(args.grid_spec)
        if args.no_quota_check:
            grid_spec = grid_spec.model_copy(update={"check_quota_headroom": False})

        # By default, dry_run is active if neither --submit nor -o/--output-dir is supplied
        dry_run = args.dry_run or (not args.submit and args.output_dir is None)

        result = generate_grid(
            grid_spec,
            output_dir=args.output_dir,
            submit=args.submit,
            dry_run=dry_run,
        )

        if args.json:
            out_data = {
                "grid_id": result.grid_id,
                "total_specs": result.total_specs,
                "total_trials": result.total_trials,
                "total_estimated_cost_usd": result.total_estimated_cost_usd,
                "specs": [s.model_dump(mode="json") for s in result.specs],
                "skipped": [
                    {
                        "name": sk.name, "task": sk.task, "agent": sk.agent,
                        "preamble": sk.preamble, "attempts": sk.attempts, "reason": sk.reason,
                    }
                    for sk in result.skipped
                ],
                "deduped": [
                    {
                        "grid_id": d.grid_id, "task": d.task, "agent": d.agent,
                        "preamble": d.preamble, "attempts": d.attempts, "reason": d.reason,
                    }
                    for d in result.deduped
                ],
                "written_files": [str(p) for p in result.written_paths],
                "submitted_specs": result.submitted_specs,
            }
            print(json.dumps(out_data, indent=2))
        else:
            print(result.summary())

        return 0
    except Exception as exc:
        print(f"Error generating grid: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
