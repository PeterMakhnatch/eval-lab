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
    DaytonaSandboxCounts,
    daytona_sandbox_counts,
    describe_teardown,
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


def completed(
    argv: list[str], *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def drain_runner() -> FakeRunner:
    return FakeRunner(
        {
            ("app", "stop", "--yes", MODAL_APP_NAME): completed(
                ["app", "stop", "--yes", MODAL_APP_NAME]
            ),
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
    monkeypatch.setattr(
        modal_ops,
        "daytona_sandbox_counts",
        lambda **kwargs: DaytonaSandboxCounts(total=9, harbor_managed=7, matched=None, reason=None),
    )


def test_selfhosted_model_matches_queue_selector_only() -> None:
    assert is_selfhosted_model(SELFHOSTED)
    assert not is_selfhosted_model("XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B")
    assert not is_selfhosted_model("openrouter-metered/XiaomiMiMo/MiMo-V2.6-Flash")
    assert not is_selfhosted_model(None)


def test_remaining_specs_cover_pending_approved_running(tmp_path: Path, sandbox: None) -> None:
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

    record = stop_selfhosted_app_if_drained(queue, tmp_path, [spec], runner=runner, now=MOMENT)

    assert record is not None
    assert record["stopped"] is True
    assert record["app"] == MODAL_APP_NAME
    assert record["app_state"] == "stopped"
    assert record["container_count"] == 0
    assert record["polls"] == 1
    assert record["daytona_sandboxes"] == {
        "total": 9,
        "harbor_managed": 7,
        "matched": None,
        "reason": None,
    }
    assert record["completed_spec_ids"] == [spec.spec_id]
    assert runner.calls[0] == ["app", "stop", "--yes", MODAL_APP_NAME]
    assert {tuple(call) for call in runner.calls} == {
        ("app", "stop", "--yes", MODAL_APP_NAME),
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

    record = stop_selfhosted_app_if_drained(queue, tmp_path, [finished], runner=runner, now=MOMENT)

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
            ("app", "stop", "--yes", MODAL_APP_NAME): completed(
                ["app", "stop", "--yes", MODAL_APP_NAME], returncode=1, stderr="boom"
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

    record = stop_selfhosted_app_if_drained(queue, tmp_path, [spec], runner=runner, now=MOMENT)

    assert record is not None
    assert record["stopped"] is False
    assert record["reason"] == "modal-stop-failed"
    assert record["stop_returncode"] == 1
    assert "boom" in record["stop_stderr_tail"]
    assert record["app_state"] is None
    assert record["container_count"] is None
    assert record["polls"] == 0
    events = [event for event in load_events(queue.events_path) if event.event == TEARDOWN_EVENT]
    assert [event.reason_code for event in events] == ["modal_stop_failed"]


def test_teardown_confirms_without_a_terminal(tmp_path: Path, sandbox: None) -> None:
    """Modal 1.5.5 aborts `app stop` without --yes when stdin is not a TTY."""
    queue = DirectoryQueue(tmp_path / "queue")
    spec = make_spec("drained-job", SELFHOSTED, "01AAAAAAAAAAAAAAAAAAAAAAAA")
    place(queue, "done", spec)
    (tmp_path / "runs" / spec.name).mkdir(parents=True)

    def reject_unconfirmed(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["app", "stop"] and "--yes" not in argv:
            return completed(
                argv,
                returncode=1,
                stderr="Aborted: no interactive terminal detected. Rerun with --yes (-y).",
            )
        if argv == ["app", "list", "--json"]:
            return completed(
                argv,
                stdout=json.dumps([{"description": MODAL_APP_NAME, "state": "stopped"}]),
            )
        return completed(argv, stdout="[]")

    record = stop_selfhosted_app_if_drained(
        queue, tmp_path, [spec], runner=reject_unconfirmed, now=MOMENT
    )

    assert record is not None
    assert record["stopped"] is True
    assert record["reason"] is None
    assert record["app_state"] == "stopped"


def test_teardown_fails_when_app_stays_deployed(tmp_path: Path, sandbox: None) -> None:
    """A zero exit is not success when the app never reaches a stopped state."""
    queue = DirectoryQueue(tmp_path / "queue")
    spec = make_spec("drained-job", SELFHOSTED, "01AAAAAAAAAAAAAAAAAAAAAAAA")
    place(queue, "done", spec)
    job_dir = tmp_path / "runs" / spec.name
    job_dir.mkdir(parents=True)
    runner = FakeRunner(
        {
            ("app", "stop", "--yes", MODAL_APP_NAME): completed(
                ["app", "stop", "--yes", MODAL_APP_NAME]
            ),
            ("app", "list", "--json"): completed(
                ["app", "list", "--json"],
                stdout=json.dumps([{"description": MODAL_APP_NAME, "state": "deployed"}]),
            ),
            ("container", "list", "--json"): completed(
                ["container", "list", "--json"], stdout="[]"
            ),
        }
    )

    record = stop_selfhosted_app_if_drained(
        queue, tmp_path, [spec], runner=runner, now=MOMENT, poll_timeout_seconds=0
    )

    assert record is not None
    assert record["stopped"] is False
    assert record["reason"] == "modal-app-not-stopped: state=deployed after 1 poll(s)"
    stored = json.loads((job_dir / TEARDOWN_FILENAME).read_text())
    assert stored["stopped"] is False
    events = [event for event in load_events(queue.events_path) if event.event == TEARDOWN_EVENT]
    assert [event.reason_code for event in events] == ["modal_stop_failed"]


def test_teardown_waits_through_stopping_state(tmp_path: Path, sandbox: None) -> None:
    """`stopping...` is transitional: poll until `stopped` and containers drain."""
    queue = DirectoryQueue(tmp_path / "queue")
    spec = make_spec("drained-job", SELFHOSTED, "01AAAAAAAAAAAAAAAAAAAAAAAA")
    place(queue, "done", spec)
    (tmp_path / "runs" / spec.name).mkdir(parents=True)
    app_states = iter(["stopping...", "stopped"])
    container_lists = iter(
        [
            [{"container_id": "ta-draining", "app_name": MODAL_APP_NAME}],
            [],
        ]
    )
    waits: list[float] = []

    def scripted(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv == ["app", "list", "--json"]:
            state = next(app_states)
            return completed(
                argv, stdout=json.dumps([{"description": MODAL_APP_NAME, "state": state}])
            )
        if argv == ["container", "list", "--json"]:
            return completed(argv, stdout=json.dumps(next(container_lists)))
        return completed(argv)

    record = stop_selfhosted_app_if_drained(
        queue,
        tmp_path,
        [spec],
        runner=scripted,
        now=MOMENT,
        clock=lambda: 0.0,
        sleeper=waits.append,
        poll_interval_seconds=5.0,
    )

    assert record is not None
    assert record["stopped"] is True
    assert record["reason"] is None
    assert record["app_state"] == "stopped"
    assert record["container_count"] == 0
    assert record["polls"] == 2
    assert waits == [5.0]
    events = [event for event in load_events(queue.events_path) if event.event == TEARDOWN_EVENT]
    assert [event.reason_code for event in events] == ["modal_app_stopped"]


def test_teardown_times_out_while_stopping(tmp_path: Path, sandbox: None) -> None:
    """Failure only after the poll window expires still in `stopping...`."""
    queue = DirectoryQueue(tmp_path / "queue")
    spec = make_spec("drained-job", SELFHOSTED, "01AAAAAAAAAAAAAAAAAAAAAAAA")
    place(queue, "done", spec)
    (tmp_path / "runs" / spec.name).mkdir(parents=True)
    now_seconds = {"value": 0.0}

    def clock() -> float:
        return now_seconds["value"]

    def sleeper(seconds: float) -> None:
        now_seconds["value"] += seconds

    def still_stopping(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv == ["app", "list", "--json"]:
            return completed(
                argv,
                stdout=json.dumps([{"description": MODAL_APP_NAME, "state": "stopping..."}]),
            )
        if argv == ["container", "list", "--json"]:
            return completed(
                argv,
                stdout=json.dumps([{"container_id": "ta-draining", "app_name": MODAL_APP_NAME}]),
            )
        return completed(argv)

    record = stop_selfhosted_app_if_drained(
        queue,
        tmp_path,
        [spec],
        runner=still_stopping,
        now=MOMENT,
        clock=clock,
        sleeper=sleeper,
        poll_timeout_seconds=10.0,
        poll_interval_seconds=5.0,
    )

    assert record is not None
    assert record["stopped"] is False
    assert record["app_state"] == "stopping..."
    assert record["container_count"] == 1
    assert record["polls"] == 2
    assert record["reason"] == "modal-app-not-stopped: state=stopping... after 2 poll(s)"
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


def _daytona_stub(monkeypatch: pytest.MonkeyPatch, client: object) -> None:
    import types

    stub = types.ModuleType("daytona")
    stub.Daytona = client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "daytona", stub)
    monkeypatch.setenv("DAYTONA_API_KEY", "test-credential")


def _sandbox(labels: dict[str, str] | None) -> object:
    return type("FakeSandbox", (), {"labels": labels})()


def test_daytona_helper_reports_missing_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "daytona", None)
    assert daytona_sandbox_counts() == DaytonaSandboxCounts(
        None, None, None, "daytona-sdk-not-installed"
    )


def test_daytona_helper_reports_missing_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import types

    stub = types.ModuleType("daytona")

    class Client:
        def list(self) -> list[object]:
            raise AssertionError("must not reach the network without credentials")

    stub.Daytona = Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "daytona", stub)
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    assert daytona_sandbox_counts() == DaytonaSandboxCounts(
        None, None, None, "daytona-credentials-missing"
    )


def test_daytona_helper_splits_harbor_managed_from_account_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Client:
        def list(self) -> list[object]:
            return [
                _sandbox({"harbor.managed": "true", "harbor.session_id": "s-1"}),
                _sandbox({"harbor.managed": "true", "harbor.session_id": "s-2"}),
                _sandbox({"other": "lane-sandbox"}),
                _sandbox(None),
            ]

    _daytona_stub(monkeypatch, Client)
    assert daytona_sandbox_counts() == DaytonaSandboxCounts(
        total=4, harbor_managed=2, matched=None, reason=None
    )


def test_daytona_helper_matches_job_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    class Client:
        def list(self) -> list[object]:
            return [
                _sandbox({"harbor.managed": "true", "harbor.session_id": "s-1"}),
                _sandbox({"harbor.managed": "true", "harbor.session_id": "s-2"}),
                _sandbox({"other": "lane-sandbox"}),
            ]

    _daytona_stub(monkeypatch, Client)
    assert daytona_sandbox_counts({"harbor.session_id": "s-1"}) == DaytonaSandboxCounts(
        total=3, harbor_managed=2, matched=1, reason=None
    )


def test_daytona_helper_reports_listing_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Client:
        def list(self) -> list[object]:
            raise RuntimeError("Invalid credentials")

    _daytona_stub(monkeypatch, Client)
    counts = daytona_sandbox_counts()
    assert counts.total is None
    assert counts.harbor_managed is None
    assert counts.matched is None
    assert counts.reason == "RuntimeError: Invalid credentials"


def test_describe_teardown_reports_daytona_census() -> None:
    record = {
        "app": MODAL_APP_NAME,
        "stopped": True,
        "app_state": "stopped",
        "container_count": 0,
        "daytona_sandboxes": {
            "total": 9,
            "harbor_managed": 7,
            "matched": None,
            "reason": None,
        },
    }
    assert describe_teardown(record) == (
        f"modal teardown: stopped {MODAL_APP_NAME} "
        "(state=stopped, containers=0); "
        "daytona sandboxes: 7 harbor-managed / 9 total"
    )


def test_describe_teardown_reads_legacy_count_receipts() -> None:
    record = {
        "app": MODAL_APP_NAME,
        "stopped": True,
        "app_state": "stopped",
        "container_count": 0,
        "daytona_sandboxes": {"count": 7, "reason": None},
    }
    assert describe_teardown(record).endswith("daytona sandboxes: 7 total")


def test_describe_teardown_marks_daytona_unknown() -> None:
    record = {
        "app": MODAL_APP_NAME,
        "stopped": True,
        "app_state": "stopped",
        "container_count": 0,
        "daytona_sandboxes": {
            "total": None,
            "harbor_managed": None,
            "matched": None,
            "reason": "daytona-credentials-missing",
        },
    }
    assert describe_teardown(record).endswith(
        "daytona sandboxes: n/a (daytona-credentials-missing)"
    )


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
