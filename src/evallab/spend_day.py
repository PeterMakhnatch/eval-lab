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

Card attribution is explicit-only and fail-closed: the declared
``policy/spend-attribution.yaml`` exact-name bindings or the catalog
``lab_metadata`` ``experiment.linear_card`` (copied from the submitting
``ExperimentSpec.linear_card``) wins, the job-name prefix (``har81-...``
-> ``HAR-81``) or the ledger path (``har111/...`` -> ``HAR-111``) covers
the rest; disagreement or a malformed explicit card fails closed to
``unattributed`` with a conflict note and is never guessed. Task,
model, harness, and app names are never attribution: the 2026-10-01
``ovn-g5-*`` runs attribute only through their declared G5-RUN.md
cell binding, and jobs with no binding at all stay ``unattributed``
(republishing with ``publication_card`` binds the results home without
rewriting raw records). Billed Daytona dollars are unavailable from the
provider API, so Daytona rows stay estimates.

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

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
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














#: Declared attribution policy: exact job/app names bound to a Linear card.
#: Read-only; an absent file means no declared bindings.
SPEND_ATTRIBUTION_POLICY = "policy/spend-attribution.yaml"
SPEND_ATTRIBUTION_SCHEMA = "evallab.spend_attribution/v1"










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






def _one_day() -> Any:
    from datetime import timedelta

    return timedelta(days=1)














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
















# ---------------------------------------------------------------------------
# Catalog + filesystem readers (read-only; never spend, never write).
# ---------------------------------------------------------------------------


#: Modal rows lag real usage: warn when the latest fetched billing hour
#: is older than this versus now (the catalog may undercount tonight).
MODAL_STALE_AFTER_HOURS = 4.0

#: Refusal when Modal billing rows are stale and launch is not allowed despite staleness.
REASON_STALE_MODAL = "stale_modal_billing"












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
