from __future__ import annotations

from pathlib import Path


def event_log_paths(path: Path) -> tuple[Path, ...]:
    """Return retained event segments from oldest archive to active log."""
    archives: list[tuple[int, Path]] = []
    for candidate in path.parent.glob(f"{path.name}.*"):
        suffix = candidate.name.removeprefix(f"{path.name}.")
        if suffix.isdigit() and candidate.is_file():
            archives.append((int(suffix), candidate))
    return tuple(candidate for _, candidate in sorted(archives, reverse=True)) + (
        (path,) if path.is_file() else ()
    )
