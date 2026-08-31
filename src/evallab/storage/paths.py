from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DERIVED_ROOT_ENV = "EVALLAB_DERIVED_ROOT"

Notifier = Callable[[str], None]

_ANNOUNCED: set[tuple[str, str]] = set()


def shared_checkout_root(repo_root: Path) -> Path:
    """Return the primary checkout for a repository or linked worktree."""
    resolved = repo_root.resolve()
    git_dir = resolved / ".git"
    if not git_dir.exists():
        return resolved
    if git_dir.is_dir():
        return resolved
    if not git_dir.is_file():
        return resolved

    try:
        content = git_dir.read_text().strip()
    except OSError:
        return resolved

    if not content.startswith("gitdir:"):
        return resolved

    gitdir_path = Path(content.removeprefix("gitdir:").strip())
    if not gitdir_path.is_absolute():
        gitdir_path = (resolved / gitdir_path).resolve()

    common_dir_file = gitdir_path / "commondir"
    if not common_dir_file.is_file():
        return resolved

    try:
        common_rel = common_dir_file.read_text().strip()
    except OSError:
        return resolved

    common_dir = (gitdir_path / common_rel).resolve()
    return common_dir.parent.resolve()


@dataclass(frozen=True)
class DerivedRootResolution:
    """Where the derived Parquet root came from, and whose tree it belongs to.

    Attributes
    ----------
    path:
        The absolute Path that parquet reads and writes should target.
    source:
        How the path was reached:
        - "explicit"     — passed via --derived-dir or equivalent parameter.
        - "environment"  — read from EVALLAB_DERIVED_ROOT.
        - "shared_repo"  — computed from the primary git checkout of a linked
                           worktree.
        - "local_repo"   — resolved to <invoking_root>/derived/parquet because
                           this checkout is already the primary, or because git
                           discovery could not locate a parent checkout.
    invoking_root:
        The repo root (or linked worktree root) where the call originated.
    owner_root:
        The repo root that owns `path`. Equals invoking_root for "local_repo"
        and "explicit"; differs when a linked worktree redirects to the
        primary checkout.
    """

    path: Path
    source: Literal["explicit", "environment", "shared_repo", "local_repo"]
    invoking_root: Path
    owner_root: Path

    @property
    def is_cross_checkout(self) -> bool:
        """True when this resolution points outside the invoking worktree."""
        return self.invoking_root.resolve() != self.owner_root.resolve()

    def announce_cross_checkout(self, notifier: Notifier | None = None) -> None:
        """Emit a one-line notification to stderr if pointing to another tree."""
        if not self.is_cross_checkout:
            return
        notice = (
            f"[evallab] Using shared Parquet root from primary checkout: "
            f"{self.path} (invoked from {self.invoking_root})"
        )
        if notifier is not None:
            notifier(notice)
        else:
            _announce_once(notice, self)


def resolve_derived_root(
    repo_root: Path | None = None,
    *,
    explicit: Path | None = None,
    environ: dict[str, str] | None = None,
) -> DerivedRootResolution:
    """Resolve the derived Parquet root and record how the answer was reached.

    Precedence:
    1. `explicit` parameter (e.g. from --derived-dir).
    2. `EVALLAB_DERIVED_ROOT` environment variable.
    3. Primary git checkout's `derived/parquet` when in a linked worktree.
    4. `<repo_root>/derived/parquet` (local default).
    """
    invoking = (repo_root or Path.cwd()).resolve()

    if explicit is not None:
        explicit_resolved = (
            explicit.resolve() if explicit.is_absolute() else (invoking / explicit).resolve()
        )
        return DerivedRootResolution(
            path=explicit_resolved,
            source="explicit",
            invoking_root=invoking,
            owner_root=invoking,
        )

    env_map = environ if environ is not None else os.environ
    env_val = env_map.get(DERIVED_ROOT_ENV)
    if env_val:
        env_path = Path(env_val)
        env_resolved = (
            env_path.resolve() if env_path.is_absolute() else (invoking / env_path).resolve()
        )
        return DerivedRootResolution(
            path=env_resolved,
            source="environment",
            invoking_root=invoking,
            owner_root=invoking,
        )

    primary = shared_checkout_root(invoking)
    if primary != invoking:
        return DerivedRootResolution(
            path=(primary / "derived" / "parquet").resolve(),
            source="shared_repo",
            invoking_root=invoking,
            owner_root=primary,
        )

    return DerivedRootResolution(
        path=(invoking / "derived" / "parquet").resolve(),
        source="local_repo",
        invoking_root=invoking,
        owner_root=invoking,
    )


def _announce_once(notice: str, resolution: DerivedRootResolution) -> None:
    key = (str(resolution.invoking_root), str(resolution.path))
    if key in _ANNOUNCED:
        return
    _ANNOUNCED.add(key)
    print(notice, file=sys.stderr)


def derived_root_from_environment(
    repo_root: Path | None = None,
    *,
    explicit: Path | None = None,
    announce: bool = True,
    environ: dict[str, str] | None = None,
) -> Path:
    """Resolve the one shared Parquet root, announcing a cross-checkout answer.

    Backward-compatible convenience function. Returns just the Path.
    Callers that need metadata about the resolution should call
    `resolve_derived_root()` directly.
    """
    resolution = resolve_derived_root(repo_root, explicit=explicit, environ=environ)
    if announce:
        resolution.announce_cross_checkout()
    return resolution.path


ParquetLayout = Literal["hot", "job", "revision", "cold-table", "cold-day", "directory", "root"]
PARQUET_LAYOUT_ORDER: tuple[ParquetLayout, ...] = (
    "hot",
    "job",
    "revision",
    "cold-table",
    "cold-day",
    "directory",
    "root",
)


@dataclass(frozen=True)
class ParquetPartition:
    """One supported physical Parquet partition."""

    path: Path
    table: str
    layout: ParquetLayout
    job_id: str | None = None
    revision_id: str | None = None
    trial_id: str | None = None
    dt: str | None = None


@dataclass(frozen=True)
class ParquetPartitionDiscovery:
    """Deterministic inventory of every supported Parquet layout under one root."""

    root: Path
    partitions: tuple[ParquetPartition, ...]
    job_directories: tuple[Path, ...]

    @property
    def table_names(self) -> frozenset[str]:
        return frozenset(partition.table for partition in self.partitions)

    def table_files(
        self,
        table: str,
        *,
        layouts: tuple[ParquetLayout, ...] | None = None,
        job_id: str | None = None,
        revision_id: str | None = None,
        dt: str | None = None,
        prefer_job_level: bool = False,
    ) -> tuple[Path, ...]:
        selected = [
            partition
            for partition in self.partitions
            if partition.table == table
            and (layouts is None or partition.layout in layouts)
            and (job_id is None or partition.job_id == job_id)
            and (revision_id is None or partition.revision_id == revision_id)
            and (dt is None or partition.dt == dt)
        ]
        if prefer_job_level and any(
            partition.layout in {"job", "revision"} for partition in selected
        ):
            selected = [partition for partition in selected if partition.layout != "hot"]
        return tuple(partition.path for partition in selected)

    def table_patterns(
        self,
        table: str,
        *,
        layouts: tuple[ParquetLayout, ...] | None = None,
        prefer_job_level: bool = False,
        fallback: bool = False,
    ) -> tuple[str, ...]:
        permitted = PARQUET_LAYOUT_ORDER if layouts is None else layouts
        selected = [
            layout
            for layout in permitted
            if any(
                partition.table == table and partition.layout == layout
                for partition in self.partitions
            )
        ]
        if prefer_job_level and any(lay in selected for lay in {"job", "revision"}):
            selected = [layout for layout in selected if layout != "hot"]
        if not selected and fallback:
            selected = list(permitted)
            if (
                prefer_job_level
                and any(lay in selected for lay in {"job", "revision"})
                and "hot" in selected
            ):
                selected.remove("hot")
        return tuple(str(self.root / _parquet_layout_pattern(layout, table)) for layout in selected)


def discover_parquet_partitions(root: Path) -> ParquetPartitionDiscovery:
    """Classify supported Parquet files once for attach and compaction consumers."""
    resolved = root.resolve()
    if not resolved.is_dir():
        return ParquetPartitionDiscovery(resolved, (), ())

    job_directories = tuple(
        sorted(
            path for path in resolved.glob("job_id=*") if path.is_dir() and path.parent == resolved
        )
    )
    partitions: list[ParquetPartition] = []
    for path in sorted(resolved.rglob("*.parquet")):
        if not path.is_file():
            continue
        partition = _classify_parquet_partition(resolved, path)
        if partition is not None:
            partitions.append(partition)
    return ParquetPartitionDiscovery(resolved, tuple(partitions), job_directories)


def _classify_parquet_partition(root: Path, path: Path) -> ParquetPartition | None:
    relative = path.relative_to(root)
    parts = relative.parts
    if len(parts) == 1:
        return ParquetPartition(path, path.stem, "root")
    if parts[0].startswith("job_id="):
        job_id = parts[0].removeprefix("job_id=")
        if len(parts) == 2:
            return ParquetPartition(path, path.stem, "job", job_id=job_id)
        if len(parts) == 3 and parts[1].startswith("revision_id="):
            return ParquetPartition(
                path,
                path.stem,
                "revision",
                job_id=job_id,
                revision_id=parts[1].removeprefix("revision_id="),
            )
        if len(parts) == 3 and parts[1].startswith("trial_id="):
            return ParquetPartition(
                path,
                path.stem,
                "hot",
                job_id=job_id,
                trial_id=parts[1].removeprefix("trial_id="),
            )
        return None
    if parts[0] == "compact":
        if len(parts) == 3 and parts[1].startswith("dt="):
            return ParquetPartition(
                path,
                path.stem,
                "cold-day",
                dt=parts[1].removeprefix("dt="),
            )
        if len(parts) == 4 and parts[2].startswith("dt=") and path.name.startswith("part"):
            return ParquetPartition(
                path,
                parts[1],
                "cold-table",
                dt=parts[2].removeprefix("dt="),
            )
        return None
    if len(parts) == 2:
        return ParquetPartition(path, parts[0], "directory")
    return None


def _parquet_layout_pattern(layout: ParquetLayout, table: str) -> str:
    patterns = {
        "hot": f"job_id=*/trial_id=*/{table}.parquet",
        "job": f"job_id=*/{table}.parquet",
        "revision": f"job_id=*/revision_id=*/{table}.parquet",
        "cold-table": f"compact/{table}/dt=*/part*.parquet",
        "cold-day": f"compact/dt=*/{table}.parquet",
        "directory": f"{table}/*.parquet",
        "root": f"{table}.parquet",
    }
    return patterns[layout]
