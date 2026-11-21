from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from evallab.harbor_telemetry import harbor_child_environment

DATASET_DIGEST = "sha256:39d9f44b40420cde8fdcc087579c0d72a7e14fa3656d603c3f0d22fb35e27732"


class HubError(RuntimeError):
    pass


class IntegrityError(HubError):
    pass


def _safe(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    for part in (*reversed(path.parents), path):
        if part.is_symlink():
            raise IntegrityError(f"Symlink target is forbidden: {part}")
    return path


def _inside(root: Path, relative: str) -> Path:
    posix = PurePosixPath(relative)
    if not relative or posix.is_absolute() or ".." in posix.parts or "\\" in relative:
        raise IntegrityError("Unsafe manifest path")
    target = _safe(root.joinpath(*posix.parts))
    if not target.is_relative_to(root):
        raise IntegrityError("Manifest path escapes acquisition root")
    return target


class NativeHarbor:

    def __init__(self, executable: str = "harbor", timeout_seconds: int = 600):
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    def _run(self, args: Sequence[str]) -> bytes:
        try:
            result = subprocess.run(
                [self.executable, *args], capture_output=True, check=False,
                timeout=self.timeout_seconds, env=harbor_child_environment(),
            )
        except subprocess.TimeoutExpired as exc:
            raise HubError("Native Harbor command timed out; no automatic retry") from exc
        except OSError as exc:
            raise HubError("Native Harbor executable could not be started") from exc
        if result.returncode:
            raise HubError(f"Native Harbor command failed (exit {result.returncode})")
        return result.stdout

    def json(self, args: Sequence[str]) -> bytes:
        return self._run([*args, "--json"])
