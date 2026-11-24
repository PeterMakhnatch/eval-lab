from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from campaign.spend_types import ROUTES

ET = ZoneInfo('America/New_York')
MAX_EXAMPLES = 5


def _usd(value: str) -> str:
    return f'{Decimal(value):.6f}'


def _split(sources: dict[str, str]) -> str:
    return ', '.join(f'{_usd(value)} {source}' for source, value in sources.items()) or '-'


def _routes(row: dict[str, Any]) -> list[str]:
    return [_split(row['routes'][route]) for route in ROUTES]


def _table(header: tuple[str, ...], rows: list[list[str]]) -> list[str]:
    return [' | '.join(header), *(' | '.join(row) for row in rows)]


def _et(stamp: str | None) -> str:
    if stamp is None:
        return '-'
    return datetime.fromisoformat(stamp).astimezone(ET).isoformat()


def _share(count: int, share: float | None) -> str:
    return '-' if share is None else f'{count} ({share:.2%})'


def _lanes(result: dict[str, Any]) -> list[str]:
    rows = [
        [row['lane'], *_routes(row), str(row['trials']), str(row['calls']), str(row['settlements']),
         str(row['settled_trials']), str(row['no_model_trials']) if row['trials'] else '-',
         _share(row['model_use_without_cost_trials'], row['model_use_without_cost_share']),
         _et(row['first']), _et(row['last'])]
        for row in result['lanes']
    ]
    return _table(('Lane', 'OpenRouter USD', 'Direct USD', 'Unknown route USD', 'Trials', 'Calls',
                   'Settlements', 'Settled trial count', 'No model (scripted or never ran)',
                   'Model use without a cost record', 'First ET', 'Last ET'), rows)


def _tokens(tokens: dict[str, int]) -> str:
    return ', '.join(f'{name} {count}' for name, count in sorted(tokens.items())) or '-'


def _missing(result: dict[str, Any]) -> list[str]:
    lanes = result['model_use_without_cost']
    if not lanes:
        return ['', 'Model use without a cost record: none.']
    lines = [
        '', 'Model use without a cost record (no price applied; token counts are as recorded):',
        *_table(('Lane', 'Route', 'Model', 'Seats', 'Seats with token counts', 'Token counts'),
                [[lane['lane'], row['route'], row['model'] or '-', str(row['seats']), str(row['seats_with_tokens']),
                  _tokens(row['tokens'])] for lane in lanes for row in lane['models']]),
    ]
    for lane in lanes:
        lines.append(f"{lane['lane']}: {lane['trials']} trials; first {MAX_EXAMPLES} paths in sort order:")
        lines.extend(f"  {row['path']}" for row in lane['records'][:MAX_EXAMPLES])
    return lines


def _details(result: dict[str, Any]) -> list[str]:
    duplicate = result['duplicates']
    lines = [
        '', f"Trials with model use and no cost record: "
        f"{_share(result['model_use_without_cost_trials'], result['model_use_without_cost_share'])}. "
        f"Trials with no model (scripted or never ran): {result['no_model_trials']}.",
        f"Duplicates removed: {duplicate['trials']} trials; {duplicate['calls']} calls; "
        f"{duplicate['settlements']} settlements. Discarded records by route and source:",
        *_table(('OpenRouter USD', 'Direct USD', 'Unknown route USD'), [_routes(duplicate)]),
        '', 'Ledger controls (not added to spend):',
    ]
    return lines + _table(
        ('Lane', 'Kind', 'Reason', 'Settlements', 'Settled trial count', 'OpenRouter USD', 'Direct USD', 'Unknown route USD'),
        [[row['lane'], row['kind'], row['reason'], str(row['settlements']), str(row['settled_trials']), *_routes(row)]
         for row in result['excluded']],
    )


def _reconciliation(result: dict[str, Any]) -> list[str]:
    rows = result['reconciliation']
    if not rows:
        return []
    return [
        '', 'OpenRouter counter reconciliation (USD):',
        f"Counter as of {rows[0]['as_of']} UTC, inclusive. This timestamp applies only to reconciliation.",
        'Assumptions: UTC calendar day, Monday-start UTC week, UTC calendar month, and cumulative key usage.',
        'Recorded amounts use the supplied roots and date filter. Only the OpenRouter route counts.',
        'The route does not prove which API key paid. Remainder = provider counter minus recorded USD.',
        f"{result['model_use_without_cost_trials']} trials in the filter show model use without a cost record; "
        'the remainder can include their cost.',
        *_table(
            ('Window', 'Start UTC', 'Provider-reported counter', 'Recorded USD', 'Recorded by source', 'Remainder USD'),
            [[row['window'], row['start'] or '-', _usd(row['counter_usd']), _usd(row['recorded_usd']),
              _split(row['recorded_sources']), _usd(row['remainder_usd'])] for row in rows],
        ),
    ]


def _key_reconciliation(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ['', 'OpenRouter lane keys: no lane has a key_env to probe.']
    fields = ('usage', 'usage_daily', 'usage_weekly', 'usage_monthly', 'limit', 'limit_remaining',
              'recorded_usd', 'remainder_usd', 'combined_remainder_usd')
    return [
        '', 'Live OpenRouter lane keys (USD):',
        'Recorded USD counts only OpenRouter records in the supplied roots and date filter, up to report start.',
        'Remainder = key usage minus recorded USD. The records do not prove which key paid.',
        'A shared key has one combined row for its provider counters and remainder.',
        *_table(
            ('Lane', 'Key name', 'Scope', 'Total', 'Day', 'Week', 'Month', 'Limit', 'Remaining', 'Recorded',
             'Remainder', 'Combined remainder'),
            [[row['lane'] or 'All lanes for key', row['key_env'], 'shared' if row['shared'] else 'single',
              *(_usd(value) if value is not None else '-' for value in (row[field] for field in fields))]
             for row in rows],
        ),
    ]


def text(result: dict[str, Any]) -> str:
    lines = [
        'Campaign spend report (USD)',
        f"Record filter: since {result['since'] or 'unbounded'} (inclusive), "
        f"until {result['until'] or 'unbounded'} (exclusive), at UTC midnight.",
        'Harbor dates use seat finish time when available, then trial finish or start. Times without an offset use UTC.',
        'A dash means no record, not zero. Trial counts use only trials with no cost record in the filter: '
        '"No model" trials have scripted seats or model seats that never ran and cost $0; '
        '"Model use without a cost record" trials show model use (the cap rule) and their cost is unknown.',
        'USD is split by source: provider-reported, token-list-price, and shared-key-delta.',
        'Only the top-model ranking and reconciliation combine provider-reported and token-list-price costs.',
        '', *_lanes(result),
        '', 'Recorded totals by source:',
        *_table(('OpenRouter USD', 'Direct USD', 'Unknown route USD'), [_routes(result['total'])]),
    ]
    for zone, rows in result['days'].items():
        lines.extend([
            '', f'By {zone} day:',
            *_table(('Date', 'Lane', 'Model', 'OpenRouter USD', 'Direct USD', 'Unknown route USD'),
                    [[row['date'], row['lane'], row['model'], *_routes(row)] for row in rows]),
        ])
    lines.extend([
        '', 'Top 25 models by combined recorded USD:',
        *_table(('Model', 'Combined USD', 'OpenRouter USD', 'Direct USD', 'Unknown route USD'),
                [[row['model'], _usd(row['recorded_usd']), *_routes(row)] for row in result['top_models']]),
        *_missing(result), *_details(result), *_reconciliation(result),
        *(_key_reconciliation(result['key_reconciliation']) if 'key_reconciliation' in result else []),
        '', f"Input problems: {len(result['problems'])}",
        *(f'problem: {problem}' for problem in result['problems']),
    ])
    return '\n'.join(lines) + '\n'
