from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import evallab.harbor_state_journal as state_journal
from evallab.harbor_state_journal import Monitor, StateJournalPlugin


@pytest.fixture
def context_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    context = tmp_path / "state-journal-context"
    context.mkdir()
    (context / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (context / "watch.py").write_text("# fake watcher\n", encoding="utf-8")
    (context / "producer.py").write_text("# fake producer\n", encoding="utf-8")
    monkeypatch.setenv("EVALLAB_STATE_JOURNAL", "on")
    return context


def _plugin(context_dir: Path, *, ready_timeout_seconds: float = 20.0) -> StateJournalPlugin:
    plugin = StateJournalPlugin(
        context_dir=context_dir,
        ready_timeout_seconds=ready_timeout_seconds,
    )
    plugin._laminar = None
    return plugin


def _event(tmp_path: Path, trial_id: str = "trial-123") -> SimpleNamespace:
    return SimpleNamespace(
        trial_id=trial_id,
        trial_name="sample-task__abc123",
        config=SimpleNamespace(trials_dir=tmp_path / "trials"),
    )


def _output_dir(event: SimpleNamespace) -> Path:
    return Path(event.config.trials_dir) / event.trial_name / "state-journal"


def _status(event: SimpleNamespace) -> dict[str, object]:
    return json.loads((_output_dir(event) / "status.json").read_text(encoding="utf-8"))


def _job(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        job_dir=tmp_path / "runs" / "job",
        on_agent_started=lambda _callback: None,
        on_agent_ended=lambda _callback: None,
        on_trial_ended=lambda _callback: None,
        on_trial_cancelled=lambda _callback: None,
    )


def _install_monitor(plugin: StateJournalPlugin, event: SimpleNamespace, name: str) -> None:
    output = _output_dir(event)
    output.mkdir(parents=True, exist_ok=True)
    plugin.monitors[str(event.trial_id)] = Monitor(
        name=name,
        output_dir=output,
        target_container="container-1",
        target_pid=4242,
    )


def test_existing_image_is_reused_without_build(
    tmp_path: Path,
    context_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event(tmp_path)
    calls: list[tuple[str, ...]] = []

    async def fake_command(*args: str, timeout: float) -> tuple[int, str, str]:
        calls.append(args)
        if args[:3] == ("docker", "image", "inspect"):
            return 0, "", ""
        if args[:2] == ("docker", "ps"):
            return 0, "container-1", ""
        if args[:2] == ("docker", "inspect"):
            return 0, "4242", ""
        if args[:2] == ("docker", "run"):
            output = _output_dir(event)
            (output / "READY").write_text("ready\n", encoding="utf-8")
            (output / "state-diff.json").write_text("{}\n", encoding="utf-8")
            (output / "status.json").write_text(
                json.dumps({"schema_version": 1, "status": "available", "reason": None}),
                encoding="utf-8",
            )
            return 0, "monitor-1", ""
        if args[:2] in {("docker", "rm"), ("docker", "stop")}:
            return 0, "", ""
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(state_journal, "_command", fake_command)
    plugin = _plugin(context_dir)

    async def lifecycle() -> None:
        await plugin.on_job_start(_job(tmp_path))
        await plugin._on_agent_started(event)
        await plugin._on_agent_ended(event)

    asyncio.run(lifecycle())

    assert plugin.image is not None
    assert plugin.image_error is None
    assert not any(args[:2] == ("docker", "build") for args in calls)
    status = _status(event)
    assert status["status"] == "available"
    assert status["observer"] == {
        "mode": "external-sidecar",
        "target_mutated": False,
        "model_visible_output": False,
        "image": plugin.image,
        "target_container": "container-1",
        "target_pid": 4242,
    }


def test_failed_build_records_image_error_and_unavailable_status(
    tmp_path: Path,
    context_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_command(*args: str, timeout: float) -> tuple[int, str, str]:
        calls.append(args)
        if args[:3] == ("docker", "image", "inspect"):
            return 1, "", "missing"
        if args[:2] == ("docker", "build"):
            return 1, "", "build exploded"
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(state_journal, "_command", fake_command)
    plugin = _plugin(context_dir)
    event = _event(tmp_path)

    async def lifecycle() -> None:
        await plugin.on_job_start(_job(tmp_path))
        await plugin._on_agent_started(event)

    asyncio.run(lifecycle())

    assert plugin.image is None
    assert plugin.image_error == "RuntimeError: build exploded"
    assert [args[:2] for args in calls] == [("docker", "image"), ("docker", "build")]
    status = _status(event)
    assert status["status"] == "unavailable"
    assert status["reason"] == "RuntimeError: build exploded"
    assert status["observer"]["image"] is None  # type: ignore[index]


def test_zero_matching_containers_writes_unavailable_status(
    tmp_path: Path,
    context_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_command(*args: str, timeout: float) -> tuple[int, str, str]:
        assert args[:2] == ("docker", "ps")
        return 0, "", ""

    monkeypatch.setattr(state_journal, "_command", fake_command)
    plugin = _plugin(context_dir)
    plugin.image = "image-1"
    event = _event(tmp_path)

    asyncio.run(plugin._on_agent_started(event))

    status = _status(event)
    assert status["status"] == "unavailable"
    assert "found 0 matching main containers" in str(status["reason"])


def test_two_matching_containers_writes_unavailable_status(
    tmp_path: Path,
    context_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_command(*args: str, timeout: float) -> tuple[int, str, str]:
        assert args[:2] == ("docker", "ps")
        return 0, "container-1\ncontainer-2", ""

    monkeypatch.setattr(state_journal, "_command", fake_command)
    plugin = _plugin(context_dir)
    plugin.image = "image-1"
    event = _event(tmp_path)

    asyncio.run(plugin._on_agent_started(event))

    status = _status(event)
    assert status["status"] == "unavailable"
    assert "found 2 matching main containers" in str(status["reason"])


def test_non_numeric_target_pid_writes_unavailable_status(
    tmp_path: Path,
    context_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_command(*args: str, timeout: float) -> tuple[int, str, str]:
        if args[:2] == ("docker", "ps"):
            return 0, "container-1", ""
        assert args[:2] == ("docker", "inspect")
        return 0, "not-a-pid", ""

    monkeypatch.setattr(state_journal, "_command", fake_command)
    plugin = _plugin(context_dir)
    plugin.image = "image-1"
    event = _event(tmp_path)

    asyncio.run(plugin._on_agent_started(event))

    status = _status(event)
    assert status["status"] == "unavailable"
    assert "invalid target pid: 'not-a-pid'" in str(status["reason"])


def test_ready_timeout_stops_monitor_and_writes_unavailable_status(
    tmp_path: Path,
    context_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_command(*args: str, timeout: float) -> tuple[int, str, str]:
        calls.append(args)
        if args[:2] == ("docker", "ps"):
            return 0, "container-1", ""
        if args[:2] == ("docker", "inspect"):
            return 0, "4242", ""
        if args[:2] in {("docker", "rm"), ("docker", "run"), ("docker", "stop")}:
            return 0, "", ""
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(state_journal, "_command", fake_command)
    plugin = _plugin(context_dir, ready_timeout_seconds=0.001)
    plugin.image = "image-1"
    event = _event(tmp_path)

    asyncio.run(plugin._on_agent_started(event))

    assert plugin.monitors == {}
    assert any(args[:2] == ("docker", "stop") for args in calls)
    status = _status(event)
    assert status["status"] == "unavailable"
    assert status["reason"] == (
        "RuntimeError: observer did not become ready before agent execution"
    )


def test_stop_without_state_diff_writes_unavailable_status(
    tmp_path: Path,
    context_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_command(*args: str, timeout: float) -> tuple[int, str, str]:
        calls.append(args)
        assert args[:2] == ("docker", "stop")
        return 0, "", ""

    monkeypatch.setattr(state_journal, "_command", fake_command)
    plugin = _plugin(context_dir)
    plugin.image = "image-1"
    event = _event(tmp_path)
    _install_monitor(plugin, event, "monitor-1")

    async def lifecycle() -> None:
        await plugin._on_trial_ended(event)
        await plugin._on_trial_ended(event)

    asyncio.run(lifecycle())

    assert len(calls) == 1
    status = _status(event)
    assert status["status"] == "unavailable"
    assert status["reason"] == "observer stopped without a state diff"


def test_stop_with_state_diff_preserves_status_and_adds_observer_target(
    tmp_path: Path,
    context_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_command(*args: str, timeout: float) -> tuple[int, str, str]:
        assert args[:2] == ("docker", "stop")
        return 0, "", ""

    monkeypatch.setattr(state_journal, "_command", fake_command)
    plugin = _plugin(context_dir)
    plugin.image = "image-1"
    event = _event(tmp_path)
    _install_monitor(plugin, event, "monitor-1")
    output = _output_dir(event)
    original = {
        "schema_version": 1,
        "status": "available",
        "reason": None,
        "event_count": 3,
        "producer": {"keep": True},
    }
    (output / "status.json").write_text(json.dumps(original), encoding="utf-8")
    (output / "state-diff.json").write_text("{}\n", encoding="utf-8")

    asyncio.run(plugin._stop(str(event.trial_id)))

    status = _status(event)
    assert status == {
        **original,
        "observer": {
            "mode": "external-sidecar",
            "target_mutated": False,
            "model_visible_output": False,
            "image": "image-1",
            "target_container": "container-1",
            "target_pid": 4242,
        },
    }


def test_stop_with_unreadable_status_writes_explicit_fallback(
    tmp_path: Path,
    context_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_command(*args: str, timeout: float) -> tuple[int, str, str]:
        assert args[:2] == ("docker", "stop")
        return 0, "", ""

    monkeypatch.setattr(state_journal, "_command", fake_command)
    plugin = _plugin(context_dir)
    plugin.image = "image-1"
    event = _event(tmp_path)
    _install_monitor(plugin, event, "monitor-1")
    output = _output_dir(event)
    (output / "status.json").write_text("not-json", encoding="utf-8")
    (output / "state-diff.json").write_text("{}\n", encoding="utf-8")

    asyncio.run(plugin._stop(str(event.trial_id)))

    status = _status(event)
    assert status["schema_version"] == 1
    assert status["status"] == "unavailable"
    assert status["reason"] == "status unreadable"
    assert status["observer"]["target_container"] == "container-1"  # type: ignore[index]
    assert status["observer"]["target_pid"] == 4242  # type: ignore[index]


def test_successful_build_and_monitor_start_failure_write_unavailable_status(
    tmp_path: Path,
    context_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_command(*args: str, timeout: float) -> tuple[int, str, str]:
        calls.append(args)
        if args[:3] == ("docker", "image", "inspect"):
            return 1, "", "missing"
        if args[:2] == ("docker", "build"):
            return 0, "image-1", ""
        if args[:2] == ("docker", "ps"):
            return 0, "container-1", ""
        if args[:2] == ("docker", "inspect"):
            return 0, "4242", ""
        if args[:2] == ("docker", "rm"):
            return 0, "", ""
        if args[:2] == ("docker", "run"):
            return 1, "", "monitor start failed"
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(state_journal, "_command", fake_command)
    plugin = _plugin(context_dir)
    event = _event(tmp_path)

    async def lifecycle() -> None:
        await plugin.on_job_start(_job(tmp_path))
        await plugin._on_agent_started(event)

    asyncio.run(lifecycle())

    assert plugin.image is not None
    assert plugin.image_error is None
    assert any(args[:2] == ("docker", "build") for args in calls)
    status = _status(event)
    assert status["status"] == "unavailable"
    assert status["reason"] == "RuntimeError: monitor start failed"


def test_incomplete_image_context_fails_open_with_unavailable_status(
    tmp_path: Path,
    context_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (context_dir / "producer.py").unlink()

    async def fake_command(*args: str, timeout: float) -> tuple[int, str, str]:
        raise AssertionError("an incomplete context must not invoke Docker")

    monkeypatch.setattr(state_journal, "_command", fake_command)
    plugin = _plugin(context_dir)
    event = _event(tmp_path)

    async def lifecycle() -> None:
        await plugin.on_job_start(_job(tmp_path))
        await plugin._on_agent_started(event)

    asyncio.run(lifecycle())

    assert plugin.image is None
    assert plugin.image_error is not None
    assert "state journal image context is incomplete" in plugin.image_error
    status = _status(event)
    assert status["status"] == "unavailable"
    assert status["reason"] == plugin.image_error


def test_disabled_observer_runs_successful_companion_hooks(
    tmp_path: Path,
    context_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import evallab.flight.plugin as flight_plugin

    async def fake_command(*args: str, timeout: float) -> tuple[int, str, str]:
        assert args[:2] == ("docker", "stop")
        return 0, "", ""

    monkeypatch.setattr(state_journal, "_command", fake_command)
    monkeypatch.setenv("EVALLAB_STATE_JOURNAL", "off")
    laminar = SimpleNamespace(on_job_start=AsyncMock(), on_job_end=AsyncMock())
    flight = SimpleNamespace(on_job_start=AsyncMock(), on_job_end=AsyncMock())
    monkeypatch.setattr(flight_plugin, "FlightRecorderPlugin", lambda: flight)
    plugin = _plugin(context_dir)
    plugin._laminar = laminar
    job = _job(tmp_path)
    job.job_dir.mkdir(parents=True)
    flight_config = job.job_dir.parent / ".flight" / f"{job.job_dir.name}.json"
    flight_config.parent.mkdir(parents=True)
    flight_config.write_text("{}\n", encoding="utf-8")
    event = _event(tmp_path)

    async def lifecycle() -> None:
        await plugin.on_job_start(job)
        await plugin._on_agent_started(event)
        _install_monitor(plugin, event, "monitor-1")
        await plugin.on_job_end(None)

    asyncio.run(lifecycle())

    laminar.on_job_start.assert_awaited_once_with(job)
    laminar.on_job_end.assert_awaited_once_with(None)
    flight.on_job_start.assert_awaited_once_with(job)
    flight.on_job_end.assert_awaited_once_with(None)
    assert plugin.image_error == "disabled by EVALLAB_STATE_JOURNAL=off"
    status = _status(event)
    assert status["status"] == "unavailable"
    assert status["reason"] == "observer stopped without a state diff"


def test_companion_hook_failures_are_swallowed_and_recorded(
    tmp_path: Path,
    context_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import evallab.flight.plugin as flight_plugin

    async def fake_command(*args: str, timeout: float) -> tuple[int, str, str]:
        raise AssertionError("disabled observer must not invoke Docker")

    monkeypatch.setattr(state_journal, "_command", fake_command)
    monkeypatch.setenv("EVALLAB_STATE_JOURNAL", "off")
    laminar = SimpleNamespace(
        on_job_start=AsyncMock(side_effect=RuntimeError("laminar start failed")),
        on_job_end=AsyncMock(side_effect=RuntimeError("laminar end failed")),
    )
    flight = SimpleNamespace(
        on_job_start=AsyncMock(side_effect=RuntimeError("flight start failed")),
        on_job_end=AsyncMock(side_effect=RuntimeError("flight end failed")),
    )
    monkeypatch.setattr(flight_plugin, "FlightRecorderPlugin", lambda: flight)
    plugin = _plugin(context_dir)
    plugin._laminar = laminar
    job = _job(tmp_path)
    job.job_dir.mkdir(parents=True)
    flight_config = job.job_dir.parent / ".flight" / f"{job.job_dir.name}.json"
    flight_config.parent.mkdir(parents=True)
    flight_config.write_text("{}\n", encoding="utf-8")
    event = _event(tmp_path)

    async def lifecycle() -> None:
        await plugin.on_job_start(job)
        await plugin._on_agent_started(event)
        await plugin.on_job_end(None)

    asyncio.run(lifecycle())

    assert plugin._flight_errors == [
        "job_start: RuntimeError: flight start failed",
        "job_end: RuntimeError: flight end failed",
    ]
    flight_status = json.loads(
        (job.job_dir / "flight-unavailable.json").read_text(encoding="utf-8")
    )
    assert flight_status == {
        "schema": "evallab.flight.status/v1",
        "status": "unavailable",
        "errors": plugin._flight_errors,
    }
    status = _status(event)
    assert status["status"] == "unavailable"
    assert status["reason"] == "disabled by EVALLAB_STATE_JOURNAL=off"
