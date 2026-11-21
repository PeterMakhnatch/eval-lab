from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

DEFAULT_CAP_USD = 20.0

UNATTRIBUTED = "unattributed"

BASIS_BILLED = "billed"
BASIS_ESTIMATE = "estimate"
BASIS_LEDGER = "ledger"

_JOB_CARD_RE = re.compile(r"(?i)^har-?0*(\d+)-")
_PATH_CARD_RES = (
    re.compile(r"(?i)har-0*(\d+)\b"),
    re.compile(r"(?i)\bhar0*(\d+)\b"),
)

FALLBACK_DAYTONA_RESOURCES: dict[str, tuple[int, int]] = {
    "mimo-v2.6-rl": (2, 8192),
}


SPEND_ATTRIBUTION_POLICY = "policy/spend-attribution.yaml"
SPEND_ATTRIBUTION_SCHEMA = "evallab.spend_attribution/v1"


def parse_dt(value: Any) -> datetime | None:
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
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def window_overlap_seconds(
    start: datetime, end: datetime, window_start: datetime, window_end: datetime
) -> float:
    overlap = (
        min(_coerce_utc(end), _coerce_utc(window_end))
        - max(_coerce_utc(start), _coerce_utc(window_start))
    ).total_seconds()
    return max(0.0, overlap)


def day_to_window(day: date) -> tuple[datetime, datetime]:
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


MODAL_STALE_AFTER_HOURS = 4.0

REASON_STALE_MODAL = "stale_modal_billing"


def _read_task_toml_env(task_path: Any) -> dict[str, Any]:
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


REASON_CEILING_EXCEEDED = "daily_cost_ceiling_exceeded"
REASON_CAP_UNVERIFIED = "daily_cap_unverified"
REASON_UNRATABLE_SPEC = "unratable_cost_spec"

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
