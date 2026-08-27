from pathlib import Path

from evallab.storage.paths import (
    DERIVED_ROOT_ENV,
    derived_root_from_environment,
    discover_parquet_partitions,
    resolve_derived_root,
    shared_checkout_root,
)


def linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """Build a primary checkout with one linked worktree, as `git worktree` does."""
    primary = tmp_path / "primary"
    worktree = primary / ".worktrees/solidify"
    git_dir = primary / ".git/worktrees/solidify"
    git_dir.mkdir(parents=True)
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n")
    (git_dir / "commondir").write_text("../..\n")
    return primary, worktree


def test_primary_checkout_uses_its_derived_root(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()

    assert shared_checkout_root(tmp_path) == tmp_path
    resolution = resolve_derived_root(tmp_path, environ={})

    assert resolution.path == tmp_path / "derived/parquet"
    assert not resolution.is_foreign
    assert resolution.notice() is None


def test_linked_worktree_never_resolves_a_foreign_derived_root_silently(
    tmp_path: Path,
) -> None:
    """F-13: sharing is allowed; resolving into another checkout unannounced is not."""
    primary, worktree = linked_worktree(tmp_path)
    announced: list[str] = []

    root = derived_root_from_environment(worktree, environ={}, notify=announced.append)

    if root.is_relative_to(worktree):
        assert announced == []
        return
    assert announced, f"{root} is outside {worktree} but nothing said so"
    assert str(primary) in announced[0]
    assert str(root) in announced[0]


def test_shared_derived_root_names_the_checkout_that_owns_it(tmp_path: Path) -> None:
    primary, worktree = linked_worktree(tmp_path)

    resolution = resolve_derived_root(worktree, environ={})

    assert resolution.path == primary / "derived/parquet"
    assert resolution.is_foreign
    assert resolution.base_root == primary
    assert str(primary) in resolution.describe()


def test_relative_environment_override_is_shared_and_announced(tmp_path: Path) -> None:
    primary, worktree = linked_worktree(tmp_path)
    announced: list[str] = []

    root = derived_root_from_environment(
        worktree,
        environ={DERIVED_ROOT_ENV: "shared/facts"},
        notify=announced.append,
    )

    assert root == primary / "shared/facts"
    assert announced and str(primary) in announced[0]


def test_named_absolute_and_explicit_paths_are_not_announced(tmp_path: Path) -> None:
    _, worktree = linked_worktree(tmp_path)
    elsewhere = tmp_path / "chosen/facts"
    announced: list[str] = []

    absolute = derived_root_from_environment(
        worktree,
        environ={DERIVED_ROOT_ENV: str(elsewhere)},
        notify=announced.append,
    )
    explicit = derived_root_from_environment(
        worktree,
        explicit=Path("scratch/facts"),
        environ={},
        notify=announced.append,
    )

    assert absolute == elsewhere
    assert explicit == worktree / "scratch/facts"
    assert announced == []


def test_default_notifier_writes_the_notice_to_stderr(tmp_path: Path, capsys) -> None:
    primary, worktree = linked_worktree(tmp_path)

    derived_root_from_environment(worktree, environ={})

    assert str(primary) in capsys.readouterr().err


def test_partition_discovery_classifies_every_supported_layout(tmp_path: Path) -> None:
    root = tmp_path / "parquet"
    relative_paths = (
        "job_id=job-1/trial_id=trial-1/trial_facts.parquet",
        "job_id=job-1/trial_id=legacy/jobs.parquet",
        "job_id=job-1/jobs.parquet",
        "compact/constraint_facts/dt=2026-08-25/part0.parquet",
        "compact/dt=2026-08-25/reward_facts.parquet",
        "behavior_labels/batch.parquet",
        "evidence_coverage.parquet",
        "unsupported/nested/table.parquet",
    )
    for relative in relative_paths:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    discovery = discover_parquet_partitions(root)

    assert {
        (
            partition.layout,
            partition.table,
            partition.job_id,
            partition.trial_id,
            partition.dt,
        )
        for partition in discovery.partitions
    } == {
        ("hot", "trial_facts", "job-1", "trial-1", None),
        ("hot", "jobs", "job-1", "legacy", None),
        ("job", "jobs", "job-1", None, None),
        ("cold-table", "constraint_facts", None, None, "2026-08-25"),
        ("cold-day", "reward_facts", None, None, "2026-08-25"),
        ("directory", "behavior_labels", None, None, None),
        ("root", "evidence_coverage", None, None, None),
    }
    assert discovery.job_directories == (root / "job_id=job-1",)

    jobs_patterns = discovery.table_patterns("jobs", prefer_job_level=True)
    assert jobs_patterns == (str(root / "job_id=*/jobs.parquet"),)
