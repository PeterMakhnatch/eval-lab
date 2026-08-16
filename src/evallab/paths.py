from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

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
