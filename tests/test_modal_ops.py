"""Modal auto-stop on queue drain, with faked command runners.

No test here ever executes the real ``modal app stop``: every teardown test
injects a fake runner. The live app is proved by the parent on a real batch
end.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evallab import modal_ops
from evallab.modal_ops import (
    MODAL_APP_NAME,
    TEARDOWN_EVENT,
    TEARDOWN_FILENAME,
    daytona_sandbox_count,
    is_selfhosted_model,
    remaining_selfhosted_specs,
    stop_selfhosted_app_if_drained,
)
from evallab.queue import DirectoryQueue, Executor, load_events
from evallab.schemas import (
    AutoRunRule,
    ExperimentSpec,
    QueueState,
    StandingApprovalsPolicy,
)

SELFHOSTED = "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
MOMENT = datetime(2026, 9, 30, tzinfo=UTC)


def make_spec(name: str, model: str | None, spec_id: str) -> ExperimentSpec:
    return ExperimentSpec(
        name=name,
        hypothesis="exercise the modal teardown",
        purpose="practice",
        task="library/tasks/event-summary",
        agent="terminus-2",
        model=model,
        environment="daytona",
        submitted_by="test-agent",
        est_cost_usd=0,
        spec_id=spec_id,
    )


def place(queue: DirectoryQueue, state: QueueState, spec: ExperimentSpec) -> Path:
    path = queue.state_dir(state) / f"terminus-2-{spec.spec_id}.json"
    path.write_text(spec.model_dump_json())
    return path


class FakeRunner:
    """Serve canned Modal outputs; record every invocation."""

    def __init__(self, outputs: dict[tuple[str, ...], subprocess.CompletedProcess[str]]) -> None:
        self.outputs = outputs
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        return self.outputs[tuple(argv)]


def completed(argv: list[str], *, returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def drain_runner() -> FakeRunner:
    return FakeRunner(
        {
            ("app", "stop", MODAL_APP_NAME): completed(["app", "stop", MODAL_APP_NAME]),
            ("app", "list", "--json"): completed(
                ["app", "list", "--json"],
                stdout=json.dumps(
                    [
                        {
                            "app_id": "ap-live",
                            "description": MODAL_APP_NAME,
                            "state": "stopped",
                            "tasks": "0",
                        },
                        {
                            "app_id": "ap-other",
                            "description": "unrelated-app",
                            "state": "deployed",
                            "tasks": "1",
                        },
                    ]
                ),
            ),
            ("container", "list", "--json"): completed(
                ["container", "list", "--json"],
                stdout=json.dumps(
                    [
                        {
                            "container_id": "ta-other",
                            "app_id": "ap-other",
                            "app_name": "unrelated-app",
                        }
                    ]
                ),
            ),
        }
    )


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(modal_ops, "daytona_sandbox_count", lambda: (7, None))


def test_selfhosted_model_matches_queue_selector_only() -> None:
    assert is_selfhosted_model(SELFHOSTED)
    assert not is_selfhosted_model("XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B")
    assert not is_selfhosted_model("openrouter-metered/XiaomiMiMo/MiMo-V2.6-Flash")
    assert not is_selfhosted_model(None)


def test_remaining_specs_cover_pending_approved_running(
    tmp_path: Path, sandbox: None
) -> None:
    queue = DirectoryQueue(tmp_path / "queue")
    waiting = make_spec("waiting-job", SELFHOSTED, "01AAAAAAAAAAAAAAAAAAAAAAAA")
    pending = make_spec("pending-job", SELFHOSTED, "01BBBBBBBBBBBBBBBBBBBBBB")
    approved = make_spec("approved-job", SELFHOSTED, "01CCCCCCCCCCCCCCCCCCCCCC")
    running = make_spec("running-job", SELFHOSTED, "01DDDDDDDDDDDDDDDDDDDDDD")
    done = make_spec("done-job", SELFHOSTED, "01EEEEEEEEEEEEEEEEEEEEEE")
    control = make_spec("control-job", None, "01FFFFFFFFFFFFFFFFFFFFFF")
    control = control.model_copy(update={"agent": "oracle"})
    place(queue, "waiting", waiting)
    place(queue, "pending", pending)
    place(queue, "approved", approved)
    place(queue, "running", running)
    place(queue, "done", done)
    place(queue, "approved", control)

    remaining = {spec.spec_id for _, spec in remaining_selfhosted_specs(queue)}
    assert remaining == {pending.spec_id, approved.spec_id, running.spec_id}
    assert waiting.spec_id not in remaining


def test_teardown_stops_and_records_evidence(tmp_path: Path, sandbox: None) -> None:
    queue = DirectoryQueue(tmp_path / "queue")
    spec = make_spec("drained-job", SELFHOSTED, "01AAAAAAAAAAAAAAAAAAAAAAAA")
    place(queue, "done", spec)
    job_dir = tmp_path / "runs" / spec.name
    job_dir.mkdir(parents=True)
    runner = drain_runner()

    record = stop_selfhosted_app_if_drained(
        queue, tmp_path, [spec], runner=runner, now=MOMENT
    )

    assert record is not None
    assert record["stopped"] is True
    assert record["app"] == MODAL_APP_NAME
    assert record["app_state"] == "stopped"
    assert record["container_count"] == 0
    assert record["daytona_sandboxes"] == {"count": 7, "reason": None}
    assert record["completed_spec_ids"] == [spec.spec_id]
    assert runner.calls[0] == ["app", "stop", MODAL_APP_NAME]
    assert {tuple(call) for call in runner.calls} == {
        ("app", "stop", MODAL_APP_NAME),
        ("app", "list", "--json"),
        ("container", "list", "--json"),
    }
    stored = json.loads((job_dir / TEARDOWN_FILENAME).read_text())
    assert stored["stopped"] is True
    assert stored["completed_spec_ids"] == [spec.spec_id]
    events = [event for event in load_events(queue.events_path) if event.event == TEARDOWN_EVENT]
    assert len(events) == 1
    assert events[0].spec_id == spec.spec_id
    assert events[0].reason_code == "modal_app_stopped"
    assert events[0].actor == "executor"


def test_teardown_skips_when_selfhosted_work_remains(tmp_path: Path, sandbox: None) -> None:
    queue = DirectoryQueue(tmp_path / "queue")
    finished = make_spec("finished-job", SELFHOSTED, "01AAAAAAAAAAAAAAAAAAAAAAAA")
    leftover = make_spec("leftover-job", SELFHOSTED, "01BBBBBBBBBBBBBBBBBBBBBB")
    place(queue, "done", finished)
    place(queue, "approved", leftover)
    runner = drain_runner()

    record = stop_selfhosted_app_if_drained(
        queue, tmp_path, [finished], runner=runner, now=MOMENT
    )

    assert record is not None
    assert record["stopped"] is False
    assert record["reason"] == "queue-not-drained"
    assert record["remaining_spec_ids"] == [leftover.spec_id]
    assert runner.calls == []
    assert load_events(queue.events_path) == []


def test_teardown_ignores_specs_that_did_not_complete(tmp_path: Path, sandbox: None) -> None:
    queue = DirectoryQueue(tmp_path / "queue")
    spec = make_spec("still-approved", SELFHOSTED, "01AAAAAAAAAAAAAAAAAAAAAAAA")
    place(queue, "approved", spec)
    runner = drain_runner()

    assert stop_selfhosted_app_if_drained(queue, tmp_path, [spec], runner=runner) is None
    assert runner.calls == []


def test_teardown_ignores_non_selfhosted_completions(tmp_path: Path, sandbox: None) -> None:
    queue = DirectoryQueue(tmp_path / "queue")
    spec = make_spec("oracle-job", None, "01AAAAAAAAAAAAAAAAAAAAAAAA")
    spec = spec.model_copy(update={"agent": "oracle"})
    place(queue, "done", spec)
    runner = drain_runner()

    assert stop_selfhosted_app_if_drained(queue, tmp_path, [spec], runner=runner) is None
    assert runner.calls == []


def test_teardown_records_stop_failure_without_raising(tmp_path: Path, sandbox: None) -> None:
    queue = DirectoryQueue(tmp_path / "queue")
    spec = make_spec("drained-job", SELFHOSTED, "01AAAAAAAAAAAAAAAAAAAAAAAA")
    place(queue, "failed", spec)
    job_dir = tmp_path / "runs" / spec.name
    job_dir.mkdir(parents=True)
    runner = FakeRunner(
        {
            ("app", "stop", MODAL_APP_NAME): completed(
                ["app", "stop", MODAL_APP_NAME], returncode=1, stderr="boom"
            ),
            ("app", "list", "--json"): completed(
                ["app", "list", "--json"],
                stdout=json.dumps([{"description": MODAL_APP_NAME, "state": "deployed"}]),
            ),
            ("container", "list", "--json"): completed(
                ["container", "list", "--json"], stdout="not json"
            ),
        }
    )

    record = stop_selfhosted_app_if_drained(
        queue, tmp_path, [spec], runner=runner, now=MOMENT
    )

    assert record is not None
    assert record["stopped"] is False
    assert record["reason"] == "modal-stop-failed"
    assert record["stop_returncode"] == 1
    assert "boom" in record["stop_stderr_tail"]
    assert record["app_state"] == "deployed"
    assert record["container_count"] is None
    events = [event for event in load_events(queue.events_path) if event.event == TEARDOWN_EVENT]
    assert [event.reason_code for event in events] == ["modal_stop_failed"]


def test_teardown_records_runner_crash_without_raising(tmp_path: Path, sandbox: None) -> None:
    queue = DirectoryQueue(tmp_path / "queue")
    spec = make_spec("drained-job", SELFHOSTED, "01AAAAAAAAAAAAAAAAAAAAAAAA")
    place(queue, "done", spec)
    (tmp_path / "runs" / spec.name).mkdir(parents=True)

    def crash(argv: list[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("no modal on PATH")

    record = stop_selfhosted_app_if_drained(queue, tmp_path, [spec], runner=crash, now=MOMENT)

    assert record is not None
    assert record["stopped"] is False
    assert "FileNotFoundError" in str(record["reason"])


def test_daytona_helper_reports_missing_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "daytona", None)
    assert daytona_sandbox_count() == (None, "daytona-sdk-not-installed")


def test_daytona_helper_counts_sandboxes(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    stub = types.ModuleType("daytona")

    class Client:
        def list(self) -> list[int]:
            return [1, 2, 3]

    stub.Daytona = Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "daytona", stub)
    assert daytona_sandbox_count() == (3, None)


def test_daytona_helper_reports_listing_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    stub = types.ModuleType("daytona")

    class Client:
        def list(self) -> list[int]:
            raise RuntimeError("Invalid credentials")

    stub.Daytona = Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "daytona", stub)
    count, reason = daytona_sandbox_count()
    assert count is None
    assert reason == "RuntimeError: Invalid credentials"


def make_executor(root: Path, **overrides) -> Executor:
    policy = StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20,
        per_job_cost_ceiling_usd=3,
        quiet_failure_rule=3,
        auto_run=[AutoRunRule(name="local-controls", agents=["oracle", "nop"])],
        escalate_to_human=["anything_exceeding_ceilings"],
    )
    settings: dict = {
        "repo_root": root,
        "queue": DirectoryQueue(root / "queue"),
        "policy": policy,
        "runner": lambda request: request.jobs_dir / request.name,
        "ingester": lambda path: None,
        "spent_today": lambda: 0,
        "consecutive_harness_failures": lambda: 0,
        "credential_probe": lambda: frozenset({"claude_oauth", "codex_auth"}),
        "sleeper": lambda _seconds: None,
    }
    settings.update(overrides)
    return Executor(**settings)


def test_tick_without_work_never_calls_teardown_hook(tmp_path: Path) -> None:
    calls: list = []
    service = make_executor(tmp_path, modal_teardown=lambda *args: calls.append(args) or None)
    assert service.tick() == 0
    assert calls == []


def test_maybe_stop_reports_stop_and_skips_quietly(tmp_path: Path) -> None:
    progress: list[str] = []
    service = make_executor(tmp_path, progress=progress.append)
    spec = make_spec("drained-job", SELFHOSTED, "01AAAAAAAAAAAAAAAAAAAAAAAA")
    record = {
        "app": MODAL_APP_NAME,
        "stopped": True,
        "reason": None,
        "app_state": "stopped",
        "container_count": 0,
    }
    service._modal_teardown = lambda queue, root, candidates: record  # type: ignore[method-assign]
    service._maybe_stop_selfhosted_app({spec.spec_id: spec}, [])
    assert progress == ["modal teardown: stopped evallab-mimo-v26-9b (state=stopped, containers=0)"]


def test_maybe_stop_swallows_hook_failure(tmp_path: Path) -> None:
    progress: list[str] = []
    service = make_executor(tmp_path, progress=progress.append)
    spec = make_spec("drained-job", SELFHOSTED, "01AAAAAAAAAAAAAAAAAAAAAAAA")

    def fail(queue, root, candidates):
        raise RuntimeError("modal unreachable")

    service._modal_teardown = fail  # type: ignore[method-assign]
    service._maybe_stop_selfhosted_app({spec.spec_id: spec}, [])
    assert progress == ["modal teardown skipped: RuntimeError"]


def test_maybe_stop_stays_quiet_when_queue_not_drained(tmp_path: Path) -> None:
    progress: list[str] = []
    service = make_executor(tmp_path, progress=progress.append)
    spec = make_spec("drained-job", SELFHOSTED, "01AAAAAAAAAAAAAAAAAAAAAAAA")
    record = {"app": MODAL_APP_NAME, "stopped": False, "reason": "queue-not-drained"}
    service._modal_teardown = lambda queue, root, candidates: record  # type: ignore[method-assign]
    service._maybe_stop_selfhosted_app({spec.spec_id: spec}, [])
    assert progress == []
