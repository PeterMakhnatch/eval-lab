"""UTC-day lab spend ledger (HAR-122) and billed-session allocation (HAR-131).

One ledger per UTC day sums ALL lab spend broken down by source (Modal
billed rows, Daytona sandbox estimates, model API ledgers), by card
(HAR-NNN) and by job, with the grand total against the policy's daily cap.

``spend day`` resolves policy at 00:00 UTC on the reported day: its dated
override applies to that whole day, including retrospective reports after
expiry. Validated overrides always expire after that day's start. This is
reporting, not renewed launch permission: ``spend check`` resolves at the
window end (the launch time), respecting expiry even with an older --since.

Source rules (each row carries ``basis`` so billed figures are never
mixed with estimates):

:- ``modal`` (basis ``billed``): the HAR-107 billing-reconcile rows in
  catalog table ``modal_billing_rows``. Modal bills the account, not
  jobs, so costs split by billed app (``description``), one row per app;
  an app lands on a card only through an explicit app -> card binding,
  and every app without one stays on its own ``unattributed`` row as the
  unknown residual. The per-(object, day) ``max(daily, hourly)`` rule is
  unchanged, so the split conserves the billed total exactly.
  When the table holds no rows for the day the ledger says so; this
  module never fetches billing itself (the reconcile path owns that).
  Per-job GPU shares come only from an explicit HAR-131 session receipt
  (:func:`session_spend_for_job`), never from dividing these rows.
- ``daytona`` (basis ``estimate``): the Daytona SDK (0.220.0) exposes
  only quota/current-usage snapshots
  (``OrganizationUsageOverview``: CPU/memory/disk quotas, no dollars,
  no history), so there is no billed per-day source. Each catalog
  trial on a Daytona backend is priced with the same formula
  ``evallab tasks qualify-collect`` uses
  (:func:`evallab.task_qualification.estimate_cost_usd` over the
  Daytona list-price card), applied to the slice of trial wall time
  that falls inside the UTC day (intervals crossing midnight are
  split). Trial resources come from the job/trial environment
  overrides, then the staged ``task.toml`` (read-only); when the
  staging is gone the ``mimo-v2.6-rl`` family fallback (2 vCPU /
  8192 MiB, the uniform shape across every surviving staged task.toml
  of that family) applies and the row evidence says so.
- ``model`` (basis ``ledger``): settled proxy-ledger dollars —
  per-job ``lab-metadata.json`` ``provider_usage`` totals
  (``cost_micros``) via :func:`evallab.ledger.build_cost_block`, plus
  experiment ``spend.jsonl`` ledgers (``cost_usd`` per row with a
  ``ts`` timestamp). Self-hosted MiMo proxy calls are $0 at the proxy
  (pricing 0/0, billed instead through Modal server time) and
  contribute no model row, so they are never double counted.

Card attribution is explicit-only and fail-closed: the catalog
``lab_metadata`` ``experiment.linear_card`` (copied from the submitting
``ExperimentSpec.linear_card``) wins, the job-name prefix (``har81-...``
-> ``HAR-81``) or the ledger path (``har111/...`` -> ``HAR-111``) covers
the rest; disagreement or a malformed explicit card fails closed to
``unattributed`` with a conflict note and is never guessed. Task,
model, harness, and app names are never attribution: jobs that predate
the explicit field (e.g. the 2026-10-01 ``ovn-g5-*`` runs) stay
``unattributed`` until republished with an explicit binding such as
``publication_card``. Billed Daytona dollars are unavailable from the
provider API, so Daytona rows stay estimates.
Model-ledger dollars attribute to the day the job finished;
``spend.jsonl`` rows attribute to the day of their own timestamp.

Window ledgers generalize the day to any half-open UTC window
[start, end): a day is the special case [day 00:00, next day 00:00).
Overlap rules per source (documented where implemented):
Modal billing rows are hourly, so a row counts whole when its
``interval_start`` hour falls in the window; Daytona trials contribute
the seconds of their wall interval overlapping the window;
``spend.jsonl`` rows count when their ``ts`` is in the window while
per-job proxy ledgers count whole on the day their job finished.
The pre-launch check (``check_launch``) additionally reads the queue's
``running``/``approved`` specs read-only to reserve in-flight spend;
this module still never spends, never writes, and never launches.

``session_spend_for_job`` reads an explicit post-session receipt, binds its
complete declared membership to teardown, and shares billed Modal dollars by
recorded trial wall seconds. It adds the supplied existing Daytona estimate;
an unknown estimate leaves the combined total unknown. This allocation is an
accounting policy, not measured per-job GPU usage or a new pricing model.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from evallab.schemas import StandingApprovalsPolicy, effective_daily_cost_ceiling, normalize_linear_card

#: Lab day-spend cap (USD) the ledger reports against.
DEFAULT_CAP_USD = 20.0

#: Card bucket for spend that cannot be tied to a card. Never guessed.
UNATTRIBUTED = "unattributed"

#: Row ``basis`` values: billed money vs estimates vs settled ledgers.
BASIS_BILLED = "billed"
BASIS_ESTIMATE = "estimate"
BASIS_LEDGER = "ledger"

_JOB_CARD_RE = re.compile(r"(?i)^har-?0*(\d+)-")
_PATH_CARD_RES = (
    re.compile(r"(?i)har-0*(\d+)\b"),
    re.compile(r"(?i)\bhar0*(\d+)\b"),
)

#: Task-family fallback sandbox sizes (cpus, memory_mb) when the staged
#: task.toml is gone. ``mimo-v2.6-rl``: uniform 2 vCPU / 8192 MiB across
#: every surviving staged task.toml of that family (10/10 on 2026-09-29/30;
#: same shape as the HAR-110 budget line, 2 vCPU + 8 GiB + 10 GiB).
FALLBACK_DAYTONA_RESOURCES: dict[str, tuple[int, int]] = {
    "mimo-v2.6-rl": (2, 8192),
}


@dataclass(frozen=True)
class SpendRow:
    """One ledger line: who spent what, on which card/job, on what basis."""

    source: str  # "modal" | "daytona" | "model"
    card: str  # "HAR-81" | "unattributed"
    job: str
    usd: float
    basis: str  # "billed" | "estimate" | "ledger"
    evidence: str  # catalog table, ledger path, or billing id


@dataclass(frozen=True)
class DayLedger:
    """Full per-day ledger with totals against the cap."""

    day: date
    rows: tuple[SpendRow, ...]
    per_source: dict[str, float]
    per_card: dict[str, float]
    total_usd: float
    cap_usd: float
    over_cap: bool
    over_by_usd: float
    headroom_usd: float
    notes: tuple[str, ...] = field(default_factory=tuple)
    unresolved_model_usd: float = 0.0
    cap_description: str | None = None


def parse_day(value: str) -> date:
    """Parse ``YYYY-MM-DD``, raising ``ValueError`` on anything else."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"invalid day: {value!r} (expected YYYY-MM-DD)") from None


def card_for_job(job_name: str | None) -> str:
    """Card for a job name (``har81-...`` -> ``HAR-81``), else unattributed."""
    if isinstance(job_name, str):
        match = _JOB_CARD_RE.match(job_name)
        if match:
            return f"HAR-{int(match.group(1))}"
    return UNATTRIBUTED


def card_for_ledger_path(path: str | Path) -> str:
    """Card for a ledger path; deepest ``harNNN``/``HAR-NNN`` part wins."""
    parts = list(Path(path).parts)
    for part in reversed(parts):
        for pattern in _PATH_CARD_RES:
            match = pattern.search(part)
            if match:
                return f"HAR-{int(match.group(1))}"
    return UNATTRIBUTED

def resolve_job_card(job_name: str | None, *, linear_card: str | None = None) -> str:
    """Card for a job: explicit ``linear_card`` first, job-name prefix second.

    The explicit card (``ExperimentSpec.linear_card`` carried through run
    provenance into the catalog) wins when it agrees with the job-name
    prefix or when the name carries none. A malformed explicit card, or
    one that disagrees with the job-name prefix, raises ``ValueError``
    fail-closed: the ledger never guesses, and query paths that catch
    this per job leave the row ``unattributed`` with a conflict note.
    Task, model, and app names are never consulted here.
    """
    explicit = normalize_linear_card(linear_card)
    inferred = card_for_job(job_name)
    if explicit is not None:
        if inferred != UNATTRIBUTED and inferred != explicit:
            raise ValueError(
                f"explicit card {explicit} conflicts with job-name card "
                f"{inferred} for {job_name!r}"
            )
        return explicit
    return inferred


def explicit_card_from_lab_metadata(lab_metadata: Any) -> str | None:
    """Explicit card carried in catalog ``lab_metadata`` (``experiment``), if any.

    Returns the normalized card, ``None`` when the job predates the field
    or leaves it blank, and raises ``ValueError`` on a malformed value.
    """
    if isinstance(lab_metadata, Mapping):
        experiment = lab_metadata.get("experiment")
        if isinstance(experiment, Mapping):
            return normalize_linear_card(experiment.get("linear_card"))
    return None


def parse_dt(value: Any) -> datetime | None:
    """Parse a catalog timestamp or epoch to an aware UTC datetime."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip().removesuffix("Z")
        if text and text[0].isdigit() and re.fullmatch(r"[0-9.]+", text):
            return parse_dt(float(text))
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None


def _coerce_utc(value: datetime) -> datetime:
    """Treat a naive datetime as UTC; pass aware datetimes through."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _live_row_counts(row: Any, start: datetime, end: datetime) -> bool:
    """Whether a live Modal row counts toward [start, end); an unparseable
    interval counts (a guard may over-count, never under-count)."""
    raw = row.interval_start
    interval = _coerce_utc(raw) if isinstance(raw, datetime) else parse_dt(raw)
    return interval is None or start <= interval < end


def window_overlap_seconds(
    start: datetime, end: datetime, window_start: datetime, window_end: datetime
) -> float:
    """Seconds of ``[start, end)`` falling inside ``[window_start, window_end)``.

    Naive bounds are treated as UTC (matching ``parse_dt``). Slice
    portions across adjacent windows always add up to the whole interval.
    """
    overlap = (
        min(_coerce_utc(end), _coerce_utc(window_end))
        - max(_coerce_utc(start), _coerce_utc(window_start))
    ).total_seconds()
    return max(0.0, overlap)


def day_to_window(day: date) -> tuple[datetime, datetime]:
    """Half-open UTC window ``[day 00:00, day+1 00:00)`` for a calendar day."""
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return start, start + _one_day()


def policy_cap_at(policy: StandingApprovalsPolicy, moment: datetime) -> tuple[float, str]:
    """Effective cap and its provenance at an aware (or naive UTC) instant."""
    moment = _coerce_utc(moment).astimezone(UTC)
    cap = effective_daily_cost_ceiling(policy, moment)
    for override in policy.daily_cost_ceiling_overrides:
        if override.utc_date == moment.date() and moment < _coerce_utc(override.expires_at):
            return cap, (
                f"dated override {override.card}; standing ${policy.daily_cost_ceiling_usd:.2f}"
            )
    return cap, "standing policy"


def _render_cap(cap_usd: float, description: str | None) -> str:
    return f"${cap_usd:.2f}" + (f" ({description})" if description else "")


def _one_day() -> Any:
    from datetime import timedelta

    return timedelta(days=1)


def record_hash(record: Mapping[str, Any]) -> str:
    """Stable identity for one ledger record (dedupe across checkouts)."""
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def dedupe_records(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Drop byte-identical ledger records, keeping the first copy."""
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for record in records:
        digest = record_hash(record)
        if digest in seen:
            continue
        seen.add(digest)
        unique.append(dict(record))
    return unique


def model_usage_from_provider_usage(
    provider_usage: Any,
) -> tuple[float | None, float | None, str]:
    """Settled dollars and attempted ceiling for one per-job proxy ledger.

    Returns ``(settled_usd, attempted_usd, reason)``. Settled dollars cover
    reconciled calls only; attempted dollars include unresolved reservations
    as an upper-bound ceiling.
    """
    from evallab.ledger import build_cost_block

    block = build_cost_block(provider_usage if isinstance(provider_usage, Mapping) else None)
    cost = block.get("cost_usd")
    cost_usd = (
        float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
    )
    attempted = block.get("attempted_cost_usd")
    attempted_usd = (
        float(attempted)
        if isinstance(attempted, (int, float)) and not isinstance(attempted, bool)
        else None
    )
    reason = str(block.get("reason") or "")
    return cost_usd, attempted_usd, reason


def model_usd_from_provider_usage(provider_usage: Any) -> tuple[float | None, str]:
    """Settled model dollars for one per-job proxy ledger.

    Returns ``(usd, reason)``; ``usd`` is None (with a reason, never a
    silent 0) when the ledger is missing, unreadable, or the
    zero-priced self-hosted route ($0 at the proxy, billed via Modal).
    Only settled ``used`` cost counts; unresolved reservations never do.
    """
    cost, _, reason = model_usage_from_provider_usage(provider_usage)
    if cost is not None:
        return cost, ""
    return None, reason or "no settled model cost"


def aggregate_spend_jsonl_calls(
    calls: Iterable[Mapping[str, Any]], window_start: datetime, window_end: datetime
) -> tuple[float, int, int]:
    """Sum ``cost_usd`` for calls whose ``ts`` falls in ``[window_start, window_end)``.

    Returns ``(usd, in_window, out_of_window)``; rows without a parsable
    timestamp never contribute dollars.
    """
    total = 0.0
    in_window = 0
    out_of_window = 0
    window_start = _coerce_utc(window_start)
    window_end = _coerce_utc(window_end)
    for call in calls:
        stamp = parse_dt(call.get("ts"))
        if stamp is None or not (window_start <= stamp < window_end):
            out_of_window += 1
            continue
        cost = call.get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            total += float(cost)
            in_window += 1
        else:
            out_of_window += 1
    return total, in_window, out_of_window


def backend_is_daytona(*configs: Any) -> bool:
    """True when any config/lock environment names a Daytona backend."""
    for config in configs:
        if not isinstance(config, Mapping):
            continue
        env = config.get("environment")
        if not isinstance(env, Mapping):
            continue
        import_path = env.get("import_path")
        if isinstance(import_path, str) and "daytona" in import_path.lower():
            return True
    return False


def trial_daytona_resources(
    *,
    task_family: str | None,
    environment_configs: Iterable[Any],
    task_toml_env: Mapping[str, Any] | None,
) -> tuple[tuple[int | None, int | None, int | None], str]:
    """Resolve ``(cpus, memory_mb, storage_mb)`` for one Daytona trial.

    Precedence mirrors ``qualify-collect``: environment overrides, then
    the staged ``task.toml`` ``[environment]``. When the staging is gone
    the task-family fallback applies (flagged ``fallback``); unknown
    families without a task.toml cannot be rated (flagged ``missing``).
    Missing values are None (the lab never invents sizes); storage
    defaults to 0 billable GiB downstream per the rate-card rule.
    """
    cpus: int | None = None
    memory_mb: int | None = None
    storage_mb: int | None = None
    for config in environment_configs:
        if not isinstance(config, Mapping):
            continue
        env = config.get("environment")
        if not isinstance(env, Mapping):
            continue
        for key, attr in (
            ("override_cpus", "cpus"),
            ("override_memory_mb", "memory_mb"),
            ("override_storage_mb", "storage_mb"),
        ):
            value = env.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                if attr == "cpus":
                    cpus = value
                elif attr == "memory_mb":
                    memory_mb = value
                else:
                    storage_mb = value
    basis = "overrides" if cpus is not None or memory_mb is not None else ""
    if task_toml_env:
        if cpus is None:
            value = task_toml_env.get("cpus")
            cpus = value if isinstance(value, int) and not isinstance(value, bool) else None
        if memory_mb is None:
            value = task_toml_env.get("memory_mb")
            memory_mb = value if isinstance(value, int) and not isinstance(value, bool) else None
        if storage_mb is None:
            value = task_toml_env.get("storage_mb")
            storage_mb = value if isinstance(value, int) and not isinstance(value, bool) else None
        basis = (
            (basis + "+task.toml" if basis else "task.toml")
            if (cpus is not None or memory_mb is not None)
            else basis
        )
    if (cpus is None or memory_mb is None) and task_family in FALLBACK_DAYTONA_RESOURCES:
        fallback_cpus, fallback_mem = FALLBACK_DAYTONA_RESOURCES[task_family]
        if cpus is None:
            cpus = fallback_cpus
        if memory_mb is None:
            memory_mb = fallback_mem
        basis = (basis + "+family-fallback" if basis else "family-fallback") + (f":{task_family}")
    return (cpus, memory_mb, storage_mb), basis or "missing"


def cap_status(total_usd: float, cap_usd: float) -> tuple[bool, float, float]:
    """``(over_cap, over_by_usd, headroom_usd)`` for a total against a cap."""
    over = total_usd > cap_usd
    return over, max(0.0, total_usd - cap_usd), cap_usd - total_usd


@dataclass(frozen=True)
class WindowLedger:
    """Full ledger for a half-open UTC window ``[start, end)``.

    Same shape as :class:`DayLedger` but bounded by datetimes instead of
    a calendar day; a day ledger is the special case covering
    [day 00:00, next day 00:00) UTC.
    """

    start: datetime
    end: datetime
    rows: tuple[SpendRow, ...]
    per_source: dict[str, float]
    per_card: dict[str, float]
    total_usd: float
    cap_usd: float
    over_cap: bool
    over_by_usd: float
    headroom_usd: float
    notes: tuple[str, ...] = field(default_factory=tuple)
    unresolved_model_usd: float = 0.0


def _accumulate_totals(
    rows: tuple[SpendRow, ...],
) -> tuple[dict[str, float], dict[str, float], float]:
    """Shared per-source/per-card/total fold for day and window ledgers."""
    per_source: dict[str, float] = {}
    per_card: dict[str, float] = {}
    for row in rows:
        per_source[row.source] = per_source.get(row.source, 0.0) + row.usd
        per_card[row.card] = per_card.get(row.card, 0.0) + row.usd
    return per_source, per_card, sum(row.usd for row in rows)


def summarize_window(
    start: datetime,
    end: datetime,
    rows: Iterable[SpendRow],
    *,
    cap_usd: float = DEFAULT_CAP_USD,
    notes: Iterable[str] = (),
    unresolved_model_usd: float = 0.0,
) -> WindowLedger:
    """Build the window ledger: per-source/card totals plus cap arithmetic."""
    ordered = tuple(rows)
    per_source, per_card, total = _accumulate_totals(ordered)
    over, over_by, headroom = cap_status(total, cap_usd)
    return WindowLedger(
        start=_coerce_utc(start),
        end=_coerce_utc(end),
        rows=ordered,
        per_source=per_source,
        per_card=per_card,
        total_usd=total,
        cap_usd=cap_usd,
        over_cap=over,
        over_by_usd=over_by,
        headroom_usd=headroom,
        notes=tuple(notes),
        unresolved_model_usd=unresolved_model_usd,
    )


def summarize_day(
    day: date,
    rows: Iterable[SpendRow],
    *,
    cap_usd: float = DEFAULT_CAP_USD,
    notes: Iterable[str] = (),
    unresolved_model_usd: float = 0.0,
    cap_description: str | None = None,
) -> DayLedger:
    """Build the day ledger: per-source/card totals plus cap arithmetic."""
    ordered = tuple(rows)
    per_source, per_card, total = _accumulate_totals(ordered)
    over, over_by, headroom = cap_status(total, cap_usd)
    return DayLedger(
        day=day,
        rows=ordered,
        per_source=per_source,
        per_card=per_card,
        total_usd=total,
        cap_usd=cap_usd,
        over_cap=over,
        over_by_usd=over_by,
        headroom_usd=headroom,
        notes=tuple(notes),
        unresolved_model_usd=unresolved_model_usd,
        cap_description=cap_description,
    )


def ledger_to_dict(ledger: DayLedger) -> dict[str, Any]:
    """JSON-serializable view of a day ledger (``--json`` output)."""
    return {
        "day": ledger.day.isoformat(),
        "cap_usd": ledger.cap_usd,
        "cap_description": ledger.cap_description,
        "total_usd": ledger.total_usd,
        "over_cap": ledger.over_cap,
        "over_by_usd": ledger.over_by_usd,
        "headroom_usd": ledger.headroom_usd,
        "per_source_usd": dict(sorted(ledger.per_source.items())),
        "per_card_usd": dict(sorted(ledger.per_card.items())),
        "rows": [
            {
                "source": row.source,
                "card": row.card,
                "job": row.job,
                "usd": row.usd,
                "basis": row.basis,
                "evidence": row.evidence,
            }
            for row in ledger.rows
        ],
        "notes": list(ledger.notes),
    }


def render_ledger(ledger: DayLedger) -> str:
    """Human-readable per-day table with totals against the cap."""
    cap = _render_cap(ledger.cap_usd, ledger.cap_description)
    lines = [
        f"spend day {ledger.day.isoformat()} (cap {cap})",
        "source | card | job | usd | basis | evidence",
    ]
    for row in ledger.rows:
        lines.append(
            f"{row.source} | {row.card} | {row.job} | ${row.usd:.4f} | {row.basis} | {row.evidence}"
        )
    lines.append("totals per source:")
    for source in sorted(ledger.per_source):
        lines.append(f"  {source}: ${ledger.per_source[source]:.4f}")
    lines.append("totals per card:")
    for card in sorted(ledger.per_card):
        lines.append(f"  {card}: ${ledger.per_card[card]:.4f}")
    verdict = (
        f"OVER CAP by ${ledger.over_by_usd:.4f}"
        if ledger.over_cap
        else f"under cap (headroom ${ledger.headroom_usd:.4f})"
    )
    lines.append(f"grand total ${ledger.total_usd:.4f} vs cap {cap}: {verdict}")
    for note in ledger.notes:
        lines.append(f"note: {note}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Catalog + filesystem readers (read-only; never spend, never write).
# ---------------------------------------------------------------------------


#: Modal rows lag real usage: warn when the latest fetched billing hour
#: is older than this versus now (the catalog may undercount tonight).
MODAL_STALE_AFTER_HOURS = 4.0

#: Refusal when Modal billing rows are stale and launch is not allowed despite staleness.
REASON_STALE_MODAL = "stale_modal_billing"


def query_modal_rows(
    database_url: str,
    window_start: datetime,
    window_end: datetime,
    *,
    card_by_app: Mapping[str, str] | None = None,
) -> tuple[list[SpendRow], str]:
    """Billed Modal rows from catalog cache for ``[window_start, window_end)``.

    Per-(object_id, UTC day) rule: for each object/day, take
    ``max(daily_cost, sum(hourly_costs))``. This survives PK collision
    (resolution not in key) and partial snapshots without double-counting.
    For intra-day windows, if an object/day has only a daily row, it counts
    the full daily row conservatively.

    Costs are then split by billed app (``description``) instead of one
    ``modal-account`` line. An app lands on an explicit card only through
    ``card_by_app`` (an operator-supplied app -> HAR card binding validated
    fail-closed); every app without a binding stays on its own
    ``unattributed`` row as the unknown residual. The split conserves the
    per-object total exactly: no daily+hourly double count, and never an
    inferred per-job model charge (Modal bills the account, not jobs).
    When no explicit binding is configured the note says so honestly.
    """
    import psycopg

    bound: dict[str, str] = {}
    for app, card in (card_by_app or {}).items():
        normalized = normalize_linear_card(card)
        if normalized is None:
            raise ValueError(f"explicit card binding for app {app!r} is empty")
        bound[app] = normalized
    window_start = _coerce_utc(window_start)
    window_end = _coerce_utc(window_end)
    with psycopg.connect(database_url) as connection:
        rows = connection.execute(
            """
            WITH hourly_per_obj AS (
                SELECT object_id,
                       description AS app,
                       (interval_start AT TIME ZONE 'UTC')::date AS day,
                       coalesce(sum(cost_usd), 0) AS hourly_usd,
                       count(*) AS hourly_count,
                       max(reported_at) AS hourly_reported
                FROM modal_billing_rows
                WHERE resolution = 'h'
                  AND interval_start >= %s AND interval_start < %s
                GROUP BY object_id, description, (interval_start AT TIME ZONE 'UTC')::date
            ),
            daily_per_obj AS (
                SELECT object_id,
                       description AS app,
                       (interval_start AT TIME ZONE 'UTC')::date AS day,
                       coalesce(sum(cost_usd), 0) AS daily_usd,
                       count(*) AS daily_count,
                       max(reported_at) AS daily_reported
                FROM modal_billing_rows
                WHERE resolution = 'd'
                  AND interval_start < %s AND interval_start + interval '1 day' > %s
                GROUP BY object_id, description, (interval_start AT TIME ZONE 'UTC')::date
            ),
            combined_keys AS (
                SELECT object_id, app, day FROM hourly_per_obj
                UNION
                SELECT object_id, app, day FROM daily_per_obj
            )
            SELECT
                k.object_id,
                k.app,
                k.day,
                coalesce(h.hourly_usd, 0) AS hourly_usd,
                coalesce(d.daily_usd, 0) AS daily_usd,
                coalesce(h.hourly_count, 0) AS hourly_count,
                coalesce(d.daily_count, 0) AS daily_count,
                greatest(h.hourly_reported, d.daily_reported) AS reported_at
            FROM combined_keys k
            LEFT JOIN hourly_per_obj h
                ON h.object_id = k.object_id AND h.app = k.app AND h.day = k.day
            LEFT JOIN daily_per_obj d
                ON d.object_id = k.object_id AND d.app = k.app AND d.day = k.day
            """,
            (window_start, window_end, window_end, window_start),
        ).fetchall()

    label = f"{window_start.isoformat()}..{window_end.isoformat()}"
    if not rows:
        return [], f"modal: no billing rows matching {label} in modal_billing_rows"

    per_app_usd: dict[str, float] = {}
    per_app_count: dict[str, int] = {}
    per_app_objects: dict[str, set[str]] = {}
    total_count = 0
    max_reported: datetime | None = None
    sub_notes: list[str] = []

    for obj_id, app, d, h_usd, d_usd, h_cnt, d_cnt, reported in rows:
        h_val = float(h_usd)
        d_val = float(d_usd)
        if h_cnt > 0 and d_cnt > 0:
            cost = max(h_val, d_val)
            cnt = h_cnt
        elif h_cnt > 0:
            cost = h_val
            cnt = h_cnt
        else:
            cost = d_val
            cnt = d_cnt
            day_start = datetime.combine(d, time.min, tzinfo=UTC)
            day_end = day_start + timedelta(days=1)
            if window_start > day_start or window_end < day_end:
                sub_notes.append(
                    f"object {obj_id} on {d} has only daily row; counted ${d_val:.4f} conservatively"
                )

        app_name = app if isinstance(app, str) and app else str(obj_id)
        per_app_usd[app_name] = per_app_usd.get(app_name, 0.0) + cost
        per_app_count[app_name] = per_app_count.get(app_name, 0) + cnt
        per_app_objects.setdefault(app_name, set()).add(str(obj_id))
        total_count += cnt
        if reported is not None:
            max_reported = reported if max_reported is None else max(max_reported, reported)

    total_usd = math.fsum(per_app_usd.values())
    modal_rows = [
        SpendRow(
            source="modal",
            card=bound.get(app_name, UNATTRIBUTED),
            job=app_name,
            usd=usd,
            basis=BASIS_BILLED,
            evidence=(
                f"catalog:modal_billing_rows:{per_app_count[app_name]} rows:"
                f"{len(per_app_objects[app_name])} objects"
            ),
        )
        for app_name, usd in sorted(per_app_usd.items())
    ]
    residual = [app for app in per_app_usd if app not in bound]
    if bound:
        binding_note = (
            f"{len(bound)} explicit app->card binding(s) applied; "
            f"{len(residual)} app(s) unattributed residual"
            if residual
            else f"{len(bound)} explicit app->card binding(s) applied; no unattributed residual"
        )
    else:
        binding_note = (
            "no explicit app->card bindings recorded; every app is unattributed "
            "residual (Modal bills the account, not jobs)"
        )
    note_text = (
        f"modal: billed ${total_usd:.4f} across {total_count} rows "
        f"in {label} split by app ({len(modal_rows)} apps; {binding_note})"
        + (f" (last reported {max_reported})" if max_reported else "")
    )
    if sub_notes:
        note_text += " (" + "; ".join(sub_notes[:3]) + ")"
    return modal_rows, note_text


def query_modal_latest_interval(database_url: str) -> datetime | None:
    """Newest Modal billing ``interval_start`` in the catalog, if any."""
    import psycopg

    with psycopg.connect(database_url) as connection:
        row = connection.execute("SELECT max(interval_start) FROM modal_billing_rows").fetchone()
    latest = row[0] if row is not None else None
    if isinstance(latest, datetime):
        return latest if latest.tzinfo is not None else latest.replace(tzinfo=UTC)
    parsed = parse_dt(latest)
    return parsed


def query_model_job_rows(
    database_url: str, window_start: datetime, window_end: datetime
) -> tuple[list[SpendRow], list[str], float]:
    """Settled per-job model dollars for jobs finished in ``[window_start, window_end)``.

    Finish time is resolved in UTC:
    1. From ``lab_metadata["finished_at"]`` (Harbor's aware UTC finish time).
    2. Fallback: ``jobs.finished_at``. Strings with explicit UTC offsets
       (including negative offsets like -07:00) are converted to UTC directly;
       only truly naive datetimes receive the historical host timezone
       (``America/New_York``) fallback.
    Jobs whose finish time cannot be established in UTC are counted as
    unfinished/unattributable. Settled proxy ledgers attribute whole to the
    job finish time (they are not split across windows).

    Returns ``(spend_rows, notes, unresolved_model_usd)``.
    """
    import psycopg

    window_start = _coerce_utc(window_start)
    window_end = _coerce_utc(window_end)
    with psycopg.connect(database_url) as connection:
        rows = connection.execute(
            """
            SELECT job_name, evidence_path, id, finished_at, lab_metadata
            FROM jobs
            WHERE finished_at IS NOT NULL
            """
        ).fetchall()
    spend_rows: list[SpendRow] = []
    unresolved_model_usd = 0.0
    without_settled = 0
    selfhosted_zero = 0
    unfinished = 0
    model_conflicts: list[str] = []
    for job_name, evidence_path, job_id, finished_at_raw, lab_metadata in rows:
        finished = None
        if isinstance(lab_metadata, dict) and lab_metadata.get("finished_at"):
            finished = parse_dt(lab_metadata.get("finished_at"))
        if finished is None and finished_at_raw:
            parsed = None
            if isinstance(finished_at_raw, str):
                try:
                    text = finished_at_raw.strip()
                    parsed = datetime.fromisoformat(text.removesuffix("Z"))
                    if text.endswith("Z"):
                        parsed = parsed.replace(tzinfo=UTC)
                except ValueError:
                    parsed = parse_dt(finished_at_raw)
            elif isinstance(finished_at_raw, datetime):
                parsed = finished_at_raw
            else:
                parsed = parse_dt(finished_at_raw)

            if parsed is not None:
                if parsed.tzinfo is None:
                    try:
                        import zoneinfo

                        tz = zoneinfo.ZoneInfo("America/New_York")
                        finished = parsed.replace(tzinfo=tz).astimezone(UTC)
                    except Exception:
                        finished = parsed.replace(tzinfo=UTC)
                else:
                    finished = parsed.astimezone(UTC)
        if finished is None:
            unfinished += 1
            continue
        if not (window_start <= finished < window_end):
            continue
        provider_usage = (
            lab_metadata.get("provider_usage") if isinstance(lab_metadata, dict) else None
        )
        used_usd, attempted_usd, reason = model_usage_from_provider_usage(provider_usage)
        if isinstance(provider_usage, Mapping):
            unresolved_requests = provider_usage.get("unresolved_requests", 0)
            if unresolved_requests and attempted_usd is None:
                raise SpendUnverified(
                    REASON_CAP_UNVERIFIED,
                    f"finished job {job_name} has {unresolved_requests} unresolved provider call(s) "
                    "with no pricing; cost ceiling cannot be verified",
                )
        if attempted_usd is not None and attempted_usd > 0:
            unresolved_model_usd += attempted_usd

        if used_usd is None:
            without_settled += 1
            if "self-hosted" in reason:
                selfhosted_zero += 1
            continue
        try:
            model_card = resolve_job_card(
                job_name if isinstance(job_name, str) else None,
                linear_card=explicit_card_from_lab_metadata(lab_metadata),
            )
        except ValueError as exc:
            model_card = UNATTRIBUTED
            model_conflicts.append(
                f"{job_name if isinstance(job_name, str) else job_id}: {exc}"
            )
        spend_rows.append(
            SpendRow(
                source="model",
                card=model_card,
                job=job_name if isinstance(job_name, str) else str(job_id),
                usd=used_usd,
                basis=BASIS_LEDGER,
                evidence=(
                    evidence_path
                    if isinstance(evidence_path, str) and evidence_path
                    else f"catalog:jobs:{job_id}"
                ),
            )
        )
    notes = [
        f"model: {len(spend_rows)} jobs with settled proxy-ledger dollars; "
        f"{without_settled} finished jobs carry none "
        f"({selfhosted_zero} zero-priced self-hosted, $0 at the proxy)"
    ]
    if unresolved_model_usd > 0:
        notes.append(
            f"model: ${unresolved_model_usd:.4f} unresolved attempted charges from finished jobs"
        )
    if unfinished:
        notes.append(f"model: {unfinished} jobs lack finished_at; unattributable")
    if model_conflicts:
        notes.append(
            "model: fail-closed to unattributed on card conflict: "
            + "; ".join(model_conflicts[:3])
        )
    return spend_rows, notes, unresolved_model_usd


def _spend_jsonl_files(roots: Iterable[Path]) -> list[Path]:
    """Every ``spend.jsonl`` under the given roots (read-only glob)."""
    files: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        files.extend(sorted(root.rglob("spend.jsonl")))
    return files


def collect_spend_jsonl_rows(
    repo_root: Path,
    window_start: datetime,
    window_end: datetime,
    *,
    extra_roots: Iterable[Path] = (),
    label: str | None = None,
) -> tuple[list[SpendRow], list[str]]:
    """Aggregate experiment ``spend.jsonl`` ledgers for ``[window_start, window_end)``.

    Ledgers are discovered under the repo root and (read-only) sibling
    worktree checkouts; byte-identical rows shared across checkouts
    count once. One row per ledger file keeps the ledger compact.
    ``spend.jsonl`` rows count when their own ``ts`` timestamp falls in
    the window.
    """
    window_start = _coerce_utc(window_start)
    window_end = _coerce_utc(window_end)
    caption = (
        label if label is not None else f"{window_start.isoformat()}..{window_end.isoformat()}"
    )
    roots = [repo_root, *[r for r in extra_roots if r != repo_root]]
    files = _spend_jsonl_files(roots)
    seen: set[str] = set()
    spend_rows: list[SpendRow] = []
    notes: list[str] = []
    duplicates = 0
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            notes.append(f"model: unreadable ledger {path}")
            continue
        calls: list[dict[str, Any]] = []
        bad = 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if not isinstance(record, dict):
                bad += 1
                continue
            digest = record_hash(record)
            if digest in seen:
                duplicates += 1
                continue
            seen.add(digest)
            calls.append(record)
        total, in_window, _ = aggregate_spend_jsonl_calls(calls, window_start, window_end)
        try:
            rel = path.relative_to(repo_root)
            evidence = str(rel)
        except ValueError:
            evidence = str(path)
        if in_window:
            spend_rows.append(
                SpendRow(
                    source="model",
                    card=card_for_ledger_path(path),
                    job=path.parent.name,
                    usd=total,
                    basis=BASIS_LEDGER,
                    evidence=f"{evidence}:{in_window} unique rows",
                )
            )
        if bad:
            notes.append(f"model: {bad} unparsable lines in {evidence}")
    notes.append(
        f"model: {len(files)} spend.jsonl ledgers scanned, "
        f"{len(spend_rows)} contribute to {caption}, "
        f"{duplicates} duplicate rows across checkouts ignored"
    )
    return spend_rows, notes


def _read_task_toml_env(task_path: Any) -> dict[str, Any]:
    """Read-only ``[environment]`` from a staged task dir, else {}."""
    if not isinstance(task_path, str) or not task_path:
        return {}
    candidate = Path(task_path) / "task.toml"
    try:
        if not candidate.is_file():
            return {}
        import tomllib

        payload = tomllib.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    env = payload.get("environment") if isinstance(payload, dict) else None
    return dict(env) if isinstance(env, Mapping) else {}


def query_daytona_rows(
    database_url: str, window_start: datetime, window_end: datetime
) -> tuple[list[SpendRow], list[str]]:
    """Daytona sandbox estimates: trial wall time in the window x rate card.

    Each trial contributes the seconds of its wall interval overlapping
    ``[window_start, window_end)`` (intervals crossing a boundary split).
    Per-job rows aggregate trial slices; every dollar here is basis
    ``estimate`` from the Daytona list-price card.

    Card attribution is explicit-only: the catalog ``lab_metadata``
    ``experiment.linear_card`` first, the job-name prefix second, failing
    closed (unattributed with a conflict note) on disagreement. Task,
    model, and harness path substrings are never treated as attribution,
    so jobs that predate the explicit field stay ``unattributed`` with an
    honest note instead of a guessed card.
    """
    import psycopg

    from evallab.task_qualification import estimate_cost_usd

    window_start = _coerce_utc(window_start)
    window_end = _coerce_utc(window_end)

    with psycopg.connect(database_url) as connection:
        trials = connection.execute(
            """
            SELECT t.trial_name, t.task_name, t.started_at, t.finished_at,
                   t.duration_seconds, t.raw_config, t.raw_lock,
                   j.job_name, j.evidence_path, j.raw_config, j.lab_metadata
            FROM trials t JOIN jobs j ON j.id = t.job_id
            WHERE t.started_at IS NOT NULL AND t.finished_at IS NOT NULL
            """
        ).fetchall()
    per_job_usd: dict[str, float] = {}
    per_job_meta: dict[str, dict[str, Any]] = {}
    trials_in_window = 0
    skipped_time = 0
    skipped_backend = 0
    unrated = 0
    measured = 0
    fallback = 0
    card_conflicts: list[str] = []
    for row in trials:
        (
            trial_name,
            task_name,
            started_raw,
            finished_raw,
            duration_seconds,
            trial_config,
            trial_lock,
            job_name,
            evidence_path,
            job_config,
        ) = row[:10]
        job_metadata = row[10] if len(row) > 10 else None
        started = parse_dt(started_raw)
        finished = parse_dt(finished_raw)
        if started is None or finished is None or finished <= started:
            skipped_time += 1
            continue
        portion = window_overlap_seconds(started, finished, window_start, window_end)
        if portion <= 0:
            continue
        if not backend_is_daytona(job_config, trial_config, trial_lock):
            skipped_backend += 1
            continue
        task_path = None
        for source in (trial_lock, trial_config):
            if isinstance(source, Mapping):
                task = source.get("task")
                if isinstance(task, Mapping) and isinstance(task.get("path"), str):
                    task_path = task["path"]
                    break
        family = (
            task_name.split("/")[0] if isinstance(task_name, str) and "/" in task_name else None
        )
        (cpus, memory_mb, storage_mb), basis = trial_daytona_resources(
            task_family=family,
            environment_configs=(job_config, trial_config),
            task_toml_env=_read_task_toml_env(task_path),
        )
        if "missing" in basis:
            unrated += 1
            continue
        if "fallback" in basis:
            fallback += 1
        else:
            measured += 1
        seconds = portion
        if (
            isinstance(duration_seconds, (int, float))
            and not isinstance(duration_seconds, bool)
            and duration_seconds > 0
        ):
            # Scale catalog duration to the window slice when the trial
            # crosses a window boundary: the slice keeps its share of wall time.
            wall = (finished - started).total_seconds()
            if wall > 0:
                seconds = float(duration_seconds) * portion / wall
        usd = estimate_cost_usd(
            backend="daytona",
            sandbox_seconds=seconds,
            cpus=cpus,
            memory_mb=memory_mb,
            storage_mb=storage_mb,
        )
        if usd is None:
            unrated += 1
            continue
        trials_in_window += 1
        key = job_name if isinstance(job_name, str) else str(trial_name)
        per_job_usd[key] = per_job_usd.get(key, 0.0) + float(usd)
        meta = per_job_meta.setdefault(
            key,
            {
                "card": UNATTRIBUTED,
                "explicit": False,
                "evidence": evidence_path
                if isinstance(evidence_path, str) and evidence_path
                else key,
                "trials": 0,
            },
        )
        if meta["trials"] == 0:
            try:
                explicit = explicit_card_from_lab_metadata(job_metadata)
                meta["card"] = resolve_job_card(
                    job_name if isinstance(job_name, str) else None,
                    linear_card=explicit,
                )
                meta["explicit"] = explicit is not None
            except ValueError as exc:
                meta["card"] = UNATTRIBUTED
                card_conflicts.append(f"{key}: {exc}")
        meta["trials"] += 1
    spend_rows = [
        SpendRow(
            source="daytona",
            card=per_job_meta[key]["card"],
            job=key,
            usd=usd,
            basis=BASIS_ESTIMATE,
            evidence=f"{per_job_meta[key]['evidence']}:{per_job_meta[key]['trials']} trials",
        )
        for key, usd in sorted(per_job_usd.items())
    ]
    explicit_jobs = sum(1 for key in per_job_usd if per_job_meta[key]["explicit"])
    unattributed_jobs = sum(1 for key in per_job_usd if per_job_meta[key]["card"] == UNATTRIBUTED)
    notes = [
        f"daytona: {trials_in_window} trial slices across {len(spend_rows)} jobs "
        f"(rate measured {measured}, family fallback {fallback}); "
        f"{unrated} slices unratable, {skipped_backend} non-daytona, "
        f"{skipped_time} missing time"
    ]
    if explicit_jobs:
        notes.append(
            f"daytona: {explicit_jobs} jobs attributed via explicit linear_card "
            "from catalog lab_metadata"
        )
    if unattributed_jobs:
        notes.append(
            f"daytona: {unattributed_jobs} jobs unattributed (no explicit "
            "linear_card in catalog lab_metadata and no HAR job-name prefix; "
            "harness/task path substrings are never attribution)"
        )
    if card_conflicts:
        notes.append(
            "daytona: fail-closed to unattributed on card conflict: "
            + "; ".join(card_conflicts[:3])
        )
    return spend_rows, notes


def sibling_worktree_roots(repo_root: Path) -> list[Path]:
    """Other worktrees beside this one across the entire repository (read-only).

    Resolves the primary checkout via ``git rev-parse --git-common-dir``,
    then adds worktrees from ``git worktree list --porcelain`` and scans
    ``<primary>/.worktrees/*``. When run from a linked worktree, this
    discovers the primary checkout and all sibling worktrees; when run
    from the primary checkout, it discovers all worktrees under
    ``.worktrees``. The caller's own checkout is always excluded.
    """
    import subprocess

    roots: set[Path] = set()
    repo_root = Path(repo_root).resolve()

    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
        )
        common_dir = Path(proc.stdout.strip())
        if not common_dir.is_absolute():
            common_dir = (repo_root / common_dir).resolve()
        else:
            common_dir = common_dir.resolve()
        primary_root = common_dir.parent
    except Exception:
        primary_root = repo_root

    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        for line in proc.stdout.splitlines():
            if line.startswith("worktree "):
                wt_path = Path(line.removeprefix("worktree ").strip())
                if wt_path.is_dir():
                    roots.add(wt_path.resolve())
    except Exception:
        pass

    if primary_root.is_dir():
        roots.add(primary_root.resolve())
        wt_dir = primary_root / ".worktrees"
        if wt_dir.is_dir():
            try:
                for child in wt_dir.iterdir():
                    if child.is_dir():
                        roots.add(child.resolve())
            except OSError:
                pass

    return sorted(p for p in roots if p != repo_root)


def build_window_ledger(
    repo_root: Path,
    window_start: datetime,
    window_end: datetime,
    *,
    database_url: str,
    cap_usd: float = DEFAULT_CAP_USD,
    extra_roots: Iterable[Path] = (),
    label: str | None = None,
    staleness_now: datetime | None = None,
    include_window_preamble: bool = True,
    live_modal_row: SpendRow | None = None,
    live_modal_notes: Iterable[str] = (),
) -> WindowLedger:
    """Assemble the full window ledger from all three sources (read-only).

    ``label`` names the window in notes (defaults to the ISO range).
    When ``live_modal_notes`` is provided, they are used directly (live report);
    otherwise Modal spend is read from the catalog cache.
    """
    window_start = _coerce_utc(window_start)
    window_end = _coerce_utc(window_end)
    caption = (
        label if label is not None else f"{window_start.isoformat()}..{window_end.isoformat()}"
    )
    rows: list[SpendRow] = []
    notes: list[str] = []
    if include_window_preamble:
        notes.append(f"window: settled spend in {caption} (half-open [start, end))")
        notes.append(
            "window granularity: modal rows count whole when their hour starts in the window; "
            "daytona trials contribute overlapping wall seconds; spend.jsonl rows count by "
            "their own ts; per-job proxy ledgers count whole on the job finish day"
        )
    injected_notes = tuple(live_modal_notes)
    if injected_notes:
        notes.extend(injected_notes)
        if live_modal_row is not None:
            rows.append(live_modal_row)
    else:
        modal_rows, modal_note = query_modal_rows(database_url, window_start, window_end)
        notes.append(modal_note)
        rows.extend(modal_rows)
        if window_end.date() >= datetime.now(UTC).date():
            notes.append(
                "modal: current-day billing data is partial; unsettled usage may still accumulate"
            )
    if staleness_now is not None:
        latest = query_modal_latest_interval(database_url)
        if latest is None:
            notes.append("modal: no billing rows in the catalog at all; settled modal is unknown")
        else:
            age_hours = (_coerce_utc(staleness_now) - latest).total_seconds() / 3600.0
            if age_hours > MODAL_STALE_AFTER_HOURS:
                notes.append(
                    f"modal: billing rows are stale (latest hour {latest.isoformat()}, "
                    f"{age_hours:.1f}h old vs now); modal rows lag, so settled spend "
                    "may undercount tonight"
                )
    model_job_rows, model_notes, unresolved_model_usd = query_model_job_rows(
        database_url, window_start, window_end
    )
    rows.extend(model_job_rows)
    notes.extend(model_notes)
    jsonl_rows, jsonl_notes = collect_spend_jsonl_rows(
        repo_root, window_start, window_end, extra_roots=extra_roots
    )
    rows.extend(jsonl_rows)
    notes.extend(jsonl_notes)
    daytona_rows, daytona_notes = query_daytona_rows(database_url, window_start, window_end)
    rows.extend(daytona_rows)
    notes.extend(daytona_notes)
    return summarize_window(
        window_start,
        window_end,
        rows,
        cap_usd=cap_usd,
        notes=notes,
        unresolved_model_usd=unresolved_model_usd,
    )


def build_day_ledger(
    repo_root: Path,
    day: date,
    *,
    database_url: str,
    cap_usd: float | None = None,
    extra_roots: Iterable[Path] = (),
) -> DayLedger:
    """Assemble the full day ledger from all three sources (read-only).

    A day is the special case of :func:`build_window_ledger` covering
    [day 00:00, next day 00:00) UTC. Without an explicit cap, resolve policy
    at that day's 00:00 UTC: the dated override applies to the whole reported
    day, even when reporting it after expiry. This never extends launch approval.
    """
    start, end = day_to_window(day)
    cap_description = None
    if cap_usd is None:
        from evallab.execution_contracts import load_policy

        cap_usd, cap_description = policy_cap_at(
            load_policy(repo_root / "policy/standing-approvals.yaml"), start
        )
    window = build_window_ledger(
        repo_root,
        start,
        end,
        database_url=database_url,
        cap_usd=cap_usd,
        extra_roots=extra_roots,
        label=day.isoformat(),
        include_window_preamble=False,
    )
    return summarize_day(
        day,
        window.rows,
        cap_usd=cap_usd,
        notes=window.notes,
        unresolved_model_usd=window.unresolved_model_usd,
        cap_description=cap_description,
    )


# ---------------------------------------------------------------------------
# Pre-launch spend-cap check (HAR-129; HAR-122 proposal §2-§5).
# ---------------------------------------------------------------------------
#
# Committed(window) = Settled(window) + InFlight + candidate, where
# Settled is the window ledger above and InFlight reserves one ceiling
# per queued spec in ``running``/``approved`` states:
# ``max(cost_limit_usd, est_cost_usd)``. The check fails closed: an
# unreachable catalog is never treated as $0, and a cloud (non-docker)
# queued spec with no positive ``est_cost_usd`` refuses as unratable.
# A total strictly greater than the cap refuses; exactly-at-cap allows.

#: Refusal when committed spend exceeds the cap (CLI exit 3).
REASON_CEILING_EXCEEDED = "daily_cost_ceiling_exceeded"
#: Fail-closed when the catalog is unreachable (CLI exit 2). Never $0.
REASON_CAP_UNVERIFIED = "daily_cap_unverified"
#: Fail-closed when a queued cloud spec cannot be rated (CLI exit 2).
REASON_UNRATABLE_SPEC = "unratable_cost_spec"

#: Queue states whose specs hold reserved spend against the cap.
#: Approved is scanned before running so an approved->running transition
#: is never missed between directory reads.
IN_FLIGHT_STATES = ("approved", "running")


class SpendUnverified(Exception):
    """Fail-closed signal: spend cannot be established, so no launch."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class InFlightItem:
    """One queued spec's reservation against the cap."""

    spec_id: str | None
    name: str
    state: str
    environment: str
    est_cost_usd: float
    cost_limit_usd: float | None
    reservation_usd: float


@dataclass(frozen=True)
class UnratableSpec:
    """A queued cloud spec with no usable cost estimate (fail closed)."""

    spec_id: str | None
    name: str
    state: str
    environment: str
    reason: str


@dataclass(frozen=True)
class LaunchDecision:
    """Verdict of the pre-launch spend-cap check for one candidate."""

    allowed: bool
    reason_code: str | None
    window_start: datetime
    window_end: datetime
    settled_usd: float
    per_source_usd: dict[str, float]
    per_basis_usd: dict[str, float]
    in_flight_usd: float
    in_flight_count: int
    in_flight: tuple[InFlightItem, ...]
    unratable: tuple[UnratableSpec, ...]
    candidate_usd: float
    committed_usd: float
    cap_usd: float
    headroom_usd: float
    unresolved_jobs_usd: float = 0.0
    notes: tuple[str, ...] = field(default_factory=tuple)
    cap_description: str | None = None


def parse_launch_since(value: str) -> datetime:
    """Parse a ``--since`` ISO-8601 timestamp to an aware UTC datetime."""
    parsed = parse_dt(value)
    if parsed is None:
        raise ValueError(f"invalid --since: {value!r} (expected ISO-8601)")
    return parsed


def day_window_now(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Current UTC day window ``[today 00:00, now)`` for the default check."""
    current = _coerce_utc(now) if now is not None else datetime.now(UTC)
    start = datetime.combine(current.date(), time.min, tzinfo=UTC)
    return start, current


def is_cloud_environment(environment: str | None) -> bool:
    """True for non-Docker spec environments (cloud or remote backends).

    Mirrors the queue's ``cloud_or_remote_environment`` gate: anything
    that is not local ``docker`` burns provider infrastructure, so its
    cost must be rated before launch.
    """
    return (environment or "docker") != "docker"


def reservation_for_spec(spec: Any) -> float:
    """In-flight reservation for one queued spec: ``max(cost_limit, est)``.

    Reserves the authorized upper ceiling while the job executes so
    concurrent jobs cannot oversubscribe the remaining headroom. A
    missing ``cost_limit_usd`` falls back to the estimate; both missing
    reserves $0 (local controls genuinely cost nothing).
    """
    est = getattr(spec, "est_cost_usd", 0.0)
    est_usd = float(est) if isinstance(est, (int, float)) and not isinstance(est, bool) else 0.0
    limit = getattr(spec, "cost_limit_usd", None)
    limit_usd = (
        float(limit) if isinstance(limit, (int, float)) and not isinstance(limit, bool) else None
    )
    candidates = [est_usd] + ([limit_usd] if limit_usd is not None else [])
    return max(candidates) if candidates else 0.0


def collect_in_flight(
    queue_root: Path,
) -> tuple[list[InFlightItem], list[UnratableSpec], list[str]]:
    """Reserve in-flight spend for every queued ``running``/``approved`` spec.

    Read-only: opens the queue with ``create=False`` and never writes.
    A missing queue directory holds no specs (empty, with a note). Any
    unreadable or invalid spec file raises :class:`SpendUnverified` —
    a reservation we cannot read is spend we cannot bound.

    A queued cloud (non-docker) spec with no positive ``est_cost_usd``
    is unratable: ``est_cost_usd`` defaults to ``0.0`` (no estimate
    recorded), and remote environments require an explicit estimate at
    prepare time, so zero means the infra cost is unknown, not free.
    """
    from evallab.queue import DirectoryQueue

    if not Path(queue_root).is_dir():
        return [], [], [f"in-flight: no queue directory at {queue_root}; reserving $0"]
    queue = DirectoryQueue(Path(queue_root), create=False)
    seen_specs: dict[str, InFlightItem] = {}
    unratable_specs: dict[str, UnratableSpec] = {}
    notes: list[str] = []
    for state in IN_FLIGHT_STATES:
        try:
            records = queue.list_specs(state)  # type: ignore[arg-type]
        except Exception as exc:
            raise SpendUnverified(
                REASON_CAP_UNVERIFIED,
                f"in-flight: cannot list queue/{state}: {type(exc).__name__}: {exc}",
            ) from exc
        for path, spec in records:
            spec_key = str(getattr(spec, "spec_id", None) or path.name)
            try:
                reservation = reservation_for_spec(spec)
            except Exception as exc:
                raise SpendUnverified(
                    REASON_CAP_UNVERIFIED,
                    f"in-flight: cannot rate queued spec {path.name}: {type(exc).__name__}: {exc}",
                ) from exc
            est = getattr(spec, "est_cost_usd", 0.0)
            est_ok = isinstance(est, (int, float)) and not isinstance(est, bool) and est > 0
            if is_cloud_environment(getattr(spec, "environment", "docker")) and not est_ok:
                unratable_specs[spec_key] = UnratableSpec(
                    spec_id=getattr(spec, "spec_id", None),
                    name=getattr(spec, "name", path.stem),
                    state=state,
                    environment=str(getattr(spec, "environment", "docker")),
                    reason=(
                        "cloud spec records no positive est_cost_usd; "
                        "its infrastructure cost is unknown, not free"
                    ),
                )
            else:
                unratable_specs.pop(spec_key, None)
            seen_specs[spec_key] = InFlightItem(
                spec_id=getattr(spec, "spec_id", None),
                name=getattr(spec, "name", path.stem),
                state=state,
                environment=str(getattr(spec, "environment", "docker")),
                est_cost_usd=float(est) if isinstance(est, (int, float)) else 0.0,
                cost_limit_usd=getattr(spec, "cost_limit_usd", None),
                reservation_usd=reservation,
            )
    items = list(seen_specs.values())
    unratable = list(unratable_specs.values())
    notes.append(
        f"in-flight: {len(items)} queued spec(s) in running/approved reserve "
        f"${sum(item.reservation_usd for item in items):.4f}"
    )
    return items, unratable, notes


def decide_launch(
    *,
    window_start: datetime,
    window_end: datetime,
    settled_usd: float,
    per_source_usd: dict[str, float] | None = None,
    per_basis_usd: dict[str, float] | None = None,
    in_flight_usd: float = 0.0,
    in_flight_count: int = 0,
    in_flight: Iterable[InFlightItem] = (),
    unratable: Iterable[UnratableSpec] = (),
    unresolved_jobs_usd: float = 0.0,
    candidate_usd: float = 0.0,
    cap_usd: float = DEFAULT_CAP_USD,
    notes: Iterable[str] = (),
) -> LaunchDecision:
    """Pure commit-vs-cap verdict: ``settled + in-flight + unresolved-jobs + candidate``.

    Refusal is strict: a committed total *greater than* the cap refuses
    with ``daily_cost_ceiling_exceeded``; exactly-at-cap allows (the cap
    is a ceiling to stay within, not below).
    """
    if not math.isfinite(candidate_usd) or candidate_usd < 0:
        raise ValueError(
            f"candidate_usd must be a finite non-negative number, got {candidate_usd!r}"
        )
    if not math.isfinite(cap_usd) or cap_usd <= 0:
        raise ValueError(f"cap_usd must be a finite positive number, got {cap_usd!r}")
    flight = tuple(in_flight)
    bad = tuple(unratable)
    committed = settled_usd + in_flight_usd + unresolved_jobs_usd + candidate_usd
    over, _, headroom = cap_status(committed, cap_usd)
    return LaunchDecision(
        allowed=not over,
        reason_code=REASON_CEILING_EXCEEDED if over else None,
        window_start=_coerce_utc(window_start),
        window_end=_coerce_utc(window_end),
        settled_usd=settled_usd,
        per_source_usd=dict(per_source_usd or {}),
        per_basis_usd=dict(per_basis_usd or {}),
        in_flight_usd=in_flight_usd,
        in_flight_count=in_flight_count,
        in_flight=flight,
        unratable=bad,
        candidate_usd=candidate_usd,
        committed_usd=committed,
        cap_usd=cap_usd,
        headroom_usd=headroom,
        unresolved_jobs_usd=unresolved_jobs_usd,
        notes=tuple(notes),
    )


def unverified_decision(
    *,
    reason_code: str,
    message: str,
    window_start: datetime,
    window_end: datetime,
    candidate_usd: float,
    cap_usd: float,
    settled_usd: float = 0.0,
    per_source_usd: dict[str, float] | None = None,
    per_basis_usd: dict[str, float] | None = None,
    in_flight_usd: float = 0.0,
    in_flight_count: int = 0,
    in_flight: Iterable[InFlightItem] = (),
    unratable: Iterable[UnratableSpec] = (),
    unresolved_jobs_usd: float = 0.0,
    notes: Iterable[str] = (),
) -> LaunchDecision:
    """Fail-closed verdict: never allowed, never assumed $0."""
    cand = candidate_usd if math.isfinite(candidate_usd) and candidate_usd >= 0 else 0.0
    cap = cap_usd if math.isfinite(cap_usd) and cap_usd > 0 else DEFAULT_CAP_USD
    committed = settled_usd + in_flight_usd + unresolved_jobs_usd + cand
    return LaunchDecision(
        allowed=False,
        reason_code=reason_code,
        window_start=_coerce_utc(window_start),
        window_end=_coerce_utc(window_end),
        settled_usd=settled_usd,
        per_source_usd=dict(per_source_usd or {}),
        per_basis_usd=dict(per_basis_usd or {}),
        in_flight_usd=in_flight_usd,
        in_flight_count=in_flight_count,
        in_flight=tuple(in_flight),
        unratable=tuple(unratable),
        candidate_usd=cand,
        committed_usd=committed,
        cap_usd=cap,
        headroom_usd=cap - committed,
        unresolved_jobs_usd=unresolved_jobs_usd,
        notes=(message, *tuple(notes)),
    )


def check_launch(
    *,
    repo_root: Path,
    queue_root: Path,
    database_url: str,
    window_start: datetime,
    window_end: datetime,
    candidate_usd: float,
    cap_usd: float = DEFAULT_CAP_USD,
    extra_roots: Iterable[Path] = (),
    now: datetime | None = None,
    allow_stale_modal: bool = False,
    modal_refresher: Any | None = None,
) -> LaunchDecision:
    """Pre-launch spend-cap check for one candidate (read-only).

    ``Committed = Settled(window) + InFlight + candidate``. Returns a
    :class:`LaunchDecision` the CLI prints and a later dispatch hook can
    call: ``allowed`` launches, ``daily_cost_ceiling_exceeded`` refuses,
    anything else fails closed:
    - ``daily_cap_unverified`` when the catalog or queue cannot be read (never $0).
    - ``unratable_cost_spec`` when a queued cloud spec has no estimate.
    - ``stale_modal_billing`` when the latest stored Modal interval is stale (>4h)
      and the window extends past it, unless ``allow_stale_modal=True``.
    """
    if not math.isfinite(candidate_usd) or candidate_usd < 0:
        return unverified_decision(
            reason_code=REASON_CAP_UNVERIFIED,
            message=(
                f"REFUSAL: {REASON_CAP_UNVERIFIED} (invalid candidate_usd: "
                f"{candidate_usd!r}; must be a finite non-negative number)"
            ),
            window_start=window_start,
            window_end=window_end,
            candidate_usd=0.0,
            cap_usd=cap_usd if math.isfinite(cap_usd) else DEFAULT_CAP_USD,
        )
    if not math.isfinite(cap_usd) or cap_usd <= 0:
        return unverified_decision(
            reason_code=REASON_CAP_UNVERIFIED,
            message=(
                f"REFUSAL: {REASON_CAP_UNVERIFIED} (invalid cap_usd: "
                f"{cap_usd!r}; must be a finite positive number)"
            ),
            window_start=window_start,
            window_end=window_end,
            candidate_usd=candidate_usd,
            cap_usd=DEFAULT_CAP_USD,
        )
    window_start = _coerce_utc(window_start)
    window_end = _coerce_utc(window_end)
    try:
        flight, unratable, flight_notes = collect_in_flight(queue_root)
    except SpendUnverified as exc:
        return unverified_decision(
            reason_code=exc.reason_code,
            message=f"REFUSAL: {exc.reason_code} ({exc})",
            window_start=window_start,
            window_end=window_end,
            candidate_usd=candidate_usd,
            cap_usd=cap_usd,
        )
    flight_usd = sum(item.reservation_usd for item in flight)

    # -----------------------------------------------------------------------
    # Modal settled spend in gate (Main rules 1 & 2):
    # 1. Always query catalog cache over the window via the per-object rule.
    # 2. Live Modal fetch for window's intersecting UTC dates.
    # 3. Modal settled = max(live_sum, cached_sum). When they differ by > $0.01,
    #    note both. Never skip catalog bills just because live fetch succeeded.
    # 4. Live fetch failure -> unverified (exit 2) unless --allow-stale-modal.
    # -----------------------------------------------------------------------
    try:
        cached_modal_rows, _ = query_modal_rows(database_url, window_start, window_end)
    except Exception as exc:
        # An unreadable cache is unknown spend, never $0 (even with
        # --allow-stale-modal, which only admits a readable stale cache).
        return unverified_decision(
            reason_code=REASON_CAP_UNVERIFIED,
            message=(
                f"REFUSAL: {REASON_CAP_UNVERIFIED} (modal billing cache unreadable, "
                f"spend unknown, never $0: {type(exc).__name__}: {exc})"
            ),
            window_start=window_start,
            window_end=window_end,
            candidate_usd=candidate_usd,
            cap_usd=cap_usd,
        )

    cached_modal_usd = math.fsum(row.usd for row in cached_modal_rows)
    cached_modal_evidence = (
        cached_modal_rows[0].evidence
        if len(cached_modal_rows) == 1
        else f"catalog:modal_billing_rows:{len(cached_modal_rows)} app rows"
        if cached_modal_rows
        else "catalog:modal_billing_rows"
    )

    start_date = window_start.date()
    end_date = (window_end - timedelta(microseconds=1)).date()
    fetch_start = start_date
    fetch_end = end_date + timedelta(days=1)

    live_modal_rows: list[Any] | None = None
    modal_fetch_error: str | None = None

    if modal_refresher is not None:
        try:
            res = modal_refresher(fetch_start, fetch_end)
            if isinstance(res, list):
                live_modal_rows = res
        except Exception as exc:
            modal_fetch_error = f"{type(exc).__name__}: {exc}"
    else:
        try:
            from evallab.modal_billing import fetch_modal_billing_report, store_billing_rows

            live_modal_rows = fetch_modal_billing_report(
                start=fetch_start,
                end=fetch_end,
                resolution="h",
                repo_root=repo_root,
            )
            # Pure upsert into catalog cache (best effort)
            with contextlib.suppress(Exception):
                store_billing_rows(database_url, live_modal_rows, resolution="h")
        except Exception as exc:
            modal_fetch_error = f"{type(exc).__name__}: {exc}"

    effective_modal_row: SpendRow | None = None
    modal_gate_notes: list[str] = []

    if live_modal_rows is not None:
        matching_rows = [
            r for r in live_modal_rows if _live_row_counts(r, window_start, window_end)
        ]
        live_modal_usd = sum(float(r.cost_usd) for r in matching_rows)

        # Rule 1: Modal settled = max(live_sum, cached_sum)
        effective_modal_usd = max(live_modal_usd, cached_modal_usd)

        if abs(live_modal_usd - cached_modal_usd) > 0.01:
            modal_gate_notes.append(
                f"modal: live Modal report (${live_modal_usd:.4f}) and catalog cache (${cached_modal_usd:.4f}) differ; "
                f"using conservative max ${effective_modal_usd:.4f}"
            )
        else:
            modal_gate_notes.append(
                f"modal: billed ${effective_modal_usd:.4f} across {len(matching_rows)} live hourly rows in "
                f"{window_start.isoformat()}..{window_end.isoformat()} (live Modal report)"
            )

        if effective_modal_usd > 0:
            evidence = (
                f"modal:live_report:{len(matching_rows)} rows"
                if effective_modal_usd == live_modal_usd
                else cached_modal_evidence
            )
            effective_modal_row = SpendRow(
                source="modal",
                card=UNATTRIBUTED,
                job="modal-account",
                usd=effective_modal_usd,
                basis=BASIS_BILLED,
                evidence=evidence,
            )
    else:
        # Live fetch failed or was not provided
        if not allow_stale_modal:
            refusal_msg = (
                f"REFUSAL: {REASON_STALE_MODAL} "
                f"(live Modal billing fetch failed: {modal_fetch_error or 'no live rows'}; "
                "modal spend cannot be verified tonight; "
                "pass --allow-stale-modal to fall back to catalog cache)"
            )
            extra_notes = list(flight_notes)
            if modal_fetch_error:
                extra_notes.append(f"modal: {modal_fetch_error}")
            extra_notes.append(
                "modal: reporting lag: Modal billing reports lag real-time execution; "
                "the current hour is partial and unsettled runtime may not yet be reflected."
            )
            return unverified_decision(
                reason_code=REASON_STALE_MODAL,
                message=refusal_msg,
                window_start=window_start,
                window_end=window_end,
                candidate_usd=candidate_usd,
                cap_usd=cap_usd,
                in_flight_usd=flight_usd,
                in_flight_count=len(flight),
                in_flight=flight,
                unratable=unratable,
                notes=extra_notes,
            )
        modal_gate_notes.append(
            f"modal: live billing fetch failed ({modal_fetch_error or 'no live rows'}); "
            f"fell back to catalog cache (${cached_modal_usd:.4f}) (--allow-stale-modal recorded)"
        )
        effective_modal_row = (
            SpendRow(
                source="modal",
                card=UNATTRIBUTED,
                job="modal-account",
                usd=cached_modal_usd,
                basis=BASIS_BILLED,
                evidence=cached_modal_evidence,
            )
            if cached_modal_rows
            else None
        )

    try:
        ledger = build_window_ledger(
            Path(repo_root),
            window_start,
            window_end,
            database_url=database_url,
            cap_usd=cap_usd,
            extra_roots=extra_roots,
            live_modal_row=effective_modal_row,
            live_modal_notes=tuple(modal_gate_notes),
        )
    except SpendUnverified as exc:
        return unverified_decision(
            reason_code=exc.reason_code,
            message=f"REFUSAL: {exc.reason_code} ({exc})",
            window_start=window_start,
            window_end=window_end,
            candidate_usd=candidate_usd,
            cap_usd=cap_usd,
            in_flight_usd=flight_usd,
            in_flight_count=len(flight),
            in_flight=flight,
            unratable=unratable,
            notes=flight_notes,
        )
    except Exception as exc:
        return unverified_decision(
            reason_code=REASON_CAP_UNVERIFIED,
            message=(
                f"REFUSAL: {REASON_CAP_UNVERIFIED} "
                f"(catalog unreachable: {type(exc).__name__}: {exc}); "
                "settled spend is unknown, never $0"
            ),
            window_start=window_start,
            window_end=window_end,
            candidate_usd=candidate_usd,
            cap_usd=cap_usd,
            in_flight_usd=flight_usd,
            in_flight_count=len(flight),
            in_flight=flight,
            unratable=unratable,
            notes=flight_notes,
        )
    per_basis: dict[str, float] = {}
    for row in ledger.rows:
        per_basis[row.basis] = per_basis.get(row.basis, 0.0) + row.usd
    dynamic_notes: list[str] = list(ledger.notes)
    if modal_fetch_error:
        dynamic_notes.append(f"modal: {modal_fetch_error}")
    dynamic_notes.append(
        "modal: reporting lag: Modal billing reports lag real-time execution; "
        "the current hour is partial and unsettled runtime may not yet be reflected."
    )
    notes = (*dynamic_notes, *flight_notes)
    if unratable:
        names = ", ".join(
            f"{spec.name} ({spec.environment}, queue/{spec.state})" for spec in unratable
        )
        return unverified_decision(
            reason_code=REASON_UNRATABLE_SPEC,
            message=(
                f"REFUSAL: {REASON_UNRATABLE_SPEC} "
                f"({len(unratable)} queued cloud spec(s) with no cost estimate: {names})"
            ),
            window_start=window_start,
            window_end=window_end,
            candidate_usd=candidate_usd,
            cap_usd=cap_usd,
            settled_usd=ledger.total_usd,
            per_source_usd=dict(ledger.per_source),
            per_basis_usd=per_basis,
            in_flight_usd=flight_usd,
            in_flight_count=len(flight),
            in_flight=flight,
            unratable=unratable,
            unresolved_jobs_usd=ledger.unresolved_model_usd,
            notes=notes,
        )
    return decide_launch(
        window_start=window_start,
        window_end=window_end,
        settled_usd=ledger.total_usd,
        per_source_usd=dict(ledger.per_source),
        per_basis_usd=per_basis,
        in_flight_usd=flight_usd,
        in_flight_count=len(flight),
        in_flight=flight,
        unresolved_jobs_usd=ledger.unresolved_model_usd,
        candidate_usd=candidate_usd,
        cap_usd=cap_usd,
        notes=notes,
    )


def decision_to_dict(decision: LaunchDecision) -> dict[str, Any]:
    """JSON-serializable view of a launch decision (``--json`` output)."""
    return {
        "allowed": decision.allowed,
        "reason_code": decision.reason_code,
        "window_start": decision.window_start.isoformat(),
        "window_end": decision.window_end.isoformat(),
        "settled_usd": decision.settled_usd,
        "per_source_usd": dict(sorted(decision.per_source_usd.items())),
        "per_basis_usd": dict(sorted(decision.per_basis_usd.items())),
        "in_flight_usd": decision.in_flight_usd,
        "in_flight_count": decision.in_flight_count,
        "in_flight": [
            {
                "spec_id": item.spec_id,
                "name": item.name,
                "state": item.state,
                "environment": item.environment,
                "est_cost_usd": item.est_cost_usd,
                "cost_limit_usd": item.cost_limit_usd,
                "reservation_usd": item.reservation_usd,
            }
            for item in decision.in_flight
        ],
        "unratable": [
            {
                "spec_id": spec.spec_id,
                "name": spec.name,
                "state": spec.state,
                "environment": spec.environment,
                "reason": spec.reason,
            }
            for spec in decision.unratable
        ],
        "unresolved_jobs_usd": decision.unresolved_jobs_usd,
        "candidate_usd": decision.candidate_usd,
        "committed_usd": decision.committed_usd,
        "cap_usd": decision.cap_usd,
        "cap_description": decision.cap_description,
        "headroom_usd": decision.headroom_usd,
        "notes": list(decision.notes),
    }


def render_decision(decision: LaunchDecision) -> str:
    """Human-readable pre-launch verdict with billed/estimate/ledger split."""
    lines = [
        f"spend check [{decision.window_start.isoformat()}..{decision.window_end.isoformat()})",
        f"settled: ${decision.settled_usd:.4f} "
        f"(billed ${decision.per_basis_usd.get(BASIS_BILLED, 0.0):.4f}, "
        f"estimate ${decision.per_basis_usd.get(BASIS_ESTIMATE, 0.0):.4f}, "
        f"ledger ${decision.per_basis_usd.get(BASIS_LEDGER, 0.0):.4f})",
        f"in-flight: ${decision.in_flight_usd:.4f} "
        f"({decision.in_flight_count} queued spec(s) in running/approved)",
    ]
    for item in decision.in_flight:
        lines.append(
            f"  - {item.name} [{item.state}/{item.environment}]: "
            f"reservation ${item.reservation_usd:.4f} "
            f"(max(cost_limit {item.cost_limit_usd}, est {item.est_cost_usd:.4f}))"
        )
    if decision.unresolved_jobs_usd > 0:
        lines.append(
            f"unresolved-jobs: ${decision.unresolved_jobs_usd:.4f} "
            "(pending provider charges from finished jobs)"
        )
    lines.append(f"candidate: ${decision.candidate_usd:.4f}")
    lines.append(f"cap: {_render_cap(decision.cap_usd, decision.cap_description)}")
    eq_parts = ["settled", "in-flight"]
    if decision.unresolved_jobs_usd > 0:
        eq_parts.append("unresolved-jobs")
    eq_parts.append("candidate")
    lines.append(
        f"committed ({' + '.join(eq_parts)}): ${decision.committed_usd:.4f} "
        f"vs cap ${decision.cap_usd:.2f}: headroom ${decision.headroom_usd:.4f}"
    )
    if decision.allowed:
        lines.append("VERDICT: ALLOWED — launch would stay within cap")
    elif decision.reason_code == REASON_CEILING_EXCEEDED:
        lines.append(
            f"REFUSAL: {REASON_CEILING_EXCEEDED} — committed "
            f"${decision.committed_usd:.4f} exceeds cap ${decision.cap_usd:.2f}"
        )
    else:
        lines.append(f"REFUSAL: {decision.reason_code} — launch is unverified, failing closed")
    for note in decision.notes:
        lines.append(f"note: {note}")
    return "\n".join(lines)


def _session_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field {key!r}")
        result[key] = value
    return result


def _read_session_json(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_session_json_pairs)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"cannot read session spend input {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"session spend input must be a JSON object: {path}")
    return payload, raw


def _session_text(entry: Mapping[str, Any], key: str, label: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} has no nonempty {key}")
    return value


def _session_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite nonnegative number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{label} must be finite") from exc
    if not math.isfinite(number) or number < 0.0:
        raise ValueError(f"{label} must be a finite nonnegative number")
    return number


def _session_timestamp(entry: Mapping[str, Any], key: str, label: str) -> datetime:
    value = _session_text(entry, key, label)
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} has invalid {key}: {value!r}") from exc


def _session_commits_match(first: str, second: str) -> bool:
    if first == second:
        return True
    short, long = (first, second) if len(first) < len(second) else (second, first)
    return (
        re.fullmatch(r"[0-9a-fA-F]{7,64}", short) is not None
        and re.fullmatch(r"[0-9a-fA-F]{7,64}", long) is not None
        and long.startswith(short)
    )


def _validate_spend_session(entry: Any, index: int) -> dict[str, Any]:
    label = f"session {index}"
    if not isinstance(entry, dict):
        raise ValueError(f"{label} must be an object")
    session_id = _session_text(entry, "session_id", label)
    label = f"session {session_id!r}"
    deployment, teardown = entry.get("deployment"), entry.get("teardown")
    if not isinstance(deployment, dict) or not isinstance(teardown, dict):
        raise ValueError(f"{label} requires deployment and teardown objects")
    commit = _session_text(deployment, "commit", label)
    deployed = _session_timestamp(deployment, "time_deployed", label)
    recorded = _session_timestamp(teardown, "recorded_at", label)
    try:
        if deployed > recorded:
            raise ValueError(f"{label} teardown precedes deployment")
    except TypeError as exc:
        raise ValueError(f"{label} deployment/teardown timezones are ambiguous") from exc
    app = _session_text(teardown, "app", label)
    completed = teardown.get("completed_spec_ids")
    if (
        not isinstance(completed, list)
        or not completed
        or any(not isinstance(spec, str) or not spec.strip() for spec in completed)
        or len(set(completed)) != len(completed)
    ):
        raise ValueError(f"{label} requires unique completed_spec_ids")
    rows = entry.get("billing_rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{label} requires billing_rows")
    identities: set[tuple[str, datetime, str]] = set()
    intervals: dict[str, list[tuple[datetime, datetime]]] = {}
    reported: list[tuple[datetime, str]] = []
    costs: list[float] = []
    interval_aware: bool | None = None
    for pos, row in enumerate(rows):
        row_label = f"{label} billing row {pos}"
        if not isinstance(row, dict):
            raise ValueError(f"{row_label} must be an object")
        if row.get("object_id") != session_id or row.get("description") != app:
            raise ValueError(f"{row_label} object_id/description do not bind the session/app")
        for key in ("environment", "resource"):
            if not isinstance(row.get(key), str):
                raise ValueError(f"{row_label} requires string {key}")
        resolution = _session_text(row, "resolution", row_label)
        if resolution not in {"d", "h"}:
            raise ValueError(f"{row_label} requires billing resolution 'd' or 'h'")
        interval = _session_timestamp(row, "interval_start", row_label)
        aware = interval.utcoffset() is not None
        if interval_aware is not None and interval_aware != aware:
            raise ValueError(f"{label} billing interval timezones are ambiguous")
        interval_aware = aware
        identity = (session_id, interval, row["resource"])
        if identity in identities:
            raise ValueError(f"{label} repeats a billing row")
        identities.add(identity)
        try:
            interval_end = interval + timedelta(hours=24 if resolution == "d" else 1)
        except OverflowError as exc:
            raise ValueError(f"{row_label} billing interval end is out of range") from exc
        resource_intervals = intervals.setdefault(row["resource"], [])
        if any(
            interval < prior_end and prior_start < interval_end
            for prior_start, prior_end in resource_intervals
        ):
            raise ValueError(
                f"{row_label} overlaps another billing interval for the same app/resource; "
                "supply non-overlapping billing rows"
            )
        resource_intervals.append((interval, interval_end))
        reported.append((_session_timestamp(row, "reported_at", row_label), row["reported_at"]))
        costs.append(_session_number(row.get("cost_usd"), f"{row_label} cost_usd"))
    raw_members = entry.get("members")
    if not isinstance(raw_members, list) or not raw_members:
        raise ValueError(f"{label} requires members")
    members: list[dict[str, Any]] = []
    job_ids: set[str] = set()
    spec_ids: set[str] = set()
    for pos, member in enumerate(raw_members):
        member_label = f"{label} member {pos}"
        if not isinstance(member, dict):
            raise ValueError(f"{member_label} must be an object")
        fields = {
            key: _session_text(member, key, member_label)
            for key in ("job_id", "job_name", "spec_id", "repository_commit", "lab_metadata_sha256")
        }
        if fields["job_id"] in job_ids or fields["spec_id"] in spec_ids:
            raise ValueError(f"{label} repeats a member job_id or spec_id")
        job_ids.add(fields["job_id"])
        spec_ids.add(fields["spec_id"])
        if re.fullmatch(r"[0-9a-f]{64}", fields["lab_metadata_sha256"]) is None:
            raise ValueError(f"{member_label} requires a lab_metadata_sha256 hex digest")
        if not _session_commits_match(commit, fields["repository_commit"]):
            raise ValueError(f"{member_label} repository_commit does not bind deployment")
        if "daytona_estimate_usd" not in member:
            raise ValueError(f"{member_label} requires daytona_estimate_usd (null if unknown)")
        daytona = member["daytona_estimate_usd"]
        members.append(
            {
                **fields,
                "trial_wall_seconds": _session_number(
                    member.get("trial_wall_seconds"), f"{member_label} trial_wall_seconds"
                ),
                "daytona_estimate_usd": None
                if daytona is None
                else _session_number(daytona, f"{member_label} daytona_estimate_usd"),
            }
        )
    if spec_ids != set(completed):
        raise ValueError(f"{label} members do not exactly match teardown completed_spec_ids")
    longest_commit = max((m["repository_commit"] for m in members), key=len)
    if any(not _session_commits_match(longest_commit, m["repository_commit"]) for m in members):
        raise ValueError(f"{label} deployment revision is ambiguous across members")
    try:
        billed = math.fsum(costs)
        weight = math.fsum(m["trial_wall_seconds"] for m in members)
        billing_reported_at = max(reported, key=lambda item: item[0])[1]
    except OverflowError as exc:
        raise ValueError(f"{label} billed pool or wall time total is not finite") from exc
    except TypeError as exc:
        raise ValueError(f"{label} billing reported_at timezones are ambiguous") from exc
    if weight <= 0.0 or not math.isfinite(weight) or not math.isfinite(billed):
        raise ValueError(f"{label} requires finite totals and positive trial wall time")
    return {
        "session_id": session_id,
        "members": members,
        "billed_modal_usd": billed,
        "session_trial_wall_seconds": weight,
        "billing_reported_at": billing_reported_at,
    }


def session_spend_for_job(job_dir: Path, receipt_path: Path) -> dict[str, Any]:
    """Share a complete billed Modal pool by recorded trial wall seconds.

    This is an accounting policy, not measured per-job GPU usage; warm/idle/
    startup overhead stays in the pool. Add only the receipt's existing Daytona
    estimate, preserving unknowns. Validate every declared session before any
    allocation. Bind the target to its native result ID, spec, exact recorded
    revision and lab-metadata byte hash, never its directory name.
    Native billing resolutions ``d``/``h`` must not overlap for the same app
    object/resource; daily and hourly reports are not additive alternatives.

    Receipt/source bytes are read afresh without writes, queries or pricing.
    ``billing_reported_at`` retains the latest row's original timestamp string.
    Invalid, ambiguous or stale explicit inputs raise ``ValueError``.
    """
    receipt, raw_receipt = _read_session_json(receipt_path)
    if receipt.get("schema") != "evallab.session_spend/v1":
        raise ValueError(f"unsupported session spend receipt schema: {receipt_path}")
    raw_sessions = receipt.get("sessions")
    if not isinstance(raw_sessions, list) or not raw_sessions:
        raise ValueError(f"session spend receipt requires sessions: {receipt_path}")
    sessions = [_validate_spend_session(item, pos) for pos, item in enumerate(raw_sessions)]
    seen_sessions: set[str] = set()
    seen_jobs: set[str] = set()
    for session in sessions:
        if session["session_id"] in seen_sessions:
            raise ValueError("session spend receipt repeats a session_id")
        seen_sessions.add(session["session_id"])
        for member in session["members"]:
            if member["job_id"] in seen_jobs:
                raise ValueError("session spend receipt declares a job in multiple sessions")
            seen_jobs.add(member["job_id"])
    result, _ = _read_session_json(job_dir / "result.json")
    metadata, raw_metadata = _read_session_json(job_dir / "lab-metadata.json")
    job_id = _session_text(result, "id", "job result")
    experiment = metadata.get("experiment")
    spec_id = experiment.get("spec_id") if isinstance(experiment, dict) else None
    spec_path = job_dir / "experiment-spec.json"
    if spec_id is None or spec_path.exists():
        spec, _ = _read_session_json(spec_path)
        saved_spec_id = _session_text(spec, "spec_id", "job experiment-spec")
        if spec_id is not None and spec_id != saved_spec_id:
            raise ValueError("job experiment spec bindings disagree")
        spec_id = saved_spec_id
    if not isinstance(spec_id, str) or not spec_id.strip():
        raise ValueError("job lab-metadata has no usable experiment.spec_id")
    repository = metadata.get("repository")
    if not isinstance(repository, dict):
        raise ValueError("job lab-metadata requires repository.commit")
    commit = _session_text(repository, "commit", "job lab-metadata repository")
    metadata_sha = hashlib.sha256(raw_metadata).hexdigest()
    for session in sessions:
        for member in session["members"]:
            if member["job_id"] != job_id:
                continue
            if (
                member["spec_id"] != spec_id
                or member["repository_commit"] != commit
                or member["lab_metadata_sha256"] != metadata_sha
            ):
                raise ValueError(
                    f"job {job_id!r} has a stale session spend binding; rebuild receipt"
                )
            billed = session["billed_modal_usd"]
            weight = member["trial_wall_seconds"]
            session_weight = session["session_trial_wall_seconds"]
            modal_share = billed * (weight / session_weight)
            daytona = member["daytona_estimate_usd"]
            total = (
                None
                if daytona is None
                else _session_number(modal_share + daytona, f"job {job_id!r} total_usd")
            )
            return {
                "schema": "evallab.session_spend_allocation/v1",
                "session_id": session["session_id"],
                "job_id": job_id,
                "spec_id": spec_id,
                "member_count": len(session["members"]),
                "modal_allocated_usd": modal_share,
                "daytona_estimate_usd": daytona,
                "total_usd": total,
                "billed_modal_usd": billed,
                "trial_wall_seconds": weight,
                "session_trial_wall_seconds": session_weight,
                "allocation_basis": "billed_modal_wall_time_share_plus_daytona_estimate",
                "source_receipt_path": str(receipt_path),
                "source_receipt_sha256": hashlib.sha256(raw_receipt).hexdigest(),
                "billing_reported_at": session["billing_reported_at"],
                "reason": None if daytona is not None else "daytona_estimate_unknown",
            }
    raise ValueError(f"job {job_id!r} is not a member of session spend receipt {receipt_path}")
