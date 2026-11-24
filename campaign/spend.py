from __future__ import annotations

import re
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from campaign import keyenv, spend_harbor, spend_keys, spend_ledgers
from campaign.spend_types import KINDS, ROUTES, SOURCES, Entry, Root, Trial, safe
from campaign.spend_view import text as text

MAX_ROOT_BYTES = 1_000_000
MAX_ROOTS = 256
MAX_RECORDS = 2_000_000
ET = ZoneInfo('America/New_York')
ZERO = Decimal(0)

_OPTIONAL_TEXT = (
    'copies_of', 'include', 'time_field', 'model_field', 'provider_field', 'usd_field', 'id_field',
    'skip_field', 'skip_value', 'provider_override', 'model_prefix', 'cost_source',
    'key_env',
)
_LISTS = ('exclude', 'dedup_fields')
_FLAGS = ('delta_ledger', 'report_only')
_FIELD_NAMES = ('time_field', 'model_field', 'provider_field', 'usd_field', 'id_field', 'skip_field')
_FIELD_PATH = re.compile(r'[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){0,7}')
_ROOT_KEYS = {'label', 'path', 'kind', *_OPTIONAL_TEXT, *_LISTS, *_FLAGS}


def _setting(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 512 and value.isprintable()


def _root(item: object, directory: Path) -> Root:
    if not isinstance(item, dict) or item.keys() - _ROOT_KEYS:
        raise ValueError('root is not a table with recognized settings')
    label, path, kind = (item.get(key) for key in ('label', 'path', 'kind'))
    if not isinstance(kind, str) or kind not in KINDS:
        raise ValueError('unknown root kind')
    if safe(label) == '?' or not _setting(path):
        raise ValueError('root needs a safe label and a path')
    assert isinstance(path, str) and isinstance(label, str)
    options: dict[str, Any] = {}
    for key in _OPTIONAL_TEXT:
        if key in item:
            if not _setting(item[key]):
                raise ValueError('invalid text setting')
            options[key] = item[key]
    for key in _LISTS:
        if key in item:
            values = item[key]
            if not isinstance(values, list) or len(values) > 128 or not all(map(_setting, values)):
                raise ValueError('invalid list setting')
            options[key] = tuple(values)
    for key in _FLAGS:
        if key in item:
            if not isinstance(item[key], bool):
                raise ValueError('invalid boolean setting')
            options[key] = item[key]
    if 'key_env' in options and not keyenv.valid_name(options['key_env']):
        raise ValueError('key_env must be a name such as OPENROUTER_KEY_MUSTER')
    root = Root(label, directory / Path(path).expanduser(), kind, **options)
    if root.copies_of is not None and (root.kind != 'harbor-trials' or safe(root.copies_of) == '?'):
        raise ValueError('copies_of needs a Harbor root and a safe owner label')
    if root.kind == 'harbor-trials' and (root.delta_ledger or root.report_only):
        raise ValueError('Harbor roots cannot be marked as ledger controls')
    field_paths = (*root.dedup_fields, *(getattr(root, name) for name in _FIELD_NAMES))
    if any(name is not None and not _FIELD_PATH.fullmatch(name) for name in field_paths):
        raise ValueError('invalid field path')
    if root.cost_source not in (None, *SOURCES[:2]):
        raise ValueError('unknown configured cost source')
    if root.id_field and root.dedup_fields:
        raise ValueError('use id_field or dedup_fields, not both')
    if (root.skip_field is None) != (root.skip_value is None):
        raise ValueError('skip_field and skip_value must be set together')
    if root.kind == 'call-log' and not all((root.time_field, root.model_field, root.usd_field)):
        raise ValueError('call-log needs time_field, model_field and usd_field')
    if root.kind == 'call-log' and not (root.provider_field or root.provider_override):
        raise ValueError('call-log needs provider_field or provider_override')
    return root


def _roots(path: Path, problems: list[str]) -> list[Root]:
    try:
        if not path.is_file():
            raise OSError
        with path.open('rb') as stream:
            raw = stream.read(MAX_ROOT_BYTES + 1)
        if len(raw) > MAX_ROOT_BYTES:
            problems.append('roots file exceeds the size limit')
            return []
        config = tomllib.loads(raw.decode('utf-8'))
    except (OSError, ValueError, RecursionError):
        problems.append('cannot read roots file or parse its TOML')
        return []
    items = config.get('roots')
    if set(config) != {'roots'} or not isinstance(items, list) or not 0 < len(items) <= MAX_ROOTS:
        problems.append('roots file needs a non-empty bounded list of [[roots]] tables')
        return []
    candidates = []
    for number, item in enumerate(items, 1):
        try:
            candidates.append((number, _root(item, path.absolute().parent)))
        except ValueError as error:
            problems.append(f'root {number}: {error}')
        except RuntimeError:
            problems.append(f'root {number}: cannot expand home directory')
    owners = {root.label for _, root in candidates if root.kind == 'harbor-trials' and root.copies_of is None}
    roots = []
    for number, root in candidates:
        if root.copies_of is not None and root.copies_of not in owners:
            problems.append(f'root {number}: copies_of has no canonical Harbor root')
        else:
            roots.append(root)
    return roots


@dataclass
class Spend:
    values: dict[tuple[str, str], Decimal] = field(default_factory=dict)

    def add(self, entry: Entry) -> None:
        if entry.usd is not None:
            key = entry.route, entry.source
            assert entry.route in ROUTES and entry.source in SOURCES and entry.usd >= ZERO
            self.values[key] = self.values.get(key, ZERO) + entry.usd

    def routes(self) -> dict[str, dict[str, str]]:
        return {
            route: {source: _money(self.values[route, source]) for source in SOURCES if (route, source) in self.values}
            for route in ROUTES
        }

    def total(self) -> Decimal:
        return sum(self.values.values(), ZERO)


def _money(usd: Decimal) -> str:
    return format(usd, 'f')


@dataclass
class Lane:
    spend: Spend = field(default_factory=Spend)
    trials: int = 0
    calls: int = 0
    settlements: int = 0
    settled_trials: int = 0
    unknown_trials: int = 0
    first: datetime | None = None
    last: datetime | None = None

    def add(self, entry: Entry) -> None:
        self.spend.add(entry)
        self.first = entry.time if self.first is None else min(self.first, entry.time)
        self.last = entry.time if self.last is None else max(self.last, entry.time)

    def row(self, label: str) -> dict[str, Any]:
        return {
            'lane': label, 'routes': self.spend.routes(), 'trials': self.trials, 'calls': self.calls,
            'settlements': self.settlements, 'settled_trials': self.settled_trials,
            'unknown_trials': self.unknown_trials,
            'unknown_share': self.unknown_trials / self.trials if self.trials else None,
            'first': self.first.isoformat() if self.first else None,
            'last': self.last.isoformat() if self.last else None,
        }


class Summary:
    def __init__(self, lower: datetime | None, upper: datetime | None, as_of: datetime,
                 key_recorded: dict[str, Decimal]) -> None:
        assert as_of.tzinfo is UTC
        self.lower, self.upper, self.as_of = lower, upper, as_of
        self.key_recorded = key_recorded
        day = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
        self.windows = {'day': day, 'week': day - timedelta(days=day.weekday()), 'month': day.replace(day=1), 'total': None}
        self.windows_usd = {name: Spend() for name in self.windows}
        self.lanes: dict[str, Lane] = defaultdict(Lane)
        self.days: dict[str, dict[tuple[str, str, str], Spend]] = {'UTC': defaultdict(Spend), 'ET': defaultdict(Spend)}
        self.total = Spend()
        self.models: dict[str, Spend] = defaultdict(Spend)
        self.duplicates = Lane()
        self.excluded: list[dict[str, Any]] = []

    def contains(self, stamp: datetime) -> bool:
        return (self.lower is None or self.lower <= stamp) and (self.upper is None or stamp < self.upper)

    def add(self, lane: str, entry: Entry) -> None:
        assert self.contains(entry.time) and entry.source != 'shared-key-delta'
        self.lanes[lane].add(entry)
        if entry.usd is None:
            return
        self.total.add(entry)
        self.models[entry.model].add(entry)
        for zone, timezone in (('UTC', UTC), ('ET', ET)):
            day = entry.time.astimezone(timezone).date().isoformat()
            self.days[zone][day, lane, entry.model].add(entry)
        if entry.route == 'openrouter' and entry.time <= self.as_of:
            if lane in self.key_recorded:
                self.key_recorded[lane] += entry.usd
            for name, lower in self.windows.items():
                if lower is None or entry.time >= lower:
                    self.windows_usd[name].add(entry)

    def add_trial(self, label: str, trial: Trial, *, duplicate: bool) -> None:
        entries = tuple(entry for entry in trial.entries if self.contains(entry.time))
        if not entries:
            return
        lane = self.duplicates if duplicate else self.lanes[label]
        lane.trials += 1
        if not any(entry.usd is not None for entry in entries):
            lane.unknown_trials += 1
        for entry in entries:
            if duplicate:
                lane.add(entry)
            else:
                self.add(label, entry)

    def reconcile(self, usage: tuple[Decimal, Decimal, Decimal, Decimal] | None) -> list[dict[str, Any]]:
        if usage is None:
            return []
        counters = dict(zip(('total', 'day', 'week', 'month'), usage, strict=True))
        rows = []
        for name, lower in self.windows.items():
            spend = self.windows_usd[name]
            recorded = spend.total()
            rows.append({
                'window': name, 'start': lower.isoformat() if lower else None, 'as_of': self.as_of.isoformat(),
                'counter_usd': _money(counters[name]), 'counter_source': 'provider-reported',
                'recorded_usd': _money(recorded), 'recorded_sources': spend.routes()['openrouter'],
                'remainder_usd': _money(counters[name] - recorded),
            })
        return rows

    def result(self, problems: list[str], usage: tuple[Decimal, Decimal, Decimal, Decimal] | None) -> dict[str, Any]:
        lanes = [lane.row(label) for label, lane in sorted(self.lanes.items())]
        trials = sum(lane['trials'] for lane in lanes)
        unknown = sum(lane['unknown_trials'] for lane in lanes)
        return {
            'lanes': lanes, 'trials': trials, 'unknown_trials': unknown, 'unknown_share': unknown / trials if trials else None,
            'total': {'routes': self.total.routes()},
            'days': {
                zone: [{'date': day, 'lane': lane, 'model': model, 'routes': spend.routes()}
                       for (day, lane, model), spend in sorted(days.items())]
                for zone, days in self.days.items()
            },
            'top_models': [
                {'model': model, 'recorded_usd': _money(spend.total()), 'routes': spend.routes()}
                for model, spend in sorted(self.models.items(), key=lambda pair: (-pair[1].total(), pair[0]))[:25]
            ],
            'duplicates': self.duplicates.row('duplicates'), 'excluded': self.excluded,
            'reconciliation': self.reconcile(usage), 'problems': problems,
            'since': self.lower.date().isoformat() if self.lower else None,
            'until': self.upper.date().isoformat() if self.upper else None,
        }


def _priority(root: Root, trial: Trial) -> tuple[int, int, bool]:
    known = [entry for entry in trial.entries if entry.usd is not None]
    return sum(entry.channel == 'result' for entry in known), len(known), root.copies_of is None


def _scan_trials(roots: list[Root], problems: list[str], summary: Summary) -> None:
    selected: dict[tuple[str, datetime], tuple[Root, Trial]] = {}
    count = 0
    for root in roots:
        if root.kind != 'harbor-trials':
            continue
        summary.lanes.setdefault(root.owner, Lane())
        for trial in spend_harbor.trials(root, problems):
            count += 1
            if count > MAX_RECORDS:
                problems.append('report exceeds the trial limit')
                break
            previous = selected.get(trial.identity)
            if previous is None:
                selected[trial.identity] = root, trial
            elif _priority(root, trial) > _priority(*previous):
                summary.add_trial(previous[0].owner, previous[1], duplicate=True)
                selected[trial.identity] = root, trial
            else:
                summary.add_trial(root.owner, trial, duplicate=True)
        if count > MAX_RECORDS:
            break
    for root, trial in selected.values():
        summary.add_trial(root.owner, trial, duplicate=False)


def _scan_lines(roots: list[Root], problems: list[str], summary: Summary) -> None:
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    count = 0
    for root in roots:
        if root.kind == 'harbor-trials':
            continue
        controls: dict[str, Lane] = {}
        if root.delta_ledger:
            controls['shared-key-delta'] = Lane()
        elif root.report_only:
            controls['overlapping'] = Lane()
        if not (root.delta_ledger or root.report_only):
            summary.lanes.setdefault(root.owner, Lane())
        for line in spend_ledgers.lines(root, problems):
            count += 1
            if count > MAX_RECORDS:
                problems.append('report exceeds the ledger and call limit')
                return
            entry = line.entry
            if not summary.contains(entry.time):
                continue
            if root.delta_ledger and entry.source != 'shared-key-delta':
                entry = replace(entry, source='shared-key-delta')
            if entry.source == 'shared-key-delta':
                _add_control(controls, 'shared-key-delta', root, entry, line.units)
                continue
            if root.report_only:
                _add_control(controls, 'overlapping', root, entry, line.units)
                continue
            if line.identity is not None:
                key = root.kind, root.owner, line.identity
                if key in seen:
                    summary.duplicates.add(entry)
                    summary.duplicates.calls += root.kind == 'call-log'
                    summary.duplicates.settlements += root.kind != 'call-log'
                    continue
                seen.add(key)
            summary.add(root.owner, entry)
            lane = summary.lanes[root.owner]
            if root.kind == 'call-log':
                lane.calls += line.units
            else:
                lane.settlements += 1
                lane.settled_trials += line.units
        for reason, control in sorted(controls.items()):
            if reason == 'overlapping' and control.first is None and len(controls) > 1:
                continue
            summary.excluded.append({**control.row(root.owner), 'kind': root.kind, 'reason': reason})


def _add_control(controls: dict[str, Lane], reason: str, root: Root, entry: Entry, units: int) -> None:
    control = controls.get(reason)
    if control is None:
        control = controls[reason] = Lane()
    control.add(entry)
    if root.kind == 'call-log':
        control.calls += units
    else:
        control.settlements += 1
        control.settled_trials += units


def report(path: Path, *, since: date | None = None, until: date | None = None, as_of: datetime,
           usage: tuple[Decimal, Decimal, Decimal, Decimal] | None = None,
           key_system: spend_keys.KeyProbeSystem | None = None) -> dict[str, Any]:
    assert since is None or until is None or since < until
    assert as_of.tzinfo is not None
    lower = datetime.combine(since, time.min, UTC) if since is not None else None
    upper = datetime.combine(until, time.min, UTC) if until is not None else None
    problems: list[str] = []
    roots = _roots(path, problems)
    lanes = spend_keys.lane_keys(roots, problems) if key_system is not None else {}
    with localcontext() as context:
        context.prec = 60
        summary = Summary(lower, upper, as_of.astimezone(UTC), dict.fromkeys(lanes, ZERO))
        _scan_trials(roots, problems, summary)
        _scan_lines(roots, problems, summary)
        result = summary.result(problems, usage)
        if key_system is not None:
            result['key_reconciliation'] = spend_keys.rows(key_system, lanes, summary.key_recorded, problems)
        return result
