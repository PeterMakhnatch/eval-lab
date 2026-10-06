"""Per-job live-watch supervision for queue dispatch (HAR-162).

Dispatch starts a watch for every agent job and stops it when the job
reaches a terminal state. The watch itself is strictly read-only toward
the run: it only reads trial trajectories, the proxy ledger and the Harbor
hook journal (``<job>/watch/hooks.jsonl``, written by
:mod:`evallab.harbor_watch_hooks` inside the Harbor process) and writes
under ``<job>/watch/`` -- never into trial directories, never a signal to
the Harbor process, never anything that ends the run. A lifecycle hook
record triggers a pass immediately instead of at the next interval.

Model-free agents (``nop``, ``oracle``) are skipped: they have no model
behavior to watch. A watcher that fails to start or crashes never fails
the job; the failure is recorded as a ``watcher_error`` alert in the
same ``alerts.jsonl`` the job page renders.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.harbor_watch_hooks import HOOK_JOURNAL

WATCH_DIRNAME = "watch"
ALERTS_FILENAME = "alerts.jsonl"
ACKS_FILENAME = "acks.jsonl"
NOTIFIED_FILENAME = ".lin_notified.json"
WATCHER_ERROR_RULE = "watcher_error"

#: Agents without model behavior; dispatch never watches them.
MODEL_FREE_AGENTS = frozenset({"nop", "oracle"})

#: Alert rules eligible for ``lin comment`` notification. Stall, infra
#: spike, and spend are the operator-actionable subset; repetition,
#: budget burn, and the integrity rules stay on the job page only.
CRITICAL_RULES = frozenset(
    {
        "stalled",
        "spend",
        "spend_cap_warning",
        "spend_cap_exceeded",
        "infra_error",
        "infra_spike",
        "proxy_errors",
        "proxy_error_spike",
    }
)

DEFAULT_INTERVAL_SECONDS = 60.0
HOOK_POLL_SECONDS = 2.0
_STOP_JOIN_TIMEOUT_SECONDS = 30.0

_active_lock = threading.Lock()
_active_handles: set[int] = set()


def should_watch(agent: str | None) -> bool:
    """Whether dispatch attaches a watch for ``agent`` (never model-free)."""
    return str(agent or "") not in MODEL_FREE_AGENTS


def active_watch_count() -> int:
    """Live supervised watches (test seam for the exits-on-terminal proof)."""
    with _active_lock:
        return len(_active_handles)


def watch_dir_for(job_dir: Path) -> Path:
    return Path(job_dir) / WATCH_DIRNAME


def _track(handle: WatchHandle) -> None:
    with _active_lock:
        _active_handles.add(id(handle))


def _untrack(handle: WatchHandle) -> None:
    with _active_lock:
        _active_handles.discard(id(handle))


@dataclass
class WatchHandle:
    """One supervised per-job watch (background thread plus final pass)."""

    job_dir: Path
    out_dir: Path
    agent: str | None = None
    linear_card: str | None = None
    notify_enabled: bool = False
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    run_watch_fn: Callable[..., Any] | None = None
    notify_runner: Callable[..., Any] | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    start_error: str | None = None
    stopped: bool = False


def _default_run_watch(*, runs_dirs: list[Path], out_dir: Path) -> dict[str, Any]:
    # Imported here so tests can stub ``evallab.live_watch.run_watch``
    # and so this module stays cheap to import from the executor.
    from evallab.laminar import exporter_from_env
    from evallab.live_watch import WatchThresholds, run_watch

    # Laminar export is on whenever LMNR_PROJECT_API_KEY is set (EVALLAB_LAMINAR=off disables).
    return run_watch(
        runs_dirs=runs_dirs,
        out_dir=out_dir,
        thresholds=WatchThresholds(),
        laminar=exporter_from_env(out_dir),
    )


def start_auto_watch(
    *,
    jobs_dir: Path | str,
    job_name: str,
    agent: str | None,
    linear_card: str | None = None,
    notify_enabled: bool = False,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    run_watch_fn: Callable[..., Any] | None = None,
    notify_runner: Callable[..., Any] | None = None,
) -> WatchHandle | None:
    """Start the per-job watch; ``None`` when skipped (model-free agents).

    Never raises: a start failure returns a handle carrying
    ``start_error`` so :func:`stop_auto_watch` can record it and still
    run the final pass.
    """
    if not should_watch(agent):
        return None
    job_dir = Path(jobs_dir) / job_name
    handle = WatchHandle(
        job_dir=job_dir,
        out_dir=watch_dir_for(job_dir),
        agent=agent,
        linear_card=linear_card,
        notify_enabled=bool(notify_enabled),
        interval_seconds=interval_seconds,
        run_watch_fn=run_watch_fn,
        notify_runner=notify_runner,
    )
    _track(handle)
    try:
        thread = threading.Thread(
            target=_supervise,
            args=(handle,),
            name=f"auto-watch-{job_name}",
            daemon=True,
        )
        thread.start()
        handle.thread = thread
    except Exception as exc:  # noqa: BLE001 -- start failure must not fail dispatch
        handle.start_error = f"{type(exc).__name__}: {exc}"
    return handle


def start_for_request(
    request: Any,
    *,
    enabled: bool = True,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    run_watch_fn: Callable[..., Any] | None = None,
    notify_runner: Callable[..., Any] | None = None,
) -> WatchHandle | None:
    """Attach to a queue :class:`RunRequest` (duck-typed; never raises)."""
    try:
        if not enabled:
            return None
        agent = getattr(request, "agent", None)
        if not should_watch(agent):
            return None
        provenance = getattr(request, "provenance", None)
        experiment_spec = getattr(request, "experiment_spec", None)
        return start_auto_watch(
            jobs_dir=request.jobs_dir,
            job_name=request.name,
            agent=agent,
            linear_card=getattr(provenance, "linear_card", None),
            notify_enabled=bool(getattr(experiment_spec, "watch_notify_lin", False)),
            interval_seconds=interval_seconds,
            run_watch_fn=run_watch_fn,
            notify_runner=notify_runner,
        )
    except Exception as exc:  # noqa: BLE001 -- watch attach never blocks dispatch
        try:
            fallback = WatchHandle(
                job_dir=Path(str(getattr(request, "jobs_dir", ".")))
                / str(getattr(request, "name", "unknown")),
                out_dir=watch_dir_for(
                    Path(str(getattr(request, "jobs_dir", ".")))
                    / str(getattr(request, "name", "unknown"))
                ),
            )
            fallback.start_error = f"{type(exc).__name__}: {exc}"
            _track(fallback)
            return fallback
        except Exception:
            return None


def stop_auto_watch(
    handle: WatchHandle | None,
    *,
    run_watch_fn: Callable[..., Any] | None = None,
    notify_runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Stop the watch, run the final pass, notify; never raises.

    Covers every terminal path (success, failure, cancel): the caller
    holds this in a ``finally`` around the Harbor run. The final
    synchronous pass guarantees alerts land even for jobs faster than
    one background interval.
    """
    if handle is None:
        return {"watched": False}
    if handle.stopped:
        return {"watched": True, "already_stopped": True}
    handle.stopped = True
    summary: dict[str, Any] = {"watched": True, "job_dir": str(handle.job_dir)}
    try:
        handle.stop_event.set()
        thread, handle.thread = handle.thread, None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=_STOP_JOIN_TIMEOUT_SECONDS)
            if thread.is_alive():
                summary["stop_timeout"] = True
        if handle.start_error is not None:
            _record_notice(handle, f"watcher failed to start: {handle.start_error}")
        _final_pass(handle, run_watch_fn=run_watch_fn or handle.run_watch_fn)
        summary["notified"] = _maybe_notify(
            handle, notify_runner=notify_runner or handle.notify_runner
        )
        summary.update(summarize_watch_alerts(read_watch_alerts(handle.job_dir)))
        return summary
    except Exception as exc:  # noqa: BLE001 -- stopping never fails the job
        summary["stop_error"] = f"{type(exc).__name__}: {exc}"
        with contextlib.suppress(Exception):
            _record_notice(handle, f"watcher stop failed: {type(exc).__name__}: {exc}")
        return summary
    finally:
        _untrack(handle)


def _supervise(handle: WatchHandle) -> None:
    try:
        interval = max(float(handle.interval_seconds or 0), 0.05)
        poll = min(interval, HOOK_POLL_SECONDS)
        journal = handle.job_dir / HOOK_JOURNAL
        offset = _journal_size(journal)
        last_pass = time.monotonic()
        while not handle.stop_event.wait(poll):
            # A trial lifecycle hook (start, phase change, end) triggers a pass now;
            # log-chunk records alone wait for the regular interval.
            offset, lifecycle = _new_lifecycle_events(journal, offset)
            if lifecycle or time.monotonic() - last_pass >= interval:
                _pass_and_maybe_notify(handle)
                last_pass = time.monotonic()
    except Exception as exc:  # noqa: BLE001 -- crash is recorded, job continues
        with contextlib.suppress(Exception):
            _record_notice(handle, f"watcher crashed: {type(exc).__name__}: {exc}")
    finally:
        _untrack(handle)


def _journal_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _new_lifecycle_events(journal: Path, offset: int) -> tuple[int, bool]:
    """``(new_offset, saw_event)`` for hook records appended since ``offset``."""
    size = _journal_size(journal)
    if size <= offset:
        return size, False
    try:
        with journal.open("rb") as stream:
            stream.seek(offset)
            appended = stream.read(size - offset)
    except OSError:
        return offset, False
    return size, b'"kind": "event"' in appended


def _pass_and_maybe_notify(handle: WatchHandle) -> dict[str, Any] | None:
    if not handle.job_dir.is_dir():
        return None
    try:
        fn = handle.run_watch_fn or _default_run_watch
        result = fn(runs_dirs=[handle.job_dir], out_dir=handle.out_dir)
    except Exception as exc:  # noqa: BLE001 -- a bad pass is a notice, not a failure
        _record_notice(handle, f"watch pass failed: {type(exc).__name__}: {exc}")
        return None
    with contextlib.suppress(Exception):
        _maybe_notify(handle, alerts=None)
    return result if isinstance(result, dict) else None


def _final_pass(handle: WatchHandle, *, run_watch_fn: Callable[..., Any] | None = None) -> None:
    if not handle.job_dir.is_dir():
        return
    try:
        fn = run_watch_fn or handle.run_watch_fn or _default_run_watch
        fn(runs_dirs=[handle.job_dir], out_dir=handle.out_dir)
    except Exception as exc:  # noqa: BLE001 -- recorded, never raised
        _record_notice(handle, f"watch final pass failed: {type(exc).__name__}: {exc}")


def _record_notice(handle: WatchHandle, message: str) -> None:
    try:
        handle.out_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "rule": WATCHER_ERROR_RULE,
            "severity": "low",
            "scope": "job",
            "job": handle.job_dir.name,
            "trial": handle.job_dir.name,
            "task": handle.job_dir.name,
            "step_ref": None,
            "quote": "",
            "detail": message[:300],
            "first_seen": datetime.now(UTC).isoformat(),
        }
        with (handle.out_dir / ALERTS_FILENAME).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
    except Exception:
        pass


def read_watch_alerts(job_dir: Path | str) -> list[dict[str, Any]]:
    """Parsed ``<job>/watch/alerts.jsonl`` rows (best-effort; never raises)."""
    try:
        lines = (
            (Path(job_dir) / WATCH_DIRNAME / ALERTS_FILENAME)
            .read_text(encoding="utf-8")
            .splitlines()
        )
    except OSError:
        return []
    alerts: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and "rule" in payload:
            alerts.append(payload)
    return alerts


def read_watch_acks(job_dir: Path | str) -> list[dict[str, Any]]:
    """Parsed ``<job>/watch/acks.jsonl`` rows (best-effort; never raises)."""
    try:
        lines = (
            (Path(job_dir) / WATCH_DIRNAME / ACKS_FILENAME).read_text(encoding="utf-8").splitlines()
        )
    except OSError:
        return []
    acks: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("rule"):
            acks.append(payload)
    return acks


def write_watch_ack(
    job_dir: Path | str,
    *,
    rule: str,
    actor: str,
    reason: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Acknowledge one alert kind for a job (HAR-174).

    Appends one record to ``<job>/watch/acks.jsonl``; ``alerts.jsonl`` is
    never edited or deleted. Raises ``ValueError`` for an unknown job, a
    missing or blank actor or reason, or a rule with no row in that job's
    alerts.
    """
    job_path = Path(job_dir)
    if not job_path.is_dir():
        raise ValueError(f"unknown job: {job_dir}")
    kind = (rule or "").strip()
    who = (actor or "").strip()
    why = (reason or "").strip()
    if not who:
        raise ValueError("ack needs a non-blank --actor")
    if not why:
        raise ValueError("ack needs a non-blank --reason")
    covers = [
        {"trial": str(alert.get("trial")), "rule": str(alert.get("rule"))}
        for alert in read_watch_alerts(job_path)
        if kind and str(alert.get("rule")) == kind
    ]
    if not covers:
        raise ValueError(f"no alert {kind!r} in {job_path}")
    record = {
        "rule": kind,
        "actor": who,
        "reason": why,
        "acked_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
        "job": job_path.name,
        "covers": covers,
    }
    watch_dir = job_path / WATCH_DIRNAME
    watch_dir.mkdir(parents=True, exist_ok=True)
    with (watch_dir / ACKS_FILENAME).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")
    return record


def covering_ack(alert: dict[str, Any], acks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The ack covering one alert row, or ``None`` (HAR-174)."""
    trial = str(alert.get("trial"))
    rule = str(alert.get("rule"))
    fallback: dict[str, Any] | None = None
    for ack in acks:
        covers = ack.get("covers")
        if isinstance(covers, list):
            for item in covers:
                if (
                    isinstance(item, dict)
                    and str(item.get("trial")) == trial
                    and str(item.get("rule")) == rule
                ):
                    return ack
        elif str(ack.get("rule")) == rule:
            fallback = fallback or ack
    return fallback


def annotate_alerts_with_acks(
    alerts: list[dict[str, Any]], acks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Copy of ``alerts`` marking rows an ack covers (HAR-174)."""
    if not acks:
        return [dict(alert) for alert in alerts]
    annotated: list[dict[str, Any]] = []
    for alert in alerts:
        row = dict(alert)
        ack = covering_ack(alert, acks)
        if ack is not None:
            row["acknowledged_by"] = str(ack.get("actor"))
            if ack.get("acked_at"):
                row["acked_at"] = str(ack.get("acked_at"))
        annotated.append(row)
    return annotated


def summarize_watch_alerts(alerts: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts plus the latest high-severity rule (``None`` when clean)."""
    critical = [alert for alert in alerts if alert.get("severity") == "high"]
    rules = sorted({str(alert.get("rule")) for alert in alerts if alert.get("rule")})
    return {
        "n_alerts": len(alerts),
        "n_critical": len(critical),
        "latest_critical": str(critical[-1].get("rule")) if critical else None,
        "rules": rules,
    }


def watch_status_suffix(job_dir: Path | str) -> str | None:
    """One-line ``evallab status`` suffix for a running job, or ``None``."""
    alerts = read_watch_alerts(job_dir)
    summary = summarize_watch_alerts(alerts)
    if summary["n_alerts"] == 0:
        return None
    suffix = f"watch: {summary['n_alerts']} alerts"
    if summary["latest_critical"] is not None:
        suffix += f"; latest critical: {summary['latest_critical']}"
    acks = read_watch_acks(job_dir)
    actors = sorted(
        {
            str(ack.get("actor"))
            for alert in alerts
            for ack in [covering_ack(alert, acks)]
            if ack is not None and ack.get("actor")
        }
    )
    if actors:
        suffix += f"; acknowledged by {', '.join(actors)}"
    return suffix


def format_watch_lines(alerts: list[dict[str, Any]], *, trial: str | None = None) -> list[str]:
    """Markdown ``## Live watch alerts`` section for in-memory alert rows."""
    if trial is not None:
        alerts = [alert for alert in alerts if str(alert.get("trial")) == trial]
    if not alerts:
        return []
    summary = summarize_watch_alerts(alerts)
    scope = f"trial `{trial}`" if trial is not None else "job"
    lines = [
        "## Live watch alerts",
        "",
        f"- {summary['n_alerts']} alerts on {scope} "
        f"({summary['n_critical']} high severity); source `{WATCH_DIRNAME}/{ALERTS_FILENAME}`",
    ]
    for alert in alerts:
        ref = f" {alert['step_ref']}" if alert.get("step_ref") else ""
        line = (
            f"- [{alert.get('severity')}] `{alert.get('rule')}` "
            f"{alert.get('trial')}{ref}: {alert.get('detail')}"
        )
        if alert.get("acknowledged_by"):
            line += f" (acknowledged by {alert['acknowledged_by']})"
        lines.append(line)
    lines.append("")
    return lines


def render_watch_lines(job_dir: Path | str, *, trial: str | None = None) -> list[str]:
    """Markdown ``## Live watch alerts`` section for a job page (or trial)."""
    alerts = annotate_alerts_with_acks(read_watch_alerts(job_dir), read_watch_acks(job_dir))
    return format_watch_lines(alerts, trial=trial)


def _load_posted(out_dir: Path) -> set[str]:
    try:
        payload = json.loads((out_dir / NOTIFIED_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    posted = payload.get("posted") if isinstance(payload, dict) else None
    return set(map(str, posted)) if isinstance(posted, list) else set()


def _save_posted(out_dir: Path, posted: set[str]) -> None:
    with contextlib.suppress(OSError):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / NOTIFIED_FILENAME).write_text(
            json.dumps({"posted": sorted(posted)}, indent=2) + "\n", encoding="utf-8"
        )


def _maybe_notify(
    handle: WatchHandle,
    *,
    notify_runner: Callable[..., Any] | None = None,
    alerts: list[dict[str, Any]] | None = None,
) -> bool:
    """Post new critical alerts to the card; each rule at most once per job."""
    if not handle.notify_enabled or not handle.linear_card:
        return False
    rows = alerts if alerts is not None else read_watch_alerts(handle.job_dir)
    posted = _load_posted(handle.out_dir)
    fresh = [
        alert
        for alert in rows
        if str(alert.get("rule")) in CRITICAL_RULES and str(alert.get("rule")) not in posted
    ]
    if not fresh:
        return False
    runner = notify_runner or subprocess.run
    body_lines = [f"evallab watch: critical alerts for `{handle.job_dir.name}`"]
    for alert in fresh:
        ref = f" {alert['step_ref']}" if alert.get("step_ref") else ""
        body_lines.append(
            f"- [{alert.get('severity')}] {alert.get('rule')} {alert.get('trial')}{ref}: "
            f"{alert.get('detail')}"
        )
    try:
        runner(["lin", "comment", handle.linear_card, "\n".join(body_lines)], check=True)
    except Exception as exc:  # noqa: BLE001 -- notify failure never fails the job
        _record_notice(handle, f"watch notify failed: {type(exc).__name__}: {exc}")
        return False
    posted.update(str(alert.get("rule")) for alert in fresh)
    _save_posted(handle.out_dir, posted)
    return True
