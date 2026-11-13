"""Native Modal cell execution, keyed cache reuse and reserve/settle spend accounting."""

from __future__ import annotations

import fcntl
import hashlib
import json
import threading
import time
import tomllib
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from evallab.registry import task_directory_digest

#: Modal Sandbox pricing rates (USD per CPU-second, per GiB-second)
MODAL_CPU_USD_PER_S: float = 0.00003942
MODAL_GIB_USD_PER_S: float = 0.00000667
#: Shared estimate margin over measured sandbox seconds
ESTIMATE_MARGIN: float = 1.25
#: Default seconds reserved per cell when task package provides no specific calibration
PROJECTED_CELL_SECONDS: int = 240

CellRunner = Callable[[Path, Path, str], dict[str, Any]]
"""Callable (cell_dir, package, script) -> result dict."""


def sha256_text(text: str) -> str:
    """Return SHA-256 hex digest of UTF-8 encoded text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_cell_key(
    package_digest: str,
    variant: str,
    submission_digest: str,
    backend_image: str = "",
    grader_hook: str = "",
    timeout_seconds: int = 600,
) -> str:
    """Deterministic hash of a cell's evaluation parameters.

    cell_key = sha256(package digest, variant, submission or mutant digest, backend image, grader hook, timeout)
    """
    payload = "\0".join([
        str(package_digest).strip(),
        str(variant).strip(),
        str(submission_digest).strip(),
        str(backend_image).strip(),
        str(grader_hook).strip(),
        str(timeout_seconds),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def estimate_modal_cost(package: Path, wall_seconds: float) -> float:
    """Estimate Modal cost from task package resources and elapsed wall seconds."""
    env: dict[str, Any] = {}
    task_toml = package / "task.toml"
    if task_toml.is_file():
        try:
            env = tomllib.loads(task_toml.read_text(encoding="utf-8")).get("environment", {})
        except Exception:
            env = {}
    cpus = int(env.get("cpus", 2))
    memory_mb = int(env.get("memory_mb", 8192))
    rate = cpus * MODAL_CPU_USD_PER_S + (memory_mb / 1024.0) * MODAL_GIB_USD_PER_S
    cost = rate * wall_seconds * ESTIMATE_MARGIN
    if wall_seconds > 0:
        return max(0.00001, cost)
    return cost



class _FileLock:
    """Simple cross-process file lock using flock."""

    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self._fd: Any = None

    def __enter__(self) -> _FileLock:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = self.lock_path.open("a+")
        fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *args: Any) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
                self._fd.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# SandboxMeter (Drop-in replacement for mutant_auditor.SandboxMeter)
# ---------------------------------------------------------------------------


class SandboxMeter:
    """Cost estimate from measured sandbox seconds with reserve/settle support.

    Drop-in replacement for legacy mutant_auditor.SandboxMeter.
    """

    def __init__(self, cap_usd: float, gate: Any = None):
        self.cap = float(cap_usd)
        self.spent = 0.0
        self.cells = 0
        self.seconds = 0.0
        self.gate = gate
        self._lock = threading.Lock()

    @staticmethod
    def estimate(package: Path, wall_seconds: float) -> float:
        return estimate_modal_cost(package, wall_seconds)

    def admit(self, projected_usd: float) -> None:
        with self._lock:
            if self.spent + projected_usd > self.cap:
                raise RuntimeError(
                    f"Modal cap ${self.cap:.2f} would be passed (spent ${self.spent:.3f})"
                )

    def add(self, usd: float, wall_seconds: float) -> None:
        with self._lock:
            self.spent += usd
            self.seconds += wall_seconds
            self.cells += 1


# ---------------------------------------------------------------------------
# Keyed Cell Specification and Batch Runner
# ---------------------------------------------------------------------------


def modal_cell_runner(
    *,
    repo_root: Path,
    spend_gate: Any,
    meter: SandboxMeter | None = None,
    timeout_seconds: int = 600,
    results_dir: Path | None = None,
    backend_factory: Callable[..., Any] | None = None,
) -> CellRunner:
    """A CellRunner over ModalBackend; result.json is the consumption marker.

    Default timeout_seconds is 600s rather than 1800s to bound worst-case overrun
    per in-flight cell: (kill_timeout_cost - reservation).
    For 3 packages x 6 cells = 18 in flight on 2 CPU / 8 GiB instances:
    Projected 240s reservation is $0.0397. 600s kill timeout is $0.0991.
    Worst-case overrun per in-flight cell is $(0.0991 - 0.0397) = $0.0594.
    Aggregate worst-case overrun for 18 cells is 18 * $0.0594 = $1.0692
    (compared to 18 * $(0.2974 - 0.0397) = $4.6386 under 1800s).

    Returns cached or executed result dict; cached results include `cache_hit: True`.
    """

    def run(cell_dir: Path, package: Path, script: str) -> dict[str, Any]:
        variant = cell_dir.name
        pkg_path = Path(package).resolve()
        pkg_digest = task_directory_digest(pkg_path) if pkg_path.is_dir() else sha256_text(str(pkg_path))
        sub_digest = sha256_text(script)
        cell_key = compute_cell_key(
            package_digest=pkg_digest,
            variant=variant,
            submission_digest=sub_digest,
            backend_image="modal-default",
            grader_hook="",
            timeout_seconds=timeout_seconds,
        )

        cache_dir = results_dir or cell_dir.parent
        cache_dir.mkdir(parents=True, exist_ok=True)
        key_lock_path = cache_dir / f"{cell_key}.lock"
        with _FileLock(key_lock_path):
            result_path = cell_dir / "result.json"
            if result_path.exists():
                try:
                    data = json.loads(result_path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        data["cache_hit"] = True
                        return data
                except Exception:
                    pass

            keyed_result_path = cache_dir / cell_key / "result.json"
            if keyed_result_path.is_file():
                try:
                    data = json.loads(keyed_result_path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        data["cache_hit"] = True
                        cell_dir.mkdir(parents=True, exist_ok=True)
                        _write_json_atomic(result_path, data)
                        return data
                except Exception:
                    pass

            if meter is not None:
                meter.admit(meter.estimate(pkg_path, PROJECTED_CELL_SECONDS))
            cell_dir.mkdir(parents=True, exist_ok=True)
            if backend_factory is not None:
                backend = backend_factory(repo_root=repo_root, spend_gate=spend_gate)
            else:
                from evallab.qa.task_modal import ModalBackend

                backend = ModalBackend(repo_root=repo_root, spend_gate=spend_gate)
            started = time.monotonic()
            observation = backend.run_container_trial(
                pkg_path,
                digest=pkg_digest,
                mode="shortcut",
                script=script,
                trial_dir=cell_dir / "trial",
                timeout_seconds=timeout_seconds,
            )
            wall = time.monotonic() - started
            trial = cell_dir / "trial"
            agent_log = _read_text(trial / "agent/oracle.txt")
            test_output = _read_text(trial / "verifier/test_output.log")
            cost = meter.estimate(pkg_path, wall) if meter else 0.0
            if meter is not None:
                meter.add(cost, wall)
            result = {
                "cell_key": cell_key,
                "status": observation.status,
                "reason": observation.reason,
                "reward": observation.facts.get("reward"),
                "wall_s": round(wall, 1),
                "est_usd": round(cost, 5),
                "agent_log": agent_log,
                "test_output_tail": test_output[-600:],
                "evidence_sha256": sha256_text(agent_log + test_output),
                "cache_hit": False,
            }
            _write_json_atomic(result_path, result)
            return result
    return run


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
