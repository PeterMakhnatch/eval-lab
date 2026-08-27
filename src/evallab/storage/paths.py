from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DERIVED_ROOT_ENV = "EVALLAB_DERIVED_ROOT"

Notifier = Callable[[str], None]

_ANNOUNCED: set[tuple[str, str]] = set()


def shared_checkout_root(repo_root: Path) -> Path:
    """Return the primary checkout for a repository or linked worktree."""
    root = repo_root.resolve()
    git_marker = root / ".git"
    if git_marker.is_dir():
        return root
    if not git_marker.is_file():
        return root

    prefix = "gitdir:"
    marker = git_marker.read_text().strip()
    if not marker.lower().startswith(prefix):
        return root
    git_dir = Path(marker[len(prefix) :].strip())
    if not git_dir.is_absolute():
        git_dir = (root / git_dir).resolve()
    common_marker = git_dir / "commondir"
    if not common_marker.is_file():
        return root
    common_dir = Path(common_marker.read_text().strip())
    if not common_dir.is_absolute():
        common_dir = (git_dir / common_dir).resolve()
    if common_dir.name != ".git":
        return root
    return common_dir.parent.resolve()


@dataclass(frozen=True)
class DerivedRootResolution:
    """Where the derived Parquet root came from, and whose tree it belongs to.

    The lab keeps one derived store per machine because it is a rebuildable
    projection of the single PostgreSQL catalog: a per-worktree copy would
    disagree with the catalog every worktree shares. Sharing is therefore kept,
    but it is never implied — `implicit` marks a resolution that crossed into
    another checkout without anybody naming it, and `describe()` is the line an
    operator reads instead of guessing.
    """

    path: Path
    origin: str
    invoking_root: Path
    base_root: Path
    implicit: bool

    @property
    def is_foreign(self) -> bool:
        """True when the resolved root lies outside the invoking checkout."""
        return not self.path.is_relative_to(self.invoking_root)

    def describe(self) -> str:
        if not self.is_foreign:
            return f"{self.path} (this checkout, {self.origin})"
        return f"{self.path} (shared, owned by {self.base_root}, {self.origin})"

    def notice(self) -> str | None:
        """The operator-facing line for an unnamed cross-checkout resolution."""
        if not (self.is_foreign and self.implicit):
            return None
        return (
            f"evallab: derived root {self.path} belongs to {self.base_root}, "
            f"not to this checkout {self.invoking_root}; "
            f"set {DERIVED_ROOT_ENV} to an absolute path to choose another."
        )


def resolve_derived_root(
    repo_root: Path,
    *,
    explicit: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> DerivedRootResolution:
    """Resolve the derived Parquet root and record how the answer was reached.

    Pure: it reads the environment mapping and the worktree's Git markers, and
    reports. Explicit caller paths stay relative to the invoking checkout; the
    environment override and the default are relative to the primary checkout
    so every linked worktree observes the same derived store as the shared
    PostgreSQL catalog.
    """
    root = repo_root.resolve()
    if explicit is not None:
        path = explicit.resolve() if explicit.is_absolute() else (root / explicit).resolve()
        return DerivedRootResolution(
            path=path,
            origin="explicit path",
            invoking_root=root,
            base_root=root,
            implicit=False,
        )

    environment = os.environ if environ is None else environ
    configured = environment.get(DERIVED_ROOT_ENV)
    shared_root = shared_checkout_root(root)
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_absolute():
            return DerivedRootResolution(
                path=candidate.resolve(),
                origin=f"${DERIVED_ROOT_ENV}",
                invoking_root=root,
                base_root=root,
                implicit=False,
            )
        return DerivedRootResolution(
            path=(shared_root / candidate).resolve(),
            origin=f"${DERIVED_ROOT_ENV} relative to the primary checkout",
            invoking_root=root,
            base_root=shared_root,
            implicit=True,
        )
    return DerivedRootResolution(
        path=(shared_root / "derived/parquet").resolve(),
        origin="default",
        invoking_root=root,
        base_root=shared_root,
        implicit=True,
    )


def _announce_once(notice: str, resolution: DerivedRootResolution) -> None:
    key = (str(resolution.invoking_root), str(resolution.path))
    if key in _ANNOUNCED:
        return
    _ANNOUNCED.add(key)
    print(notice, file=sys.stderr)


def derived_root_from_environment(
    repo_root: Path,
    *,
    explicit: Path | None = None,
    environ: Mapping[str, str] | None = None,
    notify: Notifier | None = None,
) -> Path:
    """Resolve the one shared Parquet root, announcing a cross-checkout answer.

    Every caller reaches the derived store through this function, so this is
    where the sharing is made visible: when a linked worktree silently inherits
    another checkout's derived root, `notify` receives one line saying whose
    root it is. The default notifier writes to stderr once per invoking
    tree and root, so an interactive command says it and a nightly loop does
    not repeat it. Pass `notify` (tests do) to capture instead of print.
    """
    resolution = resolve_derived_root(repo_root, explicit=explicit, environ=environ)
    notice = resolution.notice()
    if notice is not None:
        if notify is None:
            _announce_once(notice, resolution)
        else:
            notify(notice)
    return resolution.path


ParquetLayout = Literal["hot", "job", "cold-table", "cold-day", "directory", "root"]
PARQUET_LAYOUT_ORDER: tuple[ParquetLayout, ...] = (
    "hot",
    "job",
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
        dt: str | None = None,
        prefer_job_level: bool = False,
    ) -> tuple[Path, ...]:
        selected = [
            partition
            for partition in self.partitions
            if partition.table == table
            and (layouts is None or partition.layout in layouts)
            and (job_id is None or partition.job_id == job_id)
            and (dt is None or partition.dt == dt)
        ]
        if prefer_job_level and any(partition.layout == "job" for partition in selected):
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
        if prefer_job_level and "job" in selected:
            selected = [layout for layout in selected if layout != "hot"]
        if not selected and fallback:
            selected = list(permitted)
            if prefer_job_level and "job" in selected and "hot" in selected:
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
        "cold-table": f"compact/{table}/dt=*/part*.parquet",
        "cold-day": f"compact/dt=*/{table}.parquet",
        "directory": f"{table}/*.parquet",
        "root": f"{table}.parquet",
    }
    return patterns[layout]
