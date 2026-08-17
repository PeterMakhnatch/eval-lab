"""LADDER: Evaluation grid generator (WS-E item 3).

Expands declared grid specifications (task list x agent list x preamble variants x k attempts)
into valid, purpose-tagged ExperimentSpec files under per-provider quotas and batch limits.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from evallab.profiles import CONTROL_ADAPTERS, builtin_profiles
from evallab.quota import (
    PAID_AGENTS,
    Headroom,
    default_roots,
    load_quota_report,
)
from evallab.schemas import (
    EXPLORATION_JOBS_ROOT,
    ContractModel,
    ExperimentPurpose,
    ExperimentSpec,
    validated_jobs_dir,
)

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


def sanitize_slug(value: str) -> str:
    """Normalize a string into a clean lowercase hyphen-separated slug."""
    # Strip directory parts and file extensions if it looks like a file path
    if "/" in value or "\\" in value or value.endswith(
        (".md", ".yaml", ".yml", ".json", ".toml", ".txt", ".py")
    ):
        stem = Path(value).stem
    else:
        stem = value
    lowered = stem.lower()
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

    # Truncate while keeping structure and valid suffix
    k_suffix = f"-k{attempts}"
    budget = max_len - len(k_suffix)
    truncated = candidate[:budget].rstrip("-")
    return f"{truncated}{k_suffix}"


class TaskSpec(BaseModel):
    """Specification of a task in the grid."""

    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1)
    task_path: str | None = None
    expected_reward: float | None = None
    task_version: str | None = None
    verifier_digest: str | None = None


class AgentSpec(BaseModel):
    """Specification of an agent in the grid."""

    model_config = ConfigDict(extra="forbid")

    agent: str = Field(min_length=1)
    model: str | None = None
    environment: str | None = None
    est_cost_per_trial_usd: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def validate_control_models(self) -> AgentSpec:
        if self.agent in CONTROL_ADAPTERS and self.model:
            raise ValueError(f"Control agent {self.agent!r} must not declare a model")
        return self


class ProviderLimit(BaseModel):
    """Quota and batch limits for a single provider/agent."""

    model_config = ConfigDict(extra="forbid")

    max_specs: int | None = Field(default=None, ge=1)
    max_trials: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, ge=0.0)


class GridLimits(BaseModel):
    """Global and per-provider bounds on grid expansion."""

    model_config = ConfigDict(extra="forbid")

    max_specs: int | None = Field(default=None, ge=1)
    max_trials: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, ge=0.0)
    per_provider: dict[str, ProviderLimit] = Field(default_factory=dict)


class LadderGridSpec(ContractModel):
    """Declared specification for an evaluation grid expansion."""

    schema_version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=60)
    hypothesis: str | None = None
    hypothesis_template: str | None = None
    purpose: ExperimentPurpose = "elicitation"
    tasks: list[str | TaskSpec] = Field(min_length=1)
    agents: list[str | AgentSpec] = Field(min_length=1)
    preambles: list[str] = Field(default_factory=lambda: ["none"])
    attempts: list[int] = Field(default_factory=lambda: [1])
    environment: str = "docker"
    jobs_dir: str = EXPLORATION_JOBS_ROOT
    concurrency: int = Field(default=1, ge=1)
    timeout_seconds: int = Field(default=1_800, ge=1, le=21_600)
    submitted_by: str = Field(default="ladder-generator", min_length=1)
    priority: int = Field(default=100, ge=0, le=1000)
    est_cost_per_trial_usd: dict[str, float] | float = Field(default_factory=dict)
    limits: GridLimits = Field(default_factory=GridLimits)
    check_quota_headroom: bool = True
    policy_rule: str | None = None
    requires: list[str] = Field(default_factory=list)

    @field_validator("jobs_dir")
    @classmethod
    def jobs_dir_is_a_readable_root(cls, value: str) -> str:
        return validated_jobs_dir(value)

    @field_validator("tasks")
    @classmethod
    def normalize_tasks(cls, value: list[str | TaskSpec]) -> list[TaskSpec]:
        normalized: list[TaskSpec] = []
        for item in value:
            if isinstance(item, str):
                normalized.append(TaskSpec(task=item))
            else:
                normalized.append(item)
        return normalized

    @field_validator("agents")
    @classmethod
    def normalize_agents(cls, value: list[str | AgentSpec]) -> list[AgentSpec]:
        normalized: list[AgentSpec] = []
        builtins = builtin_profiles()
        for item in value:
            if isinstance(item, str):
                if item in builtins:
                    profile = builtins[item]
                    normalized.append(
                        AgentSpec(
                            agent=profile.adapter,
                            model=profile.model,
                        )
                    )
                else:
                    normalized.append(AgentSpec(agent=item))
            else:
                normalized.append(item)
        return normalized

    @field_validator("attempts", mode="before")
    @classmethod
    def normalize_attempts(cls, value: list[int] | int) -> list[int]:
        if isinstance(value, int):
            if value < 1:
                raise ValueError("attempts must be >= 1")
            return [value]
        if not value:
            return [1]
        for v in value:
            if v < 1:
                raise ValueError(f"attempt count {v} must be >= 1")
        return value

    @field_validator("preambles")
    @classmethod
    def normalize_preambles(cls, value: list[str]) -> list[str]:
        if not value:
            return ["none"]
        return value


@dataclass(frozen=True)
class SkippedSpec:
    """Record of a candidate spec that was pruned due to quota or limit constraints."""

    name: str
    task: str
    agent: str
    preamble: str
    attempts: int
    reason: str


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

    specs: list[ExperimentSpec]
    skipped: list[SkippedSpec] = field(default_factory=list)
    written_paths: list[Path] = field(default_factory=list)
    total_specs: int = 0
    total_trials: int = 0
    total_estimated_cost_usd: float = 0.0
    by_provider: dict[str, ProviderStats] = field(default_factory=dict)

    def summary(self) -> str:
        """Render a concise human-readable summary of the generation result."""
        lines = [
            f"LADDER Grid Generation: {self.total_specs} specs generated, "
            f"{self.total_trials} total trials, "
            f"${self.total_estimated_cost_usd:.2f} est. cost."
        ]
        if self.by_provider:
            lines.append("Per-provider breakdown:")
            for p, stat in sorted(self.by_provider.items()):
                lines.append(
                    f"  - {p}: {stat.specs_count} specs, "
                    f"{stat.trials_count} trials, "
                    f"${stat.estimated_cost_usd:.2f}"
                )
        if self.skipped:
            lines.append(f"Skipped specs ({len(self.skipped)}):")
            for sk in self.skipped:
                lines.append(f"  - {sk.name} [{sk.agent}]: {sk.reason}")
        if self.written_paths:
            lines.append(f"Written {len(self.written_paths)} spec files.")
        return "\n".join(lines)


def load_grid_spec(path_or_data: Path | str | dict[str, Any]) -> LadderGridSpec:
    """Load and validate a LadderGridSpec from a file path or dict."""
    if isinstance(path_or_data, dict):
        return LadderGridSpec.model_validate(path_or_data)

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
    return LadderGridSpec.model_validate(data)


def _resolve_cost_per_trial(
    grid_spec: LadderGridSpec,
    agent_spec: AgentSpec,
) -> float:
    """Determine estimated cost per trial for an agent."""
    if agent_spec.agent in CONTROL_ADAPTERS:
        return 0.0
    if agent_spec.est_cost_per_trial_usd is not None:
        return agent_spec.est_cost_per_trial_usd
    if isinstance(grid_spec.est_cost_per_trial_usd, dict):
        if agent_spec.agent in grid_spec.est_cost_per_trial_usd:
            return float(grid_spec.est_cost_per_trial_usd[agent_spec.agent])
    elif isinstance(grid_spec.est_cost_per_trial_usd, (int, float)):
        return float(grid_spec.est_cost_per_trial_usd)

    return DEFAULT_COST_PER_TRIAL_USD.get(agent_spec.agent, 0.05)


def _render_hypothesis(
    grid_spec: LadderGridSpec,
    task_spec: TaskSpec,
    agent_spec: AgentSpec,
    preamble: str,
    attempts: int,
) -> str:
    """Construct the hypothesis string for an expanded experiment spec."""
    if grid_spec.hypothesis_template:
        return grid_spec.hypothesis_template.format(
            grid=grid_spec.name,
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

    # Default descriptive hypothesis
    if preamble and preamble != "none":
        preamble_desc = f"with preamble '{preamble}'"
    else:
        preamble_desc = "with standard prompt"
    model_desc = f" ({agent_spec.model})" if agent_spec.model else ""
    return (
        f"LADDER evaluation of {agent_spec.agent}{model_desc} on {task_spec.task} "
        f"{preamble_desc} across {attempts} attempt(s)."
    )


def generate_grid(
    grid: LadderGridSpec | Path | str | dict[str, Any],
    *,
    output_dir: Path | str | None = None,
    repo_root: Path | None = None,
    headroom_override: Headroom | None = None,
) -> GridGenerationResult:
    """Expand a grid specification into valid ExperimentSpecs while respecting limits."""
    grid_spec = load_grid_spec(grid) if not isinstance(grid, LadderGridSpec) else grid
    root = repo_root or Path.cwd()

    # Query headroom if quota checking is enabled and provider is paid
    headroom: Headroom | None = headroom_override
    if headroom is None and grid_spec.check_quota_headroom:
        try:
            quota_rep = load_quota_report(default_roots(root), now=datetime.now(UTC))
            headroom = quota_rep.headroom
        except Exception:
            headroom = None

    # Trackers
    total_specs = 0
    total_trials = 0
    total_cost = 0.0
    provider_stats: dict[str, ProviderStats] = {}
    specs: list[ExperimentSpec] = []
    skipped: list[SkippedSpec] = []

    # Check provider-level exhaustion from headroom
    provider_exhausted: set[str] = set()
    if (
        headroom is not None
        and headroom.availability == "observed"
        and (
            headroom.rate_limit_reached_type is not None
            or (headroom.used_percent is not None and headroom.used_percent >= 100.0)
        )
    ):
        # All paid agents covered by this subscription headroom
        provider_exhausted.update(PAID_AGENTS)

    # Cartesian product expansion: tasks x agents x preambles x attempts
    for task_spec in grid_spec.tasks:
        assert isinstance(task_spec, TaskSpec)
        task_slug = sanitize_slug(task_spec.task)

        for agent_spec in grid_spec.agents:
            assert isinstance(agent_spec, AgentSpec)
            agent_slug = sanitize_slug(
                f"{agent_spec.agent}-{agent_spec.model}" if agent_spec.model else agent_spec.agent
            )
            provider_key = agent_spec.agent

            if provider_key not in provider_stats:
                provider_stats[provider_key] = ProviderStats(provider=provider_key)
            p_stat = provider_stats[provider_key]

            # Check if provider is exhausted via observed headroom
            if provider_key in provider_exhausted and agent_spec.agent not in CONTROL_ADAPTERS:
                for preamble in grid_spec.preambles:
                    preamble_slug = sanitize_slug(preamble)
                    for k in grid_spec.attempts:
                        spec_name = generate_spec_name(
                            grid_spec.name, task_slug, agent_slug, preamble_slug, k
                        )
                        skipped.append(
                            SkippedSpec(
                                name=spec_name,
                                task=task_spec.task,
                                agent=agent_spec.agent,
                                preamble=preamble,
                                attempts=k,
                                reason="provider reported quota exhausted in current window",
                            )
                        )
                continue

            for preamble in grid_spec.preambles:
                preamble_slug = sanitize_slug(preamble)

                for k in grid_spec.attempts:
                    spec_name = generate_spec_name(
                        grid_spec.name, task_slug, agent_slug, preamble_slug, k
                    )
                    cost_per_trial = _resolve_cost_per_trial(grid_spec, agent_spec)
                    spec_cost = cost_per_trial * k

                    # 1. Global Batch Limits Check
                    if (
                        grid_spec.limits.max_specs is not None
                        and total_specs >= grid_spec.limits.max_specs
                    ):
                        skipped.append(
                            SkippedSpec(
                                name=spec_name,
                                task=task_spec.task,
                                agent=agent_spec.agent,
                                preamble=preamble,
                                attempts=k,
                                reason=(
                                    f"global max_specs limit ({grid_spec.limits.max_specs}) reached"
                                ),
                            )
                        )
                        continue

                    if (
                        grid_spec.limits.max_trials is not None
                        and (total_trials + k) > grid_spec.limits.max_trials
                    ):
                        skipped.append(
                            SkippedSpec(
                                name=spec_name,
                                task=task_spec.task,
                                agent=agent_spec.agent,
                                preamble=preamble,
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

                    # 2. Per-Provider Limits Check
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

                        if (
                            p_lim.max_trials is not None
                            and (p_stat.trials_count + k) > p_lim.max_trials
                        ):
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
                    )

                    specs.append(spec)
                    total_specs += 1
                    total_trials += k
                    total_cost += spec_cost
                    p_stat.specs_count += 1
                    p_stat.trials_count += k
                    p_stat.estimated_cost_usd += spec_cost

    # Write files if output_dir provided
    written_paths: list[Path] = []
    if output_dir is not None:
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        for s in specs:
            file_path = out_path / f"{s.name}.json"
            file_path.write_text(s.model_dump_json(indent=2), encoding="utf-8")
            written_paths.append(file_path)

    return GridGenerationResult(
        specs=specs,
        skipped=skipped,
        written_paths=written_paths,
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
        help="Directory to write generated ExperimentSpec JSON files (e.g. queue/proposed).",
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
        # If no subcommand provided, but a grid spec path was passed directly
        if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
            # Re-parse treating first arg as grid_spec under generate
            args = parser.parse_args(["generate", *sys.argv[1:]])
        else:
            parser.print_help()
            return 1

    try:
        grid_spec = load_grid_spec(args.grid_spec)
        if args.no_quota_check:
            grid_spec = grid_spec.model_copy(update={"check_quota_headroom": False})

        result = generate_grid(grid_spec, output_dir=args.output_dir)

        if args.json:
            out_data = {
                "total_specs": result.total_specs,
                "total_trials": result.total_trials,
                "total_estimated_cost_usd": result.total_estimated_cost_usd,
                "specs": [s.model_dump(mode="json") for s in result.specs],
                "skipped": [
                    {
                        "name": sk.name,
                        "task": sk.task,
                        "agent": sk.agent,
                        "preamble": sk.preamble,
                        "attempts": sk.attempts,
                        "reason": sk.reason,
                    }
                    for sk in result.skipped
                ],
                "written_files": [str(p) for p in result.written_paths],
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
