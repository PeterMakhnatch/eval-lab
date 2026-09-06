"""Session-wide pytest hooks: record what the running session actually collected."""

from __future__ import annotations

from pathlib import Path

import pytest

_COLLECTED_MODULES: pytest.StashKey[frozenset[str]] = pytest.StashKey()
_DEFAULT_COLLECTION: pytest.StashKey[bool] = pytest.StashKey()


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Record collected module paths and whether this was a bare (testpaths) invocation.

    Under pytest-xdist every worker collects the full set before receiving its
    share, so the record is complete in workers as well as in a serial run.
    """
    root = config.rootpath.resolve()
    collected: set[str] = set()
    for item in items:
        path = Path(item.path).resolve()
        try:
            collected.add(path.relative_to(root).as_posix())
        except ValueError:
            collected.add(path.as_posix())
    config.stash[_COLLECTED_MODULES] = frozenset(collected)
    config.stash[_DEFAULT_COLLECTION] = not config.option.file_or_dir


@pytest.fixture(scope="session")
def collected_module_paths(request: pytest.FixtureRequest) -> frozenset[str]:
    """Repo-relative paths of every module the running session collected."""
    return request.config.stash[_COLLECTED_MODULES]


@pytest.fixture(scope="session")
def default_collection_session(request: pytest.FixtureRequest) -> bool:
    """True when no positional paths were given, i.e. `testpaths` drove collection."""
    return request.config.stash[_DEFAULT_COLLECTION]
