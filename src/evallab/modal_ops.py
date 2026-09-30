"""Self-hosted Modal lifecycle: auto-stop on queue drain plus read-only probes.

The MiMo self-hosted route (``selfhosted/...``, proxy provider
``mimo_selfhosted``) bills by Modal GPU-second while the app
``evallab-mimo-v26-9b`` (see ``tools/modal-mimo-serve/serve.py`` ``APP_NAME``)
is deployed. When the queue no longer holds self-hosted work the app should
be stopped so an idle deployment does not keep billing.

The stop itself is deliberately mutating, so it only runs through
:func:`stop_selfhosted_app_if_drained`, which the queue tick calls after a
dispatch round. Everything else here is read-only. All Modal invocations go
through an injected command runner, so tests use fakes and no test ever
touches the live app.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from evallab.execution_contracts import (
    MIMO_SELFHOSTED_NATIVE_MODEL,
    is_mimo_selfhosted_model,
    new_ulid,
)
from evallab.schemas import ExperimentSpec, QueueEvent, QueueState

if TYPE_CHECKING:
    from evallab.queue import DirectoryQueue

#: Modal app serving the self-hosted MiMo weights. Mirrors
#: ``tools/modal-mimo-serve/serve.py::APP_NAME``; that file owns the value,
#: this constant only names what the teardown stops.
MODAL_APP_NAME = "evallab-mimo-v26-9b"

#: Queue states that still need the self-hosted server.
DRAIN_WATCH_STATES: tuple[QueueState, ...] = ("pending", "approved", "running")

#: States that mean a spec will never need the server again.
TERMINAL_STATES = ("done", "failed")

#: Evidence filename written into each draining job directory.
TEARDOWN_FILENAME = "modal-teardown.json"

#: Queue event recorded per drained spec.
TEARDOWN_EVENT = "modal_teardown"

#: Runs ``modal <argv>`` and returns the completed process. Raising on
#: transport failure is allowed; the teardown records it instead of crashing
#: the tick.
ModalCommandRunner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]

#: Tick hook: decide whether the queue drained and stop the app if so.
#: Returns the teardown record, or ``None`` when there was nothing to stop.
ModalTeardownHook = Callable[
    ["DirectoryQueue", Path, list[ExperimentSpec]], dict[str, Any] | None
]


def default_runner(repo_root: Path) -> ModalCommandRunner:
    """Run Modal through the pinned serve project from the repository root."""

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "uv",
                "run",
                "--project",
                "tools/modal-mimo-serve",
                "--locked",
                "modal",
                *argv,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
            cwd=repo_root,
        )

    return run


def is_selfhosted_model(model: str | None) -> bool:
    """Whether a queue spec model needs the self-hosted Modal server."""
    return is_mimo_selfhosted_model(model)


def remaining_selfhosted_specs(
    queue: DirectoryQueue,
) -> list[tuple[Path, ExperimentSpec]]:
    """Self-hosted specs still in a state that needs the server."""
    remaining: list[tuple[Path, ExperimentSpec]] = []
    for state in DRAIN_WATCH_STATES:
        for path, spec in queue.list_specs(state):
            if is_selfhosted_model(spec.model):
                remaining.append((path, spec))
    return remaining


def _spec_state(queue: DirectoryQueue, spec_id: str) -> str | None:
    try:
        return queue.locate(spec_id).parent.name
    except (ValueError, OSError):
        return None


def _run_capture(
    runner: ModalCommandRunner, argv: list[str]
) -> dict[str, Any]:
    try:
        completed = runner(argv)
    except Exception as exc:  # Transport failure is evidence, not a crash.
        return {
            "argv": argv,
            "ok": False,
            "returncode": None,
            "reason": f"{type(exc).__name__}: {exc}",
        }
    return {
        "argv": argv,
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stderr_tail": completed.stderr[-500:] if completed.stderr else "",
        "stdout": completed.stdout,
    }


def _app_state_for(entries: Any, app_name: str) -> str | None:
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, Mapping) and entry.get("description") == app_name:
            state = entry.get("state")
            return str(state) if state is not None else None
    return None


def _container_count_for(entries: Any, app_name: str) -> int | None:
    if not isinstance(entries, list):
        return None
    count = 0
    for entry in entries:
        if isinstance(entry, Mapping) and entry.get("app_name") == app_name:
            count += 1
    return count


def daytona_sandbox_count() -> tuple[int | None, str | None]:
    """Read-only sandbox count for reconcile/teardown receipts.

    Returns ``(count, None)`` on success, ``(None, reason)`` otherwise.
    Never raises and never prints credentials; only the count or a short
    error summary leaves this function.
    """
    try:
        from daytona import Daytona  # ty: ignore[unresolved-import]
    except ImportError:
        return None, "daytona-sdk-not-installed"
    try:
        return len(list(Daytona().list())), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {str(exc)[:200]}"


def stop_selfhosted_app_if_drained(
    queue: DirectoryQueue,
    repo_root: Path,
    candidates: Sequence[ExperimentSpec],
    *,
    runner: ModalCommandRunner | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Stop the Modal app when self-hosted queue work just drained.

    ``candidates`` are specs from the dispatch round that just finished
    (approved specs dispatched this tick plus specs that were running when
    the tick started). Only candidates that reached ``done``/``failed``
    count as drained, and the stop runs only when no ``pending``/``approved``/
    ``running`` self-hosted spec remains. Returns the teardown record, or
    ``None`` when no self-hosted spec completed this round.
    """
    moment = now or datetime.now(UTC)
    completed = [
        spec
        for spec in candidates
        if spec.spec_id is not None
        and is_selfhosted_model(spec.model)
        and _spec_state(queue, str(spec.spec_id)) in TERMINAL_STATES
    ]
    if not completed:
        return None
    remaining = remaining_selfhosted_specs(queue)
    if remaining:
        return {
            "app": MODAL_APP_NAME,
            "stopped": False,
            "reason": "queue-not-drained",
            "remaining_spec_ids": sorted(str(spec.spec_id) for _, spec in remaining),
            "completed_spec_ids": sorted(str(spec.spec_id) for spec in completed),
            "recorded_at": moment.isoformat(),
        }
    active = runner or default_runner(repo_root)
    stop = _run_capture(active, ["app", "stop", MODAL_APP_NAME])
    app_list = _run_capture(active, ["app", "list", "--json"])
    containers = _run_capture(active, ["container", "list", "--json"])
    app_entries: Any = None
    container_entries: Any = None
    if app_list.get("ok"):
        try:
            app_entries = json.loads(str(app_list["stdout"]))
        except (json.JSONDecodeError, TypeError, ValueError):
            app_entries = None
    if containers.get("ok"):
        try:
            container_entries = json.loads(str(containers["stdout"]))
        except (json.JSONDecodeError, TypeError, ValueError):
            container_entries = None
    sandbox_count, sandbox_reason = daytona_sandbox_count()
    stopped = bool(stop.get("ok"))
    record: dict[str, Any] = {
        "app": MODAL_APP_NAME,
        "stopped": stopped,
        "reason": None if stopped else str(stop.get("reason") or "modal-stop-failed"),
        "stop_returncode": stop.get("returncode"),
        "stop_stderr_tail": stop.get("stderr_tail", ""),
        "app_state": _app_state_for(app_entries, MODAL_APP_NAME),
        "container_count": _container_count_for(container_entries, MODAL_APP_NAME),
        "daytona_sandboxes": {"count": sandbox_count, "reason": sandbox_reason},
        "completed_spec_ids": sorted(str(spec.spec_id) for spec in completed),
        "recorded_at": moment.isoformat(),
    }
    for spec in completed:
        job_dir = repo_root / str(spec.jobs_dir) / spec.name
        if job_dir.is_dir():
            (job_dir / TEARDOWN_FILENAME).write_text(
                json.dumps(record, indent=2, sort_keys=True) + "\n"
            )
        else:
            record.setdefault("job_dirs_missing", []).append(job_dir.as_posix())
        queue.append_event(
            QueueEvent(
                event_id=new_ulid(),
                spec_id=str(spec.spec_id),
                occurred_at=moment,
                event=TEARDOWN_EVENT,
                actor="executor",
                reason_code="modal_app_stopped" if stopped else "modal_stop_failed",
                job_name=spec.name,
            )
        )
    return record


def describe_teardown(record: Mapping[str, Any]) -> str:
    """One-line tick progress summary for a teardown record."""
    if not record.get("stopped", False):
        return f"modal teardown skipped: {record.get('reason')}"
    return (
        f"modal teardown: stopped {record.get('app')} "
        f"(state={record.get('app_state')}, "
        f"containers={record.get('container_count')})"
    )


#: Catalog match for self-hosted MiMo trials: the queue selector carries a
#: ``selfhosted/`` prefix, but ingested trials record the native model id.
SELFHOSTED_CATALOG_MODELS = (MIMO_SELFHOSTED_NATIVE_MODEL, "selfhosted/")
