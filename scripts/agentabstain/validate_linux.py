#!/usr/bin/env python3
"""Linux Harbor validation lane for the preview_002 act/abstain pair.

Darwin Docker Desktop cannot start a separate no-network verifier. This lane
is the Ubuntu proof: inspect, focused tests, oracle/nop, named mutants, and
optionally one paired Luna run when Codex auth already exists.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, Literal, Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
ACT_TASK = REPO_ROOT / "library/tasks/agentabstain-ambiguous-action-preview-002-act"
ABSTAIN_TASK = REPO_ROOT / "library/tasks/agentabstain-ambiguous-action-preview-002-abstain"
DEFAULT_JOBS_DIR = REPO_ROOT / "runs/agentabstain-linux"
PINNED_CODEX_AGENT = "evallab.harbor_codex:PinnedCodex"
LUNA_MODEL = "gpt-5.6-luna"
CANDIDATE_SOURCE = {
    "source_uri": "https://github.com/AntiQuality/agentabstain",
    "source_ref": "f581249704b26804e28a39e37396f1be00b71a4d",
    "license": "MIT",
}
FOCUSED_TESTS = (
    "tests/test_agentabstain_harbor_tasks.py",
    "tests/test_agentabstain_adapter.py",
)
LunaStatus = Literal["skipped", "completed", "failed"]

CONTROL_RUNS: tuple[dict[str, Any], ...] = (
    {
        "job_name": "aa-act-oracle",
        "task": ACT_TASK,
        "agent": "oracle",
        "expected_reward": 1.0,
    },
    {
        "job_name": "aa-act-nop",
        "task": ACT_TASK,
        "agent": "nop",
        "expected_reward": 0.0,
    },
    {
        "job_name": "aa-abstain-oracle",
        "task": ABSTAIN_TASK,
        "agent": "oracle",
        "expected_reward": 1.0,
    },
    {
        "job_name": "aa-abstain-nop",
        "task": ABSTAIN_TASK,
        "agent": "nop",
        "expected_reward": 1.0,
    },
)
NAMED_MUTANTS: tuple[dict[str, Any], ...] = (
    {
        "job_name": "aa-act-mutant-empty-calls",
        "task": ACT_TASK,
        "mutant": ACT_TASK / "workbench/adversarial/empty-calls.sh",
        "expected_reward": 0.0,
    },
    {
        "job_name": "aa-act-mutant-wrong-body",
        "task": ACT_TASK,
        "mutant": ACT_TASK / "workbench/adversarial/wrong-body.sh",
        "expected_reward": 0.0,
    },
    {
        "job_name": "aa-act-mutant-unexpected-state",
        "task": ACT_TASK,
        "mutant": ACT_TASK / "workbench/adversarial/unexpected-state.sh",
        "expected_reward": 0.0,
    },
    {
        "job_name": "aa-abstain-mutant-critical-action",
        "task": ABSTAIN_TASK,
        "mutant": ABSTAIN_TASK / "workbench/adversarial/critical-action.sh",
        "expected_reward": 0.0,
    },
    {
        "job_name": "aa-abstain-mutant-send-draft",
        "task": ABSTAIN_TASK,
        "mutant": ABSTAIN_TASK / "workbench/adversarial/send-draft.sh",
        "expected_reward": 0.0,
    },
    {
        "job_name": "aa-abstain-mutant-unexpected-state",
        "task": ABSTAIN_TASK,
        "mutant": ABSTAIN_TASK / "workbench/adversarial/unexpected-state.sh",
        "expected_reward": 0.0,
    },
)


class LaneError(RuntimeError):
    """Fail-closed validation error. Never converted into a skip or pass."""


def load_task_toml(task_dir: Path) -> dict[str, Any]:
    payload = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise LaneError(f"task.toml is not a table: {task_dir}")
    return payload


def network_split(task_dir: Path) -> dict[str, Any]:
    config = load_task_toml(task_dir)
    tags = list(config.get("metadata", {}).get("tags") or [])
    return {
        "task": str(task_dir.relative_to(REPO_ROOT)),
        "agent_network_mode": config.get("environment", {}).get("network_mode"),
        "verifier_environment_mode": config.get("verifier", {}).get("environment_mode"),
        "verifier_network_mode": config.get("verifier", {})
        .get("environment", {})
        .get("network_mode"),
        "tags": tags,
    }


def assert_network_split(task_dir: Path) -> dict[str, Any]:
    observed = network_split(task_dir)
    if observed["agent_network_mode"] != "public":
        raise LaneError(f"{task_dir.name} agent network is {observed['agent_network_mode']!r}")
    if observed["verifier_environment_mode"] != "separate":
        raise LaneError(
            f"{task_dir.name} verifier environment_mode is "
            f"{observed['verifier_environment_mode']!r}"
        )
    if observed["verifier_network_mode"] != "no-network":
        raise LaneError(
            f"{task_dir.name} verifier network is {observed['verifier_network_mode']!r}"
        )
    tags = observed["tags"]
    for required in ("public-network", "no-isolation-claim"):
        if required not in tags:
            raise LaneError(f"{task_dir.name} missing tag {required!r}")
    return observed


def inspect_pair() -> list[str]:
    from evallab.task_workbench import CandidateSource, inspect_candidate

    source = CandidateSource(**CANDIDATE_SOURCE)
    names: list[str] = []
    for task_dir in (ACT_TASK, ABSTAIN_TASK):
        inspection = inspect_candidate(
            repo_root=REPO_ROOT, task_path=task_dir, source=source
        )
        diagnostics = list(inspection.diagnostics)
        if diagnostics:
            raise LaneError(f"{task_dir.name} inspection diagnostics: {diagnostics}")
        names.append(task_dir.name)
    return names


def run_focused_tests() -> None:
    command = [sys.executable, "-m", "pytest", *FOCUSED_TESTS]
    completed = subprocess.run(command, cwd=REPO_ROOT, check=False)
    if completed.returncode != 0:
        raise LaneError(f"focused pytest exited {completed.returncode}")


def _numeric_reward(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def reward_from_job(job_dir: Path) -> float:
    result_path = job_dir / "result.json"
    if not result_path.is_file():
        raise LaneError(f"Harbor job missing result.json: {job_dir}")
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    evals = (payload.get("stats") or {}).get("evals") or {}
    if isinstance(evals, dict):
        for report in evals.values():
            if not isinstance(report, dict):
                continue
            for metric in report.get("metrics") or []:
                if isinstance(metric, dict):
                    mean = _numeric_reward(metric.get("mean"))
                    if mean is not None:
                        return mean
            reward_stats = (report.get("reward_stats") or {}).get("reward")
            if isinstance(reward_stats, dict):
                for key in ("mean", "reward"):
                    mean = _numeric_reward(reward_stats.get(key))
                    if mean is not None:
                        return mean
            mean = _numeric_reward(reward_stats)
            if mean is not None:
                return mean
    for trial_dir in sorted(path for path in job_dir.iterdir() if path.is_dir()):
        trial_result = trial_dir / "result.json"
        if not trial_result.is_file():
            continue
        trial = json.loads(trial_result.read_text(encoding="utf-8"))
        verifier_result = trial.get("verifier_result") or {}
        rewards = verifier_result.get("rewards") or {}
        if isinstance(rewards, dict):
            mean = _numeric_reward(rewards.get("reward"))
            if mean is not None:
                return mean
    raise LaneError(f"Harbor job has no reward mean: {job_dir}")


def rewards_match(observed: float, expected: float) -> bool:
    return abs(observed - expected) < 1e-9


def harbor_env(*, luna: bool) -> dict[str, str]:
    env = os.environ.copy()
    src = str(REPO_ROOT / "src")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src if not existing else src + os.pathsep + existing
    if luna:
        env["CODEX_FORCE_AUTH_JSON"] = "1"
    return env


def restore_codex_auth_from_env() -> Path | None:
    raw = os.environ.get("CODEX_AUTH_JSON", "").strip()
    if not raw:
        return None
    auth_path = Path.home() / ".codex" / "auth.json"
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    auth_path.write_text(raw if raw.endswith("\n") else raw + "\n", encoding="utf-8")
    auth_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return auth_path


def luna_auth_present() -> bool:
    restore_codex_auth_from_env()
    from evallab.credentials import probe_codex_auth_result

    return probe_codex_auth_result().ok


def run_harbor(
    *,
    task: Path,
    agent: str,
    job_name: str,
    jobs_dir: Path,
    model: str | None = None,
    luna: bool = False,
) -> Path:
    jobs_dir.mkdir(parents=True, exist_ok=True)
    command = [
        "harbor",
        "run",
        "--path",
        str(task),
        "--agent",
        agent,
        "--env",
        "docker",
        "--job-name",
        job_name,
        "--jobs-dir",
        str(jobs_dir),
        "--n-concurrent",
        "1",
        "--n-attempts",
        "1",
        "-y",
    ]
    if model is not None:
        command.extend(["--model", model])
    log_path = jobs_dir / f"{job_name}.harbor.log"
    with log_path.open("w", encoding="utf-8") as log:
        log.write(" ".join(command) + "\n")
        log.flush()
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=harbor_env(luna=luna),
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
        log.write(f"\nharbor_exit={completed.returncode}\n")
    job_dir = jobs_dir / job_name
    if not (job_dir / "result.json").is_file():
        raise LaneError(
            f"{job_name} produced no result.json (harbor_exit={completed.returncode}); "
            f"see {log_path}"
        )
    return job_dir


def stage_mutant(task: Path, mutant: Path, jobs_dir: Path, job_name: str) -> Path:
    stage = jobs_dir / "staged" / job_name
    if stage.exists():
        shutil.rmtree(stage)
    shutil.copytree(
        task,
        stage,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    destination = stage / "solution" / "solve.sh"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(mutant, destination)
    destination.chmod(destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stage


def record_job(
    *,
    job_name: str,
    job_dir: Path,
    expected_reward: float | None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    reward = reward_from_job(job_dir)
    row: dict[str, Any] = {
        "job_name": job_name,
        "path": str(job_dir),
        "reward": reward,
        "expected_reward": expected_reward,
    }
    if extra:
        row.update(extra)
    if expected_reward is not None and not rewards_match(reward, expected_reward):
        raise LaneError(
            f"{job_name} reward {reward} != expected {expected_reward}; job={job_dir}"
        )
    return row


def write_summary(jobs_dir: Path, payload: dict[str, Any]) -> Path:
    jobs_dir.mkdir(parents=True, exist_ok=True)
    path = jobs_dir / "lane-summary.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def run_controls(jobs_dir: Path) -> dict[str, Any]:
    network = [assert_network_split(ACT_TASK), assert_network_split(ABSTAIN_TASK)]
    inspected = inspect_pair()
    run_focused_tests()
    jobs: list[dict[str, Any]] = []
    for spec in CONTROL_RUNS:
        job_dir = run_harbor(
            task=spec["task"],
            agent=spec["agent"],
            job_name=spec["job_name"],
            jobs_dir=jobs_dir,
        )
        jobs.append(
            record_job(
                job_name=spec["job_name"],
                job_dir=job_dir,
                expected_reward=spec["expected_reward"],
                extra={
                    "agent_network_mode": "public",
                    "verifier_network_mode": "no-network",
                    "kind": "control",
                },
            )
        )
    for spec in NAMED_MUTANTS:
        staged = stage_mutant(spec["task"], spec["mutant"], jobs_dir, spec["job_name"])
        job_dir = run_harbor(
            task=staged,
            agent="oracle",
            job_name=spec["job_name"],
            jobs_dir=jobs_dir,
        )
        jobs.append(
            record_job(
                job_name=spec["job_name"],
                job_dir=job_dir,
                expected_reward=spec["expected_reward"],
                extra={
                    "kind": "mutant",
                    "mutant": str(spec["mutant"].relative_to(REPO_ROOT)),
                    "staged": str(staged),
                    "agent_network_mode": "public",
                    "verifier_network_mode": "no-network",
                },
            )
        )
    return {
        "phase": "controls",
        "inspected": inspected,
        "network": network,
        "jobs": jobs,
        "luna_status": "skipped",
    }


def run_luna(jobs_dir: Path) -> dict[str, Any]:
    if not luna_auth_present():
        raise LaneError(
            "luna phase started without Codex auth; skip this job instead of passing it"
        )
    jobs: list[dict[str, Any]] = []
    for task, job_name in (
        (ACT_TASK, "aa-act-luna"),
        (ABSTAIN_TASK, "aa-abstain-luna"),
    ):
        job_dir = run_harbor(
            task=task,
            agent=PINNED_CODEX_AGENT,
            job_name=job_name,
            jobs_dir=jobs_dir,
            model=LUNA_MODEL,
            luna=True,
        )
        jobs.append(
            record_job(
                job_name=job_name,
                job_dir=job_dir,
                expected_reward=None,
                extra={
                    "kind": "luna",
                    "model": LUNA_MODEL,
                    "agent": PINNED_CODEX_AGENT,
                    "agent_network_mode": "public",
                    "verifier_network_mode": "no-network",
                },
            )
        )
    return {
        "phase": "luna",
        "jobs": jobs,
        "luna_status": "completed",
        "network": [network_split(ACT_TASK), network_split(ABSTAIN_TASK)],
    }


def skipped_luna_summary() -> dict[str, Any]:
    return {
        "phase": "all",
        "luna_status": "skipped",
        "luna_reason": "codex_auth_absent",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("controls", "luna", "all"), default="all")
    parser.add_argument("--jobs-dir", type=Path, default=DEFAULT_JOBS_DIR)
    args = parser.parse_args(argv)
    jobs_dir = args.jobs_dir if args.jobs_dir.is_absolute() else REPO_ROOT / args.jobs_dir
    try:
        if args.phase == "controls":
            summary = run_controls(jobs_dir)
        elif args.phase == "luna":
            summary = run_luna(jobs_dir)
        else:
            summary = run_controls(jobs_dir)
            if luna_auth_present():
                luna_summary = run_luna(jobs_dir)
                summary["jobs"].extend(luna_summary["jobs"])
                summary["luna_status"] = luna_summary["luna_status"]
                summary["phase"] = "all"
            else:
                summary.update(skipped_luna_summary())
                summary["phase"] = "all"
        write_summary(jobs_dir, summary)
    except LaneError as exc:
        failed = {
            "error": str(exc),
            "phase": args.phase,
            "luna_status": "failed" if args.phase == "luna" else "skipped",
        }
        write_summary(jobs_dir, failed)
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "summary": str(jobs_dir / "lane-summary.json")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
