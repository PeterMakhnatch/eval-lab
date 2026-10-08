"""Opt-in live-Docker end-to-end for the cheat-audit lane (HAR-204).

Runs ``evallab cheat run`` against real library tasks on the local Docker
backend at $0 (model-free agent, no provider calls). Strictly opt-in: the
test attempts shared-daemon admission first and skips with reason
``shared daemon not admitted`` on refusal, without touching other owners'
containers. Unit + fixture tests in test_cheat.py carry acceptance.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TASKS = [
    "library/tasks/transaction-reconciliation",
    "library/tasks/query-optimize",
]


def _admission() -> str | None:
    """Return a skip reason unless the shared daemon admits a trial."""
    if shutil.which("docker") is None:
        return "docker CLI unavailable"
    if shutil.which("harbor") is None:
        return "harbor CLI unavailable"
    try:
        from evallab.campaign_execution import docker_available_resources
    except ImportError:
        return "admission helper unavailable"
    try:
        cpus, memory_mb = docker_available_resources()
    except Exception:  # noqa: BLE001 - any refusal means skip
        return "shared daemon not admitted"
    if cpus < 1 or memory_mb < 1024:
        return "shared daemon not admitted"
    return None


@pytest.mark.parametrize("task_rel", TASKS)
def test_cheat_run_live_task(task_rel: str, tmp_path: Path) -> None:
    reason = _admission()
    if reason is not None:
        pytest.skip(reason)
    from evallab.cli import parser

    task = REPO_ROOT / task_rel
    if not (task / "task.toml").is_file():
        pytest.skip(f"task missing: {task_rel}")
    name = f"cheat-e2e-{os.getpid()}-{task.name[:20]}"
    args = parser().parse_args(
        [
            "cheat",
            "run",
            "--task",
            str(task),
            "--name",
            name,
            "--jobs-dir",
            str(tmp_path / "jobs"),
            "--attempts",
            "1",
        ]
    )
    assert args.func(args, REPO_ROOT) == 0
    job_dir = tmp_path / "jobs" / name
    verdicts_path = job_dir / "cheat-verdicts.json"
    assert verdicts_path.is_file(), "cheat run must write cheat-verdicts.json"
    payload = json.loads(verdicts_path.read_text(encoding="utf-8"))
    assert payload["schema"] == "evallab.cheat.verdicts/v1"
    assert payload["agent"] == "cheat"
    assert payload["harbor_rev"], "harbor rev must be recorded in outputs"
    assert len(payload["trials"]) == 1
    trial = payload["trials"][0]
    # The verdict is whatever the benchmark's own verifier returned; the
    # lane asserts plumbing (method + evidence), never exploitability.
    assert trial["verdict"] in {"cracked", "clean"}
    assert trial["method"], "at least one ladder attack must execute"
    assert trial["evidence"], "executed attacks must leave evidence paths"
    attempts = job_dir / trial["trial"] / "cheat" / "attempts.json"
    assert attempts.is_file(), "agent must log attempts with method + evidence"
