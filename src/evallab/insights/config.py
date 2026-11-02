"""Engine configuration: what to watch, how often each lens runs, which model.

The packaged defaults (``defaults/config.yaml`` and ``defaults/routes.yaml``)
are copied into the state directory by ``evallab insights init`` and read from
there, so an operator can tune cadence without a code change. Every decision
the scheduler makes from this file is recorded on the pass it creates.
"""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator

from evallab.insights.models import Lens, SourceKind
from evallab.schemas import ContractModel

CONFIG_NAME = "config.yaml"
ROUTES_NAME = "routes.yaml"


class SourceFamily(ContractModel):
    """A family of files the engine watches (globs are ``~``- and env-expanded)."""

    family: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    kind: SourceKind
    globs: list[str] = Field(min_length=1)
    exclude: list[str] = Field(default_factory=list)
    max_bytes: int = Field(default=400_000, ge=1)
    max_age_days: float | None = Field(default=None, gt=0)
    priority: int = Field(default=50, ge=0, le=100)


class SourceOverride(ContractModel):
    """An explicit file (used by backtests to freeze a prefix of a growing log)."""

    path: str
    family: str
    kind: SourceKind
    max_line: int | None = Field(default=None, ge=1)


class LensSchedule(ContractModel):
    enabled: bool = True
    cooldown_minutes: float = Field(ge=0)
    daily_cap: int = Field(ge=0)
    min_new_claims: int = Field(default=0, ge=0)
    min_new_insights: int = Field(default=0, ge=0)
    min_open_signals: int = Field(default=0, ge=0)
    min_pending: int = Field(default=0, ge=0)
    batch_size: int = Field(default=12, ge=1)
    max_pack_chars: int = Field(default=120_000, ge=1_000)
    #: Passes of this lens allowed at once (extraction backfills from several sources).
    max_parallel: int = Field(default=1, ge=1, le=8)


class RepoMirror(ContractModel):
    """A detached, engine-owned worktree kept at ``origin/main`` for committed results."""

    repo: str
    path: str
    refresh_minutes: float = Field(default=60, gt=0)


class EngineConfig(ContractModel):
    schema_version: Literal[1] = 1
    sources: list[SourceFamily] = Field(default_factory=list)
    overrides: list[SourceOverride] = Field(default_factory=list)
    tick_seconds: int = Field(default=300, ge=10)
    max_concurrent_passes: int = Field(default=2, ge=1, le=8)
    lenses: dict[Lens, LensSchedule] = Field(default_factory=dict)
    max_provider_used_percent: float = Field(default=85.0, gt=0, le=100)
    host_min_memory_level: int = Field(default=15, ge=0, le=100)
    host_max_swap_gb: float = Field(default=12.0, gt=0)
    board_top_n: int = Field(default=12, ge=1)
    repo_mirror: RepoMirror | None = None

    @model_validator(mode="after")
    def unique_families(self) -> EngineConfig:
        names = [family.family for family in self.sources]
        if len(names) != len(set(names)):
            raise ValueError("source family names must be unique")
        return self

    def schedule(self, lens: Lens) -> LensSchedule:
        found = self.lenses.get(lens)
        if found is None:
            raise KeyError(f"no schedule configured for lens {lens!r}")
        return found


class RouteSpec(ContractModel):
    """Ordered, deterministic model choice for one lens."""

    routes: list[str] = Field(min_length=1)
    thinking: Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"] = "medium"
    max_minutes: float = Field(default=25, gt=0, le=120)
    independent_of: list[Lens] = Field(default_factory=list)

    @model_validator(mode="after")
    def routes_have_provider(self) -> RouteSpec:
        for route in self.routes:
            if "/" not in route:
                raise ValueError(f"route {route!r} must be provider/model")
        return self


class RouteTable(ContractModel):
    schema_version: Literal[1] = 1
    lenses: dict[Lens, RouteSpec]

    def spec(self, lens: Lens) -> RouteSpec:
        found = self.lenses.get(lens)
        if found is None:
            raise KeyError(f"no route configured for lens {lens!r}")
        return found


def provider_of(route: str) -> str:
    return route.split("/", 1)[0]


def model_family(model: str) -> str:
    """Vendor family of a served or requested model id, for independence checks."""
    lowered = model.lower().split("/")[-1]
    for marker, family in (
        ("claude", "anthropic"),
        ("gemini", "google"),
        ("gpt", "openai"),
        ("kimi", "moonshot"),
        ("k3", "moonshot"),
        ("deepseek", "deepseek"),
        ("grok", "xai"),
        ("glm", "zhipu"),
    ):
        if marker in lowered:
            return family
    return provider_of(model) if "/" in model else lowered


def _default_text(name: str) -> str:
    return resources.files("evallab.insights").joinpath("defaults", name).read_text("utf-8")


def _expand(text: str) -> str:
    return os.path.expandvars(text)


def load_config(state_root: Path | None = None) -> EngineConfig:
    path = None if state_root is None else state_root / CONFIG_NAME
    text = path.read_text("utf-8") if path is not None and path.exists() else _default_text(CONFIG_NAME)
    return EngineConfig.model_validate(yaml.safe_load(_expand(text)))


def load_routes(state_root: Path | None = None) -> RouteTable:
    path = None if state_root is None else state_root / ROUTES_NAME
    text = path.read_text("utf-8") if path is not None and path.exists() else _default_text(ROUTES_NAME)
    return RouteTable.model_validate(yaml.safe_load(text))


def write_defaults(state_root: Path, *, overwrite: bool = False) -> list[Path]:
    """Copy the packaged defaults into ``state_root``; keep operator edits."""
    state_root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in (CONFIG_NAME, ROUTES_NAME):
        target = state_root / name
        if target.exists() and not overwrite:
            continue
        target.write_text(_default_text(name), "utf-8")
        written.append(target)
    return written
