"""UTC-day lab spend ledger (HAR-122) and billed-session allocation (HAR-131).

One ledger per UTC day sums ALL lab spend broken down by source (Modal
billed rows, Daytona sandbox estimates, model API ledgers), by card
(HAR-NNN) and by job, with the grand total against the $20/day cap.

Source rules (each row carries ``basis`` so billed figures are never
mixed with estimates):

- ``modal`` (basis ``billed``): the HAR-107 billing-reconcile rows in
  catalog table ``modal_billing_rows``. Modal bills the account, not
  jobs, so this is one row per day under the ``unattributed`` card.
  When the table holds no rows for the day the ledger says so; this
  module never fetches billing itself (the reconcile path owns that).
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

Card attribution derives from the job-name prefix (``har81-...`` ->
``HAR-81``) or the ledger path (``har111/...`` -> ``HAR-111``);
anything else lands in ``unattributed`` and is never guessed.
Model-ledger dollars attribute to the day the job finished;
``spend.jsonl`` rows attribute to the day of their own timestamp.

``session_spend_for_job`` reads an explicit post-session receipt, binds its
complete declared membership to teardown, and shares billed Modal dollars by
recorded trial wall seconds. It adds the supplied existing Daytona estimate;
an unknown estimate leaves the combined total unknown. This allocation is an
accounting policy, not measured per-job GPU usage or a new pricing model.

This module never touches the runner or queue, never spends, and only
reads the catalog plus ledger files.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

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


def day_overlap_seconds(start: datetime, end: datetime, day: date) -> float:
    """Seconds of ``[start, end)`` falling inside the UTC ``day``.

    Intervals crossing midnight split across the two days; callers sum
    the per-day slices so the day portions always add up to the whole.
    """
    day_start = datetime.combine(day, time.min, tzinfo=UTC)
    day_end = day_start + _one_day()
    overlap = (min(end, day_end) - max(start, day_start)).total_seconds()
    return max(0.0, overlap)


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


def model_usd_from_provider_usage(provider_usage: Any) -> tuple[float | None, str]:
    """Settled model dollars for one per-job proxy ledger.

    Returns ``(usd, reason)``; ``usd`` is None (with a reason, never a
    silent 0) when the ledger is missing, unreadable, or the
    zero-priced self-hosted route ($0 at the proxy, billed via Modal).
    Only settled ``used`` cost counts; unresolved reservations never do.
    """
    from evallab.ledger import build_cost_block

    block = build_cost_block(provider_usage if isinstance(provider_usage, Mapping) else None)
    cost = block.get("cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        return float(cost), ""
    return None, str(block.get("reason") or "no settled model cost")


def aggregate_spend_jsonl_calls(
    calls: Iterable[Mapping[str, Any]], day: date
) -> tuple[float, int, int]:
    """Sum ``cost_usd`` for calls whose ``ts`` falls on ``day``.

    Returns ``(usd, in_day, out_of_day)``; rows without a parsable
    timestamp never contribute dollars.
    """
    total = 0.0
    in_day = 0
    out_of_day = 0
    for call in calls:
        stamp = parse_dt(call.get("ts"))
        if stamp is None or stamp.date() != day:
            out_of_day += 1
            continue
        cost = call.get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            total += float(cost)
            in_day += 1
        else:
            out_of_day += 1
    return total, in_day, out_of_day


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


def summarize_day(
    day: date,
    rows: Iterable[SpendRow],
    *,
    cap_usd: float = DEFAULT_CAP_USD,
    notes: Iterable[str] = (),
) -> DayLedger:
    """Build the day ledger: per-source/card totals plus cap arithmetic."""
    ordered = tuple(rows)
    per_source: dict[str, float] = {}
    per_card: dict[str, float] = {}
    for row in ordered:
        per_source[row.source] = per_source.get(row.source, 0.0) + row.usd
        per_card[row.card] = per_card.get(row.card, 0.0) + row.usd
    total = sum(row.usd for row in ordered)
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
    )


def ledger_to_dict(ledger: DayLedger) -> dict[str, Any]:
    """JSON-serializable view of a day ledger (``--json`` output)."""
    return {
        "day": ledger.day.isoformat(),
        "cap_usd": ledger.cap_usd,
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
    lines = [
        f"spend day {ledger.day.isoformat()} (cap ${ledger.cap_usd:.2f})",
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
    lines.append(f"grand total ${ledger.total_usd:.4f} vs cap ${ledger.cap_usd:.2f}: {verdict}")
    for note in ledger.notes:
        lines.append(f"note: {note}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Catalog + filesystem readers (read-only; never spend, never write).
# ---------------------------------------------------------------------------


def query_modal_day(database_url: str, day: date) -> tuple[SpendRow | None, str]:
    """Billed Modal row for one day, or (None, note) when the table is empty."""
    import psycopg

    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            """
            SELECT coalesce(sum(cost_usd), 0), count(*), max(reported_at)
            FROM modal_billing_rows
            WHERE (interval_start AT TIME ZONE 'UTC')::date = %s
            """,
            (day.isoformat(),),
        ).fetchone()
    total, count, reported_at = row if row is not None else (0.0, 0, None)
    if int(count) == 0:
        return None, f"modal: no billing rows for {day.isoformat()} in modal_billing_rows"
    return (
        SpendRow(
            source="modal",
            card=UNATTRIBUTED,
            job="modal-account",
            usd=float(total),
            basis=BASIS_BILLED,
            evidence=f"catalog:modal_billing_rows:{int(count)} rows",
        ),
        f"modal: billed ${float(total):.4f} across {int(count)} rows"
        + (f" (last reported {reported_at})" if reported_at else ""),
    )


def query_model_job_rows(database_url: str, day: date) -> tuple[list[SpendRow], list[str]]:
    """Settled per-job model dollars for jobs finished on ``day``.

    Jobs are attributed whole to their finish day. Ledgers with no
    settled dollars (no calls, unreadable, or the zero-priced
    self-hosted route) contribute no row and are counted in the notes.
    """
    import psycopg

    with psycopg.connect(database_url) as connection:
        rows = connection.execute(
            """
            SELECT job_name, evidence_path, id, finished_at, lab_metadata
            FROM jobs
            WHERE substr(finished_at, 1, 10) = %s
            """,
            (day.isoformat(),),
        ).fetchall()
    spend_rows: list[SpendRow] = []
    without_settled = 0
    selfhosted_zero = 0
    unfinished = 0
    for job_name, evidence_path, job_id, finished_at, lab_metadata in rows:
        if not isinstance(finished_at, str) or not finished_at:
            unfinished += 1
            continue
        provider_usage = (
            lab_metadata.get("provider_usage") if isinstance(lab_metadata, dict) else None
        )
        usd, reason = model_usd_from_provider_usage(provider_usage)
        if usd is None:
            without_settled += 1
            if "self-hosted" in reason:
                selfhosted_zero += 1
            continue
        spend_rows.append(
            SpendRow(
                source="model",
                card=card_for_job(job_name if isinstance(job_name, str) else None),
                job=job_name if isinstance(job_name, str) else str(job_id),
                usd=usd,
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
    if unfinished:
        notes.append(f"model: {unfinished} jobs lack finished_at; unattributable")
    return spend_rows, notes


def _spend_jsonl_files(roots: Iterable[Path]) -> list[Path]:
    """Every ``spend.jsonl`` under the given roots (read-only glob)."""
    files: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        files.extend(sorted(root.rglob("spend.jsonl")))
    return files


def collect_spend_jsonl_rows(
    repo_root: Path, day: date, *, extra_roots: Iterable[Path] = ()
) -> tuple[list[SpendRow], list[str]]:
    """Aggregate experiment ``spend.jsonl`` ledgers for ``day``.

    Ledgers are discovered under the repo root and (read-only) sibling
    worktree checkouts; byte-identical rows shared across checkouts
    count once. One row per ledger file keeps the ledger compact.
    """
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
        total, in_day, _ = aggregate_spend_jsonl_calls(calls, day)
        try:
            rel = path.relative_to(repo_root)
            evidence = str(rel)
        except ValueError:
            evidence = str(path)
        if in_day:
            spend_rows.append(
                SpendRow(
                    source="model",
                    card=card_for_ledger_path(path),
                    job=path.parent.name,
                    usd=total,
                    basis=BASIS_LEDGER,
                    evidence=f"{evidence}:{in_day} unique rows",
                )
            )
        if bad:
            notes.append(f"model: {bad} unparsable lines in {evidence}")
    notes.append(
        f"model: {len(files)} spend.jsonl ledgers scanned, "
        f"{len(spend_rows)} contribute to {day.isoformat()}, "
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


def query_daytona_rows(database_url: str, day: date) -> tuple[list[SpendRow], list[str]]:
    """Daytona sandbox estimates: trial wall time on ``day`` x rate card.

    Each trial contributes its overlap with the UTC day (midnight
    crossings split). Per-job rows aggregate trial slices; every dollar
    here is basis ``estimate`` from the Daytona list-price card.
    """
    import psycopg

    from evallab.task_qualification import estimate_cost_usd

    with psycopg.connect(database_url) as connection:
        trials = connection.execute(
            """
            SELECT t.trial_name, t.task_name, t.started_at, t.finished_at,
                   t.duration_seconds, t.raw_config, t.raw_lock,
                   j.job_name, j.evidence_path, j.raw_config
            FROM trials t JOIN jobs j ON j.id = t.job_id
            WHERE t.started_at IS NOT NULL AND t.finished_at IS NOT NULL
            """
        ).fetchall()
    per_job_usd: dict[str, float] = {}
    per_job_meta: dict[str, dict[str, Any]] = {}
    trials_in_day = 0
    skipped_time = 0
    skipped_backend = 0
    unrated = 0
    measured = 0
    fallback = 0
    for (
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
    ) in trials:
        started = parse_dt(started_raw)
        finished = parse_dt(finished_raw)
        if started is None or finished is None or finished <= started:
            skipped_time += 1
            continue
        portion = day_overlap_seconds(started, finished, day)
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
            # Scale catalog duration to the day slice when the trial
            # spans midnight: the slice keeps its share of wall time.
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
        trials_in_day += 1
        key = job_name if isinstance(job_name, str) else str(trial_name)
        per_job_usd[key] = per_job_usd.get(key, 0.0) + float(usd)
        meta = per_job_meta.setdefault(
            key,
            {
                "card": card_for_job(job_name if isinstance(job_name, str) else None),
                "evidence": evidence_path
                if isinstance(evidence_path, str) and evidence_path
                else key,
                "trials": 0,
            },
        )
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
    notes = [
        f"daytona: {trials_in_day} trial slices across {len(spend_rows)} jobs "
        f"(rate measured {measured}, family fallback {fallback}); "
        f"{unrated} slices unratable, {skipped_backend} non-daytona, "
        f"{skipped_time} missing time"
    ]
    return spend_rows, notes


def sibling_worktree_roots(repo_root: Path) -> list[Path]:
    """Other ``.worktrees/*`` checkouts beside this one (read-only)."""
    roots: list[Path] = []
    worktrees = repo_root / ".worktrees"
    try:
        candidates = sorted(p for p in worktrees.iterdir() if p.is_dir())
    except OSError:
        return []
    for candidate in candidates:
        # Skip our own checkout: same directory, not a sibling.
        try:
            if candidate.resolve() == repo_root.resolve():
                continue
        except OSError:
            continue
        roots.append(candidate)
    # Primary checkout may also hold ledgers outside any worktree root.
    primary = worktrees.parent
    if primary.is_dir() and primary not in roots:
        try:
            if primary.resolve() != repo_root.resolve():
                roots.append(primary)
        except OSError:
            pass
    return roots


def build_day_ledger(
    repo_root: Path,
    day: date,
    *,
    database_url: str,
    cap_usd: float = DEFAULT_CAP_USD,
    extra_roots: Iterable[Path] = (),
) -> DayLedger:
    """Assemble the full day ledger from all three sources (read-only)."""
    rows: list[SpendRow] = []
    notes: list[str] = []
    modal_row, modal_note = query_modal_day(database_url, day)
    notes.append(modal_note)
    if modal_row is not None:
        rows.append(modal_row)
    model_job_rows, model_notes = query_model_job_rows(database_url, day)
    rows.extend(model_job_rows)
    notes.extend(model_notes)
    jsonl_rows, jsonl_notes = collect_spend_jsonl_rows(repo_root, day, extra_roots=extra_roots)
    rows.extend(jsonl_rows)
    notes.extend(jsonl_notes)
    daytona_rows, daytona_notes = query_daytona_rows(database_url, day)
    rows.extend(daytona_rows)
    notes.extend(daytona_notes)
    return summarize_day(day, rows, cap_usd=cap_usd, notes=notes)


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
        reported.append(
            (_session_timestamp(row, "reported_at", row_label), row["reported_at"])
        )
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
        members.append({
            **fields,
            "trial_wall_seconds": _session_number(
                member.get("trial_wall_seconds"), f"{member_label} trial_wall_seconds"
            ),
            "daytona_estimate_usd": None if daytona is None else _session_number(
                daytona, f"{member_label} daytona_estimate_usd"
            ),
        })
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
                raise ValueError(f"job {job_id!r} has a stale session spend binding; rebuild receipt")
            billed = session["billed_modal_usd"]
            weight = member["trial_wall_seconds"]
            session_weight = session["session_trial_wall_seconds"]
            modal_share = billed * (weight / session_weight)
            daytona = member["daytona_estimate_usd"]
            total = None if daytona is None else _session_number(
                modal_share + daytona, f"job {job_id!r} total_usd"
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
