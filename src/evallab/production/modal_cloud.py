"""Authenticated Modal production control plane, durable stores, and CPU workers.

Deploy with ``modal deploy --strategy recreate src/evallab/production/modal_cloud.py``
after importing SQLite backups and CAS objects into ``evallab-cloud-store``. One cloud actor owns
all SQLite connections: independent Modal containers must never write snapshots
of the same Volume-backed SQLite database. The remote worker entrypoint forwards
to that actor; visiting the dashboard never dispatches work or calls a model.
Recreate deployments quiesce the old actor before its replacement opens SQLite.
"""

from __future__ import annotations

import hmac
import json
import math
import os
import re
import sqlite3
import threading
from collections.abc import Callable, Mapping
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Any, Literal

import modal
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from evallab.execution_backend import ExecutionBackendConfig
from evallab.production.cloud_trace_db import CloudTraceDatabase, S3CompatibleCAS
from evallab.production.control_plane import create_app
from evallab.production.corpus_queue import ContinuousWorkQueue, CorpusRegistry
from evallab.production.monitor_worker import MisalignmentLiveWorker
from evallab.production.outbox_relay import LocalJetStreamAdapter, OutboxRelay
from evallab.production.seeker_worker import RewardSeekerLiveWorker
from evallab.production.supervisor import ProductionSupervisor
from evallab.production.tq_worker import TaskQualityLiveWorker

APP_NAME = "evallab-cloud-production"
VOLUME_NAME = "evallab-cloud-store"
DATA_ROOT = Path("/data")
CLOUD_REPO_ROOT = Path("/opt/eval-lab")
BILLING_TAG = "cloud-offload-20261024"
COOKIE_NAME = "evallab_cloud_session"


def _repository_root(source_file: str, *, local: bool) -> Path:
    # Modal imports file-based deployments as /root/modal_cloud.py, not from
    # their original package depth. Assets have a fixed cloud mount instead.
    return Path(source_file).resolve().parents[3] if local else CLOUD_REPO_ROOT


REPO_ROOT = _repository_root(__file__, local=modal.is_local())


# The fragment is never sent to Modal's HTTP logs. Exchange it for an HTTP-only
# cookie, erase it from browser history, and then open the protected dashboard.
_LOGIN = """<!doctype html><meta charset="utf-8"><title>Eval Lab cloud login</title>
<p id="status">Authenticating to your private Eval Lab cloud…</p><script>
(async () => {
 const token = new URLSearchParams(location.hash.slice(1)).get('token');
 history.replaceState(null, '', location.pathname);
 if (!token) { document.getElementById('status').textContent = 'Open with lab cloud to authenticate.'; return; }
 const response = await fetch('/auth', {method: 'POST', credentials: 'same-origin',
  headers: {'Content-Type': 'application/json'}, body: JSON.stringify({token})});
 if (response.ok) location.replace('/');
 else document.getElementById('status').textContent = 'Authentication failed. Run lab cloud again.';
})();
</script>"""


class _LoginRequest(BaseModel):
    token: str


def _matches(query: str, record: Mapping[str, Any]) -> bool:
    if not query.strip():
        return True
    terms = re.findall(r"\w+", query, flags=re.UNICODE)
    text = json.dumps(record, ensure_ascii=False).casefold()
    return bool(terms) and all(term.casefold() in text for term in terms)


def _issue_matches(
    issue: Mapping[str, Any], *, direction: str | None, defect_class: str | None,
    only_verified: bool,
) -> bool:
    return (
        (direction is None or issue["direction"] == direction)
        and (defect_class is None or issue["defect_class"] == defect_class)
        and (not only_verified or issue["status"] == "confirmed")
    )


class CloudRuntime:
    """Single-process owner of Volume-backed state; usable offline in unit tests."""

    def __init__(
        self, data_root: Path, repo_root: Path, token: str,
        *, commit: Callable[[], None] | None = None,
    ) -> None:
        if not token.strip():
            raise ValueError("EVALLAB_CLOUD_TOKEN is required; public trace access is forbidden")
        self.data_root = Path(data_root)
        self.repo_root = Path(repo_root)
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.cas_root = self.data_root / "cas"
        self.cas_root.mkdir(exist_ok=True)
        self._commit = commit or (lambda: None)
        self._step_lock = threading.Lock()
        self._closed = False
        self.registry = CorpusRegistry(self.repo_root)
        with ExitStack() as cleanup:
            self.queue = ContinuousWorkQueue(self.data_root / "prod.sqlite", registry=self.registry)
            cleanup.callback(self.queue.close)
            self.database = CloudTraceDatabase(self.data_root / "cloud-trace-db.sqlite")
            cleanup.callback(self.database.close)
            self.cas = S3CompatibleCAS(local_directory=self.cas_root, bucket="")
            self.app = create_app(self.queue, operator_tokens={token: "Peter"})
            self._add_routes(token)
            cleanup.pop_all()

    def _query(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        path = self.data_root / "cloud-trace-db.sqlite"
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(sql, parameters)]

    def _tasks(self, task_id: str | None, query: str) -> list[dict[str, Any]]:
        sql = "SELECT * FROM corpus_tasks"
        parameters: tuple[Any, ...] = ()
        if task_id is not None:
            sql += " WHERE task_id = ?"
            parameters = (task_id,)
        rows = self._query(sql + " ORDER BY task_id", parameters)
        for row in rows:
            row["metadata_json"] = json.loads(row["metadata_json"])
        return [row for row in rows if _matches(query, row)]

    def _add_routes(self, token: str) -> None:
        expected = token.encode()

        def authorized(candidate: str) -> bool:
            return hmac.compare_digest(candidate.encode(), expected)

        @self.app.middleware("http")
        async def authenticate(request: Request, call_next):
            if request.url.path != "/auth":
                bearer = request.headers.get("authorization", "")
                bearer = bearer.removeprefix("Bearer ") if bearer.startswith("Bearer ") else ""
                cookie = request.cookies.get(COOKIE_NAME, "")
                if not authorized(bearer) and not authorized(cookie):
                    return JSONResponse(
                        {"detail": "Unauthorized"}, status_code=401,
                        headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
                    )
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            return response

        @self.app.get("/auth", response_class=HTMLResponse)
        def login_page():
            return HTMLResponse(_LOGIN, headers={
                "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
                "X-Content-Type-Options": "nosniff",
            })

        @self.app.post("/auth")
        def login(body: _LoginRequest):
            if not authorized(body.token):
                raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})
            response = JSONResponse({"authenticated": True})
            response.set_cookie(
                COOKIE_NAME, token, secure=True, httponly=True, samesite="lax", path="/",
            )
            return response

        @self.app.get("/api/cloud-db/stats")
        def stats():
            counts = {
                table: self._query(f"SELECT COUNT(*) AS count FROM {table}")[0]["count"]
                for table in ("corpus_tasks", "task_issues", "agent_traces", "trace_steps")
            }
            return {
                "backend": "modal", "database": str(self.data_root / "cloud-trace-db.sqlite"),
                "queue_database": str(self.data_root / "prod.sqlite"),
                "cas_directory": str(self.cas_root),
                "cas_objects": sum(len(files) for _, _, files in os.walk(self.cas_root)),
                **counts,
                "notice": "Statuses, outcomes and receipt flags are retained source claims, not new verification.",
            }

        @self.app.get("/api/cloud-db/tasks")
        def tasks(q: str = "", task_id: str | None = None):
            return {"tasks": self._tasks(task_id, q)}

        @self.app.get("/api/cloud-db/issues")
        def issues(
            q: str = "", task_id: str | None = None,
            direction: Literal["wrong_pass", "unfair_fail"] | None = None,
            defect_class: str | None = None, only_verified: bool = False,
        ):
            rows = self.database.query_task_issues(
                task_id, direction=direction, confirmed_only=only_verified,
            )
            return {"issues": [row for row in rows if (
                (defect_class is None or row["defect_class"] == defect_class) and _matches(q, row)
            )]}

        @self.app.get("/api/cloud-db/traces")
        def traces(
            q: str = "", task_id: str | None = None, defect_class: str | None = None,
            tool_name: str | None = None, only_verified: bool = False,
            direction: Literal["wrong_pass", "unfair_fail"] | None = None,
        ):
            rows = self.database.search_traces(
                q, defect_class=defect_class, tool_name=tool_name, only_verified=only_verified,
            )
            return {"traces": [row for row in rows if (
                (task_id is None or row["task_id"] == task_id)
                and (not (direction or defect_class) or any(
                    _issue_matches(issue, direction=direction, defect_class=defect_class,
                                   only_verified=only_verified)
                    for issue in row["issues"]
                ))
            )]}

    def run_supervisor_step(self, approval: str, max_usd: float) -> dict[str, Any]:
        """One explicitly approved cloud step, never a laptop Docker fallback."""
        if not approval.strip():
            raise ValueError("explicit cloud approval is required")
        if isinstance(max_usd, bool) or not math.isfinite(max_usd) or max_usd <= 0:
            raise ValueError("max_usd must be finite and positive")
        with self._step_lock, ExitStack() as cleanup:
            cleanup.callback(self._commit)
            config = ExecutionBackendConfig(
                backend="modal", allow_local_docker=False,
                modal_app_name=f"{BILLING_TAG}-workers",
                approval=approval, max_usd=max_usd,
                spend_ledger=self.data_root / "cloud-spend.json", billing_tag=BILLING_TAG,
            )
            monitor = MisalignmentLiveWorker(emit=self.queue.append_event, registry=self.registry)
            cleanup.callback(monitor.close)
            tq = TaskQualityLiveWorker(self.queue, repo_root=self.repo_root, execution_config=config)
            seeker = RewardSeekerLiveWorker(
                self.queue, repo_root=self.repo_root,
                work_root=self.data_root / "evidence/seeker", execution_config=config,
                allow_paid_lanes=False,
            )
            publisher = LocalJetStreamAdapter(self.data_root / "outbox.sqlite")
            cleanup.callback(publisher.close)
            relay = OutboxRelay(self.queue, publisher)
            cleanup.callback(relay.close)
            supervisor = ProductionSupervisor(
                self.queue, monitor=monitor, task_quality=tq, seeker=seeker, relay=relay,
                repo_root=self.repo_root, supervisor_id="modal-cloud-production",
            )
            cleanup.callback(supervisor.close)
            report = supervisor.run_step()
            return {"backend": "modal", "allow_local_docker": False, **report}

    def close(self) -> None:
        if self._closed:
            return
        with self._step_lock, ExitStack() as cleanup:
            cleanup.callback(self._commit)
            cleanup.callback(self.database.close)
            cleanup.callback(self.queue.close)
            self._closed = True


app = modal.App(APP_NAME)
store_volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)
image = (
    modal.Image.debian_slim(python_version="3.12")
    .uv_sync(str(REPO_ROOT), extras=["laminar"], uv_version="0.9.24")
    .env({
        "PYTHONPATH": "/opt/eval-lab/src",
        "EVALLAB_DISABLE_LOCAL_DOCKER": "1",
        "EVALLAB_DERIVED_ROOT": "/data/derived",
        "EVALLAB_SEEKER_LIVE_MODEL": "0",
    })
    .workdir(str(CLOUD_REPO_ROOT))
    .add_local_dir(REPO_ROOT / "src", "/opt/eval-lab/src", ignore=["**/__pycache__", "**/*.pyc"])
    .add_local_dir(REPO_ROOT / "library", "/opt/eval-lab/library", ignore=["**/__pycache__", "**/*.pyc"])
    .add_local_dir(REPO_ROOT / "research", "/opt/eval-lab/research", ignore=["**/__pycache__", "**/*.pyc"])
    .add_local_dir(REPO_ROOT / "sql", "/opt/eval-lab/sql")
    .add_local_file(REPO_ROOT / "pyproject.toml", "/opt/eval-lab/pyproject.toml")
)


@app.cls(
    image=image, volumes={str(DATA_ROOT): store_volume},
    secrets=[modal.Secret.from_name("evallab-cloud-production-auth")],
    cpu=1, memory=2048, max_containers=1, scaledown_window=30, timeout=3600,
)
@modal.concurrent(max_inputs=100)
class CloudProduction:
    """One owner for all Volume-backed SQLite state, including worker writes."""

    @modal.enter()
    def start(self):
        # Preserve legacy absolute source references without rewriting immutable
        # queue events. Only explicitly recorded, owned source roots are aliased.
        aliases = DATA_ROOT / "legacy-repo-roots.json"
        if aliases.is_file():
            for value in json.loads(aliases.read_text()):
                path = Path(value)
                if not path.is_absolute() or not (
                    str(path).startswith("/Users/petermakhnatch/.local/state/evallab-prod-live/releases/")
                    or str(path) == "/Users/petermakhnatch/Developer/eval-lab"
                ):
                    raise ValueError("unexpected legacy repository root")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(CLOUD_REPO_ROOT, target_is_directory=True)
        # Fresh verification artifacts survive scale-down alongside the queues.
        (DATA_ROOT / "runs").mkdir(exist_ok=True)
        (CLOUD_REPO_ROOT / "runs").symlink_to(DATA_ROOT / "runs", target_is_directory=True)
        self.runtime = CloudRuntime(
            DATA_ROOT, CLOUD_REPO_ROOT, os.environ["EVALLAB_CLOUD_TOKEN"],
            commit=store_volume.commit,
        )

    @modal.asgi_app()
    def web(self) -> FastAPI:
        return self.runtime.app

    @modal.method()
    def supervisor_step(self, approval: str, max_usd: float) -> dict[str, Any]:
        return self.runtime.run_supervisor_step(approval, max_usd)

    @modal.exit()
    def stop(self):
        self.runtime.close()


@app.function(image=image, cpu=0.25, memory=256, timeout=3600, max_containers=1)
def run_cloud_supervisor_step(approval: str, max_usd: float) -> dict[str, Any]:
    """Authenticated Modal RPC; all jobs and CPU checks run in the cloud owner."""
    return CloudProduction().supervisor_step.remote(approval, max_usd)
