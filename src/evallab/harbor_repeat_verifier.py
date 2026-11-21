from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from harbor.models.trial.paths import EnvironmentPaths
from harbor.verifier.base import BaseVerifier

from evallab import execution_contracts

SCHEMA = "evallab.verifier_stability/v1"
DEFAULT_REPEAT_N = 3
STDOUT_TAIL_BYTES = 8192
STABILITY_FILENAME = "stability.json"
REPEAT_DIRNAME = "repeat"


def _coerce_repeat_n(raw: Any) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"repeat_n must be an integer, got {raw!r}") from exc
    if value < 1:
        raise ValueError(f"repeat_n must be >= 1, got {value}")
    return value


def _scalar_reward(rewards: Any) -> float | None:
    if rewards is None:
        return None
    if not isinstance(rewards, dict) or not rewards:
        return None
    raw = rewards.get("reward", next(iter(rewards.values())))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _stdout_tail_digest(test_stdout_path: Path) -> str | None:
    try:
        data = test_stdout_path.read_bytes()[-STDOUT_TAIL_BYTES:]
    except OSError:
        return None
    if not data:
        return None
    return hashlib.sha256(data).hexdigest()


def _harbor_version() -> str:
    return version("harbor")


class _ExecRecorder:

    def __init__(self, wrapped: Any) -> None:
        self._wrapped = wrapped
        self.calls: list[dict[str, Any]] = []

    async def exec(self, command: str, *args: Any, **kwargs: Any) -> Any:
        result = await self._wrapped.exec(command, *args, **kwargs)
        self.calls.append({"command": command, "return_code": result.return_code})
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)


def _test_exit_code(calls: list[dict[str, Any]]) -> int | None:
    for call in reversed(calls):
        command = str(call["command"]).lstrip()
        if command.startswith("chmod") or command.startswith("rm -f"):
            continue
        code = call["return_code"]
        return int(code) if code is not None else None
    if calls:
        code = calls[-1]["return_code"]
        return int(code) if code is not None else None
    return None


class RepeatVerifier(BaseVerifier):

    def __init__(self, *, repeat_n: Any = DEFAULT_REPEAT_N, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._passthrough = kwargs
        self._repeat_n = _coerce_repeat_n(repeat_n)
        self._inner_factory: Any | None = kwargs.pop("_inner_factory", None)
        self._logger = kwargs.get("logger") or logging.getLogger(__name__)

    def _make_inner(self, environment: Any) -> Any:
        passthrough = {
            key: value
            for key, value in self._passthrough.items()
            if key not in ("task", "trial_paths", "environment", "_inner_factory")
        }
        if self._inner_factory is not None:
            return self._inner_factory(
                task=self.task,
                trial_paths=self.trial_paths,
                environment=environment,
                **passthrough,
            )

        from harbor.verifier.verifier import Verifier  # ty: ignore[unresolved-import]

        return Verifier(
            task=self.task,
            trial_paths=self.trial_paths,
            environment=environment,
            override_env=passthrough.get("override_env"),
            logger=passthrough.get("logger"),
            verifier_env=passthrough.get("verifier_env"),
            step_name=passthrough.get("step_name"),
            include_logs=passthrough.get("include_logs"),
            exclude_logs=passthrough.get("exclude_logs"),
        )

    def _host_verifier_dir(self) -> Path:
        return self.trial_paths.verifier_dir

    def _container_reward_paths(self) -> tuple[str, str]:
        env_paths = EnvironmentPaths.for_os(self.environment.os)
        return str(env_paths.reward_text_path), str(env_paths.reward_json_path)

    async def _clear_reward_files(self) -> None:
        txt, js = self._container_reward_paths()
        try:
            await self.environment.exec(f"rm -f {txt} {js}", user="root")
        except Exception as exc:  # noqa: BLE001 — best effort, host clear follows.
            self._logger.warning("repeat verifier could not clear container rewards: %s", exc)
        for attr in ("reward_text_path", "reward_json_path"):
            try:
                Path(str(getattr(self.trial_paths, attr))).unlink(missing_ok=True)
            except (AttributeError, OSError) as exc:
                self._logger.warning("repeat verifier could not clear host %s: %s", attr, exc)

    def _snapshot_run(self, index: int, snapshots_ok: set[int]) -> None:
        src = self._host_verifier_dir()
        dst = src / REPEAT_DIRNAME / str(index)
        try:
            if dst.exists():
                shutil.rmtree(dst)
            dst.mkdir(parents=True)
            for child in sorted(src.iterdir()):
                if child.name in (REPEAT_DIRNAME, STABILITY_FILENAME):
                    continue
                target = dst / child.name
                if child.is_dir() and not child.is_symlink():
                    shutil.copytree(child, target)
                else:
                    shutil.copy2(child, target, follow_symlinks=False)
            snapshots_ok.add(index)
        except OSError as exc:
            self._logger.warning("repeat verifier could not snapshot run %d: %s", index, exc)

    def _restore_run(self, index: int, snapshots_ok: set[int]) -> None:
        if index not in snapshots_ok:
            self._logger.warning("repeat verifier skips restore of run %d (no snapshot)", index)
            return
        top = self._host_verifier_dir()
        src = top / REPEAT_DIRNAME / str(index)
        try:
            for child in sorted(top.iterdir()):
                if child.name in (REPEAT_DIRNAME, STABILITY_FILENAME):
                    continue
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            for child in sorted(src.iterdir()):
                target = top / child.name
                if child.is_dir() and not child.is_symlink():
                    shutil.copytree(child, target)
                else:
                    shutil.copy2(child, target, follow_symlinks=False)
        except OSError as exc:
            self._logger.warning("repeat verifier could not restore run %d: %s", index, exc)

    async def verify(self) -> Any:
        runs: list[dict[str, Any]] = []
        first_result: Any = None
        first_error: BaseException | None = None
        snapshots_ok: set[int] = set()
        for index in range(self._repeat_n):
            if index >= 1:
                await self._clear_reward_files()
            recorder = _ExecRecorder(self.environment)
            start = time.monotonic()
            try:
                result = await self._make_inner(recorder).verify()
                duration = time.monotonic() - start
                rewards = result.rewards if result is not None else None
                reward = _scalar_reward(rewards)
                runs.append({
                    "index": index,
                    "reward": reward,
                    "rewards": dict(rewards) if isinstance(rewards, dict) else rewards,
                    "error": None if reward is not None else "verifier returned no reward",
                    "duration_sec": round(duration, 3),
                    "exit_code": _test_exit_code(recorder.calls),
                    "stdout_tail_sha256": _stdout_tail_digest(
                        self.trial_paths.test_stdout_path
                    ),
                })
                if index == 0:
                    first_result = result
            except Exception as exc:  # noqa: BLE001 — every run is recorded.
                duration = time.monotonic() - start
                runs.append({
                    "index": index,
                    "reward": None,
                    "rewards": None,
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                    "duration_sec": round(duration, 3),
                    "exit_code": _test_exit_code(recorder.calls),
                    "stdout_tail_sha256": _stdout_tail_digest(
                        self.trial_paths.test_stdout_path
                    ),
                })
                if index == 0:
                    first_error = exc
            self._snapshot_run(index, snapshots_ok)
        self._restore_run(0, snapshots_ok)
        self._write_stability(runs)
        if first_error is not None:
            raise first_error
        return first_result

    def _write_stability(self, runs: list[dict[str, Any]]) -> None:
        payload = {
            "schema": SCHEMA,
            "verifier": execution_contracts.VERIFIER_IMPORT_PATH,
            "harbor_version": _harbor_version(),
            "repeat_n_requested": self._repeat_n,
            "n_runs": len(runs),
            "first_reward": runs[0]["reward"] if runs else None,
            "repeat_dir": REPEAT_DIRNAME,
            "runs": runs,
            "produced_at": datetime.now(UTC).isoformat(),
        }
        try:
            verifier_dir = self._host_verifier_dir()
            verifier_dir.mkdir(parents=True, exist_ok=True)
            (verifier_dir / STABILITY_FILENAME).write_text(json.dumps(payload, indent=2))
        except OSError as exc:
            self._logger.warning("repeat verifier could not write %s: %s", STABILITY_FILENAME, exc)
