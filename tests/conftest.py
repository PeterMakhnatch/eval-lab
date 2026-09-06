"""Session-wide pytest hooks: record what the running session actually collected."""

from __future__ import annotations

import zlib
from pathlib import Path

import pytest

_COLLECTED_MODULES: pytest.StashKey[frozenset[str]] = pytest.StashKey()
_DEFAULT_COLLECTION: pytest.StashKey[bool] = pytest.StashKey()
_SHARD_ASSIGNMENT: pytest.StashKey[tuple[int, int] | None] = pytest.StashKey()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--shard",
        action="store",
        default=None,
        metavar="INDEX/TOTAL",
        help="Run tests in shard INDEX/TOTAL (e.g. 1/2, 2/2).",
    )


def _parse_shard_option(value: str) -> tuple[int, int]:
    index_text, sep, total_text = value.partition("/")
    if not sep or not index_text.isdigit() or not total_text.isdigit():
        raise pytest.UsageError(f"--shard expects INDEX/TOTAL such as 1/2, got {value!r}")
    index, total = int(index_text), int(total_text)
    if not 1 <= index <= total:
        raise pytest.UsageError(f"--shard {value!r}: INDEX must satisfy 1 <= INDEX <= TOTAL")
    return index, total


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

    shard_option: str | None = config.getoption("shard", default=None)
    if not shard_option:
        config.stash[_SHARD_ASSIGNMENT] = None
        return

    index, total = _parse_shard_option(shard_option)
    config.stash[_SHARD_ASSIGNMENT] = (index, total)

    kept: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        path = Path(item.path).resolve()
        try:
            rel_path = path.relative_to(root).as_posix()
        except ValueError:
            rel_path = path.as_posix()

        if rel_path == "tests/test_ci_coverage.py":
            kept.append(item)
        else:
            item_shard = (zlib.crc32(rel_path.encode("utf-8")) % total) + 1
            if item_shard == index:
                kept.append(item)
            else:
                deselected.append(item)

    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = kept


@pytest.fixture(scope="session")
def collected_module_paths(request: pytest.FixtureRequest) -> frozenset[str]:
    """Repo-relative paths of every module the running session collected."""
    return request.config.stash[_COLLECTED_MODULES]


@pytest.fixture(scope="session")
def default_collection_session(request: pytest.FixtureRequest) -> bool:
    """True when no positional paths were given, i.e. `testpaths` drove collection."""
    return request.config.stash[_DEFAULT_COLLECTION]


@pytest.fixture(scope="session")
def shard_assignment(request: pytest.FixtureRequest) -> tuple[int, int] | None:
    """The (index, total) shard assignment tuple if --shard was given, otherwise None."""
    return request.config.stash.get(_SHARD_ASSIGNMENT, None)
