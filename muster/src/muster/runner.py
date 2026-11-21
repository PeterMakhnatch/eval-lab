"""The only code that starts a process: the pinned `harbor` command (docs/DESIGN.md section 2).

The command line carries the config path and fixed flags only. API keys reach harbor through
the inherited environment, never through arguments or files.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from muster.model import InputError

ENV_HARBOR = "MUSTER_HARBOR"
CONFIG_NAME = "job.json"  # harbor reads a .json config with json.loads
LOG_NAME = "harbor.log"
PACKAGE_PARENT = Path(__file__).resolve().parents[1]  # the directory that contains the muster package
GRACE_SEC = 120.0  # the time harbor gets to remove its containers after SIGTERM


@dataclass(frozen=True)
class RunOutcome:
    """How one `harbor run` ended."""

    command: tuple[str, ...]
    config: Path
    log: Path
    returncode: int
    timed_out: bool
    seconds: float


def find_harbor(option: str | None = None) -> Path:
    """The harbor command: --harbor, else MUSTER_HARBOR. Never the `harbor` on PATH."""
    value = option or os.environ.get(ENV_HARBOR)
    if not value:
        raise InputError(
            f"no harbor command: pass --harbor PATH or set {ENV_HARBOR} to the harbor of "
            "olamlabs/harbor-multiagent (the `harbor` on PATH can be a different program)"
        )
    path = Path(os.path.abspath(Path(value).expanduser()))
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        raise InputError(f"the harbor command {path} is not an executable file")
    return path


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The caller's environment plus `extra`, with PYTHONPATH extended so that harbor can import muster.

    An installed muster (site-packages) is not added: that directory holds other packages too,
    and it would shadow harbor's own dependencies.
    """
    env = {**os.environ, **(extra or {})}
    if PACKAGE_PARENT.name not in ("site-packages", "dist-packages"):
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(PACKAGE_PARENT), env.get("PYTHONPATH")]))
    return env


def harbor_version(harbor: Path) -> str | None:
    """The last line of `harbor --version`, or None when the command fails."""
    command = [str(harbor), "--version"]
    try:
        done = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = done.stdout.strip().splitlines()
    return lines[-1].strip() if done.returncode == 0 and lines else None


def write_config(config: dict[str, Any], out_dir: Path, stem: str = "") -> Path:
    """Write a Harbor job config as <out_dir>/<stem>job.json and return its path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{stem}{CONFIG_NAME}"
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def run_job(
    config: dict[str, Any],
    out_dir: Path,
    harbor: Path,
    env_extra: dict[str, str] | None = None,
    *,
    timeout_sec: float = 3600.0,
    stem: str = "",
) -> RunOutcome:
    """Write the config, run `harbor run -c <config>` and wait for it.

    Output goes to <out_dir>/<stem>harbor.log. At the timeout, or when muster is interrupted,
    harbor gets SIGTERM and `GRACE_SEC` to remove its containers. Then its process group is killed.
    """
    config_path = write_config(config, out_dir, stem)
    log_path = config_path.with_name(f"{stem}{LOG_NAME}")
    command = (str(harbor), "run", "-c", str(config_path), "--quiet", "--yes")
    started, timed_out = time.monotonic(), False
    with log_path.open("ab") as log, _sigterm_as_interrupt():
        log.write(f"$ {' '.join(command)}\n".encode())
        log.flush()
        # A new session: a terminal's Ctrl-C reaches harbor through muster only, as one signal.
        streams = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT}
        process = subprocess.Popen(command, env=child_env(env_extra), start_new_session=True, **streams)
        try:
            returncode = process.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            timed_out, returncode = True, _stop(process)
        except BaseException:
            _stop(process)
            raise
    return RunOutcome(command, config_path, log_path, returncode, timed_out, time.monotonic() - started)


def _stop(process: subprocess.Popen) -> int:
    process.terminate()
    try:
        return process.wait(timeout=GRACE_SEC)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        return process.wait()


@contextlib.contextmanager
def _sigterm_as_interrupt() -> Iterator[None]:
    """While harbor runs, a SIGTERM to muster becomes KeyboardInterrupt, so that harbor is stopped too."""

    def interrupt(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    main = threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGTERM, interrupt) if main else None
    try:
        yield
    finally:
        if main:
            signal.signal(signal.SIGTERM, previous or signal.SIG_DFL)
