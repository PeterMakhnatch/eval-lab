from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from evallab.results import load_job
from evallab.schemas import ExperimentMatrix, MatrixRun, RunProvenance

CONTROL_AGENTS = {"oracle", "nop"}
SAFE_JOB_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")


@dataclass(frozen=True)
class RunRequest:
    task: Path
    agent: str
    name: str
    jobs_dir: Path
    environment: str = "docker"
    model: str | None = None
    concurrency: int = 1
    attempts: int = 1
    allow_billable: bool = False
    provenance: RunProvenance | None = None


def tool_version(command: str) -> str | None:
    executable = shutil.which(command)
    if not executable:
        return None
    completed = subprocess.run(
        [executable, "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    output = (completed.stdout or completed.stderr).strip()
    return output.splitlines()[0] if output else None


def git_state(root: Path) -> dict[str, Any]:
    if not (root / ".git").exists():
        return {"commit": None, "dirty": None}
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    return {
        "commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
    }


def validate_request(request: RunRequest) -> None:
    if not request.task.is_dir():
        raise ValueError(f"Task directory does not exist: {request.task}")
    if not (request.task / "task.toml").is_file():
        raise ValueError(f"Task directory has no task.toml: {request.task}")
    if not SAFE_JOB_NAME.fullmatch(request.name):
        raise ValueError("Job names must be 3-80 lowercase letters, numbers, or hyphens")
    if request.concurrency < 1 or request.attempts < 1:
        raise ValueError("Concurrency and attempts must be positive")
    if request.agent not in CONTROL_AGENTS and not request.allow_billable:
        raise ValueError(
            f"Agent {request.agent!r} may invoke a model. Pass --allow-billable "
            "after reviewing credentials, model, and expected cost."
        )
    if request.model and request.agent in CONTROL_AGENTS:
        raise ValueError(f"The {request.agent} control does not accept a model")
    if request.model and not request.allow_billable:
        raise ValueError("A model requires --allow-billable")


def build_command(request: RunRequest) -> list[str]:
    command = [
        "harbor",
        "run",
        "--path",
        str(request.task),
        "--agent",
        request.agent,
        "--env",
        request.environment,
        "--job-name",
        request.name,
        "--jobs-dir",
        str(request.jobs_dir),
        "--n-concurrent",
        str(request.concurrency),
        "--n-attempts",
        str(request.attempts),
    ]
    if request.model:
        command.extend(["--model", request.model])
    return command


def run_experiment(request: RunRequest, *, repo_root: Path) -> Path:
    validate_request(request)
    if not shutil.which("harbor"):
        raise RuntimeError("harbor is not installed or not on PATH")

    job_dir = request.jobs_dir / request.name
    if job_dir.exists():
        raise FileExistsError(
            f"Refusing to reuse existing job directory: {job_dir}. Choose a new explicit run name."
        )

    request.jobs_dir.mkdir(parents=True, exist_ok=True)
    command = build_command(request)
    started = datetime.now(UTC)
    completed = subprocess.run(command, cwd=repo_root, check=False)
    finished = datetime.now(UTC)

    if job_dir.exists():
        metadata = {
            "schema_version": 1,
            "command": command,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "exit_code": completed.returncode,
            "host": {
                "platform": platform.platform(),
                "python": sys.version.split()[0],
            },
            "tools": {
                "harbor": tool_version("harbor"),
                "docker": tool_version("docker"),
                "uv": tool_version("uv"),
            },
            "repository": git_state(repo_root),
        }
        if request.provenance is not None:
            metadata["experiment"] = request.provenance.model_dump(mode="json")
        (job_dir / "lab-metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n"
        )

    if completed.returncode != 0:
        raise RuntimeError(
            f"Harbor exited with {completed.returncode}. Inspect {job_dir} if it exists."
        )
    load_job(job_dir)
    return job_dir


def load_matrix(path: Path) -> ExperimentMatrix:
    try:
        return ExperimentMatrix.model_validate_json(path.read_text())
    except (OSError, ValidationError) as exc:
        raise ValueError(f"Invalid experiment matrix {path}: {exc}") from exc


def request_from_matrix(
    matrix: ExperimentMatrix, run: MatrixRun, *, repo_root: Path
) -> RunRequest:
    return RunRequest(
        task=(repo_root / matrix.task).resolve(),
        agent=run.agent,
        name=run.name,
        jobs_dir=(repo_root / matrix.jobs_dir).resolve(),
        environment=matrix.environment,
        model=run.model,
        concurrency=matrix.concurrency,
        attempts=run.attempts,
        allow_billable=run.allow_billable,
    )


def expected_primary_reward(run: MatrixRun) -> float | None:
    return run.expect_reward


def database_url_from_environment(explicit: str | None = None) -> str:
    return explicit or os.environ.get(
        "DATABASE_URL",
        "postgresql://evallab:local-development-only@localhost:54329/evallab",
    )
