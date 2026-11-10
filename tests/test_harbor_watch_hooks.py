"""Harbor hook journal: what the plugin records, and how the watch consumes it."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

import evallab.auto_watch as auto_watch
import evallab.harbor_watch_hooks as hooks_mod
from evallab.harbor_watch_hooks import HOOK_JOURNAL, WatchHookPlugin, read_hook_journal
from evallab.live_watch import WatchThresholds, run_watch

TRIAL = "task__h00k"


class FakeQueue:
    def __init__(self) -> None:
        self.setup_calls: list[Any] = []

    def _setup_hooks(self, trial: Any) -> None:
        self.setup_calls.append(trial)


class FakeJob:
    """The slice of ``harbor.job.Job`` a JobPlugin touches."""

    def __init__(self, job_dir: Path) -> None:
        self.job_dir = job_dir
        self.callbacks: list[Any] = []
        self._trial_queue = FakeQueue()
        for name in (
            "on_trial_started",
            "on_environment_started",
            "on_agent_started",
            "on_agent_ended",
            "on_verification_started",
            "on_trial_ended",
            "on_trial_cancelled",
        ):
            setattr(self, name, self.callbacks.append)


def _event(name: str, trial_id: str, *, at: datetime, result: Any = None) -> Any:
    return SimpleNamespace(
        event=SimpleNamespace(value=name),
        trial_name=TRIAL,
        task_name="org/task",
        trial_id=trial_id,
        timestamp=at,
        config=SimpleNamespace(trial_name=TRIAL, agent={"kwargs": {"temperature": 0}}),
        result=result or SimpleNamespace(exception_info=None, verifier_result=None),
    )


def _start(plugin: WatchHookPlugin, job: FakeJob) -> None:
    asyncio.run(plugin.on_job_start(job))


def test_plugin_journals_lifecycle_and_never_touches_the_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hooks_mod, "_harbor_version", lambda: (0, 21, 0))
    job, plugin, trial_id = FakeJob(tmp_path), WatchHookPlugin(), str(uuid4())
    _start(plugin, job)
    hook = job.callbacks[0]
    t0 = datetime(2026, 10, 6, 3, 0, tzinfo=UTC)
    ended = SimpleNamespace(
        exception_info=SimpleNamespace(exception_type="DaytonaNotFoundError"),
        verifier_result=SimpleNamespace(rewards={"reward": 0.0}),
    )
    events = [
        _event("start", trial_id, at=t0),
        _event("agent-start", trial_id, at=t0 + timedelta(seconds=5)),
        _event("end", trial_id, at=t0 + timedelta(seconds=9), result=ended),
    ]
    before = copy.deepcopy(events)
    for event in events:
        assert asyncio.run(hook(event)) is None
    # Observation only: the hook objects Harbor passes (config, result) come back unchanged.
    assert [vars(e) for e in events] == [vars(e) for e in before]

    state = read_hook_journal(tmp_path)[TRIAL]
    assert [name for name, _ in state["events"]] == ["start", "agent-start", "end"]
    assert state["terminal"] and state["phase"] == "end"
    assert state["exception_type"] == "DaytonaNotFoundError"
    assert state["rewards"] == {"reward": 0.0}
    # 0.21: LogEntry is never subscribed, so Docker exec keeps its buffered reader.
    assert plugin.log_streaming is False
    assert "_setup_hooks" not in vars(job._trial_queue)


def test_hook_write_failure_is_swallowed(tmp_path: Path) -> None:
    blocker = tmp_path / "job"
    blocker.write_text("not a directory", encoding="utf-8")
    job = FakeJob(blocker)
    _start(WatchHookPlugin(), job)
    event = _event("start", str(uuid4()), at=datetime.now(UTC))
    assert all(asyncio.run(hook(event)) is None for hook in job.callbacks)


def test_log_entries_are_opt_in_on_024_and_fold_into_tails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hooks_mod, "_harbor_version", lambda: (0, 24, 0))
    monkeypatch.delenv(hooks_mod.LOG_HOOKS_ENV, raising=False)
    default_job = FakeJob(tmp_path / "default")
    _start(WatchHookPlugin(), default_job)
    # Off by default: a subscriber changes Docker's exec reader (see module docstring).
    assert "_setup_hooks" not in vars(default_job._trial_queue)

    monkeypatch.setenv(hooks_mod.LOG_HOOKS_ENV, "1")
    job, trial_id = FakeJob(tmp_path), str(uuid4())
    plugin = WatchHookPlugin(flush_seconds=3600)
    _start(plugin, job)
    assert plugin.log_streaming is True

    log_callbacks: list[Any] = []
    trial = SimpleNamespace(add_log_callback=log_callbacks.append)
    job._trial_queue._setup_hooks(trial)
    assert job._trial_queue.setup_calls == [trial]  # Harbor's own wiring still runs first
    (on_log,) = log_callbacks

    asyncio.run(job.callbacks[0](_event("start", trial_id, at=datetime.now(UTC))))
    for text in ["collecting tests\n", "x" * 3000 + "\n", "1 passed\n"]:
        entry = SimpleNamespace(
            trial_id=trial_id, phase="verification", text=text, timestamp=datetime.now(UTC)
        )
        assert asyncio.run(on_log(entry)) is None
    asyncio.run(plugin.on_job_end(None))

    log = read_hook_journal(tmp_path)[TRIAL]["logs"]["verification"]
    assert log["chunks"] == 3
    assert log["chars"] == len("collecting tests\n") + 3001 + len("1 passed\n")
    assert log["tail"].endswith("x\n1 passed\n")
    assert len(log["tail"]) == hooks_mod.LOG_TAIL_CHARS


def _journal(job_dir: Path, *records: dict[str, Any]) -> None:
    path = job_dir / HOOK_JOURNAL
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(
                json.dumps(
                    {"schema": hooks_mod.HOOK_SCHEMA, "trial": TRIAL, **record}, sort_keys=True
                )
                + "\n"
            )


def _iso(seconds_ago: float) -> str:
    return datetime.fromtimestamp(time.time() - seconds_ago, UTC).isoformat()


def test_watch_sees_a_hooked_trial_before_its_trajectory_and_ends_it_on_the_end_hook(
    tmp_path: Path,
) -> None:
    job = tmp_path / "job"
    (job / TRIAL).mkdir(parents=True)  # Harbor made the trial dir; no trajectory yet
    _journal(
        job,
        {"kind": "event", "event": "start", "at": _iso(60)},
        {"kind": "event", "event": "environment-start", "at": _iso(59)},
    )
    out = tmp_path / "watch"
    (status,) = run_watch(runs_dirs=[job], out_dir=out)["statuses"]
    assert (status["state"], status["phase"], status["phase_source"]) == (
        "running",
        "environment-start",
        "hooks",
    )

    _journal(
        job,
        {"kind": "event", "event": "end", "at": _iso(1), "exception_type": "DaytonaNotFoundError"},
    )
    summary = run_watch(runs_dirs=[job], out_dir=out)
    (status,) = summary["statuses"]
    assert status["state"] == "finished"
    assert status["exception_type"] == "DaytonaNotFoundError"
    assert "infra_error" in {alert["rule"] for alert in status["open_alerts"]}


def test_hook_log_activity_keeps_a_long_command_from_reading_as_stalled(tmp_path: Path) -> None:
    thresholds = WatchThresholds(stalled_minutes=10.0)
    job = tmp_path / "job"
    traj = job / TRIAL / "agent" / "trajectory.json"
    traj.parent.mkdir(parents=True)
    traj.write_text(json.dumps({"steps": []}), encoding="utf-8")
    old = time.time() - 30 * 60
    import os

    os.utime(traj, (old, old))
    quiet = run_watch(runs_dirs=[job], out_dir=tmp_path / "w1", thresholds=thresholds)
    assert "stalled" in {a["rule"] for a in quiet["statuses"][0]["open_alerts"]}

    _journal(
        job,
        {"kind": "event", "event": "agent-start", "at": _iso(30 * 60)},
        {
            "kind": "log",
            "phase": "agent",
            "chunks": 40,
            "chars": 900,
            "tail": "step 40/400\n",
            "at": _iso(5),
        },
    )
    busy = run_watch(runs_dirs=[job], out_dir=tmp_path / "w2", thresholds=thresholds)
    (status,) = busy["statuses"]
    assert "stalled" not in {a["rule"] for a in status["open_alerts"]}
    assert status["log_tail"] == {"phase": "agent", "text": "step 40/400\n"}


def test_lifecycle_hook_wakes_the_auto_watch_before_its_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auto_watch, "HOOK_POLL_SECONDS", 0.02)
    job = tmp_path / "runs" / "job"
    job.mkdir(parents=True)
    passes: list[float] = []

    def fake_pass(*, runs_dirs: list[Path], out_dir: Path) -> dict:
        passes.append(time.monotonic())
        return {}

    handle = auto_watch.start_auto_watch(
        jobs_dir=tmp_path / "runs",
        job_name="job",
        agent="mimoagent",
        interval_seconds=3600,
        run_watch_fn=fake_pass,
    )
    try:
        _journal(
            job,
            {"kind": "log", "phase": "agent", "chunks": 1, "chars": 1, "tail": "x", "at": _iso(0)},
        )
        time.sleep(0.3)
        assert passes == []  # log chunks alone wait for the interval
        _journal(job, {"kind": "event", "event": "end", "at": _iso(0)})
        deadline = time.monotonic() + 5
        while not passes and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(passes) == 1
    finally:
        auto_watch.stop_auto_watch(handle, run_watch_fn=fake_pass)


# --- Harbor version parsing -------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected", "streams"),
    [
        ("0.24.0", (0, 24, 0), True),
        ("0.24.0rc1", (0, 24, 0), True),
        ("0.24.dev", (0, 24), True),
        ("0.23.9", (0, 23, 9), False),
        ("1.2.3.4", (1, 2, 3), True),
        ("0.2rc4", (0, 2), False),
        ("dev", (), False),
    ],
)
def test_harbor_version_keeps_numeric_pieces_and_stops_at_the_first_non_numeric(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: tuple[int, ...], streams: bool
) -> None:
    monkeypatch.setattr(hooks_mod.metadata, "version", lambda _name: raw)
    assert hooks_mod._harbor_version() == expected
    assert hooks_mod.log_streaming_supported() is streams


def test_missing_harbor_package_disables_log_streaming(monkeypatch: pytest.MonkeyPatch) -> None:
    def absent(_name: str) -> str:
        raise hooks_mod.metadata.PackageNotFoundError

    monkeypatch.setattr(hooks_mod.metadata, "version", absent)
    assert hooks_mod._harbor_version() == ()
    assert hooks_mod.log_streaming_supported() is False


# --- job start / log subscription / arming ----------------------------------


def test_job_that_raises_during_start_is_swallowed(
    tmp_path: Path,
) -> None:
    class BrokenJob(FakeJob):
        def __init__(self, job_dir: Path) -> None:
            super().__init__(job_dir)
            self.on_agent_started = self._explode  # instance attr shadows FakeJob's recorder

        def _explode(self, _hook: Any) -> None:
            raise RuntimeError("harbor changed its hook API")

    plugin = WatchHookPlugin()
    asyncio.run(plugin.on_job_start(BrokenJob(tmp_path / "job")))
    assert plugin.log_streaming is False


def test_log_streaming_is_off_when_the_queue_has_no_setup_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hooks_mod, "_harbor_version", lambda: (0, 24, 0))
    monkeypatch.setenv(hooks_mod.LOG_HOOKS_ENV, "1")
    job: Any = FakeJob(tmp_path / "job")
    job._trial_queue = SimpleNamespace()
    plugin = WatchHookPlugin()
    _start(plugin, job)
    assert plugin.log_streaming is False
    assert plugin._flusher is None
    assert not hasattr(job._trial_queue, "_setup_hooks")


# --- log hook / flush loop --------------------------------------------------


def test_malformed_log_entry_is_dropped_without_touching_the_buffer() -> None:
    plugin = WatchHookPlugin()
    plugin._trial_names["id"] = TRIAL
    assert asyncio.run(plugin._on_log(SimpleNamespace(trial_id="id", phase="agent"))) is None
    assert asyncio.run(plugin._on_log(SimpleNamespace(trial_id="other", phase="agent"))) is None
    assert plugin._logs.drain() == []


def test_flush_loop_writes_the_journal_on_every_tick(tmp_path: Path) -> None:
    class TwoTicks:
        def __init__(self) -> None:
            self.waits = [False, False, True]

        def wait(self, _timeout: float) -> bool:
            return self.waits.pop(0)

    plugin = WatchHookPlugin()
    plugin._journal = tmp_path / HOOK_JOURNAL
    plugin._stop = TwoTicks()  # ty: ignore[invalid-assignment]
    plugin._logs.add(TRIAL, "agent", "hello", _iso(0))
    plugin._flush_loop()
    folded = read_hook_journal(tmp_path)
    assert folded[TRIAL]["logs"]["agent"]["tail"] == "hello"
    assert (tmp_path / HOOK_JOURNAL).read_text(encoding="utf-8").count("\n") == 1  # drained once


# --- journal reader ---------------------------------------------------------


def test_journal_reader_skips_bad_lines_and_records_without_a_trial(tmp_path: Path) -> None:
    path = tmp_path / HOOK_JOURNAL
    path.parent.mkdir(parents=True)
    good = {"kind": "event", "event": "start", "trial": TRIAL, "at": "2026-11-03T10:00:00+00:00"}
    lines = [
        "{truncated",
        json.dumps(["not", "a", "dict"]),
        json.dumps({"kind": "event", "event": "start"}),
        json.dumps({"kind": "event", "event": "start", "trial": 7}),
        json.dumps(good),
        "",
        json.dumps({**good, "event": "end", "exception_type": "X", "rewards": {"reward": 1.0}}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    folded = read_hook_journal(tmp_path)
    assert list(folded) == [TRIAL]
    assert [name for name, _at in folded[TRIAL]["events"]] == ["start", "end"]
    assert folded[TRIAL]["terminal"] is True
    assert folded[TRIAL]["exception_type"] == "X"
    assert folded[TRIAL]["rewards"] == {"reward": 1.0}


def test_journal_epoch_handles_odd_timestamps(tmp_path: Path) -> None:
    assert hooks_mod._epoch(None) == 0.0
    assert hooks_mod._epoch(12345) == 0.0
    assert hooks_mod._epoch("yesterday-ish") == 0.0
    aware = hooks_mod._epoch("2026-11-03T10:00:00+00:00")
    assert hooks_mod._epoch("2026-11-03T10:00:00Z") == aware
    assert hooks_mod._epoch("2026-11-03T10:00:00") == aware  # naive reads as UTC
    assert hooks_mod._epoch("2026-11-03T06:00:00-04:00") == aware
    path = tmp_path / HOOK_JOURNAL
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"kind": "event", "event": "start", "trial": TRIAL, "at": "garbage"}) + "\n",
        encoding="utf-8",
    )
    assert read_hook_journal(tmp_path)[TRIAL]["last_at"] == 0.0
