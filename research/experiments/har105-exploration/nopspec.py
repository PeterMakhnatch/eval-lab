"""Shared pieces for the HAR-105 scripts: nop evidence lookup and Daytona nop specs."""

from __future__ import annotations

import re
import shutil
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from evallab.registry import compute_task_digests  # noqa: E402
from evallab.storage.paths import shared_checkout_root  # noqa: E402

OUT = Path(__file__).resolve().parent
PRIMARY = shared_checkout_root(ROOT)
#: Worktrees whose ``runs/`` hold the Daytona nop jobs this card reads:
#: HAR-88 (mimo-ops), HAR-95, and HAR-105's own music and fixed-variant nops.
NOP_ROOTS = tuple(
    PRIMARY / ".worktrees" / name / "runs"
    for name in (
        "mimo-ops",
        "har95-nop-20260929",
        "har105-explore-20260929",
        "har105-taskfix-20260929",
    )
)
#: A nop whose verifier output matches this broke before grading: a raised
#: missing module or package, a pytest collection or fixture-setup error, or a
#: missing command. Two near misses are deliberately not matched: "cannot
#: import name X" from the task's own code is often the function the agent
#: must write (1702), and a logged "No module named" (stevedore skipping
#: Bandit's optional sarif formatter in 1789) is not a raised error.
SETUP_ERROR = re.compile(
    r"ModuleNotFoundError|PackageNotFoundError"
    r"|ERROR collecting|ERROR at setup|command not found"
)
MARGIN_S = 300
DAYTONA_MAX_DISK_MB = 10240


def nop_trial_dir(job_name: str) -> Path | None:
    """The single trial directory of a nop job, searched across ``NOP_ROOTS``."""
    for root in NOP_ROOTS:
        job = root / job_name
        if job.is_dir():
            trials = [p for p in job.iterdir() if p.is_dir() and "__" in p.name]
            if len(trials) == 1:
                return trials[0]
    return None


def nop_verifier_text(job_name: str) -> str | None:
    trial = nop_trial_dir(job_name)
    stdout = trial / "verifier" / "test-stdout.txt" if trial else None
    return stdout.read_text(errors="replace") if stdout and stdout.is_file() else None


def stage(rel: str) -> Path:
    """``ROOT/rel``, copied from the shared checkout when this is a worktree.

    Queue specs name tasks relative to the checkout that submits them, and a
    worktree's ``derived/`` starts empty.
    """
    task = ROOT / rel
    if not task.exists():
        shutil.copytree(PRIMARY / rel, task)
    return task


def nop_spec(rel: str, name: str, hypothesis: str) -> dict:
    """A Daytona nop spec for the task package at checkout-relative ``rel``."""
    task = stage(rel)
    config = tomllib.loads((task / "task.toml").read_text())
    env = config.get("environment", {})
    timeout = int(
        env.get("build_timeout_sec", 0)
        + env.get("healthcheck", {}).get("timeout_sec", 0)
        + config.get("verifier", {}).get("timeout_sec", 0)
        + MARGIN_S
    )
    spec = {
        "task": rel,
        "task_package_digest": compute_task_digests(task).package,
        "agent": "nop",
        "environment": "daytona",
        "name": name,
        "jobs_dir": "runs",
        "attempts": 1,
        "timeout_seconds": timeout,
        "purpose": "calibration",
        "hypothesis": hypothesis,
        "submitted_by": "har105-qualification",
    }
    if env.get("storage_mb") is None:
        spec["override_storage_mb"] = DAYTONA_MAX_DISK_MB
    return spec
