"""Repeat-verification Harbor verifier for HAR-83 verifier stability (§4.2.1).

Opt-in custom verifier: after the agent finishes, it runs the task's own
verifier ``k`` times on the same final container state and records every run
(reward or null, exit code, duration, stdout-tail digest) to
``/logs/verifier/stability.json``. It reports the FIRST run's reward as the
trial reward, so it never changes the result it audits.

Two validity guards keep the audit honest:

- Harbor's default verifier never clears ``reward.{txt,json}``: it reads
  whatever file exists. Before every run with index ≥ 1 the repeat verifier
  removes both reward files in the container (``rm -f`` as root) and on the
  host, so a run that dies before writing is recorded as ``reward=null``
  instead of inheriting the previous run's stale reward.
- After all runs, the host ``verifier/`` top level holds exactly what a
  normal single-verify trial would contain for run 0 (``reward.txt``,
  ``test-stdout.txt``, ``agent.diff``, …) plus ``verifier/repeat/<i>/`` with
  each run's files and ``stability.json``. Per-run files are moved aside
  between runs, which also clears the Docker bind mount.

Harbor 0.21 hook (verified against installed Harbor 0.21.0)::

    harbor run --path <task> --agent nop \\
        --verifier evallab.harbor_repeat_verifier:RepeatVerifier \\
        --verifier-kwarg repeat_n=3

The hook replaces the default verifier for the trial (``trial.py``
``_run_shared_verifier`` calls ``VerifierFactory.create_verifier_from_config``,
which honors ``import_path`` + ``kwargs``), so this class subclasses
``harbor.verifier.base.BaseVerifier`` and delegates each run to Harbor's
default ``harbor.verifier.verifier.Verifier``. Harbor is not an Eval Lab
dependency, so all Harbor imports here are lazy with importable fallbacks:
unit tests exercise this module without Harbor installed, while the Harbor
runtime (which has Harbor installed) resolves the real base class for its
``issubclass`` import-path check.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "evallab.verifier_stability/v1"
VERIFIER_IMPORT_PATH = "evallab.harbor_repeat_verifier:RepeatVerifier"
DEFAULT_REPEAT_N = 3
STDOUT_TAIL_BYTES = 8192
STABILITY_FILENAME = "stability.json"
REPEAT_DIRNAME = "repeat"
try:  # Harbor runtime: real base so the import-path check passes.
    from harbor.verifier.base import BaseVerifier  # ty: ignore[unresolved-import]
except ImportError:  # Repo venv (Harbor is not a dependency): test fallback.

    class BaseVerifier:  # type: ignore[no-redef]
        """Fallback base used only when Harbor is not installed."""

        def __init__(self, **kwargs: Any) -> None:
            self._fallback_kwargs = kwargs


def _coerce_repeat_n(raw: Any) -> int:
    """Coerce the ``repeat_n`` verifier kwarg (Harbor passes strings)."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"repeat_n must be an integer, got {raw!r}") from exc
    if value < 1:
        raise ValueError(f"repeat_n must be >= 1, got {value}")
    return value


def _scalar_reward(rewards: Any) -> float | None:
    """Reduce a Harbor rewards dict to one scalar for the stability table."""
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
    """Sha256 of the last bytes of the downloaded test stdout (null if absent)."""
    try:
        data = test_stdout_path.read_bytes()[-STDOUT_TAIL_BYTES:]
    except OSError:
        return None
    if not data:
        return None
    return hashlib.sha256(data).hexdigest()


def _harbor_version() -> str | None:
    try:
        from harbor import __version__ as version  # ty: ignore[unresolved-import]
    except ImportError:
        return None
    return str(version)


class _ExecRecorder:
    """Proxy around a Harbor environment that records ``exec`` return codes.

    Harbor's ``Verifier`` API surfaces only the parsed reward, not the test
    script's exit code, so the repeat verifier watches the exec calls the
    inner verifier makes. The test run is the last exec that is not the
    ``chmod +x`` preparation step.
    """

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
    """Run the task verifier ``repeat_n`` times; report the first reward."""

    def __init__(self, *args: Any, repeat_n: Any = DEFAULT_REPEAT_N, **kwargs: Any) -> None:
        # Harbor's factory passes (task, trial_paths, environment, ...) either
        # positionally or by keyword; inner-verifier construction below reuses
        # exactly what this wrapper received.
        super().__init__(*args, **kwargs)
        task = args[0] if args else kwargs.get("task")
        trial_paths = args[1] if len(args) > 1 else kwargs.get("trial_paths")
        environment = args[2] if len(args) > 2 else kwargs.get("environment")
        self._task = task
        self._trial_paths = trial_paths
        self._environment = environment
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
                task=self._task,
                trial_paths=self._trial_paths,
                environment=environment,
                **passthrough,
            )

        from harbor.verifier.verifier import Verifier  # ty: ignore[unresolved-import]

        return Verifier(
            task=self._task,
            trial_paths=self._trial_paths,
            environment=environment,
            override_env=passthrough.get("override_env"),
            logger=passthrough.get("logger"),
            verifier_env=passthrough.get("verifier_env"),
            step_name=passthrough.get("step_name"),
            include_logs=passthrough.get("include_logs"),
            exclude_logs=passthrough.get("exclude_logs"),
        )

    def _host_verifier_dir(self) -> Path:
        return Path(str(self._trial_paths.verifier_dir))

    def _container_reward_paths(self) -> tuple[str, str]:
        """Reward file paths inside the container (Harbor layout, POS fallback)."""
        try:
            from harbor.models.trial.paths import (  # ty: ignore[unresolved-import]
                EnvironmentPaths,
            )

            env_paths = EnvironmentPaths.for_os(self._environment.os)
            return str(env_paths.reward_text_path), str(env_paths.reward_json_path)
        except Exception:  # noqa: BLE001 — Harbor absent (tests) or odd backend.
            return "/logs/verifier/reward.txt", "/logs/verifier/reward.json"

    async def _clear_reward_files(self) -> None:
        """Remove stale reward files (container + host) before reruns.

        Harbor's verifier reads whatever reward file exists, so without this
        a run that dies before writing would inherit the previous run's
        reward and look falsely stable.
        """
        txt, js = self._container_reward_paths()
        try:
            await self._environment.exec(f"rm -f {txt} {js}", user="root")
        except Exception as exc:  # noqa: BLE001 — best effort, host clear follows.
            self._logger.warning("repeat verifier could not clear container rewards: %s", exc)
        for attr in ("reward_text_path", "reward_json_path"):
            try:
                Path(str(getattr(self._trial_paths, attr))).unlink(missing_ok=True)
            except (AttributeError, OSError) as exc:
                self._logger.warning("repeat verifier could not clear host %s: %s", attr, exc)

    def _snapshot_run(self, index: int, snapshots_ok: set[int]) -> None:
        """Preserve this run's host verifier files under ``repeat/<index>/``."""
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
        """Restore one run's files to the verifier top level (run 0 at the end)."""
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
            recorder = _ExecRecorder(self._environment)
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
                        Path(str(self._trial_paths.test_stdout_path))
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
                        Path(str(self._trial_paths.test_stdout_path))
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
            "verifier": VERIFIER_IMPORT_PATH,
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
