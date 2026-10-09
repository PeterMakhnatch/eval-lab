"""Closed integrity loop for lab tasks: static lint -> controls -> ladder.

For each task directory, in order:

1. Static screen (``evallab.task_lint.lint_task``): read-only findings.
2. Controls (``evallab run --agent oracle|nop``): oracle must pass (task is
   solvable), nop must fail (grader is not vacuous). Both are $0 local runs.
3. Deterministic ladder (``evallab cheat run``): the fixed model-free attack
   ladder through the official Harbor entrypoint; the benchmark's own
   verifier reward decides. $0 local Docker.

One JSONL row per task. Conclusion rule (the research discipline: a static
finding alone never counts; only verifier reward through the entrypoint does,
and only when the controls hold):

- oracle reward < 1.0        -> "broken-task"   (cheat rows meaningless)
- nop reward >= 1.0          -> "vacuous-grader" (passes doing nothing)
- any ladder attack >= 1.0   -> "cracked"
- otherwise                  -> "clean"

Usage (from the repo root, Harbor 0.24 locked extra required)::

    uv run --extra laminar python research/experiments/audit-loop/audit_loop.py \\
        --task library/tasks/event-summary \\
        --jobs-dir runs/audit-loop \\
        --out research/experiments/audit-loop/ledger.jsonl
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"--- stdout ---\n{proc.stdout[-4000:]}\n"
            f"--- stderr ---\n{proc.stderr[-4000:]}"
        )


def _reward_from_trial(trial_dir: Path) -> float | None:
    """Reward for one trial dir: reward.txt, reward.json, then result.json."""
    verifier = trial_dir / "verifier"
    txt = verifier / "reward.txt"
    if txt.is_file():
        try:
            return float(txt.read_text(encoding="utf-8").strip().split()[0])
        except (ValueError, IndexError, OSError):
            pass
    js = verifier / "reward.json"
    if js.is_file():
        try:
            payload = json.loads(js.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            payload = None
        if isinstance(payload, dict):
            ordered = ["reward"] if "reward" in payload else sorted(payload)
            for key in ordered:
                value = payload[key]
                if isinstance(value, bool):
                    continue
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
    result = trial_dir / "result.json"
    if result.is_file():
        try:
            from evallab.cheat import trial_reward

            return trial_reward(json.loads(result.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            return None
    return None


def _rewards_in_job(job_dir: Path) -> list[float]:
    """All verifier rewards recorded under a finished job directory."""
    rewards: list[float] = []
    trial_dirs = {p.parent for p in job_dir.rglob("verifier/reward.*")}
    trial_dirs |= {p.parent for p in job_dir.glob("*/result.json")}
    for trial_dir in sorted(trial_dirs):
        reward = _reward_from_trial(trial_dir)
        if reward is not None:
            rewards.append(reward)
    return rewards


def _cheat_verdicts(job_dir: Path) -> dict:
    matches = sorted(job_dir.rglob("cheat-verdicts.json"))
    if not matches:
        return {}
    return json.loads(matches[0].read_text(encoding="utf-8"))


def audit_task(task: Path, jobs_dir: Path, attempts: str = "1") -> dict:
    from evallab.task_lint import lint_task

    slug = task.name.replace("/", "_")
    task = task.resolve()
    jobs_dir.mkdir(parents=True, exist_ok=True)

    findings = lint_task(task)
    row: dict = {
        "task": str(task),
        "lint_findings": [
            {"rule": f.rule, "severity": f.severity, "detail": f.message[:300]}
            for f in findings
        ],
    }

    # Controls: oracle must pass, nop must fail.
    oracle_job = jobs_dir / f"{slug}-oracle"
    _run(
        [
            "uv",
            "run",
            "--extra",
            "laminar",
            "--no-sync",
            "evallab",
            "run",
            "--task",
            str(task),
            "--agent",
            "oracle",
            "--name",
            f"{slug}-oracle",
            "--jobs-dir",
            str(jobs_dir),
        ]
    )
    nop_job = jobs_dir / f"{slug}-nop"
    _run(
        [
            "uv",
            "run",
            "--extra",
            "laminar",
            "--no-sync",
            "evallab",
            "run",
            "--task",
            str(task),
            "--agent",
            "nop",
            "--name",
            f"{slug}-nop",
            "--jobs-dir",
            str(jobs_dir),
        ]
    )
    oracle_rewards = _rewards_in_job(oracle_job)
    nop_rewards = _rewards_in_job(nop_job)
    row["oracle_reward"] = max(oracle_rewards, default=None)
    row["nop_reward"] = max(nop_rewards, default=None)

    # Deterministic ladder through the official entrypoint.
    cheat_job = jobs_dir / f"{slug}-cheat"
    _run(
        [
            "uv",
            "run",
            "--extra",
            "laminar",
            "--no-sync",
            "evallab",
            "cheat",
            "run",
            "--task",
            str(task),
            "--name",
            f"{slug}-cheat",
            "--jobs-dir",
            str(jobs_dir),
        ]
    )
    cheat_rewards = _rewards_in_job(cheat_job)
    row["cheat_max_reward"] = max(cheat_rewards, default=None)
    row["cheat_verdicts"] = _cheat_verdicts(cheat_job)

    oracle_ok = (row["oracle_reward"] or 0.0) >= 1.0
    nop_clean = (row["nop_reward"] or 0.0) < 1.0
    cracked = (row["cheat_max_reward"] or 0.0) >= 1.0
    if not oracle_ok:
        row["conclusion"] = "broken-task"
    elif not nop_clean:
        row["conclusion"] = "vacuous-grader"
    elif cracked:
        row["conclusion"] = "cracked"
    else:
        row["conclusion"] = "clean"
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", action="append", required=True)
    parser.add_argument("--jobs-dir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    jobs_dir = Path(args.out).parent / "jobs"
    if args.jobs_dir:
        jobs_dir = Path(args.jobs_dir)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        for task in args.task:
            row = audit_task(Path(task), jobs_dir)
            handle.write(json.dumps(row) + "\n")
            print(f"{task}: conclusion={row['conclusion']} " f"oracle={row['oracle_reward']} nop={row['nop_reward']} "
                  f"cheat_max={row['cheat_max_reward']} lint={len(row['lint_findings'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
