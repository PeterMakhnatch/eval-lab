from pathlib import Path

from evallab.paths import DERIVED_ROOT_ENV, derived_root_from_environment, shared_checkout_root


def test_primary_checkout_uses_its_derived_root(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()

    assert shared_checkout_root(tmp_path) == tmp_path
    assert derived_root_from_environment(tmp_path, environ={}) == tmp_path / "derived/parquet"


def test_linked_worktree_uses_primary_checkout_derived_root(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    worktree = primary / ".worktrees/solidify"
    git_dir = primary / ".git/worktrees/solidify"
    git_dir.mkdir(parents=True)
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n")
    (git_dir / "commondir").write_text("../..\n")

    assert shared_checkout_root(worktree) == primary
    assert derived_root_from_environment(worktree, environ={}) == primary / "derived/parquet"


def test_environment_override_is_shared_but_explicit_path_is_local(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    worktree = primary / ".worktrees/solidify"
    git_dir = primary / ".git/worktrees/solidify"
    git_dir.mkdir(parents=True)
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n")
    (git_dir / "commondir").write_text("../..\n")

    assert derived_root_from_environment(
        worktree,
        environ={DERIVED_ROOT_ENV: "shared/facts"},
    ) == primary / "shared/facts"
    assert derived_root_from_environment(
        worktree,
        explicit=Path("scratch/facts"),
        environ={},
    ) == worktree / "scratch/facts"
