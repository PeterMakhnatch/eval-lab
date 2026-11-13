"""Reuse dependency imports, never SDK state, in private native worker processes."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import threading
import traceback
from contextlib import suppress
from pathlib import Path


class NativeWorkers:
    def __init__(self, python: Path, root: Path, scratch: Path):
        self.python = python
        self.root = root
        self.scratch = scratch
        self.process = None
        self.errors = None

    def _receive(self, timeout):
        if not select.select([self.process.stdout], [], [], timeout)[0]:
            raise subprocess.TimeoutExpired(self.process.args, timeout)
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError(f"native launcher exited: {self.scratch / 'launcher.stderr'}")
        return json.loads(line)

    def run(self, command, *, input, **kwargs):
        # macOS truststore imports load system frameworks that are not fork-safe.
        if sys.platform != "linux":
            return subprocess.run(command, input=input, **kwargs)
        timeout = kwargs["timeout"]
        try:
            if self.process is None:
                result = subprocess.run(command, input=input, **kwargs)
                self.errors = (self.scratch / "launcher.stderr").open("w")
                self.process = subprocess.Popen(
                    [str(self.python), "-I", str(Path(__file__).resolve())],
                    cwd=self.root,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    start_new_session=True,
                    stderr=self.errors,
                    text=True,
                )
                if self._receive(timeout) != {"ready": True}:
                    raise RuntimeError("native launcher did not become ready")
                return result
            output = Path(json.loads(input)["native_logs_dir"]).parent
            stdin, stdout, stderr = [output / f"worker.{name}" for name in ("stdin", "stdout", "stderr")]
            stdin.write_text(input)
            self.process.stdin.write(json.dumps({
                "command": command[2:], "cwd": str(self.root), "env": dict(os.environ),
                "stdin": str(stdin), "stdout": str(stdout), "stderr": str(stderr),
            }) + "\n")
            self.process.stdin.flush()
            result = subprocess.CompletedProcess(
                command, self._receive(timeout)["returncode"], stdout.read_text(), stderr.read_text(),
            )
            result.check_returncode()
            return result
        except BaseException:
            self.close()
            raise

    def close(self):
        process, self.process = self.process, None
        errors, self.errors = self.errors, None
        try:
            if process is not None:
                with suppress(OSError):
                    process.stdin.close()
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
                # Also stop a child whose launcher has already exited.
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                with suppress(OSError):
                    process.stdout.close()
        finally:
            if errors is not None:
                errors.close()


def _serve():
    # Import no Xiaomi SDK modules here: its .env, log contexts, patches, tools,
    # clients and threads must all initialize after fork in each private child.
    import httpx2  # noqa: F401
    import openai  # noqa: F401
    import yaml  # noqa: F401

    if threading.active_count() != 1:
        raise RuntimeError("native dependency preload started threads; refusing unsafe fork")
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        pid = os.fork()
        if pid == 0:
            code = 0
            try:
                for fd, name in enumerate(("stdin", "stdout", "stderr")):
                    flags = os.O_RDONLY if fd == 0 else os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                    opened = os.open(request[name], flags, 0o600)
                    os.dup2(opened, fd)
                    os.close(opened)
                os.environ.clear()
                os.environ.update(request["env"])
                tempfile.tempdir = None
                # The daemon RPC reader owns stdin until process exit.
                sys.stdin = open(0, encoding="utf-8", closefd=False)  # noqa: SIM115
                sys.argv = ["-c", *request["command"][2:]]
                os.chdir(request["cwd"])
                exec(request["command"][1], {"__name__": "__main__"})
            except BaseException:
                traceback.print_exc()
                code = 1
            finally:
                sys.stdout.flush()
                sys.stderr.flush()
            os._exit(code)
        _, status = os.waitpid(pid, 0)
        print(json.dumps({"returncode": os.waitstatus_to_exitcode(status)}), flush=True)


if __name__ == "__main__":
    _serve()
