"""Dispatch-side guards from the Baseline v1 post-mortem (HAR-163).

Four small guards, one per failure mode, all enforced on the real tick path:

1. Self-hosted readiness probe (cold Modal endpoint): a minimal
   ``POST /v1/chat/completions`` (``max_tokens=1``) against the resolved
   upstream before every self-hosted-model dispatch. A 503 or
   connection error waits with backoff inside a bounded warm-up window and
   launches on the first 200; an endpoint still cold at the bound defers
   the spec with ``selfhosted_endpoint_not_ready``. Never launches into
   a 503. Every launch re-probes: no cached-ready reuse.
2. Infra-spike stop: a ``proxy_error_spike``/``infra_spike`` alert in any
   job ``watch/alerts.jsonl`` sets the existing queue ``STOP`` fence, so
   no further specs dispatch. Running trials are untouched (``stop()``
   only fences future dispatch). Clear with ``evallab resume``.
3. Daytona memory clamp: the tick plan is clamped up front to
   ``floor((limit * safety - used - pending - reserve) / per_sandbox)``
   from ``DaytonaGuard.observe()``, so admission never refuses an
   over-planned tick. Guard unavailable falls back to one launch.
4. Smoke gate: a batch with more than one model-backed spec dispatches
   one first. Fail-closed: the rest stay approved with
   ``smoke_gate_blocked`` (plus the durable queue ``STOP`` fence) unless
   every smoke trial carries a finite verifier reward with no unexpected
   exception. A canonical ``AGENT_STOP_EXCEPTIONS`` stop still needs its
   finite grade to release; the stop name alone never releases. Missing,
   unreadable, empty, non-finite, or ungraded smoke evidence blocks.
   Gradable smoke lets the rest launch.
   Default-on; ``--no-smoke-gate`` opts out and is recorded.

These guards do not deploy resources. Readiness performs a real, authenticated
minimal chat-completions request against the configured model upstream.
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.database import AGENT_STOP_EXCEPTIONS
from evallab.execution_contracts import (
    MIMO_SELFHOSTED_PROXY_TOKEN,
    MIMO_SELFHOSTED_SECRET_FILE_ENV,
    MIMO_SELFHOSTED_UPSTREAM_ENV,
    is_mimo_selfhosted_model,
    parse_mimo_selfhosted_model,
)

SELFHOSTED_KEY_ENV = "MIMO_SELFHOSTED_API_KEY"

#: Bounded warm-up window: ~10 minutes covers the observed ~4 minute cold
#: start with headroom; configurable per tick.
DEFAULT_SELFHOSTED_WARMUP_SECONDS = 600.0
#: Single probe attempt timeout.
DEFAULT_SELFHOSTED_PROBE_TIMEOUT_SECONDS = 10.0
#: Backoff between probe attempts while the endpoint reports cold.
PROBE_BACKOFF_BASE_SECONDS = 2.0
PROBE_BACKOFF_CAP_SECONDS = 10.0

#: Auto-watch rules that fence the queue (fleet or per-job alerts.jsonl).
SPIKE_RULES = frozenset({"proxy_error_spike", "infra_spike"})

#: Agents without model behavior; never part of a smoke batch.
MODEL_FREE_AGENTS = frozenset({"nop", "oracle"})

#: Conservative fallback when the Daytona guard cannot be read.
DAYTONA_FALLBACK_ALLOWANCE = 1
#: Headroom held back as one whole sandbox.
DAYTONA_RESERVE_SANDBOXES = 1
#: Last-resort per-sandbox memory when the snapshot carries none.
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
    """One upstream attempt. Never carries key material or bodies."""

    ok: bool
    status: int | None
    cold: bool
    detail: str


ProbeFn = Callable[[str, str, str, float], SelfhostedProbeOutcome]


def is_selfhosted_spec(spec: Any) -> bool:
    """Whether this spec needs the self-hosted upstream probe."""
    return is_mimo_selfhosted_model(getattr(spec, "model", None))


def is_model_backed(spec: Any) -> bool:
    """Whether this spec invokes a model (i.e. not a free control)."""
    return str(getattr(spec, "agent", "")) not in MODEL_FREE_AGENTS


def is_daytona_spec(spec: Any) -> bool:
    """Whether this spec plans a Daytona sandbox launch."""
    return getattr(spec, "environment", None) == "daytona"


def resolve_selfhosted_endpoint(environment: Mapping[str, str] | None = None) -> str | None:
    """The configured self-hosted upstream base URL, or ``None``."""
    source = os.environ if environment is None else environment
    raw = source.get(MIMO_SELFHOSTED_UPSTREAM_ENV)
    if raw is None:
        return None
    endpoint = raw.strip().rstrip("/")
    if not endpoint:
        return None
    scheme = endpoint.split("://", 1)[0].lower()
    if scheme not in {"http", "https"}:
        return None
    return endpoint


def resolve_selfhosted_key(environment: Mapping[str, str] | None = None) -> str | None:
    """The upstream API key value, or ``None`` when unavailable.

    Reads ``MIMO_SELFHOSTED_API_KEY`` first (the same source the runner
    materializes), falling back to the owner-only secret file the runner
    also honors. Never logs or returns placeholders.
    """
    source = os.environ if environment is None else environment
    value = source.get(SELFHOSTED_KEY_ENV)
    if value and value != MIMO_SELFHOSTED_PROXY_TOKEN:
        return value
    secret_path = source.get(MIMO_SELFHOSTED_SECRET_FILE_ENV)
    if not secret_path:
        return None
    try:
        from evallab.execution_contracts import read_owner_secret_file

        return read_owner_secret_file(Path(secret_path)) or None
    except Exception:
        return None


def selfhosted_probe_model(spec_model: str | None) -> str:
    """The model name to send upstream: the served (native) id."""
    if not isinstance(spec_model, str) or not spec_model:
        return ""
    try:
        return parse_mimo_selfhosted_model(spec_model)
    except ValueError:
        return spec_model


def probe_once(
    endpoint: str, model: str, key: str, timeout_seconds: float
) -> SelfhostedProbeOutcome:
    """One minimal chat-completions probe; the only network call here."""
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
    """Poll a cold endpoint until 200 or the warm-up bound.

    Returns ``(ready, attempts, rejected)``. Only cold signals
    (503/connection error) wait; any other non-200 refuses immediately
    (``rejected=True``) so a 401 never burns the warm-up window.
    """
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
    """Whether a finished smoke job blocks the rest of its batch.

    Fail-closed: returns ``False`` only on nonempty observed gradable
    evidence — every trial carries a finite verifier reward
    (``results.TrialRecord.primary_reward`` semantics) and no trial
    carries an unexpected exception. Missing, unreadable, empty,
    non-finite, or ungraded evidence blocks, as does any infrastructure
    or wiring exception, even when a reward is present. A canonical
    ``database.AGENT_STOP_EXCEPTIONS`` stop still needs its finite grade
    to release: the stop name alone, without grading, is absence of
    evidence, not evidence of health.
    """
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
    """Max Daytona launches this tick under the memory cap.

    ``floor((limit * safety - used - pending - reserve) / per_sandbox)``
    on the ``memory_gib`` dimension, holding one sandbox in reserve.
    """
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
    """Best-effort ``watch/alerts.jsonl`` rows matching the spike rules."""
    path = Path(job_dir) / "watch" / "alerts.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    matches: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("rule") in SPIKE_RULES:
            matches.append(row)
    return matches


def scan_spike_alerts(runs_roots: Iterable[Path | str]) -> list[dict[str, Any]]:
    """All spike-rule alerts under ``<runs>/<job>/watch/alerts.jsonl``."""
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
            for row in read_spike_alerts(child):
                found.append({"job_dir": str(child), **row})
    return found
