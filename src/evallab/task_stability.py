"""Verifier-stability evidence: verdicts plus ``task_stability.parquet`` (HAR-83 §4.2.1).

Verdict vocabulary (binding contract wording):

- ``stable`` — every recorded run produced a reward and all rewards are equal.
- ``flipped`` — every recorded run produced a reward but rewards differ.
- ``errored`` — at least one run lacks a reward (testbed failure, crash, or
  missing evidence). Infra errors never count as a pass or a fail.

Collection methods: ``repeat_verifier`` (``RepeatVerifier`` re-ran the task
verifier ``k`` times in place), ``nop_repeat`` (plain control trial, one
recorded run), ``diff_replay`` (offline rerun from ``verifier/agent.diff``;
see ``docs/task-stability.md`` for the recipe).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.harbor_repeat_verifier import (
    DEFAULT_REPEAT_N,
    STABILITY_FILENAME,
    VERIFIER_IMPORT_PATH,
)

METHODS = ("repeat_verifier", "nop_repeat", "diff_replay")
VERDICTS = ("stable", "flipped", "errored")

STATE_PRESERVATION = {
    "repeat_verifier": (
        "same container, final agent state kept in place; the task verifier "
        "was re-executed k times via RepeatVerifier with no reset between runs"
    ),
    "nop_repeat": (
        "single verifier run on the final agent state; no repeat performed "
        "(no container retained)"
    ),
    "diff_replay": (
        "container not retained; state rebuilt from the task image + "
        "healthcheck setup + the trial's saved verifier/agent.diff, then "
        "test.sh re-executed k times locally"
    ),
}

MANIFEST_FILENAME = "stability-manifest.json"
TABLE_FILENAME = "task_stability.parquet"


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def verdict_for(rewards: Sequence[float | None]) -> str:
    """Map one trial's per-run rewards to stable | flipped | errored."""
    if not rewards:
        return "errored"
    if any(reward is None for reward in rewards):
        return "errored"
    first = rewards[0]
    assert first is not None
    if any(reward != first for reward in rewards[1:]):
        return "flipped"
    return "stable"


@dataclass(frozen=True)
class StabilityRuns:
    """Parsed per-run rewards from one trial's evidence."""

    method: str
    n_runs: int
    rewards: list[float | None]
    evidence_path: str


def parse_stability_file(path: Path) -> StabilityRuns | None:
    """Parse a ``stability.json`` written by ``RepeatVerifier``.

    Returns ``None`` when the file is absent or unreadable; raises
    ``ValueError`` when it parses but has no usable ``runs`` list.
    """
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    runs = payload.get("runs")
    if not isinstance(runs, list) or not runs:
        raise ValueError(f"no usable runs in {path}")
    rewards: list[float | None] = []
    for run in runs:
        reward = run.get("reward") if isinstance(run, dict) else None
        rewards.append(float(reward) if isinstance(reward, (int, float)) else None)
    return StabilityRuns(
        method="repeat_verifier",
        n_runs=len(rewards),
        rewards=rewards,
        evidence_path=str(path),
    )


def read_trial_reward(trial_dir: Path) -> float | None:
    """Read one plain trial's reward (reward.txt, else reward.json)."""
    verifier_dir = trial_dir / "verifier"
    try:
        return float((verifier_dir / "reward.txt").read_text().strip())
    except (OSError, ValueError):
        pass
    try:
        payload = json.loads((verifier_dir / "reward.json").read_text())
    except (OSError, ValueError):
        return None
    if isinstance(payload, dict):
        raw = payload.get("reward")
        if len(payload) == 1:
            raw = next(iter(payload.values()))
        try:
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None
    return None


def read_trial_task_name(trial_dir: Path) -> str | None:
    for name in ("result.json", "lock.json", "config.json"):
        try:
            payload = json.loads((trial_dir / name).read_text())
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            for key in ("task_name", "task"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    return value
    return None


def iter_trial_dirs(job_dir: Path) -> list[Path]:
    """Trial subdirectories of one Harbor job directory."""
    if not job_dir.is_dir():
        return []
    trials = [
        child
        for child in sorted(job_dir.iterdir())
        if child.is_dir()
        and ((child / "trial.log").is_file() or (child / "verifier").is_dir())
    ]
    return trials


def load_manifest(job_dir: Path) -> dict[str, dict[str, Any]]:
    """Trial-name keyed digests written by ``run_stability_jobs`` (may be empty)."""
    try:
        payload = json.loads((job_dir / MANIFEST_FILENAME).read_text())
    except (OSError, ValueError):
        return {}
    trials = payload.get("trials") if isinstance(payload, dict) else None
    return trials if isinstance(trials, dict) else {}


def collect_trial(
    job_name: str,
    trial_dir: Path,
    *,
    backend: str,
    manifest: dict[str, dict[str, Any]],
    method_override: str | None = None,
    produced_at: str | None = None,
) -> dict[str, Any]:
    """Build one ``task_stability.parquet`` row from a finished trial directory."""
    trial_name = trial_dir.name
    entry = manifest.get(trial_name, {})
    stability = None
    if method_override != "nop_repeat":
        try:
            stability = parse_stability_file(trial_dir / "verifier" / STABILITY_FILENAME)
        except ValueError:
            stability = None
    if stability is not None:
        method = method_override or stability.method
        n_runs, rewards = stability.n_runs, stability.rewards
        evidence_path = stability.evidence_path
    else:
        method = method_override or "nop_repeat"
        rewards = [read_trial_reward(trial_dir)]
        n_runs = 1
        evidence_path = str(trial_dir / "verifier")
    return {
        "task_version_digest": entry.get("task_version_digest"),
        "harbor_digest": entry.get("harbor_digest"),
        "job_name": job_name,
        "trial_name": trial_name,
        "method": method,
        "backend": backend,
        "state_preservation": STATE_PRESERVATION[method],
        "n_runs": n_runs,
        "rewards": rewards,
        "verdict": verdict_for(rewards),
        "evidence_path": evidence_path,
        "produced_at": produced_at or utc_now_iso(),
    }


def collect_jobs(
    jobs: Sequence[Path],
    *,
    backend: str = "docker",
    method_override: str | None = None,
    produced_at: str | None = None,
) -> list[dict[str, Any]]:
    """Collect every trial of the given Harbor job directories into table rows."""
    if method_override is not None and method_override not in METHODS:
        raise ValueError(f"unknown method {method_override!r}")
    rows: list[dict[str, Any]] = []
    for job_dir in jobs:
        manifest = load_manifest(job_dir)
        for trial_dir in iter_trial_dirs(job_dir):
            rows.append(collect_trial(
                job_dir.name,
                trial_dir,
                backend=backend,
                manifest=manifest,
                method_override=method_override,
                produced_at=produced_at,
            ))
    rows.sort(key=lambda row: (row["job_name"], row["trial_name"]))
    return rows


def stability_schema() -> Any:
    import pyarrow as pa

    return pa.schema([
        ("task_version_digest", pa.string()),
        ("harbor_digest", pa.string()),
        ("job_name", pa.string()),
        ("trial_name", pa.string()),
        ("method", pa.string()),
        ("backend", pa.string()),
        ("state_preservation", pa.string()),
        ("n_runs", pa.int64()),
        ("rewards", pa.list_(pa.float64())),
        ("verdict", pa.string()),
        ("evidence_path", pa.string()),
        ("produced_at", pa.string()),
    ])


def write_task_stability_parquet(rows: Sequence[dict[str, Any]], path: Path) -> Path:
    """Write collected rows to ``task_stability.parquet`` (contract schema)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = stability_schema()
    columns = {field.name: [row.get(field.name) for row in rows] for field in schema}
    table = pa.table(columns, schema=schema)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    return path

def read_task_stability_parquet(path: Path) -> list[dict[str, Any]]:
    """Read back ``task_stability.parquet`` rows (used by tests and the CLI)."""
    import pyarrow.parquet as pq

    return pq.read_table(path).to_pylist()

def stage_task(source: Path, dest: Path) -> Path:
    """Copy a task package to a staging dir so Harbor never touches the source."""
    if not source.is_dir():
        raise ValueError(f"task directory is missing: {source}")
    if dest.exists():
        raise ValueError(f"staging destination already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest, symlinks=True)
    return dest


def task_digests(task_dir: Path) -> dict[str, str | None]:
    """Eval Lab + Harbor digests for one (staged) task package; nulls on failure."""
    from evallab.registry import harbor_task_digest, task_directory_digest

    version_digest: str | None
    try:
        version_digest = _prefixed(task_directory_digest(task_dir))
    except (OSError, ValueError):
        version_digest = None
    harbor_digest: str | None
    try:
        harbor_digest = _prefixed(harbor_task_digest(task_dir))
    except (OSError, ValueError):
        harbor_digest = None
    return {"task_version_digest": version_digest, "harbor_digest": harbor_digest}


def _prefixed(digest: str) -> str:
    """Ensure one ``sha256:`` prefix (registry helpers already include it)."""
    return digest if digest.startswith("sha256:") else f"sha256:{digest}"

def write_job_manifest(
    job_dir: Path,
    entries: dict[str, dict[str, Any]],
    *,
    produced_at: str | None = None,
) -> Path:
    """Record trial-name keyed task digests beside a stability job for collection."""
    job_dir.mkdir(parents=True, exist_ok=True)
    path = job_dir / MANIFEST_FILENAME
    path.write_text(json.dumps(
        {"schema": "evallab.stability_manifest/v1", "trials": entries,
         "produced_at": produced_at or utc_now_iso()},
        indent=2,
    ))
    return path


def stability_run_command(
    *,
    task_path: Path,
    agent: str,
    job_name: str,
    jobs_dir: Path,
    repeat_n: int = DEFAULT_REPEAT_N,
    n_concurrent: int = 1,
    repo_src: Path | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Build the ``harbor run`` argv for one nop+repeat-verification trial job.

    Returns the argv plus extra environment (``PYTHONPATH`` carries the Eval
    Lab checkout so the Harbor controller process can import the opt-in
    verifier). No model flags are ever added: ``nop``/``oracle`` are $0.
    """
    if agent not in ("nop", "oracle"):
        raise ValueError(f"stability runs are $0 controls only, got agent {agent!r}")
    argv = [
        "harbor", "run",
        "--path", str(task_path),
        "--agent", agent,
        "--job-name", job_name,
        "--jobs-dir", str(jobs_dir),
        "--n-concurrent", str(n_concurrent),
        "--n-attempts", "1",
        "--verifier", VERIFIER_IMPORT_PATH,
        "--verifier-kwarg", f"repeat_n={repeat_n}",
        "-y",
    ]
    env: dict[str, str] = {}
    if repo_src is not None:
        env["PYTHONPATH"] = str(repo_src)
    return argv, env


def run_stability_jobs(
    *,
    tasks: Sequence[Path],
    job_prefix: str,
    jobs_dir: Path,
    repo_src: Path,
    agent: str = "nop",
    repeat_n: int = DEFAULT_REPEAT_N,
    n_concurrent: int = 1,
    dry_run: bool = False,
    runner: Any | None = None,
) -> list[dict[str, Any]]:
    """Stage each task, launch one nop+repeat job per task, record the manifest.

    ``runner`` is an injectable ``(argv, env) -> CompletedProcess``-like
    collaborator (tests replace it; default is ``subprocess.run``).
    """
    run = runner or (lambda argv, env: subprocess.run(argv, check=False, env=env))
    jobs_dir.mkdir(parents=True, exist_ok=True)
    outcomes: list[dict[str, Any]] = []
    for source in tasks:
        task_slug = source.name
        job_name = f"{job_prefix}-{task_slug}"
        job_dir = jobs_dir / job_name
        staged = stage_task(source, jobs_dir / ".stage" / job_name / task_slug)
        digests = task_digests(staged)
        argv, extra_env = stability_run_command(
            task_path=staged, agent=agent, job_name=job_name, jobs_dir=jobs_dir,
            repeat_n=repeat_n, n_concurrent=n_concurrent, repo_src=repo_src,
        )
        if dry_run:
            outcomes.append({"job_name": job_name, "argv": argv, "returncode": None,
                             "staged": str(staged), "trials": []})
            continue
        import os

        env = dict(os.environ)
        env.update(extra_env)
        completed = run(argv, env)
        returncode = completed.returncode
        trials: list[dict[str, Any]] = []
        if job_dir.is_dir():
            # Manifest even on failure: partial trials still collect; digests
            # identify the exact staged bytes Harbor ran.
            trial_names = [trial.name for trial in iter_trial_dirs(job_dir)]
            write_job_manifest(job_dir, {
                trial: {**digests, "task_name": staged.name, "source": str(source)}
                for trial in trial_names
            })
            trials = summarize_job_trials(job_dir)
        outcomes.append({"job_name": job_name, "argv": argv, "returncode": returncode,
                         "staged": str(staged), "trials": trials})
    return outcomes


def summarize_job_trials(job_dir: Path) -> list[dict[str, Any]]:
    """Per-trial outcome summary (Harbor exits 0 even when a trial fails)."""
    summaries: list[dict[str, Any]] = []
    for trial_dir in iter_trial_dirs(job_dir):
        stability = trial_dir / "verifier" / STABILITY_FILENAME
        try:
            parsed = parse_stability_file(stability)
        except ValueError:
            parsed = None
        summaries.append({
            "trial": trial_dir.name,
            "has_stability": parsed is not None,
            "n_runs": parsed.n_runs if parsed else 0,
            "rewards": parsed.rewards if parsed else [read_trial_reward(trial_dir)],
            "verdict": verdict_for(parsed.rewards) if parsed
            else verdict_for([read_trial_reward(trial_dir)]),
        })
    return summaries
