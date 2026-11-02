"""dbos_squad.py: Durable squad runner built on DBOS with SQLite persistence.

Mirrors ds_squad.py semantics:
- Items with `needs` dependencies and `gate` approval semantics.
- Subprocesses in separate process groups with heartbeat monitoring and clean cancellation.
- Failure classification: transient (backoff), quota-exhausted (durable DBOS.sleep),
  report-missing (continuation retry), budget-exceeded (terminal).
- Per-route DBOS queues with concurrency and rate limiting.
- Board status via DBOS.set_event / DBOS.get_event.
- Human gates via DBOS.recv / DBOS.send.
- Idempotent execution and crash recovery via SQLite step journaling.
"""
from __future__ import annotations

import collections
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dbos import DBOS, DBOSConfig, SetWorkflowID
from dbos._error import DBOSMaxStepRetriesExceeded

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Failure Exceptions
# ---------------------------------------------------------------------------

class WorkerFailure(Exception):
    """Base class for worker errors."""


class TransientWorkerError(WorkerFailure):
    """Network blip, transient exit code 1, or temporary host issue."""


class QuotaExhaustedError(WorkerFailure):
    """Rate limit or quota refusal (HTTP 429 / exit code 29)."""

    def __init__(self, reset_at: float, message: str = ""):
        super().__init__(message or f"quota exhausted; resets at epoch {reset_at}")
        self.reset_at = reset_at


class ReportMissingError(WorkerFailure):
    """Worker completed with exit code 0 but report lacks DS-DONE/DS-BLOCKED marker."""


class BudgetExceededError(WorkerFailure):
    """Item or squad cost exceeded the dollar cap."""


class TimeExceededError(WorkerFailure):
    """Worker exceeded its max_minutes / max_seconds timeout."""


class HeartbeatTimeoutError(WorkerFailure):
    """Worker failed to update heartbeat file within required interval."""


def is_transient_error(err: BaseException) -> bool:
    """Predicate for DBOS.step automatic retries with backoff."""
    return isinstance(err, TransientWorkerError)


# ---------------------------------------------------------------------------
# Usage & Cost Tracking for OMP
# ---------------------------------------------------------------------------

class Totals:
    """Incremental scanner of OMP session logs for token and cost attestation."""

    def __init__(self):
        self.offsets: dict[str, int] = {}
        self.cost = 0.0
        self.inp = self.out = self.cache = 0
        self.served: collections.Counter = collections.Counter()
        self.errors = 0
        self.fallback_events = 0
        self.model_changes: list[str] = []

    def scan(self, sess_dir: Path):
        if not sess_dir.exists():
            return
        for path in sess_dir.rglob("*.jsonl"):
            key = str(path)
            off = self.offsets.get(key, 0)
            try:
                with open(path, "rb") as fh:
                    fh.seek(off)
                    data = fh.read()
            except OSError:
                continue
            cut = data.rfind(b"\n") + 1
            if not cut:
                continue
            self.offsets[key] = off + cut
            for raw in data[:cut].splitlines():
                self._record(raw)

    def _record(self, raw: bytes):
        try:
            e = json.loads(raw)
        except ValueError:
            return
        if b'"resolvedModelIsFallback": true' in raw or b'"resolvedModelIsFallback":true' in raw:
            self.fallback_events += 1
        if e.get("type") == "model_change":
            self.model_changes.append(str(e.get("model")))
        m = e.get("message")
        if isinstance(m, dict) and m.get("role") == "assistant":
            self.served[f"{m.get('provider')}/{m.get('model')}"] += 1
            if m.get("stopReason") == "error":
                self.errors += 1
            u = m.get("usage") or {}
            self.inp += u.get("input", 0) or 0
            self.out += u.get("output", 0) or 0
            self.cache += u.get("cacheRead", 0) or 0
            c = u.get("cost")
            self.cost += (c.get("total", 0) or 0) if isinstance(c, dict) else (c or 0)

    def attest(self, want: str) -> dict:
        bad = {k: v for k, v in self.served.items() if k != want and not k.endswith("/None")}
        return {
            "requested": want,
            "served": dict(self.served),
            "off_route": bad,
            "fallback_events": self.fallback_events,
            "error_records": self.errors,
            "model_changes": self.model_changes[:6],
        }


# ---------------------------------------------------------------------------
# Subprocess Management Helper
# ---------------------------------------------------------------------------

def kill_process_group(pid: int) -> None:
    """Politely terminate then force kill a process group."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGTERM)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)
    for _ in range(15):
        try:
            os.kill(pid, 0)
            time.sleep(0.1)
        except (ProcessLookupError, PermissionError):
            return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)

def check_report_state(report_path: Path, item_id: str) -> tuple[str, str]:
    """Check report.md last line for DS-DONE or DS-BLOCKED."""
    if not report_path.exists():
        return "failed", "no report.md found"
    lines = [ln.strip() for ln in report_path.read_text(errors="ignore").splitlines() if ln.strip()]
    last = lines[-1] if lines else ""
    if last.startswith(f"DS-DONE: {item_id}"):
        return "done", ""
    if last.startswith(f"DS-BLOCKED: {item_id}"):
        return "blocked", last[len(f"DS-BLOCKED: {item_id}"):].strip()
    return "failed", f"missing DS-DONE / DS-BLOCKED marker (last line: {last[:80]!r})"


# ---------------------------------------------------------------------------
# DBOS Step: launch_worker
# ---------------------------------------------------------------------------

@DBOS.step(
    name="launch_worker",
    retries_allowed=True,
    max_attempts=3,
    interval_seconds=1.0,
    backoff_rate=2.0,
    should_retry=is_transient_error,
)
def launch_worker(
    item: dict,
    attempt: int,
    run_dir_str: str,
    plan_context: dict,
) -> dict:
    """Launch worker in its own process group, enforce heartbeats and timeouts, classify exits."""
    run_dir = Path(run_dir_str)
    iid = item["id"]
    raw_dir = run_dir / "raw" / iid
    raw_dir.mkdir(parents=True, exist_ok=True)
    report_file = raw_dir / "report.md"
    heartbeat_file = raw_dir / "heartbeat.txt"
    worker_log = raw_dir / "worker.log"
    sess_dir = run_dir / "sessions" / f"{iid}-a{attempt}"
    sess_dir.mkdir(parents=True, exist_ok=True)

    max_seconds = float(item.get("max_seconds", item.get("max_minutes", 10) * 60))
    heartbeat_timeout = float(item.get("heartbeat_timeout", 15.0))
    ledger_path = Path(plan_context.get("ledger_path", run_dir / "spend.jsonl"))

    # Construct execution command
    if item.get("command"):
        cmd = []
        for part in item["command"]:
            p = str(part)
            p = p.replace("{ID}", iid)
            p = p.replace("{RAW}", str(raw_dir))
            p = p.replace("{RUN}", str(run_dir))
            p = p.replace("{REPORT}", str(report_file))
            p = p.replace("{HEARTBEAT}", str(heartbeat_file))
            p = p.replace("{LEDGER}", str(ledger_path))
            p = p.replace("{SECONDS}", str(item.get("seconds", 20)))
            cmd.append(p)
    elif item.get("fake_agent"):
        fa_script = Path(__file__).parent / "fake_agent.py"
        cmd = [
            sys.executable,
            str(fa_script),
            "--id", iid,
            "--ledger", str(ledger_path),
            "--report", str(report_file),
            "--seconds", str(item.get("seconds", 20)),
            "--heartbeat", str(heartbeat_file),
        ]
        if item.get("quota_until"):
            cmd.extend(["--quota-until", str(item["quota_until"])])
        if item.get("fail_transient"):
            cmd.append("--fail-transient")
        if item.get("no_done_marker"):
            cmd.append("--no-done-marker")
        if item.get("mode"):
            cmd.extend(["--mode", str(item["mode"])])
        if item.get("hang_silent"):
            cmd.append("--hang-silent")
    else:
        # Real omp -p command
        model = plan_context.get("model", "google-antigravity/gemini-3.8-flash")
        config_path = plan_context.get("worker_yml", "/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/wf/worker.yml")
        prompt_file = raw_dir / (f"prompt-a{attempt}.md" if attempt > 0 else "prompt.md")
        brief_content = item.get("brief_text") or (run_dir / item.get("brief", "")).read_text(errors="ignore") if item.get("brief") else ""
        prompt_text = (
            f"# Task: {iid} - {item.get('title', '')}\n\n{brief_content}\n\n"
            f"---\n# Output Requirement:\nWrite your report to `report.md`. The very last line of `report.md` MUST be exactly `DS-DONE: {iid}`.\n"
        )
        if attempt > 0:
            tail = worker_log.read_text(errors="ignore")[-2500:] if worker_log.exists() else ""
            prompt_text += (
                f"\n\n---\n# CONTINUATION (attempt {attempt})\n"
                f"Previous attempt ended without completing. Examine raw files in {raw_dir} and complete the task. "
                f"The last report line must be `DS-DONE: {iid}`.\nPrevious output tail:\n{tail}\n"
            )
        prompt_file.write_text(prompt_text, encoding="utf-8")
        cmd = [
            "omp", "-p",
            "--model", model,
            "--config", config_path,
            "--session-dir", str(sess_dir),
            "--no-skills",
            "--tools", "read,bash,edit,write,grep,glob,todo",
            f"@{prompt_file}",
        ]

    # Spawn process in new process group
    env = {**os.environ, "EVALLAB_DISABLE_LOCAL_DOCKER": "1", "HARBOR_TELEMETRY": "off"}
    t0 = time.time()
    with open(worker_log, "ab") as log_fh:
        proc = subprocess.Popen(
            cmd,
            cwd=raw_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        heartbeat_file.write_text(f"{time.time()}:{proc.pid}\n", encoding="utf-8")
        last_hb_time = time.time()
        last_log_size = 0
        rc = None
        try:
            while proc.poll() is None:
                time.sleep(0.5)
                elapsed = time.time() - t0

                # Wall clock timeout check
                if elapsed > max_seconds:
                    kill_process_group(proc.pid)
                    proc.wait(timeout=5)
                    raise TimeExceededError(f"Worker {iid} exceeded wall-clock timeout ({elapsed:.1f}s > {max_seconds:.1f}s)")

                # Heartbeat check
                if item.get("fake_agent") and "heartbeat_timeout" in item:
                    if heartbeat_file.exists():
                        try:
                            mtime = heartbeat_file.stat().st_mtime
                            if mtime > last_hb_time:
                                last_hb_time = mtime
                            elif time.time() - last_hb_time > heartbeat_timeout:
                                kill_process_group(proc.pid)
                                proc.wait(timeout=5)
                                raise HeartbeatTimeoutError(f"Worker {iid} heartbeat timed out ({time.time() - last_hb_time:.1f}s > {heartbeat_timeout:.1f}s)")
                        except OSError:
                            pass
                elif not item.get("fake_agent") and "heartbeat_timeout" in item:
                    try:
                        current_log_size = worker_log.stat().st_size if worker_log.exists() else 0
                        if current_log_size > last_log_size:
                            last_log_size = current_log_size
                            last_hb_time = time.time()
                        elif time.time() - last_hb_time > heartbeat_timeout:
                            kill_process_group(proc.pid)
                            proc.wait(timeout=5)
                            raise HeartbeatTimeoutError(f"Worker {iid} log stalled for > {heartbeat_timeout:.1f}s")
                    except OSError:
                        pass

            rc = proc.poll()
        except (KeyboardInterrupt, Exception):
            kill_process_group(proc.pid)
            raise

    duration = time.time() - t0

    # Scan OMP session usage if OMP ran
    totals = Totals()
    totals.scan(sess_dir)
    cost = totals.cost

    # Check exit codes
    if rc == 29:
        log_text = worker_log.read_text(errors="ignore") if worker_log.exists() else ""
        matches = re.findall(r"resets at\s+(\S+)", log_text)
        if matches:
            try:
                dt = datetime.fromisoformat(matches[-1])
                reset_epoch = dt.timestamp()
            except Exception:
                pass
        if reset_epoch is None and item.get("quota_until"):
            reset_epoch = float(item["quota_until"])
        if reset_epoch is None:
            reset_epoch = time.time() + 60.0
        raise QuotaExhaustedError(reset_at=reset_epoch, message=f"Quota exhausted for {iid}; resets at epoch {reset_epoch}")

    if rc != 0:
        raise TransientWorkerError(f"Worker {iid} exited with non-zero exit code {rc}")

    # Check report state
    status, note = check_report_state(report_file, iid)
    if status == "failed":
        raise ReportMissingError(f"Worker {iid} report validation failed: {note}")

    return {
        "id": iid,
        "status": status,
        "note": note,
        "exit_code": rc,
        "duration_sec": duration,
        "cost_usd": cost,
        "attempt": attempt,
        "attestation": totals.attest(plan_context.get("model", "")),
    }


# ---------------------------------------------------------------------------
# DBOS Workflow: item_workflow
# ---------------------------------------------------------------------------

@DBOS.workflow(name="item_workflow")
def item_workflow(
    item: dict,
    run_dir_str: str,
    plan_context: dict,
    squad_wf_id: str = "",
) -> dict:
    """Durable workflow for a single item: gates, quota sleeps, continuation retries."""
    iid = item["id"]
    run_dir = Path(run_dir_str)

    # 1. Gate evaluation
    if item.get("gate_topic") or item.get("gate"):
        topic = item.get("gate_topic") or f"gate:{iid}"
        # If gate is a file path, we can either wait for file or DBOS.recv
        if item.get("gate") and (run_dir / item["gate"]).exists():
            pass
        else:
            # Wait on DBOS.recv for human approval signal
            msg = DBOS.recv(topic=topic, timeout_seconds=float(item.get("gate_timeout", 3600 * 8)))
            if isinstance(msg, dict) and not msg.get("approved", True):
                return {
                    "id": iid,
                    "status": "blocked",
                    "note": f"gate approval rejected: {msg.get('reason', 'user denied')}",
                    "cost_usd": 0.0,
                    "duration_sec": 0.0,
                }

    # 2. Execution loop with retries
    attempt = 0
    max_retries = int(item.get("retry", 0))
    total_cost = 0.0

    while True:
        try:
            res = launch_worker(item, attempt, run_dir_str, plan_context)
            total_cost += res.get("cost_usd", 0.0)
            res["cost_usd"] = total_cost
            return res
        except QuotaExhaustedError as qe:
            wait_sec = max(0.0, qe.reset_at - time.time())
            DBOS.sleep(wait_sec)
            continue
        except ReportMissingError as rme:
            if attempt < max_retries + 1:
                attempt += 1
                continue
            return {
                "id": iid,
                "status": "failed",
                "note": f"iteration cap reached after report missing: {rme}",
                "cost_usd": total_cost,
                "duration_sec": 0.0,
            }
        except TransientWorkerError as twe:
            if attempt < max_retries:
                attempt += 1
                continue
            return {
                "id": iid,
                "status": "failed",
                "note": f"iteration cap reached after transient failures: {twe}",
                "cost_usd": total_cost,
                "duration_sec": 0.0,
            }
        except DBOSMaxStepRetriesExceeded as msre:
            return {
                "id": iid,
                "status": "failed",
                "note": f"iteration cap reached after maximum step retries: {msre}",
                "cost_usd": total_cost,
                "duration_sec": 0.0,
            }
        except TimeExceededError as te:
            return {
                "id": iid,
                "status": "killed-time",
                "note": str(te),
                "cost_usd": total_cost,
                "duration_sec": 0.0,
            }
        except HeartbeatTimeoutError as he:
            return {
                "id": iid,
                "status": "failed",
                "note": str(he),
                "cost_usd": total_cost,
                "duration_sec": 0.0,
            }
        except BudgetExceededError as be:
            return {
                "id": iid,
                "status": "killed-budget",
                "note": str(be),
                "cost_usd": total_cost,
                "duration_sec": 0.0,
            }


# ---------------------------------------------------------------------------
# DBOS Workflow: squad_workflow
# ---------------------------------------------------------------------------

@DBOS.workflow(name="squad_workflow")
def squad_workflow(
    plan: dict,
    run_dir_str: str,
) -> dict:
    """Top-level squad DAG orchestrator with route queues, budget kills, and status events."""
    items = {it["id"]: it for it in plan["items"]}
    st: dict[str, dict] = {
        iid: {"status": "pending", "cost": 0.0, "duration": 0.0, "note": ""}
        for iid in items
    }
    budget_usd = float(plan.get("budget_usd", 100.0))
    spent_done = 0.0
    handles: dict[str, Any] = {}
    completed: dict[str, dict] = {}

    plan_context = {
        "model": plan.get("model", "google-antigravity/gemini-3.8-flash"),
        "budget_usd": budget_usd,
        "subscription": plan.get("subscription", True),
        "ledger_path": plan.get("ledger_path", str(Path(run_dir_str) / "spend.jsonl")),
    }

    # Helper to publish board status
    def update_board():
        board_data = {
            "items": st,
            "spent_usd": spent_done,
            "budget_usd": budget_usd,
            "completed_count": len(completed),
            "total_count": len(items),
            "timestamp": time.time(),
        }
        DBOS.set_event("board_status", board_data)

    update_board()

    while len(completed) < len(items):
        # 1. Check completed handles
        for iid in list(handles):
            h = handles[iid]
            wstatus = h.get_status().status
            if wstatus in ("SUCCESS", "ERROR", "CANCELLED"):
                try:
                    res = h.get_result()
                except Exception as exc:
                    res = {"id": iid, "status": "failed", "note": str(exc), "cost_usd": 0.0}
                completed[iid] = res
                del handles[iid]
                cost = res.get("cost_usd", 0.0)
                spent_done += cost
                st[iid].update({
                    "status": res.get("status", "done"),
                    "cost": cost,
                    "duration": res.get("duration_sec", 0.0),
                    "note": res.get("note", ""),
                })
                update_board()

        # 2. Cumulative budget kill check
        if spent_done >= budget_usd:
            for iid, s in st.items():
                if s["status"] == "pending":
                    s.update({"status": "killed-budget", "note": f"cumulative budget exceeded (${spent_done:.2f} >= ${budget_usd:.2f})"})
                    completed[iid] = {"id": iid, "status": "killed-budget", "cost_usd": 0.0}
            update_board()
            break

        # 3. Schedule ready items
        progress_made = False
        for iid, it in items.items():
            if st[iid]["status"] != "pending" or iid in handles:
                continue

            # Check dependencies
            needs = it.get("needs", [])
            needs_failed = any(st[n]["status"] in ("failed", "skipped", "killed-time", "killed-budget", "blocked") for n in needs)
            if needs_failed:
                st[iid].update({"status": "skipped", "note": f"dependency failed: {[n for n in needs if st[n]['status'] != 'done']}"})
                completed[iid] = {"id": iid, "status": "skipped", "cost_usd": 0.0}
                progress_made = True
                continue

            needs_done = all(st[n]["status"] == "done" for n in needs)
            if not needs_done:
                continue

            # Route queue selection
            route_name = it.get("route", plan.get("route", "route_default"))
            try:
                q = DBOS.retrieve_queue(route_name)
            except Exception:
                q = DBOS.retrieve_queue("route_default")

            # Enqueue child item workflow with deterministic ID
            st[iid]["status"] = "running"
            child_wf_id = it.get("workflow_id") or f"item-{iid}"
            with SetWorkflowID(child_wf_id):
                h = q.enqueue(item_workflow, it, run_dir_str, plan_context)
            handles[iid] = h
            progress_made = True

        if progress_made:
            update_board()

        # If no handles running and no progress possible, finish
        if not handles and not progress_made:
            for iid, s in st.items():
                if s["status"] == "pending":
                    s.update({"status": "skipped", "note": "unreachable dependencies or deadlocked"})
                    completed[iid] = {"id": iid, "status": "skipped", "cost_usd": 0.0}
            update_board()
            break

        time.sleep(0.5)

    update_board()
    return {
        "status": "done",
        "spent_usd": spent_done,
        "items": st,
    }


# ---------------------------------------------------------------------------
# Runner Manager Class
# ---------------------------------------------------------------------------

class DBOSSquadRunner:
    """Lifecycle manager for launching, monitoring, and recovering DBOS squad runs."""

    def __init__(
        self,
        db_path: Path,
        app_name: str = "dbos_squad",
        app_version: str = "v1.0.0",
    ):
        self.db_path = db_path
        self.app_name = app_name
        self.app_version = app_version
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_url = f"sqlite:///{self.db_path.resolve()}"
        self.config = DBOSConfig(
            name=self.app_name,
            application_version=self.app_version,
            system_database_url=self.db_url,
        )
        self.dbos = DBOS(config=self.config)

    def launch(self, routes: dict[str, dict] | None = None, max_parallel: int = 2) -> None:
        """Launch DBOS engine and register route queues."""
        DBOS.launch()
        # Default route queue
        DBOS.register_queue("route_default", concurrency=max_parallel)
        if routes:
            for rname, rcfg in routes.items():
                limiter = None
                if "rate_limit" in rcfg:
                    limiter = rcfg["rate_limit"]
                DBOS.register_queue(
                    rname,
                    concurrency=rcfg.get("concurrency", max_parallel),
                    worker_concurrency=rcfg.get("worker_concurrency"),
                    limiter=limiter,
                )

    def destroy(self) -> None:
        """Shutdown DBOS instance."""
        DBOS.destroy()

    def run_squad(self, plan: dict, run_dir: Path, workflow_id: str = "squad-run") -> dict:
        """Run squad workflow with given ID."""
        run_dir.mkdir(parents=True, exist_ok=True)
        with SetWorkflowID(workflow_id):
            return squad_workflow(plan, str(run_dir))

    def retrieve_workflow(self, workflow_id: str):
        return DBOS.retrieve_workflow(workflow_id)

    def send_gate_approval(self, workflow_id: str, item_id: str, approved: bool = True, reason: str = "") -> None:
        topic = f"gate:{item_id}"
        DBOS.send(workflow_id, {"approved": approved, "reason": reason, "item_id": item_id}, topic=topic)

    def get_board_status(self, workflow_id: str, timeout_seconds: float = 10.0) -> dict:
        return DBOS.get_event(workflow_id, "board_status", timeout_seconds=timeout_seconds)
