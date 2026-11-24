from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

MAX_WALK_ITEMS = 2_000_000
MAX_AMOUNT = Decimal('1e12')
KINDS = frozenset({'harbor-trials', 'campaign-ledger', 'settle-ledger', 'call-log'})
SOURCES = ('provider-reported', 'token-list-price', 'shared-key-delta')
ROUTES = ('openrouter', 'direct', 'unknown')
SKIP_DIRS = frozenset({
    '.git', '.venv', '__pycache__', 'artifact', 'artifacts', 'benchmarks', 'blobs', 'dataset',
    'datasets', 'environment', 'fixtures', 'golden', 'lab-extras', 'node_modules', 'raw-task',
    'repo', 'repos', 'scrub', 'submissions', 'tasks', 'tb2-leaderboard', 'testdata', 'tests',
    'upstream', 'verifier', 'vendor',
})
TOKEN = re.compile(
    r'(?i)(?:sk-(?:[a-z0-9]+-)*[a-z0-9_-]{12,}|(?:gh[pousr]_|github_pat_|xox[baprs]-|eyJ|AIza|AKIA)'
    r'[a-z0-9_-]{12,}|[a-z0-9_-]{60,})'
)
LABEL = re.compile(r'[A-Za-z0-9_./+@ :\-]{1,160}')


@dataclass(frozen=True)
class Root:
    label: str
    path: Path
    kind: str
    copies_of: str | None = None
    exclude: tuple[str, ...] = ()
    include: str | None = None
    delta_ledger: bool = False
    report_only: bool = False
    time_field: str | None = None
    model_field: str | None = None
    provider_field: str | None = None
    usd_field: str | None = None
    id_field: str | None = None
    dedup_fields: tuple[str, ...] = ()
    skip_field: str | None = None
    skip_value: str | None = None
    provider_override: str | None = None
    model_prefix: str = ''
    cost_source: str | None = None

    @property
    def owner(self) -> str:
        return self.copies_of or self.label


@dataclass(frozen=True)
class Entry:
    time: datetime
    route: str
    model: str
    usd: Decimal | None
    source: str
    channel: str


@dataclass(frozen=True)
class Trial:
    identity: tuple[str, datetime]
    time: datetime
    entries: tuple[Entry, ...]


@dataclass(frozen=True)
class Line:
    entry: Entry
    identity: tuple[str, ...] | None = None
    units: int = 1


def safe(value: object) -> str:
    if not isinstance(value, str) or not LABEL.fullmatch(value) or TOKEN.search(value):
        return '?'
    return value


def stamp(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        time = datetime.fromisoformat(value)
        return time.replace(tzinfo=UTC) if time.tzinfo is None else time.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def amount(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, Decimal | int | float):
        return None
    cost = value if isinstance(value, Decimal) else Decimal(str(value))
    if not cost.is_finite() or not 0 <= cost < MAX_AMOUNT:
        return None
    exponent = cost.as_tuple().exponent
    assert isinstance(exponent, int)
    if exponent < -30:
        return None
    return cost if cost else Decimal(0)


def mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def model_route(model: object, provider: object) -> tuple[str, str]:
    name = safe(model)
    namespace = safe(provider).lower()
    if namespace == 'openrouter' or name.startswith('openrouter/'):
        return 'openrouter', name.removeprefix('openrouter/') or '?'
    return ('unknown' if namespace in ('?', 'unknown') else 'direct'), name


def problem(root: Root, path: Path, reason: str, line: int | None = None) -> str:
    shown = TOKEN.sub('[redacted]', str(path))
    shown = ''.join(char if char.isprintable() else '?' for char in shown)[:500]
    location = f'{shown}:{line}' if line is not None else shown
    return f'{safe(root.label)}: {location}: {reason}'


def _excluded(root: Root, path: Path) -> bool:
    rel = path.relative_to(root.path).as_posix()
    return any(fnmatchcase(rel, pattern) or fnmatchcase(path.name, pattern) for pattern in root.exclude)


def _matches(path: Path, root: Root, pattern: str) -> bool:
    if root.include is None:
        return fnmatchcase(path.name, pattern)
    relative = path.relative_to(root.path).as_posix()
    base_pattern = root.include.removeprefix('**/')
    return fnmatchcase(relative, root.include) or fnmatchcase(relative, base_pattern)


def files(root: Root, problems: list[str], pattern: str, *, tree_only: bool = False) -> Iterator[Path]:
    try:
        mode = root.path.stat().st_mode
    except OSError:
        problems.append(problem(root, root.path, 'cannot read root'))
        return
    if stat.S_ISREG(mode) and not tree_only:
        if not _excluded(root, root.path):
            yield root.path
        return
    if not stat.S_ISDIR(mode):
        problems.append(problem(root, root.path, 'root is not a supported file or directory'))
        return
    if root.kind == 'harbor-trials' and SKIP_DIRS.intersection(root.path.parts):
        problems.append(problem(root, root.path, 'Harbor root is a fixture or imported submission'))
        return

    def unreadable(error: OSError) -> None:
        path = Path(error.filename) if error.filename else root.path
        problems.append(problem(root, path, 'cannot read directory'))

    visited = 0
    for directory, directories, names in os.walk(root.path, onerror=unreadable):
        base = Path(directory)
        visited += 1 + len(directories) + len(names)
        if visited > MAX_WALK_ITEMS:
            problems.append(problem(root, root.path, 'tree exceeds the walk limit'))
            return
        directories[:] = sorted(
            item for item in directories if item not in SKIP_DIRS and not _excluded(root, base / item)
        )
        for name in sorted(names):
            path = base / name
            if _matches(path, root, pattern) and not _excluded(root, path) and not path.is_symlink():
                yield path
