"""Shared pieces for the HAR-105 scripts: nop evidence lookup and Daytona nop specs."""

from __future__ import annotations

import shutil
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from evallab.registry import compute_task_digests  # noqa: E402
from evallab.storage.paths import shared_checkout_root  # noqa: E402
#: A nop whose verifier output matches this broke before grading: a raised
#: missing module or package, a pytest collection or fixture-setup error, a
#: conftest that cannot import, a package whose C extensions were never built
#: (pandas, CuPy in part 3), or a missing command. Two near misses are
#: deliberately not matched: "cannot import name X" from the task's own code
#: is often the function the agent must write (1702), and a logged "No module
#: named" (stevedore skipping Bandit's optional sarif formatter in 1789) is not
#: a raised error. A pytest collection error can also be the missing feature
#: itself; ``select_python.SETUP_ERROR_OK`` records those checked by hand.
#: The rule lives in ``evallab.task_health``; this re-exports it.
from evallab.task_health import SETUP_ERROR  # noqa: E402

OUT = Path(__file__).resolve().parent
PRIMARY = shared_checkout_root(ROOT)
#: Worktrees whose ``runs/`` hold the Daytona nop jobs this card reads:
#: HAR-88 (mimo-ops), HAR-95, and HAR-105's own music, fixed-variant and Python-code nops.
NOP_ROOTS = tuple(
    PRIMARY / ".worktrees" / name / "runs"
    for name in (
        "mimo-ops",
        "har95-nop-20260929",
        "har105-explore-20260929",
        "har105-taskfix-20260929",
        "har105-python-20260930",
    )
)
SNAPSHOTS = {
    "code": "FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6",
    "cyber": "FineEnvs__MiMo-V2.6-RL-harbor-cyber@763882ade5fc",
    "general": "FineEnvs__MiMo-V2.6-RL-harbor-general@10b732c5079c",
    "music": "FineEnvs__MiMo-V2.6-RL-harbor-music@e1a66d4553ee",
    "terminal": "FineEnvs__MiMo-V2.6-RL-harbor-terminal@fe1c2b665aae",
    "webdev": "FineEnvs__MiMo-V2.6-RL-harbor-webdev@e1a6293376e8",
}
#: HAR-97 suspects (fixed as variants in part 2) and 2684 (pass_tainted: the
#: same missing-stevedore environment as 1789).
SUSPECTS = {
    "candidate-1634-software-databases",
    "candidate-1789-security-appsec",
    "candidate-1702-ml-inference",
    "candidate-2684-security-appsec",
    "arvo_18737",
    "arvo_57589",
}
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
