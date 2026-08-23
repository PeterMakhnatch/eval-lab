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
import os
import re
import sys
import tempfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import product
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
    ExperimentArm,
    ExperimentSpec,
    GridAxes,
    GridLimits,
    GridSpec,
    LadderGridSpec,
    ProviderLimit,
    TaskSpec,
)
from evallab.screen import (
    DifficultyVariantContract,
    ModelLevelSpec,
    ScreenAnalysisReport,
    ScreenClassification,
    ScreenDecisionRules,
    ScreenSpec,
    TaskScreenResult,
    analyze_screen_results,
    generate_stage1_screen,
    generate_stage2_screen,
    load_screen_spec,
)

# Re-export schema models for callers
__all__ = [
    "AgentSpec",
    "CandidatePoint",
    "DedupeRecord",
    "ExperimentArm",
    "DifficultyVariantContract",
    "GridAxes",
    "GridGenerationResult",
    "GridLimits",
    "GridSpec",
    "LadderGridSpec",
    "ModelLevelSpec",
    "ProviderLimit",
    "PlanShard",
    "ProviderStats",
    "ScreenAnalysisReport",
    "ScreenClassification",
    "ScreenDecisionRules",
    "ScreenSpec",
    "SkippedSpec",
    "TaskScreenResult",
    "TaskSpec",
    "analyze_screen_results",
    "build_arg_parser",
    "find_existing_grid_points",
    "compile_plan_shards",
    "generate_grid",
    "generate_spec_name",
    "generate_stage1_screen",
    "generate_stage2_screen",
    "load_grid_spec",
    "load_screen_spec",
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
    arm_id: str | None
    factor_values: dict[str, Any]
    point_id: str
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
    arm_id: str | None = None
    factor_values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DedupeRecord:
    """Record of a grid point that was skipped because it was already present."""

    grid_id: str
    task: str
    agent: str
    preamble: str
    attempts: int
    arm_id: str | None = None
    factor_values: dict[str, Any] = field(default_factory=dict)
    reason: str = "already present in queue/evidence (resumed)"
@dataclass(frozen=True)
class PlanShard:
    shard_id: str
    index: int
    spec_names: tuple[str, ...]
    trial_count: int
    estimated_cost_usd: float
    sha256: str
    path: Path | None = None




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
    shards: list[PlanShard] = field(default_factory=list)
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
    *,
    arm_id: str | None = None,
    factor_values: dict[str, Any] | None = None,
) -> str:
    """Construct the hypothesis string for an expanded experiment spec."""
    factors = factor_values or {}
    grid_name = grid_spec.name or grid_spec.grid_id or "grid"
    if grid_spec.hypothesis_template:
        coordinates = {f"factor_{name}": value for name, value in factors.items()}
        return grid_spec.hypothesis_template.format(
            grid=grid_name,
            task=task_spec.task,
            task_path=task_spec.task_path or task_spec.task,
            agent=agent_spec.agent,
            model=agent_spec.model or "default",
            arm=arm_id or "legacy",
            factors_json=json.dumps(factors, sort_keys=True),
            preamble=preamble,
            attempts=attempts,
            k=attempts,
            purpose=grid_spec.purpose,
            **coordinates,
        )
    suffix: list[str] = [f"k={attempts}"]
    if arm_id:
        suffix.insert(0, f"arm={arm_id}")
    if preamble and preamble != "none":
        suffix.insert(0, f"variant={preamble}")
    if factors:
        suffix.insert(0, f"factors={json.dumps(factors, sort_keys=True)}")
    if grid_spec.hypothesis:
        return f"{grid_spec.hypothesis} ({', '.join(suffix)})"
    model_desc = f" ({agent_spec.model})" if agent_spec.model else ""
    return (
        f"LADDER evaluation of {agent_spec.agent}{model_desc} on {task_spec.task} "
        f"({', '.join(suffix)})."
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
def _point_id(
    task_ref: str,
    agent_key: str,
    preamble: str,
    k: int,
    *,
    arm_id: str | None,
    factor_values: dict[str, Any],
) -> str:
    payload = {
        "task_ref": task_ref,
        "agent_key": agent_key,
        "preamble": preamble,
        "k": k,
        "arm_id": arm_id,
        "factors": factor_values,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _factor_combinations(
    factors: dict[str, list[Any]],
    overrides: dict[str, Any],
) -> list[dict[str, Any]]:
    names = sorted(name for name in factors if name not in overrides)
    if not names:
        return [dict(sorted(overrides.items()))]
    return [
        dict(sorted({**dict(zip(names, levels, strict=True)), **overrides}.items()))
        for levels in product(*(factors[name] for name in names))
    ]


def _candidate_spec_name(grid_name: str, candidate: CandidatePoint) -> str:
    coordinate = candidate.preamble
    if candidate.factor_values:
        encoded = json.dumps(
            candidate.factor_values, sort_keys=True, separators=(",", ":")
        ).encode()
        factor_slug = f"f{hashlib.sha256(encoded).hexdigest()[:8]}"
        coordinate = (
            f"{coordinate}-{factor_slug}" if coordinate != "none" else factor_slug
        )
    treatment = candidate.arm_id or (
        f"{candidate.agent_spec.agent}-{candidate.agent_spec.model}"
        if candidate.agent_spec.model
        else candidate.agent_spec.agent
    )
    return generate_spec_name(
        grid_name,
        sanitize_slug(candidate.task_spec.task),
        sanitize_slug(treatment),
        sanitize_slug(coordinate),
        candidate.k,
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _atomic_write_text(path: Path, content: str, *, overwrite: bool) -> None:
    """Publish complete text in one filesystem operation from the destination directory."""
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        assert temporary_path is not None
        if overwrite:
            os.replace(temporary_path, path)
        else:
            os.link(temporary_path, path)
            temporary_path.unlink()
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def compile_plan_shards(
    grid_id: str,
    specs: Sequence[ExperimentSpec],
    *,
    shard_size: int,
    output_dir: Path | None = None,
) -> list[PlanShard]:
    """Compile deterministic, independently dispatchable bounded shards."""
    existing_manifest: dict[str, Any] | None = None
    existing_entries: list[dict[str, Any]] = []
    start_index = 0
    plan_dir: Path | None = None
    manifest_path: Path | None = None
    if output_dir is not None:
        plan_dir = output_dir / "_plan"
        plan_dir.mkdir(parents=True, exist_ok=True)
        grid_slug = sanitize_slug(grid_id)
        manifest_path = plan_dir / f"manifest-{grid_slug}.json"
        shard_filename = re.compile(
            rf"{re.escape(grid_slug)}-s\d{{5,}}-[0-9a-f]{{12}}\.json"
        )
        if manifest_path.exists():
            try:
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"incompatible plan manifest {manifest_path}: {exc}") from exc
            if not isinstance(loaded, dict):
                raise ValueError(f"incompatible plan manifest {manifest_path}: expected object")
            if (
                loaded.get("schema_version") != 1
                or loaded.get("grid_id") != grid_id
                or loaded.get("shard_size") != shard_size
                or not isinstance(loaded.get("shards"), list)
            ):
                raise ValueError(
                    f"incompatible plan manifest {manifest_path}: "
                    "schema_version, grid_id, and shard_size must match"
                )
            existing_manifest = loaded
            existing_entries = loaded["shards"]
            indices = [entry.get("index") for entry in existing_entries if isinstance(entry, dict)]
            if (
                len(indices) != len(existing_entries)
                or indices != list(range(len(existing_entries)))
                or any(
                    not isinstance(entry.get("path"), str)
                    or not (plan_dir / entry["path"]).is_file()
                    for entry in existing_entries
                )
            ):
                raise ValueError(
                    f"incompatible plan manifest {manifest_path}: "
                    "shards must have contiguous indices and existing paths"
                )
            if (
                not isinstance(loaded.get("spec_count"), int)
                or not isinstance(loaded.get("trial_count"), int)
                or any(
                    not isinstance(entry.get("spec_count"), int)
                    or not isinstance(entry.get("trial_count"), int)
                    for entry in existing_entries
                )
                or loaded["spec_count"]
                != sum(entry["spec_count"] for entry in existing_entries)
                or loaded["trial_count"]
                != sum(entry["trial_count"] for entry in existing_entries)
            ):
                raise ValueError(
                    f"incompatible plan manifest {manifest_path}: "
                    "aggregate counts must match shard entries"
                )
            declared_paths = {entry["path"] for entry in existing_entries}
            orphan_paths = {
                path.name
                for path in plan_dir.glob("*.json")
                if shard_filename.fullmatch(path.name)
            } - declared_paths
            if orphan_paths:
                raise ValueError(
                    f"incompatible plan manifest {manifest_path}: "
                    f"unreferenced shard files {sorted(orphan_paths)}"
                )
            start_index = len(existing_entries)
        else:
            orphan_paths = {
                path.name
                for path in plan_dir.glob("*.json")
                if shard_filename.fullmatch(path.name)
            }
            if orphan_paths:
                raise ValueError(
                    f"incompatible plan directory {plan_dir}: manifest is missing for "
                    f"shard files {sorted(orphan_paths)}"
                )

    shards: list[PlanShard] = []
    for relative_index, offset in enumerate(range(0, len(specs), shard_size)):
        index = start_index + relative_index
        members = list(specs[offset : offset + shard_size])
        entries = []
        for spec in members:
            payload = spec.model_dump(mode="json")
            entries.append({
                "name": spec.name,
                "sha256": (
                    "sha256:"
                    + hashlib.sha256(_canonical_json(payload).encode()).hexdigest()
                ),
                "attempts": spec.attempts,
                "agent": spec.agent,
                "model": spec.model,
                "grid_point": spec.grid_point,
            })
        base = {
            "schema_version": 1,
            "grid_id": grid_id,
            "index": index,
            "specs": entries,
            "trial_count": sum(spec.attempts for spec in members),
            "estimated_cost_usd": round(
                sum(spec.est_cost_usd or 0.0 for spec in members), 8
            ),
        }
        digest = "sha256:" + hashlib.sha256(
            _canonical_json(base).encode()
        ).hexdigest()
        shard_id = f"{sanitize_slug(grid_id)}-s{index:05d}-{digest[7:19]}"
        payload = {**base, "shard_id": shard_id, "sha256": digest}
        path = None
        if plan_dir is not None:
            path = plan_dir / f"{shard_id}.json"
            encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
            if path.exists() and path.read_text(encoding="utf-8") != encoded:
                raise FileExistsError(f"plan shard path collision: {path}")
            if not path.exists():
                _atomic_write_text(path, encoded, overwrite=False)
        shards.append(PlanShard(
            shard_id=shard_id,
            index=index,
            spec_names=tuple(spec.name for spec in members),
            trial_count=base["trial_count"],
            estimated_cost_usd=base["estimated_cost_usd"],
            sha256=digest,
            path=path,
        ))

    if manifest_path is not None and shards:
        new_entries = [
            {
                "shard_id": item.shard_id,
                "index": item.index,
                "sha256": item.sha256,
                "spec_count": len(item.spec_names),
                "trial_count": item.trial_count,
                "estimated_cost_usd": item.estimated_cost_usd,
                "path": item.path.name if item.path else None,
            }
            for item in shards
        ]
        manifest = {
            "schema_version": 1,
            "grid_id": grid_id,
            "shard_size": shard_size,
            "spec_count": (
                int(existing_manifest.get("spec_count", 0)) if existing_manifest else 0
            ) + len(specs),
            "trial_count": (
                int(existing_manifest.get("trial_count", 0)) if existing_manifest else 0
            ) + sum(item.trial_count for item in shards),
            "shards": [*existing_entries, *new_entries],
        }
        _atomic_write_text(
            manifest_path,
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            overwrite=True,
        )
    return shards




def find_existing_grid_points(
    grid_id: str,
    *,
    repo_root: Path | None = None,
    extra_dirs: Sequence[Path] = (),
) -> set[str]:
    """Return stable point identities already present in queue or output."""
    root = repo_root or Path.cwd()
    points: set[str] = set()
    search_dirs: list[Path] = []
    queue_root = root / "queue"
    if queue_root.is_dir():
        states = (
            "proposed", "pending", "approved", "waiting",
            "rejected", "running", "done", "failed",
        )
        search_dirs.extend(queue_root / state for state in states if (queue_root / state).is_dir())
    for extra in extra_dirs:
        if extra.is_dir() and extra not in search_dirs:
            search_dirs.append(extra)

    for directory in search_dirs:
        for json_file in directory.glob("*.json"):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict) or data.get("grid_id") != grid_id:
                continue
            grid_point = data.get("grid_point")
            point = grid_point if isinstance(grid_point, dict) else {}
            stored_id = point.get("point_id")
            if isinstance(stored_id, str):
                points.add(stored_id)
                continue
            task_ref = str(point.get("task_ref") or data.get("task") or "")
            agent = str(point.get("agent") or data.get("agent") or "")
            model = point.get("model") or data.get("model")
            agent_key = (
                f"{agent}-{model}" if model and agent not in CONTROL_ADAPTERS else agent
            )
            preamble = str(point.get("preamble") or "none")
            attempts = int(point.get("k") or data.get("attempts") or 1)
            arm_id = str(point["arm_id"]) if point.get("arm_id") else None
            factor_values = point.get("factors")
            factors = factor_values if isinstance(factor_values, dict) else {}
            points.add(_point_id(
                task_ref, agent_key, preamble, attempts,
                arm_id=arm_id, factor_values=factors,
            ))
            points.add(_point_id(
                task_ref, agent, preamble, attempts,
                arm_id=arm_id, factor_values=factors,
            ))
    return points


def _matches_str(target: str | None, val: Any) -> bool:
    return target in val if isinstance(val, (list, tuple, set)) else target == str(val)


def _point_matches_constraint(
    task_ref: str,
    agent_spec: AgentSpec,
    agent_key: str,
    preamble: str,
    k: int,
    arm_id: str,
    factor_values: dict[str, Any],
    constraint: dict[str, Any],
) -> bool:
    """Check whether a grid point matches an exclusion constraint."""
    for key, value in constraint.items():
        if key in {"task", "task_ref", "task_refs"}:
            if not _matches_str(task_ref, value):
                return False
        elif key in {"agent", "agents"}:
            allowed = value if isinstance(value, (list, tuple, set)) else [str(value)]
            if agent_spec.agent not in allowed and agent_key not in allowed:
                return False
        elif key in {"model", "models"}:
            if not _matches_str(agent_spec.model, value):
                return False
        elif key in {"preamble", "preambles"}:
            if not _matches_str(preamble, value):
                return False
        elif key in {"k", "attempts"}:
            allowed = value if isinstance(value, (list, tuple, set)) else [value]
            if k not in [int(item) for item in allowed]:
                return False
        elif key in {"arm", "arm_id", "arms"}:
            if not _matches_str(arm_id, value):
                return False
        elif key.startswith("factor."):
            factor_name = key.removeprefix("factor.")
            allowed = value if isinstance(value, (list, tuple, set)) else [value]
            if factor_values.get(factor_name) not in allowed:
                return False
        else:
            raise ValueError(f"unknown grid constraint coordinate {key!r}")
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

    # 2. Build candidate points across tasks, treatments, factors, and attempts.
    assert grid_spec.axes is not None
    tasks = [_normalize_task_item(item) for item in grid_spec.axes.task_refs]
    attempts = list(grid_spec.axes.k)
    treatments: list[
        tuple[str | None, AgentSpec, str, dict[str, Any]]
    ] = []
    if grid_spec.axes.arms:
        treatments.extend(
            (arm.arm_id, arm.agent, arm.preamble, dict(arm.factor_overrides))
            for arm in grid_spec.axes.arms
        )
    else:
        for agent in grid_spec.axes.agents:
            agent_spec = _normalize_agent_item(agent)
            for preamble in grid_spec.axes.preamble:
                treatments.append((None, agent_spec, preamble, {}))

    candidates: list[CandidatePoint] = []
    deduped: list[DedupeRecord] = []
    for task_spec in tasks:
        for arm_id, agent_spec, preamble, overrides in treatments:
            agent_key = (
                f"{agent_spec.agent}-{agent_spec.model}"
                if agent_spec.model and agent_spec.agent not in CONTROL_ADAPTERS
                else agent_spec.agent
            )
            for factor_values in _factor_combinations(grid_spec.axes.factors, overrides):
                for k in attempts:
                    point_id = _point_id(
                        task_spec.task, agent_key, preamble, k,
                        arm_id=arm_id, factor_values=factor_values,
                    )
                    if any(
                        _point_matches_constraint(
                            task_spec.task, agent_spec, agent_key, preamble, k,
                            arm_id or agent_key, factor_values, constraint,
                        )
                        for constraint in grid_spec.constraints
                    ):
                        continue
                    if point_id in existing_points:
                        deduped.append(DedupeRecord(
                            grid_id=grid_id,
                            task=task_spec.task,
                            agent=agent_key,
                            preamble=preamble,
                            attempts=k,
                            arm_id=arm_id,
                            factor_values=factor_values,
                        ))
                        continue
                    candidates.append(CandidatePoint(
                        task_spec=task_spec,
                        agent_spec=agent_spec,
                        agent_key=agent_key,
                        provider_key=agent_spec.agent,
                        preamble=preamble,
                        arm_id=arm_id,
                        factor_values=factor_values,
                        point_id=point_id,
                        k=k,
                    ))

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
        _candidate_spec_name(grid_spec.name or grid_id, candidate)
        for candidate in ordered_candidates
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

        spec_name = _candidate_spec_name(grid_spec.name or grid_id, cand)

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
                    arm_id=cand.arm_id,
                    factor_values=dict(cand.factor_values),
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
                        arm_id=cand.arm_id,
                        factor_values=dict(cand.factor_values),
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
                    arm_id=cand.arm_id,
                    factor_values=dict(cand.factor_values),
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
                    arm_id=cand.arm_id,
                    factor_values=dict(cand.factor_values),
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
                    arm_id=cand.arm_id,
                    factor_values=dict(cand.factor_values),
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
                        arm_id=cand.arm_id,
                        factor_values=dict(cand.factor_values),
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
                        arm_id=cand.arm_id,
                        factor_values=dict(cand.factor_values),
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
                        arm_id=cand.arm_id,
                        factor_values=dict(cand.factor_values),
                        reason=(
                            f"provider {provider_key} max_cost_usd limit "
                            f"(${p_lim.max_cost_usd:.2f}) would be exceeded"
                        ),
                    )
                )
                continue

        # Construct ExperimentSpec
        hypothesis = _render_hypothesis(
            grid_spec, task_spec, agent_spec, preamble, k,
            arm_id=cand.arm_id, factor_values=cand.factor_values,
        )
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
                "point_id": cand.point_id,
                "task_ref": task_spec.task,
                "arm_id": cand.arm_id,
                "agent": agent_spec.agent,
                "model": agent_spec.model,
                "preamble": preamble,
                "factors": cand.factor_values,
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

    # Write or submit only outside dry-run mode.
    plan_output: Path | None = None
    if not dry_run and submit:
        executor = Executor.from_repo(root)
        for spec in specs:
            path, _decision = executor.submit(spec)
            submitted_specs.append(spec.name)
            written_paths.append(path)
    elif not dry_run and output_dir is not None:
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        compile_plan_shards(
            grid_id,
            (),
            shard_size=grid_spec.shard_size,
            output_dir=out_path,
        )
        for spec in specs:
            file_path = out_path / f"{spec.name}.json"
            if file_path.exists():
                raise FileExistsError(
                    f"Spec file already exists and cannot be overwritten: {file_path}. "
                    "Existing points must be resumed (deduped) or removed, never overwritten."
                )
            _atomic_write_text(
                file_path, spec.model_dump_json(indent=2), overwrite=False
            )
            written_paths.append(file_path)
        plan_output = out_path
    shards = compile_plan_shards(
        grid_id,
        specs,
        shard_size=grid_spec.shard_size,
        output_dir=plan_output,
    )
    return GridGenerationResult(
        grid_id=grid_id,
        specs=specs,
        shards=shards,
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

    # screen subcommand
    screen_parser = subparsers.add_parser(
        "screen",
        help="Staged difficulty screening and follow-up generation.",
    )
    screen_subparsers = screen_parser.add_subparsers(
        dest="screen_command", help="Screen subcommands"
    )

    # screen stage1
    s1_parser = screen_subparsers.add_parser(
        "stage1",
        help="Emit Stage 1 screening specs (k=1) across tasks and model levels.",
    )
    s1_parser.add_argument("spec", type=Path, help="Path to ScreenSpec YAML/JSON file.")
    s1_parser.add_argument("-o", "--output-dir", type=Path, default=None)
    s1_parser.add_argument("--submit", action="store_true")
    s1_parser.add_argument("--dry-run", action="store_true", default=False)
    s1_parser.add_argument("--json", action="store_true")

    # screen analyze
    sa_parser = screen_subparsers.add_parser(
        "analyze",
        help="Analyze completed Stage 1 results and classify task separation.",
    )
    sa_parser.add_argument("screen_id_or_spec", help="Screen ID or path to ScreenSpec file.")
    sa_parser.add_argument("--jobs-dir", type=Path, default=None)
    sa_parser.add_argument("--json", action="store_true")

    # screen stage2
    s2_parser = screen_subparsers.add_parser(
        "stage2",
        help="Emit Stage 2 follow-up specs (k=3) for separating tasks only.",
    )
    s2_parser.add_argument("spec", type=Path, help="Path to ScreenSpec YAML/JSON file.")
    s2_parser.add_argument("-o", "--output-dir", type=Path, default=None)
    s2_parser.add_argument("--submit", action="store_true")
    s2_parser.add_argument("--dry-run", action="store_true", default=False)
    s2_parser.add_argument("--jobs-dir", type=Path, default=None)
    s2_parser.add_argument("--json", action="store_true")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for `python -m evallab.ladder`."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.command == "screen":
        if not getattr(args, "screen_command", None):
            parser.parse_args(["screen", "--help"])
            return 1
        return _handle_screen_cli(args)

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
                        "preamble": sk.preamble, "attempts": sk.attempts,
                        "reason": sk.reason, "arm_id": sk.arm_id,
                        "factor_values": sk.factor_values,
                    }
                    for sk in result.skipped
                ],
                "deduped": [
                    {
                        "grid_id": d.grid_id, "task": d.task, "agent": d.agent,
                        "preamble": d.preamble, "attempts": d.attempts,
                        "reason": d.reason, "arm_id": d.arm_id,
                        "factor_values": d.factor_values,
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


def _handle_screen_cli(args: argparse.Namespace) -> int:
    """Handle ladder screen subcommands."""
    cmd = args.screen_command
    if cmd == "stage1":
        screen_spec = load_screen_spec(args.spec)
        dry_run = args.dry_run or (not args.submit and args.output_dir is None)
        result = generate_stage1_screen(
            screen_spec,
            output_dir=args.output_dir,
            submit=args.submit,
            dry_run=dry_run,
        )
        if args.json:
            out = {
                "screen_id": result.grid_id,
                "stage": 1,
                "total_specs": result.total_specs,
                "total_trials": result.total_trials,
                "specs": [s.model_dump(mode="json") for s in result.specs],
                "written_files": [str(p) for p in result.written_paths],
            }
            print(json.dumps(out, indent=2))
        else:
            print(f"LADDER Screen Stage 1 Generation: {result.grid_id}")
            print(
                f"Generated {result.total_specs} specs "
                f"({result.total_trials} trials, k={screen_spec.initial_k})"
            )
            print(
                f"Tasks: {len(screen_spec.tasks)} | "
                f"Model levels: {len(screen_spec.model_levels)}"
            )
            if result.written_paths:
                print(
                    f"Written to: {result.written_paths[0].parent} "
                    f"({len(result.written_paths)} files)"
                )
            elif dry_run:
                print("Dry-run mode: no files written to disk.")
            print("Human approval preserved (pending review before dispatch).")
        return 0

    elif cmd == "analyze":
        target = args.screen_id_or_spec
        target_path = Path(target)
        screen_id = target
        spec_obj = None
        if target_path.is_file():
            spec_obj = load_screen_spec(target_path)
            screen_id = spec_obj.screen_id

        report = analyze_screen_results(
            screen_id,
            spec=spec_obj,
            jobs_dir=args.jobs_dir,
        )
        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            print(report.summary())
        return 0

    elif cmd == "stage2":
        screen_spec = load_screen_spec(args.spec)
        report = analyze_screen_results(
            screen_spec.screen_id,
            spec=screen_spec,
            jobs_dir=args.jobs_dir,
        )
        dry_run = args.dry_run or (not args.submit and args.output_dir is None)
        result = generate_stage2_screen(
            report,
            screen_spec,
            output_dir=args.output_dir,
            submit=args.submit,
            dry_run=dry_run,
        )
        if args.json:
            out = {
                "screen_id": result.grid_id,
                "stage": 2,
                "separating_tasks": report.separating_tasks,
                "stopped_tasks": report.stopped_tasks,
                "total_specs": result.total_specs,
                "total_trials": result.total_trials,
                "specs": [s.model_dump(mode="json") for s in result.specs],
                "written_files": [str(p) for p in result.written_paths],
            }
            print(json.dumps(out, indent=2))
        else:
            print(f"LADDER Screen Stage 2 Follow-Up Generation: {result.grid_id}")
            sep_str = ", ".join(report.separating_tasks) or "none"

            stop_str = ", ".join(report.stopped_tasks) or "none"
            print(
                f"Separating tasks selected for follow-up "
                f"({len(report.separating_tasks)}): {sep_str}"
            )
            print(f"Stopped tasks ({len(report.stopped_tasks)}): {stop_str}")
            print(
                f"Generated {result.total_specs} follow-up specs "
                f"({result.total_trials} trials, k={screen_spec.followup_k})"
            )
            if result.written_paths:
                print(
                    f"Written to: {result.written_paths[0].parent} "
                    f"({len(result.written_paths)} files)"
                )
            elif dry_run:
                print("Dry-run mode: no files written to disk.")
            print("Human approval preserved (no automatic paid dispatch).")
        return 0

    return 1



if __name__ == "__main__":
    sys.exit(main())
