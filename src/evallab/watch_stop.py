"""Live-stop contract for ``evallab watch``: stop one cheating trial, keep the evidence.

Alerting, stopping, and RL-reward changes are separate integrations; this
module never touches rewards. The dispatcher side (``evallab.live_watch``)
evaluates fresh integrity alerts, captures host-side evidence *before* the
trial is stopped, then writes a stop request. The in-process actuator
(``evallab.harbor_watch_hooks.WatchHookPlugin``) honors the request by
cancelling exactly one trial task, so the stopped trial becomes a labelled
training-time hack example: the run stops, the evidence stays.

Modes (``EVALLAB_WATCH_STOP`` or ``--stop-mode``):

- ``off``: no capture, no requests. The watch is read-only again.
- ``dry-run`` (default): host snapshot plus ``docker diff`` only, recorded
  as ``would_stop``. Nothing is paused, committed, or cancelled.
- ``on``: full capture (pause, diff, top, commit, unpause), then a stop
  request the actuator honors within about a second.

Stop-eligible rules are ``history_mining``, ``hidden_test_read``, and
``grader_tamper`` only when backed by file-access tamper evidence (the alert
carries the protected-file before/after hashes). Guards, checked on every
decision: never a terminal trial, never once ``verification-start`` is in
the hook journal, never an acked alert, never a fleet rule, never a
model-free (``nop``/``oracle``) trial. Auto-watch never attaches to
model-free agents, so the dispatcher never auto-stops them.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STOP_MODE_ENV = "EVALLAB_WATCH_STOP"
STOP_MODES = ("off", "dry-run", "on")
DEFAULT_STOP_MODE = "dry-run"

STOP_SCHEMA = "evallab.watch_stop/v1"
STOP_REQUESTS_DIRNAME = "stop-requests"
STOP_RECORDS_DIRNAME = "stop-records"

#: Rules the watch may stop a trial over. ``grader_tamper`` qualifies only
#: with file-access tamper evidence (see :func:`stop_eligible_alert`).
STOP_ELIGIBLE_RULES = frozenset({"history_mining", "grader_tamper", "hidden_test_read"})

#: Integrity rules surfaced at the top of each job's board section.
INTEGRITY_BOARD_RULES = (
    "grader_tamper",
    "history_mining",
    "hidden_test_read",
    "git_object_read",
    "copy_acquired",
)

#: Exception marker a stopped trial ends with (distinguishable from ordinary
#: ``CancelledError``). Written into ``result.json`` by the in-process
#: actuator after Harbor's own ``CancelledError`` bookkeeping.
WATCH_STOP_EXCEPTION = "WatchStopCancelledError"
#: Prefix of the ``task.cancel()`` message the actuator sends; the wrapper
#: only converts cancellations carrying it (plus a pending stop request).
WATCH_STOP_MESSAGE = "evallab-watch-stop"

#: Hook-journal event that forbids stopping: the verifier may already run.
VERIFICATION_START_EVENT = "verification-start"

#: Host files snapshotted into ``watch/stop-records/<trial>/`` when present.
#: ``trial.log`` is tailed; everything else is copied whole up to a cap.
SNAPSHOT_FILES = (
    "agent/trajectory.json",
    "trajectory.json",
    "agent/file-access.jsonl",
    "proxy-live/calls.jsonl",
    "proxy-live/limits.json",
    "config.json",
    "lock.json",
)
TRIAL_LOG_TAIL_BYTES = 262_144
SNAPSHOT_MAX_BYTES = 8 * 1024 * 1024

#: Local image tag for the committed pre-stop container (never exported).
STOP_IMAGE_PREFIX = "evallab-stop/"


def stop_mode_from_env(env: dict[str, str] | None = None) -> str:
    """Parse the stop mode (unknown values fail closed to ``off``)."""
    raw = (env if env is not None else os.environ).get(STOP_MODE_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_STOP_MODE
    mode = str(raw).strip().lower().replace("_", "-")
    return mode if mode in STOP_MODES else "off"


def stop_eligible_alert(alert: dict[str, Any]) -> tuple[bool, str]:
    """Whether one alert record may stop its trial (plus the reason)."""
    if not isinstance(alert, dict):
        return False, "not_an_alert"
    if alert.get("scope") == "fleet":
        return False, "fleet_scope"
    rule = str(alert.get("rule") or "")
    if rule not in STOP_ELIGIBLE_RULES:
        return False, f"rule_not_eligible:{rule or 'missing'}"
    if rule == "grader_tamper" and not alert.get("baseline_ref"):
        # Trajectory-only tamper hits stay observation-only; only the
        # file-access branch (before/after hashes, baseline ref) qualifies.
        return False, "grader_tamper_without_file_evidence"
    return True, "eligible"


def trial_terminal(status: dict[str, Any] | None, hooks: dict[str, Any] | None) -> bool:
    """Whether the trial already finished (files or hook journal)."""
    if hooks and hooks.get("terminal"):
        return True
    return bool(status) and status.get("state") == "finished"


def verification_started(hooks: dict[str, Any] | None) -> bool:
    """Whether ``verification-start`` is in the trial's hook journal."""
    if not hooks:
        return False
    return any(name == VERIFICATION_START_EVENT for name, _ in hooks.get("events") or [])


def stop_decision(
    alert: dict[str, Any],
    *,
    status: dict[str, Any] | None,
    hooks: dict[str, Any] | None,
    acks: list[dict[str, Any]],
    mode: str,
) -> dict[str, str]:
    """Decide ``stop`` / ``would_stop`` / ``skip`` for one fresh alert."""
    from evallab.auto_watch import covering_ack

    rule = str(alert.get("rule") or "unknown")
    trial = str(alert.get("trial") or "unknown")
    if mode == "off":
        return {"action": "skip", "reason": "mode_off", "rule": rule, "trial": trial}
    eligible, why = stop_eligible_alert(alert)
    if not eligible:
        return {"action": "skip", "reason": why, "rule": rule, "trial": trial}
    if trial_terminal(status, hooks):
        return {"action": "skip", "reason": "terminal", "rule": rule, "trial": trial}
    if verification_started(hooks):
        return {"action": "skip", "reason": "verification_started", "rule": rule, "trial": trial}
    if covering_ack(alert, acks or []) is not None:
        return {"action": "skip", "reason": "acked", "rule": rule, "trial": trial}
    if mode == "dry-run":
        return {"action": "would_stop", "reason": "dry_run", "rule": rule, "trial": trial}
    if mode == "on":
        return {"action": "stop", "reason": "eligible", "rule": rule, "trial": trial}
    return {"action": "skip", "reason": f"unknown_mode:{mode}", "rule": rule, "trial": trial}


def stop_requests_dir(watch_dir: Path) -> Path:
    """``<watch>/stop-requests`` (created on write, never on read)."""
    return Path(watch_dir) / STOP_REQUESTS_DIRNAME


def stop_records_dir(watch_dir: Path) -> Path:
    """``<watch>/stop-records`` (created on write, never on read)."""
    return Path(watch_dir) / STOP_RECORDS_DIRNAME


def request_path_for(watch_dir: Path, trial: str) -> Path:
    """Filesystem path of one trial's stop request."""
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in trial)
    return stop_requests_dir(watch_dir) / f"{safe or 'trial'}.json"


def write_stop_request(
    watch_dir: Path,
    *,
    trial: str,
    rule: str,
    step_ref: str | None,
    alert: dict[str, Any] | None,
    requested_by: str,
    mode: str,
    reason: str | None = None,
    now: float | None = None,
) -> tuple[Path, bool]:
    """Write ``stop-requests/<trial>.json``; ``(path, created)``.

    The first request wins: an existing file is left untouched so a manual
    stop and an auto stop cannot overwrite each other's provenance.
    """
    path = request_path_for(watch_dir, trial)
    if path.is_file():
        return path, False
    moment = now if now is not None else time.time()
    record = {
        "schema": STOP_SCHEMA,
        "trial": trial,
        "rule": rule,
        "step_ref": step_ref,
        "requested_at": datetime.fromtimestamp(moment, UTC).isoformat(),
        "requested_by": requested_by,
        "mode": mode,
        "alert": alert,
    }
    if reason:
        record["reason"] = reason
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    return path, True


def read_stop_request(watch_dir: Path, trial: str) -> dict[str, Any] | None:
    """Parse one trial's stop request (``None`` when absent/unreadable)."""
    try:
        payload = json.loads(request_path_for(watch_dir, trial).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def stop_already_handled(watch_dir: Path, trial: str) -> bool:
    """Whether a request or a record already exists for this trial."""
    if request_path_for(watch_dir, trial).is_file():
        return True
    safe = request_path_for(watch_dir, trial).stem
    return (stop_records_dir(watch_dir) / f"{safe}.json").is_file()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def capture_host_snapshot(
    *,
    job_dir: Path,
    trial: str,
    dest_dir: Path,
    alert: dict[str, Any] | None = None,
    status: dict[str, Any] | None = None,
    hooks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Copy host-side trial evidence into ``dest_dir``; return the manifest.

    Never raises: missing files are listed under ``missing`` and copy
    failures under ``errors``; both are recorded, never fatal.
    """
    from evallab.harbor_watch_hooks import read_hook_journal

    job_dir = Path(job_dir)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    trial_candidates = [job_dir / trial]
    manifest: list[dict[str, Any]] = []
    missing: list[str] = []
    errors: list[str] = []

    def _store(relative: str, data: bytes) -> None:
        target = dest_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        manifest.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
        )

    for trial_dir in trial_candidates:
        for name in SNAPSHOT_FILES:
            if "/" in name and name.startswith("proxy-live"):
                # Same resolution order as the watch's live-proxy reader.
                for base in (trial_dir, job_dir):
                    candidate = base / name
                    if candidate.is_file():
                        break
                else:
                    continue
            else:
                candidate = trial_dir / name
                if not candidate.is_file():
                    continue
            try:
                size = candidate.stat().st_size
                if size > SNAPSHOT_MAX_BYTES:
                    data = candidate.read_bytes()[:SNAPSHOT_MAX_BYTES]
                    manifest.append(
                        {
                            "path": name,
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "bytes": len(data),
                            "truncated": True,
                            "source_bytes": size,
                        }
                    )
                    (dest_dir / name).parent.mkdir(parents=True, exist_ok=True)
                    (dest_dir / name).write_bytes(data)
                else:
                    data = candidate.read_bytes()
                    _store(name, data)
            except OSError as exc:
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
        log_path = trial_dir / "trial.log"
        try:
            if log_path.is_file():
                with log_path.open("rb") as handle:
                    handle.seek(0, os.SEEK_END)
                    size = handle.tell()
                    handle.seek(max(0, size - TRIAL_LOG_TAIL_BYTES))
                    data = handle.read()
                _store("trial.log.tail", data)
                manifest[-1]["source_bytes"] = size
                if size > TRIAL_LOG_TAIL_BYTES:
                    manifest[-1]["truncated"] = True
        except OSError as exc:
            errors.append(f"trial.log: {type(exc).__name__}: {exc}")
        try:
            journal = read_hook_journal(job_dir).get(trial)
        except Exception as exc:  # noqa: BLE001 -- capture never fails the stop
            errors.append(f"hooks.jsonl slice: {type(exc).__name__}: {exc}")
            journal = None
        if journal is not None or hooks is not None:
            _store(
                "hook-journal-slice.json",
                json.dumps(journal if journal is not None else hooks, indent=2).encode(),
            )
        if alert is not None:
            _store("firing-alert.json", json.dumps(alert, indent=2).encode())
        if status is not None:
            _store("status-trial-entry.json", json.dumps(status, indent=2).encode())
        if not manifest and not errors:
            missing.append(str(trial_dir))
    return {"files": manifest, "missing": missing, "errors": errors}


def trial_container_id(trial_name: str, *, runner: Any = None) -> tuple[str | None, str | None]:
    """Resolve one Docker main container for a trial (``(id, error)``)."""
    from evallab.harbor_state_journal import compose_project_name

    run = runner or subprocess.run
    try:
        completed = run(
            [
                "docker",
                "ps",
                "--filter",
                f"label=com.docker.compose.project={compose_project_name(trial_name)}",
                "--filter",
                "label=com.docker.compose.service=main",
                "--format",
                "{{.ID}}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if completed.returncode != 0:
        return None, (completed.stderr or completed.stdout or "docker ps failed").strip()[:300]
    ids = [line for line in (completed.stdout or "").splitlines() if line.strip()]
    if len(ids) != 1:
        return None, f"found {len(ids)} matching main containers"
    return ids[0].strip(), None


def stop_image_tag(trial_name: str) -> str:
    """Local-only commit tag for a stopped trial's container."""
    safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in trial_name).strip("-")
    return f"{STOP_IMAGE_PREFIX}{safe or 'trial'}"


def docker_capture(
    trial_name: str,
    *,
    pause: bool,
    runner: Any = None,
) -> dict[str, Any]:
    run = runner or subprocess.run
    outcome: dict[str, Any] = {"trial": trial_name, "errors": [], "commands": []}

    def _sh(*argv: str, timeout: int) -> tuple[int, str, str]:
        try:
            completed = run(
                list(argv), check=False, capture_output=True, text=True, timeout=timeout
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return -1, "", f"{type(exc).__name__}: {exc}"
        return completed.returncode, (completed.stdout or ""), (completed.stderr or "")

    if shutil.which("docker") is None:
        outcome["skipped"] = "docker CLI unavailable"
        return outcome
    container, error = trial_container_id(trial_name, runner=run)
    if container is None:
        outcome["skipped"] = error or "no container resolved"
        return outcome
    outcome["container"] = container
    paused = False
    try:
        if pause:
            code, _, err = _sh("docker", "pause", container, timeout=30)
            outcome["commands"].append({"argv": ["docker", "pause"], "returncode": code})
            if code == 0:
                paused = True
            else:
                outcome["errors"].append(f"pause: {(err or 'failed').strip()[:300]}")
        code, out, err = _sh("docker", "diff", container, timeout=60)
        outcome["commands"].append({"argv": ["docker", "diff"], "returncode": code})
        if code == 0:
            outcome["diff"] = out[:200_000]
            if len(out) > 200_000:
                outcome["diff_truncated"] = True
        else:
            outcome["errors"].append(f"diff: {(err or 'failed').strip()[:300]}")
        code, out, err = _sh("docker", "top", container, "ps", "-ef", timeout=30)
        if code != 0:
            code, out, err = _sh("docker", "top", container, timeout=30)
        outcome["commands"].append({"argv": ["docker", "top"], "returncode": code})
        if code == 0:
            outcome["top"] = out[:50_000]
        else:
            outcome["errors"].append(f"top: {(err or 'failed').strip()[:300]}")
        if pause:
            tag = stop_image_tag(trial_name)
            code, out, err = _sh("docker", "commit", container, tag, timeout=300)
            outcome["commands"].append({"argv": ["docker", "commit"], "returncode": code})
            if code == 0:
                outcome["image"] = tag
                # The tag is transient (teardown/hygiene may reap it); the
                # digest identifies the captured content regardless.
                outcome["image_digest"] = out.strip().split()[-1][:120] if out.strip() else None
            else:
                outcome["errors"].append(f"commit: {(err or out or 'failed').strip()[:300]}")
    finally:
        if paused:
            code, _, err = _sh("docker", "unpause", container, timeout=30)
            outcome["commands"].append({"argv": ["docker", "unpause"], "returncode": code})
            if code != 0:
                outcome["errors"].append(f"unpause: {(err or 'failed').strip()[:300]}")
    return outcome


def usage_at_stop(status: dict[str, Any] | None) -> dict[str, Any]:
    """Honest usage snapshot: tokens/steps so far plus remaining budget.

    Avoided spend is unknowable; ``remaining_input_budget`` is an explicit
    upper bound on what stopping could have saved, not a claimed saving.
    """
    status = status or {}
    prompt = status.get("prompt_tokens") or 0
    limit = status.get("input_token_limit")
    report: dict[str, Any] = {
        "steps_so_far": status.get("steps"),
        "episodes_so_far": status.get("episodes"),
        "prompt_tokens_so_far": prompt,
        "completion_tokens_so_far": status.get("completion_tokens"),
        "cost_usd_so_far": status.get("cost_usd"),
        "input_token_limit": limit,
    }
    if isinstance(limit, (int, float)) and limit and isinstance(prompt, (int, float)):
        report["remaining_input_budget_upper_bound"] = max(0, int(limit) - int(prompt))
    return report


def write_stop_record(
    watch_dir: Path,
    *,
    trial: str,
    outcome: str,
    rule: str | None,
    step_ref: str | None,
    requested_by: str,
    mode: str,
    manifest: dict[str, Any] | None = None,
    docker: dict[str, Any] | None = None,
    verifier_started: bool | None = None,
    usage: dict[str, Any] | None = None,
    reason: str | None = None,
    capture_errors: list[str] | None = None,
    now: float | None = None,
) -> Path:
    """Write ``stop-records/<trial>.json`` (overwrites: latest decision wins)."""
    moment = now if now is not None else time.time()
    record: dict[str, Any] = {
        "schema": STOP_SCHEMA,
        "trial": trial,
        "outcome": outcome,
        "rule": rule,
        "step_ref": step_ref,
        "requested_by": requested_by,
        "mode": mode,
        "recorded_at": datetime.fromtimestamp(moment, UTC).isoformat(),
        "capture_manifest": manifest or {"files": [], "missing": [], "errors": []},
        "verifier_never_ran": (not verifier_started) if verifier_started is not None else None,
    }
    if docker is not None:
        record["docker"] = docker
    if usage is not None:
        record["usage_at_stop"] = usage
    if reason:
        record["reason"] = reason
    if capture_errors:
        record["capture_errors"] = capture_errors
    path = stop_records_dir(watch_dir) / f"{request_path_for(watch_dir, trial).stem}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_stop_record(watch_dir: Path, trial: str) -> dict[str, Any] | None:
    """Parse one trial's stop record (``None`` when absent/unreadable)."""
    path = stop_records_dir(watch_dir) / f"{request_path_for(watch_dir, trial).stem}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def drive_trial_stops(
    *,
    watch_dir: Path,
    job_dirs: dict[str, Path],
    statuses: list[dict[str, Any]],
    fresh_alerts: list[dict[str, Any]],
    hooks_by_job: dict[str, dict[str, dict[str, Any]]],
    acks_by_job: dict[str, list[dict[str, Any]]],
    mode: str,
    requested_by: str = "auto",
    moment: float | None = None,
) -> list[dict[str, Any]]:
    """Capture evidence and request stops for fresh eligible alerts.

    One stop lifecycle per trial (the first eligible fresh alert wins; later
    ones are ignored once a request or record exists). Capture failures are
    recorded and never prevent the stop in ``on`` mode. Returns per-trial
    stop summaries for ``status.json`` and ``BOARD.md``.
    """
    watch_dir = Path(watch_dir)
    by_trial: dict[str, dict[str, Any]] = {}
    for alert in fresh_alerts:
        trial = str(alert.get("trial") or "")
        if trial and trial not in by_trial:
            by_trial[trial] = alert
    status_by_trial = {
        str(status.get("trial")): status for status in statuses if status.get("trial")
    }
    stops: list[dict[str, Any]] = []
    for trial, alert in by_trial.items():
        status = status_by_trial.get(trial)
        job_name = str(alert.get("job") or (status or {}).get("job") or "")
        job_dir = job_dirs.get(job_name)
        hooks = (hooks_by_job.get(job_name) or {}).get(trial)
        acks = acks_by_job.get(job_name) or []
        decision = stop_decision(alert, status=status, hooks=hooks, acks=acks, mode=mode)
        action = decision["action"]
        if action == "skip":
            continue
        if stop_already_handled(watch_dir, trial):
            continue
        rule = str(alert.get("rule") or "")
        step_ref = alert.get("step_ref")
        capture_errors: list[str] = []
        manifest: dict[str, Any] = {"files": [], "missing": [], "errors": []}
        docker: dict[str, Any] | None = None
        snapshot_dir = stop_records_dir(watch_dir) / request_path_for(watch_dir, trial).stem
        if job_dir is not None:
            try:
                manifest = capture_host_snapshot(
                    job_dir=job_dir,
                    trial=trial,
                    dest_dir=snapshot_dir,
                    alert=alert,
                    status=status,
                    hooks=hooks,
                )
            except Exception as exc:  # noqa: BLE001 -- capture never blocks a stop
                capture_errors.append(f"host snapshot: {type(exc).__name__}: {exc}")
            try:
                docker = docker_capture(trial, pause=(action == "stop" and mode == "on"))
            except Exception as exc:  # noqa: BLE001 -- capture never blocks a stop
                capture_errors.append(f"docker: {type(exc).__name__}: {exc}")
        else:
            capture_errors.append(f"unknown job dir for job {job_name!r}")
        outcome = "requested" if action == "stop" else "would_stop"
        record_path = write_stop_record(
            watch_dir,
            trial=trial,
            outcome=outcome,
            rule=rule,
            step_ref=str(step_ref) if step_ref is not None else None,
            requested_by=requested_by,
            mode=mode,
            manifest=manifest,
            docker=docker,
            verifier_started=verification_started(hooks) if hooks is not None else None,
            usage=usage_at_stop(status),
            reason=decision.get("reason"),
            capture_errors=capture_errors or None,
            now=moment,
        )
        request_path: Path | None = None
        if action == "stop":
            request_path, _ = write_stop_request(
                watch_dir,
                trial=trial,
                rule=rule,
                step_ref=str(step_ref) if step_ref is not None else None,
                alert=alert,
                requested_by=requested_by,
                mode=mode,
                now=moment,
            )
        entry = {
            "trial": trial,
            "job": job_name,
            "rule": rule,
            "step_ref": step_ref,
            "mode": mode,
            "outcome": outcome,
            "reason": decision.get("reason"),
            "record": str(record_path),
        }
        if request_path is not None:
            entry["request"] = str(request_path)
        if status is not None:
            status["stop"] = {
                "mode": mode,
                "outcome": outcome,
                "rule": rule,
                "step_ref": step_ref,
            }
        stops.append(entry)
    return stops
