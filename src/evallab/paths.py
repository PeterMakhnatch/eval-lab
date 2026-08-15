from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

DERIVED_ROOT_ENV = "EVALLAB_DERIVED_ROOT"


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


def derived_root_from_environment(
    repo_root: Path,
    *,
    explicit: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the one shared Parquet root without inspecting credential values.

    Explicit CLI paths remain relative to the invoking checkout. The environment
    override and default are relative to the primary checkout so linked worktrees
    observe the same derived store as the shared PostgreSQL catalog.
    """
    root = repo_root.resolve()
    if explicit is not None:
        return explicit.resolve() if explicit.is_absolute() else (root / explicit).resolve()

    environment = os.environ if environ is None else environ
    configured = environment.get(DERIVED_ROOT_ENV)
    shared_root = shared_checkout_root(root)
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        return (shared_root / candidate).resolve()
    return (shared_root / "derived/parquet").resolve()
