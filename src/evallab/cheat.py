"""Cheat-audit lane: fixed attack ladder, verdicts from the verifier (HAR-204).

``evallab cheat run`` executes the deterministic model-free ``cheat`` agent
against a task on the local Docker backend through the existing
direct-execution path. Every step is $0: no provider calls, no deploys,
oracle/nop-style controls only.

Verdict rule (shared with sibling lanes through
:func:`cheat_verdict_for_reward`): ``cracked`` iff the benchmark's own
verifier returned reward >= 1.0, ``unscored`` iff no reward is extractable,
else ``clean``. The lane never asserts exploitability beyond the verifier.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from evallab.cheat_ladder import ATTACKS, parse_attack_selection
from evallab.execution_contracts import CHEAT_AGENT, CHEAT_ATTACKS_ENV_VAR

#: Serializes the process-env handoff below: build_command reads the attack
#: subset from the host environment, so concurrent in-process cheat runs must
#: not interleave their selection with another run's Harbor invocation.
_CHEAT_ENV_LOCK = threading.Lock()


ATTEMPTS_FILENAME = "attempts.json"
ATTEMPTS_DIRNAME = "cheat"
VERDICTS_FILENAME = "cheat-verdicts.json"
VERDICTS_SCHEMA = "evallab.cheat.verdicts/v1"

VERDICT_CRACKED = "cracked"
VERDICT_CLEAN = "clean"
VERDICT_UNSCORED = "unscored"


def cheat_verdict_for_reward(reward: float | None) -> str:
    """Map a verifier reward to a cheat verdict (pure; shared with grid lane)."""
    if reward is None or isinstance(reward, bool):
        return VERDICT_UNSCORED
    if not isinstance(reward, float) and not isinstance(reward, int):
        return VERDICT_UNSCORED
    if reward != reward:  # NaN: no usable signal
        return VERDICT_UNSCORED
    return VERDICT_CRACKED if reward >= 1.0 else VERDICT_CLEAN


def trial_reward(result: Mapping[str, Any]) -> float | None:
    """Extract the mean reward from one trial result.json, or None."""
    if not isinstance(result, Mapping):
        return None
    stats = result.get("stats")
    evals = stats.get("evals") if isinstance(stats, Mapping) else None
    reports = list(evals.values()) if isinstance(evals, Mapping) else []
    for report in reports:
        if not isinstance(report, Mapping):
            continue
        metrics = report.get("metrics") or []
        for metric in metrics:
            if isinstance(metric, Mapping) and "mean" in metric:
                try:
                    return float(metric["mean"])
                except (TypeError, ValueError):
                    continue
        rewards = (report.get("reward_stats") or {}).get("reward") or {}
        if isinstance(rewards, Mapping) and rewards:
            try:
                return float(next(iter(rewards.values())))
            except (TypeError, ValueError, StopIteration):
                continue
    verifier = result.get("verifier_result")
    direct = verifier.get("rewards") if isinstance(verifier, Mapping) else None
    if isinstance(direct, Mapping) and direct:
        ordered = [direct["reward"]] if "reward" in direct else list(direct.values())
        for value in ordered:
            if isinstance(value, bool):
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def collect_cheat_attempts(job_dir: Path) -> list[dict[str, Any]]:
    """Find every trial's cheat/attempts.json under a job directory.

    Harbor 0.24 nests agent logs at ``<trial>/agent/cheat/attempts.json``;
    older layouts kept ``<trial>/cheat/attempts.json``. Either nests under
    the trial directory, which is the first path component below the job.
    """
    found: list[dict[str, Any]] = []
    for attempts_path in sorted(job_dir.rglob(f"{ATTEMPTS_DIRNAME}/{ATTEMPTS_FILENAME}")):
        try:
            rel = attempts_path.relative_to(job_dir)
        except ValueError:
            continue
        if len(rel.parts) < 2:
            continue
        payload = _load_json(attempts_path)
        if payload is None:
            continue
        found.append({"trial": rel.parts[0], "path": attempts_path, "payload": payload})
    return found


def _trial_result_and_reward(trial_dir: Path) -> tuple[dict[str, Any] | None, float | None]:
    payload = _load_json(trial_dir / "result.json")
    if payload is None:
        return None, None
    return payload, trial_reward(payload)


def build_verdicts(job_dir: Path, *, harbor_rev: str | None = None) -> dict[str, Any]:
    """Build the per-trial verdict payload for a completed cheat job."""
    job_name = job_dir.name
    attempts = {entry["trial"]: entry for entry in collect_cheat_attempts(job_dir)}
    trial_dirs = sorted(
        candidate.relative_to(job_dir).as_posix()
        for candidate in job_dir.iterdir()
        if candidate.is_dir() and (candidate / "result.json").is_file()
    )
    trials: list[dict[str, Any]] = []
    for trial_rel in trial_dirs:
        trial_dir = job_dir / trial_rel
        _, reward = _trial_result_and_reward(trial_dir)
        entry = attempts.get(trial_rel)
        attacks = entry["payload"].get("attacks") if entry else None
        executed = [
            attack.get("name")
            for attack in (attacks or [])
            if isinstance(attack, dict) and attack.get("status") == "executed"
        ]
        evidence: list[str] = []
        if entry is not None:
            attempts_rel = entry["path"].relative_to(job_dir).as_posix()
            evidence.append(attempts_rel)
            attempts_dir_rel = entry["path"].parent.relative_to(job_dir).as_posix()
            for attack in attacks or []:
                if not isinstance(attack, dict):
                    continue
                for rel in attack.get("evidence") or []:
                    candidate = f"{attempts_dir_rel}/{rel}"
                    if (job_dir / candidate).is_file() and candidate not in evidence:
                        evidence.append(candidate)
        trials.append(
            {
                "trial": trial_rel,
                "verdict": cheat_verdict_for_reward(reward),
                "method": ",".join(executed) if executed else None,
                "reward": reward,
                "evidence": sorted(evidence),
            }
        )
    return {
        "schema": VERDICTS_SCHEMA,
        "job": job_name,
        "agent": CHEAT_AGENT,
        "harbor_rev": harbor_rev,
        "trials": trials,
    }


def write_cheat_verdicts(job_dir: Path, *, harbor_rev: str | None = None) -> Path:
    """Write <job_dir>/cheat-verdicts.json; sibling lanes reuse this helper."""
    payload = build_verdicts(job_dir, harbor_rev=harbor_rev)
    out = job_dir / VERDICTS_FILENAME
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return out


def harbor_version() -> str | None:
    """Best-effort Harbor CLI revision for output provenance."""
    exe = shutil.which("harbor")
    if exe is None:
        return None
    try:
        completed = subprocess.run(
            [exe, "--version"], check=False, capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (completed.stdout or completed.stderr or "").strip()
    return text or None


def _cheat_run_command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    from evallab.execution_contracts import RunRequest
    from evallab.harbor_view import installed_harbor_version
    from evallab.queue import Executor

    version = installed_harbor_version()
    if version is None or version < (0, 24):
        print(
            "error: evallab cheat run needs Harbor >= 0.24 on PATH "
            "(the cheat agent imports harbor.agents.capabilities); "
            "install the locked laminar extra: uv sync --frozen --extra laminar",
            file=sys.stderr,
        )
        return 2
    selected = parse_attack_selection(args.attacks)
    task = args.task if args.task.is_absolute() else (root / args.task).resolve()
    jobs_dir = args.jobs_dir if args.jobs_dir.is_absolute() else (root / args.jobs_dir).resolve()
    request = RunRequest(
        task=task,
        agent=CHEAT_AGENT,
        name=args.name,
        jobs_dir=jobs_dir,
        environment="docker",
        model=None,
        concurrency=1,
        attempts=args.attempts,
        timeout_seconds=args.timeout_seconds,
        allow_billable=False,
    )
    with _CHEAT_ENV_LOCK:
        previous = os.environ.get(CHEAT_ATTACKS_ENV_VAR)
        os.environ[CHEAT_ATTACKS_ENV_VAR] = ",".join(selected)
        try:
            job_dir = Executor.from_repo(root).execute_direct(request)
        finally:
            if previous is None:
                os.environ.pop(CHEAT_ATTACKS_ENV_VAR, None)
            else:
                os.environ[CHEAT_ATTACKS_ENV_VAR] = previous
    verdicts_path = write_cheat_verdicts(job_dir, harbor_rev=harbor_version())
    payload = json.loads(verdicts_path.read_text(encoding="utf-8"))
    print(f"completed: {job_dir}")
    for trial in payload["trials"]:
        method = trial["method"] or "none"
        print(f"{trial['trial']}: {trial['verdict']} (method={method} reward={trial['reward']})")
    print(f"verdicts: {verdicts_path}")
    print(json.dumps(payload, indent=2))
    return 0


def build_cheat_parser(commands: argparse._SubParsersAction) -> None:
    """Register the ``evallab cheat`` subcommand (one self-contained block)."""
    cheat = commands.add_parser(
        "cheat",
        help="Deterministic cheat audit on local Docker ($0, model-free)",
        description=__doc__.split("\n\n")[0] if __doc__ else "cheat audit",
    )
    sub = cheat.add_subparsers(dest="cheat_cmd", required=True)
    runner = sub.add_parser("run", help="Run the fixed attack ladder against one task")
    runner.add_argument("--task", type=Path, required=True, help="Task directory")
    runner.add_argument("--name", required=True, help="Harbor job name")
    runner.add_argument("--jobs-dir", type=Path, default=Path("runs"))
    runner.add_argument("--attempts", type=int, default=1)
    runner.add_argument(
        "--attacks",
        default=None,
        help=f"comma-separated subset of {','.join(ATTACKS)}; default: full ladder",
    )
    runner.add_argument(
        "--timeout-seconds",
        type=int,
        default=600,
        help="executor wall-clock allowance per attempted trial",
    )
    runner.set_defaults(func=_cheat_run_command)
