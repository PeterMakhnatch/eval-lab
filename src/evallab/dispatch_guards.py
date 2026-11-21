from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.database import AGENT_STOP_EXCEPTIONS
from evallab.execution_contracts import (
    BREAKER_SELFHOSTED_KEY_ENV,
    BREAKER_SELFHOSTED_SECRET_FILE_ENV,
    BREAKER_SELFHOSTED_UPSTREAM_ENV,
    MIMO_SELFHOSTED_PROXY_TOKEN,
    MIMO_SELFHOSTED_SECRET_FILE_ENV,
    MIMO_SELFHOSTED_UPSTREAM_ENV,
    breaker_selfhosted_endpoint,
    is_breaker_selfhosted_model,
    is_mimo_selfhosted_model,
    parse_breaker_selfhosted_model,
    parse_mimo_selfhosted_model,
)

SELFHOSTED_KEY_ENV = "MIMO_SELFHOSTED_API_KEY"

PROBE_BACKOFF_BASE_SECONDS = 2.0
PROBE_BACKOFF_CAP_SECONDS = 10.0

SPIKE_RULES = frozenset({"proxy_error_spike", "infra_spike"})

MODEL_FREE_AGENTS = frozenset({"nop", "oracle"})

DAYTONA_FALLBACK_ALLOWANCE = 1
DAYTONA_RESERVE_SANDBOXES = 1
DAYTONA_FALLBACK_PER_SANDBOX_GIB = 8.0

SELFHOSTED_REASON_NOT_READY = "selfhosted_endpoint_not_ready"
SELFHOSTED_REASON_UNCONFIGURED = "selfhosted_endpoint_unconfigured"
SELFHOSTED_REASON_KEY_MISSING = "selfhosted_key_missing"
SELFHOSTED_REASON_REJECTED = "selfhosted_probe_rejected"
SELFHOSTED_REASON_READY = "selfhosted_probe_ready"
DAYTONA_REASON_CLAMPED = "daytona_memory_clamped"
DAYTONA_REASON_GUARD_UNAVAILABLE = "daytona_guard_unavailable"
SMOKE_REASON_BLOCKED = "smoke_gate_blocked"
SMOKE_REASON_DISABLED = "smoke_gate_disabled"
INFRA_STOP_EVENT = "dispatch_stopped"


@dataclass(frozen=True)
class SelfhostedProbeOutcome:

    ok: bool
    status: int | None
    cold: bool
    detail: str


ProbeFn = Callable[[str, str, str, float], SelfhostedProbeOutcome]


def is_selfhosted_spec(spec: Any) -> bool:
    model = getattr(spec, "model", None)
    return is_mimo_selfhosted_model(model) or is_breaker_selfhosted_model(model)


def is_model_backed(spec: Any) -> bool:
    return str(getattr(spec, "agent", "")) not in MODEL_FREE_AGENTS


def is_daytona_spec(spec: Any) -> bool:
    return getattr(spec, "environment", None) == "daytona"


def resolve_selfhosted_endpoint(
    environment: Mapping[str, str] | None = None, *, model: str | None = None
) -> str | None:
    source = os.environ if environment is None else environment
    breaker = is_breaker_selfhosted_model(model)
    raw = source.get(BREAKER_SELFHOSTED_UPSTREAM_ENV if breaker else MIMO_SELFHOSTED_UPSTREAM_ENV)
    if raw is None:
        return None
    endpoint = raw.strip().rstrip("/")
    if not endpoint:
        return None
    if breaker:
        try:
            return breaker_selfhosted_endpoint(endpoint)
        except ValueError:
            return None
    scheme = endpoint.split("://", 1)[0].lower()
    if scheme not in {"http", "https"}:
        return None
    return endpoint


def resolve_selfhosted_key(
    environment: Mapping[str, str] | None = None, *, model: str | None = None
) -> str | None:
    source = os.environ if environment is None else environment
    breaker = is_breaker_selfhosted_model(model)
    value = source.get(BREAKER_SELFHOSTED_KEY_ENV if breaker else SELFHOSTED_KEY_ENV)
    if value and value != MIMO_SELFHOSTED_PROXY_TOKEN:
        return value
    secret_path = source.get(
        BREAKER_SELFHOSTED_SECRET_FILE_ENV if breaker else MIMO_SELFHOSTED_SECRET_FILE_ENV
    )
    if not secret_path:
        return None
    try:
        from evallab.execution_contracts import read_owner_secret_file

        return read_owner_secret_file(Path(secret_path)) or None
    except Exception:
        return None


def selfhosted_probe_model(spec_model: str | None) -> str:
    if not isinstance(spec_model, str) or not spec_model:
        return ""
    try:
        if is_breaker_selfhosted_model(spec_model):
            return parse_breaker_selfhosted_model(spec_model)
        return parse_mimo_selfhosted_model(spec_model)
    except ValueError:
        return spec_model


def probe_once(
    endpoint: str, model: str, key: str, timeout_seconds: float
) -> SelfhostedProbeOutcome:
    url = endpoint.rstrip("/") + "/v1/chat/completions"
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "ready"}],
            "max_tokens": 1,
        }
    ).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status = int(getattr(response, "status", 200))
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        exc.close()
        if status == 503:
            return SelfhostedProbeOutcome(
                ok=False, status=status, cold=True, detail="upstream reports 503"
            )
        return SelfhostedProbeOutcome(
            ok=False, status=status, cold=False, detail=f"upstream returned {status}"
        )
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return SelfhostedProbeOutcome(
            ok=False, status=None, cold=True, detail=f"connection failed ({type(exc).__name__})"
        )
    if status == 200:
        return SelfhostedProbeOutcome(ok=True, status=status, cold=False, detail="ready")
    if status == 503:
        return SelfhostedProbeOutcome(
            ok=False, status=status, cold=True, detail="upstream reports 503"
        )
    return SelfhostedProbeOutcome(
        ok=False, status=status, cold=False, detail=f"upstream returned {status}"
    )


def wait_for_selfhosted_ready(
    endpoint: str,
    model: str,
    key: str,
    *,
    warmup_seconds: float,
    timeout_seconds: float,
    probe_fn: ProbeFn | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> tuple[bool, int, bool]:
    probe = probe_fn or probe_once
    deadline = clock() + max(float(warmup_seconds), 0.0)
    backoff = PROBE_BACKOFF_BASE_SECONDS
    attempts = 0
    while True:
        outcome = probe(endpoint, model, key, timeout_seconds)
        attempts += 1
        if outcome.ok:
            return True, attempts, False
        if not outcome.cold:
            return False, attempts, True
        now = clock()
        if now >= deadline:
            return False, attempts, False
        delay = min(backoff, deadline - now)
        backoff = min(backoff * 2.0, PROBE_BACKOFF_CAP_SECONDS)
        if delay > 0:
            sleeper(delay)


def _finite_reward(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def smoke_trial_blocks(job: Any) -> tuple[bool, str]:
    if job is None:
        return True, "smoke job evidence unavailable"
    try:
        trials = list(getattr(job, "trials", ()) or ())
    except Exception:
        return True, "smoke job evidence unreadable"
    if not trials:
        return True, "smoke job has no trials"
    for trial in trials:
        try:
            name = getattr(trial, "name", None) or getattr(trial, "path", "?")
            reward = getattr(trial, "primary_reward", None)
            result = getattr(trial, "result", None)
        except Exception:
            return True, "smoke trial evidence unreadable"
        if not _finite_reward(reward):
            return True, f"smoke trial {name} ungraded (no finite verifier reward)"
        if not isinstance(result, dict):
            return True, f"smoke trial {name} has invalid result evidence"
        exception_info = result.get("exception_info")
        if exception_info is not None and not isinstance(exception_info, dict):
            return True, f"smoke trial {name} has invalid exception evidence"
        exception_type = (
            exception_info.get("exception_type") if isinstance(exception_info, dict) else None
        )
        if not exception_type:
            continue
        if str(exception_type) not in AGENT_STOP_EXCEPTIONS:
            return True, (
                f"smoke trial {name} carries an unexpected exception "
                f"({exception_type}) despite its grade"
            )
    return False, "smoke trial gradable"


def _number_or(value: Any, fallback: float) -> float:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return fallback


def daytona_tick_allowance(snapshot: Mapping[str, Any]) -> tuple[int, str]:
    limits = snapshot.get("limits") if isinstance(snapshot, Mapping) else {}
    used = snapshot.get("used") if isinstance(snapshot, Mapping) else {}
    pending = snapshot.get("pending") if isinstance(snapshot, Mapping) else {}
    per_sandbox = snapshot.get("per_sandbox_limits") if isinstance(snapshot, Mapping) else {}
    safety = snapshot.get("safety_fraction", 0.8)
    limit = _number_or(limits.get("memory_gib") if isinstance(limits, Mapping) else None, 0.0)
    safety_fraction = _number_or(safety, 0.8)
    used_gib = _number_or(used.get("memory_gib") if isinstance(used, Mapping) else None, 0.0)
    pending_gib = _number_or(
        pending.get("memory_gib") if isinstance(pending, Mapping) else None, 0.0
    )
    per_mem = _number_or(
        per_sandbox.get("memory_gib") if isinstance(per_sandbox, Mapping) else None,
        DAYTONA_FALLBACK_PER_SANDBOX_GIB,
    )
    if limit <= 0 or per_mem <= 0:
        return 0, f"daytona memory cap unreadable (limit={limit} per_sandbox={per_mem})"
    reserve_gib = DAYTONA_RESERVE_SANDBOXES * per_mem
    headroom = limit * safety_fraction - used_gib - pending_gib - reserve_gib
    allowance = max(int(math.floor(headroom / per_mem)), 0)
    detail = (
        f"daytona memory: limit {limit:g}GiB x safety {safety_fraction:g} "
        f"- used {used_gib:g} - pending {pending_gib:g} "
        f"- reserve {reserve_gib:g} = {headroom:g}GiB headroom "
        f"-> {allowance} launch(es) at {per_mem:g}GiB/sandbox"
    )
    return allowance, detail


def read_spike_alerts(job_dir: Path) -> list[dict[str, Any]]:
    path = Path(job_dir) / "watch" / "alerts.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    open_fleet_rules: set[str] | None = None
    status_path = Path(job_dir) / "watch" / "status.json"
    if status_path.is_file():
        try:
            status_doc = json.loads(status_path.read_text(encoding="utf-8"))
            if isinstance(status_doc, dict) and isinstance(status_doc.get("fleet_alerts"), list):
                open_fleet_rules = {
                    str(a.get("rule"))
                    for a in status_doc["fleet_alerts"]
                    if isinstance(a, dict) and a.get("rule")
                }
        except (OSError, ValueError):
            open_fleet_rules = None
    matches: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("rule") in SPIKE_RULES:
            if (
                row.get("rule") == "proxy_error_spike"
                and open_fleet_rules is not None
                and "proxy_error_spike" not in open_fleet_rules
            ):
                continue
            matches.append(row)
    return matches


def parse_alert_time(row: Mapping[str, Any]) -> datetime | None:
    raw = row.get("first_seen")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def alert_predates_resume(row: Mapping[str, Any], resume_time: datetime | None) -> bool:
    if resume_time is None:
        return False
    seen = parse_alert_time(row)
    return seen is not None and seen < resume_time


def read_unacked_spike_alerts(
    job_dir: Path, *, resume_time: datetime | None = None
) -> list[dict[str, Any]]:
    from evallab import auto_watch as _auto_watch

    alerts = read_spike_alerts(job_dir)
    if not alerts:
        return []
    acks = _auto_watch.read_watch_acks(job_dir)
    live: list[dict[str, Any]] = []
    for alert in alerts:
        if alert_predates_resume(alert, resume_time):
            continue
        if acks and _auto_watch.covering_ack(alert, acks) is not None:
            continue
        live.append(alert)
    return live


def job_launched_since(job_dir: Path, resume_time: datetime) -> bool:
    alerts_path = Path(job_dir) / "watch" / "alerts.jsonl"
    if alerts_path.is_file():
        try:
            return alerts_path.stat().st_mtime >= resume_time.timestamp()
        except OSError:
            return False
    try:
        return Path(job_dir).stat().st_mtime >= resume_time.timestamp()
    except OSError:
        return False


def scan_spike_alerts(
    runs_roots: Iterable[Path | str],
    *,
    resume_time: datetime | None = None,
    running_dirs: Iterable[Path | str] = (),
) -> list[dict[str, Any]]:
    running = {str(Path(item)) for item in running_dirs}
    running_names = {Path(item).name for item in running_dirs}
    found: list[dict[str, Any]] = []
    for raw_root in runs_roots:
        root = Path(raw_root)
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir():
                continue
            if (
                resume_time is not None
                and str(child) not in running
                and child.name not in running_names
                and not job_launched_since(child, resume_time)
            ):
                continue
            for row in read_unacked_spike_alerts(child, resume_time=resume_time):
                found.append({"job_dir": str(child), **row})
    return found
