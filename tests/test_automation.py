from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from evallab.automation import (
    StepTimeout,
    _process_table,
    run_with_timeout,
)


def _alive(pid: int) -> bool:
    return pid in _process_table()


def _spawn_process_tree(pid_file: Path) -> subprocess.Popen[bytes]:
    """A session-leading shell with a background grandchild, like Harbor's group."""
    shell = subprocess.Popen(
        ["/bin/sh", "-c", f"sleep 60 & echo $! > {pid_file}; wait"],
        start_new_session=True,
    )
    deadline = time.monotonic() + 10
    while not (pid_file.exists() and pid_file.read_text().strip()):
        assert time.monotonic() < deadline, "fixture process tree did not start"
        time.sleep(0.01)
    return shell


def test_run_with_timeout_terminates_the_whole_child_tree(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    spawned: list[int] = []
    calls = 0

    def hang() -> None:
        nonlocal calls
        calls += 1
        try:
            shell = _spawn_process_tree(pid_file)
            spawned.extend([shell.pid, int(pid_file.read_text())])
            shell.wait()  # Blocks until the expiry handler has killed the tree.
        except Exception as exc:
            raise AssertionError("timeout must escape ordinary handlers") from exc

    started = time.monotonic()
    with pytest.raises(StepTimeout) as expired:
        run_with_timeout(hang, 0.5, grace_seconds=2.0)

    assert time.monotonic() - started < 10
    assert expired.value.seconds == 0.5
    assert set(spawned) <= set(expired.value.terminated)
    assert calls == 1
    assert not any(_alive(pid) for pid in spawned)


