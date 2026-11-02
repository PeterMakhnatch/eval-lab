from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import os
import re
import signal
import time
from datetime import datetime, timedelta
from pathlib import Path

from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.exceptions import CancelledError as TemporalCancelledError

from temporal_squad.exceptions import ReportMissing, RouteExhausted
from temporal_squad.models import (
    LaunchResult,
    LaunchWorkerRequest,
    ReportResult,
    SettleSpendRequest,
    SpendResult,
)


@activity.defn
async def route_quota_ok(quota_file: str | None, route: str) -> bool:
    """Check if route has available quota. Reads JSON file simulating quota/balance."""
    if not quota_file:
        return True
    path = Path(quota_file)
    if not path.exists():
        # Quota file specified but not found
        return True

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ApplicationError(f"Failed to read quota file: {exc}", non_retryable=False) from exc

    routes = data.get("routes", {})
    route_info = routes.get(route)
    if route_info is not None:
        if isinstance(route_info, dict):
            if route_info.get("status") == "exhausted":
                raise RouteExhausted.to_application_error(
                    f"Route '{route}' explicitly marked exhausted in {quota_file}"
                )
            remaining = route_info.get("remaining")
            if remaining is not None and float(remaining) <= 0.0:
                raise RouteExhausted.to_application_error(
                    f"Route '{route}' remaining quota is {remaining} <= 0"
                )
        elif isinstance(route_info, (int, float)) and float(route_info) <= 0.0:
            raise RouteExhausted.to_application_error(
                f"Route '{route}' quota balance is {route_info} <= 0"
            )

    global_remaining = data.get("remaining_usd")
    if global_remaining is not None and float(global_remaining) <= 0.0:
        raise RouteExhausted.to_application_error(
            f"Global remaining balance is {global_remaining} <= 0"
        )

    return True


@activity.defn
async def launch_worker(req: LaunchWorkerRequest) -> LaunchResult:
    """Launch worker subprocess in a new process group, heartbeat periodically, and kill on cancel."""
    raw_path = Path(req.raw_dir)
    raw_path.mkdir(parents=True, exist_ok=True)
    log_path = raw_path / "worker.log"
    pid_meta_path = raw_path / f"worker-attempt-{req.attempt}.json"

    # Simulated transient launch failure hook for testing retry policy
    if req.env_vars.get("SIMULATE_TRANSIENT_LAUNCH_ERROR") == "1":
        trigger_count_file = raw_path / ".transient_counter"
        count = int(trigger_count_file.read_text()) if trigger_count_file.exists() else 0
        trigger_count_file.write_text(str(count + 1))
        if count == 0:
            raise ApplicationError(
                "Simulated transient launch failure",
                type="TransientLaunchError",
                non_retryable=False,
            )

    # 1. Check if previous execution already completed report (idempotency / crash recovery)
    rep_path = raw_path / "report.md"
    if rep_path.exists():
        lines = [ln.strip() for ln in rep_path.read_text(errors="ignore").splitlines() if ln.strip()]
        if lines and lines[-1].startswith(f"DS-DONE: {req.item_id}"):
            activity.logger.info(
                f"Item {req.item_id} already has completed report.md; reusing prior run."
            )
            return LaunchResult(
                item_id=req.item_id,
                attempt=req.attempt,
                exit_code=0,
                pid=0,
                log_size=log_path.stat().st_size if log_path.exists() else 0,
                elapsed_sec=0.0,
                reused_prior_run=True,
            )

    # 2. Check if a process was already spawned and is still alive
    existing_pid = None
    if pid_meta_path.exists():
        try:
            meta = json.loads(pid_meta_path.read_text())
            p = meta.get("pid")
            if p and isinstance(p, int):
                os.kill(p, 0)  # check if alive
                existing_pid = p
        except (OSError, ValueError):
            existing_pid = None

    t0 = time.time()
    proc: asyncio.subprocess.Process | None = None
    proc_pid: int = 0

    log_fh = open(log_path, "ab")  # noqa: SIM115, ASYNC230
    try:
        if existing_pid:
            activity.logger.info(f"Re-attaching to existing process {existing_pid} for {req.item_id}")
            proc_pid = existing_pid
        else:
            # Spawn new process in its own session / process group
            proc = await asyncio.create_subprocess_exec(
                *req.cmd,
                cwd=req.cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log_fh,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                env={**os.environ, **req.env_vars},
            )
            proc_pid = proc.pid
            pid_meta_path.write_text(json.dumps({"pid": proc_pid, "cmd": req.cmd, "t0": t0}))

        # Monitor loop with heartbeats
        while True:
            # Heartbeat with current stats
            log_size = log_path.stat().st_size if log_path.exists() else 0
            activity.heartbeat(
                {
                    "item_id": req.item_id,
                    "attempt": req.attempt,
                    "pid": proc_pid,
                    "log_size": log_size,
                    "elapsed_sec": time.time() - t0,
                }
            )

            if proc is not None:
                if proc.returncode is not None:
                    break
                try:
                    step = min(0.5, req.heartbeat_interval_sec)
                    await asyncio.wait_for(proc.wait(), timeout=step)
                    break
                except TimeoutError:
                    continue
            else:
                # Re-attached external process: poll with waitpid or kill(0)
                try:
                    os.kill(proc_pid, 0)
                except ProcessLookupError:
                    # Process exited
                    break
                await asyncio.sleep(req.heartbeat_interval_sec)

        exit_code = proc.returncode if proc is not None and proc.returncode is not None else 0
        elapsed = time.time() - t0
        log_size = log_path.stat().st_size if log_path.exists() else 0
        if exit_code == 29:
            wait_sec = 90.0
            if log_path.exists():
                text = log_path.read_text(errors="ignore")
                m = re.search(r"resets at (\S+)", text)
                if m:
                    with contextlib.suppress(Exception):
                        wait_sec = max(0.0, datetime.fromisoformat(m.group(1)).timestamp() - time.time())
            raise ApplicationError("quota", next_retry_delay=timedelta(seconds=wait_sec))
        return LaunchResult(
            item_id=req.item_id,
            attempt=req.attempt,
            exit_code=exit_code,
            pid=proc_pid,
            log_size=log_size,
            elapsed_sec=elapsed,
            reused_prior_run=False,
        )

    except (asyncio.CancelledError, TemporalCancelledError):
        activity.logger.warning(
            f"Launch activity for {req.item_id} cancelled. Terminating process group {proc_pid}"
        )
        if proc_pid > 0:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc_pid, signal.SIGKILL)

            if proc is not None:
                with contextlib.suppress(TimeoutError, OSError):
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
        raise
    finally:
        log_fh.close()


@activity.defn
async def read_report_state(raw_dir: str, item_id: str) -> ReportResult:
    """Read report.md and parse the last line terminal marker (DS-DONE / DS-BLOCKED)."""
    rp_path = Path(raw_dir) / "report.md"
    if not rp_path.exists():
        raise ReportMissing.to_application_error(
            f"Report file {rp_path} does not exist for item {item_id}"
        )

    content = rp_path.read_text(encoding="utf-8", errors="ignore")
    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    if not lines:
        raise ReportMissing.to_application_error(f"Report file {rp_path} is empty for item {item_id}")

    last_line = lines[-1]
    done_marker = f"DS-DONE: {item_id}"
    blocked_marker = f"DS-BLOCKED: {item_id}"

    if last_line.startswith(done_marker):
        return ReportResult(item_id=item_id, status="done", note="", report_path=str(rp_path))
    elif last_line.startswith(blocked_marker):
        note = last_line[len(blocked_marker) :].strip()
        return ReportResult(item_id=item_id, status="blocked", note=note, report_path=str(rp_path))
    else:
        raise ReportMissing.to_application_error(
            f"Report last line {last_line!r} lacks required terminal marker for item {item_id}"
        )


@activity.defn
async def settle_spend(req: SettleSpendRequest) -> SpendResult:
    """Parse session logs for cost and token usage; append to spend ledger if configured."""
    cost = req.fixed_cost or 0.0
    inp_tokens = 0
    out_tokens = 0
    cache_read = 0
    served: collections.Counter[str] = collections.Counter()
    fallback_events = 0

    sess_path = Path(req.session_dir)
    if sess_path.exists() and req.fixed_cost is None:
        for jsonl_file in sess_path.rglob("*.jsonl"):
            try:
                for line in jsonl_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if not line.strip():
                        continue
                    if (
                        '"resolvedModelIsFallback": true' in line
                        or '"resolvedModelIsFallback":true' in line
                    ):
                        fallback_events += 1
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    msg = record.get("message")
                    if isinstance(msg, dict) and msg.get("role") == "assistant":
                        provider = msg.get("provider", "unknown")
                        model = msg.get("model", "unknown")
                        served[f"{provider}/{model}"] += 1
                        usage = msg.get("usage") or {}
                        inp_tokens += usage.get("input", 0) or 0
                        out_tokens += usage.get("output", 0) or 0
                        cache_read += usage.get("cacheRead", 0) or 0
                        c = usage.get("cost")
                        if isinstance(c, dict):
                            cost += c.get("total", 0.0) or 0.0
                        elif isinstance(c, (int, float)):
                            cost += float(c)
            except (OSError, ValueError) as exc:
                activity.logger.warning(f"Error scanning session logs in {jsonl_file}: {exc}")

    off_route = {k: v for k, v in served.items() if k != req.route and not k.endswith("/None")}

    real_cost = 0.0 if req.subscription and req.fixed_cost is None else round(cost, 6)
    return SpendResult(
        item_id=req.item_id,
        cost=real_cost,
        input_tokens=inp_tokens,
        output_tokens=out_tokens,
        cache_read=cache_read,
        served=dict(served),
        off_route=off_route,
        fallback_events=fallback_events,
    )
