"""Host contract for the shipped tau3-retail-1 verifier failure paths.

The verifier runs in the task container with Python 3 and the tau2-bench
runtime under ``/opt/tau2-bench``. This module runs the unchanged evaluator by
path with ``python -I``, redirects ``--runtime-log``, ``--reward`` and
``--result`` into ``tmp_path``, and points ``TAU2_BENCH_ROOT`` at an empty
directory so no host runtime is used. Every failure path must write reward
``0.0`` and a status: a regression that crashes before writing, or that scores
an unscorable log, otherwise shows only inside Docker.

The package is vendored from tau3-bench and pinned by digest, so the contract
lives here instead of ``library/tasks/tau3-retail-1``.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "library" / "tasks" / "tau3-retail-1"
EVALUATOR = PACKAGE / "tests" / "evaluate.py"
CONFIG = PACKAGE / "tests" / "config.json"


def package_digest() -> str:
    """Digest over every file in the package, so a run cannot rewrite bytes."""
    digest = hashlib.sha256()
    for path in sorted(candidate for candidate in PACKAGE.rglob("*") if candidate.is_file()):
        digest.update(path.relative_to(PACKAGE).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


@pytest.fixture(autouse=True)
def unchanged_package() -> Iterator[None]:
    before = package_digest()
    yield
    assert package_digest() == before
    assert not list(PACKAGE.rglob("__pycache__"))


@pytest.mark.parametrize(
    ("runtime_log", "expected_status"),
    [
        pytest.param(None, "missing_runtime_log", id="missing-log"),
        pytest.param("{bad", "invalid_runtime_log", id="truncated-json"),
        pytest.param("[1,2]", "invalid_runtime_log", id="json-not-object"),
        pytest.param(
            '{"messages": [], "termination_reason": "agent_stop"}',
            "tau2_runtime_log_error",
            id="empty-trajectory",
        ),
        pytest.param(
            '{"termination_reason": "agent_stop"}',
            "tau2_runtime_log_error",
            id="no-messages",
        ),
    ],
)
def test_unscorable_runtime_log_writes_zero_reward(
    tmp_path: Path, runtime_log: str | None, expected_status: str
) -> None:
    tau2_root = tmp_path / "empty-tau2-bench"
    tau2_root.mkdir()
    log_path = tmp_path / "logs" / "agent" / "tau3_runtime_state.json"
    log_path.parent.mkdir(parents=True)
    if runtime_log is not None:
        log_path.write_text(runtime_log, encoding="utf-8")
    reward_path = tmp_path / "logs" / "verifier" / "reward.txt"
    result_path = tmp_path / "logs" / "verifier" / "result.json"

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(EVALUATOR),
            "--config",
            str(CONFIG),
            "--runtime-log",
            str(log_path),
            "--reward",
            str(reward_path),
            "--result",
            str(result_path),
        ],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "TAU2_BENCH_ROOT": str(tau2_root)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == expected_status
    assert result["reward"] == 0.0
    assert result["used_tau2_evaluator"] is False
    assert reward_path.read_text(encoding="utf-8") == "0.0"
