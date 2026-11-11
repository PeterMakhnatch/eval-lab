from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
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


@pytest.mark.parametrize("exit_code", [0, 7])
def test_command_returns_decoded_output_and_child_exit_code(exit_code: int) -> None:
    result = asyncio.run(
        state_journal._command(
            "sh",
            "-c",
            "printf ' output\\377 \\n'; printf ' error\\376 \\n' >&2; exit \"$1\"",
            "journal-command",
            str(exit_code),
            timeout=10,
        )
    )

    assert result == (exit_code, "output\ufffd", "error\ufffd")


def test_command_timeout_kills_and_reaps_the_real_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_subprocess = asyncio.create_subprocess_exec
    process: asyncio.subprocess.Process | None = None
    process_wait: AsyncMock | None = None

    async def capture_subprocess(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        nonlocal process, process_wait
        process = await create_subprocess(*args, **kwargs)
        process_wait = AsyncMock(wraps=process.wait)
        monkeypatch.setattr(process, "wait", process_wait)
        return process

    monkeypatch.setattr(state_journal.asyncio, "create_subprocess_exec", capture_subprocess)

    async def run_command() -> None:
        try:
            result = await state_journal._command(
                "sh", "-c", "exec sleep 30", timeout=0.05
            )
            assert result == (124, "", "command timed out after 0s")
            assert process is not None
            assert process_wait is not None
            process_wait.assert_awaited_once_with()
            assert process.returncode == -signal.SIGKILL
            with pytest.raises(ChildProcessError):
                os.waitpid(process.pid, os.WNOHANG)
        finally:
            if process is not None:
                if process.returncode is None:
                    process.kill()
                await process.wait()

    asyncio.run(run_command())


def test_laminar_import_failure_does_not_block_the_job(
    tmp_path: Path,
    context_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVALLAB_STATE_JOURNAL", "off")
    monkeypatch.setitem(sys.modules, "evallab.harbor_laminar", None)
    command = AsyncMock(side_effect=AssertionError("Docker must not run"))
    monkeypatch.setattr(state_journal, "_command", command)
    plugin = StateJournalPlugin(context_dir=context_dir)
    event = _event(tmp_path)

    async def lifecycle() -> None:
        await plugin.on_job_start(_job(tmp_path))
        await plugin._on_agent_started(event)
        await plugin.on_job_end(None)

    assert asyncio.run(lifecycle()) is None
    assert plugin._laminar is None
    assert _status(event)["reason"] == "disabled by EVALLAB_STATE_JOURNAL=off"
    command.assert_not_awaited()


@pytest.mark.parametrize("hook", ["on_job_start", "on_job_end"])
def test_laminar_cancellation_propagates_the_original_error(
    tmp_path: Path,
    context_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    hook: str,
) -> None:
    monkeypatch.setenv("EVALLAB_STATE_JOURNAL", "off")
    command = AsyncMock(side_effect=AssertionError("Docker must not run"))
    monkeypatch.setattr(state_journal, "_command", command)
    laminar = SimpleNamespace(on_job_start=AsyncMock(), on_job_end=AsyncMock())
    plugin = _plugin(context_dir)
    plugin._laminar = laminar
    job = _job(tmp_path)
    result = SimpleNamespace(reward=0.75)
    error = asyncio.CancelledError("the caller cancelled the job")
    failing_hook = getattr(laminar, hook)
    failing_hook.side_effect = error

    async def lifecycle() -> None:
        await plugin.on_job_start(job)
        if hook == "on_job_end":
            await plugin.on_job_end(result)

    with pytest.raises(asyncio.CancelledError) as raised:
        asyncio.run(lifecycle())

    assert raised.value is error
    failing_hook.assert_awaited_once_with(job if hook == "on_job_start" else result)
    command.assert_not_awaited()


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


def test_disabled_observer_runs_successful_laminar_hooks(
    tmp_path: Path,
    context_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_command(*args: str, timeout: float) -> tuple[int, str, str]:
        assert args[:2] == ("docker", "stop")
        return 0, "", ""

    monkeypatch.setattr(state_journal, "_command", fake_command)
    monkeypatch.setenv("EVALLAB_STATE_JOURNAL", "off")
    laminar = SimpleNamespace(on_job_start=AsyncMock(), on_job_end=AsyncMock())
    plugin = _plugin(context_dir)
    plugin._laminar = laminar
    job = _job(tmp_path)
    event = _event(tmp_path)

    async def lifecycle() -> None:
        await plugin.on_job_start(job)
        await plugin._on_agent_started(event)
        _install_monitor(plugin, event, "monitor-1")
        await plugin.on_job_end(None)

    asyncio.run(lifecycle())

    laminar.on_job_start.assert_awaited_once_with(job)
    laminar.on_job_end.assert_awaited_once_with(None)
    assert plugin.image_error == "disabled by EVALLAB_STATE_JOURNAL=off"
    status = _status(event)
    assert status["status"] == "unavailable"
    assert status["reason"] == "observer stopped without a state diff"


def test_laminar_hook_failures_do_not_block_the_journal(
    tmp_path: Path,
    context_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_command(*args: str, timeout: float) -> tuple[int, str, str]:
        raise AssertionError("disabled observer must not invoke Docker")

    monkeypatch.setattr(state_journal, "_command", fake_command)
    monkeypatch.setenv("EVALLAB_STATE_JOURNAL", "off")
    laminar = SimpleNamespace(
        on_job_start=AsyncMock(side_effect=RuntimeError("laminar start failed")),
        on_job_end=AsyncMock(side_effect=RuntimeError("laminar end failed")),
    )
    plugin = _plugin(context_dir)
    plugin._laminar = laminar
    job = _job(tmp_path)
    event = _event(tmp_path)
    result = SimpleNamespace(reward=0.75)

    async def lifecycle() -> None:
        await plugin.on_job_start(job)
        await plugin._on_agent_started(event)
        await plugin.on_job_end(result)

    assert asyncio.run(lifecycle()) is None

    laminar.on_job_start.assert_awaited_once_with(job)
    laminar.on_job_end.assert_awaited_once_with(result)
    status = _status(event)
    assert status["status"] == "unavailable"
    assert status["reason"] == "disabled by EVALLAB_STATE_JOURNAL=off"
